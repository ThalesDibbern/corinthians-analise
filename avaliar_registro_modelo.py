"""
avaliar_registro_modelo.py — mede o MODELO a partir da foto pré-apito gravada
em `public.registro_modelo` (`claude/arquitetura_registro_modelo.md §5`).

    SOMENTE LEITURA. Nenhuma tabela é criada, alterada ou gravada. A conexão
    vira só-leitura pelo próprio banco e o script termina em rollback.

    python avaliar_registro_modelo.py [--desde AAAA-MM-DD] [--ate AAAA-MM-DD]

  --desde / --ate   filtram pela DATA DO APITO em horário de Brasília
                    (calendário se lê por data, nunca por rodada — §1 caso 13).
                    Sem eles: todos os jogos já iniciados que têm foto.

--------------------------------------------------------------------------
O QUE ELE FAZ (nada é reescrito — tudo é importado)
--------------------------------------------------------------------------

1. FOTO QUE CONTA: a ÚLTIMA antes do apito de cada
   (jogo_id, tipo_padrao, linha, direcao, jogador_id). O banco já recusa
   foto depois do apito (CHECK da tabela); aqui isso é conferido de novo.
2. UNIDADE DE MEDIDA — a mesma identidade do arquivamento de produção:
     - mercado de JOGADOR (jogador_id preenchido): fixture + jogador
     - mercado de JOGO INTEIRO (`combinacoes.MERCADOS_JOGO_INTEIRO`): fixture
       — as duas perspectivas viram UMA unidade, com a MÉDIA das duas
       probabilidades (a regra do `arquivar_recomendacoes`)
     - todo o resto é mercado de TIME: jogo_id (a perspectiva)
   ⚠️ CARTÃO DE TIME (`cartao` com jogador_id vazio) é mercado de TIME, por
   jogo_id — embora `cartao` esteja em `MERCADOS_JOGADOR`. É o defeito de
   chave achado em 06/10 no arquivamento; aqui ele NÃO se repete.
3. VEREDITO: `avaliacao.avaliar_resultado` — a fonte única de produção,
   importada — com o jogo_id e a descrição da MESMA linha (a de menor
   jogo_id do grupo), nunca misturados. Não grava veredito: recalcula do
   dado cru a cada execução. "pendente" fica fora das taxas e é contado.
4. MEDIDAS (escritas antes do dado, §5 da arquitetura):
     M1  calibração por faixa de p_modelo: previsto × observado
     M2  por classe (JOGADOR / TIME / JOGO INTEIRO) × direção, e por mercado
     M3  M1 com e sem as unidades de jogo inteiro em que as duas
         perspectivas discordam mais que `TOLERANCIA_COERENCIA` (a do
         detector, importada) — o defeito do §5.16 medido linha a linha
     M4  gap contra a casa de referência (100/odd_ref, COM a margem)
     M5  o subconjunto que o sistema TERIA recomendado: encolhimento de
         produção (`encolher_para_o_mercado`, importado) contra odd_ref e
         VE > piso de produção, perspectiva a perspectiva; ROI com stake 1
         na odd de referência. ⚠️ Referência = superbet.rs, não a Superbet
         BR: M4 e M5 não se comparam número a número com as rodadas 24-28.

Contar JOGOS, não linhas: toda tabela traz `fixtures` ao lado do N.
"""

import os
import sys
from datetime import date, datetime

import psycopg2

from avaliacao import avaliar_resultado                    # fonte ÚNICA de acerto
from combinacoes import MERCADOS_JOGO_INTEIRO              # conjunto de PRODUÇÃO
from detector_coerencia_perspectivas import TOLERANCIA_COERENCIA
import motor_recomendacoes as mr                           # encolhimento e pisos de PRODUÇÃO

DATABASE_URL = os.environ["DATABASE_URL"]

FAIXAS = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 100.0001)]

