"""
Script de migração (rodar 1x, manualmente) - popula a tabela `times` com
um registro pra cada adversário já visto em `jogos.adversario`, e preenche
as colunas novas `jogos.mandante_id` / `jogos.visitante_id` a partir do
texto que já existia (adversario + mandante boolean).

Os adversários entram na tabela `times` só com o nome por enquanto (sem
api_football_team_id / oddspapi_participant_id) - se algum deles vier a ser
adicionado como time "principal" do projeto no futuro (ex: pra expandir pra
outros clubes do Brasileirão), os ids reais das APIs podem ser completados
depois, do mesmo jeito que já é feito hoje pro api_football_id de jogadores
(busca por id, cai pra nome se não achar).

Seguro rodar mais de uma vez - só preenche o que ainda estiver NULL.

Variáveis de ambiente necessárias:
  - DATABASE_URL -> a URL de conexão do Postgres (mesma usada nos outros scripts)
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

NOME_TIME_PRINCIPAL = "Corinthians"


def get_or_create_time(cur, nome):
    cur.execute("SELECT id FROM times WHERE nome = %s", (nome,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO times (nome) VALUES (%s) RETURNING id", (nome,))
    return cur.fetchone()[0]


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        cur.execute("SELECT id FROM times WHERE nome = %s", (NOME_TIME_PRINCIPAL,))
        row = cur.fetchone()
        if not row:
            raise RuntimeError(
                f"Time '{NOME_TIME_PRINCIPAL}' não encontrado na tabela times - "
                "rode a migracao_times.sql antes desse script."
            )
        corinthians_id = row[0]

        cur.execute(
            "SELECT id, adversario, mandante FROM jogos WHERE mandante_id IS NULL OR visitante_id IS NULL"
        )
        jogos_pendentes = cur.fetchall()

        if not jogos_pendentes:
            print("Nenhum jogo pendente de backfill - todos já têm mandante_id/visitante_id preenchidos.")
            return

        atualizados = 0
        times_criados = set()

        for jogo_id, adversario, eh_mandante in jogos_pendentes:
            if not adversario:
                print(f"  Aviso: jogo {jogo_id} sem adversário salvo, pulando.")
                continue

            adversario_id = get_or_create_time(cur, adversario)
            if adversario not in times_criados:
                times_criados.add(adversario)

            if eh_mandante:
                mandante_id, visitante_id = corinthians_id, adversario_id
            else:
                mandante_id, visitante_id = adversario_id, corinthians_id

            cur.execute(
                "UPDATE jogos SET mandante_id = %s, visitante_id = %s WHERE id = %s",
                (mandante_id, visitante_id, jogo_id),
            )
            atualizados += 1

        conn.commit()
        print(f"Concluído! {atualizados} jogo(s) atualizado(s) com mandante_id/visitante_id.")
        print(f"{len(times_criados)} time(s) adversário(s) diferente(s) cadastrado(s) em `times`.")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
