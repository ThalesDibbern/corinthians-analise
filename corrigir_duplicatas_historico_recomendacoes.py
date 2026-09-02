"""
Correção pontual, 2 fases (verificar / aplicar): junta as duplicatas que já
existem em `historico_recomendacoes` pros mercados de MERCADOS_JOGO_INTEIRO
(resultado_final, dupla_chance_1t/2t, ambas_marcam_1t/2t, escanteio_total,
cartao_total, gols_total) - a pendência 54 da seção 32-F.

Por que isso existe: o dedup de `arquivar_recomendacoes.py` (seção 50,
30-31/08/2026) só vale pra ARQUIVAMENTOS NOVOS. Linhas que já estavam em
`historico_recomendacoes` de antes da correção continuam duplicadas - uma
por perspectiva do mesmo evento real - contando 2x na tabela de calibração.
Confirmado em 01/09/2026 com a rodada de 29-31/08: mesmo jogos arquivados
DEPOIS da correção entrar no ar (ex: Cruzeiro x Vasco) saíram duplicados,
porque a linha já tinha sido movida de `recomendacoes` pra
`historico_recomendacoes` antes do deploy pegar.

MESMA lógica de identidade e de resolução de `arquivar_recomendacoes.py`
(chave estruturada, representante = menor id, probabilidade = MÉDIA das
cópias, VE recalculado) - reaproveitada aqui de propósito, não duplicada
com desvio.

⚠️ PROTEÇÃO que arquivar_recomendacoes.py não precisa ter (porque ele nunca
lida com aposta já salva - essas linhas ainda nem existiam quando uma
aposta poderia ter sido feita em cima delas): antes de apagar a cópia
"perdedora" de um grupo, checa se o `jogo_id` dela aparece em alguma perna
de `apostas_salvas` (`avaliar_pernas_aposta` casa pela EXATA jogo_id salva
- apagar essa linha quebraria a avaliação dessa aposta pra sempre). Se
aparecer, o grupo NÃO é apagado - só sincroniza a probabilidade/VE nas
duas linhas (mais conservador, mas nunca quebra uma aposta).

Escopo: HISTÓRICO INTEIRO, não só a rodada de 29-31/08 - é a mesma
correção que a pendência 54 já pedia.

Uso:
  python corrigir_duplicatas_historico_recomendacoes.py verificar
  python corrigir_duplicatas_historico_recomendacoes.py aplicar
"""

import os
import sys
from datetime import datetime

import psycopg2

from combinacoes import MERCADOS_JOGO_INTEIRO

DATABASE_URL = os.environ["DATABASE_URL"]


def cabecalho(fase):
    print("=" * 78)
    print(f"corrigir_duplicatas_historico_recomendacoes.py | fase: {fase}")
    print(f"argv: {sys.argv}")
    print(f"relógio: {datetime.now().isoformat()}")
    print("=" * 78)


def buscar_grupos_duplicados(cur):
    """Agrupa historico_recomendacoes pela MESMA chave estruturada que
    arquivar_recomendacoes.py usa, só pra MERCADOS_JOGO_INTEIRO (mercado de
    jogador fica fora por enquanto - identidade e risco são diferentes,
    trata em separado se aparecer necessidade)."""
    cur.execute(
        """SELECT h.id, h.jogo_id, j.fixture_id_api, h.tipo_padrao, h.linha,
                  h.direcao, h.casa_aposta, h.probabilidade_historica,
                  h.odd_oferecida, h.valor_esperado, h.resultado, h.descricao
           FROM historico_recomendacoes h
           JOIN jogos j ON j.id = h.jogo_id
           WHERE h.tipo_padrao = ANY(%s)
             AND h.resultado IN ('acertou', 'errou')
           ORDER BY j.fixture_id_api, h.tipo_padrao, h.id""",
        (list(MERCADOS_JOGO_INTEIRO),),
    )
    linhas = cur.fetchall()

    grupos = {}
    for row in linhas:
        (rec_id, jogo_id, fixture_id_api, tipo_padrao, linha, direcao,
         casa, prob, odd, ve, resultado, descricao) = row
        chave = (fixture_id_api, tipo_padrao, linha, direcao, casa)
        grupos.setdefault(chave, []).append({
            "id": rec_id, "jogo_id": jogo_id, "prob": float(prob), "odd": float(odd),
            "ve": float(ve), "resultado": resultado, "descricao": descricao,
        })

    return {k: v for k, v in grupos.items() if len(v) > 1}


def jogo_id_tem_aposta(cur, jogo_id):
    cur.execute(
        """SELECT EXISTS (
               SELECT 1 FROM apostas_salvas a, jsonb_array_elements(a.pernas) AS perna
               WHERE (perna->>'jogo_id')::int = %s
           )""",
        (jogo_id,),
    )
    return cur.fetchone()[0]


