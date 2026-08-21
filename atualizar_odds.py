"""
Script que verifica se o Corinthians tem jogo nos próximos dias e, se tiver,
busca as odds desse jogo (cartões de jogador + escanteios do time) na
OddsPapi, salvando tudo na tabela `odds` do Postgres.

NOVO: também tenta descobrir o árbitro escalado pra esse jogo, consultando
a API-Football (a OddsPapi não fornece esse dado). A escalação de árbitro
geralmente só é confirmada perto do jogo, então essa busca roda na mesma
janela de 2 dias de antecedência que já é usada pras odds. Se a API ainda
não tiver o árbitro definido, o campo fica em branco por enquanto - o
motor_recomendacoes.py simplesmente não aplica o ajuste de árbitro nesse caso.

NOVO (descrição legível e específica da odd): a OddsPapi nomeia mercados de
time genericamente como "Equipe 1"/"Equipe 2" (mandante/visitante), sem dizer
qual time é qual - e a descrição salva não incluía a linha (handicap) nem a
direção (Mais/Menos/Sim/Não) escolhida. Isso causava dois problemas: (1) duas
odds de linhas diferentes do mesmo mercado (ex: +3.5 e +5.5 escanteios)
ficavam com a MESMA descrição, parecendo apostas idênticas quando eram
diferentes; (2) não dava pra saber, só olhando a descrição, se "Equipe 2" era
o Corinthians ou o adversário. Agora a descrição é montada já traduzindo o
time certo e incluindo linha + direção (ex: "Escanteios Corinthians - Mais de
4.5").

NOVO (limpeza de odds antigas): antes, o script só inseria odds novas, sem
nunca apagar as antigas do mesmo jogo. Como o Cron roda todo dia dentro da
janela de 2 dias antes do jogo, isso fazia a tabela `odds` acumular várias
cópias da mesma aposta (uma por dia rodado), cada uma com o preço daquele
dia - gerando "recomendações duplicadas" com odd e probabilidade levemente
diferentes. Agora, antes de salvar as odds de um jogo, o script apaga as
odds anteriores desse mesmo jogo.

Feito para rodar automaticamente todo dia (Cron Schedule no Railway).
Se não houver jogo próximo, o script simplesmente não faz nada naquele dia.

Variáveis de ambiente necessárias (configuradas no Railway, aba "Variables"):
  - ODDSPAPI_KEY      -> sua chave da OddsPapi (api.oddspapi.io)
  - API_FOOTBALL_KEY  -> sua chave da API-Football (usada só pra buscar o árbitro)
  - DATABASE_URL      -> a URL de conexão do Postgres (mesma usada no outro script)
"""

import os
import re
import time
import json
from datetime import datetime, timezone

import requests
import psycopg2

# ---------- Configurações ----------
API_KEY = os.environ["ODDSPAPI_KEY"]
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY")  # opcional: sem ela, só pula o árbitro
DATABASE_URL = os.environ["DATABASE_URL"]

API_BASE = "https://api.oddspapi.io/v4"
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
SPORT_ID = 10
TOURNAMENT_ID = 325     # Brasileirão Série A
BOOKMAKERS = "superbet.bet.br"  # só Superbet por enquanto (plano pago com Player Props)
DIAS_ANTECEDENCIA = 2   # busca odds de jogos que acontecem em até X dias


# NOVO (multi-time): os times rastreados vêm da própria tabela `times`
# (marcados com `rastreado = TRUE`) - adicionar um time novo é: preencher
# os IDs dele + marcar rastreado=TRUE, e esse script já passa a coletar as
# odds dele também, sem precisar mexer em código.
# IMPORTANTE: NÃO basta ter os IDs preenchidos - um time pode ter os dois
# IDs preenchidos só por coincidência (correção de duplicata, ou por ter
# aparecido como adversário algum dia), sem nunca ter sido escolhido pra
# ser rastreado de verdade. `rastreado` é a marcação explícita que evita
# esse problema (foi exatamente isso que fez o script tentar coletar o
# Bahia inteiro sem ninguém ter pedido, e estourar o limite de requisição).
def buscar_times_rastreados(cur):
    cur.execute(
        "SELECT id, nome, oddspapi_participant_id, api_football_team_id FROM times "
        "WHERE rastreado = TRUE AND oddspapi_participant_id IS NOT NULL "
        "AND api_football_team_id IS NOT NULL ORDER BY nome"
    )
    return cur.fetchall()

# Palavras usadas para filtrar quais mercados nos interessam. Comparação é
# feita sem diferenciar maiúsculas.
# "tempo completo" captura especificamente o mercado "Resultado Tempo Completo"
# (confirmado manualmente na OddsPapi) - sem pegar por engano os mercados de
# resultado do 1º/2º tempo, que têm nomes parecidos mas não têm "completo".
# CORRIGIDO: "cart" (não "cartão"/"cartao" por extenso) - o plural em
# português de "cartão" é "cartões" (troca irregular, não é só "+s"), então
# "cartão"/"cartao" nunca batia com nomes de mercado que vêm no plural
# ("Cartões - Mais/Menos Equipe 1", "Cartões - Handicap", "Cartões -
# Ímpar/Par"...) - esses mercados eram descartados silenciosamente, mesmo
# tendo preço real na Superbet. Confirmado comparando com o catálogo real
# da OddsPapi: "cart" não aparece em nenhum nome de mercado que não seja
# sobre cartão. Mesma lição já aplicada em identificar_tipo_padrao()
# (motor_recomendacoes.py) pro "cartao_total".
PALAVRAS_MERCADO_INTERESSE = [
    "card", "cart", "corner", "escanteio",
    "falta", "desarme", "chute", "impediment", "tempo completo",
]


def get_com_retry_429(url, params, tentativas=5, espera_segundos=20):
    """NOVO: tenta de novo automaticamente se a OddsPapi responder 429
    (limite de requisição por minuto) - ficou mais comum agora que o script
    processa vários times rastreados em sequência rápida, uma execução
    atrás da outra. Espera um tempo fixo e tenta de novo, até um número
    máximo de tentativas, antes de desistir de vez.
    NOVO: também tenta de novo em caso de erro de CONEXÃO (não só 429) -
    ex: "IncompleteRead"/"ChunkedEncodingError", quando a conexão cai no
    meio do download de uma resposta grande (o catálogo de mercados tem
    ~600KB) - antes disso derrubava o script inteiro, sem nenhuma segunda
    tentativa, mesmo sendo uma falha de rede passageira."""
    resp = None
    for tentativa in range(tentativas):
        try:
            resp = requests.get(url, params=params, timeout=30)
        except requests.exceptions.RequestException as e:
            if tentativa == tentativas - 1:
                raise
            print(f"  Aviso: erro de conexão com a OddsPapi ({e.__class__.__name__}) - "
                  f"esperando {espera_segundos}s e tentando de novo...")
            time.sleep(espera_segundos)
            continue

        if resp.status_code == 429:
            if tentativa == tentativas - 1:
                resp.raise_for_status()
            print(f"  Aviso: limite de requisição da OddsPapi atingido (429) - "
                  f"esperando {espera_segundos}s e tentando de novo...")
            time.sleep(espera_segundos)
            continue
        return resp
    return resp


