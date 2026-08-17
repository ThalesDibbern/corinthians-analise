"""
Módulo compartilhado de avaliação - a ÚNICA fonte de verdade pra decidir
se uma aposta (automática, vinda do motor de recomendações, OU manual,
anotada pelo usuário no "Criador de Odd") acertou ou errou, comparando com
o resultado real de um jogo já coletado.

EXTRAÍDO (antes existiam DUAS cópias dessa lógica - uma em
arquivar_recomendacoes.py, pras recomendações automáticas, outra em
app.py, pras apostas manuais - e a cópia manual não cobria todos os
mercados nem tinha a mesma correção de bug que a automática já tinha
ganho). Ter duas cópias já causou divergência real nesse projeto antes
(ver combinacoes.py) - agora as duas fontes de aposta usam exatamente a
mesma função.

Cobre os 10 tipos de mercado que o projeto sabe calcular a partir do
próprio banco (API-Football): cartao, falta_cometida, desarme,
chute_no_gol, chute_total, falta_sofrida, impedimento, escanteio_time,
escanteio_total, cartao_total, resultado_final.

ATUALIZADO: "cartao" agora cobre DOIS formatos de mercado, distinguidos
por jogador_id ser preenchido ou não - correção do bug de plural em
atualizar_odds.py/motor_recomendacoes.py ("Cartões" não batia com
"cartão"/"cartao") destravou a captura de "Cartões - Mais/Menos Equipe
1/2" (por TIME, mercado de linha), que antes nem chegava a ser salvo.
Até então "cartao" só existia no formato de JOGADOR (mercado binário
Sim/Não, jogador_id sempre preenchido).

NOVO: gols_total (jogo inteiro), gols_time (só nosso lado) e equipe_marca
(Sim/Não) - os três usam direto jogos.placar_corinthians/placar_adversario,
sem depender de estatisticas_jogo.
"""


def normalizar(direcao):
    return (direcao or "").strip().lower()


def avaliar_linha(valor_real, linha, direcao):
    """Confere um mercado de linha (Mais/Menos), usando a mesma definição de
    fronteira usada no cálculo da frequência histórica: "mais" conta valores
    ESTRITAMENTE acima da linha; "menos" é o complementar (valores até a
    linha, inclusive) - os dois juntos cobrem 100% dos casos, sem sobra nem
    lacuna."""
    if direcao == "mais":
        return "acertou" if valor_real > float(linha) else "errou"
    if direcao == "menos":
        return "acertou" if valor_real <= float(linha) else "errou"
    return "pendente"  # direção desconhecida/não reconhecida - não arrisca avaliar


def avaliar_binario(ocorreu, direcao):
    """Confere um mercado binário (Sim/Não - ex: jogador recebeu cartão)."""
    if direcao == "sim":
        return "acertou" if ocorreu else "errou"
    if direcao in ("não", "nao"):
        return "acertou" if not ocorreu else "errou"
    return "pendente"


def jogo_totalmente_processado(cur, jogo_id):
    """Um jogo é considerado "totalmente processado" quando as estatísticas
    de TIME dos dois lados já foram salvas - depois disso, não vai chegar
    mais dado novo pra esse jogo (popular_banco.py já processou ele por
    completo, pra sempre). Usado pra distinguir "ainda não temos o dado
    desse jogador" (esperar mais) de "esse jogador simplesmente não jogou
    esse jogo" (pode avaliar como zero, sem ficar pendente pra sempre) -
    sem isso, uma recomendação/aposta de jogador que ficou no banco sem
    entrar (chute no gol, falta, desarme, impedimento, cartão) nunca tinha
    jeito de ser avaliada, porque nunca ia aparecer uma linha em
    jogador_estatisticas_jogo pra ele."""
    cur.execute(
        "SELECT COUNT(DISTINCT lado) FROM estatisticas_jogo WHERE jogo_id = %s",
        (jogo_id,),
    )
    row = cur.fetchone()
    return row is not None and row[0] == 2


