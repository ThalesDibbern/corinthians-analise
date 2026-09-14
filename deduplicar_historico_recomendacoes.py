"""
Remove copias contaminadas de `historico_recomendacoes`.

v2 - 14/09/2026. A v1 abortava por uma premissa errada minha. O que a
trava dela pegou virou o achado principal; ver ACHADO abaixo.

O QUE FOI MEDIDO (rota /api/consulta, 14/09)
--------------------------------------------
    rodada 26 ->  38 grupos com mais de uma linha
    rodada 27 -> 267 grupos, 299 linhas excedentes

Na rodada 27 isso infla o historico de 398 para 697 linhas (+75%), e a
inflacao NAO e uniforme: jogo arquivado mais cedo aparece ate 3 vezes,
jogo arquivado por ultimo aparece 1 vez. Gap e ROI calculados sobre a
tabela crua ponderam sexta 3x contra domingo 1x.

MECANISMO DA DUPLICACAO
-----------------------
`salvar_recomendacoes` apaga de `recomendacoes` apenas o que e de jogo
FUTURO antes de gravar (1_ESSENCIAL 5.9). A assimetria protege o
historico pre-jogo e esta certa. O custo nao registrado: se
`motor_recomendacoes` roda DEPOIS de o jogo ja ter sido arquivado, ele
regera as linhas daquele jogo, o DELETE nao as alcanca, e o
arquivamento seguinte as move para o historico outra vez.

Cada execucao extra do motor apos o arquivamento = uma copia a mais.

⚠️ ACHADO - as copias NAO sao identicas, e a diferenca tem direcao
-------------------------------------------------------------------
A v1 assumia copias identicas e abortava se odd, probabilidade ou
veredito divergissem. Elas divergem -- em PROBABILIDADE, e so nela.

Exemplo medido, jogo_id 715 (Coritiba x Atletico-PR, 13/09):

    linha      odd    12/09    13/09    14/09   veredito
    Mais 1.5   1.39   73.23    73.56    73.56   acertou
    Mais 2.5   2.20   47.58    47.75    47.75   acertou
    Mais 3.5   4.00   27.07    27.86    27.86   acertou
    Mais 4.5   8.20   15.30    15.79    15.79   acertou

Odd congelada (a casa nao atualiza jogo encerrado). Probabilidade sobe.
Todas as quatro sobem, e todas acertaram.

Sobre os 305 grupos duplicados, primeira copia contra ultima:

    veredito   grupos   prob subiu   prob caiu   igual   delta medio
    acertou      163        48           2        113       +0.209
    errou        142         5          48         89       -0.295

96 de 103 mudancas na DIRECAO DO RESULTADO.

Causa: `motor_padroes` regrava a janela de 50 jogos todo dia as 12:03
UTC. O jogo recem-disputado entra nessa janela. A regeracao seguinte
calcula a probabilidade daquele jogo ja tendo visto o proprio resultado.

Isso e look-ahead. A copia mais nova nao e uma copia -- e uma previsao
contaminada pelo evento que ela deveria prever.

CONSEQUENCIA PARA ESTE SCRIPT
-----------------------------
Apagar nao perde dado: remove dado contaminado.

E manter a linha de MENOR `arquivado_em` deixa de ser indiferente e
passa a ser obrigatorio -- e a unica que nao viu o resultado.

CRITERIO DE DEDUP
-----------------
Identidade estrutural, a mesma de `buscar_resultado_perna`:

    (jogo_id, tipo_padrao, jogador_id, linha, direcao)

Nunca `descricao` -- e regra do projeto, e o texto muda entre versoes
sem que a aposta mude.

A TRAVA QUE SOBROU
------------------
Divergencia de PROBABILIDADE e esperada (e o achado acima) -- a fase
`verificar` a mede e imprime, nao aborta.

Divergencia de ODD ou de VEREDITO aborta: odd diferente significa
captura em momento diferente de precificacao, veredito diferente
significa que a avaliacao mudou de opiniao. Nos dois casos as linhas
nao sao a mesma aposta e apagar seria perda de dado real.

SEGURANCA
---------
- Nenhuma tabela referencia `historico_recomendacoes` por FK
  (verificado em information_schema, 14/09).
- `apostas_salvas.pernas` casa por ESTRUTURA, nao por id do historico.
- Duas fases. `verificar` nao escreve.
- `aplicar` refaz a contagem e faz rollback se o DELETE divergir dela.

USO
---
    python deduplicar_historico_recomendacoes.py verificar
    python deduplicar_historico_recomendacoes.py aplicar
"""

import os
import sys
from datetime import datetime, timezone

import psycopg2


CHAVE = """
    h.jogo_id,
    h.tipo_padrao,
    COALESCE(h.jogador_id, -1),
    COALESCE(h.linha, -999),
    COALESCE(h.direcao, '')
"""

