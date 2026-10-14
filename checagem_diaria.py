"""
checagem_diaria.py — confere todo dia se o sistema fez o que devia, e grava
o resultado em `public.checagem_diaria`.

Duas fases:

    python checagem_diaria.py verificar   # faz as checagens e imprime; NÃO grava nada
    python checagem_diaria.py aplicar     # faz as checagens, imprime e grava em checagem_diaria

Lugar no cron (`refreshing-freedom`), no FIM, depois do registrar_modelo:
    ... ; python registrar_modelo.py aplicar ; python checagem_diaria.py aplicar

--------------------------------------------------------------------------
O QUE GARANTE
--------------------------------------------------------------------------

  - TODAS as checagens rodam numa conexão em sessão READ ONLY, conferida
    com `SHOW transaction_read_only` (a sessão só-leitura só vale da
    PRÓXIMA transação — `1_ESSENCIAL §6.2`). Nenhuma checagem consegue
    escrever, nem por engano.
  - A única escrita é o `aplicar`, numa SEGUNDA conexão, e só em
    `public.checagem_diaria` (criada aqui se não existir; idempotente).
  - Não muda nada do que o sistema gera: não toca odds, recomendações,
    padrões, histórico, múltiplas, registros nem as ligas.
  - O que dá para importar é importado: o `planejar` e o `SQL_CADEIA` do
    `registrar_publico`, e o `MERCADOS_JOGO_INTEIRO` do `combinacoes`.

Cada checagem sai OK, INFO, ALERTA ou FALHA.
  - FALHA = algo que devia ter acontecido não aconteceu (cron não rodou,
    odds não chegaram, foto do modelo faltando, cadeia quebrada...).
  - ALERTA = estranho, vale olhar (placar atrasado, múltipla presa a mais...).
Se houver alguma FALHA, o script termina com código 1 — o Railway marca a
execução em vermelho. A gravação acontece ANTES disso.

Uma checagem que dá erro (tabela que não existe, consulta que quebra) vira
FALHA com o erro no detalhe, e as outras continuam.

Variáveis de ambiente:
  DATABASE_URL         obrigatória
  CHECAGEM_CASA        casa esperada nas odds (padrão superbet.rs)
  CHECAGEM_VOLUME_GB   tamanho do volume do Postgres (padrão 5)
"""

import os
import sys
from datetime import datetime

import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]
VERSAO_CODIGO = (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "desconhecida")[:12]
CASA_ESPERADA = os.environ.get("CHECAGEM_CASA", "superbet.rs")
VOLUME_GB = float(os.environ.get("CHECAGEM_VOLUME_GB", "5"))

# Limites (horas) — por que cada um:
#   placar: o cron das 12:00 UTC sempre passa por um jogo em até 24h do apito
#     (jogo às 14:59 UTC termina ~17:00 e é lido no cron do dia seguinte).
#   estatística do Brasileirão: /fixtures/statistics atrasa dias (§4) -> 72h.
#   ligas: coleta às 17:00 UTC; jogo que termina depois disso só entra no dia
#     seguinte, e a checagem roda ~15:00 UTC -> 48h placar, 96h estatística.
#   jogo próximo: a foto e as odds são de jogos nas próximas 30h.
HORAS_PLACAR = 24
HORAS_ESTATISTICA = 72
HORAS_PLACAR_LIGA = 48
HORAS_ESTATISTICA_LIGA = 96
HORAS_JOGO_PROXIMO = 30
DIAS_RODADA_FUTURA = 10
HORARIOS_MINIMOS_RODADA = 3   # mesmo teste de cheiro do §5.7
LIMITE_API_DIA = 7000         # LIMITE_REQUISICOES_DIA do popular_banco
MAX_LISTADOS = 8

STATUS_VALIDOS = ("OK", "INFO", "ALERTA", "FALHA")

