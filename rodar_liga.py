"""
rodar_liga.py — roda o pipeline do Brasileirão, SEM CÓPIA DE CÓDIGO, no schema
de uma liga extra (`liga_<chave>`). Fase 3 da `claude/arquitetura_ligas_extras.md`.

    python rodar_liga.py preparar verificar --liga premier
    python rodar_liga.py preparar aplicar   --liga premier
    python rodar_liga.py diario --liga premier
    python rodar_liga.py odds   --liga premier
    (--liga aceita várias: --liga premier,laliga)

COMO: cada script do repositório roda como SUBPROCESSO, com
  - PGOPTIONS="-c search_path=liga_<chave>,public" — a libpq aplica isso a toda
    conexão do subprocesso, então `jogos`, `odds`, `padroes_*`... resolvem para
    o schema da liga; o que só existe em `public` (cota, registro público,
    tabelas do site) continua em `public`;
  - as variáveis da Fase 2 (liga na API-Football, torneio na OddsPapi, nome da
    competição, temporada pela coluna, tamanho da liga e das zonas, calendário
    por torneio, LIGA_CHAVE do registro público).
  Nada disso fica no painel do Railway: só existe dentro do subprocesso.

🔴 A TRAVA roda ANTES DE CADA SCRIPT, com a mesma configuração de conexão do
subprocesso: toda tabela de `public` que NÃO é compartilhada tem que resolver
para o schema da liga, toda compartilhada para `public`, e nenhuma sequência
do schema da liga pode ser de fora. Qualquer desvio: a liga para ali.

O QUE NÃO RODA NAS LIGAS:
  - `limpar_historico.py` — apaga jogo antigo que "nenhum jogador precisa"
    olhando os últimos 50 jogos da TABELA INTEIRA (não por time). Numa liga,
    apagaria temporadas inteiras que a coleta trouxe (13/10/2026).
  - `popular_banco.py` — a coleta das ligas é o `coletar_ligas.py`.
  - `registrar_modelo.py` — cota da chave grátis; fica para depois.

MODOS:
  preparar  cria no schema da liga as tabelas do pipeline que ainda não
            existem (LIKE public, sequência PRÓPRIA, leitura para
            `claude_leitura`). Duas fases. Não toca em `public`.
  diario    popular_tabela → motor_padroes → arquivar_recomendacoes
  odds      atualizar_odds → sincronizar_datas_jogos aplicar --somente-futuros
            → motor_recomendacoes → motor_combinacoes → registrar_publico aplicar
  Dentro de uma liga, falhou um script, a liga para (como o `&&` do cron).

MODO SOMBRA (arquitetura §5): as recomendações ficam no schema da liga; o site
lê só `public`, então nada aparece como aposta. O registro público recebe as
linhas com `liga = <chave>`.
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]
PASTA = os.path.dirname(os.path.abspath(__file__))

# chave: configuração da liga. Zonas: vagas diretas de cima / rebaixamento direto.
LIGAS = {
    "premier":    {"api": 39,  "torneio": 17, "nome": "Premier League",   "times": 20, "rodadas": 38, "topo": 4, "rebaixa": 3},
    "laliga":     {"api": 140, "torneio": 8,  "nome": "La Liga",          "times": 20, "rodadas": 38, "topo": 4, "rebaixa": 3},
    "seriea":     {"api": 135, "torneio": 23, "nome": "Serie A (Itália)", "times": 20, "rodadas": 38, "topo": 4, "rebaixa": 3},
    "bundesliga": {"api": 78,  "torneio": 35, "nome": "Bundesliga",       "times": 18, "rodadas": 34, "topo": 4, "rebaixa": 2},
    "ligue1":     {"api": 61,  "torneio": 34, "nome": "Ligue 1",          "times": 18, "rodadas": 34, "topo": 3, "rebaixa": 2},
}

# Ficam SÓ em `public`, de propósito (arquitetura §2.2): cota, site, registros.
TABELAS_COMPARTILHADAS = {
    "controle_api_uso", "usuarios", "apostas_salvas", "banca_movimentos",
    "execucoes_atualizacao", "atualizacoes_odds", "combinacoes_sugeridas",
    "registro_publico", "registro_modelo",
}

SEQUENCIA = {
    "diario": [
        ["popular_tabela.py"],
        ["motor_padroes.py"],
        ["arquivar_recomendacoes.py"],
    ],
    "odds": [
        ["atualizar_odds.py"],
        ["sincronizar_datas_jogos.py", "aplicar", "--somente-futuros"],
        ["motor_recomendacoes.py"],
        ["motor_combinacoes.py"],
        ["registrar_publico.py", "aplicar"],
    ],
}

# Contagens de `public` antes e depois de cada liga — o contador que pode
# derrubar a promessa de isolamento.
TABELAS_PUBLIC_VIGIADAS = ["jogos", "odds", "recomendacoes", "historico_recomendacoes",
                           "multiplas_candidatas", "padroes_gols_total", "jogos_liga", "times"]


class TravaRecusou(Exception):
    pass


def schema_de(chave):
    return f"liga_{chave}"


def ambiente_da_liga(chave):
    c = LIGAS[chave]
    env = dict(os.environ)
    env.update({
        "PGOPTIONS": f"-c search_path={schema_de(chave)},public",
        "API_FOOTBALL_LEAGUE_ID": str(c["api"]),
        "ODDSPAPI_TOURNAMENT_ID": str(c["torneio"]),
        "NOME_COMPETICAO": c["nome"],
        "CALENDARIO_POR_TORNEIO": "1",
        "TEMPORADA_POR_COLUNA": "1",
        "TOTAL_TIMES_LIGA": str(c["times"]),
        "TOTAL_RODADAS_LIGA": str(c["rodadas"]),
        "TAMANHO_ZONA_TOPO": str(c["topo"]),
        "TAMANHO_ZONA_REBAIXAMENTO": str(c["rebaixa"]),
        "LIGA_CHAVE": chave,
    })
    return env


def conectar(search_path=None):
    if search_path:
        return psycopg2.connect(DATABASE_URL, options=f"-c search_path={search_path}")
    return psycopg2.connect(DATABASE_URL)


# ---------------------------------------------------------------------------
# catálogo
# ---------------------------------------------------------------------------
def tabelas_public(cur):
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
    return [r[0] for r in cur.fetchall()]


def tabelas_do_pipeline(cur):
    return [t for t in tabelas_public(cur) if t not in TABELAS_COMPARTILHADAS]


def tabela_existe(cur, schema, tabela):
    cur.execute("SELECT 1 FROM pg_tables WHERE schemaname = %s AND tablename = %s", (schema, tabela))
    return cur.fetchone() is not None


def schema_da_tabela(cur, nome):
    cur.execute(
        "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.oid = to_regclass(%s)", (nome,))
    row = cur.fetchone()
    return row[0] if row else None


SQL_SEQUENCIAS = """
    SELECT c.relname, a.attname, sn.nspname, s.relname
    FROM pg_attrdef d
    JOIN pg_class c ON c.oid = d.adrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
    JOIN pg_depend dep ON dep.classid = 'pg_attrdef'::regclass AND dep.objid = d.oid
                      AND dep.refclassid = 'pg_class'::regclass
    JOIN pg_class s ON s.oid = dep.refobjid AND s.relkind = 'S'
    JOIN pg_namespace sn ON sn.oid = s.relnamespace
    WHERE n.nspname = %s
