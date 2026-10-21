"""
separar_homonimos.py — H-ID, Fase 2 (`claude/arquitetura_identidade_jogador.md §3`).

SEPARA O ACERVO DOS HOMÔNIMOS FUNDIDOS. Lê o mapa da Fase 0
(`homonimos_linha`, feito pelo `mapear_homonimos.py mapa`) e, para cada linha
classificada como `intruso`, TROCA O `jogador_id` das linhas daquele jogo para
o `jogadores` da pessoa certa (o id da API que estava na escalação).

🔴 REGRA CENTRAL: SÓ TROCA `jogador_id`. Nunca insere, apaga ou muda contagem
de gol, cartão, substituição, escalação ou estatística. Por isso os totais por
(jogo, lado) ficam iguais por construção — e o script confere isso por md5,
jogo a jogo, antes de cada commit (critério A2).

    python separar_homonimos.py verificar [--limite N] [--fixture F]
    python separar_homonimos.py aplicar   [--limite N] [--fixture F]

  verificar  faz TUDO numa transação e desfaz no fim (ROLLBACK): mostra o que
             seria trocado, quantos `jogadores` seriam criados, o teste do
             mesmo dia antes e "depois", e o que ficou de fora. Nada fica
             gravado — nem os `jogadores` novos.
  aplicar    o mesmo, com COMMIT jogo a jogo. Jogo cuja conferência falha é
             desfeito sozinho (SAVEPOINT) e os outros seguem. Pode ser
             interrompido e rodado de novo: o que já foi trocado não aparece
             mais (as linhas já não estão no id fundido).

O QUE ENTRA: só `situacao = 'intruso'`. `provavel`, `ambiguo`, `sem_par` e
`id_zero` ficam onde estão (decisão de 21/10). Para cada (jogo F, `jogadores.id`
X) com intruso:
  unico     X só representa UMA pessoa em F (o dono não jogou, e não há outra
            linha do mapa para X em F) → TODAS as linhas de X em F vão para a
            pessoa certa, em todas as tabelas, sem olhar o lado (cobre o lado
            trocado de 2022 e o gol contra).
  por_time  X representa 2+ pessoas em F (o dono e o intruso, ou dois
            intrusos) → só as linhas do TIME do intruso, e só se nenhuma
            outra pessoa de X está naquele time:
              jogador_estatisticas_jogo  pelo `lado` (mando REAL)
              gols, cartoes, substituicoes  pelo `lado` relativo à perspectiva
                                         (`'mandante'` = time da perspectiva)
              gol contra                 fica (o time do evento é ambíguo)
              escalacoes                 pelo `titular` da escalação da API,
                                         só se ele separa as pessoas e há
                                         exatamente uma linha com esse valor
  O que não dá para separar fica no id fundido e é CONTADO no fim.

O `jogadores` da pessoa certa: o que já tem esse `api_football_id` (pode ter
sido criado pelo `popular_banco` desde 21/10, Fase 1) ou um novo, com o nome
da escalação, `ativo = FALSE` e `time_atual_id` NULL (o `motor_padroes`
recalcula os dois no cron seguinte).

NÃO TOCA, de propósito (arquitetura §3, Fase 2): `historico_recomendacoes`,
`registro_modelo`, `registro_publico`, pernas de múltiplas (registram o que
FOI recomendado), `odds`, `recomendacoes`, `lesoes_suspensoes`, `padroes_*`
(regravados pelo cron).

QUANDO RODAR: fora das janelas dos crons (09:00-10:00, ~12:00-12:10 e
~14:00-14:05 BRT) — o `popular_banco` das 09:00 grava em `jogadores` e nas
tabelas de jogo. E DEPOIS de um `mapear_homonimos.py mapa` recente: o mapa
fala dos `jogadores.id` de quando foi feito.

EFEITO: muda o `p_modelo` de jogador a partir do cron seguinte (os padrões são
regravados com as pessoas separadas). Todo número de mercado de jogador
anterior é série à parte (`1_ESSENCIAL §5.22`).
"""

import os
import sys
from datetime import datetime, timezone

import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

TABELAS_VIGIADAS = ["jogador_estatisticas_jogo", "escalacoes", "gols", "cartoes", "substituicoes"]

