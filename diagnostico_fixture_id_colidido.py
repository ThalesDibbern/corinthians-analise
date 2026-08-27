"""
Script PONTUAL, só-leitura - encontra TODA linha de `jogos` com
fixture_id_api = 1492358 (o valor que causou o UniqueViolation ao rodar
corrigir_perspectivas_divergentes.py aplicar: "(fixture_id_api,
nosso_time_id)=(1492358, 6) already exists"). Esse ID deveria ser único
(é o ID real da API-Football pra UM jogo específico) - se aparece em mais
de uma linha com nosso_time_id diferente do esperado, ou em confrontos
diferentes, o problema é outro do que foi diagnosticado antes.

Não grava nada.

Rodar:
    python diagnostico_fixture_id_colidido.py

Variáveis de ambiente necessárias: DATABASE_URL
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

FIXTURE_ID_SUSPEITO = 1492358


def main():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    print("=" * 78)
    print(f"Toda linha de jogos com fixture_id_api = {FIXTURE_ID_SUSPEITO}")
    print("=" * 78)
    cur.execute(
        """SELECT j.id, t.nome AS nosso_time, j.nosso_time_id, j.adversario,
                  j.data_jogo, j.mandante_id, j.visitante_id, j.fixture_id_api
           FROM jogos j
           JOIN times t ON t.id = j.nosso_time_id
           WHERE j.fixture_id_api = %s
           ORDER BY j.id""",
        (FIXTURE_ID_SUSPEITO,),
    )
    for row in cur.fetchall():
        print(row)

    print("\n" + "=" * 78)
    print("Pra comparar: as linhas originais 1121 e 1296 (estado ATUAL, pós-rollback)")
    print("=" * 78)
    cur.execute(
        """SELECT id, nosso_time_id, adversario, data_jogo, mandante_id, visitante_id, fixture_id_api
           FROM jogos WHERE id IN (1121, 1296) ORDER BY id"""
    )
    for row in cur.fetchall():
        print(row)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
