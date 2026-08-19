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

# NOVO (correção após crash): tabelas de padrão CALCULADO (não dado real)
# - o conteúdo é sempre derivado/recalculável, então o fantasma só precisa
# ser apagado daqui, nunca reatribuído.
PREFIXOS_PARA_DELETAR = ("padroes_",)

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

    # ------------------------------------------------------------------
    # Grupo 3 - detecção feita depois da ativação dos 10 times restantes
    # do Brasileirão (18/08/2026), que dobrou o número de elencos no banco.
    #
    # Critério de entrada: os DOIS lados têm estatística real (histórico
    # partido de verdade, que distorce probabilidade na tela - órfãos com
    # 0 linhas foram deixados de fora por não afetarem cálculo nenhum), e
    # o par é o MESMO jogador com certeza (só diferença de acento ou
    # abreviação de primeiro nome, sem nenhum outro jogador plausível
    # concorrendo pelo mesmo nome).
    #
    # Direção: o fantasma é sempre o registro SEM api_football_id (nasceu
    # do lado da OddsPapi, por nome); o real é o que tem api_football_id
    # confirmado. Mantém a identidade por ID, nunca por texto.
    #
    # Pares AMBÍGUOS ficaram DE FORA de propósito - ver lista comentada
    # no fim deste bloco.
    # ------------------------------------------------------------------
    (405, 1073),    # Tchê Tchê / Tche Tche
    (811, 1127),    # Cristian Pavón / C. Pavon
    (539, 221),     # Germán Cano / G. Cano
    (797, 226),     # Jhon Arias / J. Arias
    (910, 533),     # Giorgian De Arrascaeta / G. de Arrascaeta
    (758, 1084),    # Renê / Rene
    (401, 1120),    # Júnior Santos / Junior Santos
    (866, 1177),    # Michel Araújo / Michel Araujo
    (906, 517),     # Franco Cristaldo / F. Cristaldo
    (757, 663),     # Gabriel Mercado / G. Mercado
    (417, 17),      # Matheus Bidú / Matheus Bidu
    (536, 1096),    # Luiz Araújo / Luiz Araujo
    (563, 438),     # Yeferson Soteldo / Y. Soteldo
    (459, 1069),    # Nicolás Acevedo / Nicolas Acevedo
    (927, 589),     # Pablo Vegetti / P. Vegetti
    (569, 623),     # Henry Mosquera / H. Mosquera
    (849, 369),     # Erick Pulgar / E. Pulgar
    (630, 1066),    # Cacá / Caca
    (941, 645),     # Jefferson Savarino / J. Savarino
    (881, 450),     # Thiago Borbas / T. Borbas
    (983, 733),     # Kevin Serna / K. Serna
    (860, 900),     # Richard Rios / Richard Ríos
    (919, 1254),    # Lucas Esquivel / L. Esquivel
    (920, 565),     # Bruno Zapelli / B. Zapelli
    (609, 367),     # Pedro Raúl / Pedro Raul
    (756, 1381),    # Fabricio Bustos / F. Bustos
    (808, 257),     # Luciano Castan / Luciano Castán
    (1020, 1016),   # Damián Bobadilla / D. Bobadilla
    (447, 349),     # Madson / Mádson
    (406, 704),     # Víctor Cuesta / V. Cuesta
    (386, 275),     # Matías Zaracho / M. Zaracho
    (1018, 1147),   # André Silva / Andre Silva
    (1035, 1036),   # Aníbal Moreno / A. Moreno
    (894, 480),     # Enner Valencia / E. Valencia
    (635, 1165),    # Zé Welison / Ze Welison
    (1003, 996),    # André Carrillo / A. Carrillo
    (914, 1164),    # Emmanuel Martínez / Emmanuel Martinez
    (390, 276),     # Eduardo Vargas / E. Vargas
    (523, 1131),    # André Henrique / Andre Henrique
    (971, 696),     # Yannick Bolasie / Y. Bolasie
    (831, 322),     # Raúl Cáceres / R. Cáceres
    (1063, 1125),   # Martin Braithwaite / M. Braithwaite
    (457, 1501),    # Rodrigo Battaglia / R. Battaglia
    (861, 398),     # Tomás Pochettino / T. Pochettino
    (981, 729),     # Facundo Bernal / F. Bernal
    (833, 321),     # Gonzalo Mastriani / G. Mastriani
    (1047, 1210),   # Lautaro Díaz / L. Diaz
    (973, 702),     # Santiago Arias / S. Arias
    (785, 190),     # David Terans / D. Terans
    (1012, 1006),   # Memphis Depay / M. Depay
    (612, 1172),    # Caique Gonçalves / Caique Goncalves
    (783, 1400),    # Nicolás Hernández / N. Hernández
    (902, 512),     # Juan Martín Lucero / J. Lucero
    (760, 122),     # Alexandre Alemão / Alemão
    (882, 1500),    # Mauricio Lemos / M. Lemos
    (1061, 1059),   # Miguel Monsalve / M. Monsalve
    (987, 1072),    # José Martínez / J. Martinez
    (934, 625),     # Ignacio Laquintana / I. Laquintana
    (370, 1098),    # Matheus Gonçalves / Matheus Goncalves
    (581, 521),     # Luis Suárez / L. Suárez
    (921, 1530),    # Felipe Carballo / F. Carballo
    (965, 690),     # Juan Sforza / J. Sforza
    (863, 526),     # Leonel Di Plácido / L. Di Plácido
    (381, 1106),    # Fabrício Daniel / Fabricio Daniel
    (896, 1513),    # Marcelino Moreno / M. Moreno
    (754, 1378),    # Leonardo Realpe / L. Realpe
    (664, 478),     # Charles Aránguiz / C. Aránguiz
    (975, 1572),    # Agustín Marchesín / A. Marchesín
    (850, 416),     # Arturo Vidal / A. Vidal
    (738, 68),      # Luís Oyama / Luis Oyama
    (907, 522),     # João Pedro Galvão / J. Galvão
    (617, 1174),    # Nenê / Nene
    (911, 540),     # Yony González / Y. González
    (748, 90),      # Eduard Atuesta / E. Atuesta
    (828, 1444),    # Giuliano Galoppo / G. Galoppo
    (889, 1507),    # Gary Medel / G. Medel
    (918, 561),     # Julio Furch / J. Furch
    (864, 403),     # Matías Segovia / M. Segovia
    (1031, 1556),   # Mateo Gamarra / M. Gamarra
    (765, 138),     # Emiliano Rigoni / E. Rigoni
    (923, 586),     # Camilo Cándido / C. Cándido
    (669, 668),     # Lucas Di Yorio / L. Di Yorio
    (912, 541),     # Leonardo Fernández / L. Fernández
    (892, 470),     # Luca Orellano / L. Orellano
    (1051, 1052),   # Pablo Galdames / P. Galdames
    (395, 98),      # Silvio Romero / S. Romero
    (976, 711),     # Matías Arezo / M. Arezo
    (1048, 1044),   # Kaique Kenji / Kenji
    (885, 464),     # Vinicius Mingotti / Vinícius Mingotti
    (437, 255),     # Stiven Mendoza / S. Mendoza

    # ------------------------------------------------------------------
    # Grupo 4 - segunda passada de detecção (18/08/2026), rodada DEPOIS
    # do Grupo 3 já ter sido aplicado. Não são casos novos: estavam na
    # mesma varredura, mas ficaram escondidos atrás dos pares de volume
    # alto. Mesmo critério de entrada e mesma direção do Grupo 3.
    # ------------------------------------------------------------------
    (938, 1550),    # Damián Suárez / D. Suárez
    (1000, 994),    # Thiago Almada / T. Almada
    (384, 1468),    # Jhon Chancellor / J. Chancellor
    (853, 1469),    # Jesús Trindade / J. Trindade
    (822, 309),     # Braian Romero / B. Romero
    (945, 650),     # Alejo Cruz / A. Cruz
    (755, 1379),    # Kevin Lomónaco / K. Lomónaco
    (562, 460),     # Matías Rojas / M. Rojas
    (897, 487),     # Diogo Oliveira / Diogo de Oliveira
    (903, 514),     # Imanol Machuca / I. Machuca
    (31, 32),       # Julián Palacios / J. Palacios
    (659, 660),     # Lucas Alario / L. Alario
    (841, 344),     # Luis Orejuela / L. Orejuela
    (993, 988),     # Carlos Alcaraz / C. Alcaraz
    (968, 1567),    # Wilker Ángel / W. Ángel
    (893, 474),     # Gabriel Carabajal / G. Carabajal
    (891, 592),     # Sebastián Ferreira / S. Ferreira
    (807, 1424),    # Guillermo De Los Santos / G. De Los Santos
    (922, 576),     # Lucas Besozzi / L. Besozzi
    (986, 1067),    # Héctor Hernández / H. Hernandez
    (857, 14),      # Rafael Bilú / Rafael Bilu
    (496, 318),     # Léo Mana / Leo Mana
    (917, 558),     # Maximiliano Silvera / M. Silvera
    (954, 44),      # Gustavo García / Gustavo Garcia
    (898, 1516),    # Helibelton Palacios / H. Palacios
    (880, 446),     # Luciano Arriagada / L. Arriagada
    (877, 434),     # Pablo Ceppelini / P. Ceppelini
    (972, 701),     # Miguel Trauco / M. Trauco
    (887, 591),     # Manuel Capasso / M. Capasso
    (782, 186),     # Óscar Ruiz / Ó. Ruíz
    (1046, 1041),   # Fabrizio Peralta / F. Peralta
    (791, 1408),    # Emiliano Velázquez / E. Velázquez
    (805, 252),     # Michel Macedo / Míchel Macedo
    (829, 313),     # Nahuel Bustos / N. Bustos
    (827, 314),     # Andres Colorado / A. Colorado
    (1004, 1011),   # Gonzalo Freitas / G. Freitas
    (804, 247),     # Cléber / Cleber
    (984, 1579),    # Tomás Cardona / T. Cardona
    (837, 332),     # Kelvin Osorio / K. Osorio
    (979, 719),     # Ronie Carrillo / R. Carrillo
    (798, 230),     # Mario Pineida / M. Pineida
    (751, 101),     # Valentin Depietri / V. Depietri
    (852, 374),     # Jhon Vásquez / J. Vásquez
    (915, 543),     # Juan Cazares / J. Cazares
    (1021, 1019),   # Jamal Lewis / J. Lewis
    (1014, 1009),   # Matías Lacava / M. Lacava
    (1005, 1010),   # Joel Campbell / J. Campbell
    (875, 432),     # Nicolás Quagliata / N. Quagliata
    (818, 286),     # Paolo Guerrero / P. Guerrero
    (744, 73),      # Jonathan Copete / J. Copete

    # ------------------------------------------------------------------
    # DELIBERADAMENTE FORA - pares que a detecção sugeriu mas que NÃO
    # devem ser juntados. Mantidos aqui como registro pra ninguém (nem eu
    # no futuro) achar que foram esquecidos por descuido:
    #
    # PESSOAS DIFERENTES (sobrenome/apelido em comum, só isso):
    #   908 James Rodríguez      x 1077 José Luis Rodríguez
    #   299 Jadsom Silva         x  737 Jonathan Silva
    #   737 Jonathan Silva       x 1111 Jemmes Bruno Ribeiro Da Silva
    #   737 Jonathan Silva       x 2186 João Pedro Silva
    #   737 Jonathan Silva       x 1095 João Silva
    #   737 Jonathan Silva       x 1967 João Vitor Silva
    #   866 Michel Araújo        x 1140 Matheus Araujo
    #    66 Raul Gustavo         x  680 Ryan Gustavo
    #   944 Emiliano Rodriguez   x 1053 Emerson Rodríguez
    #   929 Thalisson Gabriel    x 1593 Tevis Gabriel
    #   777 Marcão Silva         x 1426 Marcio Silva
    #   957 Luan Santos          x 1880 Leonardo Santos
    #   957 Luan Santos          x 2153 Lucas Matheus Ferreira dos Santos
    #   807 Guillermo De Los Santos x 1762 Guilherme Santos
    #
    # AMBÍGUOS - existe MAIS DE UM jogador real plausível pro mesmo
    # registro fantasma, então juntar é chute. Precisam de conferência
    # jogador a jogador (escalação/time/temporada) antes de decidir:
    #   814 Luís Henrique  -> pode ser 639 (Luiz Henrique) OU 281 (Luis Henrique)
    #   776 André Luís     -> pode ser 163 (André Luis) OU 1305 (Andre Luis)
    #   267 Adrián Martínez-> casa com 259 (A. Martínez) E com 1928 (A. Martinez)
    #   908 James Rodríguez-> casa com 469 (J. Rodríguez, api 517) E com
    #       1077 (José Luis Rodríguez) - o api 517 provavelmente É o James
    #       de verdade, mas não foi confirmado, então fica fora
    #   944 Emiliano Rodriguez -> casa com 654 (E. Rodríguez) E com
    #       1053 (Emerson Rodríguez)
    #   844 Alex Nascimento -> 2005 (A. Gomes do Nascimento) é plausível
    #       mas não confirmado
    #   914/1079/1164/2245/542 - cluster Martínez (Emiliano x Emmanuel x
    #       Emiliano/Emmanuel sem acento): são pelo menos 4 pessoas reais
    #       embaralhadas; só (914, 1164) foi aceito acima por ser o único
    #       par sem concorrente plausível
    #   2213 Estevao      -> pode ser 1443 (Estevão, api 311447) OU
    #       677 (Estêvão, api 425733) - dois jogadores reais distintos,
    #       impossível decidir pelo nome
    #
    # SEM LADO CANÔNICO - os DOIS registros estão sem api_football_id,
    # então não há "real" pra onde apontar. Juntar exigiria escolher um
    # dos dois arbitrariamente como sobrevivente:
    #   421 Aloísio x 769 Aloisio
    # ------------------------------------------------------------------
]


