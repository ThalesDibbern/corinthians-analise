"""
Script PONTUAL (verificar/aplicar) - preenche a coluna `assinatura` das
linhas JÁ EXISTENTES em `historico_multiplas_destaque` (adicionada por
migrar_assinatura_multiplas_destaque.py, que precisa ter rodado antes).

PRA QUE SERVE:
    `arquivar_recomendacoes.py`/`selecionar_top5_do_jogo` só passou a
    gravar `assinatura` DAQUI PRA FRENTE. As linhas que já existiam no
    banco (144 na auditoria de 26/08/2026, todas ainda com detalhe
    completo - nenhuma comprimida) ficaram com `assinatura = NULL` até
    este script casar cada uma de volta com a candidata original em
    `multiplas_candidatas` (que tem `assinatura` com UNIQUE constraint
    desde sempre) e copiar o valor.

COMO CASA:
    `historico_multiplas_destaque` não guarda `pernas` (só `descricao`,
    texto já montado), então não dá pra recalcular o hash de
    `_assinatura_combo` direto - precisa achar a linha original em
    `multiplas_candidatas` por outro caminho. (`casa_aposta`, `descricao`,
    `odd_combinada`, `probabilidade_combinada`) foram copiados
    VERBATIM de `multiplas_candidatas` no momento da inserção (ver
    `selecionar_top5_do_jogo`), então casar pelos quatro juntos é
    confiável - a chance de duas candidatas DIFERENTES terem essa
    quádrupla idêntica é desprezível (descrição já inclui os mercados e
    ajustes específicos de cada perna).

    Se a quádrupla achar MAIS DE UMA candidata (ambíguo) ou NENHUMA (a
    candidata original não existe mais - não deveria acontecer, nada no
    projeto até hoje apaga `multiplas_candidatas`), a linha fica de fora
    e é listada à parte pra checar na mão. Não adivinha.

DUAS FASES:

  FASE 1 (`verificar`) - NÃO GRAVA NADA:
    Mostra quantas linhas seriam preenchidas, quantas ficariam
    ambíguas/sem match, e o tamanho do efeito colateral esperado: quantas
    combinações duplicadas (mesma assinatura em jogo_id diferentes)
    existem hoje - é esse número que deve sumir da lista de /historico
    depois que a leitura em app.py (já deduplicada) entrar no ar.

  FASE 2 (`aplicar`):
    Faz o UPDATE de verdade, numa transação só.

Rodar:
    python corrigir_duplicata_multiplas_destaque.py verificar
    python corrigir_duplicata_multiplas_destaque.py aplicar

Variáveis de ambiente necessárias: DATABASE_URL
"""

import os
import sys
from datetime import datetime

import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]