# A conferência do A2: o conteúdo de cada tabela do jogo SEM as colunas de
# jogador. Se o script só trocou `jogador_id`, isto não muda.
SQL_IMPRESSAO = {
    "jogador_estatisticas_jogo": """
        SELECT COUNT(*), md5(COALESCE(string_agg(
            concat_ws('|', id, jogo_id, lado, minutos, posicao, nota, chutes, chutes_no_gol, gols,
                      assistencias, passes, passes_certos, desarmes, interceptacoes, duelos_total,
                      duelos_vencidos, dribles_tentados, dribles_sucesso, faltas_cometidas,
                      faltas_sofridas, impedimentos, cartao_amarelo, cartao_vermelho,
                      penalti_marcado, penalti_perdido, penalti_sofrido), ',' ORDER BY id), ''))
        FROM public.jogador_estatisticas_jogo WHERE jogo_id = ANY(%s)""",
    "escalacoes": """
        SELECT COUNT(*), md5(COALESCE(string_agg(
            concat_ws('|', id, jogo_id, titular, minuto_saida), ',' ORDER BY id), ''))
        FROM public.escalacoes WHERE jogo_id = ANY(%s)""",
    "gols": """
        SELECT COUNT(*), md5(COALESCE(string_agg(
            concat_ws('|', id, jogo_id, lado, minuto, periodo, penalti, gol_contra), ',' ORDER BY id), ''))
        FROM public.gols WHERE jogo_id = ANY(%s)""",
    "cartoes": """
        SELECT COUNT(*), md5(COALESCE(string_agg(
            concat_ws('|', id, jogo_id, lado, cor, minuto, periodo), ',' ORDER BY id), ''))
        FROM public.cartoes WHERE jogo_id = ANY(%s)""",
    "substituicoes": """
        SELECT COUNT(*), md5(COALESCE(string_agg(
            concat_ws('|', id, jogo_id, lado, minuto, periodo), ',' ORDER BY id), ''))
        FROM public.substituicoes WHERE jogo_id = ANY(%s)""",
}

# Teste de identidade (`1_ESSENCIAL §5.22`): um jogador não joga duas partidas
# diferentes no mesmo dia.
SQL_MESMO_DIA = """
    SELECT COUNT(DISTINCT jogador_id), COUNT(*) FROM (
        SELECT jeg.jogador_id, j.data_jogo
        FROM public.jogador_estatisticas_jogo jeg
        JOIN public.jogos j ON j.id = jeg.jogo_id
        GROUP BY 1, 2
        HAVING COUNT(DISTINCT j.fixture_id_api) > 1) t
"""


def cabecalho(fase, limite, fixture):
    print("=" * 78)
    print(f"separar_homonimos.py | fase: {fase} | limite: {limite or '-'} | fixture: {fixture or '-'}")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print("=" * 78)


def ler_argumentos(argv):
    if not argv or argv[0] not in ("verificar", "aplicar"):
        raise SystemExit(__doc__)
    limite = fixture = None
    if "--limite" in argv:
        limite = int(argv[argv.index("--limite") + 1])
    if "--fixture" in argv:
        fixture = int(argv[argv.index("--fixture") + 1])
    return argv[0], limite, fixture


def tabela_existe(cur, nome):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{nome}",))
    return cur.fetchone()[0]


def contagens(cur):
    out = {}
    for t in TABELAS_VIGIADAS + ["jogadores"]:
        cur.execute(f"SELECT COUNT(*) FROM public.{t}")
        out[t] = cur.fetchone()[0]
    return out


def mesmo_dia(cur):
    cur.execute(SQL_MESMO_DIA)
    return cur.fetchone()


# ---------------------------------------------------------------------------
# o plano
# ---------------------------------------------------------------------------
def fixtures_com_intruso(cur, limite, fixture):
    sql = "SELECT DISTINCT fixture_id_api FROM public.homonimos_linha WHERE situacao = 'intruso'"
    par = []
    if fixture:
        sql += " AND fixture_id_api = %s"
        par.append(fixture)
    sql += " ORDER BY 1"
    if limite:
        sql += f" LIMIT {int(limite)}"
    cur.execute(sql, par)
    return [r[0] for r in cur.fetchall()]


