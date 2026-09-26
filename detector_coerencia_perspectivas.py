"""
detector_coerencia_perspectivas.py — Fase 1 do item 5.9

⚠️ SCRIPT SÓ-LEITURA. Não altera nenhuma recomendação, não grava nada, e
termina em `conn.rollback()` incondicional. É um instrumento de medida,
não um conserto.

POR QUE ELE EXISTE
------------------
A Fase 0.4 (`simular_reconciliacao_gols_total.py`) mediu a reconciliação
no acervo e deu um resultado bom demais: ROI de +36,6% no grupo que
sobreviveria, contra +3,4% de hoje. Bom demais literalmente — aquele
backtest tem LOOK-AHEAD estrutural e não dá pra confiar nele:

    `padroes_gols_total` é recalculada sobre os últimos 50 jogos ATÉ HOJE.
    Ao simular uma recomendação da rodada 24, a janela de hoje já contém
    as rodadas 24 a 28 — inclusive o próprio jogo que se quer prever.

O §5.12 do projeto registra isso: *look-ahead sempre melhora o gap
artificialmente*.

Este script não tem esse defeito, porque roda ANTES dos jogos. A janela de
50 do momento não contém o que ainda não aconteceu. O que ele imprime é
pré-registro de verdade.

O QUE ELE MEDE
--------------
Para cada recomendação VIVA de mercado de JOGO INTEIRO com direção
mais/menos, ele lê a frequência das DUAS perspectivas do jogo e mostra o
quanto elas discordam.

A conta é mais simples do que parece. Se a perspectiva A diz `mais` com
P_A(mais) e a B diz `menos` com P_B(menos) = 100 − P_B(mais), então:

    soma do par = P_A(mais) + 100 − P_B(mais)
                = 100 + (P_A(mais) − P_B(mais))

    desvio de 100 = P_A(mais) − P_B(mais)

Ou seja: **a incoerência do par É a diferença entre as duas estimativas do
mesmo evento.** Não precisa das duas recomendações existirem — basta ler
as duas frequências.

⚠️ O teste é `|desvio| > TOLERANCIA`, NUNCA `desvio > 0`. O defeito é
bidirecional: na base, a soma do par vai de 55,9 a 174,3. Um teste
unidirecional deixaria metade dos casos passar.

O PRÉ-REGISTRO
--------------
Ele imprime os `id` das recomendações que a reconciliação ELIMINARIA, uma
por linha. Isso é deliberado: a regra 19 do projeto manda pré-registrar
gravando IDENTIDADES, não contagens — o pré-registro de 19/09 gravou "461
ativas", o arquivamento levou 404, e a diferença não pôde ser reconstruída.

Depois que a rodada for arquivada, essas identidades permitem medir
OUT-OF-SAMPLE se a reconciliação teria acertado, sem nenhum look-ahead.

⚠️ O log do Railway vem TRUNCADO. Copie a saída deste script para o
protocolo da rodada em vez de confiar que o log vai estar lá depois.

COMO RODAR
----------
    python detector_coerencia_perspectivas.py

Sem argumento — ele só tem um modo, e esse modo não escreve.

DUAS FORMAS DE USAR, e a diferença importa:

  a) AVULSO (recomendado para a rodada 29): rodar à mão depois que o
     `motor_recomendacoes` gerar, e antes do primeiro apito. Não é deploy,
     não toca no cron, e dá exatamente o mesmo dado.

  b) NO CRON: acrescentar ao fim da cadeia do `refreshing-freedom`, depois
     do `motor_combinacoes`. ⚠️ Isso É um deploy. Ele nunca pode entrar
     ANTES do motor com `&&`, porque uma falha aqui pararia a geração —
     e um instrumento de medida jamais deve poder derrubar a produção.

Variáveis de ambiente:
    DATABASE_URL
"""

import os
import sys
from datetime import datetime

import psycopg2

# ⚠️ Funções REAIS de produção. Se alguma faltar ou mudar de assinatura, o
# script para aqui em vez de adivinhar — instrumento divergente mente sem
# avisar, e isso já custou quatro hipóteses em 21/09.
try:
    from motor_recomendacoes import (
        buscar_frequencia_gols_total,
        buscar_frequencia_escanteio_total,
        buscar_frequencia_cartao_total,
        time_tem_historico_curto,
        carregar_total_jogos_por_time,
    )