SQL_EXCEDENTES = f"""
WITH ordenadas AS (
    SELECT
        h.id,
        ROW_NUMBER() OVER (
            PARTITION BY {CHAVE}
            ORDER BY h.arquivado_em ASC, h.id ASC
        ) AS posicao
    FROM historico_recomendacoes h
)
SELECT id
FROM ordenadas
WHERE posicao > 1
"""

SQL_RESUMO = f"""
WITH ordenadas AS (
    SELECT
        j.rodada_numero,
        j.fixture_id_api,
        ROW_NUMBER() OVER (
            PARTITION BY {CHAVE}
            ORDER BY h.arquivado_em ASC, h.id ASC
        ) AS posicao
    FROM historico_recomendacoes h
    JOIN jogos j ON j.id = h.jogo_id
)
SELECT
    rodada_numero,
    COUNT(*) FILTER (WHERE posicao = 1) AS ficam,
    COUNT(*) FILTER (WHERE posicao > 1) AS saem,
    COUNT(DISTINCT fixture_id_api) AS fixtures
FROM ordenadas
GROUP BY rodada_numero
HAVING COUNT(*) FILTER (WHERE posicao > 1) > 0
ORDER BY rodada_numero
"""

# ABORTA: odd ou veredito divergente = nao e a mesma aposta.
SQL_BLOQUEIO = """
SELECT
    h.jogo_id,
    h.tipo_padrao,
    COALESCE(h.linha, -999) AS linha,
    COALESCE(h.direcao, '') AS direcao,
    COUNT(*) AS copias,
    COUNT(DISTINCT h.odd_oferecida) AS odds_distintas,
    COUNT(DISTINCT h.resultado) AS vereditos_distintos
FROM historico_recomendacoes h
GROUP BY
    h.jogo_id,
    h.tipo_padrao,
    COALESCE(h.jogador_id, -1),
    COALESCE(h.linha, -999),
    COALESCE(h.direcao, '')
HAVING COUNT(*) > 1
   AND (COUNT(DISTINCT h.odd_oferecida) > 1
        OR COUNT(DISTINCT h.resultado) > 1)
ORDER BY 5 DESC
LIMIT 40
"""

# NAO aborta: mede o look-ahead para conferencia antes de aplicar.
SQL_LOOK_AHEAD = """
WITH g AS (
    SELECT
        h.jogo_id,
        h.tipo_padrao,
        COALESCE(h.jogador_id, -1) AS jog,
        COALESCE(h.linha, -999) AS lin,
        COALESCE(h.direcao, '') AS dir,
        MIN(h.arquivado_em) AS prim,
        MAX(h.arquivado_em) AS ult
    FROM historico_recomendacoes h
    GROUP BY 1, 2, 3, 4, 5
    HAVING COUNT(*) > 1
),
d AS (
    SELECT
        g.*,
        (SELECT h1.probabilidade_historica
           FROM historico_recomendacoes h1
          WHERE h1.jogo_id = g.jogo_id
            AND h1.tipo_padrao = g.tipo_padrao
            AND COALESCE(h1.jogador_id, -1) = g.jog
            AND COALESCE(h1.linha, -999) = g.lin
            AND COALESCE(h1.direcao, '') = g.dir
            AND h1.arquivado_em = g.prim
          LIMIT 1) AS p_prim,
        (SELECT h2.probabilidade_historica
           FROM historico_recomendacoes h2
          WHERE h2.jogo_id = g.jogo_id
            AND h2.tipo_padrao = g.tipo_padrao
            AND COALESCE(h2.jogador_id, -1) = g.jog
            AND COALESCE(h2.linha, -999) = g.lin
            AND COALESCE(h2.direcao, '') = g.dir
            AND h2.arquivado_em = g.ult
          LIMIT 1) AS p_ult,
        (SELECT h3.resultado
           FROM historico_recomendacoes h3
          WHERE h3.jogo_id = g.jogo_id
            AND h3.tipo_padrao = g.tipo_padrao
            AND COALESCE(h3.jogador_id, -1) = g.jog
            AND COALESCE(h3.linha, -999) = g.lin
            AND COALESCE(h3.direcao, '') = g.dir
            AND h3.arquivado_em = g.prim
          LIMIT 1) AS res
    FROM g
)
SELECT
    res,
    COUNT(*) AS grupos,
    SUM(CASE WHEN p_ult > p_prim THEN 1 ELSE 0 END) AS prob_subiu,
    SUM(CASE WHEN p_ult < p_prim THEN 1 ELSE 0 END) AS prob_caiu,
    SUM(CASE WHEN p_ult = p_prim THEN 1 ELSE 0 END) AS igual,
    ROUND(AVG(p_ult - p_prim), 3) AS delta_medio
FROM d
GROUP BY res
ORDER BY res
"""


