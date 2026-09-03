"""
Simulação da correção de seleção adversa do VE - fase `verificar`.

O PROBLEMA (pendência 28, achado mais estrutural do projeto): quando a
probabilidade que o modelo calcula diverge muito da probabilidade que a
odd da casa implica, essa divergência quase sempre é o MODELO errando,
não uma vantagem real. Medido: VE alto prediz resultado sistematicamente
pior, mesmo controlando pela faixa de probabilidade prevista (ver sessão
de investigação em 03/09/2026).

A CORREÇÃO: encolher a probabilidade do modelo em direção à probabilidade
implícita da odd da casa, proporcional à divergência. Mesma lógica já
usada no confronto direto como prior (seção 41), agora contra o mercado.

    p_mercado = 100 / odd_oferecida
    diff      = p_modelo - p_mercado
    peso      = 1 / (1 + K_ENCOLHIMENTO * abs(diff) / 100)
    p_final   = p_mercado + diff * peso

K_ENCOLHIMENTO = 15, escolhido por backtest em 03/09/2026: resolve quase
todo o gap da faixa de VE mais alta (0,60+: -8,68 -> -1,14) sem
super-corrigir a faixa que já estava bem calibrada (0,10-0,20).

ESCOPO: só os mercados sem problema de calibração próprio (medido -
esses já erram mesmo com VE baixo, então a correção entraria em cima de
outro bug e confundiria causa com sintoma):
    - resultado_final, forma recente -> FORA. Aguardando medição da
      rodada de 05/09/2026 (critério de reversão já definido, seção 56).
    - correlação -> FORA. Problema próprio, ainda sem investigação.

ESTA FASE NÃO GRAVA NADA. Só lê `historico_recomendacoes`, aplica a
fórmula em memória e imprime o antes/depois - agregado por faixa de VE
e por mercado, e o impacto operacional (quantas recomendações que hoje
passam no filtro de VE>0 deixariam de passar).

Uso:
  python simular_correcao_divergencia_ve.py verificar
  python simular_correcao_divergencia_ve.py verificar --k 20
"""

import os
import sys
from datetime import datetime
from collections import defaultdict

import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

K_ENCOLHIMENTO_PADRAO = 15

MERCADOS_ESCOPO = [
    "escanteio_time", "escanteio_total",
    "cartao", "cartao_total",
    "gols_time", "gols_total",
    "ambas_marcam_1t", "ambas_marcam_2t",
    "marca_ambos_tempos",
    "equipe_marca",
    "chute_no_gol", "falta_cometida",
]

MERCADOS_FORA_DO_ESCOPO = [
    "resultado_final",  # aguardando rodada de 05/09 (seção 56)
    "correlacao",        # problema próprio, não investigado ainda
]


def cabecalho(fase, extra=""):
    print("=" * 78)
    print(f"simular_correcao_divergencia_ve.py | fase: {fase} {extra}")
    print(f"argv: {sys.argv}")
    print(f"relógio: {datetime.now().isoformat()}")
    print("=" * 78)
    sys.stdout.flush()


def calcular_p_final(p_modelo, odd_oferecida, k):
    """Encolhe p_modelo em direção à probabilidade implícita da odd,
    proporcional à divergência. Ver docstring do módulo."""
    if odd_oferecida is None or odd_oferecida <= 0:
        return p_modelo
    p_mercado = 100.0 / odd_oferecida
    diff = p_modelo - p_mercado
    peso = 1.0 / (1 + k * abs(diff) / 100.0)
    return p_mercado + diff * peso


def faixa_ve(valor_esperado):
    if valor_esperado < 0.10:
        return "1) 0.00-0.10"
    if valor_esperado < 0.20:
        return "2) 0.10-0.20"
    if valor_esperado < 0.30:
        return "3) 0.20-0.30"
    if valor_esperado < 0.40:
        return "4) 0.30-0.40"
    if valor_esperado < 0.60:
        return "5) 0.40-0.60"
    return "6) 0.60+"


def buscar_recomendacoes(cur):
    """Só resultado avaliado (acertou/errou) e só mercados do escopo."""
    placeholders = ",".join(["%s"] * len(MERCADOS_ESCOPO))
    cur.execute(
        f"""
        SELECT id, jogo_id, tipo_padrao, probabilidade_historica,
               odd_oferecida, valor_esperado, resultado
        FROM historico_recomendacoes
        WHERE resultado IN ('acertou', 'errou')
          AND tipo_padrao IN ({placeholders})
          AND odd_oferecida IS NOT NULL
          AND odd_oferecida > 0
        """,
        MERCADOS_ESCOPO,
    )
    return cur.fetchall()