def juntar_jogador(cur, id_fantasma, id_real):
    # NOVO (correção após crash real): em vez de listar as tabelas que
    # referenciam `jogadores` na mão (a lista original esqueceu a tabela
    # `gols`, causando um ForeignKeyViolation e derrubando o serviço),
    # descobre DINAMICAMENTE, direto do banco, toda tabela+coluna que tem
    # uma chave estrangeira apontando pra jogadores(id) - nunca mais
    # esquece uma tabela nova que apareça no futuro.
    cur.execute(
        """
        SELECT tc.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = 'public'
          AND ccu.table_name = 'jogadores' AND ccu.column_name = 'id'
        """
    )
    tabelas_referenciando = cur.fetchall()

    for tabela, coluna in tabelas_referenciando:
        if tabela.startswith(PREFIXOS_PARA_DELETAR):
            # tabelas de padrão CALCULADO (padroes_jogador_*) - apaga o
            # fantasma, não reatribui: reatribuir daria erro de chave
            # duplicada se o jogador real já tiver uma linha pro mesmo
            # (tipo, linha); o motor_padroes.py recria do zero mesmo,
            # combinando o histórico completo dos dois lados
            cur.execute(f'DELETE FROM "{tabela}" WHERE "{coluna}" = %s', (id_fantasma,))
        else:
            # tabelas de dado real (estatísticas, cartões, gols, odds,
            # recomendações...) - reatribui de verdade, não apaga nada
            cur.execute(f'UPDATE "{tabela}" SET "{coluna}" = %s WHERE "{coluna}" = %s', (id_real, id_fantasma))

    # multiplas_candidatas guarda jogador_id DENTRO de um campo JSONB, não
    # é uma chave estrangeira de verdade - não aparece na busca acima,
    # precisa ser tratado à parte
    cur.execute(
        "UPDATE multiplas_candidatas SET jogos = REPLACE(jogos::text, %s, %s)::jsonb WHERE jogos::text LIKE %s",
        (f'"jogador_id": {id_fantasma}', f'"jogador_id": {id_real}', f'%"jogador_id": {id_fantasma}%'),
    )

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
