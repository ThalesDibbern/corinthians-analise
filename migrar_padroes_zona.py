"""
Script de migração (rodar 1x, manualmente, no serviço de migração pontual
do Railway) - cria a tabela `padroes_zona_time`, base da Fase C: como
cada time se comporta dependendo da ZONA da tabela em que estava ANTES de
cada jogo (G4/meio/Z4), e como isso muda dependendo do resultado do jogo
ANTERIOR (efeito de sequência/momento - "ganhou 2 seguidas e sai da zona"
vs "perdeu 2 seguidas e afunda mais").

`condicao` guarda 4 valores possíveis:
  - "geral"         -> todo jogo em que o time estava naquela zona, sem
                        filtrar pelo resultado do jogo anterior
  - "apos_vitoria"   -> só os jogos em que, além de estar naquela zona, o
                        time tinha VENCIDO o jogo anterior (mesma temporada)
  - "apos_empate"    -> idem, mas empatou o jogo anterior
  - "apos_derrota"   -> idem, mas perdeu o jogo anterior

Comparar "apos_vitoria" com "apos_derrota" dentro da MESMA zona é o que
revela o efeito de sequência/momento.

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
            CREATE TABLE IF NOT EXISTS padroes_zona_time (
                id SERIAL PRIMARY KEY,
                time_id INTEGER NOT NULL REFERENCES times(id),
                tipo_padrao VARCHAR(20) NOT NULL,
                zona VARCHAR(10) NOT NULL,
                condicao VARCHAR(20) NOT NULL,
                valor NUMERIC(8,4) NOT NULL,
                jogos_amostra INTEGER NOT NULL,
                atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (time_id, tipo_padrao, zona, condicao)
            )
            """
        )
        print("Tabela padroes_zona_time OK.")

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_padroes_zona_time_time "
            "ON padroes_zona_time (time_id)"
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
