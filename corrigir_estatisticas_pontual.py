"""
Script PONTUAL (não faz parte do encadeamento diário do cron) - corrige os
2 jogos confirmados com estatística coletada no meio da partida, antes do
jogo ter terminado de verdade (a API-Football às vezes marca o fixture
como "FT" antes do endpoint de estatísticas terminar de agregar os números
finais - ver conversa de 16/08/2026 que descobriu isso a partir de uma
odd de escanteio marcada "errou" que na real tinha acertado).

Jogos afetados:
  - Athletico-PR x RB Bragantino, 15/08/2026 (fixture_id_api 1492330,
    2 linhas em `jogos`: id 34 e id 1492330, um por perspectiva de time
    rastreado)
  - Bahia x Corinthians, 26/07/2026 (fixture_id_api 1492301, 1 linha em
    `jogos`: id 1492301 - Bahia não é rastreado, só existe a perspectiva
    do Corinthians)

O que faz, em DUAS FASES SEPARADAS (rodar uma de cada vez, de propósito -
dá pra conferir a fase 1 contra um site de estatística antes de deixar a
fase 2 mexer em histórico/banca):

  FASE 1 (`estatisticas`):
    1. Apaga as linhas erradas de estatisticas_jogo e
       jogador_estatisticas_jogo desses jogos.
    2. Busca de novo na API-Football (estatística de time + de jogador,
       1 chamada de cada por fixture, nunca por jogo_id - evita gastar
       cota em dobro pro mesmo jogo real) e salva o valor final correto
       em CADA jogo_id ligado àquele fixture.
    3. Imprime o resumo pra conferência manual (compare com Sofascore/
       site de resultado antes de rodar a fase 2).

  FASE 2 (`reavaliar`):
    4. Reavalia (via avaliacao.avaliar_resultado, a mesma função de
       sempre) toda linha de historico_recomendacoes que dependia desses
       jogos - atualiza `resultado` só onde mudou, imprime cada mudança.
    5. Reavalia a aposta real #24 (Criador de Odd, usuário 1, R$0,50,
       múltipla de 2 pernas - uma delas era a perna afetada) e, se o
       resultado da múltipla mudou, credita a banca certinho (mesma
       fórmula de sempre: retorno = valor_apostado * (odd - 1)).

Rodar manualmente (uma vez cada fase, na ordem):
    python corrigir_estatisticas_pontual.py estatisticas
    ... conferir os números impressos contra Sofascore ...
    python corrigir_estatisticas_pontual.py reavaliar

Variáveis de ambiente necessárias (mesmas do popular_banco.py/app.py):
  - API_FOOTBALL_KEY
  - DATABASE_URL
"""

import sys
import psycopg2

from popular_banco import (
    chamar_api,
    buscar_estatisticas,
    buscar_estatisticas_jogadores,
    salvar_estatisticas,
    salvar_estatisticas_jogadores,
)
from avaliacao import avaliar_resultado
from app import registrar_movimento_banca

DATABASE_URL = __import__("os").environ["DATABASE_URL"]

# fixture_id_api -> lista de jogo_id que representam esse jogo real no banco
JOGOS_PARA_CORRIGIR = {
    1492330: [34, 1492330],   # Athletico-PR x RB Bragantino, 15/08/2026
    1492301: [1492301],       # Bahia x Corinthians, 26/07/2026
}

APOSTA_SALVA_PARA_REAVALIAR = 24  # usuário 1, R$0,50, múltipla de 2 pernas


# ---------- utilidades ----------

def buscar_home_team_id(cur, jogo_id):
    """Descobre o api_football_team_id do mandante desse jogo_id, direto do
    banco (não precisa de chamada de API extra pra isso - já temos o dado)."""
    cur.execute(
        """
        SELECT tm.api_football_team_id
        FROM jogos j JOIN times tm ON tm.id = j.mandante_id
        WHERE j.id = %s
        """,
        (jogo_id,),
    )
    row = cur.fetchone()
    if row is None or row[0] is None:
        raise RuntimeError(f"Não encontrei api_football_team_id do mandante do jogo_id {jogo_id}.")
    return row[0]


