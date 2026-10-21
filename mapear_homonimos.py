"""
mapear_homonimos.py — H-ID, Fase 0 (`claude/arquitetura_identidade_jogador.md §3`).

O MAPA DOS HOMÔNIMOS. Para cada jogo do Brasileirão (`public`) que tem
escalação no banco, pede à API-Football a escalação do jogo
(`/fixtures/lineups`, 1 chamada por jogo) e anota quem jogou: nome, id do
jogador NA API e time.

21/10/2026 — O MAPA CASA POR JOGO E TIME, NÃO POR NOME (arquitetura §3,
Fase 0, revisão de 21/10). A 1ª versão agrupava pelo nome que a escalação
devolve HOJE e reprovou no A1 (45 de 60): a API muda o nome do jogador com o
tempo ("Wesley Gassova" em 2023, "Wesley" em 2024; "Joao Victor" × "João
Victor"), e a fusão aconteceu com o nome da ÉPOCA. Agora, para cada linha do
banco (jogo, `jogadores.id`, time — o time vem do lado real da estatística):

  dono      o id da API desse `jogadores.id` está na escalação daquele time,
            naquele jogo → a linha é dele mesmo
  intruso   não está, e na escalação daquele time há EXATAMENTE UM jogador
            com nome compatível (sem acento, maiúscula ou pontuação; um nome
            contido no outro) → a linha é desse id da API
  provavel  nenhum compatível, mas exatamente um com o mesmo PRIMEIRO nome
            no time → separado do intruso; a Fase 2 decide se usa
  ambiguo   não está, e há 2+ compatíveis → não decide
  sem_par   não está, e nenhum compatível → não decide
  id_zero   o `jogadores` tem api_football_id = 0: a API deu id 0 a quem não
            tinha cadastro e todos caíram no mesmo jogador — outra causa
O dono em QUALQUER time do jogo conta como dono (em parte de 2022 o lado da
estatística parece trocado).
Escalação sem estatística (reserva que não entrou) não tem time no banco: aí
procura nos dois times do jogo.

    python mapear_homonimos.py verificar   [--limite N]
    python mapear_homonimos.py aplicar     [--teto N] [--limite N]
    python mapear_homonimos.py relatorio

  verificar  SÓ LÊ O BANCO, em sessão só-leitura conferida. Não chama a API,
             não cria tabela. Diz quantos jogos faltam buscar, quanto da cota
             do dia isso custa, e lista os jogadores suspeitos pelo teste do
             mesmo dia (o pré-registro do critério A1).
  aplicar    cria (se faltar) e grava SÓ nas tabelas novas abaixo. Chama a
             API só para jogo que ainda não está no cache — pode ser
             interrompido (teto de cota, falha da API, push) e continuado
             depois sem gastar de novo. No fim, refaz o mapa e imprime o
             relatório.
  mapa       NÃO chama a API: refaz o mapa (`homonimos_linha`) a partir do
             cache e imprime o relatório. Grava só em `homonimos_linha`.
  relatorio  SÓ LEITURA: o relatório a partir do que já está no cache.

TABELAS (todas novas, em `public`, leitura liberada ao `claude_leitura`):
  homonimos_fixture  um registro por jogo buscado (quando, quantos jogadores)
  homonimos_lineup   uma linha por (jogo, time, jogador) da escalação da API
  homonimos_api      o mapa da 1ª versão, por nome (20/10). Não é mais
                     refeito; fica como registro.
  homonimos_linha    o mapa de 21/10: uma linha por (jogo, `jogadores.id`,
                     time) que NÃO é do dono — intruso, ambiguo ou sem_par.
                     Refeita do zero a cada `aplicar` / `mapa`.
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
⚠️ O `jogadores.id` usado é o de HOJE. Depois do H1 (21/10) o intruso que
voltar a jogar ganha `jogadores` próprio e as linhas NOVAS já nascem nele; as
antigas continuam no id fundido — é o que o mapa descreve.
"""

import functools
import os
import re
import unicodedata
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
    """CREATE TABLE IF NOT EXISTS public.homonimos_linha (
           fixture_id_api     INTEGER NOT NULL,
           jogador_id_atual   INTEGER NOT NULL,
           team_api_id        INTEGER NOT NULL,   -- 0 = time desconhecido (só escalação)
           origem             TEXT NOT NULL,
           situacao           TEXT NOT NULL,
           api_id_certo       INTEGER,
           nome_api           TEXT,
           candidatos         INTEGER NOT NULL,
           montado_em         TIMESTAMPTZ NOT NULL DEFAULT now(),
           PRIMARY KEY (fixture_id_api, jogador_id_atual, origem, team_api_id))""",
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

