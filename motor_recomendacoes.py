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

NOVO (confronto direto): pros mercados de escanteio total, cartão total e
resultado final, o sistema tenta primeiro usar a frequência histórica
ESPECÍFICA contra aquele adversário (ex: "cartões totais contra o Palmeiras,
jogando em casa"), calculada pelo motor_padroes.py em padroes_confronto_direto.
Só cai pra média geral do time (sem filtrar por adversário) se não houver
confronto direto com amostra suficiente ainda. Isso captura rivalidades e
mandos de campo específicos que a média geral não enxerga (ex: um confronto
historicamente mais truncado, ou um adversário que o Corinthians nunca perde
em casa). Quando esse dado é usado, a descrição da recomendação ganha o sufixo
"(confronto direto)".

NOVO (forma recente): pro mercado de resultado final, depois de decidir a
probabilidade principal (confronto direto ou média geral), o sistema aplica
um pequeno ajuste baseado no "momento atual" do time (últimos 5 jogos,
independente de adversário) - times em boa fase têm a probabilidade de
vitória/empate levemente puxada pra cima, times em má fase levemente pra
baixo. O ajuste é limitado a um intervalo estreito (0.85x a 1.15x) e NUNCA
domina sobre o confronto direto ou a média geral - só "belisca" o número,
igual já acontece com o ajuste de árbitro em cartão/falta.

NOVO (disponibilidade de jogador): antes de gerar qualquer recomendação de
mercado específico de jogador (cartão, falta, desarme, chute, impedimento),
o sistema checa se ele provavelmente vai jogar. Prioridade 1: escalação
CONFIRMADA da partida específica, se já capturada pelo popular_banco.py.
Prioridade 2 (fallback, quando a escalação da partida ainda não saiu):
olha se o jogador apareceu em pelo menos 1 dos últimos 3 jogos - se sumiu
das 3 escalações seguidas, é sinal de lesão/suspensão/corte do time, e a
recomendação é descartada. Evita recomendar aposta em jogador fora de
campo.

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

# NOVO (forma recente): mesma filosofia do ajuste de árbitro - o momento
# atual do time só belisca a probabilidade de resultado final, nunca domina
# sobre uma fonte mais específica (como o confronto direto).
FATOR_FORMA_MINIMO = 0.85
FATOR_FORMA_MAXIMO = 1.15

# NOVO (disponibilidade de jogador): quantos jogos recentes olhar pra decidir
# se um jogador "sumiu" da escalação (sinal de lesão/suspensão/corte do
# time) - só usado quando ainda não temos a escalação confirmada da
# partida específica (ver jogador_disponivel).
JOGOS_JANELA_DISPONIBILIDADE = 3


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
    """Busca odds de jogos que ainda não aconteceram. Traz também o árbitro
    do jogo (j.arbitro), usado no ajuste de cartão/falta.

    NOVO (confronto direto): também traz mandante_id/visitante_id, usados
    pra identificar o time adversário por ID (não por texto - evita o
    problema de nomes grafados diferente entre fontes) e cruzar com
    padroes_confronto_direto."""
    cur.execute(
        """
        SELECT o.id, o.jogo_id, o.jogador_id, o.casa_aposta, o.mercado,
               o.valor_odd, o.linha, o.direcao, j.data_jogo, j.adversario,
               j.mandante, j.arbitro, j.mandante_id, j.visitante_id
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


def buscar_id_corinthians(cur):
    """NOVO (confronto direto): busca o id do Corinthians na tabela `times`,
    usado pra identificar o adversário de cada jogo por ID (comparando com
    mandante_id/visitante_id), em vez de por texto."""
    cur.execute("SELECT id FROM times WHERE nome = %s", ("Corinthians",))
    row = cur.fetchone()
    return row[0] if row else None


def buscar_frequencia_confronto(cur, adversario_id, mandante_filtro, tipo_padrao, linha=0, resultado=""):
    """NOVO (confronto direto): busca a frequência específica contra esse
    adversário (ex: "cartões totais contra o Palmeiras, jogando em casa"),
    se já tiver sido calculada com uma amostra que não seja pequena demais.
    Retorna None se não houver dado suficiente - nesse caso, quem chamou
    essa função deve cair de volta pro padrão geral (não filtrado por
    adversário)."""
    if adversario_id is None:
        return None
    cur.execute(
        """SELECT frequencia FROM padroes_confronto_direto
           WHERE adversario_id = %s AND mandante_filtro = %s AND tipo_padrao = %s
             AND linha = %s AND resultado = %s AND amostra_pequena = FALSE""",
        (adversario_id, mandante_filtro, tipo_padrao, linha, resultado),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def buscar_frequencia_forma_recente(cur, resultado):
    """NOVO (forma recente): frequência de vitória/empate/derrota nos
    últimos jogos do Corinthians (qualquer adversário/mando de campo)."""
    cur.execute(
        "SELECT frequencia FROM padroes_forma_recente WHERE resultado = %s ORDER BY janela DESC LIMIT 1",
        (resultado,),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def calcular_fator_forma_recente(cur, resultado_cor):
    """NOVO (forma recente): retorna o multiplicador a aplicar em cima da
    probabilidade de resultado final (vinda do confronto direto ou da média
    geral), com base em quanto o momento atual do time (últimos jogos) se
    desvia da referência de longo prazo pra esse mesmo resultado. Limitado
    ao intervalo [FATOR_FORMA_MINIMO, FATOR_FORMA_MAXIMO] - mesma filosofia
    do ajuste de árbitro: o momento recente BELISCA a probabilidade, nunca
    domina sobre um dado mais específico (como o confronto direto)."""
    baseline = buscar_frequencia_resultado(cur, "geral", resultado_cor)
    recente = buscar_frequencia_forma_recente(cur, resultado_cor)
    if baseline is None or recente is None or baseline == 0:
        return None

    fator = recente / baseline
    return max(FATOR_FORMA_MINIMO, min(FATOR_FORMA_MAXIMO, fator))


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


def jogador_disponivel(cur, jogador_id, jogo_id):
    """NOVO: evita recomendar aposta em jogador que provavelmente não vai
    jogar (suspenso, lesionado, cortado do time).

    Prioridade 1 - escalação CONFIRMADA da partida específica: a API-Football
    normalmente libera isso só perto do jogo (às vezes só ~1h antes), então
    nem sempre vai estar disponível quando esse script rodar. Se já tiver
    sido capturada (jogo_tem_escalacao), essa é a fonte mais confiável -
    usa ela e ignora qualquer outra coisa.

    Prioridade 2 - fallback pros últimos 3 jogos: se ainda não temos a
    escalação confirmada dessa partida específica, olha se o jogador
    apareceu (titular OU reserva, não precisa ter entrado em campo) em pelo
    menos 1 dos últimos 3 jogos concluídos. Se sumiu das 3 escalações
    seguidas, é sinal razoável de lesão/suspensão/saída do time. Se só
    ficou de fora uma vez (rotação normal), continua sendo tratado como
    disponível.

    Se não houver dado de escalação suficiente pra decidir (pipeline ainda
    não processou, ou jogador muito novo no banco), NÃO bloqueia - dado
    insuficiente não deve descartar uma recomendação que poderia ser boa."""
    cur.execute("SELECT COUNT(*) FROM escalacoes WHERE jogo_id = %s", (jogo_id,))
    tem_escalacao_confirmada = cur.fetchone()[0] > 0

    if tem_escalacao_confirmada:
        cur.execute(
            "SELECT 1 FROM escalacoes WHERE jogo_id = %s AND jogador_id = %s",
            (jogo_id, jogador_id),
        )
        return cur.fetchone() is not None

    cur.execute(
        """
        SELECT COUNT(*) FROM escalacoes
        WHERE jogo_id IN (
            SELECT id FROM jogos WHERE data_jogo < CURRENT_DATE ORDER BY data_jogo DESC LIMIT %s
        )
        """,
        (JOGOS_JANELA_DISPONIBILIDADE,),
    )
    tem_dado_recente_geral = cur.fetchone()[0] > 0

    if not tem_dado_recente_geral:
        return True  # sem dado suficiente pra decidir - não bloqueia

    cur.execute(
        """
        SELECT COUNT(*) FROM escalacoes
        WHERE jogador_id = %s
          AND jogo_id IN (
              SELECT id FROM jogos WHERE data_jogo < CURRENT_DATE ORDER BY data_jogo DESC LIMIT %s
          )
        """,
        (jogador_id, JOGOS_JANELA_DISPONIBILIDADE),
    )
    apareceu_nos_recentes = cur.fetchone()[0]
    return apareceu_nos_recentes > 0


def calcular_recomendacoes(cur):
    odds = buscar_odds_futuras(cur)
    recomendacoes = []
    jogadores_indisponiveis_pulados = 0

    # NOVO: calcula a média geral de cartões uma única vez, fora do loop
    media_geral_cartoes = buscar_media_geral_cartoes(cur)

    # NOVO (confronto direto): id do Corinthians, calculado uma única vez,
    # usado pra identificar o adversário de cada jogo por ID.
    corinthians_id = buscar_id_corinthians(cur)

    for (odd_id, jogo_id, jogador_id, casa, mercado, valor_odd,
         linha, direcao, data_jogo, adversario, mandante, arbitro,
         mandante_id, visitante_id) in odds:

        tipo = identificar_tipo_padrao(mercado)
        if tipo is None:
            continue

        # NOVO: pula qualquer mercado de jogador específico se ele
        # provavelmente não vai jogar (ver docstring de jogador_disponivel).
        if jogador_id and not jogador_disponivel(cur, jogador_id, jogo_id):
            jogadores_indisponiveis_pulados += 1
            continue

        frequencia = None
        resultado_cor = None
        fator_arbitro_aplicado = None
        veio_de_confronto_direto = False
        fator_forma_aplicado = None

        # NOVO (confronto direto): identifica o adversário por ID (não por
        # texto - evita o problema de nomes grafados diferente entre
        # fontes) e o filtro de mandante/visitante correspondente, usados
        # pra tentar uma frequência específica contra esse adversário antes
        # de cair pro padrão geral do time.
        adversario_id = None
        if corinthians_id is not None and mandante_id is not None and visitante_id is not None:
            adversario_id = visitante_id if mandante_id == corinthians_id else mandante_id
        mandante_filtro_atual = "mandante" if mandante else "visitante"

        # NOVO: suporte ao lado "Menos"/"Não" de cada mercado, além do "Mais"/
        # "Sim" que já existia. A tabela de padrão sempre guarda a frequência
        # do lado "Mais"/"Sim" (ex: "frequência de passar de 7.5 escanteios");
        # o lado oposto tem frequência complementar (100 - frequência), já
        # que os dois lados juntos somam 100% dos jogos. Sem isso, o sistema
        # deixava de considerar metade de cada mercado - e é comum o lado
        # "Menos" ter Valor Esperado positivo mesmo quando o "Mais" não tem
        # (a odd de cada lado é precificada separadamente pela casa).
        direcao_normalizada = (direcao or "").strip().lower()

        if tipo == "cartao" and jogador_id and direcao_normalizada in ("sim", "não", "nao"):
            frequencia_bruta = buscar_frequencia_cartao(cur, jogador_id)
            if frequencia_bruta is not None:
                # NOVO: aplica o ajuste de árbitro, se disponível - sempre em
                # cima da frequência do lado "Sim", antes de inverter pro "Não"
                fator = calcular_fator_arbitro(cur, arbitro, media_geral_cartoes)
                if fator is not None:
                    frequencia_bruta = min(round(frequencia_bruta * fator, 2), 100.0)
                    fator_arbitro_aplicado = fator

                frequencia = frequencia_bruta if direcao_normalizada == "sim" else round(100 - frequencia_bruta, 2)

        elif tipo in ("falta_cometida", "desarme", "chute_no_gol") and jogador_id \
                and direcao_normalizada in ("mais", "menos") and linha is not None:
            frequencia_bruta = buscar_frequencia_linha_jogador(cur, jogador_id, tipo, linha)
            if frequencia_bruta is not None:
                frequencia = frequencia_bruta if direcao_normalizada == "mais" else round(100 - frequencia_bruta, 2)

        elif tipo == "impedimento" and jogador_id and direcao_normalizada in ("sim", "não", "nao"):
            frequencia_bruta = buscar_frequencia_simples_jogador(cur, jogador_id, "impedimento")
            if frequencia_bruta is not None:
                frequencia = frequencia_bruta if direcao_normalizada == "sim" else round(100 - frequencia_bruta, 2)

        elif tipo == "escanteio_time" and not jogador_id \
                and direcao_normalizada in ("mais", "menos") and linha is not None:
            # NOVO: o padrão de escanteio_time (padroes_time_escanteio) só é
            # calculado com base nos jogos do Corinthians - não temos base
            # histórica de outros times ainda. Sem essa checagem, o sistema
            # aplicava por engano a frequência do Corinthians em mercados de
            # escanteio do ADVERSÁRIO (ex: "Escanteios - Mais/Menos Clube do
            # Remo PA"), gerando recomendação com probabilidade errada.
            # Quando outros times tiverem padrão próprio calculado, trocar
            # essa checagem fixa por uma busca dinâmica pelo time certo.
            if "corinthians" in mercado.lower():
                frequencia_bruta = buscar_frequencia_escanteio_time(cur, linha)
                if frequencia_bruta is not None:
                    frequencia = frequencia_bruta if direcao_normalizada == "mais" else round(100 - frequencia_bruta, 2)

        elif tipo == "escanteio_total" and not jogador_id \
                and direcao_normalizada in ("mais", "menos") and linha is not None:
            # NOVO (confronto direto): tenta primeiro a frequência específica
            # contra esse adversário (ex: "escanteios totais contra o
            # Palmeiras, jogando em casa"); só cai pro padrão geral do time
            # se não houver confronto direto com amostra suficiente ainda.
            frequencia_bruta = buscar_frequencia_confronto(
                cur, adversario_id, mandante_filtro_atual, "escanteio_total", linha=linha
            )
            if frequencia_bruta is not None:
                veio_de_confronto_direto = True
            else:
                frequencia_bruta = buscar_frequencia_escanteio_total(cur, linha)
            if frequencia_bruta is not None:
                frequencia = frequencia_bruta if direcao_normalizada == "mais" else round(100 - frequencia_bruta, 2)

        elif tipo == "cartao_total" and not jogador_id \
                and direcao_normalizada in ("mais", "menos") and linha is not None:
            # NOVO (confronto direto): mesma lógica de prioridade do escanteio
            # total acima.
            frequencia_bruta = buscar_frequencia_confronto(
                cur, adversario_id, mandante_filtro_atual, "cartao_total", linha=linha
            )
            if frequencia_bruta is not None:
                veio_de_confronto_direto = True
            else:
                frequencia_bruta = buscar_frequencia_cartao_total(cur, linha)
            if frequencia_bruta is not None:
                frequencia = frequencia_bruta if direcao_normalizada == "mais" else round(100 - frequencia_bruta, 2)

        elif tipo == "resultado_final" and not jogador_id:
            resultado_cor = resultado_do_ponto_de_vista_corinthians(direcao, mandante)
            if resultado_cor:
                # NOVO (confronto direto): tenta primeiro o resultado
                # específico contra esse adversário (ex: "Corinthians nunca
                # perde pro São Paulo em casa"); só cai pro padrão geral por
                # mandante/visitante se não houver confronto direto com
                # amostra suficiente ainda.
                frequencia = buscar_frequencia_confronto(
                    cur, adversario_id, mandante_filtro_atual, "resultado_final", resultado=resultado_cor
                )
                if frequencia is not None:
                    veio_de_confronto_direto = True
                else:
                    lado = "mandante" if mandante else "visitante"
                    frequencia = buscar_frequencia_resultado(cur, lado, resultado_cor)

                # NOVO (forma recente): belisca a probabilidade (seja ela do
                # confronto direto ou do padrão geral) com base no momento
                # atual do time - nunca domina sobre a fonte principal, só
                # ajusta dentro de um intervalo estreito (ver docstring de
                # calcular_fator_forma_recente).
                if frequencia is not None:
                    fator_forma = calcular_fator_forma_recente(cur, resultado_cor)
                    if fator_forma is not None:
                        frequencia = min(round(frequencia * fator_forma, 2), 100.0)
                        fator_forma_aplicado = fator_forma

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

        if fator_forma_aplicado is not None:
            descricao_final += f" (ajustado pela forma recente, fator {fator_forma_aplicado:.2f}x)"

        if veio_de_confronto_direto:
            descricao_final += " (confronto direto)"

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

    if jogadores_indisponiveis_pulados:
        print(f"  ({jogadores_indisponiveis_pulados} odd(s) de jogador ignorada(s) por "
              f"indisponibilidade - fora da escalação recente/confirmada.)")

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
