"""
Script de migração (rodar 1x, manualmente, no serviço de migração pontual
do Railway) - cria a tabela `padroes_correlacao_estatisticas`.

Diferente de Estilo de Jogo / Padrão por Rodada / Zona da Tabela (que são
POR TIME), essa é uma estatística da LIGA como um todo (olhando o TOTAL de
cada jogo - mandante + visitante somados), então não tem `time_id` -
é sempre a mesma tabela pequena, uma linha por par de estatísticas
testado, atualizada toda vez que o motor_padroes.py roda.

Ideia: dentro do mesmo jogo, algumas estatísticas tendem a se mover
juntas (ex: jogo com muito chute tende a ter mais escanteio; jogo com
muita falta tende a ter mais cartão). Guarda, pra cada par testado, a
média geral da estatística B, separada entre "jogos onde A ficou ACIMA
da média" e "jogos onde A ficou ABAIXO da média" - comparar os dois
revela o efeito.

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
            CREATE TABLE IF NOT EXISTS padroes_correlacao_estatisticas (
                id SERIAL PRIMARY KEY,
                par VARCHAR(40) NOT NULL UNIQUE,
                estatistica_a VARCHAR(20) NOT NULL,
                estatistica_b VARCHAR(20) NOT NULL,
                media_a NUMERIC(8,3) NOT NULL,
                valor_b_acima NUMERIC(8,3) NOT NULL,
                valor_b_abaixo NUMERIC(8,3) NOT NULL,
                jogos_acima INTEGER NOT NULL,
                jogos_abaixo INTEGER NOT NULL,
                jogos_total INTEGER NOT NULL,
                atualizado_em TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
        print("Tabela padroes_correlacao_estatisticas OK.")

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
