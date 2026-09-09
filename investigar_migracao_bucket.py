"""
investigar_migracao_bucket.py

Segue-a-dor do `simular_correcao_correlacao.py`. O resultado daquele
script levantou um problema novo: a fórmula corrigida conserta o
bucket "pra baixo" nos dois mercados, mas piora o bucket "pra cima" -
principalmente em cartao_total (+6,8 -> -16,6). Este script NÃO propõe
mais nenhuma fórmula. Só quebra o "pra cima" corrigido em fatias, uma
variável de cada vez, pra achar ONDE a piora mora - princípio do
projeto: "quando as hipóteses se acumulam sem convergir, trocar de
método: congelar uma variável por vez".

HIPÓTESE A TESTAR (não assumida - só levantada por leitura do código,
precisa de dado pra confirmar ou derrubar)
----------------------------------------------------------------------
`buscar_fator_correlacao` (a função de PRODUÇÃO) não recebe `direcao`
como parâmetro - ela devolve o mesmo fator pra uma recomendação "mais"
e pra uma "menos" do mesmo jogo. Isso é o BUG #2 que a própria
arquitetura já documentava (seção 3.2: "o mesmo fator é aplicado a
'mais' e a 'menos', quando os efeitos são opostos"). A fórmula
corrigida deste backtest CORRIGE isso (usa a recíproca pra "menos").

Consequência aritmética: uma recomendação "menos" cujo `a_estimado`
caiu do lado ABAIXO da média (fator bruto < 1, ex. 0.88) tinha, pelo
fator ATUAL (bugado), ido pro bucket "pra baixo". Corrigindo a
direção, o fator vira a recíproca (~1/0.88 ≈ 1.6-2.0, quase sempre
batendo no TETO_FATOR_LINHA=2.00) - migrando pro bucket "pra cima".

Se essa hipótese for a causa raiz, o "pra cima" corrigido deveria ter
MUITO mais "menos" que "mais" dentro do grupo migrado, e boa parte dos
fatores corrigidos ali deveria estar colado no teto (2.00) - um sinal
de que o problema não é a direção em si, mas o TAMANHO do salto (o
piso/teto da seção 3.5 pode estar calibrado pro regime de escanteio,
que tem média ~10, e ser agressivo demais pro regime de cartão, que
tem média ~5.4 - a mesma linha de meio-gol/cartão pesa mais numa
distribuição de Poisson com lambda menor).

MÉTODO
------
Reusa as mesmas funções (Poisson e mr.*) do script anterior - mesma
lógica, mesmos números, só reorganizados por fatia:

  PARTE A - matriz de migração (bucket ATUAL x bucket CORRIGIDO), com
            observado/previsto/gap por célula. Isola se o problema
            está concentrado no grupo que MUDOU de bucket ou se é
            geral.
  PARTE B - dentro do "pra cima" corrigido, quebra por DIREÇÃO
            (mais/menos) - testa a hipótese acima diretamente.
  PARTE C - dentro do "pra cima" corrigido, quebra por distância até o
            teto (colado no teto >=1.90 vs longe do teto <1.90) -
            testa se é o clamp que está causando o problema.
  PARTE D - amostra de até 25 linhas individuais do grupo migrado
            (baixo->cima), pra inspeção manual.

Só leitura. Não grava nada.

Uso:
    python investigar_migracao_bucket.py
"""
import os
import sys
import math
from collections import defaultdict
from datetime import datetime, timezone

import psycopg2

import motor_recomendacoes as mr

FASE = "investigar_migracao_bucket (segue-a-dor do item 1.11)"


def cabecalho():
    print("=" * 78)
    print(f"FASE: {FASE}")
    print(f"argv: {sys.argv}")
    print(f"relógio (UTC): {datetime.now(timezone.utc).isoformat()}")
    print("Somente leitura - nenhuma escrita no banco nesta execução.")
    print("=" * 78)


# ========================= Poisson sem scipy (idêntico ao script anterior) =========================

def poisson_cdf(k, lam):
    if lam is None or lam <= 0:
        return 1.0
    termo = math.exp(-lam)
    soma = termo
    for i in range(1, k + 1):
        termo *= lam / i
        soma += termo
    return min(soma, 1.0)


def poisson_p_mais(linha, lam):
    k = int(math.floor(float(linha)))
    return max(0.0, min(1.0, 1.0 - poisson_cdf(k, lam)))


def chance(p):
    p = min(max(p, 1e-9), 1 - 1e-9)
    return p / (1 - p)


