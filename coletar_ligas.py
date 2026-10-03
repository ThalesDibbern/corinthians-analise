"""
coletar_ligas.py — coleta de ligas EXTRAS (Premier League etc.), cada uma no
SEU schema do banco (`liga_premier`, `liga_laliga`, ...), sem tocar no
Brasileirão e sem gerar recomendação.

⚠️ 02/10 (Fase 1 da `claude/arquitetura_ligas_extras.md`): até aqui tudo ia
para um schema único `ligas`. Agora é um schema POR LIGA. Liga que ainda tem
dado em `ligas` precisa ser migrada antes (`migrar_ligas_por_schema.py`); o
`aplicar` recusa enquanto isso não acontecer.

Duas fases, como todo script que toca dado neste projeto:

    python coletar_ligas.py verificar --liga premier
    python coletar_ligas.py aplicar   --liga premier [--teto 6500] [--temporadas 2026,2025]

`verificar` NÃO chama a API e NÃO grava em tabela nenhuma (nem no
`controle_api_uso`). Só lê e mostra o estado.

--------------------------------------------------------------------------
POR QUE UM SCHEMA SEPARADO (`ligas`)
--------------------------------------------------------------------------

O `motor_padroes` calcula médias e correlações da "liga inteira" lendo
`jogos`/`estatisticas_jogo` sem filtro de competição. Se jogos da Premier
League entrassem nas mesmas tabelas, as recomendações do Brasileirão
mudariam em silêncio — no meio da medição da rodada 29.

Aqui tudo vai para o schema `ligas` (`ligas.jogos`, `ligas.gols`, ...). O
código do Brasileirão usa nomes sem schema, que resolvem para `public`, e
por isso NÃO ENXERGA nada do que este script grava.

--------------------------------------------------------------------------
COMO ISSO REAPROVEITA O popular_banco SEM REESCREVER
--------------------------------------------------------------------------

A conexão abre com `search_path = ligas, public`. As funções de GRAVAÇÃO do
`popular_banco` (get_or_create_jogo, salvar_eventos, salvar_estatisticas,
salvar_estatisticas_jogadores, salvar_escalacao, get_or_create_time...) são
IMPORTADAS e chamadas sem alteração: as tabelas delas existem em `ligas`,
então os INSERTs caem lá. `controle_api_uso` NÃO é copiada, então resolve
para `public` — o contador de cota continua o MESMO dos crons do
Brasileirão, e o teto vale para a soma do dia.

🔴 A TRAVA QUE IMPEDE O PIOR CASO: antes de qualquer gravação,
`garantir_isolamento` pergunta ao próprio Postgres em qual schema cada
tabela está resolvendo. Se UMA das 9 tabelas de dado resolver para
`public`, o script aborta sem gravar nada. Não depende de eu ter escrito o
nome certo — é o banco que responde.

O que é código NOVO aqui é só o laço principal, e por um motivo: o
`popular_banco` busca os 4 endpoints UMA VEZ POR PERSPECTIVA. Na Premier
League os dois times de todo jogo são rastreados, então isso daria 8
chamadas por jogo. Este laço busca cada endpoint UMA VEZ POR JOGO e grava
nas duas perspectivas com as mesmas funções de gravação — 4 chamadas por
jogo, metade da cota.

--------------------------------------------------------------------------
COTA DA API-FOOTBALL (compartilhada com o Brasileirão)
--------------------------------------------------------------------------

Limite do plano: 7.500/dia. O `popular_banco` para em 7.000. Este script
para no TETO (padrão 6.500) contado sobre o MESMO contador do dia — ou seja,
sobra margem para os crons do Brasileirão e para scripts manuais.

⚠️ Rodar SEMPRE depois dos dois crons do dia (12:00 e ~15:0x UTC). O
contador zera por dia (CURRENT_DATE do banco, UTC); rodando depois dos
crons, o Brasileirão já coletou o dia dele antes.

Ao bater no teto, o script para, grava o que fez e sai com sucesso. A
próxima execução continua de onde parou: jogo com dado completo é pulado.

--------------------------------------------------------------------------
O QUE AINDA NÃO FAZ (de propósito)
--------------------------------------------------------------------------

- Não gera recomendação, não busca odd, não toca a OddsPapi.
- A data de jogo FUTURO é gravada uma vez e não é sincronizada (mesma
  limitação que o Brasileirão tinha antes do `sincronizar_datas_jogos`).
  Irrelevante enquanto não houver recomendação para essas ligas.
- `jogos.competicao` é corrigida para o nome da liga logo depois do INSERT
  (o `get_or_create_jogo` grava "Brasileirão Série A" fixo).

Variáveis de ambiente (as mesmas do `popular_banco`):
    DATABASE_URL, API_FOOTBALL_KEY
    PAUSA_API_SEGUNDOS (opcional, padrão 1.0)
"""

