"""
Motor de recomendações - cruza os padrões já calculados (motor_padroes.py)
com as odds reais coletadas (atualizar_odds.py) para encontrar apostas
com "valor esperado" positivo: onde a probabilidade histórica do padrão
acontecer é maior do que a odd da casa está sugerindo.

NOVO: quando o jogo já tem o árbitro confirmado (salvo pelo atualizar_odds.py)
e existe um perfil calculado pra ele (motor_padroes.py), a probabilidade de
cartão e falta é ajustada pelo "fator" desse árbitro - juízes que dão mais
cartão que a média puxam a probabilidade pra cima, os que seguram mais o
cartão puxam pra baixo. O ajuste é limitado a um intervalo (0.85x a 1.15x)
pra não deixar uma amostra ainda pequena por árbitro dominar a conta.

NOVO (confronto direto): pros mercados de escanteio total, cartão total e
resultado final, o sistema tenta primeiro usar a frequência histórica
ESPECÍFICA contra aquele adversário (ex: "cartões totais contra o Palmeiras,
jogando em casa"), calculada pelo motor_padroes.py em padroes_confronto_direto.
Só cai pra média geral do time (sem filtrar por adversário) se não houver
confronto direto com amostra suficiente ainda. Isso captura rivalidades e
mandos de campo específicos que a média geral não enxerga (ex: um confronto
historicamente mais truncado, ou um adversário que o Corinthians nunca perde
em casa). Quando esse dado é usado, a descrição da recomendação ganha o sufixo
"(confronto direto)".

NOVO (forma recente): pro mercado de resultado final, depois de decidir a
probabilidade principal (confronto direto ou média geral), o sistema aplica
um pequeno ajuste baseado no "momento atual" do time (últimos 5 jogos,
independente de adversário) - times em boa fase têm a probabilidade de
vitória/empate levemente puxada pra cima, times em má fase levemente pra
baixo. O ajuste é limitado a um intervalo estreito (0.85x a 1.15x) e NUNCA
domina sobre o confronto direto ou a média geral - só "belisca" o número,
igual já acontece com o ajuste de árbitro em cartão/falta.

NOVO (disponibilidade de jogador): antes de gerar qualquer recomendação de
mercado específico de jogador (cartão, falta, desarme, chute, impedimento),
o sistema checa se ele provavelmente vai jogar. Prioridade 1: escalação
CONFIRMADA da partida específica, se já capturada pelo popular_banco.py.
Prioridade 2 (fallback, quando a escalação da partida ainda não saiu):
olha se o jogador apareceu em pelo menos 1 dos últimos 3 jogos - se sumiu
das 3 escalações seguidas, é sinal de lesão/suspensão/corte do time, e a
recomendação é descartada. Evita recomendar aposta em jogador fora de
campo.

NOVO (Grupo A de integração - Estilo de Jogo, Padrão por Rodada, Zona da
Tabela, 11/08/2026): três recursos que antes eram só visuais passam a
ajustar recomendações de verdade, nos 4 pontos de encaixe onde existe um
mercado apostável no nível certo (time ou jogador, batendo com o que a
OddsPapi oferece):
  - Resultado final (time): Zona da Tabela + Padrão por Rodada
  - Escanteio de time: Zona da Tabela + Padrão por Rodada
  - Cartão de jogador: Estilo de Jogo (ofensivo do nosso time + defensivo
    do adversário) + Árbitro (já existia)
  - Impedimento de jogador: Estilo de Jogo (ofensivo do nosso time +
    defensivo do adversário)
As outras estatísticas de Zona/Rodada (falta, chute, chute no gol,
desarme, cartão do time) ficam de fora dessa integração de propósito -
só existem como mercado POR JOGADOR na OddsPapi, não como total de time,
então não há onde encaixar um ajuste calculado no nível de time sem
inventar uma correspondência que o dado não sustenta.

Cada fator individual continua com piso/teto próprio (0.85x-1.15x, igual
Árbitro/Forma recente já usavam) - é a primeira camada de segurança. Por
cima disso, o PRODUTO de todos os fatores que atuam no mesmo mercado ao
mesmo tempo é limitado a um TETO COMBINADO (0.75x-1.25x) - segunda camada,
garante que múltiplos ajustes concordando entre si nunca dominam sobre o
padrão histórico real, mesmo no pior caso (3 fatores empilhados em
Resultado Final ou Cartão de Jogador).

Cada fator só entra na conta se tiver pelo menos
JOGOS_MINIMOS_FATOR_RECOMENDACAO jogos de amostra - abaixo disso, o fator
é ignorado silenciosamente (sem quebrar a recomendação, só não ajusta).

Fórmula usada (valor esperado por unidade apostada):
    VE = (probabilidade_historica * odd) - 1
Se VE > 0, a aposta é estatisticamente favorável no longo prazo, segundo
o nosso histórico.

IMPORTANTE: isso não é garantia de acerto em uma aposta individual - é uma
estimativa baseada em dados históricos, que só faz sentido com dados de
jogadores/temporada atuais. Enquanto o banco ainda é de 2022-2024, use
os resultados aqui só para validar a lógica, não para apostar de verdade.

Só considera odds de jogos que ainda vão acontecer (data_jogo >= hoje).

Variáveis de ambiente necessárias:
  - DATABASE_URL -> a URL de conexão do Postgres (mesma usada nos outros scripts)
"""

import os
import psycopg2

from tabela import calcular_tabela

DATABASE_URL = os.environ["DATABASE_URL"]

VALOR_ESPERADO_MINIMO = 0.0  # só guarda recomendações com VE acima disso

# =====================================================================
# NOVO (03/09/2026) - CORREÇÃO DA SELEÇÃO ADVERSA DO VE (pendência 28)
# =====================================================================
#
# O PROBLEMA: quando a probabilidade que o modelo calcula diverge MUITO
# da probabilidade que a odd da casa implica, essa divergência quase
# sempre é o MODELO errando - não uma vantagem real. Medido sobre 939
# recomendações já avaliadas, controlando pela faixa de probabilidade
# prevista (o que ISOLA o mecanismo - não é só "a probabilidade está
# mal calibrada"):
#
#   faixa de prob. prevista | gap com VE baixo | gap com VE alto
#   40-60%                  |       -5,1       |      -23,5
#   60-80%                  |       -8,6       |      -25,3
#   80-100%                 |       -9,1       |      -16,8
#
# Mesma probabilidade prevista, só muda o quanto o modelo discorda da
# casa - e o erro dispara. Nas faixas de VE 0,30-0,60 a odd da casa
# acertou o resultado real quase exato (36,06% implícito vs 35,94%
# observado) enquanto o modelo errava por 12-15 pontos. A casa é um
# previsor MELHOR que o modelo justamente onde o VE é alto.
#
# A CORREÇÃO: encolher a probabilidade do modelo em direção à implícita
# na odd, proporcional à divergência. É a MESMA lógica do confronto
# direto como prior (seção 41), agora aplicada contra o mercado:
#
#     p_mercado = 100 / odd
#     diff      = p_modelo - p_mercado
#     peso      = 1 / (1 + K * |diff| / 100)
#     p_final   = p_mercado + diff * peso
#
# K=15 escolhido por backtest (03/09/2026): resolve quase todo o gap da
# faixa mais grave (0,60+: -13,6 -> -1,1) sem super-corrigir a faixa
# que já estava calibrada (0,10-0,20, que vira só +1,5). K maior segue
# melhorando as faixas do meio mas PIORA a 0,10-0,20 e a 0,60+ pro
# outro lado - não existe K que otimize todas, 15 é o equilíbrio.
#
# ⚠️ A fórmula sozinha NÃO filtra nada. Como p_final fica sempre ENTRE
# p_modelo e p_mercado, o VE encolhe mas nunca cruza zero - medido: 0
# recomendações mudariam de lado do filtro VE>0. Por isso o piso abaixo
# é parte da correção, não um extra.
VALOR_K_ENCOLHIMENTO_MERCADO = 15.0

# Piso de VE (já corrigido) pros mercados do escopo.
#
# ⚠️ DESLIGADO NESTA ETAPA (0.0 = mesmo comportamento de antes).
# DELIBERADO, não esquecimento. Só o ENCOLHIMENTO entra agora.
#
# POR QUÊ: um piso uniforme é muito mais exigente em odd BAIXA do que em
# odd alta, porque o VE é percentual (p * odd - 1) - a mesma divergência
# absoluta rende menos VE quanto menor a odd. Medido, com piso 0,05:
#
#   odd 1.20 -> o modelo precisa divergir 11,1 pontos da casa pra passar
#   odd 2.00 -> 4,0 pontos
#   odd 5.00 -> 1,2 ponto
#   odd 10.0 -> 0,6 ponto
#
# Isso corta MAIS onde o app erra MENOS: a faixa de VE corrigido
# 0,00-0,02 (a que o piso mataria primeiro) tinha 58,8% de acerto, odd
# média 1,94 e ROI de só -4,2%. Já a faixa 0,20+ (odd média 11,45, 12,6%
# de acerto) passaria folgada. Efeito perverso pro objetivo de "mostrar
# as melhores" - e atinge em cheio as pernas de múltipla, que vivem na
# faixa 1.20-1.60.
#
# O ROI por faixa que definiria o piso (medido 03/09/2026) também não
# sustenta uma escolha confiante ainda:
#
#   faixa VE corrigido |  n  | ROI     | margem de erro
#   0,00-0,02          |  97 | -4,2%   | confiável (perde)
#   0,02-0,05          | 212 | -2,5%   | confiável (perde)
#   0,05-0,10          | 347 | +1,2%   | ~+-6 pts  -> indistinguível de 0
#   0,10-0,20          | 188 | +3,7%   | ~+-13 pts -> indistinguível de 0
#   0,20+              |  95 | +11,8%  | ~+-39 pts -> RUÍDO (só 12 vitórias,
#                                        cada uma move o ROI em ~12 pts)
#
# DECISÃO: medir o encolhimento sozinho na rodada de 05/09/2026 e só
# então escolher o piso, com dado JÁ produzido pela fórmula nova. Escolher
# agora seria calibrar sobre um mundo que deixou de existir. Uma etapa
# por vez é o que torna a próxima medição interpretável - mesma razão
# pela qual a seção 29 (Dupla Chance) segue em etapas.
#
# Pra ligar depois: trocar por 0.05 (ou o valor que a medição indicar).
VALOR_ESPERADO_MINIMO_CORRIGIDO = 0.0

# ESCOPO da correção. Só os mercados SEM problema de calibração próprio.
# resultado_final/forma recente e correlação ficam FORA de propósito:
# eles erram muito MESMO com VE baixo (gaps de -19 a -26 já na faixa
# 0,00-0,10), ou seja, têm um segundo defeito que esta correção não
# trata. Aplicar aqui seria empilhar conserto em cima de bug e perder a
# capacidade de saber qual dos dois resolveu o quê.
#
#   - resultado_final: aguarda a medição da rodada de 05/09/2026, que já
#     tem critério de reversão definido (seção 56). Não dá pra separar
#     "antes/depois" da correção de simetria hoje - só 1 linha nova.
#   - correlacao: problema próprio, nunca investigado. Pendência à parte.
#
# Esses dois também servem de GRUPO DE CONTROLE: se os mercados do
# escopo melhorarem e eles não, o mérito é desta correção.
MERCADOS_CORRECAO_DIVERGENCIA_MERCADO = {
    "escanteio_time", "escanteio_total",
    "cartao", "cartao_total",
    "gols_time", "gols_total",
    "ambas_marcam_1t", "ambas_marcam_2t",
    "marca_ambos_tempos",
    "equipe_marca",
    "chute_no_gol", "falta_cometida",
}

# limites do ajuste de árbitro - evita que uma amostra pequena por árbitro
# distorça demais a probabilidade calculada a partir dos últimos 50 jogos do jogador
FATOR_ARBITRO_MINIMO = 0.85
FATOR_ARBITRO_MAXIMO = 1.15

# NOVO (forma recente): mesma filosofia do ajuste de árbitro - o momento
# atual do time só belisca a probabilidade de resultado final, nunca domina
# sobre uma fonte mais específica (como o confronto direto).
FATOR_FORMA_MINIMO = 0.85
FATOR_FORMA_MAXIMO = 1.15

# NOVO (cartão x suspensão): quanto mais jogadores dos dois times estão a
# 1 cartão amarelo da suspensão automática (regra do Brasileirão: 3
# cartões = 1 jogo de suspensão), mais cauteloso o jogo tende a ser -
# ninguém quer arriscar ficar de fora do próximo jogo. Esse ajuste só
# REDUZ a probabilidade de "mais de X cartões" (por isso o teto é 1.0,
# nunca aumenta), com um piso pra não dominar sobre o padrão real mesmo
# em cenários extremos (ex: os 2 times inteiros na régua).
# DESATIVADO (11/08/2026): testado numa rodada real e não se comportou
# bem - tirado da fórmula de cartao_total por pedido do dono do projeto.
# As constantes e a função continuam aqui, só não são mais chamadas.
FATOR_SUSPENSAO_MINIMO = 0.80
FATOR_SUSPENSAO_MAXIMO = 1.00
FATOR_SUSPENSAO_ESCALA = 0.15
PESO_PADRAO_SEM_HISTORICO_CARTAO = 0.15  # jogador na régua sem padrão calculado ainda (poucos jogos) - peso neutro
TEMPORADA_ATUAL = 2026  # cartão não carrega de uma temporada pra outra

# NOVO (Grupo A): piso/teto individual dos 4 fatores novos - igualado ao
# mesmo intervalo que Árbitro/Forma recente já usam, por consistência.
FATOR_ZONA_MINIMO = 0.85
FATOR_ZONA_MAXIMO = 1.15
FATOR_RODADA_MINIMO = 0.85
FATOR_RODADA_MAXIMO = 1.15
FATOR_ESTILO_MINIMO = 0.85
FATOR_ESTILO_MAXIMO = 1.15

# NOVO (Correlação entre Estatísticas na fórmula - 18/08/2026): mesma faixa
# individual dos outros fatores do Grupo A. Esse é o PRIMEIRO fator do
# Grupo A a atuar em mercado de JOGO INTEIRO (escanteio_total/cartao_total);
# todos os outros (Zona/Rodada/Estilo) são por time ou por jogador.
FATOR_CORRELACAO_MINIMO = 0.85
FATOR_CORRELACAO_MAXIMO = 1.15

# NOVO (Grupo A): teto combinado - depois de multiplicar TODOS os fatores
# que atuam no mesmo mercado ao mesmo tempo, o produto final nunca passa
# desse intervalo, não importa quantos fatores concordaram entre si.
TETO_COMBINADO_MINIMO = 0.75
TETO_COMBINADO_MAXIMO = 1.25

# NOVO (Grupo A): amostra mínima pra um fator valer numa recomendação de
# verdade - mais rígido que o mínimo usado nos cards visuais (3, pra não
# ficar tudo vazio na tela), porque aqui o número já influencia dinheiro.
JOGOS_MINIMOS_FATOR_RECOMENDACAO = 5

# NOVO (correção de amostra pequena - Zona da Tabela, investigação de
# 17/08/2026): a condição de momento (apos_vitoria/apos_empate/
# apos_derrota) divide a amostra do time em 3 fatias, então bate no piso
# de 5 jogos com muito mais facilidade que o "geral" (que junta tudo).
# Cruzando 209 recomendações reais contra o resultado de verdade, o
# cenário "reforça uma aposta de Mais, baseado numa condição de momento"
# teve taxa de acerto de só 20% (N=15) - claro sinal de overfitting em
# amostra pequena. Piso mais alto só pra essa fatia (momento específico);
# o "geral" continua com o piso normal, porque na prática já tem amostra
# grande (raramente abaixo de 14 jogos, olhando o banco).
JOGOS_MINIMOS_FATOR_ZONA_MOMENTO = 10



# NOVO (disponibilidade de jogador): quantos jogos recentes olhar pra decidir
# se um jogador "sumiu" da escalação (sinal de lesão/suspensão/corte do
# time) - só usado quando ainda não temos a escalação confirmada da
# partida específica (ver jogador_disponivel).
JOGOS_JANELA_DISPONIBILIDADE = 3


def encolher_para_o_mercado(frequencia, valor_odd, tipo_padrao):
    """Encolhe a probabilidade do modelo em direção à implícita na odd da
    casa, proporcional ao tamanho da divergência.

    Ver o bloco de constantes no topo (VALOR_K_ENCOLHIMENTO_MERCADO) pro
    diagnóstico completo e os números que justificam K=15.

    Resumo do mecanismo: divergência PEQUENA passa quase intacta (o
    modelo e a casa concordam, não há nada a corrigir); divergência
    GRANDE é puxada com força pra perto da odd, porque foi medido que
    nesses casos a casa acerta e o modelo não.

    Devolve `(frequencia_final, houve_ajuste)`. Fora do escopo ou sem odd
    utilizável, devolve a frequência original intacta - nunca levanta.

    ⚠️ p_final fica sempre ENTRE p_modelo e p_mercado, então o VE encolhe
    mas nunca vira negativo por conta disso. Quem filtra é o piso
    VALOR_ESPERADO_MINIMO_CORRIGIDO, não esta função.
    """
    if tipo_padrao not in MERCADOS_CORRECAO_DIVERGENCIA_MERCADO:
        return frequencia, False

    try:
        odd = float(valor_odd)
    except (TypeError, ValueError):
        return frequencia, False

    if odd <= 0:
        return frequencia, False

    p_mercado = 100.0 / odd
    diff = frequencia - p_mercado

    # Divergência desprezível: não mexe. Evita poluir a descrição na tela
    # com "(ajustado ...)" quando o ajuste não muda nada de fato.
    if abs(diff) < 0.01:
        return frequencia, False

    peso = 1.0 / (1.0 + VALOR_K_ENCOLHIMENTO_MERCADO * abs(diff) / 100.0)
    p_final = p_mercado + diff * peso

    # Clamp defensivo: a fórmula não pode sair de [0, 100] por
    # construção (p_final fica entre p_modelo e p_mercado, ambos válidos),
    # mas uma odd corrompida no banco (< 1.0) faria p_mercado passar de
    # 100. Barato garantir aqui do que rastrear depois.
    p_final = max(0.0, min(100.0, p_final))

    return round(p_final, 2), True


