"""
cadastrar_participantes_ligas.py — cadastra o `oddspapi_participant_id` dos
times das 5 ligas europeias, em `liga_<chave>.times`. Duas fases.

    python cadastrar_participantes_ligas.py verificar [--liga premier,laliga,...]
    python cadastrar_participantes_ligas.py aplicar   [--liga premier,laliga,...]

POR QUÊ (13/10/2026): o `atualizar_odds.get_or_create_time` procura o time
pelo `oddspapi_participant_id` e, se não achar, pelo NOME — e, se o nome não
bater, CRIA um time novo. Nas ligas, "Man Utd" × "Manchester United" viraria
time duplicado no primeiro dia de odds. Cadastrar os IDs antes fecha a porta.

COMO CASA (por jogo, não por nome):
  1. Uma chamada ao calendário da OddsPapi por liga (`/fixtures` com
     `tournamentId`, sem `participantId`). Cada jogo traz participant1 (mandante)
     e participant2 (visitante), com ID e nome.
  2. Cada jogo da OddsPapi é comparado com os jogos do banco da mesma liga e
     temporada com data a até 1 dia (a OddsPapi e a API-Football divergem na
     data, `1_ESSENCIAL §4`).
  3. O voto é por DERIVAÇÃO EXATA: se o adversário já está casado, só existe
     um jogo do banco na janela em que ele jogou daquele lado, e o outro lado
     desse jogo é o candidato. O nome serve só de SEMENTE para começar a
     corrente; nenhum ID é aceito sem derivações por jogo (>= VOTOS_MINIMOS,
     e sem candidato concorrente relevante). Dois IDs no mesmo time: nenhum
     entra.
  4. Prova final: quantos jogos da OddsPapi ficam explicados pelo mapa
     (os dois IDs mapeados e um jogo do banco com esse mandante × visitante a
     até 1 dia).

O QUE GRAVA: só `liga_<chave>.times.oddspapi_participant_id`, só onde está
vazio. Nome de tabela sempre QUALIFICADO pelo schema (nada depende de
`search_path`). Nunca sobrescreve um ID já cadastrado: se o cadastrado
discorda do casamento, é CONFLITO e a liga inteira fica de fora.

O QUE NÃO TOCA: `public` (Brasileirão). Contagem e md5 de `public.times`
conferidos antes e depois, na mesma transação; se mudarem, rollback.

CUSTO: 1 requisição da OddsPapi por liga (5 no total), pela `ODDSPAPI_KEY`
(o calendário não depende da casa do plano). Nenhuma da API-Football.

FASE `verificar`: sessão SÓ LEITURA (conferida com SHOW), termina em
rollback — nenhuma tabela é tocada, em nenhum schema.
"""

import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import psycopg2

import atualizar_odds as ao  # API_BASE, SPORT_ID, API_KEY e o get com retentativa de PRODUÇÃO

# chave: (tournamentId na OddsPapi [medido, 02/10], liga_api_id na API-Football)
LIGAS = {
    "premier": (17, 39),
    "laliga": (8, 140),
    "seriea": (23, 135),
    "bundesliga": (35, 78),
    "ligue1": (34, 61),
}
TEMPORADA = 2026                       # season da API-Football = 2026/27
INICIO_TEMPORADA = datetime(2026, 7, 1, tzinfo=timezone.utc)
TOLERANCIA_DIAS = 1
VOTOS_MINIMOS = 2
MARGEM_SOBRE_SEGUNDO = 4.0
SEMENTE_NOME = 0.80

DATABASE_URL = os.environ["DATABASE_URL"]


class Recusa(Exception):
    pass


# ---------------------------------------------------------------------------
# utilidades
# ---------------------------------------------------------------------------
def mascarar(texto):
    texto = str(texto)
    for chave in (os.environ.get("ODDSPAPI_KEY"), os.environ.get("ODDSPAPI_KEY_TESTE")):
        if chave:
            texto = texto.replace(chave, "***")
    return texto


def normalizar(nome):
    return " ".join(str(nome or "").lower().replace(".", " ").replace("-", " ").split())


def parecido(a, b):
    return round(SequenceMatcher(None, normalizar(a), normalizar(b)).ratio(), 2)


def cabecalho(fase, ligas):
    print("=" * 78)
    print(f"cadastrar_participantes_ligas.py | fase: {fase}")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"ligas: {', '.join(ligas)} · temporada {TEMPORADA} · tolerância de data ±{TOLERANCIA_DIAS} dia")
    print("=" * 78)


# ---------------------------------------------------------------------------
# OddsPapi
# ---------------------------------------------------------------------------
requisicoes = {"total": 0}


