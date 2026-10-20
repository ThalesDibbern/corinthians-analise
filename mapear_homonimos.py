"""
mapear_homonimos.py — H-ID, Fase 0 (`claude/arquitetura_identidade_jogador.md §3`).

O MAPA DOS HOMÔNIMOS. Para cada jogo do Brasileirão (`public`) que tem
escalação no banco, pede à API-Football a escalação do jogo
(`/fixtures/lineups`, 1 chamada por jogo) e anota quem jogou: nome, id do
jogador NA API e time. Depois cruza com `jogadores`:

  dono         o id da API está em `jogadores.api_football_id`
  intruso      o id da API NÃO está em `jogadores`, mas existe um jogador
               com o MESMO NOME — os dados dele foram fundidos nesse jogador
               pelo `popular_banco.get_or_create_jogador` (§1.1 da arquitetura)
  nao_mapeado  nem o id nem o nome estão em `jogadores` (a API mudou o nome
               desde a coleta, por exemplo)

    python mapear_homonimos.py verificar   [--limite N]
    python mapear_homonimos.py aplicar     [--teto N] [--limite N]
    python mapear_homonimos.py relatorio

  verificar  SÓ LÊ O BANCO, em sessão só-leitura conferida. Não chama a API,
             não cria tabela. Diz quantos jogos faltam buscar, quanto da cota
             do dia isso custa, e lista os jogadores suspeitos pelo teste do
             mesmo dia (o pré-registro do critério A1).
  aplicar    cria (se faltar) e grava SÓ nas 3 tabelas novas abaixo. Chama a
             API só para jogo que ainda não está no cache — pode ser
             interrompido (teto de cota, falha da API, push) e continuado
             depois sem gastar de novo. No fim, refaz `homonimos_api` e
             imprime o relatório.
  relatorio  SÓ LEITURA: o relatório a partir do que já está no cache.

TABELAS (todas novas, em `public`, leitura liberada ao `claude_leitura`):
  homonimos_fixture  um registro por jogo buscado (quando, quantos jogadores)
  homonimos_lineup   uma linha por (jogo, time, jogador) da escalação da API
  homonimos_api      o mapa: (nome, id da API) → categoria, `jogadores.id`
                     de hoje, jogos e times. Refeita do zero a cada `aplicar`.
  Nenhuma tabela existente é escrita. As contagens de `jogadores`,
  `escalacoes`, `jogador_estatisticas_jogo`, `gols`, `cartoes`,
  `substituicoes` e `odds` são conferidas antes e depois.

COTA: mesmo contador do resto do projeto (`controle_api_uso`), teto próprio
(padrão 6.500, como o `coletar_ligas`). Rodar FORA das janelas dos crons
(09:00-10:00, ~12:00-12:10 e ~14:00-14:05 BRT): o `popular_banco` das 09:00
usa o mesmo contador e o teto dele é 7.000.

CRITÉRIO A1 (escrito antes do dado, arquitetura §4): todo jogador do teste
do mesmo dia tem de aparecer como destino de pelo menos um intruso. Se faltar
algum, o mapa está errado e a Fase 2 não roda.
"""

import functools
import os
import sys
import time
import types
from datetime import datetime

import psycopg2
import requests

import popular_banco as pb
import coletar_ligas as cl

TETO_PADRAO = 6500
PAUSA_ENTRE_CHAMADAS = float(os.environ.get("PAUSA_API_SEGUNDOS", "1.0"))

TABELAS_VIGIADAS = ["jogadores", "escalacoes", "jogador_estatisticas_jogo",
                    "gols", "cartoes", "substituicoes", "odds"]

