"""
Motor de padrões - Cartões, faltas, desarmes, chutes e impedimentos de
jogador + Escanteios do time + perfil de cada árbitro + NOVO: escanteios e
cartões TOTAIS do jogo (mandante + visitante somados).

Para cada jogador com dados suficientes, calcula a frequência histórica de
cada padrão (ex: recebeu cartão, cometeu falta, teve X+ desarmes...). Para
o time, calcula a frequência de passar de cada linha de escanteios testada
(só o lado do Corinthians). Para o jogo como um todo, calcula a mesma coisa
mas somando os dois lados (escanteios e cartões totais) - mercados desse
tipo tendem a ter frequência histórica mais alta que os de um lado só,
aumentando a chance de gerar recomendação com odd mais baixa. Para cada
árbitro, calcula a média de cartões e faltas nos jogos que ele apitou (soma
os dois times, não só o Corinthians - a ideia é capturar o "jeito de
apitar" dele, que vale pro jogo inteiro).
Todos considerando os últimos 50 jogos disponíveis (ou todos os jogos
apitados, no caso do árbitro).

Feito para rodar automaticamente todo dia (depois que o script de coleta
de dados já rodou), recalculando os padrões com os dados mais recentes.

Variáveis de ambiente necessárias:
  - DATABASE_URL -> a URL de conexão do Postgres (mesma usada nos outros scripts)
"""

import os
import psycopg2

from tabela import calcular_tabela

DATABASE_URL = os.environ["DATABASE_URL"]

JOGOS_MINIMOS_PARA_ANALISAR = 5   # não vale a pena calcular padrão com poucos jogos
JANELA_MAXIMA_DE_JOGOS = 50       # olha no máximo os últimos 50 jogos

# NOVO: quantos jogos recentes do Corinthians olhar pra decidir se um
# jogador ainda está no elenco - se ele não aparecer (nem titular, nem
# reserva) jogando pelo Corinthians em nenhum desses jogos, é marcado como
# inativo (ver calcular_jogadores_ativos). 15 jogos é ~4-5 meses de
# ausência - bem mais que qualquer lesão comum, então é um sinal
# confiável de saída do time (transferência, empréstimo).
JANELA_ATIVIDADE_JOGADOR = 15

# linhas de escanteio testadas, no mesmo padrão que as casas de aposta usam
# (mercados "mais de X.5 escanteios")
LINHAS_ESCANTEIO = [3.5, 4.5, 5.5, 6.5, 7.5]

# NOVO: linhas testadas pro escanteio TOTAL do jogo (mandante + visitante
# somados) - naturalmente mais alto que o escanteio só do Corinthians
LINHAS_ESCANTEIO_TOTAL = [7.5, 8.5, 9.5, 10.5, 11.5, 12.5]

# NOVO: linhas testadas pro cartão TOTAL do jogo (mandante + visitante
# somados)
LINHAS_CARTAO_TOTAL = [2.5, 3.5, 4.5, 5.5]

# NOVO (confronto direto): linhas testadas pra falta total do jogo (mandante
# + visitante somados) e chutes no gol total do jogo (soma das estatísticas
# individuais dos jogadores, já que não há total agregado por lado salvo)
LINHAS_FALTA_TOTAL = [15.5, 18.5, 21.5, 24.5]
LINHAS_CHUTE_TOTAL = [4.5, 6.5, 8.5, 10.5, 12.5]

# NOVO (confronto direto): confronto direto naturalmente tem poucos jogos
# disputados (2 a 4 por temporada, às vezes menos) - usamos um piso mais
# permissivo que o geral (JOGOS_MINIMOS_PARA_ANALISAR=5), mas o resultado
# fica marcado como "amostra pequena" quando ainda estiver abaixo do piso
# geral, pra quem for usar esse dado saber que é uma estimativa mais frágil.
JOGOS_MINIMOS_CONFRONTO = 3

# novos padrões por jogador: nome do tipo -> (coluna no banco, linhas testadas)
PADROES_LINHA_JOGADOR = {
    "falta_cometida": ("faltas_cometidas", [0.5, 1.5, 2.5]),
    "desarme": ("desarmes", [0.5, 1.5, 2.5]),
    "chute_no_gol": ("chutes_no_gol", [0.5, 1.5]),
    # NOVO: chute total (dentro + fora do gol) - diferente de "chute no gol",
    # que já existia. Usa a coluna `chutes`, já coletada desde o início do
    # projeto (jogador_estatisticas_jogo.chutes), nunca aproveitada até
    # agora. Confirmado no catálogo da OddsPapi (marketType "players-shots"),
    # mas ainda não confirmamos se a Superbet publica preço real pra esse
    # mercado no Brasileirão - se não publicar, essa recomendação nunca
    # aparece, sem quebrar nada.
    "chute_total": ("chutes", [0.5, 1.5, 2.5, 3.5]),
    # NOVO: faltas sofridas - confirmado que NÃO existe mercado real na
    # OddsPapi/Superbet (nem "cometida" nem "sofrida" têm preço real hoje).
    # Fica só como estatística informativa em /jogadores e /clube/<time>,
    # e disponível pra "criar aposta manual" (onde o usuário anota a odd
    # dele mesmo, sem depender de mercado nosso). O dado já era coletado
    # desde o início do projeto (jogador_estatisticas_jogo.faltas_sofridas),
    # nunca tinha virado padrão.
    "falta_sofrida": ("faltas_sofridas", [0.5, 1.5, 2.5]),
}

# NOVO (estatísticas de time): linhas testadas pros padrões de TIME que
# faltavam - faltas, chutes (finalizações) e cartões, sempre só do NOSSO
# lado (diferente de LINHAS_FALTA_TOTAL/LINHAS_CHUTE_TOTAL/LINHAS_CARTAO_TOTAL,
# que somam os dois times do jogo). Servem pra alimentar a página
# "Estatísticas de Times" (igual a de jogadores, mas por time) - não
# dependem de odd disponível em lugar nenhum, então cobrem justamente os
# mercados que não têm preço real na Superbet (faltas, chutes), do mesmo
# jeito que /jogadores já faz pra jogador.
LINHAS_FALTA_TIME = [9.5, 11.5, 13.5, 15.5, 17.5]
LINHAS_CHUTE_TIME = [8.5, 10.5, 12.5, 14.5, 16.5]
LINHAS_CARTAO_TIME = [0.5, 1.5, 2.5, 3.5]

# padrões novos por TIME: nome do tipo -> (coluna em estatisticas_jogo, linhas testadas)
PADROES_LINHA_TIME_ESTATISTICA = {
    "falta": ("faltas", LINHAS_FALTA_TIME),
    "chute": ("finalizacoes", LINHAS_CHUTE_TIME),
}

# NOVO (estatísticas de time - mais completas): essas três não têm coluna
# pronta em estatisticas_jogo (a API não manda um total agregado por lado
# pra elas) - são calculadas somando a estatística individual de cada
# jogador do NOSSO time que entrou em campo, jogo a jogo, usando
# jogador_estatisticas_jogo (que já coleta isso por jogador desde o início
# do projeto). Ex: chutes no gol do time = soma dos chutes no gol de todos
# os jogadores do time naquele jogo.
LINHAS_CHUTE_NO_GOL_TIME = [3.5, 4.5, 5.5, 6.5]
LINHAS_IMPEDIMENTO_TIME = [0.5, 1.5, 2.5]
LINHAS_DESARME_TIME = [12.5, 15.5, 18.5, 21.5]

# padrões novos por TIME (soma das estatísticas de jogador): nome do tipo -> (coluna em jogador_estatisticas_jogo, linhas testadas)
PADROES_LINHA_TIME_SOMA_JOGADOR = {
    "chute_no_gol": ("chutes_no_gol", LINHAS_CHUTE_NO_GOL_TIME),
    "impedimento": ("impedimentos", LINHAS_IMPEDIMENTO_TIME),
    "desarme": ("desarmes", LINHAS_DESARME_TIME),
}

# NOVO (Mais/Menos gols e Equipe Marca): gols vêm direto de jogos.placar_*
# (não de estatisticas_jogo, e não sofrem do lag que motivou a janela de
# espera de estatística - o placar final já vem certo assim que o fixture
# fecha como "FT"). Linhas mais baixas que escanteio/cartão porque gol é
# um evento bem mais raro por jogo.
LINHAS_GOLS_TIME = [0.5, 1.5, 2.5]
LINHAS_GOLS_TOTAL = [1.5, 2.5, 3.5, 4.5]

# NOVO (Handicap Asiático - só linha de meio gol, ver decisão de
# arquitetura de 17/08/2026): linhas candidatas testadas por time - lista
# curta de propósito, cobre a faixa que realmente aparece precificada na
# Superbet pro Brasileirão (handicaps grandes tipo -5.5 quase nunca são
# oferecidos de verdade).
LINHAS_HANDICAP = [-2.5, -1.5, -0.5, 0.5, 1.5, 2.5]

# padrões simples (sim/não teve pelo menos 1 no jogo)
PADROES_FREQUENCIA_JOGADOR = {
    "impedimento": "impedimentos",
}

# ajuste do fator de árbitro: limita o quanto a probabilidade de um jogador
# pode ser puxada pra cima/baixo com base no árbitro, pra não deixar o
# sistema "confiar demais" numa amostra que ainda é pequena por árbitro
FATOR_ARBITRO_MINIMO = 0.85
FATOR_ARBITRO_MAXIMO = 1.15


def calcular_jogadores_ativos(cur):
    """NOVO: marca cada jogador como ativo/inativo, olhando se ele apareceu
    (titular ou reserva) jogando PELO time dele em pelo menos 1 dos últimos
    JANELA_ATIVIDADE_JOGADOR jogos concluídos DESSE time. Quem não aparece
    em nenhum deles é marcado inativo - normalmente jogador que foi
    transferido/emprestado pra fora, ou (efeito colateral útil) jogador de
    time adversário (não rastreado) que só está na tabela `jogadores` por
    ter aparecido como oponente em algum jogo.

    Totalmente reversível: se o jogador voltar a aparecer numa escalação
    de um time rastreado, volta pra ativo sozinho na próxima execução.

    NOVO (multi-time): percorre TODOS os times rastreados (não só o
    Corinthians), cada um com sua própria janela de "últimos jogos" e seu
    próprio elenco - um jogador fica marcado ativo pro time em que
    realmente jogou recentemente, nunca por outro. jogadores.time_atual_id
    é preenchido de acordo."""
    times_rastreados = buscar_times_rastreados(cur)
    if not times_rastreados:
        return 0, 0

    ativos_por_jogador = {}  # jogador_id -> time_id (pra qual time ele está ativo)

    for time_id, time_nome, time_api_football_id in times_rastreados:
        cur.execute(
            """
            SELECT id FROM jogos
            WHERE nosso_time_id = %s
              AND ((datahora_jogo IS NOT NULL AND datahora_jogo < NOW())
                OR (datahora_jogo IS NULL AND data_jogo < CURRENT_DATE))
            ORDER BY COALESCE(datahora_jogo, data_jogo::timestamp) DESC
            LIMIT %s
            """,
            (time_id, JANELA_ATIVIDADE_JOGADOR),
        )
        jogos_recentes = [row[0] for row in cur.fetchall()]
        if not jogos_recentes:
            continue  # esse time ainda não tem jogo concluído suficiente - pula, sem travar os outros

        cur.execute(
            """
            SELECT DISTINCT e.jogador_id
            FROM escalacoes e
            JOIN jogador_estatisticas_jogo jeg ON jeg.jogo_id = e.jogo_id AND jeg.jogador_id = e.jogador_id
            JOIN jogos jg ON jg.id = e.jogo_id
            WHERE e.jogo_id = ANY(%s)
              AND ((jeg.lado = 'mandante' AND jg.mandante = TRUE)
                OR (jeg.lado = 'visitante' AND jg.mandante = FALSE))
            """,
            (jogos_recentes,),
        )
        for (jogador_id,) in cur.fetchall():
            ativos_por_jogador[jogador_id] = time_id

    cur.execute("SELECT id, ativo, time_atual_id FROM jogadores")
    todos_com_time = cur.fetchall()

    for jogador_id, ativo_atual, time_atual_salvo in todos_com_time:
        deve_estar_ativo = jogador_id in ativos_por_jogador
        novo_time_atual = ativos_por_jogador.get(jogador_id)
        # NOVO: bug real corrigido - antes só atualizava quando o campo
        # `ativo` mudava de valor. Jogador CRIADO AGORA já nasce com
        # ativo=TRUE (valor padrão da coluna) - então, mesmo estando
        # corretamente ativo, a comparação "mudou?" dava falso, e o
        # `time_atual_id` nunca era preenchido (ficava sempre NULL). Agora
        # também atualiza quando só o time_atual_id está errado/faltando.
        if deve_estar_ativo != ativo_atual or novo_time_atual != time_atual_salvo:
            cur.execute(
                "UPDATE jogadores SET ativo = %s, time_atual_id = %s WHERE id = %s",
                (deve_estar_ativo, novo_time_atual, jogador_id),
            )

    # NOVO: reporta a contagem final de verdade (quantos ESTÃO ativos/inativos
    # agora), não só quantos mudaram de estado nessa execução.
    total_ativos = len(ativos_por_jogador)
    total_inativos = len(todos_com_time) - total_ativos

    return total_ativos, total_inativos


def calcular_padroes_cartao(cur):
    """Para cada jogador, olha seus últimos jogos e calcula a frequência de cartão.

    NOVO: filtra só os jogos em que o jogador estava jogando PELO time dele
    (lado dele bate com o lado do time naquele jogo específico) - sem isso,
    um jogador que trocou de time tinha os dois períodos misturados na
    mesma frequência, distorcendo o padrão real dele hoje.

    NOVO (multi-time): também filtra por nosso_time_id (jogos.nosso_time_id
    = jogadores.time_atual_id) - agora que a tabela `jogos` pode ter jogos
    de mais de um time rastreado, sem esse filtro um jogador do Corinthians
    poderia acidentalmente "ver" jogos do Athletico Paranaense (ou
    vice-versa) se a condição de lado/mandante coincidisse por acaso."""
    cur.execute("SELECT id, nome, time_atual_id FROM jogadores WHERE ativo = TRUE")
    jogadores = cur.fetchall()

    resultados = []

    for jogador_id, nome, time_atual_id in jogadores:
        cur.execute(
            """
            SELECT jeg.cartao_amarelo, jeg.cartao_vermelho
            FROM jogador_estatisticas_jogo jeg
            JOIN jogos j ON j.id = jeg.jogo_id
            WHERE jeg.jogador_id = %s AND j.nosso_time_id = %s
              AND ((jeg.lado = 'mandante' AND j.mandante = TRUE)
                OR (jeg.lado = 'visitante' AND j.mandante = FALSE))
            ORDER BY j.data_jogo DESC
            LIMIT %s
            """,
            (jogador_id, time_atual_id, JANELA_MAXIMA_DE_JOGOS),
        )
        jogos_do_jogador = cur.fetchall()

        jogos_analisados = len(jogos_do_jogador)
        if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
            continue  # dados de menos pra confiar no padrão ainda

        jogos_com_cartao = sum(
            1 for amarelo, vermelho in jogos_do_jogador
            if (amarelo or 0) > 0 or (vermelho or 0) > 0
        )
        frequencia = round(100 * jogos_com_cartao / jogos_analisados, 2)

        resultados.append((jogador_id, nome, jogos_analisados, jogos_com_cartao, frequencia))

    return resultados


def salvar_padroes(cur, resultados):
    for jogador_id, nome, jogos_analisados, jogos_com_cartao, frequencia in resultados:
        cur.execute(
            """
            INSERT INTO padroes_jogador_cartao (jogador_id, jogos_analisados, jogos_com_cartao, frequencia, atualizado_em)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (jogador_id) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                jogos_com_cartao = EXCLUDED.jogos_com_cartao,
                frequencia = EXCLUDED.frequencia,
                atualizado_em = NOW()
            """,
            (jogador_id, jogos_analisados, jogos_com_cartao, frequencia),
        )
        print(f"  {nome}: {jogos_com_cartao}/{jogos_analisados} jogos com cartão ({frequencia}%)")


def calcular_padroes_escanteio(cur, time_id, lado="geral"):
    """Olha os escanteios DESSE time (não do adversário) nos últimos jogos
    dele, e calcula a frequência de passar de cada linha testada (3.5, 4.5, ...).
    NOVO (multi-time): filtra por nosso_time_id - sem isso, misturaria
    escanteios de jogos de times rastreados diferentes.

    NOVO (25/08/2026 - separação por mando): o parâmetro `lado` aceita
    'mandante', 'visitante' ou 'geral'. Motivo, medido na base inteira
    (1652 jogos, 5 temporadas):

        mandante  5.82 escanteios/jogo
        visitante 4.54 escanteios/jogo
        vantagem  1.29  (estável: 1.34 / 1.32 / 1.20 / 1.28 / 1.28 por ano)

    Até aqui existia UM número por (time, linha), misturando casa e fora,
    enquanto a casa de apostas precifica os dois separadamente. Isso vale
    ~7 pontos de probabilidade em toda linha, sempre no mesmo sentido -
    mais que a margem da casa (~5%), e portanto suficiente pra fabricar VE
    positivo onde não existe.

    Sintoma na auditoria da rodada de 22-24/08/2026: das 74 recomendações
    de escanteio de time, 69 estavam no lado errado (44 de "Menos" pra
    mandante e 25 de "Mais" pra visitante), com taxa de acerto de 24.3%
    contra 54.3% previstos.

    IMPORTANTE - o filtro de lado vem ANTES do LIMIT. Se viesse depois, o
    recorte pegaria ~metade dos últimos 50 jogos e a janela efetiva ficaria
    sem controle. Assim cada lado tem sua própria janela de
    JANELA_MAXIMA_DE_JOGOS.

    Vale lembrar que `estatisticas_jogo.lado` é o mando REAL do jogo (ao
    contrário de `cartoes.lado`, que é relativo ao nosso time) - por isso
    a tradução via `j.mandante` abaixo está correta aqui."""
    if lado == "mandante":
        filtro_lado = "AND j.mandante = TRUE"
    elif lado == "visitante":
        filtro_lado = "AND j.mandante = FALSE"
    else:
        filtro_lado = ""

    cur.execute(
        f"""
        SELECT eg.escanteios
        FROM estatisticas_jogo eg
        JOIN jogos j ON j.id = eg.jogo_id
        WHERE j.nosso_time_id = %s
          AND ((j.mandante = TRUE AND eg.lado = 'mandante')
           OR (j.mandante = FALSE AND eg.lado = 'visitante'))
          {filtro_lado}
        ORDER BY j.data_jogo DESC
        LIMIT %s
        """,
        (time_id, JANELA_MAXIMA_DE_JOGOS),
    )
    linhas_brutas = [row[0] for row in cur.fetchall() if row[0] is not None]

    jogos_analisados = len(linhas_brutas)
    if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
        return None, jogos_analisados

    media = round(sum(float(v) for v in linhas_brutas) / jogos_analisados, 2)

    resultados = []
    for linha in LINHAS_ESCANTEIO:
        jogos_acima = sum(1 for v in linhas_brutas if float(v) > linha)
        frequencia = round(100 * jogos_acima / jogos_analisados, 2)
        resultados.append((linha, jogos_analisados, jogos_acima, frequencia, media))

    return resultados, jogos_analisados


def salvar_padroes_escanteio(cur, resultados, time_id, lado="geral"):
    """ATENÇÃO: o ON CONFLICT usa (time_id, linha, lado). A constraint
    antiga era (time_id, linha) - a migração
    `migrar_padroes_escanteio_lado.py` PRECISA rodar antes deste código
    subir, senão o INSERT quebra e derruba o cron."""
    for linha, jogos_analisados, jogos_acima, frequencia, media in resultados:
        cur.execute(
            """
            INSERT INTO padroes_time_escanteio (time_id, linha, lado, jogos_analisados, jogos_acima_da_linha, frequencia, media, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (time_id, linha, lado) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                jogos_acima_da_linha = EXCLUDED.jogos_acima_da_linha,
                frequencia = EXCLUDED.frequencia,
                media = EXCLUDED.media,
                atualizado_em = NOW()
            """,
            (time_id, linha, lado, jogos_analisados, jogos_acima, frequencia, media),
        )
        print(f"  [{lado}] Mais de {linha} escanteios: {jogos_acima}/{jogos_analisados} jogos ({frequencia}%)")


