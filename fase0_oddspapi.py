"""
fase0_oddspapi.py — SÓ LEITURA. Não conecta no banco (a conexão é BLOQUEADA
no começo do script) e não grava nada em lugar nenhum.

Fase 0 da `claude/arquitetura_ligas_extras.md`: antes de escrever qualquer
código do pipeline das ligas, responder com a OddsPapi de verdade:

  1. Qual o `tournamentId` de cada liga (Premier League, La Liga, Serie A,
     Bundesliga, Ligue 1) — e confirmar que 325 é mesmo o Brasileirão.
  2. A Superbet (`superbet.bet.br`) tem odds para jogos dessas ligas?
  3. Desses mercados, QUAIS o nosso pipeline aproveitaria — usando o
     filtro e o classificador DE PRODUÇÃO, não uma cópia:
       - `atualizar_odds.salvar_odds_do_jogo` (o mesmo filtro de mercado,
         os mesmos nomes, a mesma montagem de descrição), rodado contra um
         cursor FALSO que só anota o que seria gravado;
       - `motor_recomendacoes.identificar_tipo_padrao` para dizer em qual
         `tipo_padrao` cada odd cairia.
     E comparar com um jogo do Brasileirão processado do mesmo jeito.
  4. Quantas requisições cada coisa custa (contador próprio, inclui
     retentativas).

Uso (no `abundant-abundance`, avulso — ele já tem ODDSPAPI_KEY e DATABASE_URL;
o DATABASE_URL só é exigido porque os módulos importados o leem na
importação — a conexão em si é bloqueada):

    python fase0_oddspapi.py

Custo esperado: ~14 requisições da OddsPapi (1 torneios + 1 catálogo +
6 calendários + até 6 folhas de odds). Nenhuma da API-Football.

--------------------------------------------------------------------------
MODO `casas` (v3, 06/10) — que mercados cada CASA tem no Brasileirão
--------------------------------------------------------------------------

Motivo: a MP de 25/09 tirou a Superbet BR do ar em 06/10. Antes de trocar a
casa do projeto, medir — num jogo real e próximo — o que cada casa oferece,
passando tudo pelo MESMO filtro e classificador de produção.

    python fase0_oddspapi.py casas
    python fase0_oddspapi.py casas --casas superbet,bet365,pinnacle --jogos 2 --horas 36

  --casas   palavras (ou slugs exatos) separadas por vírgula. Cada palavra
            casa com TODO slug da lista /bookmakers que a contém. Padrão:
            superbet, bet365, betmgm, pinnacle, betfair, williamhill,
            unibet, betano, sbobet, 1xbet.
  --jogos   quantos jogos do Brasileirão analisar (padrão 2).
  --horas   só jogos que começam dentro destas horas (padrão 36).
  --max-casas  teto de slugs (padrão 30) — protege a cota.

Chave: usa `ODDSPAPI_KEY_TESTE` (conta grátis, todas as casas) se existir;
senão a `ODDSPAPI_KEY` do plano pago (que só libera a Superbet BR). As duas
são mascaradas em qualquer texto de erro.

Custo: 1 (/bookmakers) + 1 (catálogo) + 1 (calendário) + jogos × lotes de
10 casas. Com o padrão, ~7-9 requisições.
"""

import os
import sys
import time
from datetime import datetime, timezone

import psycopg2


# ---------------------------------------------------------------------------
# 🔴 trava: este script NÃO pode conectar no banco. Qualquer tentativa vira
# erro imediato — nada do que os módulos importados fizerem alcança o Postgres.
# ---------------------------------------------------------------------------
class ConexaoProibida(Exception):
    pass


def _conexao_proibida(*args, **kwargs):
    raise ConexaoProibida("fase0_oddspapi.py é só leitura e não conecta no banco.")


psycopg2.connect = _conexao_proibida

import atualizar_odds as ao          # noqa: E402  (funções de PRODUÇÃO)
import motor_recomendacoes as mr     # noqa: E402  (classificador de PRODUÇÃO)


# ---------------------------------------------------------------------------
# contador de requisições (inclui as retentativas do get_com_retry_429)
# ---------------------------------------------------------------------------
_get_original = ao.requests.get
requisicoes = {"total": 0}


def _get_contado(url, *args, **kwargs):
    requisicoes["total"] += 1
    return _get_original(url, *args, **kwargs)


ao.requests.get = _get_contado


