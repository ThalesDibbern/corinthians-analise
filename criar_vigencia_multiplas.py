"""
criar_vigencia_multiplas.py — cria o que as múltiplas vigentes precisam
(`claude/arquitetura_multiplas_vigentes.md §3.1 e §3.4`), no `public` E nas
5 ligas. Duas fases:

    python criar_vigencia_multiplas.py verificar   # só lê; diz o que falta
    python criar_vigencia_multiplas.py aplicar     # cria, confere, commit

O QUE CRIA (tudo com IF NOT EXISTS — rodar de novo não muda nada):
  - `<schema>.multiplas_candidatas.capturada_cron_em` (timestamptz, NULL)
  - `<schema>.multiplas_candidatas.vigente_no_apito`  (boolean, NULL = desconhecido)
  - `<schema>.multiplas_lotes (id serial, rodou_em timestamptz, capturadas int,
    versao_codigo text)`, com sequência PRÓPRIA do schema e índice em rodou_em
  em `public` e em cada `liga_*` do `rodar_liga.LIGAS`. `GRANT SELECT` ao
  `claude_leitura` (se o role existir).

🔴 POR QUE AS LIGAS JUNTO: o `rodar_liga.py` recusa a liga inteira se uma
tabela de `public` não existir no schema dela (foi o crash de 30-31/10 com a
`multiplas_retiradas`). A `multiplas_lotes` nasce no `public` e nas 5 ligas
na MESMA transação.

PROVAS, antes do commit (qualquer uma falha = rollback, nada fica):
  - pelo catálogo: as 2 colunas existem nas 6 `multiplas_candidatas`; a
    `multiplas_lotes` existe nos 6 schemas e cada `id` usa sequência do
    PRÓPRIO schema;
  - V6: `recomendacoes`, `historico_recomendacoes`, `registro_publico`,
    `registro_modelo` e `multiplas_candidatas` (colunas antigas) com contagem
    e md5 iguais antes e depois.
DEPOIS do commit: a trava do `rodar_liga` (importada) roda nas 5 ligas.

Ninguém lê nada disso até o push do código novo — pode rodar a qualquer
hora fora das janelas dos crons (o ALTER TABLE pega um bloqueio curto em
`multiplas_candidatas`).

Variáveis de ambiente: DATABASE_URL.
"""

import sys
from datetime import datetime

import psycopg2

import rodar_liga
from rodar_liga import LIGAS, SQL_SEQUENCIAS, TravaRecusou, schema_de, trava

DATABASE_URL = rodar_liga.DATABASE_URL
VERSAO = "criar_vigencia_multiplas 03/11/2026 v1"

COLUNAS_NOVAS = {"capturada_cron_em": "timestamp with time zone", "vigente_no_apito": "boolean"}

# V6: o que não pode mudar. multiplas_candidatas pelas colunas que já existiam.
VIGIADAS = {
    "recomendacoes": "t.*",
    "historico_recomendacoes": "t.*",
    "registro_publico": "t.*",
    "registro_modelo": "t.*",
    "multiplas_candidatas": ("t.id, t.assinatura, t.casa_aposta, t.descricao, t.odd_combinada, "
                             "t.probabilidade_combinada, t.pernas, t.jogos, t.primeiro_apito, t.congelada, "
                             "t.avaliada, t.criada_em, t.atualizada_em, t.resultado"),
}


def schemas():
    return ["public"] + [schema_de(c) for c in LIGAS]


def existe_schema(cur, s):
    cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (s,))
    return cur.fetchone() is not None


def existe_tabela(cur, s, t):
    cur.execute("SELECT 1 FROM pg_tables WHERE schemaname = %s AND tablename = %s", (s, t))
    return cur.fetchone() is not None


def colunas(cur, s, t):
    cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s", (s, t))
    return dict(cur.fetchall())


def o_que_falta(cur):
    """Lista (schema, item) do que ainda não existe."""
    falta = []
    for s in schemas():
        if not existe_schema(cur, s):
            raise TravaRecusou(f"schema `{s}` não existe")
        if not existe_tabela(cur, s, "multiplas_candidatas"):
            raise TravaRecusou(f"`{s}.multiplas_candidatas` não existe — rode antes `rodar_liga.py preparar`")
        cols = colunas(cur, s, "multiplas_candidatas")
        for c, tipo in COLUNAS_NOVAS.items():
            if c not in cols:
                falta.append((s, f"coluna multiplas_candidatas.{c}"))
            elif cols[c] != tipo:
                raise TravaRecusou(f"`{s}.multiplas_candidatas.{c}` existe com tipo `{cols[c]}` (esperado `{tipo}`)")
        if not existe_tabela(cur, s, "multiplas_lotes"):
            falta.append((s, "tabela multiplas_lotes"))
    return falta