def calcular_padrao_linha_time_estatistica(cur, time_id, coluna, linhas_testadas):
    """NOVO (estatísticas de time): generaliza calcular_padroes_escanteio pra
    qualquer coluna de estatisticas_jogo (faltas, finalizações) - mesma
    lógica: só o lado do NOSSO time, traduzindo o lado real (mandante/
    visitante da API-Football) pra "nosso time" via jogos.mandante (ver
    docstring de calcular_padroes_escanteio pra mais detalhe dessa
    tradução)."""
    cur.execute(
        f"""
        SELECT eg.{coluna}
        FROM estatisticas_jogo eg
        JOIN jogos j ON j.id = eg.jogo_id
        WHERE j.nosso_time_id = %s
          AND ((j.mandante = TRUE AND eg.lado = 'mandante')
           OR (j.mandante = FALSE AND eg.lado = 'visitante'))
          AND eg.{coluna} IS NOT NULL
        ORDER BY j.data_jogo DESC
        LIMIT %s
        """,
        (time_id, JANELA_MAXIMA_DE_JOGOS),
    )
    valores = [row[0] for row in cur.fetchall()]

    jogos_analisados = len(valores)
    if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
        return None, jogos_analisados

    media = round(sum(float(v) for v in valores) / jogos_analisados, 2)

    resultados = []
    for linha in linhas_testadas:
        jogos_acima = sum(1 for v in valores if float(v) > linha)
        frequencia = round(100 * jogos_acima / jogos_analisados, 2)
        resultados.append((linha, jogos_analisados, jogos_acima, frequencia, media))

    return resultados, jogos_analisados


def calcular_padrao_cartao_time(cur, time_id, linhas_testadas):
    """NOVO (estatísticas de time): cartões (amarelo + vermelho) recebidos
    por jogadores do NOSSO time em cada jogo - diferente de
    padroes_cartao_total (que soma os dois times do jogo). Usa a tabela
    cartoes diretamente: o campo lado ali já é gravado relativo ao
    NOSSO time (ver popular_banco.py/salvar_eventos), não ao
    mandante/visitante real do jogo - por isso, diferente da função acima,
    não precisa de nenhuma tradução via jogos.mandante.
    CORRIGIDO: mesmo bug de contagem duplicada de calcular_padroes_cartao_total
    (JOIN direto com cartoes depois de já ter juntado com estatisticas_jogo,
    que tem 2 linhas por jogo, duplicava cada cartão) - agora conta numa
    subconsulta separada."""
    cur.execute(
        """
        SELECT contagem.total_cartoes
        FROM (
            SELECT j.id AS jogo_id, j.data_jogo,
                   (SELECT COUNT(*) FROM cartoes c WHERE c.jogo_id = j.id AND c.lado = 'mandante') AS total_cartoes,
                   COUNT(DISTINCT eg.lado) AS lados
            FROM jogos j
            JOIN estatisticas_jogo eg ON eg.jogo_id = j.id
            WHERE j.nosso_time_id = %s AND j.data_jogo < CURRENT_DATE
            GROUP BY j.id, j.data_jogo
        ) contagem
        WHERE contagem.lados = 2
        ORDER BY contagem.data_jogo DESC
        LIMIT %s
        """,
        (time_id, JANELA_MAXIMA_DE_JOGOS),
    )
    valores = [row[0] for row in cur.fetchall()]

    jogos_analisados = len(valores)
    if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
        return None, jogos_analisados

    media = round(sum(float(v) for v in valores) / jogos_analisados, 2)

    resultados = []
    for linha in linhas_testadas:
        jogos_acima = sum(1 for v in valores if float(v) > linha)
        frequencia = round(100 * jogos_acima / jogos_analisados, 2)
        resultados.append((linha, jogos_analisados, jogos_acima, frequencia, media))

    return resultados, jogos_analisados


def salvar_padrao_linha_time(cur, tipo, resultados, time_id):
    """NOVO (estatísticas de time): salva em `padroes_time_linha` - tabela
    nova, separada de padroes_time_escanteio (que já existia antes e
    continua do jeito que estava, pra não quebrar nada que já dependia
    dela). tipo diferencia falta/chute/cartão dentro da mesma tabela."""
    for linha, jogos_analisados, jogos_acima, frequencia, media in resultados:
        cur.execute(
            """
            INSERT INTO padroes_time_linha
                (time_id, tipo, linha, jogos_analisados, jogos_acima, frequencia, media, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (time_id, tipo, linha) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                jogos_acima = EXCLUDED.jogos_acima,
                frequencia = EXCLUDED.frequencia,
                media = EXCLUDED.media,
                atualizado_em = NOW()
            """,
            (time_id, tipo, linha, jogos_analisados, jogos_acima, frequencia, media),
        )
        print(f"  [{tipo}] Mais de {linha}: {jogos_acima}/{jogos_analisados} jogos ({frequencia}%)")


def calcular_padrao_linha_time_soma_jogadores(cur, time_id, coluna, linhas_testadas):
    """NOVO (estatísticas de time): soma a estatística individual de TODOS
    os jogadores do NOSSO time que entraram em campo, jogo a jogo (ex:
    soma de chutes no gol de todo mundo = chutes no gol do time naquele
    jogo) - usa jogador_estatisticas_jogo porque não existe um total
    agregado por lado salvo pra essas métricas (diferente de escanteio/
    falta/chute, que vêm prontos de estatisticas_jogo). O `lado` em
    jogador_estatisticas_jogo também é baseado no mandante/visitante REAL
    do jogo (ver popular_banco.py/salvar_estatisticas_jogadores), então
    precisa da mesma tradução via jogos.mandante que os outros padrões de
    time (baseados em estatisticas_jogo) já usam."""
    cur.execute(
        f"""
        SELECT SUM(jeg.{coluna})
        FROM jogador_estatisticas_jogo jeg
        JOIN jogos j ON j.id = jeg.jogo_id
        WHERE j.nosso_time_id = %s
          AND ((j.mandante = TRUE AND jeg.lado = 'mandante')
           OR (j.mandante = FALSE AND jeg.lado = 'visitante'))
          AND jeg.{coluna} IS NOT NULL
        GROUP BY jeg.jogo_id, j.data_jogo
        ORDER BY j.data_jogo DESC
        LIMIT %s
        """,
        (time_id, JANELA_MAXIMA_DE_JOGOS),
    )
    valores = [row[0] for row in cur.fetchall()]

    jogos_analisados = len(valores)
    if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
        return None, jogos_analisados

    media = round(sum(float(v) for v in valores) / jogos_analisados, 2)

    resultados = []
    for linha in linhas_testadas:
        jogos_acima = sum(1 for v in valores if float(v) > linha)
        frequencia = round(100 * jogos_acima / jogos_analisados, 2)
        resultados.append((linha, jogos_analisados, jogos_acima, frequencia, media))

    return resultados, jogos_analisados


def calcular_padrao_gols_time(cur, time_id, linhas_testadas):
    """NOVO (Mais/Menos gols do time): mesmo padrão de calcular_padroes_escanteio,
    mas a fonte é jogos.placar_corinthians (não estatisticas_jogo) - o
    placar final já vem pronto assim que o jogo termina, sem depender do
    endpoint de estatísticas (e sem o lag que motivou a janela de espera -
    ver seção do bug de estatística coletada no meio do jogo). Salvo na
    MESMA tabela padroes_time_linha que já existe (tipo="gols"), não
    precisa de tabela nova."""
    cur.execute(
        """
        SELECT placar_corinthians
        FROM jogos
        WHERE nosso_time_id = %s
          AND placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL
        ORDER BY data_jogo DESC
        LIMIT %s
        """,
        (time_id, JANELA_MAXIMA_DE_JOGOS),
    )
    valores = [row[0] for row in cur.fetchall()]

    jogos_analisados = len(valores)
    if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
        return None, jogos_analisados

    media = round(sum(float(v) for v in valores) / jogos_analisados, 2)

    resultados = []
    for linha in linhas_testadas:
        jogos_acima = sum(1 for v in valores if float(v) > linha)
        frequencia = round(100 * jogos_acima / jogos_analisados, 2)
        resultados.append((linha, jogos_analisados, jogos_acima, frequencia, media))

    return resultados, jogos_analisados


def calcular_padroes_gols_total(cur, time_id):
    """NOVO (Mais/Menos gols TOTAL do jogo, mandante + visitante somados):
    mesmo padrão de calcular_padroes_escanteio_total/calcular_padroes_cartao_total,
    mas somando placar_corinthians + placar_adversario (o "total" aqui já é
    o próprio placar do jogo, não precisa somar dois lados de
    estatisticas_jogo)."""
    cur.execute(
        """
        SELECT (placar_corinthians + placar_adversario)
        FROM jogos
        WHERE nosso_time_id = %s
          AND placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL
        ORDER BY data_jogo DESC
        LIMIT %s
        """,
        (time_id, JANELA_MAXIMA_DE_JOGOS),
    )
    valores = [row[0] for row in cur.fetchall()]

    jogos_analisados = len(valores)
    if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
        return None, jogos_analisados

    media = round(sum(float(v) for v in valores) / jogos_analisados, 2)

    resultados = []
    for linha in LINHAS_GOLS_TOTAL:
        jogos_acima = sum(1 for v in valores if float(v) > linha)
        frequencia = round(100 * jogos_acima / jogos_analisados, 2)
        resultados.append((linha, jogos_analisados, jogos_acima, frequencia, media))

    return resultados, jogos_analisados


def salvar_padroes_gols_total(cur, resultados, time_id):
    """NOVO: salva em padroes_gols_total (tabela nova, mesmo formato de
    padroes_escanteio_total/padroes_cartao_total)."""
    for linha, jogos_analisados, jogos_acima, frequencia, media in resultados:
        cur.execute(
            """
            INSERT INTO padroes_gols_total (time_id, linha, jogos_analisados, jogos_acima_da_linha, frequencia, media, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (time_id, linha) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                jogos_acima_da_linha = EXCLUDED.jogos_acima_da_linha,
                frequencia = EXCLUDED.frequencia,
                media = EXCLUDED.media,
                atualizado_em = NOW()
            """,
            (time_id, linha, jogos_analisados, jogos_acima, frequencia, media),
        )
        print(f"  [gols total] Mais de {linha}: {jogos_acima}/{jogos_analisados} jogos ({frequencia}%)")


def calcular_padrao_equipe_marca(cur, time_id):
    """NOVO (Equipe Marca - Sim/Não): frequência binária de jogos em que o
    NOSSO time marcou pelo menos 1 gol (placar_corinthians > 0). Mesma
    fonte de dado de calcular_padrao_gols_time, mas mercado binário (Sim/
    Não), não de linha - por isso não usa linhas_testadas nem
    padroes_time_linha, e sim uma tabela dedicada com uma frequência só
    por time."""
    cur.execute(
        """
        SELECT placar_corinthians
        FROM jogos
        WHERE nosso_time_id = %s
          AND placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL
        ORDER BY data_jogo DESC
        LIMIT %s
        """,
        (time_id, JANELA_MAXIMA_DE_JOGOS),
    )
    valores = [row[0] for row in cur.fetchall()]

    jogos_analisados = len(valores)
    if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
        return None, jogos_analisados

    jogos_que_marcou = sum(1 for v in valores if v > 0)
    frequencia = round(100 * jogos_que_marcou / jogos_analisados, 2)

    return (jogos_analisados, jogos_que_marcou, frequencia), jogos_analisados


def salvar_padrao_equipe_marca(cur, resultado, time_id):
    """NOVO: salva em padroes_time_marca (tabela nova, um registro por time -
    não tem linha/handicap, é só a frequência binária)."""
    jogos_analisados, jogos_que_marcou, frequencia = resultado
    cur.execute(
        """
        INSERT INTO padroes_time_marca (time_id, jogos_analisados, jogos_que_marcou, frequencia, atualizado_em)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (time_id) DO UPDATE SET
            jogos_analisados = EXCLUDED.jogos_analisados,
            jogos_que_marcou = EXCLUDED.jogos_que_marcou,
            frequencia = EXCLUDED.frequencia,
            atualizado_em = NOW()
        """,
        (time_id, jogos_analisados, jogos_que_marcou, frequencia),
    )
    print(f"  [equipe marca] {jogos_que_marcou}/{jogos_analisados} jogos ({frequencia}%)")



# cobertos pra identificar "jeito de jogar" de cada time rastreado - só
# impedimento e cartão por enquanto (escopo reduzido de propósito, pra
# validar a ideia com pouco risco antes de expandir pra outros mercados).
# Cada tipo é medido em dois PAPÉIS:
#   - "ofensivo": o quanto o PRÓPRIO time gera esse evento (ex: time que
#     joga bola longa tende a ter mais impedimento; time indisciplinado
#     tende a levar mais cartão)
#   - "defensivo": o quanto esse time INFLUENCIA O ADVERSÁRIO a gerar mais
#     ou menos esse evento (ex: time que joga com a linha de defesa muito
#     recuada tende a fazer o ATAQUE ADVERSÁRIO cair menos em impedimento;
#     time que marca duro/comete muita falta tende a fazer o ADVERSÁRIO
#     reagir com mais cartão também)
# Isso é só EXIBIÇÃO por enquanto (ver /time/<id> em app.py) - não entra
# em nenhuma fórmula de probabilidade/VE ainda. A ideia é os dois donos do
# projeto conferirem se os números batem com o que eles veem assistindo
# aos jogos, antes de decidir se vale a pena virar um ajuste fino de
# verdade (com piso/teto, igual árbitro/forma/suspensão já são).
TIPOS_ESTILO = ["impedimento", "cartao"]
PAPEIS_ESTILO = ["ofensivo", "defensivo"]


def calcular_media_evento_por_jogo(cur, time_id, tipo, papel):
    """NOVO (estilo de time): média BRUTA (não frequência de linha) de
    quantas vezes esse evento aconteceu por jogo, olhando o lado certo
    conforme o papel pedido:
      - cartao/ofensivo:  cartões que os jogadores do PRÓPRIO time levaram
      - cartao/defensivo: cartões que os jogadores do ADVERSÁRIO levaram,
                           nos jogos DESSE time
      - impedimento/ofensivo:  impedimentos do PRÓPRIO time
      - impedimento/defensivo: impedimentos do ADVERSÁRIO, nos jogos
                                DESSE time
    Cartão usa a tabela `cartoes` direto (o campo `lado` ali já vem gravado
    como 'mandante' = nosso time / 'visitante' = adversário, sem precisar
    de tradução via jogos.mandante - mesmo padrão já usado em
    calcular_padrao_cartao_time). Impedimento usa
    jogador_estatisticas_jogo, que guarda o lado MANDANTE/VISITANTE REAL
    do jogo - por isso essa parte SIM precisa traduzir via jogos.mandante
    (mesmo padrão de calcular_padrao_linha_time_soma_jogadores), só que
    invertido quando o papel é "defensivo" (queremos o lado do ADVERSÁRIO,
    não o nosso)."""
    if tipo == "cartao":
        lado_sql = "'mandante'" if papel == "ofensivo" else "'visitante'"
        cur.execute(
            f"""
            SELECT contagem.total
            FROM (
                SELECT j.id AS jogo_id, j.data_jogo,
                       (SELECT COUNT(*) FROM cartoes c WHERE c.jogo_id = j.id AND c.lado = {lado_sql}) AS total,
                       COUNT(DISTINCT eg.lado) AS lados
                FROM jogos j
                JOIN estatisticas_jogo eg ON eg.jogo_id = j.id
                WHERE j.nosso_time_id = %s AND j.data_jogo < CURRENT_DATE
                GROUP BY j.id, j.data_jogo
            ) contagem
            WHERE contagem.lados = 2
            ORDER BY contagem.data_jogo DESC
            LIMIT %s
            """,
            (time_id, JANELA_MAXIMA_DE_JOGOS),
        )
    elif tipo == "impedimento":
        if papel == "ofensivo":
            condicao_lado = ("((j.mandante = TRUE AND jeg.lado = 'mandante') "
                              "OR (j.mandante = FALSE AND jeg.lado = 'visitante'))")
        else:
            condicao_lado = ("((j.mandante = TRUE AND jeg.lado = 'visitante') "
                              "OR (j.mandante = FALSE AND jeg.lado = 'mandante'))")
        cur.execute(
            f"""
            SELECT SUM(jeg.impedimentos)
            FROM jogador_estatisticas_jogo jeg
            JOIN jogos j ON j.id = jeg.jogo_id
            WHERE j.nosso_time_id = %s
              AND {condicao_lado}
              AND jeg.impedimentos IS NOT NULL
              AND j.data_jogo < CURRENT_DATE
            GROUP BY jeg.jogo_id, j.data_jogo
            ORDER BY j.data_jogo DESC
            LIMIT %s
            """,
            (time_id, JANELA_MAXIMA_DE_JOGOS),
        )
    else:
        raise ValueError(f"tipo desconhecido pra estilo de time: {tipo}")

    valores = [row[0] for row in cur.fetchall()]
    jogos_analisados = len(valores)
    if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
        return None, jogos_analisados

    media = round(sum(float(v) for v in valores) / jogos_analisados, 3)
    return media, jogos_analisados


def calcular_estilo_times(cur, times_rastreados):
    """NOVO (estilo de time - Fase 1): pra cada time rastreado, calcula o
    fator (media_do_time / media_da_liga) em cada tipo/papel. A "média da
    liga" é sempre a média do papel OFENSIVO entre todos os times
    rastreados com dado suficiente - ofensivo e defensivo estão na MESMA
    unidade (eventos de UM time em UMA partida), então dá pra comparar os
    dois contra essa mesma régua (ex: um fator defensivo de 0.75 significa
    "os adversários desse time geram esse evento 25% MENOS do que um time
    médio gera quando ataca", o que é exatamente a leitura que queremos:
    "esse time suprime esse evento no rival"). Só entre os times
    rastreados (não dá pra comparar com o campeonato inteiro, já que só
    eles têm a temporada coletada de verdade)."""
    medias = {}
    for time_id, time_nome, _ in times_rastreados:
        for tipo in TIPOS_ESTILO:
            for papel in PAPEIS_ESTILO:
                media, jogos = calcular_media_evento_por_jogo(cur, time_id, tipo, papel)
                medias[(time_id, tipo, papel)] = (media, jogos)

    baselines = {}
    for tipo in TIPOS_ESTILO:
        valores_ofensivos = [
            m for (_tid, t, p), (m, _j) in medias.items()
            if t == tipo and p == "ofensivo" and m is not None
        ]
        baselines[tipo] = round(sum(valores_ofensivos) / len(valores_ofensivos), 3) if valores_ofensivos else None

    resultados = []
    for (time_id, tipo, papel), (media, jogos) in medias.items():
        baseline = baselines.get(tipo)
        if media is None or baseline is None or baseline == 0:
            continue
        fator = round(media / baseline, 3)
        resultados.append((time_id, tipo, papel, media, baseline, jogos, fator))

    return resultados


