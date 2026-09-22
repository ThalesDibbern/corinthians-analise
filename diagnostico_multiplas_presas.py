"""
DIAGNÓSTICO: por que múltiplas congeladas nunca são avaliadas.

⚠️ SÓ LEITURA. Este script NÃO escreve nada. Não tem fase `aplicar`, e
isso é deliberado: a regra do projeto pede duas fases em todo script que
TOCA dado - este não toca. A conexão é aberta com autocommit desligado e
termina em `rollback()` incondicional, dentro de `finally`.

--------------------------------------------------------------------------
POR QUE ESTE SCRIPT EXISTE
--------------------------------------------------------------------------

Medido em 21/09/2026: 717 candidatas da rodada 28 ficaram `congelada=TRUE,
avaliada=FALSE` mesmo DEPOIS do cron que avaliou 281 outras na mesma
execução. Na base inteira são 5.154 presas, desde 15/08.

QUATRO hipóteses foram levantadas e TODAS derrubadas por medição:

  1. "É atraso de um ciclo"          -> ❌ o cron de 21/09 avaliou 281 e
                                          deixou 717 na MESMA execução.
  2. "A avaliação está acoplada ao   -> ❌ `selecionar_top5_do_jogo` lê
      top-5"                             `avaliada = TRUE`; é consequência,
                                         não causa.
  3. "Alguma perna está 'pendente'"  -> ❌ ZERO linhas com
                                         `resultado='pendente'` em toda a
                                         `historico_recomendacoes`
                                         (1.272 errou + 1.014 acertou).
  4. "A perna não tem                -> ❌ 1.740 de 1.740 pernas têm o
      `fixture_id_api` no JSONB"         campo preenchido, não-nulo.

E a reimplementação do casamento em SQL não reproduz o resultado do código:
ela acha 222 pernas órfãs, o que explicaria 211 candidatas - mas 506 têm
TODAS as pernas casando pela minha query e mesmo assim não foram avaliadas.

CONCLUSÃO: a dedução indireta não está convergindo, e o projeto tem uma
regra exatamente para isso - "hipóteses acumulando sem convergir: trocar de
método" e "parar de deduzir e instrumentar".

--------------------------------------------------------------------------
O QUE ESTE SCRIPT FAZ DE DIFERENTE
--------------------------------------------------------------------------

Ele **importa e chama a função de produção** `buscar_resultado_perna`, em
vez de reescrever a lógica dela em SQL. Foi justamente a reescrita que
produziu as quatro hipóteses erradas: minha versão era mais permissiva na
chave do jogo (aceitava `jogo_id` OU `fixture_id_api`) e mais estrita na
`direcao` (sem `LOWER(TRIM())`). Uma reimplementação que diverge do
original no detalhe é a mesma família de bug já registrada no projeto -
"duas funções com a mesma responsabilidade: auditar lado a lado".

Para cada perna que a função de produção devolve `None`, o script
classifica O PORQUÊ, consultando o banco em três níveis:

  AINDA_ATIVA        A recomendação existe em `recomendacoes` - ou seja,
                     ainda não foi arquivada. A múltipla está esperando,
                     legitimamente. Some sozinha no próximo cron.

  CASAMENTO_FALHOU   Existe algo em `historico_recomendacoes` para o mesmo
                     jogo/fixture + tipo_padrao, mas nenhuma linha casa com
                     a chave completa. É BUG DE CASAMENTO, e o script
                     imprime lado a lado o que a perna pede e o que o
                     histórico tem - `linha`, `direcao` e `jogador_id` -
                     para que o campo divergente apareça sem adivinhação.

  SUMIU              Não existe nem em `recomendacoes` nem em
                     `historico_recomendacoes`, por chave nenhuma. A
                     recomendação foi APAGADA sem nunca ser arquivada.
                     Hipótese em aberto: o `DELETE` de
                     `salvar_recomendacoes` remove as recomendações de jogo
                     futuro a cada geração; se a odd mudou e o VE caiu
                     abaixo do piso, a perna não é regerada e some. A
                     múltipla que a contém fica presa PARA SEMPRE - nenhum
                     caminho do sistema a reabre.

⚠️ As três classes exigem consertos DIFERENTES. É por isso que o script
separa em vez de contar um número só:

  - AINDA_ATIVA      não é bug. Nada a fazer.
  - CASAMENTO_FALHOU conserta-se na chave de `buscar_resultado_perna`.
  - SUMIU            não se conserta ali: ou o motor para de apagar perna
                     referenciada por candidata viva, ou a candidata
                     precisa de um varredor que a encerre como
                     "inavaliável" em vez de deixá-la presa (item 9.17).

--------------------------------------------------------------------------
COMO RODAR (serviço `abundant-abundance`, Custom Start Command)
--------------------------------------------------------------------------

    python diagnostico_multiplas_presas.py

      Amostra: as 60 candidatas presas mais recentes. Rápido, e é a fatia
      que responde a rodada 28. "Auditoria cara: rodar numa fatia primeiro."

    python diagnostico_multiplas_presas.py --limite 300

    python diagnostico_multiplas_presas.py --desde 2026-09-19

      Só candidatas cujo `primeiro_apito` é dessa data em diante.

    python diagnostico_multiplas_presas.py --todas

      Varre as 5.154. ⚠️ Faz algumas consultas por perna - pode levar
      minutos. Rodar só depois que a amostra mostrar que vale.

Variáveis de ambiente:
  - DATABASE_URL -> a mesma dos outros scripts.
"""

