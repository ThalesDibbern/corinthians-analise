"""
simular_correcao_correlacao.py

Fase 1 do plano (item 1.11, seção 67) - responde a Q3 de
`arquitetura_correcao_correlacao.md` ("qual o impacto simulado?"):
backtest só-leitura do fator de correlação CORRIGIDO (razão de chances
via Poisson, grupo vs base, direção certa - seções 3.1 a 3.4-B do
documento) contra recomendações já avaliadas. Mesmo formato do
`simular_correcao_divergencia_ve.py`, que validou o encolhimento
(seção 61) antes dele subir - nenhuma linha de produção é tocada aqui,
nenhum dado é gravado.

ESCOPO AMPLIADO em relação ao desenho original: o documento de
arquitetura (seção 4) só cobria `escanteio_total`, com `cartao_total`
como grupo de controle. O achado da seção 71 (08/09/2026) encontrou a
MESMA assinatura de erro de unidade em `cartao_total`, com N maior -
então este script mede os dois mercados juntos. `cartao` (cartão de
TIME) entra só como diagnóstico: a arquitetura marca esse mercado como
FORA de escopo (construção diferente, ruim nos dois lados, precisa de
investigação própria - seção 5.5) e este script não propõe fórmula
corrigida pra ele, só reproduz o bucket pelo fator atual.

MÉTODO
------
Para cada recomendação fechada (acertou/errou) de escanteio_total e
cartao_total:
  1. Recalcula o fator ATUAL (o que a produção aplicou), chamando as
     MESMAS funções de motor_recomendacoes.py (buscar_fator_correlacao),
     não uma reimplementação - garante que a lógica bate com a que está
     no ar.
  2. Recalcula o fator CORRIGIDO: razão de chances via Poisson, grupo
     (valor_b do lado que esse jogo caiu) contra a BASE (media_b da
     liga) - não grupo contra grupo, que é a comparação errada que o
     próprio documento corrigiu (seção 3.4-B). Direção "menos" usa a
     recíproca do fator de "mais" no mesmo deslocamento de lambda,
     porque P(menos) = 1 - P(mais) (seção 3.2). Piso 0.50 / teto 2.00
     (seção 3.5).
  3. Reclassifica as recomendações pelos dois fatores (atual e
     corrigido) e imprime observado/previsto/gap por bucket nos dois
     casos, lado a lado.

LIMITAÇÃO CONHECIDA (registrar, não esconder)
----------------------------------------------
`padroes_correlacao_estatisticas` é sobrescrita todo dia pelo
`motor_padroes.py` - este script lê o snapshot de HOJE, não o valor
exato do dia em que cada recomendação foi gerada. Pra escanteio_total
(onde a correlação é o ÚNICO fator que entra em `combinar_fatores`, ver
seção 4 de motor_recomendacoes.py), o script checa isso na prática:
compara o fator recalculado contra o "fator combinado" que já está
gravado no texto da própria recomendação, e avisa se divergir.

Uso:
    python simular_correcao_correlacao.py

Só lê. Não grava nada - não tem fase verificar/aplicar porque não há o
que aplicar aqui.
"""
import os
import re
import sys
import math
from collections import defaultdict
from datetime import datetime, timezone

import psycopg2

import motor_recomendacoes as mr

FASE = "simular_correcao_correlacao (Q3 da arquitetura / item 1.11 do plano)"


def cabecalho():
    print("=" * 78)
    print(f"FASE: {FASE}")
    print(f"argv: {sys.argv}")
    print(f"relógio (UTC): {datetime.now(timezone.utc).isoformat()}")
    print("Somente leitura - nenhuma escrita no banco nesta execução.")
    print("=" * 78)


# ========================= Poisson sem scipy =========================

def poisson_cdf(k, lam):
    """P(X <= k) para X ~ Poisson(lam), k inteiro >= 0. Implementado à
    mão (soma direta dos termos da PMF) pra não depender de scipy, que
    pode não estar instalado no serviço."""
    if lam is None or lam <= 0:
        return 1.0
    termo = math.exp(-lam)
    soma = termo
    for i in range(1, k + 1):
        termo *= lam / i
        soma += termo
    return min(soma, 1.0)


def poisson_p_mais(linha, lam):
    """P(X > linha). `linha` é tipicamente uma linha de meio gol/cartão/
    escanteio (7.5, 8.5, ...), então P(X > 7.5) = 1 - P(X <= 7)."""
    k = int(math.floor(float(linha)))
    return max(0.0, min(1.0, 1.0 - poisson_cdf(k, lam)))


