"""
Gera o `schema.sql` do projeto a partir do banco REAL.

POR QUE ISSO EXISTE
-------------------
Levantamento de 22/08/2026: das ~34 tabelas em uso, só 18 têm um script de
migração no repositório. As outras foram criadas ao longo do tempo por
scripts que nunca foram commitados, ou direto no console do Postgres.

Consequência: **o repositório não consegue reconstruir o banco**. Se o
Postgres do Railway fosse perdido, seria preciso recriar tabela por tabela
adivinhando os tipos de coluna a partir dos INSERT/SELECT espalhados pelo
código.

Esse script resolve isso de uma vez: lê o catálogo do próprio Postgres e
escreve o DDL completo (colunas, tipos, defaults, chaves e índices) num
arquivo `schema.sql`, que passa a ser a planta oficial do banco.

Diferente das migrações, que descrevem MUDANÇAS, esse arquivo descreve o
ESTADO. Os dois têm valor: a migração conta a história, o schema garante
a reconstrução.

SÓ LEITURA. Não cria, não altera e não apaga nada. Pode rodar com o site
no ar e os crons ativos.

Rodar:
    python gerar_schema.py            # escreve schema.sql na pasta atual
    python gerar_schema.py --stdout   # imprime na tela (útil no Railway,
                                      # onde o log é o que dá pra ver)

Variáveis de ambiente necessárias: DATABASE_URL
"""

import os
import sys
from datetime import datetime, timezone

import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]


def listar_tabelas(cur):
    """Todas as tabelas normais do schema public, em ordem alfabética.
    Ignora views e tabelas de sistema."""
    cur.execute(
        """
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind = 'r'
        ORDER BY c.relname
        """
    )
    return [row[0] for row in cur.fetchall()]


def colunas_da_tabela(cur, tabela):
    """Nome, tipo exato, default e nullability de cada coluna.

    Usa pg_catalog em vez de information_schema porque `format_type` devolve
    o tipo já formatado do jeito que o Postgres escreveria de volta
    (ex: `character varying(20)`, `numeric(5,2)`) - o information_schema
    obriga a remontar isso na mão a partir de várias colunas separadas."""
    cur.execute(
        """
        SELECT a.attname,
               pg_catalog.format_type(a.atttypid, a.atttypmod),
               pg_get_expr(d.adbin, d.adrelid),
               a.attnotnull
        FROM pg_attribute a
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = %s::regclass
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (tabela,),
    )
    return cur.fetchall()


def constraints_da_tabela(cur, tabela):
    """Chave primária, estrangeiras, unique e checks - já no formato SQL
    pronto, via pg_get_constraintdef. Ordena por tipo pra a PK aparecer
    antes das FKs, que é como se costuma escrever à mão."""
    cur.execute(
        """
        SELECT conname, pg_get_constraintdef(oid), contype
        FROM pg_constraint
        WHERE conrelid = %s::regclass
        ORDER BY CASE contype
                     WHEN 'p' THEN 1
                     WHEN 'u' THEN 2
                     WHEN 'f' THEN 3
                     ELSE 4
                 END,
                 conname
        """,
        (tabela,),
    )
    return cur.fetchall()


def indices_da_tabela(cur, tabela):
    """Índices que NÃO vêm de constraint (esses já saem no CREATE TABLE).
    Sem esse filtro, o índice da chave primária apareceria duas vezes."""
    cur.execute(
        """
        SELECT indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = %s
          AND indexname NOT IN (
              SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass
          )
        ORDER BY indexname
        """,
        (tabela, tabela),
    )
    return [row[0] for row in cur.fetchall()]


def montar_definicao_coluna(nome, tipo, default, not_null):
    """Monta a linha da coluna. Converte o par (integer + nextval) de volta
    pra SERIAL, que é como a coluna foi declarada originalmente - manter o
    nextval cru exigiria criar a sequence à parte pra o script rodar num
    banco vazio."""
    if default and "nextval(" in default:
        tipo_serial = {
            "integer": "SERIAL",
            "bigint": "BIGSERIAL",
            "smallint": "SMALLSERIAL",
        }.get(tipo)
        if tipo_serial:
            return f"    {nome} {tipo_serial}"

    partes = [f"    {nome} {tipo}"]
    if default:
        partes.append(f"DEFAULT {default}")
    if not_null:
        partes.append("NOT NULL")
    return " ".join(partes)


def gerar_ddl(cur):
    linhas = []
    agora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    linhas.append("-- ============================================================")
    linhas.append("-- SCHEMA DO PROJETO - gerado automaticamente por gerar_schema.py")
    linhas.append(f"-- Gerado em: {agora}")
    linhas.append("--")
    linhas.append("-- Este arquivo descreve o ESTADO do banco, não as mudanças.")
    linhas.append("-- Serve pra reconstruir tudo do zero e pra consultar o tipo real")
    linhas.append("-- de uma coluna sem precisar abrir o Postgres.")
    linhas.append("--")
    linhas.append("-- NÃO editar à mão: rodar gerar_schema.py de novo depois de")
    linhas.append("-- qualquer migração, pra manter fiel ao banco de verdade.")
    linhas.append("-- ============================================================")
    linhas.append("")

    tabelas = listar_tabelas(cur)
    linhas.append(f"-- {len(tabelas)} tabelas encontradas.")
    linhas.append("")

    for tabela in tabelas:
        linhas.append("")
        linhas.append(f"-- ---------- {tabela} ----------")
        linhas.append(f"CREATE TABLE IF NOT EXISTS {tabela} (")

        partes = [
            montar_definicao_coluna(nome, tipo, default, not_null)
            for nome, tipo, default, not_null in colunas_da_tabela(cur, tabela)
        ]
        for nome_c, definicao, _tipo in constraints_da_tabela(cur, tabela):
            partes.append(f"    CONSTRAINT {nome_c} {definicao}")

        linhas.append(",\n".join(partes))
        linhas.append(");")

        for indexdef in indices_da_tabela(cur, tabela):
            linhas.append(f"{indexdef};")

    linhas.append("")
    return "\n".join(linhas), tabelas


def main():
    para_tela = "--stdout" in sys.argv

    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        ddl, tabelas = gerar_ddl(cur)
        cur.close()
    finally:
        conn.close()

    if para_tela:
        print(ddl)
        return

    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    with open(caminho, "w", encoding="utf-8") as f:
        f.write(ddl)

    print(f"schema.sql gerado com {len(tabelas)} tabelas em {caminho}")
    print()
    print("Tabelas:")
    for t in tabelas:
        print(f"  - {t}")


if __name__ == "__main__":
    main()