SQL_TABELA = """
CREATE TABLE IF NOT EXISTS public.checagem_diaria (
    id             bigserial PRIMARY KEY,
    registrado_em  timestamptz NOT NULL DEFAULT now(),
    dia            date        NOT NULL,
    checagem       varchar(40) NOT NULL,
    status         varchar(6)  NOT NULL,
    valor          numeric,
    detalhe        text        NOT NULL,
    versao_codigo  text        NOT NULL,
    CONSTRAINT checagem_diaria_status_ck CHECK (status IN ('OK', 'INFO', 'ALERTA', 'FALHA'))
)
"""
SQL_INDICE = ("CREATE INDEX IF NOT EXISTS checagem_diaria_dia_idx "
              "ON public.checagem_diaria (dia, checagem)")
SQL_INSERIR = """
    INSERT INTO public.checagem_diaria (dia, checagem, status, valor, detalhe, versao_codigo)
    VALUES (%s, %s, %s, %s, %s, %s)
"""

# Relógio único de todas as checagens, em UTC (as colunas de data do banco
# são UTC sem fuso, §5.7). `ultimo_cron_12` = o último cron das 12:00 UTC
# que já deveria ter terminado (margem de 1h30).
SQL_RELOGIO = """
    SELECT (now() AT TIME ZONE 'UTC') AS agora,
           (now() AT TIME ZONE 'UTC')::date AS hoje,
           CASE WHEN (now() AT TIME ZONE 'UTC') >= date_trunc('day', now() AT TIME ZONE 'UTC') + interval '13 hours 30 minutes'
                THEN date_trunc('day', now() AT TIME ZONE 'UTC') + interval '12 hours'
                ELSE date_trunc('day', now() AT TIME ZONE 'UTC') - interval '12 hours' END AS ultimo_cron_12
"""


def cabecalho(fase):
    print("=" * 78)
    print(f"checagem_diaria.py | fase: {fase} | versão do código: {VERSAO_CODIGO} | casa esperada: {CASA_ESPERADA}")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 78)
    sys.stdout.flush()


def um(cur, sql, params=None):
    cur.execute(sql, params)
    return cur.fetchone()


def todos(cur, sql, params=None):
    cur.execute(sql, params)
    return cur.fetchall()


def listar(itens):
    itens = list(itens)
    texto = ", ".join(str(i) for i in itens[:MAX_LISTADOS])
    if len(itens) > MAX_LISTADOS:
        texto += f" (+{len(itens) - MAX_LISTADOS})"
    return texto


def existe(cur, nome):
    return um(cur, "SELECT to_regclass(%s) IS NOT NULL", (nome,))[0]


# ---------------------------------------------------------------------------
# checagens — cada uma recebe (cur, ctx) e devolve uma lista de
# (checagem, status, valor, detalhe). Só leitura.
# ---------------------------------------------------------------------------

def chk_cron_12utc(cur, ctx):
    """O cron `corinthians-analise` (12:00 UTC) rodou? popular_tabela grava
    `jogos_liga.atualizado_em`; motor_padroes grava `padroes_gols_total.atualizado_em`."""
    corte = ctx["ultimo_cron_12"]
    tabela = um(cur, "SELECT MAX(atualizado_em) FROM jogos_liga")[0]
    padroes = um(cur, "SELECT MAX(atualizado_em) FROM padroes_gols_total")[0]
    faltou = []
    if tabela is None or tabela < corte:
        faltou.append(f"popular_tabela (jogos_liga até {tabela})")
    if padroes is None or padroes < corte:
        faltou.append(f"motor_padroes (padroes até {padroes})")
    if faltou:
        return [("cron_12utc", "FALHA", len(faltou),
                 f"sem sinal depois de {corte:%d/%m %H:%M} UTC: " + "; ".join(faltou))]
    return [("cron_12utc", "OK", 0,
             f"jogos_liga {tabela:%d/%m %H:%M} · padrões {padroes:%d/%m %H:%M} UTC")]


def jogos_proximos(cur, ctx):
    """Fixtures do Brasileirão com apito nas próximas HORAS_JOGO_PROXIMO horas."""
    return todos(cur, """
        SELECT DISTINCT fixture_id_api FROM jogos
        WHERE datahora_jogo > %s AND datahora_jogo <= %s + %s * interval '1 hour'
    """, (ctx["agora"], ctx["agora"], HORAS_JOGO_PROXIMO))


