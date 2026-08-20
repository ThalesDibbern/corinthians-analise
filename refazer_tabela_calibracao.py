"""
Refaz a tabela de calibração da seção 9 da documentação.

A tabela documentada foi tirada ANTES das correções desta sessão (bug do
chute_total, 64 linhas / 13 mudaram de resultado; Athletico-PR x Bragantino
nível de jogador, mais 6 linhas). Esse script reaproveita a MESMA query e o
MESMO agrupamento por faixa que o app usa em `buscar_calibracao()` (rota
/historico), pra garantir que o número documentado bate exatamente com o
que já aparece no card - sem duplicar lógica em dois lugares.

Só leitura. Não grava nada no banco.

Rodar:
    python refazer_tabela_calibracao.py

Variáveis de ambiente necessárias: DATABASE_URL
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

# Mesma constante do app (FAIXA_CALIBRACAO_LARGURA).
FAIXA_CALIBRACAO_LARGURA = 20


def buscar_calibracao(cur):
    """Cópia fiel de buscar_calibracao() em app.py - mesma query, mesmo
    agrupamento. Se um dia o app mudar essa lógica, este script precisa
    ser atualizado junto (não importa de app.py pra evitar acoplar um
    script pontual ao Flask)."""
    cur.execute(
        """
        SELECT probabilidade_historica, resultado, COUNT(*)
        FROM historico_recomendacoes
        WHERE resultado IN ('acertou', 'errou')
        GROUP BY probabilidade_historica, resultado
        ORDER BY probabilidade_historica DESC
        """
    )
    por_valor = {}
    for prob, resultado, contagem in cur.fetchall():
        prob_float = float(prob)
        por_valor.setdefault(prob_float, {"acertou": 0, "errou": 0})
        por_valor[prob_float][resultado] = contagem

    faixas = {}
    for prob, dados in por_valor.items():
        inicio_faixa = min(
            int(prob // FAIXA_CALIBRACAO_LARGURA) * FAIXA_CALIBRACAO_LARGURA,
            100 - FAIXA_CALIBRACAO_LARGURA,
        )
        faixas.setdefault(inicio_faixa, {"acertou": 0, "errou": 0})
        faixas[inicio_faixa]["acertou"] += dados["acertou"]
        faixas[inicio_faixa]["errou"] += dados["errou"]

    detalhamento = []
    for inicio_faixa, dados in sorted(faixas.items(), reverse=True):
        total = dados["acertou"] + dados["errou"]
        taxa = round(100 * dados["acertou"] / total, 1) if total else 0.0
        detalhamento.append({
            "inicio": inicio_faixa,
            "fim": inicio_faixa + FAIXA_CALIBRACAO_LARGURA,
            "acertou": dados["acertou"],
            "errou": dados["errou"],
            "total": total,
            "taxa": taxa,
        })

    return detalhamento


def buscar_totais_gerais(cur):
    """Contexto extra pra documentação: total geral e comparação com o
    último número documentado, pra deixar claro o quanto mudou."""
    cur.execute(
        """
        SELECT resultado, COUNT(*)
        FROM historico_recomendacoes
        WHERE resultado IN ('acertou', 'errou')
        GROUP BY resultado
        """
    )
    totais = {"acertou": 0, "errou": 0}
    for resultado, contagem in cur.fetchall():
        totais[resultado] = contagem
    return totais


def main():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    try:
        detalhamento = buscar_calibracao(cur)
        totais = buscar_totais_gerais(cur)
    finally:
        cur.close()
        conn.close()

    total_geral = totais["acertou"] + totais["errou"]
    taxa_geral = round(100 * totais["acertou"] / total_geral, 1) if total_geral else 0.0

    print("=" * 60)
    print("TABELA DE CALIBRAÇÃO - refeita")
    print("=" * 60)
    print()
    print(f"{'Faixa':<12}{'N':<8}{'Taxa de acerto':<18}")
    print("-" * 38)
    for item in detalhamento:
        faixa_str = f"{item['inicio']}-{item['fim']}%"
        print(f"{faixa_str:<12}{item['total']:<8}{item['taxa']}%")
    print("-" * 38)
    print(f"{'Geral':<12}{total_geral:<8}{taxa_geral}%")
    print()
    print("--- Markdown pronto pra colar na seção 9 ---")
    print()
    print("| Faixa | N | Taxa de acerto |")
    print("|---|---|---|")
    for item in detalhamento:
        faixa_str = f"{item['inicio']}-{item['fim']}%"
        print(f"| {faixa_str} | {item['total']} | {item['taxa']}% |")
    print()
    print(f"Total avaliado: {total_geral} ({totais['acertou']} acertou / {totais['errou']} errou) — {taxa_geral}% geral")


if __name__ == "__main__":
    main()
