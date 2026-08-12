"""
Script de migração (rodar 1x, manualmente, no serviço de migração pontual
do Railway) - prepara o banco pra Fase B: detecção automática de "quebra
de padrão" por rodada (ex: o Vasco começa o campeonato ganhando 3 seguidas,
depois varia, e no segundo turno perde muito mais - o sistema encontra
sozinho ONDE essa mudança acontece, sem faixa fixa definida na mão).

Três coisas:

1) Coluna `rodada_numero` em `jogos` - a coluna `rodada` (texto, ex:
   "Regular Season - 20") já existe desde a sessão anterior; essa nova
   guarda só o número extraído (20), pra agrupar/comparar rodadas sem
   precisar reparsear o texto toda vez.

2) `padroes_rodada_bruto` - o dado bruto, uma linha por (time, mercado,
   número da rodada), juntando as ~5 temporadas coletadas. Pra "resultado"
   guarda taxa de vitória; pros outros mercados (cartão, escanteio, falta,
   chute, chute no gol, impedimento, desarme), guarda a média de eventos
   por jogo daquele time naquela posição de rodada.

3) `padroes_quebra_rodada` - o resultado da detecção automática: em qual
   rodada a maior mudança de comportamento acontece, pra cada
   (time, mercado), calculado deslizando uma janela de algumas rodadas por
   cima do dado bruto e achando onde a diferença entre "antes" e "depois"
   é maior.

Seguro rodar mais de uma vez - usa IF NOT EXISTS / ADD COLUMN IF NOT EXISTS.

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
        cur.execute("ALTER TABLE jogos ADD COLUMN IF NOT EXISTS rodada_numero INTEGER")
        print("Coluna jogos.rodada_numero OK (jogos já existentes ficam NULL até "
              "popular_banco.py passar de novo e fizer backfill).")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS padroes_rodada_bruto (
                id SERIAL PRIMARY KEY,
                time_id INTEGER NOT NULL REFERENCES times(id),
                tipo_padrao VARCHAR(20) NOT NULL,
                rodada_numero INTEGER NOT NULL,
                valor NUMERIC(8,4) NOT NULL,
                jogos_amostra INTEGER NOT NULL,
                atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (time_id, tipo_padrao, rodada_numero)
            )
            """
        )
        print("Tabela padroes_rodada_bruto OK.")

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_padroes_rodada_bruto_time "
            "ON padroes_rodada_bruto (time_id, tipo_padrao)"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS padroes_quebra_rodada (
                id SERIAL PRIMARY KEY,
                time_id INTEGER NOT NULL REFERENCES times(id),
                tipo_padrao VARCHAR(20) NOT NULL,
                rodada_quebra INTEGER NOT NULL,
                valor_antes NUMERIC(8,4) NOT NULL,
                valor_depois NUMERIC(8,4) NOT NULL,
                diferenca NUMERIC(8,4) NOT NULL,
                atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (time_id, tipo_padrao)
            )
            """
        )
        print("Tabela padroes_quebra_rodada OK.")

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_padroes_quebra_rodada_time "
            "ON padroes_quebra_rodada (time_id)"
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
