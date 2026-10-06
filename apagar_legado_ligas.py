"""
apagar_legado_ligas.py — apaga o schema LEGADO `ligas` (30/09-02/10), depois
de PROVAR que nada dele se perde.

Item L3 do `2_ABERTO`. Fase 1 da `claude/arquitetura_ligas_extras.md`: as três
ligas do legado (PL, La Liga, Serie A) foram copiadas para `liga_premier`,
`liga_laliga` e `liga_seriea` em 03/10 e conferidas por md5. O legado ficou
como cópia de segurança. Este script tira a cópia.

Duas fases:

    python apagar_legado_ligas.py verificar
    python apagar_legado_ligas.py aplicar

`verificar` NÃO apaga nem grava nada: mede e prova. `aplicar` refaz TODAS as
provas dentro da mesma transação do DROP e só faz COMMIT se todas passarem.

--------------------------------------------------------------------------
AS PROVAS (qualquer uma falhando = nada é apagado)
--------------------------------------------------------------------------

  1. TODA liga presente no legado tem o seu schema próprio (`liga_<chave>`).
  2. COLUNAS: cada tabela do legado tem as mesmas colunas, na mesma ordem,
     em cada schema de destino.
  3. NENHUM ID SE PERDE: cada linha do legado, em cada uma das 9 tabelas, tem
     o mesmo `id` no schema DA SUA LIGA (a migração preservou os ids). Nunca
     "em qualquer schema": cada schema tem a sua sequência, e a coleta nova de
     uma liga cria ids que coincidem com ids migrados de outra. A fatia de
     cada liga é a da migração (filtros IMPORTADOS do migrar_ligas_por_schema).
     E nenhuma linha do legado pode estar fora da fatia de todas as ligas.
  4. CONTEÚDO: quantas linhas do legado existem IDÊNTICAS (todas as colunas)
     no schema da sua liga. As que têm o id mas não são idênticas são
     linhas que a coleta ATUALIZOU depois da migração — o destino é a versão
     mais nova; o legado nunca mais é gravado. São mostradas, não bloqueiam.
  5. NADA DE FORA DEPENDE DO LEGADO: nenhuma FK, view ou default fora de
     `ligas` aponta para dentro dele.
  6. `public` (Brasileirão) com as mesmas contagens antes e depois.

O DROP é feito SEM CASCADE: `DROP TABLE ... RESTRICT` das 9 tabelas e depois
`DROP SCHEMA ligas RESTRICT`. Se existir qualquer coisa que a prova 5 não
viu, o PRÓPRIO BANCO recusa e o ROLLBACK desfaz tudo.

Espaço: apagar libera o disco das tabelas (~107 MB em 05/10) e grava pouco
WAL. Não há risco de encher o volume.

O que NÃO faz: não chama API, não toca em `liga_*` nem em `public`.

⚠️ Não rodar enquanto o `coleta-ligas` estiver rodando (ele lê `ligas.jogos`
na trava de migração; o DROP esperaria a coleta ou a coleta esperaria o DROP).
Depois do DROP, o coletar_ligas segue funcionando: legado ausente conta como
0 linhas na trava (`jogos_da_liga`).
"""

import sys
from datetime import datetime

import psycopg2

import coletar_ligas as cl   # funções de PRODUÇÃO: ligas, tabelas, contagens do public
import migrar_ligas_por_schema as mig   # a fatia de cada liga, a MESMA da migração

LEGADO = cl.SCHEMA_LEGADO
TABELAS = list(cl.TABELAS_COPIADAS)


class ProvaFalhou(Exception):
    """Alguma prova falhou: o legado NÃO pode ser apagado."""


def cabecalho(fase):
    print("=" * 78)
    print(f"apagar_legado_ligas.py | fase: {fase}")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now().isoformat(timespec='seconds')}")
    print(f"alvo: schema `{LEGADO}` (legado) · destinos: liga_<chave> · `public` não é tocado")
    print("=" * 78)
    sys.stdout.flush()


def colunas(cur, schema, tabela):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, tabela),
    )
    return [r[0] for r in cur.fetchall()]


def tamanho_legado(cur):
    cur.execute(
        "SELECT COALESCE(SUM(pg_total_relation_size(c.oid)), 0) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind = 'r'",
        (LEGADO,),
    )
    return cur.fetchone()[0]


