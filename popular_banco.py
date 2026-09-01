
"""
Script que busca jogos, gols, cartões, substituições, estatísticas agregadas
do time (posse de bola, escanteios, faltas, passes, finalizações) e
estatísticas individuais por jogador (chutes, desarmes, faltas cometidas e
sofridas, impedimentos, nota etc.) do Corinthians na API-Football, e salva
tudo no banco Postgres (Railway).

MODO HISTÓRICO + ATUAL: busca todos os jogos das temporadas 2022 a 2026
(plano pago da API-Football libera dados atualizados até 2026). Pula o que
já está no banco - verifica eventos e cada tipo de estatística de forma
independente, então é seguro rodar esse script várias vezes, ele só
processa o que falta, sem duplicar nada.

ESCOPO: qualquer time com api_football_team_id preenchido na tabela `times`
(hoje: Corinthians e Athletico Paranaense) - basta adicionar um time novo
no banco pra esse script passar a coletar o histórico dele também, sem
precisar mexer em código.

NOVO: também salva o árbitro de cada jogo (campo "referee" da API), usado
depois pelo motor_padroes.py pra calcular o "perfil" de cada árbitro
(tendência a dar mais ou menos cartão). Testado: 114/114 jogos do
Corinthians em 2022-2024 vieram com esse campo preenchido.

NOVO (identidade de jogador): a tabela `jogadores` agora tem a coluna
`api_football_id`, que é o identificador único que a própria API-Football
usa pra cada jogador. Antes, jogadores eram identificados só pelo nome, o
que podia misturar jogadores homônimos de times diferentes (ex: dois
jogadores chamados "Gabriel" em times diferentes viravam uma única pessoa
no banco). Agora a função get_or_create_jogador procura primeiro pelo id;
se não achar, procura pelo nome (pra aproveitar jogadores já cadastrados
antes dessa mudança) e completa o id neles; só cria um registro novo se não
encontrar de nenhuma forma.

NOVO (escalação): também busca e salva a escalação de cada jogo (titulares e
reservas de cada time, endpoint fixtures/lineups), na tabela `escalacoes`.
Pra titulares que saíram durante o jogo, cruza com a tabela `substituicoes`
(já populada pelos eventos) pra saber o minuto exato da saída. Serve de base
pra, no futuro, considerar só jogos em que o jogador realmente esteve em
campo (e por quanto tempo) ao calcular os padrões históricos dele - hoje
ainda não filtra por isso, só coleta e guarda o dado.

O plano atual da API-Football tem um limite de 7.500 requisições por dia.
Cada jogo consome até 4 requisições (eventos + estatísticas do time +
estatísticas por jogador + escalação). Com esse volume, o histórico completo
(2022-2026) do Corinthians deve caber tranquilamente numa única execução,
mas o script continua seguro pra rodar em mais de um dia se precisar - ele
continua de onde parou.

NOVO (correção de performance - jogos futuros): antes, o script tentava
buscar eventos/estatísticas/escalação de TODO jogo do calendário da
temporada, incluindo os que ainda não aconteceram - como esses endpoints
sempre voltam vazios pra jogo futuro, isso nunca ficava "resolvido" e o
script pagava as 4 chamadas + ~28s de sleep por jogo futuro, TODA execução,
pra CADA time. Agora ele olha `fixture.status.short` da API-Football: só
busca o detalhe completo se o jogo estiver realmente finalizado (FT, AET ou
PEN). Jogo que ainda não rolou, foi adiado, cancelado ou está em andamento
só tem a linha em `jogos` criada/atualizada (rápido, sem chamada extra) e é
pulado. Isso é o que fazia a cadeia pesada durar cada vez mais ao adicionar
times - o tempo gasto agora escala com jogos que JÁ ACONTECERAM desde a
última execução, não com o calendário inteiro da temporada.

NOVO (01/09/2026 - Lacuna 2 da seção 27, "evento nunca é rebuscado"):
`falta_eventos` era `not jogo_ja_processado(...)` puro - "já tem QUALQUER
evento salvo" era tratado como definitivo pra sempre. Diagnóstico direto na
API-Football (script `diagnostico_cartoes_desaparecidos.py`, fora deste
arquivo) confirmou em 3 jogos reais que ISSO ESTAVA ERRADO: a resposta de
`/fixtures/events` muda depois do jogo acabar - não só completando cartões
que faltavam (ex: Bahia x Internacional só tinha 2 de 6 cartões no banco,
todos os 4 que faltavam são cartões normais de falta, sem nada de especial
no tipo), como corrigindo atribuição errada (São Paulo x Bragantino tinha
um cartão de "Pedro Henrique" que não existe mais na resposta atual da API,
e outro cartão com o minuto errado). Isso aconteceu em jogos processados
DIAS antes do diagnóstico - bem mais que os 6h usados pra estatística.
Por isso os eventos agora usam uma janela PRÓPRIA e mais generosa
(JANELA_ESPERA_EVENTOS_HORAS), com o mesmo padrão de "rebusca e substitui"
já usado pra estatística. Isso é só a Camada 1 do desenho de 3 camadas já
proposto na seção 27 - ainda é uma janela fixa por relógio, não por
estabilidade (Camada 2, ainda em aberto: rebuscar até 2 coletas seguidas
darem o mesmo resultado, em vez de confiar num prazo fixo).

Variáveis de ambiente necessárias (configuradas no Railway, aba "Variables"):
  - API_FOOTBALL_KEY   -> sua chave da API-Football (api-sports.io)
  - DATABASE_URL       -> a URL de conexão do Postgres (o Railway já cria essa
                           automaticamente quando você conecta os dois serviços)
"""

import os
import re
import time
import requests
import psycopg2

