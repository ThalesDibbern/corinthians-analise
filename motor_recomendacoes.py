"""
Motor de combinações - pega as recomendações individuais já geradas
(motor_recomendacoes.py) e monta "múltiplas" (2 ou 3 apostas combinadas),
buscando bater dentro de uma faixa de odd desejada.

Regras aplicadas:
  - Só combina apostas da MESMA casa de apostas (múltiplas não funcionam
    misturando casas diferentes).
  - Nunca combina duas pernas do mesmo jogador (reduz o risco de tratar
    eventos correlacionados como se fossem independentes).
  - Odd combinada = produto das odds individuais.
  - Probabilidade combinada = produto das probabilidades individuais
    (assumindo independência - ver ressalva abaixo).

IMPORTANTE - limitação conhecida: o cálculo de probabilidade combinada
assume que os eventos são independentes entre si. Isso é uma aproximação;
eventos do mesmo jogo (ex: cartões e escanteios) podem ter alguma
correlação real (jogos mais "quentes" tendem a ter mais de ambos). Trate
a probabilidade combinada como uma estimativa, não um valor exato.

Variáveis de ambiente:
  - DATABASE_URL -> a URL de conexão do Postgres (mesma usada nos outros scripts)
  - ODD_MINIMA    -> (opcional) menor odd combinada aceita (padrão: 1.5)
  - ODD_MAXIMA    -> (opcional) maior odd combinada aceita (padrão: 5.0)
"""

import os
from itertools import combinations

import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]
ODD_MINIMA = float(os.environ.get("ODD_MINIMA", "1.5"))
ODD_MAXIMA = float(os.environ.get("ODD_MAXIMA", "5.0"))
TAMANHOS_DE_MULTIPLA = [2, 3, 4, 5]
MAXIMO_SUGESTOES = 10


def buscar_recomendacoes(cur):
    cur.execute(
        """
        SELECT r.id, r.jogo_id, r.jogador_id, r.descricao, r.casa_aposta,
               r.odd_oferecida, r.probabilidade_historica, j.adversario, j.data_jogo, r.tipo_padrao
        FROM recomendacoes r
        JOIN jogos j ON j.id = r.jogo_id
        """
    )
    return cur.fetchall()


def montar_combinacoes(recomendacoes):
    """Agrupa por (jogo, casa de apostas) e testa combinações de 2 a 5 pernas.
    O mercado 'resultado_final' só entra na lista de candidatos quando a
    faixa de odd pedida permite valores acima de 5.0 (alta variância)."""
    grupos = {}
    for rec in recomendacoes:
        (_, jogo_id, jogador_id, descricao, casa, odd, prob, adversario, data_jogo, tipo_padrao) = rec

        if tipo_padrao == "resultado_final" and ODD_MAXIMA <= 5.0:
            continue

        chave = (jogo_id, casa)
        grupos.setdefault(chave, []).append({
            "jogador_id": jogador_id,
            "descricao": descricao,
            "odd": float(odd),
            "probabilidade": float(prob) / 100,
            "adversario": adversario,
            "data_jogo": data_jogo,
        })

    combinacoes_validas = []

    for (jogo_id, casa), pernas in grupos.items():
        for tamanho in TAMANHOS_DE_MULTIPLA:
            if len(pernas) < tamanho:
                continue

            for combo in combinations(pernas, tamanho):
                jogadores_no_combo = [p["jogador_id"] for p in combo if p["jogador_id"] is not None]
                if len(jogadores_no_combo) != len(set(jogadores_no_combo)):
                    continue  # tem jogador repetido nessa combinação, pula

                odd_combinada = 1.0
                prob_combinada = 1.0
                for perna in combo:
                    odd_combinada *= perna["odd"]
                    prob_combinada *= perna["probabilidade"]

                if not (ODD_MINIMA <= odd_combinada <= ODD_MAXIMA):
                    continue

                valor_esperado = round((prob_combinada * odd_combinada) - 1, 3)
                descricao_completa = " + ".join(p["descricao"] for p in combo)

                combinacoes_validas.append({
                    "jogo_id": jogo_id,
                    "casa_aposta": casa,
                    "descricao": descricao_completa,
                    "odd_combinada": round(odd_combinada, 2),
                    "probabilidade_combinada": round(prob_combinada * 100, 2),
                    "valor_esperado": valor_esperado,
                    "adversario": combo[0]["adversario"],
                    "data_jogo": combo[0]["data_jogo"],
                })

    return combinacoes_validas


def salvar_combinacoes(cur, combinacoes):
    cur.execute("DELETE FROM combinacoes_sugeridas")

    for c in combinacoes:
        cur.execute(
            """INSERT INTO combinacoes_sugeridas
               (jogo_id, casa_aposta, descricao, odd_combinada, probabilidade_combinada, valor_esperado_combinado)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (c["jogo_id"], c["casa_aposta"], c["descricao"], c["odd_combinada"],
             c["probabilidade_combinada"], c["valor_esperado"]),
        )
        print(f"  [{c['data_jogo']} vs {c['adversario']}] ({c['casa_aposta']}) "
              f"ODD {c['odd_combinada']} | prob. {c['probabilidade_combinada']}% | "
              f"VE {c['valor_esperado']}\n    -> {c['descricao']}")


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        print(f"Buscando recomendações para montar múltiplas entre odd {ODD_MINIMA} e {ODD_MAXIMA}...")
        recomendacoes = buscar_recomendacoes(cur)
        total_salvas = 0

        if len(recomendacoes) < 2:
            print("Recomendações insuficientes para montar múltiplas (precisa de pelo menos 2).")
            salvar_combinacoes(cur, [])
        else:
            combinacoes = montar_combinacoes(recomendacoes)
            combinacoes.sort(key=lambda c: c["valor_esperado"], reverse=True)
            melhores = combinacoes[:MAXIMO_SUGESTOES]
            salvar_combinacoes(cur, melhores)
            total_salvas = len(melhores)

        conn.commit()
        print(f"\nConcluído! {total_salvas} combinação(ões) salva(s).")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