def destinos(cur):
    """Prova 1. Devolve [(liga_api_id, schema)] de cada liga do legado."""
    cur.execute(f"SELECT liga_api_id, COUNT(*) FROM {LEGADO}.jogos GROUP BY 1 ORDER BY 1")
    no_legado = cur.fetchall()
    por_api_id = {api_id: chave for chave, (api_id, _nome) in cl.LIGAS.items()}
    existentes = [cl.schema_da_liga(ch) for ch in cl.LIGAS
                  if cl.schema_existe(cur, cl.schema_da_liga(ch))]
    print("\n[1] ligas no legado e o schema de cada uma:")
    faltando, pares = [], []
    for api_id, n in no_legado:
        chave = por_api_id.get(api_id)
        schema = cl.schema_da_liga(chave) if chave else None
        ok = schema in existentes
        print(f"    liga_api_id {api_id}: {n} linha(s) de jogos → {schema or '??'} "
              f"{'✅' if ok else '❌ NÃO EXISTE'}")
        if ok:
            pares.append((api_id, schema))
        else:
            faltando.append(str(api_id))
    if faltando:
        raise ProvaFalhou(f"liga(s) do legado sem schema próprio: {', '.join(faltando)}")
    if not pares:
        raise ProvaFalhou("o legado não tem nenhuma liga — estado inesperado, investigar antes")
    return pares


def provar_tabelas(cur, ligas_do_legado):
    """Provas 2, 3 e 4, tabela por tabela E liga por liga.

    Cada linha do legado é conferida contra o schema DA SUA liga — nunca
    contra "qualquer schema": depois da migração, cada schema tem a sua
    sequência, e a coleta nova de uma liga pode criar um id que coincide com
    o id migrado de OUTRA liga (pego na bancada de 05/10). A fatia de cada
    liga é a MESMA da migração: os filtros são importados do
    migrar_ligas_por_schema, não copiados.
    """
    print("\n[2-4] tabela por tabela, cada liga contra o SEU schema:")
    print(f"    {'tabela':<28} {'liga':<16} {'legado':>8} {'id perdido':>11} {'idênticas':>10} {'atualizadas':>12}")
    problemas = []
    total_atualizadas = 0
    for t in TABELAS:
        cols = colunas(cur, LEGADO, t)
        divergentes = [s for _a, s in ligas_do_legado if colunas(cur, s, t) != cols]
        if divergentes:
            problemas.append(f"{t}: colunas de {', '.join(divergentes)} diferem do legado")
            continue
        lista = ", ".join(cols)

        for api_id, s in ligas_do_legado:
            p = {"liga": api_id}
            fatia = f"FROM {LEGADO}.{t} l WHERE {mig.filtro(t)}"
            cur.execute(f"SELECT COUNT(*) {fatia}", p)
            n = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) {fatia} AND NOT EXISTS (SELECT 1 FROM {s}.{t} d WHERE d.id = l.id)", p)
            perdidos = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM (SELECT {lista} {fatia} "
                        f"EXCEPT SELECT {lista} FROM {s}.{t}) x", p)
            nao_identicas = cur.fetchone()[0]
            atualizadas = nao_identicas - perdidos
            total_atualizadas += atualizadas
            marca = "✅" if perdidos == 0 else "❌"
            print(f"    {t:<28} {s:<16} {n:>8} {perdidos:>11} {n - nao_identicas:>10} {atualizadas:>12} {marca}")
            if perdidos:
                problemas.append(f"{t}: {perdidos} linha(s) de {s} no legado sem o mesmo id no destino")

        # Linhas do legado que não pertencem à fatia de NENHUMA liga: a
        # migração não as copiou, então apagar o legado as perderia.
        cond, params = [], {}
        for i, (api_id, _s) in enumerate(ligas_do_legado):
            cond.append("(" + mig.filtro(t).replace("%(liga)s", f"%(liga{i})s") + ")")
            params[f"liga{i}"] = api_id
        cur.execute(f"SELECT COUNT(*) FROM {LEGADO}.{t} WHERE NOT ({' OR '.join(cond)})", params)
        orfas = cur.fetchone()[0]
        if orfas:
            print(f"    {t:<28} {'(nenhuma liga)':<16} {orfas:>8}  ❌ fora da fatia de todas as ligas")
            problemas.append(f"{t}: {orfas} linha(s) do legado fora da fatia de qualquer liga (não migradas)")
    if total_atualizadas:
        print(f"    ⚠️ {total_atualizadas} linha(s) 'atualizadas': o id existe no destino com conteúdo "
              f"diferente (coleta posterior à migração). O destino é a versão mais nova.")
    if problemas:
        raise ProvaFalhou("; ".join(problemas))


