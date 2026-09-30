"""
Sincroniza a DATA dos jogos com a API-Football. Duas fases (verificar / aplicar).

POR QUE ESTE SCRIPT EXISTE
==========================

A data de um jogo é WRITE-ONCE no sistema hoje. Três arquivos escrevem, nenhum
corrige `[código, 16/09/2026]`:

  - `atualizar_odds.py` cria a linha de `jogos` antes do jogo e, quando acha
    divergência, avisa e NÃO altera. Isso está certo: a OddsPapi é a fonte
    fraca e ele não tem como decidir.
  - `popular_banco.get_or_create_jogo` faz `if datahora_salva is None:` — é
    BACKFILL, não sincronização. E `data_jogo` não é atualizada em lugar
    nenhum depois do INSERT.
  - `popular_tabela.salvar_jogo_liga` tem `ON CONFLICT DO UPDATE SET` com
    rodada, rodada_numero, placar_*, status e atualizado_em — `data_jogo`
    ficou FORA da lista.

Consequência: a API-Football entrega uma data PROVISÓRIA para rodada que a CBF
ainda não detalhou (um carimbo único para a rodada inteira), refina conforme a
rodada chega, e o sistema nunca recolhe o refino. As duas tabelas ficam erradas
JUNTAS — por isso a checagem de divergência entre elas nunca acusou nada.

Medido em 17/09/2026: **12 rodadas de 2026 com menos de 3 horários distintos,
totalizando 120 fixtures.** As rodadas 28 a 38 têm todas os 10 jogos no MESMO
minuto. A rodada 27, disputada em quatro dias (11, 12, 13 e 14/09 `[externo,
16/09]`), tem nove dos dez carimbados com `2026-09-13 20:00:00`.

O ESTRAGO QUE ISSO CAUSA
========================

Com a data apontando para o futuro, `motor_recomendacoes.buscar_odds_futuras`
trata um jogo JÁ DISPUTADO como futuro e REGERA a recomendação dele. O
arquivamento seguinte grava uma segunda cópia. Cada ciclo soma uma cópia, e a
cópia nova carrega LOOK-AHEAD: `motor_padroes` já regravou a janela de 50 jogos
com o próprio jogo dentro dela. Medido sobre 305 grupos: 93,2% das diferenças
de probabilidade apontam na direção do resultado.

⚠️ E ATENÇÃO AO QUE **NÃO** FAZER: a primeira cópia arquivada é a LIMPA (de
antes do jogo), e a dedup guarda a de menor `arquivado_em`. Qualquer conserto
que RETENHA o arquivamento troca "duplicatas dedupláveis" por "um registro só,
contaminado" — porque `salvar_recomendacoes` apaga e regrava jogo futuro. Isso
já foi tentado em 16/09 e revertido no mesmo dia. **Conserta-se a DATA, nunca
o arquivamento.**

POR QUE UM SCRIPT SEPARADO, E NÃO O CONSERTO NOS DOIS ARQUIVOS
==============================================================

O conserto definitivo é pequeno em cada arquivo (trocar `is None` por
`IS DISTINCT FROM` no `popular_banco`; incluir `data_jogo = EXCLUDED.data_jogo`
no `popular_tabela`) e continua sendo o caminho certo — este script NÃO o
substitui. Mas:

  1. A rodada 28 já está gerando recomendação HOJE (192 linhas em 4 fixtures,
     todas carimbadas `20/09 20:00`), então o prazo é de dois dias.
  2. Regravar dois arquivos grandes à mão para mudar três linhas é exatamente
     o erro que este projeto já cometeu — e a regra escrita é "não redigitar
     arquivo grande para mudar uma linha".
  3. Um arquivo NOVO tem risco de transcrição zero, e cabe nas duas fases
     (`verificar` / `aplicar`) que todo script que toca dado precisa ter.

⚠️ **Enquanto os dois arquivos não forem corrigidos, este script é obrigatório
na cadeia** — ou rodado à mão antes de cada rodada. Duas funções com a mesma
responsabilidade convivendo é dívida registrada, não desenho.

QUEM GANHA A DATA
=================

A **API-Football**, sempre. Ela é dona do `fixture_id_api`, refina conforme a
CBF detalha, e para jogo encerrado a data dela é definitiva. A OddsPapi não
sobrescreve nada (o `atualizar_odds` continua como está).

O QUE ESTE SCRIPT NÃO TOCA
==========================

  - `historico_recomendacoes` — a `data_jogo` de lá é cópia congelada no
    momento do arquivamento. Corrigir `jogos` não a reescreve, e não deve.
  - `recomendacoes` — não apaga nem gera nada. Só arruma a data; o motor
    decide sozinho o que fazer na próxima execução.
  - Qualquer coluna que não seja `jogos.data_jogo`, `jogos.datahora_jogo` e
    `jogos_liga.data_jogo`.

CUSTO DE API
============

**1 requisição por temporada** (`fixtures?league=71&season=YYYY` devolve o
calendário inteiro, ~380 jogos). Por padrão roda só 2026. O contador em
`controle_api_uso` é atualizado, igual aos outros scripts.

TRAVA CONTRA LIXO DA FONTE
==========================

Se mais de `LIMITE_DIVERGENCIA_PCT` dos fixtures da temporada divergirem, o
`aplicar` PARA e exige `--confirmo-massa`. O esperado hoje é ~31% (as 12
rodadas carimbadas). Se vier 90%, a API devolveu outra coisa e reescrever a
temporada em silêncio seria pior que não fazer nada.

⚠️ E a regra que já custou caro neste projeto: **quando a trava dispara, a
primeira pergunta é "a premissa está certa?", não "como libero?"**.

USO
===

    python sincronizar_datas_jogos.py verificar
    python sincronizar_datas_jogos.py aplicar
    python sincronizar_datas_jogos.py aplicar --confirmo-massa
    python sincronizar_datas_jogos.py verificar --temporadas 2025,2026

A segunda execução do `aplicar` deve alterar **0 linhas**. É essa idempotência
que permite rodar de novo depois de um crash sem trabalhar às cegas — foi ela
que provou, em 15/09, que um commit tinha passado antes de um traceback.

Variáveis de ambiente (as mesmas do popular_banco.py):
  - API_FOOTBALL_KEY
  - DATABASE_URL
"""

