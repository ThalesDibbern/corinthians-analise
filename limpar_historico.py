"""
Limpeza automática do histórico - mantém sempre os últimos 50 jogos por
jogador (e os últimos 50 do time), apagando o resto pra não deixar o banco
crescendo indefinidamente.

Um jogo só é apagado quando NENHUM jogador ainda precisa dele (ou seja, já
saiu do "top 50 mais recentes" de todos os jogadores que jogaram nele) E
o time como um todo também não precisa mais dele (últimos 50 jogos do
time, usados nos padrões de escanteio e resultado final). Por segurança,
nunca apaga um jogo que ainda está referenciado em odds, recomendações,
aposta salva ou no recurso de Múltiplas em Destaque.

CORRIGIDO: a checagem de uso não incluía `apostas_salvas` (apesar de já
ter sido documentado como corrigido antes - a correção nunca chegou a
subir no GitHub, ou foi revertida em algum momento; o arquivo real não
tinha a checagem). Um jogo podia ser apagado mesmo com uma aposta salva
ainda dependendo dele - a aposta ficava "pendente" pra sempre, sem
conseguir ser avaliada, porque o `jogo_id` fica dentro do JSONB
`apostas_salvas.pernas`, não numa coluna direta (por isso passava
despercebido, exige `jsonb_array_elements` pra consultar). Corrigido de
verdade agora.

NOVO: também checa `multiplas_candidatas` (via jogos JSONB) e
`historico_multiplas_destaque` (jogo_id direto) antes de apagar - um jogo
que ainda é usado pelo recurso de Múltiplas em Destaque (candidata ainda
não avaliada, ou já faz parte do top-5 de algum jogo) não pode ser
apagado, senão a chave estrangeira de historico_multiplas_destaque.jogo_id
quebraria (ou, no caso de multiplas_candidatas, a avaliação nunca
conseguiria terminar).

NOVO: comprime `historico_multiplas_destaque` - mantém detalhe completo
(casa/descrição/odd/jogos) só nas 2 rodadas mais recentes que já têm
Múltiplas em Destaque calculadas; a partir da 3ª rodada mais recente, só
sobra `probabilidade_combinada`/`resultado`/`rodada`/`jogo_id` (o
suficiente pro resumo de calibração do /historico continuar funcionando,
sem guardar quase nada a longo prazo).

Feito pra rodar periodicamente (dentro do encadeamento diário) - é uma
tarefa leve, não faz nenhuma chamada de API externa, só limpeza no banco.

Variáveis de ambiente:
  - DATABASE_URL -> a URL de conexão do Postgres (mesma usada nos outros scripts)
"""

import json
import os
import re
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]
JANELA_MAXIMA_DE_JOGOS = 50

# quantas rodadas (com Múltiplas em Destaque já calculadas) mantêm o
# detalhe completo antes de comprimir
RODADAS_COM_DETALHE_COMPLETO = 2

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
    (histórico ou ativas), aposta salva, ou no recurso de Múltiplas em
    Destaque - se estiver, não apaga por segurança."""
    cur.execute(
        """
        SELECT 1 FROM odds WHERE jogo_id = %s
        UNION SELECT 1 FROM recomendacoes WHERE jogo_id = %s
        UNION SELECT 1 FROM historico_recomendacoes WHERE jogo_id = %s
        UNION SELECT 1 FROM historico_multiplas_destaque WHERE jogo_id = %s
        UNION SELECT 1 FROM multiplas_candidatas WHERE jogos @> %s::jsonb
        UNION SELECT 1 FROM apostas_salvas a
            WHERE EXISTS (
                SELECT 1 FROM jsonb_array_elements(a.pernas) AS perna
                WHERE (perna->>'jogo_id')::int = %s
            )
        LIMIT 1
        """,
        (jogo_id, jogo_id, jogo_id, jogo_id, json.dumps([{"jogo_id": jogo_id}]), jogo_id),
    )
    return cur.fetchone() is not None


def apagar_jogo(cur, jogo_id):
    for tabela in TABELAS_DEPENDENTES:
        cur.execute(f"DELETE FROM {tabela} WHERE jogo_id = %s", (jogo_id,))
    cur.execute("DELETE FROM jogos WHERE id = %s", (jogo_id,))


def _numero_rodada(rodada):
    """Extrai o número de dentro de "Regular Season - 20" -> 20. Rodada
    sem número reconhecível vai pro fim da lista (nunca comprimida por
    engano antes de uma rodada normal)."""
    m = re.search(r"(\d+)", rodada or "")
    return int(m.group(1)) if m else -1


def comprimir_multiplas_destaque_antigas(cur):
    """Mantém detalhe completo só nas RODADAS_COM_DETALHE_COMPLETO rodadas
    mais recentes que já têm Múltiplas em Destaque calculadas - a partir
    da rodada seguinte (mais antiga), os campos pesados viram NULL,
    sobrando só probabilidade_combinada/resultado/rodada/jogo_id."""
    cur.execute(
        "SELECT DISTINCT rodada FROM historico_multiplas_destaque "
        "WHERE rodada IS NOT NULL AND casa_aposta IS NOT NULL"
    )
    rodadas = [row[0] for row in cur.fetchall()]
    if len(rodadas) <= RODADAS_COM_DETALHE_COMPLETO:
        return 0

    rodadas.sort(key=_numero_rodada, reverse=True)
    rodadas_para_comprimir = rodadas[RODADAS_COM_DETALHE_COMPLETO:]

    cur.execute(
        """
        UPDATE historico_multiplas_destaque
        SET casa_aposta = NULL, descricao = NULL, odd_combinada = NULL, jogos = NULL
        WHERE rodada = ANY(%s) AND casa_aposta IS NOT NULL
        """,
        (rodadas_para_comprimir,),
    )
    return cur.rowcount


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
        else:
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
                      "por ainda estarem referenciados em odds/recomendações/aposta salva/"
                      "Múltiplas em Destaque.)")

        comprimidas = comprimir_multiplas_destaque_antigas(cur)
        conn.commit()
        if comprimidas:
            print(f"\n{comprimidas} linha(s) de Múltiplas em Destaque comprimida(s) "
                  f"(fora das {RODADAS_COM_DETALHE_COMPLETO} rodadas mais recentes) - "
                  "só probabilidade/resultado mantidos.")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