except ImportError as e:
    print("=" * 78)
    print("ERRO: não consegui importar do motor_recomendacoes.")
    print(f"Detalhe: {e}")
    print()
    print("Este script NÃO reimplementa a lógica de propósito. Rode-o na")
    print("mesma pasta do motor_recomendacoes.py.")
    print("=" * 78)
    raise

DATABASE_URL = os.environ["DATABASE_URL"]

# Proposta em `arquitetura_recomendacao_contraditoria.md` §9. Em pontos
# percentuais, sobre |soma − 100|.
TOLERANCIA_COERENCIA = 1.0

# Só mercados de JOGO INTEIRO com par mais/menos. `resultado_final`,
# `dupla_chance_*` e `ambas_marcam_*` ficam fora: as direções deles não são
# um par complementar de duas leituras do mesmo número.
#
# ⚠️ Mercado de TIME (`gols_time`, `escanteio_time`, `equipe_marca`,
# `marca_ambos_tempos`, `handicap_asiatico`) NÃO entra. Lá as duas
# perspectivas são APOSTAS DIFERENTES, não duas estimativas do mesmo
# evento — e a Fase 0.4 mostrou que aplicar a fórmula ali PIORA o ROI
# (−2,2 pontos). Isso é o grupo de controle, e ele tem que ficar intacto.
FONTES_JOGO_INTEIRO = {
    "gols_total": buscar_frequencia_gols_total,
    "escanteio_total": buscar_frequencia_escanteio_total,
    "cartao_total": buscar_frequencia_cartao_total,
}

TABELA_AMOSTRA = {
    "gols_total": "padroes_gols_total",
    "escanteio_total": "padroes_escanteio_total",
    "cartao_total": "padroes_cartao_total",
}


def cabecalho():
    print("=" * 78)
    print("detector_coerencia_perspectivas.py | Fase 1 do item 5.9")
    print(f"argv: {sys.argv}")
    print(f"relógio: {datetime.now().isoformat()}")
    print(f"TOLERANCIA_COERENCIA = {TOLERANCIA_COERENCIA} pontos")
    print("SÓ LEITURA — não altera recomendação, termina em rollback()")
    print("=" * 78)
    sys.stdout.flush()


def buscar_amostra(cur, tipo_padrao, linha, time_id):
    """Lê `jogos_analisados` da tabela do tipo. Leitura de campo — a função
    de produção consulta esta mesma tabela com este mesmo filtro, mas não
    devolve o tamanho da amostra, e a ponderação precisa dele."""
    cur.execute(
        f"SELECT jogos_analisados FROM {TABELA_AMOSTRA[tipo_padrao]} "
        f"WHERE linha = %s AND time_id = %s",
        (linha, time_id),
    )
    row = cur.fetchone()
    return int(row[0]) if row and row[0] else None


def buscar_recomendacoes_vivas(cur):
    """Recomendações de jogos que ainda não começaram, nos mercados de jogo
    inteiro com par mais/menos. Mesmo critério de 'futuro' que o
    `salvar_recomendacoes` usa."""
    cur.execute(
        """
        SELECT r.id, r.jogo_id, r.tipo_padrao, r.linha, r.direcao,
               r.odd_oferecida, r.probabilidade_historica, r.descricao,
               j.fixture_id_api, j.nosso_time_id, j.mandante_id, j.visitante_id,
               j.datahora_jogo, j.rodada_numero
        FROM recomendacoes r
        JOIN jogos j ON j.id = r.jogo_id
        WHERE r.tipo_padrao = ANY(%s)
          AND LOWER(TRIM(r.direcao)) IN ('mais', 'menos')
          AND ((j.datahora_jogo IS NOT NULL AND j.datahora_jogo >= NOW())
            OR (j.datahora_jogo IS NULL AND j.data_jogo >= CURRENT_DATE))
        ORDER BY j.datahora_jogo, j.fixture_id_api, r.tipo_padrao, r.linha, r.direcao
        """,
        (list(FONTES_JOGO_INTEIRO.keys()),),
    )
    return cur.fetchall()


