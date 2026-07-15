"""
Script que busca jogos, gols, cartões, substituições e estatísticas agregadas
(posse de bola, escanteios, faltas, passes, finalizações) do Corinthians na
API-Football, e salva tudo no banco Postgres (Railway).

MODO HISTÓRICO: busca todos os jogos das temporadas 2022, 2023 e 2024
(as disponíveis no plano grátis). Pula o que já está no banco - verifica
eventos e estatísticas de forma independente, então é seguro rodar esse
script várias vezes, ele só processa o que falta, sem duplicar nada.

O plano grátis da API-Football tem um limite de 100 requisições por dia.
Cada jogo consome até 2 requisições (eventos + estatísticas). Pode ser
necessário rodar o script em mais de um dia até completar tudo - ele
simplesmente continua de onde parou.

Variáveis de ambiente necessárias (configuradas no Railway, aba "Variables"):
  - API_FOOTBALL_KEY   -> sua chave da API-Football (api-sports.io)
  - DATABASE_URL       -> a URL de conexão do Postgres (o Railway já cria essa
                           automaticamente quando você conecta os dois serviços)
"""

import os
import time
import requests
import psycopg2

# ---------- Configurações ----------
API_KEY = os.environ["API_FOOTBALL_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

LEAGUE_ID = 71                    # Brasileirão Série A
TEAM_ID = 131                      # Corinthians
TEMPORADAS = [2022, 2023, 2024]    # todas as disponíveis no plano grátis
LIMITE_REQUISICOES_DIA = 90        # margem de segurança abaixo do limite de 100/dia

requisicoes_usadas = 0


class LimiteDiarioAtingido(Exception):
    """Levantada quando chegamos perto do limite diário de requisições da API."""
    pass


def chamar_api(endpoint, params, tentativas=3):
    """Faz uma chamada à API contando requisições. Se bater no limite por minuto
    (erro 429), espera um pouco e tenta de novo automaticamente."""
    global requisicoes_usadas
    if requisicoes_usadas >= LIMITE_REQUISICOES_DIA:
        raise LimiteDiarioAtingido(
            f"Limite de segurança de {LIMITE_REQUISICOES_DIA} requisições atingido. "
            "O script vai continuar de onde parou na próxima execução automática."
        )

    for tentativa in range(1, tentativas + 1):
        resp = requests.get(f"{API_BASE}/{endpoint}", headers=HEADERS, params=params)
        requisicoes_usadas += 1

        if resp.status_code == 429:
            espera = 20 * tentativa
            print(f"  Limite por minuto atingido, esperando {espera}s antes de tentar de novo...")
            time.sleep(espera)
            continue

        resp.raise_for_status()
        return resp.json()

    raise RuntimeError(f"Falhou após {tentativas} tentativas por causa do erro 429 (limite por minuto).")


def buscar_jogos(temporada):
    """Busca os jogos do Corinthians numa temporada específica."""
    dados = chamar_api("fixtures", {"league": LEAGUE_ID, "season": temporada, "team": TEAM_ID})

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


def buscar_eventos(fixture_id):
    """Busca gols, cartões e substituições de um jogo específico."""
    dados = chamar_api("fixtures/events", {"fixture": fixture_id})
    return dados["response"]


def buscar_estatisticas(fixture_id):
    """Busca as estatísticas agregadas do jogo (posse, escanteios, faltas etc.),
    uma entrada por lado (mandante/visitante)."""
    dados = chamar_api("fixtures/statistics", {"fixture": fixture_id})
    return dados["response"]


def get_or_create_jogador(cur, nome):
    """Garante que o jogador existe na tabela `jogadores`, retorna o id."""
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


def get_or_create_jogo(cur, fixture):
    """Garante que o jogo existe na tabela `jogos`, retorna o id."""
    fixture_id = fixture["fixture"]["id"]
    cur.execute("SELECT id FROM jogos WHERE id = %s", (fixture_id,))
    row = cur.fetchone()
    if row:
        return row[0]

    data_jogo = fixture["fixture"]["date"][:10]
    mandante_nome = fixture["teams"]["home"]["name"]
    visitante_nome = fixture["teams"]["away"]["name"]
    eh_mandante = fixture["teams"]["home"]["id"] == TEAM_ID
    adversario = visitante_nome if eh_mandante else mandante_nome
    placar_corinthians = (
        fixture["goals"]["home"] if eh_mandante else fixture["goals"]["away"]
    )
    placar_adversario = (
        fixture["goals"]["away"] if eh_mandante else fixture["goals"]["home"]
    )

    cur.execute(
        """
        INSERT INTO jogos (id, data_jogo, adversario, mandante, competicao,
                            placar_corinthians, placar_adversario)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            fixture_id,
            data_jogo,
            adversario,
            eh_mandante,
            "Brasileirão Série A",
            placar_corinthians,
            placar_adversario,
        ),
    )
    return fixture_id


def salvar_eventos(cur, jogo_id, eventos):
    """Classifica cada evento (gol / cartão / substituição) e salva na tabela certa."""
    contagem = {"gols": 0, "cartoes": 0, "substituicoes": 0, "ignorados": 0}

    for ev in eventos:
        tipo = ev["type"]  # "Goal", "Card", "subst" (varia conforme a API)
        minuto = ev["time"]["elapsed"]
        lado = "mandante" if ev["team"]["id"] == TEAM_ID else "visitante"
        jogador_nome = ev["player"]["name"] if ev["player"]["name"] else None

        if not jogador_nome:
            contagem["ignorados"] += 1
            continue

        jogador_id = get_or_create_jogador(cur, jogador_nome)
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
            jogador_entrou_id = (
                get_or_create_jogador(cur, jogador_entrou_nome)
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


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        total_processados = 0
        total_pulados = 0

        for temporada in TEMPORADAS:
            jogos = buscar_jogos(temporada)

            for fixture in jogos:
                fixture_id = fixture["fixture"]["id"]
                home_team_id = fixture["teams"]["home"]["id"]

                falta_eventos = not jogo_ja_processado(cur, fixture_id)
                falta_estatisticas = not jogo_tem_estatisticas(cur, fixture_id)

                if not falta_eventos and not falta_estatisticas:
                    total_pulados += 1
                    continue

                print(f"\nProcessando jogo {fixture_id} (temporada {temporada})...")
                jogo_id = get_or_create_jogo(cur, fixture)

                if falta_eventos:
                    eventos = buscar_eventos(fixture_id)
                    contagem = salvar_eventos(cur, jogo_id, eventos)
                    print(f"  -> {contagem['gols']} gols, {contagem['cartoes']} cartões, "
                          f"{contagem['substituicoes']} substituições salvos "
                          f"({contagem['ignorados']} eventos ignorados).")
                    conn.commit()
                    time.sleep(7)  # respeita o limite de ~10 requisições por minuto do plano grátis

                if falta_estatisticas:
                    estatisticas = buscar_estatisticas(fixture_id)
                    salvos = salvar_estatisticas(cur, jogo_id, estatisticas, home_team_id)
                    print(f"  -> estatísticas de {salvos} lado(s) salvas.")
                    conn.commit()
                    time.sleep(7)

                total_processados += 1

        print(f"\nConcluído! {total_processados} jogos novos processados, "
              f"{total_pulados} já existiam no banco e foram pulados.")

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