def buscar_fixtures(tournament_id):
    """Calendário da liga. 1 requisição (mais as retentativas de 429 do
    `get_com_retry_429` de produção)."""
    requisicoes["total"] += 1
    resp = ao.get_com_retry_429(
        f"{ao.API_BASE}/fixtures",
        params={"tournamentId": tournament_id, "sportId": ao.SPORT_ID, "apiKey": ao.API_KEY},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"/fixtures (tournamentId={tournament_id}) respondeu {resp.status_code}: "
                           f"{mascarar(resp.text[:300])}")
    return resp.json()


def jogos_oddspapi(brutos):
    """(data UTC, pid mandante, nome, pid visitante, nome) dos jogos da temporada."""
    saida = []
    for j in brutos or []:
        inicio, p1, p2 = j.get("startTime"), j.get("participant1Id"), j.get("participant2Id")
        if not inicio or p1 is None or p2 is None:
            continue
        quando = datetime.fromisoformat(str(inicio).replace("Z", "+00:00"))
        if quando < INICIO_TEMPORADA:
            continue
        saida.append((quando.date(), p1, j.get("participant1Name"), p2, j.get("participant2Name")))
    return saida


# ---------------------------------------------------------------------------
# banco (tudo QUALIFICADO pelo schema)
# ---------------------------------------------------------------------------
def schema_existe(cur, schema):
    cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,))
    return cur.fetchone() is not None


def jogos_do_banco(cur, schema):
    """Um registro por fixture: (data, id do mandante, id do visitante), a
    partir das perspectivas (`mandante` = a perspectiva é a do mandante)."""
    cur.execute(
        f"SELECT fixture_id_api, data_jogo, nosso_time_id, mandante FROM {schema}.jogos "
        f"WHERE temporada = %s AND fixture_id_api IS NOT NULL",
        (TEMPORADA,),
    )
    por_fixture = defaultdict(dict)
    for fixture, data, time_id, eh_mandante in cur.fetchall():
        por_fixture[fixture]["data"] = data
        por_fixture[fixture]["mand" if eh_mandante else "vis"] = time_id
    jogos = []
    for fixture, d in por_fixture.items():
        if d.get("mand") is not None and d.get("vis") is not None and d.get("data") is not None:
            jogos.append((d["data"], d["mand"], d["vis"]))
    return jogos


def times_da_temporada(cur, schema):
    cur.execute(
        f"SELECT t.id, t.nome, t.oddspapi_participant_id FROM {schema}.times t "
        f"WHERE t.id IN (SELECT nosso_time_id FROM {schema}.jogos WHERE temporada = %s) ORDER BY t.nome",
        (TEMPORADA,),
    )
    return {r[0]: {"nome": r[1], "pid": r[2]} for r in cur.fetchall()}


def pids_ja_usados(cur, schema):
    cur.execute(f"SELECT oddspapi_participant_id, id FROM {schema}.times WHERE oddspapi_participant_id IS NOT NULL")
    return {r[0]: r[1] for r in cur.fetchall()}


def retrato_public(cur):
    cur.execute("SELECT COUNT(*), md5(string_agg(t::text, '|' ORDER BY t.id)) FROM public.times t")
    return cur.fetchone()


