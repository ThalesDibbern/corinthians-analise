"""
Motor de recomendações - cruza os padrões já calculados (motor_padroes.py)
com as odds reais coletadas (atualizar_odds.py) para encontrar apostas
com "valor esperado" positivo: onde a probabilidade histórica do padrão
acontecer é maior do que a odd da casa está sugerindo.

Fórmula usada (valor esperado por unidade apostada):
    VE = (probabilidade_historica * odd) - 1
Se VE > 0, a aposta é estatisticamente favorável no longo prazo, segundo
o nosso histórico.

IMPORTANTE: isso não é garantia de acerto em uma aposta individual - é uma
estimativa baseada em dados históricos, que só faz sentido com dados de
jogadores/temporada atuais. Enquanto o banco ainda é de 2022-2024, use
os resultados aqui só para validar a lógica, não para apostar de verdade.

Só considera odds de jogos que ainda vão acontecer (data_jogo >= hoje).

Variáveis de ambiente necessárias:
  - DATABASE_URL -> a URL de conexão do Postgres (mesma usada nos outros scripts)
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

VALOR_ESPERADO_MINIMO = 0.0  # só guarda recomendações com VE acima disso


def identificar_tipo_padrao(mercado):
    """Adivinha a que tipo de padrão um mercado se refere, a partir do nome
    (em português, como vem da OddsPapi)."""
    nome = mercado.lower()
    if "cartão" in nome or "cartao" in nome or "card" in nome:
        return "cartao"
    if "falta" in nome:
        return "falta_cometida"
    if "desarme" in nome or "tackle" in nome:
        return "desarme"
    if "chute" in nome or "shot" in nome:
        return "chute_no_gol"
    if "impediment" in nome:
        return "impedimento"
    if "escanteio" in nome or "corner" in nome:
        return "escanteio_time"
    return None


def buscar_odds_futuras(cur):
    """Busca odds de jogos que ainda não aconteceram."""
    cur.execute(
        """
        SELECT o.id, o.jogo_id, o.jogador_id, o.casa_aposta, o.mercado,
               o.valor_odd, o.linha, o.direcao, j.data_jogo, j.adversario
        FROM odds o
        JOIN jogos j ON j.id = o.jogo_id
        WHERE j.data_jogo >= CURRENT_DATE
        """
    )
    return cur.fetchall()


def buscar_frequencia_cartao(cur, jogador_id):
    cur.execute(
        "SELECT frequencia FROM padroes_jogador_cartao WHERE jogador_id = %s",
        (jogador_id,),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def buscar_frequencia_linha_jogador(cur, jogador_id, tipo, linha):
    cur.execute(
        "SELECT frequencia FROM padroes_jogador_linha WHERE jogador_id = %s AND tipo = %s AND linha = %s",
        (jogador_id, tipo, linha),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def buscar_frequencia_simples_jogador(cur, jogador_id, tipo):
    cur.execute(
        "SELECT frequencia FROM padroes_jogador_frequencia WHERE jogador_id = %s AND tipo = %s",
        (jogador_id, tipo),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def buscar_frequencia_escanteio_time(cur, linha):
    cur.execute(
        "SELECT frequencia FROM padroes_time_escanteio WHERE linha = %s",
        (linha,),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def calcular_recomendacoes(cur):
    odds = buscar_odds_futuras(cur)
    recomendacoes = []

    for (odd_id, jogo_id, jogador_id, casa, mercado, valor_odd,
         linha, direcao, data_jogo, adversario) in odds:

        tipo = identificar_tipo_padrao(mercado)
        if tipo is None:
            continue

        frequencia = None

        if tipo == "cartao" and jogador_id and direcao and direcao.lower() == "sim":
            frequencia = buscar_frequencia_cartao(cur, jogador_id)

        elif tipo in ("falta_cometida", "desarme", "chute_no_gol") and jogador_id \
                and direcao and direcao.lower() == "mais" and linha is not None:
            frequencia = buscar_frequencia_linha_jogador(cur, jogador_id, tipo, linha)

        elif tipo == "impedimento" and jogador_id and direcao and direcao.lower() == "sim":
            frequencia = buscar_frequencia_simples_jogador(cur, jogador_id, "impedimento")

        elif tipo == "escanteio_time" and not jogador_id \
                and direcao and direcao.lower() == "mais" and linha is not None:
            frequencia = buscar_frequencia_escanteio_time(cur, linha)

        if frequencia is None:
            continue  # não temos padrão calculado pra cruzar com essa odd ainda

        probabilidade = frequencia / 100
        valor_esperado = round((probabilidade * float(valor_odd)) - 1, 3)

        if valor_esperado > VALOR_ESPERADO_MINIMO:
            recomendacoes.append({
                "jogo_id": jogo_id,
                "jogador_id": jogador_id,
                "tipo_padrao": tipo,
                "descricao": mercado,
                "casa_aposta": casa,
                "odd_oferecida": valor_odd,
                "probabilidade_historica": round(probabilidade * 100, 2),
                "valor_esperado": valor_esperado,
                "adversario": adversario,
                "data_jogo": data_jogo,
            })

    return recomendacoes


def salvar_recomendacoes(cur, recomendacoes):
    # limpa recomendações antigas antes de gerar as novas, pra não acumular
    # recomendações desatualizadas de execuções anteriores
    cur.execute("DELETE FROM recomendacoes")

    for r in recomendacoes:
        cur.execute(
            """INSERT INTO recomendacoes
               (jogo_id, jogador_id, tipo_padrao, descricao, casa_aposta,
                odd_oferecida, probabilidade_historica, valor_esperado)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                r["jogo_id"], r["jogador_id"], r["tipo_padrao"], r["descricao"],
                r["casa_aposta"], r["odd_oferecida"], r["probabilidade_historica"],
                r["valor_esperado"],
            ),
        )
        print(f"  [{r['data_jogo']} vs {r['adversario']}] {r['descricao']} "
              f"({r['casa_aposta']}) - odd {r['odd_oferecida']} | "
              f"prob. histórica {r['probabilidade_historica']}% | VE {r['valor_esperado']}")


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        print("Cruzando padrões com odds de jogos futuros...")
        recomendacoes = calcular_recomendacoes(cur)

        if not recomendacoes:
            print("Nenhuma recomendação de valor encontrada no momento "
                  "(sem jogo próximo, sem odds coletadas, ou sem padrão correspondente).")
            salvar_recomendacoes(cur, [])  # ainda assim limpa recomendações antigas
        else:
            recomendacoes.sort(key=lambda r: r["valor_esperado"], reverse=True)
            salvar_recomendacoes(cur, recomendacoes)

        conn.commit()
        print(f"\nConcluído! {len(recomendacoes)} recomendação(ões) salva(s).")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