SQL_DEPENDENCIAS_DE_FORA = """
    SELECT DISTINCT dn.nspname || '.' || dc.relname AS objeto,
           CASE d.classid WHEN 'pg_constraint'::regclass THEN 'FK/constraint'
                          WHEN 'pg_rewrite'::regclass THEN 'view/regra'
                          WHEN 'pg_attrdef'::regclass THEN 'default' ELSE d.classid::regclass::text END AS tipo,
           rc.relname AS aponta_para
    FROM pg_depend d
    JOIN pg_class rc ON d.refclassid = 'pg_class'::regclass AND rc.oid = d.refobjid
    JOIN pg_namespace rn ON rn.oid = rc.relnamespace AND rn.nspname = %s
    LEFT JOIN pg_attrdef ad ON d.classid = 'pg_attrdef'::regclass AND ad.oid = d.objid
    LEFT JOIN pg_constraint co ON d.classid = 'pg_constraint'::regclass AND co.oid = d.objid
    LEFT JOIN pg_rewrite rw ON d.classid = 'pg_rewrite'::regclass AND rw.oid = d.objid
    JOIN pg_class dc ON dc.oid = COALESCE(ad.adrelid, co.conrelid, rw.ev_class)
    JOIN pg_namespace dn ON dn.oid = dc.relnamespace
    WHERE dn.nspname <> %s
"""


def provar_dependencias(cur):
    """Prova 5."""
    cur.execute(SQL_DEPENDENCIAS_DE_FORA, (LEGADO, LEGADO))
    deps = cur.fetchall()
    print(f"\n[5] objetos FORA de `{LEGADO}` que dependem dele: {len(deps)}")
    for objeto, tipo, alvo in deps:
        print(f"    ❌ {objeto} ({tipo}) → {LEGADO}.{alvo}")
    if deps:
        raise ProvaFalhou(f"{len(deps)} objeto(s) de fora dependem do legado")


def outros_objetos(cur):
    """O que existe em `ligas` além das 9 tabelas, suas sequências e índices."""
    cur.execute(
        "SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind NOT IN ('S', 'i', 't') AND NOT (c.relname = ANY(%s))",
        (LEGADO, TABELAS),
    )
    return cur.fetchall()


def provar(cur):
    pares = destinos(cur)
    provar_tabelas(cur, pares)
    provar_dependencias(cur)
    extras = outros_objetos(cur)
    if extras:
        raise ProvaFalhou(f"objetos inesperados em `{LEGADO}`: {extras} — o DROP RESTRICT os recusaria")
    print(f"\n[ok] `{LEGADO}` tem só as 9 tabelas (com sequências e índices).")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("verificar", "aplicar"):
        print(__doc__)
        sys.exit(1)
    fase = sys.argv[1]
    cabecalho(fase)

    conn = psycopg2.connect(cl.pb.DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        if not cl.schema_existe(cur, LEGADO):
            print(f"Schema `{LEGADO}` não existe — já foi apagado. Nada a fazer.")
            return

        tamanho = tamanho_legado(cur)
        publico_antes = cl.contagens_public(cur)
        print(f"tamanho de `{LEGADO}`: {tamanho / 1024 / 1024:.0f} MB · public.jogos: {publico_antes['jogos']}")

        provar(cur)

        if fase == "verificar":
            print("\n✅ Todas as provas passaram: o aplicar pode apagar o legado sem perder nada.")
            return

        print(f"\nApagando, sem CASCADE:")
        cur.execute(f"DROP TABLE {', '.join(f'{LEGADO}.{t}' for t in TABELAS)} RESTRICT")
        print(f"    DROP TABLE das 9 tabelas: ok")
        cur.execute(f"DROP SCHEMA {LEGADO} RESTRICT")
        print(f"    DROP SCHEMA {LEGADO}: ok")

        publico_depois = cl.contagens_public(cur)
        if publico_depois != publico_antes:
            raise ProvaFalhou(f"`public` mudou: {publico_antes} → {publico_depois}")
        if cl.schema_existe(cur, LEGADO):
            raise ProvaFalhou(f"`{LEGADO}` ainda existe depois do DROP")

        conn.commit()
        print(f"\n✅ `{LEGADO}` apagado ({tamanho / 1024 / 1024:.0f} MB). `public` intocado "
              f"(jogos: {publico_depois['jogos']}). Schemas das ligas intocados.")
    except Exception as e:
        conn.rollback()
        print(f"\n🔴 ROLLBACK — nada foi apagado: {e}")
        raise
    finally:
        if fase == "verificar":
            conn.rollback()  # incondicional: esta fase nunca grava
        cur.close()
        conn.close()
    if fase == "verificar":
        print("\n" + "=" * 78)
        print(f"FASE VERIFICAR — nada foi apagado nem gravado (nem `{LEGADO}`, nem `liga_*`, nem `public`).")
        print("=" * 78)


if __name__ == "__main__":
    main()