def identificar_tipo_padrao(mercado):
    """Adivinha a que tipo de padrão um mercado se refere, a partir do nome
    (em português, como vem da OddsPapi).

    NOVO: os mercados de total do jogo (escanteios/cartões somando os dois
    times) são checados ANTES dos mercados por time/jogador, porque o nome
    deles ("Escanteios Total do Jogo", "Cartões Total do Jogo") também
    contém as palavras "escanteio"/"cartão" - sem essa ordem, cairiam por
    engano nos tipos genéricos (escanteio_time/cartao)."""
    nome = mercado.lower()
    # NOVO (bug corrigido): a checagem antiga procurava pelo texto exato
    # "escanteio total do jogo" (singular) - mas o nome real que a OddsPapi
    # manda é "Escanteios Total do Jogo" (plural, com "s"). Como a
    # comparação era de substring exata, o "s" extra quebrava a
    # correspondência e esse mercado NUNCA era classificado corretamente -
    # caía em None silenciosamente, e nenhuma recomendação de escanteio/
    # cartão total do jogo era gerada, mesmo com VE positivo confirmado.
    # Agora a checagem não depende de singular/plural exato.
    if "escanteio" in nome and "total do jogo" in nome:
        return "escanteio_total"
    # NOVO: "cart" (não "cartão"/"cartões" por extenso) porque "cartão"
    # (singular) e "cartões" (plural) têm radicais diferentes em português
    # - uma checagem por texto exato de um dos dois falharia pro outro,
    # exatamente como aconteceu com escanteio/escanteios.
    if "cart" in nome and "total do jogo" in nome:
        return "cartao_total"
    # NOVO (Onda 2 - Dupla Chance/Ambas Marcam por tempo, Marca em Ambos os
    # Tempos): nomes fixos, sem ambiguidade (ver MARKET_TYPES_POR_TEMPO/
    # MARKET_TYPES_MARCA_AMBOS_TEMPOS em atualizar_odds.py). Checados ANTES
    # do " marca" genérico (equipe_marca) porque "Ambas Marcam..." e
    # "...Marca em Ambos os Tempos" também contêm " marca" como substring -
    # mesmo cuidado de ordem já usado em escanteio/cartão total.
    if "dupla chance primeiro tempo" in nome:
        return "dupla_chance_1t"
    if "dupla chance segundo tempo" in nome:
        return "dupla_chance_2t"
    if "ambas marcam primeiro tempo" in nome:
        return "ambas_marcam_1t"
    if "ambas marcam segundo tempo" in nome:
        return "ambas_marcam_2t"
    if "marca em ambos os tempos" in nome:
        return "marca_ambos_tempos"
    # NOVO (Handicap Asiático - só meia linha): nome único, sem risco de
    # colidir com "Hándicap" (marketType diferente, fora de escopo).
    if "handicap asiático" in nome:
        return "handicap_asiatico"
    # NOVO (Mais/Menos gols e Equipe Marca): nomes fixos, sem ambiguidade
    # (ver MARKET_TYPES_GOLS_E_MARCA em atualizar_odds.py) - "gols total
    # do jogo" precisa vir ANTES de "gols do time", mesma lógica do
    # escanteio/cartão total checados antes do tipo por time.
    if "gols total do jogo" in nome:
        return "gols_total"
    if "gols do time" in nome:
        return "gols_time"
    # "Equipe 1 Marca"/"Equipe 2 Marca" já vem com o "Equipe X" substituído
    # pelo nome real do time (ver montar_descricao_mercado) - por isso a
    # checagem é só " marca" (com espaço antes, pra não bater em nada que
    # termine com essas letras por coincidência).
    if " marca" in nome:
        return "equipe_marca"
    # CORRIGIDO: era "cartão"/"cartao" (singular) - "cartões" (plural) tem
    # troca irregular (não é só "+s"), então nunca batia com mercados que
    # vêm no plural ("Cartões - Mais/Menos Equipe 1/2", "Cartões -
    # Handicap", "Cartões - Ímpar/Par"...). Trocado por "cart" (substring
    # que cobre singular e plural), mesma lógica já usada 2 linhas acima
    # pro "cartao_total". Confirmado no catálogo real: "cart" não aparece
    # em nenhum nome de mercado que não seja sobre cartão.
    if "cart" in nome or "card" in nome:
        return "cartao"
    if "falta" in nome:
        return "falta_cometida"
    if "desarme" in nome or "tackle" in nome:
        return "desarme"
    # NOVO: "chute no gol" precisa ser checado ANTES do genérico "chute" -
    # senão "Chutes do Jogador" (chute total, sem "no gol" no nome) cairia
    # por engano no tipo errado, já que "chute" sozinho bate nos dois.
    # CORRIGIDO: o mercado principal (tempo completo) da OddsPapi vem
    # escrito "Mais/Menos chutes A gol do jogador" - com "a", não "no". Só
    # as variações por período (1º/2º/3º) usam "no Gol" corretamente. Sem
    # essa checagem extra, "chute no gol" do jogo inteiro caía no tipo
    # genérico "chute_total", misturado com chute total de verdade.
    # Confirmado que "a gol" não aparece em nenhum outro nome de mercado de
    # chute (só nesses dois casos).
    if ("chute" in nome and ("no gol" in nome or "a gol" in nome)) or "shotsongoal" in nome or "shots on goal" in nome:
        return "chute_no_gol"
    if "chute" in nome or "shot" in nome:
        return "chute_total"
    if "impediment" in nome:
        return "impedimento"
    if "escanteio" in nome or "corner" in nome:
        return "escanteio_time"
    if "resultado" in nome and "tempo completo" in nome:
        return "resultado_final"
    return None


def buscar_odds_futuras(cur):
    """Busca odds de jogos que ainda não aconteceram. Traz também o árbitro
    do jogo (j.arbitro), usado no ajuste de cartão/falta.

    NOVO (confronto direto): também traz mandante_id/visitante_id, usados
    pra identificar o time adversário por ID (não por texto - evita o
    problema de nomes grafados diferente entre fontes) e cruzar com
    padroes_confronto_direto.

    NOVO (multi-time): também traz j.nosso_time_id - cada odd agora sabe de
    qual time rastreado ela é, em vez de assumir sempre Corinthians.

    NOVO (Grupo A - Zona/Rodada): também traz rodada_numero e a temporada
    (derivada do ano de data_jogo) - usados pra descobrir em que zona da
    tabela e em que ponto do padrão por rodada esse jogo específico cai."""
    cur.execute(
        """
        SELECT o.id, o.jogo_id, o.jogador_id, o.casa_aposta, o.mercado,
               o.valor_odd, o.linha, o.direcao, j.data_jogo, j.adversario,
               j.mandante, j.arbitro, j.mandante_id, j.visitante_id, j.nosso_time_id,
               j.rodada_numero, EXTRACT(YEAR FROM j.data_jogo)::int
        FROM odds o
        JOIN jogos j ON j.id = o.jogo_id
        WHERE (j.datahora_jogo IS NOT NULL AND j.datahora_jogo >= NOW())
           OR (j.datahora_jogo IS NULL AND j.data_jogo >= CURRENT_DATE)
        """
    )
    return cur.fetchall()


# NOVO (21/08/2026 - suavização pra time de histórico curto): um time com
# poucos jogos no banco produz frequências extremas por puro acaso de
# amostra pequena. O caso real que motivou isso: Chapecoense e Remo, que
# subiram em 2026 e tinham ~22 jogos cada quando os 20 times foram
# ativados. Como os padrões são separados por LADO (mandante/visitante),
# 22 jogos viram ~11 por padrão - e um mercado que aconteceu em 11 de 11
# virava frequência 100%, tratada como CERTEZA.
#
# O estrago aparecia nas múltiplas: perna de 100% multiplica por 1.0, ou
# seja, não reduz nada. Uma múltipla de 3 pernas com duas delas em 100%
# virava, na prática, uma aposta de 1 perna com odd de 3 - probabilidade
# combinada de 94% que era pura ilusão. Direção perigosa (superestima).
#
# Piso de amostra por contagem de jogo NÃO resolve esses dois times: o
# Brasileirão tem 38 rodadas, então eles terminam a temporada com 38
# jogos (~19 por lado) e nunca chegariam num piso alto o bastante pra
# tornar o 100% raro. Qualquer piso alcançável ou é inútil ou os exclui
# do campeonato inteiro.
#
# Solução: suavização de Laplace, ESCOPADA só nos times de histórico
# curto. Em vez de acertos/total, usa (acertos + 1) / (total + 2). Com
# 11 jogos: 11/11 vira 92.3% (não 100%), 10/11 vira 84.6% (não 90.91%).
# O extremo é puxado pro centro na proporção do tamanho da amostra.
#
# Por que escopado e não geral: aplicar em todos os 20 times mudaria a
# escala de TODA probabilidade do sistema, criando duas escalas
# convivendo no histórico (antigas sem suavizar, novas suavizadas) e
# inutilizando a tabela de calibração da seção 9 por meses. Escopado nos
# times de histórico curto, os 18 times estabelecidos ficam intocados e
# a calibração continua válida pro grosso do volume.
JOGOS_MINIMOS_HISTORICO_COMPLETO = 60

# NOVO (25/08/2026 - separação de escanteio por mando)
#
# Piso PRÓPRIO do recorte por lado de `padroes_time_escanteio`, mais alto
# que JOGOS_MINIMOS_PARA_ANALISAR (=5, em motor_padroes.py). Amostra
# dividida em subgrupos merece piso próprio - mesmo princípio que criou
# JOGOS_MINIMOS_FATOR_ZONA_MOMENTO = 10.
#
# Onde 15 cai hoje:
#   18 times estabelecidos -> 50 por lado (janela cheia) -> usam o recorte
#   Chapecoense (22 jogos)  -> ~11 por lado -> cai no 'geral'
#   Remo (23 jogos)         -> ~11 por lado -> cai no 'geral'
#
# E é ALCANÇÁVEL: terminando o Brasileirão com 38 jogos eles chegam a 19
# por lado e passam a usar o recorte. Isso é o oposto do piso de 40 jogos
# que foi rejeitado na sessão da suavização - aquele era exclusão
# permanente disfarçada de temporária, porque o campeonato só tem 38
# rodadas.
JOGOS_MINIMOS_ESCANTEIO_POR_LADO = 15

# Abaixo desta amostra, a frequência lida é suavizada por Laplace mesmo
# que o TIME tenha histórico completo. Ver _deve_suavizar_pela_amostra.
JOGOS_MINIMOS_AMOSTRA_CONFIAVEL = 30

# NOVO (28/08/2026 - confronto direto como PRIOR, não como substituto)
# ---------------------------------------------------------------------
# Peso do padrão geral do time, em "jogos equivalentes", quando o
# confronto direto é combinado com ele em vez de simplesmente substituí-lo.
#
# O DEFEITO QUE ISSO CORRIGE: a escada do confronto direto era
# `confronto -> geral -> média do time`, ou seja SUBSTITUIÇÃO. Um confronto
# de 7 jogos derrubava por completo os 138 jogos de histórico do time - e os
# 7 já estavam DENTRO dos 138. Não era informação nova sendo somada, era um
# recorte pequeno vetando o conjunto inteiro.
#
# Caso medido (Cruzeiro x Vasco, 27/08/2026, "Mais de 8.5 escanteios"):
#   - confronto direto: 7 de 7 jogos acima da linha -> 88,9% suavizado
#     -> 96,9% depois da correlação. O sistema exibia 97%.
#   - a base inteira: Cruzeiro 71,0% (138 jogos), Vasco 61,3% (137 jogos)
#   Ou seja: a resposta honesta era ~63-70%, não 97%. Sete jogos seguidos
#   acima da linha, com taxa real de ~63%, acontecem por acaso em ~4% dos
#   pares - e com 190 pares de times x 6 linhas, sequências assim aparecem
#   às dezenas (medido: 80+ linhas com frequência 100% e n=5 na base).
#
# COMO FUNCIONA: em vez de Laplace puxar a frequência pra 50% (um prior
# arbitrário que quase nunca é a resposta certa), o confronto é encolhido
# em direção ao PADRÃO GERAL DO PRÓPRIO TIME naquela linha:
#
#     P = (acertos_confronto + k * freq_geral) / (n_confronto + k)
#
# Quando n_confronto == k, os dois pesam igual; abaixo disso o geral
# domina, acima o confronto assume - de forma contínua, sem degrau. Isso
# generaliza a suavização de Laplace, que é exatamente este formato com
# k=2 e freq_geral=50%.
#
# POR QUE 10: dois times se enfrentam ~2x por temporada, então k=10
# significa que o confronto só empata com o geral depois de ~5 temporadas
# de confrontos. Conservador de propósito - na rodada de 22-24/08 o
# confronto direto em escanteio_total deu gap de -19,9 (40,5% observado
# contra 60,4% previsto, 37 recomendações), contra +1,5 do grupo SEM
# confronto direto (78 recomendações). O mecanismo nunca demonstrou
# vantagem neste mercado; até que demonstre, entra com peso pequeno.
#
# ESCOLHIDO COMO CONSTANTE de propósito: calibrar k depois é trocar um
# número, não reescrever lógica.
PESO_PRIOR_CONFRONTO_DIRETO = 10


# NOVO (01/09/2026 - mercado de EMPATE desativado)
# ---------------------------------------------------------------------
# Quando True, `resultado_final` com resultado "empate" NÃO gera
# recomendação. Vitória e derrota seguem normalmente.
#
# POR QUE: 0 acertos em 22 recomendações de empate, medido no histórico
# INTEIRO (não só numa rodada), com probabilidade média prevista de 34,7%.
# Se a taxa real de empate é ~27,3% (203 de 743 jogos, 24 meses), a chance
# de 22 recomendações errarem TODAS por azar é menor que 0,1%.
#
# O QUE JÁ FOI DESCARTADO como causa (investigado em 01/09/2026, cada um
# com consulta própria no banco - "descartar hipótese vale tanto quanto
# confirmar"):
#
#   1. Erro de AVALIAÇÃO -> descartado. Os 22 jogos foram conferidos um a
#      um contra o placar real de `jogos_liga`: nenhum empatou de verdade.
#      `avaliacao.py` está certo.
#
#   2. FORMA RECENTE aplicada ao empate (o bug corrigido em 28/08) ->
#      descartado como causa principal. Separando as 22 linhas em antes e
#      depois da correção: 0 de 13 acertaram ANTES, 0 de 9 DEPOIS. A
#      correção estava certa em ser feita, mas não move este número.
#
#   3. CONFRONTO DIRETO inflando a estimativa -> descartado. Comparando
#      COM e SEM confronto direto dentro do próprio mercado de empate:
#      0 de 15 COM (previsto 35,0%), 0 de 7 SEM (previsto 34,1%). Se o
#      confronto fosse a causa, o grupo SEM estaria melhor. Não está.
#
# O QUE SOBRA (hipótese estrutural, sem conserto desenhado): o filtro de
# VE só deixa passar uma recomendação de empate quando o modelo estima
# ACIMA do que a odd da casa implica. Empate é um resultado quase
# simétrico, difícil de ter edge real, e a casa precifica bem - então
# "modelo discorda da casa pra cima no empate" é, na prática, ruído do
# modelo, não sinal. É a pendência 28 (o filtro de VE seleciona os
# maiores erros do próprio modelo) na sua forma mais extrema.
#
# POR QUE DESATIVAR em vez de continuar ajustando: três hipóteses de
# conserto já foram testadas e descartadas com dado real. Manter no ar um
# mercado com 0% de acerto medido, enquanto a causa-raiz não tem desenho,
# só contamina a banca e a tabela de calibração. Desativar é reversível
# numa linha; o prejuízo acumulado, não.
#
# COMO REVERTER: trocar para False. As recomendações de empate voltam a
# ser geradas exatamente como antes - nenhuma outra lógica foi removida,
# só o ponto de geração é pulado.
#
# ESCOPO DELIBERADO: só o empate. Vitória (26,7% observado) e derrota
# (26,3%) continuam ativas e servem de GRUPO DE CONTROLE - se o problema
# for do mercado de resultado como um todo, elas vão mostrar isso ao
# longo das próximas rodadas sem que nada precise ser desfeito aqui.
GERAR_RECOMENDACAO_DE_EMPATE = False


def carregar_total_jogos_por_time(cur):
    """NOVO (suavização): conta quantos jogos JÁ CONCLUÍDOS cada time tem,
    pra decidir quais entram na suavização. Usa o MESMO critério de "jogo
    concluído" que motor_padroes.py usa pra montar os padrões - se os dois
    divergissem, um time poderia ser suavizado com base num número que o
    cálculo do padrão nem enxergou.

    Carregado UMA vez por execução e passado adiante como dict, em vez de
    consultar por odd - são ~20 linhas, não vale uma query por iteração."""
    cur.execute(
        """
        SELECT nosso_time_id, COUNT(*)
        FROM jogos
        WHERE nosso_time_id IS NOT NULL
          AND ((datahora_jogo IS NOT NULL AND datahora_jogo < NOW())
            OR (datahora_jogo IS NULL AND data_jogo < CURRENT_DATE))
        GROUP BY nosso_time_id
        """
    )
    return {time_id: total for time_id, total in cur.fetchall()}


def time_tem_historico_curto(total_jogos_por_time, time_id):
    """NOVO (suavização): True quando esse time ainda não tem histórico
    suficiente pra que uma frequência extrema signifique alguma coisa.
    Time desconhecido (sem linha em `jogos`) conta como histórico curto -
    é o lado conservador: suavizar de menos deixa passar probabilidade
    falsa, suavizar de mais só torna a estimativa mais modesta."""
    if time_id is None:
        return True
    return total_jogos_por_time.get(time_id, 0) < JOGOS_MINIMOS_HISTORICO_COMPLETO


def _frequencia_do_row(row, suavizar):
    """NOVO (suavização): converte a linha lida de uma tabela de padrão na
    frequência final, aplicando Laplace quando `suavizar` está ligado.

    Espera row = (frequencia, jogos_analisados). Aceita row com só a
    frequência (tabelas que não passaram a trazer a amostra) - nesse caso
    devolve o valor cru, sem suavizar, porque sem saber o tamanho da
    amostra não dá pra suavizar honestamente.

    Trabalha direto sobre a frequência em vez de recuperar a contagem de
    acertos: como frequencia = acertos/total*100, a conta
    (freq/100*n + 1)/(n + 2) é matematicamente idêntica a
    (acertos + 1)/(total + 2), sem depender de arredondamento pra
    reconstruir o numerador inteiro."""
    if not row or row[0] is None:
        return None

    frequencia = float(row[0])
    if not suavizar:
        return frequencia

    jogos = row[1] if len(row) > 1 else None
    if not jogos:
        return frequencia

    jogos = int(jogos)
    acertos = frequencia / 100 * jogos
    return round(100 * (acertos + 1) / (jogos + 2), 2)


def _encolher_para_prior(frequencia_confronto, jogos_confronto, frequencia_geral):
    """NOVO (28/08/2026): combina a frequência do confronto direto com a
    frequência geral do time, ponderando pelo tamanho da amostra do
    confronto - ver PESO_PRIOR_CONFRONTO_DIRETO pro diagnóstico completo.

        P = (acertos_confronto + k * freq_geral) / (n_confronto + k)

    onde acertos_confronto = freq_confronto/100 * n_confronto.

    É a mesma forma da suavização de Laplace `(a+1)/(n+2)`, só que
    encolhendo em direção ao padrão real do time em vez de 50%. Vantagens
    sobre subir o piso de amostra (a alternativa considerada e descartada):

      - nenhum confronto é DESCARTADO, só entra com o peso que merece.
        Isso importa pros times que sobem e descem entre Série A e B
        (Coritiba, Vitória, Santos, Mirassol...), que têm histórico
        picotado: um piso de 8 jogos zerava o confronto direto deles por
        completo (medido), enquanto aqui eles continuam contando.
      - a transição é contínua: um confronto de 12 jogos pesa mais que um
        de 7, que pesa mais que um de 5, sem degrau arbitrário.

    Com jogos_confronto = 0 não há o que combinar - devolve o prior."""
    if not jogos_confronto:
        return round(frequencia_geral, 2)

    acertos = frequencia_confronto / 100 * jogos_confronto
    combinado = (
        (acertos + PESO_PRIOR_CONFRONTO_DIRETO * frequencia_geral / 100)
        / (jogos_confronto + PESO_PRIOR_CONFRONTO_DIRETO)
    )
    return round(100 * combinado, 2)


