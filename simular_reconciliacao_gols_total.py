"""
simular_reconciliacao_gols_total.py — Fase 0.4 do item 5.9

⚠️ SCRIPT SÓ-LEITURA. Não tem fase `aplicar` porque não há o que aplicar.
Nenhuma linha de produção é tocada, nada é gravado, e o script termina em
`conn.rollback()` incondicional.

O QUE ELE RESPONDE
------------------
Uma pergunta só, e ela é o bloqueio do item 5.9:

    Se o mercado de JOGO INTEIRO passasse a reconciliar as duas
    perspectivas, QUANTAS recomendações deixariam de existir?

O critério de decisão foi travado por escrito ANTES deste script existir
(`arquitetura_recomendacao_contraditoria.md`, §8):

    se a reconciliação eliminar MAIS DE UM TERÇO das recomendações de
    jogo inteiro, a média ponderada é agressiva demais e o desenho volta
    para a prancheta.

POR QUE SÓ `gols_total`
-----------------------
Dos três mercados de jogo inteiro onde a contradição aparece, `gols_total`
é o único que NÃO recebe fator nenhum do Grupo A. A cadeia inteira é:

    buscar_frequencia_gols_total -> complemento -> encolher_para_o_mercado

`escanteio_total` leva correlação + confronto direto, e `cartao_total` leva
correlação + árbitro + confronto — os dois calculados POR PERSPECTIVA.
Reproduzir isso fora do motor é exatamente o erro de 21/09 que custou
quatro hipóteses. Eles entram numa segunda etapa, com o mesmo método.

`gols_total` também é onde o defeito mais apareceu: 8 dos 13 grupos
contraditórios da rodada 28.

O MÉTODO
--------
Nada de lógica de produção é reimplementado aqui. O script IMPORTA
`buscar_frequencia_gols_total`, `encolher_para_o_mercado` e
`time_tem_historico_curto` do `motor_recomendacoes` e chama as funções de
verdade — regra 33 do projeto ("instrumentar é importar, não reescrever").

A reconciliação proposta segue o padrão JÁ VALIDADO três vezes no próprio
motor (`buscar_frequencia_dupla_chance_tempo` 26/08,
`buscar_frequencia_resultado_combinada` 28/08,
`buscar_frequencia_ambas_marcam_tempo_decomposta` 02/09): ler os dois
lados e tirar a média ponderada por `jogos_analisados`.

    P(mais) = (n_mand * f_mand + n_vis * f_vis) / (n_mand + n_vis)
    P(menos) = 100 - P(mais)

`gols_total` é o quarto caso do mesmo defeito e o único que ficou de fora.

O TESTE DE SOBREVIVÊNCIA
------------------------
Não precisa reproduzir o encolhimento. O próprio motor documenta que
`p_final` fica sempre ENTRE `p_modelo` e `p_mercado`, e que por isso "0
recomendações mudariam de lado do filtro VE>0". Como `p_mercado = 100/odd`
é exatamente o ponto onde VE = 0:

    sobrevive  <=>  p_reconciliado > 100 / odd_gravada

O encolhimento é calculado mesmo assim, só para o relatório — para mostrar
qual probabilidade apareceria na tela.

OS TRÊS CONTADORES QUE PODEM DERRUBAR ESTE SCRIPT
--------------------------------------------------
Todo diagnóstico carrega o contador que pode derrubá-lo (regra 34). Se
qualquer um destes vier ruim, o número principal NÃO vale:

1. DERIVA DO SNAPSHOT — `padroes_gols_total` é sobrescrita todo dia pelo
   `motor_padroes`. Este script lê o snapshot de HOJE, não o do dia em que
   cada recomendação foi gerada. O script recalcula a probabilidade ATUAL
   pela perspectiva original e compara com a `probabilidade_historica`
   gravada. Divergência alta = o mundo mudou e a simulação não vale.

2. COBERTURA — quantas linhas têm mandante/visitante resolvíveis e
   `jogos_analisados` nos dois lados. Cobertura baixa = amostra enviesada.

3. GRUPO DE CONTROLE (`gols_time`) — mercado de TIME. As duas perspectivas
   ali são APOSTAS DIFERENTES, então a reconciliação não se aplica e NADA
   pode mudar. Se mudar, o escopo vazou e o desenho está errado.

Uso:
    python simular_reconciliacao_gols_total.py verificar

Variáveis de ambiente:
    DATABASE_URL
"""

