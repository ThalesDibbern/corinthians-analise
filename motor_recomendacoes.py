"""
Motor de recomendações - cruza os padrões já calculados (motor_padroes.py)
com as odds reais coletadas (atualizar_odds.py) para encontrar apostas
com "valor esperado" positivo: onde a probabilidade histórica do padrão
acontecer é maior do que a odd da casa está sugerindo.

NOVO: quando o jogo já tem o árbitro confirmado (salvo pelo atualizar_odds.py)
e existe um perfil calculado pra ele (motor_padroes.py), a probabilidade de
cartão e falta é ajustada pelo "fator" desse árbitro - juízes que dão mais
cartão que a média puxam a probabilidade pra cima, os que seguram mais o
cartão puxam pra baixo. O ajuste é limitado a um intervalo (0.85x a 1.15x)
pra não deixar uma amostra ainda pequena por árbitro dominar a conta.

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

# limites do ajuste de árbitro - evita que uma amostra pequena por árbitro
# distorça demais a probabilidade calculada a partir dos últimos 50 jogos do jogador
FATOR_ARBITRO_MINIMO = 0.85
FATOR_ARBITRO_MAXIMO = 1.15


def identificar_tipo_padrao(mercado):
    """Adivinha a que tipo de padrão um mercado se refere, a partir do nome
    (em português, como vem da OddsPapi).

    NOVO: os mercados de total do jogo (escanteios/cartões somando os dois
    times) são checados ANTES dos mercados por time/jogador, porque o nome
    deles ("Escanteios Total do Jogo", "Cartões Total do Jogo") também
    contém as palavras "escanteio"/"cartão" - sem essa ordem, cairiam por
    engano nos tipos genéricos (escanteio_time/cartao)."""
    nome = mercado.lower()
    if "escanteio total do jogo" in nome:
        return "escanteio_total"
    if "cartão total do jogo" in nome or "cartao total do jogo" in nome:
        return "cartao_total"
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
    if "resultado" in nome and "tempo completo" in nome:
        return "resultado_final"
    return None


def buscar_odds_futuras(cur):
    """Busca odds de jogos que ainda não aconteceram. NOVO: também traz o
    árbitro do jogo (j.arbitro), usado no ajuste de cartão/falta."""
    cur.execute(
        """
        SELECT o.id, o.jogo_id, o.jogador_id, o.casa_aposta, o.mercado,
               o.valor_odd, o.linha, o.direcao, j.data_jogo, j.adversario,
               j.mandante, j.arbitro
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


