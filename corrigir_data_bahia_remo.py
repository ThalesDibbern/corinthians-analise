"""
Corrige a data do jogo Bahia x Remo (fixture 1492371) da rodada 27.

MOTIVO (13/09/2026):
A CBF transferiu o confronto de domingo (13/09) para segunda (14/09), às
20h, na Arena Fonte Nova - confirmado em fonte externa. O registro em
`jogos` foi criado quando a partida ainda estava marcada pra 13/09, e o
`popular_banco` de 13/09 detectou a divergência mas NÃO alterou a data
automaticamente, de propósito (não dá pra saber por código qual fonte
está certa). Log daquela execução:

    Jogo encontrado: Bahia x Clube do Remo PA em 2026-09-14
    ⚠️  Jogo #1492371 já existe com data 2026-09-13, mas essa chamada
        calculou 2026-09-14 pro mesmo confronto - reaproveitando o jogo
        #1492371 SEM criar duplicata. Data NÃO alterada automaticamente.

Neste caso a fonte externa desempatou: a API estava CERTA, o banco está
ERRADO.

POR QUE ISSO É URGENTE - `arquivar_recomendacoes.buscar_recomendacoes_para_arquivar`
seleciona com OR, não AND `[código, 13/09]`:

    WHERE (j.datahora_jogo IS NOT NULL AND j.datahora_jogo < NOW())
       OR (j.datahora_jogo IS NULL AND j.data_jogo < CURRENT_DATE)
       OR jl.status IN ('FT', 'AET', 'PEN')

Com a data gravada em 13/09, o primeiro ramo fica VERDADEIRO já na
segunda de manhã. O cron `corinthians-analise` roda 12:00 UTC (09:00 BRT)
- **onze horas ANTES do apito** - e arquivaria as 30 recomendações do
jogo avaliando contra um jogo que ainda não aconteceu.

⚠️ O estrago é ASSIMÉTRICO. Mercado que depende de estatística volta
como `pendente` e `reavaliar_pendentes_ja_arquivadas` conserta sozinho
na execução seguinte. Mas mercado avaliado contra PLACAR pode receber um
veredito definitivo errado (`errou`), e `historico_recomendacoes`
CONGELA resultado - `resolver_pendentes` só reavalia linha `pendente`,
nunca linha já decidida. Essa metade não se cura sozinha: precisaria do
script de reavaliação, depois de alguém perceber.

Efeito colateral do mesmo erro: `multiplas_candidatas.primeiro_apito`
vem de `datahora_jogo`, então `congelar_candidatas_vencidas` congelaria
as candidatas deste jogo no instante em que fossem capturadas.

ESCOPO: só a tabela `jogos`. Um confronto entre dois times rastreados
tem DUAS linhas (uma por perspectiva) com o MESMO `fixture_id_api` - o
script corrige as duas, e o `UPDATE` é por `fixture_id_api` justamente
por isso. `recomendacoes` não guarda data. `historico_recomendacoes`
guarda, mas nada deste jogo foi arquivado ainda - a fase `verificar`
confere isso e AVISA se encontrar linha arquivada, porque aí o escopo
muda e este script não basta.

USO:
    python corrigir_data_bahia_remo.py verificar
    python corrigir_data_bahia_remo.py aplicar
"""

import os
import sys
from datetime import datetime, timezone

import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

FIXTURE_ID = 1492371

# 14/09/2026, 20h BRT (Arena Fonte Nova) = 23:00 UTC do mesmo dia.
# `datahora_jogo` é gravado em UTC em todo o projeto (ver convenção 5.7).
DATA_CORRETA = "2026-09-14"
DATAHORA_CORRETA_UTC = "2026-09-14 23:00:00"