def ler_duas_perspectivas(cur, tipo_padrao, linha, mandante_id, visitante_id, total_jogos):
    """Lê P(mais) pelas DUAS perspectivas, chamando a função de produção.

    Devolve (p_mandante, n_mandante, p_visitante, n_visitante) ou None."""
    if mandante_id is None or visitante_id is None:
        return None

    buscar = FONTES_JOGO_INTEIRO[tipo_padrao]
    saida = []
    for time_id in (mandante_id, visitante_id):
        suavizar = time_tem_historico_curto(total_jogos, time_id)
        p = buscar(cur, linha, time_id, suavizar=suavizar)
        n = buscar_amostra(cur, tipo_padrao, linha, time_id)
        if p is None or not n:
            return None
        saida.append((p, n))
    return saida[0][0], saida[0][1], saida[1][0], saida[1][1]


def main():
    cabecalho()

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        total_jogos = carregar_total_jogos_por_time(cur)
        vivas = buscar_recomendacoes_vivas(cur)

        print(f"recomendações vivas de JOGO INTEIRO (mais/menos): {len(vivas)}")
        if not vivas:
            print()
            print("Nada a medir. Isso é o estado CORRETO fora de rodada — a")
            print("tabela `recomendacoes` fica vazia entre uma rodada e outra.")
            return

        incoerentes = []
        sem_dois_lados = 0
        eliminadas = []      # pré-registro: identidades, não contagens
        por_mercado = {}

        for (rec_id, jogo_id, tipo, linha, direcao, odd, prob_gravada, descricao,
             fixture, nosso_time_id, mandante_id, visitante_id,
             datahora, rodada) in vivas:

            direcao = (direcao or "").strip().lower()
            odd = float(odd)
            prob_gravada = float(prob_gravada)

            lido = ler_duas_perspectivas(
                cur, tipo, linha, mandante_id, visitante_id, total_jogos
            )
            if lido is None:
                sem_dois_lados += 1
                continue

            p_mand, n_mand, p_vis, n_vis = lido

            # A incoerência do par É a diferença entre as duas estimativas
            # do mesmo evento — ver docstring do módulo.
            desvio = p_mand - p_vis
            soma_par = 100.0 + desvio

            m = por_mercado.setdefault(tipo, {"n": 0, "incoerentes": 0, "desvios": []})
            m["n"] += 1
            m["desvios"].append(abs(desvio))

            # ⚠️ Teste BIDIRECIONAL. `desvio > 0` deixaria metade passar.
            if abs(desvio) > TOLERANCIA_COERENCIA:
                m["incoerentes"] += 1
                incoerentes.append(
                    (rec_id, fixture, tipo, linha, direcao, p_mand, n_mand,
                     p_vis, n_vis, desvio, soma_par, prob_gravada, odd, rodada)
                )

            # O que a reconciliação faria com esta linha, calculado com a
            # janela DE AGORA — sem look-ahead, porque o jogo não aconteceu.
            p_mais_rec = (n_mand * p_mand + n_vis * p_vis) / (n_mand + n_vis)
            p_dir = p_mais_rec if direcao == "mais" else 100.0 - p_mais_rec
            if p_dir <= 100.0 / odd:
                eliminadas.append((rec_id, fixture, tipo, linha, direcao,
                                   prob_gravada, round(p_dir, 2), odd, descricao))

        # ---------------- RELATÓRIO ----------------
        print()
        print("-" * 78)
        print("COERÊNCIA ENTRE PERSPECTIVAS, por mercado")
        print("-" * 78)
        print(f"  {'mercado':<18} {'N':>5} {'incoer.':>8} {'%':>7} "
              f"{'|desvio| méd':>13} {'máx':>8}")
        for tipo, m in sorted(por_mercado.items()):
            if not m["n"]:
                continue
            med = sum(m["desvios"]) / len(m["desvios"])
            mx = max(m["desvios"])
            print(f"  {tipo:<18} {m['n']:>5} {m['incoerentes']:>8} "
                  f"{100.0*m['incoerentes']/m['n']:>6.1f}% {med:>13.2f} {mx:>8.2f}")

        if sem_dois_lados:
            print()
            print(f"  ⚠️ {sem_dois_lados} sem os dois lados legíveis (fallback seria usado)")

        print()
        print("-" * 78)
        print(f"PARES INCOERENTES  (|soma − 100| > {TOLERANCIA_COERENCIA})")
        print("-" * 78)
        if not incoerentes:
            print("  nenhum — as perspectivas concordam em todas as linhas vivas")
        else:
            print(f"  {'id':>7} {'fixture':>9} {'mercado':<17} {'linha':>6} {'dir':<6} "
                  f"{'P_mand':>7} {'P_vis':>7} {'soma':>7}")
            for (rec_id, fixture, tipo, linha, direcao, p_mand, n_mand,
                 p_vis, n_vis, desvio, soma_par, prob_gravada, odd, rodada) in incoerentes:
                print(f"  {rec_id:>7} {fixture:>9} {tipo:<17} {linha:>6} {direcao:<6} "
                      f"{p_mand:>7.1f} {p_vis:>7.1f} {soma_par:>7.1f}")
            somas = [i[10] for i in incoerentes]
            print()
            print(f"  soma do par: média {sum(somas)/len(somas):.1f} · "
                  f"mín {min(somas):.1f} · máx {max(somas):.1f}")
            acima = sum(1 for s in somas if s > 100)
            print(f"  acima de 100: {acima} · abaixo de 100: {len(somas)-acima}")
            print("  ⚠️ se houver casos abaixo de 100, o defeito é bidirecional")
            print("     nesta rodada também — e o teste unidirecional estaria errado.")

        # ---------------- PRÉ-REGISTRO ----------------
        print()
        print("=" * 78)
        print("PRÉ-REGISTRO — identidades, não contagens (regra 19)")
        print("=" * 78)
        print("Estas são as recomendações que a reconciliação ELIMINARIA, com a")
        print("janela de AGORA. Como o jogo ainda não aconteceu, não há look-ahead.")
        print()
        print("⚠️ COPIE ESTA LISTA para o protocolo da rodada. O log do Railway")
        print("   vem truncado e pode não estar disponível depois.")
        print()
        print(f"eliminaria {len(eliminadas)} de {len(vivas)} "
              f"({100.0*len(eliminadas)/len(vivas):.1f}%)"
              if vivas else "")
        print()
        if eliminadas:
            print(f"  {'id':>7} {'fixture':>9} {'mercado':<17} {'linha':>6} {'dir':<6} "
                  f"{'hoje':>7} {'reconc':>7} {'odd':>6} {'casa':>7}")
            for (rec_id, fixture, tipo, linha, direcao, prob_hoje, p_rec, odd, _d) in eliminadas:
                print(f"  {rec_id:>7} {fixture:>9} {tipo:<17} {linha:>6} {direcao:<6} "
                      f"{prob_hoje:>7.1f} {p_rec:>7.1f} {odd:>6.2f} {100.0/odd:>7.1f}")
            print()
            print("  IDs em uma linha, para colar:")
            print("  " + ",".join(str(e[0]) for e in eliminadas))

        print()
        print("=" * 78)
        print("O QUE FAZER COM ISSO DEPOIS DO JOGO")
        print("=" * 78)
        print("Quando a rodada for arquivada, medir o ROI dos dois grupos:")
        print("  - as que a reconciliação teria ELIMINADO (ids acima)")
        print("  - as que teria MANTIDO")
        print()
        print("Se as eliminadas tiverem ROI pior que as mantidas, a Fase 0.4 se")
        print("confirma FORA de amostra e sem look-ahead — e aí a Fase 2 tem base.")
        print("Se não tiver, o +36,6% do backtest era contaminação, e o REPROVADO")
        print("do critério de volume estava certo pelos dois motivos.")
        print()
        print("Nenhuma recomendação foi alterada. Nada foi gravado.")
        print("=" * 78)

    finally:
        conn.rollback()
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
