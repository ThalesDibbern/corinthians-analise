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

NOVO (16/09/2026 - DIAGNÓSTICO NO LOG. O ARQUIVAMENTO NÃO MUDA.):

⚠️ LEIA ANTES DE "CONSERTAR" ESTE ARQUIVO. Em 16/09 uma trava foi escrita
aqui e REVERTIDA no mesmo dia, depois que a fonte externa derrubou a
premissa dela. O registro fica para que ninguém a reescreva.

A SUSPEITA QUE MOTIVOU TUDO: parecia que 273 recomendações da rodada 27
tinham sido arquivadas ANTES do jogo - o fixture 1492374 com 31h48 de
antecedência. Isso seria grave, porque `historico_recomendacoes` CONGELA
o veredito e mercado avaliado contra PLACAR nunca se cura.

O QUE A FONTE EXTERNA MOSTROU (ge/imprensa esportiva, 16/09): a rodada 27
foi disputada em QUATRO dias - sexta 11/09 (Coritiba x Athletico-PR),
sábado 12/09 (cinco jogos), domingo 13/09 (dois jogos) e segunda 14/09
(Bahia x Remo). O banco carimbou NOVE desses dez fixtures com o MESMO
`2026-09-13 20:00:00`, nas DUAS tabelas (`jogos` e `jogos_liga`) - por
isso a checagem de divergência entre elas não acusou nada.

Conferido fixture a fixture: TODO arquivamento aconteceu no primeiro cron
depois do jogo realmente terminar. NENHUM foi prematuro. A "antecedência"
era a data falsa medindo errado.

  - 1492374 (jogo sexta 11/09) -> arquivado 12/09 12:07 ✓
  - os cinco de sábado 12/09   -> arquivados 13/09 12:12 ✓
  - os dois de domingo 13/09   -> arquivados 14/09 12:09 ✓
  - 1492371 (segunda 14/09)    -> arquivado 15/09 12:12 ✓

POR QUE A TRAVA FOI REVERTIDA - ela pioraria o dado, não melhoraria:

A trava adiava o arquivamento enquanto `datahora_jogo` não chegasse. Com
a data errada apontando para o futuro, esses jogos ficariam presos em
`recomendacoes` - e `salvar_recomendacoes` APAGA E REGRAVA jogo futuro
(ver o `DELETE` de lá). A recomendação limpa, gerada antes do jogo, seria
DESTRUÍDA, e o único registro que sobraria seria o regerado DEPOIS do
jogo, contaminado por look-ahead (`motor_padroes` já teria colocado o
próprio jogo na janela de 50).

Hoje, sem trava: sobram cópias, e a dedup guarda a de MENOR
`arquivado_em` - que é a limpa. A duplicação estava PROTEGENDO a
medição. A trava tirava a proteção.

ONDE O BUG REALMENTE ESTÁ - e não é aqui:

Este arquivo confia no `status` da liga. `motor_recomendacoes.
buscar_odds_futuras` NÃO confia: decide só por data. Com a data errada,
ele regera recomendação de jogo já disputado, e cada regeração vira uma
cópia no histórico. O conserto é lá (não gerar para fixture cujo
`jogos_liga.status` já diz encerrado), não aqui. Ver `2_ABERTO`.

O QUE ESTE ARQUIVO PASSA A FAZER - só isto, e só no log:

  1. Imprime, para cada linha, QUAL dos três ramos do WHERE a tornou
     elegível. É o que faltava em 15/09 e obrigou a deduzir por
     eliminação durante dois dias.

  2. AVISA (sem bloquear) quando arquiva uma linha cujo `datahora_jogo`
     ainda está no futuro. Agora se sabe o que isso significa: a DATA
     daquele fixture está errada. É o detector do bug real, e ele
     imprime datahora, as datas das duas tabelas, status, placar,
     relógio e fuso - tudo no instante da decisão, antes que o
     `popular_banco` do dia seguinte sobrescreva.

  3. MODO DIAGNÓSTICO: `python arquivar_recomendacoes.py --diagnostico`
     roda a consulta, classifica, imprime tudo e faz ROLLBACK. Não
     escreve nada.

O comportamento de arquivamento é BYTE A BYTE o de antes. Nenhuma linha é
retida, nenhuma é arquivada que não fosse antes.

Variáveis de ambiente:
  - DATABASE_URL -> a URL de conexão do Postgres (mesma usada nos outros scripts)