SQL_FOTO = """
SELECT DISTINCT ON (rm.jogo_id, rm.tipo_padrao, rm.linha, rm.direcao, rm.jogador_id)
       rm.id, rm.registrado_em, rm.fixture_id_api, rm.jogo_id, rm.datahora_jogo,
       rm.tipo_padrao, rm.linha, rm.direcao, rm.jogador_id, rm.descricao,
       rm.p_modelo, rm.casa_ref, rm.odd_ref,
       j.fixture_id_api AS fixture_jogos, j.rodada_numero,
       ((rm.datahora_jogo AT TIME ZONE 'UTC') AT TIME ZONE 'America/Sao_Paulo')::date AS dia_brt
FROM public.registro_modelo rm
JOIN public.jogos j ON j.id = rm.jogo_id
WHERE rm.datahora_jogo < (now() AT TIME ZONE 'UTC')
  AND rm.registrado_em < (rm.datahora_jogo AT TIME ZONE 'UTC')
  AND (%s::date IS NULL OR ((rm.datahora_jogo AT TIME ZONE 'UTC') AT TIME ZONE 'America/Sao_Paulo')::date >= %s::date)
  AND (%s::date IS NULL OR ((rm.datahora_jogo AT TIME ZONE 'UTC') AT TIME ZONE 'America/Sao_Paulo')::date <= %s::date)
ORDER BY rm.jogo_id, rm.tipo_padrao, rm.linha, rm.direcao, rm.jogador_id,
         rm.registrado_em DESC, rm.id DESC
"""

COLUNAS = ["id", "registrado_em", "fixture_id_api", "jogo_id", "datahora_jogo", "tipo_padrao", "linha",
           "direcao", "jogador_id", "descricao", "p_modelo", "casa_ref", "odd_ref", "fixture_jogos",
           "rodada_numero", "dia_brt"]

SQL_STATUS = """
SELECT fixture_id_api, status FROM public.jogos_liga WHERE fixture_id_api = ANY(%s)
"""


# ---------------------------------------------------------------------------
# classificação e unidades
# ---------------------------------------------------------------------------
def classe_da_linha(f):
    if f["jogador_id"] is not None:
        return "JOGADOR"
    if f["tipo_padrao"] in MERCADOS_JOGO_INTEIRO:
        return "JOGO INTEIRO"
    return "TIME"   # inclui cartão de TIME (cartao sem jogador)


def chave_da_unidade(f):
    classe = classe_da_linha(f)
    if classe == "JOGADOR":
        return (classe, f["fixture_id_api"], f["tipo_padrao"], f["jogador_id"], f["linha"], f["direcao"])
    if classe == "JOGO INTEIRO":
        return (classe, f["fixture_id_api"], f["tipo_padrao"], f["linha"], f["direcao"])
    return (classe, f["jogo_id"], f["tipo_padrao"], f["linha"], f["direcao"])


def grupo_direcao(direcao):
    d = (direcao or "").strip().lower()
    if d in ("mais", "menos"):
        return d
    if d in ("sim", "não", "nao"):
        return "sim/não"
    return "outra"


def passa_no_filtro(f):
    """O que o motor de produção faria com esta linha e a odd de referência:
    encolhe contra a odd (função importada) e aplica o piso de VE de
    produção (o mesmo `if` do motor). Devolve (passou, p_final)."""
    if f["odd_ref"] is None:
        return False, None
    odd = float(f["odd_ref"])
    p_final, _ = mr.encolher_para_o_mercado(float(f["p_modelo"]), odd, f["tipo_padrao"])
    ve = round((p_final / 100) * odd - 1, 3)
    if f["tipo_padrao"] in mr.MERCADOS_CORRECAO_DIVERGENCIA_MERCADO:
        piso = mr.VALOR_ESPERADO_MINIMO_CORRIGIDO
    else:
        piso = mr.VALOR_ESPERADO_MINIMO
    return ve > piso, p_final


