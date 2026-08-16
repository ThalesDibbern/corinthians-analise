"""
Script de limpeza pontual - junta jogadores duplicados (mesmo jogador
real, dois `id` diferentes na tabela `jogadores`) numa lista de pares
(fantasma, real). Reatribui todo o dado ligado ao fantasma pro registro
real (o que já tem api_football_id confirmado), apaga os padrões
calculados do fantasma (serão recriados do zero no próximo
motor_padroes.py, já combinando o histórico completo dos dois lados) e
remove o fantasma.

Roda tudo dentro de uma transação só - se qualquer par der erro, nada é
salvo (nenhum jogador fica pela metade).

Reaproveitável: pra juntar mais jogadores no futuro, só adicionar o par
(id_fantasma, id_real) na lista PARES_PARA_JUNTAR e rodar de novo - pares
já processados são detectados e pulados automaticamente (o fantasma já
não existe mais).

Variáveis de ambiente necessárias:
  - DATABASE_URL -> a URL de conexão do Postgres (mesma usada nos outros scripts)
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

# NOVO: pares (id_fantasma, id_real) - Grupo 1, só casos de ALTA
# CONFIANÇA, onde o nome bate EXATAMENTE depois de remover acento (não
# usa abreviação de primeiro nome, que tem risco maior de juntar duas
# pessoas diferentes por engano - esses ficam pra um Grupo 2, revisado
# manualmente antes de entrar aqui).
PARES_PARA_JUNTAR = [
    # Grupo 1 - alta confiança, só diferença de acento
    (2, 1140),      # Matheus Araújo / Matheus Araujo
    (16, 65),       # Róger Guedes / Roger Guedes
    (52, 12),       # Fágner / Fagner
    (96, 1289),     # Hércules / Hercules
    (113, 1137),    # Bruno Tubarão / Bruno Tubarao
    (121, 1185),    # Maurício / Mauricio
    (174, 168),     # Valdivia / Valdívia
    (175, 774),     # Joao Carlos / João Carlos
    (238, 801),     # Éverton Ribeiro / Everton Ribeiro
    (273, 1127),    # C. Pavón / C. Pavon
    (308, 759),     # Edenílson / Edenilson
    (312, 133),     # Éder / Eder
    (356, 139),     # Cassio / Cássio
    (128, 764),     # Igor Vinicius / Igor Vinícius

    # Grupo 2 - abreviação de primeiro nome, confirmado pelo dono do
    # projeto (11/08/2026) - risco maior que o Grupo 1, mas aceito
    (19, 4),        # F. Vera / Fausto Vera
    (38, 47),       # Joaquín Piquerez / J. Piquerez
    (39, 528),      # Á. Romero / Ángel Romero
    (51, 900),      # R. Rios / Richard Ríos
    (70, 606),      # R. Saravia / Renzo Saravia
    (84, 85),       # G. Gómez / Gustavo Gómez
    (88, 747),      # B. Kuscevic / Benjamin Kuscevic
    (102, 295),     # Brayan Ceballos / B. Ceballos
    (126, 529),     # J. Calleri / Jonathan Calleri
    (137, 136),     # Gabriel Neves / G. Neves
    (152, 327),     # Baralhas / Gabriel Baralhas
    (185, 672),     # I. Pitta / Isidro Pitta
    (193, 564),     # T. Cuello / Tomás Cuello
    (222, 1187),    # Ganso / PH Ganso
    (269, 272),     # Nacho Fernández / I. Fernández
    (279, 524),     # Fernando Marçal / Marçal
    (289, 1082),    # José López / J. Lopez
    (292, 1082),    # J. López / J. Lopez (mesmo par de 289, dedup automático via jogadores.id já removido)
    (291, 819),     # Tabata / Bruno Tabata
    (303, 1251),    # José Hurtado / José Andrés Hurtado
    (304, 305),     # Carlos De Pena / C. de Pena
    (306, 5),       # Victor Cantillo / V. Cantillo
    (310, 34),      # Fabián Balbuena / F. Balbuena
    (319, 324),     # Martín Benítez / M. Benítez
    (320, 23),      # Bruno Méndez / B. Méndez
    (329, 158),     # Diego Churin / D. Churín
    (343, 842),     # A. Canobbio / Agustín Canobbio
    (357, 221),     # German Cano / G. Cano
    (360, 796),     # Martinelli / Matheus Martinelli
]


def juntar_jogador(cur, id_fantasma, id_real):
    cur.execute("UPDATE jogador_estatisticas_jogo SET jogador_id = %s WHERE jogador_id = %s", (id_real, id_fantasma))
    cur.execute("UPDATE cartoes SET jogador_id = %s WHERE jogador_id = %s", (id_real, id_fantasma))
    cur.execute("UPDATE odds SET jogador_id = %s WHERE jogador_id = %s", (id_real, id_fantasma))
    cur.execute("UPDATE recomendacoes SET jogador_id = %s WHERE jogador_id = %s", (id_real, id_fantasma))
    cur.execute("UPDATE historico_recomendacoes SET jogador_id = %s WHERE jogador_id = %s", (id_real, id_fantasma))
    cur.execute(
        "UPDATE multiplas_candidatas SET jogos = REPLACE(jogos::text, %s, %s)::jsonb WHERE jogos::text LIKE %s",
        (f'"jogador_id": {id_fantasma}', f'"jogador_id": {id_real}', f'%"jogador_id": {id_fantasma}%'),
    )
    cur.execute("DELETE FROM padroes_jogador_linha WHERE jogador_id = %s", (id_fantasma,))
    cur.execute("DELETE FROM padroes_jogador_cartao WHERE jogador_id = %s", (id_fantasma,))
    cur.execute("DELETE FROM padroes_jogador_frequencia WHERE jogador_id = %s", (id_fantasma,))
    cur.execute("DELETE FROM padroes_correlacao_jogador WHERE jogador_id = %s", (id_fantasma,))
    cur.execute("DELETE FROM jogadores WHERE id = %s", (id_fantasma,))


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        processados = 0
        for id_fantasma, id_real in PARES_PARA_JUNTAR:
            cur.execute("SELECT nome FROM jogadores WHERE id = %s", (id_fantasma,))
            row_f = cur.fetchone()
            cur.execute("SELECT nome, api_football_id FROM jogadores WHERE id = %s", (id_real,))
            row_r = cur.fetchone()
            if not row_f or not row_r:
                print(f"  PULADO ({id_fantasma} -> {id_real}): um dos dois já não existe mais (par já processado antes?)")
                continue
            print(f"  Juntando '{row_f[0]}' (id {id_fantasma}) -> '{row_r[0]}' (id {id_real}, api_football_id {row_r[1]})")
            juntar_jogador(cur, id_fantasma, id_real)
            processados += 1

        conn.commit()
        print(f"\nConcluído! {processados} par(es) juntados de verdade "
              f"(de {len(PARES_PARA_JUNTAR)} na lista).")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a limpeza - nada foi salvo: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