DDL = [
    """CREATE TABLE IF NOT EXISTS public.homonimos_fixture (
           fixture_id_api  INTEGER PRIMARY KEY,
           jogadores       INTEGER NOT NULL,
           buscado_em      TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS public.homonimos_lineup (
           fixture_id_api  INTEGER NOT NULL REFERENCES public.homonimos_fixture(fixture_id_api),
           team_api_id     INTEGER NOT NULL,
           team_nome       TEXT,
           api_id          INTEGER NOT NULL,
           nome            TEXT NOT NULL,
           titular         BOOLEAN NOT NULL,
           PRIMARY KEY (fixture_id_api, team_api_id, api_id))""",
    """CREATE TABLE IF NOT EXISTS public.homonimos_api (
           nome               TEXT NOT NULL,
           api_id             INTEGER NOT NULL,
           categoria          TEXT NOT NULL,
           jogadores_id_atual INTEGER,
           ids_api_no_nome    INTEGER NOT NULL,
           n_fixtures         INTEGER NOT NULL,
           fixtures           INTEGER[] NOT NULL,
           times              TEXT[] NOT NULL,
           primeiro_jogo      DATE,
           ultimo_jogo        DATE,
           montado_em         TIMESTAMPTZ NOT NULL DEFAULT now(),
           PRIMARY KEY (nome, api_id))""",
]

# Os jogos a buscar: os de `public` que têm escalação gravada (é nelas, e nas
# estatísticas, que a fusão aconteceu). Mais novos primeiro.
SQL_FIXTURES_COM_ESCALACAO = """
    SELECT j.fixture_id_api, MIN(j.data_jogo) AS data_jogo
    FROM public.jogos j
    WHERE j.fixture_id_api IS NOT NULL
      AND EXISTS (SELECT 1 FROM public.escalacoes e WHERE e.jogo_id = j.id)
    GROUP BY j.fixture_id_api
    ORDER BY MIN(j.data_jogo) DESC, j.fixture_id_api
"""

# O teste do mesmo dia (arquitetura §1.3): um jogador não joga duas partidas
# diferentes no mesmo dia.
SQL_SUSPEITOS = """
    SELECT s.jogador_id, jg.nome, jg.api_football_id, COUNT(*) AS dias_duplos
    FROM (SELECT jeg.jogador_id, j.data_jogo
          FROM public.jogador_estatisticas_jogo jeg
          JOIN public.jogos j ON j.id = jeg.jogo_id
          GROUP BY jeg.jogador_id, j.data_jogo
          HAVING COUNT(DISTINCT j.fixture_id_api) >= 2) s
    JOIN public.jogadores jg ON jg.id = s.jogador_id
    GROUP BY s.jogador_id, jg.nome, jg.api_football_id
    ORDER BY COUNT(*) DESC, s.jogador_id
"""

# O mapa, refeito do cache. `jogadores` com o mesmo nome deveria ser no
# máximo 1 (por construção); se houver mais, fica o de menor id.
SQL_MONTAR_MAPA = """
    WITH datas AS (
        SELECT fixture_id_api, MIN(data_jogo) AS data_jogo
        FROM public.jogos WHERE fixture_id_api IS NOT NULL GROUP BY fixture_id_api
    ),
    lin AS (
        SELECT l.nome, l.api_id,
               COUNT(DISTINCT l.fixture_id_api) AS n_fixtures,
               ARRAY_AGG(DISTINCT l.fixture_id_api ORDER BY l.fixture_id_api) AS fixtures,
               ARRAY_AGG(DISTINCT COALESCE(l.team_nome, l.team_api_id::text)) AS times,
               MIN(d.data_jogo) AS primeiro_jogo, MAX(d.data_jogo) AS ultimo_jogo
        FROM public.homonimos_lineup l
        LEFT JOIN datas d ON d.fixture_id_api = l.fixture_id_api
        GROUP BY l.nome, l.api_id
    ),
    por_nome AS (
        SELECT nome, COUNT(*) AS ids_api FROM lin GROUP BY nome
    ),
    classif AS (
        SELECT lin.*, pn.ids_api,
               (SELECT jg.id FROM public.jogadores jg WHERE jg.api_football_id = lin.api_id
                ORDER BY jg.id LIMIT 1) AS pelo_id,
               (SELECT jg.id FROM public.jogadores jg WHERE jg.nome = lin.nome
                ORDER BY jg.id LIMIT 1) AS pelo_nome
        FROM lin JOIN por_nome pn ON pn.nome = lin.nome
    )
    INSERT INTO public.homonimos_api (nome, api_id, categoria, jogadores_id_atual, ids_api_no_nome,
                                      n_fixtures, fixtures, times, primeiro_jogo, ultimo_jogo)
    SELECT nome, api_id,
           CASE WHEN pelo_id IS NOT NULL THEN 'dono'
                WHEN pelo_nome IS NOT NULL THEN 'intruso'
                ELSE 'nao_mapeado' END,
           COALESCE(pelo_id, pelo_nome),
           ids_api, n_fixtures, fixtures, times, primeiro_jogo, ultimo_jogo
    FROM classif
    WHERE ids_api >= 2 OR pelo_id IS NULL
"""


