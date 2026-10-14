"""
simular_b5.py — Fase 0 do B5 (`claude/arquitetura_calibracao_pontas.md`).

    SOMENTE LEITURA. Nenhuma tabela é criada, alterada ou gravada. A sessão
    vira só-leitura pelo próprio banco (conferido) e o script termina em
    rollback.

    python simular_b5.py [--ajuste-desde D] [--ajuste-ate D] [--teste-desde D] [--teste-ate D]

    Datas em AAAA-MM-DD, pela data do apito em Brasília. Padrão:
      ajuste = rodadas 29-30 (07/10 a 12/10)
      teste  = rodada 31     (16/10 a 19/10) — sem foto ainda, só o ajuste sai

--------------------------------------------------------------------------
O QUE FAZ
--------------------------------------------------------------------------

1. Foto, unidade e veredito são os do `avaliar_registro_modelo` (IMPORTADOS:
   SQL_FOTO, montar_unidades, avaliar_resultado, base_do_par,
   marcar_casa_sem_margem). Nada é reescrito.
2. Só entram unidades de DOIS LADOS (mais/menos, sim/não). O lado "outra"
   fica fora (arquitetura §3).
3. Probabilidade FINAL de cada candidato = o que o sistema acreditaria:
     hoje (linha de base) ... encolher_de_produção(p)
     A — temperatura T ...... encolher_de_produção(σ(logit(p)/T))
     B — linear λ ........... encolher_de_produção(50 + λ(p − 50))
     C — casa mais forte K .. encolhimento com K, em TODOS os mercados de dois lados
   O encolhimento é a função de produção (`motor_recomendacoes`), com K e
   escopo trocados só dentro deste processo no candidato C.
4. Brier por PAR (cada par uma vez — modelo e casa dão p e 100 − p e o
   resultado se inverte, então o erro é igual nos dois lados).
5. Ajuste: grade em cada parâmetro, menor Brier no período de ajuste.
6. Teste (se houver foto no período): critérios T1-T4 da arquitetura §4.3,
   aplicados à probabilidade FINAL.

⚠️ Unidade de jogo inteiro = média das duas perspectivas, com a odd da
linha representante (a mesma convenção do medidor). Em produção o filtro é
perspectiva a perspectiva — a diferença é pequena e igual para todos os
candidatos.
"""

import math
import os
import sys
from datetime import date, datetime

import psycopg2

import avaliar_registro_modelo as ar                 # foto, unidade, par (PRODUÇÃO)
from avaliacao import avaliar_resultado               # veredito (fonte única)
import motor_recomendacoes as mr                      # encolhimento e pisos (PRODUÇÃO)

DATABASE_URL = os.environ["DATABASE_URL"]

PADRAO = {"ajuste_desde": date(2026, 10, 7), "ajuste_ate": date(2026, 10, 12),
          "teste_desde": date(2026, 10, 16), "teste_ate": date(2026, 10, 19)}

GRADE_T = [round(1.0 + 0.05 * i, 2) for i in range(41)]          # 1,00 a 3,00
GRADE_L = [round(0.50 + 0.02 * i, 2) for i in range(26)]         # 0,50 a 1,00
GRADE_K = [15.0 + 2.5 * i for i in range(19)]                     # 15 a 60
MELHORA_MINIMA_T1 = 0.003
EMPATE = 0.001
CLASSES = ("JOGADOR", "TIME", "JOGO INTEIRO")


# ---------------------------------------------------------------------------
# transformações
# ---------------------------------------------------------------------------
def temperatura(p, t):
    q = min(max(p, 0.5), 99.5) / 100.0
    z = math.log(q / (1.0 - q)) / t
    return 100.0 / (1.0 + math.exp(-z))


def linear(p, lam):
    return 50.0 + lam * (p - 50.0)


def encolher_producao(p, odd, tipo):
    if not odd:
        return p
    return mr.encolher_para_o_mercado(p, float(odd), tipo)[0]