import os
import sys
import time
from datetime import datetime

import functools
import types

import psycopg2
import requests

# Import das funções REAIS de gravação. Exige DATABASE_URL e API_FOOTBALL_KEY
# no ambiente (o popular_banco lê as duas na importação).
import popular_banco as pb

# FASE 1 da `claude/arquitetura_ligas_extras.md` (02/10): UM SCHEMA POR LIGA.
# `liga_premier`, `liga_laliga`, ... Assim a "liga inteira" que o
# motor_padroes enxerga é a liga certa, sem filtro no código (§1 da arquitetura).
# O schema único `ligas` (30/09-02/10) é o LEGADO: os dados dele são copiados
# por `migrar_ligas_por_schema.py`, e este script se RECUSA a coletar uma liga
# que ainda tem dado lá sem ter sido migrada (ver `checar_migracao`).
SCHEMA_LEGADO = "ligas"
SCHEMA = None  # definido por definir_liga(chave) antes de qualquer acesso ao banco


def schema_da_liga(chave):
    return f"liga_{chave}"


def definir_liga(chave):
    """Aponta TODO o módulo para o schema da liga. Chamado uma vez, no main
    (ou por quem importa este módulo, como o migrar_ligas_por_schema)."""
    global SCHEMA
    if chave not in LIGAS:
        raise ValueError(f"liga desconhecida: {chave}")
    SCHEMA = schema_da_liga(chave)
    return SCHEMA

LIGAS = {
    # chave: (id da liga na API-Football, nome gravado em jogos.competicao)
    "premier": (39, "Premier League"),
    "laliga": (140, "La Liga"),
    "seriea": (135, "Serie A (Itália)"),
    "bundesliga": (78, "Bundesliga"),
    "ligue1": (61, "Ligue 1"),
}

# Na API-Football, season=2025 é a temporada 2025/26 das ligas europeias.
# Mais recente primeiro: se a cota acabar, o dado mais útil já entrou.
TEMPORADAS_PADRAO = [2026, 2025, 2024, 2023, 2022]

TETO_PADRAO = 6500

# O popular_banco dorme 7s entre chamadas (comentário lá: limite do plano
# GRÁTIS, ~10/min). No plano pago o limite por minuto é bem maior
# [inferido]; 1s dá folga, e o `chamar_api` já trata o 429 esperando e
# tentando de novo. Ajustável sem deploy pela variável de ambiente.
PAUSA_ENTRE_CHAMADAS = float(os.environ.get("PAUSA_API_SEGUNDOS", "1.0"))

# Tabelas que as funções de gravação do popular_banco tocam. Todas ganham
# cópia em `ligas`. Se o popular_banco passar a gravar numa tabela nova,
# ela TEM que entrar aqui — senão a trava de isolamento não a vê.
TABELAS_COPIADAS = [
    "times", "jogadores", "jogos", "gols", "cartoes", "substituicoes",
    "estatisticas_jogo", "jogador_estatisticas_jogo", "escalacoes",
]
# Continua em `public` de propósito: contador de cota compartilhado.
TABELAS_COMPARTILHADAS = ["controle_api_uso"]

# Contagens do `public` impressas antes e depois — o contador que pode
# derrubar a promessa de isolamento.
TABELAS_PUBLIC_VIGIADAS = ["jogos", "gols", "cartoes", "substituicoes", "estatisticas_jogo",
                           "jogador_estatisticas_jogo", "escalacoes", "times", "jogadores"]


# FALHA PASSAGEIRA DA API (01/10): a 1ª execução real morreu com um
# `502 Bad Gateway` no jogo 473, depois de ~1.900 chamadas certas. O
# `chamar_api` do popular_banco só tenta de novo no 429; qualquer 5xx ou
# queda de conexão derrubava a execução inteira. Aqui: tenta de novo com
# espera crescente; se o jogo continuar falhando, ANOTA e segue para o
# próximo (a execução seguinte completa o que faltou — o jogo fica com o que
# já foi gravado e o `o_que_falta` busca só o resto). Muitas falhas SEGUIDAS
# = API fora do ar: para, sem gastar cota à toa.
ESPERAS_RETENTATIVA_SEGUNDOS = (30, 90, 180)
FALHAS_SEGUIDAS_PARA_PARAR = 5

# `requests.get` sem timeout pode ficar pendurado para sempre e travar o
# cron. O timeout vale SÓ dentro deste processo: troca o `requests` que o
# módulo popular_banco enxerga aqui, sem alterar o arquivo dele (os crons do
# Brasileirão rodam em outro processo e não são afetados).
TIMEOUT_API_SEGUNDOS = 60