def salvar_estilo_times(cur, resultados):
    """NOVO (estilo de time - Fase 1): salva em `padroes_estilo_time`
    (precisa rodar migrar_estilo_time.py antes, uma vez, pra criar essa
    tabela)."""
    for time_id, tipo, papel, media, baseline, jogos, fator in resultados:
        cur.execute(
            """
            INSERT INTO padroes_estilo_time
                (time_id, tipo, papel, media_time, media_liga, jogos_analisados, fator, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (time_id, tipo, papel) DO UPDATE SET
                media_time = EXCLUDED.media_time,
                media_liga = EXCLUDED.media_liga,
                jogos_analisados = EXCLUDED.jogos_analisados,
                fator = EXCLUDED.fator,
                atualizado_em = NOW()
            """,
            (time_id, tipo, papel, media, baseline, jogos, fator),
        )
        desvio = round((fator - 1) * 100, 1)
        sinal = "+" if desvio >= 0 else ""
        print(f"  [{tipo}/{papel}] time_id={time_id}: média {media}/jogo vs. liga {baseline}/jogo "
              f"({sinal}{desvio}%, {jogos} jogo(s))")


# NOVO (Correlação entre estatísticas, só exibição): dentro do MESMO jogo,
# algumas estatísticas tendem a se mover juntas (ex: jogo com muito chute
# tende a ter mais escanteio também - o time que ataca mais gera as duas
# coisas ao mesmo tempo). Diferente de Estilo/Rodada/Zona (que são POR
# TIME), essa é uma estatística da LIGA inteira, olhando o TOTAL de cada
# jogo (mandante + visitante somados) - só pares que fazem sentido
# futebolisticamente (decisão consciente de não testar todo par possível,
# ex: impedimento x chute não tem relação lógica, fica de fora).
PARES_CORRELACAO_ESTATISTICAS = [
    ("chutes", "escanteios"),
    ("faltas", "cartoes"),
    ("desarmes", "faltas"),
]


def buscar_totais_por_jogo_liga(cur):
    """{fixture_id_api: {"chutes":..., "escanteios":..., "faltas":...,
    "cartoes":..., "desarmes":...}} - total do jogo (mandante+visitante),
    só pra jogos com as duas estatísticas completas. Deduplicado por
    fixture_id_api - o mesmo jogo real gera 2 linhas em `jogos` quando os
    dois times envolvidos são rastreados (visões diferentes), mas aqui
    cada jogo real só pode contar UMA vez, senão o mesmo jogo pesaria
    dobrado na correlação."""
    cur.execute(
        """
        SELECT contagem.fixture_id_api, contagem.jogo_id, contagem.chutes,
               contagem.escanteios, contagem.faltas
        FROM (
            SELECT j.fixture_id_api, j.id AS jogo_id,
                   SUM(eg.finalizacoes) AS chutes, SUM(eg.escanteios) AS escanteios,
                   SUM(eg.faltas) AS faltas, COUNT(DISTINCT eg.lado) AS lados
            FROM jogos j
            JOIN estatisticas_jogo eg ON eg.jogo_id = j.id
            WHERE j.data_jogo < CURRENT_DATE AND j.fixture_id_api IS NOT NULL
            GROUP BY j.fixture_id_api, j.id
        ) contagem
        WHERE contagem.lados = 2
        """
    )
    linhas = cur.fetchall()

    cur.execute("SELECT jogo_id, COUNT(*) FROM cartoes GROUP BY jogo_id")
    cartoes_por_jogo = dict(cur.fetchall())

    cur.execute(
        "SELECT jogo_id, SUM(desarmes) FROM jogador_estatisticas_jogo "
        "WHERE desarmes IS NOT NULL GROUP BY jogo_id"
    )
    desarmes_por_jogo = dict(cur.fetchall())

    totais = {}
    for fixture_id_api, jogo_id, chutes, escanteios, faltas in linhas:
        if fixture_id_api in totais:
            continue  # mesmo jogo real já visto pela outra perspectiva
        if chutes is None or escanteios is None or faltas is None:
            continue
        cartoes = cartoes_por_jogo.get(jogo_id)
        desarmes = desarmes_por_jogo.get(jogo_id)
        if cartoes is None or desarmes is None:
            continue
        totais[fixture_id_api] = {
            "chutes": float(chutes), "escanteios": float(escanteios), "faltas": float(faltas),
            "cartoes": float(cartoes), "desarmes": float(desarmes),
        }
    return totais


def calcular_correlacoes_estatisticas(cur):
    """Pra cada par de PARES_CORRELACAO_ESTATISTICAS, separa os jogos em
    "A acima da média" vs "A abaixo da média" e compara a média de B em
    cada grupo - a diferença entre os dois grupos é o efeito. Estatística
    simples (média e comparação de grupo), no mesmo espírito do resto do
    projeto - não é correlação estatística formal (ex: coeficiente de
    Pearson), de propósito, pra ficar fácil de conferir/explicar."""
    totais = buscar_totais_por_jogo_liga(cur)
    jogos = list(totais.values())
    if len(jogos) < JOGOS_MINIMOS_PARA_ANALISAR:
        return []

    resultados = []
    for chave_a, chave_b in PARES_CORRELACAO_ESTATISTICAS:
        valores_a = [j[chave_a] for j in jogos]
        media_a = sum(valores_a) / len(valores_a)

        grupo_acima = [j[chave_b] for j in jogos if j[chave_a] > media_a]
        grupo_abaixo = [j[chave_b] for j in jogos if j[chave_a] <= media_a]

        if len(grupo_acima) < 3 or len(grupo_abaixo) < 3:
            continue

        media_b_acima = sum(grupo_acima) / len(grupo_acima)
        media_b_abaixo = sum(grupo_abaixo) / len(grupo_abaixo)

        resultados.append((
            chave_a, chave_b, round(media_a, 3),
            round(media_b_acima, 3), round(media_b_abaixo, 3),
            len(grupo_acima), len(grupo_abaixo), len(jogos),
        ))
    return resultados


def salvar_correlacoes_estatisticas(cur, resultados):
    for (chave_a, chave_b, media_a, media_b_acima, media_b_abaixo,
         jogos_acima, jogos_abaixo, jogos_total) in resultados:
        par = f"{chave_a}_{chave_b}"
        cur.execute(
            """
            INSERT INTO padroes_correlacao_estatisticas
                (par, estatistica_a, estatistica_b, media_a, valor_b_acima, valor_b_abaixo,
                 jogos_acima, jogos_abaixo, jogos_total, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (par) DO UPDATE SET
                media_a = EXCLUDED.media_a, valor_b_acima = EXCLUDED.valor_b_acima,
                valor_b_abaixo = EXCLUDED.valor_b_abaixo, jogos_acima = EXCLUDED.jogos_acima,
                jogos_abaixo = EXCLUDED.jogos_abaixo, jogos_total = EXCLUDED.jogos_total,
                atualizado_em = NOW()
            """,
            (par, chave_a, chave_b, media_a, media_b_acima, media_b_abaixo,
             jogos_acima, jogos_abaixo, jogos_total),
        )
        diferenca = round(media_b_acima - media_b_abaixo, 3)
        print(f"  [{chave_a} -> {chave_b}] acima da média ({media_a}): {media_b_acima}/jogo | "
              f"abaixo da média: {media_b_abaixo}/jogo (diferença: {diferenca:+.3f}, "
              f"{jogos_acima}+{jogos_abaixo} jogo(s))")


# NOVO (Correlação entre estatísticas - POR TIME): complementa a versão
# geral acima - a mesma pergunta ("A acima da média puxa B pra cima?"),
# mas calculada só com os jogos de UM time, pra ver se aquele time tem
# essa relação mais forte, mais fraca, ou até invertida em relação ao
# padrão geral da liga. Reaproveita os mesmos PARES_CORRELACAO_ESTATISTICAS
# de cima - a única diferença é a fonte dos jogos (só desse time, não a
# liga inteira).
def buscar_totais_por_jogo_time(cur, time_id):
    """Mesma ideia de buscar_totais_por_jogo_liga, mas só com os jogos
    DESSE time - sem precisar deduplicar por fixture_id_api, já que
    filtrar por nosso_time_id já garante um jogo real por linha (mesmo
    quando o adversário também é rastreado - cada time vê seu PRÓPRIO
    jogo, sem duplicar)."""
    cur.execute(
        """
        SELECT contagem.jogo_id, contagem.chutes, contagem.escanteios, contagem.faltas
        FROM (
            SELECT j.id AS jogo_id,
                   SUM(eg.finalizacoes) AS chutes, SUM(eg.escanteios) AS escanteios,
                   SUM(eg.faltas) AS faltas, COUNT(DISTINCT eg.lado) AS lados
            FROM jogos j
            JOIN estatisticas_jogo eg ON eg.jogo_id = j.id
            WHERE j.nosso_time_id = %s AND j.data_jogo < CURRENT_DATE
            GROUP BY j.id
        ) contagem
        WHERE contagem.lados = 2
        """,
        (time_id,),
    )
    linhas = cur.fetchall()

    cur.execute(
        "SELECT c.jogo_id, COUNT(*) FROM cartoes c "
        "JOIN jogos j ON j.id = c.jogo_id WHERE j.nosso_time_id = %s GROUP BY c.jogo_id",
        (time_id,),
    )
    cartoes_por_jogo = dict(cur.fetchall())

    cur.execute(
        "SELECT jeg.jogo_id, SUM(jeg.desarmes) FROM jogador_estatisticas_jogo jeg "
        "JOIN jogos j ON j.id = jeg.jogo_id WHERE j.nosso_time_id = %s AND jeg.desarmes IS NOT NULL "
        "GROUP BY jeg.jogo_id",
        (time_id,),
    )
    desarmes_por_jogo = dict(cur.fetchall())

    totais = []
    for jogo_id, chutes, escanteios, faltas in linhas:
        if chutes is None or escanteios is None or faltas is None:
            continue
        cartoes = cartoes_por_jogo.get(jogo_id)
        desarmes = desarmes_por_jogo.get(jogo_id)
        if cartoes is None or desarmes is None:
            continue
        totais.append({
            "chutes": float(chutes), "escanteios": float(escanteios), "faltas": float(faltas),
            "cartoes": float(cartoes), "desarmes": float(desarmes),
        })
    return totais


def calcular_correlacoes_time(cur, time_id):
    jogos = buscar_totais_por_jogo_time(cur, time_id)
    if len(jogos) < JOGOS_MINIMOS_PARA_ANALISAR:
        return []

    resultados = []
    for chave_a, chave_b in PARES_CORRELACAO_ESTATISTICAS:
        valores_a = [j[chave_a] for j in jogos]
        media_a = sum(valores_a) / len(valores_a)

        grupo_acima = [j[chave_b] for j in jogos if j[chave_a] > media_a]
        grupo_abaixo = [j[chave_b] for j in jogos if j[chave_a] <= media_a]

        # NOVO: mínimo de 5 jogos por grupo - com amostra POR TIME (bem
        # menor que a da liga inteira), um grupo com poucos jogos vira
        # ruído fácil demais pra confiar.
        if len(grupo_acima) < JOGOS_MINIMOS_PARA_ANALISAR or len(grupo_abaixo) < JOGOS_MINIMOS_PARA_ANALISAR:
            continue

        media_b_acima = sum(grupo_acima) / len(grupo_acima)
        media_b_abaixo = sum(grupo_abaixo) / len(grupo_abaixo)

        resultados.append((
            time_id, chave_a, chave_b, round(media_a, 3),
            round(media_b_acima, 3), round(media_b_abaixo, 3),
            len(grupo_acima), len(grupo_abaixo), len(jogos),
        ))
    return resultados


def salvar_correlacoes_time(cur, resultados):
    for (time_id, chave_a, chave_b, media_a, media_b_acima, media_b_abaixo,
         jogos_acima, jogos_abaixo, jogos_total) in resultados:
        par = f"{chave_a}_{chave_b}"
        cur.execute(
            """
            INSERT INTO padroes_correlacao_time
                (time_id, par, estatistica_a, estatistica_b, media_a, valor_b_acima, valor_b_abaixo,
                 jogos_acima, jogos_abaixo, jogos_total, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (time_id, par) DO UPDATE SET
                media_a = EXCLUDED.media_a, valor_b_acima = EXCLUDED.valor_b_acima,
                valor_b_abaixo = EXCLUDED.valor_b_abaixo, jogos_acima = EXCLUDED.jogos_acima,
                jogos_abaixo = EXCLUDED.jogos_abaixo, jogos_total = EXCLUDED.jogos_total,
                atualizado_em = NOW()
            """,
            (time_id, par, chave_a, chave_b, media_a, media_b_acima, media_b_abaixo,
             jogos_acima, jogos_abaixo, jogos_total),
        )
    if resultados:
        print(f"  {len(resultados)} par(es) de correlação calculados pra esse time.")


# NOVO (Correlação entre CATEGORIAS de jogador, só exibição, GERAL da
# liga): cruza a estatística de um GRUPO de jogadores (por posição -
# Goleiro/Defensor/Meio-campista/Atacante, a granularidade máxima que a
# API-Football oferece) com a de OUTRO grupo, dos dois times somados, no
# mesmo jogo. Ex: "quando os ATACANTES sofrem muita falta, os DEFENSORES
# do jogo levam mais cartão?". Calculado uma vez só (é um dado da liga,
# não de um time específico), mesma janela de "acima/abaixo da média" do
# resto das correlações.
EXPRESSOES_ESTATISTICA_JOGADOR = {
    "faltas_sofridas": "jeg.faltas_sofridas",
    "faltas_cometidas": "jeg.faltas_cometidas",
    "cartoes": "(COALESCE(jeg.cartao_amarelo, 0) + COALESCE(jeg.cartao_vermelho, 0))",
    "desarmes": "jeg.desarmes",
    "chutes": "jeg.chutes",
    "chutes_no_gol": "jeg.chutes_no_gol",
}

PARES_CORRELACAO_CATEGORIA = [
    {
        "par": "falta_sofrida_atacante_cartao_defensor",
        "categoria_a": "F", "estatistica_a": "faltas_sofridas", "titulo_a": "Faltas sofridas (atacantes)",
        "categoria_b": "D", "estatistica_b": "cartoes", "titulo_b": "Cartões (defensores)",
    },
]


def buscar_totais_categoria_por_jogo(cur, categoria_a, estatistica_a, categoria_b, estatistica_b):
    """{fixture_id_api: (valor_a, valor_b)} - soma da estatística A entre
    os jogadores da categoria A (os dois times juntos) e da estatística B
    entre os jogadores da categoria B, por jogo. Deduplicado por
    fixture_id_api (mesmo jogo real conta uma vez só, mesmo se os dois
    times envolvidos forem rastreados)."""
    expr_a = EXPRESSOES_ESTATISTICA_JOGADOR[estatistica_a]
    expr_b = EXPRESSOES_ESTATISTICA_JOGADOR[estatistica_b]
    cur.execute(
        f"""
        SELECT j.fixture_id_api, j.id,
               SUM(CASE WHEN jeg.posicao = %s THEN {expr_a} END) AS valor_a,
               SUM(CASE WHEN jeg.posicao = %s THEN {expr_b} END) AS valor_b
        FROM jogos j
        JOIN jogador_estatisticas_jogo jeg ON jeg.jogo_id = j.id
        WHERE j.data_jogo < CURRENT_DATE AND j.fixture_id_api IS NOT NULL
        GROUP BY j.fixture_id_api, j.id
        """,
        (categoria_a, categoria_b),
    )
    totais = {}
    for fixture_id_api, jogo_id, valor_a, valor_b in cur.fetchall():
        if fixture_id_api in totais:
            continue  # mesmo jogo real já visto pela outra perspectiva
        if valor_a is None or valor_b is None:
            continue
        totais[fixture_id_api] = (float(valor_a), float(valor_b))
    return totais


def calcular_correlacoes_categoria(cur):
    resultados = []
    for par_def in PARES_CORRELACAO_CATEGORIA:
        totais = buscar_totais_categoria_por_jogo(
            cur, par_def["categoria_a"], par_def["estatistica_a"],
            par_def["categoria_b"], par_def["estatistica_b"],
        )
        jogos = list(totais.values())
        if len(jogos) < JOGOS_MINIMOS_PARA_ANALISAR:
            continue

        valores_a = [v[0] for v in jogos]
        media_a = sum(valores_a) / len(valores_a)

        grupo_acima = [v[1] for v in jogos if v[0] > media_a]
        grupo_abaixo = [v[1] for v in jogos if v[0] <= media_a]

        if len(grupo_acima) < 5 or len(grupo_abaixo) < 5:
            continue

        media_b_acima = sum(grupo_acima) / len(grupo_acima)
        media_b_abaixo = sum(grupo_abaixo) / len(grupo_abaixo)

        resultados.append((
            par_def["par"], par_def["categoria_a"], par_def["estatistica_a"],
            par_def["categoria_b"], par_def["estatistica_b"], round(media_a, 3),
            round(media_b_acima, 3), round(media_b_abaixo, 3),
            len(grupo_acima), len(grupo_abaixo), len(jogos),
        ))
    return resultados


def salvar_correlacoes_categoria(cur, resultados):
    for (par, cat_a, est_a, cat_b, est_b, media_a, media_b_acima, media_b_abaixo,
         jogos_acima, jogos_abaixo, jogos_total) in resultados:
        cur.execute(
            """
            INSERT INTO padroes_correlacao_categoria
                (par, categoria_a, estatistica_a, categoria_b, estatistica_b, media_a,
                 valor_b_acima, valor_b_abaixo, jogos_acima, jogos_abaixo, jogos_total, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (par) DO UPDATE SET
                media_a = EXCLUDED.media_a, valor_b_acima = EXCLUDED.valor_b_acima,
                valor_b_abaixo = EXCLUDED.valor_b_abaixo, jogos_acima = EXCLUDED.jogos_acima,
                jogos_abaixo = EXCLUDED.jogos_abaixo, jogos_total = EXCLUDED.jogos_total,
                atualizado_em = NOW()
            """,
            (par, cat_a, est_a, cat_b, est_b, media_a, media_b_acima, media_b_abaixo,
             jogos_acima, jogos_abaixo, jogos_total),
        )
        diferenca = round(media_b_acima - media_b_abaixo, 3)
        print(f"  [{par}] acima da média ({media_a}): {media_b_acima}/jogo | "
              f"abaixo: {media_b_abaixo}/jogo (diferença: {diferenca:+.3f}, "
              f"{jogos_acima}+{jogos_abaixo} jogo(s))")


# NOVO (Correlação entre categorias - POR TIME, só visual): diferente da
# versão geral (que soma os DOIS lados juntos), essa isola a DIREÇÃO - só
# os jogadores da categoria A DO PRÓPRIO time contra os da categoria B DO
# ADVERSÁRIO, no mesmo jogo. Revela se o ataque de UM time específico tem
# esse efeito mais forte/fraco que a média da liga.
def buscar_totais_categoria_direcional_por_jogo(cur, time_id, categoria_a, estatistica_a, categoria_b, estatistica_b):
    expr_a = EXPRESSOES_ESTATISTICA_JOGADOR[estatistica_a]
    expr_b = EXPRESSOES_ESTATISTICA_JOGADOR[estatistica_b]
    cur.execute(
        f"""
        SELECT j.id,
               SUM(CASE WHEN jeg.posicao = %s AND
                   ((j.mandante = TRUE AND jeg.lado = 'mandante') OR (j.mandante = FALSE AND jeg.lado = 'visitante'))
                   THEN {expr_a} END) AS valor_a,
               SUM(CASE WHEN jeg.posicao = %s AND
                   ((j.mandante = TRUE AND jeg.lado = 'visitante') OR (j.mandante = FALSE AND jeg.lado = 'mandante'))
                   THEN {expr_b} END) AS valor_b
        FROM jogos j
        JOIN jogador_estatisticas_jogo jeg ON jeg.jogo_id = j.id
        WHERE j.nosso_time_id = %s AND j.data_jogo < CURRENT_DATE
        GROUP BY j.id
        """,
        (categoria_a, categoria_b, time_id),
    )
    totais = []
    for jogo_id, valor_a, valor_b in cur.fetchall():
        if valor_a is None or valor_b is None:
            continue
        totais.append((float(valor_a), float(valor_b)))
    return totais


