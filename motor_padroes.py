"""
Motor de padrões - Cartões, faltas, desarmes, chutes e impedimentos de
jogador + Escanteios do time + NOVO: perfil de cada árbitro.

Para cada jogador com dados suficientes, calcula a frequência histórica de
cada padrão (ex: recebeu cartão, cometeu falta, teve X+ desarmes...). Para
o time, calcula a frequência de passar de cada linha de escanteios testada.
Para cada árbitro, calcula a média de cartões e faltas nos jogos que ele
apitou (some os dois times, não só o Corinthians - a ideia é capturar o
"jeito de apitar" dele, que vale pro jogo inteiro).
Todos considerando os últimos 50 jogos disponíveis (ou todos os jogos
apitados, no caso do árbitro).

Feito para rodar automaticamente todo dia (depois que o script de coleta
de dados já rodou), recalculando os padrões com os dados mais recentes.

Variáveis de ambiente necessárias:
  - DATABASE_URL -> a URL de conexão do Postgres (mesma usada nos outros scripts)
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

JOGOS_MINIMOS_PARA_ANALISAR = 5   # não vale a pena calcular padrão com poucos jogos
JANELA_MAXIMA_DE_JOGOS = 50       # olha no máximo os últimos 50 jogos

# linhas de escanteio testadas, no mesmo padrão que as casas de aposta usam
# (mercados "mais de X.5 escanteios")
LINHAS_ESCANTEIO = [3.5, 4.5, 5.5, 6.5, 7.5]

# novos padrões por jogador: nome do tipo -> (coluna no banco, linhas testadas)
PADROES_LINHA_JOGADOR = {
    "falta_cometida": ("faltas_cometidas", [0.5, 1.5, 2.5]),
    "desarme": ("desarmes", [0.5, 1.5, 2.5]),
    "chute_no_gol": ("chutes_no_gol", [0.5, 1.5]),
}

# padrões simples (sim/não teve pelo menos 1 no jogo)
PADROES_FREQUENCIA_JOGADOR = {
    "impedimento": "impedimentos",
}

# ajuste do fator de árbitro: limita o quanto a probabilidade de um jogador
# pode ser puxada pra cima/baixo com base no árbitro, pra não deixar o
# sistema "confiar demais" numa amostra que ainda é pequena por árbitro
FATOR_ARBITRO_MINIMO = 0.85
FATOR_ARBITRO_MAXIMO = 1.15


def calcular_padroes_cartao(cur):
    """Para cada jogador, olha seus últimos jogos e calcula a frequência de cartão."""
    cur.execute("SELECT id, nome FROM jogadores")
    jogadores = cur.fetchall()

    resultados = []

    for jogador_id, nome in jogadores:
        cur.execute(
            """
            SELECT jeg.cartao_amarelo, jeg.cartao_vermelho
            FROM jogador_estatisticas_jogo jeg
            JOIN jogos j ON j.id = jeg.jogo_id
            WHERE jeg.jogador_id = %s
            ORDER BY j.data_jogo DESC
            LIMIT %s
            """,
            (jogador_id, JANELA_MAXIMA_DE_JOGOS),
        )
        jogos_do_jogador = cur.fetchall()

        jogos_analisados = len(jogos_do_jogador)
        if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
            continue  # dados de menos pra confiar no padrão ainda

        jogos_com_cartao = sum(
            1 for amarelo, vermelho in jogos_do_jogador
            if (amarelo or 0) > 0 or (vermelho or 0) > 0
        )
        frequencia = round(100 * jogos_com_cartao / jogos_analisados, 2)

        resultados.append((jogador_id, nome, jogos_analisados, jogos_com_cartao, frequencia))

    return resultados


def salvar_padroes(cur, resultados):
    for jogador_id, nome, jogos_analisados, jogos_com_cartao, frequencia in resultados:
        cur.execute(
            """
            INSERT INTO padroes_jogador_cartao (jogador_id, jogos_analisados, jogos_com_cartao, frequencia, atualizado_em)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (jogador_id) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                jogos_com_cartao = EXCLUDED.jogos_com_cartao,
                frequencia = EXCLUDED.frequencia,
                atualizado_em = NOW()
            """,
            (jogador_id, jogos_analisados, jogos_com_cartao, frequencia),
        )
        print(f"  {nome}: {jogos_com_cartao}/{jogos_analisados} jogos com cartão ({frequencia}%)")


def calcular_padroes_escanteio(cur):
    """Olha os escanteios do Corinthians (não do adversário) nos últimos jogos,
    e calcula a frequência de passar de cada linha testada (3.5, 4.5, ...)."""
    cur.execute(
        """
        SELECT eg.escanteios
        FROM estatisticas_jogo eg
        JOIN jogos j ON j.id = eg.jogo_id
        WHERE (j.mandante = TRUE AND eg.lado = 'mandante')
           OR (j.mandante = FALSE AND eg.lado = 'visitante')
        ORDER BY j.data_jogo DESC
        LIMIT %s
        """,
        (JANELA_MAXIMA_DE_JOGOS,),
    )
    linhas_brutas = [row[0] for row in cur.fetchall() if row[0] is not None]

    jogos_analisados = len(linhas_brutas)
    if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
        return None, jogos_analisados

    media = round(sum(float(v) for v in linhas_brutas) / jogos_analisados, 2)

    resultados = []
    for linha in LINHAS_ESCANTEIO:
        jogos_acima = sum(1 for v in linhas_brutas if float(v) > linha)
        frequencia = round(100 * jogos_acima / jogos_analisados, 2)
        resultados.append((linha, jogos_analisados, jogos_acima, frequencia, media))

    return resultados, jogos_analisados


def salvar_padroes_escanteio(cur, resultados):
    for linha, jogos_analisados, jogos_acima, frequencia, media in resultados:
        cur.execute(
            """
            INSERT INTO padroes_time_escanteio (linha, jogos_analisados, jogos_acima_da_linha, frequencia, media, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (linha) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                jogos_acima_da_linha = EXCLUDED.jogos_acima_da_linha,
                frequencia = EXCLUDED.frequencia,
                media = EXCLUDED.media,
                atualizado_em = NOW()
            """,
            (linha, jogos_analisados, jogos_acima, frequencia, media),
        )
        print(f"  Mais de {linha} escanteios: {jogos_acima}/{jogos_analisados} jogos ({frequencia}%)")


def calcular_padrao_linha_jogador(cur, coluna, linhas_testadas):
    """Função genérica: para cada jogador, testa várias linhas (0.5, 1.5, ...)
    numa coluna numérica da tabela jogador_estatisticas_jogo (ex: desarmes)."""
    cur.execute("SELECT id, nome FROM jogadores")
    jogadores = cur.fetchall()

    resultados = []

    for jogador_id, nome in jogadores:
        cur.execute(
            f"""
            SELECT jeg.{coluna}
            FROM jogador_estatisticas_jogo jeg
            JOIN jogos j ON j.id = jeg.jogo_id
            WHERE jeg.jogador_id = %s AND jeg.{coluna} IS NOT NULL
            ORDER BY j.data_jogo DESC
            LIMIT %s
            """,
            (jogador_id, JANELA_MAXIMA_DE_JOGOS),
        )
        valores = [row[0] for row in cur.fetchall()]

        jogos_analisados = len(valores)
        if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
            continue

        for linha in linhas_testadas:
            jogos_acima = sum(1 for v in valores if float(v) > linha)
            frequencia = round(100 * jogos_acima / jogos_analisados, 2)
            resultados.append((jogador_id, nome, linha, jogos_analisados, jogos_acima, frequencia))

    return resultados


def salvar_padrao_linha_jogador(cur, tipo, resultados):
    for jogador_id, nome, linha, jogos_analisados, jogos_acima, frequencia in resultados:
        cur.execute(
            """
            INSERT INTO padroes_jogador_linha (jogador_id, tipo, linha, jogos_analisados, jogos_acima, frequencia, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (jogador_id, tipo, linha) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                jogos_acima = EXCLUDED.jogos_acima,
                frequencia = EXCLUDED.frequencia,
                atualizado_em = NOW()
            """,
            (jogador_id, tipo, linha, jogos_analisados, jogos_acima, frequencia),
        )
    print(f"  {tipo}: {len(resultados)} linha(s)/jogador(es) calculados.")


def calcular_padrao_frequencia_jogador(cur, coluna):
    """Função genérica: para cada jogador, calcula a frequência de ter tido
    pelo menos 1 ocorrência (ex: pelo menos 1 impedimento no jogo)."""
    cur.execute("SELECT id, nome FROM jogadores")
    jogadores = cur.fetchall()

    resultados = []

    for jogador_id, nome in jogadores:
        cur.execute(
            f"""
            SELECT jeg.{coluna}
            FROM jogador_estatisticas_jogo jeg
            JOIN jogos j ON j.id = jeg.jogo_id
            WHERE jeg.jogador_id = %s AND jeg.{coluna} IS NOT NULL
            ORDER BY j.data_jogo DESC
            LIMIT %s
            """,
            (jogador_id, JANELA_MAXIMA_DE_JOGOS),
        )
        valores = [row[0] for row in cur.fetchall()]

        jogos_analisados = len(valores)
        if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
            continue

        jogos_com_evento = sum(1 for v in valores if v and v > 0)
        frequencia = round(100 * jogos_com_evento / jogos_analisados, 2)
        resultados.append((jogador_id, nome, jogos_analisados, jogos_com_evento, frequencia))

    return resultados


def salvar_padrao_frequencia_jogador(cur, tipo, resultados):
    for jogador_id, nome, jogos_analisados, jogos_com_evento, frequencia in resultados:
        cur.execute(
            """
            INSERT INTO padroes_jogador_frequencia (jogador_id, tipo, jogos_analisados, jogos_com_evento, frequencia, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (jogador_id, tipo) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                jogos_com_evento = EXCLUDED.jogos_com_evento,
                frequencia = EXCLUDED.frequencia,
                atualizado_em = NOW()
            """,
            (jogador_id, tipo, jogos_analisados, jogos_com_evento, frequencia),
        )
    print(f"  {tipo}: {len(resultados)} jogador(es) calculados.")


def calcular_padroes_resultado(cur):
    """Calcula a frequência histórica de vitória/empate/derrota do Corinthians,
    separado por mandante e visitante."""
    resultados_finais = []

    for lado_bool, lado_nome in [(True, "mandante"), (False, "visitante")]:
        cur.execute(
            """
            SELECT placar_corinthians, placar_adversario
            FROM jogos
            WHERE mandante = %s AND placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL
            ORDER BY data_jogo DESC
            LIMIT %s
            """,
            (lado_bool, JANELA_MAXIMA_DE_JOGOS),
        )
        jogos = cur.fetchall()
        total = len(jogos)
        if total < JOGOS_MINIMOS_PARA_ANALISAR:
            continue

        contagem = {"vitoria": 0, "empate": 0, "derrota": 0}
        for placar_cor, placar_adv in jogos:
            if placar_cor > placar_adv:
                contagem["vitoria"] += 1
            elif placar_cor == placar_adv:
                contagem["empate"] += 1
            else:
                contagem["derrota"] += 1

        for resultado, ocorrencias in contagem.items():
            frequencia = round(100 * ocorrencias / total, 2)
            resultados_finais.append((lado_nome, resultado, total, ocorrencias, frequencia))

    return resultados_finais


def salvar_padroes_resultado(cur, resultados):
    for lado, resultado, total, ocorrencias, frequencia in resultados:
        cur.execute(
            """
            INSERT INTO padroes_time_resultado (lado, resultado, jogos_analisados, ocorrencias, frequencia, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (lado, resultado) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                ocorrencias = EXCLUDED.ocorrencias,
                frequencia = EXCLUDED.frequencia,
                atualizado_em = NOW()
            """,
            (lado, resultado, total, ocorrencias, frequencia),
        )
        print(f"  {lado} - {resultado}: {ocorrencias}/{total} jogos ({frequencia}%)")


def calcular_padroes_arbitro(cur):
    """NOVO: para cada árbitro que já apitou algum jogo do Corinthians (e já
    tem resultado conhecido), calcula a média de cartões e faltas do jogo
    inteiro (ambos os times, não só o Corinthians) e a frequência de jogos
    com pelo menos 1 cartão vermelho. Não usa janela de 50 - usa todos os
    jogos disponíveis daquele árbitro, já que a amostra por árbitro é bem
    menor que a de jogador."""
    cur.execute(
        """
        SELECT DISTINCT arbitro FROM jogos
        WHERE arbitro IS NOT NULL AND data_jogo < CURRENT_DATE
        """
    )
    arbitros = [row[0] for row in cur.fetchall()]

    resultados = []

    for arbitro in arbitros:
        cur.execute(
            "SELECT id FROM jogos WHERE arbitro = %s AND data_jogo < CURRENT_DATE",
            (arbitro,),
        )
        jogo_ids = [row[0] for row in cur.fetchall()]

        jogos_analisados = len(jogo_ids)
        if jogos_analisados < JOGOS_MINIMOS_PARA_ANALISAR:
            continue  # amostra pequena demais pra confiar no perfil desse árbitro ainda

        # cartões totais do jogo (os dois times, não só o Corinthians -
        # a tabela `cartoes` já guarda eventos de ambos os lados)
        cur.execute(
            "SELECT jogo_id, COUNT(*) FROM cartoes WHERE jogo_id = ANY(%s) GROUP BY jogo_id",
            (jogo_ids,),
        )
        cartoes_por_jogo = dict(cur.fetchall())
        total_cartoes = sum(cartoes_por_jogo.values())
        media_cartoes = round(total_cartoes / jogos_analisados, 2)

        # jogos com pelo menos 1 cartão vermelho
        cur.execute(
            "SELECT DISTINCT jogo_id FROM cartoes WHERE jogo_id = ANY(%s) AND cor = 'vermelho'",
            (jogo_ids,),
        )
        jogos_com_vermelho = len(cur.fetchall())
        frequencia_vermelho = round(100 * jogos_com_vermelho / jogos_analisados, 2)

        # faltas totais do jogo (soma dos dois lados, quando a estatística existe)
        cur.execute(
            "SELECT jogo_id, SUM(faltas) FROM estatisticas_jogo WHERE jogo_id = ANY(%s) AND faltas IS NOT NULL GROUP BY jogo_id",
            (jogo_ids,),
        )
        faltas_por_jogo = dict(cur.fetchall())
        media_faltas = None
        if faltas_por_jogo:
            media_faltas = round(sum(float(v) for v in faltas_por_jogo.values()) / len(faltas_por_jogo), 2)

        resultados.append((
            arbitro, jogos_analisados, media_cartoes, media_faltas,
            jogos_com_vermelho, frequencia_vermelho,
        ))

    return resultados


def salvar_padroes_arbitro(cur, resultados):
    for arbitro, jogos_analisados, media_cartoes, media_faltas, jogos_com_vermelho, frequencia_vermelho in resultados:
        cur.execute(
            """
            INSERT INTO padroes_arbitro (arbitro, jogos_analisados, media_cartoes, media_faltas,
                                          jogos_com_vermelho, frequencia_vermelho, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (arbitro) DO UPDATE SET
                jogos_analisados = EXCLUDED.jogos_analisados,
                media_cartoes = EXCLUDED.media_cartoes,
                media_faltas = EXCLUDED.media_faltas,
                jogos_com_vermelho = EXCLUDED.jogos_com_vermelho,
                frequencia_vermelho = EXCLUDED.frequencia_vermelho,
                atualizado_em = NOW()
            """,
            (arbitro, jogos_analisados, media_cartoes, media_faltas, jogos_com_vermelho, frequencia_vermelho),
        )
        print(f"  {arbitro}: {jogos_analisados} jogo(s), média de {media_cartoes} cartões/jogo, "
              f"{frequencia_vermelho}% dos jogos com vermelho")


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        print("Calculando padrões de cartão por jogador...")
        resultados_cartao = calcular_padroes_cartao(cur)

        if not resultados_cartao:
            print("Nenhum jogador com dados suficientes ainda "
                  f"(mínimo de {JOGOS_MINIMOS_PARA_ANALISAR} jogos analisados).")
        else:
            # ordena do mais frequente pro menos frequente, só para o log ficar mais legível
            resultados_cartao.sort(key=lambda r: r[4], reverse=True)
            salvar_padroes(cur, resultados_cartao)
            conn.commit()
            print(f"Concluído! Padrões de cartão calculados para {len(resultados_cartao)} jogador(es).")

        print("\nCalculando padrões de escanteio do time...")
        resultados_escanteio, jogos_analisados = calcular_padroes_escanteio(cur)

        if not resultados_escanteio:
            print(f"Dados insuficientes ainda para escanteio ({jogos_analisados} jogos analisados, "
                  f"mínimo de {JOGOS_MINIMOS_PARA_ANALISAR}).")
        else:
            salvar_padroes_escanteio(cur, resultados_escanteio)
            conn.commit()
            print(f"Concluído! Padrões de escanteio calculados com base em {jogos_analisados} jogo(s).")

        print("\nCalculando padrões de linha por jogador (faltas, desarmes, chutes)...")
        for tipo, (coluna, linhas) in PADROES_LINHA_JOGADOR.items():
            resultados = calcular_padrao_linha_jogador(cur, coluna, linhas)
            if resultados:
                salvar_padrao_linha_jogador(cur, tipo, resultados)
                conn.commit()
            else:
                print(f"  {tipo}: nenhum jogador com dados suficientes ainda.")

        print("\nCalculando padrões de frequência por jogador (impedimentos)...")
        for tipo, coluna in PADROES_FREQUENCIA_JOGADOR.items():
            resultados = calcular_padrao_frequencia_jogador(cur, coluna)
            if resultados:
                salvar_padrao_frequencia_jogador(cur, tipo, resultados)
                conn.commit()
            else:
                print(f"  {tipo}: nenhum jogador com dados suficientes ainda.")

        print("\nCalculando padrões de resultado final (vitória/empate/derrota)...")
        resultados_finais = calcular_padroes_resultado(cur)
        if resultados_finais:
            salvar_padroes_resultado(cur, resultados_finais)
            conn.commit()
        else:
            print("  Dados insuficientes ainda para resultado final.")

        print("\nCalculando perfil de árbitros (cartões e faltas por jogo apitado)...")
        resultados_arbitro = calcular_padroes_arbitro(cur)
        if resultados_arbitro:
            salvar_padroes_arbitro(cur, resultados_arbitro)
            conn.commit()
            print(f"Concluído! Perfil calculado para {len(resultados_arbitro)} árbitro(s).")
        else:
            print(f"  Nenhum árbitro com dados suficientes ainda (mínimo de {JOGOS_MINIMOS_PARA_ANALISAR} jogos apitados).")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
