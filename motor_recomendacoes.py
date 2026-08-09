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

# NOVO (cartão x suspensão): quanto mais jogadores dos dois times estão a
# 1 cartão amarelo da suspensão automática (regra do Brasileirão: 3
# cartões = 1 jogo de suspensão), mais cauteloso o jogo tende a ser -
# ninguém quer arriscar ficar de fora do próximo jogo. Esse ajuste só
# REDUZ a probabilidade de "mais de X cartões" (por isso o teto é 1.0,
# nunca aumenta), com um piso pra não dominar sobre o padrão real mesmo
# em cenários extremos (ex: os 2 times inteiros na régua).
FATOR_SUSPENSAO_MINIMO = 0.80
FATOR_SUSPENSAO_MAXIMO = 1.00
FATOR_SUSPENSAO_ESCALA = 0.15
PESO_PADRAO_SEM_HISTORICO_CARTAO = 0.15  # jogador na régua sem padrão calculado ainda (poucos jogos) - peso neutro
TEMPORADA_ATUAL = 2026  # cartão não carrega de uma temporada pra outra

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
    # NOVO (bug corrigido): a checagem antiga procurava pelo texto exato
    # "escanteio total do jogo" (singular) - mas o nome real que a OddsPapi
    # manda é "Escanteios Total do Jogo" (plural, com "s"). Como a
    # comparação era de substring exata, o "s" extra quebrava a
    # correspondência e esse mercado NUNCA era classificado corretamente -
    # caía em None silenciosamente, e nenhuma recomendação de escanteio/
    # cartão total do jogo era gerada, mesmo com VE positivo confirmado.
    # Agora a checagem não depende de singular/plural exato.
    if "escanteio" in nome and "total do jogo" in nome:
        return "escanteio_total"
    # NOVO: "cart" (não "cartão"/"cartões" por extenso) porque "cartão"
    # (singular) e "cartões" (plural) têm radicais diferentes em português
    # - uma checagem por texto exato de um dos dois falharia pro outro,
    # exatamente como aconteceu com escanteio/escanteios.
    if "cart" in nome and "total do jogo" in nome:
        return "cartao_total"
    if "cartão" in nome or "cartao" in nome or "card" in nome:
        return "cartao"
    if "falta" in nome:
        return "falta_cometida"
    if "desarme" in nome or "tackle" in nome:
        return "desarme"
    # NOVO: "chute no gol" precisa ser checado ANTES do genérico "chute" -
    # senão "Chutes do Jogador" (chute total, sem "no gol" no nome) cairia
    # por engano no tipo errado, já que "chute" sozinho bate nos dois.
    if ("chute" in nome and "no gol" in nome) or "shotsongoal" in nome or "shots on goal" in nome:
        return "chute_no_gol"
    if "chute" in nome or "shot" in nome:
        return "chute_total"
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
    padroes_confronto_direto.

    NOVO (multi-time): também traz j.nosso_time_id - cada odd agora sabe de
    qual time rastreado ela é, em vez de assumir sempre Corinthians."""
    cur.execute(
        """
        SELECT o.id, o.jogo_id, o.jogador_id, o.casa_aposta, o.mercado,
               o.valor_odd, o.linha, o.direcao, j.data_jogo, j.adversario,
               j.mandante, j.arbitro, j.mandante_id, j.visitante_id, j.nosso_time_id
        FROM odds o
        JOIN jogos j ON j.id = o.jogo_id
        WHERE (j.datahora_jogo IS NOT NULL AND j.datahora_jogo >= NOW())
           OR (j.datahora_jogo IS NULL AND j.data_jogo >= CURRENT_DATE)
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


