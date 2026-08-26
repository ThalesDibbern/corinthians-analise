"""
Migração: adiciona `resolvido_manualmente boolean` em `apostas_salvas`.

PRA QUE SERVE:
    Suporta a correção manual de aposta já resolvida (botão "🔧 Corrigir
    manualmente" em /minhas-apostas - cancelar / marcar como acertou /
    marcar como errou). Sem essa coluna, uma correção manual seria
    indistinguível de uma resolução automática, e o script
    `reabrir_apostas_desatualizadas.py` (que recalcula toda aposta
    resolvida contra `historico_recomendacoes` de novo) desfaria a
    correção manual sem avisar na próxima vez que rodasse - a correção
    manual É, por definição, um caso em que o usuário sabe de algo que o
    dado automático não sabe (ou nunca vai saber, ex: evento que a API
    nunca gravou - ver seção 27 da documentação), então ela precisa ficar
    IMUNE à reavaliação automática pra sempre.

    `resolver_apostas_pendentes()` não precisa mudar: ela só lê
    `resultado = 'pendente'`, e uma aposta corrigida manualmente nunca
    fica pendente de novo (a ação "cancelar" apaga a linha, "acertou"/
    "errou" grava o resultado final direto). Só
    `reabrir_apostas_desatualizadas.py` precisa passar a ignorar linhas
    com `resolvido_manualmente = TRUE`.

DEFAULT FALSE: todas as apostas já resolvidas até hoje foram resolvidas
pelo caminho automático, então o valor certo pra elas é FALSE - nenhum
backfill necessário além do DEFAULT.

IDEMPOTENTE: pode rodar mais de uma vez sem estragar nada.

Rodar:
    python migrar_apostas_resolvido_manualmente.py

Variáveis de ambiente necessárias: DATABASE_URL
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]


def coluna_existe(cur, tabela, coluna):
    cur.execute(
        """SELECT 1 FROM information_schema.columns
           WHERE table_name = %s AND column_name = %s""",
        (tabela, coluna),
    )
    return cur.fetchone() is not None


def migrar():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        if coluna_existe(cur, "apostas_salvas", "resolvido_manualmente"):
            print("Coluna 'resolvido_manualmente' já existe - nada a fazer.")
        else:
            cur.execute(
                "ALTER TABLE apostas_salvas "
                "ADD COLUMN resolvido_manualmente boolean NOT NULL DEFAULT FALSE"
            )
            print("Coluna 'resolvido_manualmente' adicionada (DEFAULT FALSE, "
                  "sem backfill necessário).")

        conn.commit()
        print("\n✅ Migração concluída e commitada.")
        print("Lembrete: rodar gerar_schema.py depois e substituir o schema.sql.")

    except Exception:
        conn.rollback()
        print("\n❌ Erro na migração - rollback, nada foi gravado.")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    migrar()