# ---------- Configurações ----------
API_KEY = os.environ["API_FOOTBALL_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

LEAGUE_ID = 71                    # Brasileirão Série A
TEMPORADAS = [2022, 2023, 2024, 2025, 2026]  # histórico + temporada atual (plano pago libera 2025/2026)
LIMITE_REQUISICOES_DIA = 7000      # margem de segurança abaixo do limite de 7.500/dia do plano novo


def _numero_rodada(rodada):
    """NOVO (Fase B): extrai o número de dentro de "Regular Season - 20"
    -> 20. Mesma lógica já usada em popular_tabela.py/limpar_historico.py -
    mantida duplicada aqui de propósito (utilitário pequeno e autocontido,
    baixo risco de divergência - ver combinacoes.py pra contraste com
    lógica de negócio complexa, essa sim compartilhada)."""
    m = re.search(r"(\d+)", rodada or "")
    return int(m.group(1)) if m else None

# NOVO (correção de performance): status da API-Football (campo
# fixture.status.short) que indicam jogo REALMENTE finalizado, com
# estatísticas/eventos disponíveis pra buscar. Qualquer outro status
# (NS = not started, TBD, 1H, HT, 2H, ET, P, LIVE, PST = postponed,
# CANC = cancelado, ABD = abandonado etc.) significa que não existe
# dado real pra buscar ainda - antes o script tentava os 4 endpoints
# (eventos, estatísticas, estatísticas de jogador, escalação) pra TODO
# jogo futuro do calendário inteiro da temporada, em todo time, todo
# dia, sempre voltando vazio e ainda assim pagando os 4x 7s de sleep
# (~28s por jogo futuro) - com N times isso virava N x 38 jogos x 28s
# de tempo jogado fora, e crescia sem limite ao adicionar mais times.
STATUS_JOGO_FINALIZADO = {"FT", "AET", "PEN"}

# NOVO (correção estrutural do bug de estatística coletada no meio do jogo -
# decisão de arquitetura de 17/08/2026, "Opção 1"): enquanto o jogo tiver
# terminado há menos desse tanto de horas, a estatística NÃO é tratada como
# definitiva - mesmo que já exista uma linha salva, o script busca de novo
# a cada execução do cron. O /fixtures/statistics da API-Football pode
# ficar atrasado em relação ao status do fixture (o jogo já aparece "FT"
# antes desse endpoint terminar de agregar os números finais) - já
# confirmado em 2 jogos reais (Bahia x Corinthians 26/07 e Athletico-PR x
# Bragantino 15/08). O jogo mais longo dura ~2h30 (90min + intervalo +
# acréscimos); 6h dá uma folga generosa pro backend da própria API-Football
# terminar de fechar o dado do lado deles.
JANELA_ESPERA_ESTATISTICA_HORAS = 6

# NOVO (01/09/2026): janela própria pra EVENTOS (gols/cartões/substituições),
# separada da de estatística. Tem que ser bem mais generosa - o diagnóstico
# que motivou essa mudança encontrou jogo com evento sendo corrigido pela
# API-Football dias depois do fim, não horas. 72h (3 dias) é conservador o
# bastante pra pegar a maioria dos casos observados sem rebuscar pra sempre;
# fica documentado que ainda não é uma garantia (ver Camada 2 acima).
JANELA_ESPERA_EVENTOS_HORAS = 72

# NOVO (multi-time): os times rastreados vêm da própria tabela `times`
# (marcados com `rastreado = TRUE`) - adicionar um time novo é: preencher
# os IDs dele + marcar rastreado=TRUE, e esse script já passa a coletar o
# histórico dele também, sem precisar mexer em código.
# IMPORTANTE: NÃO basta ter os IDs preenchidos - um time pode ter
# api_football_team_id preenchido só por ter aparecido como adversário
# algum dia (isso é automático), sem nunca ter sido escolhido pra ser
# rastreado de verdade. `rastreado` é a marcação explícita que evita isso.
def buscar_times_rastreados(cur):
    cur.execute(
        "SELECT id, nome, api_football_team_id FROM times "
        "WHERE rastreado = TRUE AND api_football_team_id IS NOT NULL ORDER BY nome"
    )
    return cur.fetchall()

requisicoes_usadas = 0
_cursor_para_contador = None  # referência ao cursor do banco, usada só pelo controle de limite


def carregar_requisicoes_usadas_hoje(cur):
    """Lê do banco quantas requisições já foram usadas HOJE, considerando
    também execuções anteriores do script no mesmo dia."""
    cur.execute(
        "INSERT INTO controle_api_uso (dia, requisicoes) VALUES (CURRENT_DATE, 0) "
        "ON CONFLICT (dia) DO NOTHING"
    )
    cur.execute("SELECT requisicoes FROM controle_api_uso WHERE dia = CURRENT_DATE")
    return cur.fetchone()[0]


def persistir_uma_requisicao(cur):
    """Registra no banco que mais uma requisição foi usada hoje, para que
    outras execuções do script no mesmo dia saibam disso."""
    cur.execute(
        "UPDATE controle_api_uso SET requisicoes = requisicoes + 1 WHERE dia = CURRENT_DATE"
    )
    cur.connection.commit()


class LimiteDiarioAtingido(Exception):
    """Levantada quando chegamos perto do limite diário de requisições da API."""
    pass


def chamar_api(endpoint, params, tentativas=3):
    """Faz uma chamada à API contando requisições. Se bater no limite por minuto
    (erro 429), espera um pouco e tenta de novo automaticamente."""
    global requisicoes_usadas
    if requisicoes_usadas >= LIMITE_REQUISICOES_DIA:
        raise LimiteDiarioAtingido(
            f"Limite de segurança de {LIMITE_REQUISICOES_DIA} requisições atingido hoje "
            "(somando todas as execuções do dia). O script vai continuar de onde parou "
            "na próxima execução automática, amanhã."
        )

    for tentativa in range(1, tentativas + 1):
        resp = requests.get(f"{API_BASE}/{endpoint}", headers=HEADERS, params=params)
        requisicoes_usadas += 1
        if _cursor_para_contador is not None:
            persistir_uma_requisicao(_cursor_para_contador)

        if resp.status_code == 429:
            espera = 20 * tentativa
            print(f"  Limite por minuto atingido, esperando {espera}s antes de tentar de novo...")
            time.sleep(espera)
            continue

        resp.raise_for_status()
        return resp.json()

    raise RuntimeError(f"Falhou após {tentativas} tentativas por causa do erro 429 (limite por minuto).")


def buscar_jogos(temporada, team_api_id):
    """Busca os jogos desse time numa temporada específica."""
    dados = chamar_api("fixtures", {"league": LEAGUE_ID, "season": temporada, "team": team_api_id})

    if dados.get("errors"):
        print(f"  Aviso da API para temporada {temporada}: {dados['errors']}")
        return []

    jogos = dados["response"]
    print(f"Temporada {temporada}: {len(jogos)} jogos encontrados.")
    return jogos


def jogo_ja_processado(cur, fixture_id):
    """Verifica se esse jogo já tem eventos salvos (pra não buscar de novo)."""
    cur.execute(
        "SELECT 1 FROM gols WHERE jogo_id = %s "
        "UNION SELECT 1 FROM cartoes WHERE jogo_id = %s "
        "UNION SELECT 1 FROM substituicoes WHERE jogo_id = %s LIMIT 1",
        (fixture_id, fixture_id, fixture_id),
    )
    return cur.fetchone() is not None


def jogo_tem_estatisticas(cur, fixture_id):
    """Verifica se esse jogo já tem as estatísticas agregadas salvas."""
    cur.execute("SELECT 1 FROM estatisticas_jogo WHERE jogo_id = %s LIMIT 1", (fixture_id,))
    return cur.fetchone() is not None


def jogo_tem_estatisticas_jogador(cur, fixture_id):
    """Verifica se esse jogo já tem as estatísticas individuais por jogador salvas."""
    cur.execute("SELECT 1 FROM jogador_estatisticas_jogo WHERE jogo_id = %s LIMIT 1", (fixture_id,))
    return cur.fetchone() is not None


def dentro_da_janela_de_espera(cur, jogo_id, horas):
    """NOVO (generalizada em 01/09/2026 - antes só existia pra estatística,
    com o nome `dentro_da_janela_de_espera_estatistica`; agora recebe `horas`
    porque eventos e estatística usam janelas diferentes, ver
    JANELA_ESPERA_ESTATISTICA_HORAS vs JANELA_ESPERA_EVENTOS_HORAS).

    True se o jogo terminou há menos de `horas` - nesse caso, já ter dado
    salva NÃO é motivo suficiente pra pular a busca (ver uso no loop
    principal). Usa COALESCE com `data_jogo` como fallback pro caso raro de
    uma linha antiga sem `datahora_jogo` preenchido (campo adicionado
    depois)."""
    cur.execute(
        "SELECT (NOW() - COALESCE(datahora_jogo, data_jogo::timestamp)) < INTERVAL '%s hours' "
        "FROM jogos WHERE id = %s",
        (horas, jogo_id),
    )
    row = cur.fetchone()
    return bool(row[0]) if row else False


def jogo_tem_escalacao(cur, fixture_id):
    """NOVO: verifica se esse jogo já tem a escalação (titulares/reservas) salva."""
    cur.execute("SELECT 1 FROM escalacoes WHERE jogo_id = %s LIMIT 1", (fixture_id,))
    return cur.fetchone() is not None


def buscar_eventos(fixture_id):
    """Busca gols, cartões e substituições de um jogo específico."""
    dados = chamar_api("fixtures/events", {"fixture": fixture_id})
    return dados["response"]


def buscar_estatisticas(fixture_id):
    """Busca as estatísticas agregadas do jogo (posse, escanteios, faltas etc.),
    uma entrada por lado (mandante/visitante)."""
    dados = chamar_api("fixtures/statistics", {"fixture": fixture_id})
    return dados["response"]


def buscar_estatisticas_jogadores(fixture_id):
    """Busca as estatísticas individuais de cada jogador que entrou em campo
    (faltas cometidas/sofridas, chutes, desarmes, impedimentos etc.)."""
    dados = chamar_api("fixtures/players", {"fixture": fixture_id})
    return dados["response"]


def buscar_escalacao(fixture_id):
    """NOVO: busca a escalação do jogo (titulares e reservas de cada time)."""
    dados = chamar_api("fixtures/lineups", {"fixture": fixture_id})
    return dados["response"]


def get_or_create_jogador(cur, api_football_id, nome):
    """Garante que o jogador existe na tabela `jogadores`, retorna o id.

    Usa o api_football_id (identificador único da própria API-Football) como
    chave principal de identidade, pra evitar misturar jogadores homônimos de
    times diferentes (ex: dois jogadores chamados "Gabriel" em clubes
    diferentes). A checagem acontece em camadas:

      1. Procura pelo api_football_id - se achar, é o jogador certo, sem
         ambiguidade nenhuma.
      2. Se não achar pelo id, procura pelo nome (cobre jogadores que já
         existiam no banco antes dessa mudança, cadastrados só com nome).
         Se encontrar um jogador com esse nome que ainda não tem id salvo,
         aproveita e completa o registro com o id agora.
      3. Se não encontrar de nenhuma forma, cria um jogador novo já com
         nome + id.

    Se a API não mandar o id (caso raro, dados incompletos), cai de volta
    pro comportamento antigo: busca/cria só pelo nome.
    """
    if api_football_id is not None:
        cur.execute("SELECT id FROM jogadores WHERE api_football_id = %s", (api_football_id,))
        row = cur.fetchone()
        if row:
            return row[0]

        cur.execute("SELECT id, api_football_id FROM jogadores WHERE nome = %s", (nome,))
        row = cur.fetchone()
        if row:
            jogador_id, id_existente = row
            if id_existente is None:
                cur.execute(
                    "UPDATE jogadores SET api_football_id = %s WHERE id = %s",
                    (api_football_id, jogador_id),
                )
            return jogador_id

        cur.execute(
            "INSERT INTO jogadores (nome, api_football_id) VALUES (%s, %s) RETURNING id",
            (nome, api_football_id),
        )
        return cur.fetchone()[0]

    # sem id vindo da API (não deveria acontecer normalmente, mas por segurança
    # não trava o script - cai pro comportamento antigo, só por nome)
    cur.execute("SELECT id FROM jogadores WHERE nome = %s", (nome,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO jogadores (nome) VALUES (%s) RETURNING id", (nome,))
    return cur.fetchone()[0]


def salvar_estatisticas(cur, jogo_id, estatisticas, home_team_id):
    """Salva as estatísticas agregadas (por lado) na tabela `estatisticas_jogo`."""
    salvos = 0

    for bloco in estatisticas:
        team_id = bloco["team"]["id"]
        lado = "mandante" if team_id == home_team_id else "visitante"

        valores = {item["type"]: item["value"] for item in bloco["statistics"]}

        def numero(chave):
            v = valores.get(chave)
            if v is None:
                return None
            if isinstance(v, str) and v.endswith("%"):
                v = v.replace("%", "")
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        cur.execute(
            """INSERT INTO estatisticas_jogo (jogo_id, lado, posse_de_bola, escanteios,
                                                faltas, passes, finalizacoes, desarmes)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                jogo_id,
                lado,
                numero("Ball Possession"),
                numero("Corner Kicks"),
                numero("Fouls"),
                numero("Total passes"),
                numero("Total Shots"),
                None,  # a API não fornece desarmes (tackles) nesse endpoint
            ),
        )
        salvos += 1

    return salvos


def salvar_estatisticas_jogadores(cur, jogo_id, dados_jogadores, home_team_id):
    """Salva uma linha por jogador que entrou em campo, com suas estatísticas
    individuais do jogo (faltas, chutes, desarmes, impedimentos etc.)."""
    salvos = 0

    for bloco_time in dados_jogadores:
        team_id = bloco_time["team"]["id"]
        lado = "mandante" if team_id == home_team_id else "visitante"

        for bloco_jogador in bloco_time["players"]:
            nome = bloco_jogador["player"]["name"]
            if not nome:
                continue

            api_id = bloco_jogador["player"].get("id")

            stats_lista = bloco_jogador.get("statistics") or []
            if not stats_lista:
                continue
            s = stats_lista[0]  # um jogador normalmente tem só um bloco por jogo

            minutos = (s.get("games") or {}).get("minutes")
            if minutos is None:
                continue  # jogador nem entrou em campo, não vale a pena salvar

            jogador_id = get_or_create_jogador(cur, api_id, nome)

            games = s.get("games") or {}
            shots = s.get("shots") or {}
            goals = s.get("goals") or {}
            passes = s.get("passes") or {}
            tackles = s.get("tackles") or {}
            duels = s.get("duels") or {}
            dribbles = s.get("dribbles") or {}
            fouls = s.get("fouls") or {}
            cards = s.get("cards") or {}
            penalty = s.get("penalty") or {}

            cur.execute(
                """INSERT INTO jogador_estatisticas_jogo (
                       jogo_id, jogador_id, lado, minutos, posicao, nota,
                       chutes, chutes_no_gol, gols, assistencias,
                       passes, passes_certos, desarmes, interceptacoes,
                       duelos_total, duelos_vencidos, dribles_tentados, dribles_sucesso,
                       faltas_cometidas, faltas_sofridas, impedimentos,
                       cartao_amarelo, cartao_vermelho,
                       penalti_marcado, penalti_perdido, penalti_sofrido
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                             %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    jogo_id, jogador_id, lado, minutos,
                    games.get("position"),
                    games.get("rating"),
                    shots.get("total"), shots.get("on"),
                    goals.get("total"), goals.get("assists"),
                    passes.get("total"), passes.get("key"),
                    tackles.get("total"), tackles.get("interceptions"),
                    duels.get("total"), duels.get("won"),
                    dribbles.get("attempts"), dribbles.get("success"),
                    fouls.get("committed"), fouls.get("drawn"),
                    s.get("offsides"),
                    cards.get("yellow"), cards.get("red"),
                    penalty.get("scored"), penalty.get("missed"), penalty.get("won"),
                ),
            )
            salvos += 1

    return salvos


def get_or_create_time(cur, api_football_team_id, nome):
    """Garante que o time existe na tabela `times`, retorna o id.

    Segue o mesmo padrão de identidade por id da API já usado em
    get_or_create_jogador: procura primeiro pelo api_football_team_id (evita
    duplicar/confundir times por nome); se não achar, procura pelo nome (pra
    aproveitar times já cadastrados, ex: pelo backfill de migração ou por
    atualizar_odds.py) e completa o id neles; só cria um registro novo se não
    encontrar de nenhuma forma."""
    if api_football_team_id is not None:
        cur.execute("SELECT id FROM times WHERE api_football_team_id = %s", (api_football_team_id,))
        row = cur.fetchone()
        if row:
            return row[0]

        cur.execute("SELECT id, api_football_team_id FROM times WHERE nome = %s", (nome,))
        row = cur.fetchone()
        if row:
            time_id, id_existente = row
            if id_existente is None:
                cur.execute(
                    "UPDATE times SET api_football_team_id = %s WHERE id = %s",
                    (api_football_team_id, time_id),
                )
            return time_id

        cur.execute(
            "INSERT INTO times (nome, api_football_team_id) VALUES (%s, %s) RETURNING id",
            (nome, api_football_team_id),
        )
        return cur.fetchone()[0]

    cur.execute("SELECT id FROM times WHERE nome = %s", (nome,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO times (nome) VALUES (%s) RETURNING id", (nome,))
    return cur.fetchone()[0]


def get_or_create_jogo(cur, fixture, nosso_time_id, nosso_time_api_id):
    """Garante que o jogo existe na tabela `jogos`, retorna o id.
    NOVO: também salva o árbitro (campo "referee" da API), tanto na criação
    quanto num backfill pra jogos que já existiam no banco sem esse dado.
    NOVO (times): também preenche mandante_id/visitante_id, referenciando a
    tabela `times` (em vez de só o texto solto em `adversario`) - com o
    mesmo backfill automático pra jogos que já existiam sem esse dado.

    NOVO (multi-time): a busca agora é por (fixture_id_api, nosso_time_id),
    não mais só por `id` - o mesmo jogo real da API-Football pode ter até
    duas linhas na tabela `jogos`, uma pra cada time rastreado, se os dois
    times rastreados jogarem entre si (ex: Corinthians x Athletico
    Paranaense, se os dois estiverem sendo rastreados ao mesmo tempo).
    `id` continua existindo como antes (só uma chave interna) - na maioria
    dos casos ele ainda é igual ao fixture_id_api, mas se esse número já
    estiver em uso pela visão de OUTRO time rastreado no mesmo jogo real, a
    inserção cai pra um id automático (savepoint de segurança, mesmo padrão
    já usado no atualizar_odds.py pra colisão de id).

    NOVO: também salva a rodada (fixture["league"]["round"], ex: "Regular
    Season - 20") - a API já manda esse dado em toda consulta, só nunca
    tinha sido salvo. Usado pela política de retenção do recurso de
    Múltiplas em Destaque (mantém detalhe completo só nas 2 rodadas mais
    recentes). Mesmo padrão de backfill dos outros campos - jogo que já
    existia sem rodada salva é completado agora."""
    fixture_id = fixture["fixture"]["id"]
    arbitro = fixture["fixture"].get("referee")  # pode vir None em alguns casos
    rodada = fixture.get("league", {}).get("round")  # NOVO
    rodada_numero = _numero_rodada(rodada)  # NOVO (Fase B): número extraído, pra agrupar por rodada sem reparsear toda vez

    mandante_nome = fixture["teams"]["home"]["name"]
    visitante_nome = fixture["teams"]["away"]["name"]
    mandante_api_id = fixture["teams"]["home"]["id"]
    visitante_api_id = fixture["teams"]["away"]["id"]

    cur.execute(
        "SELECT id, arbitro, mandante_id, visitante_id, datahora_jogo, "
        "placar_corinthians, placar_adversario, rodada, rodada_numero, "
        "placar_corinthians_intervalo, placar_adversario_intervalo FROM jogos "
        "WHERE fixture_id_api = %s AND nosso_time_id = %s",
        (fixture_id, nosso_time_id),
    )
    row = cur.fetchone()
    if row:
        (jogo_id, arbitro_salvo, mandante_id_salvo, visitante_id_salvo, datahora_salva,
         placar_cor_salvo, placar_adv_salvo, rodada_salva, rodada_numero_salva,
         placar_cor_intervalo_salvo, placar_adv_intervalo_salvo) = row
        # backfill: jogo já existia (de antes dessa funcionalidade) mas
        # está sem árbitro salvo, e agora a API nos deu esse dado - atualiza.
        if arbitro_salvo is None and arbitro:
            cur.execute("UPDATE jogos SET arbitro = %s WHERE id = %s", (arbitro, jogo_id))

        # NOVO: backfill de rodada (jogo já existia de antes dessa coluna existir)
        if rodada_salva is None and rodada:
            cur.execute("UPDATE jogos SET rodada = %s WHERE id = %s", (rodada, jogo_id))

        # NOVO (Fase B): backfill de rodada_numero
        if rodada_numero_salva is None and rodada_numero is not None:
            cur.execute("UPDATE jogos SET rodada_numero = %s WHERE id = %s", (rodada_numero, jogo_id))

        # backfill: jogo já existia de antes da tabela `times` existir -
        # completa mandante_id/visitante_id agora.
        if mandante_id_salvo is None or visitante_id_salvo is None:
            mandante_id = get_or_create_time(cur, mandante_api_id, mandante_nome)
            visitante_id = get_or_create_time(cur, visitante_api_id, visitante_nome)
            cur.execute(
                "UPDATE jogos SET mandante_id = %s, visitante_id = %s WHERE id = %s",
                (mandante_id, visitante_id, jogo_id),
            )

        # NOVO: backfill de datahora_jogo (jogo criado antes dessa coluna existir)
        if datahora_salva is None:
            cur.execute(
                "UPDATE jogos SET datahora_jogo = %s WHERE id = %s",
                (fixture["fixture"]["date"], jogo_id),
            )

        # NOVO: backfill de placar - esse é o bug real que resolvemos agora.
        # Jogo criado antes de acontecer (pelo atualizar_odds.py, sem placar
        # nenhum ainda) nunca tinha o placar preenchido depois, mesmo esse
        # script (popular_banco.py) já tendo o resultado real disponível -
        # fazia o mercado de "Resultado Final" ficar pendente pra sempre,
        # mesmo com o jogo já concluído há dias.
        if placar_cor_salvo is None or placar_adv_salvo is None:
            gol_home = fixture["goals"]["home"]
            gol_away = fixture["goals"]["away"]
            if gol_home is not None and gol_away is not None:
                eh_mandante_backfill = mandante_api_id == nosso_time_api_id
                novo_placar_cor = gol_home if eh_mandante_backfill else gol_away
                novo_placar_adv = gol_away if eh_mandante_backfill else gol_home
                cur.execute(
                    "UPDATE jogos SET placar_corinthians = %s, placar_adversario = %s WHERE id = %s",
                    (novo_placar_cor, novo_placar_adv, jogo_id),
                )

        # NOVO (Onda 2 - Dupla Chance/Ambas Marcam por tempo, Marca em
        # Ambos os Tempos): mesmo backfill do placar final acima, mas pro
        # placar do INTERVALO (fixture["score"]["halftime"]) - campo que a
        # API-Football já manda de graça na mesma resposta, só nunca tinha
        # sido extraído. Sem esse dado não dá pra saber o resultado do 1º/
        # 2º tempo isoladamente (só o placar final).
        if placar_cor_intervalo_salvo is None or placar_adv_intervalo_salvo is None:
            gol_home_intervalo = fixture.get("score", {}).get("halftime", {}).get("home")
            gol_away_intervalo = fixture.get("score", {}).get("halftime", {}).get("away")
            if gol_home_intervalo is not None and gol_away_intervalo is not None:
                eh_mandante_backfill = mandante_api_id == nosso_time_api_id
                novo_placar_cor_intervalo = gol_home_intervalo if eh_mandante_backfill else gol_away_intervalo
                novo_placar_adv_intervalo = gol_away_intervalo if eh_mandante_backfill else gol_home_intervalo
                cur.execute(
                    "UPDATE jogos SET placar_corinthians_intervalo = %s, placar_adversario_intervalo = %s WHERE id = %s",
                    (novo_placar_cor_intervalo, novo_placar_adv_intervalo, jogo_id),
                )
        return jogo_id

    data_jogo = fixture["fixture"]["date"][:10]
    datahora_jogo = fixture["fixture"]["date"]  # NOVO: timestamp completo, não só a data
    eh_mandante = mandante_api_id == nosso_time_api_id
    adversario = visitante_nome if eh_mandante else mandante_nome
    placar_corinthians = (
        fixture["goals"]["home"] if eh_mandante else fixture["goals"]["away"]
    )
    placar_adversario = (
        fixture["goals"]["away"] if eh_mandante else fixture["goals"]["home"]
    )
    # NOVO (Onda 2): placar do intervalo, mesma fonte (fixture["score"]
    # ["halftime"]) e mesma tradução mandante/visitante -> nosso time/
    # adversário que o placar final já usa acima.
    gol_home_intervalo = fixture.get("score", {}).get("halftime", {}).get("home")
    gol_away_intervalo = fixture.get("score", {}).get("halftime", {}).get("away")
    placar_corinthians_intervalo = (
        gol_home_intervalo if eh_mandante else gol_away_intervalo
    )
    placar_adversario_intervalo = (
        gol_away_intervalo if eh_mandante else gol_home_intervalo
    )

    mandante_id = get_or_create_time(cur, mandante_api_id, mandante_nome)
    visitante_id = get_or_create_time(cur, visitante_api_id, visitante_nome)

    # NOVO (multi-time): tenta usar o próprio fixture_id como `id` (igual
    # sempre foi) - só cai pro id automático se esse número já estiver em
    # uso pela visão de OUTRO time rasteado nesse mesmo jogo real.
    cur.execute("SAVEPOINT antes_de_inserir_jogo")
    try:
        cur.execute(
            """
            INSERT INTO jogos (id, fixture_id_api, nosso_time_id, data_jogo, datahora_jogo,
                                adversario, mandante, competicao,
                                placar_corinthians, placar_adversario, arbitro,
                                mandante_id, visitante_id, rodada, rodada_numero,
                                placar_corinthians_intervalo, placar_adversario_intervalo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                fixture_id, fixture_id, nosso_time_id,
                data_jogo, datahora_jogo, adversario, eh_mandante, "Brasileirão Série A",
                placar_corinthians, placar_adversario, arbitro,
                mandante_id, visitante_id, rodada, rodada_numero,
                placar_corinthians_intervalo, placar_adversario_intervalo,
            ),
        )
        return fixture_id
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT antes_de_inserir_jogo")
        cur.execute(
            """
            INSERT INTO jogos (fixture_id_api, nosso_time_id, data_jogo, datahora_jogo,
                                adversario, mandante, competicao,
                                placar_corinthians, placar_adversario, arbitro,
                                mandante_id, visitante_id, rodada, rodada_numero,
                                placar_corinthians_intervalo, placar_adversario_intervalo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (
                fixture_id, nosso_time_id,
                data_jogo, datahora_jogo, adversario, eh_mandante, "Brasileirão Série A",
                placar_corinthians, placar_adversario, arbitro,
                mandante_id, visitante_id, rodada, rodada_numero,
                placar_corinthians_intervalo, placar_adversario_intervalo,
            ),
        )
        return cur.fetchone()[0]


def salvar_eventos(cur, jogo_id, eventos, nosso_time_api_id):
    """Classifica cada evento (gol / cartão / substituição) e salva na tabela certa."""
    contagem = {"gols": 0, "cartoes": 0, "substituicoes": 0, "ignorados": 0}

    for ev in eventos:
        tipo = ev["type"]  # "Goal", "Card", "subst" (varia conforme a API)
        minuto = ev["time"]["elapsed"]
        lado = "mandante" if ev["team"]["id"] == nosso_time_api_id else "visitante"
        jogador_nome = ev["player"]["name"] if ev["player"]["name"] else None
        jogador_api_id = ev["player"].get("id") if ev.get("player") else None

        if not jogador_nome:
            contagem["ignorados"] += 1
            continue

        jogador_id = get_or_create_jogador(cur, jogador_api_id, jogador_nome)
        periodo = "1_tempo" if minuto <= 45 else "2_tempo"

        if tipo == "Goal":
            cur.execute(
                """INSERT INTO gols (jogo_id, jogador_id, lado, minuto, periodo,
                                      penalti, gol_contra)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    jogo_id, jogador_id, lado, minuto, periodo,
                    ev.get("detail") == "Penalty",
                    ev.get("detail") == "Own Goal",
                ),
            )
            contagem["gols"] += 1

        elif tipo == "Card":
            cor = "amarelo" if "Yellow" in ev.get("detail", "") else "vermelho"
            cur.execute(
                """INSERT INTO cartoes (jogo_id, jogador_id, lado, cor, minuto, periodo)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (jogo_id, jogador_id, lado, cor, minuto, periodo),
            )
            contagem["cartoes"] += 1

        elif tipo == "subst":
            jogador_entrou_nome = ev["assist"]["name"] if ev.get("assist") else None
            jogador_entrou_api_id = ev["assist"].get("id") if ev.get("assist") else None
            jogador_entrou_id = (
                get_or_create_jogador(cur, jogador_entrou_api_id, jogador_entrou_nome)
                if jogador_entrou_nome
                else None
            )
            cur.execute(
                """INSERT INTO substituicoes (jogo_id, jogador_saiu_id, jogador_entrou_id,
                                               lado, minuto, periodo)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (jogo_id, jogador_id, jogador_entrou_id, lado, minuto, periodo),
            )
            contagem["substituicoes"] += 1
        else:
            contagem["ignorados"] += 1

    return contagem


def buscar_minuto_saida(cur, jogo_id, jogador_id):
    """NOVO: cruza com a tabela `substituicoes` (já salva pelos eventos) pra
    saber em que minuto esse jogador saiu do jogo, se saiu. Fica NULL se o
    jogador jogou o jogo inteiro (titular que não foi substituído) ou se
    entrou como reserva (não tem "saída" pra registrar)."""
    cur.execute(
        "SELECT minuto FROM substituicoes WHERE jogo_id = %s AND jogador_saiu_id = %s",
        (jogo_id, jogador_id),
    )
    row = cur.fetchone()
    return row[0] if row else None


def salvar_escalacao(cur, jogo_id, dados_lineups):
    """NOVO: salva a escalação do jogo (titulares e reservas dos dois times)
    na tabela `escalacoes`. Pra titulares que saíram durante o jogo, cruza
    com `substituicoes` pra preencher o minuto de saída - por isso esse
    passo deve rodar DEPOIS de salvar_eventos, que é quem popula
    `substituicoes` primeiro."""
    salvos = 0

    for bloco_time in dados_lineups:
        titulares = bloco_time.get("startXI", [])
        reservas = bloco_time.get("substitutes", [])

        for entrada in titulares:
            jogador_info = entrada.get("player") or {}
            nome = jogador_info.get("name")
            if not nome:
                continue
            api_id = jogador_info.get("id")
            jogador_id = get_or_create_jogador(cur, api_id, nome)
            minuto_saida = buscar_minuto_saida(cur, jogo_id, jogador_id)

            cur.execute(
                """INSERT INTO escalacoes (jogo_id, jogador_id, titular, minuto_saida)
                   VALUES (%s, %s, %s, %s)""",
                (jogo_id, jogador_id, True, minuto_saida),
            )
            salvos += 1

        for entrada in reservas:
            jogador_info = entrada.get("player") or {}
            nome = jogador_info.get("name")
            if not nome:
                continue
            api_id = jogador_info.get("id")
            jogador_id = get_or_create_jogador(cur, api_id, nome)

            # reserva não tem "minuto de saída" (ele entrou, não saiu) -
            # fica NULL mesmo que ele tenha entrado e jogado o resto do jogo
            cur.execute(
                """INSERT INTO escalacoes (jogo_id, jogador_id, titular, minuto_saida)
                   VALUES (%s, %s, %s, %s)""",
                (jogo_id, jogador_id, False, None),
            )
            salvos += 1

    return salvos


def main():
    global requisicoes_usadas, _cursor_para_contador

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()
    _cursor_para_contador = cur

    requisicoes_usadas = carregar_requisicoes_usadas_hoje(cur)
    print(f"Requisições já usadas hoje (somando execuções anteriores): {requisicoes_usadas}")

    try:
        total_processados = 0
        total_pulados = 0
        total_futuros = 0

        times_rastreados = buscar_times_rastreados(cur)
        if not times_rastreados:
            print("Nenhum time com api_football_team_id preenchido na tabela `times` - nada a fazer.")
            return

        for time_id, time_nome, time_api_id in times_rastreados:
            print(f"\n========== {time_nome} ==========")

            for temporada in TEMPORADAS:
                jogos = buscar_jogos(temporada, time_api_id)

                for fixture in jogos:
                    fixture_id = fixture["fixture"]["id"]
                    home_team_id = fixture["teams"]["home"]["id"]
                    status_jogo = fixture["fixture"]["status"]["short"]

                    # NOVO (multi-time): get_or_create_jogo roda ANTES das
                    # checagens de "já processado" agora, porque o `jogo_id`
                    # de verdade só é conhecido depois dele (pode ser
                    # diferente do fixture_id, no caso raro de dois times
                    # rastreados jogarem entre si).
                    jogo_id = get_or_create_jogo(cur, fixture, time_id, time_api_id)
                    conn.commit()

                    # NOVO (correção de performance): se o jogo ainda não
                    # aconteceu (ou não terminou de verdade - adiado,
                    # cancelado, em andamento etc.), não existe evento,
                    # estatística ou escalação real pra buscar ainda. Cria/
                    # atualiza a linha do jogo (acima, pra `atualizar_odds.py`
                    # conseguir casar a odd com o jogo) e pula pro próximo,
                    # sem gastar as 4 chamadas de API + 28s de sleep por
                    # jogo futuro - isso é o que fazia a cadeia pesada durar
                    # cada vez mais ao adicionar times, processando o
                    # calendário inteiro da temporada todo santo dia.
                    if status_jogo not in STATUS_JOGO_FINALIZADO:
                        total_futuros += 1
                        continue

                    # NOVO (correção estrutural do bug de estatística/evento
                    # coletado antes da hora, e de evento corrigido pela API
                    # depois do fetch original - seção 27, Lacuna 1 e 2):
                    # dentro da janela de espera, força a busca de novo mesmo
                    # que já exista linha salva - o dado só é tratado como
                    # definitivo depois que a janela passar. Eventos e
                    # estatística têm janelas DIFERENTES (ver constantes).
                    dentro_da_janela_eventos = dentro_da_janela_de_espera(cur, jogo_id, JANELA_ESPERA_EVENTOS_HORAS)
                    dentro_da_janela_estatisticas = dentro_da_janela_de_espera(cur, jogo_id, JANELA_ESPERA_ESTATISTICA_HORAS)

                    falta_eventos = not jogo_ja_processado(cur, jogo_id) or dentro_da_janela_eventos
                    falta_estatisticas = not jogo_tem_estatisticas(cur, jogo_id) or dentro_da_janela_estatisticas
                    falta_estatisticas_jogador = not jogo_tem_estatisticas_jogador(cur, jogo_id) or dentro_da_janela_estatisticas
                    falta_escalacao = not jogo_tem_escalacao(cur, jogo_id)

                    if not falta_eventos and not falta_estatisticas \
                            and not falta_estatisticas_jogador and not falta_escalacao:
                        total_pulados += 1
                        continue

                    print(f"\nProcessando jogo {fixture_id} do {time_nome} (temporada {temporada})...")

                    if falta_eventos:
                        if dentro_da_janela_eventos and jogo_ja_processado(cur, jogo_id):
                            # NOVO (01/09/2026): já tinha evento salvo, mas
                            # ainda está dentro da janela de espera - apaga
                            # gols/cartões/substituições antigos antes de
                            # regravar (senão duplica, já que os INSERTs
                            # abaixo não têm ON CONFLICT). NÃO mexe em
                            # `escalacoes` - o minuto de saída ali é
                            # recalculado só na hora da inserção original;
                            # se essa rebusca mudar uma substituição depois
                            # da escalação já ter sido salva, o minuto de
                            # saída pode ficar desatualizado (limitação
                            # conhecida, fora do escopo desta correção).
                            cur.execute("DELETE FROM gols WHERE jogo_id = %s", (jogo_id,))
                            cur.execute("DELETE FROM cartoes WHERE jogo_id = %s", (jogo_id,))
                            cur.execute("DELETE FROM substituicoes WHERE jogo_id = %s", (jogo_id,))
                            print("  (dentro da janela de espera - rebuscando eventos pra ver se a API-Football completou/corrigiu algo)")
                        eventos = buscar_eventos(fixture_id)
                        contagem = salvar_eventos(cur, jogo_id, eventos, time_api_id)
                        print(f"  -> {contagem['gols']} gols, {contagem['cartoes']} cartões, "
                              f"{contagem['substituicoes']} substituições salvos "
                              f"({contagem['ignorados']} eventos ignorados).")
                        conn.commit()
                        time.sleep(7)  # respeita o limite de ~10 requisições por minuto do plano grátis

                    if falta_estatisticas:
                        if dentro_da_janela_estatisticas and jogo_tem_estatisticas(cur, jogo_id):
                            # NOVO: já tinha estatística salva, mas ainda
                            # está dentro da janela de espera - apaga a
                            # linha antiga antes de regravar (senão duplica,
                            # já que o INSERT abaixo não tem ON CONFLICT).
                            cur.execute("DELETE FROM estatisticas_jogo WHERE jogo_id = %s", (jogo_id,))
                            print("  (dentro da janela de espera - rebuscando estatística de time pra confirmar se já é definitiva)")
                        estatisticas = buscar_estatisticas(fixture_id)
                        salvos = salvar_estatisticas(cur, jogo_id, estatisticas, home_team_id)
                        print(f"  -> estatísticas de {salvos} lado(s) salvas.")
                        conn.commit()
                        time.sleep(7)

                    if falta_estatisticas_jogador:
                        if dentro_da_janela_estatisticas and jogo_tem_estatisticas_jogador(cur, jogo_id):
                            cur.execute("DELETE FROM jogador_estatisticas_jogo WHERE jogo_id = %s", (jogo_id,))
                            print("  (dentro da janela de espera - rebuscando estatística de jogador pra confirmar se já é definitiva)")
                        stats_jogadores = buscar_estatisticas_jogadores(fixture_id)
                        salvos = salvar_estatisticas_jogadores(cur, jogo_id, stats_jogadores, home_team_id)
                        print(f"  -> estatísticas individuais de {salvos} jogador(es) salvas.")
                        conn.commit()
                        time.sleep(7)

                    if falta_escalacao:
                        # NOVO: roda depois de eventos, pois precisa que
                        # `substituicoes` já esteja salva pra cruzar o minuto de
                        # saída de cada titular substituído.
                        lineups = buscar_escalacao(fixture_id)
                        salvos = salvar_escalacao(cur, jogo_id, lineups)
                        print(f"  -> escalação: {salvos} jogador(es) salvos (titulares + reservas).")
                        conn.commit()
                        time.sleep(7)

                    total_processados += 1

            print(f"  Concluído {time_nome}.")

        print(f"\nConcluído! {total_processados} jogos novos processados, "
              f"{total_pulados} já existiam no banco e foram pulados, "
              f"{total_futuros} ainda não aconteceram (ou não terminaram) e foram ignorados.")

    except LimiteDiarioAtingido as e:
        conn.commit()  # garante que o que já foi processado nessa execução fica salvo
        print(f"\n{e}")
        print(f"({total_processados} jogos processados nessa execução antes de parar.)")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
