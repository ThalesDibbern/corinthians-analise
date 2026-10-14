"""
diagnostico_multiplas_pontas.py — dois diagnósticos, SOMENTE LEITURA.

    python diagnostico_multiplas_pontas.py [multiplas|pontas|tudo] [--desde D] [--ate D]

    multiplas  C1 (`2_ABERTO §1.3`): de onde vem o −40% das múltiplas do MESMO jogo.
    pontas     O que o B5 deixou: o "menos" (e o "mais") acima de 80% no registro
               do modelo, aberto por mercado e linha. --desde/--ate só valem
               aqui (data BRT do apito; padrão 2026-10-07 a 2026-10-12).
    tudo       os dois (padrão).

    Nenhuma tabela é criada, alterada ou gravada. A sessão vira só-leitura pelo
    próprio banco (conferido) e o script termina em rollback.

--------------------------------------------------------------------------
MULTIPLAS — o que separa
--------------------------------------------------------------------------

Para cada múltipla AVALIADA (acertou/errou), cada perna recebe o veredito
pela função de PRODUÇÃO `arquivar_recomendacoes.buscar_resultado_perna`
(importada; o cursor só acrescenta a probabilidade e a odd da mesma linha do
histórico na coluna seguinte — a consulta e o casamento são os de produção).

Três números por grupo:
  previsto ..... `probabilidade_combinada` gravada (o que o sistema disse)
  Q ............ o que daria se cada perna estivesse CALIBRADA e as pernas
                 fossem INDEPENDENTES: produto, perna a perna, da taxa real de
                 acerto do histórico no mesmo (mercado, lado, faixa de 10 pontos)
  observado .... a taxa real das múltiplas

  previsto − Q  = o que é erro de CADA perna, multiplicado
  Q − observado = o que é as pernas errarem JUNTAS (dependência)

Controle: o mesmo para múltiplas de jogos DISTINTOS, onde a dependência
deveria ser ~0. ⚠️ A calibração usa o próprio histórico (dentro da
amostra): serve para DECOMPOR, não para prever.

Depois, sem as múltiplas: TODOS os pares de pernas recomendadas no mesmo
jogo (histórico deduplicado), observado P(as duas) × produto calibrado.
Esse teste não depende da seleção das 300 de maior probabilidade.

--------------------------------------------------------------------------
PONTAS
--------------------------------------------------------------------------

Unidade, veredito, casa sem margem e encolhimento: os do `simular_b5` /
`avaliar_registro_modelo` (importados). Faixa pelo p CRU, como no B5.
"""

import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime

import psycopg2

import arquivar_recomendacoes as arq                      # casamento de perna (PRODUÇÃO)
from combinacoes import MERCADOS_JOGO_INTEIRO
import avaliar_registro_modelo as ar                      # lado da aposta
import simular_b5 as b5                                   # unidades do registro + encolhimento

DATABASE_URL = os.environ["DATABASE_URL"]
PADRAO_DESDE, PADRAO_ATE = date(2026, 10, 7), date(2026, 10, 12)
N_MIN_CELULA = 15          # abaixo disso, a calibração cai para (lado, faixa)
TOP_PARES_MERCADO = 10


# ---------------------------------------------------------------------------
# cursor que acrescenta probabilidade/odd à consulta de produção
# ---------------------------------------------------------------------------
class CursorComProbabilidade:
    """Repassa ao cursor real. Na consulta de `buscar_resultado_perna`,
    acrescenta `probabilidade_historica, odd_oferecida` DEPOIS de `resultado`
    — a função continua lendo `row[0]`. Se a consulta de produção mudar e a
    troca não acontecer, para (não adivinha)."""

    TROCAS = [("SELECT hr.resultado", "SELECT hr.resultado, hr.probabilidade_historica, hr.odd_oferecida"),
              ("SELECT resultado FROM historico_recomendacoes",
               "SELECT resultado, probabilidade_historica, odd_oferecida FROM historico_recomendacoes")]

    def __init__(self, cur):
        self.cur = cur
        self.ultima = None

    def execute(self, sql, params=None):
        for antes, depois in self.TROCAS:
            if antes in sql:
                sql = sql.replace(antes, depois, 1)
                break
        else:
            raise SystemExit("🔴 a consulta de `buscar_resultado_perna` mudou — script desatualizado.")
        self.cur.execute(sql, params)

    def fetchone(self):
        self.ultima = self.cur.fetchone()
        return self.ultima