class IsolamentoQuebrado(Exception):
    """Alguma tabela de dado não resolve para o schema da liga."""


class MigracaoPendente(Exception):
    """A liga tem dado no schema legado `ligas` que ainda não foi copiado."""


# Tabelas de evento: o motor_padroes busca por `jogo_id` nelas. Sem índice,
# uma consulta de auditoria estourou os 15s da /api/consulta em 02/10 com 2
# ligas no schema. Índice só nos schemas das ligas — `public` não é tocado.
TABELAS_COM_INDICE_JOGO = [
    "gols", "cartoes", "substituicoes", "estatisticas_jogo",
    "jogador_estatisticas_jogo", "escalacoes",
]


class FalhaPassageiraPersistente(Exception):
    """A API continuou com 5xx / sem conexão depois de todas as retentativas."""


class ApiForaDoAr(Exception):
    """Vários jogos seguidos falharam — parar a execução."""


# ---------------------------------------------------------------------------
# utilidades
# ---------------------------------------------------------------------------

def cabecalho(fase, liga_chave, teto, temporadas):
    print("=" * 78)
    print(f"coletar_ligas.py | fase: {fase} | liga: {liga_chave}")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now().isoformat(timespec='seconds')}")
    print(f"schema de destino: {SCHEMA} | teto de cota: {teto} | temporadas: {temporadas}")
    print(f"pausa entre chamadas: {PAUSA_ENTRE_CHAMADAS}s")
    print("=" * 78)
    sys.stdout.flush()


def conectar():
    """Conexão com `search_path = liga_<chave>, public`. Tabela que existe no
    schema da liga resolve para lá; o resto (controle_api_uso) para `public`."""
    if SCHEMA is None:
        raise RuntimeError("definir_liga() não foi chamado — sem schema de destino.")
    conn = psycopg2.connect(pb.DATABASE_URL, options=f"-c search_path={SCHEMA},public")
    conn.autocommit = False
    return conn


def schema_existe(cur, schema=None):
    cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema or SCHEMA,))
    return cur.fetchone() is not None


def jogos_da_liga(cur, schema, liga_api_id):
    """Quantas linhas de `jogos` dessa liga existem no schema (0 se não existe)."""
    if not tabela_existe(cur, schema, "jogos"):
        return 0
    cur.execute(f"SELECT COUNT(*) FROM {schema}.jogos WHERE liga_api_id = %s", (liga_api_id,))
    return cur.fetchone()[0]


def checar_migracao(cur, liga_api_id):
    """🔴 Recusa coletar uma liga que ainda tem dado no schema legado sem ter
    sido migrada. Sem isso, a coleta recomeçaria do zero no schema novo
    (~6.400 chamadas jogadas fora) e o legado ficaria divergente."""
    legado = jogos_da_liga(cur, SCHEMA_LEGADO, liga_api_id)
    novo = jogos_da_liga(cur, SCHEMA, liga_api_id)
    if legado and novo < legado:
        raise MigracaoPendente(
            f"`{SCHEMA_LEGADO}` tem {legado} linha(s) de jogos dessa liga e `{SCHEMA}` tem {novo}. "
            f"Rode antes: python migrar_ligas_por_schema.py aplicar --liga <chave>. Nada foi gravado.")
    return legado, novo


def tabela_existe(cur, schema, tabela):
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
        (schema, tabela),
    )
    return cur.fetchone() is not None


def contagens_public(cur):
    """Contagem exata das tabelas vigiadas do `public` (nome qualificado)."""
    contagens = {}
    for tabela in TABELAS_PUBLIC_VIGIADAS:
        cur.execute(f"SELECT COUNT(*) FROM public.{tabela}")
        contagens[tabela] = cur.fetchone()[0]
    return contagens


def schema_da_tabela(cur, nome):
    """Em qual schema o nome SEM qualificação resolve agora (search_path)."""
    cur.execute(
        "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.oid = to_regclass(%s)",
        (nome,),
    )
    row = cur.fetchone()
    return row[0] if row else None


SQL_SEQUENCIAS_DO_SCHEMA = """
    SELECT c.relname AS tabela, a.attname AS coluna, sn.nspname AS schema_seq, s.relname AS sequencia
    FROM pg_attrdef d
    JOIN pg_class c ON c.oid = d.adrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
    JOIN pg_depend dep ON dep.classid = 'pg_attrdef'::regclass AND dep.objid = d.oid
                      AND dep.refclassid = 'pg_class'::regclass
    JOIN pg_class s ON s.oid = dep.refobjid AND s.relkind = 'S'
    JOIN pg_namespace sn ON sn.oid = s.relnamespace
    WHERE n.nspname = %s
"""
# Pelo CATÁLOGO, não pelo texto do default: com `ligas` no search_path o
# Postgres escreve `nextval('jogos_id_seq')` sem schema tanto para a
# sequência de `ligas` quanto para a de `public` — o texto é ambíguo.
# (Pego na bancada de 30/09: a primeira versão confiava no texto.)


