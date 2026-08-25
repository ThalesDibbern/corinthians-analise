"""
Migração: separa `padroes_time_escanteio` por MANDO DE CAMPO.

POR QUE (medido na base inteira - 1652 jogos, 5 temporadas):

    ano    jogos  media_total  mandante  visitante  vantagem
    2022     324        11.00      6.17       4.83      1.34
    2023     368        10.58      5.95       4.63      1.32
    2024     359        10.18      5.69       4.49      1.20
    2025     368         9.92      5.60       4.32      1.28
    2026     233        10.09      5.69       4.40      1.28
    GERAL   1652        10.36      5.82       4.54      1.29

Mandante faz 1.29 escanteio a mais por jogo que visitante, e o número é
estável ano a ano (variação de 0.14 entre o maior e o menor). Até aqui
`padroes_time_escanteio` guardava UM número por (time_id, linha),
misturando casa e fora, enquanto a casa de apostas precifica os dois
separadamente.

Isso vale ~7 pontos de probabilidade em toda linha, sempre no mesmo
sentido - acima da margem da casa (~5%), portanto suficiente pra fabricar
VE positivo onde não existe.

SINTOMA (auditoria da rodada de 22-24/08/2026, fase 2):
    das 74 recomendações de escanteio de time, 69 saíram do lado errado
    (44 de "Menos" pra time mandante, 25 de "Mais" pra time visitante).
    Acerto de 24.3% contra 54.3% previstos - o pior gap de todos os
    mercados, com o maior volume.

POR QUE ESTA ESTATÍSTICA E NÃO AS OUTRAS:
    A mesma medição descartou os demais mercados. Gols tem vantagem de
    0.14 (~2 pontos de probabilidade, abaixo da margem) e teve gap de
    +0.7 na rodada. Faltas tem 0.34 em 13.5, ruído. Finalizações tem
    vantagem grande (3.55) mas não existe mercado de chute por TIME, e
    onde ela entra de fato - a correlação chutes->escanteios do
    `escanteio_total` - o `a_estimado` SOMA os dois times, então a
    assimetria se cancela (15.15 + 11.60 = 26.75, contra 13.4 + 13.4).

    Escanteio é a única estatística onde a assimetria de mando é grande o
    bastante pra importar num mercado que existe. O conserto é cirúrgico:
    uma tabela, um mercado, uma fronteira só no histórico de calibração.

O QUE ESTA MIGRAÇÃO FAZ:
    1. adiciona a coluna `lado` com DEFAULT 'geral'
    2. troca a constraint UNIQUE (time_id, linha) por (time_id, linha, lado)
    3. cria índice em (time_id, lado, linha) pro caminho de leitura

SEM BACKFILL, de propósito: o DEFAULT 'geral' já converte as linhas
existentes no recorte geral - que é exatamente o que elas são hoje. Os
recortes 'mandante' e 'visitante' nascem na primeira execução de
`motor_padroes.py`, que regrava os padrões inteiros a cada rodada.

⚠️ ORDEM OBRIGATÓRIA: esta migração PRECISA rodar ANTES de subir o
`motor_padroes.py` novo. O `salvar_padroes_escanteio` corrigido usa
`ON CONFLICT (time_id, linha, lado)`; com a constraint antiga no banco,
o INSERT quebra e derruba o cron inteiro. Não corrompe dado, mas para o
pipeline.

IDEMPOTENTE: pode rodar mais de uma vez sem estragar nada. Cada passo
checa se já foi aplicado antes de mexer.

Rodar:
    python migrar_padroes_escanteio_lado.py

Variáveis de ambiente necessárias: DATABASE_URL
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

TABELA = "padroes_time_escanteio"
CONSTRAINT_ANTIGA = "padroes_time_escanteio_time_linha_key"
CONSTRAINT_NOVA = "padroes_time_escanteio_time_linha_lado_key"
INDICE_NOVO = "idx_padroes_time_escanteio_time_lado_linha"


def coluna_existe(cur, tabela, coluna):
    cur.execute(
        """SELECT 1 FROM information_schema.columns
           WHERE table_name = %s AND column_name = %s""",
        (tabela, coluna),
    )
    return cur.fetchone() is not None


def constraint_existe(cur, nome):
    cur.execute("SELECT 1 FROM pg_constraint WHERE conname = %s", (nome,))
    return cur.fetchone() is not None


def indice_existe(cur, nome):
    cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", (nome,))
    return cur.fetchone() is not None


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        cur.execute(f"SELECT COUNT(*) FROM {TABELA}")
        antes = cur.fetchone()[0]
        print(f"{TABELA}: {antes} linha(s) antes da migração.\n")

        # ---------- 1. coluna ----------
        if coluna_existe(cur, TABELA, "lado"):
            print("1. Coluna `lado` já existe - pulando.")
        else:
            cur.execute(
                f"ALTER TABLE {TABELA} "
                "ADD COLUMN lado VARCHAR(10) NOT NULL DEFAULT 'geral'"
            )
            print("1. Coluna `lado` criada (DEFAULT 'geral').")
            print("   As linhas existentes viraram o recorte 'geral' automaticamente.")

        # ---------- 2. constraints ----------
        if constraint_existe(cur, CONSTRAINT_ANTIGA):
            cur.execute(f"ALTER TABLE {TABELA} DROP CONSTRAINT {CONSTRAINT_ANTIGA}")
            print(f"2. Constraint antiga `{CONSTRAINT_ANTIGA}` removida.")
        else:
            print(f"2. Constraint antiga `{CONSTRAINT_ANTIGA}` não existe - pulando.")

        if constraint_existe(cur, CONSTRAINT_NOVA):
            print(f"   Constraint nova `{CONSTRAINT_NOVA}` já existe - pulando.")
        else:
            cur.execute(
                f"ALTER TABLE {TABELA} "
                f"ADD CONSTRAINT {CONSTRAINT_NOVA} UNIQUE (time_id, linha, lado)"
            )
            print(f"   Constraint nova `{CONSTRAINT_NOVA}` criada (time_id, linha, lado).")

        # ---------- 3. índice ----------
        if indice_existe(cur, INDICE_NOVO):
            print(f"3. Índice `{INDICE_NOVO}` já existe - pulando.")
        else:
            cur.execute(
                f"CREATE INDEX {INDICE_NOVO} ON {TABELA} (time_id, lado, linha)"
            )
            print(f"3. Índice `{INDICE_NOVO}` criado.")

        # ---------- conferência ----------
        cur.execute(f"SELECT lado, COUNT(*) FROM {TABELA} GROUP BY lado ORDER BY lado")
        print("\n-- CONFERÊNCIA --")
        for lado, qtd in cur.fetchall():
            print(f"    lado='{lado}': {qtd} linha(s)")

        conn.commit()
        print("\n✅ Migração aplicada e commitada.")
        print("\nPRÓXIMOS PASSOS, nesta ordem:")
        print("  1. subir motor_padroes.py, motor_recomendacoes.py e app.py")
        print("  2. rodar `python motor_padroes.py` uma vez à mão")
        print("     (senão os recortes 'mandante'/'visitante' só nascem no cron do dia seguinte,")
        print("      e até lá a escada cai no 'geral' - sem erro, mas sem o conserto também)")
        print("  3. rodar `python gerar_schema.py` e substituir o schema.sql")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Erro, nada foi salvo (rollback): {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
