"""
Auditoria do PÊNALTI PERDIDO no histórico inteiro - fase `verificar`.

O BUG: até 01/09/2026, `salvar_eventos()` em popular_banco.py inseria uma
linha em `gols` pra QUALQUER evento com `type == "Goal"`, sem olhar o
`detail`. A API-Football usa `type="Goal"` também pra registrar pênalti
PERDIDO (`detail="Missed Penalty"`) - que não é gol. Resultado: linha
fantasma em `gols` toda vez que um jogo teve pênalti perdido.

Descoberto ao investigar Mirassol x Palmeiras (30/08/2026): a API
devolvia 3 eventos "Goal" pro placar real de 1x1. O terceiro era o
pênalti anulado do Fernandinho aos 83'.

POR QUE ISSO IMPORTA: `jogos.placar_*` vem de `fixture["goals"]` da API,
então o PLACAR exibido está certo. Mas qualquer análise que conta linhas
de `gols` diretamente é contaminada - Marca em Ambos os Tempos, Dupla
Chance por tempo, gols por período, e os padrões derivados disso.

POR QUE PRECISA REBUSCAR A API: não dá pra achar essas linhas só olhando
o banco. Um pênalti perdido gravado por engano fica com `penalti=False` e
`gol_contra=False` - idêntico a um gol normal. A ÚNICA forma de
identificar é comparar a contagem do banco contra a resposta atual de
/fixtures/events.

CUSTO: 1 requisição por fixture finalizado. Com ~1350 jogos reais e
sleep de 7s (mesmo intervalo do resto do projeto), roda em ~2h40. Bem
dentro da cota diária da API-Football (7.500/dia), o problema é só a
duração.

ESTA FASE NÃO GRAVA NADA. Só lê, compara e imprime a lista de fixtures
suspeitos. Pode ser interrompida e rodada de novo do zero sem risco.

Retomada: o progresso é impresso a cada fixture. Se cair no meio, use
--desde <fixture_id> pra continuar de onde parou sem refazer tudo.

Uso:
  python auditar_penalti_perdido.py verificar
  python auditar_penalti_perdido.py verificar --desde 1180567
  python auditar_penalti_perdido.py verificar --temporada 2026
"""

import os
import sys
import time
from datetime import datetime

import requests
import psycopg2

