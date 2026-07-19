"""
Script que verifica se o Corinthians tem jogo nos próximos dias e, se tiver,
busca as odds desse jogo (cartões de jogador + escanteios do time) na
OddsPapi, salvando tudo na tabela `odds` do Postgres.

Feito para rodar automaticamente todo dia (Cron Schedule no Railway).
Se não houver jogo próximo, o script simplesmente não faz nada naquele dia.

Variáveis de ambiente necessárias (configuradas no Railway, aba "Variables"):
  - ODDSPAPI_KEY  -> sua chave da OddsPapi (api.oddspapi.io)
  - DATABASE_URL  -> a URL de conexão do Postgres (mesma usada no outro script)
"""

import os
from datetime import datetime, timedelta, timezone

import requests
import psycopg2

# ---------- Configurações ----------
API_KEY = os.environ["ODDSPAPI_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

API_BASE = "https://api.oddspapi.io/v4"
SPORT_ID = 10
TOURNAMENT_ID = 325     # Brasileirão Série A
PARTICIPANT_ID = 1957   # Corinthians
BOOKMAKERS = "betano.bet.br,superbet.bet.br"
DIAS_ANTECEDENCIA = 2   # busca odds de jogos que acontecem em até X dias

# Palavras usadas para filtrar quais mercados nos interessam. Comparação é
# feita sem diferenciar maiúsculas.
# OBS: "resultado"/"vencedor"/"1x2" tentam capturar o mercado de resultado
# final (1X2) - o nome exato em português na OddsPapi ainda não foi
# confirmado manualmente, então pode ser necessário ajustar essas palavras
# depois de ver o dado real chegando (ou não) na tabela `odds`.
PALAVRAS_MERCADO_INTERESSE = [
    "card", "cartão", "cartao", "corner", "escanteio",
    "falta", "desarme", "chute", "impediment",
    "resultado", "vencedor", "1x2",
]


def buscar_catalogo_mercados():
    """Busca a lista de todos os mercados existentes, incluindo a linha
    (handicap) de cada mercado e o nome de cada outcome (Mais/Menos/Sim/Não/
    0/1+/2+), para conseguirmos interpretar as odds corretamente depois."""
    resp = requests.get(
        f"{API_BASE}/markets",
        params={"sportId": SPORT_ID, "language": "pt", "apiKey": API_KEY},
    )
    resp.raise_for_status()
    mercados = resp.json()

    catalogo = {}
    for m in mercados:
        catalogo[str(m["marketId"])] = {
            "nome": m["marketName"],
            "handicap": m.get("handicap"),
            "outcomes": {str(o["outcomeId"]): o["outcomeName"] for o in m.get("outcomes", [])},
        }
    return catalogo


def buscar_proximos_jogos():
    """Busca jogos do Corinthians no Brasileirão e filtra os que acontecem
    dentro da janela de antecedência definida."""
    resp = requests.get(
        f"{API_BASE}/fixtures",
        params={
            "tournamentId": TOURNAMENT_ID,
            "sportId": SPORT_ID,
            "participantId": PARTICIPANT_ID,
            "apiKey": API_KEY,
        },
    )
    resp.raise_for_status()
    jogos = resp.json()

    agora = datetime.now(timezone.utc)
    limite = agora + timedelta(days=DIAS_ANTECEDENCIA)

    proximos = []
    for jogo in jogos:
        if not jogo.get("startTime"):
            continue
        inicio = datetime.fromisoformat(jogo["startTime"].replace("Z", "+00:00"))
        if agora <= inicio <= limite:
            proximos.append(jogo)

    return proximos


def buscar_odds(fixture_id):
    """Busca as odds de um jogo específico nas casas configuradas."""
    resp = requests.get(
        f"{API_BASE}/odds",
        params={
            "fixtureId": fixture_id,
            "bookmakers": BOOKMAKERS,
            "oddsFormat": "decimal",
            "language": "pt",
            "verbosity": 3,
            "apiKey": API_KEY,
        },
    )
    resp.raise_for_status()
    return resp.json()


def mercado_interessa(nome_mercado):
    nome = nome_mercado.lower()
    return any(palavra in nome for palavra in PALAVRAS_MERCADO_INTERESSE)


def get_or_create_jogador(cur, nome):
    cur.execute("SELECT id FROM jogadores WHERE nome = %s", (nome,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO jogadores (nome) VALUES (%s) RETURNING id", (nome,))
    return cur.fetchone()[0]


def get_or_create_jogo(cur, data_jogo, adversario, mandante):
    """Encontra (ou cria) o jogo pela combinação data + adversário - assim
    esse script funciona independente do jogo já existir vindo do outro
    script (API-Football) ou ser inteiramente novo (jogo futuro)."""
    cur.execute(
        "SELECT id FROM jogos WHERE data_jogo = %s AND adversario = %s",
        (data_jogo, adversario),
    )
    row = cur.fetchone()
    if row:
        return row[0]

    cur.execute(
        """INSERT INTO jogos (data_jogo, adversario, mandante, competicao)
           VALUES (%s, %s, %s, %s) RETURNING id""",
        (data_jogo, adversario, mandante, "Brasileirão Série A"),
    )
    return cur.fetchone()[0]


def salvar_odds_do_jogo(cur, jogo_id, dados_odds, catalogo_mercados):
    """Percorre as odds de todas as casas/mercados retornados e salva só os
    mercados de interesse (cartão de jogador + escanteios do time), incluindo
    a linha (handicap) e a direção (Mais/Menos/Sim/Não) de cada odd."""
    salvos = 0
    bookmaker_odds = dados_odds.get("bookmakerOdds", {})

    for casa, info_casa in bookmaker_odds.items():
        if info_casa.get("suspended"):
            continue

        markets = info_casa.get("markets", {})
        for market_id, market_info in markets.items():
            info_mercado = catalogo_mercados.get(market_id)
            if not info_mercado or not mercado_interessa(info_mercado["nome"]):
                continue

            nome_mercado = info_mercado["nome"]
            linha = info_mercado["handicap"]

            outcomes = market_info.get("outcomes", {})
            for outcome_id, outcome_info in outcomes.items():
                direcao = info_mercado["outcomes"].get(outcome_id)
                players = outcome_info.get("players", {})

                for player_key, dados in players.items():
                    if not dados.get("active", True):
                        continue

                    price = dados.get("price")
                    if price is None:
                        continue

                    player_name = dados.get("playerName")
                    descricao_mercado = nome_mercado
                    jogador_id = None
                    if player_name:
                        jogador_id = get_or_create_jogador(cur, player_name)
                        descricao_mercado = f"{nome_mercado} - {player_name}"

                    cur.execute(
                        """INSERT INTO odds (jogo_id, jogador_id, casa_aposta, mercado, valor_odd, linha, direcao)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (jogo_id, jogador_id, casa, descricao_mercado, price, linha, direcao),
                    )
                    salvos += 1

    return salvos


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        jogos = buscar_proximos_jogos()

        if not jogos:
            print(f"Nenhum jogo do Corinthians nos próximos {DIAS_ANTECEDENCIA} dias. Nada a fazer hoje.")
            return

        print("Carregando catálogo de mercados...")
        catalogo_mercados = buscar_catalogo_mercados()

        for jogo in jogos:
            eh_mandante = jogo["participant1Id"] == PARTICIPANT_ID
            adversario = jogo["participant2Name"] if eh_mandante else jogo["participant1Name"]
            data_jogo = jogo["startTime"][:10]

            print(f"\nJogo encontrado: Corinthians x {adversario} em {data_jogo}")

            jogo_id = get_or_create_jogo(cur, data_jogo, adversario, eh_mandante)
            dados_odds = buscar_odds(jogo["fixtureId"])
            salvos = salvar_odds_do_jogo(cur, jogo_id, dados_odds, catalogo_mercados)

            print(f"  -> {salvos} odds salvas.")
            conn.commit()

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