def imprimir_estatisticas_salvas(cur, jogo_id, rotulo):
    cur.execute(
        """SELECT lado, posse_de_bola, escanteios, faltas, passes, finalizacoes
           FROM estatisticas_jogo WHERE jogo_id = %s ORDER BY lado""",
        (jogo_id,),
    )
    linhas = cur.fetchall()
    print(f"  [{rotulo}] jogo_id {jogo_id}:")
    if not linhas:
        print("    (sem estatística de time salva)")
    for lado, posse, escanteios, faltas, passes, finalizacoes in linhas:
        print(f"    {lado}: posse={posse} escanteios={escanteios} faltas={faltas} "
              f"passes={passes} finalizacoes={finalizacoes}")


# ---------- FASE 1 ----------

def fase_estatisticas():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        for fixture_id, jogo_ids in JOGOS_PARA_CORRIGIR.items():
            print(f"\n===== Fixture {fixture_id} ({len(jogo_ids)} linha(s) em `jogos`: {jogo_ids}) =====")

            print("Estatística ANTES da correção:")
            for jogo_id in jogo_ids:
                imprimir_estatisticas_salvas(cur, jogo_id, "antes")

            # apaga o dado errado
            for jogo_id in jogo_ids:
                cur.execute("DELETE FROM estatisticas_jogo WHERE jogo_id = %s", (jogo_id,))
                cur.execute("DELETE FROM jogador_estatisticas_jogo WHERE jogo_id = %s", (jogo_id,))

            # busca uma vez só por fixture (não por jogo_id - evita gastar cota em dobro)
            estatisticas_time = buscar_estatisticas(fixture_id)
            estatisticas_jogadores = buscar_estatisticas_jogadores(fixture_id)

            if not estatisticas_time:
                raise RuntimeError(
                    f"A API-Football não devolveu estatística de time pro fixture {fixture_id} - "
                    "parando sem commitar, confere manualmente antes de rodar de novo."
                )

            # salva em CADA jogo_id ligado a esse fixture (mesmo padrão que
            # popular_banco.py já usa quando 2 times rastreados jogam entre si)
            for jogo_id in jogo_ids:
                home_team_id = buscar_home_team_id(cur, jogo_id)
                salvos_time = salvar_estatisticas(cur, jogo_id, estatisticas_time, home_team_id)
                salvos_jogador = salvar_estatisticas_jogadores(
                    cur, jogo_id, estatisticas_jogadores, home_team_id
                )
                print(f"  jogo_id {jogo_id}: {salvos_time} lado(s) de estatística de time, "
                      f"{salvos_jogador} jogador(es) salvos.")

            print("\nEstatística DEPOIS da correção:")
            for jogo_id in jogo_ids:
                imprimir_estatisticas_salvas(cur, jogo_id, "depois")

        conn.commit()
        print("\n✅ Fase 1 concluída e commitada. CONFIRA os números acima contra "
              "Sofascore/resultado real antes de rodar a fase 2 (`reavaliar`).")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Erro na fase 1, nada foi salvo (rollback): {e}")
        raise
    finally:
        cur.close()
        conn.close()


# ---------- FASE 2 ----------

def reavaliar_historico_recomendacoes(cur):
    todos_jogo_ids = [jid for jids in JOGOS_PARA_CORRIGIR.values() for jid in jids]

    cur.execute(
        """SELECT id, jogo_id, jogador_id, tipo_padrao, descricao, linha, direcao, resultado
           FROM historico_recomendacoes WHERE jogo_id = ANY(%s)""",
        (todos_jogo_ids,),
    )
    linhas = cur.fetchall()

    if not linhas:
        print("Nenhuma linha em historico_recomendacoes pra esses jogos (esperado pro jogo do Bahia).")
        return

    mudou = 0
    for rec_id, jogo_id, jogador_id, tipo_padrao, descricao, linha, direcao, resultado_antigo in linhas:
        novo_resultado = avaliar_resultado(cur, tipo_padrao, jogador_id, jogo_id, linha, descricao, direcao)
        if novo_resultado != resultado_antigo:
            cur.execute(
                "UPDATE historico_recomendacoes SET resultado = %s WHERE id = %s",
                (novo_resultado, rec_id),
            )
            print(f"  #{rec_id} [{tipo_padrao}] \"{descricao}\": "
                  f"{resultado_antigo} -> {novo_resultado}")
            mudou += 1

    print(f"\n{mudou} de {len(linhas)} linha(s) de historico_recomendacoes corrigida(s).")


