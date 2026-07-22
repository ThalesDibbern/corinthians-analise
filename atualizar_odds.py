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


def buscar_arbitro_api_football(data_jogo):
    """NOVO: consulta a API-Football pra descobrir o árbitro escalado pro
    jogo do Corinthians numa data específica. Retorna None se a chave não
    estiver configurada, se a API ainda não tiver o árbitro definido, ou se
    a consulta falhar por qualquer motivo (não deve travar o script todo -
    o árbitro é um dado complementar, não essencial)."""
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
        return jogos[0]["fixture"].get("referee")
    except Exception as e:
        print(f"  Aviso: não foi possível buscar o árbitro ({e}). Seguindo sem esse dado.")
        return None


def get_or_create_jogador(cur, nome):
    cur.execute("SELECT id FROM jogadores WHERE nome = %s", (nome,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO jogadores (nome) VALUES (%s) RETURNING id", (nome,))
    return cur.fetchone()[0]


def get_or_create_jogo(cur, data_jogo, adversario, mandante):
    """Encontra (ou cria) o jogo pela combinação data + adversário - assim
    esse script funciona independente do jogo já existir vindo do outro
    script (API-Football) ou ser inteiramente novo (jogo futuro).
    NOVO: se o jogo já existe mas ainda não tem árbitro salvo, tenta buscar
    e atualizar (a escalação pode ter sido confirmada entre uma execução e
    outra do cron, já que ambas rodam na mesma janela de 2 dias)."""
    cur.execute(
        "SELECT id, arbitro FROM jogos WHERE data_jogo = %s AND adversario = %s",
        (data_jogo, adversario),
    )
    row = cur.fetchone()
    if row:
        jogo_id, arbitro_salvo = row
        if arbitro_salvo is None:
            arbitro = buscar_arbitro_api_football(data_jogo)
            if arbitro:
                cur.execute("UPDATE jogos SET arbitro = %s WHERE id = %s", (arbitro, jogo_id))
                print(f"  Árbitro confirmado: {arbitro}")
        return jogo_id

    arbitro = buscar_arbitro_api_football(data_jogo)
    if arbitro:
        print(f"  Árbitro confirmado: {arbitro}")

    cur.execute(
        """INSERT INTO jogos (data_jogo, adversario, mandante, competicao, arbitro)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (data_jogo, adversario, mandante, "Brasileirão Série A", arbitro),
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

    if linha is not None and direcao:
        detalhe = f"{direcao} de {linha}"
    elif linha is not None:
        detalhe = str(linha)
    else:
        detalhe = direcao

    if detalhe:
        descricao = f"{descricao} - {detalhe}"

    if player_name:
        descricao = f"{descricao} - {player_name}"

    return descricao


def salvar_odds_do_jogo(cur, jogo_id, dados_odds, catalogo_mercados, mandante, adversario):
    """Percorre as odds de todas as casas/mercados retornados e salva só os
    mercados de interesse (cartão de jogador + escanteios do time), incluindo
    a linha (handicap) e a direção (Mais/Menos/Sim/Não) de cada odd.

    NOVO: a descrição agora é montada com montar_descricao_mercado, que
    traduz o time e inclui linha + direção (ver docstring dela)."""
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
            data_jogo = jogo["startTime"][:10]

            print(f"\nJogo encontrado: Corinthians x {adversario} em {data_jogo}")

            jogo_id = get_or_create_jogo(cur, data_jogo, adversario, eh_mandante)
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
