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

NOVO (30/08/2026 - status da API como critério, não só a data): um jogo
também é considerado passado quando `jogos_liga.status` diz que ele
terminou ('FT' = tempo normal, 'AET' = prorrogação, 'PEN' = pênaltis).
Antes o único critério era a data/hora gravada em `jogos`, e isso falhava
quando essa data estava errada.

Caso real (30/08/2026): Cruzeiro x Vasco aconteceu no SÁBADO 29/08, mas
ficou gravado em `jogos` como 30/08 20:00. Resultado: 51 recomendações
continuaram na tela como se o jogo não tivesse acontecido, mesmo com
placar (1x3) e estatísticas já coletados - porque `datahora_jogo` ainda
não tinha "passado".

POR QUE O STATUS E NÃO A DATA: a data se mostrou não confiável nas DUAS
tabelas, e nos DOIS sentidos. Medido na base: `jogos` e `jogos_liga`
divergem em vários jogos, às vezes uma adiantada, às vezes a outra
(Coritiba x Remo: 31/08 contra 30/08; Atlético-MG x Vitória: 29/08 contra
30/08; Mirassol x Flamengo: 02/09 contra 25/02 - seis meses). Não é
fuso horário, senão a diferença teria sempre o mesmo sinal. Já o `status`
é uma afirmação direta da API-Football sobre o jogo ter acabado, e não
depende de fuso nem de remarcação.

POR QUE NÃO USAR "mercados suspensos/vazios da OddsPapi" COMO SINAL:
considerado e descartado. Mercado suspenso acontece com jogo terminado,
mas TAMBÉM com jogo em andamento, suspensão momentânea (gol, VAR, lesão),
jogo adiado e falha parcial da API. Tratar isso como "acabou" arquivaria
recomendações no MEIO do jogo, avaliando contra estatística incompleta -
a mesma família de bug que já aconteceu duas vezes neste projeto.

O `LEFT JOIN` é de propósito: se não existir linha em `jogos_liga` pra
aquele fixture, o comportamento continua sendo exatamente o de antes (só
data), sem regressão.

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

NOVO (26/08/2026): `selecionar_top5_do_jogo` agora também grava a
`assinatura` de cada combinação (vinda de `multiplas_candidatas.
assinatura`, a identidade estável de cada múltipla) em
`historico_multiplas_destaque`. A ESCRITA continua gerando 2 linhas pra
uma combinação que cruza 2 jogos (decisão consciente, mantida) - só a
LEITURA em app.py passa a deduplicar usando essa coluna, corrigindo a
combinação aparecer 2x na lista de /historico e contar 2x no resumo
agregado. Precisa da migração migrar_assinatura_multiplas_destaque.py
rodada ANTES.

NOVO (Fase 2, 08/09/2026 - casamento de perna por estrutura, não texto):
`buscar_resultado_perna` casava a perna de uma múltipla com o resultado
já arquivado usando `jogo_id + jogador_id + tipo_padrao + descricao`
(descrição em TEXTO EXATO). Isso tinha dois problemas latentes:

1) `jogo_id` é específico de uma perspectiva do jogo real. Quando um
   mercado de jogo inteiro ou de jogador (ver MERCADOS_JOGO_INTEIRO/
   MERCADOS_JOGADOR em combinacoes.py) é arquivado, `buscar_recomendacoes_
   para_arquivar` pode DEDUPLICAR duas linhas (uma por perspectiva) em
   uma só, mantendo como "representante" a de MENOR id - que não é
   necessariamente a mesma perspectiva usada quando a perna da múltipla
   foi originalmente montada em combinacoes.py. Se a perna guardou o
   jogo_id da perspectiva que acabou sendo APAGADA no arquivamento, a
   busca por esse jogo_id nunca mais encontra nada - a perna fica presa
   pra sempre em "ainda não avaliada" (congelada=TRUE, avaliada=FALSE).

2) `descricao` muda de texto entre a captura da perna e o resultado
   arquivado sempre que um sufixo (Grupo A, encolhimento pra odd da casa,
   confronto direto) aparece, some ou muda de valor entre uma geração e
   outra - o mesmo princípio já registrado nos aprendizados do projeto:
   nunca usar texto de descrição como identidade de uma aposta.

