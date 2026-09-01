"""
Correção pontual, 2 fases (verificar / aplicar): reavalia as linhas de
`historico_recomendacoes` dos 4 jogos que tiveram cartão/gol/substituição
regravados em 01/09/2026 (`corrigir_eventos_defasados_01_09.py`).

Por que precisa disso: `historico_recomendacoes.resultado` só é reavaliado
pelo fluxo normal (`resolver_pendentes`, em arquivar_recomendacoes.py)
enquanto ainda está 'pendente'. Uma vez que já virou 'acertou'/'errou',
fica congelado pra sempre - é o mesmo buraco que já existia pra
`apostas_salvas` antes do `reabrir_apostas_desatualizadas.py` (seção 37-B),
só que ninguém tinha escrito o equivalente pra `historico_recomendacoes`
em si. Os 4 jogos abaixo já estavam arquivados com `resultado` calculado
em cima do cartão/gol ERRADO - sem esse script, o CSV de "Odds por Rodada"
continua mostrando os números velhos mesmo depois da correção.

Reaproveita `avaliar_resultado` de avaliacao.py - única fonte da lógica de
avaliação, não duplica nada daqui.

Jogos (jogo_id, um por perspectiva):
  944, 1492352   -> Bahia x Internacional
  270, 1492356   -> Mirassol x Palmeiras
  1296, 1121     -> São Paulo x RB Bragantino
  1492349, 1216  -> Vitória x Bahia

Uso:
  python reavaliar_historico_eventos_corrigidos.py verificar
  python reavaliar_historico_eventos_corrigidos.py aplicar
"""

import os
import sys
from datetime import datetime

import psycopg2

from avaliacao import avaliar_resultado

DATABASE_URL = os.environ["DATABASE_URL"]

JOGO_IDS = [944, 1492352, 270, 1492356, 1296, 1121, 1492349, 1216]


def cabecalho(fase):
    print("=" * 78)
    print(f"reavaliar_historico_eventos_corrigidos.py | fase: {fase}")
    print(f"argv: {sys.argv}")
    print(f"relógio: {datetime.now().isoformat()}")
    print("=" * 78)


def buscar_linhas(cur):
    cur.execute(
        """SELECT id, jogo_id, tipo_padrao, jogador_id, linha, descricao, direcao, resultado
           FROM historico_recomendacoes
           WHERE jogo_id = ANY(%s)
           ORDER BY jogo_id, id""",
        (JOGO_IDS,),
    )
    return cur.fetchall()


def fase_verificar():
    cabecalho("verificar (só leitura, nenhuma escrita)")
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    linhas = buscar_linhas(cur)
    print(f"\n{len(linhas)} linhas de historico_recomendacoes encontradas pros 4 jogos.\n")

    mudou = 0
    for rec_id, jogo_id, tipo_padrao, jogador_id, linha, descricao, direcao, resultado_antigo in linhas:
        novo_resultado = avaliar_resultado(cur, tipo_padrao, jogador_id, jogo_id, linha, descricao, direcao)
        if novo_resultado != resultado_antigo:
            mudou += 1
            print(f"  [MUDA] id={rec_id} jogo_id={jogo_id} {tipo_padrao} | "
                  f"{resultado_antigo!r} -> {novo_resultado!r} | {descricao}")

    print(f"\n{mudou} de {len(linhas)} linhas vão mudar de resultado.")
    print("Nenhuma escrita foi feita. Rode com 'aplicar' pra gravar de verdade.")

    cur.close()
    conn.close()


def fase_aplicar():
    cabecalho("aplicar (vai atualizar historico_recomendacoes.resultado)")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        linhas = buscar_linhas(cur)
        mudou = 0
        for rec_id, jogo_id, tipo_padrao, jogador_id, linha, descricao, direcao, resultado_antigo in linhas:
            novo_resultado = avaliar_resultado(cur, tipo_padrao, jogador_id, jogo_id, linha, descricao, direcao)
            if novo_resultado != resultado_antigo:
                cur.execute(
                    "UPDATE historico_recomendacoes SET resultado = %s WHERE id = %s",
                    (novo_resultado, rec_id),
                )
                mudou += 1
                print(f"  [MUDOU] id={rec_id} jogo_id={jogo_id} {tipo_padrao} | "
                      f"{resultado_antigo!r} -> {novo_resultado!r}")

        conn.commit()
        print(f"\n{mudou} linhas atualizadas e commitadas.")
        print("Lembrete: se alguma dessas linhas alimenta uma aposta salva já "
              "resolvida (apostas_salvas), rodar reabrir_apostas_desatualizadas.py "
              "em seguida pra propagar pra lá também.")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a aplicação, rollback feito: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("verificar", "aplicar"):
        print("Uso: python reavaliar_historico_eventos_corrigidos.py [verificar|aplicar]")
        sys.exit(1)

    if sys.argv[1] == "verificar":
        fase_verificar()
    else:
        fase_aplicar()
