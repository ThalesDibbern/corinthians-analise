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
"""

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


def get_json(caminho, params):
    params = dict(params, apiKey=ao.API_KEY)
    resp = ao.get_com_retry_429(f"{ao.API_BASE}/{caminho}", params=params)
    if resp.status_code != 200:
        corpo = resp.text[:300].replace(ao.API_KEY, "***")
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
        "odds_que_seriam_gravadas": len(cur.odds),
        "de_jogador": sum(1 for o in cur.odds if o["jogador_id"] is not None),
        "por_tipo": por_tipo,
        "sem_tipo": sem_tipo,
    }


def imprimir_analise(nome, a, referencia=None):
    print(f"\n  {nome}: {a['jogo']} | início {a['inicio']} | fixtureId {a['fixtureId']} "
          f"| {a['custo']} requisição(ões)")
    print(f"    casas na resposta: {a['casas'] or 'nenhuma'} · mercados brutos: {a['mercados_brutos']} "
          f"· utilizável: {'sim' if a['utilizavel'] else 'NÃO'}")
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


if __name__ == "__main__":
    main()
