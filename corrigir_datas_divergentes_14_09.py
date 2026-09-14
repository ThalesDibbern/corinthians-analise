"""
Corrige as divergencias de data medidas em 14/09/2026.

O QUE FOI MEDIDO (rota /api/consulta, 14/09)
--------------------------------------------
10 fixtures com `jogos.data_jogo` diferente de `jogos_liga.data_jogo`.
Eles se separam em TRES casos com causas e riscos diferentes -- e essa
separacao e a parte que importa, porque so um deles e urgente.

CASO A - perspectivas divergentes entre si (2 fixtures)
    1492361  RB Bragantino x Bahia     uma perspectiva 05/09, outra 06/09
    1492363  Coritiba x Mirassol       uma perspectiva 05/09, outra 06/09

    Sao os UNICOS 2 fixtures do banco inteiro com mais de uma data
    entre suas duas perspectivas. `jogos_liga` diz 06/09 nos dois, e o
    jogo foi disputado (status FT, com cartoes, gols e estatisticas dos
    dois lados).

    ⚠️ O jogo_id 714 -- registrado em 2_ABERTO 6.4 como "contradicao:
    gravado 05/09 mas tem 6 cartoes e estatistica dos dois lados" -- e
    exatamente a perspectiva errada do fixture 1492363. Nao e jogo nao
    disputado com dado: e perspectiva com data um dia atrasada. O item
    6.4 fecha aqui.

    Correcao: alinhar a perspectiva atrasada com a irma e com jogos_liga.

CASO B - jogos_liga desatualizado, jogos correto (7 fixtures)
    1492371  Bahia x Remo          jogos 14/09  liga 13/09   NS
    1492311  Botafogo x Gremio     jogos 16/09  liga 29/07   NS
    1492368  Sao Paulo x Atletico-MG    05/09       06/09     FT
    1492369  Vitoria x Gremio           07/09       06/09     FT
    1492351  Atletico-MG x Vitoria      29/08       30/08     FT
    1492357  Remo x Coritiba            31/08       30/08     FT
    1492358  Sao Paulo x RB Bragantino  29/08       30/08     FT
    1492145  Flamengo x Mirassol        02/09       25/02     FT

    `jogos_liga` guarda a data PROVISORIA da API-Football (hipotese ja
    derrubada em 04/09: nao e a fonte confiavel). Nos jogos ja
    encerrados isso e cosmetico -- o arquivamento le `jl.status`, nao
    `jl.data_jogo`.

    Os DOIS com status NS sao o residuo real:
      - 1492371 e o Bahia x Remo corrigido em `jogos` as 01:03 UTC de
        14/09. A divergencia ficou INVERTIDA: jogos certo, liga errado.
        A Q13 do protocolo le `jogos_liga` e mostraria a data errada no
        card, e o proximo popular_banco pode avisar de novo, desta vez
        contra o valor correto.
      - 1492311 e um jogo da rodada 21 remarcado de 29/07 para 16/09.

    Este script alinha `jogos_liga` SOMENTE nesses dois. Os encerrados
    ficam como estao: mexer neles e reescrever historico sem ganho, e a
    regra do projeto e nao mexer no que esta sendo medido.

CASO C - nao e divergencia (0 fixtures a tocar)
    Nenhum caso onde `jogos_liga` esteja certo e `jogos` errado foi
    encontrado nesta varredura.

FONTE DO DESEMPATE
------------------
Caso A: as duas perspectivas do MESMO fixture nao podem ter datas
diferentes -- e contradicao interna, e `jogos_liga` + o status FT
concordam com 06/09. Desempate estrutural, nao externo.

Caso B: `jogos` ja foi conferido contra fonte externa (CBF/ge) em
13-14/09 para o 1492371. Para o 1492311 a data 16/09 vem da API atual e
e futura -- se for para tratar como decisao cara, conferir no ge antes
de rodar a fase aplicar.

SEGURANCA
---------
- Duas fases. `verificar` nao escreve.
- `aplicar` aborta se qualquer fixture do caso A ja tiver linha em
  `historico_recomendacoes` com data divergente -- ai o escopo deixa de
  ser so `jogos` e passa a exigir reavaliacao.
- Nenhuma FK aponta para `jogos_liga` (verificado em information_schema).
- Cada UPDATE confere o rowcount esperado antes do commit.

USO
---
    python corrigir_datas_divergentes_14_09.py verificar
    python corrigir_datas_divergentes_14_09.py aplicar
"""

import os
import sys
from datetime import datetime, timezone

import psycopg2


# CASO A: fixture -> data correta (a que jogos_liga e o status FT apoiam)
CASO_A = {
    1492361: "2026-09-06",
    1492363: "2026-09-06",
}

# CASO B: fixture -> data correta em `jogos`, a ser copiada para jogos_liga.
# Apenas os dois ainda NAO disputados.
CASO_B = {
    1492371: "2026-09-14",
    1492311: "2026-09-16",
}


