"""
Script PONTUAL (verificar/aplicar) - mescla pares de `jogos` que são o
MESMO jogo real duplicado por `data_jogo` divergente entre duas fontes
(OddsPapi vs API-Football) - mesmo par de times, mesma perspectiva
(nosso_time_id), datas 1 dia de diferença.

Caso que expôs o problema (27/08/2026): RB Bragantino x São Paulo -
    jogo #1296: data=29/08 23:00, fixture_id_api sintético (-524484861),
                641 odds, 18 recomendações
    jogo #1492358: data=30/08 20:00, fixture_id_api REAL (1492358),
                    0 odds, 0 recomendações
21h de diferença entre os dois horários - grande demais pra ser só
UTC vs BRT (isso daria ~3h). Mais provável: reagendamento real do jogo
entre quando a OddsPapi coletou e quando a API-Football confirmou, e as
duas fontes ficaram temporariamente dessincronizadas.

REGRA DE MERGE:
  - "Sobrevivente" (mantém o ID) = a linha com MAIS referências
    (odds + recomendacoes + historico_recomendacoes) - migrar pouca
    coisa é mais barato e mais seguro do que migrar muita coisa.
  - Se a linha PERDEDORA tiver fixture_id_api REAL (positivo) e a
    sobrevivente não tiver (ou for diferente), a sobrevivente HERDA
    fixture_id_api/data_jogo/datahora_jogo da perdedora - confia no dado
    confirmado pela API-Football por cima do que veio só da OddsPapi.
  - Toda referência (odds, recomendacoes, historico_recomendacoes, e
    qualquer outra tabela com FK pra jogos.id - descoberta via
    information_schema, NUNCA de memória, pra não repetir o bug que já
    aconteceu com limpeza_jogadores_duplicados.py) é reatribuída da
    perdedora pra sobrevivente. Campos JSONB que guardam jogo_id sem ser
    FK de verdade (multiplas_candidatas.jogos, apostas_salvas.pernas)
    são tratados à parte, via substituição de texto.
  - A linha perdedora é apagada só depois de tudo reatribuído.

DUAS FASES:
  FASE 1 (`verificar`) - só mostra o que seria feito, não grava nada.
  FASE 2 (`aplicar`) - faz de verdade, numa transação só, rollback em
  qualquer erro.

Rodar:
    python corrigir_jogos_duplicados_fuso.py verificar
    python corrigir_jogos_duplicados_fuso.py aplicar

Variáveis de ambiente necessárias: DATABASE_URL
"""

import os
import sys
from datetime import datetime

import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]


