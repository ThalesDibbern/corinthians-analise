"""
Migração: adiciona `assinatura varchar(64)` em `historico_multiplas_destaque`.

PRA QUE SERVE:
    Corrige a duplicata visível em /historico ("Múltiplas em Destaque"):
    `historico_multiplas_destaque` grava, de propósito (ver comentário
    original em migrar_multiplas_destaque.py), UMA LINHA POR (jogo,
    combinação) - uma múltipla que cruza 2 jogos e entra no top-5 dos
    dois gera 2 linhas, uma pra cada `jogo_id`. Isso é escrita
    intencional (mantida como está - decisão de 26/08/2026), mas a
    página /historico lê tudo achatado numa lista só, sem saber que 2
    linhas são a MESMA combinação - o combo aparece 2x na lista visível
    e conta 2x no resumo agregado (43/101/144/29.9% viravam números
    inflados por causa disso).

    `multiplas_candidatas.assinatura` já é a identidade estável de cada
    combinação (hash de casa_aposta + pernas ordenadas, NUNCA odd/
    probabilidade - ver `_assinatura_combo` em combinacoes.py), com
    UNIQUE constraint. Essa migração só abre espaço pra propagar essa
    mesma identidade pro histórico, pra leitura poder agrupar por ela.

    A ESCRITA continua gerando 2 linhas por combo cross-game (decisão
    consciente - pode servir pra uma visão por-jogo no futuro). Só a
    LEITURA (`buscar_multiplas_destaque`/`montar_resumo_multiplas` em
    app.py) passa a deduplicar por essa coluna.

NULLABLE de propósito: linhas já existentes ficam com `assinatura = NULL`
até o script de backfill (`corrigir_duplicata_multiplas_destaque.py`)
preencher - essa migração só cria a coluna, não backfilla.

IDEMPOTENTE: pode rodar mais de uma vez sem estragar nada.

Rodar:
    python migrar_assinatura_multiplas_destaque.py

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
        if coluna_existe(cur, "historico_multiplas_destaque", "assinatura"):
            print("Coluna 'assinatura' já existe em historico_multiplas_destaque - nada a fazer.")
        else:
            cur.execute(
                "ALTER TABLE historico_multiplas_destaque "
                "ADD COLUMN assinatura VARCHAR(64)"
            )
            print("Coluna 'assinatura' adicionada (NULL até o backfill rodar).")

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_historico_multiplas_assinatura "
            "ON historico_multiplas_destaque (assinatura)"
        )
        print("Índice em 'assinatura' OK.")

        conn.commit()
        print("\n✅ Migração concluída e commitada.")
        print("Próximo passo: rodar corrigir_duplicata_multiplas_destaque.py")
        print("verificar/aplicar pra preencher a assinatura nas 144 linhas já existentes.")
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