def _deve_suavizar_pela_amostra(row, suavizar_por_time):
    """NOVO (25/08/2026): decide a suavização pela amostra da PRÓPRIA LINHA
    lida, e não só pelo total de jogos do time.

    POR QUE existe: `suavizar` é calculado no loop principal a partir do
    total de jogos do time (JOGOS_MINIMOS_HISTORICO_COMPLETO = 60). Esse
    número descreve bem uma tabela de padrão geral, mas deixa de descrever
    a amostra assim que o padrão passa a ser recortado - foi exatamente o
    buraco encontrado em 22/08 no confronto direto, onde o Corinthians
    contava como "histórico completo" por ter 173 jogos enquanto o
    confronto específico tinha 5.

    Com o escanteio separado por mando o mesmo risco reaparece: um time
    que faz 5.8 escanteios de média em casa tem ~80% de chance de passar
    de 3.5, e 50 jogos de recorte podem dar 50/50 = 100%. Frequência de
    100% multiplica por 1.0 na múltipla e some da conta - o buraco que a
    suavização de Laplace foi criada pra fechar.

    Regra, em três degraus:

      1. o time já era de histórico curto -> suaviza
         (comportamento antigo, preservado sem mudança)

      2. a linha lida tem amostra pequena
         (< JOGOS_MINIMOS_AMOSTRA_CONFIAVEL) -> suaviza

      3. a frequência é 0% ou 100% -> suaviza SEMPRE, seja qual for a
         amostra

    O degrau 3 não é detalhe - foi pego em bancada e é o motivo de a regra
    não ser só "amostra < 30". Um recorte cheio tem 50 jogos, acima de
    qualquer limiar razoável, e mesmo assim 50/50 = 100% é possível numa
    linha baixa (um mandante que faz 5.8 escanteios de média passa de 3.5
    em ~80% dos jogos; 50 acertos seguidos é raro mas acontece em ~200
    linhas de recorte). Só aumentar o limiar acima de 50 resolveria isso,
    mas ao custo de suavizar TODA leitura e deslocar a escala do mercado
    inteiro - o que a sessão da suavização decidiu explicitamente evitar.

    Tratar 0% e 100% como caso próprio conserta o extremo sem mexer no
    resto da distribuição. E é coerente com o princípio já registrado:
    probabilidade de 100% não é aposta ótima, é sinal de amostra
    insuficiente - nenhum evento esportivo tem certeza absoluta.

    Efeito num recorte de 50 jogos com 40 acertos: 80% vira 78.85%.
    Num recorte de 50/50: 100% vira 98.08%.
    Conservador de propósito - puxa pro centro, nunca pro extremo."""
    if suavizar_por_time:
        return True
    if not row or row[0] is None:
        return False

    frequencia = float(row[0])
    if frequencia >= 100.0 or frequencia <= 0.0:
        return True

    if len(row) < 2 or not row[1]:
        return False
    return int(row[1]) < JOGOS_MINIMOS_AMOSTRA_CONFIAVEL


def buscar_frequencia_cartao(cur, jogador_id, suavizar=False):
    cur.execute(
        "SELECT frequencia, jogos_analisados FROM padroes_jogador_cartao WHERE jogador_id = %s",
        (jogador_id,),
    )
    return _frequencia_do_row(cur.fetchone(), suavizar)


def buscar_frequencia_linha_jogador(cur, jogador_id, tipo, linha, suavizar=False):
    cur.execute(
        "SELECT frequencia, jogos_analisados FROM padroes_jogador_linha WHERE jogador_id = %s AND tipo = %s AND linha = %s",
        (jogador_id, tipo, linha),
    )
    return _frequencia_do_row(cur.fetchone(), suavizar)


def buscar_frequencia_simples_jogador(cur, jogador_id, tipo, suavizar=False):
    cur.execute(
        "SELECT frequencia, jogos_analisados FROM padroes_jogador_frequencia WHERE jogador_id = %s AND tipo = %s",
        (jogador_id, tipo),
    )
    return _frequencia_do_row(cur.fetchone(), suavizar)


def buscar_frequencia_escanteio_time(cur, linha, time_id, suavizar=False, mandante=None):
    """NOVO (25/08/2026 - separação por mando): escada de dois degraus,
    no mesmo formato já usado por `buscar_frequencia_confronto`:

        recorte do mando do jogo  ->  'geral'  ->  None

    POR QUE, em números medidos na base inteira (1652 jogos, 5 temporadas):
    mandante faz 5.82 escanteios por jogo, visitante 4.54 - vantagem de
    1.29, estável ano a ano (1.34 / 1.32 / 1.20 / 1.28 / 1.28). A tabela
    guardava UM número por (time, linha), misturando os dois, enquanto a
    casa precifica separado. Isso vale ~7 pontos de probabilidade em toda
    linha e sempre no mesmo sentido - acima da margem da casa (~5%).

    Resultado da auditoria de 22-24/08/2026: 69 das 74 recomendações desse
    mercado saíram do lado errado, com 24.3% de acerto contra 54.3%
    previstos.

    O piso do recorte por lado (JOGOS_MINIMOS_ESCANTEIO_POR_LADO) é maior
    que o piso geral de propósito: amostra dividida em subgrupos merece
    piso próprio - mesmo princípio que criou
    JOGOS_MINIMOS_FATOR_ZONA_MOMENTO. E é um piso ALCANÇÁVEL: um time que
    termine o Brasileirão com 38 jogos chega a 19 por lado e passa a usar
    o recorte específico.

    `mandante=None` (chamador que ainda não passa o mando) cai direto no
    'geral', preservando o comportamento antigo em vez de adivinhar."""
    if mandante is not None:
        lado = "mandante" if mandante else "visitante"
        cur.execute(
            """SELECT frequencia, jogos_analisados FROM padroes_time_escanteio
               WHERE linha = %s AND time_id = %s AND lado = %s""",
            (linha, time_id, lado),
        )
        row = cur.fetchone()
        if row and row[0] is not None and row[1] and int(row[1]) >= JOGOS_MINIMOS_ESCANTEIO_POR_LADO:
            return _frequencia_do_row(row, _deve_suavizar_pela_amostra(row, suavizar))

    cur.execute(
        """SELECT frequencia, jogos_analisados FROM padroes_time_escanteio
           WHERE linha = %s AND time_id = %s AND lado = 'geral'""",
        (linha, time_id),
    )
    row = cur.fetchone()
    return _frequencia_do_row(row, _deve_suavizar_pela_amostra(row, suavizar))


def buscar_frequencia_escanteio_total(cur, linha, time_id, suavizar=False):
    """NOVO: frequência de escanteios do jogo INTEIRO (mandante + visitante
    somados) passar de uma linha - diferente de buscar_frequencia_escanteio_time,
    que olha só o lado do Corinthians. Filtra por time_id, já que essa
    tabela pode ter frequências diferentes calculadas pra times diferentes
    quando outros clubes forem adicionados."""
    cur.execute(
        "SELECT frequencia, jogos_analisados FROM padroes_escanteio_total WHERE linha = %s AND time_id = %s",
        (linha, time_id),
    )
    return _frequencia_do_row(cur.fetchone(), suavizar)


def buscar_frequencia_cartao_total(cur, linha, time_id, suavizar=False):
    """NOVO: frequência de cartões do jogo INTEIRO (mandante + visitante
    somados) passar de uma linha. Filtra por time_id (ver docstring de
    buscar_frequencia_escanteio_total)."""
    cur.execute(
        "SELECT frequencia, jogos_analisados FROM padroes_cartao_total WHERE linha = %s AND time_id = %s",
        (linha, time_id),
    )
    return _frequencia_do_row(cur.fetchone(), suavizar)


def buscar_frequencia_gols_time(cur, linha, time_id, suavizar=False):
    """NOVO (Mais/Menos gols do time): lê de padroes_time_linha, tipo=
    "gols" - mesma tabela já usada por falta/chute/cartão do time (ver
    calcular_padrao_gols_time em motor_padroes.py)."""
    cur.execute(
        "SELECT frequencia, jogos_analisados FROM padroes_time_linha WHERE tipo = %s AND linha = %s AND time_id = %s",
        ("gols", linha, time_id),
    )
    return _frequencia_do_row(cur.fetchone(), suavizar)


def buscar_frequencia_cartao_time(cur, linha, time_id, suavizar=False):
    """NOVO (Cartão por TIME - mercado de linha Mais/Menos, destravado pela
    correção do bug de plural em atualizar_odds.py): lê de
    padroes_time_linha, tipo="cartao" - essa tabela já é populada há tempo
    por calcular_padrao_cartao_time (motor_padroes.py), só nunca tinha sido
    lida aqui pra gerar recomendação de verdade."""
    cur.execute(
        "SELECT frequencia, jogos_analisados FROM padroes_time_linha WHERE tipo = %s AND linha = %s AND time_id = %s",
        ("cartao", linha, time_id),
    )
    return _frequencia_do_row(cur.fetchone(), suavizar)


def buscar_frequencia_gols_total(cur, linha, time_id, suavizar=False):
    """NOVO: frequência de gols do jogo INTEIRO (mandante + visitante
    somados) passar de uma linha - mesmo padrão de
    buscar_frequencia_escanteio_total/buscar_frequencia_cartao_total."""
    cur.execute(
        "SELECT frequencia, jogos_analisados FROM padroes_gols_total WHERE linha = %s AND time_id = %s",
        (linha, time_id),
    )
    return _frequencia_do_row(cur.fetchone(), suavizar)


def buscar_frequencia_equipe_marca(cur, time_id, suavizar=False):
    """NOVO (Equipe Marca - Sim/Não): frequência binária, um valor só por
    time (sem linha/handicap)."""
    cur.execute(
        "SELECT frequencia, jogos_analisados FROM padroes_time_marca WHERE time_id = %s",
        (time_id,),
    )
    return _frequencia_do_row(cur.fetchone(), suavizar)


def buscar_frequencia_dupla_chance_tempo(cur, periodo, resultado, mandante_id,
                                         visitante_id, suavizar=False):
    """Dupla Chance por tempo ('1X'/'12'/'2X' no 1T ou no 2T).

    REESCRITA EM 26/08/2026 - passou a combinar OS DOIS LADOS.
    ---------------------------------------------------------
    Antes esta função lia UMA linha só, a do `nosso_time` daquela
    perspectiva:

        buscar_frequencia_dupla_chance_tempo(cur, nosso_time_id, "1T",
                                             mandante_filtro_atual, "1X")

    Isso tinha dois defeitos, os dois medidos na auditoria da rodada de
    22-24/08/2026:

    1) USAVA METADE DA INFORMAÇÃO.
       "1X no primeiro tempo" é um evento do JOGO: o mandante não perde o
       1T. Quem informa sobre isso são os DOIS times - o histórico do
       mandante jogando em casa E o do visitante jogando fora. A versão
       antiga lia só o lado do `nosso_time` da linha. Quando o nosso time
       era o visitante, a linha `visitante/1X` guarda a taxa em que os
       ADVERSÁRIOS GENÉRICOS que ele enfrentou fora não perderam o 1T -
       não diz nada sobre o adversário desta partida. A outra metade da
       informação já estava na tabela e simplesmente não era consultada.

    2) O MESMO EVENTO GANHAVA DUAS PROBABILIDADES.
       Um confronto entre dois times rastreados gera 2 linhas em `jogos`,
       e cada uma produzia sua própria estimativa do MESMO evento. Na
       rodada auditada, 7 dos 25 eventos apareceram duas vezes, com até
       6 pontos de diferença (Bragantino x Grêmio, 2T-2X: 72,0% e 66,0%).
       Como o filtro de VE só aceita a aposta quando a probabilidade é
       alta o bastante, ele sempre escolhia a MAIOR das duas - seleção
       adversa embutida no mercado.

    Resultado medido do mercado antes desta correção: 13 acertos em 25
    eventos (52,0%) contra 73,3% previstos. Gap de -21,3 pontos, com
    p = 0,018 de acontecer por acaso.

    COMO COMBINA:
        P(resultado) = média das duas leituras, PONDERADA por
                       `jogos_analisados` de cada uma.

    A ponderação é o que faz o lado com histórico maior pesar mais, e
    também o que torna desnecessário um piso rígido: uma linha de 6 jogos
    entra com peso 6 contra 25 da outra, em vez de ser descartada ou de
    valer o mesmo. O piso mínimo já existe uma camada acima -
    `motor_padroes.calcular_padroes_dupla_chance_tempo` só grava a linha
    se houver JOGOS_MINIMOS_PARA_ANALISAR jogos.

    Cada lado é suavizado ANTES de entrar na média, pela própria amostra
    (ver `_deve_suavizar_pela_amostra`). Isso importa aqui mais que em
    outros mercados: o filtro `placar_intervalo IS NOT NULL` corta muito
    a janela, porque o placar de intervalo só passou a ser coletado
    recentemente. Na prática este mercado roda com 12 a 25 jogos por
    lado, não com os 50 da janela - foi assim que apareceu o
    "Fluminense 1T-1X = 92,9%", que é 12 de 12 jogos em casa já suavizado.

    Como as duas perspectivas do mesmo jogo passam a ler exatamente as
    mesmas duas linhas, elas agora produzem o MESMO número, e a duplicata
    desaparece da lista de recomendações.

    ESCADA: se um dos lados não existir, usa o outro sozinho. Se nenhum
    existir, devolve None e quem chamou não gera recomendação.
    """
    if mandante_id is None or visitante_id is None:
        return None

    leituras = []
    for time_id, lado in ((mandante_id, "mandante"), (visitante_id, "visitante")):
        cur.execute(
            """
            SELECT frequencia, jogos_analisados FROM padroes_dupla_chance_tempo
            WHERE time_id = %s AND periodo = %s AND lado = %s AND resultado = %s
            """,
            (time_id, periodo, lado, resultado),
        )
        row = cur.fetchone()
        if not row or row[0] is None or not row[1]:
            continue
        freq = _frequencia_do_row(row, _deve_suavizar_pela_amostra(row, suavizar))
        if freq is None:
            continue
        leituras.append((freq, int(row[1])))

    if not leituras:
        return None
    peso_total = sum(peso for _f, peso in leituras)
    if peso_total <= 0:
        return None
    return round(sum(f*peso for f, peso in leituras)/peso_total, 2)


def buscar_frequencia_ambas_marcam_tempo(cur, time_id, periodo, lado, suavizar=False):
    """NOVO (Onda 2 - Ambas Marcam por tempo): frequência binária, separada
    por período (1T/2T) e por lado (mandante/visitante)."""
    cur.execute(
        "SELECT frequencia, jogos_analisados FROM padroes_ambas_marcam_tempo WHERE time_id = %s AND periodo = %s AND lado = %s",
        (time_id, periodo, lado),
    )
    return _frequencia_do_row(cur.fetchone(), suavizar)


def buscar_frequencia_marca_periodo(cur, time_id, periodo, lado, suavizar=False):
    """NOVO (02/09/2026): componente por time do Ambas Marcam - "este time
    marca pelo menos um gol neste período?", separado por mandante/visitante.

    Ver `buscar_frequencia_ambas_marcam_tempo_decomposta` pro porquê da
    decomposição."""
    cur.execute(
        "SELECT frequencia, jogos_analisados FROM padroes_time_marca_periodo "
        "WHERE time_id = %s AND periodo = %s AND lado = %s",
        (time_id, periodo, lado),
    )
    return _frequencia_do_row(cur.fetchone(), suavizar)


def buscar_frequencia_ambas_marcam_tempo_decomposta(cur, mandante_id, visitante_id, periodo, suavizar=False):
    """NOVO (02/09/2026): estimativa de "Ambas Marcam no período" montada
    a partir dos COMPONENTES dos dois times reais da partida, em vez da
    frequência conjunta histórica.

        P(ambas marcam) = P(mandante marca) x P(visitante marca)

    POR QUE (medido em 1662 jogos, 5 temporadas - ver
    migrar_padroes_time_marca_periodo.py): o fator de correlação entre os
    dois eventos fica entre 0.91 e 1.08 (média 0.99). Eles são
    praticamente independentes, então o produto puro basta - nenhum fator
    de correção pra manter.

    O QUE ISSO CONSERTA: a frequência conjunta de
    `padroes_ambas_marcam_tempo` é uma média sobre todos os adversários
    que o time já enfrentou - ignora QUEM é o adversário do próximo jogo.
    E como ela depende de qual time é o `nosso_time` da linha, o mesmo
    evento saía com dois números (divergência média medida: 7,14 pontos no
    1T, 8,78 no 2T, máximas de 27,6 e 32,0), e o filtro de VE ficava
    sempre com o mais otimista.

    O sintoma mais claro, no log de 02/09/2026 (Mirassol x Flamengo): o
    app recomendou "Ambas Marcam 1T - Sim" (23,33%) por uma perspectiva e
    "Ambas Marcam 1T - Não" (88,00%) pela outra, no mesmo jogo. Se Sim
    vale 23,33%, Não vale 76,67% - não 88%.

    Aqui o cálculo NÃO depende da perspectiva: recebe mandante e
    visitante do jogo e devolve o mesmo número seja de que lado se olhe.
    O defeito some por construção.

    Devolve None se faltar qualquer um dos dois componentes - nesse caso
    quem chama cai no método antigo (escada de fallback)."""
    freq_mandante = buscar_frequencia_marca_periodo(cur, mandante_id, periodo, "mandante", suavizar=suavizar)
    freq_visitante = buscar_frequencia_marca_periodo(cur, visitante_id, periodo, "visitante", suavizar=suavizar)

    if freq_mandante is None or freq_visitante is None:
        return None

    # As duas vêm em porcentagem (0-100); o produto precisa ser feito em
    # probabilidade (0-1) e devolvido em porcentagem de novo.
    return round((freq_mandante / 100.0) * (freq_visitante / 100.0) * 100.0, 2)


def buscar_frequencia_marca_ambos_tempos_decomposta(cur, time_id, lado, suavizar=False):
    """NOVO (02/09/2026 - Fase 2 da decomposição): estimativa de "este time
    marca nos DOIS tempos" montada a partir dos componentes por período,
    em vez da frequência conjunta.

        P(marca nos dois) = P(marca 1T | lado) x P(marca 2T | lado)

    POR QUE (medido em 1662 jogos, 5 temporadas, 02/09/2026):

        ano   jogos  p_marca_1t  p_marca_2t  p_ambos_real  fator
        2022    324      0.4660      0.5586        0.2654  1.0195
        2023    368      0.4891      0.5000        0.2609  1.0667
        2024    359      0.4540      0.5766        0.2535  0.9682
        2025    368      0.4348      0.5299        0.2582  1.1205
        2026    243      0.4403      0.4897        0.2058  0.9542

    O fator (real / produto) fica em 1.03 de média, variando 0.95-1.12.
    A hipótese inicial era que houvesse correlação POSITIVA clara (time
    que marca no 1T pega o jogo aberto e marca de novo) - a medição
    mostrou que não: os dois eventos são praticamente independentes,
    igual ao caso do Ambas Marcam. Os ~3% de desvio sobre 1.0 estão
    dentro da variação entre temporadas, então não vale introduzir um
    fator de correção pra manter e envelhecer.

    GANHO EXTRA - separação por mando, de graça:
        `padroes_marca_ambos_tempos` (o método antigo) tem PRIMARY KEY
        (time_id) e nenhuma coluna `lado` - mistura casa e fora num número
        só. Separar por mando tinha sido AVALIADO E DESCARTADO nesta mesma
        sessão por custo/benefício: a diferença medida foi de 4,5 pontos
        (34,01% mandante contra 29,51% visitante, ~15% relativo), abaixo
        do que justificaria uma migração de tabela + backfill só pra isso.

        Como `padroes_time_marca_periodo` já nasceu com `lado`, a
        decomposição entrega essa separação sem nenhum custo adicional.

    Devolve None se faltar qualquer um dos dois componentes - quem chama
    cai no método antigo (escada de fallback)."""
    freq_1t = buscar_frequencia_marca_periodo(cur, time_id, "1T", lado, suavizar=suavizar)
    freq_2t = buscar_frequencia_marca_periodo(cur, time_id, "2T", lado, suavizar=suavizar)

    if freq_1t is None or freq_2t is None:
        return None

    return round((freq_1t / 100.0) * (freq_2t / 100.0) * 100.0, 2)