# ---------------------------------------------------------------------------
# ligas e como reconhecer cada uma na lista de torneios
# ---------------------------------------------------------------------------
LIGAS = [
    # chave, nome, palavras que a categoria deve conter, nomes aceitos (minúsculo)
    ("premier", "Premier League", ("england", "inglaterra"), ("premier league",)),
    ("laliga", "La Liga", ("spain", "espanha"), ("laliga", "la liga", "primera division", "primera división")),
    ("seriea", "Serie A (Itália)", ("italy", "itália", "italia"), ("serie a",)),
    ("bundesliga", "Bundesliga", ("germany", "alemanha"), ("bundesliga",)),
    ("ligue1", "Ligue 1", ("france", "frança", "franca"), ("ligue 1",)),
]
ID_BRASILEIRAO = ao.TOURNAMENT_ID  # 325 hoje — conferido abaixo contra a própria lista


def cabecalho():
    print("=" * 78)
    print("fase0_oddspapi.py | SÓ LEITURA (conexão com o banco bloqueada)")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"OddsPapi: {ao.API_BASE} | sportId={ao.SPORT_ID} | casas={ao.BOOKMAKERS}")
    print("=" * 78)


_CHAVES_PARA_MASCARAR = {ao.API_KEY}


def mascarar(texto):
    texto = str(texto)
    for chave in _CHAVES_PARA_MASCARAR:
        if chave:
            texto = texto.replace(chave, "***")
    return texto


def get_json(caminho, params):
    params = dict(params, apiKey=ao.API_KEY)
    resp = ao.get_com_retry_429(f"{ao.API_BASE}/{caminho}", params=params)
    if resp.status_code != 200:
        corpo = mascarar(resp.text[:300])
        raise RuntimeError(f"/{caminho} respondeu {resp.status_code}: {corpo}")
    return resp.json()


def campo(obj, *nomes):
    for n in nomes:
        if isinstance(obj, dict) and obj.get(n) is not None:
            return obj[n]
    return None


# ---------------------------------------------------------------------------
# 1. torneios
# ---------------------------------------------------------------------------
def achar_torneios():
    print("\n## 1. TORNEIOS (sportId=10)")
    antes = requisicoes["total"]
    torneios = get_json("tournaments", {"sportId": ao.SPORT_ID})
    print(f"  {len(torneios)} torneios na lista · {requisicoes['total'] - antes} requisição(ões)")

    def descr(t):
        return (f"id={campo(t, 'tournamentId', 'id')} | {campo(t, 'tournamentName', 'name')} | "
                f"categoria={campo(t, 'categoryName', 'categorySlug')}")

    brasil = [t for t in torneios if campo(t, "tournamentId", "id") == ID_BRASILEIRAO]
    print(f"\n  Conferência do Brasileirão (TOURNAMENT_ID={ID_BRASILEIRAO} no atualizar_odds):")
    print("    " + (descr(brasil[0]) if brasil else "⚠️ id NÃO encontrado na lista"))

    escolhidos = {}
    for chave, nome, categorias, nomes in LIGAS:
        candidatos = []
        for t in torneios:
            cat = str(campo(t, "categoryName", "categorySlug") or "").lower()
            nom = str(campo(t, "tournamentName", "name") or "").lower().strip()
            if any(c in cat for c in categorias) and nom in nomes:
                candidatos.append(t)
        print(f"\n  {nome}: {len(candidatos)} candidato(s) exato(s)")
        for t in candidatos:
            print("    " + descr(t))
        if len(candidatos) == 1:
            escolhidos[chave] = campo(candidatos[0], "tournamentId", "id")
        else:
            # ajuda a escolher à mão: tudo da categoria com nome parecido
            parecidos = [t for t in torneios
                         if any(c in str(campo(t, "categoryName", "categorySlug") or "").lower() for c in categorias)
                         and any(p.split()[0] in str(campo(t, "tournamentName", "name") or "").lower() for p in nomes)]
            print(f"    ⚠️ ambíguo ou ausente — {len(parecidos)} parecido(s) na mesma categoria:")
            for t in parecidos[:15]:
                print("      " + descr(t))
    return escolhidos


# ---------------------------------------------------------------------------
# 2. calendário de cada liga
# ---------------------------------------------------------------------------
def proximos_jogos(tournament_id):
    antes = requisicoes["total"]
    jogos = get_json("fixtures", {"tournamentId": tournament_id, "sportId": ao.SPORT_ID})
    custo = requisicoes["total"] - antes
    agora = datetime.now(timezone.utc)
    futuros = []
    for j in jogos:
        inicio = j.get("startTime")
        if not inicio:
            continue
        quando = datetime.fromisoformat(inicio.replace("Z", "+00:00"))
        if quando > agora:
            futuros.append((quando, j))
    futuros.sort(key=lambda x: x[0])
    return jogos, futuros, custo