def chk_odds(cur, ctx):
    proximos = {r[0] for r in ctx["proximos"]}
    if not proximos:
        return [("odds_hoje", "INFO", 0, f"nenhum jogo nas próximas {HORAS_JOGO_PROXIMO}h — nada a exigir")]
    linhas = todos(cur, """
        SELECT j.fixture_id_api, o.casa_aposta, COUNT(*)
        FROM odds o JOIN jogos j ON j.id = o.jogo_id
        WHERE j.fixture_id_api = ANY(%s) AND o.coletado_em >= %s
        GROUP BY 1, 2
    """, (list(proximos), ctx["hoje"]))
    com_casa = {f for f, casa, _n in linhas if casa == CASA_ESPERADA}
    casas = sorted({casa for _f, casa, _n in linhas})
    total = sum(n for f, casa, n in linhas if casa == CASA_ESPERADA)
    sem = proximos - com_casa
    if not com_casa:
        outras = f" (chegou de outra casa: {listar(casas)})" if casas else ""
        return [("odds_hoje", "FALHA", 0,
                 f"0 de {len(proximos)} jogo(s) próximo(s) com odd da {CASA_ESPERADA} coletada hoje{outras}")]
    if sem:
        return [("odds_hoje", "ALERTA", total,
                 f"{len(com_casa)} de {len(proximos)} jogo(s) com odd da {CASA_ESPERADA} hoje; "
                 f"sem: {listar(sorted(sem))}")]
    return [("odds_hoje", "OK", total, f"{len(proximos)} jogo(s), {total} odd(s) da {CASA_ESPERADA} coletadas hoje")]


def chk_recomendacoes(cur, ctx):
    tot, fixtures = um(cur, """
        SELECT COUNT(*), COUNT(DISTINCT j.fixture_id_api)
        FROM recomendacoes r JOIN jogos j ON j.id = r.jogo_id
        WHERE j.datahora_jogo > %s
    """, (ctx["agora"],))
    jog, times_jog = um(cur, """
        SELECT COUNT(*), COUNT(DISTINCT jg.time_atual_id)
        FROM recomendacoes r JOIN jogos j ON j.id = r.jogo_id
        LEFT JOIN jogadores jg ON jg.id = r.jogador_id
        WHERE j.datahora_jogo > %s AND r.jogador_id IS NOT NULL
    """, (ctx["agora"],))
    detalhe = (f"{tot} viva(s) em {fixtures} jogo(s) futuro(s); de jogador: {jog}, "
               f"de {times_jog} time(s) (time_atual_id)")
    if ctx["proximos"] and tot == 0:
        return [("recomendacoes_vivas", "ALERTA", 0, f"há jogo nas próximas {HORAS_JOGO_PROXIMO}h e {detalhe}")]
    return [("recomendacoes_vivas", "INFO", tot, detalhe)]


def chk_registro_modelo(cur, ctx):
    proximos = {r[0] for r in ctx["proximos"]}
    if not proximos:
        return [("registro_modelo_foto", "INFO", 0, f"nenhum jogo nas próximas {HORAS_JOGO_PROXIMO}h")]
    if not existe(cur, "public.registro_modelo"):
        return [("registro_modelo_foto", "FALHA", 0, "public.registro_modelo não existe")]
    linhas = todos(cur, """
        SELECT fixture_id_api, COUNT(*) FROM public.registro_modelo
        WHERE fixture_id_api = ANY(%s) AND registrado_em >= (%s::timestamp AT TIME ZONE 'UTC')
        GROUP BY 1
    """, (list(proximos), ctx["hoje"]))
    com = {f for f, _n in linhas}
    total = sum(n for _f, n in linhas)
    sem = proximos - com
    if not com:
        return [("registro_modelo_foto", "FALHA", 0, f"nenhuma foto hoje para os {len(proximos)} jogo(s) próximo(s)")]
    if sem:
        return [("registro_modelo_foto", "ALERTA", total,
                 f"foto hoje em {len(com)} de {len(proximos)} jogo(s); sem foto: {listar(sorted(sem))}")]
    return [("registro_modelo_foto", "OK", total, f"foto hoje nos {len(proximos)} jogo(s) próximo(s), {total} linha(s)")]