import os
import sys
from datetime import datetime

import requests
import psycopg2

API_KEY = os.environ["API_FOOTBALL_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

LEAGUE_ID = 71
TEMPORADAS_PADRAO = [2026]

# Se mais que isso divergir, o aplicar exige confirmação explícita.
LIMITE_DIVERGENCIA_PCT = 50.0

# Teste de cheiro de `1_ESSENCIAL §5.7`: uma rodada real do Brasileirão tem
# 5 a 8 horários distintos entre os 10 jogos. Menos de 3 é carimbo provisório.
HORARIOS_MINIMOS_RODADA_REAL = 3


def cabecalho(fase):
    print("=" * 78)
    print(f"sincronizar_datas_jogos.py | fase: {fase}")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 78)


def relogio_do_banco(cur):
    """O relógio e o fuso da SESSÃO, impressos junto de qualquer medição de
    data. Em 16/09 uma hipótese inteira (fuso deslocando a comparação) foi
    levantada e derrubada; ter isso no log dispensa levantá-la de novo."""
    cur.execute("SELECT LOCALTIMESTAMP, CURRENT_DATE, current_setting('TimeZone')")
    agora, hoje, fuso = cur.fetchone()
    print(f"relógio da sessão: {agora}  (CURRENT_DATE={hoje}, TimeZone={fuso})")
    return agora, hoje


