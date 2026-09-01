"""
Diagnóstico SÓ LEITURA: JSON cru de /fixtures/events na API-Football pro
Vitória x Bahia (23/08/2026, fixture 1492349, jogo_id 1216/1492349) - a
divergência aberta desde a auditoria da rodada de 22-24/08 (seção 25):
2 cartões do Vitória que nunca chegaram no banco.

NÃO grava nada. Só imprime o que a API tem HOJE, com todo o detalhe (cor,
minuto, comments), pra comparar com o que já está salvo antes de decidir
se roda a correção pontual (mesmo padrão do corrigir_eventos_defasados_01_09.py).

Uso:
  python diagnostico_vitoria_bahia.py
"""

import os
import sys
from datetime import datetime

import requests

API_KEY = os.environ["API_FOOTBALL_KEY"]
API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

FIXTURE_ID = 1492349


def cabecalho():
    print("=" * 78)
    print("diagnostico_vitoria_bahia.py | fase: leitura_api_eventos")
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


def main():
    cabecalho()
    eventos = buscar_eventos(FIXTURE_ID)
    print(f"\n{len(eventos)} eventos no total.\n")

    for ev in eventos:
        tipo = ev.get("type")
        detail = ev.get("detail")
        time_nome = (ev.get("team") or {}).get("name")
        minuto = (ev.get("time") or {}).get("elapsed")
        jogador = (ev.get("player") or {}).get("name")
        comments = ev.get("comments")
        print(f"  [{tipo:6s}] [{minuto}'] {time_nome} | detail={detail!r} | "
              f"player={jogador!r} | comments={comments!r}")

    print("\nFim do diagnóstico. Nenhuma escrita foi feita no banco.")


if __name__ == "__main__":
    main()
