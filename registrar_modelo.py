"""
registrar_modelo.py — a foto, ANTES do apito, da probabilidade do MODELO de
produção para cada linha que a casa ofereceria. Grava SÓ em
`public.registro_modelo`. Arquitetura: `claude/arquitetura_registro_modelo.md`.

    python registrar_modelo.py verificar [--horas 48] [--equivalencia]
    python registrar_modelo.py aplicar   [--horas 48]

  verificar     calcula tudo, imprime contagens e amostras, NÃO grava nada.
  aplicar       calcula e grava a foto (uma transação, commit no fim).
  --horas       jogos do Brasileirão que começam dentro destas horas (padrão 48).
  --equivalencia  (só no verificar) roda o motor uma 2ª vez, com a odd REAL da
                casa e o encolhimento LIGADO, e confere que o resultado é
                exatamente o encolhimento de produção aplicado ao p_modelo
                gravado (critério C2). Dobra o tempo de cálculo.

--------------------------------------------------------------------------
COMO FUNCIONA (nada é reescrito — tudo é importado)
--------------------------------------------------------------------------

1. CONEXÃO DE CÁLCULO EM MODO SÓ LEITURA (`SET SESSION CHARACTERISTICS AS
   TRANSACTION READ ONLY`). O PRÓPRIO BANCO recusa qualquer escrita feita
   pelo `atualizar_odds` ou pelo `motor_recomendacoes` neste processo.
2. Jogos futuros lidos de `jogos` (as duas perspectivas).
3. Calendário da OddsPapi (1 chamada) → cada jogo nosso casado com o fixture
   da OddsPapi pelos `oddspapi_participant_id` dos dois times + data (±36h).
4. 1 folha de odds por jogo, só da casa de referência (`superbet.rs`), com a
   chave GRÁTIS `ODDSPAPI_KEY_TESTE`.
5. A folha passa por `atualizar_odds.salvar_odds_do_jogo` DE PRODUÇÃO, uma
   vez por perspectiva (na ordem do cron: por nome do time), com um cursor
   que lê de verdade mas:
     - ANOTA o `INSERT INTO odds` em vez de executar;
     - responde o "já existe odd de jogador?" com o que já anotou (a mesma
       deduplicação entre perspectivas que o cron faz pelo banco);
     - NUNCA cria jogador: nome desconhecido vira id negativo e a linha é
       descartada (e contada).
6. As linhas anotadas entram no motor no lugar de `buscar_odds_futuras`, com
   o encolhimento trocado pela identidade e uma odd fictícia ÚNICA por linha
   (1000 + índice): o filtro VE > 0 não descarta nada com p > 0, e o índice
   devolvido em `odd_oferecida` liga cada resultado à sua linha e à odd real.
   As duas trocas são atribuições no módulo importado — o arquivo do motor
   não muda, e o cron do Brasileirão não é afetado.
7. Conexão de ESCRITA separada, só `INSERT INTO public.registro_modelo`.

Conferência em toda execução: contagem de `public.odds`, `recomendacoes` e
`jogadores` antes e depois — tem que ser igual.

Nada aqui troca a casa do projeto: `public.odds` continua sem a superbet.rs.
"""

import os
import sys
from datetime import datetime, timezone

import psycopg2

import atualizar_odds as ao            # funções de PRODUÇÃO (OddsPapi, filtro de mercado, descrição)
from combinacoes import MERCADOS_JOGADOR  # o conjunto de PRODUÇÃO
import motor_recomendacoes as mr       # motor de PRODUÇÃO

DATABASE_URL = os.environ["DATABASE_URL"]
CHAVE_TESTE = os.environ.get("ODDSPAPI_KEY_TESTE")
CASA_REFERENCIA = os.environ.get("REGISTRO_MODELO_CASA", "superbet.rs")
VERSAO_CODIGO = (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "desconhecida")[:12]

ODD_FICTICIA_BASE = 1000
TOLERANCIA_DATA_HORAS = 36
TABELAS_VIGIADAS = ["odds", "recomendacoes", "jogadores"]


