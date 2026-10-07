"""
criar_registro_modelo.py — cria a tabela `public.registro_modelo`
(`claude/arquitetura_registro_modelo.md §4`).

Duas fases:

    python criar_registro_modelo.py verificar
    python criar_registro_modelo.py aplicar

`verificar` NÃO cria nem altera nada. `aplicar` é idempotente.

O que é: a foto, ANTES do apito, da probabilidade do modelo para cada linha
que a casa ofereceria. Mede o modelo sem casa de aposta (a Superbet BR saiu
do ar em 06/10 pela MP de 25/09). Quem grava é o `registrar_modelo.py`.

Garantias dadas pelo próprio banco (mesmo desenho do `registro_publico`,
sem a cadeia de hash — é instrumento interno, não prova para terceiro):
  1. SÓ INCLUSÃO: trigger recusa UPDATE, DELETE e TRUNCATE.
  2. A HORA É DO BANCO: `registrado_em := now()` no INSERT.
  3. NADA DEPOIS DO APITO: CHECK registrado_em < datahora_jogo.

Nenhum script existente lê esta tabela. Não muda nada do que o sistema gera.
"""

import os
import sys
from datetime import datetime

import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

SQL_TABELA = """
CREATE TABLE IF NOT EXISTS public.registro_modelo (
    id               bigserial PRIMARY KEY,
    registrado_em    timestamptz NOT NULL DEFAULT now(),
    fixture_id_api   integer     NOT NULL,
    jogo_id          integer     NOT NULL,
    datahora_jogo    timestamp   NOT NULL,
    nosso_time_id    integer,
    tipo_padrao      varchar(30) NOT NULL,
    linha            numeric(5,2),
    direcao          text,
    jogador_id       integer,
    mercado          varchar(255) NOT NULL,
    descricao        text        NOT NULL,
    p_modelo         numeric(5,2) NOT NULL,
    casa_ref         varchar(50),
    odd_ref          numeric(8,3),
    versao_codigo    text        NOT NULL,
    CONSTRAINT registro_modelo_p_ck CHECK (p_modelo >= 0 AND p_modelo <= 100),
    CONSTRAINT registro_modelo_antes_do_apito_ck CHECK (
        registrado_em < (datahora_jogo AT TIME ZONE 'UTC'))
)
"""

SQL_FUNCAO_ANTES_INSERIR = """
CREATE OR REPLACE FUNCTION public.registro_modelo_antes_inserir()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.registrado_em := now();
    RETURN NEW;
END
$$
"""

SQL_FUNCAO_RECUSAR = """
CREATE OR REPLACE FUNCTION public.registro_modelo_recusar_alteracao()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'registro_modelo é só-inclusão: % recusado', TG_OP;
END
$$
"""

TRIGGERS = [
    ("registro_modelo_inserir",
     "CREATE TRIGGER registro_modelo_inserir BEFORE INSERT ON public.registro_modelo "
     "FOR EACH ROW EXECUTE FUNCTION public.registro_modelo_antes_inserir()"),
    ("registro_modelo_sem_update_delete",
     "CREATE TRIGGER registro_modelo_sem_update_delete BEFORE UPDATE OR DELETE ON public.registro_modelo "
     "FOR EACH ROW EXECUTE FUNCTION public.registro_modelo_recusar_alteracao()"),
    ("registro_modelo_sem_truncate",
     "CREATE TRIGGER registro_modelo_sem_truncate BEFORE TRUNCATE ON public.registro_modelo "
     "FOR EACH STATEMENT EXECUTE FUNCTION public.registro_modelo_recusar_alteracao()"),
]

SQL_INDICES = [
    "CREATE INDEX IF NOT EXISTS registro_modelo_jogo_idx ON public.registro_modelo "
    "(fixture_id_api, jogo_id, tipo_padrao, registrado_em)",
    "CREATE INDEX IF NOT EXISTS registro_modelo_data_idx ON public.registro_modelo (datahora_jogo)",
]


def cabecalho(fase):
    print("=" * 78)
    print(f"criar_registro_modelo.py | fase: {fase}")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 78)
    sys.stdout.flush()


def existe_tabela(cur):
    cur.execute("SELECT to_regclass('public.registro_modelo') IS NOT NULL")
    return cur.fetchone()[0]


def triggers_existentes(cur):
    cur.execute("SELECT tgname FROM pg_trigger WHERE tgrelid = to_regclass('public.registro_modelo') "
                "AND NOT tgisinternal")
    return {r[0] for r in cur.fetchall()}


def fase_verificar(cur):
    if not existe_tabela(cur):
        print("`public.registro_modelo` NÃO existe — o aplicar cria tabela, 2 funções, 3 triggers e 2 índices.")
        return
    cur.execute("SELECT COUNT(*) FROM public.registro_modelo")
    print(f"`public.registro_modelo` existe, {cur.fetchone()[0]} linha(s).")
    existentes = triggers_existentes(cur)
    for nome, _sql in TRIGGERS:
        print(f"  trigger {nome}: {'ok' if nome in existentes else 'FALTA'}")


def fase_aplicar(cur):
    ja_existia = existe_tabela(cur)
    cur.execute(SQL_TABELA)
    cur.execute(SQL_FUNCAO_ANTES_INSERIR)
    cur.execute(SQL_FUNCAO_RECUSAR)
    existentes = triggers_existentes(cur)
    for nome, sql in TRIGGERS:
        if nome not in existentes:
            cur.execute(sql)
            print(f"  trigger {nome}: criado")
    for sql in SQL_INDICES:
        cur.execute(sql)
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'claude_leitura'")
    if cur.fetchone():
        cur.execute("GRANT SELECT ON public.registro_modelo TO claude_leitura")
        print("  leitura liberada para claude_leitura.")
    print(f"\n✅ `public.registro_modelo` {'já existia — conferido' if ja_existia else 'criada'}. "
          f"Nenhuma outra tabela foi tocada.")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("verificar", "aplicar"):
        print(__doc__)
        sys.exit(1)
    fase = sys.argv[1]
    cabecalho(fase)
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    try:
        if fase == "verificar":
            fase_verificar(cur)
        else:
            fase_aplicar(cur)
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if fase == "verificar":
            conn.rollback()  # incondicional: esta fase nunca grava
        cur.close()
        conn.close()
    if fase == "verificar":
        print("\n" + "=" * 78)
        print("FASE VERIFICAR — nada foi criado nem alterado (nem `registro_modelo`, nem outra tabela).")
        print("=" * 78)


if __name__ == "__main__":
    main()
