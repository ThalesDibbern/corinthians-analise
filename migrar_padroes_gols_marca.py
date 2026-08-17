"""
Script de migração (rodar 1x, manualmente, no serviço de migração pontual
do Railway) - cria as tabelas novas usadas pelos mercados de "Mais/Menos
gols" (total do jogo) e "Equipe Marca".

NÃO cria tabela pra "gols do time" (Mais/Menos gols só do nosso lado) -
esse reaproveita a tabela `padroes_time_linha` que já existe (mesma usada
por falta/chute/cartão do time), só com um novo valor de `tipo` ("gols").

Cria 2 tabelas:
  - padroes_gols_total: gols do jogo INTEIRO (mandante + visitante
    somados) por linha - mesmo formato de padroes_escanteio_total/
    padroes_cartao_total, que já existem.
  - padroes_time_marca: frequência binária (Sim/Não) de jogos em que o
    time marcou pelo menos 1 gol - um registro por time, sem linha/
    handicap (mercado binário, não de linha).

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
            CREATE TABLE IF NOT EXISTS padroes_gols_total (
                id SERIAL PRIMARY KEY,
                time_id INTEGER NOT NULL REFERENCES times(id),
                linha NUMERIC(5,2) NOT NULL,
                jogos_analisados INTEGER NOT NULL,
                jogos_acima_da_linha INTEGER NOT NULL,
                frequencia NUMERIC(5,2) NOT NULL,
                media NUMERIC(6,2) NOT NULL,
                atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (time_id, linha)
            )
            """
        )
        print("Tabela padroes_gols_total OK.")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS padroes_time_marca (
                id SERIAL PRIMARY KEY,
                time_id INTEGER NOT NULL REFERENCES times(id),
                jogos_analisados INTEGER NOT NULL,
                jogos_que_marcou INTEGER NOT NULL,
                frequencia NUMERIC(5,2) NOT NULL,
                atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (time_id)
            )
            """
        )
        print("Tabela padroes_time_marca OK.")

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