class ApiForaDoAr(Exception):
    pass


# ---------------------------------------------------------------------------
def cabecalho(fase, teto, limite):
    print("=" * 78)
    print(f"mapear_homonimos.py | fase: {fase}"
          + (" | SOMENTE LEITURA" if fase != "aplicar" else ""))
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now().isoformat(timespec='seconds')}")
    if fase == "aplicar":
        print(f"teto de cota: {teto} | limite de jogos nesta execução: {limite or 'nenhum'} "
              f"| pausa entre chamadas: {PAUSA_ENTRE_CHAMADAS}s")
    print("=" * 78)
    sys.stdout.flush()


def conectar():
    conn = psycopg2.connect(pb.DATABASE_URL, options="-c search_path=public")
    conn.autocommit = False
    return conn


def conectar_so_leitura():
    """Sessão só-leitura CONFERIDA. O SET SESSION só vale da próxima transação
    (1_ESSENCIAL §6.2): commit logo depois e SHOW para provar."""
    conn = conectar()
    cur = conn.cursor()
    cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    conn.commit()
    cur.execute("SHOW transaction_read_only")
    if str(cur.fetchone()[0]).lower() != "on":
        conn.close()
        raise RuntimeError("a sessão não ficou só-leitura — abortando sem fazer nada")
    return conn, cur


def tabela_existe(cur, tabela):
    cur.execute("SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = %s", (tabela,))
    return cur.fetchone() is not None


def contagens(cur):
    out = {}
    for t in TABELAS_VIGIADAS:
        cur.execute(f"SELECT COUNT(*) FROM public.{t}")
        out[t] = int(cur.fetchone()[0])
    return out


def fixtures_buscados(cur):
    if not tabela_existe(cur, "homonimos_fixture"):
        return set()
    cur.execute("SELECT fixture_id_api FROM public.homonimos_fixture")
    return {int(r[0]) for r in cur.fetchall()}


def fixtures_a_buscar(cur, limite):
    cur.execute(SQL_FIXTURES_COM_ESCALACAO)
    todos = [int(r[0]) for r in cur.fetchall()]
    feitos = fixtures_buscados(cur)
    faltam = [f for f in todos if f not in feitos]
    if limite:
        faltam = faltam[:limite]
    return todos, feitos, faltam


def requisicoes_hoje(cur):
    cur.execute("SELECT requisicoes FROM public.controle_api_uso WHERE dia = CURRENT_DATE")
    row = cur.fetchone()
    return int(row[0]) if row else 0


def suspeitos(cur):
    cur.execute(SQL_SUSPEITOS)
    return [(int(r[0]), r[1], r[2], int(r[3])) for r in cur.fetchall()]


