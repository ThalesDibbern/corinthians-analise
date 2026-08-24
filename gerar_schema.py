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

import base64
import gzip
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
    antes das FKs, que é como se costuma escrever à mão.

    IMPORTANTE (corrigido em 24/08/2026): filtra contype = 'n'. A partir do
    Postgres 17, as restrições NOT NULL passaram a aparecer em
    pg_constraint (antes ficavam só em pg_attribute.attnotnull). Sem esse
    filtro, o DDL gerado saía com linhas INVÁLIDAS do tipo
    `CONSTRAINT x_col_not_null NOT NULL col` - que não é sintaxe de
    CREATE TABLE e quebraria o schema.sql inteiro na hora de rodar. Além
    de redundante: o NOT NULL já sai na definição da coluna."""
    cur.execute(
        """
        SELECT conname, pg_get_constraintdef(oid), contype
        FROM pg_constraint
        WHERE conrelid = %s::regclass
          AND contype IN ('p', 'u', 'f', 'c')
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


LARGURA_BLOCO = 180


def imprimir_empacotado(ddl, tabelas):
    """Imprime o schema comprimido (gzip + base64) em blocos numerados.

    POR QUE NÃO IMPRIMIR O TEXTO DIRETO
    -----------------------------------
    Testado em 24/08/2026 no Railway: imprimir o DDL cru deu errado por
    DOIS motivos ao mesmo tempo.

    1) EMBARALHOU. O coletor de log do Railway atribui timestamps com
       resolução menor que o intervalo entre as linhas, e entrega elas
       FORA DE ORDEM. O schema saiu com colunas de uma tabela no meio de
       outra - ilegível e impossível de reordenar com segurança.
    2) TRUNCOU. O log foi cortado na 23ª de 49 tabelas.

    Comprimir resolve os dois: o gzip reduz o volume o bastante pra não
    truncar, e o número de sequência em cada bloco permite remontar na
    ordem certa mesmo que o log entregue embaralhado.

    Não é elegante, mas é o formato que sobrevive ao transporte."""
    bruto = gzip.compress(ddl.encode("utf-8"), compresslevel=9)
    texto = base64.b64encode(bruto).decode("ascii")
    blocos = [texto[i:i + LARGURA_BLOCO] for i in range(0, len(texto), LARGURA_BLOCO)]

    print("=" * 60)
    print(f"SCHEMA EMPACOTADO - {len(tabelas)} tabelas, {len(blocos)} blocos")
    print("Copie TODAS as linhas que começam com SCHEMA| (a ordem não importa,")
    print("o número no início permite remontar).")
    print("=" * 60)
    for i, bloco in enumerate(blocos):
        print(f"SCHEMA|{i:04d}|{bloco}")
    print("=" * 60)
    print(f"FIM - {len(blocos)} blocos no total")
    print()
    print("Tabelas encontradas:")
    for t in tabelas:
        print(f"  - {t}")


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
        # Modo cru: só serve pra rodar localmente, onde o terminal não
        # reordena nem trunca. No Railway, usar o modo empacotado.
        print(ddl)
        return

    imprimir_empacotado(ddl, tabelas)


if __name__ == "__main__":
    main()
