"""
Avaliação e arquivamento de recomendações.

Para jogos que já aconteceram (data_jogo < hoje), tenta avaliar se cada
recomendação "acertou" ou "errou", comparando com o resultado real do jogo
(se já estiver disponível no banco). Move as recomendações de jogos além
das 5 rodadas mais recentes já disputadas para a tabela de histórico
(resumida), liberando a tabela `recomendacoes` para focar só no que ainda
é relevante (jogos futuros e as últimas rodadas jogadas).

IMPORTANTE: a avaliação de acerto/erro só funciona se já tivermos os dados
reais daquele jogo no banco (tabelas cartoes, jogador_estatisticas_jogo,
estatisticas_jogo). Isso depende de uma fonte de dados com cobertura da
temporada atual (o histórico grátis da API-Football só vai até 2024) - até
lá, o resultado fica marcado como "pendente", e a estrutura já está pronta
pra funcionar automaticamente assim que os dados reais chegarem.

Variáveis de ambiente:
  - DATABASE_URL -> a URL de conexão do Postgres (mesma usada nos outros scripts)
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]
RODADAS_A_MANTER_DETALHADAS = 5


def avaliar_resultado(cur, tipo_padrao, jogador_id, jogo_id, linha):
    """Compara a recomendação com o resultado real do jogo, se disponível.
    Retorna 'acertou', 'errou' ou 'pendente' (se ainda não temos o dado real)."""

    if tipo_padrao == "cartao":
        cur.execute("SELECT 1 FROM cartoes WHERE jogo_id = %s AND jogador_id = %s", (jogo_id, jogador_id))
        if cur.fetchone():
            return "acertou"
        cur.execute(
            "SELECT 1 FROM jogador_estatisticas_jogo WHERE jogo_id = %s AND jogador_id = %s",
            (jogo_id, jogador_id),
        )
        return "errou" if cur.fetchone() else "pendente"

    if tipo_padrao in ("falta_cometida", "desarme", "chute_no_gol"):
        coluna = {
            "falta_cometida": "faltas_cometidas",
            "desarme": "desarmes",
            "chute_no_gol": "chutes_no_gol",
        }[tipo_padrao]
        cur.execute(
            f"SELECT {coluna} FROM jogador_estatisticas_jogo WHERE jogo_id = %s AND jogador_id = %s",
            (jogo_id, jogador_id),
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return "pendente"
        return "acertou" if float(row[0]) > float(linha) else "errou"

    if tipo_padrao == "impedimento":
        cur.execute(
            "SELECT impedimentos FROM jogador_estatisticas_jogo WHERE jogo_id = %s AND jogador_id = %s",
            (jogo_id, jogador_id),
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return "pendente"
        return "acertou" if row[0] > 0 else "errou"

    if tipo_padrao == "escanteio_time":
        cur.execute("SELECT mandante FROM jogos WHERE id = %s", (jogo_id,))
        info_jogo = cur.fetchone()
        if not info_jogo:
            return "pendente"
        lado = "mandante" if info_jogo[0] else "visitante"
        cur.execute(
            "SELECT escanteios FROM estatisticas_jogo WHERE jogo_id = %s AND lado = %s",
            (jogo_id, lado),
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return "pendente"
        return "acertou" if float(row[0]) > float(linha) else "errou"

    return "pendente"


def buscar_jogos_recentes_a_manter(cur):
    """Os N jogos mais recentes já disputados ficam com odds detalhadas
    (não arquivadas ainda)."""
    cur.execute(
        """
        SELECT DISTINCT j.id FROM jogos j
        JOIN recomendacoes r ON r.jogo_id = j.id
        WHERE j.data_jogo < CURRENT_DATE
        ORDER BY j.id DESC
        LIMIT %s
        """,
        (RODADAS_A_MANTER_DETALHADAS,),
    )
    return {row[0] for row in cur.fetchall()}


def buscar_recomendacoes_para_arquivar(cur, jogos_a_manter):
    cur.execute(
        """
        SELECT r.id, r.jogo_id, r.jogador_id, r.tipo_padrao, r.descricao, r.casa_aposta,
               r.odd_oferecida, r.probabilidade_historica, r.valor_esperado, r.linha, j.data_jogo
        FROM recomendacoes r
        JOIN jogos j ON j.id = r.jogo_id
        WHERE j.data_jogo < CURRENT_DATE
        """
    )
    todas = cur.fetchall()
    return [r for r in todas if r[1] not in jogos_a_manter]


def arquivar(cur, recomendacoes):
    contagem = {"acertou": 0, "errou": 0, "pendente": 0}

    for (rec_id, jogo_id, jogador_id, tipo_padrao, descricao, casa,
         odd, prob, ve, linha, data_jogo) in recomendacoes:

        resultado = avaliar_resultado(cur, tipo_padrao, jogador_id, jogo_id, linha)
        contagem[resultado] += 1

        cur.execute(
            """INSERT INTO historico_recomendacoes
               (jogo_id, jogador_id, tipo_padrao, descricao, casa_aposta,
                odd_oferecida, probabilidade_historica, valor_esperado, linha,
                resultado, data_jogo)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (jogo_id, jogador_id, tipo_padrao, descricao, casa, odd, prob, ve,
             linha, resultado, data_jogo),
        )
        cur.execute("DELETE FROM recomendacoes WHERE id = %s", (rec_id,))

    return contagem


def resumo_geral(cur):
    """Retorna a taxa de acerto histórica geral, pra acompanhar a performance
    do sistema ao longo do tempo."""
    cur.execute(
        "SELECT resultado, COUNT(*) FROM historico_recomendacoes GROUP BY resultado"
    )
    return dict(cur.fetchall())


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        jogos_a_manter = buscar_jogos_recentes_a_manter(cur)
        recomendacoes = buscar_recomendacoes_para_arquivar(cur, jogos_a_manter)

        if not recomendacoes:
            print("Nada para arquivar no momento "
                  f"(mantendo detalhado as últimas {RODADAS_A_MANTER_DETALHADAS} rodadas jogadas).")
        else:
            contagem = arquivar(cur, recomendacoes)
            print(f"Arquivadas {len(recomendacoes)} recomendação(ões): "
                  f"{contagem['acertou']} acertou, {contagem['errou']} errou, "
                  f"{contagem['pendente']} ainda pendente (aguardando dados reais do jogo).")

        conn.commit()

        resumo = resumo_geral(cur)
        if resumo:
            total_avaliado = resumo.get("acertou", 0) + resumo.get("errou", 0)
            if total_avaliado > 0:
                taxa = round(100 * resumo.get("acertou", 0) / total_avaliado, 1)
                print(f"\nDesempenho histórico geral: {resumo.get('acertou', 0)}/{total_avaliado} "
                      f"acertos avaliados ({taxa}%). {resumo.get('pendente', 0)} ainda pendente(s).")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