def calcular_correlacoes_categoria_time(cur, time_id):
    resultados = []
    for par_def in PARES_CORRELACAO_CATEGORIA:
        jogos = buscar_totais_categoria_direcional_por_jogo(
            cur, time_id, par_def["categoria_a"], par_def["estatistica_a"],
            par_def["categoria_b"], par_def["estatistica_b"],
        )
        if len(jogos) < JOGOS_MINIMOS_PARA_ANALISAR:
            continue

        valores_a = [v[0] for v in jogos]
        media_a = sum(valores_a) / len(valores_a)

        grupo_acima = [v[1] for v in jogos if v[0] > media_a]
        grupo_abaixo = [v[1] for v in jogos if v[0] <= media_a]

        if len(grupo_acima) < JOGOS_MINIMOS_PARA_ANALISAR or len(grupo_abaixo) < JOGOS_MINIMOS_PARA_ANALISAR:
            continue

        media_b_acima = sum(grupo_acima) / len(grupo_acima)
        media_b_abaixo = sum(grupo_abaixo) / len(grupo_abaixo)

        resultados.append((
            time_id, par_def["par"], par_def["categoria_a"], par_def["estatistica_a"],
            par_def["categoria_b"], par_def["estatistica_b"], round(media_a, 3),
            round(media_b_acima, 3), round(media_b_abaixo, 3),
            len(grupo_acima), len(grupo_abaixo), len(jogos),
        ))
    return resultados


def salvar_correlacoes_categoria_time(cur, resultados):
    for (time_id, par, cat_a, est_a, cat_b, est_b, media_a, media_b_acima, media_b_abaixo,
         jogos_acima, jogos_abaixo, jogos_total) in resultados:
        cur.execute(
            """
            INSERT INTO padroes_correlacao_categoria_time
                (time_id, par, categoria_a, estatistica_a, categoria_b, estatistica_b, media_a,
                 valor_b_acima, valor_b_abaixo, jogos_acima, jogos_abaixo, jogos_total, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (time_id, par) DO UPDATE SET
                media_a = EXCLUDED.media_a, valor_b_acima = EXCLUDED.valor_b_acima,
                valor_b_abaixo = EXCLUDED.valor_b_abaixo, jogos_acima = EXCLUDED.jogos_acima,
                jogos_abaixo = EXCLUDED.jogos_abaixo, jogos_total = EXCLUDED.jogos_total,
                atualizado_em = NOW()
            """,
            (time_id, par, cat_a, est_a, cat_b, est_b, media_a, media_b_acima, media_b_abaixo,
             jogos_acima, jogos_abaixo, jogos_total),
        )
    if resultados:
        print(f"  {len(resultados)} par(es) de correlação categoria->categoria calculados pra esse time.")


# NOVO (Correlação POR JOGADOR NOMEADO, só visual): a API-Football não
# informa quem marca quem em campo, então não dá pra afirmar "jogador X
# tomou cartão POR CAUSA do jogador Y". O que dá pra provar com o dado
# real: pra um atacante específico, nos jogos em que ELE sofreu mais falta
# que a PRÓPRIA média pessoal, o time ADVERSÁRIO daquele jogo (como um
# todo, não um defensor nomeado) reagiu de um jeito ou de outro - medido
# em cartão dos defensores do adversário.
JOGOS_MINIMOS_JOGADOR_CORRELACAO = 10  # jogador precisa de amostra própria razoável


def expr_estatistica_jogador(estatistica, alias):
    """Monta a expressão SQL certa pra essa estatística de jogador, com o
    alias de tabela certo - "cartoes" é especial (soma amarelo+vermelho,
    não é uma coluna única)."""
    if estatistica == "cartoes":
        return f"(COALESCE({alias}.cartao_amarelo, 0) + COALESCE({alias}.cartao_vermelho, 0))"
    return f"{alias}.{estatistica}"


def buscar_jogadores_atacantes_qualificados(cur):
    """Jogadores classificados como Atacante (posição predominante),
    ativos, com jogos suficientes registrados (faltas_sofridas não nula)
    pra calcular uma média pessoal minimamente confiável."""
    cur.execute(
        """
        SELECT j.id, j.nome
        FROM jogadores j
        WHERE j.ativo = TRUE
          AND (
              SELECT jeg.posicao FROM jogador_estatisticas_jogo jeg
              WHERE jeg.jogador_id = j.id AND jeg.posicao IS NOT NULL
              GROUP BY jeg.posicao ORDER BY COUNT(*) DESC LIMIT 1
          ) = 'F'
          AND (
              SELECT COUNT(*) FROM jogador_estatisticas_jogo jeg
              WHERE jeg.jogador_id = j.id AND jeg.faltas_sofridas IS NOT NULL
          ) >= %s
        """,
        (JOGOS_MINIMOS_JOGADOR_CORRELACAO,),
    )
    return cur.fetchall()


def buscar_jogos_jogador_vs_adversario(cur, jogador_id, categoria_b, estatistica_b):
    """Pra um jogador específico: pra cada jogo real (deduplicado por
    fixture_id_api), devolve (faltas sofridas pessoais dele nesse jogo,
    soma da estatística B entre os jogadores da categoria B do ADVERSÁRIO
    nesse mesmo jogo)."""
    expr_b = expr_estatistica_jogador(estatistica_b, "jeg_adv")
    cur.execute(
        f"""
        SELECT j.fixture_id_api, jeg_proprio.faltas_sofridas,
               (SELECT SUM(CASE WHEN jeg_adv.posicao = %s THEN {expr_b} END)
                FROM jogador_estatisticas_jogo jeg_adv
                WHERE jeg_adv.jogo_id = j.id AND jeg_adv.lado != jeg_proprio.lado) AS valor_b
        FROM jogador_estatisticas_jogo jeg_proprio
        JOIN jogos j ON j.id = jeg_proprio.jogo_id
        WHERE jeg_proprio.jogador_id = %s
          AND jeg_proprio.faltas_sofridas IS NOT NULL
          AND j.data_jogo < CURRENT_DATE
        """,
        (categoria_b, jogador_id),
    )
    totais = {}
    for fixture_id_api, faltas_pessoais, valor_b in cur.fetchall():
        if fixture_id_api in totais:
            continue  # mesmo jogo real já visto (outra perspectiva)
        if faltas_pessoais is None or valor_b is None:
            continue
        totais[fixture_id_api] = (float(faltas_pessoais), float(valor_b))
    return list(totais.values())


def calcular_correlacoes_jogador(cur):
    jogadores = buscar_jogadores_atacantes_qualificados(cur)
    resultados = []
    for jogador_id, nome in jogadores:
        jogos = buscar_jogos_jogador_vs_adversario(cur, jogador_id, "D", "cartoes")
        if len(jogos) < JOGOS_MINIMOS_JOGADOR_CORRELACAO:
            continue

        valores_a = [v[0] for v in jogos]
        media_pessoal = sum(valores_a) / len(valores_a)

        grupo_acima = [v[1] for v in jogos if v[0] > media_pessoal]
        grupo_abaixo = [v[1] for v in jogos if v[0] <= media_pessoal]

        if len(grupo_acima) < 5 or len(grupo_abaixo) < 5:
            continue

        media_b_acima = sum(grupo_acima) / len(grupo_acima)
        media_b_abaixo = sum(grupo_abaixo) / len(grupo_abaixo)

        resultados.append((
            jogador_id, "faltas_sofridas_cartao_defensor_adversario", "faltas_sofridas",
            "D", "cartoes", round(media_pessoal, 3),
            round(media_b_acima, 3), round(media_b_abaixo, 3),
            len(grupo_acima), len(grupo_abaixo), len(jogos),
        ))
    return resultados


def salvar_correlacoes_jogador(cur, resultados):
    for (jogador_id, par, est_a, cat_b, est_b, media_pessoal, media_b_acima, media_b_abaixo,
         jogos_acima, jogos_abaixo, jogos_total) in resultados:
        cur.execute(
            """
            INSERT INTO padroes_correlacao_jogador
                (jogador_id, par, estatistica_a, categoria_b, estatistica_b, media_pessoal,
                 valor_b_acima, valor_b_abaixo, jogos_acima, jogos_abaixo, jogos_total, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (jogador_id, par) DO UPDATE SET
                media_pessoal = EXCLUDED.media_pessoal, valor_b_acima = EXCLUDED.valor_b_acima,
                valor_b_abaixo = EXCLUDED.valor_b_abaixo, jogos_acima = EXCLUDED.jogos_acima,
                jogos_abaixo = EXCLUDED.jogos_abaixo, jogos_total = EXCLUDED.jogos_total,
                atualizado_em = NOW()
            """,
            (jogador_id, par, est_a, cat_b, est_b, media_pessoal, media_b_acima, media_b_abaixo,
             jogos_acima, jogos_abaixo, jogos_total),
        )
    if resultados:
        print(f"  {len(resultados)} jogador(es) atacante(s) com correlação pessoal calculada.")


# NOVO (Fase B - padrão por rodada, detecção automática de quebra): pra
# cada time rastreado, calcula o comportamento em CADA número de rodada
# (1, 2, 3... até 38), juntando as ~5 temporadas coletadas - ex: "na
# rodada 1, esse time venceu 4 das 5 vezes". Isso é o dado BRUTO, uma
# linha por rodada, sem suavização nenhuma.
#
# Em cima desse bruto, desliza uma "régua" de algumas rodadas (ver
# TAMANHO_JANELA_QUEBRA) comparando um pedaço com o pedaço seguinte, e
# marca onde a diferença entre as duas médias é a MAIOR de todas - esse é
# o "ponto de quebra" (ex: "entre a rodada 3 e a 4, a taxa de vitória cai
# de 85% pra 42%"). É estatística simples (média e subtração), não um
# método acadêmico de detecção de mudança - de propósito, pra ficar fácil
# de conferir/explicar.
#
# Cobre "resultado" (taxa de vitória) e os mesmos mercados que já têm
# padrão geral por time hoje (cartão, escanteio, falta, chute, chute no
# gol, impedimento, desarme) - usando média de eventos por jogo pra esses
# últimos, em vez de escolher uma linha específica (mesma filosofia do
# Estilo de Jogo).
#
# Só EXIBIÇÃO por enquanto (ver /time/<id> em app.py) - não entra em
# nenhuma fórmula de recomendação/VE ainda.
TIPOS_PADRAO_RODADA_ESTATISTICA_JOGO = {
    "escanteio": "escanteios",
    "falta": "faltas",
    "chute": "finalizacoes",
}
TIPOS_PADRAO_RODADA_SOMA_JOGADOR = {
    "chute_no_gol": "chutes_no_gol",
    "impedimento": "impedimentos",
    "desarme": "desarmes",
}
TAMANHO_JANELA_QUEBRA = 3


def calcular_bruto_rodada_resultado(cur, time_id):
    """{rodada_numero: [1.0 se venceu, 0.0 se não, por jogo]} - placar_
    corinthians já representa o placar do NOSSO time (nome legado de
    quando só existia 1 time rastreado), sempre comparável direto com
    placar_adversario, independente de mandante/visitante."""
    cur.execute(
        """
        SELECT rodada_numero, CASE WHEN placar_corinthians > placar_adversario THEN 1.0 ELSE 0.0 END
        FROM jogos
        WHERE nosso_time_id = %s AND rodada_numero IS NOT NULL
          AND placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL
          AND data_jogo < CURRENT_DATE
        """,
        (time_id,),
    )
    por_rodada = {}
    for rodada_numero, venceu in cur.fetchall():
        por_rodada.setdefault(rodada_numero, []).append(float(venceu))
    return por_rodada


def calcular_bruto_rodada_cartao(cur, time_id):
    """{rodada_numero: [cartões do NOSSO time, por jogo]} - mesma
    subconsulta separada (sem JOIN direto com cartoes) usada em todo o
    resto do motor_padroes.py, pra não contar em dobro (ver correção
    histórica do bug de cartão total)."""
    cur.execute(
        """
        SELECT contagem.rodada_numero, contagem.total
        FROM (
            SELECT j.id AS jogo_id, j.rodada_numero,
                   (SELECT COUNT(*) FROM cartoes c WHERE c.jogo_id = j.id AND c.lado = 'mandante') AS total,
                   COUNT(DISTINCT eg.lado) AS lados
            FROM jogos j
            JOIN estatisticas_jogo eg ON eg.jogo_id = j.id
            WHERE j.nosso_time_id = %s AND j.rodada_numero IS NOT NULL AND j.data_jogo < CURRENT_DATE
            GROUP BY j.id, j.rodada_numero
        ) contagem
        WHERE contagem.lados = 2
        """,
        (time_id,),
    )
    por_rodada = {}
    for rodada_numero, valor in cur.fetchall():
        por_rodada.setdefault(rodada_numero, []).append(float(valor))
    return por_rodada


def calcular_bruto_rodada_estatistica_jogo(cur, time_id, coluna):
    """{rodada_numero: [valor do NOSSO time, por jogo]} - a partir de
    estatisticas_jogo (lado MANDANTE/VISITANTE real, precisa traduzir via
    jogos.mandante)."""
    cur.execute(
        f"""
        SELECT j.rodada_numero, eg.{coluna}
        FROM estatisticas_jogo eg
        JOIN jogos j ON j.id = eg.jogo_id
        WHERE j.nosso_time_id = %s AND j.rodada_numero IS NOT NULL AND j.data_jogo < CURRENT_DATE
          AND eg.{coluna} IS NOT NULL
          AND ((j.mandante = TRUE AND eg.lado = 'mandante')
           OR (j.mandante = FALSE AND eg.lado = 'visitante'))
        """,
        (time_id,),
    )
    por_rodada = {}
    for rodada_numero, valor in cur.fetchall():
        por_rodada.setdefault(rodada_numero, []).append(float(valor))
    return por_rodada


def calcular_bruto_rodada_soma_jogador(cur, time_id, coluna):
    """{rodada_numero: [soma do NOSSO time naquele jogo, por jogo]} - a
    partir de jogador_estatisticas_jogo (mesma tradução de lado)."""
    cur.execute(
        f"""
        SELECT j.rodada_numero, SUM(jeg.{coluna})
        FROM jogador_estatisticas_jogo jeg
        JOIN jogos j ON j.id = jeg.jogo_id
        WHERE j.nosso_time_id = %s AND j.rodada_numero IS NOT NULL AND j.data_jogo < CURRENT_DATE
          AND jeg.{coluna} IS NOT NULL
          AND ((j.mandante = TRUE AND jeg.lado = 'mandante')
           OR (j.mandante = FALSE AND jeg.lado = 'visitante'))
        GROUP BY j.id, j.rodada_numero
        """,
        (time_id,),
    )
    por_rodada = {}
    for rodada_numero, valor in cur.fetchall():
        por_rodada.setdefault(rodada_numero, []).append(float(valor) if valor is not None else 0.0)
    return por_rodada


def detectar_quebra(bruto_por_rodada):
    """Desliza uma janela de TAMANHO_JANELA_QUEBRA rodadas por cima do
    dado bruto (ordenado por número de rodada), comparando cada janela com
    a janela seguinte - devolve onde a diferença entre as duas médias é a
    MAIOR de todas. None se não tiver rodada suficiente pra formar 2
    janelas completas (jogos_amostra de cada rodada não entra na conta
    aqui - só o número de POSIÇÕES de rodada com pelo menos 1 dado)."""
    pontos = sorted(
        (rodada_numero, sum(valores) / len(valores))
        for rodada_numero, valores in bruto_por_rodada.items()
        if valores
    )
    n = len(pontos)
    if n < TAMANHO_JANELA_QUEBRA * 2:
        return None

    melhor = None
    for i in range(0, n - TAMANHO_JANELA_QUEBRA * 2 + 1):
        janela_antes = pontos[i:i + TAMANHO_JANELA_QUEBRA]
        janela_depois = pontos[i + TAMANHO_JANELA_QUEBRA:i + TAMANHO_JANELA_QUEBRA * 2]
        media_antes = sum(v for _, v in janela_antes) / TAMANHO_JANELA_QUEBRA
        media_depois = sum(v for _, v in janela_depois) / TAMANHO_JANELA_QUEBRA
        diferenca = abs(media_depois - media_antes)
        if melhor is None or diferenca > melhor["diferenca"]:
            melhor = {
                "rodada_quebra": janela_depois[0][0],
                "valor_antes": round(media_antes, 4),
                "valor_depois": round(media_depois, 4),
                "diferenca": round(diferenca, 4),
            }
    return melhor


def calcular_padroes_rodada_time(cur, time_id):
    """Calcula o bruto por rodada + a quebra detectada, pra todos os
    mercados cobertos, de UM time. Devolve (bruto_pra_salvar, quebras_pra_salvar)."""
    fontes = {"resultado": lambda: calcular_bruto_rodada_resultado(cur, time_id),
              "cartao": lambda: calcular_bruto_rodada_cartao(cur, time_id)}
    for tipo, coluna in TIPOS_PADRAO_RODADA_ESTATISTICA_JOGO.items():
        fontes[tipo] = lambda coluna=coluna: calcular_bruto_rodada_estatistica_jogo(cur, time_id, coluna)
    for tipo, coluna in TIPOS_PADRAO_RODADA_SOMA_JOGADOR.items():
        fontes[tipo] = lambda coluna=coluna: calcular_bruto_rodada_soma_jogador(cur, time_id, coluna)

    bruto_pra_salvar = []
    quebras_pra_salvar = []
    for tipo, funcao_busca in fontes.items():
        bruto_por_rodada = funcao_busca()
        for rodada_numero, valores in bruto_por_rodada.items():
            if not valores:
                continue
            media = sum(valores) / len(valores)
            bruto_pra_salvar.append((time_id, tipo, rodada_numero, round(media, 4), len(valores)))

        quebra = detectar_quebra(bruto_por_rodada)
        if quebra:
            quebras_pra_salvar.append((time_id, tipo, quebra))

    return bruto_pra_salvar, quebras_pra_salvar


def salvar_padroes_rodada(cur, bruto_pra_salvar, quebras_pra_salvar):
    for time_id, tipo, rodada_numero, valor, jogos_amostra in bruto_pra_salvar:
        cur.execute(
            """
            INSERT INTO padroes_rodada_bruto (time_id, tipo_padrao, rodada_numero, valor, jogos_amostra, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (time_id, tipo_padrao, rodada_numero) DO UPDATE SET
                valor = EXCLUDED.valor, jogos_amostra = EXCLUDED.jogos_amostra, atualizado_em = NOW()
            """,
            (time_id, tipo, rodada_numero, valor, jogos_amostra),
        )

    for time_id, tipo, quebra in quebras_pra_salvar:
        cur.execute(
            """
            INSERT INTO padroes_quebra_rodada
                (time_id, tipo_padrao, rodada_quebra, valor_antes, valor_depois, diferenca, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (time_id, tipo_padrao) DO UPDATE SET
                rodada_quebra = EXCLUDED.rodada_quebra, valor_antes = EXCLUDED.valor_antes,
                valor_depois = EXCLUDED.valor_depois, diferenca = EXCLUDED.diferenca, atualizado_em = NOW()
            """,
            (time_id, tipo, quebra["rodada_quebra"], quebra["valor_antes"],
             quebra["valor_depois"], quebra["diferenca"]),
        )
        sinal = "+" if quebra["valor_depois"] >= quebra["valor_antes"] else "-"
        print(f"  [{tipo}] quebra na rodada {quebra['rodada_quebra']}: "
              f"{quebra['valor_antes']} -> {quebra['valor_depois']} ({sinal}{abs(quebra['diferenca']):.3f})")


# NOVO (Fase C - comportamento por zona da tabela, só exibição): como
# cada time se comporta dependendo de estar no G4, no meio de tabela, ou
# no Z4 - e como isso muda dependendo do resultado do jogo ANTERIOR
# (efeito de sequência/momento, ex: "ganhou 2 seguidas e sai da zona" vs
# "perdeu 2 seguidas e afunda mais"). Usa a tabela reconstruída da Fase A
# (tabela.py/calcular_tabela) pra saber em que zona o time estava ANTES de
# cada jogo (rodada anterior, na mesma temporada). Zonas G4/Z4/meio -
# aproximação simples de propósito (não distingue Libertadores/
# Sul-Americana ainda, ver documentação da decisão).
#
# Cobre os mesmos mercados da Fase B (resultado + cartão, escanteio,
# falta, chute, chute no gol, impedimento, desarme).
#
# Só EXIBIÇÃO por enquanto - não entra em nenhuma fórmula de
# recomendação/VE ainda.
JOGOS_MINIMOS_ZONA = 3  # menor que o padrão (5) de propósito - zona x condição já fatia bastante o dado