PISO_FATOR_LINHA = 0.50
TETO_FATOR_LINHA = 2.00


def fator_corrigido_poisson(linha, media_b, valor_b_lado, direcao_norm):
    p_base = poisson_p_mais(linha, media_b)
    p_grupo = poisson_p_mais(linha, valor_b_lado)
    razao_mais = chance(p_grupo) / chance(p_base)
    razao_bruta = razao_mais if direcao_norm == "mais" else (1.0 / razao_mais)
    razao_final = max(PISO_FATOR_LINHA, min(TETO_FATOR_LINHA, razao_bruta))
    return razao_final, razao_bruta


def bucket_do_fator(fator):
    if fator is None:
        return None
    if fator <= 0.97:
        return "pra baixo"
    if fator >= 1.03:
        return "pra cima"
    return "neutro"


MERCADOS_ESCOPO = {
    "escanteio_total": ("chutes_escanteios", "finalizacoes"),
    "cartao_total": ("faltas_cartoes", "faltas"),
}


def coletar(cur, tipo_padrao, par, coluna_a):
    cur.execute(
        """
        SELECT h.id, h.linha, h.direcao, h.resultado, h.probabilidade_historica,
               j.mandante_id, j.visitante_id, j.fixture_id_api
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
    for (rec_id, linha, direcao, resultado, prob_historica,
         mandante_id, visitante_id, fixture_id_api) in linhas:

        direcao_norm = (direcao or "").strip().lower()
        if direcao_norm not in ("mais", "menos"):
            continue
        if mandante_id is None or visitante_id is None:
            continue

        fator_atual = mr.buscar_fator_correlacao(cur, tipo_padrao, mandante_id, visitante_id)
        if fator_atual is None:
            continue

        media_mandante = mr.buscar_media_estatistica_time(cur, mandante_id, coluna_a)
        media_visitante = mr.buscar_media_estatistica_time(cur, visitante_id, coluna_a)
        if media_mandante is None or media_visitante is None:
            continue
        a_estimado = media_mandante + media_visitante
        lado = "acima" if a_estimado > media_a else "abaixo"
        valor_b_lado = valor_b_acima if lado == "acima" else valor_b_abaixo

        fator_corr, fator_corr_bruto = fator_corrigido_poisson(
            float(linha), media_b, valor_b_lado, direcao_norm
        )

        resultados.append({
            "id": rec_id,
            "fixture_id_api": fixture_id_api,
            "resultado": resultado,
            "previsto": float(prob_historica),
            "linha": float(linha),
            "direcao": direcao_norm,
            "lado": lado,
            "fator_atual": fator_atual,
            "fator_corrigido": fator_corr,
            "fator_corrigido_bruto": fator_corr_bruto,
            "colado_no_teto": fator_corr >= 1.90,
            "colado_no_piso": fator_corr <= 0.55,
        })

    return resultados


def stats(rs):
    if not rs:
        return 0, 0, 0.0, 0.0, 0.0
    n = len(rs)
    jogos = len({r["fixture_id_api"] for r in rs})
    acertos = sum(1 for r in rs if r["resultado"] == "acertou")
    observado = 100.0 * acertos / n
    previsto = sum(r["previsto"] for r in rs) / n
    gap = observado - previsto
    return n, jogos, observado, previsto, gap


def imprimir_linha(rotulo, rs, largura=28):
    n, jogos, observado, previsto, gap = stats(rs)
    if n == 0:
        print(f"  {rotulo:<{largura}} |    - |     - |          - |         - |       -")
        return
    print(f"  {rotulo:<{largura}} | {n:>4} | {jogos:>5} | {observado:>9.1f}% | "
          f"{previsto:>8.1f}% | {gap:>+6.1f}")


def cabecalho_tabela(largura=28):
    print(f"  {'':<{largura}} | {'N':>4} | {'jogos':>5} | {'observado':>10} | "
          f"{'previsto':>9} | {'gap':>7}")


def parte_a_matriz(tipo_padrao, resultados):
    print(f"\n  PARTE A - matriz de migração ({tipo_padrao})")
    matriz = defaultdict(list)
    for r in resultados:
        ba = bucket_do_fator(r["fator_atual"])
        bc = bucket_do_fator(r["fator_corrigido"])
        matriz[(ba, bc)].append(r)

    cabecalho_tabela(34)
    for ba in ("pra baixo", "neutro", "pra cima"):
        for bc in ("pra baixo", "neutro", "pra cima"):
            rs = matriz.get((ba, bc), [])
            if not rs:
                continue
            rotulo = f"atual={ba} -> corrigido={bc}"
            imprimir_linha(rotulo, rs, 34)
    total_migrado_baixo_cima = matriz.get(("pra baixo", "pra cima"), [])
    total_ficou_cima = matriz.get(("pra cima", "pra cima"), [])
    print(f"\n  Migraram pra baixo->cima: {len(total_migrado_baixo_cima)} | "
          f"já estavam em pra cima (ficaram): {len(total_ficou_cima)}")
    return total_migrado_baixo_cima, total_ficou_cima


def parte_b_direcao(tipo_padrao, cima_corrigido):
    print(f"\n  PARTE B - grupo 'pra cima' (corrigido) quebrado por DIREÇÃO ({tipo_padrao})")
    cabecalho_tabela(20)
    for direcao in ("mais", "menos"):
        rs = [r for r in cima_corrigido if r["direcao"] == direcao]
        imprimir_linha(direcao, rs, 20)
    n_total = len(cima_corrigido)
    n_menos = len([r for r in cima_corrigido if r["direcao"] == "menos"])
    pct_menos = 100.0 * n_menos / n_total if n_total else 0.0
    print(f"  -> {pct_menos:.0f}% do grupo 'pra cima' corrigido é 'menos' "
          f"({n_menos}/{n_total})")


def parte_c_clamp(tipo_padrao, cima_corrigido):
    print(f"\n  PARTE C - grupo 'pra cima' (corrigido) quebrado por distância do teto ({tipo_padrao})")
    cabecalho_tabela(28)
    colado = [r for r in cima_corrigido if r["colado_no_teto"]]
    longe = [r for r in cima_corrigido if not r["colado_no_teto"]]
    imprimir_linha("colado no teto (>=1.90)", colado, 28)
    imprimir_linha("longe do teto (<1.90)", longe, 28)


def parte_d_amostra(tipo_padrao, migrado_baixo_cima, limite=25):
    print(f"\n  PARTE D - amostra do grupo migrado baixo->cima ({tipo_padrao}, até {limite} linhas)")
    print(f"  {'fixture':>9} | {'linha':>6} | {'direção':>7} | {'lado':>6} | "
          f"{'fat.atual':>9} | {'fat.corr':>8} | {'bruto':>7} | {'previsto':>8} | resultado")
    for r in migrado_baixo_cima[:limite]:
        print(f"  {r['fixture_id_api']:>9} | {r['linha']:>6} | {r['direcao']:>7} | "
              f"{r['lado']:>6} | {r['fator_atual']:>9.3f} | {r['fator_corrigido']:>8.3f} | "
              f"{r['fator_corrigido_bruto']:>7.3f} | {r['previsto']:>7.1f}% | {r['resultado']}")


def main():
    cabecalho()
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    try:
        for tipo_padrao, (par, coluna_a) in MERCADOS_ESCOPO.items():
            print("\n" + "=" * 78)
            print(f"MERCADO: {tipo_padrao}")
            print("=" * 78)

            resultados = coletar(cur, tipo_padrao, par, coluna_a)
            print(f"  {len(resultados)} recomendação(ões) fechada(s) na amostra.")
            if not resultados:
                continue

            migrado_bc, ficou_cima = parte_a_matriz(tipo_padrao, resultados)

            cima_corrigido_total = [
                r for r in resultados if bucket_do_fator(r["fator_corrigido"]) == "pra cima"
            ]
            parte_b_direcao(tipo_padrao, cima_corrigido_total)
            parte_c_clamp(tipo_padrao, cima_corrigido_total)

            if migrado_bc:
                parte_d_amostra(tipo_padrao, migrado_bc)
            else:
                print("\n  PARTE D - nenhuma recomendação migrou de baixo pra cima nesse mercado.")

        print("\n" + "=" * 78)
        print("Fim. Nenhuma linha de código de produção foi tocada, nada foi gravado.")
        print("Isto é diagnóstico - não decide nada sozinho. Se a hipótese A se")
        print("confirmar (grupo migrado dominado por 'menos' e colado no teto),")
        print("o próximo passo é uma decisão de ARQUITETURA (não código ainda):")
        print("calibrar PISO_FATOR_LINHA/TETO_FATOR_LINHA por mercado, em vez de")
        print("usar 0.50/2.00 fixo pros dois regimes (escanteio ~10 de média,")
        print("cartão ~5.4 de média).")
        print("=" * 78)

    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