def fase_verificar():
    cabecalho("verificar (só leitura, nenhuma escrita)")
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    grupos = buscar_grupos_duplicados(cur)
    print(f"\n{len(grupos)} grupos com duplicata encontrados.\n")

    seguros, protegidos = 0, 0
    resultados_divergentes = 0

    for chave, recs in grupos.items():
        fixture_id_api, tipo_padrao, linha, direcao, casa = chave
        resultados = {r["resultado"] for r in recs}
        if len(resultados) > 1:
            resultados_divergentes += 1
            print(f"  [!! ATENÇÃO] fixture={fixture_id_api} {tipo_padrao} direcao={direcao} - "
                  f"as cópias têm RESULTADO DIFERENTE ({resultados}) - não deveria acontecer "
                  f"(mesmo evento real). Pulando esse grupo, precisa olhar na mão.")
            continue

        tem_aposta = any(jogo_id_tem_aposta(cur, r["jogo_id"]) for r in recs)
        probs = [r["prob"] for r in recs]
        prob_media = round(sum(probs) / len(probs), 2)

        if tem_aposta:
            protegidos += 1
            acao = "SINCRONIZA (protegido por aposta salva)"
        else:
            seguros += 1
            acao = "APAGA duplicata(s), mantém representante"

        print(f"  fixture={fixture_id_api} {tipo_padrao} direcao={direcao} | "
              f"{len(recs)} cópias, probs={probs} -> média={prob_media} | {acao}")

    print(f"\nResumo: {seguros} grupos seguros pra apagar, {protegidos} protegidos "
          f"(só sincroniza), {resultados_divergentes} com resultado divergente (pulados, "
          f"precisam de revisão manual).")
    print("Nenhuma escrita foi feita. Rode com 'aplicar' pra gravar de verdade.")

    cur.close()
    conn.close()


def fase_aplicar():
    cabecalho("aplicar (vai apagar duplicata segura e sincronizar as protegidas)")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        grupos = buscar_grupos_duplicados(cur)
        apagadas, sincronizadas, pulados = 0, 0, 0

        for chave, recs in grupos.items():
            fixture_id_api, tipo_padrao, linha, direcao, casa = chave
            resultados = {r["resultado"] for r in recs}
            if len(resultados) > 1:
                pulados += 1
                print(f"  [PULADO] fixture={fixture_id_api} {tipo_padrao} - resultado divergente entre cópias.")
                continue

            tem_aposta = any(jogo_id_tem_aposta(cur, r["jogo_id"]) for r in recs)
            probs = [r["prob"] for r in recs]
            prob_media = round(sum(probs) / len(probs), 2)
            representante = min(recs, key=lambda r: r["id"])
            odd = representante["odd"]
            ve_recalculado = round((prob_media / 100 * odd) - 1, 3)

            if tem_aposta:
                for r in recs:
                    cur.execute(
                        "UPDATE historico_recomendacoes SET probabilidade_historica = %s, valor_esperado = %s WHERE id = %s",
                        (prob_media, ve_recalculado, r["id"]),
                    )
                sincronizadas += 1
                print(f"  [SINCRONIZADO] fixture={fixture_id_api} {tipo_padrao} direcao={direcao} - "
                      f"{len(recs)} linhas mantidas (protegidas por aposta), prob agora {prob_media}%.")
            else:
                cur.execute(
                    "UPDATE historico_recomendacoes SET probabilidade_historica = %s, valor_esperado = %s WHERE id = %s",
                    (prob_media, ve_recalculado, representante["id"]),
                )
                ids_apagar = [r["id"] for r in recs if r["id"] != representante["id"]]
                cur.execute(
                    "DELETE FROM historico_recomendacoes WHERE id = ANY(%s)",
                    (ids_apagar,),
                )
                apagadas += len(ids_apagar)
                print(f"  [DEDUPLICADO] fixture={fixture_id_api} {tipo_padrao} direcao={direcao} - "
                      f"mantido id={representante['id']} (prob {prob_media}%), apagados {ids_apagar}.")

        conn.commit()
        print(f"\n{apagadas} linhas duplicadas apagadas, {sincronizadas} grupos sincronizados "
              f"sem apagar (protegidos por aposta), {pulados} pulados por resultado divergente.")
        print("Commitado. Rodar refazer_tabela_calibracao.py de novo em seguida, "
              "já que o N da tabela mudou.")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a aplicação, rollback feito: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("verificar", "aplicar"):
        print("Uso: python corrigir_duplicatas_historico_recomendacoes.py [verificar|aplicar]")
        sys.exit(1)

    if sys.argv[1] == "verificar":
        fase_verificar()
    else:
        fase_aplicar()