def encolher_k(p, odd, tipo, k):
    """A função de PRODUÇÃO com K e escopo trocados só durante a chamada."""
    if not odd:
        return p
    k_antes, escopo_antes = mr.VALOR_K_ENCOLHIMENTO_MERCADO, mr.MERCADOS_CORRECAO_DIVERGENCIA_MERCADO
    mr.VALOR_K_ENCOLHIMENTO_MERCADO = k
    mr.MERCADOS_CORRECAO_DIVERGENCIA_MERCADO = set(escopo_antes) | {tipo}
    try:
        return mr.encolher_para_o_mercado(p, float(odd), tipo)[0]
    finally:
        mr.VALOR_K_ENCOLHIMENTO_MERCADO, mr.MERCADOS_CORRECAO_DIVERGENCIA_MERCADO = k_antes, escopo_antes


def candidato(nome, param):
    if nome == "hoje":
        return lambda u: encolher_producao(u["p"], u["odd_ref"], u["tipo_padrao"])
    if nome == "A":
        return lambda u: encolher_producao(temperatura(u["p"], param), u["odd_ref"], u["tipo_padrao"])
    if nome == "B":
        return lambda u: encolher_producao(linear(u["p"], param), u["odd_ref"], u["tipo_padrao"])
    if nome == "C":
        return lambda u: encolher_k(u["p"], u["odd_ref"], u["tipo_padrao"], param)
    raise ValueError(nome)


# ---------------------------------------------------------------------------
# medidas
# ---------------------------------------------------------------------------
def acertou(u):
    return 1.0 if u["resultado"] == "acertou" else 0.0


def um_por_par(unidades):
    """Uma unidade por par (prefere o lado "mais"); unidade sem par também entra."""
    escolhidas = {}
    for u in unidades:
        base = ar.base_do_par(u)
        atual = escolhidas.get(base)
        if atual is None or (ar.lado(u["direcao"]) == "mais" and ar.lado(atual["direcao"]) != "mais"):
            escolhidas[base] = u
    return list(escolhidas.values())


def brier(unidades, f):
    if not unidades:
        return None
    return sum((f(u) / 100.0 - acertou(u)) ** 2 for u in unidades) / len(unidades)


def gap_pontas(unidades, f):
    """{(lado, faixa): (N, observado − previsto)} nas faixas 0-20 e 80-100 do p CRU."""
    saida = {}
    for l in ("mais", "menos"):
        for nome, lo, hi in (("0-20", 0, 20), ("80-100", 80, 100.0001)):
            sel = [u for u in unidades if ar.lado(u["direcao"]) == l and lo <= u["p"] < hi]
            if sel:
                obs = 100.0 * sum(acertou(u) for u in sel) / len(sel)
                prev = sum(f(u) for u in sel) / len(sel)
                saida[(l, nome)] = (len(sel), obs - prev)
    return saida


def m5(unidades, f):
    """O que passaria no filtro (VE > piso de produção) com a probabilidade final f."""
    passou = []
    for u in unidades:
        if not u["odd_ref"]:
            continue
        odd = float(u["odd_ref"])
        p = f(u)
        piso = (mr.VALOR_ESPERADO_MINIMO_CORRIGIDO if u["tipo_padrao"] in mr.MERCADOS_CORRECAO_DIVERGENCIA_MERCADO
                else mr.VALOR_ESPERADO_MINIMO)
        if round(p / 100.0 * odd - 1, 3) > piso:
            passou.append((u, p, odd))
    if not passou:
        return {"n": 0, "gap": None, "roi": None}
    obs = 100.0 * sum(acertou(u) for u, _, _ in passou) / len(passou)
    prev = sum(p for _, p, _ in passou) / len(passou)
    lucro = sum((odd - 1) if acertou(u) else -1.0 for u, _, odd in passou)
    return {"n": len(passou), "gap": obs - prev, "roi": 100.0 * lucro / len(passou)}


def ajustar(pares):
    resultado = {}
    for nome, grade in (("A", GRADE_T), ("B", GRADE_L), ("C", GRADE_K)):
        curva = [(param, brier(pares, candidato(nome, param))) for param in grade]
        melhor = min(curva, key=lambda x: x[1])
        resultado[nome] = (melhor[0], melhor[1], curva)
    return resultado


