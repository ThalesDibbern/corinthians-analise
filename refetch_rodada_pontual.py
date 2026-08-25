"""
Script PONTUAL (não faz parte do encadeamento diário do cron) - rebusca na
API-Football o dado de jogos específicos que a auditoria apontou como
divergente da fonte externa (Sofascore/ge), e reavalia o que dependia
deles.

MOTIVAÇÃO (auditoria da rodada de 22-24/08/2026, fase 1):
    Dois jogos divergiram do print real, cada um por um motivo diferente:

    1) Fluminense x Remo (22/08) - ESCANTEIO parcial
       Banco: Fluminense 4, Remo ~2, total <= 7
       Real:  Fluminense 11, Remo 2, total 13
       Placar (2x1), gols por tempo e cartões do mesmo jogo estão certos.
       Só a estatística de time está errada - assinatura exata do bug de
       "estatística coletada no meio do jogo": o placar vem de /fixtures
       (confiável assim que o fixture fecha em FT), o escanteio vem de
       /fixtures/statistics (pode vir parcial e se corrigir dias depois).
       10 recomendações foram avaliadas em cima disso, todas como falso
       positivo.

       ATUALIZADO 25/08: a fase `verificar` confirmou que a API já se
       corrigiu (11/2 escanteios, 10/10 faltas, 20/7 finalizações, todos
       batendo com a fonte externa). Alvo ATIVO.

       Observação que não aparecia na auditoria: as FALTAS desse jogo
       também estavam pela metade (10 no banco contra 20 reais). Não
       existe mercado apostável de falta por jogo, então nenhuma
       recomendação foi avaliada errada por causa disso - mas falta é
       input do fator de correlação faltas->cartões, então esse jogo
       vinha entrando torto no cálculo.

    2) Vitória x Bahia (23/08) - CARTÃO faltando
       Banco: Vitória 3, Bahia 2, total 5
       Real:  Vitória 4, Bahia 3, total 7
       Um cartão faltando de cada lado. Não é troca de lado (nenhuma
       combinação de troca fecha os dois números) - é evento ausente.
       Jogo teve gol aos 90+7', então cartão tardio é plausível.

LACUNA ESTRUTURAL QUE ESTE SCRIPT EXPÕE:
    ATUALIZADO 25/08 - a janela de 6h também é CURTA DEMAIS pra
    estatística: o jogo Fluminense x Remo foi na noite de 22/08 e o cron
    rodou 23/08 às 9h, mais de 12h depois (portanto FORA da janela), e
    mesmo assim a API ainda devolvia número de meio de jogo naquele
    momento. O atraso real da API-Football se mede em DIAS, não em horas.
    Aumentar a janela é decisão separada, fora do escopo deste script.

    Em `popular_banco.py` o rebusca de EVENTO é decidido por

        falta_eventos = not jogo_ja_processado(cur, jogo_id)

    ou seja: evento é buscado UMA vez e nunca mais. A janela de espera de
    6h (`JANELA_ESPERA_ESTATISTICA_HORAS`) força rebusca de ESTATÍSTICA de
    time e de jogador, mas NÃO de evento. Um cartão registrado tarde pela
    API nunca entra sozinho no banco.

    Este script resolve os casos pontuais. A correção estrutural (aplicar
    a janela de espera também aos eventos) fica como decisão separada - é
    mudança em `popular_banco.py`, e mudar coleta no meio de uma auditoria
    mistura duas fronteiras no histórico.

TRÊS FASES SEPARADAS (rodar uma de cada vez, de propósito):

  FASE 1 (`verificar`) - NÃO GRAVA NADA NO BANCO:
    Resolve os fixtures a partir dos nomes/datas configurados, mostra o
    que está salvo hoje E o que a API devolve AGORA, lado a lado. Essa é
    a pergunta que importa antes de qualquer coisa: "a API já se
    corrigiu?". Se ela ainda devolver o número parcial, não adianta
    aplicar - o certo é esperar mais um dia ou partir pra correção manual
    (mesmo caminho de `corrigir_manual_athletico_bragantino.py`).
    Consome cota de API (1 chamada por fixture por tipo de dado).

  FASE 2 (`aplicar`):
    Apaga só o que vai ser regravado e salva o dado novo em CADA jogo_id
    ligado ao fixture (um confronto entre 2 times rastreados tem 2 linhas
    em `jogos`, uma por perspectiva). Imprime antes/depois.

  FASE 3 (`reavaliar`):
    Reavalia via `avaliacao.avaliar_resultado` (a mesma função de sempre)
    toda linha de `historico_recomendacoes` desses jogos, gravando só
    onde mudou.

ESCOPO DELIBERADAMENTE ESTREITO:
    - "estatisticas" mexe em `estatisticas_jogo` e
      `jogador_estatisticas_jogo`.
    - "cartoes" mexe SÓ na tabela `cartoes`. NÃO apaga nem regrava `gols`
      e `substituicoes`, mesmo eles vindo do mesmo endpoint - um script
      destrutivo que mexe em duas coisas não serve quando só uma precisa
      ser refeita, e o placar/gols desses jogos já foi conferido como
      correto na auditoria.
    - Nenhuma aposta real, banca ou ROI é tocada. Se alguma aposta tiver
      perna desses jogos, ela é resolvida pelo caminho normal do app
      (casamento contra `historico_recomendacoes`), depois da fase 3.

Rodar:
    python refetch_rodada_pontual.py verificar
    ... conferir a coluna "API AGORA" contra o print do Sofascore ...
    python refetch_rodada_pontual.py aplicar
    python refetch_rodada_pontual.py reavaliar

Variáveis de ambiente necessárias: API_FOOTBALL_KEY, DATABASE_URL
"""