def sequencias_do_schema(cur):
    cur.execute(SQL_SEQUENCIAS_DO_SCHEMA, (SCHEMA,))
    return cur.fetchall()


def garantir_isolamento(cur):
    """🔴 A trava. Roda ANTES de qualquer gravação. Pergunta ao Postgres
    onde cada tabela resolve; qualquer desvio aborta."""
    problemas = []
    for tabela in TABELAS_COPIADAS:
        onde = schema_da_tabela(cur, tabela)
        if onde != SCHEMA:
            problemas.append(f"`{tabela}` resolve para `{onde}` (esperado `{SCHEMA}`)")
    for tabela in TABELAS_COMPARTILHADAS:
        onde = schema_da_tabela(cur, tabela)
        if onde != "public":
            problemas.append(f"`{tabela}` resolve para `{onde}` (esperado `public`)")

    # Sequência de id apontando para fora de `ligas` consumiria os ids do
    # Brasileirão. Confere no catálogo, não no que eu acho que criei.
    for tabela, coluna, schema_seq, sequencia in sequencias_do_schema(cur):
        if schema_seq != SCHEMA:
            problemas.append(f"`{SCHEMA}.{tabela}.{coluna}` usa sequência de fora: {schema_seq}.{sequencia}")

    if problemas:
        raise IsolamentoQuebrado("ISOLAMENTO QUEBRADO — nada foi gravado:\n  - " + "\n  - ".join(problemas))
    print(f"✅ isolamento confirmado pelo banco: {len(TABELAS_COPIADAS)} tabelas de dado resolvem "
          f"para `{SCHEMA}`, `controle_api_uso` para `public`, nenhuma sequência de fora.")


# ---------------------------------------------------------------------------
# estrutura
# ---------------------------------------------------------------------------

def criar_estrutura(cur):
    """Cria o schema e as tabelas (idempotente). Só roda na fase aplicar."""
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    for tabela in TABELAS_COPIADAS:
        if tabela_existe(cur, SCHEMA, tabela):
            continue
        # Copia colunas, defaults, CHECKs, índices/PK/UNIQUE. FKs não são
        # copiadas pelo LIKE — de propósito, nada aqui aponta para `public`.
        cur.execute(
            f"CREATE TABLE {SCHEMA}.{tabela} (LIKE public.{tabela} "
            f"INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES "
            f"INCLUDING IDENTITY INCLUDING GENERATED)"
        )
        print(f"  criada {SCHEMA}.{tabela}")

    # O LIKE copia `DEFAULT nextval('public.x_seq')` — trocaria ids com o
    # Brasileirão. Cada coluna serial ganha sequência própria em `ligas`.
    fora = [(t, c) for t, c, schema_seq, _s in sequencias_do_schema(cur) if schema_seq != SCHEMA]
    for tabela, coluna in fora:
        seq = f"{SCHEMA}.{tabela}_{coluna}_seq"
        cur.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq}")
        cur.execute(f"ALTER TABLE {SCHEMA}.{tabela} ALTER COLUMN {coluna} SET DEFAULT nextval('{seq}')")
        cur.execute(f"ALTER SEQUENCE {seq} OWNED BY {SCHEMA}.{tabela}.{coluna}")
        print(f"  sequência própria: {SCHEMA}.{tabela}.{coluna} -> {seq}")

    # Colunas que só existem aqui: a liga e a temporada da API (que nas
    # ligas europeias NÃO é o ano da data do jogo).
    cur.execute(f"ALTER TABLE {SCHEMA}.jogos ADD COLUMN IF NOT EXISTS liga_api_id INTEGER")
    cur.execute(f"ALTER TABLE {SCHEMA}.jogos ADD COLUMN IF NOT EXISTS temporada INTEGER")
    cur.execute(f"ALTER TABLE {SCHEMA}.times ADD COLUMN IF NOT EXISTS liga_api_id INTEGER")

    for tabela in TABELAS_COM_INDICE_JOGO:
        cur.execute(f"CREATE INDEX IF NOT EXISTS {tabela}_jogo_id_idx ON {SCHEMA}.{tabela} (jogo_id)")

    # Leitura para a auditoria pelo /api/consulta (papel claude_leitura).
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'claude_leitura'")
    if cur.fetchone():
        cur.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO claude_leitura")
        cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO claude_leitura")
        cur.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} GRANT SELECT ON TABLES TO claude_leitura")
    else:
        print("  ⚠️ papel claude_leitura não existe — leitura pela /api/consulta não vai enxergar o schema.")