def resultado_e_probabilidade(proxy, perna, cache):
    chave = (perna.get("tipo_padrao"), perna.get("jogo_id"), perna.get("fixture_id_api"),
             perna.get("jogador_id"), perna.get("linha"), perna.get("direcao"))
    if chave not in cache:
        proxy.ultima = None
        r = arq.buscar_resultado_perna(proxy, perna.get("tipo_padrao"), perna.get("jogo_id"),
                                       perna.get("fixture_id_api"), perna.get("jogador_id"),
                                       perna.get("linha"), perna.get("direcao"))
        linha = proxy.ultima
        p = float(linha[1]) if linha and linha[1] is not None else None
        odd = float(linha[2]) if linha and linha[2] is not None else None
        cache[chave] = (r, p, odd)
    return cache[chave]


# ---------------------------------------------------------------------------
# classificação
# ---------------------------------------------------------------------------
def classe(tipo, jogador_id):
    if arq.e_aposta_de_jogador(tipo, jogador_id):
        return "JOGADOR"
    if tipo in MERCADOS_JOGO_INTEIRO:
        return "JOGO INTEIRO"
    return "TIME"


def mercado_da_perna(tipo, jogador_id, jogo_id, fixture):
    """Mesmo mercado = mesma aposta a menos da linha/direção (time por jogo_id)."""
    c = classe(tipo, jogador_id)
    return (tipo, jogador_id, jogo_id if c == "TIME" else fixture)