def foto(cur):
    """Contagem + md5 das tabelas vigiadas, em cada schema onde existem."""
    out = {}
    for s in schemas():
        for t, cols in VIGIADAS.items():
            if not existe_tabela(cur, s, t):
                continue
            cur.execute(f"SELECT COUNT(*), md5(COALESCE(string_agg(md5(ROW({cols})::text), '' ORDER BY t.id), '')) "
                        f"FROM {s}.{t} t")
            out[f"{s}.{t}"] = tuple(cur.fetchone())
    return out


def criar(cur, s):
    cur.execute(f"ALTER TABLE {s}.multiplas_candidatas "
                "ADD COLUMN IF NOT EXISTS capturada_cron_em TIMESTAMPTZ, "
                "ADD COLUMN IF NOT EXISTS vigente_no_apito BOOLEAN")
    # SERIAL num nome qualificado cria a sequência no MESMO schema
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {s}.multiplas_lotes (
            id SERIAL PRIMARY KEY,
            rodou_em TIMESTAMPTZ NOT NULL,
            capturadas INTEGER NOT NULL,
            versao_codigo TEXT
        )""")
    cur.execute(f"CREATE INDEX IF NOT EXISTS multiplas_lotes_rodou_em_idx ON {s}.multiplas_lotes (rodou_em)")
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'claude_leitura'")
    if cur.fetchone():
        cur.execute(f"GRANT SELECT ON {s}.multiplas_lotes TO claude_leitura")


def provar_catalogo(cur):
    problemas = []
    for s in schemas():
        cols = colunas(cur, s, "multiplas_candidatas")
        for c, tipo in COLUNAS_NOVAS.items():
            if cols.get(c) != tipo:
                problemas.append(f"`{s}.multiplas_candidatas.{c}`: {cols.get(c)!r}")
        if not existe_tabela(cur, s, "multiplas_lotes"):
            problemas.append(f"`{s}.multiplas_lotes` não existe")
        cur.execute(SQL_SEQUENCIAS, (s,))
        for tabela, coluna, schema_seq, seq in cur.fetchall():
            if tabela == "multiplas_lotes" and schema_seq != s:
                problemas.append(f"`{s}.multiplas_lotes.{coluna}` usa sequência de fora: {schema_seq}.{seq}")
        cur.execute("SELECT COUNT(*) FROM pg_attrdef d JOIN pg_class c ON c.oid = d.adrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = %s AND c.relname = 'multiplas_lotes'", (s,))
        if cur.fetchone()[0] < 1:
            problemas.append(f"`{s}.multiplas_lotes.id` sem DEFAULT")
    return problemas


def main(argv):
    if len(argv) != 2 or argv[1] not in ("verificar", "aplicar"):
        print(__doc__)
        return 2
    fase = argv[1]
    print(f"=== {VERSAO} · fase {fase.upper()} · argv {argv[1:]} · {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        falta = o_que_falta(cur)
        print(f"\nschemas: {', '.join(schemas())}")
        print(f"{len(falta)} item(ns) a criar:")
        for s, item in falta:
            print(f"   {s}: {item}")
        if fase == "verificar":
            conn.rollback()
            print("\nVERIFICAR — nada foi criado; nenhuma tabela foi tocada (rollback).")
            return 0

        antes = foto(cur)
        for s in schemas():
            criar(cur, s)
        problemas = provar_catalogo(cur)
        depois = foto(cur)
        for k in sorted(set(antes) | set(depois)):
            if antes.get(k) != depois.get(k):
                problemas.append(f"V6: `{k}` mudou ({antes.get(k)} → {depois.get(k)})")
        if problemas:
            conn.rollback()
            print("\n🔴 PROVAS FALHARAM — rollback, nada ficou:\n   - " + "\n   - ".join(problemas))
            return 1
        conn.commit()
        print(f"\n✅ criado e conferido pelo catálogo nos {len(schemas())} schemas; "
              f"V6 ok em {len(antes)} tabela(s) (contagem + md5 iguais).")
        for k in sorted(antes):
            print(f"   {k}: {antes[k][0]} linha(s)")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    print("\n== trava do rodar_liga depois do commit ==")
    ok = True
    for chave in LIGAS:
        try:
            print(f"   {chave}: {trava(chave)} tabelas do pipeline resolvem para `{schema_de(chave)}` ✅")
        except TravaRecusou as e:
            ok = False
            print(f"   🔴 {chave}: {e}")
    if not ok:
        print("\n🔴 a trava recusou alguma liga — o cron das ligas vai falhar. Não fazer o push; abrir o log.")
        return 1
    print("\n✅ pronto. O código novo pode subir na janela combinada.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
