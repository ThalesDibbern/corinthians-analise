"""
Script de migração (rodar 1x, manualmente, no serviço de migração pontual
do Railway) - cria a tabela `padroes_correlacao_time`.

Complementa `padroes_correlacao_estatisticas` (a versão GERAL da liga,
já implementada) - essa aqui calcula a mesma coisa, mas separado por
time: "quando O FLAMENGO especificamente chuta muito, os escanteios do
jogo dele aumentam mais ou menos que a média da liga?" - serve pra
identificar times onde essa relação é mais forte (ou mais fraca, ou até
invertida) do que o padrão geral do futebol.

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
            CREATE TABLE IF NOT EXISTS padroes_correlacao_time (
                id SERIAL PRIMARY KEY,
                time_id INTEGER NOT NULL REFERENCES times(id),
                par VARCHAR(40) NOT NULL,
                estatistica_a VARCHAR(20) NOT NULL,
                estatistica_b VARCHAR(20) NOT NULL,
                media_a NUMERIC(8,3) NOT NULL,
                valor_b_acima NUMERIC(8,3) NOT NULL,
                valor_b_abaixo NUMERIC(8,3) NOT NULL,
                jogos_acima INTEGER NOT NULL,
                jogos_abaixo INTEGER NOT NULL,
                jogos_total INTEGER NOT NULL,
                atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (time_id, par)
            )
            """
        )
        print("Tabela padroes_correlacao_time OK.")

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_padroes_correlacao_time_time "
            "ON padroes_correlacao_time (time_id)"
        )
        print("Índice OK.")

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