import os
import sys
import psycopg2

from popular_banco import (
    buscar_eventos,
    buscar_estatisticas,
    buscar_estatisticas_jogadores,
    salvar_estatisticas,
    salvar_estatisticas_jogadores,
)
from avaliacao import avaliar_resultado

DATABASE_URL = os.environ["DATABASE_URL"]

# Alvos da correção. Resolvidos por data + nomes de time (com ILIKE), pra
# não precisar decorar fixture_id. O script ABORTA se um alvo resolver pra
# zero ou mais de um fixture - nome ambíguo não pode virar correção
# silenciosa no banco.
#
# `refazer` aceita "estatisticas" e/ou "cartoes".
ALVOS = [
    {
        "ativo": True,
        "rotulo": "Fluminense x Remo (22/08) - escanteio parcial",
        "data": "2026-08-22",
        "mandante": "Fluminense",
        "visitante": "Remo",
        "refazer": ["estatisticas"],
        "esperado": "Fluminense 11 escanteios, Remo 2, total 13",
    },
    {
        # DESATIVADO em 25/08/2026 depois da fase `verificar`.
        #
        # A API-Football AINDA devolve os mesmos 5 cartões que já estão no
        # banco (Vitória 26' e 90'; Bahia 42', 78' e 90'), enquanto a fonte
        # externa mostra Vitória 4 e Bahia 3 - ou seja, faltam 2 cartões do
        # VITÓRIA, e o lado do Bahia já está correto.
        #
        # Aplicar agora só apagaria e regravaria exatamente o mesmo dado,
        # gastando cota e criando escrita sem ganho. O caso Athletico-PR x
        # Bragantino mostrou que a API pode levar ~3 dias pra agregar - a
        # decisão é REVERIFICAR em 27/08 (antes de a coleta de odds da
        # rodada seguinte começar) e, se ainda não tiver corrigido, partir
        # pra correção manual no formato de
        # `corrigir_manual_athletico_bragantino.py`.
        #
        # Pra religar: trocar "ativo" pra True e rodar `verificar` de novo
        # ANTES de `aplicar`.
        "ativo": False,
        "rotulo": "Vitória x Bahia (23/08) - 2 cartões do Vitória faltando",
        "data": "2026-08-23",
        "mandante": "Vit",
        "visitante": "Bahia",
        "refazer": ["cartoes"],
        "esperado": "Vitória 4 cartões, Bahia 3, total 7",
    },
]

