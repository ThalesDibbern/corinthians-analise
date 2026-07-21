"""
TESTE ISOLADO - verifica se o campo "referee" vem preenchido nos jogos
do Corinthians na API-Football, nas 3 temporadas disponíveis (2022-2024).

Este script NÃO mexe no banco de dados. Só faz a chamada à API, conta
quantos jogos vieram com árbitro preenchido, e mostra alguns exemplos.
É só pra decidirmos se vale a pena implementar a funcionalidade de
estatísticas de árbitro de verdade.

CUSTO: 3 requisições no total (1 por temporada) - bem abaixo do limite
diário de 100.

Como rodar:
  1. Defina a variável de ambiente API_FOOTBALL_KEY (a mesma que já
     está configurada no Railway)
  2. Rode: python testar_campo_arbitro.py

Não precisa de DATABASE_URL - esse script não toca no banco.
"""

import os
import requests

API_KEY = os.environ["API_FOOTBALL_KEY"]
API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

LEAGUE_ID = 71    # Brasileirão Série A
TEAM_ID = 131     # Corinthians
TEMPORADAS = [2022, 2023, 2024]


def buscar_jogos(temporada):
    resp = requests.get(
        f"{API_BASE}/fixtures",
        headers=HEADERS,
        params={"league": LEAGUE_ID, "season": temporada, "team": TEAM_ID},
    )
    resp.raise_for_status()
    dados = resp.json()

    if dados.get("errors"):
        print(f"  Aviso da API: {dados['errors']}")
        return []

    return dados["response"]


def main():
    print("Testando campo 'referee' nos jogos do Corinthians (2022-2024)...\n")

    total_geral = 0
    com_arbitro_geral = 0

    for temporada in TEMPORADAS:
        jogos = buscar_jogos(temporada)
        total = len(jogos)

        if total == 0:
            print(f"Temporada {temporada}: nenhum jogo retornado pela API.\n")
            continue

        com_arbitro = [j for j in jogos if j["fixture"].get("referee")]
        sem_arbitro = total - len(com_arbitro)

        percentual = round(100 * len(com_arbitro) / total, 1)

        print(f"Temporada {temporada}: {len(com_arbitro)}/{total} jogos com árbitro preenchido ({percentual}%)")

        if com_arbitro:
            exemplos = [j["fixture"]["referee"] for j in com_arbitro[:3]]
            print(f"  Exemplos de valor bruto retornado: {exemplos}")

        if sem_arbitro:
            print(f"  ({sem_arbitro} jogo(s) vieram com referee vazio/nulo)")

        print()

        total_geral += total
        com_arbitro_geral += len(com_arbitro)

    print("-" * 50)
    if total_geral:
        percentual_geral = round(100 * com_arbitro_geral / total_geral, 1)
        print(f"RESUMO GERAL: {com_arbitro_geral}/{total_geral} jogos com árbitro preenchido ({percentual_geral}%)")
    else:
        print("Nenhum jogo encontrado em nenhuma temporada. Verifique a API_FOOTBALL_KEY.")


if __name__ == "__main__":
    main()
