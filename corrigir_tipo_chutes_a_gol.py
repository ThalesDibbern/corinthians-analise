"""
Script PONTUAL (não faz parte do encadeamento diário do cron) - corrige um
bug de DADO LEGADO em `historico_recomendacoes`: recomendações do mercado
"chutes A gol" (finalização no gol) que ficaram gravadas com
`tipo_padrao = 'chute_total'` em vez de `'chute_no_gol'`.

DE ONDE VEM O BUG:
    `identificar_tipo_padrao` (motor_recomendacoes.py) só reconhecia o
    mercado de chute no gol quando o nome trazia a expressão "no gol". Mas
    o nome real que a OddsPapi usa pro mercado de tempo completo é "chutes
    A gol" (com "a", não "no"). Resultado: toda recomendação desse mercado
    caía no tipo genérico `chute_total`.

    Isso JÁ FOI CORRIGIDO no motor (aceita "no gol" OU "a gol"), então
    recomendação NOVA nasce com o tipo certo. O problema é o que ficou
    gravado ANTES da correção - o tipo errado está cristalizado na linha
    do histórico, e `avaliacao.py` avalia pelo tipo SALVO, não pela
    descrição.

POR QUE ISSO IMPORTA (não é cosmético):
    `avaliacao.py` mapeia `chute_total` -> coluna `chutes` e
    `chute_no_gol` -> coluna `chutes_no_gol`. Como chutes totais é sempre
    >= chutes no gol, essas linhas vinham sendo avaliadas com um número
    inflado - apostas de "Mais de X" marcadas como acerto com muito mais
    facilidade do que deveriam.

    Ou seja: a taxa de acerto que aparece hoje nesse mercado está
    OTIMISTA. Depois dessa correção o número tende a PIORAR - e é
    justamente por isso que ela vale a pena, porque a auditoria de
    calibração (a mesma que revelou o problema de amostra da Zona da
    Tabela) usa esses dados pra tomar decisão de fórmula.

ESCOPO CONFERIDO ANTES DE ESCREVER ESTE SCRIPT:
    - 64 linhas afetadas em `historico_recomendacoes` (23 "acertou",
      41 "errou"), todas com "chutes a gol" na descrição e `chute_total`
      no tipo. Nenhuma linha desse mercado tinha o tipo certo.
    - NENHUMA aposta real (`apostas_salvas`) tem perna desse mercado -
      confirmado por consulta no jsonb `pernas`. Por isso este script
      NÃO mexe em aposta, banca, ROI nem retorno. Se algum dia isso mudar,
      a correção teria que trocar o tipo dentro de `pernas` TAMBÉM, senão
      o casamento por (jogo_id, tipo_padrao, linha, direcao) quebra e a
      aposta fica "pendente" pra sempre.

DUAS FASES SEPARADAS (rodar uma de cada vez, de propósito):

  FASE 1 (`verificar`) - NÃO GRAVA NADA:
    Mostra quantas linhas serão afetadas e, pra cada uma, qual seria o
    resultado avaliado pela coluna CERTA vs. o que está salvo hoje.
    Serve pra você ver o tamanho do estrago antes de aplicar.

  FASE 2 (`aplicar`):
    1. Troca `tipo_padrao` de 'chute_total' pra 'chute_no_gol' nas linhas
       cuja descrição contém "chutes a gol".
    2. Reavalia essas linhas com `avaliacao.avaliar_resultado` (a mesma
       função de sempre - lógica de avaliação nunca deve existir em mais
       de um lugar), atualizando `resultado` só onde mudou.
    Ordem importa: trocar o tipo ANTES de reavaliar. Reavaliar primeiro só
    repetiria o mesmo erro, porque a avaliação lê o tipo salvo.

Rodar:
    python corrigir_tipo_chutes_a_gol.py verificar
    python corrigir_tipo_chutes_a_gol.py aplicar

Variáveis de ambiente necessárias: DATABASE_URL
"""

import os
import sys
import psycopg2

from avaliacao import avaliar_resultado

DATABASE_URL = os.environ["DATABASE_URL"]

# Filtro de identificação das linhas afetadas. A descrição é o único lugar
# onde a informação "é chute NO GOL" sobreviveu - o tipo_padrao salvo está
# errado justamente por isso. Só aqui a descrição é usada como critério, e
# só porque é uma correção pontual de dado legado: em nenhum outro lugar do
# projeto a descrição deve virar identidade de aposta (aprendizado antigo).
FILTRO_DESCRICAO = "%chutes a gol%"
TIPO_ERRADO = "chute_total"
TIPO_CERTO = "chute_no_gol"


def buscar_linhas_afetadas(cur):
    cur.execute(
        """
        SELECT h.id, h.jogo_id, h.jogador_id, h.descricao, h.linha, h.direcao,
               h.resultado, t.nome, j.adversario, j.data_jogo
        FROM historico_recomendacoes h
        LEFT JOIN jogos j ON j.id = h.jogo_id
        LEFT JOIN times t ON t.id = j.nosso_time_id
        WHERE h.tipo_padrao = %s
          AND h.descricao ILIKE %s
        ORDER BY h.id
        """,
        (TIPO_ERRADO, FILTRO_DESCRICAO),
    )
    return cur.fetchall()