def mascarar(texto):
    texto = str(texto)
    for chave in (CHAVE_TESTE, ao.API_KEY):
        if chave:
            texto = texto.replace(chave, "***")
    return texto


# ---------------------------------------------------------------------------
# cursor que lê de verdade e anota as escritas
# ---------------------------------------------------------------------------
class CursorAnotador:
    """Envolve um cursor REAL (de sessão só-leitura). SELECT passa. INSERT em
    `odds` é anotado; INSERT em `jogadores` devolve id negativo (jogador
    desconhecido, a linha será descartada); a checagem de duplicata de odd de
    jogador é respondida com as anotações. Qualquer outra escrita chega ao
    banco — e o banco a recusa (sessão READ ONLY)."""

    def __init__(self, cur_real, fixture_id_api, anotadas_do_fixture):
        self.cur = cur_real
        self.fixture_id_api = fixture_id_api
        self.anotadas = anotadas_do_fixture   # compartilhada entre as perspectivas do mesmo jogo
        self.odds = []                        # só desta perspectiva
        self.jogadores_novos = {}
        self._pendente = None

    def execute(self, sql, params=None):
        texto = " ".join(sql.split()).upper()
        self._pendente = None
        if texto.startswith("INSERT INTO ODDS"):
            jogo_id, jogador_id, casa, mercado, valor, linha, direcao = params
            odd = {"jogador_id": jogador_id, "casa": casa, "mercado": mercado,
                   "odd": valor, "linha": linha, "direcao": direcao}
            self.odds.append(odd)
            if jogador_id is not None:
                self.anotadas.add((jogador_id, casa, mercado, linha, direcao))
            return
        if texto.startswith("INSERT INTO JOGADORES"):
            nome = params[0]
            if nome not in self.jogadores_novos:
                self.jogadores_novos[nome] = -(len(self.jogadores_novos) + 1)
            self._pendente = (self.jogadores_novos[nome],)
            return
        if texto.startswith("SELECT 1 FROM ODDS O JOIN JOGOS J"):
            # ja_existe_odd_jogador: (fixture_id_api, jogador_id, casa, mercado, linha, direcao)
            _fx, jogador_id, casa, mercado, linha, direcao = params
            existe = (jogador_id, casa, mercado, linha, direcao) in self.anotadas
            self._pendente = (1,) if existe else None
            return
        self.cur.execute(sql, params)
        self._pendente = "REAL"

    def fetchone(self):
        if self._pendente == "REAL":
            return self.cur.fetchone()
        r, self._pendente = self._pendente, None
        return r

    def fetchall(self):
        if self._pendente == "REAL":
            return self.cur.fetchall()
        return []


# ---------------------------------------------------------------------------
# leitura
# ---------------------------------------------------------------------------
SQL_JOGOS = """
    SELECT j.id, j.fixture_id_api, j.datahora_jogo, j.data_jogo, j.adversario, j.mandante, j.arbitro,
           j.mandante_id, j.visitante_id, j.nosso_time_id, j.rodada_numero,
           EXTRACT(YEAR FROM j.data_jogo)::int, t.nome, t.oddspapi_participant_id
    FROM jogos j
    JOIN times t ON t.id = j.nosso_time_id
    WHERE j.datahora_jogo > (now() AT TIME ZONE 'UTC')
      AND j.datahora_jogo <= (now() AT TIME ZONE 'UTC') + %s * interval '1 hour'
      AND j.data_jogo >= DATE '2026-01-01'
    ORDER BY j.datahora_jogo, j.fixture_id_api, t.nome
"""


def contagens(cur):
    r = {}
    for t in TABELAS_VIGIADAS:
        cur.execute(f"SELECT COUNT(*) FROM public.{t}")
        r[t] = cur.fetchone()[0]
    return r


def ler_jogos(cur, horas):
    cur.execute(SQL_JOGOS, (horas,))
    cols = ["jogo_id", "fixture_id_api", "datahora_jogo", "data_jogo", "adversario", "mandante", "arbitro",
            "mandante_id", "visitante_id", "nosso_time_id", "rodada_numero", "temporada",
            "nosso_nome", "nosso_pid"]
    por_fixture = {}
    for row in cur.fetchall():
        p = dict(zip(cols, row))
        por_fixture.setdefault(p["fixture_id_api"], []).append(p)
    return por_fixture