# ---------------------------------------------------------------------------
# relatório (só leitura, nomes qualificados)
# ---------------------------------------------------------------------------

def relatorio_liga(cur, liga_api_id, nome_liga):
    if not schema_existe(cur) or not tabela_existe(cur, SCHEMA, "jogos"):
        print(f"\n{nome_liga}: schema `{SCHEMA}` ainda não existe — nada coletado.")
        return
    cur.execute(
        f"""
        SELECT j.temporada,
               COUNT(DISTINCT j.fixture_id_api) AS fixtures,
               COUNT(DISTINCT j.fixture_id_api) FILTER (WHERE j.placar_corinthians IS NOT NULL) AS com_placar,
               COUNT(DISTINCT j.fixture_id_api) FILTER (WHERE EXISTS (
                   SELECT 1 FROM {SCHEMA}.estatisticas_jogo e WHERE e.jogo_id = j.id)) AS com_estatistica,
               COUNT(DISTINCT j.fixture_id_api) FILTER (WHERE EXISTS (
                   SELECT 1 FROM {SCHEMA}.escalacoes s WHERE s.jogo_id = j.id)) AS com_escalacao,
               COUNT(*) AS linhas
        FROM {SCHEMA}.jogos j
        WHERE j.liga_api_id = %s
        GROUP BY j.temporada ORDER BY j.temporada DESC
        """,
        (liga_api_id,),
    )
    linhas = cur.fetchall()
    print(f"\n{nome_liga} em `{SCHEMA}` (por temporada da API):")
    if not linhas:
        print("  (nenhum jogo ainda)")
        return
    print(f"  {'temp':>6} {'fixtures':>9} {'c/placar':>9} {'c/estat':>8} {'c/escal':>8} {'linhas':>7}")
    for temporada, fixtures, placar, estat, escal, n in linhas:
        print(f"  {temporada:>6} {fixtures:>9} {placar:>9} {estat:>8} {escal:>8} {n:>7}")


def requisicoes_hoje(cur):
    cur.execute("SELECT requisicoes FROM public.controle_api_uso WHERE dia = CURRENT_DATE")
    row = cur.fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# coleta
# ---------------------------------------------------------------------------

def pausa():
    time.sleep(PAUSA_ENTRE_CHAMADAS)


