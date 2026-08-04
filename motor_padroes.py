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


def calcular_padroes_escanteio(cur, time_id):
    """Olha os escanteios DESSE time (não do adversário) nos últimos jogos
    dele, e calcula a frequência de passar de cada linha testada (3.5, 4.5, ...).
    NOVO (multi-time): filtra por nosso_time_id - sem isso, misturaria
    escanteios de jogos de times rastreados diferentes."""
    cur.execute(
        """
        SELECT eg.escanteios
        FROM estatisticas_jogo eg
        JOIN jogos j ON j.id = eg.jogo_id
        WHERE j.nosso_time_id = %s
          AND ((j.mandante = TRUE AND eg.lado = 'mandante')
           OR (j.mandante = FALSE AND eg.lado = 'visitante'))
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


def salvar_padroes_escanteio(cur, resultados, time_id):
    for linha, jogos_analisados, jogos_acima, frequencia, media in resultados:
        cur.execute(
            """
            INSERT INTO padroes_time_escanteio (time_id, linha, jogos_analisados, jogos_acima_da_linha, frequencia, media, atualizado_em)
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
        print(f"  Mais de {linha} escanteios: {jogos_acima}/{jogos_analisados} jogos ({frequencia}%)")


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
    `cartoes` diretamente: o campo `lado` ali já é gravado relativo ao
    NOSSO time (ver popular_banco.py/salvar_eventos - `lado = "mandante"
    if ev["team"]["id"] == nosso_time_api_id else "visitante"`), não ao
    mandante/visitante real do jogo - por isso, diferente da função acima,
    não precisa de nenhuma tradução via jogos.mandante."""
    cur.execute(
        """
        SELECT contagem.total_cartoes
        FROM (
            SELECT j.id AS jogo_id, j.data_jogo,
                   COUNT(c.id) FILTER (WHERE c.lado = 'mandante') AS total_cartoes,
                   COUNT(DISTINCT eg.lado) AS lados
            FROM jogos j
            JOIN estatisticas_jogo eg ON eg.jogo_id = j.id
            LEFT JOIN cartoes c ON c.jogo_id = j.id
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
    """NOVO: cartões do jogo INTEIRO (mandante + visitante somados). Só
    considera jogos "completos" (com estatísticas dos dois lados já salvas
    em estatisticas_jogo) como critério de que o jogo já foi totalmente
    processado - sem isso, um jogo ainda não coletado entraria como "0
    cartões" por engano, em vez de simplesmente não entrar na amostra.
    NOVO (multi-time): filtra por nosso_time_id."""
    cur.execute(
        """
        SELECT contagem.total_cartoes
        FROM (
            SELECT j.id AS jogo_id, j.data_jogo, COUNT(c.id) AS total_cartoes,
                   COUNT(DISTINCT eg.lado) AS lados
            FROM jogos j
            JOIN estatisticas_jogo eg ON eg.jogo_id = j.id
            LEFT JOIN cartoes c ON c.jogo_id = j.id
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
    condicao = condicao_confronto(mandante_filtro)
    cur.execute(
        f"""
        SELECT contagem.total_cartoes
        FROM (
            SELECT j.id AS jogo_id, COUNT(c.id) AS total_cartoes,
                   COUNT(DISTINCT eg.lado) AS lados
            FROM jogos j
            JOIN estatisticas_jogo eg ON eg.jogo_id = j.id
            LEFT JOIN cartoes c ON c.jogo_id = j.id
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

            print("Calculando padrões de escanteio do time...")
            resultados_escanteio, jogos_analisados = calcular_padroes_escanteio(cur, time_id)

            if not resultados_escanteio:
                print(f"  Dados insuficientes ainda para escanteio ({jogos_analisados} jogos analisados, "
                      f"mínimo de {JOGOS_MINIMOS_PARA_ANALISAR}).")
            else:
                salvar_padroes_escanteio(cur, resultados_escanteio, time_id)
                conn.commit()
                print(f"  Concluído! Padrões de escanteio calculados com base em {jogos_analisados} jogo(s).")

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

            print("Calculando padrões de resultado final (vitória/empate/derrota)...")
            resultados_finais = calcular_padroes_resultado(cur, time_id)
            if resultados_finais:
                salvar_padroes_resultado(cur, resultados_finais, time_id)
                conn.commit()
            else:
                print("  Dados insuficientes ainda para resultado final.")

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