# ---------------------------------------------------------------------------
# o casamento
# ---------------------------------------------------------------------------
def casar(op_jogos, db_jogos, times):
    """Devolve (mapa pid -> time_id, relatório por pid, jogos explicados).

    Votar "todo jogo da janela" não separa: numa janela de ±1 dia cabe a
    rodada inteira, e o time errado ganha voto quase toda semana. Então o voto
    é por DERIVAÇÃO EXATA: se o adversário de um jogo da OddsPapi já está
    casado, só existe (normalmente) UM jogo do banco na janela em que ele
    jogou daquele lado — e o outro lado desse jogo é o candidato.

    A semente é o nome (parecido >= SEMENTE_NOME e sem empate), mas a semente
    sozinha NUNCA é aceita: todo ID final precisa de derivações por jogo.
    """
    por_data = defaultdict(list)
    for data, mand, vis in db_jogos:
        por_data[data].append((mand, vis))

    def na_janela(data):
        for d in range(-TOLERANCIA_DIAS, TOLERANCIA_DIAS + 1):
            yield from por_data.get(data + timedelta(days=d), [])

    nomes_op = {}
    for _, p1, n1, p2, n2 in op_jogos:
        nomes_op[p1], nomes_op[p2] = n1, n2

    # semente pelo nome
    conhecido = {}
    for pid, nome in nomes_op.items():
        notas = sorted(((parecido(nome, t["nome"]), tid) for tid, t in times.items()), reverse=True)
        if notas and notas[0][0] >= SEMENTE_NOME and (len(notas) == 1 or notas[0][0] - notas[1][0] >= 0.15):
            conhecido[pid] = notas[0][1]

    relatorio, aceito = {}, {}
    for _ in range(10):
        derivacoes = defaultdict(lambda: defaultdict(int))
        for data, p1, _, p2, _ in op_jogos:
            # p1 é mandante; se o visitante p2 é conhecido, acha o jogo dele como visitante
            if p2 in conhecido:
                achados = [m for m, v in na_janela(data) if v == conhecido[p2]]
                if len(achados) == 1:
                    derivacoes[p1][achados[0]] += 1
            if p1 in conhecido:
                achados = [v for m, v in na_janela(data) if m == conhecido[p1]]
                if len(achados) == 1:
                    derivacoes[p2][achados[0]] += 1
        novo = {}
        relatorio = {}
        for pid in nomes_op:
            cand = sorted(derivacoes[pid].items(), key=lambda x: -x[1])
            if not cand:
                relatorio[pid] = {"nome_op": nomes_op[pid], "time_id": None, "votos": 0, "segundo": 0,
                                  "status": "SEM DERIVAÇÃO"}
                continue
            topo_id, topo = cand[0]
            segundo = cand[1][1] if len(cand) > 1 else 0
            ok = topo >= VOTOS_MINIMOS and topo >= MARGEM_SOBRE_SEGUNDO * segundo and (segundo == 0 or topo >= 4)
            relatorio[pid] = {"nome_op": nomes_op[pid], "time_id": topo_id, "votos": topo,
                              "segundo": segundo, "status": "ok" if ok else "AMBÍGUO"}
            if ok:
                novo[pid] = topo_id
        # a semente segue valendo para derivar enquanto não for contrariada
        proximo = dict(conhecido)
        proximo.update(novo)
        if novo == aceito and proximo == conhecido:
            break
        aceito, conhecido = novo, proximo

    escolha = dict(aceito)
    por_time = defaultdict(list)
    for pid, tid in escolha.items():
        por_time[tid].append(pid)
    for tid, pids in por_time.items():
        if len(pids) > 1:
            for pid in pids:
                relatorio[pid]["status"] = "COLISÃO"
                escolha.pop(pid, None)
    for pid, tid in list(escolha.items()):
        if tid not in times:
            relatorio[pid]["status"] = "TIME FORA DA TEMPORADA"
            escolha.pop(pid)

    explicados = 0
    for data, p1, _, p2, _ in op_jogos:
        if p1 in escolha and p2 in escolha and (escolha[p1], escolha[p2]) in list(na_janela(data)):
            explicados += 1
    return escolha, relatorio, explicados


def analisar_liga(cur, chave, brutos):
    schema = f"liga_{chave}"
    if not schema_existe(cur, schema):
        raise Recusa(f"schema `{schema}` não existe")
    op = jogos_oddspapi(brutos)
    db = jogos_do_banco(cur, schema)
    times = times_da_temporada(cur, schema)
    escolha, relatorio, explicados = casar(op, db, times)
    usados = pids_ja_usados(cur, schema)

    gravar, conflitos = {}, []
    for pid, tid in escolha.items():
        ja = times[tid]["pid"]
        if ja is not None and ja != pid:
            conflitos.append(f"{times[tid]['nome']} (id {tid}) já tem {ja}; o casamento diz {pid}")
        elif pid in usados and usados[pid] != tid:
            conflitos.append(f"participante {pid} já está no time id {usados[pid]}; o casamento diz id {tid}")
        elif ja is None:
            gravar[tid] = pid

    print(f"\n## {chave.upper()} ({schema})")
    print(f"  jogos da OddsPapi na temporada: {len(op)} · jogos do banco (temporada {TEMPORADA}): {len(db)} · "
          f"times na temporada: {len(times)}")
    print(f"  {'time no banco':<28}{'id':>6}  {'participante':>11}  {'nome na OddsPapi':<28}{'votos':>6}{'2º':>4}"
          f"{'nome≈':>7}  estado")
    pid_por_time = {tid: pid for pid, tid in escolha.items()}
    for tid, t in times.items():
        pid = pid_por_time.get(tid)
        if pid is None:
            print(f"  {t['nome'][:27]:<28}{tid:>6}  {'—':>11}  {'(sem casamento)':<28}{'':>6}{'':>4}{'':>7}  "
                  f"{'já cadastrado: ' + str(t['pid']) if t['pid'] else 'SEM ID'}")
            continue
        r = relatorio[pid]
        estado = "já cadastrado, igual" if t["pid"] == pid else ("GRAVAR" if tid in gravar else "CONFLITO")
        print(f"  {t['nome'][:27]:<28}{tid:>6}  {pid:>11}  {str(r['nome_op'])[:27]:<28}{r['votos']:>6}"
              f"{r['segundo']:>4}{parecido(t['nome'], r['nome_op']):>7}  {estado}")
    sobra = [(pid, r) for pid, r in relatorio.items() if r["status"] != "ok"]
    if sobra:
        print("  participantes da OddsPapi NÃO aceitos:")
        for pid, r in sobra:
            nome_tid = times.get(r["time_id"], {}).get("nome", f"id {r['time_id']}")
            print(f"    {pid} {r['nome_op']} — {r['status']} (melhor: {nome_tid}, {r['votos']} votos; 2º: {r['segundo']})")
    print(f"  prova: {explicados} de {len(op)} jogos da OddsPapi explicados pelo mapa")
    for c in conflitos:
        print(f"  🔴 CONFLITO: {c}")
    sem_id = [t["nome"] for tid, t in times.items() if t["pid"] is None and tid not in gravar]
    print(f"  resumo: gravar {len(gravar)} · já cadastrados {sum(1 for t in times.values() if t['pid'])} · "
          f"ficam sem ID {len(sem_id)}{(' (' + ', '.join(sem_id) + ')') if sem_id else ''} · conflitos {len(conflitos)}")
    return schema, gravar, conflitos