_cache_tabela_temporada_rodada = {}


def _tabela_cacheada(cur, temporada, rodada_numero):
    """Evita recalcular a tabela reconstruída várias vezes pra
    (temporada, rodada) iguais, entre times diferentes, na mesma
    execução do script."""
    chave = (temporada, rodada_numero)
    if chave not in _cache_tabela_temporada_rodada:
        _cache_tabela_temporada_rodada[chave] = calcular_tabela(cur, temporada, rodada_numero)
    return _cache_tabela_temporada_rodada[chave]


def buscar_jogos_ordenados_time(cur, time_id):
    """Jogos desse time, em ordem cronológica, com temporada (derivada do
    ano de data_jogo - o Brasileirão não cruza virada de ano) e resultado
    (vitória/empate/derrota) - espinha dorsal pra computar zona/momento de
    cada jogo."""
    cur.execute(
        """
        SELECT id, EXTRACT(YEAR FROM data_jogo)::int AS temporada, rodada_numero,
               CASE WHEN placar_corinthians > placar_adversario THEN 'vitoria'
                    WHEN placar_corinthians = placar_adversario THEN 'empate'
                    ELSE 'derrota' END AS resultado
        FROM jogos
        WHERE nosso_time_id = %s AND rodada_numero IS NOT NULL
          AND placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL
          AND data_jogo < CURRENT_DATE
        ORDER BY temporada, rodada_numero
        """,
        (time_id,),
    )
    return cur.fetchall()


def calcular_valor_por_jogo_cartao(cur, time_id):
    """{jogo_id: cartões do NOSSO time nesse jogo} - mesma subconsulta
    separada usada no resto do motor_padroes.py, pra não contar em
    dobro."""
    cur.execute(
        """
        SELECT contagem.jogo_id, contagem.total
        FROM (
            SELECT j.id AS jogo_id,
                   (SELECT COUNT(*) FROM cartoes c WHERE c.jogo_id = j.id AND c.lado = 'mandante') AS total,
                   COUNT(DISTINCT eg.lado) AS lados
            FROM jogos j
            JOIN estatisticas_jogo eg ON eg.jogo_id = j.id
            WHERE j.nosso_time_id = %s AND j.rodada_numero IS NOT NULL AND j.data_jogo < CURRENT_DATE
            GROUP BY j.id
        ) contagem
        WHERE contagem.lados = 2
        """,
        (time_id,),
    )
    return {jogo_id: float(v) for jogo_id, v in cur.fetchall()}


def calcular_valor_por_jogo_estatistica_jogo(cur, time_id, coluna):
    """{jogo_id: valor do NOSSO time nesse jogo} - a partir de
    estatisticas_jogo (lado real, precisa traduzir via jogos.mandante)."""
    cur.execute(
        f"""
        SELECT eg.jogo_id, eg.{coluna}
        FROM estatisticas_jogo eg
        JOIN jogos j ON j.id = eg.jogo_id
        WHERE j.nosso_time_id = %s AND j.rodada_numero IS NOT NULL AND j.data_jogo < CURRENT_DATE
          AND eg.{coluna} IS NOT NULL
          AND ((j.mandante = TRUE AND eg.lado = 'mandante')
           OR (j.mandante = FALSE AND eg.lado = 'visitante'))
        """,
        (time_id,),
    )
    return {jogo_id: float(v) for jogo_id, v in cur.fetchall()}


def calcular_valor_por_jogo_soma_jogador(cur, time_id, coluna):
    """{jogo_id: soma do NOSSO time nesse jogo} - a partir de
    jogador_estatisticas_jogo (mesma tradução de lado)."""
    cur.execute(
        f"""
        SELECT j.id, SUM(jeg.{coluna})
        FROM jogador_estatisticas_jogo jeg
        JOIN jogos j ON j.id = jeg.jogo_id
        WHERE j.nosso_time_id = %s AND j.rodada_numero IS NOT NULL AND j.data_jogo < CURRENT_DATE
          AND jeg.{coluna} IS NOT NULL
          AND ((j.mandante = TRUE AND jeg.lado = 'mandante')
           OR (j.mandante = FALSE AND jeg.lado = 'visitante'))
        GROUP BY j.id
        """,
        (time_id,),
    )
    return {jogo_id: (float(v) if v is not None else 0.0) for jogo_id, v in cur.fetchall()}


def calcular_padroes_zona_time(cur, time_id, api_football_team_id):
    """Calcula, pra esse time, a média/frequência de cada mercado
    condicionada à zona da tabela (G4/meio/Z4) em que ele estava ANTES de
    cada jogo, e ao resultado do jogo ANTERIOR (momento/sequência).
    Devolve lista de (time_id, tipo, zona, condicao, valor, jogos_amostra)
    pronta pra salvar."""
    jogos_ordenados = buscar_jogos_ordenados_time(cur, time_id)
    if not jogos_ordenados:
        return []

    valores_por_mercado = {
        "resultado": {jid: (1.0 if res == "vitoria" else 0.0) for jid, _, _, res in jogos_ordenados},
        "cartao": calcular_valor_por_jogo_cartao(cur, time_id),
    }
    for tipo, coluna in TIPOS_PADRAO_RODADA_ESTATISTICA_JOGO.items():
        valores_por_mercado[tipo] = calcular_valor_por_jogo_estatistica_jogo(cur, time_id, coluna)
    for tipo, coluna in TIPOS_PADRAO_RODADA_SOMA_JOGADOR.items():
        valores_por_mercado[tipo] = calcular_valor_por_jogo_soma_jogador(cur, time_id, coluna)

    # NOVO: anda cronologicamente, calculando zona ANTES de cada jogo
    # (rodada anterior, via tabela reconstruída) e o resultado do jogo
    # ANTERIOR (reiniciado a cada temporada nova - não carrega "momento"
    # de uma temporada pra outra, faz sentido: elenco/contexto muda).
    contextos = []  # (jogo_id, zona_antes, condicao)
    resultado_anterior_por_temporada = {}
    for jogo_id, temporada, rodada_numero, resultado in jogos_ordenados:
        rodada_anterior = rodada_numero - 1
        zona_antes = None
        if rodada_anterior >= 1:
            tabela_rodada = _tabela_cacheada(cur, temporada, rodada_anterior)
            item = next((t for t in tabela_rodada if t["time_api_id"] == api_football_team_id), None)
            zona_antes = item["zona"] if item else None

        if zona_antes:
            contextos.append((jogo_id, zona_antes, "geral"))
            resultado_anterior = resultado_anterior_por_temporada.get(temporada)
            if resultado_anterior:
                contextos.append((jogo_id, zona_antes, f"apos_{resultado_anterior}"))

        resultado_anterior_por_temporada[temporada] = resultado

    agregados = {}
    for jogo_id, zona_antes, condicao in contextos:
        for tipo, valores in valores_por_mercado.items():
            valor = valores.get(jogo_id)
            if valor is None:
                continue
            agregados.setdefault((tipo, zona_antes, condicao), []).append(valor)

    resultado_final = []
    for (tipo, zona, condicao), valores in agregados.items():
        if len(valores) < JOGOS_MINIMOS_ZONA:
            continue
        media = sum(valores) / len(valores)
        resultado_final.append((time_id, tipo, zona, condicao, round(media, 4), len(valores)))

    return resultado_final


def salvar_padroes_zona(cur, resultados):
    for time_id, tipo, zona, condicao, valor, jogos_amostra in resultados:
        cur.execute(
            """
            INSERT INTO padroes_zona_time (time_id, tipo_padrao, zona, condicao, valor, jogos_amostra, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (time_id, tipo_padrao, zona, condicao) DO UPDATE SET
                valor = EXCLUDED.valor, jogos_amostra = EXCLUDED.jogos_amostra, atualizado_em = NOW()
            """,
            (time_id, tipo, zona, condicao, valor, jogos_amostra),
        )
    if resultados:
        print(f"  {len(resultados)} combinação(ões) zona/condição/mercado calculadas.")


def calcular_posse_time(cur, time_id):
    """NOVO (estatísticas de time): média de posse de bola do time - coluna
    já coletada desde o início (estatisticas_jogo.posse_de_bola), nunca
    tinha virado estatística visível em lugar nenhum. Diferente dos outros
    padrões de time, não faz sentido testar "linha" (não é uma contagem,
    já é um percentual) - só guarda a média mesmo."""
    cur.execute(
        """
        SELECT eg.posse_de_bola
        FROM estatisticas_jogo eg
        JOIN jogos j ON j.id = eg.jogo_id
        WHERE j.nosso_time_id = %s
          AND ((j.mandante = TRUE AND eg.lado = 'mandante')
           OR (j.mandante = FALSE AND eg.lado = 'visitante'))
          AND eg.posse_de_bola IS NOT NULL
        ORDER BY j.data_jogo DESC
        LIMIT %s
        """,
        (time_id, JANELA_MAXIMA_DE_JOGOS),
    )
    valores = [row[0] for row in cur.fetchall()]

    jogos_analisados = len(valores)
    if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
        return None, jogos_analisados

    media = round(sum(float(v) for v in valores) / jogos_analisados, 1)
    return media, jogos_analisados


def salvar_posse_time(cur, media, jogos_analisados, time_id):
    """NOVO: salva a posse média em padroes_time_linha com um `linha`
    sentinela (0) e jogos_acima/frequencia sem uso real - o template só lê
    o campo `media` pra esse tipo (ver buscar_estatisticas_time em app.py,
    que não gera nenhum "item de linha" pro tipo 'posse')."""
    cur.execute(
        """
        INSERT INTO padroes_time_linha
            (time_id, tipo, linha, jogos_analisados, jogos_acima, frequencia, media, atualizado_em)
        VALUES (%s, 'posse', 0, %s, 0, 0, %s, NOW())
        ON CONFLICT (time_id, tipo, linha) DO UPDATE SET
            jogos_analisados = EXCLUDED.jogos_analisados,
            media = EXCLUDED.media,
            atualizado_em = NOW()
        """,
        (time_id, jogos_analisados, media),
    )
    print(f"  [posse] Média: {media}% ({jogos_analisados} jogos)")


def calcular_padroes_escanteio_total(cur, time_id):
    """NOVO: escanteios do jogo INTEIRO (mandante + visitante somados),
    diferente de calcular_padroes_escanteio, que olha só o lado do nosso
    time. Mercados de "total do jogo" tendem a ter frequência histórica
    mais alta que mercados de um lado só, o que aumenta a chance de gerar
    recomendação com odd mais baixa.
    NOVO (multi-time): filtra por nosso_time_id."""
    cur.execute(
        """
        SELECT totais.total_escanteios
        FROM (
            SELECT eg.jogo_id, j.data_jogo, SUM(eg.escanteios) AS total_escanteios,
                   COUNT(DISTINCT eg.lado) AS lados
            FROM estatisticas_jogo eg
            JOIN jogos j ON j.id = eg.jogo_id
            WHERE j.data_jogo < CURRENT_DATE AND eg.escanteios IS NOT NULL AND j.nosso_time_id = %s
            GROUP BY eg.jogo_id, j.data_jogo
        ) totais
        WHERE totais.lados = 2
        ORDER BY totais.data_jogo DESC
        LIMIT %s
        """,
        (time_id, JANELA_MAXIMA_DE_JOGOS),
    )
    valores = [row[0] for row in cur.fetchall()]

    jogos_analisados = len(valores)
    if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
        return None, jogos_analisados

    media = round(sum(float(v) for v in valores) / jogos_analisados, 2)

    resultados = []
    for linha in LINHAS_ESCANTEIO_TOTAL:
        jogos_acima = sum(1 for v in valores if float(v) > linha)
        frequencia = round(100 * jogos_acima / jogos_analisados, 2)
        resultados.append((linha, jogos_analisados, jogos_acima, frequencia, media))

    return resultados, jogos_analisados


def salvar_padroes_escanteio_total(cur, resultados, time_id):
    for linha, jogos_analisados, jogos_acima, frequencia, media in resultados:
        cur.execute(
            """
            INSERT INTO padroes_escanteio_total (time_id, linha, jogos_analisados, jogos_acima_da_linha, frequencia, media, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (time_id, linha) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                jogos_acima_da_linha = EXCLUDED.jogos_acima_da_linha,
                frequencia = EXCLUDED.frequencia,
                media = EXCLUDED.media,
                atualizado_em = NOW()
            """,
            (time_id, linha, jogos_analisados, jogos_acima, frequencia, media),
        )
        print(f"  Mais de {linha} escanteios (total do jogo): {jogos_acima}/{jogos_analisados} jogos ({frequencia}%)")


def calcular_padroes_cartao_total(cur, time_id):
    """Cartões do jogo INTEIRO (mandante + visitante somados). Só
    considera jogos completos (com estatísticas dos dois lados já salvas
    em estatisticas_jogo) como critério de que o jogo já foi totalmente
    processado - sem isso, um jogo ainda não coletado entraria como 0
    cartões por engano, em vez de simplesmente não entrar na amostra.
    CORRIGIDO: antes fazia JOIN direto com cartoes DEPOIS de já ter
    juntado com estatisticas_jogo - só que estatisticas_jogo tem 2
    linhas por jogo (mandante + visitante), então cada cartão real virava
    2 linhas na junção (uma pra cada lado de estatisticas_jogo), contando
    tudo em DOBRO (um jogo com 6 cartões de verdade aparecia como 12).
    Isso também explicava outro sintoma: como o total sempre saía par
    (dobro de um inteiro), mais de 4.5 e mais de 5.5 empatavam sempre
    - o valor real nunca cai em 5 (ímpar) pra diferenciar as duas linhas.
    Agora conta os cartões numa subconsulta separada, sem passar pela
    junção com estatisticas_jogo, então não duplica mais."""
    cur.execute(
        """
        SELECT contagem.total_cartoes
        FROM (
            SELECT j.id AS jogo_id, j.data_jogo,
                   (SELECT COUNT(*) FROM cartoes c WHERE c.jogo_id = j.id) AS total_cartoes,
                   COUNT(DISTINCT eg.lado) AS lados
            FROM jogos j
            JOIN estatisticas_jogo eg ON eg.jogo_id = j.id
            WHERE j.data_jogo < CURRENT_DATE AND j.nosso_time_id = %s
            GROUP BY j.id, j.data_jogo
        ) contagem
        WHERE contagem.lados = 2
        ORDER BY contagem.data_jogo DESC
        LIMIT %s
        """,
        (time_id, JANELA_MAXIMA_DE_JOGOS),
    )
    valores = [row[0] for row in cur.fetchall()]

    jogos_analisados = len(valores)
    if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
        return None, jogos_analisados

    media = round(sum(float(v) for v in valores) / jogos_analisados, 2)

    resultados = []
    for linha in LINHAS_CARTAO_TOTAL:
        jogos_acima = sum(1 for v in valores if float(v) > linha)
        frequencia = round(100 * jogos_acima / jogos_analisados, 2)
        resultados.append((linha, jogos_analisados, jogos_acima, frequencia, media))

    return resultados, jogos_analisados


def salvar_padroes_cartao_total(cur, resultados, time_id):
    for linha, jogos_analisados, jogos_acima, frequencia, media in resultados:
        cur.execute(
            """
            INSERT INTO padroes_cartao_total (time_id, linha, jogos_analisados, jogos_acima_da_linha, frequencia, media, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (time_id, linha) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                jogos_acima_da_linha = EXCLUDED.jogos_acima_da_linha,
                frequencia = EXCLUDED.frequencia,
                media = EXCLUDED.media,
                atualizado_em = NOW()
            """,
            (time_id, linha, jogos_analisados, jogos_acima, frequencia, media),
        )
        print(f"  Mais de {linha} cartões (total do jogo): {jogos_acima}/{jogos_analisados} jogos ({frequencia}%)")


def buscar_id_time(cur, nome):
    cur.execute("SELECT id FROM times WHERE nome = %s", (nome,))
    row = cur.fetchone()
    return row[0] if row else None


def buscar_times_rastreados(cur):
    """NOVO (multi-time): times marcados explicitamente como rastreados
    (`rastreado = TRUE`) - não basta ter os ids preenchidos (um time pode
    ter isso por coincidência, sem nunca ter sido escolhido de verdade)."""
    cur.execute(
        "SELECT id, nome, api_football_team_id FROM times "
        "WHERE rastreado = TRUE AND api_football_team_id IS NOT NULL ORDER BY nome"
    )
    return cur.fetchall()


def buscar_adversarios_com_historico(cur, corinthians_id):
    """NOVO (confronto direto): lista os adversários que já enfrentaram esse
    time pelo menos JOGOS_MINIMOS_CONFRONTO vezes, com jogo já concluído e
    mandante_id/visitante_id preenchidos.
    NOVO (multi-time): filtra também por nosso_time_id - sem isso, um jogo
    entre DOIS times rastreados (ex: Corinthians x Athletico Paranaense)
    seria contado duas vezes (uma por linha, uma por time)."""
    cur.execute(
        """
        SELECT CASE WHEN mandante_id = %s THEN visitante_id ELSE mandante_id END AS adversario_id,
               COUNT(*) AS total
        FROM jogos
        WHERE (mandante_id = %s OR visitante_id = %s)
          AND mandante_id IS NOT NULL AND visitante_id IS NOT NULL
          AND data_jogo < CURRENT_DATE AND nosso_time_id = %s
        GROUP BY adversario_id
        HAVING COUNT(*) >= %s
        """,
        (corinthians_id, corinthians_id, corinthians_id, corinthians_id, JOGOS_MINIMOS_CONFRONTO),
    )
    return cur.fetchall()


def condicao_confronto(mandante_filtro):
    """NOVO (confronto direto): monta a condição SQL que filtra os jogos
    contra um adversário específico, considerando o lado (geral, só como
    mandante, ou só como visitante).
    NOVO (multi-time): também exige j.nosso_time_id = %(corinthians_id)s -
    sem isso, um jogo entre DOIS times rastreados (ex: Corinthians x
    Athletico Paranaense) seria contado duas vezes (uma linha por time)."""
    if mandante_filtro == "mandante":
        condicao_lado = "j.mandante_id = %(corinthians_id)s AND j.visitante_id = %(adversario_id)s"
    elif mandante_filtro == "visitante":
        condicao_lado = "j.mandante_id = %(adversario_id)s AND j.visitante_id = %(corinthians_id)s"
    else:
        condicao_lado = ("((j.mandante_id = %(corinthians_id)s AND j.visitante_id = %(adversario_id)s) "
                          "OR (j.mandante_id = %(adversario_id)s AND j.visitante_id = %(corinthians_id)s))")
    return f"({condicao_lado}) AND j.nosso_time_id = %(corinthians_id)s"


def buscar_totais_escanteio_confronto(cur, corinthians_id, adversario_id, mandante_filtro):
    condicao = condicao_confronto(mandante_filtro)
    cur.execute(
        f"""
        SELECT totais.total_escanteios
        FROM (
            SELECT eg.jogo_id, SUM(eg.escanteios) AS total_escanteios,
                   COUNT(DISTINCT eg.lado) AS lados
            FROM estatisticas_jogo eg
            JOIN jogos j ON j.id = eg.jogo_id
            WHERE {condicao} AND j.data_jogo < CURRENT_DATE AND eg.escanteios IS NOT NULL
            GROUP BY eg.jogo_id
        ) totais
        WHERE totais.lados = 2
        """,
        {"corinthians_id": corinthians_id, "adversario_id": adversario_id},
    )
    return [row[0] for row in cur.fetchall()]