def imprimir_cabecalho(fase):
    print("=" * 78)
    print("corrigir_jogos_duplicados_fuso.py")
    print(f"  fase:   {fase}")
    print(f"  argv:   {sys.argv}")
    print(f"  agora:  {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 78)


def buscar_pares(cur):
    cur.execute(
        """
        SELECT a.id, a.data_jogo, a.datahora_jogo, a.fixture_id_api,
               b.id, b.data_jogo, b.datahora_jogo, b.fixture_id_api
        FROM jogos a
        JOIN jogos b
          ON a.mandante_id = b.mandante_id
         AND a.visitante_id = b.visitante_id
         AND a.nosso_time_id = b.nosso_time_id
         AND a.id < b.id
         AND ABS(a.data_jogo - b.data_jogo) = 1
        WHERE a.mandante_id IS NOT NULL AND a.visitante_id IS NOT NULL
        ORDER BY a.data_jogo
        """
    )
    return cur.fetchall()


def contar_referencias(cur, jogo_id):
    cur.execute("SELECT COUNT(*) FROM odds WHERE jogo_id = %s", (jogo_id,))
    n_odds = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM recomendacoes WHERE jogo_id = %s", (jogo_id,))
    n_rec = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM historico_recomendacoes WHERE jogo_id = %s", (jogo_id,))
    n_hist = cur.fetchone()[0]
    return n_odds + n_rec + n_hist, {"odds": n_odds, "recomendacoes": n_rec, "historico_recomendacoes": n_hist}


def decidir_merge(cur, par):
    (id_a, data_a, datahora_a, fixture_a, id_b, data_b, datahora_b, fixture_b) = par

    total_a, detalhe_a = contar_referencias(cur, id_a)
    total_b, detalhe_b = contar_referencias(cur, id_b)

    if total_a != total_b:
        sobrevivente, perdedora = (id_a, id_b) if total_a > total_b else (id_b, id_a)
    elif (fixture_a > 0) != (fixture_b > 0):
        sobrevivente, perdedora = (id_a, id_b) if fixture_a > 0 else (id_b, id_a)
    else:
        sobrevivente, perdedora = (id_a, id_b) if id_a < id_b else (id_b, id_a)

    dados = {
        id_a: {"data_jogo": data_a, "datahora_jogo": datahora_a, "fixture_id_api": fixture_a, "total_ref": total_a, "detalhe": detalhe_a},
        id_b: {"data_jogo": data_b, "datahora_jogo": datahora_b, "fixture_id_api": fixture_b, "total_ref": total_b, "detalhe": detalhe_b},
    }

    # a sobrevivente herda data/fixture da perdedora só se a PERDEDORA tiver
    # fixture_id_api real (positivo) e a sobrevivente não - confia no dado
    # confirmado pela API-Football.
    herdar_da_perdedora = dados[perdedora]["fixture_id_api"] > 0 and dados[sobrevivente]["fixture_id_api"] <= 0

    return {
        "sobrevivente": sobrevivente,
        "perdedora": perdedora,
        "dados": dados,
        "herdar_da_perdedora": herdar_da_perdedora,
    }


def buscar_tabelas_que_referenciam_jogos(cur):
    cur.execute(
        """
        SELECT tc.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = 'public'
          AND ccu.table_name = 'jogos' AND ccu.column_name = 'id'
        """
    )
    return cur.fetchall()


def mesclar(cur, sobrevivente, perdedora, herdar_da_perdedora, dados):
    if herdar_da_perdedora:
        cur.execute(
            "UPDATE jogos SET data_jogo = %s, datahora_jogo = %s, fixture_id_api = %s WHERE id = %s",
            (dados[perdedora]["data_jogo"], dados[perdedora]["datahora_jogo"],
             dados[perdedora]["fixture_id_api"], sobrevivente),
        )
        print(f"  Sobrevivente #{sobrevivente} herdou data/fixture_id_api de #{perdedora} "
              f"(era {dados[sobrevivente]['fixture_id_api']}, virou {dados[perdedora]['fixture_id_api']})")

    tabelas = buscar_tabelas_que_referenciam_jogos(cur)
    for tabela, coluna in tabelas:
        cur.execute(f'UPDATE "{tabela}" SET "{coluna}" = %s WHERE "{coluna}" = %s', (sobrevivente, perdedora))
        if cur.rowcount:
            print(f"    reatribuído: {cur.rowcount} linha(s) em {tabela}.{coluna}")

    # multiplas_candidatas guarda jogo_id DENTRO de um campo JSONB (`jogos`),
    # não é FK de verdade - não aparece na busca acima.
    cur.execute(
        "UPDATE multiplas_candidatas SET jogos = REPLACE(jogos::text, %s, %s)::jsonb WHERE jogos::text LIKE %s",
        (f'"jogo_id": {perdedora}', f'"jogo_id": {sobrevivente}', f'%"jogo_id": {perdedora}%'),
    )
    if cur.rowcount:
        print(f"    reatribuído: {cur.rowcount} linha(s) em multiplas_candidatas.jogos (JSONB)")

    # apostas_salvas.pernas também é JSONB com jogo_id embutido.
    cur.execute(
        "UPDATE apostas_salvas SET pernas = REPLACE(pernas::text, %s, %s)::jsonb WHERE pernas::text LIKE %s",
        (f'"jogo_id": {perdedora}', f'"jogo_id": {sobrevivente}', f'%"jogo_id": {perdedora}%'),
    )
    if cur.rowcount:
        print(f"    reatribuído: {cur.rowcount} linha(s) em apostas_salvas.pernas (JSONB)")

    cur.execute("DELETE FROM jogos WHERE id = %s", (perdedora,))
    print(f"  Apagado: jogo #{perdedora} (perdedora, já sem referências)")


def fase_verificar():
    imprimir_cabecalho("verificar")
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    try:
        pares = buscar_pares(cur)
        if not pares:
            print("\nNenhum par encontrado - nada a fazer.")
            return

        print(f"\n{len(pares)} par(es):\n")
        for par in pares:
            plano = decidir_merge(cur, par)
            s, p, dados = plano["sobrevivente"], plano["perdedora"], plano["dados"]
            print(f"  Sobrevivente: #{s}  (refs: {dados[s]['detalhe']}, total={dados[s]['total_ref']}, "
                  f"fixture_id_api={dados[s]['fixture_id_api']}, data={dados[s]['data_jogo']})")
            print(f"  Perdedora:    #{p}  (refs: {dados[p]['detalhe']}, total={dados[p]['total_ref']}, "
                  f"fixture_id_api={dados[p]['fixture_id_api']}, data={dados[p]['data_jogo']})")
            if plano["herdar_da_perdedora"]:
                print(f"  -> sobrevivente vai herdar data/fixture_id_api de #{p} (fixture real confirmado lá)")
            print()

        print("Se fizer sentido, rode: python corrigir_jogos_duplicados_fuso.py aplicar")

    finally:
        cur.close()
        conn.close()


def fase_aplicar():
    imprimir_cabecalho("aplicar")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        pares = buscar_pares(cur)
        if not pares:
            print("\nNenhum par encontrado - nada a fazer.")
            cur.close()
            conn.close()
            return

        for par in pares:
            plano = decidir_merge(cur, par)
            print(f"\nMesclando #{plano['perdedora']} -> #{plano['sobrevivente']}:")
            mesclar(cur, plano["sobrevivente"], plano["perdedora"], plano["herdar_da_perdedora"], plano["dados"])

        conn.commit()
        print(f"\n✅ Concluído e commitado. {len(pares)} par(es) mesclado(s).")

    except Exception:
        conn.rollback()
        print("\n❌ Erro no meio da aplicação - rollback, nada foi gravado.")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("verificar", "aplicar"):
        sys.exit("Uso: python corrigir_jogos_duplicados_fuso.py [verificar|aplicar]")

    if sys.argv[1] == "verificar":
        fase_verificar()
    else:
        fase_aplicar()