# ---------------------------------------------------------------------------
# 3. odds de UM jogo, passadas pelo filtro e pelo classificador de produção
# ---------------------------------------------------------------------------
class CursorFalso:
    """Faz o papel do cursor para `salvar_odds_do_jogo`: não fala com banco
    nenhum. SELECT devolve 'não existe', INSERT ... RETURNING devolve um id
    inventado, e todo INSERT INTO odds é ANOTADO em vez de executado."""

    def __init__(self):
        self.odds = []
        self._proximo = None
        self._id = 0

    def execute(self, sql, params=None):
        texto = " ".join(sql.split()).upper()
        if texto.startswith("INSERT INTO ODDS"):
            jogo_id, jogador_id, casa, mercado, valor, linha, direcao = params
            self.odds.append({"jogador_id": jogador_id, "casa": casa, "mercado": mercado,
                              "odd": valor, "linha": linha, "direcao": direcao})
            self._proximo = None
        elif "RETURNING" in texto:
            self._id += 1
            self._proximo = (self._id,)
        else:
            self._proximo = None  # SELECT: "não existe"

    def fetchone(self):
        r, self._proximo = self._proximo, None
        return r

    def fetchall(self):
        return []


def analisar_odds(jogo, catalogo):
    nome_mand = campo(jogo, "participant1Name") or "?"
    nome_vis = campo(jogo, "participant2Name") or "?"
    antes = requisicoes["total"]
    dados = ao.buscar_odds(jogo["fixtureId"])
    custo = requisicoes["total"] - antes

    casas = dados.get("bookmakerOdds", {}) or {}
    brutos = sum(len((info or {}).get("markets", {}) or {}) for info in casas.values())
    utilizavel = ao.existem_odds_utilizaveis(dados)
    suspensas = [c for c, info in casas.items() if (info or {}).get("suspended")]

    cur = CursorFalso()
    if utilizavel:
        # Perspectiva do MANDANTE, como o cron faz para o 1º time rastreado.
        ao.salvar_odds_do_jogo(cur, jogo_id=0, dados_odds=dados, catalogo_mercados=catalogo,
                               mandante=True, adversario=nome_vis, nosso_nome=nome_mand,
                               fixture_id_api=None)

    por_tipo = {}
    sem_tipo = {}
    for o in cur.odds:
        tipo = mr.identificar_tipo_padrao(o["mercado"])
        if tipo:
            por_tipo[tipo] = por_tipo.get(tipo, 0) + 1
        else:
            base = o["mercado"].split(" - ")[0]
            sem_tipo[base] = sem_tipo.get(base, 0) + 1
    return {
        "jogo": f"{nome_mand} x {nome_vis}",
        "inicio": jogo.get("startTime"),
        "fixtureId": jogo["fixtureId"],
        "custo": custo,
        "casas": list(casas.keys()),
        "mercados_brutos": brutos,
        "utilizavel": utilizavel,
        "suspensas": suspensas,
        "odds_que_seriam_gravadas": len(cur.odds),
        "de_jogador": sum(1 for o in cur.odds if o["jogador_id"] is not None),
        "por_tipo": por_tipo,
        "sem_tipo": sem_tipo,
    }


def classificar_casa(dados_casa, catalogo, nome_mand, nome_vis):
    """Uma casa só: o mesmo caminho de produção de `analisar_odds`."""
    cur = CursorFalso()
    if ao.existem_odds_utilizaveis(dados_casa):
        ao.salvar_odds_do_jogo(cur, jogo_id=0, dados_odds=dados_casa, catalogo_mercados=catalogo,
                               mandante=True, adversario=nome_vis, nosso_nome=nome_mand,
                               fixture_id_api=None)
    por_tipo = {}
    for o in cur.odds:
        tipo = mr.identificar_tipo_padrao(o["mercado"])
        if tipo:
            por_tipo[tipo] = por_tipo.get(tipo, 0) + 1
    return cur.odds, por_tipo


