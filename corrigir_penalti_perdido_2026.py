"""
Correção do PÊNALTI PERDIDO - fase `aplicar`, pros 8 fixtures de 2026 que
a auditoria (`auditar_penalti_perdido.py verificar --temporada 2026`,
02/09/2026) encontrou contaminados.

O BUG (ver auditar_penalti_perdido.py pro diagnóstico completo):
`salvar_eventos()` inseria linha em `gols` pra qualquer `type == "Goal"`,
sem checar o `detail`. A API-Football usa `type="Goal"` também pra pênalti
PERDIDO (`detail="Missed Penalty"`), que não é gol. Já corrigido na raiz em
popular_banco.py (01/09/2026) - este script limpa o que ficou gravado antes.

Resultado da auditoria de 2026: 380 fixtures varridos, 9 tiveram pênalti
perdido, 8 estavam contaminados. O 9º (1492357, Remo x Coritiba) já estava
certo - foi regravado depois da correção da raiz, o que confirma que ela
funciona em produção.

Caso mais chamativo: fixture 1492206 (Coritiba, 05/04/2026) - a API diz 0
gols, o banco tinha 1. O jogo terminou 0x0 e o banco achava que teve gol.

COMO CORRIGE: rebusca /fixtures/events e regrava gols/cartões/substituições
das DUAS perspectivas, usando a mesma `salvar_eventos` já corrigida (que
agora pula `Missed Penalty` e evento sem jogador). Mesmo padrão do
corrigir_eventos_defasados_01_09.py.

⚠️ NÃO cobre 2022-2025. A auditoria dessas temporadas ainda não rodou
(estimativa: ~20 fixtures contaminados, ~2h de execução). Quando rodar, é
só trocar a lista FIXTURES_A_CORRIGIR abaixo.

DEPOIS DE RODAR: os padrões derivados de `gols` ficam desatualizados
(Marca em Ambos os Tempos, Dupla Chance por tempo, gols por período).
Rodar `motor_padroes.py` em seguida - e ver se alguma linha de
`historico_recomendacoes` desses jogos muda de resultado
(reavaliar_historico_eventos_corrigidos.py, trocando os jogo_id).

Uso:
  python corrigir_penalti_perdido_2026.py verificar
  python corrigir_penalti_perdido_2026.py aplicar
"""

import os
import sys
import time
from datetime import datetime

import requests
import psycopg2

