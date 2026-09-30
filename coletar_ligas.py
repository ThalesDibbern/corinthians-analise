"""
coletar_ligas.py — coleta de ligas EXTRAS (Premier League etc.) num schema
SEPARADO do banco, sem tocar no Brasileirão e sem gerar recomendação.

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

import psycopg2

# Import das funções REAIS de gravação. Exige DATABASE_URL e API_FOOTBALL_KEY
# no ambiente (o popular_banco lê as duas na importação).
import popular_banco as pb

SCHEMA = "ligas"

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


class IsolamentoQuebrado(Exception):
    """Alguma tabela de dado não resolve para o schema `ligas`."""


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
    """Conexão com `search_path = ligas, public`. Tabela que existe em
    `ligas` resolve para lá; o resto (controle_api_uso) para `public`."""
    conn = psycopg2.connect(pb.DATABASE_URL, options=f"-c search_path={SCHEMA},public")
    conn.autocommit = False
    return conn


def schema_existe(cur):
    cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (SCHEMA,))
    return cur.fetchone() is not None


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


def buscar_calendario(liga_api_id, temporada):
    """1 chamada: todos os jogos da liga na temporada."""
    dados = pb.chamar_api("fixtures", {"league": liga_api_id, "season": temporada})
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
        eventos = pb.buscar_eventos(fixture_id)
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
        estatisticas = pb.buscar_estatisticas(fixture_id)
        pausa()
        for jogo_id, _api_id, f in faltas:
            if not f["estatisticas"]:
                continue
            if f["janela_estatisticas"] and pb.jogo_tem_estatisticas(cur, jogo_id):
                cur.execute("DELETE FROM estatisticas_jogo WHERE jogo_id = %s", (jogo_id,))
            pb.salvar_estatisticas(cur, jogo_id, estatisticas, home["id"])
        conn.commit()

    if any(f["jogadores"] for _, _, f in faltas):
        jogadores = pb.buscar_estatisticas_jogadores(fixture_id)
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
        lineups = pb.buscar_escalacao(fixture_id)
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
    publico_antes = None
    try:
        publico_antes = contagens_public(cur)

        criar_estrutura(cur)
        garantir_isolamento(cur)   # 🔴 antes de QUALQUER gravação de dado
        conn.commit()

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
            fixtures = buscar_calendario(liga_api_id, temporada)
            print(f"  {len(fixtures)} jogos no calendário da API")
            antes = dict(contagem)
            for fixture in fixtures:
                processar_fixture(conn, cur, fixture, liga_api_id, nome_liga, temporada, contagem)
            print(f"  temporada {temporada}: +{contagem['processados'] - antes['processados']} processados, "
                  f"+{contagem['pulados'] - antes['pulados']} já completos, "
                  f"+{contagem['futuros'] - antes['futuros']} ainda não jogados. "
                  f"Cota usada no dia: {pb.requisicoes_usadas}")

        print("\n✅ Todas as temporadas pedidas percorridas.")

    except pb.LimiteDiarioAtingido as e:
        conn.commit()
        print(f"\n⏸️ {e}")
        print("   (parada normal — a próxima execução continua de onde parou)")

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
    cabecalho(fase, liga, teto, temporadas)
    if fase == "verificar":
        fase_verificar(liga, teto, temporadas)
    else:
        fase_aplicar(liga, teto, temporadas)


if __name__ == "__main__":
    main()