def avaliar_resultado(cur, tipo_padrao, jogador_id, jogo_id, linha, descricao, direcao):
    """Compara uma aposta (automática ou manual) com o resultado real do
    jogo, se disponível. Retorna 'acertou', 'errou' ou 'pendente' (se ainda
    não temos o dado real)."""
    d = normalizar(direcao)

    if tipo_padrao == "cartao":
        if jogador_id is not None:
            # Cartão de JOGADOR (mercado binário Sim/Não) - formato original.
            cur.execute("SELECT 1 FROM cartoes WHERE jogo_id = %s AND jogador_id = %s", (jogo_id, jogador_id))
            recebeu_cartao = cur.fetchone() is not None
            if recebeu_cartao:
                return avaliar_binario(True, d)
            if jogo_totalmente_processado(cur, jogo_id):
                return avaliar_binario(False, d)
            return "pendente"

        # NOVO: cartão por TIME (mercado de linha Mais/Menos, "Cartões -
        # Mais/Menos Equipe 1/2") - jogador_id vem None. Destravado pela
        # correção do bug de plural em atualizar_odds.py/
        # motor_recomendacoes.py ("Cartões" não batia com "cartão"/
        # "cartao"). Mesmo padrão de "lado" (mandante/visitante) já usado
        # em escanteio_time - soma os cartões (tabela `cartoes` já traz
        # `lado` por linha) do lado do NOSSO time nesse jogo específico.
        if not jogo_totalmente_processado(cur, jogo_id):
            return "pendente"
        cur.execute("SELECT mandante FROM jogos WHERE id = %s", (jogo_id,))
        info_jogo = cur.fetchone()
        if not info_jogo:
            return "pendente"
        lado = "mandante" if info_jogo[0] else "visitante"
        cur.execute("SELECT COUNT(*) FROM cartoes WHERE jogo_id = %s AND lado = %s", (jogo_id, lado))
        total_cartoes_time = cur.fetchone()[0]
        return avaliar_linha(float(total_cartoes_time), linha, d)

    if tipo_padrao in ("falta_cometida", "desarme", "chute_no_gol", "chute_total", "falta_sofrida"):
        coluna = {
            "falta_cometida": "faltas_cometidas",
            "desarme": "desarmes",
            "chute_no_gol": "chutes_no_gol",
            "chute_total": "chutes",
            "falta_sofrida": "faltas_sofridas",
        }[tipo_padrao]
        cur.execute(
            f"SELECT {coluna} FROM jogador_estatisticas_jogo WHERE jogo_id = %s AND jogador_id = %s",
            (jogo_id, jogador_id),
        )
        row = cur.fetchone()
        if row is not None and row[0] is not None:
            return avaliar_linha(float(row[0]), linha, d)
        if jogo_totalmente_processado(cur, jogo_id):
            return avaliar_linha(0.0, linha, d)
        return "pendente"

    if tipo_padrao == "impedimento":
        cur.execute(
            "SELECT impedimentos FROM jogador_estatisticas_jogo WHERE jogo_id = %s AND jogador_id = %s",
            (jogo_id, jogador_id),
        )
        row = cur.fetchone()
        if row is not None and row[0] is not None:
            return avaliar_binario(row[0] > 0, d)
        if jogo_totalmente_processado(cur, jogo_id):
            return avaliar_binario(False, d)
        return "pendente"

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
        return avaliar_linha(float(row[0]), linha, d)

    if tipo_padrao == "escanteio_total":
        cur.execute(
            """
            SELECT SUM(escanteios), COUNT(DISTINCT lado)
            FROM estatisticas_jogo WHERE jogo_id = %s AND escanteios IS NOT NULL
            """,
            (jogo_id,),
        )
        row = cur.fetchone()
        if row is None or row[0] is None or row[1] != 2:
            return "pendente"  # falta o dado de algum dos dois lados ainda
        return avaliar_linha(float(row[0]), linha, d)

    if tipo_padrao == "cartao_total":
        if not jogo_totalmente_processado(cur, jogo_id):
            return "pendente"  # jogo ainda não totalmente processado
        cur.execute("SELECT COUNT(*) FROM cartoes WHERE jogo_id = %s", (jogo_id,))
        total_cartoes = cur.fetchone()[0]
        return avaliar_linha(float(total_cartoes), linha, d)

    # NOVO (Mais/Menos gols e Equipe Marca): os três usam direto
    # jogos.placar_corinthians/placar_adversario - não precisa de
    # jogo_totalmente_processado aqui, porque o placar final vem pronto
    # assim que o fixture fecha como "FT" (não depende do endpoint de
    # estatísticas, que é o que sofre o lag que motivou aquela checagem em
    # outros mercados - ver seção do bug de estatística coletada no meio
    # do jogo).
    if tipo_padrao in ("gols_total", "gols_time", "equipe_marca"):
        cur.execute(
            "SELECT placar_corinthians, placar_adversario FROM jogos WHERE id = %s",
            (jogo_id,),
        )
        row = cur.fetchone()
        if row is None or row[0] is None or row[1] is None:
            return "pendente"
        placar_nosso, placar_adversario = row
        if tipo_padrao == "gols_total":
            return avaliar_linha(float(placar_nosso + placar_adversario), linha, d)
        if tipo_padrao == "gols_time":
            return avaliar_linha(float(placar_nosso), linha, d)
        return avaliar_binario(placar_nosso > 0, d)

    if tipo_padrao == "resultado_final":
        cur.execute(
            "SELECT placar_corinthians, placar_adversario FROM jogos WHERE id = %s",
            (jogo_id,),
        )
        row = cur.fetchone()
        if row is None or row[0] is None or row[1] is None:
            return "pendente"
        placar_cor, placar_adv = row
        if placar_cor > placar_adv:
            resultado_real = "vitória"
        elif placar_cor == placar_adv:
            resultado_real = "empate"
        else:
            resultado_real = "derrota"
        # a descrição salva foi montada como "Resultado Final - Vitória do Corinthians" etc.
        return "acertou" if resultado_real in (descricao or "").lower() else "errou"

    return "pendente"