# ---------------------------------------------------------------------------
# leitura
# ---------------------------------------------------------------------------
def carregar(cur, desde, ate):
    cur.execute(ar.SQL_FOTO, (desde, desde, ate, ate))
    fotos = [dict(zip(ar.COLUNAS, r)) for r in cur.fetchall()]
    fotos = [f for f in fotos if f["fixture_jogos"] == f["fixture_id_api"]]
    if not fotos:
        return [], 0
    unidades, _ = ar.montar_unidades(fotos)
    for u in unidades:
        u["resultado"] = avaliar_resultado(cur, u["tipo_padrao"], u["jogador_id"], u["jogo_id"],
                                           u["linha"], u["descricao"], u["direcao"])
    avaliadas = [u for u in unidades if u["resultado"] in ("acertou", "errou")
                 and ar.lado(u["direcao"]) != "outra"]
    ar.marcar_casa_sem_margem(avaliadas)
    return avaliadas, len({f["fixture_id_api"] for f in fotos})


# ---------------------------------------------------------------------------
# impressão
# ---------------------------------------------------------------------------
def nome_param(nome, param):
    if nome == "hoje":
        return "produção de hoje"
    if nome == "A":
        return f"A · T={param:.2f}"
    if nome == "B":
        return f"B · λ={param:.2f}"
    return f"C · K={param:.1f}"


def imprimir_bloco(titulo, unidades, n_jogos, escolhas):
    pares = um_por_par(unidades)
    print(f"\n## {titulo}: {n_jogos} jogo(s) · {len(unidades)} unidade(s) de dois lados · {len(pares)} par(es)")
    justas = [u for u in pares if u.get("p_casa_justa") is not None]
    if justas:
        print(f"  referência — casa sem margem (nos {len(justas)} pares com odd dos dois lados): "
              f"Brier {brier(justas, lambda u: u['p_casa_justa']):.4f} · "
              f"produção de hoje nos mesmos: {brier(justas, candidato('hoje', None)):.4f}")
    print(f"  {'candidato':<20} {'Brier':>8} " + " ".join(f"{c[:12]:>12}" for c in CLASSES)
          + f" {'M5 n':>6} {'M5 gap':>8} {'M5 ROI':>8}")
    linhas = {}
    for nome, param in escolhas:
        f = candidato(nome, param)
        por_classe = [brier([u for u in pares if u["classe"] == c], f) for c in CLASSES]
        r5 = m5(unidades, f)
        linhas[(nome, param)] = (brier(pares, f), por_classe, r5, gap_pontas(unidades, f))
        b = linhas[(nome, param)][0]
        txt = " ".join(f"{x:>12.4f}" if x is not None else f"{'—':>12}" for x in por_classe)
        gap = f"{r5['gap']:>+8.1f}" if r5["gap"] is not None else f"{'—':>8}"
        roi = f"{r5['roi']:>+7.1f}%" if r5["roi"] is not None else f"{'—':>8}"
        print(f"  {nome_param(nome, param):<20} {b:>8.4f} {txt} {r5['n']:>6} {gap} {roi}")
    print("  erro nas pontas (observado − previsto, faixa pelo p CRU):")
    for (nome, param), (_, _, _, pontas) in linhas.items():
        txt = " · ".join(f"{l} {fx}: {g:+.1f} (N={n})" for (l, fx), (n, g) in sorted(pontas.items()))
        print(f"    {nome_param(nome, param):<20} {txt}")
    return linhas