def buscar_frequencia_marca_ambos_tempos(cur, time_id, suavizar=False):
    """NOVO (Onda 2 - Marca em Ambos os Tempos): frequência binária, um
    valor só por time (sem separação por lado - jogo inteiro, não faz
    tanta diferença mandante/visitante quanto os mercados por tempo
    isolado)."""
    cur.execute(
        "SELECT frequencia, jogos_analisados FROM padroes_marca_ambos_tempos WHERE time_id = %s",
        (time_id,),
    )
    return _frequencia_do_row(cur.fetchone(), suavizar)


def buscar_frequencia_handicap(cur, time_id, lado, linha, suavizar=False):
    """NOVO (Handicap Asiático - só meia linha): frequência de "cobrir"
    essa linha específica, separada por lado (mandante/visitante) - ver
    calcular_padrao_handicap em motor_padroes.py pro porquê da separação."""
    cur.execute(
        "SELECT frequencia, jogos_analisados FROM padroes_time_handicap WHERE time_id = %s AND lado = %s AND linha = %s",
        (time_id, lado, linha),
    )
    return _frequencia_do_row(cur.fetchone(), suavizar)


def buscar_frequencia_resultado(cur, lado, resultado, time_id, suavizar=False):
    cur.execute(
        "SELECT frequencia, jogos_analisados FROM padroes_time_resultado WHERE lado = %s AND resultado = %s AND time_id = %s",
        (lado, resultado, time_id),
    )
    return _frequencia_do_row(cur.fetchone(), suavizar)


# NOVO (28/08/2026): "vitória do mandante" na perspectiva do mandante é a
# MESMA coisa que "derrota do visitante" na perspectiva do visitante. Pra
# combinar as duas leituras do mesmo evento, a do outro lado precisa ser
# INVERTIDA - somar "vitória do A" com "vitória do B" seria somar dois
# eventos opostos. O empate é o único simétrico (empate pro mandante =
# empate pro visitante), e é justamente onde a inflação foi medida.
_RESULTADO_ESPELHADO = {"vitoria": "derrota", "empate": "empate", "derrota": "vitoria"}


