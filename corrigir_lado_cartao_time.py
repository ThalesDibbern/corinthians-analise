"""
Script PONTUAL (não faz parte do encadeamento diário do cron) - reavalia
todas as recomendações do mercado de CARTÃO POR TIME que foram avaliadas
com o lado errado.

DE ONDE VEM O BUG:
    `avaliacao.py` decidia de qual lado somar os cartões traduzindo
    `jogos.mandante` (o mando REAL do jogo) num valor de `cartoes.lado`:

        lado = "mandante" if info_jogo[0] else "visitante"

    Só que as duas tabelas usam a palavra "lado" com significados
    diferentes:

      - `estatisticas_jogo.lado` = mandante/visitante REAL do jogo
      - `cartoes.lado`           = relativo ao NOSSO time
                                   ('mandante' = nosso time, sempre)

    A gravação está em popular_banco.salvar_eventos:

        lado = "mandante" if ev["team"]["id"] == nosso_time_api_id
               else "visitante"

    Resultado: toda vez que o nosso time era VISITANTE real, a avaliação
    somava os cartões do ADVERSÁRIO. Quando era mandante real, coincidia
    com o certo por acaso.

    `escanteio_time` faz a mesma tradução e está CORRETO, porque ele lê de
    `estatisticas_jogo`, onde `lado` é o mando real. É a armadilha das
    duas semânticas de `lado`, já registrada nos aprendizados do projeto.

COMO FOI DESCOBERTO:
    Auditoria da rodada de 22-24/08/2026 (fase 1 do protocolo), comparando
    as 399 recomendações contra as estatísticas reais (Sofascore/ge) dos
    10 jogos. Regra observada sem exceção: quando o time nomeado era o
    visitante real, o número avaliado era o do mandante.

      - 14 recomendações nomeando o mandante real: 14/14 corretas
      - 18 nomeando o visitante real: 7 divergiram e 11 coincidiram só
        porque os dois times tinham o mesmo número de cartões

    Ou seja, a exposição real é o conjunto das linhas de visitante, não
    só as 7 que apareceram. Por isso este script reavalia TODAS as linhas
    do mercado, não uma lista fixa.

O QUE ESTE SCRIPT **NÃO** PRECISA FAZER:
    Não mexe em `tipo_padrao` nem em nenhum dado coletado. O dado bruto
    em `cartoes` sempre esteve certo dos dois lados - a prova é que
    `cartao_total` (que soma o jogo inteiro, sem filtrar lado) bateu em
    100% dos casos na auditoria. O erro era só na LEITURA por time.

    Também não precisa recalcular probabilidade: `motor_padroes.py` já
    usava `c.lado = 'mandante'` fixo (o comportamento certo), então a
    frequência histórica nasceu correta. Diferente do bug do
    `chute_total`, aqui o impacto é SIMPLES (só avaliação), não duplo.

PRÉ-REQUISITO OBRIGATÓRIO:
    Subir o `avaliacao.py` corrigido ANTES de rodar a fase `aplicar`.
    Este script chama `avaliacao.avaliar_resultado` de propósito (lógica
    de avaliação nunca deve existir em mais de um lugar) - se o módulo
    ainda estiver com o bug, ele vai "corrigir" pro mesmo valor errado.
    A fase `verificar` avisa se detectar que o módulo ainda está antigo.

DUAS FASES SEPARADAS (rodar uma de cada vez, de propósito):

  FASE 1 (`verificar`) - NÃO GRAVA NADA:
    Para cada linha do mercado, mostra o valor que a avaliação está
    usando hoje, o valor correto, e se o resultado mudaria. Separa por
    mando real, pra deixar visível que o erro só atinge o lado visitante.

  FASE 2 (`aplicar`):
    Reavalia com `avaliacao.avaliar_resultado` e grava `resultado` só
    onde mudou.

Rodar:
    python corrigir_lado_cartao_time.py verificar
    python corrigir_lado_cartao_time.py aplicar

Variáveis de ambiente necessárias: DATABASE_URL
"""

import os
import sys
from datetime import datetime

import psycopg2

from avaliacao import avaliar_resultado

DATABASE_URL = os.environ["DATABASE_URL"]

# O mercado de cartão por TIME é identificado por tipo_padrao = 'cartao'
# COM jogador_id NULL. Com jogador_id preenchido é o mercado binário de
# cartão de JOGADOR, que nunca teve esse problema (filtra por jogador_id,
# não por lado).
TIPO_PADRAO = "cartao"


