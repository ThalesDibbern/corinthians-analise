"""
Script PONTUAL (verificar/aplicar) - conserta pares de `jogos` já
existentes onde as DUAS perspectivas do mesmo jogo real (mesma data,
mesmo par mandante_id/visitante_id, nosso_time_id diferente) ficaram
DESALINHADAS em dois pontos:

  1. `adversario` (texto) - uma perspectiva pode ter o nome canônico de
     `times.nome`, a outra o texto cru que a API mandou naquela chamada
     específica (ex: "Sao Paulo" vs "Sao Paulo FC SP"). Isso quebra o
     agrupamento por nome do card "Jogos disponíveis" - o mesmo jogo
     real vira 2 cards.
  2. `fixture_id_api` - as duas perspectivas podem ter resolvido (ou
     sintetizado) um `fixture_id_api` DIFERENTE pro MESMO jogo real. É a
     identidade usada pra travar múltipla com pernas contraditórias do
     mesmo jogo (`combinacoes.identidade_jogo` / `chave_mercado_da_perna`
     - seção 11-B da documentação) - com fixture_id_api divergente entre
     as duas linhas, essa trava não reconhece que é o mesmo jogo.

Caso real que expôs os dois problemas (27/08/2026): RB Bragantino x São
Paulo de 29/08/2026 -
    jogo #1121 (perspectiva São Paulo): adversario='RB Bragantino', fixture_id_api=1492358 (real)
    jogo #1296 (perspectiva RB Bragantino): adversario='Sao Paulo FC SP', fixture_id_api=-524484861 (sintético)

`get_or_create_jogo` (atualizar_odds.py) já foi corrigido pra não deixar
isso acontecer de novo daqui pra frente - este script só conserta o que
já existe no banco.

REGRA DE DESEMPATE (quando as duas linhas de um par divergem):
  - `adversario`: sempre vira o nome canônico de `times.nome` do time
    que está do outro lado (nunca o texto cru de nenhuma das duas linhas
    - mesma regra que o `get_or_create_jogo` corrigido usa).
  - `fixture_id_api`: prioriza o valor POSITIVO (real, confirmado pela
    API-Football) se só uma das duas tiver; se as duas forem sintéticas
    (negativas) ou as duas forem reais mas diferentes (não deveria
    acontecer, mas por segurança), fica com a de MENOR id de jogo (a
    criada primeiro) e avisa pra checar na mão.

DUAS FASES:
  FASE 1 (`verificar`) - só lista os pares desalinhados, NÃO grava nada.
  FASE 2 (`aplicar`) - corrige de verdade, numa transação só, com
    rollback em qualquer erro.

Rodar:
    python corrigir_perspectivas_divergentes.py verificar
    python corrigir_perspectivas_divergentes.py aplicar

Variáveis de ambiente necessárias: DATABASE_URL
"""

import os
import sys
from datetime import datetime

import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]


