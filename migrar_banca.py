"""
Script de migração (rodar 1x, manualmente, no serviço de migração pontual
do Railway) - adiciona suporte a "banca" (dinheiro fictício, não real) por
usuário.

O que essa migração faz:
  1. Adiciona a coluna `usuarios.banca_atual` (saldo atual da banca de cada
     usuário, começa em 0 - precisa depositar pra ela funcionar).
  2. Cria a tabela `banca_movimentos` - um extrato completo de tudo que
     mexeu na banca (depósito, resgate, valor que saiu quando uma aposta
     foi salva, valor que voltou quando uma aposta foi resolvida). Serve
     tanto pra mostrar o histórico pro usuário quanto pra auditoria/debug
     se algum saldo parecer errado no futuro.

Seguro rodar mais de uma vez - usa IF NOT EXISTS em tudo.

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
            "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS banca_atual NUMERIC(12,2) NOT NULL DEFAULT 0"
        )
        print("Coluna usuarios.banca_atual OK.")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS banca_movimentos (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
                tipo VARCHAR(20) NOT NULL,
                valor NUMERIC(12,2) NOT NULL,
                saldo_apos NUMERIC(12,2) NOT NULL,
                aposta_id INTEGER REFERENCES apostas_salvas(id) ON DELETE SET NULL,
                criado_em TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
        print("Tabela banca_movimentos OK.")

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_banca_movimentos_usuario "
            "ON banca_movimentos (usuario_id, criado_em DESC)"
        )
        print("Índice idx_banca_movimentos_usuario OK.")

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