# `verificar` mostra TODOS os alvos (inclusive os desativados - a graça é
# justamente poder reconferir se a API já se corrigiu). `aplicar` e
# `reavaliar` só tocam nos ativos.
def alvos_ativos():
    return [a for a in ALVOS if a.get("ativo", True)]



# ---------- resolução dos alvos ----------

def resolver_alvo(cur, alvo):
    """Descobre fixture_id_api e todos os jogo_id daquele confronto real.
    Aborta se ficar ambíguo."""
    cur.execute(
        """
        SELECT DISTINCT j.fixture_id_api
        FROM jogos j
        JOIN times m ON m.id = j.mandante_id
        JOIN times v ON v.id = j.visitante_id
        WHERE j.data_jogo = %s
          AND m.nome ILIKE %s
          AND v.nome ILIKE %s
        """,
        (alvo["data"], f"%{alvo['mandante']}%", f"%{alvo['visitante']}%"),
    )
    fixtures = [r[0] for r in cur.fetchall()]
    if len(fixtures) != 1:
        raise RuntimeError(
            f"Alvo '{alvo['rotulo']}' resolveu pra {len(fixtures)} fixture(s): "
            f"{fixtures}. Ajuste os nomes/data em ALVOS antes de continuar."
        )
    fixture_id = fixtures[0]

    cur.execute(
        """
        SELECT j.id, t.nome, j.mandante
        FROM jogos j LEFT JOIN times t ON t.id = j.nosso_time_id
        WHERE j.fixture_id_api = %s ORDER BY j.id
        """,
        (fixture_id,),
    )
    perspectivas = cur.fetchall()
    return fixture_id, perspectivas


def buscar_home_team_id(cur, jogo_id):
    """api_football_team_id do MANDANTE REAL desse jogo - é o que
    salvar_estatisticas usa pra decidir o `lado` (que ali significa mando
    real, diferente de `cartoes`)."""
    cur.execute(
        """
        SELECT tm.api_football_team_id
        FROM jogos j JOIN times tm ON tm.id = j.mandante_id
        WHERE j.id = %s
        """,
        (jogo_id,),
    )
    row = cur.fetchone()
    if row is None or row[0] is None:
        raise RuntimeError(f"Sem api_football_team_id do mandante do jogo_id {jogo_id}.")
    return row[0]


def buscar_nosso_time_api_id(cur, jogo_id):
    """api_football_team_id do NOSSO time nessa perspectiva - é o que
    salvar_eventos usa pra decidir o `lado` em `cartoes` ('mandante' =
    nosso time). Ver o bug corrigido em avaliacao.py em 25/08/2026."""
    cur.execute(
        """
        SELECT tm.api_football_team_id
        FROM jogos j JOIN times tm ON tm.id = j.nosso_time_id
        WHERE j.id = %s
        """,
        (jogo_id,),
    )
    row = cur.fetchone()
    if row is None or row[0] is None:
        raise RuntimeError(f"Sem api_football_team_id do nosso time do jogo_id {jogo_id}.")
    return row[0]


# ---------- leitura do que está salvo ----------

def estatisticas_salvas(cur, jogo_id):
    cur.execute(
        """SELECT lado, escanteios, faltas, finalizacoes, posse_de_bola
           FROM estatisticas_jogo WHERE jogo_id = %s ORDER BY lado""",
        (jogo_id,),
    )
    return cur.fetchall()