def imprimir_cabecalho(fase):
    print("=" * 78)
    print("corrigir_duplicata_multiplas_destaque.py")
    print(f"  fase:   {fase}")
    print(f"  argv:   {sys.argv}")
    print(f"  agora:  {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 78)


def buscar_linhas_sem_assinatura(cur):
    cur.execute(
        """SELECT id, jogo_id, casa_aposta, descricao, odd_combinada, probabilidade_combinada
           FROM historico_multiplas_destaque
           WHERE assinatura IS NULL AND casa_aposta IS NOT NULL
           ORDER BY id"""
    )
    return cur.fetchall()


def achar_assinatura_candidata(cur, casa_aposta, descricao, odd_combinada, probabilidade_combinada):
    """Casa (casa_aposta, descricao, odd_combinada, probabilidade_combinada)
    com multiplas_candidatas. Devolve (assinatura, None) se achar exatamente
    uma, (None, 'ambiguo') se achar mais de uma, (None, 'sem_match') se não
    achar nenhuma."""
    cur.execute(
        """SELECT assinatura FROM multiplas_candidatas
           WHERE casa_aposta = %s AND descricao = %s
             AND odd_combinada = %s AND probabilidade_combinada = %s""",
        (casa_aposta, descricao, odd_combinada, probabilidade_combinada),
    )
    candidatos = cur.fetchall()
    if len(candidatos) == 1:
        return candidatos[0][0], None
    if len(candidatos) == 0:
        return None, "sem_match"
    return None, "ambiguo"


def diagnosticar(cur):
    linhas = buscar_linhas_sem_assinatura(cur)
    encontradas = []
    sem_match = []
    ambiguas = []

    for (rec_id, jogo_id, casa, descricao, odd, prob) in linhas:
        assinatura, problema = achar_assinatura_candidata(cur, casa, descricao, odd, prob)
        if assinatura:
            encontradas.append((rec_id, jogo_id, descricao, assinatura))
        elif problema == "ambiguo":
            ambiguas.append((rec_id, jogo_id, descricao))
        else:
            sem_match.append((rec_id, jogo_id, descricao))

    return encontradas, sem_match, ambiguas


def contar_duplicatas_previstas(encontradas):
    """Quantas combinações (por assinatura) aparecem em mais de uma linha -
    esse número de linhas EXTRAS é o que deve sumir da lista visível de
    /historico depois da correção."""
    from collections import Counter
    contagem = Counter(assinatura for (_id, _jogo, _desc, assinatura) in encontradas)
    linhas_extras = sum(c - 1 for c in contagem.values() if c > 1)
    combinacoes_duplicadas = sum(1 for c in contagem.values() if c > 1)
    return combinacoes_duplicadas, linhas_extras


def fase_verificar():
    imprimir_cabecalho("verificar")
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    try:
        encontradas, sem_match, ambiguas = diagnosticar(cur)
        total = len(encontradas) + len(sem_match) + len(ambiguas)

        print(f"\n{total} linha(s) sem assinatura (com detalhe completo) no total.")
        print(f"  Casadas com sucesso:      {len(encontradas)}")
        print(f"  Sem match em candidatas:  {len(sem_match)}")
        print(f"  Ambíguas (2+ candidatas): {len(ambiguas)}")

        if encontradas:
            combinacoes_duplicadas, linhas_extras = contar_duplicatas_previstas(encontradas)
            print(f"\n{combinacoes_duplicadas} combinação(ões) aparecem em mais de uma linha "
                  f"(cross-game) - {linhas_extras} linha(s) extra(s) que devem sumir da lista "
                  "visível de /historico depois que a leitura deduplicada entrar no ar.")

        if sem_match:
            print("\n" + "=" * 78)
            print("SEM MATCH (checar na mão - candidata original não achada)")
            print("=" * 78)
            for rec_id, jogo_id, descricao in sem_match:
                print(f"  #{rec_id} jogo {jogo_id} - {descricao}")

        if ambiguas:
            print("\n" + "=" * 78)
            print("AMBÍGUAS (checar na mão - mais de uma candidata bateu)")
            print("=" * 78)
            for rec_id, jogo_id, descricao in ambiguas:
                print(f"  #{rec_id} jogo {jogo_id} - {descricao}")

        print("\nSe fizer sentido, rode:  python corrigir_duplicata_multiplas_destaque.py aplicar")

    finally:
        cur.close()
        conn.close()


def fase_aplicar():
    imprimir_cabecalho("aplicar")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        encontradas, sem_match, ambiguas = diagnosticar(cur)

        if not encontradas:
            print("\nNenhuma linha casada com sucesso - nada a gravar.")
            cur.close()
            conn.close()
            return

        for rec_id, _jogo_id, _descricao, assinatura in encontradas:
            cur.execute(
                "UPDATE historico_multiplas_destaque SET assinatura = %s WHERE id = %s",
                (assinatura, rec_id),
            )

        conn.commit()
        print(f"\n✅ Concluído e commitado. {len(encontradas)} linha(s) tiveram a "
              "assinatura preenchida.")
        if sem_match or ambiguas:
            print(f"⚠️  {len(sem_match)} sem match + {len(ambiguas)} ambígua(s) continuam "
                  "com assinatura NULL - a leitura deduplicada trata cada uma como única "
                  "(nunca colide com outra NULL), então elas não geram falso-dedup, mas "
                  "também não deixam de aparecer duplicadas SE forem parte de algum combo "
                  "cross-game. Checar na mão a lista impressa na fase verificar.")

    except Exception:
        conn.rollback()
        print("\n❌ Erro no meio da aplicação - rollback, nada foi gravado.")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("verificar", "aplicar"):
        sys.exit("Uso: python corrigir_duplicata_multiplas_destaque.py [verificar|aplicar]")

    if sys.argv[1] == "verificar":
        fase_verificar()
    else:
        fase_aplicar()