def imprimir_suspeitos(lista):
    print(f"\n## PRÉ-REGISTRO A1 — teste do mesmo dia: {len(lista)} jogador(es) de `jogadores` "
          f"com estatística em 2+ jogos no mesmo dia")
    for jid, nome, api, dias in lista[:15]:
        print(f"   {jid:>6}  {nome:<28} api {api}  ({dias} dia(s) duplo(s))")
    if len(lista) > 15:
        print(f"   … e mais {len(lista) - 15}")
    print("   Critério: TODOS têm de aparecer como destino de um intruso no mapa. "
          "Faltou algum → a Fase 2 não roda.")


# ---------------------------------------------------------------------------
def gravar_fixture(cur, fixture_id, resposta):
    linhas = []
    for bloco in resposta or []:
        time_info = bloco.get("team") or {}
        team_id = time_info.get("id")
        team_nome = time_info.get("name")
        for chave, titular in (("startXI", True), ("substitutes", False)):
            for entrada in bloco.get(chave) or []:
                jog = entrada.get("player") or {}
                if jog.get("id") is None or not jog.get("name") or team_id is None:
                    continue
                linhas.append((fixture_id, team_id, team_nome, jog["id"], jog["name"], titular))
    cur.execute("INSERT INTO public.homonimos_fixture (fixture_id_api, jogadores) VALUES (%s, %s)",
                (fixture_id, len(linhas)))
    for ln in linhas:
        cur.execute(
            """INSERT INTO public.homonimos_lineup
                   (fixture_id_api, team_api_id, team_nome, api_id, nome, titular)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (fixture_id_api, team_api_id, api_id) DO NOTHING""", ln)
    return len(linhas)


def montar_mapa(cur):
    cur.execute("DELETE FROM public.homonimos_api")
    cur.execute(SQL_MONTAR_MAPA)


def relatorio(cur, lista_suspeitos, todos):
    feitos = fixtures_buscados(cur)
    print(f"\n## COBERTURA: {len(feitos & set(todos))} de {len(todos)} jogo(s) com escalação já buscados")
    if not tabela_existe(cur, "homonimos_api"):
        print("   (o mapa ainda não existe — rode `aplicar`)")
        return None
    cur.execute("SELECT categoria, COUNT(*), COUNT(DISTINCT nome), COALESCE(SUM(n_fixtures),0) "
                "FROM public.homonimos_api GROUP BY categoria ORDER BY categoria")
    print("\n## O MAPA (`homonimos_api`)")
    print("   categoria      ids da API   nomes   aparições em jogos")
    for cat, n, nomes, nf in cur.fetchall():
        print(f"   {cat:<14} {int(n):>10}  {int(nomes):>6}   {int(nf):>8}")
    cur.execute("SELECT COUNT(*) FROM (SELECT nome FROM public.homonimos_api "
                "GROUP BY nome HAVING COUNT(*) >= 2) x")
    print(f"   nomes com 2+ ids da API: {int(cur.fetchone()[0])}")

    cur.execute("""SELECT nome, api_id, categoria, jogadores_id_atual, n_fixtures,
                          array_to_string(times, ', '), primeiro_jogo, ultimo_jogo
                   FROM public.homonimos_api
                   WHERE nome IN (SELECT nome FROM public.homonimos_api WHERE categoria = 'intruso')
                   ORDER BY nome, categoria, api_id""")
    linhas = cur.fetchall()
    print(f"\n## NOMES COM INTRUSO ({len({r[0] for r in linhas})} nome(s); até 40 linhas)")
    for nome, api, cat, jid, nf, tms, pri, ult in linhas[:40]:
        print(f"   {nome:<24} api {api:>7}  {cat:<8} → jogadores.id {jid}  {nf:>3} jogo(s)  "
              f"{pri}…{ult}  [{tms}]")

    cur.execute("SELECT DISTINCT jogadores_id_atual FROM public.homonimos_api "
                "WHERE categoria = 'intruso' AND jogadores_id_atual IS NOT NULL")
    destinos = {int(r[0]) for r in cur.fetchall()}
    faltam = [s for s in lista_suspeitos if s[0] not in destinos]
    print(f"\n## CRITÉRIO A1: {len(lista_suspeitos) - len(faltam)} de {len(lista_suspeitos)} "
          f"suspeitos do teste do mesmo dia são destino de um intruso")
    for jid, nome, api, dias in faltam[:30]:
        print(f"   ❌ {jid:>6}  {nome:<28} api {api}  ({dias} dia(s) duplo(s))")
    completo = len(feitos & set(todos)) == len(todos)
    if faltam and not completo:
        print("   ⏳ mapa PARCIAL — ainda faltam jogos; o A1 só vale com a cobertura completa")
    elif faltam:
        print("   🔴 A1 REPROVADO — o mapa não explica todos os suspeitos. A Fase 2 NÃO roda.")
    else:
        print("   ✅ A1 aprovado" + ("" if completo else " (com o mapa ainda parcial)"))
    cur.execute("SELECT COUNT(*) FROM public.homonimos_api WHERE categoria = 'nao_mapeado'")
    nm = int(cur.fetchone()[0])
    if nm:
        print(f"   ⚠️ {nm} id(s) da API sem jogador no banco nem pelo id nem pelo nome "
              f"(nome mudou na API?) — olhar antes da Fase 2")
    return not faltam and completo