def perspectivas(cur, fixture):
    """{jogos.id: (time da perspectiva, time adversário)} em ids da API, e o
    par (mandante, visitante) em ids da API."""
    cur.execute("""
        SELECT j.id, tn.api_football_team_id, tm.api_football_team_id, tv.api_football_team_id
        FROM public.jogos j
        JOIN public.times tn ON tn.id = j.nosso_time_id
        JOIN public.times tm ON tm.id = j.mandante_id
        JOIN public.times tv ON tv.id = j.visitante_id
        WHERE j.fixture_id_api = %s""", (fixture,))
    persp, mand, vis = {}, None, None
    for jid, nosso, m, v in cur.fetchall():
        persp[jid] = (nosso, v if nosso == m else m)
        mand, vis = m, v
    return persp, mand, vis


def pessoas_de_x(cur, fixture, x):
    """As pessoas que o `jogadores.id` X representa no jogo: as linhas do mapa
    (qualquer situação) e o dono, se ele está na escalação da API."""
    cur.execute("""SELECT team_api_id, situacao, api_id_certo, nome_api
                   FROM public.homonimos_linha WHERE fixture_id_api = %s AND jogador_id_atual = %s""",
                (fixture, x))
    linhas = cur.fetchall()
    cur.execute("""SELECT l.team_api_id, l.titular FROM public.homonimos_lineup l
                   JOIN public.jogadores jg ON jg.api_football_id = l.api_id
                   WHERE l.fixture_id_api = %s AND jg.id = %s""", (fixture, x))
    dono = cur.fetchall()
    return linhas, dono


def titular_na_api(cur, fixture, api_id):
    cur.execute("SELECT titular FROM public.homonimos_lineup WHERE fixture_id_api = %s AND api_id = %s",
                (fixture, api_id))
    r = cur.fetchall()
    return r[0][0] if len(r) == 1 else None


def jogador_destino(cur, api_id, nome_api, criados):
    cur.execute("SELECT id FROM public.jogadores WHERE api_football_id = %s", (api_id,))
    r = cur.fetchone()
    if r:
        return r[0]
    cur.execute("""INSERT INTO public.jogadores (nome, api_football_id, ativo, time_atual_id)
                   VALUES (%s, %s, FALSE, NULL) RETURNING id""", (nome_api, api_id))
    novo = cur.fetchone()[0]
    criados.append((novo, api_id, nome_api))
    return novo


def impressao(cur, jogos_ids):
    out = {}
    for t, sql in SQL_IMPRESSAO.items():
        cur.execute(sql, (jogos_ids,))
        out[t] = tuple(cur.fetchone())
    return out