"""


def contagens_public(cur):
    out = {}
    for t in TABELAS_PUBLIC_VIGIADAS:
        if tabela_existe(cur, "public", t):
            cur.execute(f"SELECT COUNT(*) FROM public.{t}")
            out[t] = cur.fetchone()[0]
    return out


# ---------------------------------------------------------------------------
# 🔴 a trava
# ---------------------------------------------------------------------------
def trava(chave):
    """Com a MESMA configuração de conexão do subprocesso, pergunta ao
    Postgres onde cada tabela resolve. Qualquer desvio aborta a liga."""
    schema = schema_de(chave)
    # Conecta pelo MESMO mecanismo do subprocesso: PGOPTIONS no ambiente,
    # `psycopg2.connect(DATABASE_URL)` sem `options`. Se a libpq não aplicar
    # o PGOPTIONS (ou a URL trouxer outro search_path), o SHOW abaixo acusa.
    anterior = os.environ.get("PGOPTIONS")
    os.environ["PGOPTIONS"] = ambiente_da_liga(chave)["PGOPTIONS"]
    try:
        conn = conectar()
    finally:
        if anterior is None:
            os.environ.pop("PGOPTIONS", None)
        else:
            os.environ["PGOPTIONS"] = anterior
    try:
        cur = conn.cursor()
        problemas = []
        cur.execute("SHOW search_path")
        visto = cur.fetchone()[0].replace(" ", "")
        if visto != f"{schema},public":
            raise TravaRecusou(f"o PGOPTIONS não foi aplicado: search_path = `{visto}` (esperado `{schema},public`)")
        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,))
        if not cur.fetchone():
            raise TravaRecusou(f"schema `{schema}` não existe")
        pipeline = tabelas_do_pipeline(cur)
        for t in pipeline:
            onde = schema_da_tabela(cur, t)
            if onde != schema:
                problemas.append(f"`{t}` resolve para `{onde}` (esperado `{schema}`)")
        for t in sorted(TABELAS_COMPARTILHADAS):
            if tabela_existe(cur, "public", t):
                onde = schema_da_tabela(cur, t)
                if onde != "public":
                    problemas.append(f"`{t}` (compartilhada) resolve para `{onde}` (esperado `public`)")
        cur.execute(SQL_SEQUENCIAS, (schema,))
        for tabela, coluna, schema_seq, seq in cur.fetchall():
            if schema_seq != schema:
                problemas.append(f"`{schema}.{tabela}.{coluna}` usa sequência de fora: {schema_seq}.{seq}")
        if problemas:
            raise TravaRecusou("TRAVA — nada foi executado:\n    - " + "\n    - ".join(problemas))
        return len(pipeline)
    finally:
        conn.rollback()
        conn.close()


# ---------------------------------------------------------------------------
# preparar
# ---------------------------------------------------------------------------
def preparar(chave, aplicar):
    schema = schema_de(chave)
    conn = conectar()
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,))
        if not cur.fetchone():
            raise TravaRecusou(f"schema `{schema}` não existe — a coleta (`coletar_ligas.py`) cria o schema")
        faltam = [t for t in tabelas_do_pipeline(cur) if not tabela_existe(cur, schema, t)]
        print(f"\n## {chave} ({schema}) — {len(faltam)} tabela(s) do pipeline a criar")
        for t in faltam:
            print(f"   {t}")
        if not aplicar:
            print("   (verificar: nada foi criado)")
            return
        antes = contagens_public(cur)
        for t in faltam:
            cur.execute(
                f"CREATE TABLE {schema}.{t} (LIKE public.{t} INCLUDING DEFAULTS INCLUDING CONSTRAINTS "
                f"INCLUDING INDEXES INCLUDING IDENTITY INCLUDING GENERATED)")
        # O LIKE copia `DEFAULT nextval('public.x_seq')` — trocaria ids com o Brasileirão.
        cur.execute(SQL_SEQUENCIAS, (schema,))
        for tabela, coluna, schema_seq, _seq in cur.fetchall():
            if schema_seq != schema:
                nova = f"{schema}.{tabela}_{coluna}_seq"
                cur.execute(f"CREATE SEQUENCE IF NOT EXISTS {nova}")
                cur.execute(f"ALTER TABLE {schema}.{tabela} ALTER COLUMN {coluna} SET DEFAULT nextval('{nova}')")
                cur.execute(f"ALTER SEQUENCE {nova} OWNED BY {schema}.{tabela}.{coluna}")
                print(f"   sequência própria: {schema}.{tabela}.{coluna}")
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'claude_leitura'")
        if cur.fetchone():
            cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO claude_leitura")
        depois = contagens_public(cur)
        if antes != depois:
            raise TravaRecusou(f"`public` mudou durante o preparar ({antes} → {depois}) — rollback")
        conn.commit()
        print(f"   ✅ {len(faltam)} tabela(s) criada(s); `public` intocado")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"   trava depois do preparar: {trava(chave)} tabelas do pipeline resolvem para `{schema}` ✅")


# ---------------------------------------------------------------------------
# diario / odds
# ---------------------------------------------------------------------------
def rodar(chave, modo):
    schema = schema_de(chave)
    env = ambiente_da_liga(chave)
    print(f"\n## {chave} ({schema}) — modo {modo}")
    conn = conectar()
    cur = conn.cursor()
    antes = contagens_public(cur)
    conn.rollback()
    conn.close()
    for comando in SEQUENCIA[modo]:
        n = trava(chave)
        print(f"\n--- {' '.join(comando)}  (trava ok: {n} tabelas em `{schema}`) "
              f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
        inicio = time.time()
        r = subprocess.run([sys.executable, os.path.join(PASTA, comando[0])] + comando[1:],
                           env=env, cwd=PASTA)
        print(f"--- fim {comando[0]}: código {r.returncode}, {time.time() - inicio:.0f}s")
        if r.returncode != 0:
            print(f"🔴 {chave}: {comando[0]} falhou — o resto da liga NÃO roda")
            return False
    conn = conectar()
    cur = conn.cursor()
    depois = contagens_public(cur)
    conn.rollback()
    conn.close()
    mudou = {t: (antes.get(t), depois.get(t)) for t in depois if antes.get(t) != depois.get(t)}
    if mudou:
        print(f"⚠️ contagens de `public` mudaram durante a liga: {mudou} — conferir se algum cron do "
              f"Brasileirão rodou ao mesmo tempo; se não, é VAZAMENTO e a liga tem de parar")
    else:
        print(f"✅ `public` com as mesmas contagens antes e depois ({', '.join(TABELAS_PUBLIC_VIGIADAS)})")
    return True


def ler_ligas(argv):
    if "--liga" not in argv or argv.index("--liga") + 1 >= len(argv):
        raise SystemExit("informe --liga (ex.: --liga premier ou --liga premier,laliga)")
    ligas = [x.strip() for x in argv[argv.index("--liga") + 1].split(",") if x.strip()]
    ruins = [x for x in ligas if x not in LIGAS]
    if ruins:
        raise SystemExit(f"liga desconhecida: {ruins} (válidas: {list(LIGAS)})")
    return ligas


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] not in ("preparar", "diario", "odds"):
        raise SystemExit(__doc__)
    modo = argv[0]
    ligas = ler_ligas(argv)
    print("=" * 78)
    print(f"rodar_liga.py | modo: {modo} | ligas: {', '.join(ligas)}")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print("=" * 78)
    falhas = []
    for chave in ligas:
        try:
            if modo == "preparar":
                if len(argv) < 2 or argv[1] not in ("verificar", "aplicar"):
                    raise SystemExit("preparar precisa de fase: verificar ou aplicar")
                preparar(chave, argv[1] == "aplicar")
            elif not rodar(chave, modo):
                falhas.append(chave)
        except TravaRecusou as e:
            print(f"\n🔴 {chave}: {e}")
            falhas.append(chave)
    if falhas:
        print(f"\n🔴 ligas com falha: {falhas}")
        sys.exit(1)
    print("\n✅ fim")


if __name__ == "__main__":
    main()