API_KEY = os.environ["API_FOOTBALL_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

SEGUNDOS_ENTRE_REQUISICOES = 7
ARQUIVO_SAIDA = "penaltis_perdidos_encontrados.txt"


def cabecalho(fase, extra=""):
    print("=" * 78)
    print(f"auditar_penalti_perdido.py | fase: {fase} {extra}")
    print(f"argv: {sys.argv}")
    print(f"relógio: {datetime.now().isoformat()}")
    print("=" * 78)
    sys.stdout.flush()


def buscar_eventos(fixture_id):
    resp = requests.get(
        f"{API_BASE}/fixtures/events",
        headers=HEADERS,
        params={"fixture": fixture_id},
    )
    resp.raise_for_status()
    dados = resp.json()
    if dados.get("errors"):
        print(f"  !! aviso da API no fixture {fixture_id}: {dados['errors']}")
    return dados["response"]


def buscar_fixtures_para_auditar(cur, desde=None, temporada=None):
    """Um fixture real por linha (não uma por perspectiva) - agrupa por
    fixture_id_api e pega o menor jogo_id como representante. Só fixture
    POSITIVO: os sintéticos negativos não existem na API-Football."""
    sql = """
        SELECT j.fixture_id_api,
               MIN(j.id) AS jogo_id_representante,
               MIN(j.data_jogo) AS data_jogo,
               COUNT(DISTINCT j.id) AS n_perspectivas
        FROM jogos j
        WHERE j.fixture_id_api > 0
    """
    params = []
    if desde is not None:
        sql += " AND j.fixture_id_api >= %s"
        params.append(desde)
    if temporada is not None:
        sql += " AND EXTRACT(YEAR FROM j.data_jogo) = %s"
        params.append(temporada)
    sql += " GROUP BY j.fixture_id_api ORDER BY j.fixture_id_api"

    cur.execute(sql, params)
    return cur.fetchall()


def contar_gols_no_banco(cur, fixture_id_api):
    """Conta linhas de `gols` por perspectiva. Cada perspectiva tem sua
    própria cópia dos eventos, então conta separado e devolve um dict."""
    cur.execute(
        """SELECT g.jogo_id, COUNT(*)
           FROM gols g
           JOIN jogos j ON j.id = g.jogo_id
           WHERE j.fixture_id_api = %s
           GROUP BY g.jogo_id""",
        (fixture_id_api,),
    )
    return dict(cur.fetchall())


def fase_verificar(desde=None, temporada=None):
    filtro = ""
    if desde is not None:
        filtro += f"(desde fixture {desde}) "
    if temporada is not None:
        filtro += f"(temporada {temporada}) "
    cabecalho("verificar (só leitura, nenhuma escrita)", filtro)

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    fixtures = buscar_fixtures_para_auditar(cur, desde, temporada)
    total = len(fixtures)
    minutos = total * SEGUNDOS_ENTRE_REQUISICOES / 60
    print(f"\n{total} fixtures reais pra auditar.")
    print(f"Tempo estimado: ~{minutos:.0f} minutos ({minutos/60:.1f}h) "
          f"a {SEGUNDOS_ENTRE_REQUISICOES}s por requisição.\n")
    sys.stdout.flush()

    suspeitos = []
    sem_penalti = 0
    erros = 0

    for indice, (fixture_id, jogo_id_rep, data_jogo, n_perspectivas) in enumerate(fixtures, start=1):
        try:
            eventos = buscar_eventos(fixture_id)
        except Exception as e:
            erros += 1
            print(f"[{indice}/{total}] fixture={fixture_id} !! ERRO na API: {e}")
            sys.stdout.flush()
            time.sleep(SEGUNDOS_ENTRE_REQUISICOES)
            continue

        perdidos = [
            ev for ev in eventos
            if ev.get("type") == "Goal" and ev.get("detail") == "Missed Penalty"
        ]

        if not perdidos:
            sem_penalti += 1
            if indice % 25 == 0:
                print(f"[{indice}/{total}] ... {len(suspeitos)} suspeitos até aqui")
                sys.stdout.flush()
            time.sleep(SEGUNDOS_ENTRE_REQUISICOES)
            continue

        # Gols REAIS segundo a API (mesma regra que salvar_eventos usa hoje,
        # já corrigida): exclui pênalti perdido E evento sem jogador.
        gols_reais_api = sum(
            1 for ev in eventos
            if ev.get("type") == "Goal"
            and ev.get("detail") != "Missed Penalty"
            and (ev.get("player") or {}).get("name")
        )
        gols_no_banco = contar_gols_no_banco(cur, fixture_id)

        contaminado = any(qtd > gols_reais_api for qtd in gols_no_banco.values())
        marca = "CONTAMINADO" if contaminado else "já está certo"

        detalhe_perdidos = "; ".join(
            f"{(ev.get('player') or {}).get('name')} {(ev.get('time') or {}).get('elapsed')}'"
            for ev in perdidos
        )
        print(f"[{indice}/{total}] fixture={fixture_id} data={data_jogo} "
              f"| {len(perdidos)} pênalti(s) perdido(s): {detalhe_perdidos} "
              f"| API diz {gols_reais_api} gols, banco tem {gols_no_banco} -> {marca}")
        sys.stdout.flush()

        if contaminado:
            suspeitos.append(fixture_id)

        time.sleep(SEGUNDOS_ENTRE_REQUISICOES)

    print("\n" + "=" * 78)
    print(f"RESUMO: {total} fixtures auditados.")
    print(f"  {sem_penalti} sem nenhum pênalti perdido (nada a fazer)")
    print(f"  {len(suspeitos)} CONTAMINADOS (têm linha fantasma em `gols`)")
    print(f"  {erros} falharam na chamada da API (rodar de novo com --desde pra tentar)")
    print("=" * 78)

    if suspeitos:
        with open(ARQUIVO_SAIDA, "w") as f:
            f.write(",".join(str(fid) for fid in suspeitos))
        print(f"\nLista dos contaminados salva em {ARQUIVO_SAIDA}:")
        print(",".join(str(fid) for fid in suspeitos))
        print("\n⚠️ O Railway apaga o filesystem entre execuções - COPIE essa lista")
        print("   do log antes de rodar a fase `aplicar`.")
    else:
        print("\nNenhum fixture contaminado encontrado. Nada a corrigir.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "verificar":
        print("Uso: python auditar_penalti_perdido.py verificar [--desde FIXTURE_ID] [--temporada ANO]")
        sys.exit(1)

    desde = None
    temporada = None
    for i, arg in enumerate(sys.argv):
        if arg == "--desde" and i + 1 < len(sys.argv):
            desde = int(sys.argv[i + 1])
        if arg == "--temporada" and i + 1 < len(sys.argv):
            temporada = int(sys.argv[i + 1])

    fase_verificar(desde, temporada)