def imprimir_cabecalho(fase):
    print("=" * 78)
    print("corrigir_perspectivas_divergentes.py")
    print(f"  fase:   {fase}")
    print(f"  argv:   {sys.argv}")
    print(f"  agora:  {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 78)


def buscar_pares(cur):
    """Encontra todo par de linhas em `jogos` que representam o MESMO
    jogo real (mesma data, mesmo par mandante_id/visitante_id, nosso_
    time_id diferente) - devolve só os pares onde adversario e/ou
    fixture_id_api DIVERGEM entre as duas linhas."""
    cur.execute(
        """
        SELECT a.id, a.nosso_time_id, a.adversario, a.fixture_id_api,
               b.id, b.nosso_time_id, b.adversario, b.fixture_id_api,
               a.data_jogo, a.mandante_id, a.visitante_id
        FROM jogos a
        JOIN jogos b
          ON a.data_jogo = b.data_jogo
         AND a.mandante_id = b.mandante_id
         AND a.visitante_id = b.visitante_id
         AND a.nosso_time_id < b.nosso_time_id
        WHERE a.mandante_id IS NOT NULL AND a.visitante_id IS NOT NULL
          AND (a.adversario != b.adversario OR a.fixture_id_api != b.fixture_id_api)
        ORDER BY a.data_jogo, a.id
        """
    )
    return cur.fetchall()


def nome_canonico(cur, time_id):
    cur.execute("SELECT nome FROM times WHERE id = %s", (time_id,))
    row = cur.fetchone()
    return row[0] if row else None


def decidir_correcao(cur, par):
    (id_a, nosso_time_a, adversario_a, fixture_a,
     id_b, nosso_time_b, adversario_b, fixture_b,
     data_jogo, mandante_id, visitante_id) = par

    # nome canônico de cada lado: o adversário de A é o nosso_time de B, e vice-versa
    nome_correto_a = nome_canonico(cur, nosso_time_b)
    nome_correto_b = nome_canonico(cur, nosso_time_a)

    # fixture_id_api: prioriza o positivo (real); se empate (as duas
    # positivas divergentes, ou as duas negativas), fica com o da linha
    # de MENOR id e avisa.
    aviso_fixture = None
    if fixture_a > 0 and fixture_b <= 0:
        fixture_correto = fixture_a
    elif fixture_b > 0 and fixture_a <= 0:
        fixture_correto = fixture_b
    elif fixture_a == fixture_b:
        fixture_correto = fixture_a
    else:
        fixture_correto = fixture_a if id_a < id_b else fixture_b
        tipo = "reais só que DIFERENTES" if fixture_a > 0 and fixture_b > 0 else "sintéticas"
        aviso_fixture = (
            f"jogo #{id_a} e #{id_b}: as duas fixture_id_api são {tipo} "
            f"({fixture_a} vs {fixture_b}) - fico com a do menor id (#{min(id_a, id_b)}), "
            "mas vale checar na mão se não é um caso mais estranho."
        )

    return {
        "id_a": id_a, "adversario_atual_a": adversario_a, "adversario_correto_a": nome_correto_a,
        "id_b": id_b, "adversario_atual_b": adversario_b, "adversario_correto_b": nome_correto_b,
        "fixture_atual_a": fixture_a, "fixture_atual_b": fixture_b, "fixture_correto": fixture_correto,
        "aviso_fixture": aviso_fixture,
    }


def fase_verificar():
    imprimir_cabecalho("verificar")
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    try:
        pares = buscar_pares(cur)
        if not pares:
            print("\nNenhum par desalinhado encontrado - nada a fazer.")
            return

        print(f"\n{len(pares)} par(es) desalinhado(s):\n")
        for par in pares:
            c = decidir_correcao(cur, par)
            print(f"  Jogo #{c['id_a']}: adversario '{c['adversario_atual_a']}' -> '{c['adversario_correto_a']}'"
                  f" | fixture_id_api {c['fixture_atual_a']} -> {c['fixture_correto']}")
            print(f"  Jogo #{c['id_b']}: adversario '{c['adversario_atual_b']}' -> '{c['adversario_correto_b']}'"
                  f" | fixture_id_api {c['fixture_atual_b']} -> {c['fixture_correto']}")
            if c["aviso_fixture"]:
                print(f"  ⚠️  {c['aviso_fixture']}")
            print()

        print("Se fizer sentido, rode: python corrigir_perspectivas_divergentes.py aplicar")

    finally:
        cur.close()
        conn.close()


def fase_aplicar():
    imprimir_cabecalho("aplicar")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        pares = buscar_pares(cur)
        if not pares:
            print("\nNenhum par desalinhado encontrado - nada a fazer.")
            cur.close()
            conn.close()
            return

        avisos = []
        for par in pares:
            c = decidir_correcao(cur, par)
            cur.execute(
                "UPDATE jogos SET adversario = %s, fixture_id_api = %s WHERE id = %s",
                (c["adversario_correto_a"], c["fixture_correto"], c["id_a"]),
            )
            cur.execute(
                "UPDATE jogos SET adversario = %s, fixture_id_api = %s WHERE id = %s",
                (c["adversario_correto_b"], c["fixture_correto"], c["id_b"]),
            )
            print(f"  Corrigido: jogo #{c['id_a']} e #{c['id_b']}")
            if c["aviso_fixture"]:
                avisos.append(c["aviso_fixture"])

        conn.commit()
        print(f"\n✅ Concluído e commitado. {len(pares)} par(es) corrigido(s).")
        if avisos:
            print(f"\n⚠️  {len(avisos)} caso(s) precisam de checagem manual:")
            for a in avisos:
                print(f"  - {a}")

    except Exception:
        conn.rollback()
        print("\n❌ Erro no meio da aplicação - rollback, nada foi gravado.")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("verificar", "aplicar"):
        sys.exit("Uso: python corrigir_perspectivas_divergentes.py [verificar|aplicar]")

    if sys.argv[1] == "verificar":
        fase_verificar()
    else:
        fase_aplicar()