# ---------------------------------------------------------------------------
# fases
# ---------------------------------------------------------------------------
def ler_ligas(argv):
    if "--liga" in argv:
        i = argv.index("--liga")
        if i + 1 >= len(argv):
            raise SystemExit("--liga precisa de valor (ex.: --liga premier,laliga)")
        ligas = [x.strip() for x in argv[i + 1].split(",") if x.strip()]
    else:
        ligas = list(LIGAS)
    desconhecidas = [x for x in ligas if x not in LIGAS]
    if desconhecidas:
        raise SystemExit(f"liga desconhecida: {desconhecidas} (válidas: {list(LIGAS)})")
    return ligas


def buscar_todas(ligas):
    brutos = {}
    for chave in ligas:
        try:
            brutos[chave] = buscar_fixtures(LIGAS[chave][0])
        except Exception as e:
            print(f"⚠️ {chave}: calendário da OddsPapi falhou ({mascarar(e)[:200]}) — liga fica de fora")
    print(f"\nRequisições da OddsPapi: {requisicoes['total']}")
    return brutos


def fase_verificar(ligas):
    cabecalho("verificar (SÓ LEITURA)", ligas)
    brutos = buscar_todas(ligas)
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        conn.commit()  # só vale da PRÓXIMA transação (1_ESSENCIAL §6.2)
        cur.execute("SHOW transaction_read_only")
        if cur.fetchone()[0] != "on":
            raise Recusa("a sessão não ficou READ ONLY — abortando")
        for chave in ligas:
            if chave in brutos:
                try:
                    analisar_liga(cur, chave, brutos[chave])
                except Recusa as e:
                    print(f"\n🔴 {chave}: {e}")
    finally:
        conn.rollback()
        conn.close()
    print("\n" + "=" * 78)
    print("FASE VERIFICAR — SÓ LEITURA: nenhuma tabela foi tocada, em nenhum schema "
          "(nem `liga_*.times`, nem `public`).")
    print("=" * 78)


def fase_aplicar(ligas):
    cabecalho("aplicar", ligas)
    brutos = buscar_todas(ligas)
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    try:
        cur = conn.cursor()
        antes_public = retrato_public(cur)
        total = 0
        for chave in ligas:
            if chave not in brutos:
                continue
            try:
                schema, gravar, conflitos = analisar_liga(cur, chave, brutos[chave])
            except Recusa as e:
                print(f"\n🔴 {chave}: {e} — liga fica de fora")
                continue
            if conflitos:
                print(f"  🔴 {chave}: {len(conflitos)} conflito(s) — NADA gravado nesta liga")
                continue
            for tid, pid in gravar.items():
                cur.execute(
                    f"UPDATE {schema}.times SET oddspapi_participant_id = %s "
                    f"WHERE id = %s AND oddspapi_participant_id IS NULL",
                    (pid, tid),
                )
                if cur.rowcount != 1:
                    raise Recusa(f"{schema}.times id {tid}: UPDATE afetou {cur.rowcount} linhas (esperado 1)")
            total += len(gravar)
            print(f"  ✅ {chave}: {len(gravar)} time(s) gravado(s)")
        depois_public = retrato_public(cur)
        if antes_public != depois_public:
            raise Recusa(f"`public.times` mudou ({antes_public} → {depois_public}) — rollback")
        conn.commit()
        print(f"\n✅ commit · {total} time(s) com `oddspapi_participant_id` novo · `public.times` idêntico "
              f"({antes_public[0]} linhas, md5 igual)")
    except Exception:
        conn.rollback()
        print("\n🔴 erro — ROLLBACK, nada foi gravado")
        raise
    finally:
        conn.close()


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("verificar", "aplicar"):
        raise SystemExit(__doc__)
    ligas = ler_ligas(sys.argv[2:])
    if sys.argv[1] == "verificar":
        fase_verificar(ligas)
    else:
        fase_aplicar(ligas)


if __name__ == "__main__":
    main()
