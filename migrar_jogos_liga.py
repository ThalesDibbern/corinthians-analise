"""
Script de migração (rodar 1x, manualmente, no serviço de migração pontual
do Railway) - cria a tabela `jogos_liga`.

Essa tabela é diferente de `jogos`: `jogos` guarda dado RICO (escalação,
cartão, estatística de jogador) só dos times RASTREADOS, com uma linha
por (jogo, time rastreado). `jogos_liga` guarda dado LEVE (só placar,
data e rodada) de TODO MUNDO no Brasileirão - inclusive confrontos entre
dois times que o projeto nunca vai rastrear de verdade (ex: Bahia x
Vitória). É o único jeito de saber a posição de um time na tabela em
qualquer rodada do passado, já que a API-Football não oferece "tabela
histórica por rodada" pronta (só a atual/final) - o cálculo de posição
tem que ser feito por conta própria, a partir do placar de cada jogo.

Existir como tabela separada (em vez de misturar dentro de `jogos`) é de
propósito: os dois têm finalidade e formato bem diferentes, e o projeto já
aprendeu (documentado várias vezes) que misturar responsabilidades
diferentes no mesmo lugar é fonte de bug.

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
            CREATE TABLE IF NOT EXISTS jogos_liga (
                id SERIAL PRIMARY KEY,
                fixture_id_api INTEGER NOT NULL UNIQUE,
                temporada INTEGER NOT NULL,
                rodada VARCHAR(50),
                rodada_numero INTEGER,
                data_jogo DATE NOT NULL,
                mandante_api_id INTEGER NOT NULL,
                mandante_nome VARCHAR(100) NOT NULL,
                visitante_api_id INTEGER NOT NULL,
                visitante_nome VARCHAR(100) NOT NULL,
                placar_mandante INTEGER,
                placar_visitante INTEGER,
                status VARCHAR(10),
                atualizado_em TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
        print("Tabela jogos_liga OK.")

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_jogos_liga_temporada_rodada "
            "ON jogos_liga (temporada, rodada_numero)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_jogos_liga_mandante "
            "ON jogos_liga (mandante_api_id, temporada)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_jogos_liga_visitante "
            "ON jogos_liga (visitante_api_id, temporada)"
        )
        print("Índices de jogos_liga OK.")

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
