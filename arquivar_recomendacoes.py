"""
Avaliação e arquivamento de recomendações.

Para jogos que já aconteceram (já passou o horário do jogo), avalia se cada
recomendação "acertou" ou "errou", comparando com o resultado real do jogo
(se já estiver disponível no banco), e move pra tabela de histórico. Isso
libera a tabela `recomendacoes` para focar só no que ainda é relevante
(jogos futuros).

IMPORTANTE: a avaliação de acerto/erro só funciona se já tivermos os dados
reais daquele jogo no banco (tabelas cartoes, jogador_estatisticas_jogo,
estatisticas_jogo). Até lá, o resultado fica marcado como "pendente", e a
estrutura já está pronta pra funcionar automaticamente assim que os dados
reais chegarem (ver reavaliar_pendentes_ja_arquivadas).

NOVO: corrige um bug em que a avaliação só sabia conferir o lado "Mais"/
"Sim" de cada mercado - uma recomendação de "Menos" que tivesse acertado
de verdade era marcada como "errou" por engano, porque a lógica antiga só
comparava "> linha", nunca o lado oposto. Agora usa a coluna `direcao`
(salva pelo motor_recomendacoes.py) pra conferir do jeito certo.
NOVO: também cobre os mercados de escanteio total e cartão total do jogo
(mandante + visitante somados), que a versão anterior nunca avaliava.
NOVO: reavalia recomendações que ficaram "pendente" em execuções passadas,
assim que o dado real do jogo chegar (antes ficavam pendentes pra sempre).
NOVO: usa datahora_jogo (data + hora) em vez de só data_jogo pra decidir se
um jogo já é "passado" - antes, um jogo de hoje já encerrado só era
considerado passado depois da meia-noite, deixando o histórico vazio por
horas mesmo depois do jogo terminar.
NOVO: removida a regra de "manter as últimas 5 rodadas detalhadas sem
arquivar" - ela fazia sentido antes de existir a página /historico no
site, mas depois passou a esconder justamente os resultados mais recentes
(os que o usuário mais quer ver) da página de histórico. Agora qualquer
jogo já passado é arquivado assim que esse script roda, não importa há
quanto tempo terminou.

NOVO: também avalia as múltiplas capturadas em `multiplas_candidatas`
(ver combinacoes.py/capturar_candidatas_multiplas) - pra cada candidata
já CONGELADA (jogo mais próximo já começou) e ainda não avaliada, confere
se todas as pernas já têm resultado conhecido (reaproveitando o resultado
que a avaliação individual acima já calculou, em vez de reavaliar do
zero - uma função de avaliação só, evita o mesmo tipo de bug de lógica
duplicada divergindo que já aconteceu antes nesse projeto). Assim que uma
candidata fica pronta, os jogos dela têm o "top-5 por probabilidade
histórica" recalculado em `historico_multiplas_destaque` - substitui a
página /historico, que antes recalculava tudo ao vivo a cada acesso.

Variáveis de ambiente:
  - DATABASE_URL -> a URL de conexão do Postgres (mesma usada nos outros scripts)
"""

import json
import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]


def normalizar(direcao):
    return (direcao or "").strip().lower()


def avaliar_linha(valor_real, linha, direcao):
    """Confere um mercado de linha (Mais/Menos), usando a mesma definição de
    fronteira usada no cálculo da frequência histórica: "mais" conta valores
    ESTRITAMENTE acima da linha; "menos" é o complementar (valores até a
    linha, inclusive) - os dois juntos cobrem 100% dos casos, sem sobra nem
    lacuna."""
    if direcao == "mais":
        return "acertou" if valor_real > float(linha) else "errou"
    if direcao == "menos":
        return "acertou" if valor_real <= float(linha) else "errou"
    return "pendente"  # direção desconhecida/não reconhecida - não arrisca avaliar