def chance(p):
    p = min(max(p, 1e-9), 1 - 1e-9)
    return p / (1 - p)


PISO_FATOR_LINHA = 0.50
TETO_FATOR_LINHA = 2.00


def fator_corrigido_poisson(linha, media_b, valor_b_lado, direcao_norm):
    """Razão de CHANCES da linha específica, grupo (valor_b_lado) contra
    a BASE (media_b) - seção 3.4-B da arquitetura, não grupo-vs-grupo.
    Direção 'menos' é a recíproca de 'mais' no mesmo deslocamento de
    lambda (seção 3.2). Piso/teto da seção 3.5."""
    p_base = poisson_p_mais(linha, media_b)
    p_grupo = poisson_p_mais(linha, valor_b_lado)
    razao_mais = chance(p_grupo) / chance(p_base)
    razao = razao_mais if direcao_norm == "mais" else (1.0 / razao_mais)
    return max(PISO_FATOR_LINHA, min(TETO_FATOR_LINHA, razao))


# ============ Parte 1: snapshot de hoje, referência rápida ============

def parte1_snapshot(cur):
    print("\n" + "-" * 78)
    print("PARTE 1 - snapshot de HOJE em padroes_correlacao_estatisticas")
    print("(referência - compare com a tabela da seção 3.4-B da arquitetura,")
    print(" medida em 04/09/2026, pra ver o quanto a base andou desde lá)")
    print("-" * 78)

    for par in ("chutes_escanteios", "faltas_cartoes"):
        dados = mr.buscar_correlacao_liga(cur, par)
        if dados is None:
            print(f"\n[{par}] sem dado suficiente hoje (amostra abaixo do piso).")
            continue
        media_a, valor_b_acima, valor_b_abaixo, media_b = dados
        print(f"\n[{par}]")
        print(f"  media_a={media_a:.3f}  media_b={media_b:.3f}  "
              f"valor_b_abaixo={valor_b_abaixo:.3f}  valor_b_acima={valor_b_acima:.3f}")
        print(f"  fator ATUAL (razão de contagens, o que a produção aplica): "
              f"abaixo={valor_b_abaixo / media_b:.3f}  acima={valor_b_acima / media_b:.3f}")

        print(f"\n  {'linha':>6} | {'Poisson abaixo':>15} | {'Poisson acima':>15} | "
              f"{'razão chances abaixo':>21} | {'razão chances acima':>20}")
        for linha in (6.5, 7.5, 8.5, 9.5, 10.5, 11.5, 12.5):
            p_base = poisson_p_mais(linha, media_b)
            p_ab = poisson_p_mais(linha, valor_b_abaixo)
            p_ac = poisson_p_mais(linha, valor_b_acima)
            r_ab = max(PISO_FATOR_LINHA, min(TETO_FATOR_LINHA, chance(p_ab) / chance(p_base)))
            r_ac = max(PISO_FATOR_LINHA, min(TETO_FATOR_LINHA, chance(p_ac) / chance(p_base)))
            print(f"  {linha:>6} | {p_ab * 100:>14.1f}% | {p_ac * 100:>14.1f}% | "
                  f"{r_ab:>21.3f} | {r_ac:>20.3f}")


# ============ Parte 2: reclassificação das recomendações fechadas ============

MERCADOS_ESCOPO = {
    "escanteio_total": ("chutes_escanteios", "finalizacoes"),
    "cartao_total": ("faltas_cartoes", "faltas"),
}

PADRAO_FATOR_COMBINADO = re.compile(r"fator combinado ([\d.]+)x")


def bucket_do_fator(fator):
    if fator is None:
        return None
    if fator <= 0.97:
        return "pra baixo (<=0.97)"
    if fator >= 1.03:
        return "pra cima (>=1.03)"
    return "neutro (0.97-1.03)"


