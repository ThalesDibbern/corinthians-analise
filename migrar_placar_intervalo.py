"""
Script de migração (rodar 1x, manualmente, no serviço de migração pontual
do Railway) - parte 1 da Onda 2 (Dupla Chance/Ambas Marcam por tempo,
Marca em Ambos os Tempos).

Faz duas coisas:
  1. Adiciona 2 colunas em `jogos`: placar_corinthians_intervalo e
     placar_adversario_intervalo - o placar no intervalo (fixture["score"]
     ["halftime"] da API-Football), mesma convenção de "nosso time"/
     "adversário" que placar_corinthians/placar_adversario já usam (não é
     nome fixo de time - representa sempre o time da PERSPECTIVA daquela
     linha de `jogos`).
  2. Cria as 3 tabelas novas de padrão: padroes_dupla_chance_tempo,
     padroes_ambas_marcam_tempo, padroes_marca_ambos_tempos.

Depois de rodar esse script, ainda falta rodar backfill_placar_intervalo.py
(script separado, pontual) pra preencher o placar de intervalo dos jogos
que já foram coletados antes dessa coluna existir (2022 até agora) - sem
isso, as duas colunas novas ficam NULL pra todo o histórico já salvo, e o
motor_padroes.py não tem amostra nenhuma pra calcular frequência.

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
        cur.execute("ALTER TABLE jogos ADD COLUMN IF NOT EXISTS placar_corinthians_intervalo INTEGER")
        cur.execute("ALTER TABLE jogos ADD COLUMN IF NOT EXISTS placar_adversario_intervalo INTEGER")
        print("Colunas de placar de intervalo em `jogos` OK.")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS padroes_dupla_chance_tempo (
                id SERIAL PRIMARY KEY,
                time_id INTEGER NOT NULL REFERENCES times(id),
                periodo VARCHAR(2) NOT NULL,   -- '1T' ou '2T'
                lado VARCHAR(10) NOT NULL,     -- 'mandante' ou 'visitante'
                resultado VARCHAR(3) NOT NULL, -- '1X', '12' ou '2X'
                jogos_analisados INTEGER NOT NULL,
                frequencia NUMERIC(5,2) NOT NULL,
                atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (time_id, periodo, lado, resultado)
            )
            """
        )
        print("Tabela padroes_dupla_chance_tempo OK.")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS padroes_ambas_marcam_tempo (
                id SERIAL PRIMARY KEY,
                time_id INTEGER NOT NULL REFERENCES times(id),
                periodo VARCHAR(2) NOT NULL,   -- '1T' ou '2T'
                lado VARCHAR(10) NOT NULL,     -- 'mandante' ou 'visitante'
                jogos_analisados INTEGER NOT NULL,
                jogos_com_ambas INTEGER NOT NULL,
                frequencia NUMERIC(5,2) NOT NULL,
                atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (time_id, periodo, lado)
            )
            """
        )
        print("Tabela padroes_ambas_marcam_tempo OK.")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS padroes_marca_ambos_tempos (
                id SERIAL PRIMARY KEY,
                time_id INTEGER NOT NULL REFERENCES times(id),
                jogos_analisados INTEGER NOT NULL,
                jogos_que_marcou_nos_dois INTEGER NOT NULL,
                frequencia NUMERIC(5,2) NOT NULL,
                atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (time_id)
            )
            """
        )
        print("Tabela padroes_marca_ambos_tempos OK.")

        conn.commit()
        print("\nMigração concluída com sucesso.")
        print("Próximo passo: rodar backfill_placar_intervalo.py pra preencher o histórico já coletado.")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a migração: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
