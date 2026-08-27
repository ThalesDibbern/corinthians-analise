"""
Script PONTUAL, só-leitura - procura na base INTEIRA por pares de linhas
em `jogos` que são provavelmente o MESMO jogo real, duplicado por causa
de `data_jogo` divergente entre dois pontos do pipeline (fuso horário:
BRT vs UTC na virada da meia-noite). Critério: mesmo par de times
(mandante_id/visitante_id), mesma perspectiva (nosso_time_id igual), e
`data_jogo` com exatamente 1 dia de diferença.

Caso que expôs o padrão (27/08/2026): jogo #1296 (RB Bragantino,
29/08, fixture sintético) e #1492358 (RB Bragantino, 30/08, fixture
real 1492358) - mesmo jogo real, duplicado por 1 dia de diferença.

Não grava nada - só mapeia o tamanho do problema antes de desenhar a
correção (que vai precisar de MERGE, não só UPDATE, porque as duas
linhas duplicadas podem ter odds/recomendações próprias que precisam ser
reunidas ou descartadas).

Rodar:
    python diagnostico_jogos_duplicados_fuso.py

Variáveis de ambiente necessárias: DATABASE_URL
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]


def main():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    print("=" * 78)
    print("Pares de jogos: mesmo par de times + mesma perspectiva, data 1 dia de diferença")
    print("=" * 78)
    cur.execute(
        """
        SELECT a.id, a.nosso_time_id, a.data_jogo, a.datahora_jogo, a.fixture_id_api, a.adversario,
               b.id, b.data_jogo, b.datahora_jogo, b.fixture_id_api, b.adversario
        FROM jogos a
        JOIN jogos b
          ON a.mandante_id = b.mandante_id
         AND a.visitante_id = b.visitante_id
         AND a.nosso_time_id = b.nosso_time_id
         AND a.id < b.id
         AND ABS(a.data_jogo - b.data_jogo) = 1
        WHERE a.mandante_id IS NOT NULL AND a.visitante_id IS NOT NULL
        ORDER BY a.data_jogo
        """
    )
    pares = cur.fetchall()

    if not pares:
        print("\nNenhum par encontrado com esse critério.")
        cur.close()
        conn.close()
        return

    print(f"\n{len(pares)} par(es) encontrado(s):\n")
    for (id_a, nosso_time_id, data_a, datahora_a, fixture_a, adversario_a,
         id_b, data_b, datahora_b, fixture_b, adversario_b) in pares:
        print(f"  jogo #{id_a}: data={data_a} datahora={datahora_a} fixture_id_api={fixture_a} adversario='{adversario_a}'")
        print(f"  jogo #{id_b}: data={data_b} datahora={datahora_b} fixture_id_api={fixture_b} adversario='{adversario_b}'")

        for jogo_id in (id_a, id_b):
            cur.execute("SELECT COUNT(*) FROM odds WHERE jogo_id = %s", (jogo_id,))
            n_odds = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM recomendacoes WHERE jogo_id = %s", (jogo_id,))
            n_rec = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM historico_recomendacoes WHERE jogo_id = %s", (jogo_id,))
            n_hist = cur.fetchone()[0]
            print(f"      jogo #{jogo_id}: odds={n_odds}  recomendacoes={n_rec}  historico_recomendacoes={n_hist}")
        print()

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