def recalcular_mercado(cur, tipo_padrao, par, coluna_a):
    cur.execute(
        """
        SELECT h.id, h.linha, h.direcao, h.resultado, h.probabilidade_historica,
               h.descricao, j.mandante_id, j.visitante_id, j.fixture_id_api
        FROM historico_recomendacoes h
        JOIN jogos j ON j.id = h.jogo_id
        WHERE h.tipo_padrao = %s
          AND h.jogador_id IS NULL
          AND h.resultado IN ('acertou', 'errou')
          AND h.linha IS NOT NULL
          AND h.direcao IS NOT NULL
        """,
        (tipo_padrao,),
    )
    linhas = cur.fetchall()

    dados_par = mr.buscar_correlacao_liga(cur, par)
    if dados_par is None:
        print(f"[{tipo_padrao}] sem dado em padroes_correlacao_estatisticas pro par {par}.")
        return []
    media_a, valor_b_acima, valor_b_abaixo, media_b = dados_par

    resultados = []
    sem_media_time = 0

    for (rec_id, linha, direcao, resultado, prob_historica, descricao,
         mandante_id, visitante_id, fixture_id_api) in linhas:

        direcao_norm = (direcao or "").strip().lower()
        if direcao_norm not in ("mais", "menos"):
            continue
        if mandante_id is None or visitante_id is None:
            continue

        fator_atual = mr.buscar_fator_correlacao(cur, tipo_padrao, mandante_id, visitante_id)
        if fator_atual is None:
            sem_media_time += 1
            continue

        # Mesma estimativa de A que buscar_fator_correlacao usa
        # internamente, recalculada aqui só pra saber de que LADO
        # (acima/abaixo) esse jogo caiu, e então usar o valor_b certo
        # na fórmula de Poisson.
        media_mandante = mr.buscar_media_estatistica_time(cur, mandante_id, coluna_a)
        media_visitante = mr.buscar_media_estatistica_time(cur, visitante_id, coluna_a)
        if media_mandante is None or media_visitante is None:
            sem_media_time += 1
            continue
        a_estimado = media_mandante + media_visitante
        valor_b_lado = valor_b_acima if a_estimado > media_a else valor_b_abaixo

        fator_corr = fator_corrigido_poisson(float(linha), media_b, valor_b_lado, direcao_norm)

        divergencia_stock = None
        if tipo_padrao == "escanteio_total":
            # Único mercado onde a correlação é o ÚNICO fator que entra
            # em combinar_fatores (ver linha ~2621 de motor_recomendacoes.py)
            # - então "fator combinado" gravado na descrição É o fator de
            # correlação puro, e dá pra conferir contra o que recalculei.
            m = PADRAO_FATOR_COMBINADO.search(descricao or "")
            if m:
                divergencia_stock = round(fator_atual - float(m.group(1)), 4)

        resultados.append({
            "id": rec_id,
            "fixture_id_api": fixture_id_api,
            "resultado": resultado,
            "previsto": float(prob_historica),
            "fator_atual": fator_atual,
            "fator_corrigido": fator_corr,
            "divergencia_stock": divergencia_stock,
        })

    if sem_media_time:
        print(f"[{tipo_padrao}] {sem_media_time} recomendação(ões) fechada(s) ficaram fora da "
              f"amostra por falta de média própria de algum dos dois times hoje.")

    return resultados


def recalcular_cartao_time(cur):
    """Só diagnóstico - a arquitetura marca esse mercado como fora de
    escopo (construção diferente, sem fórmula corrigida proposta ainda).
    Reproduz o bucket pelo fator ATUAL, nada mais."""
    cur.execute(
        """
        SELECT h.id, h.resultado, h.probabilidade_historica,
               j.mandante_id, j.visitante_id, j.nosso_time_id, j.fixture_id_api
        FROM historico_recomendacoes h
        JOIN jogos j ON j.id = h.jogo_id
        WHERE h.tipo_padrao = 'cartao'
          AND h.jogador_id IS NULL
          AND h.resultado IN ('acertou', 'errou')
        """
    )
    linhas = cur.fetchall()

    resultados = []
    for (rec_id, resultado, prob_historica, mandante_id, visitante_id,
         nosso_time_id, fixture_id_api) in linhas:
        if nosso_time_id is None or mandante_id is None or visitante_id is None:
            continue
        adversario_id = visitante_id if nosso_time_id == mandante_id else mandante_id
        fator_atual = mr.buscar_fator_correlacao_time(cur, "faltas_cartoes", adversario_id)
        if fator_atual is None:
            continue
        resultados.append({
            "id": rec_id,
            "fixture_id_api": fixture_id_api,
            "resultado": resultado,
            "previsto": float(prob_historica),
            "fator_atual": fator_atual,
        })
    return resultados