def buscar_calendario(cur, temporada):
    """1 requisição: o calendário inteiro da liga na temporada."""
    resp = requests.get(
        f"{API_BASE}/fixtures",
        headers=HEADERS,
        params={"league": LEAGUE_ID, "season": temporada},
        timeout=60,
    )
    cur.execute(
        "INSERT INTO controle_api_uso (dia, requisicoes) VALUES (CURRENT_DATE, 0) "
        "ON CONFLICT (dia) DO NOTHING"
    )
    cur.execute(
        "UPDATE controle_api_uso SET requisicoes = requisicoes + 1 WHERE dia = CURRENT_DATE"
    )
    cur.connection.commit()

    resp.raise_for_status()
    dados = resp.json()
    if dados.get("errors"):
        print(f"  ⚠️  Aviso da API para {temporada}: {dados['errors']}")
        return []
    fixtures = dados["response"]
    print(f"  temporada {temporada}: {len(fixtures)} fixtures no calendário da API")
    return fixtures


def _numero_rodada(rodada):
    import re
    m = re.search(r"(\d+)", rodada or "")
    return int(m.group(1)) if m else None


def levantar_divergencias(cur, fixtures):
    """Compara o que a API diz AGORA com o que está gravado.

    Devolve duas listas: divergências em `jogos` (uma entrada por LINHA, e
    lembrando que um jogo real tem DUAS linhas — uma por perspectiva) e
    divergências em `jogos_liga` (uma por fixture, a tabela é 1:1).

    ⚠️ A comparação é feita pelo Postgres, não em Python: a string da API
    (`2026-09-13T20:00:00+00:00`) é convertida com o mesmo cast que o INSERT
    original usa, então "igual" aqui significa exatamente "o INSERT gravaria
    o mesmo valor". Comparar em Python exigiria replicar o parsing e abriria
    espaço para as duas metades divergirem."""
    div_jogos = []
    div_liga = []

    for fx in fixtures:
        fixture_id = fx["fixture"]["id"]
        data_api_iso = fx["fixture"]["date"]
        rodada_numero = _numero_rodada(fx.get("league", {}).get("round"))

        cur.execute(
            """
            SELECT id, nosso_time_id, data_jogo, datahora_jogo, rodada_numero
            FROM jogos
            WHERE fixture_id_api = %s
              AND (datahora_jogo IS DISTINCT FROM %s::timestamptz AT TIME ZONE 'UTC'
                   OR data_jogo IS DISTINCT FROM (%s::timestamptz AT TIME ZONE 'UTC')::date)
            ORDER BY id
            """,
            (fixture_id, data_api_iso, data_api_iso),
        )
        for jogo_id, nosso_time_id, data_atual, datahora_atual, rod in cur.fetchall():
            div_jogos.append({
                "jogo_id": jogo_id,
                "fixture_id": fixture_id,
                "rodada": rod if rod is not None else rodada_numero,
                "data_atual": data_atual,
                "datahora_atual": datahora_atual,
                "data_api_iso": data_api_iso,
            })

        cur.execute(
            """
            SELECT id, data_jogo, status
            FROM jogos_liga
            WHERE fixture_id_api = %s
              AND data_jogo IS DISTINCT FROM (%s::timestamptz AT TIME ZONE 'UTC')::date
            """,
            (fixture_id, data_api_iso),
        )
        for liga_id, data_atual, status in cur.fetchall():
            div_liga.append({
                "liga_id": liga_id,
                "fixture_id": fixture_id,
                "rodada": rodada_numero,
                "data_atual": data_atual,
                "status": status,
                "data_api_iso": data_api_iso,
            })

    return div_jogos, div_liga


