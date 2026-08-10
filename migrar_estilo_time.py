"""
Script de migração (rodar 1x, manualmente, no serviço de migração pontual
do Railway) - cria a tabela `padroes_estilo_time`, usada pela Fase 1 do
recurso "estilo de jogo por time" (ver /time/<id> em app.py e
motor_padroes.py/calcular_estilo_times).

Guarda, pra cada time rastreado, o quanto ele foge da média entre os times
rastreados em dois pilares - OFENSIVO (o quanto o próprio time gera um
evento, ex: joga bola longa e sofre mais impedimento) e DEFENSIVO (o
quanto ele influencia o ADVERSÁRIO a gerar mais/menos esse evento, ex:
joga recuado e faz o ataque adversário cair menos em impedimento) - só
pra impedimento e cartão por enquanto (escopo reduzido de propósito, pra
validar a ideia antes de expandir).

IMPORTANTE: essa é a Fase 1 do recurso - só alimenta uma exibição visual
na tela do time, não entra em nenhum cálculo de recomendação/VE ainda.

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
            CREATE TABLE IF NOT EXISTS padroes_estilo_time (
                id SERIAL PRIMARY KEY,
                time_id INTEGER NOT NULL REFERENCES times(id),
                tipo VARCHAR(20) NOT NULL,
                papel VARCHAR(20) NOT NULL,
                media_time NUMERIC(6,3) NOT NULL,
                media_liga NUMERIC(6,3) NOT NULL,
                jogos_analisados INTEGER NOT NULL,
                fator NUMERIC(6,3) NOT NULL,
                atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (time_id, tipo, papel)
            )
            """
        )
        print("Tabela padroes_estilo_time OK.")

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_padroes_estilo_time_time_id ON padroes_estilo_time (time_id)"
        )
        print("Índice idx_padroes_estilo_time_time_id OK.")

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