def buscar_linhas_afetadas(cur):
    """Todas as recomendações de cartão por TIME já avaliadas (acertou ou
    errou). Linhas 'pendente' ficam de fora: elas serão avaliadas pela
    primeira vez pelo cron normal, já com o código corrigido."""
    cur.execute(
        """
        SELECT h.id, h.jogo_id, h.jogador_id, h.descricao, h.linha, h.direcao,
               h.resultado, h.probabilidade_historica,
               t.nome AS nosso_time, j.adversario, j.data_jogo, j.mandante
        FROM historico_recomendacoes h
        JOIN jogos j ON j.id = h.jogo_id
        LEFT JOIN times t ON t.id = j.nosso_time_id
        WHERE h.tipo_padrao = %s
          AND h.jogador_id IS NULL
          AND h.resultado IN ('acertou', 'errou')
        ORDER BY j.data_jogo, h.id
        """,
        (TIPO_PADRAO,),
    )
    return cur.fetchall()


def contar_cartoes(cur, jogo_id, lado):
    cur.execute(
        "SELECT COUNT(*) FROM cartoes WHERE jogo_id = %s AND lado = %s",
        (jogo_id, lado),
    )
    return cur.fetchone()[0]


def modulo_ainda_bugado():
    """Checagem barata de segurança: lê o fonte do módulo de avaliação e
    procura a tradução antiga. Não é infalível, mas pega o caso comum de
    rodar o script antes de subir o avaliacao.py novo."""
    try:
        import avaliacao
        with open(avaliacao.__file__.replace(".pyc", ".py"), encoding="utf-8") as f:
            fonte = f.read()
    except Exception:
        return False
    # O trecho antigo aparece 1x em escanteio_time (legítimo, lá a
    # tradução é correta) e aparecia mais 1x no bloco de cartão (o bug).
    # 2 ou mais ocorrências = módulo ainda é a versão antiga.
    trecho_antigo = 'lado = "mandante" if info_jogo[0] else "visitante"'
    return fonte.count(trecho_antigo) >= 2