def chk_registro_publico(cur, ctx):
    if not existe(cur, "public.registro_publico"):
        return [("registro_publico", "FALHA", 0, "public.registro_publico não existe")]
    import registrar_publico as rp
    saida = []
    vivas, novas, mudadas, retiradas, _sem = rp.planejar(cur)
    pend = len(novas) + len(mudadas) + len(retiradas)
    if pend:
        saida.append(("registro_publico", "ALERTA", pend,
                      f"{pend} evento(s) que o registrar_publico ainda gravaria "
                      f"({len(novas)} novas, {len(mudadas)} mudadas, {len(retiradas)} retiradas) — ele rodou?"))
    else:
        saida.append(("registro_publico", "OK", len(vivas), f"em dia com as {len(vivas)} recomendação(ões) viva(s)"))
    total, quebradas, primeira, ultimo = um(cur, rp.SQL_CADEIA)
    if quebradas:
        saida.append(("registro_publico_cadeia", "FALHA", quebradas,
                      f"{quebradas} de {total} linha(s) não conferem; primeira: id {primeira}"))
    else:
        saida.append(("registro_publico_cadeia", "OK", total, f"cadeia íntegra, {total} linha(s)"))
    return saida


def chk_placar(cur, ctx):
    linhas = todos(cur, """
        SELECT DISTINCT j.fixture_id_api, COALESCE(jl.status, '?')
        FROM jogos j LEFT JOIN jogos_liga jl ON jl.fixture_id_api = j.fixture_id_api
        WHERE j.data_jogo >= DATE '2026-01-01'
          AND j.datahora_jogo < %s - %s * interval '1 hour'
          AND j.placar_corinthians IS NULL
        ORDER BY 1
    """, (ctx["agora"], HORAS_PLACAR))
    if linhas:
        return [("placar_atrasado", "ALERTA", len(linhas),
                 f"jogo(s) com apito há mais de {HORAS_PLACAR}h sem placar (fixture:status): "
                 + listar(f"{f}:{s}" for f, s in linhas))]
    return [("placar_atrasado", "OK", 0, f"todo jogo de 2026 com mais de {HORAS_PLACAR}h tem placar")]


def chk_estatistica(cur, ctx):
    linhas = todos(cur, """
        SELECT DISTINCT j.fixture_id_api
        FROM jogos j
        WHERE j.data_jogo >= DATE '2026-01-01'
          AND j.datahora_jogo < %s - %s * interval '1 hour'
          AND j.placar_corinthians IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM estatisticas_jogo e WHERE e.jogo_id = j.id)
        ORDER BY 1
    """, (ctx["agora"], HORAS_ESTATISTICA))
    if linhas:
        return [("estatistica_atrasada", "ALERTA", len(linhas),
                 f"jogo(s) com placar e sem estatística depois de {HORAS_ESTATISTICA}h: "
                 + listar(r[0] for r in linhas))]
    return [("estatistica_atrasada", "OK", 0, f"todo jogo de 2026 com mais de {HORAS_ESTATISTICA}h tem estatística")]


def chk_arquivamento(cur, ctx):
    """O arquivar_recomendacoes apaga de `recomendacoes` o que arquiva. Jogo que
    começou antes do último cron das 12:00 UTC não devia ter recomendação lá."""
    linhas = todos(cur, """
        SELECT j.fixture_id_api, COUNT(*)
        FROM recomendacoes r JOIN jogos j ON j.id = r.jogo_id
        WHERE j.datahora_jogo < %s
        GROUP BY 1 ORDER BY 1
    """, (ctx["ultimo_cron_12"],))
    total = sum(n for _f, n in linhas)
    if linhas:
        return [("arquivamento", "ALERTA", total,
                 f"{total} recomendação(ões) de {len(linhas)} jogo(s) que já passaram e não foram arquivadas: "
                 + listar(f for f, _n in linhas))]
    return [("arquivamento", "OK", 0, "nada de jogo passado esperando arquivamento")]


