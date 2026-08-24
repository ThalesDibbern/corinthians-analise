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

NOVO (Onda 2): dupla_chance_1t/2t, ambas_marcam_1t/2t e marca_ambos_tempos -
usam também placar_corinthians_intervalo/placar_adversario_intervalo (o
placar no intervalo), pra derivar o resultado de cada tempo isolado.
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


# NOVO (22/08/2026 - fechando a segunda metade da proteção da janela de
# espera): mesmo valor de JANELA_ESPERA_ESTATISTICA_HORAS em
# popular_banco.py. Repetido aqui em vez de importado porque avaliacao.py
# é módulo compartilhado (app.py e arquivar_recomendacoes.py dependem
# dele) e não deve puxar um script de coleta junto - importar
# popular_banco.py aqui arrastaria requests, chaves de API e a lógica de
# cota pra dentro do site. Se um dia esse número mudar, mudar nos dois.
JANELA_ESPERA_ESTATISTICA_HORAS = 6


def jogo_totalmente_processado(cur, jogo_id):
    """Um jogo é considerado "totalmente processado" quando as estatísticas
    de TIME dos dois lados já foram salvas E o jogo já saiu da janela de
    espera - depois disso, não vai chegar mais dado novo pra esse jogo.
    Usado pra distinguir "ainda não temos o dado desse jogador" (esperar
    mais) de "esse jogador simplesmente não jogou esse jogo" (pode avaliar
    como zero, sem ficar pendente pra sempre) - sem isso, uma
    recomendação/aposta de jogador que ficou no banco sem entrar (chute no
    gol, falta, desarme, impedimento, cartão) nunca tinha jeito de ser
    avaliada, porque nunca ia aparecer uma linha em
    jogador_estatisticas_jogo pra ele.

    NOVO (22/08/2026 - JANELA DE ESPERA):
    -------------------------------------
    Antes essa função checava APENAS se existiam as 2 linhas em
    `estatisticas_jogo`. Isso repetia exatamente o erro de raciocínio do
    bug original de "estatística coletada no meio do jogo": confundir
    EXISTÊNCIA do dado com MATURIDADE do dado.

    A janela de 6h em popular_banco.py protege a COLETA (rebusca a
    estatística mesmo já existindo linha salva, enquanto o jogo for
    recente). Mas a AVALIAÇÃO roda logo em seguida, no mesmo cron
    (popular_banco -> ... -> arquivar_recomendacoes), e continuava
    perguntando só "existem os 2 lados?".

    Consequência: rodar a cadeia completa pouco depois de um jogo gravava
    dado parcial, encontrava as 2 linhas, dava a avaliação como definitiva
    e CONGELAVA acertou/errou em cima de números do meio do segundo tempo.
    Depois o popular_banco corrigia a estatística, mas a avaliação já
    estava gravada.

    O cron das 9h nunca sofreu disso (jogos da noite anterior já passaram
    das 6h), mas um "Run Now" manual logo após um jogo, sim.

    Agora exige as DUAS condições. Devolver False aqui significa
    "pendente" em todos os pontos de uso (nunca "errou"), então o pior
    caso é adiar a avaliação pro próximo cron - que é exatamente o
    comportamento desejado.

    Não afeta os mercados de placar (gols_total, gols_time, equipe_marca,
    dupla chance/ambas marcam por tempo, resultado_final): esses não
    passam por aqui de propósito, porque o placar vem confiável assim que
    o fixture fecha em "FT" - quem sofre o atraso é o endpoint de
    estatísticas."""
    cur.execute(
        """
        SELECT
            (SELECT COUNT(DISTINCT lado) FROM estatisticas_jogo WHERE jogo_id = %s) AS lados,
            (NOW() - COALESCE(j.datahora_jogo, j.data_jogo::timestamp))
                >= (%s * INTERVAL '1 hour') AS fora_da_janela
        FROM jogos j
        WHERE j.id = %s
        """,
        (jogo_id, JANELA_ESPERA_ESTATISTICA_HORAS, jogo_id),
    )
    row = cur.fetchone()
    if row is None:
        return False
    lados, fora_da_janela = row
    return lados == 2 and bool(fora_da_janela)


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

    # NOVO (Onda 2 - Dupla Chance/Ambas Marcam por tempo, Marca em Ambos os
    # Tempos): os quatro usam jogos.mandante + o placar final E o placar de
    # intervalo (pra derivar o placar de cada tempo isolado - 1º tempo é
    # direto o intervalo, 2º tempo é final menos intervalo).
    if tipo_padrao in ("dupla_chance_1t", "dupla_chance_2t", "ambas_marcam_1t",
                       "ambas_marcam_2t", "marca_ambos_tempos"):
        cur.execute(
            """
            SELECT mandante, placar_corinthians, placar_adversario,
                   placar_corinthians_intervalo, placar_adversario_intervalo
            FROM jogos WHERE id = %s
            """,
            (jogo_id,),
        )
        row = cur.fetchone()
        if row is None:
            return "pendente"
        mandante, placar_nosso_final, placar_adv_final, placar_nosso_int, placar_adv_int = row
        if (placar_nosso_final is None or placar_adv_final is None
                or placar_nosso_int is None or placar_adv_int is None):
            return "pendente"

        if tipo_padrao == "marca_ambos_tempos":
            gols_1t = placar_nosso_int
            gols_2t = placar_nosso_final - placar_nosso_int
            return avaliar_binario(gols_1t > 0 and gols_2t > 0, d)

        if tipo_padrao in ("dupla_chance_1t", "ambas_marcam_1t"):
            placar_nosso_periodo = placar_nosso_int
            placar_adv_periodo = placar_adv_int
        else:  # 2T
            placar_nosso_periodo = placar_nosso_final - placar_nosso_int
            placar_adv_periodo = placar_adv_final - placar_adv_int

        if tipo_padrao in ("ambas_marcam_1t", "ambas_marcam_2t"):
            ambas_marcaram = placar_nosso_periodo > 0 and placar_adv_periodo > 0
            return avaliar_binario(ambas_marcaram, d)

        # dupla_chance_1t/2t: resultado desse tempo, do ponto de vista de
        # mandante/visitante (1X/12/2X) - mesma convenção "1"=mandante,
        # "2"=visitante que resultado_final já usa.
        if placar_nosso_periodo == placar_adv_periodo:
            resultado_periodo = "empate"
        elif placar_nosso_periodo > placar_adv_periodo:
            resultado_periodo = "vitoria"
        else:
            resultado_periodo = "derrota"

        if resultado_periodo == "empate":
            acertou = d in ("1x", "2x")
        elif resultado_periodo == "vitoria":
            acertou = (d in ("1x", "12")) if mandante else (d in ("2x", "12"))
        else:
            acertou = (d in ("2x", "12")) if mandante else (d in ("1x", "12"))
        return "acertou" if acertou else "errou"

    # NOVO (Handicap Asiático - só linha de meio gol, ver decisão de
    # arquitetura de 17/08/2026): `linha` é o handicap bruto (convenção
    # OddsPapi, sempre relativo ao mandante). Com placar de futebol
    # sempre inteiro, meio gol nunca empata matematicamente - resultado
    # sempre binário, sem "push".
    if tipo_padrao == "handicap_asiatico":
        cur.execute(
            "SELECT mandante, placar_corinthians, placar_adversario FROM jogos WHERE id = %s",
            (jogo_id,),
        )
        row = cur.fetchone()
        if row is None or row[1] is None or row[2] is None:
            return "pendente"
        mandante, placar_nosso, placar_adversario = row
        diferenca = placar_nosso - placar_adversario
        limite = -linha if mandante else linha
        return "acertou" if diferenca > limite else "errou"

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