import os
import sys
from collections import Counter
from datetime import datetime

import psycopg2

# ⚠️ O PONTO INTEIRO DO SCRIPT: a função de produção, não uma cópia dela.
# Se algum dia `buscar_resultado_perna` mudar, este diagnóstico muda junto -
# que é o comportamento desejado.
from arquivar_recomendacoes import buscar_resultado_perna
from combinacoes import MERCADOS_JOGO_INTEIRO, MERCADOS_JOGADOR

DATABASE_URL = os.environ["DATABASE_URL"]

LIMITE_PADRAO = 60
MAX_EXEMPLOS_POR_CLASSE = 8


def ler_argumentos(argv):
    """Sem argparse de propósito - o padrão dos scripts deste projeto é
    ler `sys.argv` direto, e o cabeçalho do log imprime o argv cru para
    que não reste dúvida sobre o que rodou."""
    limite = LIMITE_PADRAO
    desde = None
    todas = "--todas" in argv

    if "--limite" in argv:
        limite = int(argv[argv.index("--limite") + 1])
    if "--desde" in argv:
        desde = argv[argv.index("--desde") + 1]

    return limite, desde, todas


def buscar_presas(cur, limite, desde, todas):
    """As candidatas congeladas e não avaliadas.

    ⚠️ A cláusula é a MESMA de `buscar_candidatas_prontas_para_avaliar` no
    `arquivar_recomendacoes.py` (`congelada = TRUE AND avaliada = FALSE`),
    de propósito: o conjunto examinado aqui tem que ser exatamente o
    conjunto que a produção tenta avaliar e não consegue. Só o ORDER BY e
    o LIMIT são acrescentados, e nenhum dos dois muda quem é elegível."""
    filtro_data = ""
    parametros = []

    if desde:
        filtro_data = "AND primeiro_apito >= %s "
        parametros.append(desde)

    sql = (
        "SELECT id, pernas, jogos, primeiro_apito, criada_em "
        "FROM multiplas_candidatas "
        "WHERE congelada = TRUE AND avaliada = FALSE " + filtro_data +
        "ORDER BY primeiro_apito DESC"
    )

    if not todas:
        sql += " LIMIT %s"
        parametros.append(limite)

    cur.execute(sql, parametros)
    return cur.fetchall()


def contar_em_recomendacoes(cur, perna):
    """A recomendação dessa perna ainda está VIVA em `recomendacoes`?

    Se está, a múltipla não é um caso de bug: ela espera o arquivamento
    daquele jogo. Busca pelas duas chaves possíveis para não classificar
    errado por causa de perspectiva."""
    cur.execute(
        """
        SELECT COUNT(*)
        FROM recomendacoes r
        JOIN jogos j ON j.id = r.jogo_id
        WHERE r.tipo_padrao = %s
          AND r.jogador_id IS NOT DISTINCT FROM %s
          AND r.linha IS NOT DISTINCT FROM %s
          AND LOWER(TRIM(COALESCE(r.direcao, ''))) = LOWER(TRIM(COALESCE(%s, '')))
          AND (r.jogo_id = %s OR j.fixture_id_api = %s)
        """,
        (
            perna["tipo_padrao"],
            perna["jogador_id"],
            perna["linha"],
            perna["direcao"],
            perna["jogo_id"],
            perna.get("fixture_id_api"),
        ),
    )
    return cur.fetchone()[0]


