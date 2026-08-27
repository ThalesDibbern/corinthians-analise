"""
Script PONTUAL, só-leitura - mapeia duplicata de verdade em `jogos`: mais
de uma linha com o MESMO (data_jogo, mandante_id, visitante_id,
nosso_time_id) - ou seja, o mesmo time, o mesmo confronto, a mesma data,
mas linhas DIFERENTES (não é o caso normal de "2 perspectivas", é
duplicata mesmo). Achado ao rodar corrigir_perspectivas_divergentes.py
aplicar - travou em "(fixture_id_api, nosso_time_id)=(1492358, 6) already
exists", ou seja, já tem uma linha (6, aquele fixture) separada da que o
script estava tentando alinhar.

Não grava nada. Devolve, pra cada grupo duplicado: as linhas de `jogos`
envolvidas E quantas linhas em `odds`, `recomendacoes` e
`historico_recomendacoes` cada uma tem - necessário pra decidir COMO
mesclar (qual linha vira a canônica, o que precisa ser migrado antes de
apagar a outra).

Rodar:
    python diagnostico_jogos_duplicados_mesmo_time.py

Variáveis de ambiente necessárias: DATABASE_URL
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]


def main():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    print("=" * 78)
    print("Grupos de jogos com o MESMO (data_jogo, mandante_id, visitante_id, nosso_time_id)")
    print("=" * 78)
    cur.execute(
        """
        SELECT data_jogo, mandante_id, visitante_id, nosso_time_id, COUNT(*), array_agg(id ORDER BY id)
        FROM jogos
        WHERE mandante_id IS NOT NULL AND visitante_id IS NOT NULL
        GROUP BY data_jogo, mandante_id, visitante_id, nosso_time_id
        HAVING COUNT(*) > 1
        ORDER BY data_jogo
        """
    )
    grupos = cur.fetchall()

    if not grupos:
        print("\nNenhum grupo duplicado encontrado (com esse critério).")
        cur.close()
        conn.close()
        return

    print(f"\n{len(grupos)} grupo(s) duplicado(s):\n")

    for data_jogo, mandante_id, visitante_id, nosso_time_id, qtd, ids in grupos:
        print(f"--- data={data_jogo} mandante_id={mandante_id} visitante_id={visitante_id} "
              f"nosso_time_id={nosso_time_id} ({qtd} linhas: {ids}) ---")
        for jogo_id in ids:
            cur.execute(
                "SELECT adversario, fixture_id_api, arbitro, datahora_jogo FROM jogos WHERE id = %s",
                (jogo_id,),
            )
            adversario, fixture_id_api, arbitro, datahora_jogo = cur.fetchone()

            cur.execute("SELECT COUNT(*) FROM odds WHERE jogo_id = %s", (jogo_id,))
            n_odds = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM recomendacoes WHERE jogo_id = %s", (jogo_id,))
            n_recomendacoes = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM historico_recomendacoes WHERE jogo_id = %s", (jogo_id,))
            n_historico = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM estatisticas_jogo WHERE jogo_id = %s", (jogo_id,))
            n_estatisticas = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM apostas_salvas WHERE pernas::text LIKE %s",
                (f'%"jogo_id": {jogo_id}%',),
            )
            n_apostas = cur.fetchone()[0]

            print(f"    jogo #{jogo_id}: adversario='{adversario}' fixture_id_api={fixture_id_api} "
                  f"arbitro={arbitro} datahora={datahora_jogo}")
            print(f"        odds={n_odds}  recomendacoes={n_recomendacoes}  "
                  f"historico_recomendacoes={n_historico}  estatisticas_jogo={n_estatisticas}  "
                  f"apostas_salvas(aprox)={n_apostas}")
        print()

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
