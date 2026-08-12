"""
Script de migração (rodar 1x, manualmente, no serviço de migração pontual
do Railway) - cria a tabela `padroes_correlacao_categoria`.

Complementa `padroes_correlacao_estatisticas` (estatística do jogo inteiro)
- essa aqui cruza a estatística de um GRUPO de jogadores (por posição:
Goleiro/Defensor/Meio-campista/Atacante) com a de OUTRO grupo, dos dois
times somados, no mesmo jogo. Ex: "quando os ATACANTES sofrem muita falta,
os DEFENSORES do jogo levam mais cartão?" - a API-Football só classifica
jogador nessas 4 categorias amplas (não dá pra separar lateral de zagueiro
ou ponta de centroavante), então essa é a granularidade máxima possível
com o dado disponível.

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
            CREATE TABLE IF NOT EXISTS padroes_correlacao_categoria (
                id SERIAL PRIMARY KEY,
                par VARCHAR(60) NOT NULL UNIQUE,
                categoria_a VARCHAR(5) NOT NULL,
                estatistica_a VARCHAR(20) NOT NULL,
                categoria_b VARCHAR(5) NOT NULL,
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
        print("Tabela padroes_correlacao_categoria OK.")

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