def cartoes_salvos(cur, jogo_id):
    cur.execute(
        """SELECT lado, cor, minuto FROM cartoes WHERE jogo_id = %s
           ORDER BY minuto NULLS LAST, id""",
        (jogo_id,),
    )
    return cur.fetchall()


# ---------- leitura do que a API devolve agora ----------

def resumir_estatisticas_api(estatisticas, home_team_id):
    """Mesma tradução de lado que salvar_estatisticas faz, só que sem
    gravar - pra imprimir comparável com o que está no banco."""
    saida = []
    for bloco in estatisticas:
        team_id = bloco["team"]["id"]
        lado = "mandante" if team_id == home_team_id else "visitante"
        valores = {item["type"]: item["value"] for item in bloco["statistics"]}
        saida.append((lado, valores.get("Corner Kicks"), valores.get("Fouls"),
                      valores.get("Total Shots"), valores.get("Ball Possession")))
    return sorted(saida)


def resumir_cartoes_api(eventos, nosso_time_api_id):
    """Mesma classificação de salvar_eventos: 'mandante' = NOSSO time."""
    saida = []
    for ev in eventos:
        if ev.get("type") != "Card":
            continue
        lado = "mandante" if ev["team"]["id"] == nosso_time_api_id else "visitante"
        cor = "amarelo" if "Yellow" in (ev.get("detail") or "") else "vermelho"
        saida.append((lado, cor, ev["time"]["elapsed"]))
    return sorted(saida, key=lambda x: (x[2] if x[2] is not None else 999))