def imprimir_analise(nome, a, referencia=None):
    print(f"\n  {nome}: {a['jogo']} | início {a['inicio']} | fixtureId {a['fixtureId']} "
          f"| {a['custo']} requisição(ões)")
    print(f"    casas na resposta: {a['casas'] or 'nenhuma'} · mercados brutos: {a['mercados_brutos']} "
          f"· utilizável: {'sim' if a['utilizavel'] else 'NÃO'} "
          f"· casa marcada como SUSPENSA: {a['suspensas'] or 'nenhuma'}")
    print(f"    odds que o atualizar_odds gravaria: {a['odds_que_seriam_gravadas']} "
          f"(de jogador: {a['de_jogador']})")
    tipos = sorted(set(a["por_tipo"]) | set((referencia or {}).get("por_tipo", {})))
    if tipos:
        print("    tipo_padrao                    odds" + ("   Brasileirão" if referencia else ""))
        for t in tipos:
            n = a["por_tipo"].get(t, 0)
            linha = f"    {t:<30} {n:>5}"
            if referencia:
                r = referencia["por_tipo"].get(t, 0)
                marca = "" if (n > 0) == (r > 0) else ("   ⚠️ só no Brasileirão" if r else "   ⚠️ só nesta liga")
                linha += f"   {r:>10}{marca}"
            print(linha)
    if a["sem_tipo"]:
        print(f"    ⚠️ gravadas mas SEM tipo_padrao (o motor ignoraria): "
              f"{sum(a['sem_tipo'].values())} odd(s) em {len(a['sem_tipo'])} mercado(s):")
        for base, n in sorted(a["sem_tipo"].items(), key=lambda x: -x[1])[:10]:
            print(f"      {n:>4} × {base}")


def main():
    cabecalho()
    escolhidos = achar_torneios()

    print("\n## 2. CATÁLOGO DE MERCADOS")
    antes = requisicoes["total"]
    catalogo = ao.buscar_catalogo_mercados()
    print(f"  {len(catalogo)} mercados · {requisicoes['total'] - antes} requisição(ões)")

    print("\n## 3. CALENDÁRIO E ODDS — 1 jogo por liga (o próximo), Brasileirão como referência")
    referencia = None
    resultados = []
    for chave, nome, _, _ in [("brasileirao", "Brasileirão", None, None)] + LIGAS:
        tid = ID_BRASILEIRAO if chave == "brasileirao" else escolhidos.get(chave)
        if tid is None:
            print(f"\n  {nome}: ⚠️ sem tournamentId definido — pulado (ver seção 1)")
            resultados.append((nome, None, None, None))
            continue
        try:
            jogos, futuros, custo = proximos_jogos(tid)
        except Exception as e:
            print(f"\n  {nome} (tournamentId={tid}): ⚠️ calendário falhou: {e}")
            resultados.append((nome, tid, None, None))
            continue
        print(f"\n  {nome} (tournamentId={tid}): {len(jogos)} jogos no calendário, "
              f"{len(futuros)} futuros · {custo} requisição(ões)")
        if futuros:
            proximos = ", ".join(f"{q:%d/%m %H:%M}Z" for q, _ in futuros[:3])
            print(f"    próximos: {proximos}")
        if not futuros:
            resultados.append((nome, tid, len(jogos), None))
            continue
        try:
            a = analisar_odds(futuros[0][1], catalogo)
        except Exception as e:
            print(f"    ⚠️ odds falharam: {e}")
            resultados.append((nome, tid, len(jogos), None))
            continue
        imprimir_analise(nome, a, None if chave == "brasileirao" else referencia)
        if chave == "brasileirao":
            referencia = a
        resultados.append((nome, tid, len(jogos), a))
        time.sleep(1)

    print("\n## 4. RESUMO")
    print(f"  {'liga':<18} {'tournamentId':>12} {'jogos cal.':>10} {'odds gravadas':>14} {'tipos':>6}")
    for nome, tid, n, a in resultados:
        print(f"  {nome:<18} {str(tid):>12} {str(n):>10} "
              f"{(str(a['odds_que_seriam_gravadas']) if a else '-'):>14} "
              f"{(str(len(a['por_tipo'])) if a else '-'):>6}")
    print(f"\n  Requisições da OddsPapi gastas por este script: {requisicoes['total']}")
    print("\n" + "=" * 78)
    print("FIM — SÓ LEITURA: nenhuma conexão com o banco foi aberta; nada foi gravado.")
    print("=" * 78)


# ---------------------------------------------------------------------------
# MODO `casas` (v3)
# ---------------------------------------------------------------------------
CASAS_PADRAO = ["superbet", "bet365", "betmgm", "pinnacle", "betfair", "williamhill",
                "unibet", "betano", "sbobet", "1xbet"]