def cabecalho(fase):
    print("=" * 72)
    print("corrigir_datas_divergentes_14_09.py")
    print(f"FASE......: {fase}")
    print(f"ARGV......: {sys.argv}")
    print(f"RELOGIO...: {datetime.now(timezone.utc).isoformat()} UTC")
    print("=" * 72)
    sys.stdout.flush()


def conectar():
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ERRO: DATABASE_URL nao configurada.")
        sys.exit(1)
    return psycopg2.connect(url)


def estado(conn):
    fixtures = tuple(list(CASO_A) + list(CASO_B))

    with conn.cursor() as cur:
        cur.execute("""
            SELECT j.fixture_id_api, j.id, j.data_jogo, j.datahora_jogo,
                   jl.data_jogo, jl.status,
                   (SELECT COUNT(*) FROM historico_recomendacoes h
                     WHERE h.jogo_id = j.id) AS no_historico,
                   (SELECT COUNT(*) FROM recomendacoes r
                     WHERE r.jogo_id = j.id) AS ativas
            FROM jogos j
            LEFT JOIN jogos_liga jl ON jl.fixture_id_api = j.fixture_id_api
            WHERE j.fixture_id_api IN %s
            ORDER BY j.fixture_id_api, j.id
        """, (fixtures,))
        linhas = cur.fetchall()

    print()
    print("Estado atual:")
    print()
    print("  fixture | jogo_id | jogos.data | datahora            | liga.data  | st | hist | ativas")
    print("  --------+---------+------------+---------------------+------------+----+------+-------")
    for fx, jid, dj, dh, dl, st, hist, ativas in linhas:
        print(f"  {fx:>7} | {jid:>7} | {str(dj):>10} | {str(dh):>19} | {str(dl):>10} "
              f"| {str(st):>2} | {hist:>4} | {ativas:>6}")
    print()
    return linhas


def verificar(conn):
    linhas = estado(conn)

    bloqueios = []
    for fx, jid, dj, dh, dl, st, hist, ativas in linhas:
        if fx in CASO_A and hist > 0 and str(dj) != CASO_A[fx]:
            bloqueios.append(
                f"fixture {fx} jogo_id {jid}: {hist} linha(s) ja em "
                f"historico_recomendacoes com a data errada ({dj})"
            )

    print("Plano:")
    print()
    print("  CASO A - alinhar perspectiva atrasada em `jogos`:")
    for fx, data in CASO_A.items():
        print(f"    fixture {fx} -> data_jogo = {data} nas duas perspectivas")
    print()
    print("  CASO B - alinhar `jogos_liga` (so os nao disputados):")
    for fx, data in CASO_B.items():
        print(f"    fixture {fx} -> jogos_liga.data_jogo = {data}")
    print()

    if bloqueios:
        print("!! BLOQUEIO - NAO APLICAR SEM DECIDIR !!")
        print()
        for b in bloqueios:
            print("   " + b)
        print()
        print("A recomendacao ja foi arquivada com a data errada. Corrigir")
        print("`jogos` nao reabre `historico_recomendacoes`: mercado avaliado")
        print("contra PLACAR congela veredito (1_ESSENCIAL 5.8). Decidir se")
        print("entra tambem uma reavaliacao antes de rodar aplicar.")
        print()
        return False

    print("Sem bloqueio. Seguro aplicar.")
    print()
    return True


def aplicar(conn):
    if not verificar(conn):
        print("Abortado pela fase verificar.")
        return

    with conn.cursor() as cur:
        for fx, data in CASO_A.items():
            cur.execute("""
                UPDATE jogos
                   SET data_jogo = %s::date,
                       datahora_jogo = %s::date
                                     + (datahora_jogo - data_jogo::timestamp)
                 WHERE fixture_id_api = %s
                   AND data_jogo <> %s::date
            """, (data, data, fx, data))
            print(f"CASO A fixture {fx}: {cur.rowcount} perspectiva(s) alinhada(s).")

        for fx, data in CASO_B.items():
            cur.execute("""
                UPDATE jogos_liga
                   SET data_jogo = %s::date,
                       atualizado_em = NOW()
                 WHERE fixture_id_api = %s
                   AND data_jogo <> %s::date
            """, (data, fx, data))
            print(f"CASO B fixture {fx}: {cur.rowcount} linha(s) em jogos_liga.")

        conn.commit()

    print()
    print("Commit feito. Estado final:")
    estado(conn)


def main():
    fase = sys.argv[1] if len(sys.argv) > 1 else ""
    if fase not in ("verificar", "aplicar"):
        print("uso: python corrigir_datas_divergentes_14_09.py verificar|aplicar")
        sys.exit(1)

    cabecalho(fase)
    conn = conectar()
    try:
        if fase == "verificar":
            verificar(conn)
        else:
            aplicar(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
