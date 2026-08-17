"""
Script de migração (rodar 1x, manualmente, no serviço de migração pontual
do Railway) - cria a tabela do Handicap Asiático (só linha de meio gol,
ver decisão de arquitetura de 17/08/2026).

Não precisa de coluna nova em `jogos` nem de backfill - usa só
placar_corinthians/placar_adversario, que já existem e já estão
preenchidos pra todo o histórico.

Seguro rodar mais de uma vez - usa IF NOT EXISTS.

Variáveis de ambiente necessárias:
  - DATABASE_URL -> a URL de conexão do Postgres (mesma usada nos outros scripts)
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS padroes_time_handicap (
                id SERIAL PRIMARY KEY,
                time_id INTEGER NOT NULL REFERENCES times(id),
                lado VARCHAR(10) NOT NULL,     -- 'mandante' ou 'visitante'
                linha NUMERIC(4,2) NOT NULL,   -- handicap bruto, convenção OddsPapi (relativo ao mandante)
                jogos_analisados INTEGER NOT NULL,
                jogos_cobriu INTEGER NOT NULL,
                frequencia NUMERIC(5,2) NOT NULL,
                media_diferenca NUMERIC(5,2) NOT NULL,
                atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (time_id, lado, linha)
            )
            """
        )
        print("Tabela padroes_time_handicap OK.")

        conn.commit()
        print("\nMigração concluída com sucesso.")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a migração: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