def buscar_catalogo_mercados():
    """Busca a lista de todos os mercados existentes, incluindo a linha
    (handicap) de cada mercado e o nome de cada outcome (Mais/Menos/Sim/Não/
    0/1+/2+), para conseguirmos interpretar as odds corretamente depois."""
    resp = get_com_retry_429(
        f"{API_BASE}/markets",
        params={"sportId": SPORT_ID, "language": "pt", "apiKey": API_KEY},
    )
    resp.raise_for_status()
    mercados = resp.json()

    catalogo = {}
    for m in mercados:
        catalogo[str(m["marketId"])] = {
            "nome": m["marketName"],
            "handicap": m.get("handicap"),
            "tipo": m.get("marketType"),
            # NOVO (Onda 2 - Dupla Chance/Ambas Marcam por tempo): precisa
            # saber o período (fulltime/p1/p2) pra distinguir entre as
            # versões "jogo inteiro" (sem preço na Superbet, já
            # confirmado) e "por tempo" (com preço) do MESMO marketType -
            # o nome bruto sozinho não seria confiável o suficiente aqui.
            "periodo": m.get("period"),
            "outcomes": {str(o["outcomeId"]): o["outcomeName"] for o in m.get("outcomes", [])},
        }
    return catalogo


def buscar_proximos_jogos(participant_id):
    """Busca jogos desse time no Brasileirão e filtra os que acontecem
    dentro da janela de antecedência definida."""
    resp = get_com_retry_429(
        f"{API_BASE}/fixtures",
        params={
            "tournamentId": TOURNAMENT_ID,
            "sportId": SPORT_ID,
            "participantId": participant_id,
            "apiKey": API_KEY,
        },
    )
    resp.raise_for_status()
    jogos = resp.json()

    hoje = datetime.now(timezone.utc).date()

    proximos = []
    for jogo in jogos:
        if not jogo.get("startTime"):
            continue
        inicio = datetime.fromisoformat(jogo["startTime"].replace("Z", "+00:00"))
        data_do_jogo = inicio.date()

        # NOVO: compara por DIA calendário, não por horário exato. Assim,
        # se o jogo é dia 23, o script já funciona a partir de 00:00 do dia
        # 21 (D-2), em vez de só a partir de exatas 48h antes do horário
        # do jogo (o que antes travava até as 19:30 do dia 21, por exemplo,
        # se o jogo fosse às 19:30 do dia 23).
        dias_de_diferenca = (data_do_jogo - hoje).days
        if 0 <= dias_de_diferenca <= DIAS_ANTECEDENCIA:
            proximos.append(jogo)

    return proximos


# NOVO: cache dos bookmakers que já sabemos estar restritos nesse plano,
# descoberto na primeira vez que a API recusar. Reaproveitado pro resto da
# execução, pra não gastar 2 requisições por jogo (uma que sempre falha +
# o retry) - a partir do 2º jogo, já pede direto só com os liberados.
_bookmakers_restritos_conhecidos = set()


def buscar_odds(fixture_id, bookmakers=None):
    """Busca as odds de um jogo específico nas casas configuradas.

    NOVO: se a OddsPapi recusar por causa de bookmaker restrito no plano
    (erro "RESTRICTED_ACCESS"), remove automaticamente esse bookmaker da
    lista e tenta de novo só com os que têm acesso liberado - assim uma
    casa sem permissão no plano não derruba a busca de odds de todos os
    jogos (e, por consequência, o cron inteiro). O bookmaker restrito fica
    guardado em cache pro resto da execução, evitando repetir a descoberta
    (e a requisição extra) a cada jogo processado."""
    global _bookmakers_restritos_conhecidos

    if bookmakers is None:
        bookmakers = [b for b in BOOKMAKERS.split(",") if b not in _bookmakers_restritos_conhecidos]

    if not bookmakers:
        return {"bookmakerOdds": {}}

    resp = get_com_retry_429(
        f"{API_BASE}/odds",
        params={
            "fixtureId": fixture_id,
            "bookmakers": ",".join(bookmakers),
            "oddsFormat": "decimal",
            "language": "pt",
            "verbosity": 3,
            "apiKey": API_KEY,
        },
    )

    if resp.status_code == 403:
        try:
            erro = resp.json().get("error", {})
        except ValueError:
            erro = {}

        if erro.get("code") == "RESTRICTED_ACCESS":
            restritos = [b for b in bookmakers if b in erro.get("details", "")]
            restantes = [b for b in bookmakers if b not in restritos]

            # trava de segurança: se não identificou nenhum bookmaker restrito
            # a partir do texto do erro (formato de mensagem mudou, por ex.),
            # não tenta de novo com a mesma lista - isso causaria loop infinito
            if not restritos:
                print(f"  Aviso: acesso negado (RESTRICTED_ACCESS), mas não foi possível "
                      f"identificar qual bookmaker está restrito. Detalhe da API: "
                      f"{erro.get('details')}")
                resp.raise_for_status()

            # guarda no cache, pra não precisar redescobrir isso a cada jogo
            _bookmakers_restritos_conhecidos.update(restritos)

            print(f"  Aviso: sem acesso a {restritos} nesse plano. "
                  f"Tentando de novo só com {restantes or '(nenhum restante)'}...")
            return buscar_odds(fixture_id, bookmakers=restantes)

    resp.raise_for_status()
    return resp.json()


