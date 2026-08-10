"""
Motor de combinações - gera as múltiplas (2+ pernas) a partir das
recomendações ativas (mesma lógica que a página principal usa, importada
de combinacoes.py pra nunca divergir) e captura cada uma em
`multiplas_candidatas`, abastecendo o ranking de "Múltiplas em Destaque"
do /historico MESMO SEM ninguém abrir o site.

Roda como o último passo do refreshing-freedom, depois de
atualizar_odds.py e motor_recomendacoes.py (precisa das odds e das
recomendações individuais já atualizadas). Também roda implicitamente
quando alguém clica "Atualizar recomendações" na página principal - o
app.py chama capturar_candidatas_multiplas() direto, com o resultado que
já calculou pra mostrar na tela, sem precisar rodar esse arquivo de novo.

Duas responsabilidades, nessa ordem:
1) Captura: grava/atualiza cada múltipla gerada em `multiplas_candidatas`
   (upsert por assinatura - ver combinacoes.py). Usa uma faixa de odd bem
   larga (1.01 a 1000) pra capturar o máximo de combinações válidas
   possível, já que aqui não existe "faixa que o usuário pediu" - o
   objetivo é abastecer o ranking, não responder um clique específico.
2) Congelamento: marca como congeladas as candidatas cujo jogo mais
   próximo já começou - a partir daí, a odd/probabilidade guardada é
   definitiva ("a última geração antes do apito").

A seleção do top-5 por jogo e a avaliação de acerto/erro acontecem
DEPOIS, em arquivar_recomendacoes.py, quando o jogo já terminou e o
resultado de cada perna já é conhecido.

Variáveis de ambiente necessárias:
  - DATABASE_URL -> a URL de conexão do Postgres (mesma usada nos outros scripts)
"""

import os
import psycopg2

from combinacoes import (
    buscar_recomendacoes, montar_combinacoes,
    capturar_candidatas_multiplas, congelar_candidatas_vencidas,
)

DATABASE_URL = os.environ["DATABASE_URL"]

# NOVO: faixa de odd bem larga - esse script não responde a um pedido
# específico de usuário (não tem "odd mínima/máxima" pra respeitar), o
# objetivo é capturar o máximo de combinações válidas (VE > 0) possível
# pra abastecer o ranking. Os limites internos de montar_combinacoes
# (redução de linhas por mercado, tamanho adaptativo, teto de 300 por
# casa) continuam protegendo a performance normalmente.
ODD_MINIMA_CAPTURA = 1.01
ODD_MAXIMA_CAPTURA = 1000.0


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        print("Buscando recomendações ativas...")
        recomendacoes = buscar_recomendacoes(cur)

        if not recomendacoes:
            print("Nenhuma recomendação ativa no momento - nada pra combinar.")
        else:
            print(f"{len(recomendacoes)} recomendação(ões) ativa(s) encontrada(s). Montando combinações...")
            combinacoes_geradas = montar_combinacoes(recomendacoes, ODD_MINIMA_CAPTURA, ODD_MAXIMA_CAPTURA)
            multiplas = [c for c in combinacoes_geradas if len(c["pernas"]) > 1]

            if not multiplas:
                print("Nenhuma múltipla de valor encontrada no momento.")
            else:
                capturadas = capturar_candidatas_multiplas(cur, multiplas)
                conn.commit()
                print(f"Concluído! {capturadas} múltipla(s) candidata(s) capturada(s)/atualizada(s).")

        congeladas = congelar_candidatas_vencidas(cur)
        conn.commit()
        if congeladas:
            print(f"{congeladas} candidata(s) congelada(s) (jogo mais próximo já começou).")
        else:
            print("Nenhuma candidata nova pra congelar no momento.")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
