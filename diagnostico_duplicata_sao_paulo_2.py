"""
Script PONTUAL, só-leitura - complemento do diagnóstico anterior. Confirma
se existe a segunda linha em `jogos` (perspectiva do São Paulo, id=7) pro
mesmo confronto RB Bragantino x São Paulo de 29/08/2026, e mostra o texto
exato salvo em `adversario` nas duas linhas - a hipótese é que uma foi
criada com o nome cru da OddsPapi ("Sao Paulo FC SP") e a outra com o
nome canônico de `times.nome` ("Sao Paulo"), textos diferentes pro MESMO
time - isso quebraria o agrupamento por nome em "Jogos disponíveis".

Rodar:
    python diagnostico_duplicata_sao_paulo_2.py

Variáveis de ambiente necessárias: DATABASE_URL
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]


def main():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    print("=" * 78)
    print("TODAS as linhas de jogos em 29/08/2026 envolvendo RB Bragantino OU São Paulo")
    print("=" * 78)
    cur.execute(
        """SELECT j.id, t.nome AS nosso_time, j.adversario, j.mandante_id, j.visitante_id,
                  j.fixture_id_api, j.nosso_time_id
           FROM jogos j
           JOIN times t ON t.id = j.nosso_time_id
           WHERE j.data_jogo = %s
             AND j.nosso_time_id IN (
                 SELECT id FROM times WHERE nome ILIKE %s OR nome ILIKE %s
             )
           ORDER BY j.id""",
        ("2026-08-29", "%paulo%", "%bragantino%"),
    )
    for row in cur.fetchall():
        print(row)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
