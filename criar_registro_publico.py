"""
criar_registro_publico.py — cria a tabela `public.registro_publico` (Fase 5 da
`claude/arquitetura_ligas_extras.md`, decisão do usuário em 01/10: começa pelo
Brasileirão, já).

Duas fases:

    python criar_registro_publico.py verificar
    python criar_registro_publico.py aplicar

`verificar` NÃO cria nem altera nada: só diz o que existe e o que seria criado.
`aplicar` é idempotente: rodar de novo não muda o que já existe.

--------------------------------------------------------------------------
O QUE É O REGISTRO PÚBLICO
--------------------------------------------------------------------------

`historico_recomendacoes` é gravado DEPOIS do apito (no arquivamento). Ele
prova o resultado, não que a recomendação existia ANTES do jogo. Esta tabela
grava cada recomendação viva — e cada mudança dela — com data e hora
ANTERIORES ao apito, e o banco garante isso sozinho:

  1. SÓ INCLUSÃO. Trigger recusa UPDATE, DELETE e TRUNCATE.
  2. A HORA É DO BANCO. No INSERT, `registrado_em` é sobrescrito com now() —
     um INSERT com data inventada não passa.
  3. NADA DEPOIS DO APITO. CHECK: registrado_em < horário do jogo.
  4. CADEIA DE HASH. Cada linha guarda sha256(hash da linha anterior +
     conteúdo dela). Mudar uma linha antiga quebra a cadeia daquela linha em
     diante, e qualquer um confere com uma consulta só (ver
     `registrar_publico.py cadeia`). A conta é feita NO BANCO, pelo trigger,
     com UMA função de conteúdo — a mesma que a verificação usa. Não existe
     uma segunda implementação para divergir.

⚠️ LIMITE HONESTO: o dono do banco (superusuário) consegue desligar o
trigger e reescrever a cadeia INTEIRA de forma consistente. Para um terceiro
desconfiado, a proteção completa exige publicar o último hash fora do banco
de tempos em tempos (passo futuro, não feito aqui).

Não muda NADA do que o Brasileirão gera: nenhum script existente lê esta
tabela.
"""

import os
import sys
from datetime import datetime

import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

TABELA = "public.registro_publico"

SQL_TABELA = """
CREATE TABLE IF NOT EXISTS public.registro_publico (
    id               bigserial PRIMARY KEY,
    registrado_em    timestamptz NOT NULL DEFAULT now(),
    liga             text        NOT NULL,
    evento           text        NOT NULL,
    fixture_id_api   integer     NOT NULL,
    jogo_id          integer     NOT NULL,
    datahora_jogo    timestamp   NOT NULL,
    tipo_padrao      varchar(30) NOT NULL,
    linha            numeric(4,1),
    direcao          text,
    jogador_id       integer,
    casa_aposta      varchar(50) NOT NULL,
    descricao        varchar(255) NOT NULL,
    odd              numeric(6,2),
    probabilidade    numeric(5,2),
    valor_esperado   numeric(5,3),
    versao_codigo    text        NOT NULL,
    hash_anterior    text,
    hash_linha       text        NOT NULL,
    CONSTRAINT registro_publico_evento_ck CHECK (evento IN ('ativa', 'retirada')),
    CONSTRAINT registro_publico_valores_ck CHECK (
        evento = 'retirada'
        OR (odd IS NOT NULL AND probabilidade IS NOT NULL AND valor_esperado IS NOT NULL)),
    CONSTRAINT registro_publico_antes_do_apito_ck CHECK (
        registrado_em < (datahora_jogo AT TIME ZONE 'UTC'))
)
"""

# A ÚNICA definição do conteúdo que entra no hash. Usada pelo trigger E pela
# verificação. Formatos fixos (UTC, numeric com escala da coluna) para o texto
# não depender de configuração de sessão.
SQL_FUNCAO_CONTEUDO = """
CREATE OR REPLACE FUNCTION public.registro_publico_conteudo(r public.registro_publico)
RETURNS text LANGUAGE sql STABLE AS $$
    SELECT concat_ws('|',
        r.id::text,
        to_char(r.registrado_em AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
        r.liga, r.evento,
        r.fixture_id_api::text, r.jogo_id::text,
        to_char(r.datahora_jogo, 'YYYY-MM-DD"T"HH24:MI:SS'),
        r.tipo_padrao,
        COALESCE(r.linha::text, ''), COALESCE(r.direcao, ''), COALESCE(r.jogador_id::text, ''),
        r.casa_aposta, r.descricao,
        COALESCE(r.odd::text, ''), COALESCE(r.probabilidade::text, ''), COALESCE(r.valor_esperado::text, ''),
        r.versao_codigo)
$$
"""