def cabecalho(fase):
    print("=" * 70)
    print(f"  corrigir_data_bahia_remo.py")
    print(f"  fase .... {fase}")
    print(f"  argv .... {sys.argv}")
    print(f"  relogio . {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
    print(f"  alvo .... fixture {FIXTURE_ID} -> {DATAHORA_CORRETA_UTC} UTC")
    print("=" * 70)
    print()


def mostrar_estado(cur):
    print("--- jogos (as duas perspectivas) ---")
    cur.execute(
        """
        SELECT j.id, t.nome AS nosso_time, j.adversario, j.data_jogo,
               j.datahora_jogo, j.rodada_numero,
               (SELECT COUNT(*) FROM recomendacoes r WHERE r.jogo_id = j.id) AS recomendacoes
        FROM jogos j
        LEFT JOIN times t ON t.id = j.nosso_time_id
        WHERE j.fixture_id_api = %s
        ORDER BY j.id
        """,
        (FIXTURE_ID,),
    )
    linhas = cur.fetchall()
    if not linhas:
        print(f"  !! nenhuma linha com fixture_id_api = {FIXTURE_ID}")
        return 0, 0
    total_recs = 0
    for jid, nosso, adv, data, datahora, rodada, recs in linhas:
        total_recs += recs or 0
        print(f"  jogo #{jid:<8} {nosso} x {adv}")
        print(f"      data_jogo ..... {data}")
        print(f"      datahora_jogo . {datahora}  (UTC)")
        print(f"      rodada ........ {rodada}")
        print(f"      recomendações . {recs}")
    print(f"\n  linhas em `jogos`: {len(linhas)}   recomendações ativas: {total_recs}")

    print("\n--- jogos_liga (fonte da API-Football) ---")
    cur.execute(
        """
        SELECT fixture_id_api, mandante_nome, visitante_nome, data_jogo, status, rodada_numero
        FROM jogos_liga
        WHERE fixture_id_api = %s
        """,
        (FIXTURE_ID,),
    )
    liga = cur.fetchall()
    if not liga:
        print("  (nenhuma linha - o arquivamento por `status` não tem o que ler)")
    for fx, mand, vis, data, status, rodada in liga:
        print(f"  {mand} x {vis} | data {data} | status {status!r} | rodada {rodada}")

    print("\n--- já foi arquivado algo deste jogo? ---")
    cur.execute(
        """
        SELECT hr.resultado, COUNT(*)
        FROM historico_recomendacoes hr
        JOIN jogos j ON j.id = hr.jogo_id
        WHERE j.fixture_id_api = %s
        GROUP BY 1 ORDER BY 1
        """,
        (FIXTURE_ID,),
    )
    arquivadas = cur.fetchall()
    if not arquivadas:
        print("  nada arquivado ✅ - corrigir `jogos` é suficiente")
    else:
        print("  ⚠️  JÁ TEM LINHA ARQUIVADA - o escopo deste script NÃO basta:")
        for res, n in arquivadas:
            print(f"      {res}: {n}")
        print("      Linha `errou`/`acertou` está CONGELADA e precisa do script")
        print("      de reavaliação. Linha `pendente` se cura sozinha.")

    print("\n--- candidatas de múltipla deste jogo ---")
    cur.execute(
        """
        SELECT COUNT(*), COUNT(*) FILTER (WHERE congelada), COUNT(*) FILTER (WHERE avaliada)
        FROM multiplas_candidatas mc
        WHERE mc.jogos::text LIKE %s
        """,
        (f'%"fixture_id_api": {FIXTURE_ID}%',),
    )
    total, congeladas, avaliadas = cur.fetchone()
    print(f"  total {total} | congeladas {congeladas} | avaliadas {avaliadas}")

    return len(linhas), len(arquivadas)


def verificar():
    cabecalho("verificar")
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    try:
        n_jogos, n_arquivadas = mostrar_estado(cur)
        print()
        print("=" * 70)
        if n_jogos == 0:
            print("  NADA A FAZER - fixture não encontrado.")
        elif n_arquivadas:
            print("  NÃO RODAR `aplicar` AINDA - já há linha arquivada.")
            print("  Corrigir a data sem tratar o histórico deixa os dois divergentes.")
        else:
            print(f"  PRONTO PRA APLICAR - vai reescrever {n_jogos} linha(s):")
            print(f"     data_jogo     -> {DATA_CORRETA}")
            print(f"     datahora_jogo -> {DATAHORA_CORRETA_UTC} UTC (20h BRT)")
        print("=" * 70)
    finally:
        cur.close()
        conn.close()


def aplicar():
    cabecalho("aplicar")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        print(">>> ANTES\n")
        _, n_arquivadas = mostrar_estado(cur)

        if n_arquivadas:
            print("\n!! ABORTADO - há linha em historico_recomendacoes pra este jogo.")
            print("   Rode `verificar` e trate o histórico antes.")
            conn.rollback()
            return

        cur.execute(
            """
            UPDATE jogos
               SET data_jogo = %s::date,
                   datahora_jogo = %s::timestamp
             WHERE fixture_id_api = %s
            """,
            (DATA_CORRETA, DATAHORA_CORRETA_UTC, FIXTURE_ID),
        )
        alteradas = cur.rowcount
        print(f"\n>>> UPDATE aplicado em {alteradas} linha(s) de `jogos`")

        conn.commit()

        print("\n>>> DEPOIS\n")
        mostrar_estado(cur)

        print("\n" + "=" * 70)
        print("  OK. O jogo agora está no FUTURO, então:")
        print("   - arquivar_recomendacoes NÃO vai varrer as 30 recomendações")
        print("     no cron de segunda 12:00 UTC")
        print("   - motor_combinacoes captura as candidatas com primeiro_apito")
        print("     no futuro, sem congelar na hora")
        print("   - o arquivamento correto acontece no cron de terça")
        print("=" * 70)

    except Exception as e:
        conn.rollback()
        print(f"\nErro: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    fase = sys.argv[1] if len(sys.argv) > 1 else ""
    if fase == "verificar":
        verificar()
    elif fase == "aplicar":
        aplicar()
    else:
        print("uso: python corrigir_data_bahia_remo.py [verificar|aplicar]")
        sys.exit(1)