def participantes(cur):
    cur.execute("SELECT id, oddspapi_participant_id FROM times WHERE oddspapi_participant_id IS NOT NULL")
    return {tid: pid for tid, pid in cur.fetchall()}


def casar_fixtures(por_fixture, pids, calendario):
    """fixture nosso → fixture da OddsPapi, pelos dois participantes e pela data."""
    casados, sem_par = {}, []
    for fx, perspectivas in por_fixture.items():
        p0 = perspectivas[0]
        alvo = {pids.get(p0["mandante_id"]), pids.get(p0["visitante_id"])}
        quando = p0["datahora_jogo"]
        if isinstance(quando, str):
            quando = datetime.fromisoformat(quando)
        quando = quando.replace(tzinfo=timezone.utc)
        achado = None
        for jo in calendario:
            if {jo.get("participant1Id"), jo.get("participant2Id")} != alvo or None in alvo:
                continue
            inicio = datetime.fromisoformat(str(jo.get("startTime", "")).replace("Z", "+00:00"))
            if abs((inicio - quando).total_seconds()) <= TOLERANCIA_DATA_HORAS * 3600:
                achado = jo
                break
        if achado:
            casados[fx] = achado
        else:
            sem_par.append(fx)
    return casados, sem_par


# ---------------------------------------------------------------------------
# cálculo
# ---------------------------------------------------------------------------
def montar_linhas(cur_leitura, por_fixture, casados, catalogo):
    """Folha da OddsPapi → linhas no formato de `buscar_odds_futuras`.
    Devolve (linhas, referencia[indice] = dict, relatorio por fixture)."""
    linhas, referencia, relatorio = [], {}, {}
    for fx, jo in casados.items():
        rel = {"jogo": f"{jo.get('participant1Name')} x {jo.get('participant2Name')}",
               "odds_por_perspectiva": [], "descartadas_jogador_desconhecido": 0, "erro": None}
        relatorio[fx] = rel
        try:
            dados = ao.buscar_odds(jo["fixtureId"], bookmakers=[CASA_REFERENCIA])
        except Exception as e:
            rel["erro"] = mascarar(e)[:200]
            continue
        if not ao.existem_odds_utilizaveis(dados):
            rel["erro"] = "sem odds utilizáveis (casa ausente ou suspensa)"
            continue
        anotadas = set()
        for p in sorted(por_fixture[fx], key=lambda x: x["nosso_nome"]):
            eh_mandante = jo.get("participant1Id") == p["nosso_pid"]
            adversario_oddspapi = jo.get("participant2Name") if eh_mandante else jo.get("participant1Name")
            cur = CursorAnotador(cur_leitura, fx, anotadas)
            ao.salvar_odds_do_jogo(cur, jogo_id=p["jogo_id"], dados_odds=dados, catalogo_mercados=catalogo,
                                   mandante=eh_mandante, adversario=adversario_oddspapi,
                                   nosso_nome=p["nosso_nome"], fixture_id_api=fx)
            validas = 0
            for o in cur.odds:
                if o["jogador_id"] is not None and o["jogador_id"] < 0:
                    rel["descartadas_jogador_desconhecido"] += 1
                    continue
                indice = len(linhas)
                referencia[indice] = {"perspectiva": p, "mercado": o["mercado"], "odd_ref": o["odd"],
                                      "casa": o["casa"]}
                linhas.append((
                    -(indice + 1), p["jogo_id"], o["jogador_id"], o["casa"], o["mercado"],
                    ODD_FICTICIA_BASE + indice, o["linha"], o["direcao"], p["data_jogo"],
                    p["adversario"], p["mandante"], p["arbitro"], p["mandante_id"], p["visitante_id"],
                    p["nosso_time_id"], p["rodada_numero"], p["temporada"],
                ))
                validas += 1
            rel["odds_por_perspectiva"].append((p["nosso_nome"], len(cur.odds), validas))
    return linhas, referencia, relatorio


