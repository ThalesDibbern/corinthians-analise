"""
Diagnóstico só-leitura: a API-Football ainda devolve eventos de fixtures
ANTIGOS?

MOTIVO: a auditoria de pênalti perdido varreu 1.419 fixtures de 2022-2025
e encontrou ZERO pênaltis perdidos. Isso é implausível - a taxa medida em
2026 foi de 2,4% (9 jogos em 380), o que daria ~34 jogos esperados no
período. Zero absoluto sugere que a API não está devolvendo os eventos,
e não que eles não existem.

O problema é que `auditar_penalti_perdido.py` trata os dois casos igual:

    if not perdidos:
        sem_penalti += 1
        continue

Um fixture com `response: []` (API sem dados) e um fixture com eventos
mas sem pênalti perdido caem os dois em "sem_penalti". A auditoria não
consegue distinguir "está limpo" de "não consegui ver".

Este script resolve a dúvida: chama a API pra alguns fixtures de cada
temporada e imprime QUANTOS eventos vieram, por tipo. Se as temporadas
antigas voltarem com 0 eventos e 2026 vier com dezenas, a auditoria de
2022-2025 é INCONCLUSIVA, não negativa.

NÃO GRAVA NADA. ~2 requisições por temporada.

Uso:
  python diagnostico_eventos_antigos.py
"""

import os
import sys
import time
from collections import Counter
from datetime import datetime

import psycopg2
import requests

DATABASE_URL = os.environ["DATABASE_URL"]
API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": os.environ["API_FOOTBALL_KEY"]}

SEGUNDOS_ENTRE_REQUISICOES = 7
FIXTURES_POR_TEMPORADA = 2
TEMPORADAS = [2022, 2023, 2024, 2025, 2026]


def cabecalho():
    print("=" * 78)
    print("diagnostico_eventos_antigos.py | fase: verificar (só leitura)")
    print(f"argv: {sys.argv}")
    print(f"relógio: {datetime.now().isoformat()}")
    print("=" * 78)
    sys.stdout.flush()


def buscar_amostra(cur, temporada, limite):
    """Pega fixtures que TÊM gols/cartões no banco - ou seja, que na época
    da coleta a API respondeu normalmente. Se hoje vierem vazios, a
    diferença é da API, não do jogo."""
    cur.execute(
        """
        SELECT j.fixture_id_api,
               MIN(j.data_jogo) AS data_jogo,
               (SELECT COUNT(*) FROM gols g
                JOIN jogos j2 ON j2.id = g.jogo_id
                WHERE j2.fixture_id_api = j.fixture_id_api) AS gols_banco,
               (SELECT COUNT(*) FROM cartoes c
                JOIN jogos j3 ON j3.id = c.jogo_id
                WHERE j3.fixture_id_api = j.fixture_id_api) AS cartoes_banco
        FROM jogos j
        WHERE j.fixture_id_api > 0
          AND EXTRACT(YEAR FROM j.data_jogo) = %s
        GROUP BY j.fixture_id_api
        HAVING (SELECT COUNT(*) FROM cartoes c
                JOIN jogos j3 ON j3.id = c.jogo_id
                WHERE j3.fixture_id_api = j.fixture_id_api) > 0
        ORDER BY j.fixture_id_api
        LIMIT %s
        """,
        (temporada, limite),
    )
    return cur.fetchall()


def main():
    cabecalho()
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    print(f"\n{FIXTURES_POR_TEMPORADA} fixtures por temporada, "
          f"{len(TEMPORADAS)} temporadas = "
          f"~{FIXTURES_POR_TEMPORADA * len(TEMPORADAS)} requisições "
          f"(~{FIXTURES_POR_TEMPORADA * len(TEMPORADAS) * SEGUNDOS_ENTRE_REQUISICOES / 60:.0f} min).\n")
    sys.stdout.flush()

    resumo = {}

    for temporada in TEMPORADAS:
        amostra = buscar_amostra(cur, temporada, FIXTURES_POR_TEMPORADA)
        if not amostra:
            print(f"--- {temporada}: nenhum fixture com cartão no banco, pulando ---")
            continue

        print(f"--- TEMPORADA {temporada} ---")
        total_eventos_temporada = 0

        for fixture_id, data_jogo, gols_banco, cartoes_banco in amostra:
            try:
                resp = requests.get(
                    f"{API_BASE}/fixtures/events",
                    headers=HEADERS,
                    params={"fixture": fixture_id},
                    timeout=30,
                )
                resp.raise_for_status()
                dados = resp.json()
            except Exception as e:
                print(f"  fixture={fixture_id} ({data_jogo}) !! ERRO: {e}")
                sys.stdout.flush()
                time.sleep(SEGUNDOS_ENTRE_REQUISICOES)
                continue

            eventos = dados.get("response", [])
            erros_api = dados.get("errors")
            total_eventos_temporada += len(eventos)

            tipos = Counter(ev.get("type") for ev in eventos)
            detalhes = Counter(ev.get("detail") for ev in eventos)

            print(f"  fixture={fixture_id} ({data_jogo})")
            print(f"    banco tem:  {gols_banco} gols, {cartoes_banco} cartões")
            print(f"    API devolve: {len(eventos)} eventos  {dict(tipos) if tipos else '(VAZIO)'}")
            if erros_api:
                print(f"    !! errors da API: {erros_api}")
            if detalhes:
                print(f"    detalhes: {dict(detalhes)}")
            sys.stdout.flush()
            time.sleep(SEGUNDOS_ENTRE_REQUISICOES)

        resumo[temporada] = total_eventos_temporada
        print()

    print("=" * 78)
    print("RESUMO - total de eventos devolvidos pela API, por temporada")
    print("=" * 78)
    for temporada, total in sorted(resumo.items()):
        marca = "  <-- VAZIO! API não tem dados desta temporada" if total == 0 else ""
        print(f"  {temporada}: {total} eventos{marca}")

    print()
    if any(v == 0 for v in resumo.values()):
        print("VEREDITO: pelo menos uma temporada volta VAZIA da API.")
        print("A auditoria de pênalti perdido nessas temporadas é INCONCLUSIVA,")
        print("não negativa - ela não conseguiu ver os eventos.")
        print("O resultado '0 contaminados' NÃO deve ser registrado como")
        print("'está limpo'.")
    else:
        print("VEREDITO: a API devolve eventos em todas as temporadas testadas.")
        print("O resultado '0 pênaltis perdidos em 1.419 fixtures' então é real")
        print("- e merece explicação própria, porque contraria a taxa de 2,4%")
        print("medida em 2026.")
    print("=" * 78)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