import os
import sys
from datetime import datetime

import psycopg2

# ⚠️ Import das funções REAIS de produção. Se qualquer uma faltar ou tiver
# mudado de assinatura, o script PARA aqui com mensagem clara em vez de
# adivinhar — a cópia do arquivo no projeto pode estar defasada em relação
# ao GitHub, e um instrumento divergente mente sem avisar.
try:
    from motor_recomendacoes import (
        buscar_frequencia_gols_total,
        buscar_frequencia_gols_time,
        encolher_para_o_mercado,
        time_tem_historico_curto,
        carregar_total_jogos_por_time,
    )
except ImportError as e:
    print("=" * 78)
    print("ERRO: não consegui importar do motor_recomendacoes.")
    print(f"Detalhe: {e}")
    print()
    print("Este script NÃO reimplementa a lógica de propósito. Rode-o na")
    print("mesma pasta do motor_recomendacoes.py do GitHub (não a cópia do")
    print("projeto, que pode estar velha).")
    print("=" * 78)
    raise

DATABASE_URL = os.environ["DATABASE_URL"]

# Travado na arquitetura ANTES deste script existir. Se a eliminação passar
# disso, o desenho volta pra prancheta.
LIMIAR_FALSEAMENTO_PCT = 33.3

# Acima disso, o snapshot de padroes_gols_total já mudou demais desde que as
# recomendações foram geradas, e a simulação perde o sentido.
LIMIAR_DERIVA_ACEITAVEL = 2.0   # pontos percentuais, média absoluta

# Cada tipo de mercado lê de uma TABELA DIFERENTE, e cada um tem sua própria
# função de produção. Sem este mapa, o grupo de controle leria a tabela do
# alvo e não controlaria nada — foi exatamente o bug pego na revisão por AST
# deste script (`buscar_frequencia_gols_time` importada e nunca chamada).
#
# A função é sempre a DE PRODUÇÃO, importada. O SQL ao lado lê só o campo
# `jogos_analisados` da MESMA tabela e com o MESMO filtro que ela usa — é
# leitura de campo, não reimplementação: a função de produção não devolve o
# tamanho da amostra, e a reconciliação precisa dele como peso.
FONTES = {
    "gols_total": (
        buscar_frequencia_gols_total,
        "SELECT jogos_analisados FROM padroes_gols_total "
        "WHERE linha = %s AND time_id = %s",
    ),
    "gols_time": (
        buscar_frequencia_gols_time,
        "SELECT jogos_analisados FROM padroes_time_linha "
        "WHERE tipo = 'gols' AND linha = %s AND time_id = %s",
    ),
}


def cabecalho(fase):
    print("=" * 78)
    print(f"simular_reconciliacao_gols_total.py | fase: {fase}")
    print(f"argv: {sys.argv}")
    print(f"relógio: {datetime.now().isoformat()}")
    print("SÓ LEITURA — termina em rollback() incondicional")
    print("=" * 78)
    sys.stdout.flush()


def buscar_amostra(cur, tipo_padrao, linha, time_id):
    """Lê `jogos_analisados` da tabela do tipo pedido (ver FONTES).

    Isto é leitura de CAMPO, não reimplementação de lógica: a função de
    produção consulta exatamente esta tabela com exatamente este filtro,
    mas devolve só a frequência, sem o tamanho da amostra. A reconciliação
    precisa do peso.

    ⚠️ É justamente esta lacuna que a Fase 2 do 5.9 vai ter que fechar no
    motor, seguindo o precedente de `buscar_confronto_detalhado`, que
    devolve (frequencia, jogos) pelo mesmo motivo."""
    _func, sql = FONTES[tipo_padrao]
    cur.execute(sql, (linha, time_id))
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    return int(row[0])