def julgar(linhas, escolhas):
    base = linhas[("hoje", None)]
    print("\n## DECISÃO PELOS CRITÉRIOS T1-T4 (arquitetura §4.3)")
    aprovados = []
    for nome, param in escolhas:
        if nome == "hoje":
            continue
        b, classes, r5, pontas = linhas[(nome, param)]
        t1 = b <= base[0] - MELHORA_MINIMA_T1
        t2 = sum(1 for c, cb in zip(classes, base[1]) if c is not None and cb is not None and c < cb) >= 2
        t3 = all(abs(pontas[k][1]) < abs(base[3][k][1]) for k in base[3] if k in pontas)
        t4 = (r5["gap"] is None or base[2]["gap"] is None or abs(r5["gap"]) <= abs(base[2]["gap"]))
        ok = t1 and t2 and t3 and t4
        print(f"  {nome_param(nome, param):<20} T1 {'✅' if t1 else '❌'} (Brier {b:.4f} × hoje {base[0]:.4f}) · "
              f"T2 {'✅' if t2 else '❌'} · T3 {'✅' if t3 else '❌'} · T4 {'✅' if t4 else '❌'} → "
              f"{'ACEITO' if ok else 'reprovado'}")
        if ok:
            aprovados.append((b, "BAC".index(nome), nome, param))
    if not aprovados:
        print("\n  → nenhum candidato passou: NADA MUDA (arquitetura §4.3).")
        return
    aprovados.sort()
    melhor = aprovados[0]
    empatados = [a for a in aprovados if a[0] - melhor[0] < EMPATE]
    escolhido = min(empatados, key=lambda a: a[1])
    print(f"\n  → vencedor: {nome_param(escolhido[2], escolhido[3])} "
          f"(menor Brier; empate < {EMPATE} → o mais simples: B, depois A, depois C).")
    print("    Decisão final é do usuário (arquitetura §5, Fase 1).")


def ler_opcoes(argv):
    opcoes = dict(PADRAO)
    i = 1
    while i < len(argv):
        chave = argv[i].lstrip("-").replace("-", "_")
        if chave in opcoes and i + 1 < len(argv):
            opcoes[chave] = date.fromisoformat(argv[i + 1])
            i += 2
        else:
            print(__doc__)
            raise SystemExit(f"argumento não reconhecido: {argv[i]}")
    return opcoes


def main():
    opcoes = ler_opcoes(sys.argv)
    print("=" * 78)
    print("simular_b5.py | SOMENTE LEITURA (rollback no fim)")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now().isoformat(timespec='seconds')}")
    print(f"ajuste: {opcoes['ajuste_desde']} a {opcoes['ajuste_ate']} · teste: {opcoes['teste_desde']} a {opcoes['teste_ate']} (BRT)")
    print(f"K de produção: {mr.VALOR_K_ENCOLHIMENTO_MERCADO} · escopo de produção: {len(mr.MERCADOS_CORRECAO_DIVERGENCIA_MERCADO)} mercados")
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
        ajuste, n_jogos = carregar(cur, opcoes["ajuste_desde"], opcoes["ajuste_ate"])
        if not ajuste:
            print("\nnenhuma unidade avaliada no período de ajuste — nada a fazer.")
            return
        pares = um_por_par(ajuste)
        fit = ajustar(pares)
        print(f"\n## AJUSTE (menor Brier por par no período de ajuste, {len(pares)} pares)")
        for nome in ("A", "B", "C"):
            param, b, curva = fit[nome]
            borda = " ⚠️ no limite da grade" if param in (curva[0][0], curva[-1][0]) else ""
            print(f"  {nome_param(nome, param):<20} Brier {b:.4f}{borda}")
        escolhas = [("hoje", None)] + [(n, fit[n][0]) for n in ("A", "B", "C")]
        imprimir_bloco("PERÍODO DE AJUSTE (dentro da amostra — não decide nada)", ajuste, n_jogos, escolhas)

        teste, n_teste = carregar(cur, opcoes["teste_desde"], opcoes["teste_ate"])
        if not teste:
            print(f"\n## TESTE: nenhuma unidade avaliada entre {opcoes['teste_desde']} e {opcoes['teste_ate']} — "
                  f"rodar de novo depois que a rodada fechar.")
            return
        linhas = imprimir_bloco("PERÍODO DE TESTE (fora da amostra — é este que decide)", teste, n_teste, escolhas)
        julgar(linhas, escolhas)
    finally:
        conn.rollback()   # incondicional: este script nunca grava
        cur.close()
        conn.close()
        print("\n" + "=" * 78)
        print("SOMENTE LEITURA — nada foi gravado (nem `registro_modelo`, nem outra tabela).")
        print("=" * 78)


if __name__ == "__main__":
    main()