def buscar_fixture_api_football(data_jogo, team_api_football_id):
    """Consulta a API-Football pra encontrar o fixture do Corinthians numa
    data específica. Retorna o fixture inteiro (dict) ou None se a chave não
    estiver configurada, se a API ainda não tiver esse jogo cadastrado, ou se
    a consulta falhar por qualquer motivo (não deve travar o script todo -
    esse dado é complementar, não essencial).

    NOVO: antes essa função só retornava o árbitro. Agora retorna o fixture
    completo porque também precisamos do ID real do jogo na API-Football
    (fixture["fixture"]["id"]) - usar esse mesmo ID como PK ao criar o jogo
    aqui evita que esse script e o popular_banco.py criem dois registros
    diferentes pro mesmo jogo (um com ID auto-incrementado, outro com o ID
    real), o que deixaria odds e estatísticas/escalação "cegos" um pro
    outro."""
    if not API_FOOTBALL_KEY:
        return None

    try:
        resp = requests.get(
            f"{API_FOOTBALL_BASE}/fixtures",
            headers={"x-apisports-key": API_FOOTBALL_KEY},
            params={"team": team_api_football_id, "date": data_jogo},
        )
        resp.raise_for_status()
        dados = resp.json()
        jogos = dados.get("response", [])
        if not jogos:
            return None
        return jogos[0]
    except Exception as e:
        print(f"  Aviso: não foi possível consultar a API-Football ({e}). Seguindo sem esse dado.")
        return None


def normalizar_nome_oddspapi(nome):
    """NOVO: a OddsPapi retorna nome de jogador no formato "Sobrenome, Nome"
    (ex: "Alberto, Yuri"), diferente da API-Football, que usa "Nome
    Sobrenome" (ex: "Yuri Alberto"). Sem converter, o cadastro por nome
    nunca batia com o jogador já criado pelo popular_banco.py - criava um
    registro duplicado, sem api_football_id e sem nenhuma escalação
    vinculada, mesmo o jogador sendo titular de verdade (foi assim que o
    Yuri Alberto ficou marcado como "indisponível" por engano)."""
    if "," in nome:
        partes = [p.strip() for p in nome.split(",", 1)]
        if len(partes) == 2 and partes[0] and partes[1]:
            sobrenome, nome_proprio = partes
            return f"{nome_proprio} {sobrenome}".strip()
    return nome


def get_or_create_jogador(cur, nome):
    """NOVO: normaliza o nome (ver normalizar_nome_oddspapi) antes de
    procurar/criar - assim, o jogador criado aqui casa com o mesmo registro
    que o popular_banco.py usa (que vem no formato "Nome Sobrenome" da
    API-Football)."""
    nome_normalizado = normalizar_nome_oddspapi(nome)

    cur.execute("SELECT id FROM jogadores WHERE nome = %s", (nome_normalizado,))
    row = cur.fetchone()
    if row:
        return row[0]

    # rede de segurança: se por algum motivo já existir um registro com o
    # nome no formato original (não normalizado), aproveita em vez de duplicar
    if nome_normalizado != nome:
        cur.execute("SELECT id FROM jogadores WHERE nome = %s", (nome,))
        row = cur.fetchone()
        if row:
            return row[0]

    cur.execute("INSERT INTO jogadores (nome) VALUES (%s) RETURNING id", (nome_normalizado,))
    return cur.fetchone()[0]