def buscar_totais_cartao_confronto(cur, corinthians_id, adversario_id, mandante_filtro):
    """CORRIGIDO: mesmo bug de contagem duplicada das outras funções de
    cartão total (JOIN direto com `cartoes` depois de `estatisticas_jogo`,
    que tem 2 linhas por jogo, duplicava cada cartão) - agora conta numa
    subconsulta separada."""
    condicao = condicao_confronto(mandante_filtro)
    cur.execute(
        f"""
        SELECT contagem.total_cartoes
        FROM (
            SELECT j.id AS jogo_id,
                   (SELECT COUNT(*) FROM cartoes c WHERE c.jogo_id = j.id) AS total_cartoes,
                   COUNT(DISTINCT eg.lado) AS lados
            FROM jogos j
            JOIN estatisticas_jogo eg ON eg.jogo_id = j.id
            WHERE {condicao} AND j.data_jogo < CURRENT_DATE
            GROUP BY j.id
        ) contagem
        WHERE contagem.lados = 2
        """,
        {"corinthians_id": corinthians_id, "adversario_id": adversario_id},
    )
    return [row[0] for row in cur.fetchall()]


def buscar_totais_cartao_time_confronto(cur, corinthians_id, adversario_id, mandante_filtro):
    """NOVO (confronto direto pra cartão de TIME): cartões recebidos pelo
    NOSSO time nos jogos contra um adversário específico - diferente de
    buscar_totais_cartao_confronto, que soma os cartões dos DOIS times.

    Por que existe: sem isso, o mercado "Cartões - Mais/Menos [Time]"
    usava sempre a média geral do time contra qualquer adversário,
    enquanto "Cartões Total do Jogo" já tinha confronto direto. Num
    clássico (ex: Corinthians x Palmeiras), o total enxergava a rivalidade
    e subia a linha, mas o mercado por time não - gerando recomendações
    contraditórias do MESMO jogo (ex: "Menos de 3.5 pro Corinthians" +
    "Menos de 2.5 pro Palmeiras" convivendo com "Mais de 8.5 no total",
    que somam 6 contra 8.5). Pior: como são tipos de mercado diferentes,
    a trava de mercado repetido em combinacoes.py não impedia as três de
    caírem na MESMA múltipla, sendo multiplicadas como independentes
    quando na prática são quase mutuamente exclusivas.

    Detalhe importante do schema: em `cartoes`, o campo `lado` é gravado
    relativo ao NOSSO time (ver popular_banco.py/salvar_eventos), NÃO ao
    mandante/visitante real - por isso `c.lado = 'mandante'` aqui
    significa "cartão do nosso time", igual em calcular_padrao_cartao_time.
    Como condicao_confronto já exige j.nosso_time_id = corinthians_id, a
    perspectiva é sempre a do time que estamos analisando.

    Conta numa subconsulta separada de propósito: JOIN direto com
    `cartoes` depois de `estatisticas_jogo` (que tem 2 linhas por jogo)
    duplicaria cada cartão - mesmo bug já corrigido nas funções vizinhas."""
    condicao = condicao_confronto(mandante_filtro)
    cur.execute(
        f"""
        SELECT contagem.total_cartoes
        FROM (
            SELECT j.id AS jogo_id,
                   (SELECT COUNT(*) FROM cartoes c
                     WHERE c.jogo_id = j.id AND c.lado = 'mandante') AS total_cartoes,
                   COUNT(DISTINCT eg.lado) AS lados
            FROM jogos j
            JOIN estatisticas_jogo eg ON eg.jogo_id = j.id
            WHERE {condicao} AND j.data_jogo < CURRENT_DATE
            GROUP BY j.id
        ) contagem
        WHERE contagem.lados = 2
        """,
        {"corinthians_id": corinthians_id, "adversario_id": adversario_id},
    )
    return [row[0] for row in cur.fetchall()]


def buscar_totais_falta_confronto(cur, corinthians_id, adversario_id, mandante_filtro):
    condicao = condicao_confronto(mandante_filtro)
    cur.execute(
        f"""
        SELECT totais.total_faltas
        FROM (
            SELECT eg.jogo_id, SUM(eg.faltas) AS total_faltas,
                   COUNT(DISTINCT eg.lado) AS lados
            FROM estatisticas_jogo eg
            JOIN jogos j ON j.id = eg.jogo_id
            WHERE {condicao} AND j.data_jogo < CURRENT_DATE AND eg.faltas IS NOT NULL
            GROUP BY eg.jogo_id
        ) totais
        WHERE totais.lados = 2
        """,
        {"corinthians_id": corinthians_id, "adversario_id": adversario_id},
    )
    return [row[0] for row in cur.fetchall()]


def buscar_totais_chute_confronto(cur, corinthians_id, adversario_id, mandante_filtro):
    """Chutes no gol (ambos os times, somados) - vem da soma das
    estatísticas individuais dos jogadores no jogo, já que não existe um
    total agregado por lado salvo em estatisticas_jogo pra essa métrica."""
    condicao = condicao_confronto(mandante_filtro)
    cur.execute(
        f"""
        SELECT SUM(jeg.chutes_no_gol)
        FROM jogador_estatisticas_jogo jeg
        JOIN jogos j ON j.id = jeg.jogo_id
        WHERE {condicao} AND j.data_jogo < CURRENT_DATE AND jeg.chutes_no_gol IS NOT NULL
        GROUP BY jeg.jogo_id
        """,
        {"corinthians_id": corinthians_id, "adversario_id": adversario_id},
    )
    return [row[0] for row in cur.fetchall()]


def calcular_frequencias_linha(valores, linhas_testadas):
    jogos_analisados = len(valores)
    resultados = []
    for linha in linhas_testadas:
        jogos_acima = sum(1 for v in valores if float(v) > linha)
        frequencia = round(100 * jogos_acima / jogos_analisados, 2) if jogos_analisados else 0.0
        resultados.append((linha, jogos_acima, frequencia))
    return resultados


def salvar_padrao_confronto_linha(cur, nosso_time_id, adversario_id, mandante_filtro, tipo_padrao, jogos_analisados, resultados):
    amostra_pequena = jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR
    for linha, ocorrencias, frequencia in resultados:
        cur.execute(
            """
            INSERT INTO padroes_confronto_direto
                (nosso_time_id, adversario_id, mandante_filtro, tipo_padrao, linha, resultado,
                 jogos_analisados, ocorrencias, frequencia, amostra_pequena, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, '', %s, %s, %s, %s, NOW())
            ON CONFLICT (nosso_time_id, adversario_id, mandante_filtro, tipo_padrao, linha, resultado) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                ocorrencias = EXCLUDED.ocorrencias,
                frequencia = EXCLUDED.frequencia,
                amostra_pequena = EXCLUDED.amostra_pequena,
                atualizado_em = NOW()
            """,
            (nosso_time_id, adversario_id, mandante_filtro, tipo_padrao, linha,
             jogos_analisados, ocorrencias, frequencia, amostra_pequena),
        )


def calcular_resultado_confronto(cur, corinthians_id, adversario_id, mandante_filtro):
    condicao = condicao_confronto(mandante_filtro)
    cur.execute(
        f"""
        SELECT j.placar_corinthians, j.placar_adversario
        FROM jogos j
        WHERE {condicao} AND j.data_jogo < CURRENT_DATE
          AND j.placar_corinthians IS NOT NULL AND j.placar_adversario IS NOT NULL
        """,
        {"corinthians_id": corinthians_id, "adversario_id": adversario_id},
    )
    jogos = cur.fetchall()
    total = len(jogos)
    if total == 0:
        return None, 0

    contagem = {"vitoria": 0, "empate": 0, "derrota": 0}
    for placar_cor, placar_adv in jogos:
        if placar_cor > placar_adv:
            contagem["vitoria"] += 1
        elif placar_cor == placar_adv:
            contagem["empate"] += 1
        else:
            contagem["derrota"] += 1
    return contagem, total


def salvar_padrao_confronto_resultado(cur, nosso_time_id, adversario_id, mandante_filtro, contagem, total):
    amostra_pequena = total < JOGOS_MINIMOS_PARA_ANALISAR
    for resultado, ocorrencias in contagem.items():
        frequencia = round(100 * ocorrencias / total, 2)
        cur.execute(
            """
            INSERT INTO padroes_confronto_direto
                (nosso_time_id, adversario_id, mandante_filtro, tipo_padrao, linha, resultado,
                 jogos_analisados, ocorrencias, frequencia, amostra_pequena, atualizado_em)
            VALUES (%s, %s, %s, 'resultado_final', 0, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (nosso_time_id, adversario_id, mandante_filtro, tipo_padrao, linha, resultado) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                ocorrencias = EXCLUDED.ocorrencias,
                frequencia = EXCLUDED.frequencia,
                amostra_pequena = EXCLUDED.amostra_pequena,
                atualizado_em = NOW()
            """,
            (nosso_time_id, adversario_id, mandante_filtro, resultado, total, ocorrencias, frequencia, amostra_pequena),
        )


def calcular_padroes_confronto_direto(cur, time_id):
    """NOVO: calcula padrões específicos por adversário (não só a média
    geral por mandante/visitante) - escanteio total, cartão total, falta
    total, chutes no gol total e resultado, cada um separado em três
    visões: 'geral' (os dois lados juntos), 'mandante' (só quando esse time
    manda esse confronto) e 'visitante' (só quando visita).
    Isso permite capturar rivalidades específicas (ex: jogo sempre mais
    truncado/com mais falta contra um adversário em particular) e mandos de
    campo muito marcantes contra um time específico (ex: anos sem perder
    em casa pra um rival), que a média geral do time inteiro não enxerga.
    NOVO (multi-time): recebe time_id como parâmetro, chamada uma vez por
    time rastreado."""
    corinthians_id = time_id

    adversarios = buscar_adversarios_com_historico(cur, corinthians_id)
    if not adversarios:
        print(f"  Nenhum adversário com pelo menos {JOGOS_MINIMOS_CONFRONTO} jogos analisados ainda.")
        return 0

    total_calculado = 0
    for adversario_id, total_jogos in adversarios:
        cur.execute("SELECT nome FROM times WHERE id = %s", (adversario_id,))
        row = cur.fetchone()
        nome_adversario = row[0] if row else f"time #{adversario_id}"

        for mandante_filtro in ("geral", "mandante", "visitante"):
            valores = buscar_totais_escanteio_confronto(cur, corinthians_id, adversario_id, mandante_filtro)
            if len(valores) >= JOGOS_MINIMOS_CONFRONTO:
                resultados = calcular_frequencias_linha(valores, LINHAS_ESCANTEIO_TOTAL)
                salvar_padrao_confronto_linha(cur, corinthians_id, adversario_id, mandante_filtro, "escanteio_total", len(valores), resultados)
                total_calculado += 1

            valores = buscar_totais_cartao_confronto(cur, corinthians_id, adversario_id, mandante_filtro)
            if len(valores) >= JOGOS_MINIMOS_CONFRONTO:
                resultados = calcular_frequencias_linha(valores, LINHAS_CARTAO_TOTAL)
                salvar_padrao_confronto_linha(cur, corinthians_id, adversario_id, mandante_filtro, "cartao_total", len(valores), resultados)
                total_calculado += 1

            # NOVO (confronto direto pra cartão de TIME): mesma ideia do
            # cartao_total logo acima, mas contando só os cartões do NOSSO
            # time. Usa LINHAS_CARTAO_TIME (0.5 a 3.5), não
            # LINHAS_CARTAO_TOTAL (2.5 a 5.5) - as linhas do mercado por
            # time são naturalmente mais baixas, já que é metade do jogo.
            valores = buscar_totais_cartao_time_confronto(cur, corinthians_id, adversario_id, mandante_filtro)
            if len(valores) >= JOGOS_MINIMOS_CONFRONTO:
                resultados = calcular_frequencias_linha(valores, LINHAS_CARTAO_TIME)
                salvar_padrao_confronto_linha(cur, corinthians_id, adversario_id, mandante_filtro, "cartao_time", len(valores), resultados)
                total_calculado += 1

            valores = buscar_totais_falta_confronto(cur, corinthians_id, adversario_id, mandante_filtro)
            if len(valores) >= JOGOS_MINIMOS_CONFRONTO:
                resultados = calcular_frequencias_linha(valores, LINHAS_FALTA_TOTAL)
                salvar_padrao_confronto_linha(cur, corinthians_id, adversario_id, mandante_filtro, "falta_total", len(valores), resultados)
                total_calculado += 1

            valores = buscar_totais_chute_confronto(cur, corinthians_id, adversario_id, mandante_filtro)
            if len(valores) >= JOGOS_MINIMOS_CONFRONTO:
                resultados = calcular_frequencias_linha(valores, LINHAS_CHUTE_TOTAL)
                salvar_padrao_confronto_linha(cur, corinthians_id, adversario_id, mandante_filtro, "chute_total", len(valores), resultados)
                total_calculado += 1

            contagem, total = calcular_resultado_confronto(cur, corinthians_id, adversario_id, mandante_filtro)
            if contagem and total >= JOGOS_MINIMOS_CONFRONTO:
                salvar_padrao_confronto_resultado(cur, corinthians_id, adversario_id, mandante_filtro, contagem, total)
                total_calculado += 1

        print(f"  {nome_adversario}: {total_jogos} confronto(s) direto(s) no histórico.")

    return total_calculado


def calcular_padrao_linha_jogador(cur, coluna, linhas_testadas):
    """Função genérica: para cada jogador, testa várias linhas (0.5, 1.5, ...)
    numa coluna numérica da tabela jogador_estatisticas_jogo (ex: desarmes).

    NOVO: mesmo filtro de lado usado em calcular_padroes_cartao - só conta
    jogos em que o jogador estava jogando pelo time dele.
    NOVO (multi-time): também filtra por nosso_time_id, mesmo motivo de
    calcular_padroes_cartao.
    NOVO (bug corrigido): a API-Football manda `null` pra essas colunas
    quando o valor real é ZERO (ex: 0 chutes no gol), não quando falta
    dado - confirmado comparando com outras colunas do mesmo jogador que
    nunca vêm vazias (como `passes`) nos mesmos jogos. Antes, esses jogos
    eram EXCLUÍDOS da amostra (como se não tivéssemos o dado), inflando a
    frequência pra cima (só sobravam os jogos em que ele teve pelo menos 1
    ocorrência). Agora trata `null` como 0 de verdade (COALESCE), incluindo
    esses jogos na amostra."""
    cur.execute("SELECT id, nome, time_atual_id FROM jogadores WHERE ativo = TRUE")
    jogadores = cur.fetchall()

    resultados = []

    for jogador_id, nome, time_atual_id in jogadores:
        cur.execute(
            f"""
            SELECT COALESCE(jeg.{coluna}, 0)
            FROM jogador_estatisticas_jogo jeg
            JOIN jogos j ON j.id = jeg.jogo_id
            WHERE jeg.jogador_id = %s AND j.nosso_time_id = %s
              AND ((jeg.lado = 'mandante' AND j.mandante = TRUE)
                OR (jeg.lado = 'visitante' AND j.mandante = FALSE))
            ORDER BY j.data_jogo DESC
            LIMIT %s
            """,
            (jogador_id, time_atual_id, JANELA_MAXIMA_DE_JOGOS),
        )
        valores = [row[0] for row in cur.fetchall()]

        jogos_analisados = len(valores)
        if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
            continue

        for linha in linhas_testadas:
            jogos_acima = sum(1 for v in valores if float(v) > linha)
            frequencia = round(100 * jogos_acima / jogos_analisados, 2)
            resultados.append((jogador_id, nome, linha, jogos_analisados, jogos_acima, frequencia))

    return resultados


def salvar_padrao_linha_jogador(cur, tipo, resultados):
    for jogador_id, nome, linha, jogos_analisados, jogos_acima, frequencia in resultados:
        cur.execute(
            """
            INSERT INTO padroes_jogador_linha (jogador_id, tipo, linha, jogos_analisados, jogos_acima, frequencia, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (jogador_id, tipo, linha) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                jogos_acima = EXCLUDED.jogos_acima,
                frequencia = EXCLUDED.frequencia,
                atualizado_em = NOW()
            """,
            (jogador_id, tipo, linha, jogos_analisados, jogos_acima, frequencia),
        )
    print(f"  {tipo}: {len(resultados)} linha(s)/jogador(es) calculados.")


def calcular_padrao_frequencia_jogador(cur, coluna):
    """Função genérica: para cada jogador, calcula a frequência de ter tido
    pelo menos 1 ocorrência (ex: pelo menos 1 impedimento no jogo).

    NOVO: mesmo filtro de lado usado em calcular_padroes_cartao - só conta
    jogos em que o jogador estava jogando pelo time dele.
    NOVO (multi-time): também filtra por nosso_time_id, mesmo motivo de
    calcular_padroes_cartao.
    NOVO (bug corrigido): mesmo problema e mesma correção de
    calcular_padrao_linha_jogador - a API-Football manda `null` quando o
    valor real é zero, não quando falta dado. Trata como 0 (COALESCE) em
    vez de excluir esses jogos da amostra."""
    cur.execute("SELECT id, nome, time_atual_id FROM jogadores WHERE ativo = TRUE")
    jogadores = cur.fetchall()

    resultados = []

    for jogador_id, nome, time_atual_id in jogadores:
        cur.execute(
            f"""
            SELECT COALESCE(jeg.{coluna}, 0)
            FROM jogador_estatisticas_jogo jeg
            JOIN jogos j ON j.id = jeg.jogo_id
            WHERE jeg.jogador_id = %s AND j.nosso_time_id = %s
              AND ((jeg.lado = 'mandante' AND j.mandante = TRUE)
                OR (jeg.lado = 'visitante' AND j.mandante = FALSE))
            ORDER BY j.data_jogo DESC
            LIMIT %s
            """,
            (jogador_id, time_atual_id, JANELA_MAXIMA_DE_JOGOS),
        )
        valores = [row[0] for row in cur.fetchall()]

        jogos_analisados = len(valores)
        if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
            continue

        jogos_com_evento = sum(1 for v in valores if v and v > 0)
        frequencia = round(100 * jogos_com_evento / jogos_analisados, 2)
        resultados.append((jogador_id, nome, jogos_analisados, jogos_com_evento, frequencia))

    return resultados


def salvar_padrao_frequencia_jogador(cur, tipo, resultados):
    for jogador_id, nome, jogos_analisados, jogos_com_evento, frequencia in resultados:
        cur.execute(
            """
            INSERT INTO padroes_jogador_frequencia (jogador_id, tipo, jogos_analisados, jogos_com_evento, frequencia, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (jogador_id, tipo) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                jogos_com_evento = EXCLUDED.jogos_com_evento,
                frequencia = EXCLUDED.frequencia,
                atualizado_em = NOW()
            """,
            (jogador_id, tipo, jogos_analisados, jogos_com_evento, frequencia),
        )
    print(f"  {tipo}: {len(resultados)} jogador(es) calculados.")


def calcular_padroes_resultado(cur, time_id):
    """Calcula a frequência histórica de vitória/empate/derrota desse time,
    separado por mandante e visitante. NOVO: também calcula uma linha 'geral'
    (sem filtrar por mandante/visitante) - usada como referência de base pra
    comparar com a forma recente (ver calcular_forma_recente).
    NOVO (multi-time): filtra por nosso_time_id."""
    resultados_finais = []

    combinacoes = [(True, "mandante"), (False, "visitante"), (None, "geral")]

    for lado_bool, lado_nome in combinacoes:
        if lado_bool is None:
            cur.execute(
                """
                SELECT placar_corinthians, placar_adversario
                FROM jogos
                WHERE nosso_time_id = %s AND placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL
                ORDER BY data_jogo DESC
                LIMIT %s
                """,
                (time_id, JANELA_MAXIMA_DE_JOGOS),
            )
        else:
            cur.execute(
                """
                SELECT placar_corinthians, placar_adversario
                FROM jogos
                WHERE nosso_time_id = %s AND mandante = %s
                  AND placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL
                ORDER BY data_jogo DESC
                LIMIT %s
                """,
                (time_id, lado_bool, JANELA_MAXIMA_DE_JOGOS),
            )
        jogos = cur.fetchall()
        total = len(jogos)
        if total < JOGOS_MINIMOS_PARA_ANALISAR:
            continue

        contagem = {"vitoria": 0, "empate": 0, "derrota": 0}
        for placar_cor, placar_adv in jogos:
            if placar_cor > placar_adv:
                contagem["vitoria"] += 1
            elif placar_cor == placar_adv:
                contagem["empate"] += 1
            else:
                contagem["derrota"] += 1

        for resultado, ocorrencias in contagem.items():
            frequencia = round(100 * ocorrencias / total, 2)
            resultados_finais.append((lado_nome, resultado, total, ocorrencias, frequencia))

    return resultados_finais