def horarios_por_rodada(cur, rodadas):
    """O teste de cheiro, antes e depois: quantos horários distintos cada
    rodada tem. Menos de 3 em 10 jogos = carimbo provisório."""
    if not rodadas:
        return {}
    cur.execute(
        """
        SELECT rodada_numero,
               COUNT(DISTINCT fixture_id_api) AS fixtures,
               COUNT(DISTINCT datahora_jogo) AS horarios,
               COUNT(DISTINCT data_jogo) AS dias
        FROM jogos
        WHERE data_jogo >= DATE '2026-01-01' AND rodada_numero = ANY(%s)
        GROUP BY 1 ORDER BY 1
        """,
        (list(rodadas),),
    )
    return {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}


def imprimir_relatorio(div_jogos, div_liga, total_fixtures, horarios_antes):
    print("-" * 78)
    if not div_jogos and not div_liga:
        print("Nenhuma divergência de data. O banco está igual ao que a API diz agora.")
        return 0.0

    fixtures_afetados = {d["fixture_id"] for d in div_jogos} | {d["fixture_id"] for d in div_liga}
    pct = 100.0 * len(fixtures_afetados) / total_fixtures if total_fixtures else 0.0

    print(f"DIVERGÊNCIAS: {len(fixtures_afetados)} fixture(s) de {total_fixtures} "
          f"({pct:.1f}%) — {len(div_jogos)} linha(s) em `jogos`, "
          f"{len(div_liga)} em `jogos_liga`.")

    por_rodada = {}
    for d in div_jogos:
        por_rodada.setdefault(d["rodada"], []).append(d)

    print("-" * 78)
    print("Por rodada (horários distintos HOJE — menos de 3 em 10 jogos é carimbo):")
    for rodada in sorted(k for k in por_rodada if k is not None):
        fixtures_rod = {d["fixture_id"] for d in por_rodada[rodada]}
        antes = horarios_antes.get(rodada)
        marca = ""
        if antes and antes[1] < HORARIOS_MINIMOS_RODADA_REAL:
            marca = "  ⚠️ CARIMBO PROVISÓRIO"
        detalhe = f"{antes[2]} dia(s), {antes[1]} horário(s) em {antes[0]} fixtures" if antes else "?"
        print(f"  rodada {rodada:>2}: {len(fixtures_rod):>2} fixture(s) a corrigir | hoje: {detalhe}{marca}")

    print("-" * 78)
    print("Amostra (até 25 linhas de `jogos`):")
    for d in div_jogos[:25]:
        print(f"  jogo #{d['jogo_id']} fixture={d['fixture_id']} rodada={d['rodada']} | "
              f"banco: {d['datahora_atual']} (data {d['data_atual']}) "
              f"-> API: {d['data_api_iso']}")
    if len(div_jogos) > 25:
        print(f"  ... e mais {len(div_jogos) - 25} linha(s).")

    if div_liga:
        print("-" * 78)
        print("Amostra (até 15 linhas de `jogos_liga`):")
        for d in div_liga[:15]:
            print(f"  liga #{d['liga_id']} fixture={d['fixture_id']} status={d['status']} | "
                  f"banco: {d['data_atual']} -> API: {d['data_api_iso']}")
        if len(div_liga) > 15:
            print(f"  ... e mais {len(div_liga) - 15} linha(s).")

    return pct