# O mapa da 1ª versão (20/10), por nome. NÃO É MAIS USADO (reprovou no A1);
# fica aqui como registro do que a `homonimos_api` contém.
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
          + (" | SOMENTE LEITURA" if fase in ("verificar", "relatorio") else "")
          + (" | SEM API" if fase == "mapa" else ""))
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


def normalizar(nome):
    s = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def compativel(a, b):
    """Nome do banco × nome da escalação: iguais sem acento/pontuação, ou as
    palavras de um contidas nas do outro ("Wesley" × "Wesley Gassova",
    "Wesley Ribeiro" × "Wesley")."""
    na, nb = normalizar(a), normalizar(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    return ta <= tb or tb <= ta


# As linhas do banco: (jogo, jogadores.id, time da API). O time vem do lado
# REAL da estatística (`jogador_estatisticas_jogo.lado` = mando real, §5.2).
SQL_LINHAS_ESTATISTICA = """
    SELECT DISTINCT j.fixture_id_api, jeg.jogador_id,
           CASE WHEN jeg.lado = 'mandante' THEN tm.api_football_team_id
                ELSE tv.api_football_team_id END
    FROM public.jogador_estatisticas_jogo jeg
    JOIN public.jogos j ON j.id = jeg.jogo_id
    JOIN public.times tm ON tm.id = j.mandante_id
    JOIN public.times tv ON tv.id = j.visitante_id
    WHERE j.fixture_id_api IN (SELECT fixture_id_api FROM public.homonimos_fixture)
"""
# Escalação sem estatística (reserva que não entrou): o banco não sabe o time.
SQL_LINHAS_SO_ESCALACAO = """
    SELECT DISTINCT j.fixture_id_api, e.jogador_id
    FROM public.escalacoes e
    JOIN public.jogos j ON j.id = e.jogo_id
    WHERE j.fixture_id_api IN (SELECT fixture_id_api FROM public.homonimos_fixture)
      AND NOT EXISTS (SELECT 1 FROM public.jogador_estatisticas_jogo jeg
                      JOIN public.jogos j2 ON j2.id = jeg.jogo_id
                      WHERE j2.fixture_id_api = j.fixture_id_api AND jeg.jogador_id = e.jogador_id)
"""


def primeiro_nome(nome):
    n = normalizar(nome).split()
    return n[0] if n else ""


def classificar(dono, nome_banco, escalacao, jogo_inteiro):
    """escalacao: (api_id, nome) do time da linha; jogo_inteiro: dos dois times.
    Devolve (situacao, api_id_certo, nome_api, n_candidatos).

    - O dono em QUALQUER time do jogo conta como dono: em parte de 2022 o
      `lado` da estatística parece trocado (o Gil aparece no time adversário);
      na dúvida, a linha fica com quem já está.
    - `dono == 0`: a API deu id 0 a jogadores sem cadastro, e todos caíram no
      mesmo `jogadores` ("Miguel Terceros", api 0). Não é homônimo; classe à
      parte, não se resolve pelo nome.
    - intruso: exatamente um nome compatível no time (ou, sem nenhum no time,
      no jogo inteiro).
    - provavel: nenhum compatível, mas exatamente um com o MESMO PRIMEIRO NOME
      no time ("Wesley Ribeiro" no banco × "Wesley Gassova" na escalação).
      Fica separado do intruso: a Fase 2 decide se usa."""
    if dono == 0:
        return "id_zero", None, None, 0
    if dono is not None and any(a == dono for a, _ in jogo_inteiro):
        return "dono", dono, None, 0
    for grupo in (escalacao, jogo_inteiro):
        cand = [(a, n) for a, n in grupo if a != dono and compativel(nome_banco, n)]
        if len(cand) == 1:
            return "intruso", cand[0][0], cand[0][1], 1
        if cand:
            return "ambiguo", None, None, len(cand)
    pn = primeiro_nome(nome_banco)
    cand = [(a, n) for a, n in escalacao if a != dono and pn and primeiro_nome(n) == pn]
    if len(cand) == 1:
        return "provavel", cand[0][0], cand[0][1], 1
    return ("ambiguo" if cand else "sem_par"), None, None, len(cand)


def montar_mapa(cur):
    cur.execute("SELECT fixture_id_api, team_api_id, api_id, nome FROM public.homonimos_lineup")
    por_time, por_jogo = {}, {}
    for f, tm, a, n in cur.fetchall():
        por_time.setdefault((int(f), int(tm)), []).append((int(a), n))
        por_jogo.setdefault(int(f), []).append((int(a), n))
    cur.execute("SELECT id, nome, api_football_id FROM public.jogadores")
    jog = {int(i): (n, (int(a) if a is not None else None)) for i, n, a in cur.fetchall()}

    saida, contagem = [], {}
    cur.execute(SQL_LINHAS_ESTATISTICA)
    for f, jid, tm in cur.fetchall():
        f, jid = int(f), int(jid)
        nome, dono = jog.get(jid, ("", None))
        tm = int(tm) if tm is not None else 0          # 0 = time desconhecido
        esc = por_time.get((f, tm), []) if tm else por_jogo.get(f, [])
        sit, certo, nome_api, nc = classificar(dono, nome, esc, por_jogo.get(f, []))
        contagem[("estatistica", sit)] = contagem.get(("estatistica", sit), 0) + 1
        if sit != "dono":
            saida.append((f, jid, tm, "estatistica", sit, certo, nome_api, nc))
    cur.execute(SQL_LINHAS_SO_ESCALACAO)
    for f, jid in cur.fetchall():
        f, jid = int(f), int(jid)
        nome, dono = jog.get(jid, ("", None))
        sit, certo, nome_api, nc = classificar(dono, nome, por_jogo.get(f, []), por_jogo.get(f, []))
        contagem[("escalacao", sit)] = contagem.get(("escalacao", sit), 0) + 1
        if sit != "dono":
            saida.append((f, jid, 0, "escalacao", sit, certo, nome_api, nc))

    cur.execute("DELETE FROM public.homonimos_linha")
    for i in range(0, len(saida), 500):
        bloco = saida[i:i + 500]
        cur.execute(
            "INSERT INTO public.homonimos_linha (fixture_id_api, jogador_id_atual, team_api_id, origem, "
            "situacao, api_id_certo, nome_api, candidatos) VALUES "
            + ",".join(["(%s, %s, %s, %s, %s, %s, %s, %s)"] * len(bloco)),
            [v for ln in bloco for v in ln])
    return contagem


def relatorio(cur, lista_suspeitos, todos):
    feitos = fixtures_buscados(cur)
    print(f"\n## COBERTURA: {len(feitos & set(todos))} de {len(todos)} jogo(s) com escalação já buscados")
    if not tabela_existe(cur, "homonimos_linha"):
        print("   (o mapa por jogo e time ainda não existe — rode `mapa` ou `aplicar`)")
        return None
    cur.execute("SELECT origem, situacao, COUNT(*), COUNT(DISTINCT jogador_id_atual) "
                "FROM public.homonimos_linha GROUP BY 1, 2 ORDER BY 1, 2")
    print("\n## O MAPA POR JOGO E TIME (`homonimos_linha`; as linhas do dono não entram)")
    print("   origem        situação    linhas   jogadores.id")
    for o, s, n, nj in cur.fetchall():
        print(f"   {o:<13} {s:<10} {int(n):>7}   {int(nj):>8}")

    cur.execute("""SELECT h.jogador_id_atual, jg.nome, jg.api_football_id, h.api_id_certo,
                          MIN(h.nome_api), COUNT(*)
                   FROM public.homonimos_linha h JOIN public.jogadores jg ON jg.id = h.jogador_id_atual
                   WHERE h.situacao = 'intruso'
                   GROUP BY 1, 2, 3, 4 ORDER BY COUNT(*) DESC, 1, 4""")
    pares = cur.fetchall()
    print(f"\n## INTRUSOS: {len(pares)} par(es) (jogadores.id fundido → id da API de outra pessoa); até 40")
    for jid, nome, dono, certo, nome_api, n in pares[:40]:
        print(f"   {jid:>6} {nome:<24} (dono api {dono})  ← api {certo:>7} \"{nome_api}\"  {int(n):>4} linha(s)")

    destinos = {int(r[0]) for r in pares}
    cur.execute("SELECT DISTINCT jogador_id_atual FROM public.homonimos_linha WHERE situacao = 'provavel'")
    provaveis = {int(r[0]) for r in cur.fetchall()}
    cur.execute("SELECT id FROM public.jogadores WHERE api_football_id = 0")
    ids_zero = {int(r[0]) for r in cur.fetchall()}
    faltam = [s for s in lista_suspeitos if s[0] not in destinos]
    print(f"\n## CRITÉRIO A1: {len(lista_suspeitos) - len(faltam)} de {len(lista_suspeitos)} "
          f"suspeitos do teste do mesmo dia são destino de um intruso")
    for jid, nome, api, dias in faltam[:30]:
        cur.execute("SELECT situacao, COUNT(*) FROM public.homonimos_linha WHERE jogador_id_atual = %s "
                    "GROUP BY 1 ORDER BY 1", (jid,))
        sits = ", ".join(f"{s} {int(n)}" for s, n in cur.fetchall()) or "nenhuma linha fora do dono"
        print(f"   ❌ {jid:>6}  {nome:<28} api {api}  ({dias} dia(s) duplo(s)) — {sits}")
    so_provavel = [s for s in faltam if s[0] in provaveis]
    zero = [s for s in faltam if s[0] in ids_zero]
    print(f"   desses que faltam: {len(so_provavel)} explicado(s) só por 'provavel' (mesmo primeiro nome) · "
          f"{len(zero)} com id 0 da API (não é homônimo; outra causa)")
    completo = len(feitos & set(todos)) == len(todos)
    if faltam and not completo:
        print("   ⏳ mapa PARCIAL — ainda faltam jogos; o A1 só vale com a cobertura completa")
    elif faltam:
        print("   🔴 A1 REPROVADO — o mapa não explica todos os suspeitos. A Fase 2 NÃO roda.")
    else:
        print("   ✅ A1 aprovado" + ("" if completo else " (com o mapa ainda parcial)"))
    cur.execute("SELECT situacao, COUNT(*) FROM public.homonimos_linha "
                "WHERE situacao IN ('ambiguo', 'sem_par', 'provavel', 'id_zero') GROUP BY 1 ORDER BY 1")
    for s, n in cur.fetchall():
        print(f"   ⚠️ {int(n)} linha(s) {s} — a Fase 2 não mexe nelas; olhar antes")
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
        for t in ("homonimos_fixture", "homonimos_lineup", "homonimos_api", "homonimos_linha"):
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


def fase_mapa():
    conn = conectar()
    cur = conn.cursor()
    try:
        for ddl in DDL:
            cur.execute(ddl)
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'claude_leitura'")
        if cur.fetchone():
            cur.execute("GRANT SELECT ON public.homonimos_linha TO claude_leitura")
        conn.commit()
        antes = contagens(cur)
        cur.execute(SQL_FIXTURES_COM_ESCALACAO)
        todos = [int(r[0]) for r in cur.fetchall()]
        contagem = montar_mapa(cur)
        depois = contagens(cur)
        if depois != antes:
            conn.rollback()
            raise RuntimeError(f"tabelas vigiadas mudaram durante a execução — nada gravado: "
                               f"{ {t: (antes[t], depois[t]) for t in antes if antes[t] != depois[t]} }")
        conn.commit()
        print("\nlinhas do banco classificadas (origem, situação → quantas):")
        for (o, s), n in sorted(contagem.items()):
            print(f"   {o:<12} {s:<9} {n:>7}")
        print(f"✅ `homonimos_linha` refeito · tabelas vigiadas iguais antes e depois "
              f"({', '.join(TABELAS_VIGIADAS)})")
        relatorio(cur, suspeitos(cur), todos)
        conn.rollback()
    finally:
        conn.close()
    print("\nFASE MAPA — nenhuma chamada à API; escrito só em homonimos_linha.")


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
                        "public.homonimos_api, public.homonimos_linha TO claude_leitura")
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
        print(f"✅ `homonimos_linha` refeito · tabelas vigiadas iguais antes e depois "
              f"({', '.join(TABELAS_VIGIADAS)})")

        relatorio(cur, suspeitos(cur), todos)
        conn.rollback()
    finally:
        conn.close()
        conn_cota.close()
    print("\nFASE APLICAR — escrito só em homonimos_fixture, homonimos_lineup, homonimos_linha "
          "(e o contador de cota).")


def ler_argumentos(argv):
    if not argv or argv[0] not in ("verificar", "aplicar", "mapa", "relatorio"):
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
    elif fase == "mapa":
        fase_mapa()
    else:
        fase_aplicar(teto, limite)


if __name__ == "__main__":
    main()