def salvar_padroes_resultado(cur, resultados, time_id):
    for lado, resultado, total, ocorrencias, frequencia in resultados:
        cur.execute(
            """
            INSERT INTO padroes_time_resultado (time_id, lado, resultado, jogos_analisados, ocorrencias, frequencia, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (time_id, lado, resultado) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                ocorrencias = EXCLUDED.ocorrencias,
                frequencia = EXCLUDED.frequencia,
                atualizado_em = NOW()
            """,
            (time_id, lado, resultado, total, ocorrencias, frequencia),
        )
        print(f"  {lado} - {resultado}: {ocorrencias}/{total} jogos ({frequencia}%)")


def calcular_padroes_dupla_chance_tempo(cur, time_id):
    """NOVO (Onda 2 - Dupla Chance por tempo): frequência de cada resultado
    de Dupla Chance (1X/12/2X) por tempo (1º/2º), separado por mandante/
    visitante - mesmo padrão de calcular_padroes_resultado, mas calculado
    em cima do placar de cada TEMPO isolado (intervalo pro 1º tempo,
    final-menos-intervalo pro 2º), não do placar final do jogo inteiro."""
    resultados_finais = []
    for lado_bool, lado_nome in [(True, "mandante"), (False, "visitante")]:
        cur.execute(
            """
            SELECT placar_corinthians, placar_adversario,
                   placar_corinthians_intervalo, placar_adversario_intervalo
            FROM jogos
            WHERE nosso_time_id = %s AND mandante = %s
              AND placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL
              AND placar_corinthians_intervalo IS NOT NULL AND placar_adversario_intervalo IS NOT NULL
            ORDER BY data_jogo DESC
            LIMIT %s
            """,
            (time_id, lado_bool, JANELA_MAXIMA_DE_JOGOS),
        )
        jogos = cur.fetchall()
        total = len(jogos)
        if total < JOGOS_MINIMOS_PARA_ANALISAR:
            continue

        janelas_periodo = [
            ("1T", lambda pc, pa, pci, pai: (pci, pai)),
            ("2T", lambda pc, pa, pci, pai: (pc - pci, pa - pai)),
        ]
        for periodo_nome, calc_placar in janelas_periodo:
            contagem = {"1X": 0, "12": 0, "2X": 0}
            for placar_cor, placar_adv, placar_cor_int, placar_adv_int in jogos:
                nosso_periodo, adv_periodo = calc_placar(placar_cor, placar_adv, placar_cor_int, placar_adv_int)
                if nosso_periodo == adv_periodo:
                    contagem["1X"] += 1
                    contagem["2X"] += 1
                elif nosso_periodo > adv_periodo:
                    # nosso time venceu esse período
                    contagem["1X" if lado_bool else "2X"] += 1
                    contagem["12"] += 1
                else:
                    # nosso time perdeu esse período
                    contagem["2X" if lado_bool else "1X"] += 1
                    contagem["12"] += 1

            for resultado, ocorrencias in contagem.items():
                frequencia = round(100 * ocorrencias / total, 2)
                resultados_finais.append((periodo_nome, lado_nome, resultado, total, frequencia))

    return resultados_finais


def salvar_padroes_dupla_chance_tempo(cur, resultados, time_id):
    for periodo, lado, resultado, jogos_analisados, frequencia in resultados:
        cur.execute(
            """
            INSERT INTO padroes_dupla_chance_tempo (time_id, periodo, lado, resultado, jogos_analisados, frequencia, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (time_id, periodo, lado, resultado) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                frequencia = EXCLUDED.frequencia,
                atualizado_em = NOW()
            """,
            (time_id, periodo, lado, resultado, jogos_analisados, frequencia),
        )
        print(f"  [{periodo}/{lado}] {resultado}: {frequencia}% ({jogos_analisados} jogos)")


def calcular_padroes_ambas_marcam_tempo(cur, time_id):
    """NOVO (Onda 2 - Ambas Marcam por tempo): frequência binária de Ambas
    Equipes Marcarem nesse tempo (1º/2º), separado por mandante/visitante -
    mesma fonte de dado (placar de intervalo) de calcular_padroes_dupla_chance_tempo."""
    resultados_finais = []
    for lado_bool, lado_nome in [(True, "mandante"), (False, "visitante")]:
        cur.execute(
            """
            SELECT placar_corinthians, placar_adversario,
                   placar_corinthians_intervalo, placar_adversario_intervalo
            FROM jogos
            WHERE nosso_time_id = %s AND mandante = %s
              AND placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL
              AND placar_corinthians_intervalo IS NOT NULL AND placar_adversario_intervalo IS NOT NULL
            ORDER BY data_jogo DESC
            LIMIT %s
            """,
            (time_id, lado_bool, JANELA_MAXIMA_DE_JOGOS),
        )
        jogos = cur.fetchall()
        total = len(jogos)
        if total < JOGOS_MINIMOS_PARA_ANALISAR:
            continue

        janelas_periodo = [
            ("1T", lambda pc, pa, pci, pai: (pci, pai)),
            ("2T", lambda pc, pa, pci, pai: (pc - pci, pa - pai)),
        ]
        for periodo_nome, calc_placar in janelas_periodo:
            com_ambas = 0
            for placar_cor, placar_adv, placar_cor_int, placar_adv_int in jogos:
                nosso_periodo, adv_periodo = calc_placar(placar_cor, placar_adv, placar_cor_int, placar_adv_int)
                if nosso_periodo > 0 and adv_periodo > 0:
                    com_ambas += 1
            frequencia = round(100 * com_ambas / total, 2)
            resultados_finais.append((periodo_nome, lado_nome, total, com_ambas, frequencia))

    return resultados_finais


def salvar_padroes_ambas_marcam_tempo(cur, resultados, time_id):
    for periodo, lado, jogos_analisados, jogos_com_ambas, frequencia in resultados:
        cur.execute(
            """
            INSERT INTO padroes_ambas_marcam_tempo (time_id, periodo, lado, jogos_analisados, jogos_com_ambas, frequencia, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (time_id, periodo, lado) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                jogos_com_ambas = EXCLUDED.jogos_com_ambas,
                frequencia = EXCLUDED.frequencia,
                atualizado_em = NOW()
            """,
            (time_id, periodo, lado, jogos_analisados, jogos_com_ambas, frequencia),
        )
        print(f"  [{periodo}/{lado}] Ambas Marcam: {jogos_com_ambas}/{jogos_analisados} jogos ({frequencia}%)")


def calcular_padrao_marca_ambos_tempos(cur, time_id):
    """NOVO (Onda 2 - Marca em Ambos os Tempos): frequência de o time
    marcar no 1º tempo E no 2º tempo, no mesmo jogo - sem separar por
    mandante/visitante (mercado sobre o jogo inteiro, escopo mais amplo
    que os mercados por tempo isolado acima)."""
    cur.execute(
        """
        SELECT placar_corinthians, placar_corinthians_intervalo
        FROM jogos
        WHERE nosso_time_id = %s
          AND placar_corinthians IS NOT NULL
          AND placar_corinthians_intervalo IS NOT NULL
        ORDER BY data_jogo DESC
        LIMIT %s
        """,
        (time_id, JANELA_MAXIMA_DE_JOGOS),
    )
    jogos = cur.fetchall()
    total = len(jogos)
    if total < JOGOS_MINIMOS_PARA_ANALISAR:
        return None, total

    marcou_nos_dois = 0
    for placar_final, placar_intervalo in jogos:
        gols_1t = placar_intervalo
        gols_2t = placar_final - placar_intervalo
        if gols_1t > 0 and gols_2t > 0:
            marcou_nos_dois += 1

    frequencia = round(100 * marcou_nos_dois / total, 2)
    return (total, marcou_nos_dois, frequencia), total


def salvar_padrao_marca_ambos_tempos(cur, resultado, time_id):
    jogos_analisados, jogos_que_marcou_nos_dois, frequencia = resultado
    cur.execute(
        """
        INSERT INTO padroes_marca_ambos_tempos (time_id, jogos_analisados, jogos_que_marcou_nos_dois, frequencia, atualizado_em)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (time_id) DO UPDATE SET
            jogos_analisados = EXCLUDED.jogos_analisados,
            jogos_que_marcou_nos_dois = EXCLUDED.jogos_que_marcou_nos_dois,
            frequencia = EXCLUDED.frequencia,
            atualizado_em = NOW()
        """,
        (time_id, jogos_analisados, jogos_que_marcou_nos_dois, frequencia),
    )
    print(f"  [marca ambos tempos] {jogos_que_marcou_nos_dois}/{jogos_analisados} jogos ({frequencia}%)")


def calcular_padrao_handicap(cur, time_id):
    """NOVO (Handicap Asiático - só linha de meio gol): frequência de
    "cobrir" cada linha candidata (LINHAS_HANDICAP), separado por
    mandante/visitante - precisa separar por lado porque a MESMA linha
    bruta (ex: -0.5, que vem sempre relativa ao mandante, convenção da
    OddsPapi) se aplica com sinal invertido dependendo de quem é o
    mandante de verdade nesse jogo.

    Cobrir a linha H com nosso time mandante: diferença de gols (nosso -
    adversário) > -H. Cobrir a linha H com nosso time visitante: diferença
    > H (sinal invertido) - mesma lógica documentada na conversa que
    desenhou esse mercado (17/08/2026)."""
    resultados = []
    for lado_bool, lado_nome in [(True, "mandante"), (False, "visitante")]:
        cur.execute(
            """
            SELECT placar_corinthians, placar_adversario
            FROM jogos
            WHERE nosso_time_id = %s AND mandante = %s
              AND placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL
            ORDER BY data_jogo DESC
            LIMIT %s
            """,
            (time_id, lado_bool, JANELA_MAXIMA_DE_JOGOS),
        )
        jogos = cur.fetchall()
        total = len(jogos)
        if total < JOGOS_MINIMOS_PARA_ANALISAR:
            continue

        diferencas = [placar_nosso - placar_adv for placar_nosso, placar_adv in jogos]
        media_diferenca = round(sum(diferencas) / total, 2)

        for linha in LINHAS_HANDICAP:
            limite = -linha if lado_bool else linha
            jogos_cobriu = sum(1 for d in diferencas if d > limite)
            frequencia = round(100 * jogos_cobriu / total, 2)
            resultados.append((lado_nome, linha, total, jogos_cobriu, frequencia, media_diferenca))

    return resultados


def salvar_padrao_handicap(cur, resultados, time_id):
    for lado, linha, jogos_analisados, jogos_cobriu, frequencia, media_diferenca in resultados:
        cur.execute(
            """
            INSERT INTO padroes_time_handicap
                (time_id, lado, linha, jogos_analisados, jogos_cobriu, frequencia, media_diferenca, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (time_id, lado, linha) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                jogos_cobriu = EXCLUDED.jogos_cobriu,
                frequencia = EXCLUDED.frequencia,
                media_diferenca = EXCLUDED.media_diferenca,
                atualizado_em = NOW()
            """,
            (time_id, lado, linha, jogos_analisados, jogos_cobriu, frequencia, media_diferenca),
        )
        print(f"  [{lado}] linha {linha:+.1f}: {jogos_cobriu}/{jogos_analisados} jogos cobriram ({frequencia}%)")


# NOVO (forma recente): quantos jogos definem "recente" - janela bem mais
# curta que o padrão geral (últimos 50), propositalmente, já que o objetivo
# aqui é capturar o momento ATUAL do time, não uma média de longo prazo.
JOGOS_FORMA_RECENTE = 5

# piso mínimo pra calcular - só protege contra o caso raro de ainda não
# existir jogo suficiente no banco (não deve acontecer na prática, já que
# o Corinthians sempre tem mais de 5 jogos concluídos no histórico)
JOGOS_MINIMOS_FORMA_RECENTE = 3


def calcular_forma_recente(cur, time_id):
    """NOVO: frequência de vitória/empate/derrota nos últimos
    JOGOS_FORMA_RECENTE jogos desse time, independente de mandante/
    visitante ou adversário - captura o "momento atual" do time, separado
    da média histórica geral.
    NOVO (multi-time): filtra por nosso_time_id."""
    cur.execute(
        """
        SELECT placar_corinthians, placar_adversario
        FROM jogos
        WHERE nosso_time_id = %s AND data_jogo < CURRENT_DATE
          AND placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL
        ORDER BY data_jogo DESC
        LIMIT %s
        """,
        (time_id, JOGOS_FORMA_RECENTE),
    )
    jogos = cur.fetchall()
    total = len(jogos)
    if total < JOGOS_MINIMOS_FORMA_RECENTE:
        return None, total

    contagem = {"vitoria": 0, "empate": 0, "derrota": 0}
    for placar_cor, placar_adv in jogos:
        if placar_cor > placar_adv:
            contagem["vitoria"] += 1
        elif placar_cor == placar_adv:
            contagem["empate"] += 1
        else:
            contagem["derrota"] += 1

    return contagem, total


def salvar_forma_recente(cur, contagem, total, time_id):
    for resultado, ocorrencias in contagem.items():
        frequencia = round(100 * ocorrencias / total, 2)
        cur.execute(
            """
            INSERT INTO padroes_forma_recente (time_id, janela, resultado, jogos_analisados, ocorrencias, frequencia, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (time_id, janela, resultado) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                ocorrencias = EXCLUDED.ocorrencias,
                frequencia = EXCLUDED.frequencia,
                atualizado_em = NOW()
            """,
            (time_id, JOGOS_FORMA_RECENTE, resultado, total, ocorrencias, frequencia),
        )
        print(f"  Últimos {total} jogos - {resultado}: {ocorrencias}/{total} ({frequencia}%)")


def calcular_padroes_arbitro(cur):
    """NOVO: para cada árbitro que já apitou algum jogo do Corinthians (e já
    tem resultado conhecido), calcula a média de cartões e faltas do jogo
    inteiro (ambos os times, não só o Corinthians) e a frequência de jogos
    com pelo menos 1 cartão vermelho. Não usa janela de 50 - usa todos os
    jogos disponíveis daquele árbitro, já que a amostra por árbitro é bem
    menor que a de jogador."""
    cur.execute(
        """
        SELECT DISTINCT arbitro FROM jogos
        WHERE arbitro IS NOT NULL AND data_jogo < CURRENT_DATE
        """
    )
    arbitros = [row[0] for row in cur.fetchall()]

    resultados = []

    for arbitro in arbitros:
        # CORRIGIDO: quando dois times rastreados jogam entre si, o jogo
        # gera 2 linhas em `jogos` (uma por perspectiva - arquitetura
        # multi-time). Sem esse DISTINCT ON, esse jogo era contado 2x pro
        # perfil do árbitro (2x nos jogos_analisados, 2x nos cartões/faltas
        # somados via jogo_id), inflando artificialmente a média. Agora
        # pega só 1 linha por jogo real (COALESCE cobre jogos antigos sem
        # fixture_id_api preenchido, tratando cada um deles como único).
        cur.execute(
            """
            SELECT DISTINCT ON (COALESCE(fixture_id_api, id)) id
            FROM jogos
            WHERE arbitro = %s AND data_jogo < CURRENT_DATE
            ORDER BY COALESCE(fixture_id_api, id), id
            """,
            (arbitro,),
        )
        jogo_ids = [row[0] for row in cur.fetchall()]

        jogos_analisados = len(jogo_ids)
        if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
            continue  # amostra pequena demais pra confiar no perfil desse árbitro ainda

        # cartões totais do jogo (os dois times, não só o Corinthians -
        # a tabela `cartoes` já guarda eventos de ambos os lados)
        cur.execute(
            "SELECT jogo_id, COUNT(*) FROM cartoes WHERE jogo_id = ANY(%s) GROUP BY jogo_id",
            (jogo_ids,),
        )
        cartoes_por_jogo = dict(cur.fetchall())
        total_cartoes = sum(cartoes_por_jogo.values())
        media_cartoes = round(total_cartoes / jogos_analisados, 2)

        # jogos com pelo menos 1 cartão vermelho
        cur.execute(
            "SELECT DISTINCT jogo_id FROM cartoes WHERE jogo_id = ANY(%s) AND cor = 'vermelho'",
            (jogo_ids,),
        )
        jogos_com_vermelho = len(cur.fetchall())
        frequencia_vermelho = round(100 * jogos_com_vermelho / jogos_analisados, 2)

        # faltas totais do jogo (soma dos dois lados, quando a estatística existe)
        cur.execute(
            "SELECT jogo_id, SUM(faltas) FROM estatisticas_jogo WHERE jogo_id = ANY(%s) AND faltas IS NOT NULL GROUP BY jogo_id",
            (jogo_ids,),
        )
        faltas_por_jogo = dict(cur.fetchall())
        media_faltas = None
        if faltas_por_jogo:
            media_faltas = round(sum(float(v) for v in faltas_por_jogo.values()) / len(faltas_por_jogo), 2)

        resultados.append((
            arbitro, jogos_analisados, media_cartoes, media_faltas,
            jogos_com_vermelho, frequencia_vermelho,
        ))

    return resultados