def chk_duplicados(cur, ctx):
    n = um(cur, """
        SELECT COUNT(*) FROM (
            SELECT 1 FROM historico_recomendacoes
            GROUP BY jogo_id, tipo_padrao, jogador_id, linha, direcao
            HAVING COUNT(*) > 1) d
    """)[0]
    if n:
        return [("historico_duplicados", "ALERTA", n, f"{n} grupo(s) duplicado(s) no histórico (§5.12)")]
    return [("historico_duplicados", "OK", 0, "0 grupos duplicados no histórico")]


def chk_pares(cur, ctx):
    from combinacoes import MERCADOS_JOGO_INTEIRO
    linhas = todos(cur, """
        SELECT j.fixture_id_api, r.tipo_padrao, r.linha
        FROM recomendacoes r JOIN jogos j ON j.id = r.jogo_id
        WHERE j.datahora_jogo > %s AND r.tipo_padrao = ANY(%s)
          AND lower(trim(r.direcao)) IN ('mais', 'menos', 'sim', 'não', 'nao')
        GROUP BY 1, 2, 3
        HAVING COUNT(DISTINCT CASE WHEN lower(trim(r.direcao)) IN ('mais', 'sim') THEN 1 ELSE 2 END) = 2
    """, (ctx["agora"], sorted(MERCADOS_JOGO_INTEIRO)))
    return [("pares_contraditorios", "INFO", len(linhas),
             f"{len(linhas)} aposta(s) de jogo inteiro recomendada(s) nos DOIS lados (defeito conhecido, §5.16)"
             + (": " + listar(f"{f}/{t}/{l}" for f, t, l in linhas) if linhas else ""))]


def chk_presas(cur, ctx):
    n, mais_nova = um(cur, """
        SELECT COUNT(*), MAX(primeiro_apito) FROM multiplas_candidatas
        WHERE congelada = TRUE AND avaliada = FALSE
    """)
    anterior = ctx.get("presas_anterior")
    detalhe = f"{n} múltipla(s) presa(s), apito mais recente {mais_nova}"
    if anterior is None:
        return [("multiplas_presas", "INFO", n, detalhe + " (sem medição anterior para comparar)")]
    valor_ant, dia_ant = anterior
    if n > valor_ant:
        return [("multiplas_presas", "ALERTA", n, detalhe + f" — eram {int(valor_ant)} em {dia_ant:%d/%m} (§5.18)")]
    return [("multiplas_presas", "INFO", n, detalhe + f" — eram {int(valor_ant)} em {dia_ant:%d/%m}")]


def chk_data_divergente(cur, ctx):
    linhas = todos(cur, """
        SELECT DISTINCT j.fixture_id_api
        FROM jogos j JOIN jogos_liga jl ON jl.fixture_id_api = j.fixture_id_api
        WHERE jl.data_jogo >= %s AND j.data_jogo <> jl.data_jogo
        ORDER BY 1
    """, (ctx["hoje"],))
    if linhas:
        return [("data_jogos_x_tabela", "ALERTA", len(linhas),
                 "jogo(s) futuro(s) com data diferente em jogos e jogos_liga: " + listar(r[0] for r in linhas))]
    return [("data_jogos_x_tabela", "OK", 0, "jogos e jogos_liga com a mesma data em todo jogo futuro")]


def chk_rodada_carimbada(cur, ctx):
    """Teste de cheiro (§5.7): rodada próxima com 5+ jogos e menos de 3 horários
    distintos ainda tem data provisória."""
    linhas = todos(cur, """
        SELECT jl.rodada_numero, COUNT(DISTINCT jl.fixture_id_api), COUNT(DISTINCT j.datahora_jogo)
        FROM jogos_liga jl JOIN jogos j ON j.fixture_id_api = jl.fixture_id_api
        WHERE jl.status = 'NS' AND jl.data_jogo BETWEEN %s AND %s + %s
        GROUP BY 1 ORDER BY 1
    """, (ctx["hoje"], ctx["hoje"], DIAS_RODADA_FUTURA))
    carimbadas = [(r, f, h) for r, f, h in linhas if f >= 5 and h < HORARIOS_MINIMOS_RODADA]
    resumo = listar(f"{r}:{h}h/{f}j" for r, f, h in linhas) or "nenhuma"
    if carimbadas:
        return [("rodada_carimbada", "ALERTA", len(carimbadas),
                 "rodada(s) dos próximos dias com data provisória: "
                 + listar(f"{r} ({h} horário(s) em {f} jogos)" for r, f, h in carimbadas))]
    return [("rodada_carimbada", "OK", 0, f"rodadas nos próximos {DIAS_RODADA_FUTURA} dias (horários/jogos): {resumo}")]