def get_or_create_time(cur, oddspapi_participant_id, nome):
    """Garante que o time existe na tabela `times`, retorna o id.

    Mesma lógica de identidade por id da API usada em get_or_create_jogador,
    só que aqui usando o participantId da OddsPapi (que é diferente do
    team_id da API-Football - por isso a coluna separada oddspapi_participant_id
    em `times`). Times criados aqui casam automaticamente com os que o
    popular_banco.py cria pelo lado da API-Football, porque os dois caem no
    mesmo fallback por nome quando o id de uma das duas APIs ainda não está
    salvo no registro."""
    if oddspapi_participant_id is not None:
        cur.execute("SELECT id FROM times WHERE oddspapi_participant_id = %s", (oddspapi_participant_id,))
        row = cur.fetchone()
        if row:
            return row[0]

        cur.execute("SELECT id, oddspapi_participant_id FROM times WHERE nome = %s", (nome,))
        row = cur.fetchone()
        if row:
            time_id, id_existente = row
            if id_existente is None:
                cur.execute(
                    "UPDATE times SET oddspapi_participant_id = %s WHERE id = %s",
                    (oddspapi_participant_id, time_id),
                )
            return time_id

        cur.execute(
            "INSERT INTO times (nome, oddspapi_participant_id) VALUES (%s, %s) RETURNING id",
            (nome, oddspapi_participant_id),
        )
        return cur.fetchone()[0]

    cur.execute("SELECT id FROM times WHERE nome = %s", (nome,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO times (nome) VALUES (%s) RETURNING id", (nome,))
    return cur.fetchone()[0]


def get_or_create_jogo(cur, data_jogo, adversario, mandante,
                        mandante_nome, visitante_nome,
                        mandante_oddspapi_id, visitante_oddspapi_id,
                        nosso_time_id, nosso_time_api_football_id,
                        datahora_jogo=None):
    """Encontra (ou cria) o jogo, retorna o id.
    NOVO: se o jogo já existe mas ainda não tem árbitro salvo, tenta buscar
    e atualizar (a escalação pode ter sido confirmada entre uma execução e
    outra do cron, já que ambas rodam na mesma janela de 2 dias).
    NOVO (ID real da API-Football): ao criar um jogo novo, busca o fixture
    correspondente na API-Football e usa o MESMO ID como chave primária.
    Sem isso, esse script criava o jogo com um ID auto-incrementado do banco,
    diferente do ID real que o popular_banco.py usaria mais tarde pro mesmo
    jogo - resultando em DOIS registros duplicados pro mesmo jogo (um com as
    odds, outro com estatísticas/escalação), cegos um pro outro. Se por
    algum motivo o ID real já estiver em uso por outro registro, a inserção
    com ID explícito falha com segurança (savepoint) e cai de volta pro
    comportamento antigo (ID automático), sem travar o script.
    NOVO (datahora_jogo): a OddsPapi já manda o horário completo do jogo
    (jogo["startTime"]) - salvamos ele também, não só a data, porque sem
    hora o sistema não conseguia saber se um jogo "de hoje" já tinha
    terminado ou ainda ia acontecer (continuava sugerindo odds de um jogo
    já encerrado até a virada do dia).
    NOVO (busca por ID de time, não por texto): antes, o jogo era procurado
    por "data + nome do adversário" (texto). Como a OddsPapi e a API-Football
    às vezes grafam o mesmo time de forma diferente (ex: "Remo" vs "Clube
    do Remo PA"), isso criava jogo DUPLICADO sempre que o nome divergia -
    e o Brasileirão tem vários times com nomes parecidos (vários "Atlético",
    por exemplo), o que tornava esse risco frequente, não raro. Agora a
    busca usa mandante_id/visitante_id (já resolvidos de forma confiável
    via api_football_team_id/oddspapi_participant_id) - o texto só entra
    como fallback de segurança pra jogos bem antigos que ainda não tenham
    esses ids preenchidos.
    NOVO (multi-time): a busca também filtra por nosso_time_id - se os DOIS
    times de um jogo forem rastreados (ex: Corinthians x Athletico
    Paranaense), cada um enxerga sua PRÓPRIA linha desse jogo (mesmo padrão
    já usado no popular_banco.py), em vez de compartilhar uma linha só."""
    mandante_id = get_or_create_time(cur, mandante_oddspapi_id, mandante_nome)
    visitante_id = get_or_create_time(cur, visitante_oddspapi_id, visitante_nome)

    cur.execute(
        "SELECT id, arbitro, datahora_jogo FROM jogos "
        "WHERE data_jogo = %s AND mandante_id = %s AND visitante_id = %s AND nosso_time_id = %s",
        (data_jogo, mandante_id, visitante_id, nosso_time_id),
    )
    row = cur.fetchone()

    if not row:
        # fallback de segurança: jogo antigo que ainda não tem mandante_id/
        # visitante_id preenchido (não deveria mais acontecer, mas evita
        # duplicar um jogo legítimo enquanto algum registro assim existir)
        cur.execute(
            "SELECT id, arbitro, datahora_jogo FROM jogos "
            "WHERE data_jogo = %s AND adversario = %s AND nosso_time_id = %s "
            "AND (mandante_id IS NULL OR visitante_id IS NULL)",
            (data_jogo, adversario, nosso_time_id),
        )
        row = cur.fetchone()

    if row:
        jogo_id, arbitro_salvo, datahora_salva = row
        if arbitro_salvo is None:
            fixture = buscar_fixture_api_football(data_jogo, nosso_time_api_football_id)
            arbitro = fixture["fixture"].get("referee") if fixture else None
            if arbitro:
                cur.execute("UPDATE jogos SET arbitro = %s WHERE id = %s", (arbitro, jogo_id))
                print(f"  Árbitro confirmado: {arbitro}")

        # backfill: jogo achado pelo fallback de texto - garante que fica
        # com mandante_id/visitante_id preenchido daqui pra frente
        cur.execute(
            "UPDATE jogos SET mandante_id = %s, visitante_id = %s WHERE id = %s "
            "AND (mandante_id IS NULL OR visitante_id IS NULL)",
            (mandante_id, visitante_id, jogo_id),
        )

        # NOVO: backfill de datahora_jogo (jogo criado antes dessa coluna existir)
        if datahora_salva is None and datahora_jogo is not None:
            cur.execute(
                "UPDATE jogos SET datahora_jogo = %s WHERE id = %s",
                (datahora_jogo, jogo_id),
            )
        return jogo_id

    fixture = buscar_fixture_api_football(data_jogo, nosso_time_api_football_id)
    arbitro = fixture["fixture"].get("referee") if fixture else None
    fixture_id_real = fixture["fixture"]["id"] if fixture else None
    if arbitro:
        print(f"  Árbitro confirmado: {arbitro}")

    if fixture_id_real is not None:
        # tenta usar o ID real da API-Football, com uma rede de segurança
        # (savepoint) caso esse ID já esteja em uso por outro caminho -
        # inclusive pela visão de OUTRO time rastreado nesse mesmo jogo real.
        cur.execute("SAVEPOINT antes_de_inserir_jogo")
        try:
            cur.execute(
                """INSERT INTO jogos (id, fixture_id_api, nosso_time_id, data_jogo, datahora_jogo,
                                       adversario, mandante, competicao,
                                       arbitro, mandante_id, visitante_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (fixture_id_real, fixture_id_real, nosso_time_id, data_jogo, datahora_jogo,
                 adversario, mandante, "Brasileirão Série A",
                 arbitro, mandante_id, visitante_id),
            )
            return cur.fetchone()[0]
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT antes_de_inserir_jogo")
            print(f"  Aviso: ID real {fixture_id_real} já em uso por outro registro - "
                  "criando esse jogo com ID automático (verificar depois se não duplicou).")

    # NOVO: fixture_id_api é obrigatório agora - se a API-Football não
    # confirmou o fixture real (ex: chave ausente, falha de rede), usa um
    # valor sintético negativo (nunca colide com um fixture_id real, que é
    # sempre positivo) só pra não violar a coluna obrigatória.
    fixture_id_para_salvar = fixture_id_real if fixture_id_real is not None else -int(time.time() * 1000)

    cur.execute(
        """INSERT INTO jogos (fixture_id_api, nosso_time_id, data_jogo, datahora_jogo,
                               adversario, mandante, competicao, arbitro,
                               mandante_id, visitante_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (fixture_id_para_salvar, nosso_time_id, data_jogo, datahora_jogo, adversario, mandante,
         "Brasileirão Série A", arbitro, mandante_id, visitante_id),
    )
    return cur.fetchone()[0]


def buscar_lesoes_suspensos(fixture_id_api):
    """NOVO (integração /injuries): busca a lista de jogadores machucados/
    suspensos reportada pela API-Football pra um jogo específico. Usa o
    parâmetro `fixture` (o mais direto - traz o relatório de
    indisponibilidade já filtrado pros dois times daquele confronto,
    sem precisar cruzar manualmente com a lista inteira da liga).

    Retorna lista de dicts (um por jogador reportado, já sem duplicata) ou
    lista vazia se não tiver nada, a chave não estiver configurada, ou a
    chamada falhar por qualquer motivo - esse dado é complementar, não deve
    travar o script. Guarda o item bruto (`bruto`) junto, pra não perder
    nada se algum campo específico vier com nome diferente do esperado.

    CORRIGIDO: confirmado na prática que a API-Football devolve cada
    jogador reportado DUAS VEZES na resposta de /injuries?fixture=X (não é
    bug nosso na montagem da chamada - a lista que ela manda já vem assim).
    Deduplica aqui, na origem, pra tanto o log quanto o salvamento no banco
    refletirem a lista já limpa - antes só deduplicava na hora de salvar,
    então o log continuava mostrando a contagem e os nomes duplicados."""
    if not API_FOOTBALL_KEY:
        return []

    try:
        resp = requests.get(
            f"{API_FOOTBALL_BASE}/injuries",
            headers={"x-apisports-key": API_FOOTBALL_KEY},
            params={"fixture": fixture_id_api},
            timeout=15,
        )
        resp.raise_for_status()
        dados = resp.json()
        if dados.get("errors"):
            print(f"  Aviso: API-Football retornou erro em /injuries: {dados['errors']}")
            return []

        resultado = []
        vistos = set()
        for item in dados.get("response", []):
            jogador_api = item.get("player", {}) or {}
            time_api = item.get("team", {}) or {}
            chave = jogador_api.get("id") or jogador_api.get("name")
            if chave in vistos:
                continue
            vistos.add(chave)
            resultado.append({
                "jogador_api_football_id": jogador_api.get("id"),
                "jogador_nome": jogador_api.get("name"),
                "time_api_football_id": time_api.get("id"),
                "time_nome": time_api.get("name"),
                "tipo": jogador_api.get("type"),
                "motivo": jogador_api.get("reason"),
                "bruto": item,
            })
        return resultado
    except Exception as e:
        print(f"  Aviso: não foi possível consultar /injuries desse jogo ({e}). Seguindo sem esse dado.")
        return []


def salvar_lesoes_suspensoes(cur, jogo_id, lesoes):
    """NOVO (integração /injuries): grava a lista de lesão/suspensão desse
    jogo. Apaga os registros antigos desse jogo_id antes - o status pode
    mudar de um dia pro outro (jogador recuperado, por exemplo), então não
    faz sentido acumular; sempre reflete a última checagem.

    CORRIGIDO: confirmado na prática que a API-Football devolve cada
    jogador reportado DUAS VEZES na resposta de /injuries?fixture=X (não
    é bug nosso na montagem da chamada - a lista que ela manda já vem
    assim). Deduplica por jogador (api_football_id, com fallback pro nome
    quando não vem ID) antes de gravar, senão cada jogador vira 2 linhas
    idênticas na tabela."""
    cur.execute("DELETE FROM lesoes_suspensoes WHERE jogo_id = %s", (jogo_id,))

    vistos = set()
    for item in lesoes:
        chave = item["jogador_api_football_id"] or item["jogador_nome"]
        if chave in vistos:
            continue
        vistos.add(chave)

        jogador_id = None
        if item["jogador_api_football_id"] is not None:
            cur.execute(
                "SELECT id FROM jogadores WHERE api_football_id = %s",
                (item["jogador_api_football_id"],),
            )
            row = cur.fetchone()
            jogador_id = row[0] if row else None

        cur.execute(
            """INSERT INTO lesoes_suspensoes
               (jogo_id, jogador_id, jogador_nome_api, tipo, motivo, dados_brutos)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (jogo_id, jogador_id, item["jogador_nome"], item["tipo"], item["motivo"],
             json.dumps(item["bruto"], ensure_ascii=False)),
        )


def nome_time_por_posicao(mandante, adversario, posicao, nosso_nome):
    """NOVO: traduz "Equipe 1"/"Equipe 2" (nomenclatura genérica que a
    OddsPapi usa pra mercados de time) pro nome real do time. Convenção da
    OddsPapi: Equipe 1 = mandante do jogo, Equipe 2 = visitante."""
    nosso_time_eh_equipe_1 = mandante
    if posicao == "1":
        return nosso_nome if nosso_time_eh_equipe_1 else adversario
    return adversario if nosso_time_eh_equipe_1 else nosso_nome


def nome_real_handicap_meia_linha(linha, direcao):
    """NOVO (20/08/2026): decide o nome comercial REAL da Superbet pro
    mercado de handicap de meia linha, baseado no valor da linha do ponto
    de vista do time descrito nessa direção - em vez de aceitar direto o
    "Handicap Asiático" que a OddsPapi devolve pra TODO o tipo de mercado
    "spreads" (marketName genérico da API, sem diferenciar linha nenhuma).

    Mesma regra de valor que motor_recomendacoes.montar_descricao_handicap
    já usa pra descrição final da recomendação - só que aplicada aqui, na
    hora de gravar a odd crua, pra tabela `odds` não guardar o nome errado
    nem internamente. As duas funções não compartilham código (arquivos
    diferentes, sem import cruzado) mas implementam a MESMA regra de
    negócio - se essa regra mudar de novo (ex: confirmação de mais um
    caso via print real), atualizar as duas juntas.

    Convenção da OddsPapi: a `linha` do catálogo é sempre relativa à
    Equipe 1 (mandante). Pra Equipe 2 (visitante, direção "2"), o sinal
    inverte - mesma convenção que nome_time_por_posicao já usa.

    Preparado pro futuro: se um dia a linha de quarto de gol for
    implementada (item 14/C da documentação, hoje fora de escopo), o
    valor cai automaticamente no "else" e volta a ser rotulado
    "Handicap Asiático" de verdade - sem precisar tocar nessa função de
    novo, porque a decisão é por valor, não por lista fixa."""
    try:
        linha_time = float(linha) if str(direcao) == "1" else -float(linha)
    except (TypeError, ValueError):
        return "Handicap"

    if linha_time == 0.5:
        return "Dupla Chance"
    if linha_time == -0.5:
        return "Resultado Final"
    if round(linha_time % 1, 2) == 0.5:
        return "Handicap"
    return "Handicap Asiático"  # quarto de gol/linha cheia - fora de escopo hoje, mas nome certo se um dia entrar


def montar_descricao_mercado(nome_mercado, linha, direcao, mandante, adversario, nosso_nome, player_name=None):
    """NOVO: monta uma descrição legível e ESPECÍFICA da odd, resolvendo
    "Equipe 1"/"Equipe 2" pro nome real do time e incluindo a linha
    (handicap) e a direção escolhida (Mais/Menos/Sim/Não).

    Sem isso, duas odds do mesmo mercado com linhas diferentes (ex: "mais de
    3.5 escanteios" e "mais de 5.5 escanteios") ficavam com a mesma descrição
    salva, e o motor de combinações não tinha como saber que eram apostas
    diferentes (ou que media a mesma coisa em pontos de corte diferentes)."""
    descricao = nome_mercado

    def substituir(match):
        return nome_time_por_posicao(mandante, adversario, match.group(1), nosso_nome)

    descricao = re.sub(r"Equipe\s*([12])", substituir, descricao, flags=re.IGNORECASE)

    direcao_normalizada = (direcao or "").strip().lower()
    if linha is not None and direcao_normalizada in ("mais", "menos"):
        detalhe = f"{direcao} de {linha}"
    elif linha is not None and direcao_normalizada in ("1", "2"):
        # NOVO (Handicap Asiático): direção vem crua da OddsPapi como "1"/
        # "2" (não "Equipe 1"/"Equipe 2") - resolve pro nome do time do
        # mesmo jeito que o resto da descrição já faz, e inclui a linha
        # (handicap) com sinal, que é a parte que realmente diferencia uma
        # odd de handicap da outra.
        nome_time_direcao = nome_time_por_posicao(mandante, adversario, direcao_normalizada, nosso_nome)
        sinal = "+" if linha > 0 else ""
        detalhe = f"{nome_time_direcao} {sinal}{linha}"
    else:
        detalhe = direcao

    if detalhe:
        descricao = f"{descricao} - {detalhe}"

    if player_name:
        descricao = f"{descricao} - {player_name}"

    return descricao


# NOVO: marketTypes que identificam mercados de TOTAL DO JOGO (mandante +
# visitante somados), confirmados olhando o catálogo real da OddsPapi -
# diferente dos mercados por time (teamtotals-corner-team1/team2, que temos
# hoje) e dos mercados de handicap por time (spread-bookings, fora de
# escopo). Mapeia pro nome final que queremos salvar, já sem ambiguidade
# com o mercado por time.
MARKET_TYPES_TOTAL_DO_JOGO = {
    "totals-corners": "Escanteios Total do Jogo",
    "totals-bookings": "Cartões Total do Jogo",
}

# NOVO (Mais/Menos gols e Equipe Marca): diferente de todos os mercados de
# cima, esses são identificados só pelo marketType, NUNCA por palavra-chave
# no nome (não entram em PALAVRAS_MERCADO_INTERESSE). O nome bruto da
# OddsPapi pra esses ("Mais/Menos Tempo Completo", "Mais/Menos Equipe 1/2",
# "Equipe 1 Marca") é genérico demais pra usar palavra-chave sem risco de
# capturar mercado errado por engano - ex: a palavra "marca" também aparece
# em "Marcador a Qualquer Momento" e "Primeiro Marcador" (mercados de
# artilheiro, fora de escopo por enquanto). "totals" e "teamtotals-team1/2"
# também são renomeados aqui (mesmo motivo do MARKET_TYPES_TOTAL_DO_JOGO
# acima: deixar claro que é GOL, não escanteio/cartão, já que o nome bruto
# não diz).
MARKET_TYPES_GOLS_E_MARCA = {
    "totals": "Gols Total do Jogo",
    "teamtotals-team1": "Gols do Time - Equipe 1",
    "teamtotals-team2": "Gols do Time - Equipe 2",
    "toscore-team1": "Equipe 1 Marca",
    "toscore-team2": "Equipe 2 Marca",
}

# NOVO (Onda 2 - Dupla Chance/Ambas Marcam por tempo): diferente de todos
# os outros mercados de cima, esses são identificados por (marketType,
# período) - o MESMO marketType tem uma versão "jogo inteiro" (sem preço
# confirmado na Superbet) e uma versão "por tempo" (com preço) - por isso
# não dá pra usar só marketType como chave, como os dicts acima fazem.
# São também os ÚNICOS mercados do projeto que passam por cima do filtro
# de "sem parcial" (mercado_de_tempo_parcial) de propósito - só entram
# nessa lista aqui.
MARKET_TYPES_POR_TEMPO = {
    ("doublechance", "p1"): "Dupla Chance Primeiro Tempo",
    ("doublechance", "p2"): "Dupla Chance Segundo Tempo",
    ("bothteamsscore", "p1"): "Ambas Marcam Primeiro Tempo",
    ("bothteamsscore", "p2"): "Ambas Marcam Segundo Tempo",
}

# NOVO (Onda 2 - Marca em Ambos os Tempos): esse já vem com nome único e
# sem "Primeiro/Segundo Tempo" no texto (é um mercado sobre o jogo inteiro,
# olhando os dois tempos juntos) - só precisa do bypass do filtro de
# palavra-chave, não do de tempo parcial.
MARKET_TYPES_MARCA_AMBOS_TEMPOS = {
    "toscoreinbh-team1": "Equipe 1 Marca em Ambos os Tempos",
    "toscoreinbh-team2": "Equipe 2 Marca em Ambos os Tempos",
}


def eh_handicap_meia_linha(tipo_mercado, periodo, handicap):
    """NOVO (Handicap Asiático - decisão de arquitetura de 17/08/2026): só
    aceita handicap de MEIO gol (termina em .5, ex: -0.5, -1.5, 2.5) - com
    placar de futebol sempre inteiro, meio gol nunca empata matematicamente,
    então o resultado é sempre binário (ganhou/perdeu), sem "push" nem
    aposta dividida - encaixa no mesmo modelo que todo o resto do projeto
    já usa. Linha cheia (0, -1, -2...) pode empatar exatamente (vira
    aposta anulada) e linha de quarto de gol (-0.25, -0.75...) SEMPRE
    divide a aposta em duas metades com resultado parcial - os dois
    ficam de fora de propósito: implementar "resultado parcial" de
    verdade exigiria mudar banca/VE/múltiplas/histórico ao mesmo tempo,
    risco bem maior que o ganho de cobrir essas linhas a mais."""
    if tipo_mercado != "spreads" or periodo != "fulltime" or handicap is None:
        return False
    return round(float(handicap) % 1, 2) == 0.5


def mercado_de_tempo_parcial(nome_mercado):
    """NOVO: a OddsPapi também retorna versões de Primeiro Tempo/Segundo
    Tempo desses mesmos mercados (ex: "Cartões - Mais/Menos Primeiro
    Tempo") - fora do escopo do projeto hoje (só olhamos o jogo completo).
    "Tempo Completo" (usado no mercado de resultado final) não é excluído."""
    nome = nome_mercado.lower()
    if "tempo completo" in nome:
        return False
    return "primeiro tempo" in nome or "segundo tempo" in nome


def existem_odds_utilizaveis(dados_odds):
    """NOVO: checa se a resposta da OddsPapi realmente trouxe algo
    aproveitável (pelo menos uma casa não suspensa, com mercados). Sem essa
    checagem, uma resposta "com sucesso" mas vazia (ex: mercados suspensos
    temporariamente perto/durante o jogo) fazia o script apagar as odds
    antigas (boas) e não colocar nada no lugar - o jogo ficava
    permanentemente sem odds depois disso, mesmo tendo tido odds válidas
    antes."""
    bookmaker_odds = dados_odds.get("bookmakerOdds", {})
    for info_casa in bookmaker_odds.values():
        if info_casa.get("suspended"):
            continue
        if info_casa.get("markets"):
            return True
    return False


def ja_existe_odd_jogador(cur, fixture_id_api, jogador_id, mercado, casa, linha, direcao):
    """NOVO (correção de bug real - duplicata multi-time, raiz do problema
    já corrigido nas camadas de exibição): quando os DOIS times de um jogo
    são rastreados, esse jogo real gera 2 linhas em `jogos` (uma por
    perspectiva) - e como a OddsPapi devolve a folha de odds INTEIRA do
    confronto (os DOIS lados) pra cada busca, o mesmo mercado de JOGADOR
    acabava sendo salvo duas vezes: uma quando processamos a perspectiva
    do time A, outra quando processamos a do time B. Antes de inserir uma
    odd de jogador, confere se ela já existe pra esse MESMO jogo real
    (mesmo fixture_id_api), vinda da OUTRA perspectiva - se sim, não
    insere de novo.
    Só se aplica a mercados de JOGADOR (jogador_id preenchido) - mercados
    de time (escanteio de time, resultado final, escanteio/cartão total
    do jogo) NÃO entram aqui de propósito: pra esses, a estimativa de
    probabilidade calculada depois É diferente por perspectiva (vem do
    histórico de cada time), então guardar as duas cópias é intencional -
    ver deduplicar_recomendacoes() em combinacoes.py, que já lida com
    isso na hora de ler, escolhendo a de maior probabilidade."""
    if jogador_id is None or fixture_id_api is None:
        return False
    cur.execute(
        """
        SELECT 1 FROM odds o
        JOIN jogos j ON j.id = o.jogo_id
        WHERE j.fixture_id_api = %s AND o.jogador_id = %s AND o.casa_aposta = %s
          AND o.mercado = %s AND o.linha IS NOT DISTINCT FROM %s AND o.direcao IS NOT DISTINCT FROM %s
        LIMIT 1
        """,
        (fixture_id_api, jogador_id, casa, mercado, linha, direcao),
    )
    return cur.fetchone() is not None


def salvar_odds_do_jogo(cur, jogo_id, dados_odds, catalogo_mercados, mandante, adversario, nosso_nome,
                         fixture_id_api=None):
    """Percorre as odds de todas as casas/mercados retornados e salva só os
    mercados de interesse (cartão de jogador + escanteios do time), incluindo
    a linha (handicap) e a direção (Mais/Menos/Sim/Não) de cada odd.

    NOVO: a descrição agora é montada com montar_descricao_mercado, que
    traduz o time e inclui linha + direção (ver docstring dela).
    NOVO (mercados de total do jogo): mercados com marketType em
    MARKET_TYPES_TOTAL_DO_JOGO (escanteios/cartões somando os dois times)
    usam um nome fixo e inequívoco, em vez do nome genérico do catálogo -
    evita confundir com o mercado por time (que também contém a palavra
    "escanteio"/"cartão", mas se refere só a um lado).
    NOVO (correção de duplicata na raiz): recebe `fixture_id_api` do jogo
    - usado só pra checar, via ja_existe_odd_jogador, se uma odd de
    JOGADOR já foi salva pela outra perspectiva desse mesmo jogo real
    antes de inserir de novo."""
    salvos = 0
    pulados_duplicados = 0
    bookmaker_odds = dados_odds.get("bookmakerOdds", {})

    for casa, info_casa in bookmaker_odds.items():
        if info_casa.get("suspended"):
            continue

        markets = info_casa.get("markets", {})
        for market_id, market_info in markets.items():
            info_mercado = catalogo_mercados.get(market_id)
            if not info_mercado:
                continue

            tipo_mercado = info_mercado.get("tipo")
            periodo_mercado = info_mercado.get("periodo")
            chave_periodo = (tipo_mercado, periodo_mercado)

            # NOVO (Mais/Menos gols, Equipe Marca, e Onda 2 - Dupla Chance/
            # Ambas Marcam por tempo/Marca em Ambos os Tempos): passam
            # mesmo sem bater nenhuma palavra-chave, porque são
            # identificados só pelo marketType (ou marketType+período).
            interessa_por_tipo = (
                tipo_mercado in MARKET_TYPES_TOTAL_DO_JOGO
                or tipo_mercado in MARKET_TYPES_GOLS_E_MARCA
                or tipo_mercado in MARKET_TYPES_MARCA_AMBOS_TEMPOS
                or chave_periodo in MARKET_TYPES_POR_TEMPO
                or eh_handicap_meia_linha(tipo_mercado, periodo_mercado, info_mercado.get("handicap"))
            )
            if not interessa_por_tipo and not mercado_interessa(info_mercado["nome"]):
                continue

            # NOVO (Onda 2): mercados por tempo são EXCEÇÃO de propósito ao
            # filtro de "sem parcial" - só passam se estiverem na lista
            # explícita MARKET_TYPES_POR_TEMPO acima (nada mais passa por
            # esse bypass, evita abrir brecha sem querer pra outro
            # mercado por tempo qualquer).
            eh_mercado_por_tempo_permitido = chave_periodo in MARKET_TYPES_POR_TEMPO
            if not eh_mercado_por_tempo_permitido and mercado_de_tempo_parcial(info_mercado["nome"]):
                continue

            if tipo_mercado in MARKET_TYPES_TOTAL_DO_JOGO:
                nome_mercado = MARKET_TYPES_TOTAL_DO_JOGO[tipo_mercado]
            elif tipo_mercado in MARKET_TYPES_GOLS_E_MARCA:
                nome_mercado = MARKET_TYPES_GOLS_E_MARCA[tipo_mercado]
            elif tipo_mercado in MARKET_TYPES_MARCA_AMBOS_TEMPOS:
                nome_mercado = MARKET_TYPES_MARCA_AMBOS_TEMPOS[tipo_mercado]
            elif chave_periodo in MARKET_TYPES_POR_TEMPO:
                nome_mercado = MARKET_TYPES_POR_TEMPO[chave_periodo]
            else:
                nome_mercado = info_mercado["nome"]

            linha = info_mercado["handicap"]
            eh_handicap_meia_linha_mercado = eh_handicap_meia_linha(
                tipo_mercado, periodo_mercado, info_mercado.get("handicap")
            )

            outcomes = market_info.get("outcomes", {})
            for outcome_id, outcome_info in outcomes.items():
                direcao = info_mercado["outcomes"].get(outcome_id)
                players = outcome_info.get("players", {})

                # NOVO (20/08/2026): pro mercado de handicap de meia linha,
                # o nome muda por outcome (depende da direção/sinal do
                # time descrito) - substitui o nome_mercado genérico
                # ("Handicap Asiático", vindo cru da OddsPapi) pelo nome
                # real. Ver nome_real_handicap_meia_linha().
                nome_mercado_efetivo = nome_mercado
                if eh_handicap_meia_linha_mercado and direcao in ("1", "2"):
                    nome_mercado_efetivo = nome_real_handicap_meia_linha(linha, direcao)

                for player_key, dados in players.items():
                    if not dados.get("active", True):
                        continue

                    price = dados.get("price")
                    if price is None:
                        continue

                    player_name = dados.get("playerName")
                    jogador_id = None
                    if player_name:
                        jogador_id = get_or_create_jogador(cur, player_name)

                    descricao_mercado = montar_descricao_mercado(
                        nome_mercado_efetivo, linha, direcao, mandante, adversario, nosso_nome, player_name
                    )

                    if jogador_id is not None and ja_existe_odd_jogador(
                        cur, fixture_id_api, jogador_id, descricao_mercado, casa, linha, direcao
                    ):
                        pulados_duplicados += 1
                        continue

                    cur.execute(
                        """INSERT INTO odds (jogo_id, jogador_id, casa_aposta, mercado, valor_odd, linha, direcao)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (jogo_id, jogador_id, casa, descricao_mercado, price, linha, direcao),
                    )
                    salvos += 1

    if pulados_duplicados:
        print(f"  ({pulados_duplicados} odd(s) de jogador não salva(s) de novo - já existiam "
              f"pela outra perspectiva desse mesmo jogo real.)")
    return salvos


def mercado_interessa(nome_mercado):
    nome = nome_mercado.lower()
    return any(palavra in nome for palavra in PALAVRAS_MERCADO_INTERESSE)


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        times_rastreados = buscar_times_rastreados(cur)
        if not times_rastreados:
            print("Nenhum time com oddspapi_participant_id + api_football_team_id "
                  "preenchidos na tabela `times` - nada a fazer.")
            return

        print("Carregando catálogo de mercados...")
        catalogo_mercados = buscar_catalogo_mercados()

        for time_id, time_nome, time_participant_id, time_api_football_id in times_rastreados:
            print(f"\n========== {time_nome} ==========")
            time.sleep(3)  # NOVO: pequena folga entre times, margem extra contra 429

            jogos = buscar_proximos_jogos(time_participant_id)

            if not jogos:
                print(f"Nenhum jogo do {time_nome} nos próximos {DIAS_ANTECEDENCIA} dias.")
                continue

            for jogo in jogos:
                eh_mandante = jogo["participant1Id"] == time_participant_id
                adversario = jogo["participant2Name"] if eh_mandante else jogo["participant1Name"]
                adversario_oddspapi_id = jogo.get("participant2Id") if eh_mandante else jogo.get("participant1Id")
                data_jogo = jogo["startTime"][:10]
                datahora_jogo = jogo["startTime"]  # NOVO: timestamp completo, não só a data

                print(f"\nJogo encontrado: {time_nome} x {adversario} em {data_jogo}")

                if eh_mandante:
                    mandante_nome, visitante_nome = time_nome, adversario
                    mandante_oddspapi_id, visitante_oddspapi_id = time_participant_id, adversario_oddspapi_id
                else:
                    mandante_nome, visitante_nome = adversario, time_nome
                    mandante_oddspapi_id, visitante_oddspapi_id = adversario_oddspapi_id, time_participant_id

                jogo_id = get_or_create_jogo(
                    cur, data_jogo, adversario, eh_mandante,
                    mandante_nome, visitante_nome,
                    mandante_oddspapi_id, visitante_oddspapi_id,
                    time_id, time_api_football_id,
                    datahora_jogo=datahora_jogo,
                )
                conn.commit()

                # NOVO (integração /injuries): só dá pra consultar lesão/
                # suspensão por fixture da API-Football se esse jogo já tem
                # um fixture_id_api CONFIRMADO (positivo) - o valor sintético
                # negativo (fallback de segurança do get_or_create_jogo,
                # usado quando a API-Football ainda não confirmou o fixture
                # real) nunca corresponde a um jogo de verdade lá, então
                # pular direto evita gastar uma chamada de API à toa.
                cur.execute("SELECT fixture_id_api FROM jogos WHERE id = %s", (jogo_id,))
                fixture_id_api_do_jogo = cur.fetchone()[0]
                if fixture_id_api_do_jogo and fixture_id_api_do_jogo > 0:
                    lesoes = buscar_lesoes_suspensos(fixture_id_api_do_jogo)
                    salvar_lesoes_suspensoes(cur, jogo_id, lesoes)
                    conn.commit()
                    if lesoes:
                        # NOVO: o /injuries?fixture=X traz o relatório dos DOIS
                        # times do confronto, não só o nosso - mostra o time de
                        # cada jogador reportado, pra não parecer que todo
                        # mundo é do nosso time quando metade pode ser do
                        # adversário.
                        detalhes = ", ".join(
                            f"{l['jogador_nome'] or '?'} ({l.get('time_nome') or '?'})" for l in lesoes
                        )
                        print(f"  {len(lesoes)} jogador(es) com lesão/suspensão reportada nesse confronto: {detalhes}")

                # NOVO: se a busca de odds falhar pra ESSE jogo específico (ex: 403,
                # jogo fora da cobertura da OddsPapi, competição não suportada),
                # não deixa isso travar o restante do pipeline - avisa e segue pro
                # próximo jogo. Sem isso, um único jogo problemático derrubava o
                # script inteiro e, por consequência, todos os scripts seguintes
                # do cron (motor_padroes, motor_recomendacoes etc.) deixavam de rodar.
                try:
                    dados_odds = buscar_odds(jogo["fixtureId"])
                except Exception as e:
                    print(f"  Aviso: não foi possível buscar odds desse jogo ({e}). Pulando pro próximo.")
                    continue

                # NOVO: só apaga as odds antigas se a resposta nova realmente
                # trouxer algo aproveitável (evita zerar odds boas quando a
                # casa suspende temporariamente os mercados, comum perto/durante
                # o jogo - antes disso, isso deixava o jogo sem NENHUMA odd
                # depois, mesmo tendo tido odds válidas na coleta anterior).
                if existem_odds_utilizaveis(dados_odds):
                    cur.execute("DELETE FROM odds WHERE jogo_id = %s", (jogo_id,))
                    salvos = salvar_odds_do_jogo(
                        cur, jogo_id, dados_odds, catalogo_mercados, eh_mandante, adversario, time_nome,
                        fixture_id_api=fixture_id_api_do_jogo,
                    )
                    print(f"  -> {salvos} odds salvas.")
                else:
                    print("  Aviso: nenhuma odd utilizável nessa resposta (mercados suspensos/vazios) - "
                          "mantendo as odds já salvas desse jogo.")
                conn.commit()

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