def faixa10(p):
    return min(int(p // 10), 9)


class Calibracao:
    def __init__(self, linhas):
        cel, cel_lado = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
        for tipo, lado_, p, acerto in linhas:
            for d, k in ((cel, (tipo, lado_, faixa10(p))), (cel_lado, (lado_, faixa10(p)))):
                d[k][0] += 1
                d[k][1] += acerto
        self.cel, self.cel_lado = cel, cel_lado
        self.fallback = 0

    def taxa(self, tipo, lado_, p):
        n, a = self.cel.get((tipo, lado_, faixa10(p)), (0, 0))
        if n >= N_MIN_CELULA:
            return a / n
        n, a = self.cel_lado.get((lado_, faixa10(p)), (0, 0))
        self.fallback += 1
        return a / n if n else p / 100.0


# ---------------------------------------------------------------------------
# histórico deduplicado (1_ESSENCIAL §5.12) em unidades de aposta
# ---------------------------------------------------------------------------
SQL_HISTORICO = """
SELECT DISTINCT ON (hr.jogo_id, hr.tipo_padrao, hr.jogador_id, hr.linha, hr.direcao)
       hr.jogo_id, j.fixture_id_api, hr.tipo_padrao, hr.jogador_id, hr.linha, hr.direcao,
       hr.probabilidade_historica, hr.odd_oferecida, hr.resultado
FROM historico_recomendacoes hr
JOIN jogos j ON j.id = hr.jogo_id
WHERE hr.resultado IN ('acertou', 'errou')
ORDER BY hr.jogo_id, hr.tipo_padrao, hr.jogador_id, hr.linha, hr.direcao, hr.arquivado_em, hr.id
"""


def carregar_historico(cur):
    cur.execute(SQL_HISTORICO)
    unidades = {}
    for jogo_id, fx, tipo, jog, linha, direcao, p, odd, res in cur.fetchall():
        c = classe(tipo, jog)
        ident = (fx if c != "TIME" else jogo_id, tipo, jog, linha, (direcao or "").strip().lower())
        if ident in unidades:          # jogo inteiro/jogador visto pelas duas perspectivas: uma aposta só
            continue
        unidades[ident] = {"jogo_id": jogo_id, "fixture": fx, "tipo": tipo, "jogador_id": jog, "linha": linha,
                           "lado": ar.lado(direcao), "p": float(p), "odd": float(odd),
                           "x": 1 if res == "acertou" else 0, "classe": c}
    return list(unidades.values())


# ---------------------------------------------------------------------------
# impressão
# ---------------------------------------------------------------------------
def pct(x):
    return f"{100 * x:6.1f}" if x is not None else f"{'—':>6}"


def media(v):
    return sum(v) / len(v) if v else None


def linha_grupo(rotulo, itens):
    """itens: dicts com prev, pp, q, x, odd, fixture."""
    if not itens:
        print(f"  {rotulo:<34} {0:>6}")
        return
    n = len(itens)
    fx = defaultdict(list)
    for i in itens:
        fx[i["fixture"]].append(i)
    por_fx = lambda campo: media([media([i[campo] for i in v]) for v in fx.values()])
    roi = 100 * sum((i["odd"] - 1) if i["x"] else -1 for i in itens) / n
    print(f"  {rotulo:<34} {n:>6} {len(fx):>5} {pct(media([i['prev'] for i in itens]))} "
          f"{pct(media([i['pp'] for i in itens if i['pp'] is not None]))} {pct(media([i['q'] for i in itens]))} "
          f"{pct(media([i['x'] for i in itens]))} {roi:>+7.1f}% │ {pct(por_fx('prev'))} {pct(por_fx('q'))} "
          f"{pct(por_fx('x'))}")


def cabecalho_grupo():
    print(f"  {'':<34} {'N':>6} {'jogos':>5} {'prev':>6} {'Πp':>6} {'Q':>6} {'obs':>6} {'ROI':>8} │"
          f" {'— por jogo (cada jogo pesa 1) —':^20}")
    print(f"  {'':<34} {'':>6} {'':>5} {'':>6} {'':>6} {'':>6} {'':>6} {'':>8} │ {'prev':>6} {'Q':>6} {'obs':>6}")


# ---------------------------------------------------------------------------
# MULTIPLAS
# ---------------------------------------------------------------------------
def modo_multiplas(cur):
    print("\n" + "#" * 78 + "\n# MULTIPLAS — o −40% das múltiplas do mesmo jogo (C1)\n" + "#" * 78)
    hist = carregar_historico(cur)
    cal = Calibracao([(u["tipo"], u["lado"], u["p"], u["x"]) for u in hist])
    print(f"\nhistórico deduplicado: {len(hist)} apostas avaliadas · "
          f"{len({u['fixture'] for u in hist})} jogos — base da calibração (dentro da amostra)")

    cur.execute("SELECT id, fixture_id_api FROM jogos")
    fixture_de = dict(cur.fetchall())

    cur.execute("""SELECT id, pernas, probabilidade_combinada, odd_combinada, resultado, descricao, primeiro_apito
                   FROM multiplas_candidatas
                   WHERE avaliada = TRUE AND resultado IN ('acertou', 'errou')
                   ORDER BY id""")
    candidatas = cur.fetchall()
    proxy = CursorComProbabilidade(cur)
    cache = {}
    itens, sem_veredito, discordam = [], 0, 0
    pernas_vistas = {}
    for cid, pernas, prob, odd, res, desc, apito in candidatas:
        if isinstance(pernas, str):
            pernas = json.loads(pernas)
        legs = []
        for pr in pernas:
            r, p, odd_p = resultado_e_probabilidade(proxy, pr, cache)
            fx = pr.get("fixture_id_api") or fixture_de.get(pr.get("jogo_id"))
            legs.append({"tipo": pr.get("tipo_padrao"), "jogador_id": pr.get("jogador_id"),
                         "jogo_id": pr.get("jogo_id"), "fixture": fx, "linha": pr.get("linha"),
                         "lado": ar.lado(pr.get("direcao")), "r": r, "p": p})
        if any(l["r"] not in ("acertou", "errou") or l["p"] is None for l in legs):
            sem_veredito += 1
            continue
        x = 1 if all(l["r"] == "acertou" for l in legs) else 0
        if x != (1 if res == "acertou" else 0):
            discordam += 1

        # agrupa a faixa (mesmo mercado, mais + menos) como um evento só
        por_mercado = defaultdict(list)
        for l in legs:
            por_mercado[mercado_da_perna(l["tipo"], l["jogador_id"], l["jogo_id"], l["fixture"])].append(l)
        pp, q, tem_faixa = 1.0, 1.0, False
        for grupo in por_mercado.values():
            if len(grupo) == 2:
                tem_faixa = True
                pp *= max(sum(l["p"] for l in grupo) / 100.0 - 1, 0.0)
                q *= max(sum(cal.taxa(l["tipo"], l["lado"], l["p"]) for l in grupo) - 1, 0.0)
            else:
                for l in grupo:
                    pp *= l["p"] / 100.0
                    q *= cal.taxa(l["tipo"], l["lado"], l["p"])
        fixtures = {l["fixture"] for l in legs}
        mesmo = len(fixtures) == 1
        if mesmo:
            for l in legs:
                pernas_vistas[(l["fixture"], l["tipo"], l["jogador_id"], l["jogo_id"] if classe(l["tipo"], l["jogador_id"]) == "TIME" else None,
                               l["linha"], l["lado"])] = l
        itens.append({"id": cid, "mesmo": mesmo, "n": len(legs), "faixa": tem_faixa or "(faixa)" in (desc or ""),
                      "max_p": max(l["p"] for l in legs), "tem_menos": any(l["lado"] == "menos" for l in legs),
                      "jogador": any(classe(l["tipo"], l["jogador_id"]) == "JOGADOR" for l in legs),
                      "prev": float(prob) / 100.0, "pp": pp, "q": q, "x": x, "odd": float(odd),
                      "fixture": min(fixtures, key=lambda f: (f is None, f)) if mesmo else tuple(sorted(f for f in fixtures if f)),
                      "apito": apito})

    print(f"múltiplas avaliadas: {len(candidatas)} · usadas: {len(itens)} · sem veredito/probabilidade de "
          f"alguma perna: {sem_veredito} · veredito recalculado ≠ gravado: {discordam}")
    print(f"pernas que caíram na calibração por (lado, faixa) por falta de N no mercado: {cal.fallback}")
    if not itens:
        return
    print(f"período (primeiro apito): {min(i['apito'] for i in itens)} a {max(i['apito'] for i in itens)}")
    print("\nprev = probabilidade gravada · Πp = produto das pernas pela probabilidade do histórico ·"
          "\nQ = produto CALIBRADO e independente · obs = taxa real · ROI pela odd combinada")

    mesmo = [i for i in itens if i["mesmo"]]
    dist = [i for i in itens if not i["mesmo"]]
    print("\n## 1. A DECOMPOSIÇÃO")
    cabecalho_grupo()
    linha_grupo("jogos DISTINTOS (controle)", dist)
    linha_grupo("MESMO jogo", mesmo)
    for n in sorted({i["n"] for i in mesmo}):
        linha_grupo(f"  mesmo jogo, {n} pernas", [i for i in mesmo if i["n"] == n])

    print("\n## 2. MESMO JOGO, POR HIPÓTESE")
    cabecalho_grupo()
    for rot, f in (("com FAIXA", lambda i: i["faixa"]), ("sem faixa", lambda i: not i["faixa"]),
                   ("alguma perna ≥ 80%", lambda i: i["max_p"] >= 80), ("todas as pernas < 80%", lambda i: i["max_p"] < 80),
                   ("com perna de 'menos'", lambda i: i["tem_menos"]), ("sem perna de 'menos'", lambda i: not i["tem_menos"]),
                   ("com perna de jogador", lambda i: i["jogador"]), ("sem perna de jogador", lambda i: not i["jogador"])):
        linha_grupo(rot, [i for i in mesmo if f(i)])
    print("  — e o controle (jogos distintos), pelos mesmos cortes:")
    for rot, f in (("  distintos, alguma perna ≥ 80%", lambda i: i["max_p"] >= 80),
                   ("  distintos, todas < 80%", lambda i: i["max_p"] < 80)):
        linha_grupo(rot, [i for i in dist if f(i)])

    print("\n## 3. CONCENTRAÇÃO — quantas múltiplas por jogo (N efetivo)")
    cont = defaultdict(int)
    for i in mesmo:
        cont[i["fixture"]] += 1
    top = sorted(cont.items(), key=lambda kv: -kv[1])
    if top:
        tot = sum(cont.values())
        top5 = sum(v for _, v in top[:5])
        print(f"  {len(cont)} jogos · os 5 maiores têm {top5} de {tot} múltiplas ({100 * top5 / tot:.0f}%) · "
              f"mediana {sorted(cont.values())[len(cont) // 2]} por jogo")

    print("\n## 4. AS PERNAS DAS MÚLTIPLAS DO MESMO JOGO × TODAS AS RECOMENDAÇÕES (efeito da seleção)")
    legs = list(pernas_vistas.values())
    for rot, grupo in (("pernas distintas nas múltiplas", legs), ("todas as recomendações (histórico)", hist)):
        if grupo:
            n = len(grupo)
            obs = media([1 if (g.get("r") == "acertou" or g.get("x") == 1) else 0 for g in grupo])
            print(f"  {rot:<36} N {n:>5} · p médio {media([g['p'] for g in grupo]):5.1f} · obs {100 * obs:5.1f} · "
                  f"≥80%: {sum(1 for g in grupo if g['p'] >= 80)} · lado menos: {sum(1 for g in grupo if g['lado'] == 'menos')}")

    modo_pares(hist, cal)


def modo_pares(hist, cal):
    print("\n## 5. DEPENDÊNCIA SEM AS MÚLTIPLAS — todos os pares de recomendações do mesmo jogo")
    print("  obs = P(as duas acertam) · Q = produto calibrado · Πp = produto do modelo ·"
          " razão obs/Q > 1 = acertam juntas, < 1 = uma exclui a outra")
    por_fx = defaultdict(list)
    for u in hist:
        u["c"] = cal.taxa(u["tipo"], u["lado"], u["p"])
        por_fx[u["fixture"]].append(u)
    grupos = defaultdict(list)
    for fx, us in por_fx.items():
        for i in range(len(us)):
            for j in range(i + 1, len(us)):
                a, b = us[i], us[j]
                mesmo_mercado = (mercado_da_perna(a["tipo"], a["jogador_id"], a["jogo_id"], fx) ==
                                 mercado_da_perna(b["tipo"], b["jogador_id"], b["jogo_id"], fx))
                par = (a["x"] * b["x"], a["c"] * b["c"], a["p"] * b["p"] / 1e4, fx)
                grupos["mesmo mercado (outra linha/direção)" if mesmo_mercado else "mercados diferentes"].append(par)
                if not mesmo_mercado:
                    lados = "-".join(sorted([a["lado"], b["lado"]]))
                    grupos[f"  lados {lados}"].append(par)
                    grupos["  classes " + " × ".join(sorted([a["classe"], b["classe"]]))].append(par)
                    grupos["tipo " + " × ".join(sorted([a["tipo"], b["tipo"]]))].append(par)
    print(f"  {'':<52} {'pares':>7} {'jogos':>5} {'Πp':>6} {'Q':>6} {'obs':>6} {'obs/Q':>6}")
    tipos = sorted([k for k in grupos if k.startswith("tipo ")], key=lambda k: -len(grupos[k]))[:TOP_PARES_MERCADO]
    for k in ([k for k in grupos if not k.startswith("tipo ") and not k.startswith("  ")]
              + sorted(k for k in grupos if k.startswith("  ")) + tipos):
        v = grupos[k]
        obs, q, pp = media([x[0] for x in v]), media([x[1] for x in v]), media([x[2] for x in v])
        print(f"  {k[:52]:<52} {len(v):>7} {len({x[3] for x in v}):>5} {pct(pp)} {pct(q)} {pct(obs)} "
              f"{(obs / q if q else 0):>6.2f}")


# ---------------------------------------------------------------------------
# PONTAS
# ---------------------------------------------------------------------------
def modo_pontas(cur, desde, ate):
    print("\n" + "#" * 78 + f"\n# PONTAS — registro do modelo, {desde} a {ate} (BRT)\n" + "#" * 78)
    unidades, n_jogos = b5.carregar(cur, desde, ate)
    if not unidades:
        print("\nnenhuma unidade avaliada no período.")
        return
    hoje = b5.candidato("hoje", None)
    for u in unidades:
        u["p_final"] = hoje(u)
        u["x"] = 1 if u["resultado"] == "acertou" else 0
    print(f"\n{n_jogos} jogo(s) · {len(unidades)} unidade(s) de dois lados")
    print("p cru = modelo · p final = depois do encolhimento de produção · casa = sem margem (só onde há odd dos dois lados)")

    def tabela(titulo, sel, chave):
        print(f"\n## {titulo}")
        print(f"  {'':<40} {'N':>5} {'jogos':>5} {'p cru':>6} {'p fin':>6} {'casa':>6} {'(N)':>5} {'obs':>6} {'obs−fin':>8}")
        grupos = defaultdict(list)
        for u in sel:
            grupos[chave(u)].append(u)
        grupos = sorted(grupos.items(), key=lambda kv: -len(kv[1]))
        for k, us in [("TOTAL", sel)] + grupos:
            justas = [u["p_casa_justa"] for u in us if u.get("p_casa_justa") is not None]
            obs = 100 * media([u["x"] for u in us])
            fin = media([u["p_final"] for u in us])
            print(f"  {str(k)[:40]:<40} {len(us):>5} {len({u['fixture_id_api'] for u in us}):>5} "
                  f"{media([u['p'] for u in us]):6.1f} {fin:6.1f} "
                  f"{(f'{media(justas):6.1f}' if justas else f'{chr(8212):>6}')} {len(justas):>5} {obs:6.1f} {obs - fin:>+8.1f}")

    for lado_, faixa, f in (("menos", "≥ 80%", lambda u: u["p"] >= 80), ("mais", "≥ 80%", lambda u: u["p"] >= 80),
                            ("menos", "≤ 20%", lambda u: u["p"] < 20), ("mais", "≤ 20%", lambda u: u["p"] < 20)):
        sel = [u for u in unidades if ar.lado(u["direcao"]) == lado_ and f(u)]
        if not sel:
            continue
        tabela(f"'{lado_}' com p cru {faixa} — por mercado", sel, lambda u: f"{u['classe'][:5]} {u['tipo_padrao']}")
        if lado_ == "menos" and faixa == "≥ 80%":
            tabela("'menos' ≥ 80% — por mercado e linha", sel, lambda u: f"{u['tipo_padrao']} {u['linha']}")
            tabela("'menos' ≥ 80% — por jogo", sel, lambda u: f"fixture {u['fixture_id_api']}")


def ler_opcoes(argv):
    modo, desde, ate = "tudo", PADRAO_DESDE, PADRAO_ATE
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("multiplas", "pontas", "tudo"):
            modo = a
            i += 1
        elif a in ("--desde", "--ate") and i + 1 < len(argv):
            valor = date.fromisoformat(argv[i + 1])
            desde, ate = (valor, ate) if a == "--desde" else (desde, valor)
            i += 2
        else:
            print(__doc__)
            raise SystemExit(f"argumento não reconhecido: {a}")
    return modo, desde, ate


def main():
    modo, desde, ate = ler_opcoes(sys.argv)
    print("=" * 78)
    print(f"diagnostico_multiplas_pontas.py | modo: {modo} | SOMENTE LEITURA (rollback no fim)")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 78)
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    conn.commit()   # só vale da PRÓXIMA transação (1_ESSENCIAL §6.2)
    cur.execute("SHOW transaction_read_only")
    if str(cur.fetchone()[0]).lower() != "on":
        conn.close()
        raise SystemExit("🔴 a conexão NÃO ficou só-leitura — nada foi lido.")
    try:
        if modo in ("multiplas", "tudo"):
            modo_multiplas(cur)
        if modo in ("pontas", "tudo"):
            modo_pontas(cur, desde, ate)
    finally:
        conn.rollback()   # incondicional: este script nunca grava
        cur.close()
        conn.close()
        print("\n" + "=" * 78)
        print("SOMENTE LEITURA — nada foi gravado.")
        print("=" * 78)


if __name__ == "__main__":
    main()
