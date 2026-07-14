"""
Script que busca jogos, gols, cartões e substituições do Corinthians
na API-Football e salva no banco Postgres (Railway).

MODO TESTE: busca só 3 jogos da temporada 2023, pra validar
que tudo está funcionando antes de rodar o histórico completo.

Variáveis de ambiente necessárias (configuradas no Railway, aba "Variables"):
  - API_FOOTBALL_KEY   -> sua chave da API-Football (api-sports.io)
  - DATABASE_URL       -> a URL de conexão do Postgres (o Railway já cria essa
                           automaticamente quando você conecta os dois serviços)
"""

import os
import time
import requests
import psycopg2

# ---------- Configurações ----------
API_KEY = os.environ["API_FOOTBALL_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

LEAGUE_ID = 71    # Brasileirão Série A
TEAM_ID = 131     # Corinthians
SEASON = 2023     # temporada de teste (plano grátis cobre 2022-2024)
LIMITE_TESTE = 3  # quantidade de jogos pra esse teste inicial


def buscar_jogos():
    """Busca os jogos do Corinthians na temporada definida."""
    url = f"{API_BASE}/fixtures"
    params = {"league": LEAGUE_ID, "season": SEASON, "team": TEAM_ID}
    resp = requests.get(url, headers=HEADERS, params=params)
    resp.raise_for_status()
    dados = resp.json()

    if dados.get("errors"):
        raise RuntimeError(f"Erro da API: {dados['errors']}")

    jogos = dados["response"][:LIMITE_TESTE]
    print(f"Encontrados {len(dados['response'])} jogos no total. Usando {len(jogos)} para o teste.")
    return jogos


def buscar_eventos(fixture_id):
    """Busca gols, cartões e substituições de um jogo específico."""
    url = f"{API_BASE}/fixtures/events"
    params = {"fixture": fixture_id}
    resp = requests.get(url, headers=HEADERS, params=params)
    resp.raise_for_status()
    dados = resp.json()
    return dados["response"]


def get_or_create_jogador(cur, nome):
    """Garante que o jogador existe na tabela `jogadores`, retorna o id."""
    cur.execute("SELECT id FROM jogadores WHERE nome = %s", (nome,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO jogadores (nome) VALUES (%s) RETURNING id", (nome,))
    return cur.fetchone()[0]


def get_or_create_jogo(cur, fixture):
    """Garante que o jogo existe na tabela `jogos`, retorna o id."""
    fixture_id = fixture["fixture"]["id"]
    cur.execute("SELECT id FROM jogos WHERE id = %s", (fixture_id,))
    row = cur.fetchone()
    if row:
        return row[0]

    data_jogo = fixture["fixture"]["date"][:10]
    mandante_nome = fixture["teams"]["home"]["name"]
    visitante_nome = fixture["teams"]["away"]["name"]
    eh_mandante = fixture["teams"]["home"]["id"] == TEAM_ID
    adversario = visitante_nome if eh_mandante else mandante_nome
    placar_corinthians = (
        fixture["goals"]["home"] if eh_mandante else fixture["goals"]["away"]
    )
    placar_adversario = (
        fixture["goals"]["away"] if eh_mandante else fixture["goals"]["home"]
    )

    cur.execute(
        """
        INSERT INTO jogos (id, data_jogo, adversario, mandante, competicao,
                            placar_corinthians, placar_adversario)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            fixture_id,
            data_jogo,
            adversario,
            eh_mandante,
            "Brasileirão Série A",
            placar_corinthians,
            placar_adversario,
        ),
    )
    return fixture_id


def salvar_eventos(cur, jogo_id, eventos):
    """Classifica cada evento (gol / cartão / substituição) e salva na tabela certa."""
    contagem = {"gols": 0, "cartoes": 0, "substituicoes": 0, "ignorados": 0}

    for ev in eventos:
        tipo = ev["type"]  # "Goal", "Card", "subst" (varia conforme a API)
        minuto = ev["time"]["elapsed"]
        lado = "mandante" if ev["team"]["id"] == TEAM_ID else "visitante"
        jogador_nome = ev["player"]["name"] if ev["player"]["name"] else None

        if not jogador_nome:
            contagem["ignorados"] += 1
            continue

        jogador_id = get_or_create_jogador(cur, jogador_nome)
        periodo = "1_tempo" if minuto <= 45 else "2_tempo"

        if tipo == "Goal":
            cur.execute(
                """INSERT INTO gols (jogo_id, jogador_id, lado, minuto, periodo,
                                      penalti, gol_contra)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    jogo_id, jogador_id, lado, minuto, periodo,
                    ev.get("detail") == "Penalty",
                    ev.get("detail") == "Own Goal",
                ),
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
            jogador_entrou_id = (
                get_or_create_jogador(cur, jogador_entrou_nome)
                if jogador_entrou_nome
                else None
            )
            cur.execute(
                """INSERT INTO substituicoes (jogo_id, jogador_saiu_id, jogador_entrou_id,
                                               lado, minuto, periodo)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (jogo_id, jogador_id, jogador_entrou_id, lado, minuto, periodo),
            )
            contagem["substituicoes"] += 1
        else:
            contagem["ignorados"] += 1

    return contagem


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        jogos = buscar_jogos()

        for fixture in jogos:
            fixture_id = fixture["fixture"]["id"]
            print(f"\nProcessando jogo {fixture_id}...")

            jogo_id = get_or_create_jogo(cur, fixture)
            eventos = buscar_eventos(fixture_id)
            contagem = salvar_eventos(cur, jogo_id, eventos)

            print(f"  -> {contagem['gols']} gols, {contagem['cartoes']} cartões, "
                  f"{contagem['substituicoes']} substituições salvos "
                  f"({contagem['ignorados']} eventos ignorados).")

            conn.commit()
            time.sleep(1)  # respeita o limite de requisições do plano grátis

        print("\nConcluído! Dados de teste salvos no banco.")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
