"""
Remove linhas duplicadas de `historico_recomendacoes`.

POR QUE EXISTE
--------------
Medido em 14/09/2026 pela rota /api/consulta:

    rodada 26 ->  38 grupos duplicados,  38 linhas excedentes
    rodada 27 -> 267 grupos duplicados, 299 linhas excedentes

Na rodada 27 isso infla o histórico de 398 para 697 linhas (+75%), e a
inflação NAO e uniforme: os jogos arquivados mais cedo aparecem ate 3
vezes, os arquivados por ultimo aparecem 1 vez. Qualquer gap ou ROI
calculado sobre a tabela crua pondera os jogos de sexta 3x contra os de
domingo 1x.

MECANISMO (inferido do dado, confirmar no codigo antes de fechar o item)
-----------------------------------------------------------------------
`salvar_recomendacoes` apaga de `recomendacoes` apenas o que e de jogo
FUTURO antes de gravar (1_ESSENCIAL 5.9). A assimetria protege o
historico pre-jogo, e isso esta certo. Mas ela tem um custo que nao
estava registrado: se `motor_recomendacoes` roda DEPOIS de o jogo ja ter
sido arquivado, ele gera de novo as linhas daquele jogo, elas ficam em
`recomendacoes` (o DELETE nao alcanca passado), e o arquivamento
seguinte as move para o historico outra vez.

Cada execucao extra do motor apos o arquivamento = uma copia a mais.

Exemplo medido, fixture 1492374 (Coritiba x Atletico-PR, jogo de 12/09):

    arquivado_em 2026-09-12 12:07 -> 34 linhas
    arquivado_em 2026-09-13 12:12 -> 41 linhas
    arquivado_em 2026-09-14 12:09 -> 41 linhas

As linhas sao identicas em odd, probabilidade e resultado. Nao ha perda
de informacao ao manter apenas a primeira.

CRITERIO DE DEDUP
-----------------
Identidade estrutural de uma recomendacao, a mesma que
`buscar_resultado_perna` usa em combinacoes.py:

    (jogo_id, tipo_padrao, jogador_id, linha, direcao)

Nao se usa `descricao` como identidade -- e regra do projeto, e a
descricao muda de texto entre versoes sem que a aposta mude.

Mantem-se a linha de MENOR `arquivado_em`: e a que corresponde a ultima
geracao ANTES do apito. As copias posteriores foram geradas com o jogo
ja encerrado.

SEGURANCA
---------
- Nenhuma tabela referencia `historico_recomendacoes` por chave
  estrangeira (verificado em information_schema, 14/09).
- `apostas_salvas.pernas` casa por ESTRUTURA, nao por id do historico,
  entao apagar uma copia identica nao desliga nenhuma aposta.
- Duas fases. `verificar` nao escreve nada.
- A fase `aplicar` aborta se o numero de linhas a apagar divergir do que
  a fase `verificar` contou na mesma execucao.

USO
---
    python deduplicar_historico_recomendacoes.py verificar
    python deduplicar_historico_recomendacoes.py aplicar
"""

import os
import sys
from datetime import datetime, timezone

import psycopg2


# Consulta unica que identifica, por grupo de identidade estrutural,
# todas as linhas menos a mais antiga. `ctid` nao e usado de proposito:
# a tabela tem `id` proprio, e ctid muda com VACUUM.
SQL_EXCEDENTES = """
WITH ordenadas AS (
    SELECT
        h.id,
        h.jogo_id,
        h.tipo_padrao,
        h.arquivado_em,
        j.rodada_numero,
        ROW_NUMBER() OVER (
            PARTITION BY
                h.jogo_id,
                h.tipo_padrao,
                COALESCE(h.jogador_id, -1),
                COALESCE(h.linha, -999),
                COALESCE(h.direcao, '')
            ORDER BY h.arquivado_em ASC, h.id ASC
        ) AS posicao
    FROM historico_recomendacoes h
    JOIN jogos j ON j.id = h.jogo_id
)
SELECT id, jogo_id, tipo_padrao, rodada_numero, arquivado_em
FROM ordenadas
WHERE posicao > 1
"""

SQL_RESUMO = """
WITH ordenadas AS (
    SELECT
        h.id,
        j.rodada_numero,
        j.fixture_id_api,
        h.arquivado_em,
        ROW_NUMBER() OVER (
            PARTITION BY
                h.jogo_id,
                h.tipo_padrao,
                COALESCE(h.jogador_id, -1),
                COALESCE(h.linha, -999),
                COALESCE(h.direcao, '')
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

# Checagem que pode DERRUBAR a premissa deste script: se as copias
# divergirem em odd, probabilidade ou resultado, entao elas nao sao
# copias -- sao recomendacoes diferentes, e apagar seria perda de dado.
SQL_COPIAS_DIVERGENTES = """
SELECT
    h.jogo_id,
    h.tipo_padrao,
    COALESCE(h.linha, -999) AS linha,
    COALESCE(h.direcao, '') AS direcao,
    COUNT(*) AS copias,
    COUNT(DISTINCT h.odd_oferecida) AS odds_distintas,
    COUNT(DISTINCT h.probabilidade_historica) AS probs_distintas,
    COUNT(DISTINCT h.resultado) AS resultados_distintos
FROM historico_recomendacoes h
GROUP BY
    h.jogo_id,
    h.tipo_padrao,
    COALESCE(h.jogador_id, -1),
    COALESCE(h.linha, -999),
    COALESCE(h.direcao, '')
HAVING COUNT(*) > 1
   AND (COUNT(DISTINCT h.odd_oferecida) > 1
        OR COUNT(DISTINCT h.probabilidade_historica) > 1
        OR COUNT(DISTINCT h.resultado) > 1)
ORDER BY 5 DESC
LIMIT 40
"""


def cabecalho(fase):
    """Cabecalho obrigatorio: o log do Railway embaralha e e facil abrir
    o deploy anterior por engano."""
    print("=" * 72)
    print("deduplicar_historico_recomendacoes.py")
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
        cur.execute(SQL_COPIAS_DIVERGENTES)
        divergentes = cur.fetchall()

        if divergentes:
            print()
            print("!! PREMISSA DERRUBADA - NAO APLICAR !!")
            print()
            print("Existem grupos com mais de uma linha que NAO sao copias")
            print("identicas: divergem em odd, probabilidade ou resultado.")
            print("Isso significa que sao recomendacoes diferentes, e apagar")
            print("perderia dado. Investigar antes de rodar a fase aplicar.")
            print()
            print("jogo_id | tipo_padrao | linha | direcao | copias | odds | probs | resultados")
            for linha in divergentes:
                print(" | ".join(str(c) for c in linha))
            print()
            return None

        print()
        print("Checagem de premissa: todas as copias sao identicas em odd,")
        print("probabilidade e resultado. Apagar excedente nao perde dado.")

        cur.execute(SQL_RESUMO)
        resumo = cur.fetchall()

        cur.execute(f"SELECT COUNT(*) FROM ({SQL_EXCEDENTES}) t")
        total = cur.fetchone()[0]

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
        print("Abortado: a fase verificar derrubou a premissa.")
        return

    if total_esperado == 0:
        print("Nada a fazer.")
        return

    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM historico_recomendacoes WHERE id IN (SELECT id FROM ({SQL_EXCEDENTES}) t)")
        apagadas = cur.rowcount

        if apagadas != total_esperado:
            conn.rollback()
            print()
            print(f"ABORTADO: esperava apagar {total_esperado}, o DELETE")
            print(f"atingiu {apagadas}. Nada foi gravado (rollback).")
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
