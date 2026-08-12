"""
Script de migração (rodar 1x, manualmente, no serviço de migração pontual
do Railway) - cria duas tabelas novas:

1) `padroes_correlacao_categoria_time` - a versão POR TIME de
   `padroes_correlacao_categoria`. Diferença importante: a versão geral
   soma os DOIS lados juntos (atacantes dos dois times x defensores dos
   dois times); essa aqui isola a DIREÇÃO - só os atacantes DO PRÓPRIO
   time (categoria A) contra os defensores DO ADVERSÁRIO (categoria B),
   no mesmo jogo. Revela se o ataque de um time específico tem esse
   efeito mais forte/fraco que a média da liga.

2) `padroes_correlacao_jogador` - a versão POR JOGADOR NOMEADO. Pra cada
   jogador atacante com jogos suficientes, calcula a MÉDIA PESSOAL dele
   de uma estatística (ex: faltas sofridas), e separa os jogos dele em
   "acima da própria média" vs "abaixo" - comparando o efeito no time
   ADVERSÁRIO daquele jogo (ex: quantos cartões os defensores do
   adversário levaram). NÃO afirma qual jogador especificamente marcou
   quem (a API-Football não informa isso) - só que, nos jogos em que ESSE
   jogador sofreu mais falta que o normal dele, o adversário como um todo
   reagiu de um jeito ou de outro.

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
            CREATE TABLE IF NOT EXISTS padroes_correlacao_categoria_time (
                id SERIAL PRIMARY KEY,
                time_id INTEGER NOT NULL REFERENCES times(id),
                par VARCHAR(60) NOT NULL,
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
                atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (time_id, par)
            )
            """
        )
        print("Tabela padroes_correlacao_categoria_time OK.")

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_padroes_correlacao_categoria_time_time "
            "ON padroes_correlacao_categoria_time (time_id)"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS padroes_correlacao_jogador (
                id SERIAL PRIMARY KEY,
                jogador_id INTEGER NOT NULL REFERENCES jogadores(id),
                par VARCHAR(60) NOT NULL,
                estatistica_a VARCHAR(20) NOT NULL,
                categoria_b VARCHAR(5) NOT NULL,
                estatistica_b VARCHAR(20) NOT NULL,
                media_pessoal NUMERIC(8,3) NOT NULL,
                valor_b_acima NUMERIC(8,3) NOT NULL,
                valor_b_abaixo NUMERIC(8,3) NOT NULL,
                jogos_acima INTEGER NOT NULL,
                jogos_abaixo INTEGER NOT NULL,
                jogos_total INTEGER NOT NULL,
                atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (jogador_id, par)
            )
            """
        )
        print("Tabela padroes_correlacao_jogador OK.")

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_padroes_correlacao_jogador_jogador "
            "ON padroes_correlacao_jogador (jogador_id)"
        )
        print("Índices OK.")

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
