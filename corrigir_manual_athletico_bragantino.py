"""
Script PONTUAL (não faz parte do cron) - corrige manualmente a estatística
de TIME do jogo Athletico-PR x RB Bragantino (15/08/2026, fixture_id_api
1492330), sem depender de mais uma tentativa da API-Football (que já
confirmamos, pela 2ª vez, que ainda devolve o dado parcial/incompleto de
antes - ver conversa de 17/08/2026).

Números confirmados via Sofascore (posse, faltas, chutes/finalizações -
citado pelo BemParaná) e Ogol (escanteios), 17/08/2026:
  Athletico-PR (mandante): posse 52%, escanteios 8, faltas 6, chutes 20
  RB Bragantino (visitante): posse 48%, escanteios 5, faltas 8, chutes 15

NÃO mexe em `passes` (deixado como está - nenhum mercado/recomendação do
sistema usa esse dado hoje, não vale o risco de inventar um número sem
fonte confiável) nem em estatística de JOGADOR (jogador_estatisticas_jogo -
44 linhas, sem fonte jogador-a-jogador confiável disponível; fica como
pendência separada pra quando tiver mais tempo, sem fila de outras coisas
pra fazer).

Só corrige `estatisticas_jogo` - depois de rodar esse script, ainda falta
rodar a FASE 2 (reavaliar) do corrigir_estatisticas_pontual.py já existente
(reaproveitando ele, sem duplicar lógica):
    python corrigir_estatisticas_pontual.py reavaliar

Rodar manualmente, uma vez:
    python corrigir_manual_athletico_bragantino.py

Variáveis de ambiente necessárias:
  - DATABASE_URL
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

# fixture_id_api 1492330 -> as 2 linhas em `jogos` que representam esse
# jogo real (Athletico-PR rastreado E RB Bragantino rastreado - por isso
# tem 2 perspectivas salvas).
JOGOS_PARA_CORRIGIR = [34, 1492330]

# Números confirmados via Sofascore/Ogol - ver docstring acima.
ESTATISTICA_CORRETA = {
    "mandante": {  # Athletico-PR
        "posse_de_bola": 52.0,
        "escanteios": 8,
        "faltas": 6,
        "finalizacoes": 20,
    },
    "visitante": {  # RB Bragantino
        "posse_de_bola": 48.0,
        "escanteios": 5,
        "faltas": 8,
        "finalizacoes": 15,
    },
}


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        for jogo_id in JOGOS_PARA_CORRIGIR:
            print(f"\n===== jogo_id {jogo_id} =====")
            for lado, valores in ESTATISTICA_CORRETA.items():
                cur.execute(
                    """
                    UPDATE estatisticas_jogo
                    SET posse_de_bola = %s, escanteios = %s, faltas = %s, finalizacoes = %s
                    WHERE jogo_id = %s AND lado = %s
                    """,
                    (
                        valores["posse_de_bola"], valores["escanteios"],
                        valores["faltas"], valores["finalizacoes"],
                        jogo_id, lado,
                    ),
                )
                if cur.rowcount == 0:
                    print(f"  ⚠️  Nenhuma linha encontrada pra jogo_id={jogo_id}, lado={lado} - "
                          "confere se o jogo_id/lado estão certos.")
                else:
                    print(f"  {lado}: posse={valores['posse_de_bola']}% escanteios={valores['escanteios']} "
                          f"faltas={valores['faltas']} chutes={valores['finalizacoes']} - atualizado.")

        conn.commit()
        print("\n✅ Correção manual concluída e commitada.")
        print("Próximo passo: python corrigir_estatisticas_pontual.py reavaliar")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a correção: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
