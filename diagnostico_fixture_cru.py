"""
Diagnóstico só-leitura: imprime os eventos CRUS que a API-Football
devolve pra um fixture, e compara com o que está gravado em `gols`.

Serve pra qualquer investigação pontual de divergência de evento. Foi
escrito pro caso do fixture 1478037 (Bahia 2x0 Sport Recife, 03/12/2025),
que tem 3 linhas em `gols` pra um placar de 2 gols - o único jogo com gol
a mais em 1.663 auditados.

NÃO GRAVA NADA. 1 requisição por fixture.

Uso:
  python diagnostico_fixture_cru.py 1478037
  python diagnostico_fixture_cru.py 1478037 1492186
"""

import json
import os
import sys
from datetime import datetime

import psycopg2
import requests

DATABASE_URL = os.environ["DATABASE_URL"]
API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": os.environ["API_FOOTBALL_KEY"]}


def cabecalho():
    print("=" * 78)
    print("diagnostico_fixture_cru.py | fase: verificar (só leitura)")
    print(f"argv: {sys.argv}")
    print(f"relógio: {datetime.now().isoformat()}")
    print("=" * 78)
    sys.stdout.flush()


def main(fixture_ids):
    cabecalho()
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    for fixture_id in fixture_ids:
        print(f"\n{'=' * 78}")
        print(f"FIXTURE {fixture_id}")
        print("=" * 78)

        # --- o que o banco tem ---
        cur.execute(
            """
            SELECT j.id, j.data_jogo, t.nome, j.adversario, j.mandante,
                   j.placar_corinthians, j.placar_adversario
            FROM jogos j
            JOIN times t ON t.id = j.nosso_time_id
            WHERE j.fixture_id_api = %s
            ORDER BY j.id
            """,
            (fixture_id,),
        )
        perspectivas = cur.fetchall()
        print(f"\n-- BANCO: {len(perspectivas)} perspectiva(s) --")
        for jid, data, nosso, adv, mandante, pc, pa in perspectivas:
            papel = "mandante" if mandante else "visitante"
            print(f"  jogo_id={jid} {data} | {nosso} ({papel}) x {adv} | placar {pc}x{pa}")

            cur.execute(
                """SELECT g.minuto, g.periodo, g.lado, g.penalti, g.gol_contra,
                          COALESCE(jo.nome, '(sem jogador)')
                   FROM gols g
                   LEFT JOIN jogadores jo ON jo.id = g.jogador_id
                   WHERE g.jogo_id = %s ORDER BY g.minuto""",
                (jid,),
            )
            for minuto, periodo, lado, pen, contra, nome in cur.fetchall():
                extras = []
                if pen:
                    extras.append("pênalti")
                if contra:
                    extras.append("gol contra")
                print(f"      gol {minuto}' [{lado}] {nome} {' '.join(extras)}")

        # --- o que a API devolve HOJE ---
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
            print(f"\n  !! ERRO na API: {e}")
            continue

        eventos = dados.get("response", [])
        if dados.get("errors"):
            print(f"  !! errors da API: {dados['errors']}")

        print(f"\n-- API HOJE: {len(eventos)} eventos --")
        gols_api = [ev for ev in eventos if ev.get("type") == "Goal"]
        print(f"   {len(gols_api)} com type='Goal':")
        for ev in gols_api:
            minuto = (ev.get("time") or {}).get("elapsed")
            extra = (ev.get("time") or {}).get("extra")
            jogador = (ev.get("player") or {}).get("name")
            time_nome = (ev.get("team") or {}).get("name")
            detail = ev.get("detail")
            print(f"      {minuto}'{f'+{extra}' if extra else ''} "
                  f"[{time_nome}] {jogador} | detail={detail!r}")

        # qualquer evento que NÃO seja type=Goal mas mencione gol/VAR
        outros = [
            ev for ev in eventos
            if ev.get("type") != "Goal"
            and ("goal" in str(ev.get("detail", "")).lower()
                 or ev.get("type") == "Var")
        ]
        if outros:
            print(f"\n   {len(outros)} evento(s) de VAR / relacionados a gol:")
            for ev in outros:
                minuto = (ev.get("time") or {}).get("elapsed")
                jogador = (ev.get("player") or {}).get("name")
                time_nome = (ev.get("team") or {}).get("name")
                print(f"      {minuto}' [{time_nome}] {jogador} | "
                      f"type={ev.get('type')!r} detail={ev.get('detail')!r}")

        print("\n-- JSON CRU dos eventos type='Goal' e type='Var' --")
        print(json.dumps(
            [ev for ev in eventos if ev.get("type") in ("Goal", "Var")],
            ensure_ascii=False, indent=2
        ))
        sys.stdout.flush()

    cur.close()
    conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python diagnostico_fixture_cru.py FIXTURE_ID [FIXTURE_ID ...]")
        sys.exit(1)
    main([int(a) for a in sys.argv[1:]])
