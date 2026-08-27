"""
Script PONTUAL, só-leitura - diagnostica a duplicata "RB Bragantino x Sao
Paulo" / "RB Bragantino x Sao Paulo FC SP" vista em /historico antes de
decidir como corrigir. Não grava nada no banco.

Rodar:
    python diagnostico_duplicata_sao_paulo.py

Variáveis de ambiente necessárias: DATABASE_URL
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]


def main():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    print("=" * 78)
    print("1) Times com 'paulo' no nome")
    print("=" * 78)
    cur.execute(
        """SELECT id, nome, oddspapi_participant_id, api_football_team_id, rastreado
           FROM times WHERE nome ILIKE %s ORDER BY id""",
        ("%paulo%",),
    )
    for row in cur.fetchall():
        print(row)

    print("\n" + "=" * 78)
    print("2) Jogos do RB Bragantino em 29/08 contra algo com 'paulo'")
    print("=" * 78)
    cur.execute(
        """SELECT j.id, j.adversario, j.data_jogo, j.mandante_id, j.visitante_id,
                  j.fixture_id_api, j.nosso_time_id, t.nome AS nosso_time
           FROM jogos j
           JOIN times t ON t.id = j.nosso_time_id
           WHERE j.data_jogo = %s
             AND (j.adversario ILIKE %s OR t.nome ILIKE %s)
           ORDER BY j.id""",
        ("2026-08-29", "%paulo%", "%bragantino%"),
    )
    for row in cur.fetchall():
        print(row)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