def rodar_motor(cur_leitura, linhas, encolher_ligado=False, odds_reais=None):
    """Roda o `calcular_recomendacoes` de produção sobre as linhas dadas."""
    buscar_original = mr.buscar_odds_futuras
    encolher_original = mr.encolher_para_o_mercado
    entrada = linhas
    if odds_reais is not None:
        entrada = [l[:5] + (odds_reais[i],) + l[6:] for i, l in enumerate(linhas)]
    try:
        mr.buscar_odds_futuras = lambda cur: entrada
        if not encolher_ligado:
            mr.encolher_para_o_mercado = lambda frequencia, valor_odd, tipo_padrao: (frequencia, False)
        return mr.calcular_recomendacoes(cur_leitura)
    finally:
        mr.buscar_odds_futuras = buscar_original
        mr.encolher_para_o_mercado = encolher_original


def para_foto(recomendacoes, referencia):
    foto, sem_indice = [], 0
    for r in recomendacoes:
        indice = int(round(float(r["odd_oferecida"]))) - ODD_FICTICIA_BASE
        ref = referencia.get(indice)
        if ref is None:
            sem_indice += 1
            continue
        p = ref["perspectiva"]
        foto.append({
            "indice": indice, "fixture_id_api": p["fixture_id_api"], "jogo_id": r["jogo_id"],
            "datahora_jogo": p["datahora_jogo"], "nosso_time_id": p["nosso_time_id"],
            "tipo_padrao": r["tipo_padrao"], "linha": r["linha"], "direcao": r["direcao"],
            "jogador_id": r["jogador_id"], "mercado": ref["mercado"][:255], "descricao": r["descricao"],
            "p_modelo": r["probabilidade_historica"], "casa_ref": ref["casa"], "odd_ref": ref["odd_ref"],
        })
    return foto, sem_indice