def aplicar_correcoes(cur, div_jogos, div_liga):
    """Grava, com guarda de idempotência no próprio `WHERE`. Segunda execução
    altera 0 linhas — e o log tem que mostrar isso."""
    alterados_jogos = 0
    for d in div_jogos:
        cur.execute(
            """
            UPDATE jogos
               SET datahora_jogo = %s::timestamptz AT TIME ZONE 'UTC',
                   data_jogo     = (%s::timestamptz AT TIME ZONE 'UTC')::date
             WHERE id = %s
               AND (datahora_jogo IS DISTINCT FROM %s::timestamptz AT TIME ZONE 'UTC'
                    OR data_jogo IS DISTINCT FROM (%s::timestamptz AT TIME ZONE 'UTC')::date)
            """,
            (d["data_api_iso"], d["data_api_iso"], d["jogo_id"],
             d["data_api_iso"], d["data_api_iso"]),
        )
        if cur.rowcount:
            alterados_jogos += cur.rowcount
            print(f"  📅 jogos #{d['jogo_id']} (fixture {d['fixture_id']}, rodada {d['rodada']}): "
                  f"{d['datahora_atual']} -> {d['data_api_iso']}")

    alterados_liga = 0
    for d in div_liga:
        cur.execute(
            """
            UPDATE jogos_liga
               SET data_jogo = (%s::timestamptz AT TIME ZONE 'UTC')::date
             WHERE id = %s
               AND data_jogo IS DISTINCT FROM (%s::timestamptz AT TIME ZONE 'UTC')::date
            """,
            (d["data_api_iso"], d["liga_id"], d["data_api_iso"]),
        )
        if cur.rowcount:
            alterados_liga += cur.rowcount
            print(f"  📅 jogos_liga #{d['liga_id']} (fixture {d['fixture_id']}): "
                  f"{d['data_atual']} -> {d['data_api_iso']}")

    return alterados_jogos, alterados_liga