# ---------------------------------------------------------------------------
def fase_verificar(limite):
    conn, cur = conectar_so_leitura()
    try:
        todos, feitos, faltam = fixtures_a_buscar(cur, limite)
        usadas = requisicoes_hoje(cur)
        print(f"\njogos de `public` com escalação: {len(todos)}")
        print(f"já buscados (cache): {len(feitos & set(todos))}")
        print(f"a buscar nesta execução: {len(faltam)} → {len(faltam)} chamada(s) a /fixtures/lineups "
              f"(+ retentativas, se a API falhar)")
        print(f"cota hoje: {usadas} usadas · sobram {TETO_PADRAO - usadas} até o teto padrão {TETO_PADRAO}")
        if faltam and usadas + len(faltam) > TETO_PADRAO:
            print("   ⚠️ não cabe tudo hoje — o `aplicar` para no teto e continua em outro dia")
        imprimir_suspeitos(suspeitos(cur))
        for t in ("homonimos_fixture", "homonimos_lineup", "homonimos_api"):
            print(f"tabela `{t}`: {'existe' if tabela_existe(cur, t) else 'ainda não existe'}")
    finally:
        conn.rollback()
        conn.close()
    print("\nFASE VERIFICAR — nenhuma tabela foi criada ou escrita, nenhuma chamada à API.")


def fase_relatorio():
    conn, cur = conectar_so_leitura()
    try:
        cur.execute(SQL_FIXTURES_COM_ESCALACAO)
        todos = [int(r[0]) for r in cur.fetchall()]
        relatorio(cur, suspeitos(cur), todos)
    finally:
        conn.rollback()
        conn.close()
    print("\nRELATÓRIO — somente leitura, nada foi escrito.")