LOTE_CASAS = 10


def ler_opcoes(argv):
    opcoes = {"casas": CASAS_PADRAO, "jogos": 2, "horas": 36.0, "max_casas": 30}
    i = 2
    while i < len(argv):
        nome = argv[i]
        valor = argv[i + 1] if i + 1 < len(argv) else None
        if valor is None:
            raise SystemExit(f"opção {nome} sem valor")
        if nome == "--casas":
            opcoes["casas"] = [c.strip().lower() for c in valor.split(",") if c.strip()]
        elif nome == "--jogos":
            opcoes["jogos"] = int(valor)
        elif nome == "--horas":
            opcoes["horas"] = float(valor)
        elif nome == "--max-casas":
            opcoes["max_casas"] = int(valor)
        else:
            raise SystemExit(f"opção desconhecida: {nome}")
        i += 2
    return opcoes


def slug_de(item):
    if isinstance(item, str):
        return item
    return campo(item, "slug", "bookmaker", "bookmakerSlug", "id", "name")


def escolher_slugs(lista, palavras, teto):
    slugs = sorted({str(slug_de(b)) for b in lista if slug_de(b)})
    escolhidos, por_palavra = [], {}
    for p in palavras:
        achados = [s for s in slugs if p == s.lower() or p in s.lower()]
        por_palavra[p] = achados
        for s in achados:
            if s not in escolhidos:
                escolhidos.append(s)
    cortados = escolhidos[teto:]
    return escolhidos[:teto], por_palavra, cortados, len(slugs)


def buscar_odds_lote(fixture_id, slugs):
    """Odds de várias casas, em lotes. Lote que falha é refeito casa a casa,
    para uma casa problemática não esconder as outras."""
    juntas, falhas = {}, {}
    for i in range(0, len(slugs), LOTE_CASAS):
        lote = slugs[i:i + LOTE_CASAS]
        try:
            dados = ao.buscar_odds(fixture_id, bookmakers=lote)
            juntas.update(dados.get("bookmakerOdds", {}) or {})
        except Exception as e:
            print(f"    ⚠️ lote {lote} falhou ({mascarar(e)[:160]}) — tentando casa a casa")
            for s in lote:
                try:
                    dados = ao.buscar_odds(fixture_id, bookmakers=[s])
                    juntas.update(dados.get("bookmakerOdds", {}) or {})
                except Exception as e2:
                    falhas[s] = mascarar(e2)[:160]
    return juntas, falhas