"""

import json
import os
import sys
from datetime import datetime

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


def e_aposta_de_jogador(tipo_padrao, jogador_id):
    """NOVO (07/10/2026 - cartão de TIME fundido entre os dois times):
    `cartao` está em MERCADOS_JOGADOR, mas tem DOIS formatos (ver
    avaliacao.py): de JOGADOR (jogador_id preenchido) e de TIME ("Cartões -
    Mais/Menos <time>", jogador_id vazio). Só o primeiro é a mesma aposta
    nas duas perspectivas.

    Antes, o cartão de TIME era tratado como de jogador nos dois lugares
    abaixo, com jogador_id NULL na chave:
      - no arquivamento, "Mais de 2.5 cartões do Palmeiras" e "Mais de 2.5
        cartões do São Paulo" (mesmo fixture, mesma linha e direção) viravam
        UMA linha só, com a probabilidade das duas misturada - a aposta
        descartada sumia do histórico;
      - no casamento de perna, a perna de um time podia receber o veredito
        do OUTRO. Caso real: múltipla 31636 (Palmeiras x São Paulo, 13/09)
        marcada `acertou` com a perna do São Paulo (2 cartões) avaliada pela
        linha do Palmeiras (4 cartões) - deveria ser `errou`.
    Agora o cartão de TIME cai no ramo de mercado de TIME (identidade por
    `jogo_id`), como escanteio_time e gols_time. Nenhum outro tipo de
    MERCADOS_JOGADOR existe sem jogador_id, então para eles nada muda."""
    return tipo_padrao in MERCADOS_JOGADOR and jogador_id is not None

# NOVO (16/09/2026): os três ramos do WHERE ganham NOME, pra que o log
# possa dizer qual deles tornou cada linha elegível. Sem isso, quando o
# arquivamento prematuro acontece, a única saída é deduzir por eliminação
# dias depois - foi exatamente o que custou a investigação de 15-16/09.
RAMO_DATAHORA = "datahora_jogo ja passou"
RAMO_DATA_SEM_HORA = "data_jogo ja passou (datahora nula)"
RAMO_STATUS = "status da liga diz encerrado"

STATUS_ENCERRADOS = ("FT", "AET", "PEN")


def _classificar_ramos(datahora_jogo, data_jogo, status, agora, hoje):
    """Diz QUAIS ramos do WHERE são verdadeiros para esta linha.

    Repete em Python a mesma lógica do SQL, de propósito - é o preço de
    poder nomear a causa no log. Para que a repetição não divirja, os
    dois lados comparam contra os MESMOS valores: `agora` vem de
    LOCALTIMESTAMP (timestamp sem fuso, igual a `datahora_jogo`) e `hoje`
    de CURRENT_DATE, ambos lidos na MESMA consulta que trouxe as linhas.

    Por que LOCALTIMESTAMP e não NOW(): `datahora_jogo` é `timestamp
    without time zone` e NOW() é `timestamptz`. O Postgres resolve essa
    comparação convertendo a naive com o fuso da SESSÃO. LOCALTIMESTAMP é
    NOW() já convertido com esse mesmo fuso - então comparar em Python
    contra ele dá exatamente o resultado que o SQL deu, qualquer que seja
    o fuso configurado. (Se um dia o fuso da sessão deixar de ser UTC,
    isto continua correto, e o cabeçalho do log mostra qual é.)"""
    ramos = []
    if datahora_jogo is not None and datahora_jogo < agora:
        ramos.append(RAMO_DATAHORA)
    if datahora_jogo is None and data_jogo is not None and data_jogo < hoje:
        ramos.append(RAMO_DATA_SEM_HORA)
    if status in STATUS_ENCERRADOS:
        ramos.append(RAMO_STATUS)
    return ramos


def _data_do_jogo_e_suspeita(datahora_jogo, data_liga, agora, hoje):
    """DETECTOR - não bloqueia nada. Ver o item 2 do NOVO de 16/09.

    Esta função já foi uma TRAVA, e a reversão dela é o ponto inteiro:
    quando uma linha chega aqui, ela é elegível para arquivamento por
    algum ramo do WHERE, mas a data gravada do jogo ainda aponta para o
    futuro. As duas coisas não podem ser verdade ao mesmo tempo - logo a
    DATA está errada.

    Foi exatamente o que aconteceu com a rodada 27: nove jogos disputados
    entre sexta e domingo, todos carimbados com `2026-09-13 20:00:00`. O
    `status` da liga estava certo, a data é que não estava.

    Reter a linha seria o erro (ver o NOVO no topo). O certo é arquivar
    normalmente e GRITAR no log, porque este é o sintoma visível do bug
    de data - e o único momento em que o dado que o comprova ainda
    existe, antes do `popular_banco` do dia seguinte reescrevê-lo.

    Duas fontes, porque uma sozinha não enxerga os dois casos:

    (1) `jogos.datahora_jogo` no futuro - a data de `jogos` está errada.
    (2) `jogos_liga.data_jogo` no futuro - a data da liga está errada,
        mesmo que a de `jogos` pareça certa. `data_liga` nula (fixture
        sem linha em `jogos_liga`) não acusa nada, igual ao motivo pelo
        qual o JOIN é LEFT."""
    if datahora_jogo is not None and datahora_jogo > agora:
        return True
    if data_liga is not None and data_liga > hoje:
        return True
    return False


def _descrever_linha_suspeita(rec):
    """Uma linha de log com TUDO que identifica o fixture de data errada,
    lido no instante da decisão - antes que o próximo cron sobrescreva."""
    (rec_id, jogo_id, _jogador_id, tipo_padrao, _descricao, _casa,
     _odd, _prob, _ve, _linha, _direcao, data_jogo, fixture_id_api,
     datahora_jogo, status_liga, data_liga, placar_m, placar_v) = rec[:18]
    return (
        f"  rec_id={rec_id} fixture={fixture_id_api} jogo_id={jogo_id} "
        f"mercado={tipo_padrao} | jogos.datahora={datahora_jogo} "
        f"jogos.data={data_jogo} | jogos_liga.data={data_liga} "
        f"status={status_liga} placar={placar_m}x{placar_v}"
    )


def buscar_recomendacoes_para_arquivar(cur):
    """NOVO: arquiva TODO jogo já passado, sem exceção de "últimas rodadas
    mantidas detalhadas" (ver nota no topo do arquivo).

    NOVO (correção de duplicata): agrupa por identidade real da aposta
    (ver MERCADOS_JOGO_INTEIRO/MERCADOS_JOGADOR) - quando duas linhas em
    `recomendacoes` são, na prática, a MESMA aposta real (vindas das duas
    perspectivas de um jogo entre times rastreados), só a de maior
    probabilidade histórica vira uma entrada em historico_recomendacoes;
    a(s) outra(s) ainda são apagadas de `recomendacoes` (não ficam presas
    lá pra sempre), só não geram um registro duplicado no histórico.

    NOVO (16/09/2026): a consulta passa a trazer também as colunas que
    DECIDEM a elegibilidade (`j.datahora_jogo`, `jl.status`,
    `jl.data_jogo`, os placares) e o relógio/data da própria sessão. Elas
    não entram no arquivamento e NÃO filtram nada - servem só para
    classificar o ramo e imprimir o diagnóstico. Ficam DEPOIS do índice
    12, então todo o fatiamento posterior (`representante[:12]`, `r[7]`,
    `representante[9:12]`) continua apontando para os mesmos campos de
    antes. Ver o NOVO no topo do arquivo.

    ⚠️ O conjunto de linhas arquivadas é IDÊNTICO ao de antes de 16/09.

    Devolve `(a_arquivar, suspeitas_de_data, contagem_por_ramo, relogio)`."""
    cur.execute(
        """
        SELECT r.id, r.jogo_id, r.jogador_id, r.tipo_padrao, r.descricao, r.casa_aposta,
               r.odd_oferecida, r.probabilidade_historica, r.valor_esperado, r.linha,
               r.direcao, j.data_jogo, j.fixture_id_api,
               j.datahora_jogo, jl.status, jl.data_jogo, jl.placar_mandante,
               jl.placar_visitante, LOCALTIMESTAMP, CURRENT_DATE,
               current_setting('TimeZone')
        FROM recomendacoes r
        JOIN jogos j ON j.id = r.jogo_id
        LEFT JOIN jogos_liga jl ON jl.fixture_id_api = j.fixture_id_api
        WHERE (j.datahora_jogo IS NOT NULL AND j.datahora_jogo < NOW())
           OR (j.datahora_jogo IS NULL AND j.data_jogo < CURRENT_DATE)
           OR jl.status IN ('FT', 'AET', 'PEN')
        """
    )
    linhas = cur.fetchall()

    if not linhas:
        cur.execute("SELECT LOCALTIMESTAMP, CURRENT_DATE, current_setting('TimeZone')")
        agora, hoje, fuso = cur.fetchone()
        return [], [], {}, (agora, hoje, fuso)

    agora, hoje, fuso = linhas[0][18], linhas[0][19], linhas[0][20]
    relogio = (agora, hoje, fuso)

    suspeitas = []
    contagem_ramos = {}

    for rec in linhas:
        datahora_jogo, status_liga = rec[13], rec[14]
        ramos = _classificar_ramos(datahora_jogo, rec[11], status_liga, agora, hoje)
        chave_ramo = " + ".join(ramos) if ramos else "NENHUM (impossivel - investigar)"
        contagem_ramos[chave_ramo] = contagem_ramos.get(chave_ramo, 0) + 1

        # ⚠️ APENAS ANOTA. Não há `continue` aqui, e não pode haver - ver o
        # NOVO de 16/09 no topo: reter a linha destrói a versão limpa da
        # recomendação. Toda linha elegível segue para o arquivamento.
        if _data_do_jogo_e_suspeita(datahora_jogo, rec[15], agora, hoje):
            suspeitas.append((rec, chave_ramo))

    grupos = {}
    for rec in linhas:
        (rec_id, jogo_id, jogador_id, tipo_padrao, descricao, casa,
         odd, prob, ve, linha, direcao, data_jogo, fixture_id_api) = rec[:13]

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
        elif e_aposta_de_jogador(tipo_padrao, jogador_id):  # NOVO (07/10): cartão de TIME fica de fora
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
    return resultado, suspeitas, contagem_ramos, relogio


def imprimir_diagnostico(a_arquivar, suspeitas, contagem_ramos, relogio):
    """Imprime a PROVA - ver o item 2 do NOVO de 16/09 no topo do arquivo.

    Roda sempre, no cron e no modo diagnóstico. É barato (algumas dezenas
    de linhas) e é a única chance de capturar o estado no instante da
    decisão: `jogos_liga.atualizado_em` e `jogos.datahora_jogo` são
    reescritos pelo `popular_banco` no cron seguinte."""
    agora, hoje, fuso = relogio
    print("=" * 72)
    print("ARQUIVAMENTO - diagnostico de elegibilidade")
    print(f"  relogio da sessao : {agora}  (CURRENT_DATE={hoje}, TimeZone={fuso})")
    print(f"  argv              : {sys.argv}")
    print(f"  relogio do processo: {datetime.now().isoformat(timespec='seconds')}")
    print("-" * 72)

    if contagem_ramos:
        print("  Ramo do WHERE que tornou cada linha elegivel:")
        for ramo, quantas in sorted(contagem_ramos.items(), key=lambda p: -p[1]):
            print(f"    {quantas:5d}  {ramo}")
    else:
        print("  Nenhuma linha candidata.")

    if suspeitas:
        print("-" * 72)
        print(f"  ⚠️  DATA ERRADA em {len(suspeitas)} linha(s) - ELAS SAO ARQUIVADAS NORMALMENTE.")
        print("      O jogo ja terminou (algum ramo do WHERE confirma), mas a data")
        print("      gravada ainda aponta para o futuro. As duas coisas nao podem")
        print("      ser verdade: a DATA do fixture esta errada.")
        print("      Consequencia real: `motor_recomendacoes.buscar_odds_futuras`")
        print("      decide so por data, entao ele REGERA recomendacao desse jogo")
        print("      ja disputado - e cada regeracao vira uma copia com look-ahead")
        print("      no historico. Corrigir a data (ou o motor), nunca reter aqui.")
        fixtures_afetados = set()
        for rec, chave_ramo in suspeitas:
            fixtures_afetados.add(rec[12])
            print(f"    [{chave_ramo}]")
            print(_descrever_linha_suspeita(rec))
        print(f"      fixtures com data errada: {sorted(f for f in fixtures_afetados if f is not None)}")
    else:
        print("  Datas: nenhum fixture com data no futuro sendo arquivado. OK.")

    print("-" * 72)
    print(f"  A arquivar agora: {len(a_arquivar)} aposta(s) apos deduplicacao.")
    print("=" * 72)


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
        and (tipo_padrao in MERCADOS_JOGO_INTEIRO or e_aposta_de_jogador(tipo_padrao, jogador_id))
    )  # NOVO (07/10): cartão de TIME casa por jogo_id - ver e_aposta_de_jogador

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
    # NOVO (16/09/2026): modo diagnóstico. Roda a mesma consulta e a mesma
    # classificação, imprime tudo e faz ROLLBACK - não escreve nada. É a
    # fase `verificar` que toda mudança que toca dado precisa ter neste
    # projeto, aplicada a um script que antes só tinha a fase `aplicar`.
    somente_diagnostico = "--diagnostico" in sys.argv

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        recomendacoes, suspeitas, contagem_ramos, relogio = (
            buscar_recomendacoes_para_arquivar(cur)
        )
        imprimir_diagnostico(recomendacoes, suspeitas, contagem_ramos, relogio)

        if somente_diagnostico:
            conn.rollback()
            print("\nMODO DIAGNOSTICO: rollback feito, nada foi escrito.")
            return

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