def carregar_historico(cur, tipo_padrao):
    """Histórico do tipo pedido, DEDUPLICADO e com o ano filtrado.

    - dedup por (jogo_id, tipo_padrao, jogador_id, linha, direcao), menor
      `arquivado_em` — a duplicação com look-ahead sempre melhora o gap
      artificialmente (convenção 5.12)
    - `data_jogo >= 2026-01-01` — `rodada_numero` não é único por
      temporada (convenção 5.11)
    """
    cur.execute(
        """
        SELECT DISTINCT ON (h.jogo_id, h.tipo_padrao, h.jogador_id, h.linha, h.direcao)
               h.jogo_id, h.linha, h.direcao, h.resultado,
               h.odd_oferecida, h.probabilidade_historica,
               j.fixture_id_api, j.nosso_time_id, j.mandante_id, j.visitante_id
        FROM historico_recomendacoes h
        JOIN jogos j ON j.id = h.jogo_id
        WHERE h.tipo_padrao = %s
          AND j.data_jogo >= DATE '2026-01-01'
          AND h.resultado IN ('acertou', 'errou')
        ORDER BY h.jogo_id, h.tipo_padrao, h.jogador_id, h.linha, h.direcao, h.arquivado_em
        """,
        (tipo_padrao,),
    )
    return cur.fetchall()


def probabilidade_atual(cur, linha, direcao, time_id, odd, tipo_padrao, total_jogos):
    """Reproduz a cadeia de produção PELA PERSPECTIVA ORIGINAL, chamando as
    funções reais. Serve para o contador de deriva: se isto não reproduz a
    `probabilidade_historica` gravada, o snapshot mudou."""
    buscar_frequencia, _sql = FONTES[tipo_padrao]
    suavizar = time_tem_historico_curto(total_jogos, time_id)
    bruta = buscar_frequencia(cur, linha, time_id, suavizar=suavizar)
    if bruta is None:
        return None
    freq = bruta if direcao == "mais" else round(100 - bruta, 2)
    final, _ajustou = encolher_para_o_mercado(freq, odd, tipo_padrao)
    return final


def probabilidade_reconciliada(cur, linha, direcao, mandante_id, visitante_id,
                               odd, tipo_padrao, total_jogos):
    """A proposta: média ponderada por amostra dos dois lados, complemento
    aplicado DEPOIS e uma vez só.

    Mesmo padrão de `buscar_frequencia_dupla_chance_tempo` (26/08) e
    `buscar_frequencia_resultado_combinada` (28/08). Cada lado é suavizado
    pelo histórico do PRÓPRIO time, não pelo da perspectiva.

    Devolve (p_final, p_mais_reconciliada, n_mand, n_vis) ou None quando
    falta qualquer um dos dois lados."""
    if mandante_id is None or visitante_id is None:
        return None

    buscar_frequencia, _sql = FONTES[tipo_padrao]
    leituras = []
    for time_id in (mandante_id, visitante_id):
        suavizar = time_tem_historico_curto(total_jogos, time_id)
        freq = buscar_frequencia(cur, linha, time_id, suavizar=suavizar)
        n = buscar_amostra(cur, tipo_padrao, linha, time_id)
        if freq is None or not n:
            continue
        leituras.append((freq, n))

    if len(leituras) < 2:
        return None

    peso_total = sum(n for _f, n in leituras)
    if peso_total <= 0:
        return None

    p_mais = round(sum(f * n for f, n in leituras) / peso_total, 2)
    p_dir = p_mais if direcao == "mais" else round(100 - p_mais, 2)
    p_final, _ajustou = encolher_para_o_mercado(p_dir, odd, tipo_padrao)
    return p_final, p_mais, leituras[0][1], leituras[1][1]