CORREÇÃO: pra mercados de jogo inteiro/jogador, o casamento passa a usar
`fixture_id_api` (estável entre as duas perspectivas, via JOIN com
`jogos`) em vez de `jogo_id`, e `linha`/`direcao` (o dado estrutural por
trás da aposta) em vez de `descricao`. Pra mercados de TIME (que não são
deduplicados no arquivamento, então não sofrem do problema 1), o
casamento continua por `jogo_id` - `fixture_id_api` sozinho ambiguaria a
aposta do nosso time com a do adversário. A mesma identidade
(`_identidade_estavel_perna`) foi usada do lado de combinacoes.py, na
assinatura de Múltiplas em Destaque - ver a docstring de lá.

Candidatas capturadas ANTES dessa mudança não têm `fixture_id_api` no
JSONB de `pernas` - `perna.get("fixture_id_api")` devolve None pra essas,
e o casamento cai de volta no comportamento antigo (por jogo_id), sem
precisar de migração nem backfill.

Variáveis de ambiente:
  - DATABASE_URL -> a URL de conexão do Postgres (mesma usada nos outros scripts)
"""

import json
import os
import psycopg2

from avaliacao import avaliar_resultado
# NOVO: a lógica de avaliação (comparar aposta com resultado real) foi
# extraída pro módulo avaliacao.py, compartilhado com app.py (Criador de
# Odd / apostas manuais) - antes existiam duas cópias divergindo, agora
# essa é a ÚNICA fonte de verdade.

from combinacoes import MERCADOS_JOGO_INTEIRO, MERCADOS_JOGADOR
# NOVO (correção de bug real, encontrado via consulta direta no banco): a
# mesma duplicata multi-time já corrigida em combinacoes.py (jogo entre 2
# times rastreados gera 2 linhas em `jogos`, duplicando odds/recomendações
# de mercado de jogador e de jogo inteiro) também afetava o ARQUIVAMENTO -
# `arquivar_recomendacoes.py` lê `recomendacoes` direto, sem passar pela
# deduplicação que já existia só em combinacoes.py/buscar_recomendacoes.
# Isso significava que a MESMA recomendação real virava DUAS entradas em
# historico_recomendacoes, inflando artificialmente as contagens de
# acerto/erro do /historico. Reaproveita a mesma classificação de
# mercados (MERCADOS_JOGO_INTEIRO/MERCADOS_JOGADOR), pra não duplicar
# essa lógica de novo. NOVO (Fase 2): os dois conjuntos também decidem,
# em `buscar_resultado_perna`, se o casamento de perna usa fixture_id_api
# ou jogo_id - ver a docstring da função e o NOVO no topo deste arquivo.

DATABASE_URL = os.environ["DATABASE_URL"]


def buscar_recomendacoes_para_arquivar(cur):
    """NOVO: arquiva TODO jogo já passado, sem exceção de "últimas rodadas
    mantidas detalhadas" (ver nota no topo do arquivo).

    NOVO (correção de duplicata): agrupa por identidade real da aposta
    (ver MERCADOS_JOGO_INTEIRO/MERCADOS_JOGADOR) - quando duas linhas em
    `recomendacoes` são, na prática, a MESMA aposta real (vindas das duas
    perspectivas de um jogo entre times rastreados), só a de maior
    probabilidade histórica vira uma entrada em historico_recomendacoes;
    a(s) outra(s) ainda são apagadas de `recomendacoes` (não ficam presas
    lá pra sempre), só não geram um registro duplicado no histórico."""
    cur.execute(
        """
        SELECT r.id, r.jogo_id, r.jogador_id, r.tipo_padrao, r.descricao, r.casa_aposta,
               r.odd_oferecida, r.probabilidade_historica, r.valor_esperado, r.linha,
               r.direcao, j.data_jogo, j.fixture_id_api
        FROM recomendacoes r
        JOIN jogos j ON j.id = r.jogo_id
        LEFT JOIN jogos_liga jl ON jl.fixture_id_api = j.fixture_id_api
        WHERE (j.datahora_jogo IS NOT NULL AND j.datahora_jogo < NOW())
           OR (j.datahora_jogo IS NULL AND j.data_jogo < CURRENT_DATE)
           OR jl.status IN ('FT', 'AET', 'PEN')
        """
    )
    linhas = cur.fetchall()

    grupos = {}
    for rec in linhas:
        (rec_id, jogo_id, jogador_id, tipo_padrao, descricao, casa,
         odd, prob, ve, linha, direcao, data_jogo, fixture_id_api) = rec

        if tipo_padrao in MERCADOS_JOGO_INTEIRO:
            # NOVO (30/08/2026): identidade ESTRUTURADA, não mais a
            # `descricao`. O texto não serve como identidade aqui porque
            # o mesmo evento pode ser descrito de dois jeitos: "Vitória do
            # Cruzeiro" (perspectiva do Cruzeiro) e "Derrota do Vasco DA
            # Gama" (perspectiva do Vasco) são a MESMA aposta, com a mesma
            # `direcao` gravada - `direcao` é relativa ao mandante real,
            # não ao "nosso time" (confirmado na base). Com a chave por
            # texto elas nunca se encontravam.
            #
            # Vale o princípio já registrado nos aprendizados do projeto:
            # nunca usar texto de descrição como identidade de uma aposta.
            chave = ("jogo_inteiro", fixture_id_api, tipo_padrao, linha, direcao, casa)
        elif tipo_padrao in MERCADOS_JOGADOR:
            chave = ("jogador", fixture_id_api, tipo_padrao, jogador_id, linha, direcao, casa)
        else:
            chave = ("unico", rec_id)  # mercado sem risco de duplicata - grupo de 1, comportamento inalterado

        grupos.setdefault(chave, []).append(rec)

    resultado = []
    for recs in grupos.values():
        # NOVO (30/08/2026): quando as cópias divergem, a probabilidade
        # arquivada passa a ser a MÉDIA delas, não a MAIOR.
        #
        # A regra antiga (`max`) foi escrita quando "duplicata" significava
        # cópia idêntica - aí tanto fazia qual sobrava. Mas em
        # resultado_final as duas perspectivas podem dar números
        # diferentes pro mesmo evento (medido em 30/08: "Vitória do
        # Cruzeiro" 30,91% e "Derrota do Vasco" 34,50%, por causa da
        # assimetria de fatores que ainda está aberta). Ficar com a maior
        # seria seleção adversa embutida no arquivamento, contaminando
        # justamente a tabela de calibração.
        #
        # ⚠️ REPRESENTANTE = a linha de MENOR id, e os campos `jogo_id` e
        # `descricao` vêm dela JUNTOS, nunca misturados entre cópias.
        # Isso não é detalhe: `avaliacao.avaliar_resultado` avalia
        # resultado_final comparando o texto da descrição ("vitória"/
        # "empate"/"derrota") com o placar lido a partir do `jogo_id`.
        # Misturar a descrição de uma perspectiva com o jogo_id da outra
        # INVERTE o resultado silenciosamente - "Derrota do Vasco" avaliada
        # pela ótica do Cruzeiro daria "acertou" onde foi erro.
        representante = min(recs, key=lambda r: r[0])
        ids_duplicados = [r[0] for r in recs if r[0] != representante[0]]

        if len(recs) > 1:
            prob_media = round(sum(float(r[7]) for r in recs) / len(recs), 2)
            # o VE precisa continuar coerente com a probabilidade gravada -
            # senão o histórico fica com prob e VE que não fecham entre si
            odd = float(representante[6])
            ve_recalculado = round((prob_media / 100 * odd) - 1, 3)
            linha_final = (
                representante[:7] + (prob_media, ve_recalculado) + representante[9:12]
            )
        else:
            linha_final = representante[:12]

        resultado.append(linha_final + (ids_duplicados,))
    return resultado


def arquivar(cur, recomendacoes):
    contagem = {"acertou": 0, "errou": 0, "pendente": 0}

    for (rec_id, jogo_id, jogador_id, tipo_padrao, descricao, casa,
         odd, prob, ve, linha, direcao, data_jogo, ids_duplicados) in recomendacoes:

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
        if ids_duplicados:
            # NOVO: apaga as cópias duplicadas (vindas da outra perspectiva
            # do mesmo jogo real) sem gerar outro registro no histórico -
            # senão ficariam presas em `recomendacoes` pra sempre, mesmo
            # com o jogo já encerrado.
            cur.execute("DELETE FROM recomendacoes WHERE id = ANY(%s)", (ids_duplicados,))

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


def buscar_resultado_perna(cur, tipo_padrao, jogo_id, fixture_id_api, jogador_id, linha, direcao):
    """Busca o resultado (acertou/errou/pendente) já avaliado dessa perna
    individual em `historico_recomendacoes` - REAPROVEITA a avaliação que
    arquivar() já fez acima, em vez de reavaliar do zero (uma função de
    avaliação só, `avaliar_resultado`, continua sendo a única fonte de
    verdade). Se a perna ainda não foi arquivada (jogo dela ainda não
    passou de verdade, mesmo que o PRIMEIRO jogo da múltipla já tenha
    passado - lembra que uma múltipla pode cruzar jogos com datas
    diferentes), retorna None.

    CORRIGIDO (Fase 2, 08/09/2026): casamento por ESTRUTURA
    (tipo_padrao + jogador_id + linha + direcao), não mais por texto de
    `descricao`. E pra mercados de jogo inteiro/jogador (ver
    MERCADOS_JOGO_INTEIRO/MERCADOS_JOGADOR), o casamento usa
    `fixture_id_api` via JOIN com `jogos` em vez de `jogo_id` - porque
    esses mercados podem ser deduplicados no arquivamento mantendo a
    linha da OUTRA perspectiva (a de menor id), e aí o `jogo_id` que a
    perna guardou deixa de existir em `historico_recomendacoes` pra
    sempre. `fixture_id_api` é o mesmo nas duas perspectivas do jogo real
    e não sofre desse problema. Mercados de TIME (não deduplicados no
    arquivamento) continuam usando `jogo_id`, que aqui é seguro e
    necessário pra não confundir a aposta do nosso time com a do
    adversário.

    `fixture_id_api` pode vir None (perna capturada antes dessa correção,
    ou mercado de time) - nesse caso cai automaticamente no casamento por
    jogo_id, idêntico ao comportamento anterior."""
    usa_fixture = (
        fixture_id_api is not None
        and (tipo_padrao in MERCADOS_JOGO_INTEIRO or tipo_padrao in MERCADOS_JOGADOR)
    )

    if usa_fixture:
        cur.execute(
            """
            SELECT hr.resultado
            FROM historico_recomendacoes hr
            JOIN jogos j ON j.id = hr.jogo_id
            WHERE j.fixture_id_api = %s
              AND hr.tipo_padrao = %s
              AND hr.jogador_id IS NOT DISTINCT FROM %s
              AND hr.linha IS NOT DISTINCT FROM %s
              AND LOWER(TRIM(hr.direcao)) = LOWER(TRIM(%s))
            ORDER BY hr.id DESC LIMIT 1
            """,
            (fixture_id_api, tipo_padrao, jogador_id, linha, direcao or ""),
        )
    else:
        cur.execute(
            """
            SELECT resultado FROM historico_recomendacoes
            WHERE jogo_id = %s
              AND tipo_padrao = %s
              AND jogador_id IS NOT DISTINCT FROM %s
              AND linha IS NOT DISTINCT FROM %s
              AND LOWER(TRIM(direcao)) = LOWER(TRIM(%s))
            ORDER BY id DESC LIMIT 1
            """,
            (jogo_id, tipo_padrao, jogador_id, linha, direcao or ""),
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
        SELECT assinatura, casa_aposta, descricao, odd_combinada, jogos, probabilidade_combinada, resultado
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
    for assinatura, casa, descricao, odd, jogos, prob, resultado in top5:
        cur.execute(
            """
            INSERT INTO historico_multiplas_destaque
                (jogo_id, rodada, casa_aposta, descricao, odd_combinada, jogos,
                 probabilidade_combinada, resultado, assinatura)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (jogo_id, rodada, casa, descricao, odd, json.dumps(jogos, default=str), prob, resultado, assinatura),
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
            # NOVO (Fase 2, 08/09/2026): passa também `fixture_id_api` (via
            # .get - candidata antiga não tem essa chave) e `linha`/
            # `direcao` no lugar de `descricao` - ver a docstring de
            # buscar_resultado_perna.
            resultado_perna = buscar_resultado_perna(
                cur,
                perna["tipo_padrao"],
                perna["jogo_id"],
                perna.get("fixture_id_api"),
                perna["jogador_id"],
                perna["linha"],
                perna["direcao"],
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