SQL_FUNCAO_ANTES_INSERIR = """
CREATE OR REPLACE FUNCTION public.registro_publico_antes_inserir()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    anterior text;
BEGIN
    -- Um gravador por vez: a cadeia precisa de ordem total.
    PERFORM pg_advisory_xact_lock(4242001);
    -- id e hora atribuídos DEPOIS da trava: a ordem do id é a ordem da cadeia,
    -- e a hora é a do banco, não a de quem insere.
    NEW.id := nextval(pg_get_serial_sequence('public.registro_publico', 'id'));
    NEW.registrado_em := now();
    SELECT hash_linha INTO anterior FROM public.registro_publico ORDER BY id DESC LIMIT 1;
    NEW.hash_anterior := anterior;
    NEW.hash_linha := encode(sha256(convert_to(
        COALESCE(anterior, '') || public.registro_publico_conteudo(NEW), 'UTF8')), 'hex');
    RETURN NEW;
END
$$
"""

SQL_FUNCAO_RECUSAR = """
CREATE OR REPLACE FUNCTION public.registro_publico_recusar_alteracao()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'registro_publico é só-inclusão: % recusado', TG_OP;
END
$$
"""

TRIGGERS = [
    ("registro_publico_inserir",
     "CREATE TRIGGER registro_publico_inserir BEFORE INSERT ON public.registro_publico "
     "FOR EACH ROW EXECUTE FUNCTION public.registro_publico_antes_inserir()"),
    ("registro_publico_sem_update_delete",
     "CREATE TRIGGER registro_publico_sem_update_delete BEFORE UPDATE OR DELETE ON public.registro_publico "
     "FOR EACH ROW EXECUTE FUNCTION public.registro_publico_recusar_alteracao()"),
    ("registro_publico_sem_truncate",
     "CREATE TRIGGER registro_publico_sem_truncate BEFORE TRUNCATE ON public.registro_publico "
     "FOR EACH STATEMENT EXECUTE FUNCTION public.registro_publico_recusar_alteracao()"),
]

SQL_INDICE = ("CREATE INDEX IF NOT EXISTS registro_publico_chave_idx ON public.registro_publico "
              "(liga, jogo_id, tipo_padrao, id)")


def cabecalho(fase):
    print("=" * 78)
    print(f"criar_registro_publico.py | fase: {fase}")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 78)
    sys.stdout.flush()


def existe_tabela(cur):
    cur.execute("SELECT to_regclass('public.registro_publico') IS NOT NULL")
    return cur.fetchone()[0]


def linhas(cur):
    cur.execute("SELECT COUNT(*) FROM public.registro_publico")
    return cur.fetchone()[0]


def existe_funcao(cur, nome):
    cur.execute("SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public' AND p.proname = %s", (nome,))
    return cur.fetchone() is not None


def triggers_existentes(cur):
    cur.execute("SELECT tgname FROM pg_trigger WHERE tgrelid = to_regclass('public.registro_publico') "
                "AND NOT tgisinternal")
    return {r[0] for r in cur.fetchall()}


def fase_verificar(cur):
    if not existe_tabela(cur):
        print("`public.registro_publico` NÃO existe — o aplicar cria tabela, 3 funções, 3 triggers e 1 índice.")
        return
    print(f"`public.registro_publico` existe, {linhas(cur)} linha(s).")
    for nome in ("registro_publico_conteudo", "registro_publico_antes_inserir",
                 "registro_publico_recusar_alteracao"):
        print(f"  função {nome}: {'ok' if existe_funcao(cur, nome) else 'FALTA'}")
    existentes = triggers_existentes(cur)
    for nome, _sql in TRIGGERS:
        print(f"  trigger {nome}: {'ok' if nome in existentes else 'FALTA'}")


def fase_aplicar(cur):
    ja_existia = existe_tabela(cur)
    tem_linhas = ja_existia and linhas(cur) > 0

    cur.execute(SQL_TABELA)
    if tem_linhas and existe_funcao(cur, "registro_publico_conteudo"):
        # Trocar a função de conteúdo com linhas gravadas tornaria a cadeia
        # antiga inverificável. Não troca — nunca, por este script.
        print("  tabela já tem linhas: funções NÃO são recriadas (a cadeia depende delas).")
    else:
        cur.execute(SQL_FUNCAO_CONTEUDO)
        cur.execute(SQL_FUNCAO_ANTES_INSERIR)
        cur.execute(SQL_FUNCAO_RECUSAR)
        print("  funções criadas/atualizadas.")
    existentes = triggers_existentes(cur)
    for nome, sql in TRIGGERS:
        if nome not in existentes:
            cur.execute(sql)
            print(f"  trigger {nome}: criado")
    cur.execute(SQL_INDICE)

    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'claude_leitura'")
    if cur.fetchone():
        cur.execute("GRANT SELECT ON public.registro_publico TO claude_leitura")
        print("  leitura liberada para claude_leitura.")
    print(f"\n✅ `public.registro_publico` {'já existia — conferido' if ja_existia else 'criada'}. "
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
        print("FASE VERIFICAR — nada foi criado nem alterado (nem `registro_publico`, nem outra tabela).")
        print("=" * 78)


if __name__ == "__main__":
    main()
