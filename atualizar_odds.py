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
PARTICIPANT_ID = 1957   # Corinthians
TEAM_ID_API_FOOTBALL = 131  # Corinthians na API-Football (id diferente do da OddsPapi)
BOOKMAKERS = "superbet.bet.br"  # só Superbet por enquanto (plano pago com Player Props)
DIAS_ANTECEDENCIA = 2   # busca odds de jogos que acontecem em até X dias

# Palavras usadas para filtrar quais mercados nos interessam. Comparação é
# feita sem diferenciar maiúsculas.
# "tempo completo" captura especificamente o mercado "Resultado Tempo Completo"
# (confirmado manualmente na OddsPapi) - sem pegar por engano os mercados de
# resultado do 1º/2º tempo, que têm nomes parecidos mas não têm "completo".
PALAVRAS_MERCADO_INTERESSE = [
    "card", "cartão", "cartao", "corner", "escanteio",
    "falta", "desarme", "chute", "impediment", "tempo completo",
]


def buscar_catalogo_mercados():
    """Busca a lista de todos os mercados existentes, incluindo a linha
    (handicap) de cada mercado e o nome de cada outcome (Mais/Menos/Sim/Não/
    0/1+/2+), para conseguirmos interpretar as odds corretamente depois."""
    resp = requests.get(
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
            "outcomes": {str(o["outcomeId"]): o["outcomeName"] for o in m.get("outcomes", [])},
        }
    return catalogo