API_KEY = os.environ["API_FOOTBALL_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

FIXTURES_A_CORRIGIR = [
    1492120,  # 19/02/2026 - Julimar 16'      | API 1 gol,  banco tinha 2
    1492133,  # 12/02/2026 - Isidro Pitta 59' | API 2 gols, banco tinha 3
    1492152,  # 11/03/2026 - Willian José 5'  | API 2 gols, banco tinha 3
    1492186,  # 22/03/2026 - Luciano Juba 57' | API 5 gols, banco tinha 6
    1492206,  # 05/04/2026 - Alef Manga 42'   | API 0 gols, banco tinha 1 (!)
    1492264,  # 17/05/2026 - Eduardo Sasha 89'| API 2 gols, banco tinha 3
    1492278,  # 24/05/2026 - Eduardo Sasha 87'| API 3 gols, banco tinha 4
    1492286,  # 31/05/2026 - Yannick Bolasie 90' | API 1 gol, banco tinha 2
]

SEGUNDOS_ENTRE_REQUISICOES = 7


def cabecalho(fase):
    print("=" * 78)
    print(f"corrigir_penalti_perdido_2026.py | fase: {fase}")
    print(f"argv: {sys.argv}")
    print(f"relógio: {datetime.now().isoformat()}")
    print("=" * 78)
    sys.stdout.flush()


def buscar_eventos(fixture_id):
    resp = requests.get(
        f"{API_BASE}/fixtures/events",
        headers=HEADERS,
        params={"fixture": fixture_id},
    )
    resp.raise_for_status()
    dados = resp.json()
    if dados.get("errors"):
        print(f"  !! aviso da API: {dados['errors']}")
    return dados["response"]


def get_or_create_jogador(cur, api_football_id, nome):
    """Mesma lógica de identidade do popular_banco.py."""
    if api_football_id is not None:
        cur.execute("SELECT id FROM jogadores WHERE api_football_id = %s", (api_football_id,))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute("SELECT id, api_football_id FROM jogadores WHERE nome = %s", (nome,))
        row = cur.fetchone()
        if row:
            jogador_id, id_existente = row
            if id_existente is None:
                cur.execute("UPDATE jogadores SET api_football_id = %s WHERE id = %s",
                            (api_football_id, jogador_id))
            return jogador_id
        cur.execute("INSERT INTO jogadores (nome, api_football_id) VALUES (%s, %s) RETURNING id",
                    (nome, api_football_id))
        return cur.fetchone()[0]

    cur.execute("SELECT id FROM jogadores WHERE nome = %s", (nome,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO jogadores (nome) VALUES (%s) RETURNING id", (nome,))
    return cur.fetchone()[0]


def salvar_eventos(cur, jogo_id, eventos, nosso_time_api_id):
    """Cópia da salvar_eventos() de popular_banco.py JÁ CORRIGIDA - pula
    `Missed Penalty` e evento sem nome de jogador."""
    contagem = {"gols": 0, "cartoes": 0, "substituicoes": 0, "ignorados": 0}
    for ev in eventos:
        tipo = ev["type"]
        minuto = ev["time"]["elapsed"]
        lado = "mandante" if ev["team"]["id"] == nosso_time_api_id else "visitante"
        jogador_nome = ev["player"]["name"] if ev["player"]["name"] else None
        jogador_api_id = ev["player"].get("id") if ev.get("player") else None

        if not jogador_nome:
            contagem["ignorados"] += 1
            continue

        jogador_id = get_or_create_jogador(cur, jogador_api_id, jogador_nome)
        periodo = "1_tempo" if minuto <= 45 else "2_tempo"

        if tipo == "Goal":
            # É ISTO que este script existe pra corrigir: pênalti perdido
            # vem como type="Goal" e NÃO é gol.
            if ev.get("detail") == "Missed Penalty":
                contagem["ignorados"] += 1
                continue
            cur.execute(
                """INSERT INTO gols (jogo_id, jogador_id, lado, minuto, periodo, penalti, gol_contra)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (jogo_id, jogador_id, lado, minuto, periodo,
                 ev.get("detail") == "Penalty", ev.get("detail") == "Own Goal"),
            )
            contagem["gols"] += 1
        elif tipo == "Card":
            cor = "amarelo" if "Yellow" in ev.get("detail", "") else "vermelho"
            cur.execute(
                """INSERT INTO cartoes (jogo_id, jogador_id, lado, cor, minuto, periodo)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (jogo_id, jogador_id, lado, cor, minuto, periodo),
            )
            contagem["cartoes"] += 1
        elif tipo == "subst":
            jogador_entrou_nome = ev["assist"]["name"] if ev.get("assist") else None
            jogador_entrou_api_id = ev["assist"].get("id") if ev.get("assist") else None
            jogador_entrou_id = (
                get_or_create_jogador(cur, jogador_entrou_api_id, jogador_entrou_nome)
                if jogador_entrou_nome else None
            )
            cur.execute(
                """INSERT INTO substituicoes (jogo_id, jogador_saiu_id, jogador_entrou_id, lado, minuto, periodo)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (jogo_id, jogador_id, jogador_entrou_id, lado, minuto, periodo),
            )
            contagem["substituicoes"] += 1
        else:
            contagem["ignorados"] += 1

    return contagem


def contar_atual(cur, jogo_id):
    cur.execute("SELECT COUNT(*) FROM gols WHERE jogo_id = %s", (jogo_id,))
    gols = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM cartoes WHERE jogo_id = %s", (jogo_id,))
    cartoes = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM substituicoes WHERE jogo_id = %s", (jogo_id,))
    subs = cur.fetchone()[0]
    return gols, cartoes, subs


def linhas_do_fixture(cur, fixture_id):
    cur.execute(
        """SELECT j.id, j.nosso_time_id,
                  (SELECT api_football_team_id FROM times WHERE id = j.nosso_time_id),
                  (SELECT nome FROM times WHERE id = j.nosso_time_id),
                  j.data_jogo
           FROM jogos j WHERE j.fixture_id_api = %s""",
        (fixture_id,),
    )
    return cur.fetchall()


def fase_verificar():
    cabecalho("verificar (só leitura, nenhuma escrita)")
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    for fixture_id in FIXTURES_A_CORRIGIR:
        print(f"\n### Fixture {fixture_id}")
        linhas = linhas_do_fixture(cur, fixture_id)
        if not linhas:
            print("  !! nenhuma linha em `jogos` - não deveria acontecer.")
            continue

        eventos = buscar_eventos(fixture_id)
        gols_api = sum(
            1 for ev in eventos
            if ev.get("type") == "Goal"
            and ev.get("detail") != "Missed Penalty"
            and (ev.get("player") or {}).get("name")
        )
        cartoes_api = sum(1 for ev in eventos if ev.get("type") == "Card")
        subs_api = sum(1 for ev in eventos if ev.get("type") == "subst")
        perdidos = [ev for ev in eventos
                    if ev.get("type") == "Goal" and ev.get("detail") == "Missed Penalty"]

        print(f"  API: {gols_api} gols reais, {cartoes_api} cartões, {subs_api} subs "
              f"({len(perdidos)} pênalti perdido, será excluído)")

        for jogo_id, _tid, _apiid, nome, data_jogo in linhas:
            g, c, s = contar_atual(cur, jogo_id)
            muda = (g, c, s) != (gols_api, cartoes_api, subs_api)
            print(f"  jogo_id {jogo_id} ({nome}, {data_jogo}): banco tem {g} gols, {c} cartões, "
                  f"{s} subs {'-> VAI SER REGRAVADO' if muda else '(já bate)'}")

        time.sleep(SEGUNDOS_ENTRE_REQUISICOES)

    cur.close()
    conn.close()
    print("\nFase verificar concluída. Nenhuma escrita foi feita.")


def fase_aplicar():
    cabecalho("aplicar (vai apagar e regravar gols/cartões/substituições)")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        for fixture_id in FIXTURES_A_CORRIGIR:
            print(f"\n### Fixture {fixture_id}")
            linhas = linhas_do_fixture(cur, fixture_id)
            if not linhas:
                print("  !! nenhuma linha em `jogos` - pulando.")
                continue

            eventos = buscar_eventos(fixture_id)

            for jogo_id, _tid, nosso_time_api_id, nome, data_jogo in linhas:
                cur.execute("DELETE FROM gols WHERE jogo_id = %s", (jogo_id,))
                cur.execute("DELETE FROM cartoes WHERE jogo_id = %s", (jogo_id,))
                cur.execute("DELETE FROM substituicoes WHERE jogo_id = %s", (jogo_id,))
                contagem = salvar_eventos(cur, jogo_id, eventos, nosso_time_api_id)
                print(f"  jogo_id {jogo_id} ({nome}): regravado -> {contagem['gols']} gols, "
                      f"{contagem['cartoes']} cartões, {contagem['substituicoes']} subs "
                      f"({contagem['ignorados']} ignorados).")

            conn.commit()
            time.sleep(SEGUNDOS_ENTRE_REQUISICOES)

        print("\nFase aplicar concluída e commitada.")
        print("\nPRÓXIMOS PASSOS (a correção não se propaga sozinha):")
        print("  1. rodar motor_padroes.py - os padrões derivados de `gols` estão")
        print("     desatualizados (Marca em Ambos os Tempos, Dupla Chance por tempo)")
        print("  2. checar se alguma linha de historico_recomendacoes desses jogos")
        print("     muda de resultado (reavaliar_historico_eventos_corrigidos.py)")
        print("  3. checar apostas_salvas resolvidas nesses jogos")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a aplicação, rollback feito: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("verificar", "aplicar"):
        print("Uso: python corrigir_penalti_perdido_2026.py [verificar|aplicar]")
        sys.exit(1)

    if sys.argv[1] == "verificar":
        fase_verificar()
    else:
        fase_aplicar()
