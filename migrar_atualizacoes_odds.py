"""
Script de migração (rodar 1x, manualmente, no serviço de migração pontual
do Railway) - cria a tabela `atualizacoes_odds`, usada pra controlar
quando o app dispara um "Run Now" do serviço de odds (`refreshing-freedom`)
via API do Railway, em vez de deixar esse serviço rodando de hora em hora
o dia inteiro (inclusive de madrugada e em dias sem jogo, gastando cota da
OddsPapi à toa).

Guarda só um log (append-only) de cada disparo - o app lê o mais recente
(MAX(criado_em)) pra saber se já rodou na última hora ou não.

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
            CREATE TABLE IF NOT EXISTS atualizacoes_odds (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id),
                forcado BOOLEAN NOT NULL DEFAULT FALSE,
                criado_em TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
        print("Tabela atualizacoes_odds OK.")

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