def fase_verificar(k):
    cabecalho("verificar (só leitura, nenhuma escrita)", f"k={k}")

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    linhas = buscar_recomendacoes(cur)
    total = len(linhas)
    print(f"\n{total} recomendações avaliadas nos mercados do escopo.\n")
    sys.stdout.flush()

    if total == 0:
        print("Nada pra simular. Confira MERCADOS_ESCOPO e os dados de historico_recomendacoes.")
        cur.close()
        conn.close()
        return

    # --- agregação por faixa de VE (antigo) ---
    agregado_faixa = defaultdict(lambda: {"n": 0, "acertos": 0, "soma_p_antigo": 0.0, "soma_p_novo": 0.0})
    # --- agregação por mercado ---
    agregado_mercado = defaultdict(lambda: {"n": 0, "acertos": 0, "soma_p_antigo": 0.0, "soma_p_novo": 0.0})

    flip_deixaria_de_ser_ve_positivo = 0
    flip_passaria_a_ser_ve_positivo = 0
    ve_positivo_antes = 0
    ve_positivo_depois = 0

    for (rec_id, jogo_id, tipo_padrao, p_modelo, odd, ve_antigo, resultado) in linhas:
        p_modelo = float(p_modelo)
        odd = float(odd)
        ve_antigo = float(ve_antigo) if ve_antigo is not None else (p_modelo / 100.0 * odd - 1)
        acertou = 1 if resultado == "acertou" else 0

        p_novo = calcular_p_final(p_modelo, odd, k)
        ve_novo = (p_novo / 100.0) * odd - 1

        era_positivo = ve_antigo > 0
        fica_positivo = ve_novo > 0
        if era_positivo:
            ve_positivo_antes += 1
        if fica_positivo:
            ve_positivo_depois += 1
        if era_positivo and not fica_positivo:
            flip_deixaria_de_ser_ve_positivo += 1
        if not era_positivo and fica_positivo:
            flip_passaria_a_ser_ve_positivo += 1

        fx = faixa_ve(ve_antigo)
        agregado_faixa[fx]["n"] += 1
        agregado_faixa[fx]["acertos"] += acertou
        agregado_faixa[fx]["soma_p_antigo"] += p_modelo
        agregado_faixa[fx]["soma_p_novo"] += p_novo

        agregado_mercado[tipo_padrao]["n"] += 1
        agregado_mercado[tipo_padrao]["acertos"] += acertou
        agregado_mercado[tipo_padrao]["soma_p_antigo"] += p_modelo
        agregado_mercado[tipo_padrao]["soma_p_novo"] += p_novo

    print("=" * 78)
    print("POR FAIXA DE VE (calculado com a probabilidade ANTIGA)")
    print("=" * 78)
    print(f"{'faixa_ve':<16}{'n':>6}{'observ%':>10}{'prev_antigo%':>15}{'gap_antigo':>13}"
          f"{'prev_novo%':>13}{'gap_novo':>11}")
    for fx in sorted(agregado_faixa.keys()):
        d = agregado_faixa[fx]
        n = d["n"]
        observ = 100.0 * d["acertos"] / n
        prev_antigo = d["soma_p_antigo"] / n
        prev_novo = d["soma_p_novo"] / n
        gap_antigo = observ - prev_antigo
        gap_novo = observ - prev_novo
        print(f"{fx:<16}{n:>6}{observ:>10.1f}{prev_antigo:>15.1f}{gap_antigo:>13.1f}"
              f"{prev_novo:>13.1f}{gap_novo:>11.1f}")

    print("\n" + "=" * 78)
    print("POR MERCADO")
    print("=" * 78)
    print(f"{'tipo_padrao':<22}{'n':>6}{'observ%':>10}{'prev_antigo%':>15}{'gap_antigo':>13}"
          f"{'prev_novo%':>13}{'gap_novo':>11}")
    for tp in sorted(agregado_mercado.keys()):
        d = agregado_mercado[tp]
        n = d["n"]
        if n < 5:
            continue
        observ = 100.0 * d["acertos"] / n
        prev_antigo = d["soma_p_antigo"] / n
        prev_novo = d["soma_p_novo"] / n
        gap_antigo = observ - prev_antigo
        gap_novo = observ - prev_novo
        print(f"{tp:<22}{n:>6}{observ:>10.1f}{prev_antigo:>15.1f}{gap_antigo:>13.1f}"
              f"{prev_novo:>13.1f}{gap_novo:>11.1f}")

    print("\n" + "=" * 78)
    print("IMPACTO OPERACIONAL (quantas recomendações mudam de lado do filtro VE>0)")
    print("=" * 78)
    print(f"Tinham VE>0 (seriam geradas hoje): {ve_positivo_antes}")
    print(f"Passariam a ter VE>0 com a correção: {ve_positivo_depois}")
    print(f"  -> deixariam de ser geradas (VE colapsou pra <=0): {flip_deixaria_de_ser_ve_positivo}")
    print(f"  -> passariam a ser geradas (não eram antes): {flip_passaria_a_ser_ve_positivo}")
    print(f"Saldo: {ve_positivo_depois - ve_positivo_antes:+d} recomendações "
          f"({100.0 * (ve_positivo_depois - ve_positivo_antes) / max(ve_positivo_antes,1):+.1f}%)")

    print("\n" + "=" * 78)
    print(f"NOTA: {len(MERCADOS_FORA_DO_ESCOPO)} mercado(s) FORA desta simulação "
          f"(grupo de controle / pendência separada): {', '.join(MERCADOS_FORA_DO_ESCOPO)}")
    print("=" * 78)

    cur.close()
    conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "verificar":
        print("Uso: python simular_correcao_divergencia_ve.py verificar [--k VALOR]")
        sys.exit(1)

    k = K_ENCOLHIMENTO_PADRAO
    for i, arg in enumerate(sys.argv):
        if arg == "--k" and i + 1 < len(sys.argv):
            k = float(sys.argv[i + 1])

    fase_verificar(k)