# ---------------------------------------------------------------------------
# um jogo
# ---------------------------------------------------------------------------
def separar_jogo(cur, fixture, criados, sobra):
    """Troca o jogador_id das linhas de um jogo. Devolve {tabela: linhas}."""
    persp, mand, vis = perspectivas(cur, fixture)
    ids = sorted(persp)
    trocas = {t: 0 for t in TABELAS_VIGIADAS}
    if not ids:
        sobra["jogo_sem_perspectiva"] += 1
        return trocas, ids
    cur.execute("""SELECT DISTINCT jogador_id_atual FROM public.homonimos_linha
                   WHERE fixture_id_api = %s AND situacao = 'intruso' ORDER BY 1""", (fixture,))
    for (x,) in cur.fetchall():
        linhas, dono = pessoas_de_x(cur, fixture, x)
        intrusos = sorted({(t, a, n) for t, s, a, n in linhas if s == "intruso"})
        outros = [(t, s) for t, s, a, n in linhas if s != "intruso"]
        times_ocupados = [t for t, _ in dono] + [t for t, _ in outros]

        if not dono and not outros and len(intrusos) == 1:
            # ---- unico: tudo de X neste jogo é da pessoa certa
            _, api, nome = intrusos[0]
            y = jogador_destino(cur, api, nome, criados)
            for t in ("jogador_estatisticas_jogo", "escalacoes", "gols", "cartoes"):
                cur.execute(f"UPDATE public.{t} SET jogador_id = %s WHERE jogo_id = ANY(%s) AND jogador_id = %s",
                            (y, ids, x))
                trocas[t] += cur.rowcount
            for col in ("jogador_saiu_id", "jogador_entrou_id"):
                cur.execute(f"UPDATE public.substituicoes SET {col} = %s WHERE jogo_id = ANY(%s) AND {col} = %s",
                            (y, ids, x))
                trocas["substituicoes"] += cur.rowcount
            continue

        # ---- por_time
        titulares_outros = [tit for _, tit in dono]
        for time_api, api, nome in intrusos:
            # outra pessoa de X no mesmo time — ou de time desconhecido (0),
            # que pode estar em qualquer um — impede separar pelo time
            if not time_api or time_api in times_ocupados or 0 in times_ocupados or \
                    sum(1 for t, _, _ in intrusos if t == time_api) > 1:
                sobra["por_time_sem_time_proprio"] += 1
                continue
            y = jogador_destino(cur, api, nome, criados)
            # estatística: lado = mando REAL
            lado_real = "mandante" if time_api == mand else ("visitante" if time_api == vis else None)
            if lado_real:
                cur.execute("""UPDATE public.jogador_estatisticas_jogo SET jogador_id = %s
                               WHERE jogo_id = ANY(%s) AND jogador_id = %s AND lado = %s""",
                            (y, ids, x, lado_real))
                trocas["jogador_estatisticas_jogo"] += cur.rowcount
            # eventos: lado relativo à perspectiva
            for jid, (nosso, adv) in persp.items():
                lado_rel = "mandante" if time_api == nosso else ("visitante" if time_api == adv else None)
                if not lado_rel:
                    continue
                cur.execute("""UPDATE public.gols SET jogador_id = %s WHERE jogo_id = %s AND jogador_id = %s
                               AND lado = %s AND gol_contra IS NOT TRUE""", (y, jid, x, lado_rel))
                trocas["gols"] += cur.rowcount
                cur.execute("""SELECT COUNT(*) FROM public.gols WHERE jogo_id = %s AND jogador_id = %s
                               AND gol_contra IS TRUE""", (jid, x))
                sobra["gol_contra"] += cur.fetchone()[0]
                cur.execute("""UPDATE public.cartoes SET jogador_id = %s WHERE jogo_id = %s AND jogador_id = %s
                               AND lado = %s""", (y, jid, x, lado_rel))
                trocas["cartoes"] += cur.rowcount
                for col in ("jogador_saiu_id", "jogador_entrou_id"):
                    cur.execute(f"""UPDATE public.substituicoes SET {col} = %s WHERE jogo_id = %s
                                    AND {col} = %s AND lado = %s""", (y, jid, x, lado_rel))
                    trocas["substituicoes"] += cur.rowcount
            # escalação: sem coluna de time — só pelo titular, se ele separa
            tit = titular_na_api(cur, fixture, api)
            outros_tit = titulares_outros + [titular_na_api(cur, fixture, a) for t, a, _ in intrusos if a != api]
            if tit is None or outros or tit in outros_tit:
                sobra["escalacao_inseparavel"] += 1
                continue
            for jid in ids:
                cur.execute("SELECT id FROM public.escalacoes WHERE jogo_id = %s AND jogador_id = %s AND titular = %s",
                            (jid, x, tit))
                alvo = cur.fetchall()
                if len(alvo) == 1:
                    cur.execute("UPDATE public.escalacoes SET jogador_id = %s WHERE id = %s", (y, alvo[0][0]))
                    trocas["escalacoes"] += cur.rowcount
                elif alvo:
                    sobra["escalacao_inseparavel"] += 1
    return trocas, ids


