"""
Script de migração (rodar 1x, manualmente, no serviço de migração pontual
do Railway) - cria a tabela `padroes_time_linha`, usada pela nova página
"Estatísticas de Times".

Guarda a frequência histórica de faltas, chutes (finalizações) e cartões
de cada time rastreado, sempre só do lado DELE (não soma com o
adversário - isso já existe em padroes_time_escanteio, separado, e
continua existindo do jeito que estava). É o equivalente, por time, do que
`padroes_jogador_linha` já faz por jogador.

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
            CREATE TABLE IF NOT EXISTS padroes_time_linha (
                id SERIAL PRIMARY KEY,
                time_id INTEGER NOT NULL REFERENCES times(id),
                tipo VARCHAR(20) NOT NULL,
                linha NUMERIC(5,2) NOT NULL,
                jogos_analisados INTEGER NOT NULL,
                jogos_acima INTEGER NOT NULL,
                frequencia NUMERIC(5,2) NOT NULL,
                media NUMERIC(6,2) NOT NULL,
                atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (time_id, tipo, linha)
            )
            """
        )
        print("Tabela padroes_time_linha OK.")

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
