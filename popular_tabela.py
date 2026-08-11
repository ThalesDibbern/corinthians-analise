"""
Fase A (zona da tabela / posição por rodada) - coleta LEVE de todo o
calendário do Brasileirão Série A, temporada por temporada: placar, data,
mandante, visitante e rodada de TODO jogo, não só dos times rastreados.

Diferença importante pro popular_banco.py: esse aqui NÃO busca eventos,
estatísticas, escalação ou jogador - só o resultado básico de cada jogo,
o suficiente pra reconstruir a posição de qualquer time na tabela em
qualquer rodada do passado (impossível de outro jeito, já que a
API-Football não oferece "tabela histórica por rodada" pronta, só a
atual/final - conferido na documentação oficial deles).

Extremamente barato de cota: como o endpoint /fixtures aceita filtrar só
por liga+temporada (sem precisar de time nenhum), UMA chamada já traz o
calendário inteiro daquela temporada (~380 jogos, 20 times) de uma vez -
bem diferente do popular_banco.py, que precisa de várias chamadas por
jogo (eventos, estatísticas, escalação) só pros times rastreados.

Salva em `jogos_liga` (ver migrar_jogos_liga.py - precisa rodar essa
migração antes, uma vez).

Variáveis de ambiente necessárias:
  - API_FOOTBALL_KEY -> mesma chave usada pelo popular_banco.py
  - DATABASE_URL     -> a URL de conexão do Postgres
"""

import os
import re
import time
import requests
import psycopg2

API_KEY = os.environ["API_FOOTBALL_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

LEAGUE_ID = 71  # Brasileirão Série A
TEMPORADAS = [2022, 2023, 2024, 2025, 2026]


def chamar_api(endpoint, params, tentativas=3):
    """Mesma lógica de retry do popular_banco.py (429 = limite por minuto,
    espera e tenta de novo) - duplicada de propósito aqui: é uma função
    pequena e autocontida (só faz um GET com retry), bem diferente do
    tipo de lógica de negócio complexa que já causou bug grande quando
    duplicada nesse projeto (ver combinacoes.py) - o risco de divergência
    aqui é baixo o suficiente pra não justificar um módulo compartilhado
    só pra isso."""
    for tentativa in range(1, tentativas + 1):
        resp = requests.get(f"{API_BASE}/{endpoint}", headers=HEADERS, params=params)
        if resp.status_code == 429:
            espera = 20 * tentativa
            print(f"  Limite por minuto atingido, esperando {espera}s antes de tentar de novo...")
            time.sleep(espera)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Falhou após {tentativas} tentativas por causa do erro 429 (limite por minuto).")


def _numero_rodada(rodada):
    """Extrai o número de dentro de "Regular Season - 20" -> 20. Mesma
    lógica usada em limpar_historico.py - mantida separada aqui pelo
    mesmo motivo do chamar_api acima (utilitário pequeno, baixo risco)."""
    m = re.search(r"(\d+)", rodada or "")
    return int(m.group(1)) if m else None


def buscar_calendario_temporada(temporada):
    """Busca TODOS os jogos do Brasileirão numa temporada, de uma vez -
    não filtra por time, traz o campeonato inteiro (~380 jogos numa liga
    de 20 times)."""
    dados = chamar_api("fixtures", {"league": LEAGUE_ID, "season": temporada})
    if dados.get("errors"):
        print(f"  Aviso da API para temporada {temporada}: {dados['errors']}")
        return []
    jogos = dados["response"]
    print(f"Temporada {temporada}: {len(jogos)} jogos encontrados no calendário.")
    return jogos


def salvar_jogo_liga(cur, fixture):
    fixture_id = fixture["fixture"]["id"]
    data_jogo = fixture["fixture"]["date"][:10]
    status = fixture["fixture"]["status"]["short"]
    rodada = fixture["league"].get("round")
    rodada_numero = _numero_rodada(rodada)
    temporada = fixture["league"]["season"]

    mandante_id = fixture["teams"]["home"]["id"]
    mandante_nome = fixture["teams"]["home"]["name"]
    visitante_id = fixture["teams"]["away"]["id"]
    visitante_nome = fixture["teams"]["away"]["name"]

    placar_mandante = fixture["goals"]["home"]
    placar_visitante = fixture["goals"]["away"]

    cur.execute(
        """
        INSERT INTO jogos_liga
            (fixture_id_api, temporada, rodada, rodada_numero, data_jogo,
             mandante_api_id, mandante_nome, visitante_api_id, visitante_nome,
             placar_mandante, placar_visitante, status, atualizado_em)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (fixture_id_api) DO UPDATE SET
            rodada = EXCLUDED.rodada,
            rodada_numero = EXCLUDED.rodada_numero,
            placar_mandante = EXCLUDED.placar_mandante,
            placar_visitante = EXCLUDED.placar_visitante,
            status = EXCLUDED.status,
            atualizado_em = NOW()
        """,
        (
            fixture_id, temporada, rodada, rodada_numero, data_jogo,
            mandante_id, mandante_nome, visitante_id, visitante_nome,
            placar_mandante, placar_visitante, status,
        ),
    )


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        total_salvos = 0
        for temporada in TEMPORADAS:
            jogos = buscar_calendario_temporada(temporada)
            for fixture in jogos:
                salvar_jogo_liga(cur, fixture)
                total_salvos += 1
            conn.commit()

        print(f"\nConcluído! {total_salvos} jogo(s) processado(s) (inserido(s) ou atualizado(s)) em jogos_liga.")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