def buscar_proximos_jogos():
    """Busca jogos do Corinthians no Brasileirão e filtra os que acontecem
    dentro da janela de antecedência definida."""
    resp = requests.get(
        f"{API_BASE}/fixtures",
        params={
            "tournamentId": TOURNAMENT_ID,
            "sportId": SPORT_ID,
            "participantId": PARTICIPANT_ID,
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

    resp = requests.get(
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


def buscar_fixture_api_football(data_jogo):
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
            params={"team": TEAM_ID_API_FOOTBALL, "date": data_jogo},
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
    esses ids preenchidos."""
    mandante_id = get_or_create_time(cur, mandante_oddspapi_id, mandante_nome)
    visitante_id = get_or_create_time(cur, visitante_oddspapi_id, visitante_nome)

    cur.execute(
        "SELECT id, arbitro, datahora_jogo FROM jogos "
        "WHERE data_jogo = %s AND mandante_id = %s AND visitante_id = %s",
        (data_jogo, mandante_id, visitante_id),
    )
    row = cur.fetchone()

    if not row:
        # fallback de segurança: jogo antigo que ainda não tem mandante_id/
        # visitante_id preenchido (não deveria mais acontecer, mas evita
        # duplicar um jogo legítimo enquanto algum registro assim existir)
        cur.execute(
            "SELECT id, arbitro, datahora_jogo FROM jogos "
            "WHERE data_jogo = %s AND adversario = %s AND (mandante_id IS NULL OR visitante_id IS NULL)",
            (data_jogo, adversario),
        )
        row = cur.fetchone()

    if row:
        jogo_id, arbitro_salvo, datahora_salva = row
        if arbitro_salvo is None:
            fixture = buscar_fixture_api_football(data_jogo)
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

    fixture = buscar_fixture_api_football(data_jogo)
    arbitro = fixture["fixture"].get("referee") if fixture else None
    fixture_id_real = fixture["fixture"]["id"] if fixture else None
    if arbitro:
        print(f"  Árbitro confirmado: {arbitro}")

    if fixture_id_real is not None:
        # tenta usar o ID real da API-Football, com uma rede de segurança
        # (savepoint) caso esse ID já esteja em uso por outro caminho
        cur.execute("SAVEPOINT antes_de_inserir_jogo")
        try:
            cur.execute(
                """INSERT INTO jogos (id, data_jogo, datahora_jogo, adversario, mandante, competicao,
                                       arbitro, mandante_id, visitante_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (fixture_id_real, data_jogo, datahora_jogo, adversario, mandante, "Brasileirão Série A",
                 arbitro, mandante_id, visitante_id),
            )
            return cur.fetchone()[0]
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT antes_de_inserir_jogo")
            print(f"  Aviso: ID real {fixture_id_real} já em uso por outro registro - "
                  "criando esse jogo com ID automático (verificar depois se não duplicou).")

    cur.execute(
        """INSERT INTO jogos (data_jogo, datahora_jogo, adversario, mandante, competicao, arbitro,
                               mandante_id, visitante_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (data_jogo, datahora_jogo, adversario, mandante, "Brasileirão Série A", arbitro,
         mandante_id, visitante_id),
    )
    return cur.fetchone()[0]


def nome_time_por_posicao(mandante, adversario, posicao):
    """NOVO: traduz "Equipe 1"/"Equipe 2" (nomenclatura genérica que a
    OddsPapi usa pra mercados de time) pro nome real do time. Convenção da
    OddsPapi: Equipe 1 = mandante do jogo, Equipe 2 = visitante."""
    corinthians_eh_equipe_1 = mandante
    if posicao == "1":
        return "Corinthians" if corinthians_eh_equipe_1 else adversario
    return adversario if corinthians_eh_equipe_1 else "Corinthians"


def montar_descricao_mercado(nome_mercado, linha, direcao, mandante, adversario, player_name=None):
    """NOVO: monta uma descrição legível e ESPECÍFICA da odd, resolvendo
    "Equipe 1"/"Equipe 2" pro nome real do time e incluindo a linha
    (handicap) e a direção escolhida (Mais/Menos/Sim/Não).

    Sem isso, duas odds do mesmo mercado com linhas diferentes (ex: "mais de
    3.5 escanteios" e "mais de 5.5 escanteios") ficavam com a mesma descrição
    salva, e o motor de combinações não tinha como saber que eram apostas
    diferentes (ou que media a mesma coisa em pontos de corte diferentes)."""
    descricao = nome_mercado

    def substituir(match):
        return nome_time_por_posicao(mandante, adversario, match.group(1))

    descricao = re.sub(r"Equipe\s*([12])", substituir, descricao, flags=re.IGNORECASE)

    direcao_normalizada = (direcao or "").strip().lower()
    if linha is not None and direcao_normalizada in ("mais", "menos"):
        detalhe = f"{direcao} de {linha}"
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


def mercado_de_tempo_parcial(nome_mercado):
    """NOVO: a OddsPapi também retorna versões de Primeiro Tempo/Segundo
    Tempo desses mesmos mercados (ex: "Cartões - Mais/Menos Primeiro
    Tempo") - fora do escopo do projeto hoje (só olhamos o jogo completo).
    "Tempo Completo" (usado no mercado de resultado final) não é excluído."""
    nome = nome_mercado.lower()
    if "tempo completo" in nome:
        return False
    return "primeiro tempo" in nome or "segundo tempo" in nome


def salvar_odds_do_jogo(cur, jogo_id, dados_odds, catalogo_mercados, mandante, adversario):
    """Percorre as odds de todas as casas/mercados retornados e salva só os
    mercados de interesse (cartão de jogador + escanteios do time), incluindo
    a linha (handicap) e a direção (Mais/Menos/Sim/Não) de cada odd.

    NOVO: a descrição agora é montada com montar_descricao_mercado, que
    traduz o time e inclui linha + direção (ver docstring dela).
    NOVO (mercados de total do jogo): mercados com marketType em
    MARKET_TYPES_TOTAL_DO_JOGO (escanteios/cartões somando os dois times)
    usam um nome fixo e inequívoco, em vez do nome genérico do catálogo -
    evita confundir com o mercado por time (que também contém a palavra
    "escanteio"/"cartão", mas se refere só a um lado)."""
    salvos = 0
    bookmaker_odds = dados_odds.get("bookmakerOdds", {})

    for casa, info_casa in bookmaker_odds.items():
        if info_casa.get("suspended"):
            continue

        markets = info_casa.get("markets", {})
        for market_id, market_info in markets.items():
            info_mercado = catalogo_mercados.get(market_id)
            if not info_mercado or not mercado_interessa(info_mercado["nome"]):
                continue

            if mercado_de_tempo_parcial(info_mercado["nome"]):
                continue

            tipo_mercado = info_mercado.get("tipo")
            if tipo_mercado in MARKET_TYPES_TOTAL_DO_JOGO:
                nome_mercado = MARKET_TYPES_TOTAL_DO_JOGO[tipo_mercado]
            else:
                nome_mercado = info_mercado["nome"]

            linha = info_mercado["handicap"]

            outcomes = market_info.get("outcomes", {})
            for outcome_id, outcome_info in outcomes.items():
                direcao = info_mercado["outcomes"].get(outcome_id)
                players = outcome_info.get("players", {})

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
                        nome_mercado, linha, direcao, mandante, adversario, player_name
                    )

                    cur.execute(
                        """INSERT INTO odds (jogo_id, jogador_id, casa_aposta, mercado, valor_odd, linha, direcao)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (jogo_id, jogador_id, casa, descricao_mercado, price, linha, direcao),
                    )
                    salvos += 1

    return salvos


def mercado_interessa(nome_mercado):
    nome = nome_mercado.lower()
    return any(palavra in nome for palavra in PALAVRAS_MERCADO_INTERESSE)


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        jogos = buscar_proximos_jogos()

        if not jogos:
            print(f"Nenhum jogo do Corinthians nos próximos {DIAS_ANTECEDENCIA} dias. Nada a fazer hoje.")
            return

        print("Carregando catálogo de mercados...")
        catalogo_mercados = buscar_catalogo_mercados()

        for jogo in jogos:
            eh_mandante = jogo["participant1Id"] == PARTICIPANT_ID
            adversario = jogo["participant2Name"] if eh_mandante else jogo["participant1Name"]
            adversario_oddspapi_id = jogo.get("participant2Id") if eh_mandante else jogo.get("participant1Id")
            data_jogo = jogo["startTime"][:10]
            datahora_jogo = jogo["startTime"]  # NOVO: timestamp completo, não só a data

            print(f"\nJogo encontrado: Corinthians x {adversario} em {data_jogo}")

            if eh_mandante:
                mandante_nome, visitante_nome = "Corinthians", adversario
                mandante_oddspapi_id, visitante_oddspapi_id = PARTICIPANT_ID, adversario_oddspapi_id
            else:
                mandante_nome, visitante_nome = adversario, "Corinthians"
                mandante_oddspapi_id, visitante_oddspapi_id = adversario_oddspapi_id, PARTICIPANT_ID

            jogo_id = get_or_create_jogo(
                cur, data_jogo, adversario, eh_mandante,
                mandante_nome, visitante_nome,
                mandante_oddspapi_id, visitante_oddspapi_id,
                datahora_jogo=datahora_jogo,
            )
            conn.commit()

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

            # NOVO: apaga as odds antigas desse jogo antes de salvar as novas.
            # Sem isso, cada execução do cron (dentro da janela de 2 dias)
            # inseria de novo as mesmas odds com o preço daquele dia,
            # acumulando "duplicatas" com odd/probabilidade levemente
            # diferentes de uma execução pra outra.
            cur.execute("DELETE FROM odds WHERE jogo_id = %s", (jogo_id,))

            salvos = salvar_odds_do_jogo(
                cur, jogo_id, dados_odds, catalogo_mercados, eh_mandante, adversario
            )

            print(f"  -> {salvos} odds salvas.")
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
