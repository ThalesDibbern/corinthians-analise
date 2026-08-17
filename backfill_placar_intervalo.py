"""
Script PONTUAL (não faz parte do encadeamento diário do cron) - parte 2 da
Onda 2 (Dupla Chance/Ambas Marcam por tempo, Marca em Ambos os Tempos).

Preenche o placar de intervalo (placar_corinthians_intervalo/
placar_adversario_intervalo) de todos os jogos já coletados desde 2022 que
ainda não têm esse dado - precisa rodar DEPOIS de migrar_placar_intervalo.py
(que cria as colunas) e ANTES do motor_padroes.py conseguir calcular
qualquer frequência nova (sem histórico, não tem amostra).

Busca por fixture_id_api DISTINTO (não por jogo_id) - quando os dois times
de um jogo real são rastreados, existem 2 linhas em `jogos` pro mesmo
fixture; buscar por fixture_id_api evita gastar 2 chamadas de API pro
mesmo jogo real, e atualiza as 2 linhas de uma vez só.

Reaproveita chamar_api() de popular_banco.py - mesmo controle de cota
diária (LIMITE_REQUISICOES_DIA) e mesmo tratamento de erro 429 (limite por
minuto) que o resto do projeto já usa. Se bater no limite diário no meio
do processamento, para e deixa o que já foi salvo (commit é feito jogo a
jogo, não tudo de uma vez no final) - só rodar de novo depois que a cota
resetar, que ele continua de onde parou (a busca já pula fixture_id_api
que já têm o dado preenchido).

Rodar manualmente, uma vez (ou mais, se precisar de mais de um dia por
causa da cota):
    python backfill_placar_intervalo.py

Variáveis de ambiente necessárias (mesmas do popular_banco.py):
  - API_FOOTBALL_KEY
  - DATABASE_URL
"""

import os
import psycopg2

from popular_banco import chamar_api, LimiteDiarioAtingido

DATABASE_URL = os.environ["DATABASE_URL"]


def buscar_fixtures_sem_intervalo(cur):
    """Fixture_id_api distintos que já têm placar final salvo (jogo
    concluído) mas ainda não têm placar de intervalo - candidatos ao
    backfill. Um fixture_id_api só, mesmo que existam 2 linhas em `jogos`
    pra ele (times rastreados dos dois lados)."""
    cur.execute(
        """
        SELECT DISTINCT fixture_id_api FROM jogos
        WHERE placar_corinthians_intervalo IS NULL
          AND placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL
        ORDER BY fixture_id_api
        """
    )
    return [row[0] for row in cur.fetchall()]


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        fixture_ids = buscar_fixtures_sem_intervalo(cur)
        total = len(fixture_ids)
        print(f"{total} jogo(s) real(is) sem placar de intervalo ainda.\n")

        atualizados = 0
        sem_dado_na_api = 0
        nao_encontrados = 0

        for i, fixture_id_api in enumerate(fixture_ids, 1):
            try:
                dados = chamar_api("fixtures", {"id": fixture_id_api})
            except LimiteDiarioAtingido as e:
                print(f"\n{e}")
                print(f"Progresso salvo: {atualizados}/{total} atualizados até agora. "
                      "Roda de novo amanhã pra continuar de onde parou.")
                break

            resposta = dados.get("response") or []
            if not resposta:
                nao_encontrados += 1
                print(f"  [{i}/{total}] fixture {fixture_id_api}: não encontrado na API, pulando.")
                continue

            fixture = resposta[0]
            gol_home_intervalo = fixture.get("score", {}).get("halftime", {}).get("home")
            gol_away_intervalo = fixture.get("score", {}).get("halftime", {}).get("away")

            if gol_home_intervalo is None or gol_away_intervalo is None:
                sem_dado_na_api += 1
                print(f"  [{i}/{total}] fixture {fixture_id_api}: API não tem placar de "
                      "intervalo pra esse jogo (fica NULL, sem problema).")
                continue

            cur.execute("SELECT id, mandante FROM jogos WHERE fixture_id_api = %s", (fixture_id_api,))
            linhas = cur.fetchall()
            for jogo_id, mandante in linhas:
                placar_nosso_intervalo = gol_home_intervalo if mandante else gol_away_intervalo
                placar_adv_intervalo = gol_away_intervalo if mandante else gol_home_intervalo
                cur.execute(
                    "UPDATE jogos SET placar_corinthians_intervalo = %s, "
                    "placar_adversario_intervalo = %s WHERE id = %s",
                    (placar_nosso_intervalo, placar_adv_intervalo, jogo_id),
                )
            conn.commit()
            atualizados += 1

            if i % 50 == 0:
                print(f"  [{i}/{total}] ... {atualizados} atualizado(s) até agora.")

        else:
            print(f"\nConcluído! {atualizados} jogo(s) real(is) atualizado(s) com o placar de intervalo.")

        if sem_dado_na_api:
            print(f"{sem_dado_na_api} jogo(s) a API não tinha placar de intervalo disponível "
                  "(ficou NULL - motor_padroes.py ignora esses na amostra, sem quebrar nada).")
        if nao_encontrados:
            print(f"{nao_encontrados} fixture(s) não encontrado(s) na API (pulado(s)).")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante o backfill: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