def buscar_frequencia_escanteio_total(cur, linha):
    """NOVO: frequência de escanteios do jogo INTEIRO (mandante + visitante
    somados) passar de uma linha - diferente de buscar_frequencia_escanteio_time,
    que olha só o lado do Corinthians."""
    cur.execute(
        "SELECT frequencia FROM padroes_escanteio_total WHERE linha = %s",
        (linha,),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def buscar_frequencia_cartao_total(cur, linha):
    """NOVO: frequência de cartões do jogo INTEIRO (mandante + visitante
    somados) passar de uma linha."""
    cur.execute(
        "SELECT frequencia FROM padroes_cartao_total WHERE linha = %s",
        (linha,),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def buscar_frequencia_resultado(cur, lado, resultado):
    cur.execute(
        "SELECT frequencia FROM padroes_time_resultado WHERE lado = %s AND resultado = %s",
        (lado, resultado),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def buscar_media_geral_cartoes(cur):
    """NOVO: média geral de cartões por jogo, calculada a partir de todos os
    árbitros com perfil já calculado. Serve de linha de base pra saber se um
    árbitro específico dá mais ou menos cartão que a média."""
    cur.execute("SELECT AVG(media_cartoes) FROM padroes_arbitro")
    row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def buscar_perfil_arbitro(cur, arbitro):
    cur.execute(
        "SELECT media_cartoes, media_faltas FROM padroes_arbitro WHERE arbitro = %s",
        (arbitro,),
    )
    return cur.fetchone()


def calcular_fator_arbitro(cur, arbitro, media_geral_cartoes):
    """NOVO: retorna o multiplicador a aplicar na probabilidade de cartão,
    com base em quanto esse árbitro se desvia da média geral. Limitado ao
    intervalo [FATOR_ARBITRO_MINIMO, FATOR_ARBITRO_MAXIMO]. Retorna None se
    não houver árbitro definido, perfil calculado, ou média geral disponível."""
    if not arbitro or media_geral_cartoes is None or media_geral_cartoes == 0:
        return None

    perfil = buscar_perfil_arbitro(cur, arbitro)
    if not perfil:
        return None

    media_cartoes_arbitro, _ = perfil
    if media_cartoes_arbitro is None:
        return None

    fator = float(media_cartoes_arbitro) / media_geral_cartoes
    return max(FATOR_ARBITRO_MINIMO, min(FATOR_ARBITRO_MAXIMO, fator))


def resultado_do_ponto_de_vista_corinthians(direcao, mandante):
    """Traduz o outcome da odd (1/X/2) para vitória/empate/derrota do
    Corinthians, considerando se ele é mandante ou visitante nesse jogo."""
    d = (direcao or "").strip().upper()

    if d in ("X", "EMPATE", "DRAW"):
        return "empate"
    if d == "1":
        return "vitoria" if mandante else "derrota"
    if d == "2":
        return "vitoria" if not mandante else "derrota"
    return None


def calcular_recomendacoes(cur):
    odds = buscar_odds_futuras(cur)
    recomendacoes = []

    # NOVO: calcula a média geral de cartões uma única vez, fora do loop
    media_geral_cartoes = buscar_media_geral_cartoes(cur)

    for (odd_id, jogo_id, jogador_id, casa, mercado, valor_odd,
         linha, direcao, data_jogo, adversario, mandante, arbitro) in odds:

        tipo = identificar_tipo_padrao(mercado)
        if tipo is None:
            continue

        frequencia = None
        resultado_cor = None
        fator_arbitro_aplicado = None

        if tipo == "cartao" and jogador_id and direcao and direcao.lower() == "sim":
            frequencia = buscar_frequencia_cartao(cur, jogador_id)

            # NOVO: aplica o ajuste de árbitro, se disponível
            if frequencia is not None:
                fator = calcular_fator_arbitro(cur, arbitro, media_geral_cartoes)
                if fator is not None:
                    frequencia = min(round(frequencia * fator, 2), 100.0)
                    fator_arbitro_aplicado = fator

        elif tipo in ("falta_cometida", "desarme", "chute_no_gol") and jogador_id \
                and direcao and direcao.lower() == "mais" and linha is not None:
            frequencia = buscar_frequencia_linha_jogador(cur, jogador_id, tipo, linha)

        elif tipo == "impedimento" and jogador_id and direcao and direcao.lower() == "sim":
            frequencia = buscar_frequencia_simples_jogador(cur, jogador_id, "impedimento")

        elif tipo == "escanteio_time" and not jogador_id \
                and direcao and direcao.lower() == "mais" and linha is not None:
            frequencia = buscar_frequencia_escanteio_time(cur, linha)

        elif tipo == "escanteio_total" and not jogador_id \
                and direcao and direcao.lower() == "mais" and linha is not None:
            frequencia = buscar_frequencia_escanteio_total(cur, linha)

        elif tipo == "cartao_total" and not jogador_id \
                and direcao and direcao.lower() == "mais" and linha is not None:
            frequencia = buscar_frequencia_cartao_total(cur, linha)

        elif tipo == "resultado_final" and not jogador_id:
            resultado_cor = resultado_do_ponto_de_vista_corinthians(direcao, mandante)
            if resultado_cor:
                lado = "mandante" if mandante else "visitante"
                frequencia = buscar_frequencia_resultado(cur, lado, resultado_cor)

        if frequencia is None:
            continue  # não temos padrão calculado pra cruzar com essa odd ainda

        probabilidade = frequencia / 100
        valor_esperado = round((probabilidade * float(valor_odd)) - 1, 3)

        descricao_final = mercado
        if tipo == "resultado_final":
            nomes = {"vitoria": "Vitória do Corinthians", "empate": "Empate", "derrota": "Derrota do Corinthians"}
            descricao_final = f"Resultado Final - {nomes[resultado_cor]}"

        if fator_arbitro_aplicado is not None:
            descricao_final += f" (ajustado pelo árbitro, fator {fator_arbitro_aplicado:.2f}x)"

        if valor_esperado > VALOR_ESPERADO_MINIMO:
            recomendacoes.append({
                "jogo_id": jogo_id,
                "jogador_id": jogador_id,
                "tipo_padrao": tipo,
                "descricao": descricao_final,
                "casa_aposta": casa,
                "odd_oferecida": valor_odd,
                "probabilidade_historica": round(probabilidade * 100, 2),
                "valor_esperado": valor_esperado,
                "linha": linha,
                "adversario": adversario,
                "data_jogo": data_jogo,
            })

    return recomendacoes


def salvar_recomendacoes(cur, recomendacoes):
    # limpa só as recomendações de jogos FUTUROS antes de gerar as novas
    # (as de jogos já ocorridos ficam intactas até o script de arquivamento
    # processá-las - senão perderíamos o histórico antes de avaliar acerto/erro)
    cur.execute(
        "DELETE FROM recomendacoes WHERE jogo_id IN (SELECT id FROM jogos WHERE data_jogo >= CURRENT_DATE)"
    )

    for r in recomendacoes:
        cur.execute(
            """INSERT INTO recomendacoes
               (jogo_id, jogador_id, tipo_padrao, descricao, casa_aposta,
                odd_oferecida, probabilidade_historica, valor_esperado, linha)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                r["jogo_id"], r["jogador_id"], r["tipo_padrao"], r["descricao"],
                r["casa_aposta"], r["odd_oferecida"], r["probabilidade_historica"],
                r["valor_esperado"], r.get("linha"),
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