def fase_aplicar(teto, limite):
    conn = conectar()
    cur = conn.cursor()
    conn_cota = conectar()           # o contador commita a cada chamada; fica numa conexão à parte
    cur_cota = conn_cota.cursor()
    try:
        for ddl in DDL:
            cur.execute(ddl)
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'claude_leitura'")
        if cur.fetchone():
            cur.execute("GRANT SELECT ON public.homonimos_fixture, public.homonimos_lineup, "
                        "public.homonimos_api TO claude_leitura")
        conn.commit()

        antes = contagens(cur)
        conn.rollback()
        todos, feitos, faltam = fixtures_a_buscar(cur, limite)
        conn.rollback()
        print(f"\njogos com escalação: {len(todos)} · já no cache: {len(feitos & set(todos))} "
              f"· a buscar agora: {len(faltam)}")

        pb.requests = types.SimpleNamespace(
            get=functools.partial(requests.get, timeout=cl.TIMEOUT_API_SEGUNDOS))
        pb.LIMITE_REQUISICOES_DIA = teto
        pb._cursor_para_contador = cur_cota
        pb.requisicoes_usadas = pb.carregar_requisicoes_usadas_hoje(cur_cota)
        conn_cota.commit()
        print(f"cota hoje antes de começar: {pb.requisicoes_usadas} (teto {teto})")

        buscados = vazios = jogadores = 0
        falhas_seguidas = 0
        pulados = []
        try:
            for i, fid in enumerate(faltam, start=1):
                try:
                    resposta = cl.com_retentativa(conn_cota, pb.buscar_escalacao, fid)
                    falhas_seguidas = 0
                except cl.FalhaPassageiraPersistente as erro:
                    pulados.append(fid)
                    falhas_seguidas += 1
                    print(f"  ⏭️ jogo {fid} pulado ({erro})")
                    if falhas_seguidas >= cl.FALHAS_SEGUIDAS_PARA_PARAR:
                        raise ApiForaDoAr(f"{falhas_seguidas} jogos seguidos falharam")
                    continue
                n = gravar_fixture(cur, fid, resposta)
                conn.commit()
                buscados += 1
                jogadores += n
                if n == 0:
                    vazios += 1
                if i % 100 == 0:
                    print(f"  {i}/{len(faltam)} jogos · cota do dia {pb.requisicoes_usadas}")
                    sys.stdout.flush()
                time.sleep(PAUSA_ENTRE_CHAMADAS)
        except pb.LimiteDiarioAtingido as e:
            print(f"\n⏸️ {e}\n   (parada normal — a próxima execução continua do cache)")
        except ApiForaDoAr as e:
            print(f"\n🔴 {e} — parando para não gastar cota; a próxima execução continua do cache")
        finally:
            conn.rollback()      # nada pendente além de um jogo interrompido no meio
            conn_cota.commit()

        print(f"\nbuscados agora: {buscados} jogo(s), {jogadores} linha(s) de escalação "
              f"({vazios} jogo(s) sem escalação na API) · pulados por falha: {len(pulados)} "
              f"· cota do dia: {pb.requisicoes_usadas}")

        montar_mapa(cur)
        depois = contagens(cur)
        if depois != antes:
            mudou = {t: (antes[t], depois[t]) for t in antes if antes[t] != depois[t]}
            conn.rollback()
            raise RuntimeError(
                f"tabelas que este script não escreve mudaram durante a execução: {mudou}. "
                "Se um cron rodou ao mesmo tempo, rode de novo fora da janela dele. "
                "O mapa NÃO foi refeito (o cache dos jogos buscados ficou).")
        conn.commit()
        print(f"✅ `homonimos_api` refeito · tabelas vigiadas iguais antes e depois "
              f"({', '.join(TABELAS_VIGIADAS)})")

        relatorio(cur, suspeitos(cur), todos)
        conn.rollback()
    finally:
        conn.close()
        conn_cota.close()
    print("\nFASE APLICAR — escrito só em homonimos_fixture, homonimos_lineup, homonimos_api "
          "(e o contador de cota).")


def ler_argumentos(argv):
    if not argv or argv[0] not in ("verificar", "aplicar", "relatorio"):
        raise SystemExit(__doc__)
    fase = argv[0]
    teto = TETO_PADRAO
    limite = None
    if "--teto" in argv:
        teto = int(argv[argv.index("--teto") + 1])
    if "--limite" in argv:
        limite = int(argv[argv.index("--limite") + 1])
    return fase, teto, limite


def main():
    fase, teto, limite = ler_argumentos(sys.argv[1:])
    cabecalho(fase, teto, limite)
    if fase == "verificar":
        fase_verificar(limite)
    elif fase == "relatorio":
        fase_relatorio()
    else:
        fase_aplicar(teto, limite)


if __name__ == "__main__":
    main()