def fase_verificar():
    """Só lê e simula. Nenhuma escrita no banco."""
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    try:
        linhas = buscar_linhas_afetadas(cur)
        if not linhas:
            print("Nenhuma linha afetada encontrada - nada a corrigir.")
            return

        print(f"{len(linhas)} linha(s) afetada(s).\n")
        print("=" * 78)
        print("SIMULAÇÃO (avaliando pela coluna CERTA, sem gravar nada)")
        print("=" * 78)

        mudariam = 0
        contagem_antes = {}
        contagem_depois = {}

        for (rec_id, jogo_id, jogador_id, descricao, linha, direcao,
             resultado_antigo, nosso_time, adversario, data_jogo) in linhas:

            # avaliar_resultado recebe o tipo CERTO direto - a simulação não
            # depende do UPDATE ter acontecido.
            novo_resultado = avaliar_resultado(
                cur, TIPO_CERTO, jogador_id, jogo_id, linha, descricao, direcao
            )

            contagem_antes[resultado_antigo] = contagem_antes.get(resultado_antigo, 0) + 1
            contagem_depois[novo_resultado] = contagem_depois.get(novo_resultado, 0) + 1

            if novo_resultado != resultado_antigo:
                mudariam += 1
                jogo_txt = f"{nosso_time} x {adversario} ({data_jogo})" if nosso_time else f"jogo {jogo_id}"
                print(f"  #{rec_id} {jogo_txt}")
                print(f"      {descricao}")
                print(f"      {resultado_antigo} -> {novo_resultado}")

        print("\n" + "=" * 78)
        print(f"{mudariam} de {len(linhas)} linha(s) mudariam de resultado.")
        print(f"\n  Antes:  {dict(sorted(contagem_antes.items()))}")
        print(f"  Depois: {dict(sorted(contagem_depois.items()))}")
        print("\nSe fizer sentido, rode:  python corrigir_tipo_chutes_a_gol.py aplicar")
        print("Lembrete: é esperado que a taxa de acerto PIORE - o número de hoje")
        print("está otimista porque avalia chute no gol usando chute total.")

    finally:
        cur.close()
        conn.close()


def fase_aplicar():
    """Troca o tipo e reavalia. Não toca em aposta/banca (confirmado que
    nenhuma aposta real usa esse mercado)."""
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        linhas = buscar_linhas_afetadas(cur)
        if not linhas:
            print("Nenhuma linha afetada encontrada - nada a corrigir.")
            return

        ids = [l[0] for l in linhas]

        # Passo 1: corrige o tipo. Precisa vir ANTES da reavaliação, senão
        # avaliar_resultado voltaria a ler o tipo errado.
        cur.execute(
            "UPDATE historico_recomendacoes SET tipo_padrao = %s WHERE id = ANY(%s)",
            (TIPO_CERTO, ids),
        )
        print(f"Passo 1: {cur.rowcount} linha(s) com tipo_padrao corrigido "
              f"('{TIPO_ERRADO}' -> '{TIPO_CERTO}').")

        # Passo 2: reavalia com a coluna certa.
        print("\nPasso 2: reavaliando...")
        mudou = 0
        for (rec_id, jogo_id, jogador_id, descricao, linha, direcao,
             resultado_antigo, _nosso_time, _adversario, _data_jogo) in linhas:

            novo_resultado = avaliar_resultado(
                cur, TIPO_CERTO, jogador_id, jogo_id, linha, descricao, direcao
            )
            if novo_resultado != resultado_antigo:
                cur.execute(
                    "UPDATE historico_recomendacoes SET resultado = %s WHERE id = %s",
                    (novo_resultado, rec_id),
                )
                print(f"  #{rec_id} \"{descricao}\": {resultado_antigo} -> {novo_resultado}")
                mudou += 1

        conn.commit()
        print(f"\n✅ Concluído e commitado. {mudou} de {len(linhas)} linha(s) "
              f"tiveram o resultado corrigido.")
        print("   Nenhuma aposta real, banca ou ROI foi tocada.")
        print("\n   O card 'Acertos e erros por probabilidade histórica' em /historico")
        print("   já reflete a correção na próxima vez que a página abrir (ele lê a")
        print("   tabela na hora, não tem cache).")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Erro, nada foi salvo (rollback): {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("verificar", "aplicar"):
        print("Uso:")
        print("  python corrigir_tipo_chutes_a_gol.py verificar   (só simula, não grava)")
        print("  python corrigir_tipo_chutes_a_gol.py aplicar     (corrige de verdade)")
        sys.exit(1)

    if sys.argv[1] == "verificar":
        fase_verificar()
    else:
        fase_aplicar()