def avaliar_binario(ocorreu, direcao):
    """Confere um mercado binário (Sim/Não - ex: jogador recebeu cartão)."""
    if direcao == "sim":
        return "acertou" if ocorreu else "errou"
    if direcao in ("não", "nao"):
        return "acertou" if not ocorreu else "errou"
    return "pendente"


def avaliar_resultado(cur, tipo_padrao, jogador_id, jogo_id, linha, descricao, direcao):
    """Compara a recomendação com o resultado real do jogo, se disponível.
    Retorna 'acertou', 'errou' ou 'pendente' (se ainda não temos o dado real)."""
    d = normalizar(direcao)

    if tipo_padrao == "cartao":
        cur.execute("SELECT 1 FROM cartoes WHERE jogo_id = %s AND jogador_id = %s", (jogo_id, jogador_id))
        recebeu_cartao = cur.fetchone() is not None

        cur.execute(
            "SELECT 1 FROM jogador_estatisticas_jogo WHERE jogo_id = %s AND jogador_id = %s",
            (jogo_id, jogador_id),
        )
        jogador_tem_dado = cur.fetchone() is not None

        if not recebeu_cartao and not jogador_tem_dado:
            return "pendente"  # ainda não temos as estatísticas desse jogo
        return avaliar_binario(recebeu_cartao, d)

    if tipo_padrao in ("falta_cometida", "desarme", "chute_no_gol", "chute_total"):
        coluna = {
            "falta_cometida": "faltas_cometidas",
            "desarme": "desarmes",
            "chute_no_gol": "chutes_no_gol",
            "chute_total": "chutes",
        }[tipo_padrao]
        cur.execute(
            f"SELECT {coluna} FROM jogador_estatisticas_jogo WHERE jogo_id = %s AND jogador_id = %s",
            (jogo_id, jogador_id),
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return "pendente"
        return avaliar_linha(float(row[0]), linha, d)

    if tipo_padrao == "impedimento":
        cur.execute(
            "SELECT impedimentos FROM jogador_estatisticas_jogo WHERE jogo_id = %s AND jogador_id = %s",
            (jogo_id, jogador_id),
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return "pendente"
        return avaliar_binario(row[0] > 0, d)

    if tipo_padrao == "escanteio_time":
        cur.execute("SELECT mandante FROM jogos WHERE id = %s", (jogo_id,))
        info_jogo = cur.fetchone()
        if not info_jogo:
            return "pendente"
        lado = "mandante" if info_jogo[0] else "visitante"
        cur.execute(
            "SELECT escanteios FROM estatisticas_jogo WHERE jogo_id = %s AND lado = %s",
            (jogo_id, lado),
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return "pendente"
        return avaliar_linha(float(row[0]), linha, d)

    if tipo_padrao == "escanteio_total":
        cur.execute(
            """
            SELECT SUM(escanteios), COUNT(DISTINCT lado)
            FROM estatisticas_jogo WHERE jogo_id = %s AND escanteios IS NOT NULL
            """,
            (jogo_id,),
        )
        row = cur.fetchone()
        if row is None or row[0] is None or row[1] != 2:
            return "pendente"  # falta o dado de algum dos dois lados ainda
        return avaliar_linha(float(row[0]), linha, d)

    if tipo_padrao == "cartao_total":
        cur.execute(
            "SELECT COUNT(DISTINCT lado) FROM estatisticas_jogo WHERE jogo_id = %s",
            (jogo_id,),
        )
        row = cur.fetchone()
        if row is None or row[0] != 2:
            return "pendente"  # jogo ainda não totalmente processado
        cur.execute("SELECT COUNT(*) FROM cartoes WHERE jogo_id = %s", (jogo_id,))
        total_cartoes = cur.fetchone()[0]
        return avaliar_linha(float(total_cartoes), linha, d)

    if tipo_padrao == "resultado_final":
        cur.execute(
            "SELECT placar_corinthians, placar_adversario FROM jogos WHERE id = %s",
            (jogo_id,),
        )
        row = cur.fetchone()
        if row is None or row[0] is None or row[1] is None:
            return "pendente"
        placar_cor, placar_adv = row
        if placar_cor > placar_adv:
            resultado_real = "vitória"
        elif placar_cor == placar_adv:
            resultado_real = "empate"
        else:
            resultado_real = "derrota"
        # a descrição salva foi montada como "Resultado Final - Vitória do Corinthians" etc.
        return "acertou" if resultado_real in descricao.lower() else "errou"

    return "pendente"


def buscar_recomendacoes_para_arquivar(cur):
    """NOVO: arquiva TODO jogo já passado, sem exceção de "últimas rodadas
    mantidas detalhadas" (ver nota no topo do arquivo)."""
    cur.execute(
        """
        SELECT r.id, r.jogo_id, r.jogador_id, r.tipo_padrao, r.descricao, r.casa_aposta,
               r.odd_oferecida, r.probabilidade_historica, r.valor_esperado, r.linha,
               r.direcao, j.data_jogo
        FROM recomendacoes r
        JOIN jogos j ON j.id = r.jogo_id
        WHERE (j.datahora_jogo IS NOT NULL AND j.datahora_jogo < NOW())
           OR (j.datahora_jogo IS NULL AND j.data_jogo < CURRENT_DATE)
        """
    )
    return cur.fetchall()


def arquivar(cur, recomendacoes):
    contagem = {"acertou": 0, "errou": 0, "pendente": 0}

    for (rec_id, jogo_id, jogador_id, tipo_padrao, descricao, casa,
         odd, prob, ve, linha, direcao, data_jogo) in recomendacoes:

        resultado = avaliar_resultado(cur, tipo_padrao, jogador_id, jogo_id, linha, descricao, direcao)
        contagem[resultado] += 1

        cur.execute(
            """INSERT INTO historico_recomendacoes
               (jogo_id, jogador_id, tipo_padrao, descricao, casa_aposta,
                odd_oferecida, probabilidade_historica, valor_esperado, linha,
                direcao, resultado, data_jogo)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (jogo_id, jogador_id, tipo_padrao, descricao, casa, odd, prob, ve,
             linha, direcao, resultado, data_jogo),
        )
        cur.execute("DELETE FROM recomendacoes WHERE id = %s", (rec_id,))

    return contagem


def reavaliar_pendentes_ja_arquivadas(cur):
    """NOVO: recomendações que já foram arquivadas como 'pendente' (porque na
    época ainda não tínhamos o dado real do jogo) podem ser reavaliadas mais
    tarde, assim que o dado chegar - sem isso, uma recomendação ficaria
    "pendente" pra sempre mesmo depois do jogo ser totalmente processado."""
    cur.execute(
        """
        SELECT id, jogo_id, jogador_id, tipo_padrao, descricao, linha, direcao
        FROM historico_recomendacoes
        WHERE resultado = 'pendente'
        """
    )
    pendentes = cur.fetchall()
    if not pendentes:
        return 0

    reavaliadas = 0
    for rec_id, jogo_id, jogador_id, tipo_padrao, descricao, linha, direcao in pendentes:
        novo_resultado = avaliar_resultado(cur, tipo_padrao, jogador_id, jogo_id, linha, descricao, direcao)
        if novo_resultado != "pendente":
            cur.execute(
                "UPDATE historico_recomendacoes SET resultado = %s WHERE id = %s",
                (novo_resultado, rec_id),
            )
            reavaliadas += 1

    return reavaliadas


def garantir_coluna_resultado_candidatas(cur):
    """NOVO: adiciona a coluna `resultado` em `multiplas_candidatas`, se
    ainda não existir - guarda o resultado já calculado da múltipla
    (acertou/errou), pra não precisar recalcular de novo toda vez que
    selecionar_top5_do_jogo roda. ADD COLUMN IF NOT EXISTS é seguro e
    instantâneo no Postgres, rodar isso aqui evita depender de uma
    migração manual separada só por causa de uma coluna."""
    cur.execute("ALTER TABLE multiplas_candidatas ADD COLUMN IF NOT EXISTS resultado VARCHAR(10)")


def buscar_resultado_perna(cur, jogo_id, jogador_id, tipo_padrao, descricao):
    """Busca o resultado (acertou/errou/pendente) já avaliado dessa perna
    individual em `historico_recomendacoes` - REAPROVEITA a avaliação que
    arquivar() já fez acima, em vez de reavaliar do zero (uma função de
    avaliação só, `avaliar_resultado`, continua sendo a única fonte de
    verdade). Se a perna ainda não foi arquivada (jogo dela ainda não
    passou de verdade, mesmo que o PRIMEIRO jogo da múltipla já tenha
    passado - lembra que uma múltipla pode cruzar jogos com datas
    diferentes), retorna None."""
    cur.execute(
        """
        SELECT resultado FROM historico_recomendacoes
        WHERE jogo_id = %s AND jogador_id IS NOT DISTINCT FROM %s
          AND tipo_padrao = %s AND descricao = %s
        ORDER BY id DESC LIMIT 1
        """,
        (jogo_id, jogador_id, tipo_padrao, descricao),
    )
    row = cur.fetchone()
    return row[0] if row else None


def buscar_candidatas_prontas_para_avaliar(cur):
    """Candidatas já congeladas (jogo mais próximo já começou) e ainda não
    avaliadas."""
    cur.execute(
        "SELECT id, pernas, jogos FROM multiplas_candidatas "
        "WHERE congelada = TRUE AND avaliada = FALSE"
    )
    return cur.fetchall()


def selecionar_top5_do_jogo(cur, jogo_id):
    """Recalcula o top-5 de Múltiplas em Destaque desse jogo - pega até 5
    candidatas já avaliadas ligadas a ele, ordenadas por probabilidade
    histórica, e substitui a seleção anterior (idempotente - útil quando
    uma candidata nova ainda mais provável aparece depois).

    NOVO (protege a compressão de limpar_historico.py): se esse jogo já
    tem QUALQUER linha comprimida (casa_aposta NULL) em
    historico_multiplas_destaque, significa que ele já saiu da janela de
    2 rodadas com detalhe completo - não reabre o detalhe dele só porque
    uma candidata atrasada terminou de ser avaliada agora."""
    cur.execute(
        "SELECT COUNT(*) FROM historico_multiplas_destaque WHERE jogo_id = %s AND casa_aposta IS NULL",
        (jogo_id,),
    )
    if cur.fetchone()[0] > 0:
        return 0

    cur.execute(
        """
        SELECT casa_aposta, descricao, odd_combinada, jogos, probabilidade_combinada, resultado
        FROM multiplas_candidatas
        WHERE avaliada = TRUE AND jogos @> %s::jsonb
        ORDER BY probabilidade_combinada DESC
        LIMIT 5
        """,
        (json.dumps([{"jogo_id": jogo_id}]),),
    )
    top5 = cur.fetchall()

    cur.execute("SELECT rodada FROM jogos WHERE id = %s", (jogo_id,))
    row = cur.fetchone()
    rodada = row[0] if row else None

    cur.execute("DELETE FROM historico_multiplas_destaque WHERE jogo_id = %s", (jogo_id,))
    for casa, descricao, odd, jogos, prob, resultado in top5:
        cur.execute(
            """
            INSERT INTO historico_multiplas_destaque
                (jogo_id, rodada, casa_aposta, descricao, odd_combinada, jogos,
                 probabilidade_combinada, resultado)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (jogo_id, rodada, casa, descricao, odd, json.dumps(jogos, default=str), prob, resultado),
        )
    return len(top5)


def avaliar_e_selecionar_top5(cur):
    """Pra cada candidata congelada ainda não avaliada, confere se TODAS
    as pernas já têm resultado conhecido - se sim, calcula o resultado da
    múltipla (errou se qualquer perna errou; acertou só se TODAS
    acertaram; senão continua pendente, tenta de novo na próxima
    execução) e marca `avaliada = TRUE`. Depois recalcula o top-5 de cada
    jogo afetado."""
    garantir_coluna_resultado_candidatas(cur)
    candidatas = buscar_candidatas_prontas_para_avaliar(cur)
    if not candidatas:
        return 0, 0

    avaliadas_agora = 0
    jogos_a_reselecionar = set()

    for cand_id, pernas, jogos in candidatas:
        resultados_pernas = []
        pronto = True
        for perna in pernas:
            resultado_perna = buscar_resultado_perna(
                cur, perna["jogo_id"], perna["jogador_id"], perna["tipo_padrao"], perna["descricao"]
            )
            if resultado_perna is None:
                pronto = False
                break
            resultados_pernas.append(resultado_perna)

        if not pronto:
            continue

        if any(r == "errou" for r in resultados_pernas):
            resultado_final = "errou"
        elif all(r == "acertou" for r in resultados_pernas):
            resultado_final = "acertou"
        else:
            resultado_final = "pendente"  # alguma perna arquivada mas ainda sem dado real

        if resultado_final == "pendente":
            continue

        cur.execute(
            "UPDATE multiplas_candidatas SET avaliada = TRUE, resultado = %s WHERE id = %s",
            (resultado_final, cand_id),
        )
        avaliadas_agora += 1
        for j in jogos:
            jogos_a_reselecionar.add(j["jogo_id"])

    for jogo_id in jogos_a_reselecionar:
        selecionar_top5_do_jogo(cur, jogo_id)

    return avaliadas_agora, len(jogos_a_reselecionar)


def resumo_geral(cur):
    """Retorna a taxa de acerto histórica geral, pra acompanhar a performance
    do sistema ao longo do tempo."""
    cur.execute(
        "SELECT resultado, COUNT(*) FROM historico_recomendacoes GROUP BY resultado"
    )
    return dict(cur.fetchall())


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        recomendacoes = buscar_recomendacoes_para_arquivar(cur)

        if not recomendacoes:
            print("Nada para arquivar no momento (nenhum jogo passado com recomendação pendente).")
        else:
            contagem = arquivar(cur, recomendacoes)
            print(f"Arquivadas {len(recomendacoes)} recomendação(ões): "
                  f"{contagem['acertou']} acertou, {contagem['errou']} errou, "
                  f"{contagem['pendente']} ainda pendente (aguardando dados reais do jogo).")

        conn.commit()

        reavaliadas = reavaliar_pendentes_ja_arquivadas(cur)
        if reavaliadas:
            print(f"\n{reavaliadas} recomendação(ões) que estavam pendentes foram "
                  f"reavaliadas agora que o dado real do jogo chegou.")
        conn.commit()

        avaliadas, jogos_processados = avaliar_e_selecionar_top5(cur)
        if avaliadas:
            print(f"\n{avaliadas} múltipla(s) candidata(s) avaliada(s) agora; "
                  f"top-5 de Múltiplas em Destaque recalculado pra {jogos_processados} jogo(s).")
        conn.commit()

        resumo = resumo_geral(cur)
        if resumo:
            total_avaliado = resumo.get("acertou", 0) + resumo.get("errou", 0)
            if total_avaliado > 0:
                taxa = round(100 * resumo.get("acertou", 0) / total_avaliado, 1)
                print(f"\nDesempenho histórico geral: {resumo.get('acertou', 0)}/{total_avaliado} "
                      f"acertos avaliados ({taxa}%). {resumo.get('pendente', 0)} ainda pendente(s).")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