def montar_unidades(fotos):
    grupos = {}
    for f in fotos:
        grupos.setdefault(chave_da_unidade(f), []).append(f)

    unidades = []
    odd_divergente = 0
    for chave, membros in grupos.items():
        rep = min(membros, key=lambda m: m["jogo_id"])   # jogo_id e descrição vêm JUNTOS desta linha
        ps = [float(m["p_modelo"]) for m in membros]
        odds = {m["odd_ref"] for m in membros if m["odd_ref"] is not None}
        if len(odds) > 1:
            odd_divergente += 1
        filtro = [passa_no_filtro(m) for m in membros]
        passaram = [p for ok, p in filtro if ok]
        unidades.append({
            "classe": chave[0],
            "fixture_id_api": rep["fixture_id_api"],
            "rodada_numero": rep["rodada_numero"],
            "tipo_padrao": rep["tipo_padrao"],
            "jogador_id": rep["jogador_id"],
            "jogo_id": rep["jogo_id"],
            "linha": rep["linha"],
            "direcao": rep["direcao"],
            "descricao": rep["descricao"],
            "odd_ref": rep["odd_ref"],
            "p": sum(ps) / len(ps),
            "perspectivas": len(membros),
            "divergencia": (max(ps) - min(ps)) if len(ps) > 1 else None,
            "passou_filtro": bool(passaram),
            "p_filtro": (sum(passaram) / len(passaram)) if passaram else None,
        })
    return unidades, odd_divergente


# ---------------------------------------------------------------------------
# impressão
# ---------------------------------------------------------------------------
def linha_tabela(rotulo, itens, campo_p="p", com_casa=False, com_roi=False):
    n = len(itens)
    if n == 0:
        print(f"  {rotulo:<26} {0:>6}")
        return
    fixtures = len({u["fixture_id_api"] for u in itens})
    prev = sum(u[campo_p] for u in itens) / n
    obs = 100.0 * sum(1 for u in itens if u["resultado"] == "acertou") / n
    texto = f"  {rotulo:<26} {n:>6} {fixtures:>8} {prev:>9.1f} {obs:>9.1f} {obs - prev:>+8.1f}"
    if com_casa:
        com_odd = [u for u in itens if u["odd_ref"]]
        if com_odd:
            casa = sum(100.0 / float(u["odd_ref"]) for u in com_odd) / len(com_odd)
            obs_c = 100.0 * sum(1 for u in com_odd if u["resultado"] == "acertou") / len(com_odd)
            texto += f" {casa:>9.1f} {obs_c - casa:>+9.1f}  (n odd {len(com_odd)})"
    if com_roi:
        lucro = sum((float(u["odd_ref"]) - 1) if u["resultado"] == "acertou" else -1.0 for u in itens)
        texto += f" {100.0 * lucro / n:>+8.1f}%"
    print(texto)


def cabecalho_tabela(com_casa=False, com_roi=False):
    texto = f"  {'':<26} {'N':>6} {'fixtures':>8} {'previsto':>9} {'observ.':>9} {'gap':>8}"
    if com_casa:
        texto += f" {'casa':>9} {'gap casa':>9}"
    if com_roi:
        texto += f" {'ROI':>9}"
    print(texto)


def por_faixa(itens, campo_p="p", com_casa=False, com_roi=False):
    cabecalho_tabela(com_casa, com_roi)
    for lo, hi in FAIXAS:
        sel = [u for u in itens if lo <= u[campo_p] < hi]
        linha_tabela(f"{lo:.0f}-{min(hi, 100):.0f}%", sel, campo_p, com_casa, com_roi)
    linha_tabela("TOTAL", itens, campo_p, com_casa, com_roi)