def filtrar_somente_futuros(div_jogos, div_liga, hoje):
    """NOVO (17/09/2026): escopa a correção aos jogos que AINDA NÃO
    ACONTECERAM, pela data que a API diz.

    POR QUE ISSO EXISTE — e é a diferença entre 6.8 e 6.9:

    `motor_padroes` ordena a janela de 50 jogos POR DATA. Corrigir a data de
    um jogo JÁ DISPUTADO reordena essa janela e, portanto, muda o padrão
    gerado na próxima execução. A regra do projeto é clara: correção que
    muda o gerado não sobe no meio de medição, e a medição do handicap
    (item 1.20) está aberta na rodada 28.

    O caso extremo medido em 17/09: o fixture 1492145 está gravado como
    2026-02-25 e a API diz 2026-09-02 — **seis meses**. É o Mirassol x
    Flamengo que a docstring do `arquivar_recomendacoes` já registrava.
    Mover esse jogo reordena a janela de vários times de uma vez.

    Já os jogos FUTUROS não estão em nenhuma janela de padrão ainda (a
    janela só tem jogo disputado), então corrigi-los é neutro para o
    gerado — e é exatamente o que impede a duplicação da rodada que vem.

    Escopar assim preserva a comparabilidade da medição em curso, que é
    outra regra escrita deste projeto."""
    fj = [d for d in div_jogos if d["data_api_iso"][:10] >= str(hoje)]
    fl = [d for d in div_liga if d["data_api_iso"][:10] >= str(hoje)]
    return fj, fl


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("verificar", "aplicar"):
        print(__doc__)
        print("Uso: python sincronizar_datas_jogos.py verificar|aplicar "
              "[--somente-futuros] [--confirmo-massa] [--temporadas 2025,2026]")
        sys.exit(1)

    fase = sys.argv[1]
    confirmo_massa = "--confirmo-massa" in sys.argv
    somente_futuros = "--somente-futuros" in sys.argv

    temporadas = TEMPORADAS_PADRAO
    if "--temporadas" in sys.argv:
        temporadas = [int(t) for t in sys.argv[sys.argv.index("--temporadas") + 1].split(",")]

    cabecalho(fase)

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        _agora, hoje = relogio_do_banco(cur)
        print(f"temporadas: {temporadas}")
        print(f"escopo: {'SOMENTE JOGOS FUTUROS (>= ' + str(hoje) + ')' if somente_futuros else 'TODOS os jogos'}")
        if not somente_futuros:
            print("  ⚠️  Sem --somente-futuros, jogo JÁ DISPUTADO também é corrigido, e isso")
            print("      REORDENA a janela de 50 do motor_padroes — muda o gerado. Ver a")
            print("      docstring de filtrar_somente_futuros antes de rodar assim durante")
            print("      uma medição aberta.")

        total_jogos = total_liga = 0

        for temporada in temporadas:
            print("\n" + "=" * 78)
            print(f"TEMPORADA {temporada}")
            fixtures = buscar_calendario(cur, temporada)
            if not fixtures:
                continue

            div_jogos, div_liga = levantar_divergencias(cur, fixtures)
            if somente_futuros:
                antes_j, antes_l = len(div_jogos), len(div_liga)
                div_jogos, div_liga = filtrar_somente_futuros(div_jogos, div_liga, hoje)
                print(f"  escopo futuro: {len(div_jogos)}/{antes_j} linha(s) de `jogos` e "
                      f"{len(div_liga)}/{antes_l} de `jogos_liga` — o resto fica para o item 6.9")
            rodadas = {d["rodada"] for d in div_jogos if d["rodada"] is not None}
            horarios_antes = horarios_por_rodada(cur, rodadas)
            pct = imprimir_relatorio(div_jogos, div_liga, len(fixtures), horarios_antes)

            if fase == "verificar":
                continue

            if not div_jogos and not div_liga:
                continue

            if pct > LIMITE_DIVERGENCIA_PCT and not confirmo_massa:
                print("-" * 78)
                print(f"⛔ PARADO: {pct:.1f}% dos fixtures divergem, acima do limite de "
                      f"{LIMITE_DIVERGENCIA_PCT}%.")
                print("   Isso pode significar que a API devolveu outra coisa (temporada")
                print("   errada, campeonato errado, resposta parcial) — reescrever a")
                print("   temporada inteira em silêncio seria pior que não fazer nada.")
                print("   ⚠️ A pergunta certa aqui é 'a premissa está certa?', não")
                print("   'como libero?'. Confira a amostra acima contra o ge/Sofascore")
                print("   ANTES de rodar com --confirmo-massa.")
                conn.rollback()
                sys.exit(2)

            print("-" * 78)
            print("APLICANDO:")
            a_j, a_l = aplicar_correcoes(cur, div_jogos, div_liga)
            conn.commit()
            total_jogos += a_j
            total_liga += a_l

            horarios_depois = horarios_por_rodada(cur, rodadas)
            print("-" * 78)
            print("Teste de cheiro, antes -> depois (horários distintos por rodada):")
            for rodada in sorted(k for k in rodadas if k is not None):
                a = horarios_antes.get(rodada)
                d = horarios_depois.get(rodada)
                if not a or not d:
                    continue
                sinal = "✅" if d[1] >= HORARIOS_MINIMOS_RODADA_REAL else "⚠️ ainda carimbada"
                print(f"  rodada {rodada:>2}: {a[1]} -> {d[1]} horário(s), "
                      f"{a[2]} -> {d[2]} dia(s)  {sinal}")

        print("\n" + "=" * 78)
        if fase == "verificar":
            # Item 9.20 (29/09/2026): a mensagem antiga dizia "nada foi
            # escrito", e não era verdade — `buscar_calendario` commita
            # +1 em `controle_api_uso` a cada temporada consultada, em
            # QUALQUER fase. O dado de jogo é que não é tocado.
            print("FASE VERIFICAR — `jogos` e `jogos_liga` NÃO foram tocadas.")
            print(f"  (`controle_api_uso` recebeu +{len(temporadas)} requisição(ões) — "
                  f"1 por temporada consultada, igual em qualquer fase.)")
        else:
            print(f"APLICADO: {total_jogos} linha(s) de `jogos` e {total_liga} de `jogos_liga`.")
            if total_jogos == 0 and total_liga == 0:
                print("0 linhas alteradas — idempotente, já estava sincronizado.")
            else:
                print("⚠️ Rode de novo: a segunda execução tem que dar 0 linhas alteradas.")
                print("⚠️ `historico_recomendacoes` NÃO foi tocada, de propósito.")
        print("=" * 78)

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
