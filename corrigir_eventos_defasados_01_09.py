"""
Correção pontual, 2 fases (verificar / aplicar), pros 3 jogos em que o
diagnóstico de 01/09/2026 confirmou evento defasado no banco (comparado
direto contra a resposta ATUAL de /fixtures/events da API-Football):

  1492352  Bahia 3x2 Internacional      -> banco tinha 2 cartões, API tem 6
  1492356  Mirassol 1x1 Palmeiras       -> banco tinha 4 cartões, API tem 5
  1492358  São Paulo 2x1 RB Bragantino  -> banco tinha 6 cartões (1 errado:
                                            "Pedro Henrique" 83', que não
                                            existe mais na API), API tem 7

NÃO inclui Corinthians x Santos - nesse jogo o banco já bate 6/6 com a API-
Football; a diferença pro Sofascore (4 cartões do Corinthians vs 3 da API)
é a própria API-Football não tendo esse cartão, não é defasagem do banco.
Rebuscar não vai mudar nada ali.

Como cada jogo real tem 2 linhas em `jogos` (uma por perspectiva, mesmo
fixture_id_api), esse script corrige as DUAS.

Uso:
  python corrigir_eventos_defasados_01_09.py verificar
  python corrigir_eventos_defasados_01_09.py aplicar
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

FIXTURES_A_CORRIGIR = [1492352, 1492356, 1492358]


def cabecalho(fase):
    print("=" * 78)
    print(f"corrigir_eventos_defasados_01_09.py | fase: {fase}")
    print(f"argv: {sys.argv}")
    print(f"relógio: {datetime.now().isoformat()}")
    print("=" * 78)


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
    """Mesma lógica de identidade do popular_banco.py - duplicada aqui de
    propósito, pra esse script pontual não depender de importar o outro
    arquivo (baixo risco, é utilitário pequeno e autocontido)."""
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
                cur.execute("UPDATE jogadores SET api_football_id = %s WHERE id = %s", (api_football_id, jogador_id))
            return jogador_id
        cur.execute("INSERT INTO jogadores (nome, api_football_id) VALUES (%s, %s) RETURNING id", (nome, api_football_id))
        return cur.fetchone()[0]

    cur.execute("SELECT id FROM jogadores WHERE nome = %s", (nome,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO jogadores (nome) VALUES (%s) RETURNING id", (nome,))
    return cur.fetchone()[0]


def salvar_eventos(cur, jogo_id, eventos, nosso_time_api_id):
    """Cópia fiel de salvar_eventos() do popular_banco.py."""
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
            # ver comentário equivalente em popular_banco.py: pênalti perdido
            # vem como type="Goal", detail="Missed Penalty" - não é gol.
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


def fase_verificar():
    cabecalho("verificar (só leitura, nenhuma escrita)")
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    for fixture_id in FIXTURES_A_CORRIGIR:
        print(f"\n### Fixture {fixture_id}")
        cur.execute(
            "SELECT id, nosso_time_id, (SELECT nome FROM times WHERE id = nosso_time_id) "
            "FROM jogos WHERE fixture_id_api = %s",
            (fixture_id,),
        )
        linhas = cur.fetchall()
        if not linhas:
            print("  !! nenhuma linha encontrada em `jogos` pra esse fixture - não deveria acontecer.")
            continue

        eventos_api = buscar_eventos(fixture_id)
        cartoes_api = sum(1 for ev in eventos_api if ev.get("type") == "Card")
        gols_api = sum(
            1 for ev in eventos_api
            if ev.get("type") == "Goal" and ev.get("detail") != "Missed Penalty"
        )
        penaltis_perdidos_api = sum(
            1 for ev in eventos_api
            if ev.get("type") == "Goal" and ev.get("detail") == "Missed Penalty"
        )
        subs_api = sum(1 for ev in eventos_api if ev.get("type") == "subst")
        print(f"  API agora: {gols_api} gols, {cartoes_api} cartões, {subs_api} substituições"
              f"{f' ({penaltis_perdidos_api} pênalti(s) perdido(s), corretamente excluído(s) do gols)' if penaltis_perdidos_api else ''}.")

        for jogo_id, nosso_time_id, nosso_time_nome in linhas:
            gols_db, cartoes_db, subs_db = contar_atual(cur, jogo_id)
            print(f"  jogo_id {jogo_id} ({nosso_time_nome}): banco tem {gols_db} gols, "
                  f"{cartoes_db} cartões, {subs_db} substituições "
                  f"{'-> VAI SER REGRAVADO' if (gols_db, cartoes_db, subs_db) != (gols_api, cartoes_api, subs_api) else '(já bate, sem mudança)'}")
        time.sleep(2)

    cur.close()
    conn.close()
    print("\nFase verificar concluída. Nenhuma escrita foi feita.")
    print("Rode com 'aplicar' pra gravar de verdade.")


def fase_aplicar():
    cabecalho("aplicar (vai apagar e regravar gols/cartões/substituições)")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        for fixture_id in FIXTURES_A_CORRIGIR:
            print(f"\n### Fixture {fixture_id}")
            cur.execute(
                "SELECT id, nosso_time_id, "
                "(SELECT api_football_team_id FROM times WHERE id = nosso_time_id), "
                "(SELECT nome FROM times WHERE id = nosso_time_id) "
                "FROM jogos WHERE fixture_id_api = %s",
                (fixture_id,),
            )
            linhas = cur.fetchall()
            if not linhas:
                print("  !! nenhuma linha encontrada em `jogos` pra esse fixture - pulando.")
                continue

            eventos_api = buscar_eventos(fixture_id)

            for jogo_id, nosso_time_id, nosso_time_api_id, nosso_time_nome in linhas:
                cur.execute("DELETE FROM gols WHERE jogo_id = %s", (jogo_id,))
                cur.execute("DELETE FROM cartoes WHERE jogo_id = %s", (jogo_id,))
                cur.execute("DELETE FROM substituicoes WHERE jogo_id = %s", (jogo_id,))
                contagem = salvar_eventos(cur, jogo_id, eventos_api, nosso_time_api_id)
                print(f"  jogo_id {jogo_id} ({nosso_time_nome}): regravado -> "
                      f"{contagem['gols']} gols, {contagem['cartoes']} cartões, "
                      f"{contagem['substituicoes']} substituições "
                      f"({contagem['ignorados']} eventos ignorados).")

            conn.commit()
            time.sleep(2)

        print("\nFase aplicar concluída e commitada.")
        print("Lembrete: isso muda `cartoes`/`gols` de jogos já ARQUIVADOS em "
              "`historico_recomendacoes` - considerar rodar `reabrir_apostas_desatualizadas.py` "
              "se houver aposta salva resolvida com base nesse dado (regra da seção 37-B).")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a aplicação, rollback feito: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("verificar", "aplicar"):
        print("Uso: python corrigir_eventos_defasados_01_09.py [verificar|aplicar]")
        sys.exit(1)

    if sys.argv[1] == "verificar":
        fase_verificar()
    else:
        fase_aplicar()