def chk_ligas(cur, ctx):
    schemas = [r[0] for r in todos(cur, """
        SELECT nspname FROM pg_namespace WHERE nspname LIKE 'liga\\_%' ORDER BY 1
    """)]
    if not schemas:
        return [("ligas_em_dia", "INFO", 0, "nenhum schema liga_* encontrado")]
    sem_placar, sem_estat, resumo = [], [], []
    for s in schemas:
        temporada = um(cur, f"SELECT MAX(temporada) FROM {s}.jogos")[0]
        a, b, ultimo = um(cur, f"""
            SELECT
              COUNT(DISTINCT j.fixture_id_api) FILTER (
                  WHERE j.placar_corinthians IS NULL AND j.datahora_jogo < %s - %s * interval '1 hour'),
              COUNT(DISTINCT j.fixture_id_api) FILTER (
                  WHERE j.placar_corinthians IS NOT NULL AND j.datahora_jogo < %s - %s * interval '1 hour'
                    AND NOT EXISTS (SELECT 1 FROM {s}.estatisticas_jogo e WHERE e.jogo_id = j.id)),
              MAX(j.datahora_jogo) FILTER (WHERE j.placar_corinthians IS NOT NULL)
            FROM {s}.jogos j WHERE j.temporada = %s
        """, (ctx["agora"], HORAS_PLACAR_LIGA, ctx["agora"], HORAS_ESTATISTICA_LIGA, temporada))
        nome = s[len("liga_"):]
        if a:
            sem_placar.append(f"{nome} {a}")
        if b:
            sem_estat.append(f"{nome} {b}")
        resumo.append(f"{nome} {ultimo:%d/%m}" if ultimo else f"{nome} —")
    saida = []
    if sem_placar:
        saida.append(("ligas_placar", "ALERTA", len(sem_placar),
                      f"jogo(s) da temporada atual sem placar {HORAS_PLACAR_LIGA}h depois do apito "
                      f"(adiado também aparece aqui): " + ", ".join(sem_placar)))
    else:
        saida.append(("ligas_placar", "OK", 0, "último jogo com placar: " + ", ".join(resumo)))
    if sem_estat:
        saida.append(("ligas_estatistica", "ALERTA", len(sem_estat),
                      f"jogo(s) com placar e sem estatística depois de {HORAS_ESTATISTICA_LIGA}h: "
                      + ", ".join(sem_estat)))
    else:
        saida.append(("ligas_estatistica", "OK", 0, f"temporada atual completa nas {len(schemas)} liga(s)"))
    return saida


def chk_banco(cur, ctx):
    tamanho = um(cur, "SELECT pg_database_size(current_database())")[0]
    gb = tamanho / 1024 ** 3
    pct = 100 * gb / VOLUME_GB
    detalhe = (f"banco {tamanho / 1024 ** 2:.0f} MB = {pct:.0f}% do volume de {VOLUME_GB:g} GB "
               f"(o volume também guarda o WAL; olhar o Railway se passar de 50%)")
    status = "FALHA" if pct >= 75 else "ALERTA" if pct >= 50 else "OK"
    return [("banco_tamanho", status, round(pct, 1), detalhe)]


def chk_api(cur, ctx):
    linha = um(cur, "SELECT requisicoes FROM controle_api_uso WHERE dia = %s", (ctx["hoje"],))
    n = linha[0] if linha else 0
    status = "ALERTA" if n >= 0.85 * LIMITE_API_DIA else "INFO"
    return [("api_football_hoje", status, n, f"{n} de {LIMITE_API_DIA} requisições hoje (UTC) até agora")]