def buscar_frequencia_escanteio_time(cur, linha, time_id):
    cur.execute(
        "SELECT frequencia FROM padroes_time_escanteio WHERE linha = %s AND time_id = %s",
        (linha, time_id),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def buscar_frequencia_escanteio_total(cur, linha, time_id):
    """NOVO: frequência de escanteios do jogo INTEIRO (mandante + visitante
    somados) passar de uma linha - diferente de buscar_frequencia_escanteio_time,
    que olha só o lado do Corinthians. Filtra por time_id, já que essa
    tabela pode ter frequências diferentes calculadas pra times diferentes
    quando outros clubes forem adicionados."""
    cur.execute(
        "SELECT frequencia FROM padroes_escanteio_total WHERE linha = %s AND time_id = %s",
        (linha, time_id),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def buscar_frequencia_cartao_total(cur, linha, time_id):
    """NOVO: frequência de cartões do jogo INTEIRO (mandante + visitante
    somados) passar de uma linha. Filtra por time_id (ver docstring de
    buscar_frequencia_escanteio_total)."""
    cur.execute(
        "SELECT frequencia FROM padroes_cartao_total WHERE linha = %s AND time_id = %s",
        (linha, time_id),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def buscar_frequencia_resultado(cur, lado, resultado, time_id):
    cur.execute(
        "SELECT frequencia FROM padroes_time_resultado WHERE lado = %s AND resultado = %s AND time_id = %s",
        (lado, resultado, time_id),
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


def buscar_frequencia_confronto(cur, nosso_time_id, adversario_id, mandante_filtro, tipo_padrao, linha=0, resultado=""):
    """NOVO (confronto direto): busca a frequência específica contra esse
    adversário (ex: "cartões totais contra o Palmeiras, jogando em casa"),
    se já tiver sido calculada com uma amostra que não seja pequena demais.
    Retorna None se não houver dado suficiente - nesse caso, quem chamou
    essa função deve cair de volta pro padrão geral (não filtrado por
    adversário). Filtra por nosso_time_id (o time do qual estamos vendo o
    confronto - hoje sempre Corinthians), pra não colidir com o confronto
    do mesmo adversário visto de outro time no futuro."""
    if adversario_id is None:
        return None
    cur.execute(
        """SELECT frequencia FROM padroes_confronto_direto
           WHERE nosso_time_id = %s AND adversario_id = %s AND mandante_filtro = %s AND tipo_padrao = %s
             AND linha = %s AND resultado = %s AND amostra_pequena = FALSE""",
        (nosso_time_id, adversario_id, mandante_filtro, tipo_padrao, linha, resultado),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def buscar_frequencia_forma_recente(cur, resultado, time_id):
    """NOVO (forma recente): frequência de vitória/empate/derrota nos
    últimos jogos do time (qualquer adversário/mando de campo)."""
    cur.execute(
        "SELECT frequencia FROM padroes_forma_recente WHERE resultado = %s AND time_id = %s ORDER BY janela DESC LIMIT 1",
        (resultado, time_id),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def calcular_fator_forma_recente(cur, resultado_cor, time_id):
    """NOVO (forma recente): retorna o multiplicador a aplicar em cima da
    probabilidade de resultado final (vinda do confronto direto ou da média
    geral), com base em quanto o momento atual do time (últimos jogos) se
    desvia da referência de longo prazo pra esse mesmo resultado. Limitado
    ao intervalo [FATOR_FORMA_MINIMO, FATOR_FORMA_MAXIMO] - mesma filosofia
    do ajuste de árbitro: o momento recente BELISCA a probabilidade, nunca
    domina sobre um dado mais específico (como o confronto direto)."""
    baseline = buscar_frequencia_resultado(cur, "geral", resultado_cor, time_id)
    recente = buscar_frequencia_forma_recente(cur, resultado_cor, time_id)
    if baseline is None or recente is None or baseline == 0:
        return None

    fator = recente / baseline
    return max(FATOR_FORMA_MINIMO, min(FATOR_FORMA_MAXIMO, fator))


ULTIMOS_JOGOS_PARA_TITULARES = 3
QTD_TITULARES_PROVAVEIS = 11


def buscar_titulares_provaveis(cur, time_id):
    """NOVO (cartão x suspensão - só quem tende a jogar): os jogadores que
    mais apareceram como TITULAR nos últimos ULTIMOS_JOGOS_PARA_TITULARES
    jogos desse time - usado pra restringir o ajuste de cartão x suspensão
    só a quem tem chance real de entrar em campo. Reserva "na régua" que
    nem costuma jogar não muda o comportamento cauteloso do time, então
    não devia pesar na conta (incluir ele só dilui/exagera o ajuste à toa)."""
    if time_id is None:
        return set()
    cur.execute(
        "SELECT id FROM jogos WHERE nosso_time_id = %s AND placar_corinthians IS NOT NULL "
        "ORDER BY data_jogo DESC LIMIT %s",
        (time_id, ULTIMOS_JOGOS_PARA_TITULARES),
    )
    jogo_ids = [row[0] for row in cur.fetchall()]
    if not jogo_ids:
        return set()

    cur.execute(
        "SELECT jogador_id, COUNT(*) AS aparicoes FROM escalacoes "
        "WHERE jogo_id = ANY(%s) AND titular = TRUE "
        "GROUP BY jogador_id ORDER BY aparicoes DESC LIMIT %s",
        (jogo_ids, QTD_TITULARES_PROVAVEIS),
    )
    return {row[0] for row in cur.fetchall()}


def peso_jogadores_na_regua(cur, time_id):
    """NOVO (cartão x suspensão, ponderado por jogador): soma o "peso de
    cautela" dos jogadores desse time que estão a 1 cartão amarelo da
    suspensão automática (2 de 3 acumulados na TEMPORADA_ATUAL) - cada um
    pesa pela PRÓPRIA frequência histórica de tomar cartão
    (padroes_jogador_cartao), não conta igual pra todo mundo. Um
    zagueiro/volante com histórico alto de cartão (ex: 35% dos jogos) pesa
    muito mais nessa soma do que um atacante que quase nunca é cartonado
    (ex: 5%), mesmo os dois estando "na régua" no momento - o atacante
    dificilmente vai mudar o comportamento do jogo por estar cauteloso,
    o zagueiro/volante sim.
    Jogador na régua sem padrão de cartão calculado ainda (poucos jogos
    disputados) entra com um peso neutro (PESO_PADRAO_SEM_HISTORICO_CARTAO),
    em vez de simplesmente não contar - evita "sumir" da conta só por
    faltar dado, sem assumir o pior caso."""
    if time_id is None:
        return 0.0
    cur.execute(
        """
        SELECT c.jogador_id, j.data_jogo
        FROM cartoes c
        JOIN jogos j ON j.id = c.jogo_id
        JOIN jogadores jog ON jog.id = c.jogador_id
        WHERE c.cor = 'amarelo'
          AND EXTRACT(YEAR FROM j.data_jogo) = %s
          AND jog.ativo = TRUE
          AND jog.time_atual_id = %s
        ORDER BY c.jogador_id, j.data_jogo ASC, j.id ASC
        """,
        (TEMPORADA_ATUAL, time_id),
    )
    contagem = {}
    for jogador_id, _data_jogo in cur.fetchall():
        atual = contagem.get(jogador_id, 0) + 1
        contagem[jogador_id] = 0 if atual >= 3 else atual

    # NOVO: só considera quem tem chance real de jogar - reserva raramente
    # usado "na régua" não muda o comportamento do time, então não deveria
    # pesar (ver docstring de buscar_titulares_provaveis).
    titulares_provaveis = buscar_titulares_provaveis(cur, time_id)
    jogadores_na_regua = [
        jid for jid, v in contagem.items() if v == 2 and jid in titulares_provaveis
    ]
    if not jogadores_na_regua:
        return 0.0

    cur.execute(
        "SELECT jogador_id, frequencia FROM padroes_jogador_cartao WHERE jogador_id = ANY(%s)",
        (jogadores_na_regua,),
    )
    frequencias = {jid: float(freq) for jid, freq in cur.fetchall()}

    return sum(
        (frequencias[jid] / 100) if jid in frequencias else PESO_PADRAO_SEM_HISTORICO_CARTAO
        for jid in jogadores_na_regua
    )


def calcular_fator_suspensao(cur, nosso_time_id, adversario_id):
    """NOVO (cartão x suspensão): multiplicador a aplicar na probabilidade
    de "mais de X" cartões total do jogo, com base no peso combinado dos
    jogadores dos DOIS times que estão a 1 cartão da suspensão (ver
    peso_jogadores_na_regua - pondera por histórico individual de cartão,
    não conta todo jogador igual). Só reduz, nunca aumenta (teto 1.0) - e
    tem piso (FATOR_SUSPENSAO_MINIMO), pra não dominar sobre o padrão real
    mesmo com muita gente na régua dos dois lados.
    Retorna None quando ninguém está na régua (não belisca nada, evita
    ficar marcando toda recomendação com um fator 1.00x que não diz nada)."""
    peso_total = peso_jogadores_na_regua(cur, nosso_time_id) + peso_jogadores_na_regua(cur, adversario_id)
    if peso_total == 0:
        return None
    fator = 1.0 - (FATOR_SUSPENSAO_ESCALA * peso_total)
    return max(FATOR_SUSPENSAO_MINIMO, min(FATOR_SUSPENSAO_MAXIMO, fator))


def buscar_media_geral_cartoes(cur):
    """NOVO: média geral de cartões por jogo, calculada a partir de todos os
    árbitros com perfil já calculado. Serve de linha de base pra saber se um
    árbitro específico dá mais ou menos cartão que a média."""
    cur.execute("SELECT AVG(media_cartoes) FROM padroes_arbitro")
    row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def buscar_media_geral_faltas(cur):
    """NOVO: mesma ideia de buscar_media_geral_cartoes, mas pra falta - usa
    o dado que já existia calculado (padroes_arbitro.media_faltas) mas
    nunca tinha sido aproveitado em nenhum ajuste."""
    cur.execute("SELECT AVG(media_faltas) FROM padroes_arbitro")
    row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def buscar_perfil_arbitro(cur, arbitro):
    cur.execute(
        "SELECT media_cartoes, media_faltas FROM padroes_arbitro WHERE arbitro = %s",
        (arbitro,),
    )
    return cur.fetchone()


def calcular_fator_arbitro(cur, arbitro, media_geral, tipo):
    """Retorna o multiplicador a aplicar na probabilidade, com base em
    quanto esse árbitro se desvia da média geral. Limitado ao intervalo
    [FATOR_ARBITRO_MINIMO, FATOR_ARBITRO_MAXIMO]. Retorna None se não
    houver árbitro definido, perfil calculado, ou média geral disponível.

    NOVO: `tipo` decide se usa a média de CARTÃO ou de FALTA do árbitro -
    antes só existia ajuste de cartão, mesmo a média de falta já sendo
    calculada e salva há um tempo (nunca tinha sido usada). Faz sentido
    aplicar nos dois: árbitro rigoroso marca mais falta E mais cartão;
    árbitro que "deixa o jogo rolar" marca menos dos dois."""
    if not arbitro or media_geral is None or media_geral == 0:
        return None

    perfil = buscar_perfil_arbitro(cur, arbitro)
    if not perfil:
        return None

    media_cartoes_arbitro, media_faltas_arbitro = perfil
    media_arbitro = media_cartoes_arbitro if tipo == "cartao" else media_faltas_arbitro
    if media_arbitro is None:
        return None

    fator = float(media_arbitro) / media_geral
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
    """Evita recomendar aposta em jogador que provavelmente não vai jogar
    (suspenso, lesionado, cortado do time).

    Prioridade 0 - lesão/suspensão reportada (API-Football /injuries,
    coletada em atualizar_odds.py): se existe um registro pra esse
    jogador nesse jogo específico, é o sinal mais direto e com mais
    antecedência que temos - normalmente aparece dias antes do jogo, bem
    antes da escalação sair. Bloqueia direto, sem precisar dos fallbacks
    abaixo.

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
    cur.execute(
        "SELECT 1 FROM lesoes_suspensoes WHERE jogo_id = %s AND jogador_id = %s",
        (jogo_id, jogador_id),
    )
    if cur.fetchone() is not None:
        return False

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

    # NOVO: calcula a média geral de cartões e faltas uma única vez, fora do loop
    media_geral_cartoes = buscar_media_geral_cartoes(cur)
    media_geral_faltas = buscar_media_geral_faltas(cur)

    # NOVO (multi-time): nome de cada time (+ apelidos conhecidos, já que o
    # mesmo time pode aparecer com nomes diferentes em partes diferentes da
    # resposta da OddsPapi), usado pra montar a descrição do resultado
    # final com o nome certo e pra identificar de quem é um mercado de
    # escanteio_time (ver uso mais abaixo).
    cur.execute("SELECT id, nome, apelidos FROM times")
    variantes_times = {}
    nomes_times = {}
    for time_id_row, nome_row, apelidos_row in cur.fetchall():
        nomes_times[time_id_row] = nome_row
        variantes_times[time_id_row] = {nome_row.lower()} | {a.lower() for a in (apelidos_row or [])}

    for (odd_id, jogo_id, jogador_id, casa, mercado, valor_odd,
         linha, direcao, data_jogo, adversario, mandante, arbitro,
         mandante_id, visitante_id, nosso_time_id) in odds:

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
        fator_suspensao_aplicado = None

        # NOVO (confronto direto): identifica o adversário por ID (não por
        # texto - evita o problema de nomes grafados diferente entre
        # fontes) e o filtro de mandante/visitante correspondente, usados
        # pra tentar uma frequência específica contra esse adversário antes
        # de cair pro padrão geral do time.
        # NOVO (multi-time): usa nosso_time_id (vindo da própria linha de
        # odds, via jogos.nosso_time_id) em vez de um id fixo - cada jogo
        # já sabe de qual time rastreado ele é.
        adversario_id = None
        if nosso_time_id is not None and mandante_id is not None and visitante_id is not None:
            adversario_id = visitante_id if mandante_id == nosso_time_id else mandante_id
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
                fator = calcular_fator_arbitro(cur, arbitro, media_geral_cartoes, "cartao")
                if fator is not None:
                    frequencia_bruta = min(round(frequencia_bruta * fator, 2), 100.0)
                    fator_arbitro_aplicado = fator

                frequencia = frequencia_bruta if direcao_normalizada == "sim" else round(100 - frequencia_bruta, 2)

        elif tipo == "falta_cometida" and jogador_id \
                and direcao_normalizada in ("mais", "menos") and linha is not None:
            # NOVO: falta cometida agora também recebe ajuste de árbitro -
            # separado do bloco genérico de linha (desarme/chute), porque só
            # falta tem relação com o perfil do árbitro (árbitro rigoroso
            # apita mais falta, não faz o jogador chutar mais no gol).
            frequencia_bruta = buscar_frequencia_linha_jogador(cur, jogador_id, tipo, linha)
            if frequencia_bruta is not None:
                fator = calcular_fator_arbitro(cur, arbitro, media_geral_faltas, "falta")
                if fator is not None:
                    frequencia_bruta = min(round(frequencia_bruta * fator, 2), 100.0)
                    fator_arbitro_aplicado = fator

                frequencia = frequencia_bruta if direcao_normalizada == "mais" else round(100 - frequencia_bruta, 2)

        elif tipo in ("desarme", "chute_no_gol", "chute_total") and jogador_id \
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
            # NOVO (multi-time): descobrimos que o MESMO time aparece com
            # nomes DIFERENTES em partes diferentes da resposta da OddsPapi
            # (ex: Athletico Paranaense é "CA Paranaense PR" na lista de
            # jogos, mas "Atletico Paranaense" dentro do texto do mercado
            # de escanteio) - o mesmo tipo de divergência de nome que já
            # vimos entre times/jogadores em outras partes do projeto.
            # Comparar com um nome só (nem o nosso, nem o do adversário) não
            # é confiável sozinho - testa vários candidatos de cada lado:
            # o nome salvo em `times` pra cada time (via mandante_id/
            # visitante_id) e o texto de `adversario` já resolvido antes.
            mercado_lower = mercado.lower()
            adversario_id_calc = None
            if mandante_id is not None and visitante_id is not None:
                adversario_id_calc = visitante_id if mandante_id == nosso_time_id else mandante_id

            candidatos_nosso_time = variantes_times.get(nosso_time_id, set())
            candidatos_adversario = variantes_times.get(adversario_id_calc, set()) | {(adversario or "").lower()}

            bate_nosso_time = any(c and c in mercado_lower for c in candidatos_nosso_time)
            bate_adversario = any(c and c in mercado_lower for c in candidatos_adversario)

            # só aplica quando bate com A GENTE e não bate com o adversário -
            # se dermos match nos dois (nomes parecidos) ou nenhum (nome
            # totalmente diferente dos dois, formato desconhecido ainda),
            # não arrisca aplicar errado - fica sem recomendação por
            # segurança, em vez de aplicar a frequência do time errado.
            if bate_nosso_time and not bate_adversario:
                frequencia_bruta = buscar_frequencia_escanteio_time(cur, linha, nosso_time_id)
                if frequencia_bruta is not None:
                    frequencia = frequencia_bruta if direcao_normalizada == "mais" else round(100 - frequencia_bruta, 2)

        elif tipo == "escanteio_total" and not jogador_id \
                and direcao_normalizada in ("mais", "menos") and linha is not None:
            # NOVO (confronto direto): tenta primeiro a frequência específica
            # contra esse adversário (ex: "escanteios totais contra o
            # Palmeiras, jogando em casa"); só cai pro padrão geral do time
            # se não houver confronto direto com amostra suficiente ainda.
            frequencia_bruta = buscar_frequencia_confronto(
                cur, nosso_time_id, adversario_id, mandante_filtro_atual, "escanteio_total", linha=linha
            )
            if frequencia_bruta is not None:
                veio_de_confronto_direto = True
            else:
                frequencia_bruta = buscar_frequencia_escanteio_total(cur, linha, nosso_time_id)
            if frequencia_bruta is not None:
                frequencia = frequencia_bruta if direcao_normalizada == "mais" else round(100 - frequencia_bruta, 2)

        elif tipo == "cartao_total" and not jogador_id \
                and direcao_normalizada in ("mais", "menos") and linha is not None:
            # NOVO (confronto direto): mesma lógica de prioridade do escanteio
            # total acima.
            frequencia_bruta = buscar_frequencia_confronto(
                cur, nosso_time_id, adversario_id, mandante_filtro_atual, "cartao_total", linha=linha
            )
            if frequencia_bruta is not None:
                veio_de_confronto_direto = True
            else:
                frequencia_bruta = buscar_frequencia_cartao_total(cur, linha, nosso_time_id)

            # NOVO (cartão x suspensão): belisca a frequência "mais de X"
            # (seja ela do confronto direto ou do padrão geral) pra baixo
            # quando tem muita gente na régua da suspensão nos dois times -
            # ver docstring de calcular_fator_suspensao. Aplicado ANTES de
            # separar mais/menos, pra "menos" herdar o complemento certo
            # (100 - frequência já ajustada), sem precisar duplicar a conta.
            if frequencia_bruta is not None:
                fator_suspensao = calcular_fator_suspensao(cur, nosso_time_id, adversario_id)
                if fator_suspensao is not None:
                    frequencia_bruta = round(frequencia_bruta * fator_suspensao, 2)
                    fator_suspensao_aplicado = fator_suspensao

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
                    cur, nosso_time_id, adversario_id, mandante_filtro_atual, "resultado_final", resultado=resultado_cor
                )
                if frequencia is not None:
                    veio_de_confronto_direto = True
                else:
                    lado = "mandante" if mandante else "visitante"
                    frequencia = buscar_frequencia_resultado(cur, lado, resultado_cor, nosso_time_id)

                # NOVO (forma recente): belisca a probabilidade (seja ela do
                # confronto direto ou do padrão geral) com base no momento
                # atual do time - nunca domina sobre a fonte principal, só
                # ajusta dentro de um intervalo estreito (ver docstring de
                # calcular_fator_forma_recente).
                if frequencia is not None:
                    fator_forma = calcular_fator_forma_recente(cur, resultado_cor, nosso_time_id)
                    if fator_forma is not None:
                        frequencia = min(round(frequencia * fator_forma, 2), 100.0)
                        fator_forma_aplicado = fator_forma

        if frequencia is None:
            continue  # não temos padrão calculado pra cruzar com essa odd ainda

        probabilidade = frequencia / 100
        valor_esperado = round((probabilidade * float(valor_odd)) - 1, 3)

        descricao_final = mercado
        if tipo == "resultado_final":
            nome_nosso_time = nomes_times.get(nosso_time_id, "nosso time")
            nomes = {
                "vitoria": f"Vitória do {nome_nosso_time}",
                "empate": "Empate",
                "derrota": f"Derrota do {nome_nosso_time}",
            }
            descricao_final = f"Resultado Final - {nomes[resultado_cor]}"

        if fator_arbitro_aplicado is not None:
            descricao_final += f" (ajustado pelo árbitro, fator {fator_arbitro_aplicado:.2f}x)"

        if fator_forma_aplicado is not None:
            descricao_final += f" (ajustado pela forma recente, fator {fator_forma_aplicado:.2f}x)"

        if fator_suspensao_aplicado is not None:
            descricao_final += f" (ajustado por jogadores na régua da suspensão, fator {fator_suspensao_aplicado:.2f}x)"

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
                "direcao": direcao_normalizada,
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
        "DELETE FROM recomendacoes WHERE jogo_id IN ("
        "  SELECT id FROM jogos WHERE "
        "  (datahora_jogo IS NOT NULL AND datahora_jogo >= NOW()) "
        "  OR (datahora_jogo IS NULL AND data_jogo >= CURRENT_DATE)"
        ")"
    )

    for r in recomendacoes:
        cur.execute(
            """INSERT INTO recomendacoes
               (jogo_id, jogador_id, tipo_padrao, descricao, casa_aposta,
                odd_oferecida, probabilidade_historica, valor_esperado, linha, direcao)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                r["jogo_id"], r["jogador_id"], r["tipo_padrao"], r["descricao"],
                r["casa_aposta"], r["odd_oferecida"], r["probabilidade_historica"],
                r["valor_esperado"], r.get("linha"), r.get("direcao"),
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