def salvar_padroes_arbitro(cur, resultados):
    for arbitro, jogos_analisados, media_cartoes, media_faltas, jogos_com_vermelho, frequencia_vermelho in resultados:
        cur.execute(
            """
            INSERT INTO padroes_arbitro (arbitro, jogos_analisados, media_cartoes, media_faltas,
                                          jogos_com_vermelho, frequencia_vermelho, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (arbitro) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                media_cartoes = EXCLUDED.media_cartoes,
                media_faltas = EXCLUDED.media_faltas,
                jogos_com_vermelho = EXCLUDED.jogos_com_vermelho,
                frequencia_vermelho = EXCLUDED.frequencia_vermelho,
                atualizado_em = NOW()
            """,
            (arbitro, jogos_analisados, media_cartoes, media_faltas, jogos_com_vermelho, frequencia_vermelho),
        )
        print(f"  {arbitro}: {jogos_analisados} jogo(s), média de {media_cartoes} cartões/jogo, "
              f"{frequencia_vermelho}% dos jogos com vermelho")


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        print("Atualizando quais jogadores estão ativos (apareceram nos "
              f"últimos {JANELA_ATIVIDADE_JOGADOR} jogos de cada time rastreado)...")
        total_ativos, total_inativos = calcular_jogadores_ativos(cur)
        conn.commit()
        print(f"Concluído! {total_ativos} jogador(es) ativo(s) agora, "
              f"{total_inativos} inativo(s).")

        # NOVO (multi-time): as três funções de padrão por JOGADOR (cartão,
        # falta/desarme/chute, impedimento) processam TODOS os jogadores de
        # TODOS os times rastreados numa passada só - cada jogador usa o
        # time_atual_id dele mesmo pra filtrar os jogos certos internamente,
        # então não precisam de loop de time aqui fora.
        print("\nCalculando padrões de cartão por jogador...")
        resultados_cartao = calcular_padroes_cartao(cur)

        if not resultados_cartao:
            print("Nenhum jogador com dados suficientes ainda "
                  f"(mínimo de {JOGOS_MINIMOS_PARA_ANALISAR} jogos analisados).")
        else:
            resultados_cartao.sort(key=lambda r: r[4], reverse=True)
            salvar_padroes(cur, resultados_cartao)
            conn.commit()
            print(f"Concluído! Padrões de cartão calculados para {len(resultados_cartao)} jogador(es).")

        print("\nCalculando padrões de linha por jogador (faltas, desarmes, chutes)...")
        for tipo, (coluna, linhas) in PADROES_LINHA_JOGADOR.items():
            resultados = calcular_padrao_linha_jogador(cur, coluna, linhas)
            if resultados:
                salvar_padrao_linha_jogador(cur, tipo, resultados)
                conn.commit()
            else:
                print(f"  {tipo}: nenhum jogador com dados suficientes ainda.")

        print("\nCalculando padrões de frequência por jogador (impedimentos)...")
        for tipo, coluna in PADROES_FREQUENCIA_JOGADOR.items():
            resultados = calcular_padrao_frequencia_jogador(cur, coluna)
            if resultados:
                salvar_padrao_frequencia_jogador(cur, tipo, resultados)
                conn.commit()
            else:
                print(f"  {tipo}: nenhum jogador com dados suficientes ainda.")

        # NOVO (multi-time): a partir daqui, os padrões são POR TIME - cada
        # time rastreado passa por essa parte separadamente, com sua
        # própria frequência calculada.
        times_rastreados = buscar_times_rastreados(cur)
        if not times_rastreados:
            print("\nNenhum time com rastreado=TRUE - pulando padrões de time.")
        for time_id, time_nome, time_api_football_id in times_rastreados:
            print(f"\n========== Padrões de time: {time_nome} ==========")

            # NOVO (25/08/2026): três recortes por time - 'geral' (o que já
            # existia), 'mandante' e 'visitante'. O recorte 'geral' continua
            # sendo calculado e gravado porque é o degrau de fallback da
            # escada em motor_recomendacoes.buscar_frequencia_escanteio_time,
            # usada quando o recorte específico não alcança o piso próprio
            # (times de histórico curto, como Chapecoense e Remo hoje).
            print("Calculando padrões de escanteio do time (geral + por mando)...")
            for lado_escanteio in ("geral", "mandante", "visitante"):
                resultados_escanteio, jogos_analisados = calcular_padroes_escanteio(
                    cur, time_id, lado=lado_escanteio
                )
                if not resultados_escanteio:
                    print(f"  [{lado_escanteio}] Dados insuficientes ainda para escanteio "
                          f"({jogos_analisados} jogos analisados, mínimo de "
                          f"{JOGOS_MINIMOS_PARA_ANALISAR}).")
                else:
                    salvar_padroes_escanteio(cur, resultados_escanteio, time_id, lado=lado_escanteio)
                    conn.commit()
                    print(f"  [{lado_escanteio}] Concluído! Padrões de escanteio calculados "
                          f"com base em {jogos_analisados} jogo(s).")

            print("Calculando padrões de escanteio TOTAL do jogo (mandante + visitante)...")
            resultados_escanteio_total, jogos_analisados_escanteio_total = calcular_padroes_escanteio_total(cur, time_id)

            if not resultados_escanteio_total:
                print(f"  Dados insuficientes ainda para escanteio total ({jogos_analisados_escanteio_total} jogos "
                      f"analisados, mínimo de {JOGOS_MINIMOS_PARA_ANALISAR}).")
            else:
                salvar_padroes_escanteio_total(cur, resultados_escanteio_total, time_id)
                conn.commit()
                print(f"  Concluído! Padrões de escanteio total calculados com base em "
                      f"{jogos_analisados_escanteio_total} jogo(s).")

            print("Calculando padrões de cartão TOTAL do jogo (mandante + visitante)...")
            resultados_cartao_total, jogos_analisados_cartao_total = calcular_padroes_cartao_total(cur, time_id)

            if not resultados_cartao_total:
                print(f"  Dados insuficientes ainda para cartão total ({jogos_analisados_cartao_total} jogos "
                      f"analisados, mínimo de {JOGOS_MINIMOS_PARA_ANALISAR}).")
            else:
                salvar_padroes_cartao_total(cur, resultados_cartao_total, time_id)
                conn.commit()
                print(f"  Concluído! Padrões de cartão total calculados com base em "
                      f"{jogos_analisados_cartao_total} jogo(s).")

            # NOVO (Mais/Menos gols e Equipe Marca): fonte é jogos.placar_*,
            # não estatisticas_jogo - mesmo padrão de escanteio/cartão
            # total, mas sem depender do endpoint de estatísticas.
            print("Calculando padrões de gols do time (nosso lado)...")
            resultados_gols_time, jogos_gols_time = calcular_padrao_gols_time(cur, time_id, LINHAS_GOLS_TIME)
            if not resultados_gols_time:
                print(f"  Dados insuficientes ainda para gols do time ({jogos_gols_time} jogos analisados, "
                      f"mínimo de {JOGOS_MINIMOS_PARA_ANALISAR}).")
            else:
                salvar_padrao_linha_time(cur, "gols", resultados_gols_time, time_id)
                conn.commit()

            print("Calculando padrões de gols TOTAL do jogo (mandante + visitante)...")
            resultados_gols_total, jogos_analisados_gols_total = calcular_padroes_gols_total(cur, time_id)
            if not resultados_gols_total:
                print(f"  Dados insuficientes ainda para gols total ({jogos_analisados_gols_total} jogos "
                      f"analisados, mínimo de {JOGOS_MINIMOS_PARA_ANALISAR}).")
            else:
                salvar_padroes_gols_total(cur, resultados_gols_total, time_id)
                conn.commit()
                print(f"  Concluído! Padrões de gols total calculados com base em "
                      f"{jogos_analisados_gols_total} jogo(s).")

            print("Calculando padrão de Equipe Marca (Sim/Não)...")
            resultado_marca, jogos_marca = calcular_padrao_equipe_marca(cur, time_id)
            if not resultado_marca:
                print(f"  Dados insuficientes ainda para Equipe Marca ({jogos_marca} jogos analisados, "
                      f"mínimo de {JOGOS_MINIMOS_PARA_ANALISAR}).")
            else:
                salvar_padrao_equipe_marca(cur, resultado_marca, time_id)
                conn.commit()

            # NOVO (estatísticas de time): faltas e chutes só do NOSSO lado
            # (não confundir com falta_total/chute_total, que somam os dois
            # times) - alimenta a página "Estatísticas de Times".
            print("Calculando padrões de faltas e chutes do time (nosso lado)...")
            for tipo, (coluna, linhas) in PADROES_LINHA_TIME_ESTATISTICA.items():
                resultados_linha_time, jogos_linha_time = calcular_padrao_linha_time_estatistica(
                    cur, time_id, coluna, linhas
                )
                if not resultados_linha_time:
                    print(f"  [{tipo}] Dados insuficientes ainda ({jogos_linha_time} jogos analisados, "
                          f"mínimo de {JOGOS_MINIMOS_PARA_ANALISAR}).")
                else:
                    salvar_padrao_linha_time(cur, tipo, resultados_linha_time, time_id)
                    conn.commit()

            print("Calculando padrões de cartão do time (nosso lado)...")
            resultados_cartao_time, jogos_cartao_time = calcular_padrao_cartao_time(cur, time_id, LINHAS_CARTAO_TIME)
            if not resultados_cartao_time:
                print(f"  Dados insuficientes ainda para cartão do time ({jogos_cartao_time} jogos analisados, "
                      f"mínimo de {JOGOS_MINIMOS_PARA_ANALISAR}).")
            else:
                salvar_padrao_linha_time(cur, "cartao", resultados_cartao_time, time_id)
                conn.commit()

            # NOVO (estatísticas de time - mais completas): chutes no gol,
            # impedimentos e desarmes do time, somando a estatística
            # individual de cada jogador em campo (ver docstring de
            # calcular_padrao_linha_time_soma_jogadores).
            print("Calculando padrões de chute no gol, impedimento e desarme do time...")
            for tipo, (coluna, linhas) in PADROES_LINHA_TIME_SOMA_JOGADOR.items():
                resultados_soma, jogos_soma = calcular_padrao_linha_time_soma_jogadores(cur, time_id, coluna, linhas)
                if not resultados_soma:
                    print(f"  [{tipo}] Dados insuficientes ainda ({jogos_soma} jogos analisados, "
                          f"mínimo de {JOGOS_MINIMOS_PARA_ANALISAR}).")
                else:
                    salvar_padrao_linha_time(cur, tipo, resultados_soma, time_id)
                    conn.commit()

            print("Calculando posse de bola média do time...")
            media_posse, jogos_posse = calcular_posse_time(cur, time_id)
            if media_posse is None:
                print(f"  Dados insuficientes ainda para posse de bola ({jogos_posse} jogos analisados, "
                      f"mínimo de {JOGOS_MINIMOS_PARA_ANALISAR}).")
            else:
                salvar_posse_time(cur, media_posse, jogos_posse, time_id)
                conn.commit()

            # NOVO (Fase B - padrão por rodada, só exibição): calcula o
            # comportamento desse time em cada rodada (juntando as
            # temporadas coletadas) e detecta automaticamente onde está a
            # maior mudança de padrão, pra cada mercado coberto.
            print("Calculando padrões por rodada (detecção automática de quebra)...")
            bruto_rodada, quebras_rodada = calcular_padroes_rodada_time(cur, time_id)
            if bruto_rodada:
                salvar_padroes_rodada(cur, bruto_rodada, quebras_rodada)
                conn.commit()
                if not quebras_rodada:
                    print("  Dado bruto salvo, mas nenhuma quebra detectada ainda "
                          "(rodadas insuficientes pra formar as janelas de comparação).")
            else:
                print("  Dados insuficientes ainda para padrão por rodada.")

            # NOVO (Fase C - comportamento por zona da tabela, só
            # exibição): depende da tabela reconstruída pela Fase A
            # (jogos_liga/tabela.py) - se ainda não rodou popular_tabela.py
            # nesse banco, essa parte só não encontra nada pra calcular
            # (calcular_tabela devolve lista vazia), sem quebrar o resto.
            print("Calculando padrões por zona da tabela (G4/meio/Z4)...")
            padroes_zona = calcular_padroes_zona_time(cur, time_id, time_api_football_id)
            if padroes_zona:
                salvar_padroes_zona(cur, padroes_zona)
                conn.commit()
            else:
                print("  Dados insuficientes ainda para padrão por zona "
                      "(ou jogos_liga/popular_tabela.py ainda não rodou nesse banco).")

            # NOVO (Correlação entre estatísticas - POR TIME, só visual):
            # a mesma pergunta da versão geral (calculada uma vez só, fora
            # desse loop), mas só com os jogos DESSE time - revela se ele
            # tem essa relação mais forte/fraca que o padrão da liga.
            print("Calculando correlação entre estatísticas (por time)...")
            correlacoes_time = calcular_correlacoes_time(cur, time_id)
            if correlacoes_time:
                salvar_correlacoes_time(cur, correlacoes_time)
                conn.commit()
            else:
                print(f"  Dados insuficientes ainda para correlação por time "
                      f"(mínimo de {JOGOS_MINIMOS_PARA_ANALISAR} jogos por grupo).")

            # NOVO (Correlação entre categorias - POR TIME, só visual):
            # isola a direção (nosso ataque -> defesa do rival), diferente
            # da versão geral que soma os dois lados juntos.
            print("Calculando correlação entre categorias (por time)...")
            correlacoes_categoria_time = calcular_correlacoes_categoria_time(cur, time_id)
            if correlacoes_categoria_time:
                salvar_correlacoes_categoria_time(cur, correlacoes_categoria_time)
                conn.commit()
            else:
                print(f"  Dados insuficientes ainda para correlação de categoria por time "
                      f"(mínimo de {JOGOS_MINIMOS_PARA_ANALISAR} jogos por grupo).")

            print("Calculando padrões de resultado final (vitória/empate/derrota)...")
            resultados_finais = calcular_padroes_resultado(cur, time_id)
            if resultados_finais:
                salvar_padroes_resultado(cur, resultados_finais, time_id)
                conn.commit()
            else:
                print("  Dados insuficientes ainda para resultado final.")

            # NOVO (Onda 2 - Dupla Chance/Ambas Marcam por tempo, Marca em
            # Ambos os Tempos): depende do placar de intervalo
            # (placar_corinthians_intervalo/placar_adversario_intervalo) -
            # jogos sem esse dado ainda (histórico antes do backfill, ou
            # jogo que a própria API-Football não tinha o intervalo) ficam
            # de fora da amostra automaticamente (ver WHERE das queries).
            print("Calculando padrões de Dupla Chance por tempo...")
            resultados_dupla_chance = calcular_padroes_dupla_chance_tempo(cur, time_id)
            if resultados_dupla_chance:
                salvar_padroes_dupla_chance_tempo(cur, resultados_dupla_chance, time_id)
                conn.commit()
            else:
                print(f"  Dados insuficientes ainda para Dupla Chance por tempo "
                      f"(mínimo de {JOGOS_MINIMOS_PARA_ANALISAR} jogos por lado, com placar de intervalo salvo).")

            print("Calculando padrões de Ambas Marcam por tempo...")
            resultados_ambas_tempo = calcular_padroes_ambas_marcam_tempo(cur, time_id)
            if resultados_ambas_tempo:
                salvar_padroes_ambas_marcam_tempo(cur, resultados_ambas_tempo, time_id)
                conn.commit()
            else:
                print(f"  Dados insuficientes ainda para Ambas Marcam por tempo "
                      f"(mínimo de {JOGOS_MINIMOS_PARA_ANALISAR} jogos por lado, com placar de intervalo salvo).")

            print("Calculando padrão de Marca em Ambos os Tempos...")
            resultado_marca_ambos, jogos_marca_ambos = calcular_padrao_marca_ambos_tempos(cur, time_id)
            if not resultado_marca_ambos:
                print(f"  Dados insuficientes ainda para Marca em Ambos os Tempos ({jogos_marca_ambos} jogos "
                      f"analisados, mínimo de {JOGOS_MINIMOS_PARA_ANALISAR}).")
            else:
                salvar_padrao_marca_ambos_tempos(cur, resultado_marca_ambos, time_id)
                conn.commit()

            print("Calculando padrão de Handicap Asiático (só linha de meio gol)...")
            resultados_handicap = calcular_padrao_handicap(cur, time_id)
            if resultados_handicap:
                salvar_padrao_handicap(cur, resultados_handicap, time_id)
                conn.commit()
            else:
                print(f"  Dados insuficientes ainda para Handicap Asiático "
                      f"(mínimo de {JOGOS_MINIMOS_PARA_ANALISAR} jogos por lado).")

            print(f"Calculando forma recente (últimos {JOGOS_FORMA_RECENTE} jogos)...")
            contagem_forma, jogos_analisados_forma = calcular_forma_recente(cur, time_id)
            if contagem_forma:
                salvar_forma_recente(cur, contagem_forma, jogos_analisados_forma, time_id)
                conn.commit()
            else:
                print(f"  Dados insuficientes ainda pra forma recente ({jogos_analisados_forma} jogos "
                      f"disponíveis, mínimo de {JOGOS_MINIMOS_FORMA_RECENTE}).")

            print("Calculando padrões de confronto direto (por adversário específico)...")
            total_confronto = calcular_padroes_confronto_direto(cur, time_id)
            conn.commit()
            if total_confronto:
                print(f"  Concluído! {total_confronto} padrão(ões) de confronto direto calculados.")
            else:
                print(f"  Nenhum adversário com pelo menos {JOGOS_MINIMOS_CONFRONTO} jogos analisados ainda.")

        # NOVO (estilo de time - Fase 1, só visual): calculado uma vez só,
        # DEPOIS do loop por time acima (precisa da média de TODOS os times
        # rastreados junto, pra calcular a média da liga) - diferente dos
        # padrões acima, que cada time calcula o próprio sozinho. Só
        # impedimento e cartão por enquanto. Resultado alimenta só a tela
        # do time (/time/<id>) - nenhuma recomendação/VE é afetada ainda.
        if times_rastreados:
            print("\nCalculando estilo de jogo por time (ofensivo/defensivo - impedimento e cartão)...")
            resultados_estilo = calcular_estilo_times(cur, times_rastreados)
            if resultados_estilo:
                salvar_estilo_times(cur, resultados_estilo)
                conn.commit()
                print(f"Concluído! {len(resultados_estilo)} combinação(ões) time/tipo/papel calculadas.")
            else:
                print(f"  Nenhum time com dados suficientes ainda (mínimo de {JOGOS_MINIMOS_PARA_ANALISAR} jogos).")

        # NOVO (Correlação entre estatísticas, só visual): calculado uma
        # vez só, DEPOIS do loop por time - é uma estatística da LIGA
        # inteira (não é "do Corinthians"), olhando o total de cada jogo
        # (mandante + visitante somados). Não entra em recomendação/VE
        # ainda.
        print("\nCalculando correlação entre estatísticas do mesmo jogo...")
        resultados_correlacao = calcular_correlacoes_estatisticas(cur)
        if resultados_correlacao:
            salvar_correlacoes_estatisticas(cur, resultados_correlacao)
            conn.commit()
            print(f"Concluído! {len(resultados_correlacao)} par(es) de estatísticas calculados.")
        else:
            print(f"  Dados insuficientes ainda para correlação entre estatísticas "
                  f"(mínimo de {JOGOS_MINIMOS_PARA_ANALISAR} jogos completos).")

        # NOVO (Correlação entre CATEGORIAS de jogador, só visual): mesma
        # ideia, mas cruzando estatística de um GRUPO de jogadores (por
        # posição) com a de outro grupo - ex: atacantes que sofrem falta
        # x cartão dos defensores. Também calculado uma vez só (dado da
        # liga inteira).
        print("\nCalculando correlação entre categorias de jogador...")
        resultados_categoria = calcular_correlacoes_categoria(cur)
        if resultados_categoria:
            salvar_correlacoes_categoria(cur, resultados_categoria)
            conn.commit()
            print(f"Concluído! {len(resultados_categoria)} par(es) de categoria calculados.")
        else:
            print(f"  Dados insuficientes ainda para correlação entre categorias "
                  f"(mínimo de {JOGOS_MINIMOS_PARA_ANALISAR} jogos completos).")

        # NOVO (Correlação POR JOGADOR NOMEADO, só visual): pra cada
        # atacante com amostra própria suficiente, calculado uma vez só
        # (percorre todos os jogadores qualificados, não é "por time" -
        # um jogador pertence a um time, mas a análise em si não precisa
        # repetir por time).
        print("\nCalculando correlação por jogador nomeado (atacante que sofre falta)...")
        resultados_jogador = calcular_correlacoes_jogador(cur)
        if resultados_jogador:
            salvar_correlacoes_jogador(cur, resultados_jogador)
            conn.commit()
            print(f"Concluído! {len(resultados_jogador)} jogador(es) calculados.")
        else:
            print(f"  Nenhum jogador qualificado ainda (mínimo de "
                  f"{JOGOS_MINIMOS_JOGADOR_CORRELACAO} jogos com faltas sofridas registradas).")

        # NOVO: perfil de árbitro continua sendo calculado uma vez só, pra
        # TODOS os jogos disponíveis - não é "do Corinthians" nem "do
        # Athletico Paranaense", é um dado sobre o próprio árbitro, então
        # não faz sentido repetir por time (a amostra dele só fica maior e
        # melhor incluindo jogos de todos os times rastreados juntos).
        print("\nCalculando perfil de árbitros (cartões e faltas por jogo apitado)...")
        resultados_arbitro = calcular_padroes_arbitro(cur)
        if resultados_arbitro:
            salvar_padroes_arbitro(cur, resultados_arbitro)
            conn.commit()
            print(f"Concluído! Perfil calculado para {len(resultados_arbitro)} árbitro(s).")
        else:
            print(f"  Nenhum árbitro com dados suficientes ainda (mínimo de {JOGOS_MINIMOS_PARA_ANALISAR} jogos apitados).")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