def modo_casas(argv):
    opcoes = ler_opcoes(argv)
    chave_teste = os.environ.get("ODDSPAPI_KEY_TESTE")
    if chave_teste:
        _CHAVES_PARA_MASCARAR.add(chave_teste)
        ao.API_KEY = chave_teste
        origem = "ODDSPAPI_KEY_TESTE (conta grátis)"
    else:
        origem = "ODDSPAPI_KEY (plano pago — outras casas devem voltar RESTRITAS)"

    print("=" * 78)
    print("fase0_oddspapi.py casas | SÓ LEITURA (conexão com o banco bloqueada)")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"chave: {origem} · palavras: {opcoes['casas']} · jogos: {opcoes['jogos']} "
          f"· janela: {opcoes['horas']}h · teto: {opcoes['max_casas']} casas")
    print("=" * 78)

    print("\n## 1. CASAS DISPONÍVEIS (/bookmakers)")
    lista = get_json("bookmakers", {})
    if isinstance(lista, dict):
        lista = campo(lista, "bookmakers", "data", "results") or list(lista.values())
    slugs, por_palavra, cortados, total = escolher_slugs(lista, opcoes["casas"], opcoes["max_casas"])
    print(f"  {total} casas na lista")
    for p, achados in por_palavra.items():
        print(f"  '{p}': {', '.join(achados) if achados else '⚠️ nenhuma'}")
    if cortados:
        print(f"  ⚠️ {len(cortados)} cortada(s) pelo teto: {', '.join(cortados)}")
    if not slugs:
        print("  nenhuma casa escolhida — nada a fazer.")
        return

    print("\n## 2. CATÁLOGO DE MERCADOS")
    catalogo = ao.buscar_catalogo_mercados()
    print(f"  {len(catalogo)} mercados")

    print(f"\n## 3. JOGOS DO BRASILEIRÃO NAS PRÓXIMAS {opcoes['horas']:.0f}h")
    _, futuros, _ = proximos_jogos(ID_BRASILEIRAO)
    agora = datetime.now(timezone.utc)
    janela = [(q, j) for q, j in futuros if (q - agora).total_seconds() <= opcoes["horas"] * 3600]
    print(f"  {len(futuros)} futuros no calendário, {len(janela)} na janela")
    escolhidos = janela[:opcoes["jogos"]]
    if not escolhidos:
        print("  ⚠️ nenhum jogo na janela — aumente --horas.")
        return

    resumo = {s: {"jogos": 0, "presente": 0, "suspensa": 0, "brutos": 0, "odds": 0,
                  "jogador": 0, "tipos": {}} for s in slugs}
    todas_falhas = {}
    for quando, jogo in escolhidos:
        mand = campo(jogo, "participant1Name") or "?"
        vis = campo(jogo, "participant2Name") or "?"
        print(f"\n  ### {mand} x {vis} — {quando:%d/%m %H:%M}Z (fixtureId {jogo['fixtureId']})")
        antes = requisicoes["total"]
        juntas, falhas = buscar_odds_lote(jogo["fixtureId"], slugs)
        todas_falhas.update(falhas)
        print(f"    {requisicoes['total'] - antes} requisição(ões) · {len(juntas)} casa(s) na resposta")
        print(f"    {'casa':<26} {'mercados':>8} {'suspensa':>8} {'odds úteis':>10} {'jogador':>8} {'tipos':>6}")
        for s in slugs:
            r = resumo[s]
            r["jogos"] += 1
            info = juntas.get(s)
            if info is None:
                print(f"    {s:<26} {'—':>8}   ausente na resposta" +
                      (" (restrita no plano)" if s in ao._bookmakers_restritos_conhecidos else "") +
                      (f" (erro: {falhas[s]})" if s in falhas else ""))
                continue
            r["presente"] += 1
            brutos = len((info or {}).get("markets", {}) or {})
            r["brutos"] += brutos
            suspensa = bool((info or {}).get("suspended"))
            r["suspensa"] += int(suspensa)
            odds, por_tipo = classificar_casa({"bookmakerOdds": {s: info}}, catalogo, mand, vis)
            jog = sum(1 for o in odds if o["jogador_id"] is not None)
            r["odds"] += len(odds)
            r["jogador"] += jog
            for t, n in por_tipo.items():
                r["tipos"][t] = r["tipos"].get(t, 0) + n
            print(f"    {s:<26} {brutos:>8} {('SIM' if suspensa else 'não'):>8} {len(odds):>10} "
                  f"{jog:>8} {len(por_tipo):>6}")
        time.sleep(1)

    print("\n## 4. RESUMO POR CASA (soma dos jogos)")
    todos_tipos = sorted({t for r in resumo.values() for t in r["tipos"]})
    ordem = sorted(slugs, key=lambda s: (-len(resumo[s]["tipos"]), -resumo[s]["odds"]))
    print(f"  {'casa':<26} {'presente':>8} {'odds úteis':>10} {'jogador':>8} {'tipos':>6}")
    for s in ordem:
        r = resumo[s]
        print(f"  {s:<26} {r['presente']:>3}/{r['jogos']:<4} {r['odds']:>10} {r['jogador']:>8} "
              f"{len(r['tipos']):>6}")

    if todos_tipos:
        com_odds = [s for s in ordem if resumo[s]["odds"]]
        print("\n## 5. TIPO DE MERCADO × CASA (odds úteis; só casas com alguma)")
        for s in com_odds:
            print(f"  {s}:")
            faltam = [t for t in todos_tipos if t not in resumo[s]["tipos"]]
            tem = ", ".join(f"{t} {resumo[s]['tipos'][t]}" for t in todos_tipos if t in resumo[s]["tipos"])
            print(f"    tem   → {tem}")
            print(f"    falta → {', '.join(faltam) if faltam else 'nada (cobre todos os tipos vistos)'}")
        print(f"\n  tipos vistos em alguma casa ({len(todos_tipos)}): {', '.join(todos_tipos)}")

    if todas_falhas:
        print("\n  casas com erro:")
        for s, e in todas_falhas.items():
            print(f"    {s}: {e}")
    print(f"\n  Requisições da OddsPapi gastas por este script: {requisicoes['total']}")
    print("\n" + "=" * 78)
    print("FIM — SÓ LEITURA: nenhuma conexão com o banco foi aberta; nada foi gravado.")
    print("=" * 78)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "casas":
        modo_casas(sys.argv)
    else:
        main()
