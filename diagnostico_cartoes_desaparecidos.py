"""
Diagnóstico SÓ LEITURA: busca o JSON cru de /fixtures/events na API-Football
para os jogos em que a contagem de cartões no banco ficou menor que a
contagem real (conferida contra o Sofascore manualmente).

NÃO grava nada no banco. NÃO chama salvar_eventos. O objetivo é confirmar
(ou descartar) a hipótese de que `salvar_eventos()` está descartando
eventos de cartão sem nome de jogador (ver `if not jogador_nome: ... continue`
em popular_banco.py), o que bateria com o padrão observado: os cartões que
faltam no banco parecem ser todos de "Desacordo por palavras ou atos" /
"Conduta violenta" (cartões de conduta, às vezes atribuídos a banco/comissão
técnica), nunca cartões ligados a uma falta normal em campo.

Jogos investigados (rodada de 29-31/08/2026), com o gap medido entre o
banco e o Sofascore:
  1492352  Bahia 3x2 Internacional      -> banco tinha 2 cartões, real 7
  1492353  Corinthians 0x1 Santos        -> banco tinha 6 cartões, real 7
  1492356  Mirassol 1x1 Palmeiras        -> banco tinha 4 cartões, real 5
  1492358  São Paulo 2x1 RB Bragantino   -> banco tinha 6 cartões, real 7

Variável de ambiente necessária:
  - API_FOOTBALL_KEY

Uso:
  python diagnostico_cartoes_desaparecidos.py
"""

import os
import sys
import time
from datetime import datetime

import requests

API_KEY = os.environ["API_FOOTBALL_KEY"]
API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

FIXTURES_INVESTIGADOS = {
    1492352: "Bahia 3x2 Internacional (banco: 2 cartões, real: 7)",
    1492353: "Corinthians 0x1 Santos (banco: 6 cartões, real: 7)",
    1492356: "Mirassol 1x1 Palmeiras (banco: 4 cartões, real: 5)",
    1492358: "São Paulo 2x1 RB Bragantino (banco: 6 cartões, real: 7)",
}


def cabecalho():
    print("=" * 78)
    print(f"diagnostico_cartoes_desaparecidos.py | fase: leitura_api_eventos")
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

    for fixture_id, rotulo in FIXTURES_INVESTIGADOS.items():
        print(f"\n### Fixture {fixture_id} — {rotulo}")
        eventos = buscar_eventos(fixture_id)

        cartoes = [ev for ev in eventos if ev.get("type") == "Card"]
        print(f"  {len(eventos)} eventos no total, {len(cartoes)} são cartões.\n")

        for ev in cartoes:
            jogador = ev.get("player") or {}
            nome = jogador.get("name")
            time_nome = (ev.get("team") or {}).get("name")
            minuto = (ev.get("time") or {}).get("elapsed")
            detail = ev.get("detail")
            comments = ev.get("comments")
            suspeito = "  <<< SEM NOME DE JOGADOR (seria descartado por salvar_eventos)" if not nome else ""

            print(f"  [{minuto}'] {time_nome} | detail={detail!r} | "
                  f"player={nome!r} | comments={comments!r}{suspeito}")

        time.sleep(2)  # respeita o limite por minuto do plano

    print("\nFim do diagnóstico. Nenhuma escrita foi feita no banco.")


if __name__ == "__main__":
    main()
