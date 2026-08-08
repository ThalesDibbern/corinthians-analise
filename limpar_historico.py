"""
Limpeza automática do histórico - mantém sempre os últimos 50 jogos por
jogador (e os últimos 50 do time), apagando o resto pra não deixar o banco
crescendo indefinidamente.

Um jogo só é apagado quando NENHUM jogador ainda precisa dele (ou seja, já
saiu do "top 50 mais recentes" de todos os jogadores que jogaram nele) E
o time como um todo também não precisa mais dele (últimos 50 jogos do
Corinthians, usados nos padrões de escanteio e resultado final). Por
segurança, nunca apaga um jogo que ainda está referenciado em odds ou
recomendações.

Feito pra rodar periodicamente (dentro do encadeamento diário) - é uma
tarefa leve, não faz nenhuma chamada de API externa, só limpeza no banco.

Variáveis de ambiente:
  - DATABASE_URL -> a URL de conexão do Postgres (mesma usada nos outros scripts)
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]
JANELA_MAXIMA_DE_JOGOS = 50

# tabelas "filhas" que precisam ser limpas antes de conseguir apagar o jogo
TABELAS_DEPENDENTES = [
    "gols", "cartoes", "substituicoes", "estatisticas_jogo",
    "jogador_estatisticas_jogo", "escalacoes",
]


def buscar_jogos_necessarios(cur):
    """Monta o conjunto de jogos que ainda são necessários: os últimos 50
    de cada jogador, mais os últimos 50 do time como um todo."""
    necessarios = set()

    cur.execute("SELECT DISTINCT jogador_id FROM jogador_estatisticas_jogo")
    jogadores = [row[0] for row in cur.fetchall()]

    for jogador_id in jogadores:
        cur.execute(
            """
            SELECT jeg.jogo_id
            FROM jogador_estatisticas_jogo jeg
            JOIN jogos j ON j.id = jeg.jogo_id
            WHERE jeg.jogador_id = %s
            ORDER BY j.data_jogo DESC
            LIMIT %s
            """,
            (jogador_id, JANELA_MAXIMA_DE_JOGOS),
        )
        necessarios.update(row[0] for row in cur.fetchall())

    cur.execute(
        "SELECT id FROM jogos ORDER BY data_jogo DESC LIMIT %s",
        (JANELA_MAXIMA_DE_JOGOS,),
    )
    necessarios.update(row[0] for row in cur.fetchall())

    return necessarios


def buscar_jogos_candidatos(cur, necessarios):
    """Jogos que já aconteceram e não estão mais no conjunto de necessários."""
    cur.execute("SELECT id FROM jogos WHERE data_jogo < CURRENT_DATE")
    todos_passados = [row[0] for row in cur.fetchall()]
    return [jid for jid in todos_passados if jid not in necessarios]


def jogo_esta_em_uso(cur, jogo_id):
    """Verifica se o jogo ainda está referenciado em odds/recomendações
    (histórico ou ativas) OU numa aposta salva por algum usuário - se
    estiver, não apaga por segurança.
    CORRIGIDO: antes não checava `apostas_salvas` - um jogo podia ser
    apagado mesmo com uma aposta salva ainda dependendo dele (o `jogo_id`
    fica dentro do JSONB `pernas`, não numa coluna direta, por isso
    precisa de uma consulta separada pra essa tabela)."""
    cur.execute(
        """
        SELECT 1 FROM odds WHERE jogo_id = %s
        UNION SELECT 1 FROM recomendacoes WHERE jogo_id = %s
        UNION SELECT 1 FROM historico_recomendacoes WHERE jogo_id = %s
        LIMIT 1
        """,
        (jogo_id, jogo_id, jogo_id),
    )
    if cur.fetchone() is not None:
        return True

    cur.execute(
        """
        SELECT 1 FROM apostas_salvas a
        WHERE EXISTS (
            SELECT 1 FROM jsonb_array_elements(a.pernas) AS perna
            WHERE (perna->>'jogo_id')::int = %s
        )
        LIMIT 1
        """,
        (jogo_id,),
    )
    return cur.fetchone() is not None


def apagar_jogo(cur, jogo_id):
    for tabela in TABELAS_DEPENDENTES:
        cur.execute(f"DELETE FROM {tabela} WHERE jogo_id = %s", (jogo_id,))
    cur.execute("DELETE FROM jogos WHERE id = %s", (jogo_id,))


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        necessarios = buscar_jogos_necessarios(cur)
        candidatos = buscar_jogos_candidatos(cur, necessarios)

        if not candidatos:
            print("Nenhum jogo precisa ser removido no momento "
                  f"(todos ainda estão dentro do limite de {JANELA_MAXIMA_DE_JOGOS} jogos).")
            return

        apagados = 0
        mantidos_por_uso = 0

        for jogo_id in candidatos:
            if jogo_esta_em_uso(cur, jogo_id):
                mantidos_por_uso += 1
                continue

            apagar_jogo(cur, jogo_id)
            apagados += 1
            conn.commit()

        print(f"Concluído! {apagados} jogo(s) antigo(s) removido(s) do banco.")
        if mantidos_por_uso:
            print(f"({mantidos_por_uso} jogo(s) seriam removíveis, mas foram mantidos "
                  "por ainda estarem referenciados em odds/recomendações.)")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