def _e_passageiro(erro):
    """5xx, queda de conexão ou timeout = passageiro. 4xx NÃO (chave
    errada, parâmetro errado) — esse tem que estourar na hora."""
    if isinstance(erro, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(erro, requests.HTTPError):
        resposta = getattr(erro, "response", None)
        return resposta is not None and resposta.status_code >= 500
    return False


def com_retentativa(conn, funcao, *args):
    """Chama uma função de busca do popular_banco; em falha passageira,
    espera e tenta de novo. Cada tentativa CONTA na cota (o `chamar_api`
    soma antes de olhar o status) — por isso são poucas.

    Antes de esperar, faz commit: neste ponto a única coisa pendente na
    transação é o incremento do contador de cota (as buscas acontecem
    ANTES das gravações de cada etapa, e cada etapa anterior já foi
    commitada). Assim o contador não se perde se a execução cair depois."""
    for tentativa, espera in enumerate(ESPERAS_RETENTATIVA_SEGUNDOS + (None,), start=1):
        try:
            return funcao(*args)
        except Exception as erro:  # LimiteDiarioAtingido e 4xx passam direto
            if not _e_passageiro(erro):
                raise
            conn.commit()
            if espera is None:
                raise FalhaPassageiraPersistente(
                    f"{erro} — desisti depois de {tentativa} tentativas") from erro
            print(f"  ⚠️ falha passageira da API ({erro}). "
                  f"Tentativa {tentativa}/{len(ESPERAS_RETENTATIVA_SEGUNDOS) + 1}; "
                  f"esperando {espera}s.")
            time.sleep(espera)


def buscar_calendario(conn, liga_api_id, temporada):
    """1 chamada: todos os jogos da liga na temporada."""
    dados = com_retentativa(conn, pb.chamar_api, "fixtures", {"league": liga_api_id, "season": temporada})
    pausa()
    if dados.get("errors"):
        print(f"  ⚠️ aviso da API ({temporada}): {dados['errors']}")
        return []
    return dados["response"]


def registrar_perspectiva(cur, fixture, api_id, nome, liga_api_id, nome_liga, temporada):
    """Time + linha de `jogos` da perspectiva desse time, com as funções
    do popular_banco. Depois corrige o que lá é fixo do Brasileirão."""
    time_id = pb.get_or_create_time(cur, api_id, nome)
    cur.execute("UPDATE times SET rastreado = TRUE, liga_api_id = %s WHERE id = %s", (liga_api_id, time_id))
    jogo_id = pb.get_or_create_jogo(cur, fixture, time_id, api_id)
    cur.execute(
        "UPDATE jogos SET competicao = %s, liga_api_id = %s, temporada = %s WHERE id = %s",
        (nome_liga, liga_api_id, temporada, jogo_id),
    )
    return jogo_id


def o_que_falta(cur, jogo_id):
    """Mesmos critérios do laço do popular_banco, com as mesmas funções."""
    janela_ev = pb.dentro_da_janela_de_espera(cur, jogo_id, pb.JANELA_ESPERA_EVENTOS_HORAS)
    janela_st = pb.dentro_da_janela_de_espera(cur, jogo_id, pb.JANELA_ESPERA_ESTATISTICA_HORAS)
    return {
        "janela_eventos": janela_ev,
        "janela_estatisticas": janela_st,
        "eventos": not pb.jogo_ja_processado(cur, jogo_id) or janela_ev,
        "estatisticas": not pb.jogo_tem_estatisticas(cur, jogo_id) or janela_st,
        "jogadores": not pb.jogo_tem_estatisticas_jogador(cur, jogo_id) or janela_st,
        "escalacao": not pb.jogo_tem_escalacao(cur, jogo_id),
    }


def processar_fixture(conn, cur, fixture, liga_api_id, nome_liga, temporada, contagem):
    fixture_id = fixture["fixture"]["id"]
    home = fixture["teams"]["home"]
    away = fixture["teams"]["away"]
    status = fixture["fixture"]["status"]["short"]

    perspectivas = []
    for lado in (home, away):
        jogo_id = registrar_perspectiva(cur, fixture, lado["id"], lado["name"], liga_api_id, nome_liga, temporada)
        perspectivas.append((jogo_id, lado["id"]))
    conn.commit()

    if status not in pb.STATUS_JOGO_FINALIZADO:
        contagem["futuros"] += 1
        return

    faltas = [(jogo_id, api_id, o_que_falta(cur, jogo_id)) for jogo_id, api_id in perspectivas]
    if not any(f[k] for _, _, f in faltas for k in ("eventos", "estatisticas", "jogadores", "escalacao")):
        contagem["pulados"] += 1
        return

    # EVENTOS: 1 chamada, gravada por perspectiva (o `lado` de gols/cartões
    # é relativo ao NOSSO time — por isso salvar_eventos recebe o api_id).
    if any(f["eventos"] for _, _, f in faltas):
        eventos = com_retentativa(conn, pb.buscar_eventos, fixture_id)
        pausa()
        for jogo_id, api_id, f in faltas:
            if not f["eventos"]:
                continue
            if f["janela_eventos"] and pb.jogo_ja_processado(cur, jogo_id):
                for tabela in ("gols", "cartoes", "substituicoes"):
                    cur.execute(f"DELETE FROM {tabela} WHERE jogo_id = %s", (jogo_id,))
            pb.salvar_eventos(cur, jogo_id, eventos, api_id)
        conn.commit()

    # ESTATÍSTICAS DE TIME: `lado` é o mando REAL (home_team_id), igual
    # nas duas perspectivas.
    if any(f["estatisticas"] for _, _, f in faltas):
        estatisticas = com_retentativa(conn, pb.buscar_estatisticas, fixture_id)
        pausa()
        for jogo_id, _api_id, f in faltas:
            if not f["estatisticas"]:
                continue
            if f["janela_estatisticas"] and pb.jogo_tem_estatisticas(cur, jogo_id):
                cur.execute("DELETE FROM estatisticas_jogo WHERE jogo_id = %s", (jogo_id,))
            pb.salvar_estatisticas(cur, jogo_id, estatisticas, home["id"])
        conn.commit()

    if any(f["jogadores"] for _, _, f in faltas):
        jogadores = com_retentativa(conn, pb.buscar_estatisticas_jogadores, fixture_id)
        pausa()
        for jogo_id, _api_id, f in faltas:
            if not f["jogadores"]:
                continue
            if f["janela_estatisticas"] and pb.jogo_tem_estatisticas_jogador(cur, jogo_id):
                cur.execute("DELETE FROM jogador_estatisticas_jogo WHERE jogo_id = %s", (jogo_id,))
            pb.salvar_estatisticas_jogadores(cur, jogo_id, jogadores, home["id"])
        conn.commit()

    # ESCALAÇÃO por último: cruza com `substituicoes` já gravada.
    if any(f["escalacao"] for _, _, f in faltas):
        lineups = com_retentativa(conn, pb.buscar_escalacao, fixture_id)
        pausa()
        for jogo_id, _api_id, f in faltas:
            if f["escalacao"]:
                pb.salvar_escalacao(cur, jogo_id, lineups)
        conn.commit()

    contagem["processados"] += 1


# ---------------------------------------------------------------------------
# fases
# ---------------------------------------------------------------------------

def fase_verificar(liga_chave, teto, temporadas):
    liga_api_id, nome_liga = LIGAS[liga_chave]
    conn = conectar()
    cur = conn.cursor()
    try:
        print(f"search_path da sessão: ", end="")
        cur.execute("SHOW search_path")
        print(cur.fetchone()[0])

        existe = schema_existe(cur)
        print(f"schema `{SCHEMA}` existe: {'sim' if existe else 'NÃO (será criado no aplicar)'}")
        legado = jogos_da_liga(cur, SCHEMA_LEGADO, liga_api_id)
        novo = jogos_da_liga(cur, SCHEMA, liga_api_id) if existe else 0
        if legado and novo < legado:
            print(f"⚠️ MIGRAÇÃO PENDENTE: `{SCHEMA_LEGADO}` tem {legado} linha(s) de jogos dessa liga, "
                  f"`{SCHEMA}` tem {novo}. O aplicar vai RECUSAR até migrar.")
        elif legado:
            print(f"migração: ok (`{SCHEMA_LEGADO}` {legado} · `{SCHEMA}` {novo} linhas de jogos dessa liga)")
        if existe:
            faltando = [t for t in TABELAS_COPIADAS if not tabela_existe(cur, SCHEMA, t)]
            print(f"tabelas faltando em `{SCHEMA}`: {faltando or 'nenhuma'}")
            if not faltando:
                garantir_isolamento(cur)

        publico = contagens_public(cur)
        print("\n`public` (Brasileirão), só para referência — este script NÃO grava lá:")
        for tabela, n in publico.items():
            print(f"  public.{tabela}: {n}")

        usadas = requisicoes_hoje(cur)
        print(f"\ncota hoje: {usadas} usadas no contador compartilhado · teto deste script: {teto} "
              f"· disponível para ele: {max(teto - usadas, 0)}")

        relatorio_liga(cur, liga_api_id, nome_liga)
        print("\nEstimativa: ~380 jogos por temporada completa (306 na Bundesliga) x 4 chamadas "
              "+ 1 chamada de calendário por temporada.")
    finally:
        conn.rollback()  # incondicional: esta fase nunca grava
        cur.close()
        conn.close()
    print("\n" + "=" * 78)
    print("FASE VERIFICAR — nenhuma tabela foi tocada (nem `controle_api_uso`) e a API não foi chamada.")
    print("=" * 78)


def fase_aplicar(liga_chave, teto, temporadas):
    liga_api_id, nome_liga = LIGAS[liga_chave]
    conn = conectar()
    cur = conn.cursor()
    cur_contador = conn.cursor()
    contagem = {"processados": 0, "pulados": 0, "futuros": 0}
    falhados = []
    publico_antes = None
    try:
        publico_antes = contagens_public(cur)

        checar_migracao(cur, liga_api_id)   # 🔴 antes de criar qualquer coisa
        criar_estrutura(cur)
        garantir_isolamento(cur)   # 🔴 antes de QUALQUER gravação de dado
        conn.commit()

        # Timeout só neste processo (ver TIMEOUT_API_SEGUNDOS).
        pb.requests = types.SimpleNamespace(
            get=functools.partial(requests.get, timeout=TIMEOUT_API_SEGUNDOS))

        # Cota: mesmo contador do Brasileirão, teto próprio.
        pb.LIMITE_REQUISICOES_DIA = teto
        pb._cursor_para_contador = cur_contador
        pb.requisicoes_usadas = pb.carregar_requisicoes_usadas_hoje(cur_contador)
        conn.commit()
        print(f"\ncota hoje antes de começar: {pb.requisicoes_usadas} (teto {teto})")
        if pb.requisicoes_usadas >= teto:
            print("Teto do dia já atingido — nada a fazer hoje. Amanhã continua.")
            return

        for temporada in temporadas:
            print(f"\n---------- {nome_liga} · temporada {temporada} ----------")
            try:
                fixtures = buscar_calendario(conn, liga_api_id, temporada)
            except FalhaPassageiraPersistente as erro:
                raise ApiForaDoAr(f"calendário da temporada {temporada} não veio ({erro}).") from erro
            print(f"  {len(fixtures)} jogos no calendário da API")
            antes = dict(contagem)
            falhas_seguidas = 0
            for fixture in fixtures:
                try:
                    processar_fixture(conn, cur, fixture, liga_api_id, nome_liga, temporada, contagem)
                    falhas_seguidas = 0
                except FalhaPassageiraPersistente as erro:
                    conn.commit()  # só o contador está pendente (ver com_retentativa)
                    fid = fixture["fixture"]["id"]
                    falhados.append(fid)
                    falhas_seguidas += 1
                    print(f"  ⏭️ jogo {fid} pulado por falha da API ({erro}). "
                          f"A próxima execução completa o que faltou.")
                    if falhas_seguidas >= FALHAS_SEGUIDAS_PARA_PARAR:
                        raise ApiForaDoAr(
                            f"{falhas_seguidas} jogos seguidos falharam — a API parece fora do ar. "
                            "Parando para não gastar cota; a próxima execução continua.")
            print(f"  temporada {temporada}: +{contagem['processados'] - antes['processados']} processados, "
                  f"+{contagem['pulados'] - antes['pulados']} já completos, "
                  f"+{contagem['futuros'] - antes['futuros']} ainda não jogados. "
                  f"Cota usada no dia: {pb.requisicoes_usadas}")

        print("\n✅ Todas as temporadas pedidas percorridas.")

    except pb.LimiteDiarioAtingido as e:
        conn.commit()
        print(f"\n⏸️ {e}")
        print("   (parada normal — a próxima execução continua de onde parou)")

    except ApiForaDoAr as e:
        conn.commit()
        print(f"\n⏸️ {e}")

    except MigracaoPendente as e:
        conn.rollback()
        print(f"\n🔴 MIGRAÇÃO PENDENTE — {e}")
        raise

    except IsolamentoQuebrado as e:
        conn.rollback()
        print(f"\n🔴 {e}")
        raise

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise

    finally:
        try:
            conn.rollback()
            print(f"\nresumo: {contagem['processados']} jogos processados, {contagem['pulados']} já completos, "
                  f"{contagem['futuros']} ainda não jogados · cota usada no dia: {pb.requisicoes_usadas}")
            if falhados:
                print(f"⚠️ {len(falhados)} jogo(s) pulado(s) por falha da API — completados na próxima execução: "
                      f"{falhados}")
            relatorio_liga(cur, liga_api_id, nome_liga)
            if publico_antes is not None:
                publico_depois = contagens_public(cur)
                mudou = {t: (publico_antes[t], publico_depois[t]) for t in publico_antes
                         if publico_antes[t] != publico_depois[t]}
                if mudou:
                    print(f"\n⚠️ `public` mudou durante a execução: {mudou}")
                    print("   Os crons do Brasileirão podem ter rodado ao mesmo tempo — conferir o horário. "
                          "Este script não grava em `public`.")
                else:
                    print(f"\n✅ `public` intocado: {len(publico_antes)} tabelas do Brasileirão com a mesma contagem de antes.")
        finally:
            cur.close()
            cur_contador.close()
            conn.close()


def ler_argumentos(argv):
    if len(argv) < 2 or argv[1] not in ("verificar", "aplicar") or "--liga" not in argv:
        print(__doc__)
        print("Uso: python coletar_ligas.py verificar|aplicar --liga premier [--teto 6500] [--temporadas 2026,2025]")
        print(f"Ligas: {', '.join(LIGAS)}")
        sys.exit(1)
    fase = argv[1]
    liga = argv[argv.index("--liga") + 1]
    if liga not in LIGAS:
        print(f"Liga desconhecida: {liga}. Opções: {', '.join(LIGAS)}")
        sys.exit(1)
    teto = TETO_PADRAO
    if "--teto" in argv:
        teto = int(argv[argv.index("--teto") + 1])
        if teto > 7000:
            print("Teto acima de 7.000 recusado: o popular_banco para em 7.000, e passar disso "
                  "tiraria a cota do Brasileirão.")
            sys.exit(1)
    temporadas = TEMPORADAS_PADRAO
    if "--temporadas" in argv:
        temporadas = [int(t) for t in argv[argv.index("--temporadas") + 1].split(",")]
    return fase, liga, teto, temporadas


def main():
    fase, liga, teto, temporadas = ler_argumentos(sys.argv)
    definir_liga(liga)
    cabecalho(fase, liga, teto, temporadas)
    if fase == "verificar":
        fase_verificar(liga, teto, temporadas)
    else:
        fase_aplicar(liga, teto, temporadas)


if __name__ == "__main__":
    main()