def imprimir_medidas(avaliadas):
    print("\n## M1 — CALIBRAÇÃO POR FAIXA (todas as unidades avaliadas)")
    por_faixa(avaliadas)

    print("\n## M2 — POR CLASSE × DIREÇÃO")
    cabecalho_tabela()
    for classe in ("JOGADOR", "TIME", "JOGO INTEIRO"):
        for d in ("mais", "menos", "sim/não", "outra"):
            sel = [u for u in avaliadas if u["classe"] == classe and grupo_direcao(u["direcao"]) == d]
            if sel:
                linha_tabela(f"{classe} · {d}", sel)
    print("\n## M2 — POR MERCADO")
    cabecalho_tabela()
    for tipo in sorted({u["tipo_padrao"] for u in avaliadas}):
        for classe in ("JOGADOR", "TIME", "JOGO INTEIRO"):
            sel = [u for u in avaliadas if u["tipo_padrao"] == tipo and u["classe"] == classe]
            if sel:
                rotulo = tipo if tipo != "cartao" else f"cartao ({classe.lower()})"
                linha_tabela(rotulo, sel)

    print(f"\n## M3 — COERÊNCIA ENTRE PERSPECTIVAS (jogo inteiro, tolerância {TOLERANCIA_COERENCIA} ponto)")
    jogo = [u for u in avaliadas if u["classe"] == "JOGO INTEIRO"]
    duas = [u for u in jogo if u["divergencia"] is not None]
    contra = [u for u in duas if u["divergencia"] > TOLERANCIA_COERENCIA]
    print(f"  unidades de jogo inteiro: {len(jogo)} · com as duas perspectivas: {len(duas)} · "
          f"discordantes: {len(contra)} ({(100.0 * len(contra) / len(duas)) if duas else 0:.1f}%)")
    if duas:
        divs = sorted(u["divergencia"] for u in duas)
        print(f"  |P_A − P_B|: mediana {divs[len(divs) // 2]:.1f} · p90 {divs[int(0.9 * (len(divs) - 1))]:.1f}"
              f" · máx {divs[-1]:.1f}")
    print("  M1 COM as discordantes:")
    cabecalho_tabela()
    linha_tabela("TOTAL", avaliadas)
    print("  M1 SEM as discordantes:")
    ids_contra = {id(u) for u in contra}
    cabecalho_tabela()
    linha_tabela("TOTAL", [u for u in avaliadas if id(u) not in ids_contra])

    print("\n## M4 — CONTRA A CASA DE REFERÊNCIA (100/odd_ref, com margem), por faixa de p_modelo")
    por_faixa(avaliadas, com_casa=True)

    print("\n## M5 — O QUE O SISTEMA TERIA RECOMENDADO (encolhido contra odd_ref, VE > piso)")
    filtradas = [u for u in avaliadas if u["passou_filtro"]]
    print(f"  {len(filtradas)} de {len(avaliadas)} unidades passariam no filtro. "
          f"Previsto = probabilidade JÁ encolhida.")
    por_faixa(filtradas, campo_p="p_filtro", com_casa=True, com_roi=True)
    print("  por classe × direção:")
    cabecalho_tabela(com_roi=True)
    for classe in ("JOGADOR", "TIME", "JOGO INTEIRO"):
        for d in ("mais", "menos", "sim/não", "outra"):
            sel = [u for u in filtradas if u["classe"] == classe and grupo_direcao(u["direcao"]) == d]
            if sel:
                linha_tabela(f"{classe} · {d}", sel, campo_p="p_filtro", com_roi=True)
    print("  sem as discordantes:")
    cabecalho_tabela(com_roi=True)
    linha_tabela("TOTAL", [u for u in filtradas if id(u) not in ids_contra], campo_p="p_filtro", com_roi=True)


# ---------------------------------------------------------------------------
def ler_opcoes(argv):
    opcoes = {"desde": None, "ate": None}
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("--desde", "--ate") and i + 1 < len(argv):
            opcoes[a[2:]] = date.fromisoformat(argv[i + 1])
            i += 2
        elif a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        else:
            print(__doc__)
            raise SystemExit(f"argumento não reconhecido: {a}")
    return opcoes