CHECAGENS = [
    chk_cron_12utc, chk_odds, chk_recomendacoes, chk_registro_modelo, chk_registro_publico,
    chk_placar, chk_estatistica, chk_arquivamento, chk_duplicados, chk_pares, chk_presas,
    chk_data_divergente, chk_rodada_carimbada, chk_ligas, chk_banco, chk_api,
]


# ---------------------------------------------------------------------------
# execução
# ---------------------------------------------------------------------------

def conectar_leitura():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    conn.commit()  # só vale da PRÓXIMA transação (§6.2)
    cur.execute("SHOW transaction_read_only")
    if cur.fetchone()[0] != "on":
        conn.close()
        raise RuntimeError("a sessão de leitura NÃO ficou só-leitura — abortado sem checar nada")
    return conn, cur


def presas_anterior(cur, hoje):
    if not existe(cur, "public.checagem_diaria"):
        return None
    return um(cur, """
        SELECT valor, dia FROM public.checagem_diaria
        WHERE checagem = 'multiplas_presas' AND dia < %s
        ORDER BY dia DESC, id DESC LIMIT 1
    """, (hoje,))


def rodar_checagens(conn, cur):
    agora, hoje, ultimo_cron_12 = um(cur, SQL_RELOGIO)
    ctx = {"agora": agora, "hoje": hoje, "ultimo_cron_12": ultimo_cron_12}
    ctx["presas_anterior"] = presas_anterior(cur, hoje)
    ctx["proximos"] = jogos_proximos(cur, ctx)
    conn.rollback()
    resultados = []
    for funcao in CHECAGENS:
        nome = funcao.__name__[len("chk_"):]
        try:
            for item in funcao(cur, ctx):
                assert item[1] in STATUS_VALIDOS, item
                resultados.append(item)
        except Exception as erro:  # uma checagem quebrada não derruba as outras
            resultados.append((nome, "FALHA", None, f"a checagem quebrou: {type(erro).__name__}: {erro}"[:900]))
        conn.rollback()  # leitura pura; também limpa transação abortada
    return hoje, resultados


def imprimir(resultados):
    icone = {"OK": "✅", "INFO": "·", "ALERTA": "⚠️", "FALHA": "🔴"}
    print()
    for checagem, status, _valor, detalhe in resultados:
        print(f"{icone[status]} {status:<6} {checagem:<24} {detalhe}")
    contagem = {s: sum(1 for r in resultados if r[1] == s) for s in STATUS_VALIDOS}
    print("\n" + " · ".join(f"{s}: {n}" for s, n in contagem.items()))
    return contagem


def gravar(hoje, resultados):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    try:
        cur.execute(SQL_TABELA)
        cur.execute(SQL_INDICE)
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'claude_leitura'")
        if cur.fetchone():
            cur.execute("GRANT SELECT ON public.checagem_diaria TO claude_leitura")
        for checagem, status, valor, detalhe in resultados:
            cur.execute(SQL_INSERIR, (hoje, checagem, status, valor, detalhe, VERSAO_CODIGO))
        conn.commit()
        print(f"\ngravadas {len(resultados)} linha(s) em public.checagem_diaria (dia {hoje}). "
              f"Nenhuma outra tabela foi tocada.")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("verificar", "aplicar"):
        print(__doc__)
        sys.exit(1)
    fase = sys.argv[1]
    cabecalho(fase)

    conn, cur = conectar_leitura()
    try:
        hoje, resultados = rodar_checagens(conn, cur)
    finally:
        conn.rollback()  # incondicional: a conexão de leitura nunca grava
        cur.close()
        conn.close()

    contagem = imprimir(resultados)

    if fase == "aplicar":
        gravar(hoje, resultados)
    else:
        print("\n" + "=" * 78)
        print("FASE VERIFICAR — `checagem_diaria` NÃO foi tocada; nenhuma outra tabela também.")
        print("=" * 78)

    if contagem["FALHA"]:
        print(f"\n🔴 {contagem['FALHA']} FALHA(s) — saindo com código 1 para o Railway marcar a execução.")
        sys.exit(1)


if __name__ == "__main__":
    main()