SQL_INSERIR = """
    INSERT INTO public.registro_modelo
        (fixture_id_api, jogo_id, datahora_jogo, nosso_time_id, tipo_padrao, linha, direcao, jogador_id,
         mercado, descricao, p_modelo, casa_ref, odd_ref, versao_codigo)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


# ---------------------------------------------------------------------------
# relatório
# ---------------------------------------------------------------------------
def imprimir(relatorio, foto, sem_indice, linhas):
    print("\n## POR JOGO")
    for fx, rel in relatorio.items():
        if rel["erro"]:
            print(f"  {fx} {rel['jogo']}: ⚠️ {rel['erro']}")
            continue
        persp = " · ".join(f"{n}: {tot} anotadas/{val} válidas" for n, tot, val in rel["odds_por_perspectiva"])
        print(f"  {fx} {rel['jogo']}: {persp} · jogador desconhecido descartado: "
              f"{rel['descartadas_jogador_desconhecido']}")
    print(f"\n  linhas enviadas ao motor: {len(linhas)} · linhas na foto: {len(foto)}"
          f" · sem índice (não deveria acontecer): {sem_indice}")

    por_tipo = {}
    for f in foto:
        por_tipo.setdefault(f["tipo_padrao"], []).append(f)
    print("\n## POR TIPO (amostra de 3 descrições — conferir à mão, critério C3)")
    jogador_sem_id = 0
    for tipo in sorted(por_tipo):
        itens = por_tipo[tipo]
        if tipo in MERCADOS_JOGADOR:
            jogador_sem_id += sum(1 for f in itens if f["jogador_id"] is None)
        ps = sorted(float(f["p_modelo"]) for f in itens)
        print(f"  {tipo:<20} {len(itens):>6} linhas · p_modelo mín {ps[0]:.1f} / mediana "
              f"{ps[len(ps) // 2]:.1f} / máx {ps[-1]:.1f}")
        for f in itens[:3]:
            print(f"      p={float(f['p_modelo']):5.1f}  odd_ref={f['odd_ref']}  {f['mercado'][:90]}")
    print(f"\n  linhas de mercado de JOGADOR sem jogador_id: {jogador_sem_id} (critério C3: tem que ser 0)")
    return jogador_sem_id


def conferir_equivalencia(cur_leitura, linhas, referencia, foto):
    """C2: motor com a odd REAL e o encolhimento de produção LIGADO deve dar,
    para cada linha que sobrevive ao filtro, exatamente
    encolher_para_o_mercado(p_modelo, odd_real, tipo). Liga o resultado real
    à foto por (jogo, jogador, tipo, linha, direção, odd) — chave sem par
    único é contada e ignorada, nunca adivinhada."""
    odds_reais = [referencia[i]["odd_ref"] for i in range(len(linhas))]
    reais = rodar_motor(cur_leitura, linhas, encolher_ligado=True, odds_reais=odds_reais)
    indice = {}
    for f in foto:
        chave = (f["jogo_id"], f["jogador_id"], f["tipo_padrao"], str(f["linha"]), f["direcao"],
                 float(f["odd_ref"]))
        indice.setdefault(chave, []).append(f)
    conferidas, divergentes, ambiguas = 0, [], 0
    for r in reais:
        chave = (r["jogo_id"], r["jogador_id"], r["tipo_padrao"], str(r["linha"]), r["direcao"],
                 float(r["odd_oferecida"]))
        candidatos = indice.get(chave, [])
        if len(candidatos) != 1:
            ambiguas += 1
            continue
        f = candidatos[0]
        esperado, _ = mr.encolher_para_o_mercado(float(f["p_modelo"]), r["odd_oferecida"], r["tipo_padrao"])
        conferidas += 1
        if abs(float(esperado) - float(r["probabilidade_historica"])) > 0.011:
            divergentes.append((r["tipo_padrao"], f["p_modelo"], esperado, r["probabilidade_historica"]))
    print(f"\n## EQUIVALÊNCIA (C2): {len(reais)} linhas passaram no filtro com a odd real · "
          f"{conferidas} conferidas · {len(divergentes)} divergentes · {ambiguas} sem par único (ignoradas)")
    for d in divergentes[:10]:
        print(f"    ❌ {d[0]}: p_modelo {d[1]} → esperado {d[2]} · motor deu {d[3]}")
    return conferidas, divergentes


# ---------------------------------------------------------------------------
def ler_opcoes(argv):
    opcoes = {"horas": 48.0, "equivalencia": False}
    i = 2
    while i < len(argv):
        if argv[i] == "--horas":
            opcoes["horas"] = float(argv[i + 1])
            i += 2
        elif argv[i] == "--equivalencia":
            opcoes["equivalencia"] = True
            i += 1
        else:
            raise SystemExit(f"opção desconhecida: {argv[i]}")
    return opcoes


def cabecalho(fase, opcoes):
    print("=" * 78)
    print(f"registrar_modelo.py | fase: {fase} | casa de referência: {CASA_REFERENCIA} "
          f"| versão do código: {VERSAO_CODIGO}")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"janela: {opcoes['horas']}h · equivalência: {'sim' if opcoes['equivalencia'] else 'não'}")
    print("=" * 78)
    sys.stdout.flush()


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("verificar", "aplicar"):
        print(__doc__)
        sys.exit(1)
    fase = sys.argv[1]
    opcoes = ler_opcoes(sys.argv)
    cabecalho(fase, opcoes)

    if not CHAVE_TESTE:
        print("⚠️ ODDSPAPI_KEY_TESTE ausente neste serviço — nada a fazer (a chave paga não tem acesso "
              "à casa de referência). Nada foi gravado.")
        return
    # Conferência de que o motor ainda tem os nomes que este script troca.
    for nome in ("buscar_odds_futuras", "encolher_para_o_mercado", "calcular_recomendacoes"):
        if not callable(getattr(mr, nome, None)):
            raise SystemExit(f"motor_recomendacoes.{nome} não existe mais — script desatualizado. Nada foi gravado.")
    ao.API_KEY = CHAVE_TESTE

    leitura = psycopg2.connect(DATABASE_URL)
    cur_l = leitura.cursor()
    # A sessão vira só-leitura a partir da PRÓXIMA transação (o SET não vale
    # para a transação em que é executado) — por isso o commit logo depois.
    # Pego na bancada de 07/10: sem ele, a escrita passava e só a conferência
    # das contagens a denunciava.
    cur_l.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    leitura.commit()
    cur_l.execute("SHOW transaction_read_only")
    if str(cur_l.fetchone()[0]).lower() != "on":
        raise SystemExit("🔴 a conexão de cálculo NÃO ficou só-leitura — nada foi calculado nem gravado.")
    try:
        cur_l.execute("SELECT to_regclass('public.registro_modelo') IS NOT NULL")
        if not cur_l.fetchone()[0]:
            raise SystemExit("`public.registro_modelo` não existe — rode antes: python criar_registro_modelo.py aplicar")
        antes = contagens(cur_l)

        por_fixture = ler_jogos(cur_l, opcoes["horas"])
        print(f"\njogos do Brasileirão nas próximas {opcoes['horas']:.0f}h: {len(por_fixture)}")
        if not por_fixture:
            print("nada a fotografar. Nada foi gravado.")
            return
        calendario = ao.get_com_retry_429(
            f"{ao.API_BASE}/fixtures",
            params={"tournamentId": ao.TOURNAMENT_ID, "sportId": ao.SPORT_ID, "apiKey": ao.API_KEY})
        if calendario.status_code != 200:
            raise RuntimeError(mascarar(f"calendário da OddsPapi respondeu {calendario.status_code}: "
                                        f"{calendario.text[:200]}"))
        casados, sem_par = casar_fixtures(por_fixture, participantes(cur_l), calendario.json())
        print(f"casados com a OddsPapi: {len(casados)} · sem par: {sem_par or 'nenhum'}")

        catalogo = ao.buscar_catalogo_mercados()
        linhas, referencia, relatorio = montar_linhas(cur_l, por_fixture, casados, catalogo)
        recs = rodar_motor(cur_l, linhas) if linhas else []
        foto, sem_indice = para_foto(recs, referencia)
        jogador_sem_id = imprimir(relatorio, foto, sem_indice, linhas)

        if fase == "verificar" and opcoes["equivalencia"] and linhas:
            conferir_equivalencia(cur_l, linhas, referencia, foto)

        depois = contagens(cur_l)
        if depois != antes:
            raise RuntimeError(f"🔴 tabelas vigiadas mudaram durante o cálculo: {antes} → {depois}")
        print(f"\n  tabelas vigiadas iguais antes e depois: {depois}")
    finally:
        leitura.rollback()
        cur_l.close()
        leitura.close()

    if fase == "verificar":
        print("\n" + "=" * 78)
        print("FASE VERIFICAR — nada foi gravado (nem `registro_modelo`, nem `odds`, nem outra tabela).")
        print("=" * 78)
        return
    if not foto:
        print("\nfoto vazia — nada a gravar.")
        return
    if jogador_sem_id:
        print(f"\n⚠️ {jogador_sem_id} linha(s) de mercado de jogador sem jogador_id — gravadas assim mesmo "
              f"para auditoria; a medição as exclui.")

    escrita = psycopg2.connect(DATABASE_URL)
    cur_e = escrita.cursor()
    try:
        for f in foto:
            cur_e.execute(SQL_INSERIR, (
                f["fixture_id_api"], f["jogo_id"], f["datahora_jogo"], f["nosso_time_id"], f["tipo_padrao"],
                f["linha"], f["direcao"], f["jogador_id"], f["mercado"], f["descricao"], f["p_modelo"],
                f["casa_ref"], f["odd_ref"], VERSAO_CODIGO))
        cur_e.execute("SELECT COUNT(*), MIN(registrado_em) FROM public.registro_modelo "
                      "WHERE registrado_em = now()")
        n, quando = cur_e.fetchone()
        escrita.commit()
        print(f"\n✅ foto gravada: {n} linha(s) em `registro_modelo`, registrado_em {quando}.")
    except Exception:
        escrita.rollback()
        raise
    finally:
        cur_e.close()
        escrita.close()


if __name__ == "__main__":
    main()