def simular(cur, tipo_padrao, total_jogos, rotulo):
    """Roda a simulação num tipo de mercado. Usado no alvo (`gols_total`) e
    no grupo de controle (`gols_time`)."""
    linhas = carregar_historico(cur, tipo_padrao)

    total = len(linhas)
    sem_dois_lados = 0
    sem_atual = 0
    sobrevivem = 0
    somem = 0
    derivas = []
    detalhes_somem = []
    por_direcao = {"mais": [0, 0], "menos": [0, 0]}   # [sobrevive, some]

    for (jogo_id, linha, direcao, resultado, odd, prob_gravada,
         fixture, nosso_time_id, mandante_id, visitante_id) in linhas:

        odd = float(odd)
        prob_gravada = float(prob_gravada)
        direcao = (direcao or "").strip().lower()
        if direcao not in ("mais", "menos"):
            continue

        # Contador 1 — deriva do snapshot.
        atual = probabilidade_atual(
            cur, linha, direcao, nosso_time_id, odd, tipo_padrao, total_jogos
        )
        if atual is None:
            sem_atual += 1
        else:
            derivas.append(abs(atual - prob_gravada))

        rec = probabilidade_reconciliada(
            cur, linha, direcao, mandante_id, visitante_id, odd, tipo_padrao, total_jogos
        )
        if rec is None:
            sem_dois_lados += 1
            continue

        p_final, p_mais, n_mand, n_vis = rec

        # VE > 0  <=>  p > 100/odd. O encolhimento nunca cruza esse ponto.
        implicita_casa = 100.0 / odd
        if p_final > implicita_casa:
            sobrevivem += 1
            por_direcao[direcao][0] += 1
        else:
            somem += 1
            por_direcao[direcao][1] += 1
            detalhes_somem.append(
                (fixture, linha, direcao, prob_gravada, p_final, implicita_casa, resultado)
            )

    avaliadas = sobrevivem + somem

    print()
    print("-" * 78)
    print(f"{rotulo}  (tipo_padrao = {tipo_padrao})")
    print("-" * 78)
    print(f"  linhas no histórico (dedup, 2026):        {total}")
    print(f"  COBERTURA — com os dois lados legíveis:   {avaliadas}"
          f"  ({100.0*avaliadas/total:.1f}%)" if total else "  (histórico vazio)")
    print(f"  sem os dois lados (fallback necessário):  {sem_dois_lados}")
    print(f"  sem probabilidade atual recalculável:     {sem_atual}")

    if derivas:
        deriva_media = sum(derivas) / len(derivas)
        deriva_max = max(derivas)
        print()
        print(f"  CONTADOR 1 — DERIVA DO SNAPSHOT")
        print(f"    diferença média |recalculado - gravado|: {deriva_media:.2f} pontos")
        print(f"    diferença máxima:                        {deriva_max:.2f} pontos")
        if deriva_media > LIMIAR_DERIVA_ACEITAVEL:
            print(f"    🔴 ACIMA DE {LIMIAR_DERIVA_ACEITAVEL} — padroes_gols_total mudou desde a geração.")
            print(f"       O número de eliminação abaixo NÃO é confiável.")
        else:
            print(f"    ✅ dentro de {LIMIAR_DERIVA_ACEITAVEL} — snapshot compatível.")

    if not avaliadas:
        print("\n  (nada avaliável — sem conclusão)")
        return None

    pct_some = 100.0 * somem / avaliadas
    print()
    print(f"  RESULTADO")
    print(f"    sobrevivem ao filtro VE>0:  {sobrevivem}  ({100.0*sobrevivem/avaliadas:.1f}%)")
    print(f"    DEIXARIAM DE EXISTIR:       {somem}  ({pct_some:.1f}%)")
    print()
    print(f"    por direção:")
    for d in ("mais", "menos"):
        viv, mor = por_direcao[d]
        tot = viv + mor
        if tot:
            print(f"      {d:<6} sobrevivem {viv:>4} | somem {mor:>4}  ({100.0*mor/tot:.1f}% eliminadas)")

    if detalhes_somem:
        acertos_entre_as_que_somem = sum(1 for d in detalhes_somem if d[6] == "acertou")
        print()
        print(f"    das {somem} que sumiriam, {acertos_entre_as_que_somem} tinham ACERTADO"
              f" ({100.0*acertos_entre_as_que_somem/somem:.1f}%)")
        print(f"    ⚠️ esse número sozinho NÃO condena a mudança: metade dos pares")
        print(f"       contraditórios acerta por construção. Ver o ROI no relatório.")

    return pct_some