def cabecalho(fase):
    """O log do Railway embaralha e e facil abrir o deploy anterior."""
    print("=" * 72)
    print("deduplicar_historico_recomendacoes.py  (v2)")
    print(f"FASE......: {fase}")
    print(f"ARGV......: {sys.argv}")
    print(f"RELOGIO...: {datetime.now(timezone.utc).isoformat()} UTC")
    print("=" * 72)
    sys.stdout.flush()


def conectar():
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ERRO: DATABASE_URL nao configurada.")
        sys.exit(1)
    return psycopg2.connect(url)


def verificar(conn):
    with conn.cursor() as cur:
        cur.execute(SQL_BLOQUEIO)
        bloqueio = cur.fetchall()

        if bloqueio:
            print()
            print("!! BLOQUEIO - NAO APLICAR !!")
            print()
            print("Grupos com odd OU veredito divergente entre as copias.")
            print("Isso nao e regeracao: sao apostas diferentes, ou a")
            print("avaliacao mudou de opiniao. Apagar perderia dado real.")
            print()
            print("jogo_id | tipo_padrao | linha | direcao | copias | odds | vereditos")
            for l in bloqueio:
                print(" | ".join(str(c) for c in l))
            print()
            return None

        print()
        print("Trava: nenhuma copia diverge em odd nem em veredito.")
        print("Divergencia de probabilidade e esperada -- medida abaixo.")

        cur.execute(SQL_LOOK_AHEAD)
        look = cur.fetchall()

        cur.execute(SQL_RESUMO)
        resumo = cur.fetchall()

        cur.execute(f"SELECT COUNT(*) FROM ({SQL_EXCEDENTES}) t")
        total = cur.fetchone()[0]

    print()
    print("LOOK-AHEAD - probabilidade da 1a copia contra a ultima:")
    print()
    print("  veredito | grupos | subiu | caiu | igual | delta medio")
    print("  ---------+--------+-------+------+-------+------------")
    subiu_acertou = caiu_errou = mudou = 0
    for res, grupos, subiu, caiu, igual, delta in look:
        print(f"  {str(res):>8} | {grupos:>6} | {subiu:>5} | {caiu:>4} "
              f"| {igual:>5} | {str(delta):>11}")
        mudou += subiu + caiu
        if res == "acertou":
            subiu_acertou = subiu
        elif res == "errou":
            caiu_errou = caiu

    na_direcao = subiu_acertou + caiu_errou
    if mudou:
        print()
        print(f"  {na_direcao} de {mudou} mudancas foram NA DIRECAO do resultado "
              f"({100.0 * na_direcao / mudou:.1f}%).")
        print("  Confirma que as copias mais novas viram o proprio resultado.")
        print("  Manter a MAIS ANTIGA nao e preferencia: e a unica limpa.")

    print()
    print("Excedentes por rodada:")
    print()
    print("  rodada | ficam | saem | fixtures")
    print("  -------+-------+------+---------")
    for rodada, ficam, saem, fixtures in resumo:
        print(f"  {str(rodada):>6} | {ficam:>5} | {saem:>4} | {fixtures:>8}")
    print()
    print(f"TOTAL A APAGAR: {total} linha(s)")
    print()
    return total


def aplicar(conn):
    total_esperado = verificar(conn)

    if total_esperado is None:
        print("Abortado: a fase verificar bloqueou.")
        return

    if total_esperado == 0:
        print("Nada a fazer.")
        return

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM historico_recomendacoes "
            f"WHERE id IN (SELECT id FROM ({SQL_EXCEDENTES}) t)"
        )
        apagadas = cur.rowcount

        if apagadas != total_esperado:
            conn.rollback()
            print()
            print(f"ABORTADO: esperava apagar {total_esperado}, o DELETE")
            print(f"atingiu {apagadas}. Rollback, nada foi gravado.")
            return

        conn.commit()

    print()
    print(f"OK: {apagadas} linha(s) apagada(s).")
    print()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT j.rodada_numero, COUNT(*) AS linhas,
                   COUNT(DISTINCT j.fixture_id_api) AS fixtures
            FROM historico_recomendacoes h
            JOIN jogos j ON j.id = h.jogo_id
            WHERE j.data_jogo >= DATE '2026-01-01'
            GROUP BY 1 ORDER BY 1
        """)
        print("Estado final por rodada (2026):")
        print()
        print("  rodada | linhas | fixtures")
        print("  -------+--------+---------")
        for rodada, linhas, fixtures in cur.fetchall():
            print(f"  {str(rodada):>6} | {linhas:>6} | {fixtures:>8}")
    print()


def main():
    fase = sys.argv[1] if len(sys.argv) > 1 else ""
    if fase not in ("verificar", "aplicar"):
        print("uso: python deduplicar_historico_recomendacoes.py verificar|aplicar")
        sys.exit(1)

    cabecalho(fase)
    conn = conectar()
    try:
        if fase == "verificar":
            verificar(conn)
        else:
            aplicar(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
