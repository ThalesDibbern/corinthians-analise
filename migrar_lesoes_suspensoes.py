"""
Migração: cria a tabela `lesoes_suspensoes`, que guarda a lista de
jogadores machucados/suspensos reportada pela API-Football pra cada jogo
próximo (endpoint /injuries?fixture=X).

Rodar uma vez só, via o serviço `abundant-abundance` (ou qualquer serviço
com DATABASE_URL configurada), depois é descartável.
"""
import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]


def main():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS lesoes_suspensoes (
                id SERIAL PRIMARY KEY,
                jogo_id INTEGER NOT NULL REFERENCES jogos(id) ON DELETE CASCADE,
                jogador_id INTEGER REFERENCES jogadores(id),
                jogador_nome_api VARCHAR(255),
                tipo VARCHAR(255),
                motivo VARCHAR(255),
                dados_brutos JSONB,
                atualizado_em TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_lesoes_suspensoes_jogo ON lesoes_suspensoes(jogo_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_lesoes_suspensoes_jogador ON lesoes_suspensoes(jogador_id)"
        )

        conn.commit()
        print("Migração concluída: tabela `lesoes_suspensoes` pronta.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