def main():
    opcoes = ler_opcoes(sys.argv)
    print("=" * 78)
    print("avaliar_registro_modelo.py | SOMENTE LEITURA (rollback no fim)")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now().isoformat(timespec='seconds')}")
    print(f"filtro de data (BRT): desde {opcoes['desde'] or '—'} · até {opcoes['ate'] or '—'}")
    print("=" * 78)

    for nome in ("encolher_para_o_mercado", "VALOR_ESPERADO_MINIMO", "VALOR_ESPERADO_MINIMO_CORRIGIDO",
                 "MERCADOS_CORRECAO_DIVERGENCIA_MERCADO"):
        if not hasattr(mr, nome):
            raise SystemExit(f"motor_recomendacoes.{nome} não existe mais — script desatualizado.")

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    # Só vale a partir da PRÓXIMA transação — por isso o commit e a conferência.
    cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    conn.commit()
    cur.execute("SHOW transaction_read_only")
    if str(cur.fetchone()[0]).lower() != "on":
        conn.close()
        raise SystemExit("🔴 a conexão NÃO ficou só-leitura — nada foi lido nem medido.")
    try:
        cur.execute("SELECT to_regclass('public.registro_modelo') IS NOT NULL")
        if not cur.fetchone()[0]:
            print("`public.registro_modelo` não existe — nada a medir.")
            return
        d, a = opcoes["desde"], opcoes["ate"]
        cur.execute(SQL_FOTO, (d, d, a, a))
        fotos = [dict(zip(COLUNAS, r)) for r in cur.fetchall()]
        if not fotos:
            print("\nnenhuma foto de jogo já iniciado nesse intervalo — nada a medir.")
            return

        incoerentes = [f for f in fotos if f["fixture_jogos"] != f["fixture_id_api"]]
        if incoerentes:
            print(f"\n🔴 {len(incoerentes)} linha(s) com fixture_id_api diferente do de `jogos` — ficam FORA.")
            fotos = [f for f in fotos if f["fixture_jogos"] == f["fixture_id_api"]]

        fixtures = sorted({f["fixture_id_api"] for f in fotos})
        cur.execute(SQL_STATUS, (fixtures,))
        status = dict(cur.fetchall())
        fotos_distintas = len({f["registrado_em"] for f in fotos})
        print(f"\nlinhas (última foto antes do apito): {len(fotos)} · fixtures: {len(fixtures)} · "
              f"fotos distintas usadas: {fotos_distintas}")

        unidades, odd_divergente = montar_unidades(fotos)
        if odd_divergente:
            print(f"⚠️ {odd_divergente} unidade(s) com odd_ref diferente entre perspectivas — usada a da "
                  f"linha representante.")
        for u in unidades:
            u["resultado"] = avaliar_resultado(cur, u["tipo_padrao"], u["jogador_id"], u["jogo_id"],
                                               u["linha"], u["descricao"], u["direcao"])

        print("\n## POR JOGO")
        print(f"  {'fixture':>8} {'rod.':>4} {'status':>6} {'unid.':>6} {'acert.':>6} {'errou':>6} {'pend.':>6}  dia (BRT)")
        dia = {}
        for f in fotos:
            dia[f["fixture_id_api"]] = f["dia_brt"]
        for fx in fixtures:
            us = [u for u in unidades if u["fixture_id_api"] == fx]
            cont = {r: sum(1 for u in us if u["resultado"] == r) for r in ("acertou", "errou", "pendente")}
            print(f"  {fx:>8} {us[0]['rodada_numero'] or '—':>4} {status.get(fx) or '—':>6} {len(us):>6} "
                  f"{cont['acertou']:>6} {cont['errou']:>6} {cont['pendente']:>6}  {dia[fx]}")

        pendentes = [u for u in unidades if u["resultado"] == "pendente"]
        avaliadas = [u for u in unidades if u["resultado"] in ("acertou", "errou")]
        outros = [u for u in unidades if u["resultado"] not in ("acertou", "errou", "pendente")]
        print(f"\nunidades: {len(unidades)} · avaliadas: {len(avaliadas)} · pendentes: {len(pendentes)}"
              + (f" · resultado inesperado: {len(outros)}" if outros else ""))
        if pendentes:
            por_tipo = {}
            for u in pendentes:
                por_tipo[u["tipo_padrao"]] = por_tipo.get(u["tipo_padrao"], 0) + 1
            print("  pendentes por mercado: " + ", ".join(f"{t} {n}" for t, n in sorted(por_tipo.items())))
            print("  (pendente = dado do jogo ainda não maduro — estatística < 6h ou placar ausente. "
                  "Rodar de novo depois do cron das 09:00.)")
        if not avaliadas:
            print("\nnada avaliado ainda — nenhuma medida a imprimir.")
            return

        imprimir_medidas(avaliadas)
    finally:
        conn.rollback()   # incondicional: este script nunca grava
        cur.close()
        conn.close()
        print("\n" + "=" * 78)
        print("SOMENTE LEITURA — nada foi gravado (nem `registro_modelo`, nem outra tabela).")
        print("=" * 78)


if __name__ == "__main__":
    main()