def imprimir_tabela(titulo, resultados, chave_bucket):
    grupos = defaultdict(list)
    for r in resultados:
        b = bucket_do_fator(r.get(chave_bucket))
        if b is None:
            continue
        grupos[b].append(r)

    print(f"\n  {titulo}")
    print(f"  {'bucket':<22} | {'N':>4} | {'jogos':>5} | {'observado':>10} | "
          f"{'previsto':>9} | {'gap':>7}")
    ordem = ["pra baixo (<=0.97)", "neutro (0.97-1.03)", "pra cima (>=1.03)"]
    algum = False
    for b in ordem:
        rs = grupos.get(b, [])
        if not rs:
            continue
        algum = True
        n = len(rs)
        jogos = len({r["fixture_id_api"] for r in rs})
        acertos = sum(1 for r in rs if r["resultado"] == "acertou")
        observado = 100.0 * acertos / n
        previsto = sum(r["previsto"] for r in rs) / n
        gap = observado - previsto
        print(f"  {b:<22} | {n:>4} | {jogos:>5} | {observado:>9.1f}% | "
              f"{previsto:>8.1f}% | {gap:>+6.1f}")
    if not algum:
        print("  (sem recomendações nessa classificação)")


def main():
    cabecalho()
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    try:
        parte1_snapshot(cur)

        print("\n" + "-" * 78)
        print("PARTE 2 - reclassificando recomendações FECHADAS pelo fator ATUAL")
        print("(o que a produção aplicou) vs. pelo fator CORRIGIDO (Poisson,")
        print("grupo vs base, direção certa - proposto na arquitetura)")
        print("-" * 78)

        for tipo_padrao, (par, coluna_a) in MERCADOS_ESCOPO.items():
            resultados = recalcular_mercado(cur, tipo_padrao, par, coluna_a)

            divergencias = [r["divergencia_stock"] for r in resultados
                             if r["divergencia_stock"] is not None]
            if divergencias:
                maior = max(abs(d) for d in divergencias)
                print(f"\n[{tipo_padrao}] checagem de consistência: {len(divergencias)} "
                      f"recomendação(ões) comparada(s) contra o fator já gravado na "
                      f"descrição. Maior divergência: {maior:.4f}.")
                if maior > 0.02:
                    print("  ⚠️ divergência acima de 0.02 - a base (padroes_correlacao_estatisticas")
                    print("  e/ou a média dos times) mudou desde que essas recomendações foram")
                    print("  geradas. Os números abaixo usam o SNAPSHOT DE HOJE, não o do dia da")
                    print("  recomendação - leia com essa ressalva.")

            print(f"\n=== {tipo_padrao} ({len(resultados)} recomendação(ões) na amostra) ===")
            if not resultados:
                print("  (sem recomendações fechadas suficientes pra medir)")
                continue
            imprimir_tabela("Bucket pelo fator ATUAL (produção)", resultados, "fator_atual")
            imprimir_tabela("Bucket pelo fator CORRIGIDO (Poisson, proposto)", resultados, "fator_corrigido")

        print("\n" + "-" * 78)
        print("DIAGNÓSTICO (não entra na decisão) - cartão de TIME")
        print("A arquitetura marca esse mercado como FORA de escopo (seções 4 e 5.5):")
        print("construção diferente (só o adversário, média por time), ruim nos dois")
        print("lados, precisa de investigação própria. Só reproduz o bucket pelo fator")
        print("ATUAL - nenhuma fórmula corrigida é proposta aqui pra esse mercado.")
        print("-" * 78)
        resultados_time = recalcular_cartao_time(cur)
        print(f"\n=== cartao (time) ({len(resultados_time)} recomendação(ões) na amostra) ===")
        if resultados_time:
            imprimir_tabela("Bucket pelo fator ATUAL (produção)", resultados_time, "fator_atual")
        else:
            print("  (sem recomendações fechadas suficientes pra medir)")

        print("\n" + "=" * 78)
        print("Fim. Nenhuma linha de código de produção foi tocada, nada foi gravado.")
        print("Isto responde a Q3 da arquitetura (seção 5.1 do documento) / item 1.11")
        print("do plano (seção 67). A decisão de implementar (Fase 5) continua em")
        print("aberto por regra do próprio projeto - ver os números acima antes de")
        print("decidir, e comparar o critério de sucesso da seção 8 do documento:")
        print("  - gap do grupo 'pra baixo' sai de −25,9 pra dentro de ±10?")
        print("  - gap do grupo 'pra cima' não piora?")
        print("=" * 78)

    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