def buscar_frequencia_resultado_combinada(cur, resultado_nosso_time, nosso_time_id,
                                          mandante_id, visitante_id, suavizar=False):
    """NOVO (28/08/2026): probabilidade de um resultado combinando as DUAS
    perspectivas do jogo, ponderada por `jogos_analisados`.

    O DEFEITO QUE ISSO CORRIGE: o resultado de uma partida é um evento do
    JOGO - os dois times informam sobre ele. O motor lia só a linha do
    `nosso_time` daquela perspectiva e jogava a outra metade fora. Como
    cada jogo entre dois times rastreados gera duas perspectivas, o mesmo
    empate saía com duas probabilidades diferentes, e o filtro de VE
    sempre escolhia a MAIOR das duas - seleção adversa embutida.

    Medido na rodada de 22-24/08/2026 (mercado de empate):
      - 5 de 7 pares divergiam, até 20,9 pontos (Chapecoense x São Paulo:
        50,77% contra 29,90% pro mesmo empate)
      - média do que o VE escolhia: 42,6% | se combinasse: 38,5%
      - taxa real de empate na base: 27,3% (203 de 743 jogos, 24 meses)
      - resultado: 0 acertos em 17 recomendações de empate (~10 jogos reais)

    Mesmo desenho já validado na Dupla Chance por tempo (seção 29 da
    documentação): média ponderada por amostra, cada lado suavizado ANTES
    de entrar na média, escada se faltar um dos lados.

    A ponderação dispensa piso rígido: uma linha de 11 jogos entra com
    peso 11 contra 50 da outra, em vez de ser descartada ou de valer o
    mesmo.

    ATENÇÃO ao mexer aqui: o `_RESULTADO_ESPELHADO` acima não é detalhe.
    Sem a inversão, "vitória do Palmeiras" seria somada com "vitória do
    Vasco" - eventos opostos, resultado sem significado nenhum. É a mesma
    armadilha do campo `lado` com dupla semântica que já causou o bug do
    cartão de time."""
    if mandante_id is None or visitante_id is None:
        return None
    if resultado_nosso_time not in _RESULTADO_ESPELHADO:
        return None

    somos_mandante = (nosso_time_id == mandante_id)
    lado_nosso = "mandante" if somos_mandante else "visitante"
    lado_adversario = "visitante" if somos_mandante else "mandante"
    adversario_id = visitante_id if somos_mandante else mandante_id
    resultado_adversario = _RESULTADO_ESPELHADO[resultado_nosso_time]

    def ler(lado, resultado, time_id):
        cur.execute(
            "SELECT frequencia, jogos_analisados FROM padroes_time_resultado "
            "WHERE lado = %s AND resultado = %s AND time_id = %s",
            (lado, resultado, time_id),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        jogos = int(row[1]) if len(row) > 1 and row[1] else 0
        if not jogos:
            return None
        frequencia = _frequencia_do_row(row, _deve_suavizar_pela_amostra(row, suavizar))
        if frequencia is None:
            return None
        return (frequencia, jogos)

    leituras = [
        leitura for leitura in (
            ler(lado_nosso, resultado_nosso_time, nosso_time_id),
            ler(lado_adversario, resultado_adversario, adversario_id),
        ) if leitura is not None
    ]
    if not leituras:
        return None

    peso_total = sum(jogos for _freq, jogos in leituras)
    if not peso_total:
        return None
    return round(sum(freq * jogos for freq, jogos in leituras) / peso_total, 2)


def buscar_id_corinthians(cur):
    """NOVO (confronto direto): busca o id do Corinthians na tabela `times`,
    usado pra identificar o adversário de cada jogo por ID (comparando com
    mandante_id/visitante_id), em vez de por texto."""
    cur.execute("SELECT id FROM times WHERE nome = %s", ("Corinthians",))
    row = cur.fetchone()
    return row[0] if row else None


def buscar_frequencia_confronto(cur, nosso_time_id, adversario_id, mandante_filtro, tipo_padrao, linha=0, resultado="", suavizar=False):
    """NOVO (confronto direto): busca a frequência específica contra esse
    adversário (ex: "cartões totais contra o Palmeiras, jogando em casa"),
    se já tiver sido calculada com uma amostra que não seja pequena demais.
    Retorna None se não houver dado suficiente - nesse caso, quem chamou
    essa função deve cair de volta pro padrão geral (não filtrado por
    adversário). Filtra por nosso_time_id (o time do qual estamos vendo o
    confronto - hoje sempre Corinthians), pra não colidir com o confronto
    do mesmo adversário visto de outro time no futuro.

    ⚠️ NOVO (28/08/2026): esta função virou um WRAPPER de
    `buscar_confronto_detalhado`. Ela mantém o comportamento antigo
    (confronto SUBSTITUI o padrão geral, suavizado por Laplace em direção
    a 50%) e é usada por cartao_total e cartao_time.
    `escanteio_total` e `resultado_final` NÃO usam mais esta função - lá o
    confronto é encolhido em direção ao padrão geral do time (ver
    PESO_PRIOR_CONFRONTO_DIRETO). Se/quando o mesmo for medido nos outros
    2, é só trocar a chamada deles pela versão detalhada.

    (Correção de 01/09/2026: este parágrafo dizia que `resultado_final`
    ainda usava o comportamento antigo, o que deixou de valer quando ele
    passou a chamar `buscar_confronto_detalhado` direto. Era só a
    documentação defasada - o código já estava certo - mas comentário
    errado é armadilha pra quem for mexer aqui depois.)

    NOVO (21/08/2026 - suavização SEMPRE ativa aqui): o parâmetro
    `suavizar` recebido é IGNORADO de propósito - confronto direto é
    sempre suavizado, independente do tamanho do histórico do time.

    Motivo: a suavização de Laplace decide pelo total de jogos do TIME
    (ver JOGOS_MINIMOS_HISTORICO_COMPLETO), mas isso é o número errado
    aqui. O Corinthians tem 173 jogos e conta como "histórico completo" -
    só que o confronto ESPECÍFICO contra o Palmeiras tem 5 ou 6, não 173.

    E essa amostra nunca vai crescer: dois times se enfrentam 2 a 3 vezes
    por temporada, então mesmo com anos de histórico o confronto
    dificilmente passa de 8-10 jogos. É pequena por natureza, não por
    imaturidade - exatamente o caso que a suavização existe pra atender.

    Sem isso, um confronto de 5 em 5 jogos viraria frequência 100% (tratada
    como certeza) num time grande, reabrindo pela porta do confronto direto
    o mesmo problema que a suavização fechou pros times de histórico curto:
    perna de 100% multiplica por 1.0 e não reduz nada na múltipla.

    NOVO (21/08/2026 - ESCADA DE FALLBACK mando -> geral)
    -----------------------------------------------------
    O confronto direto é gravado em TRÊS recortes: 'mandante', 'visitante'
    e 'geral' (os dois juntos). Até aqui, a recomendação pedia sempre o
    recorte específico do mando do jogo - e nunca o 'geral'.

    O problema: dois times se enfrentam ~2x por temporada, e dividir isso
    em dois lados faz cada recorte ficar com METADE de uma amostra que já
    era pequena. Caso real (Bahia x Vitória, 23/08/2026): 5 jogos no banco,
    mas 3 como mandante e 2 como visitante - nenhum dos dois passava no
    piso de 5, então o confronto era descartado e a odd caía na média geral
    do time contra QUALQUER adversário. O recorte 'geral', com os 5 jogos,
    estava calculado no banco e nunca era consultado.

    O efeito era grande: no Ba-Vi, o confronto dava "Mais de 3.5 cartões do
    Bahia" em 20% (1 de 5), contra ~14% da média geral do time - ou seja, o
    sistema estava jogando fora justamente a informação de que o clássico é
    mais cartelado que a média.

    Agora tenta em escada, do mais específico pro mais genérico:
        mando do jogo -> 'geral' -> None (aí quem chamou usa a média do time)

    Isso é o mesmo diagnóstico já registrado do "Padrão por Rodada": um
    agrupamento fino demais faz o fator quase nunca ativar. A diferença é
    que aqui existia um agrupamento mais largo já pronto e ocioso.
    """
    detalhe = buscar_confronto_detalhado(
        cur, nosso_time_id, adversario_id, mandante_filtro, tipo_padrao, linha, resultado
    )
    return detalhe[0] if detalhe else None


def buscar_confronto_detalhado(cur, nosso_time_id, adversario_id, mandante_filtro,
                               tipo_padrao, linha=0, resultado="", frequencia_geral=None):
    """NOVO (28/08/2026): mesma busca de buscar_frequencia_confronto, mas
    devolvendo `(frequencia, jogos_analisados)` em vez de só a frequência,
    e aceitando `frequencia_geral` como PRIOR.

    Existe separada de propósito: `buscar_frequencia_confronto` é usada por
    4 mercados (escanteio_total, cartao_total, cartao_time,
    resultado_final) e continua com a assinatura e o comportamento de
    sempre. Só quem quiser o encolhimento e o tamanho da amostra chama esta
    aqui - hoje, apenas escanteio_total (escopo deliberado: é o único
    mercado onde o defeito foi MEDIDO; mexer nos outros sem medir antes
    repetiria o erro já registrado de "adicionar filtro a mercado
    defeituoso faz o filtro levar a culpa").

    `jogos_analisados` volta junto porque sem ele não dá pra auditar
    depois com que peso cada recomendação foi calculada - a descrição
    passa a mostrar isso.

    Quando `frequencia_geral` é None, cai no comportamento antigo (Laplace
    sempre ligado): sem prior, não existe pra onde encolher."""
    if adversario_id is None:
        return None

    def consultar(filtro):
        cur.execute(
            """SELECT frequencia, jogos_analisados FROM padroes_confronto_direto
               WHERE nosso_time_id = %s AND adversario_id = %s AND mandante_filtro = %s AND tipo_padrao = %s
                 AND linha = %s AND resultado = %s AND amostra_pequena = FALSE""",
            (nosso_time_id, adversario_id, filtro, tipo_padrao, linha, resultado),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            return None

        jogos = int(row[1]) if len(row) > 1 and row[1] else 0
        if frequencia_geral is None:
            return (_frequencia_do_row(row, True), jogos)
        return (_encolher_para_prior(float(row[0]), jogos, frequencia_geral), jogos)

    achado = consultar(mandante_filtro)
    if achado is not None:
        return achado

    # Degrau 2: o recorte 'geral' junta mandante + visitante, então tem o
    # dobro da amostra. Só faz sentido tentar se o pedido não era já ele.
    if mandante_filtro != "geral":
        return consultar("geral")

    return None


def buscar_frequencia_forma_recente(cur, resultado, time_id, suavizar=False):
    """NOVO (forma recente): frequência de vitória/empate/derrota nos
    últimos jogos do time (qualquer adversário/mando de campo)."""
    cur.execute(
        "SELECT frequencia FROM padroes_forma_recente WHERE resultado = %s AND time_id = %s ORDER BY janela DESC LIMIT 1",
        (resultado, time_id),
    )
    return _frequencia_do_row(cur.fetchone(), suavizar)


def calcular_fator_forma_recente(cur, resultado_cor, time_id):
    """⚠️ NÃO É MAIS CHAMADA no fluxo de recomendação (desde 02/09/2026).

    Foi substituída por `calcular_fator_forma_recente_combinado`, que
    combina as duas perspectivas do jogo em vez de olhar só um lado - ver
    lá o diagnóstico completo da assimetria que isso corrigiu.

    Mantida no arquivo de propósito: é a versão de UM lado só, e é o que
    precisa voltar a ser chamado se a mudança de 02/09 for revertida.
    Nenhum outro módulo a importa (conferido).

    Retorna o multiplicador a aplicar em cima da probabilidade de
    resultado final (vinda do confronto direto ou da média geral), com
    base em quanto o momento atual do time (últimos jogos) se desvia da
    referência de longo prazo pra esse mesmo resultado. Limitado ao
    intervalo [FATOR_FORMA_MINIMO, FATOR_FORMA_MAXIMO] - mesma filosofia
    do ajuste de árbitro: o momento recente BELISCA a probabilidade, nunca
    domina sobre um dado mais específico (como o confronto direto)."""
    baseline = buscar_frequencia_resultado(cur, "geral", resultado_cor, time_id)
    recente = buscar_frequencia_forma_recente(cur, resultado_cor, time_id)
    if baseline is None or recente is None or baseline == 0:
        return None

    fator = recente / baseline
    return max(FATOR_FORMA_MINIMO, min(FATOR_FORMA_MAXIMO, fator))


def _fator_forma_com_peso(cur, resultado, time_id):
    """NOVO (02/09/2026 - simetria do Grupo A no resultado_final): mesma
    conta de `calcular_fator_forma_recente`, mas devolve `(fator, peso)`
    em vez de só o fator, pra permitir combinar as duas perspectivas do
    jogo ponderando por tamanho de amostra.

    O peso é o `jogos_analisados` da linha de `padroes_forma_recente` -
    o mesmo critério de ponderação já usado em
    `buscar_frequencia_resultado_combinada` e na Dupla Chance por tempo.

    O clamp individual [FATOR_FORMA_MINIMO, FATOR_FORMA_MAXIMO] é aplicado
    AQUI, e o combinado aplica de novo por cima do resultado - piso/teto
    individual + teto combinado, o mesmo princípio de `combinar_fatores`.
    """
    if time_id is None:
        return None

    baseline = buscar_frequencia_resultado(cur, "geral", resultado, time_id)
    if baseline is None or baseline == 0:
        return None

    cur.execute(
        "SELECT frequencia, jogos_analisados FROM padroes_forma_recente "
        "WHERE resultado = %s AND time_id = %s ORDER BY janela DESC LIMIT 1",
        (resultado, time_id),
    )
    row = cur.fetchone()
    if not row or row[0] is None:
        return None

    recente = float(row[0])
    peso = int(row[1]) if len(row) > 1 and row[1] else 0
    if peso <= 0:
        return None

    fator = max(FATOR_FORMA_MINIMO, min(FATOR_FORMA_MAXIMO, recente / baseline))
    return (fator, peso)


def calcular_fator_forma_recente_combinado(cur, resultado_nosso_time, nosso_time_id, adversario_id):
    """NOVO (02/09/2026): fator de forma recente do resultado final,
    combinando as DUAS perspectivas do jogo.

    O DEFEITO QUE ISSO CORRIGE (medido na rodada 25, 29-31/08/2026): a
    frequência base já era simétrica desde 28/08 (ver
    `buscar_frequencia_resultado_combinada`), mas os fatores do Grupo A
    continuavam sendo calculados só sobre `nosso_time_id` - então o mesmo
    evento real recebia ajustes diferentes conforme a perspectiva, e o
    filtro de VE sempre escolhia o lado mais otimista. Exemplo real, jogo
    Vasco x Cruzeiro (fixture 1492359):

        "Vitória do Cruzeiro"  -> forma recente + zona da tabela, 1.03x
        "Derrota do Vasco"     -> forma recente,                  1.15x

    Mesmo evento, dois ajustes. É a MESMA armadilha já corrigida no empate
    em 28/08, aparecendo por outra porta - vitória/derrota nunca tinham
    sido tratados.

    COMO COMBINA: "o Cruzeiro vem vencendo mais que o normal?" e "o Vasco
    vem perdendo mais que o normal?" são duas leituras do mesmo fenômeno,
    ambas legítimas. Em vez de escolher uma (o que deixava a escolha pro
    filtro de VE), tira a média ponderada por amostra - mesmo desenho já
    validado na frequência base e na Dupla Chance etapa 1.

    ESCADA: se só um dos lados tiver dado, usa ele sozinho. Se nenhum,
    devolve None e o fator simplesmente não se aplica (comportamento
    idêntico ao de antes nesse caso).

    ⚠️ `_RESULTADO_ESPELHADO` não é detalhe: sem a inversão, isso somaria
    "o Cruzeiro vem vencendo" com "o Vasco vem vencendo" - eventos
    opostos. Mesma armadilha do campo `lado` com dupla semântica que
    causou o bug do cartão de time.
    """
    if resultado_nosso_time not in _RESULTADO_ESPELHADO:
        return None

    resultado_adversario = _RESULTADO_ESPELHADO[resultado_nosso_time]

    leituras = [
        leitura for leitura in (
            _fator_forma_com_peso(cur, resultado_nosso_time, nosso_time_id),
            _fator_forma_com_peso(cur, resultado_adversario, adversario_id),
        ) if leitura is not None
    ]
    if not leituras:
        return None

    peso_total = sum(peso for _fator, peso in leituras)
    if peso_total <= 0:
        return None

    combinado = sum(fator * peso for fator, peso in leituras) / peso_total
    # Teto combinado por cima do produto das pontas já clampadas - mesma
    # filosofia de `combinar_fatores`.
    return max(FATOR_FORMA_MINIMO, min(FATOR_FORMA_MAXIMO, combinado))


ULTIMOS_JOGOS_PARA_TITULARES = 3
QTD_TITULARES_PROVAVEIS = 11


def buscar_titulares_provaveis(cur, time_id):
    """NOVO (cartão x suspensão - só quem tende a jogar): os jogadores que
    mais apareceram como TITULAR nos últimos ULTIMOS_JOGOS_PARA_TITULARES
    jogos desse time - usado pra restringir o ajuste de cartão x suspensão
    só a quem tem chance real de entrar em campo. Reserva "na régua" que
    nem costuma jogar não muda o comportamento cauteloso do time, então
    não devia pesar na conta (incluir ele só dilui/exagera o ajuste à toa)."""
    if time_id is None:
        return set()
    cur.execute(
        "SELECT id FROM jogos WHERE nosso_time_id = %s AND placar_corinthians IS NOT NULL "
        "ORDER BY data_jogo DESC LIMIT %s",
        (time_id, ULTIMOS_JOGOS_PARA_TITULARES),
    )
    jogo_ids = [row[0] for row in cur.fetchall()]
    if not jogo_ids:
        return set()

    cur.execute(
        "SELECT jogador_id, COUNT(*) AS aparicoes FROM escalacoes "
        "WHERE jogo_id = ANY(%s) AND titular = TRUE "
        "GROUP BY jogador_id ORDER BY aparicoes DESC LIMIT %s",
        (jogo_ids, QTD_TITULARES_PROVAVEIS),
    )
    return {row[0] for row in cur.fetchall()}


def peso_jogadores_na_regua(cur, time_id):
    """NOVO (cartão x suspensão, ponderado por jogador): soma o "peso de
    cautela" dos jogadores desse time que estão a 1 cartão amarelo da
    suspensão automática (2 de 3 acumulados na TEMPORADA_ATUAL) - cada um
    pesa pela PRÓPRIA frequência histórica de tomar cartão
    (padroes_jogador_cartao), não conta igual pra todo mundo. Um
    zagueiro/volante com histórico alto de cartão (ex: 35% dos jogos) pesa
    muito mais nessa soma do que um atacante que quase nunca é cartonado
    (ex: 5%), mesmo os dois estando "na régua" no momento - o atacante
    dificilmente vai mudar o comportamento do jogo por estar cauteloso,
    o zagueiro/volante sim.
    Jogador na régua sem padrão de cartão calculado ainda (poucos jogos
    disputados) entra com um peso neutro (PESO_PADRAO_SEM_HISTORICO_CARTAO),
    em vez de simplesmente não contar - evita "sumir" da conta só por
    faltar dado, sem assumir o pior caso."""
    if time_id is None:
        return 0.0
    cur.execute(
        """
        SELECT c.jogador_id, j.data_jogo
        FROM cartoes c
        JOIN jogos j ON j.id = c.jogo_id
        JOIN jogadores jog ON jog.id = c.jogador_id
        WHERE c.cor = 'amarelo'
          AND EXTRACT(YEAR FROM j.data_jogo) = %s
          AND jog.ativo = TRUE
          AND jog.time_atual_id = %s
        ORDER BY c.jogador_id, j.data_jogo ASC, j.id ASC
        """,
        (TEMPORADA_ATUAL, time_id),
    )
    contagem = {}
    for jogador_id, _data_jogo in cur.fetchall():
        atual = contagem.get(jogador_id, 0) + 1
        contagem[jogador_id] = 0 if atual >= 3 else atual

    # NOVO: só considera quem tem chance real de jogar - reserva raramente
    # usado "na régua" não muda o comportamento do time, então não deveria
    # pesar (ver docstring de buscar_titulares_provaveis).
    titulares_provaveis = buscar_titulares_provaveis(cur, time_id)
    jogadores_na_regua = [
        jid for jid, v in contagem.items() if v == 2 and jid in titulares_provaveis
    ]
    if not jogadores_na_regua:
        return 0.0

    cur.execute(
        "SELECT jogador_id, frequencia FROM padroes_jogador_cartao WHERE jogador_id = ANY(%s)",
        (jogadores_na_regua,),
    )
    frequencias = {jid: float(freq) for jid, freq in cur.fetchall()}

    return sum(
        (frequencias[jid] / 100) if jid in frequencias else PESO_PADRAO_SEM_HISTORICO_CARTAO
        for jid in jogadores_na_regua
    )


def calcular_fator_suspensao(cur, nosso_time_id, adversario_id):
    """NOVO (cartão x suspensão): multiplicador a aplicar na probabilidade
    de "mais de X" cartões total do jogo, com base no peso combinado dos
    jogadores dos DOIS times que estão a 1 cartão da suspensão (ver
    peso_jogadores_na_regua - pondera por histórico individual de cartão,
    não conta todo jogador igual). Só reduz, nunca aumenta (teto 1.0) - e
    tem piso (FATOR_SUSPENSAO_MINIMO), pra não dominar sobre o padrão real
    mesmo com muita gente na régua dos dois lados.
    Retorna None quando ninguém está na régua (não belisca nada, evita
    ficar marcando toda recomendação com um fator 1.00x que não diz nada)."""
    peso_total = peso_jogadores_na_regua(cur, nosso_time_id) + peso_jogadores_na_regua(cur, adversario_id)
    if peso_total == 0:
        return None
    fator = 1.0 - (FATOR_SUSPENSAO_ESCALA * peso_total)
    return max(FATOR_SUSPENSAO_MINIMO, min(FATOR_SUSPENSAO_MAXIMO, fator))


def buscar_media_geral_cartoes(cur):
    """NOVO: média geral de cartões por jogo, calculada a partir de todos os
    árbitros com perfil já calculado. Serve de linha de base pra saber se um
    árbitro específico dá mais ou menos cartão que a média."""
    cur.execute("SELECT AVG(media_cartoes) FROM padroes_arbitro")
    row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def buscar_media_geral_faltas(cur):
    """NOVO: mesma ideia de buscar_media_geral_cartoes, mas pra falta - usa
    o dado que já existia calculado (padroes_arbitro.media_faltas) mas
    nunca tinha sido aproveitado em nenhum ajuste."""
    cur.execute("SELECT AVG(media_faltas) FROM padroes_arbitro")
    row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def buscar_perfil_arbitro(cur, arbitro):
    cur.execute(
        "SELECT media_cartoes, media_faltas FROM padroes_arbitro WHERE arbitro = %s",
        (arbitro,),
    )
    return cur.fetchone()


def calcular_fator_arbitro(cur, arbitro, media_geral, tipo):
    """Retorna o multiplicador a aplicar na probabilidade, com base em
    quanto esse árbitro se desvia da média geral. Limitado ao intervalo
    [FATOR_ARBITRO_MINIMO, FATOR_ARBITRO_MAXIMO]. Retorna None se não
    houver árbitro definido, perfil calculado, ou média geral disponível.

    NOVO: `tipo` decide se usa a média de CARTÃO ou de FALTA do árbitro -
    antes só existia ajuste de cartão, mesmo a média de falta já sendo
    calculada e salva há um tempo (nunca tinha sido usada). Faz sentido
    aplicar nos dois: árbitro rigoroso marca mais falta E mais cartão;
    árbitro que "deixa o jogo rolar" marca menos dos dois."""
    if not arbitro or media_geral is None or media_geral == 0:
        return None

    perfil = buscar_perfil_arbitro(cur, arbitro)
    if not perfil:
        return None

    media_cartoes_arbitro, media_faltas_arbitro = perfil
    media_arbitro = media_cartoes_arbitro if tipo == "cartao" else media_faltas_arbitro
    if media_arbitro is None:
        return None

    fator = float(media_arbitro) / media_geral
    return max(FATOR_ARBITRO_MINIMO, min(FATOR_ARBITRO_MAXIMO, fator))


def resultado_do_ponto_de_vista_corinthians(direcao, mandante):
    """Traduz o outcome da odd (1/X/2) para vitória/empate/derrota do
    Corinthians, considerando se ele é mandante ou visitante nesse jogo."""
    d = (direcao or "").strip().upper()

    if d in ("X", "EMPATE", "DRAW"):
        return "empate"
    if d == "1":
        return "vitoria" if mandante else "derrota"
    if d == "2":
        return "vitoria" if not mandante else "derrota"
    return None


# ==================== NOVO (Grupo A de integração) ====================

def montar_descricao_handicap(nome_time, mandante, linha):
    """NOVO (20/08/2026): traduz a linha de handicap asiático pro nome com
    que a aposta REALMENTE aparece na tela da Superbet, e corrige o sinal
    pro time visitante.

    DOIS PROBLEMAS QUE ISSO RESOLVE:

    1) SINAL INVERTIDO PRO VISITANTE. A `linha` que a OddsPapi manda é
       SEMPRE relativa ao MANDANTE (convenção da fonte). Colar o nome do
       time do lado desse número faz a descrição do visitante sair com o
       sinal trocado: aparecia "Flamengo -0.5" quando a aposta real era
       "Flamengo não perde" (ou seja, +0.5 do ponto de vista dele). O
       CÁLCULO nunca esteve errado - calcular_padrao_handicap já inverte
       o sinal pro visitante (`limite = -linha if mandante else linha`) -
       era só o texto.

    2) NOME QUE NÃO EXISTE NA CASA. A OddsPapi entrega tudo num mercado
       só (`spreads`), mas a Superbet distribui as MESMAS apostas em
       blocos com nomes diferentes: as linhas -0.5 ela vende como
       "Resultado Final", as +0.5 como "Dupla Chance", e só as de quarto
       de gol/linha cheia ficam no bloco "Handicap Asiático". Por isso
       procurar "Handicap Asiático - Cruzeiro +0.5" na Superbet não acha
       nada: lá isso se chama "Dupla Chance - 1 ou Empate".

       Não é equivalência aproximada, é identidade matemática: com meio
       gol de vantagem o empate vira vitória, então "+0.5" e "vence ou
       empata" são a mesma aposta - e por isso as odds batem exatamente
       (1.59 e 1.59 no jogo Cruzeiro x Flamengo de 22/08/2026).

    3) NOME "ASIÁTICO" TAMBÉM ESTAVA ERRADO PRAS LINHAS DE 1.5 PRA CIMA
       (confirmado por print real da Superbet em 20/08/2026). A casa
       separa em DOIS blocos distintos na tela: "Handicap Asiático" (só
       quarto de gol e meia/cheia baixa, -1.25 a +0.75 no exemplo
       observado) e "Handicap" puro (as linhas maiores: ±1.5, ±2.5,
       ±3.5...). O projeto só captura meia linha, então tudo que sobra
       depois de tratar os casos de ±0.5 cai automaticamente no bloco
       "Handicap" (sem "Asiático") - nunca no bloco Asiático de verdade,
       porque esse é só quarto de gol/linha baixa e está fora de escopo.
    """
    # ponto de vista do time: pro mandante a linha vale como veio; pro
    # visitante o sinal inverte (mesma regra do cálculo do padrão).
    linha_time = float(linha) if mandante else -float(linha)

    if linha_time == 0.5:
        return f"Dupla Chance - {nome_time} ou Empate"
    if linha_time == -0.5:
        return f"Resultado Final - Vitória do {nome_time}"
    return f"Handicap - {nome_time} {linha_time:+.1f}"


def aplicar_fator(frequencia, fator, espaco_chances=False):
    """Aplica um fator do Grupo A sobre uma frequência (0-100).

    `espaco_chances=False` (padrão) - comportamento histórico: multiplica a
    probabilidade direto e corta em 100. Mantido em todos os mercados menos
    escanteio, porque eles estão calibrados COM essa fórmula hoje
    (cartao_total, por exemplo, está em +5,3/+3,5 de gap) e trocar tudo de
    uma vez tiraria a possibilidade de medir o que mudou.

    `espaco_chances=True` - converte pra chances, multiplica, converte de
    volta:

        chance = p / (1 - p);  chance * fator;  p = chance / (1 + chance)

    POR QUE ISSO EXISTE (medido em 28/08/2026, Atlético-PR x Fluminense):
    "Escanteios Total - Mais de 7.5" apareceu com 100,0% de probabilidade e
    odd 1.34. O 100% NÃO era o modelo afirmando certeza - era o corte. O
    confronto já encolhido dava ~93%, a correlação de 1.09x pediu 101,4%, e
    o `min(..., 100.0)` cravou em 100.

    Três problemas que isso causava, todos resolvidos aqui:
      1. Probabilidade de 100% não existe - promete certeza que nenhum dado
         sustenta, e o filtro de VE adora (VE = 0.34 naquele caso).
      2. A perna virava fantasma na múltipla: multiplica por 1,0 e some sem
         deixar rastro. Visto ao vivo - múltipla de escanteio 100% + gols
         96% saía exatamente 96%.
      3. O corte destruía informação: 101% e 130% viravam ambos "100%", e a
         diferença entre uma aposta boa e uma absurda sumia.

    Em espaço de chances o estouro é IMPOSSÍVEL por construção, e o efeito
    do fator diminui naturalmente perto dos extremos - o que também é mais
    fiel ao que o fator significa: "9% mais escanteios" quase não muda a
    chance de passar de 7.5 num jogo que já espera 11.

    Efeito colateral conhecido e aceito: o fator fica MAIS FRACO em toda a
    escala (4-6 pontos no meio), simetricamente nas duas direções - fator
    que aumenta aumenta menos, fator que diminui diminui menos. Não há
    viés pra cima nem pra baixo. Se depois de 2-3 rodadas o efeito ficar
    fraco demais, o ajuste é alargar os tetos (FATOR_*_MINIMO/MAXIMO), não
    voltar pra multiplicação direta."""
    if fator is None or frequencia is None:
        return frequencia
    if not espaco_chances:
        return min(round(frequencia * fator, 2), 100.0)

    p = frequencia / 100
    if p <= 0 or p >= 1:
        # 0% e 100% crus não têm chance definida (divisão por zero). Não
        # deveriam chegar aqui - a suavização de Laplace trava os extremos
        # antes - mas se chegarem, devolve como está em vez de estourar.
        return round(frequencia, 2)
    chance = (p / (1 - p)) * fator
    return round(100 * chance / (1 + chance), 2)


def combinar_fatores(fatores):
    """Recebe uma lista de (fator_ou_None, descricao_curta). Ignora os
    None (fator não pôde ser calculado ou amostra insuficiente), multiplica
    o resto, e aplica o TETO COMBINADO por cima do produto - segunda
    camada de segurança, por cima do piso/teto que cada fator individual
    já tem. Devolve (fator_combinado_ou_None, [descricoes_aplicadas])."""
    validos = [(f, d) for f, d in fatores if f is not None]
    if not validos:
        return None, []
    produto = 1.0
    for f, _ in validos:
        produto *= f
    produto = max(TETO_COMBINADO_MINIMO, min(TETO_COMBINADO_MAXIMO, produto))
    return round(produto, 4), [d for _, d in validos]


def buscar_media_geral_time(cur, time_id, tipo_padrao):
    """Média geral do time nesse tipo de padrão, usada como denominador
    pra transformar o valor bruto de Zona/Rodada num FATOR (valor / média
    geral) - só existe pra "resultado" (taxa de vitória) e "escanteio"
    (média de escanteios do próprio time), os dois únicos tipos de
    Zona/Rodada com mercado apostável correspondente hoje."""
    if tipo_padrao == "resultado":
        cur.execute(
            """
            SELECT AVG(CASE WHEN placar_corinthians > placar_adversario THEN 1.0 ELSE 0.0 END)
            FROM jogos
            WHERE nosso_time_id = %s AND data_jogo < CURRENT_DATE
              AND placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL
            """,
            (time_id,),
        )
    elif tipo_padrao == "escanteio":
        cur.execute(
            """
            SELECT AVG(eg.escanteios)
            FROM jogos j JOIN estatisticas_jogo eg ON eg.jogo_id = j.id
            WHERE j.nosso_time_id = %s AND j.data_jogo < CURRENT_DATE
              AND ((j.mandante = TRUE AND eg.lado = 'mandante') OR (j.mandante = FALSE AND eg.lado = 'visitante'))
            """,
            (time_id,),
        )
    else:
        return None
    row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def buscar_contexto_zona_time(cur, time_id, api_football_team_id, temporada, rodada_numero):
    """Zona (g4/meio/z4) em que o time está ANTES desse jogo (rodada
    anterior, mesma temporada) + condição de momento ("apos_vitoria"/
    "apos_empate"/"apos_derrota"/"geral") baseada no resultado do jogo
    anterior dele nessa temporada. Devolve (None, "geral") se faltar dado
    (início de temporada, ou jogos_liga/popular_tabela.py sem cobertura)."""
    if not rodada_numero or not temporada or not api_football_team_id or rodada_numero < 2:
        return None, "geral"

    rodada_anterior = rodada_numero - 1
    tab = calcular_tabela(cur, temporada, rodada_anterior)
    item = next((t for t in tab if t["time_api_id"] == api_football_team_id), None)
    if item is None:
        return None, "geral"

    cur.execute(
        """
        SELECT CASE WHEN placar_corinthians > placar_adversario THEN 'vitoria'
                    WHEN placar_corinthians = placar_adversario THEN 'empate'
                    ELSE 'derrota' END
        FROM jogos
        WHERE nosso_time_id = %s AND rodada_numero = %s
          AND EXTRACT(YEAR FROM data_jogo) = %s
          AND placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL
        LIMIT 1
        """,
        (time_id, rodada_anterior, temporada),
    )
    row = cur.fetchone()
    condicao = f"apos_{row[0]}" if row else "geral"
    return item["zona"], condicao


def buscar_fator_zona(cur, time_id, zona, condicao, tipo_padrao):
    """Fator = valor guardado em padroes_zona_time / média geral do time
    nesse tipo. Se a condição específica (apos_vitoria etc.) não tiver
    amostra suficiente, cai pra "geral" dessa zona antes de desistir -
    mesma filosofia de fallback do confronto direto (fonte mais
    específica primeiro, mais genérica depois, nunca None se tiver
    QUALQUER dado aproveitável)."""
    if zona is None:
        return None

    def _buscar(cond):
        cur.execute(
            "SELECT valor, jogos_amostra FROM padroes_zona_time "
            "WHERE time_id = %s AND tipo_padrao = %s AND zona = %s AND condicao = %s",
            (time_id, tipo_padrao, zona, cond),
        )
        return cur.fetchone()

    row = _buscar(condicao)
    # NOVO (correção de amostra pequena): piso mais alto quando a condição
    # é de momento específico (apos_vitoria/empate/derrota) - ver
    # JOGOS_MINIMOS_FATOR_ZONA_MOMENTO. Pro "geral", o piso continua o
    # padrão (JOGOS_MINIMOS_FATOR_RECOMENDACAO).
    piso_condicao_atual = JOGOS_MINIMOS_FATOR_ZONA_MOMENTO if condicao != "geral" else JOGOS_MINIMOS_FATOR_RECOMENDACAO
    if (not row or row[1] < piso_condicao_atual) and condicao != "geral":
        row = _buscar("geral")
    if not row or row[1] < JOGOS_MINIMOS_FATOR_RECOMENDACAO:
        return None

    media_geral = buscar_media_geral_time(cur, time_id, tipo_padrao)
    if not media_geral:
        return None
    fator = float(row[0]) / media_geral
    return max(FATOR_ZONA_MINIMO, min(FATOR_ZONA_MAXIMO, fator))


def buscar_fator_rodada(cur, time_id, rodada_numero, tipo_padrao):
    """Fator = valor guardado em padroes_rodada_bruto (na rodada exata
    desse jogo) / média geral do time nesse tipo."""
    if not rodada_numero:
        return None
    cur.execute(
        "SELECT valor, jogos_amostra FROM padroes_rodada_bruto "
        "WHERE time_id = %s AND tipo_padrao = %s AND rodada_numero = %s",
        (time_id, tipo_padrao, rodada_numero),
    )
    row = cur.fetchone()
    if not row or row[1] < JOGOS_MINIMOS_FATOR_RECOMENDACAO:
        return None

    media_geral = buscar_media_geral_time(cur, time_id, tipo_padrao)
    if not media_geral:
        return None
    fator = float(row[0]) / media_geral
    return max(FATOR_RODADA_MINIMO, min(FATOR_RODADA_MAXIMO, fator))


def buscar_fator_estilo(cur, time_id, tipo, papel):
    """Fator já vem pronto em padroes_estilo_time (media_time/media_liga),
    só precisa aplicar o piso/teto individual - diferente de Zona/Rodada,
    que guardam valor bruto e precisam dividir pela média geral aqui."""
    if time_id is None:
        return None
    cur.execute(
        "SELECT fator, jogos_analisados FROM padroes_estilo_time "
        "WHERE time_id = %s AND tipo = %s AND papel = %s",
        (time_id, tipo, papel),
    )
    row = cur.fetchone()
    if not row or row[1] < JOGOS_MINIMOS_FATOR_RECOMENDACAO:
        return None
    fator = float(row[0])
    return max(FATOR_ESTILO_MINIMO, min(FATOR_ESTILO_MAXIMO, fator))


# ========= NOVO (Correlação entre Estatísticas na fórmula) =========
#
# Diferente de Zona/Rodada/Estilo, que descrevem uma característica do time
# JÁ CONHECIDA antes do jogo (ele está no G4, ele é ofensivo, é a rodada
# 15), a correlação mede a relação entre DUAS estatísticas que só existem
# depois do apito inicial, dentro do MESMO jogo (chutes e escanteios da
# mesma partida). Isso significa que ela NÃO pode ser aplicada direto: no
# momento de gerar a recomendação, "chutes vai ficar acima da média?" é
# uma incógnita tão grande quanto a própria estatística que a gente quer
# prever.
#
# Solução (2 etapas):
#   1) ESTIMAR A pra esse confronto usando a média histórica de cada um dos
#      dois times naquela estatística, somadas (mandante + visitante). Isso
#      já é dado conhecido ANTES do jogo.
#   2) Comparar essa estimativa com a média da liga (media_a, já salva) pra
#      decidir se esse jogo é "de A alto" ou "de A baixo", e então aplicar
#      o efeito correspondente sobre B (valor_b_acima ou valor_b_abaixo).
#
# Não precisou de tabela/coluna/migração nova: a média geral de B (o
# denominador que transforma o valor bruto em FATOR) é derivável do que já
# está salvo, porque os grupos "acima" e "abaixo" particionam TODOS os
# jogos sem sobra - a média ponderada dos dois é exatamente a média geral.

# Pares que viram ajuste de verdade. Só entram aqui pares cujo lado B tem
# mercado apostável real implementado no projeto (princípio do projeto:
# confirmar mercado no nível certo antes de integrar estatística na
# fórmula). O terceiro par calculado por motor_padroes.py
# (desarmes -> faltas) continua existindo e aparecendo na tela, mas fica
# de fora daqui: não existe mercado "Faltas Total do Jogo" implementado, e
# não está confirmado se a Superbet sequer oferece esse mercado.
#
# Formato: tipo_de_mercado -> (par_no_banco, coluna_de_A_em_estatisticas_jogo)
CORRELACAO_POR_MERCADO = {
    "escanteio_total": ("chutes_escanteios", "finalizacoes"),
    "cartao_total": ("faltas_cartoes", "faltas"),
}


def buscar_media_estatistica_time(cur, time_id, coluna):
    """Média histórica de UM time numa estatística do próprio lado dele
    (não o total do jogo) - usada pra estimar quanto de A esse confronto
    tende a produzir, ANTES do jogo acontecer.

    Funciona inclusive pra time NÃO rastreado: quando ele enfrenta um time
    nosso, `estatisticas_jogo` guarda a linha dos DOIS lados, então o dado
    dele existe (amostra menor, só os jogos contra times nossos, mas real).

    Dedup por `fixture_id_api` é obrigatório: quando os dois times de um
    jogo são rastreados, esse mesmo jogo real aparece 2x em `jogos` (uma
    visão pra cada time) - sem o DISTINCT, o jogo pesaria dobrado na média.
    """
    if time_id is None or coluna not in ("finalizacoes", "faltas"):
        return None

    cur.execute(
        f"""
        SELECT AVG(valor), COUNT(*)
        FROM (
            SELECT DISTINCT ON (j.fixture_id_api) eg.{coluna} AS valor
            FROM jogos j
            JOIN estatisticas_jogo eg ON eg.jogo_id = j.id
            WHERE j.data_jogo < CURRENT_DATE
              AND j.fixture_id_api IS NOT NULL
              AND eg.{coluna} IS NOT NULL
              AND (
                    (j.mandante_id = %s AND eg.lado = 'mandante')
                 OR (j.visitante_id = %s AND eg.lado = 'visitante')
              )
            ORDER BY j.fixture_id_api, j.id
        ) unicos
        """,
        (time_id, time_id),
    )
    row = cur.fetchone()
    if not row or row[0] is None or row[1] < JOGOS_MINIMOS_FATOR_RECOMENDACAO:
        return None
    return float(row[0])


def buscar_correlacao_liga(cur, par):
    """Lê a correlação geral da liga já calculada por motor_padroes.py.

    Devolve (media_a, valor_b_acima, valor_b_abaixo, media_b) ou None.

    `media_b` NÃO está salva no banco - é derivada aqui. Como os grupos
    "A acima da média" e "A abaixo da média" cobrem todos os jogos sem
    sobra nem sobreposição, a média ponderada dos dois grupos é exatamente
    a média geral de B. Por isso essa integração não precisou de migração.

    Exige amostra mínima nos DOIS lados: um lado raso significa que o
    "efeito" medido é ruído de poucos jogos, não sinal - mesmo cuidado que
    motivou o piso mais alto do fator de Zona da Tabela.
    """
    cur.execute(
        """
        SELECT media_a, valor_b_acima, valor_b_abaixo, jogos_acima, jogos_abaixo, jogos_total
        FROM padroes_correlacao_estatisticas
        WHERE par = %s
        """,
        (par,),
    )
    row = cur.fetchone()
    if not row:
        return None

    media_a, valor_b_acima, valor_b_abaixo, jogos_acima, jogos_abaixo, jogos_total = row
    if jogos_acima < JOGOS_MINIMOS_FATOR_RECOMENDACAO or jogos_abaixo < JOGOS_MINIMOS_FATOR_RECOMENDACAO:
        return None
    if not jogos_total:
        return None

    media_b = (
        float(valor_b_acima) * jogos_acima + float(valor_b_abaixo) * jogos_abaixo
    ) / float(jogos_total)
    if media_b == 0:
        return None

    return float(media_a), float(valor_b_acima), float(valor_b_abaixo), media_b


def buscar_fator_correlacao(cur, tipo_padrao, mandante_id, visitante_id):
    """Fator de correlação pra um mercado de JOGO INTEIRO.

    Retorna None (fator simplesmente ignorado por combinar_fatores) em
    qualquer situação de dúvida: par não mapeado, correlação sem amostra,
    ou um dos dois times sem média própria suficiente. Nunca "chuta" -
    preferir ficar sem ajuste é sempre mais seguro que aplicar um ajuste
    baseado em amostra rasa.
    """
    config = CORRELACAO_POR_MERCADO.get(tipo_padrao)
    if not config or mandante_id is None or visitante_id is None:
        return None

    par, coluna_a = config

    dados = buscar_correlacao_liga(cur, par)
    if dados is None:
        return None
    media_a, valor_b_acima, valor_b_abaixo, media_b = dados

    media_mandante = buscar_media_estatistica_time(cur, mandante_id, coluna_a)
    media_visitante = buscar_media_estatistica_time(cur, visitante_id, coluna_a)
    if media_mandante is None or media_visitante is None:
        return None

    # Etapa 1: estimativa de A pra ESSE confronto (soma dos dois lados,
    # porque media_a da liga também é do TOTAL do jogo - as duas coisas
    # precisam estar na mesma unidade pra comparação fazer sentido).
    a_estimado = media_mandante + media_visitante

    # Etapa 2: escolhe o lado do efeito, mesma regra de corte usada no
    # cálculo original (> média = grupo "acima"; <= média = grupo "abaixo").
    valor_b = valor_b_acima if a_estimado > media_a else valor_b_abaixo

    # Etapa 3: vira fator dividindo pela média geral de B.
    fator = valor_b / media_b
    return max(FATOR_CORRELACAO_MINIMO, min(FATOR_CORRELACAO_MAXIMO, fator))

# ================== fim das funções novas do Grupo A ==================


def buscar_fator_correlacao_time(cur, par, adversario_id):
    """NOVO (21/08/2026): fator de correlação pra mercado de UM TIME, não
    do jogo inteiro. Usado no cartão por time (par faltas -> cartões).

    POR QUE NÃO DÁ PRA REUSAR buscar_fator_correlacao AQUI
    ------------------------------------------------------
    Aquela função estima A somando os DOIS times (`media_mandante +
    media_visitante`), o que é correto pra mercado de jogo inteiro. Pro
    mercado de um time só, isso conta a mesma informação DUAS VEZES: a
    frequência-base já é o histórico de cartões DAQUELE time, que por
    definição já embute o quanto ele falta. Somar a falta dele de novo no
    `a_estimado` é redundância, não informação nova.

    O que a base NÃO sabe é contra QUEM ele vai jogar dessa vez. Por isso
    aqui A é estimado usando SÓ a média de faltas do ADVERSÁRIO - é o
    único lado que traz informação que a base ainda não tem.

    UNIDADES
    --------
    `media_a` salva no banco é a média de faltas do JOGO INTEIRO (dois
    times somados). Como aqui comparamos a média de UM time só, o corte
    tem que ser `media_a / 2` - a média por time da liga. Sem essa
    divisão, a comparação misturaria escalas e quase todo adversário
    cairia no grupo "abaixo".

    O fator em si (`valor_b / media_b`) é uma RAZÃO adimensional ("jogos
    faltosos têm X% mais cartão que a média"), então aplicá-lo sobre uma
    frequência de nível de time é legítimo - a proporção vale igual.
    """
    if adversario_id is None:
        return None

    dados = buscar_correlacao_liga(cur, par)
    if dados is None:
        return None
    media_a, valor_b_acima, valor_b_abaixo, media_b = dados
    if not media_b:
        return None

    media_adversario = buscar_media_estatistica_time(cur, adversario_id, "faltas")
    if media_adversario is None:
        return None

    # Corte na média POR TIME da liga (metade da média do jogo inteiro).
    media_a_por_time = float(media_a) / 2.0

    valor_b = valor_b_acima if media_adversario > media_a_por_time else valor_b_abaixo
    fator = float(valor_b) / float(media_b)
    return max(FATOR_CORRELACAO_MINIMO, min(FATOR_CORRELACAO_MAXIMO, fator))


def jogador_disponivel(cur, jogador_id, jogo_id):
    """Evita recomendar aposta em jogador que provavelmente não vai jogar
    (suspenso, lesionado, cortado do time).

    Prioridade 0 - lesão/suspensão reportada (API-Football /injuries,
    coletada em atualizar_odds.py): se existe um registro pra esse
    jogador nesse jogo específico, é o sinal mais direto e com mais
    antecedência que temos - normalmente aparece dias antes do jogo, bem
    antes da escalação sair. Bloqueia direto, sem precisar dos fallbacks
    abaixo.

    Prioridade 1 - escalação CONFIRMADA da partida específica: a API-Football
    normalmente libera isso só perto do jogo (às vezes só ~1h antes), então
    nem sempre vai estar disponível quando esse script rodar. Se já tiver
    sido capturada (jogo_tem_escalacao), essa é a fonte mais confiável -
    usa ela e ignora qualquer outra coisa.

    Prioridade 2 - fallback pros últimos 3 jogos: se ainda não temos a
    escalação confirmada dessa partida específica, olha se o jogador
    apareceu (titular OU reserva, não precisa ter entrado em campo) em pelo
    menos 1 dos últimos 3 jogos concluídos. Se sumiu das 3 escalações
    seguidas, é sinal razoável de lesão/suspensão/saída do time. Se só
    ficou de fora uma vez (rotação normal), continua sendo tratado como
    disponível.

    Se não houver dado de escalação suficiente pra decidir (pipeline ainda
    não processou, ou jogador muito novo no banco), NÃO bloqueia - dado
    insuficiente não deve descartar uma recomendação que poderia ser boa."""
    cur.execute(
        "SELECT 1 FROM lesoes_suspensoes WHERE jogo_id = %s AND jogador_id = %s",
        (jogo_id, jogador_id),
    )
    if cur.fetchone() is not None:
        return False

    cur.execute("SELECT COUNT(*) FROM escalacoes WHERE jogo_id = %s", (jogo_id,))
    tem_escalacao_confirmada = cur.fetchone()[0] > 0

    if tem_escalacao_confirmada:
        cur.execute(
            "SELECT 1 FROM escalacoes WHERE jogo_id = %s AND jogador_id = %s",
            (jogo_id, jogador_id),
        )
        return cur.fetchone() is not None

    cur.execute(
        """
        SELECT COUNT(*) FROM escalacoes
        WHERE jogo_id IN (
            SELECT id FROM jogos WHERE data_jogo < CURRENT_DATE ORDER BY data_jogo DESC LIMIT %s
        )
        """,
        (JOGOS_JANELA_DISPONIBILIDADE,),
    )
    tem_dado_recente_geral = cur.fetchone()[0] > 0

    if not tem_dado_recente_geral:
        return True  # sem dado suficiente pra decidir - não bloqueia

    cur.execute(
        """
        SELECT COUNT(*) FROM escalacoes
        WHERE jogador_id = %s
          AND jogo_id IN (
              SELECT id FROM jogos WHERE data_jogo < CURRENT_DATE ORDER BY data_jogo DESC LIMIT %s
          )
        """,
        (jogador_id, JOGOS_JANELA_DISPONIBILIDADE),
    )
    apareceu_nos_recentes = cur.fetchone()[0]
    return apareceu_nos_recentes > 0


def calcular_recomendacoes(cur):
    odds = buscar_odds_futuras(cur)
    recomendacoes = []
    jogadores_indisponiveis_pulados = 0

    # NOVO: calcula a média geral de cartões e faltas uma única vez, fora do loop
    media_geral_cartoes = buscar_media_geral_cartoes(cur)
    media_geral_faltas = buscar_media_geral_faltas(cur)

    # NOVO (multi-time): nome de cada time (+ apelidos conhecidos, já que o
    # mesmo time pode aparecer com nomes diferentes em partes diferentes da
    # resposta da OddsPapi), usado pra montar a descrição do resultado
    # final com o nome certo e pra identificar de quem é um mercado de
    # escanteio_time (ver uso mais abaixo).
    # NOVO (Grupo A): também guarda api_football_team_id por time - usado
    # pra achar a posição na tabela reconstruída (Zona da Tabela).
    cur.execute("SELECT id, nome, apelidos, api_football_team_id FROM times")
    variantes_times = {}
    nomes_times = {}
    api_ids_times = {}
    for time_id_row, nome_row, apelidos_row, api_id_row in cur.fetchall():
        nomes_times[time_id_row] = nome_row
        variantes_times[time_id_row] = {nome_row.lower()} | {a.lower() for a in (apelidos_row or [])}
        api_ids_times[time_id_row] = api_id_row

    # NOVO (suavização): quantos jogos concluídos cada time tem, pra
    # decidir quais entram na suavização de Laplace. Carregado uma vez
    # aqui, fora do loop - ver carregar_total_jogos_por_time.
    total_jogos_por_time = carregar_total_jogos_por_time(cur)

    for (odd_id, jogo_id, jogador_id, casa, mercado, valor_odd,
         linha, direcao, data_jogo, adversario, mandante, arbitro,
         mandante_id, visitante_id, nosso_time_id, rodada_numero, temporada) in odds:

        tipo = identificar_tipo_padrao(mercado)
        if tipo is None:
            continue

        # NOVO (suavização): esse time tem histórico curto o bastante pra
        # que uma frequência extrema seja provavelmente ruído de amostra?
        # Se sim, toda frequência-base dessa odd sai suavizada. Vale tanto
        # pro mercado de time quanto pro de jogador - um jogador de time
        # recém-promovido tem exatamente a mesma amostra magra.
        suavizar = time_tem_historico_curto(total_jogos_por_time, nosso_time_id)

        # NOVO: pula qualquer mercado de jogador específico se ele
        # provavelmente não vai jogar (ver docstring de jogador_disponivel).
        if jogador_id and not jogador_disponivel(cur, jogador_id, jogo_id):
            jogadores_indisponiveis_pulados += 1
            continue

        frequencia = None
        resultado_cor = None
        fator_arbitro_aplicado = None
        veio_de_confronto_direto = False
        # NOVO (28/08/2026): quantos jogos o confronto direto tinha - só é
        # preenchido em escanteio_total (único mercado que usa
        # buscar_confronto_detalhado hoje). Vai pra descrição, pra dar pra
        # auditar depois com que amostra cada recomendação foi calculada.
        jogos_confronto_direto = None
        fator_suspensao_aplicado = None
        descricoes_grupo_a = []  # NOVO (Grupo A): nomes dos fatores novos aplicados nessa perna
        fator_grupo_a_aplicado = None  # NOVO (Grupo A): multiplicador final já combinado+limitado

        # NOVO (confronto direto): identifica o adversário por ID (não por
        # texto - evita o problema de nomes grafados diferente entre
        # fontes) e o filtro de mandante/visitante correspondente, usados
        # pra tentar uma frequência específica contra esse adversário antes
        # de cair pro padrão geral do time.
        # NOVO (multi-time): usa nosso_time_id (vindo da própria linha de
        # odds, via jogos.nosso_time_id) em vez de um id fixo - cada jogo
        # já sabe de qual time rastreado ele é.
        adversario_id = None
        if nosso_time_id is not None and mandante_id is not None and visitante_id is not None:
            adversario_id = visitante_id if mandante_id == nosso_time_id else mandante_id
        mandante_filtro_atual = "mandante" if mandante else "visitante"

        # NOVO (Grupo A): api_football_team_id do nosso time - usado pra
        # achar a posição na tabela reconstruída (Zona da Tabela). Pode
        # ser None (time não cadastrado/sem api id) - as funções de fator
        # tratam isso como "sem dado", sem quebrar nada.
        nosso_time_api_id = api_ids_times.get(nosso_time_id)

        # NOVO: suporte ao lado "Menos"/"Não" de cada mercado, além do "Mais"/
        # "Sim" que já existia. A tabela de padrão sempre guarda a frequência
        # do lado "Mais"/"Sim" (ex: "frequência de passar de 7.5 escanteios");
        # o lado oposto tem frequência complementar (100 - frequência), já
        # que os dois lados juntos somam 100% dos jogos. Sem isso, o sistema
        # deixava de considerar metade de cada mercado - e é comum o lado
        # "Menos" ter Valor Esperado positivo mesmo quando o "Mais" não tem
        # (a odd de cada lado é precificada separadamente pela casa).
        direcao_normalizada = (direcao or "").strip().lower()

        if tipo == "cartao" and jogador_id and direcao_normalizada in ("sim", "não", "nao"):
            frequencia_bruta = buscar_frequencia_cartao(cur, jogador_id, suavizar=suavizar)
            if frequencia_bruta is not None:
                # NOVO (Grupo A): árbitro (já existia) + Estilo de Jogo -
                # ofensivo do NOSSO time (nosso time tende a levar mais/
                # menos cartão por conta própria) + defensivo do
                # ADVERSÁRIO (jogar contra esse adversário tende a fazer a
                # gente levar mais/menos cartão). Os três juntos, cada um
                # já limitado individualmente, com um teto combinado por
                # cima (ver combinar_fatores).
                fator_arbitro = calcular_fator_arbitro(cur, arbitro, media_geral_cartoes, "cartao")
                fator_estilo_ofensivo = buscar_fator_estilo(cur, nosso_time_id, "cartao", "ofensivo")
                fator_estilo_defensivo = buscar_fator_estilo(cur, adversario_id, "cartao", "defensivo")

                fator_grupo_a_aplicado, descricoes_grupo_a = combinar_fatores([
                    (fator_arbitro, "árbitro"),
                    (fator_estilo_ofensivo, "estilo ofensivo"),
                    (fator_estilo_defensivo, "estilo defensivo do adversário"),
                ])
                if fator_grupo_a_aplicado is not None:
                    frequencia_bruta = min(round(frequencia_bruta * fator_grupo_a_aplicado, 2), 100.0)

                frequencia = frequencia_bruta if direcao_normalizada == "sim" else round(100 - frequencia_bruta, 2)

        elif tipo == "cartao" and not jogador_id \
                and direcao_normalizada in ("mais", "menos") and linha is not None:
            # NOVO: cartão por TIME (mercado de linha Mais/Menos, "Cartões -
            # Mais/Menos Equipe 1/2") - destravado pela correção do bug de
            # plural em atualizar_odds.py ("Cartões" não batia com
            # "cartão"/"cartao"). Mesma identificação de time por variantes
            # de nome que escanteio_time/gols_time já usam.
            #
            # NOVO (21/08/2026): esse mercado nasceu SEM nenhum ajuste, de
            # propósito (medir antes de decidir). Agora recebe os três:
            # confronto direto, árbitro e correlação.
            #
            # O que motivou: sem confronto direto, esse mercado ia na média
            # BRUTA do time contra qualquer adversário, enquanto o
            # cartao_total já enxergava a rivalidade. Num clássico isso
            # gerava recomendações contraditórias do MESMO jogo - ex:
            # "Menos de 3.5 pro Corinthians" + "Menos de 2.5 pro Palmeiras"
            # (soma 6) convivendo com "Mais de 8.5 no total". E, como são
            # tipos de mercado diferentes, chave_mercado_da_perna não
            # impedia as três de caírem na MESMA múltipla, multiplicadas
            # como independentes quando são quase mutuamente exclusivas.
            mercado_lower = mercado.lower()
            adversario_id_calc = None
            if mandante_id is not None and visitante_id is not None:
                adversario_id_calc = visitante_id if mandante_id == nosso_time_id else mandante_id

            candidatos_nosso_time = variantes_times.get(nosso_time_id, set())
            candidatos_adversario = variantes_times.get(adversario_id_calc, set()) | {(adversario or "").lower()}

            bate_nosso_time = any(c and c in mercado_lower for c in candidatos_nosso_time)
            bate_adversario = any(c and c in mercado_lower for c in candidatos_adversario)

            if bate_nosso_time and not bate_adversario:
                # Confronto direto tem prioridade sobre o padrão geral -
                # mesma regra dos outros mercados que já usam confronto.
                frequencia_bruta = buscar_frequencia_confronto(
                    cur, nosso_time_id, adversario_id, mandante_filtro_atual, "cartao_time", linha=linha,
                    suavizar=suavizar
                )
                if frequencia_bruta is not None:
                    veio_de_confronto_direto = True
                else:
                    frequencia_bruta = buscar_frequencia_cartao_time(cur, linha, nosso_time_id, suavizar=suavizar)

                if frequencia_bruta is not None:
                    # Correlação faltas -> cartões no nível de TIME: usa só
                    # a média do ADVERSÁRIO, nunca a soma dos dois (ver
                    # buscar_fator_correlacao_time - somar a falta do
                    # próprio time contaria de novo algo que a base já tem).
                    fator_correlacao = buscar_fator_correlacao_time(
                        cur, "faltas_cartoes", adversario_id_calc
                    )
                    fator_arbitro_cartao = calcular_fator_arbitro(cur, arbitro, media_geral_cartoes, "cartao")
                    fator_grupo_a_aplicado, descricoes_grupo_a = combinar_fatores([
                        (fator_correlacao, "correlação faltas/cartões"),
                        (fator_arbitro_cartao, "árbitro"),
                    ])
                    if fator_grupo_a_aplicado is not None:
                        frequencia_bruta = min(round(frequencia_bruta * fator_grupo_a_aplicado, 2), 100.0)

                    frequencia = frequencia_bruta if direcao_normalizada == "mais" else round(100 - frequencia_bruta, 2)

        elif tipo == "falta_cometida" and jogador_id \
                and direcao_normalizada in ("mais", "menos") and linha is not None:
            # NOVO: falta cometida agora também recebe ajuste de árbitro -
            # separado do bloco genérico de linha (desarme/chute), porque só
            # falta tem relação com o perfil do árbitro (árbitro rigoroso
            # apita mais falta, não faz o jogador chutar mais no gol).
            frequencia_bruta = buscar_frequencia_linha_jogador(cur, jogador_id, tipo, linha, suavizar=suavizar)
            if frequencia_bruta is not None:
                fator = calcular_fator_arbitro(cur, arbitro, media_geral_faltas, "falta")
                if fator is not None:
                    frequencia_bruta = min(round(frequencia_bruta * fator, 2), 100.0)
                    fator_arbitro_aplicado = fator

                frequencia = frequencia_bruta if direcao_normalizada == "mais" else round(100 - frequencia_bruta, 2)

        elif tipo in ("desarme", "chute_no_gol", "chute_total") and jogador_id \
                and direcao_normalizada in ("mais", "menos") and linha is not None:
            frequencia_bruta = buscar_frequencia_linha_jogador(cur, jogador_id, tipo, linha, suavizar=suavizar)
            if frequencia_bruta is not None:
                frequencia = frequencia_bruta if direcao_normalizada == "mais" else round(100 - frequencia_bruta, 2)

        elif tipo == "impedimento" and jogador_id and direcao_normalizada in ("sim", "não", "nao"):
            frequencia_bruta = buscar_frequencia_simples_jogador(cur, jogador_id, "impedimento", suavizar=suavizar)
            if frequencia_bruta is not None:
                # NOVO (Grupo A): mesma ideia do cartão de jogador, sem o
                # árbitro (não faz sentido árbitro afetar impedimento).
                fator_estilo_ofensivo = buscar_fator_estilo(cur, nosso_time_id, "impedimento", "ofensivo")
                fator_estilo_defensivo = buscar_fator_estilo(cur, adversario_id, "impedimento", "defensivo")

                fator_grupo_a_aplicado, descricoes_grupo_a = combinar_fatores([
                    (fator_estilo_ofensivo, "estilo ofensivo"),
                    (fator_estilo_defensivo, "estilo defensivo do adversário"),
                ])
                if fator_grupo_a_aplicado is not None:
                    frequencia_bruta = min(round(frequencia_bruta * fator_grupo_a_aplicado, 2), 100.0)

                frequencia = frequencia_bruta if direcao_normalizada == "sim" else round(100 - frequencia_bruta, 2)

        elif tipo == "escanteio_time" and not jogador_id \
                and direcao_normalizada in ("mais", "menos") and linha is not None:
            # NOVO (multi-time): descobrimos que o MESMO time aparece com
            # nomes DIFERENTES em partes diferentes da resposta da OddsPapi
            # (ex: Athletico Paranaense é "CA Paranaense PR" na lista de
            # jogos, mas "Atletico Paranaense" dentro do texto do mercado
            # de escanteio) - o mesmo tipo de divergência de nome que já
            # vimos entre times/jogadores em outras partes do projeto.
            # Comparar com um nome só (nem o nosso, nem o do adversário) não
            # é confiável sozinho - testa vários candidatos de cada lado:
            # o nome salvo em `times` pra cada time (via mandante_id/
            # visitante_id) e o texto de `adversario` já resolvido antes.
            mercado_lower = mercado.lower()
            adversario_id_calc = None
            if mandante_id is not None and visitante_id is not None:
                adversario_id_calc = visitante_id if mandante_id == nosso_time_id else mandante_id

            candidatos_nosso_time = variantes_times.get(nosso_time_id, set())
            candidatos_adversario = variantes_times.get(adversario_id_calc, set()) | {(adversario or "").lower()}

            bate_nosso_time = any(c and c in mercado_lower for c in candidatos_nosso_time)
            bate_adversario = any(c and c in mercado_lower for c in candidatos_adversario)

            # só aplica quando bate com A GENTE e não bate com o adversário -
            # se dermos match nos dois (nomes parecidos) ou nenhum (nome
            # totalmente diferente dos dois, formato desconhecido ainda),
            # não arrisca aplicar errado - fica sem recomendação por
            # segurança, em vez de aplicar a frequência do time errado.
            if bate_nosso_time and not bate_adversario:
                # NOVO (25/08/2026): passa o mando REAL do nosso time nesse
                # jogo, pra escolher o recorte certo de
                # `padroes_time_escanteio`.
                #
                # Aqui a leitura é direta e não precisa de tradução: este
                # bloco só gera recomendação quando
                # `bate_nosso_time and not bate_adversario`, ou seja, o time
                # nomeado no mercado É SEMPRE o nosso_time da linha - nunca
                # o adversário. Logo `mandante` (vindo de jogos.mandante,
                # a mesma variável que alimenta `mandante_filtro_atual`) é
                # o mando dele, sem inversão possível.
                #
                # Vale registrar por quê: foi exatamente uma tradução de
                # mando entre tabelas com semânticas diferentes que gerou o
                # bug do cartao_time, corrigido nesta mesma data.
                frequencia_bruta = buscar_frequencia_escanteio_time(
                    cur, linha, nosso_time_id, suavizar=suavizar, mandante=mandante
                )

                # NOVO (Grupo A): Zona da Tabela + Padrão por Rodada, os
                # dois calculados em cima da própria média geral do time
                # nesse mercado (ver buscar_media_geral_time).
                if frequencia_bruta is not None:
                    zona_atual, condicao_momento = buscar_contexto_zona_time(
                        cur, nosso_time_id, nosso_time_api_id, temporada, rodada_numero
                    )
                    fator_zona = buscar_fator_zona(cur, nosso_time_id, zona_atual, condicao_momento, "escanteio")
                    fator_rodada = buscar_fator_rodada(cur, nosso_time_id, rodada_numero, "escanteio")

                    fator_grupo_a_aplicado, descricoes_grupo_a = combinar_fatores([
                        (fator_zona, "zona da tabela"),
                        (fator_rodada, "padrão por rodada"),
                    ])
                    if fator_grupo_a_aplicado is not None:
                        # NOVO (28/08/2026): espaço de chances - ver
                        # aplicar_fator. Ligado só nos dois mercados de
                        # escanteio por enquanto; os outros seguem na
                        # multiplicação direta até darem o mesmo problema.
                        frequencia_bruta = aplicar_fator(
                            frequencia_bruta, fator_grupo_a_aplicado, espaco_chances=True
                        )

                if frequencia_bruta is not None:
                    frequencia = frequencia_bruta if direcao_normalizada == "mais" else round(100 - frequencia_bruta, 2)

        elif tipo == "escanteio_total" and not jogador_id \
                and direcao_normalizada in ("mais", "menos") and linha is not None:
            # NOVO (confronto direto): tenta primeiro a frequência específica
            # contra esse adversário (ex: "escanteios totais contra o
            # Palmeiras, jogando em casa"); só cai pro padrão geral do time
            # se não houver confronto direto com amostra suficiente ainda.
            # NOVO (28/08/2026): o padrão geral é buscado ANTES do confronto,
            # não mais no `else`. Ele deixou de ser só o plano B e passou a
            # ser o PRIOR pro qual o confronto direto é encolhido - ver
            # PESO_PRIOR_CONFRONTO_DIRETO e _encolher_para_prior.
            #
            # Antes: confronto de 7 jogos SUBSTITUÍA os 138 jogos do time.
            # Agora: os dois são combinados ponderando pela amostra, e os 7
            # jogos do confronto entram com o peso de 7 jogos - não com o
            # peso de uma certeza.
            frequencia_geral = buscar_frequencia_escanteio_total(
                cur, linha, nosso_time_id, suavizar=suavizar
            )

            confronto = buscar_confronto_detalhado(
                cur, nosso_time_id, adversario_id, mandante_filtro_atual, "escanteio_total",
                linha=linha, frequencia_geral=frequencia_geral
            )
            if confronto is not None:
                frequencia_bruta, jogos_confronto_direto = confronto
                veio_de_confronto_direto = True
            else:
                frequencia_bruta = frequencia_geral

            # NOVO (Grupo A - Correlação entre Estatísticas): primeiro fator
            # do Grupo A a atuar num mercado de JOGO INTEIRO. Par usado:
            # chutes -> escanteios (jogo com muito chute tende a ter mais
            # escanteio - o time que ataca mais gera as duas coisas juntas).
            # Aplicado DEPOIS da frequência ser resolvida, exatamente como
            # em escanteio_time - inclusive quando ela veio de confronto
            # direto (o ajuste é sobre o contexto do jogo, não sobre a fonte
            # de onde a frequência saiu).
            if frequencia_bruta is not None:
                fator_correlacao = buscar_fator_correlacao(
                    cur, "escanteio_total", mandante_id, visitante_id
                )
                fator_grupo_a_aplicado, descricoes_grupo_a = combinar_fatores([
                    (fator_correlacao, "correlação chutes/escanteios"),
                ])
                if fator_grupo_a_aplicado is not None:
                    # NOVO (28/08/2026): espaço de chances - ver
                    # aplicar_fator. É ESTE ponto que produzia o 100,0%
                    # cravado do Atlético-PR x Fluminense (confronto
                    # encolhido ~93% x 1.09 = 101,4% -> cortado em 100).
                    frequencia_bruta = aplicar_fator(
                        frequencia_bruta, fator_grupo_a_aplicado, espaco_chances=True
                    )

            if frequencia_bruta is not None:
                frequencia = frequencia_bruta if direcao_normalizada == "mais" else round(100 - frequencia_bruta, 2)

        elif tipo == "cartao_total" and not jogador_id \
                and direcao_normalizada in ("mais", "menos") and linha is not None:
            # NOVO (confronto direto): mesma lógica de prioridade do escanteio
            # total acima.
            frequencia_bruta = buscar_frequencia_confronto(
                cur, nosso_time_id, adversario_id, mandante_filtro_atual, "cartao_total", linha=linha,
                suavizar=suavizar
            )
            if frequencia_bruta is not None:
                veio_de_confronto_direto = True
            else:
                frequencia_bruta = buscar_frequencia_cartao_total(cur, linha, nosso_time_id, suavizar=suavizar)

            # DESATIVADO (11/08/2026): o ajuste de suspensão (belisca a
            # frequência de cartão total pra baixo quando tem muita gente
            # na régua da suspensão) foi testado numa rodada real e não
            # se comportou bem - o dono do projeto pediu pra tirar ele da
            # FÓRMULA de recomendação. A função `calcular_fator_suspensao`
            # continua existindo no arquivo (não foi apagada), só não é
            # mais chamada aqui - fácil de reativar se um dia quiserem
            # revisitar com outro desenho. O aviso visual de "jogadores na
            # régua da suspensão" (card em /clube/<id>, em app.py) é
            # informativo/separado e continua funcionando normalmente,
            # sem relação com essa fórmula.

            # NOVO (Grupo A - Correlação entre Estatísticas): par
            # faltas -> cartões (jogo faltoso tende a ter mais cartão).
            # Mesma mecânica do escanteio_total logo acima. Note que isso
            # NÃO é o ajuste de suspensão desativado no comentário acima -
            # são coisas totalmente diferentes: aquele olhava quem estava na
            # régua da suspensão, esse olha o perfil de faltas do confronto.
            if frequencia_bruta is not None:
                fator_correlacao = buscar_fator_correlacao(
                    cur, "cartao_total", mandante_id, visitante_id
                )
                # NOVO (21/08/2026): árbitro entra também no cartão TOTAL.
                # Antes ele só existia no cartão de JOGADOR.
                #
                # Não é contagem dupla com a correlação, apesar da
                # suspeita inicial (árbitro rigoroso apita mais falta ->
                # mais cartão). Olhando como cada fator é CONSTRUÍDO:
                # a frequência-base é o histórico do time em TODOS os
                # jogos dele, então já contém a média de todos os árbitros
                # que ele pegou - corrigir esse valor pelo desvio DESTE
                # árbitro é exatamente a correção certa. E a média do
                # árbitro é calculada sobre TODOS os times que ele apitou,
                # então o efeito de time já está diluído nela. Cada fator
                # tem no input a média do que o outro mede: são quase
                # ortogonais.
                fator_arbitro_cartao = calcular_fator_arbitro(cur, arbitro, media_geral_cartoes, "cartao")
                fator_grupo_a_aplicado, descricoes_grupo_a = combinar_fatores([
                    (fator_correlacao, "correlação faltas/cartões"),
                    (fator_arbitro_cartao, "árbitro"),
                ])
                if fator_grupo_a_aplicado is not None:
                    frequencia_bruta = min(round(frequencia_bruta * fator_grupo_a_aplicado, 2), 100.0)

            if frequencia_bruta is not None:
                frequencia = frequencia_bruta if direcao_normalizada == "mais" else round(100 - frequencia_bruta, 2)

        elif tipo == "gols_time" and not jogador_id \
                and direcao_normalizada in ("mais", "menos") and linha is not None:
            # NOVO (Mais/Menos gols do time): mesmo cuidado de identificação
            # de time por variantes de nome que já existe pra escanteio_time
            # (ver comentário grande logo acima) - reaproveita a mesma
            # lógica e o mesmo `variantes_times` já calculado uma vez só no
            # início da função.
            mercado_lower = mercado.lower()
            adversario_id_calc = None
            if mandante_id is not None and visitante_id is not None:
                adversario_id_calc = visitante_id if mandante_id == nosso_time_id else mandante_id

            candidatos_nosso_time = variantes_times.get(nosso_time_id, set())
            candidatos_adversario = variantes_times.get(adversario_id_calc, set()) | {(adversario or "").lower()}

            bate_nosso_time = any(c and c in mercado_lower for c in candidatos_nosso_time)
            bate_adversario = any(c and c in mercado_lower for c in candidatos_adversario)

            if bate_nosso_time and not bate_adversario:
                frequencia_bruta = buscar_frequencia_gols_time(cur, linha, nosso_time_id, suavizar=suavizar)
                if frequencia_bruta is not None:
                    frequencia = frequencia_bruta if direcao_normalizada == "mais" else round(100 - frequencia_bruta, 2)

        elif tipo == "gols_total" and not jogador_id \
                and direcao_normalizada in ("mais", "menos") and linha is not None:
            frequencia_bruta = buscar_frequencia_gols_total(cur, linha, nosso_time_id, suavizar=suavizar)
            if frequencia_bruta is not None:
                frequencia = frequencia_bruta if direcao_normalizada == "mais" else round(100 - frequencia_bruta, 2)

        elif tipo == "equipe_marca" and not jogador_id and direcao_normalizada in ("sim", "não", "nao"):
            # NOVO (Equipe Marca): mesma identificação de time por variantes
            # de nome usada em gols_time/escanteio_time.
            mercado_lower = mercado.lower()
            adversario_id_calc = None
            if mandante_id is not None and visitante_id is not None:
                adversario_id_calc = visitante_id if mandante_id == nosso_time_id else mandante_id

            candidatos_nosso_time = variantes_times.get(nosso_time_id, set())
            candidatos_adversario = variantes_times.get(adversario_id_calc, set()) | {(adversario or "").lower()}

            bate_nosso_time = any(c and c in mercado_lower for c in candidatos_nosso_time)
            bate_adversario = any(c and c in mercado_lower for c in candidatos_adversario)

            if bate_nosso_time and not bate_adversario:
                frequencia_bruta = buscar_frequencia_equipe_marca(cur, nosso_time_id, suavizar=suavizar)
                if frequencia_bruta is not None:
                    frequencia = frequencia_bruta if direcao_normalizada == "sim" else round(100 - frequencia_bruta, 2)

        elif tipo == "dupla_chance_1t" and not jogador_id and direcao_normalizada in ("1x", "12", "2x"):
            # Dupla Chance é mercado do JOGO (1X/12/2X são direção relativa
            # a mandante/visitante, igual resultado_final) - não precisa de
            # identificação de time por nome, cada direção já tem a própria
            # frequência guardada (não é um par Mais/Menos complementar).
            #
            # ATUALIZADO 26/08/2026: passa os DOIS times em vez do
            # `nosso_time_id` daquela perspectiva. A frequência agora é a
            # média ponderada de duas leituras - o mandante como mandante e
            # o visitante como visitante. Ver o comentário longo em
            # `buscar_frequencia_dupla_chance_tempo`.
            #
            # Efeito colateral desejado: como as duas perspectivas do mesmo
            # jogo passam a ler exatamente as mesmas duas linhas, o mesmo
            # evento deixa de receber duas probabilidades diferentes, e o
            # filtro de VE para de escolher sempre a maior das duas.
            frequencia = buscar_frequencia_dupla_chance_tempo(
                cur, "1T", direcao_normalizada.upper(), mandante_id, visitante_id,
                suavizar=suavizar
            )

        elif tipo == "dupla_chance_2t" and not jogador_id and direcao_normalizada in ("1x", "12", "2x"):
            frequencia = buscar_frequencia_dupla_chance_tempo(
                cur, "2T", direcao_normalizada.upper(), mandante_id, visitante_id,
                suavizar=suavizar
            )

        elif tipo == "ambas_marcam_1t" and not jogador_id and direcao_normalizada in ("sim", "não", "nao"):
            # NOVO (02/09/2026): estimativa DECOMPOSTA - produto dos
            # componentes dos dois times reais do jogo, em vez da
            # frequência conjunta histórica do `nosso_time`. Ver
            # `buscar_frequencia_ambas_marcam_tempo_decomposta`.
            #
            # ESCADA: se faltar componente de qualquer lado, cai pro
            # método antigo - nunca fica sem estimativa.
            frequencia_bruta = None
            if mandante_id is not None and visitante_id is not None:
                frequencia_bruta = buscar_frequencia_ambas_marcam_tempo_decomposta(
                    cur, mandante_id, visitante_id, "1T", suavizar=suavizar
                )
            if frequencia_bruta is None:
                frequencia_bruta = buscar_frequencia_ambas_marcam_tempo(cur, nosso_time_id, "1T", mandante_filtro_atual, suavizar=suavizar)
            if frequencia_bruta is not None:
                frequencia = frequencia_bruta if direcao_normalizada == "sim" else round(100 - frequencia_bruta, 2)

        elif tipo == "ambas_marcam_2t" and not jogador_id and direcao_normalizada in ("sim", "não", "nao"):
            frequencia_bruta = None
            if mandante_id is not None and visitante_id is not None:
                frequencia_bruta = buscar_frequencia_ambas_marcam_tempo_decomposta(
                    cur, mandante_id, visitante_id, "2T", suavizar=suavizar
                )
            if frequencia_bruta is None:
                frequencia_bruta = buscar_frequencia_ambas_marcam_tempo(cur, nosso_time_id, "2T", mandante_filtro_atual, suavizar=suavizar)
            if frequencia_bruta is not None:
                frequencia = frequencia_bruta if direcao_normalizada == "sim" else round(100 - frequencia_bruta, 2)

        elif tipo == "marca_ambos_tempos" and not jogador_id and direcao_normalizada in ("sim", "não", "nao"):
            # NOVO (Onda 2): esse já é por TIME de verdade (o texto tem
            # "Equipe 1"/"Equipe 2" substituído pelo nome real) - mesma
            # identificação por variantes de nome de gols_time/equipe_marca.
            mercado_lower = mercado.lower()
            adversario_id_calc = None
            if mandante_id is not None and visitante_id is not None:
                adversario_id_calc = visitante_id if mandante_id == nosso_time_id else mandante_id

            candidatos_nosso_time = variantes_times.get(nosso_time_id, set())
            candidatos_adversario = variantes_times.get(adversario_id_calc, set()) | {(adversario or "").lower()}

            bate_nosso_time = any(c and c in mercado_lower for c in candidatos_nosso_time)
            bate_adversario = any(c and c in mercado_lower for c in candidatos_adversario)

            if bate_nosso_time and not bate_adversario:
                # NOVO (02/09/2026 - Fase 2): estimativa DECOMPOSTA, com
                # separação por mando que o método antigo não tinha. Ver
                # `buscar_frequencia_marca_ambos_tempos_decomposta`.
                #
                # ESCADA: sem componente, cai pro método antigo (que
                # continua sendo gravado por motor_padroes.py).
                frequencia_bruta = buscar_frequencia_marca_ambos_tempos_decomposta(
                    cur, nosso_time_id, mandante_filtro_atual, suavizar=suavizar
                )
                if frequencia_bruta is None:
                    frequencia_bruta = buscar_frequencia_marca_ambos_tempos(cur, nosso_time_id, suavizar=suavizar)
                if frequencia_bruta is not None:
                    frequencia = frequencia_bruta if direcao_normalizada == "sim" else round(100 - frequencia_bruta, 2)

        elif tipo == "handicap_asiatico" and not jogador_id \
                and direcao_normalizada in ("1", "2") and linha is not None:
            # NOVO (Handicap Asiático - só meia linha): mesma identificação
            # de time por variantes de nome de gols_time/marca_ambos_tempos
            # - só processa se a recomendação for pro NOSSO time (não pro
            # adversário). `linha` já é o handicap bruto (convenção
            # OddsPapi, relativo ao mandante) - mesmo valor usado na hora
            # de calcular e salvar em padroes_time_handicap, sem precisar
            # de nenhuma conversão aqui.
            mercado_lower = mercado.lower()
            adversario_id_calc = None
            if mandante_id is not None and visitante_id is not None:
                adversario_id_calc = visitante_id if mandante_id == nosso_time_id else mandante_id

            candidatos_nosso_time = variantes_times.get(nosso_time_id, set())
            candidatos_adversario = variantes_times.get(adversario_id_calc, set()) | {(adversario or "").lower()}

            bate_nosso_time = any(c and c in mercado_lower for c in candidatos_nosso_time)
            bate_adversario = any(c and c in mercado_lower for c in candidatos_adversario)

            if bate_nosso_time and not bate_adversario:
                frequencia = buscar_frequencia_handicap(cur, nosso_time_id, mandante_filtro_atual, linha, suavizar=suavizar)

        elif tipo == "resultado_final" and not jogador_id:
            resultado_cor = resultado_do_ponto_de_vista_corinthians(direcao, mandante)
            # NOVO (01/09/2026): empate desativado - ver
            # GERAR_RECOMENDACAO_DE_EMPATE pro diagnóstico completo (0
            # acertos em 22, com 3 hipóteses de causa já testadas e
            # descartadas). A trava fica ANTES de qualquer consulta: não
            # adianta calcular uma probabilidade que vai ser descartada.
            if resultado_cor == "empate" and not GERAR_RECOMENDACAO_DE_EMPATE:
                continue
            if resultado_cor:
                # NOVO (28/08/2026): duas mudanças, na ordem em que
                # importam - ver buscar_frequencia_resultado_combinada e
                # PESO_PRIOR_CONFRONTO_DIRETO.
                #
                # 1. O padrão geral agora COMBINA as duas perspectivas do
                #    jogo (ponderado por amostra, com a inversão
                #    vitória/derrota). Antes lia só o lado do nosso time e
                #    o mesmo empate saía com dois números diferentes.
                # 2. Ele é buscado ANTES do confronto direto, porque deixou
                #    de ser só o plano B: virou o PRIOR pro qual o confronto
                #    é encolhido. Antes um confronto de 5 jogos SUBSTITUÍA
                #    o histórico inteiro - foi assim que saiu "63,88% de
                #    empate", número que não existe no futebol.
                frequencia_geral = buscar_frequencia_resultado_combinada(
                    cur, resultado_cor, nosso_time_id, mandante_id, visitante_id,
                    suavizar=suavizar
                )

                confronto = buscar_confronto_detalhado(
                    cur, nosso_time_id, adversario_id, mandante_filtro_atual, "resultado_final",
                    resultado=resultado_cor, frequencia_geral=frequencia_geral
                )
                if confronto is not None:
                    frequencia, jogos_confronto_direto = confronto
                    veio_de_confronto_direto = True
                else:
                    frequencia = frequencia_geral

                # NOVO (forma recente, já existia) + Grupo A (Zona da
                # Tabela + Padrão por Rodada). IMPORTANTE: Zona/Rodada só
                # têm o mercado "resultado" calculado como TAXA DE VITÓRIA
                # - não existe separação por empate/derrota nesses dois
                # recursos - então só entram na conta quando o mercado
                # sendo avaliado é justamente "Vitória", nunca em Empate/
                # Derrota (aplicar um fator de "esse time vence mais aqui"
                # na aposta de Empate/Derrota não faria sentido com o dado
                # que temos).
                if frequencia is not None:
                    # NOVO (28/08/2026): forma recente NÃO se aplica ao
                    # EMPATE. Vitória e derrota são assimétricos - "time em
                    # boa fase" vence mais E o adversário perde mais, então
                    # o fator tem direção clara e as duas perspectivas se
                    # complementam. O empate é o ÚNICO resultado simétrico:
                    # é o mesmo evento pros dois lados, e não existe "time
                    # em boa fase empata mais" - a afirmação não tem sinal.
                    #
                    # O que acontecia sem essa trava (medido em 28/08/2026,
                    # antes da correção): o mesmo empate recebia 0.85x de
                    # uma perspectiva e 1.15x da outra, e saía com DOIS
                    # números pro MESMO evento -
                    #   Cruzeiro x Vasco:      55,55% x 0.85 = 47,22%
                    #                          55,55% x 1.15 = 63,88%
                    #   Atlético-MG x Vitória: 55,55%/... = 49,29% e 36,43%
                    # A camada de baixo (padrão combinado + confronto) já
                    # dava o mesmo valor nos dois lados; era o fator que
                    # reintroduzia a divergência. E como o filtro de VE
                    # sempre escolhe a maior das duas, isso era seleção
                    # adversa pura: o fator não estava medindo nada, só
                    # escolhendo qual perspectiva virava recomendação.
                    # NOVO (02/09/2026 - simetria do Grupo A): o fator de
                    # forma agora COMBINA as duas perspectivas do jogo, em
                    # vez de olhar só o `nosso_time_id` daquela linha - ver
                    # `calcular_fator_forma_recente_combinado` pro
                    # diagnóstico completo. Antes, "Vitória do Cruzeiro" e
                    # "Derrota do Vasco" (o MESMO evento real) recebiam
                    # 1.03x e 1.15x, e o filtro de VE sempre ficava com a
                    # estimativa mais otimista das duas.
                    fator_forma = None
                    if resultado_cor != "empate":
                        fator_forma = calcular_fator_forma_recente_combinado(
                            cur, resultado_cor, nosso_time_id, adversario_id
                        )

                    # NOVO (02/09/2026): Zona da Tabela e Padrão por Rodada
                    # SAÍRAM do resultado_final. Eles só existem calculados
                    # como TAXA DE VITÓRIA (`padroes_zona_time.valor` e
                    # `padroes_rodada_bruto.valor`, sem separação por
                    # resultado), então só podiam ser aplicados na
                    # perspectiva "vitória" - e era justamente isso que
                    # deixava um lado do mesmo evento com 3 fatores e o
                    # outro com 1.
                    #
                    # POR QUE NÃO ESPELHAR do adversário (opção avaliada e
                    # DESCARTADA): "derrota nossa" não é o mesmo que
                    # "vitória do adversário" - entre os dois existe o
                    # empate, ~27% no Brasileirão. Usar a taxa de vitória
                    # do adversário como proxy pra nossa derrota embutiria
                    # esse erro de 27 pontos na fórmula. Não é ajuste fino,
                    # é premissa errada.
                    #
                    # Como o dado não sustenta aplicar nos dois lados, e
                    # aplicar num lado só é a origem da assimetria, a saída
                    # coerente é não aplicar em nenhum. Isso REMOVE um
                    # ajuste que nunca tinha sido medido isolado - é
                    # deliberado, vira informação na próxima rodada.
                    #
                    # ⚠️ NÃO afeta `escanteio_time`, que usa zona/rodada com
                    # tipo_padrao="escanteio" e segue intocado.
                    fator_grupo_a_aplicado, descricoes_grupo_a = combinar_fatores([
                        (fator_forma, "forma recente"),
                    ])
                    if fator_grupo_a_aplicado is not None:
                        frequencia = min(round(frequencia * fator_grupo_a_aplicado, 2), 100.0)

        if frequencia is None:
            continue  # não temos padrão calculado pra cruzar com essa odd ainda

        # NOVO (03/09/2026 - pendência 28): encolhe a probabilidade em
        # direção à implícita na odd da casa quando as duas divergem
        # muito. Aplicado AQUI, depois de todos os fatores do Grupo A e
        # antes do VE, porque é a última palavra sobre a probabilidade -
        # corrige o número que o modelo produziu, seja lá como produziu.
        #
        # A frequência corrigida vira a probabilidade usada no VE E na
        # tela. Manter duas (uma exibida, outra interna) faria os números
        # da tela não fecharem entre si: o usuário veria "62%, odd 3.00"
        # com um VE que não bate com 62% * 3.00 - 1.
        frequencia, houve_encolhimento_mercado = encolher_para_o_mercado(
            frequencia, valor_odd, tipo
        )

        probabilidade = frequencia / 100
        valor_esperado = round((probabilidade * float(valor_odd)) - 1, 3)

        descricao_final = mercado
        if tipo == "resultado_final":
            nome_nosso_time = nomes_times.get(nosso_time_id, "nosso time")
            nomes = {
                "vitoria": f"Vitória do {nome_nosso_time}",
                "empate": "Empate",
                "derrota": f"Derrota do {nome_nosso_time}",
            }
            descricao_final = f"Resultado Final - {nomes[resultado_cor]}"

        elif tipo == "handicap_asiatico" and linha is not None:
            # NOVO (20/08/2026): usa o nome real da aposta na Superbet e
            # corrige o sinal pro visitante - ver montar_descricao_handicap.
            # Só entra aqui quando a recomendação é pro NOSSO time (o bloco
            # de cálculo lá em cima já garante isso), então `nosso_time_id`
            # é sempre o dono da aposta.
            descricao_final = montar_descricao_handicap(
                nomes_times.get(nosso_time_id, "nosso time"),
                mandante,
                linha,
            )

        if fator_arbitro_aplicado is not None:
            descricao_final += f" (ajustado pelo árbitro, fator {fator_arbitro_aplicado:.2f}x)"

        if fator_suspensao_aplicado is not None:
            descricao_final += f" (ajustado por jogadores na régua da suspensão, fator {fator_suspensao_aplicado:.2f}x)"

        # NOVO (Grupo A): descrição unificada dos fatores novos (+ árbitro/
        # forma recente, quando entram na mesma combinação) - substitui as
        # tags individuais antigas nesses 4 mercados específicos (cartão de
        # jogador, impedimento, escanteio de time, resultado final), já que
        # agora eles podem se combinar entre si com um teto conjunto.
        if descricoes_grupo_a and fator_grupo_a_aplicado is not None:
            lista_fatores = " + ".join(descricoes_grupo_a)
            descricao_final += f" (ajustado por {lista_fatores}, fator combinado {fator_grupo_a_aplicado:.2f}x)"

        if veio_de_confronto_direto:
            # NOVO (28/08/2026): mostra a amostra quando ela é conhecida.
            # Sem isso não dá pra auditar depois se uma recomendação saiu de
            # um confronto de 5 jogos ou de 15 - e foi exatamente esse tipo
            # de cegueira que fez o "97% do Cruzeiro x Vasco" passar
            # despercebido até alguém estranhar o número na tela.
            if jogos_confronto_direto:
                descricao_final += f" (confronto direto: {jogos_confronto_direto} jogos)"
            else:
                descricao_final += " (confronto direto)"

        # NOVO (03/09/2026): marca na descrição quando a probabilidade foi
        # puxada em direção à odd da casa. Sem essa marca não dá pra
        # auditar depois se uma recomendação saiu encolhida ou não - a
        # mesma cegueira que a seção 41 já custou caro ("(confronto
        # direto: N jogos)" existe pelo mesmo motivo). E a ausência do
        # sufixo passa a ser diagnóstico: mercado fora do escopo, ou
        # modelo que já concordava com a casa.
        if houve_encolhimento_mercado:
            descricao_final += " (ajustado pela odd da casa)"

        # NOVO (03/09/2026): o filtro passou a usar o piso CORRIGIDO nos
        # mercados do escopo. Nos demais (resultado_final, correlação, e
        # qualquer mercado não classificado), segue valendo o
        # VALOR_ESPERADO_MINIMO de sempre - eles são o grupo de controle
        # e não podem ter DOIS parâmetros mudando ao mesmo tempo, senão a
        # próxima medição não distingue o efeito de um do outro.
        if tipo in MERCADOS_CORRECAO_DIVERGENCIA_MERCADO:
            piso_ve = VALOR_ESPERADO_MINIMO_CORRIGIDO
        else:
            piso_ve = VALOR_ESPERADO_MINIMO

        if valor_esperado > piso_ve:
            recomendacoes.append({
                "jogo_id": jogo_id,
                "jogador_id": jogador_id,
                "tipo_padrao": tipo,
                "descricao": descricao_final,
                "casa_aposta": casa,
                "odd_oferecida": valor_odd,
                "probabilidade_historica": round(probabilidade * 100, 2),
                "valor_esperado": valor_esperado,
                "linha": linha,
                "direcao": direcao_normalizada,
                "adversario": adversario,
                "data_jogo": data_jogo,
            })

    if jogadores_indisponiveis_pulados:
        print(f"  ({jogadores_indisponiveis_pulados} odd(s) de jogador ignorada(s) por "
              f"indisponibilidade - fora da escalação recente/confirmada.)")

    return recomendacoes


def salvar_recomendacoes(cur, recomendacoes):
    # limpa só as recomendações de jogos FUTUROS antes de gerar as novas
    # (as de jogos já ocorridos ficam intactas até o script de arquivamento
    # processá-las - senão perderíamos o histórico antes de avaliar acerto/erro)
    cur.execute(
        "DELETE FROM recomendacoes WHERE jogo_id IN ("
        "  SELECT id FROM jogos WHERE "
        "  (datahora_jogo IS NOT NULL AND datahora_jogo >= NOW()) "
        "  OR (datahora_jogo IS NULL AND data_jogo >= CURRENT_DATE)"
        ")"
    )

    for r in recomendacoes:
        cur.execute(
            """INSERT INTO recomendacoes
               (jogo_id, jogador_id, tipo_padrao, descricao, casa_aposta,
                odd_oferecida, probabilidade_historica, valor_esperado, linha, direcao)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                r["jogo_id"], r["jogador_id"], r["tipo_padrao"], r["descricao"],
                r["casa_aposta"], r["odd_oferecida"], r["probabilidade_historica"],
                r["valor_esperado"], r.get("linha"), r.get("direcao"),
            ),
        )
        print(f"  [{r['data_jogo']} vs {r['adversario']}] {r['descricao']} "
              f"({r['casa_aposta']}) - odd {r['odd_oferecida']} | "
              f"prob. histórica {r['probabilidade_historica']}% | VE {r['valor_esperado']}")


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        print("Cruzando padrões com odds de jogos futuros...")
        recomendacoes = calcular_recomendacoes(cur)

        if not recomendacoes:
            print("Nenhuma recomendação de valor encontrada no momento "
                  "(sem jogo próximo, sem odds coletadas, ou sem padrão correspondente).")
            salvar_recomendacoes(cur, [])  # ainda assim limpa recomendações antigas
        else:
            recomendacoes.sort(key=lambda r: r["valor_esperado"], reverse=True)
            salvar_recomendacoes(cur, recomendacoes)

        conn.commit()
        print(f"\nConcluído! {len(recomendacoes)} recomendação(ões) salva(s).")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
