"""
Migração: cria `padroes_time_marca_periodo`.

POR QUE (medido na base inteira - 1662 jogos, 5 temporadas, 02/09/2026):

    ano   jogos  p_mand_marca  p_visit_marca  p_ambas_real  fator
    2022    324        0.5340         0.3179        0.1667  0.9819
    2023    368        0.4864         0.4022        0.2120  1.0835
    2024    359        0.5125         0.3928        0.2145  1.0655
    2025    368        0.5299         0.3370        0.1630  0.9132
    2026    243        0.5473         0.4074        0.2058  0.9228

O `fator` é P(ambas marcam no 1T) dividido pelo PRODUTO das duas
probabilidades individuais. Ele gira em torno de 1,0 (média 0.99,
variação 0.91-1.08), o que quer dizer que "o mandante marca no 1T" e
"o visitante marca no 1T" são praticamente INDEPENDENTES. Então:

    P(ambas marcam no período) = P(mandante marca) x P(visitante marca)

sem precisar de nenhum fator de correção pra manter e envelhecer.

O QUE ISSO CONSERTA:
    Hoje `padroes_ambas_marcam_tempo` guarda a frequência CONJUNTA por
    time: "nos últimos 50 jogos do Flamengo em casa, em quantos ambos
    marcaram no 1T?". Isso é uma média sobre TODOS os adversários que o
    Flamengo enfrentou - não diz nada sobre o adversário do próximo jogo.
    Se o próximo é o Mirassol, que marca pouco fora, o número real
    deveria ser menor, e o padrão não sabe disso.

    Pior: como o valor depende de qual time é o `nosso_time` da linha, o
    MESMO evento saía com dois números diferentes. Medido em 190 pares de
    times: divergência média de 7,14 pontos no 1T e 8,78 no 2T, com
    máximas de 27,6 e 32,0. E como o filtro de VE fica sempre com a
    estimativa mais otimista, a que sobrevivia era sistematicamente a
    mais alta.

    O sintoma mais visível apareceu no log da rodada de 02/09/2026, no
    Mirassol x Flamengo: o app recomendou "Ambas Marcam 1T - Sim" (23,33%)
    por uma perspectiva E "Ambas Marcam 1T - Não" (88,00%) pela outra, no
    MESMO jogo. Se Sim vale 23,33%, Não vale 76,67% - não 88%. As duas
    recomendações não podiam estar certas ao mesmo tempo.

    Decompondo em componentes, o cálculo deixa de depender da perspectiva:
    P(mandante marca) x P(visitante marca) dá o mesmo número seja qual for
    o lado de onde se olha. O defeito some por CONSTRUÇÃO, não por
    sincronização.

O QUE ESTA MIGRAÇÃO FAZ:
    Cria a tabela `padroes_time_marca_periodo`, com uma linha por
    (time_id, periodo, lado) - 4 linhas por time: {1T,2T} x
    {mandante,visitante}.

SEM BACKFILL: a tabela nasce vazia e é populada na primeira execução do
`motor_padroes.py` novo, que regrava os padrões inteiros a cada rodada.

⚠️ ORDEM OBRIGATÓRIA: esta migração PRECISA rodar ANTES de subir o
`motor_padroes.py` novo. `salvar_padroes_time_marca_periodo` usa
`ON CONFLICT (time_id, periodo, lado)`; sem a tabela, o INSERT quebra e
derruba o cron inteiro. Mesma armadilha que já derrubou a Fase C.

⚠️ `padroes_ambas_marcam_tempo` NÃO é apagada. Ela continua sendo:
    - o FALLBACK da escada, quando algum componente não tiver amostra
    - a base de comparação com o método antigo

IDEMPOTENTE: pode rodar mais de uma vez sem estragar nada.

Rodar:
    python migrar_padroes_time_marca_periodo.py

Variáveis de ambiente necessárias: DATABASE_URL
"""

import os
import sys
from datetime import datetime

import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

TABELA = "padroes_time_marca_periodo"
CONSTRAINT_UNICA = "padroes_time_marca_periodo_unico"
INDICE = "idx_padroes_time_marca_periodo_leitura"


def cabecalho():
    print("=" * 78)
    print("migrar_padroes_time_marca_periodo.py | fase: aplicar")
    print(f"argv: {sys.argv}")
    print(f"relógio: {datetime.now().isoformat()}")
    print("=" * 78)


def tabela_existe(cur, nome):
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (nome,),
    )
    return cur.fetchone() is not None


def indice_existe(cur, nome):
    cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", (nome,))
    return cur.fetchone() is not None


def main():
    cabecalho()
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        # ---------- 1. tabela ----------
        if tabela_existe(cur, TABELA):
            cur.execute(f"SELECT COUNT(*) FROM {TABELA}")
            print(f"1. Tabela `{TABELA}` já existe ({cur.fetchone()[0]} linha(s)) - pulando.")
        else:
            cur.execute(
                f"""
                CREATE TABLE {TABELA} (
                    id SERIAL,
                    time_id integer NOT NULL,
                    periodo character varying(2) NOT NULL,
                    lado character varying(10) NOT NULL,
                    jogos_analisados integer NOT NULL,
                    jogos_que_marcou integer NOT NULL,
                    frequencia numeric(5,2) NOT NULL,
                    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
                    CONSTRAINT padroes_time_marca_periodo_pkey PRIMARY KEY (id),
                    CONSTRAINT {CONSTRAINT_UNICA} UNIQUE (time_id, periodo, lado),
                    CONSTRAINT padroes_time_marca_periodo_time_fkey
                        FOREIGN KEY (time_id) REFERENCES times(id)
                )
                """
            )
            print(f"1. Tabela `{TABELA}` criada.")
            print("   Nasce VAZIA - será populada pelo motor_padroes.py novo.")

        # ---------- 2. índice de leitura ----------
        if indice_existe(cur, INDICE):
            print(f"2. Índice `{INDICE}` já existe - pulando.")
        else:
            cur.execute(
                f"CREATE INDEX {INDICE} ON {TABELA} (time_id, periodo, lado)"
            )
            print(f"2. Índice `{INDICE}` criado.")

        conn.commit()
        print("\nMigração concluída e commitada.")
        print("\nPRÓXIMOS PASSOS, nesta ordem:")
        print("  1. subir o motor_padroes.py novo e RODAR (popula os 20 times)")
        print("  2. conferir a tabela por SQL antes de mexer nas recomendações")
        print("  3. só então subir o motor_recomendacoes.py novo")
        print("\nLembrete: rodar gerar_schema.py depois, pra refletir a tabela nova")
        print("no schema.sql (a pendência de regeração já estava aberta desde 27/08).")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a migração, rollback feito: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