def vizinhos_no_historico(cur, perna):
    """O que o histórico TEM para esse jogo e esse mercado.

    Casamento FROUXO de propósito: só jogo/fixture + `tipo_padrao`, sem
    `linha`, `direcao` nem `jogador_id`. Se vier vazio, a recomendação
    nunca foi arquivada (classe SUMIU). Se vier com linhas, ela foi
    arquivada e o casamento estrito é que falhou - e aí estas linhas são a
    evidência de QUAL campo diverge, que é o que faltava para decidir."""
    cur.execute(
        """
        SELECT hr.id, hr.jogo_id, j.fixture_id_api, hr.linha, hr.direcao,
               hr.jogador_id, hr.resultado
        FROM historico_recomendacoes hr
        JOIN jogos j ON j.id = hr.jogo_id
        WHERE hr.tipo_padrao = %s
          AND (hr.jogo_id = %s OR j.fixture_id_api = %s)
        ORDER BY hr.id DESC
        LIMIT 6
        """,
        (
            perna["tipo_padrao"],
            perna["jogo_id"],
            perna.get("fixture_id_api"),
        ),
    )
    return cur.fetchall()


def classificar_perna(cur, perna):
    """Chama a função de PRODUÇÃO e, se ela falhar, diz por quê.

    Devolve `(classe, detalhe)`. `classe` é None quando a perna casou -
    nesse caso não há nada a diagnosticar."""
    achado = buscar_resultado_perna(
        cur,
        perna["tipo_padrao"],
        perna["jogo_id"],
        perna.get("fixture_id_api"),
        perna["jogador_id"],
        perna["linha"],
        perna["direcao"],
    )

    if achado is not None:
        return None, achado

    if contar_em_recomendacoes(cur, perna) > 0:
        return "AINDA_ATIVA", None

    vizinhas = vizinhos_no_historico(cur, perna)
    if vizinhas:
        return "CASAMENTO_FALHOU", vizinhas

    return "SUMIU", None


def descrever_perna(perna):
    return (
        f"tipo={perna['tipo_padrao']} jogo_id={perna['jogo_id']} "
        f"fixture={perna.get('fixture_id_api')} linha={perna['linha']} "
        f"direcao={perna['direcao']!r} jogador_id={perna['jogador_id']}"
    )


def imprimir_cabecalho(cur, limite, desde, todas):
    """Fase, argv e relógio - a armadilha do log do Railway é sair fora de
    ordem, então todo script daqui se identifica no próprio texto."""
    cur.execute("SELECT LOCALTIMESTAMP, CURRENT_DATE, current_setting('TimeZone')")
    agora, hoje, fuso = cur.fetchone()

    print("=" * 78)
    print("diagnostico_multiplas_presas.py | fase: SOMENTE LEITURA (rollback no fim)")
    print(f"argv: {sys.argv}")
    print(f"relogio do processo: {datetime.now().isoformat(timespec='seconds')}")
    print(f"relogio da sessao  : {agora}  (CURRENT_DATE={hoje}, TimeZone={fuso})")
    escopo = "TODAS as presas" if todas else f"amostra de {limite}, mais recentes primeiro"
    print(f"escopo: {escopo}" + (f" | desde {desde}" if desde else ""))
    print("=" * 78)


def imprimir_contexto(cur):
    """O tamanho do problema, para que a amostra seja lida com a proporção
    certa ao lado."""
    cur.execute(
        "SELECT COUNT(*) FROM multiplas_candidatas WHERE congelada = TRUE AND avaliada = FALSE"
    )
    presas = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM multiplas_candidatas WHERE avaliada = TRUE")
    avaliadas = cur.fetchone()[0]
    cur.execute("SELECT resultado, COUNT(*) FROM historico_recomendacoes GROUP BY resultado")
    por_resultado = dict(cur.fetchall())

    print(f"  presas na base inteira : {presas}")
    print(f"  ja avaliadas           : {avaliadas}")
    print(f"  historico_recomendacoes: {por_resultado}")
    print(f"  MERCADOS_JOGO_INTEIRO  : {sorted(MERCADOS_JOGO_INTEIRO)}")
    print(f"  MERCADOS_JOGADOR       : {sorted(MERCADOS_JOGADOR)}")
    print("-" * 78)