def reavaliar_perna(cur, perna):
    """Mesma lógica de casamento de resolver_apostas_pendentes (app.py) -
    reaproveitada aqui pra não divergir. Cobre só o caso não-manual (com
    linha/direcao gravados), que é o caso da aposta #24."""
    if perna.get("fonte") == "manual":
        return avaliar_resultado(
            cur, perna["tipo_padrao"], perna.get("jogador_id"), perna["jogo_id"],
            perna.get("linha"), perna.get("descricao"), perna.get("direcao"),
        )

    cur.execute(
        """SELECT resultado FROM historico_recomendacoes
           WHERE jogo_id = %s AND tipo_padrao = %s
             AND jogador_id IS NOT DISTINCT FROM %s
             AND linha IS NOT DISTINCT FROM %s
             AND direcao IS NOT DISTINCT FROM %s
           ORDER BY id DESC LIMIT 1""",
        (perna["jogo_id"], perna["tipo_padrao"], perna.get("jogador_id"),
         perna.get("linha"), perna.get("direcao")),
    )
    row = cur.fetchone()
    return row[0] if row else "pendente"


def reavaliar_aposta_salva(cur):
    cur.execute(
        """SELECT id, usuario_id, resultado, retorno, valor_apostado, odd_combinada, pernas
           FROM apostas_salvas WHERE id = %s""",
        (APOSTA_SALVA_PARA_REAVALIAR,),
    )
    row = cur.fetchone()
    if row is None:
        print(f"Aposta #{APOSTA_SALVA_PARA_REAVALIAR} não encontrada - nada a fazer.")
        return

    aposta_id, usuario_id, resultado_antigo, retorno_antigo, valor_apostado, odd_combinada, pernas_json = row
    pernas = pernas_json if isinstance(pernas_json, list) else __import__("json").loads(pernas_json)

    resultados_pernas = [reavaliar_perna(cur, perna) for perna in pernas]
    print(f"Aposta #{aposta_id} - resultado por perna: {resultados_pernas}")

    if any(r == "errou" for r in resultados_pernas):
        resultado_novo = "errou"
    elif all(r == "acertou" for r in resultados_pernas):
        resultado_novo = "acertou"
    else:
        resultado_novo = "pendente"

    if resultado_novo == resultado_antigo:
        print(f"Resultado não mudou ({resultado_antigo}) - nada a corrigir na banca.")
        return

    print(f"Resultado da aposta #{aposta_id}: {resultado_antigo} -> {resultado_novo}")

    if resultado_novo == "acertou":
        retorno_novo = round(float(valor_apostado) * (float(odd_combinada) - 1), 2)
    elif resultado_novo == "errou":
        retorno_novo = round(-float(valor_apostado), 2)
    else:
        retorno_novo = None  # pendente - não mexe em retorno/banca ainda

    cur.execute(
        "UPDATE apostas_salvas SET resultado = %s, retorno = %s WHERE id = %s",
        (resultado_novo, retorno_novo, aposta_id),
    )

    # Ajuste de banca: desfaz o efeito do resultado antigo (se algum já
    # tinha sido creditado) e aplica o efeito do novo. Nesse caso específico
    # (#24 estava "errou", que não credita nada na resolução - o débito do
    # valor apostado já aconteceu no momento de salvar a aposta), só precisa
    # creditar se o novo resultado for "acertou".
    if resultado_antigo == "errou" and resultado_novo == "acertou":
        valor_credito = float(valor_apostado) + retorno_novo
        novo_saldo = registrar_movimento_banca(
            cur, usuario_id, "correcao_retroativa", valor_credito, aposta_id
        )
        print(f"  Banca do usuário {usuario_id} creditada em R$ {valor_credito:.2f} "
              f"(saldo agora: R$ {novo_saldo:.2f}).")
    else:
        print("  Combinação de resultado antigo/novo não prevista neste script - "
              "NENHUM ajuste de banca foi feito automaticamente. Confira manualmente.")


def fase_reavaliar():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        print("===== Reavaliando historico_recomendacoes =====")
        reavaliar_historico_recomendacoes(cur)

        print("\n===== Reavaliando aposta salva #24 =====")
        reavaliar_aposta_salva(cur)

        conn.commit()
        print("\n✅ Fase 2 concluída e commitada.")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Erro na fase 2, nada foi salvo (rollback): {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("estatisticas", "reavaliar"):
        print(__doc__)
        sys.exit(1)

    if sys.argv[1] == "estatisticas":
        fase_estatisticas()
    else:
        fase_reavaliar()