def salvar_cartoes(cur, jogo_id, eventos, nosso_time_api_id):
    """Insere SÓ os cartões, com a mesma convenção de `lado` e `periodo`
    de popular_banco.salvar_eventos. Não toca em gols/substituições de
    propósito - eles já foram conferidos como corretos e não precisam ser
    refeitos.

    Jogador desconhecido é criado via get_or_create_jogador, o mesmo
    caminho do coletor - casamento por api_football_id, não por nome."""
    from popular_banco import get_or_create_jogador

    salvos = 0
    ignorados = 0
    for ev in eventos:
        if ev.get("type") != "Card":
            continue
        nome = ev["player"]["name"] if ev.get("player") and ev["player"].get("name") else None
        if not nome:
            ignorados += 1
            continue
        api_id = ev["player"].get("id")
        jogador_id = get_or_create_jogador(cur, api_id, nome)
        minuto = ev["time"]["elapsed"]
        periodo = "1_tempo" if (minuto or 0) <= 45 else "2_tempo"
        lado = "mandante" if ev["team"]["id"] == nosso_time_api_id else "visitante"
        cor = "amarelo" if "Yellow" in (ev.get("detail") or "") else "vermelho"
        cur.execute(
            """INSERT INTO cartoes (jogo_id, jogador_id, lado, cor, minuto, periodo)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (jogo_id, jogador_id, lado, cor, minuto, periodo),
        )
        salvos += 1
    return salvos, ignorados


# ---------- FASE 1 ----------

def fase_verificar():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    try:
        for alvo in ALVOS:
            fixture_id, perspectivas = resolver_alvo(cur, alvo)
            marca = "" if alvo.get("ativo", True) else "   [DESATIVADO - `aplicar` vai ignorar]"
            print("\n" + "=" * 88)
            print(f"{alvo['rotulo']}{marca}")
            print(f"fixture_id_api {fixture_id} | {len(perspectivas)} perspectiva(s) em `jogos`")
            print(f"esperado (fonte externa): {alvo['esperado']}")
            print("=" * 88)

            jogo_id_ref = perspectivas[0][0]

            if "estatisticas" in alvo["refazer"]:
                print("\n-- ESTATÍSTICA DE TIME --")
                print("  NO BANCO HOJE:")
                for jogo_id, nome, _mandante in perspectivas:
                    for lado, esc, faltas, fin, posse in estatisticas_salvas(cur, jogo_id):
                        print(f"    jogo_id {jogo_id} ({nome}) {lado}: "
                              f"escanteios={esc} faltas={faltas} finalizacoes={fin} posse={posse}")

                home_team_id = buscar_home_team_id(cur, jogo_id_ref)
                api = buscar_estatisticas(fixture_id)
                print("  API AGORA:")
                if not api:
                    print("    (a API não devolveu nada - não aplique, tente de novo depois)")
                for lado, esc, faltas, fin, posse in resumir_estatisticas_api(api, home_team_id):
                    print(f"    {lado}: escanteios={esc} faltas={faltas} "
                          f"finalizacoes={fin} posse={posse}")

            if "cartoes" in alvo["refazer"]:
                print("\n-- CARTÕES --")
                print("  NO BANCO HOJE ('mandante' = nosso time daquela perspectiva):")
                for jogo_id, nome, _mandante in perspectivas:
                    linhas = cartoes_salvos(cur, jogo_id)
                    print(f"    jogo_id {jogo_id} ({nome}): {len(linhas)} cartão(ões)")
                    for lado, cor, minuto in linhas:
                        print(f"      {minuto}' {cor} ({lado})")

                nosso_api_id = buscar_nosso_time_api_id(cur, jogo_id_ref)
                eventos = buscar_eventos(fixture_id)
                api_cartoes = resumir_cartoes_api(eventos, nosso_api_id)
                nome_ref = perspectivas[0][1]
                print(f"  API AGORA ('mandante' = {nome_ref}): {len(api_cartoes)} cartão(ões)")
                for lado, cor, minuto in api_cartoes:
                    print(f"      {minuto}' {cor} ({lado})")

        print("\n" + "=" * 88)
        print("Nada foi gravado. Compare a linha 'API AGORA' com o print do Sofascore.")
        print("Se a API AINDA estiver com o número antigo, NÃO aplique - espere mais um")
        print("dia (o caso Athletico x Bragantino levou ~3 dias) ou parta pra correção")
        print("manual. Se bater com o real, rode:")
        print("  python refetch_rodada_pontual.py aplicar")

    finally:
        cur.close()
        conn.close()


# ---------- FASE 2 ----------

def fase_aplicar():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        ativos = alvos_ativos()
        if not ativos:
            print("Nenhum alvo ativo em ALVOS - nada a aplicar.")
            return
        for alvo in ativos:
            fixture_id, perspectivas = resolver_alvo(cur, alvo)
            print("\n" + "=" * 88)
            print(f"{alvo['rotulo']} | fixture {fixture_id}")
            print("=" * 88)

            if "estatisticas" in alvo["refazer"]:
                estatisticas = buscar_estatisticas(fixture_id)
                estatisticas_jog = buscar_estatisticas_jogadores(fixture_id)
                if not estatisticas:
                    raise RuntimeError(
                        f"API não devolveu estatística de time pro fixture {fixture_id} - "
                        "parando sem commitar."
                    )
                for jogo_id, nome, _mandante in perspectivas:
                    cur.execute("DELETE FROM estatisticas_jogo WHERE jogo_id = %s", (jogo_id,))
                    cur.execute("DELETE FROM jogador_estatisticas_jogo WHERE jogo_id = %s", (jogo_id,))
                    home_team_id = buscar_home_team_id(cur, jogo_id)
                    n_time = salvar_estatisticas(cur, jogo_id, estatisticas, home_team_id)
                    n_jog = salvar_estatisticas_jogadores(cur, jogo_id, estatisticas_jog, home_team_id)
                    print(f"  jogo_id {jogo_id} ({nome}): {n_time} lado(s) de time, "
                          f"{n_jog} jogador(es)")
                    for lado, esc, faltas, fin, posse in estatisticas_salvas(cur, jogo_id):
                        print(f"    {lado}: escanteios={esc} faltas={faltas} finalizacoes={fin}")

            if "cartoes" in alvo["refazer"]:
                eventos = buscar_eventos(fixture_id)
                tem_cartao = any(ev.get("type") == "Card" for ev in eventos)
                if not tem_cartao:
                    raise RuntimeError(
                        f"API não devolveu nenhum cartão pro fixture {fixture_id} - "
                        "parando sem commitar (apagar sem ter o que regravar seria pior "
                        "que o problema original)."
                    )
                for jogo_id, nome, _mandante in perspectivas:
                    antes = len(cartoes_salvos(cur, jogo_id))
                    cur.execute("DELETE FROM cartoes WHERE jogo_id = %s", (jogo_id,))
                    nosso_api_id = buscar_nosso_time_api_id(cur, jogo_id)
                    salvos, ignorados = salvar_cartoes(cur, jogo_id, eventos, nosso_api_id)
                    print(f"  jogo_id {jogo_id} ({nome}): {antes} -> {salvos} cartão(ões) "
                          f"({ignorados} ignorado(s) por falta de jogador)")
                    for lado, cor, minuto in cartoes_salvos(cur, jogo_id):
                        print(f"    {minuto}' {cor} ({lado})")

        conn.commit()
        print("\n✅ Fase 2 concluída e commitada.")
        print("   CONFIRA os números acima contra o print antes de rodar a fase 3:")
        print("   python refetch_rodada_pontual.py reavaliar")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Erro na fase 2, nada foi salvo (rollback): {e}")
        raise
    finally:
        cur.close()
        conn.close()


# ---------- FASE 3 ----------

def fase_reavaliar():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        todos_jogo_ids = []
        for alvo in alvos_ativos():
            _fixture_id, perspectivas = resolver_alvo(cur, alvo)
            todos_jogo_ids.extend([p[0] for p in perspectivas])

        cur.execute(
            """SELECT id, jogo_id, jogador_id, tipo_padrao, descricao, linha,
                      direcao, resultado
               FROM historico_recomendacoes WHERE jogo_id = ANY(%s)
               ORDER BY id""",
            (todos_jogo_ids,),
        )
        linhas = cur.fetchall()
        if not linhas:
            print("Nenhuma linha de historico_recomendacoes pra esses jogos.")
            return

        mudou = 0
        for rec_id, jogo_id, jogador_id, tipo_padrao, descricao, linha, direcao, antigo in linhas:
            novo = avaliar_resultado(cur, tipo_padrao, jogador_id, jogo_id, linha, descricao, direcao)
            if novo != antigo:
                cur.execute(
                    "UPDATE historico_recomendacoes SET resultado = %s WHERE id = %s",
                    (novo, rec_id),
                )
                print(f"  #{rec_id} [{tipo_padrao}] \"{descricao}\": {antigo} -> {novo}")
                mudou += 1

        conn.commit()
        print(f"\n✅ {mudou} de {len(linhas)} linha(s) corrigida(s) e commitada(s).")
        print("   É ESPERADO que a taxa de acerto PIORE - as linhas afetadas eram")
        print("   quase todas falso positivo (marcadas 'acertou' em cima de dado")
        print("   parcial). Piorar aqui é sinal de que a correção funcionou.")
        print("\n   Depois de terminar TODAS as correções da auditoria, rode")
        print("   `refazer_tabela_calibracao.py` uma vez só.")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Erro na fase 3, nada foi salvo (rollback): {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("verificar", "aplicar", "reavaliar"):
        print("Uso:")
        print("  python refetch_rodada_pontual.py verificar   (só lê, compara banco x API)")
        print("  python refetch_rodada_pontual.py aplicar     (regrava o dado)")
        print("  python refetch_rodada_pontual.py reavaliar   (reavalia o histórico)")
        sys.exit(1)

    if sys.argv[1] == "verificar":
        fase_verificar()
    elif sys.argv[1] == "aplicar":
        fase_aplicar()
    else:
        fase_reavaliar()