def main():
    fase = sys.argv[1] if len(sys.argv) > 1 else "verificar"
    cabecalho(fase)

    if fase != "verificar":
        print("Este script só tem a fase `verificar`. Ele não grava nada.")
        return

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        total_jogos = carregar_total_jogos_por_time(cur)
        print(f"times com histórico carregado: {len(total_jogos)}")

        # ---------- ALVO ----------
        pct_alvo = simular(cur, "gols_total", total_jogos, "ALVO — mercado de JOGO INTEIRO")

        # ---------- GRUPO DE CONTROLE ----------
        print()
        print("=" * 78)
        print("GRUPO DE CONTROLE")
        print("=" * 78)
        print("`gols_time` é mercado de TIME: as duas perspectivas são APOSTAS")
        print("DIFERENTES, não duas estimativas do mesmo evento. A reconciliação")
        print("não se aplica ali e NADA pode mudar.")
        print()
        print("⚠️ O cálculo abaixo aplica a MESMA fórmula no mercado errado, de")
        print("   propósito. Se ele eliminar recomendações, isso mede o tamanho do")
        print("   estrago que um vazamento de escopo causaria — NÃO é uma proposta.")

        pct_controle = simular(cur, "gols_time", total_jogos, "CONTROLE — mercado de TIME")

        # ---------- VEREDITO ----------
        print()
        print("=" * 78)
        print("VEREDITO — critério travado ANTES do dado")
        print("=" * 78)
        print(f"Limiar de falseamento: eliminar mais de {LIMIAR_FALSEAMENTO_PCT}% "
              f"devolve o desenho pra prancheta.")
        print()

        if pct_alvo is None:
            print("🔴 SEM CONCLUSÃO — nada avaliável no alvo.")
        elif pct_alvo > LIMIAR_FALSEAMENTO_PCT:
            print(f"🔴 REPROVADO: {pct_alvo:.1f}% eliminadas, acima de {LIMIAR_FALSEAMENTO_PCT}%.")
            print("   A média ponderada é agressiva demais. Próximo desenho a")
            print("   considerar: encolher uma perspectiva EM DIREÇÃO à outra")
            print("   (como _encolher_para_prior faz com o confronto direto), em")
            print("   vez de substituir as duas pela média.")
        else:
            print(f"✅ APROVADO: {pct_alvo:.1f}% eliminadas, dentro de {LIMIAR_FALSEAMENTO_PCT}%.")
            print("   A Fase 2 pode ser escrita. ⚠️ Ela continua sendo mudança de")
            print("   fórmula: deploy isolado, e o handicap/resultado_final/mercados")
            print("   de TIME têm que sair com ZERO linhas alteradas (critério C3).")

        if pct_controle:
            print()
            print(f"⚠️ CONTROLE: a mesma fórmula em `gols_time` eliminaria {pct_controle:.1f}%.")
            print("   É a medida do estrago de um vazamento de escopo, não uma proposta.")
            print("   A Fase 2 tem que tocar SOMENTE os mercados de MERCADOS_JOGO_INTEIRO.")

        print()
        print("=" * 78)
        print("Fim. Nenhuma linha de produção foi tocada, nada foi gravado.")
        print("=" * 78)

    finally:
        # Incondicional, mesmo em caminho de sucesso — este script nunca grava.
        conn.rollback()
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