def cabecalho(fase):
    """Imprime, como PRIMEIRA linha da saída, qual fase está rodando e com
    que argumentos.

    Existe por um motivo prático: no Railway a fase é escolhida pelo
    Custom Start Command, trocar esse comando dispara um redeploy, e a
    aba Deployments passa a ter dois deploys parecidos. Já aconteceu de
    abrir o log do deploy ANTERIOR e concluir que o script não obedeceu.
    Com o cabeçalho, o log se identifica sozinho.

    O relógio serve pra separar duas execuções da mesma fase - o coletor
    de log do Railway embaralha a ordem das linhas, então o timestamp
    dele não é confiável pra isso."""
    print("=" * 92)
    print(f"corrigir_lado_cartao_time.py | FASE: {fase.upper()} | "
          f"iniciado em {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"argv recebido: {sys.argv}")
    if fase == "verificar":
        print("Esta fase NÃO grava nada no banco.")
    else:
        print("Esta fase GRAVA no banco (UPDATE em historico_recomendacoes.resultado).")
    print("=" * 92)


def fase_verificar():
    """Só lê e simula. Nenhuma escrita no banco."""
    cabecalho("verificar")
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    try:
        if modulo_ainda_bugado():
            print("⚠️  ATENÇÃO: o avaliacao.py carregado ainda parece ser a versão")
            print("    ANTIGA (com a tradução via jogos.mandante no bloco de cartão).")
            print("    Suba o arquivo corrigido antes de rodar `aplicar`.\n")

        linhas = buscar_linhas_afetadas(cur)
        if not linhas:
            print("Nenhuma recomendação de cartão por time avaliada - nada a corrigir.")
            return

        print(f"{len(linhas)} recomendação(ões) de cartão por TIME já avaliada(s).\n")
        print("=" * 92)
        print("SIMULAÇÃO (lendo o lado CERTO, sem gravar nada)")
        print("=" * 92)

        mudariam = 0
        por_mando = {True: [0, 0], False: [0, 0]}  # mandante_real -> [total, mudariam]
        contagem_antes = {}
        contagem_depois = {}

        for (rec_id, jogo_id, jogador_id, descricao, linha, direcao,
             resultado_antigo, prob, nosso_time, adversario, data_jogo, mandante) in linhas:

            lado_errado = "mandante" if mandante else "visitante"
            valor_usado = contar_cartoes(cur, jogo_id, lado_errado)
            valor_certo = contar_cartoes(cur, jogo_id, "mandante")

            novo_resultado = avaliar_resultado(
                cur, TIPO_PADRAO, jogador_id, jogo_id, linha, descricao, direcao
            )

            contagem_antes[resultado_antigo] = contagem_antes.get(resultado_antigo, 0) + 1
            contagem_depois[novo_resultado] = contagem_depois.get(novo_resultado, 0) + 1

            por_mando[bool(mandante)][0] += 1
            if novo_resultado != resultado_antigo:
                mudariam += 1
                por_mando[bool(mandante)][1] += 1
                mando_txt = "mandante real" if mandante else "VISITANTE real"
                print(f"  #{rec_id} {nosso_time} x {adversario} ({data_jogo}) [{mando_txt}]")
                print(f"      {descricao}")
                print(f"      cartões lidos: {valor_usado} (errado) -> {valor_certo} (certo)")
                print(f"      {resultado_antigo} -> {novo_resultado}   (prob. prevista {prob}%)")

        print("\n" + "=" * 92)
        print(f"{mudariam} de {len(linhas)} linha(s) mudariam de resultado.\n")
        print(f"  Nosso time era MANDANTE real: {por_mando[True][0]} linha(s), "
              f"{por_mando[True][1]} mudariam")
        print(f"  Nosso time era VISITANTE real: {por_mando[False][0]} linha(s), "
              f"{por_mando[False][1]} mudariam")
        print("\n  (o esperado é ZERO mudança no grupo mandante - se aparecer alguma,")
        print("   pare: significa que a causa não é só o lado, e sim dado faltando)")
        print(f"\n  Antes:  {dict(sorted(contagem_antes.items()))}")
        print(f"  Depois: {dict(sorted(contagem_depois.items()))}")
        print("\nSe fizer sentido, rode:  python corrigir_lado_cartao_time.py aplicar")

    finally:
        cur.close()
        conn.close()


def fase_aplicar():
    """Reavalia e grava só onde mudou. Não toca em dado coletado."""
    cabecalho("aplicar")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        if modulo_ainda_bugado():
            print("❌ O avaliacao.py carregado ainda é a versão ANTIGA.")
            print("   Suba o arquivo corrigido antes de aplicar. Nada foi alterado.")
            return

        linhas = buscar_linhas_afetadas(cur)
        if not linhas:
            print("Nenhuma recomendação de cartão por time avaliada - nada a corrigir.")
            return

        mudou = 0
        for (rec_id, jogo_id, jogador_id, descricao, linha, direcao,
             resultado_antigo, _prob, _nosso_time, _adversario, _data_jogo, _mandante) in linhas:

            novo_resultado = avaliar_resultado(
                cur, TIPO_PADRAO, jogador_id, jogo_id, linha, descricao, direcao
            )
            if novo_resultado != resultado_antigo:
                cur.execute(
                    "UPDATE historico_recomendacoes SET resultado = %s WHERE id = %s",
                    (novo_resultado, rec_id),
                )
                print(f"  #{rec_id} \"{descricao}\": {resultado_antigo} -> {novo_resultado}")
                mudou += 1

        conn.commit()
        print(f"\n✅ Concluído e commitado. {mudou} de {len(linhas)} linha(s) corrigida(s).")
        print("   Nenhum dado coletado foi tocado (o problema era só de leitura).")
        print("\n   ATENÇÃO: a tabela de calibração da seção 9 da documentação passa a")
        print("   estar desatualizada. Rode `refazer_tabela_calibracao.py` depois de")
        print("   terminar TODAS as correções da auditoria, não a cada uma.")
        print("\n   Se alguma aposta real (apostas_salvas) tiver perna desse mercado,")
        print("   ela é resolvida pelo caminho normal do app (casamento por")
        print("   jogo_id/tipo_padrao/linha/direcao contra historico_recomendacoes).")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Erro, nada foi salvo (rollback): {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    # A fase pode vir por argumento OU pela variável de ambiente FASE.
    # O argumento tem prioridade; a variável existe como caminho
    # alternativo pra quando o Custom Start Command estiver dando
    # trabalho (dá pra setar FASE=aplicar nas Variables do serviço e
    # deixar o comando como `python corrigir_lado_cartao_time.py`).
    if len(sys.argv) == 1 and os.environ.get("FASE"):
        sys.argv.append(os.environ["FASE"].strip().lower())

    if len(sys.argv) != 2 or sys.argv[1] not in ("verificar", "aplicar"):
        print("Uso:")
        print("  python corrigir_lado_cartao_time.py verificar   (só simula, não grava)")
        print("  python corrigir_lado_cartao_time.py aplicar     (corrige de verdade)")
        print("")
        print("Alternativas que não dependem do argumento:")
        print("  FASE=aplicar como variável de ambiente do serviço")
        print("  python -c \"import corrigir_lado_cartao_time as m; m.fase_aplicar()\"")
        print(f"(argv recebido: {sys.argv})")
        sys.exit(1)

    if sys.argv[1] == "verificar":
        fase_verificar()
    else:
        fase_aplicar()