def main():
    limite, desde, todas = ler_argumentos(sys.argv)

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False  # nada aqui escreve; o rollback do finally é a garantia
    cur = conn.cursor()

    try:
        imprimir_cabecalho(cur, limite, desde, todas)
        imprimir_contexto(cur)

        candidatas = buscar_presas(cur, limite, desde, todas)
        if not candidatas:
            print("Nenhuma candidata presa no escopo pedido.")
            return

        print(f"Examinando {len(candidatas)} candidata(s)...\n")

        por_classe = Counter()
        por_classe_e_tipo = Counter()
        candidatas_por_classe = Counter()
        exemplos = {}
        sem_falha_nenhuma = []

        for cand_id, pernas, _jogos, apito, criada in candidatas:
            classes_desta = set()

            for perna in pernas:
                classe, detalhe = classificar_perna(cur, perna)
                if classe is None:
                    continue

                por_classe[classe] += 1
                por_classe_e_tipo[(classe, perna["tipo_padrao"])] += 1
                classes_desta.add(classe)

                guardados = exemplos.setdefault(classe, [])
                if len(guardados) < MAX_EXEMPLOS_POR_CLASSE:
                    guardados.append((cand_id, apito, criada, perna, detalhe))

            if classes_desta:
                for classe in classes_desta:
                    candidatas_por_classe[classe] += 1
            else:
                # ⚠️ ESTE É O CASO QUE DERRUBA TUDO SE APARECER.
                # Todas as pernas casaram, então `pronto` seria True e a
                # múltipla teria sido avaliada - a menos que o veredito
                # tenha saído 'pendente', que a base diz não existir.
                # Se esta lista vier grande, a causa NÃO é a perna: é algo
                # entre o laço e o UPDATE, e o próximo passo é outro.
                sem_falha_nenhuma.append((cand_id, apito, len(pernas)))

        print("=" * 78)
        print("RESULTADO")
        print("=" * 78)
        print(f"  candidatas examinadas: {len(candidatas)}")
        print(f"  com ao menos uma perna problemática: "
              f"{len(candidatas) - len(sem_falha_nenhuma)}")
        print(f"  ⚠️  com TODAS as pernas casando: {len(sem_falha_nenhuma)}")
        print()

        if por_classe:
            print("  Pernas que falharam, por classe:")
            for classe, quantas in por_classe.most_common():
                print(f"    {quantas:6d} pernas  |  {candidatas_por_classe[classe]:5d} candidatas  |  {classe}")
            print()
            print("  Por classe e mercado:")
            for (classe, tipo), quantas in sorted(por_classe_e_tipo.items(), key=lambda p: -p[1]):
                print(f"    {quantas:6d}  {classe:18s} {tipo}")
            print()

        for classe, guardados in exemplos.items():
            print("-" * 78)
            print(f"EXEMPLOS - {classe}")
            for cand_id, apito, criada, perna, detalhe in guardados:
                print(f"  candidata={cand_id} apito={apito} criada={criada}")
                print(f"    perna pede : {descrever_perna(perna)}")
                if classe == "CASAMENTO_FALHOU":
                    print("    historico tem (mesmo jogo/fixture e mesmo mercado):")
                    for (hid, hjogo, hfix, hlinha, hdir, hjog, hres) in detalhe:
                        print(f"      hr_id={hid} jogo_id={hjogo} fixture={hfix} "
                              f"linha={hlinha} direcao={hdir!r} jogador_id={hjog} "
                              f"resultado={hres}")
                elif classe == "SUMIU":
                    print("    historico tem: NADA para esse jogo/fixture nesse mercado.")
                    print("    -> a recomendacao foi apagada sem nunca ser arquivada.")
                elif classe == "AINDA_ATIVA":
                    print("    ainda esta em `recomendacoes` - espera o arquivamento. Nao e bug.")
            print()

        if sem_falha_nenhuma:
            print("-" * 78)
            print("⚠️  CANDIDATAS COM TODAS AS PERNAS CASANDO E MESMO ASSIM PRESAS")
            print("    Se esta lista nao estiver vazia, a causa nao esta na perna.")
            print("    O proximo passo passa a ser instrumentar o laço de")
            print("    `avaliar_e_selecionar_top5`, nao o casamento.")
            for cand_id, apito, n_pernas in sem_falha_nenhuma[:20]:
                print(f"      candidata={cand_id} apito={apito} pernas={n_pernas}")
            if len(sem_falha_nenhuma) > 20:
                print(f"      ... e mais {len(sem_falha_nenhuma) - 20}")
            print()

        print("=" * 78)
        print("COMO LER ISTO")
        print("  AINDA_ATIVA      -> nao e bug. Some sozinha no proximo cron.")
        print("  CASAMENTO_FALHOU -> bug na chave de `buscar_resultado_perna`.")
        print("                      Comparar 'perna pede' com 'historico tem':")
        print("                      o campo que diverge e a causa.")
        print("  SUMIU            -> a recomendacao foi apagada antes de ser")
        print("                      arquivada. Nao se conserta no casamento.")
        print("  TODAS CASANDO    -> a causa nao esta na perna. Trocar de alvo.")
        print("=" * 78)

    finally:
        conn.rollback()  # ⚠️ incondicional: este script nunca escreve
        print("\nSOMENTE LEITURA: rollback feito, nada foi escrito.")
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