# ---------------------------------------------------------------------------
# fases
# ---------------------------------------------------------------------------
def rodar(fase, limite, fixture):
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        for t in ("homonimos_linha", "homonimos_lineup"):
            if not tabela_existe(cur, t):
                raise SystemExit(f"🔴 `{t}` não existe — rode antes `python mapear_homonimos.py mapa`")
        cur.execute("""SELECT MAX(montado_em), COUNT(*) FILTER (WHERE situacao = 'intruso'),
                              COUNT(DISTINCT jogador_id_atual) FILTER (WHERE situacao = 'intruso')
                       FROM public.homonimos_linha""")
        montado, n_intr, j_intr = cur.fetchone()
        print(f"\nmapa: montado em {montado} · {n_intr} linha(s) intruso em {j_intr} jogadores.id")

        antes = contagens(cur)
        md_antes = mesmo_dia(cur)
        lista = fixtures_com_intruso(cur, limite, fixture)
        print(f"jogos a processar: {len(lista)}")
        conn.commit()  # fecha a transação das leituras

        total = {t: 0 for t in TABELAS_VIGIADAS}
        criados, falhas, feitos = [], [], 0
        sobra = {"por_time_sem_time_proprio": 0, "gol_contra": 0,
                 "escalacao_inseparavel": 0, "jogo_sem_perspectiva": 0}
        for i, fx in enumerate(lista, 1):
            cur.execute("SAVEPOINT jogo")
            n_criados = len(criados)
            try:
                persp, _, _ = perspectivas(cur, fx)
                ids = sorted(persp)
                imp_antes = impressao(cur, ids) if ids else None
                trocas, ids = separar_jogo(cur, fx, criados, sobra)
                imp_depois = impressao(cur, ids) if ids else None
                if imp_antes != imp_depois:
                    raise RuntimeError(f"A2: conteúdo do jogo mudou fora do jogador_id: {imp_antes} → {imp_depois}")
                cur.execute("RELEASE SAVEPOINT jogo")
                for t in total:
                    total[t] += trocas[t]
                feitos += 1
                if fase == "aplicar":
                    conn.commit()
            except Exception as e:  # noqa: BLE001 — um jogo ruim não para os outros
                cur.execute("ROLLBACK TO SAVEPOINT jogo")
                del criados[n_criados:]
                falhas.append((fx, str(e)[:200]))
            if i % 200 == 0:
                print(f"   … {i}/{len(lista)}")

        depois = contagens(cur)
        md_depois = mesmo_dia(cur)

        print("\n## LINHAS COM jogador_id TROCADO")
        for t in TABELAS_VIGIADAS:
            print(f"   {t:<28} {total[t]:>7}")
        print(f"\n## JOGADORES CRIADOS: {len(criados)} (ativo = FALSE; o motor_padroes recalcula)")
        for novo, api, nome in criados[:15]:
            print(f"   id {novo:>6}  api {api:>7}  {nome}")
        if len(criados) > 15:
            print(f"   … mais {len(criados) - 15}")

        print("\n## O QUE FICOU NO ID FUNDIDO (de propósito)")
        for k, v in sobra.items():
            print(f"   {k:<28} {v:>7}")

        print("\n## CONFERÊNCIAS")
        iguais = all(antes[t] == depois[t] for t in TABELAS_VIGIADAS)
        print(f"   A2 contagens das 5 tabelas iguais antes/depois: {'✅' if iguais else '🔴'} "
              f"{ {t: (antes[t], depois[t]) for t in TABELAS_VIGIADAS} }")
        print(f"   jogadores: {antes['jogadores']} → {depois['jogadores']} "
              f"({'✅' if depois['jogadores'] - antes['jogadores'] == len(criados) else '🔴'} = criados)")
        print(f"   jogos feitos {feitos} · com falha {len(falhas)} (desfeitos, sem efeito)")
        for fx, err in falhas[:10]:
            print(f"     🔴 {fx}: {err}")
        print(f"   A3 teste do mesmo dia: {md_antes[0]} jogadores.id ({md_antes[1]} pares) → "
              f"{md_depois[0]} ({md_depois[1]} pares)")
        cur.execute("""SELECT jogador_id, nome, api, COUNT(*) FROM (
                           SELECT jeg.jogador_id, jg.nome, jg.api_football_id api, j.data_jogo
                           FROM public.jogador_estatisticas_jogo jeg JOIN public.jogos j ON j.id = jeg.jogo_id
                           JOIN public.jogadores jg ON jg.id = jeg.jogador_id
                           GROUP BY 1, 2, 3, 4 HAVING COUNT(DISTINCT j.fixture_id_api) > 1) t
                       GROUP BY 1, 2, 3 ORDER BY 4 DESC, 1 LIMIT 15""")
        resto = cur.fetchall()
        if resto:
            print("   quem ainda aparece em 2 jogos no mesmo dia (até 15):")
            for jid, nome, api, n in resto:
                print(f"     {jid:>6} {nome:<26} api {api}  {n} dia(s)")

        if fase == "verificar":
            conn.rollback()
            print("\nFASE VERIFICAR — tudo desfeito (ROLLBACK): nenhuma linha trocada, nenhum jogador criado.")
        else:
            conn.commit()
            print(f"\nFASE APLICAR — {feitos} jogo(s) gravados, um a um.")
        if falhas or not iguais:
            sys.exit(1)
    finally:
        conn.rollback()
        conn.close()


def main():
    fase, limite, fixture = ler_argumentos(sys.argv[1:])
    cabecalho(fase, limite, fixture)
    rodar(fase, limite, fixture)


if __name__ == "__main__":
    main()
