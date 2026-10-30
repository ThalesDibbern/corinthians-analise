"""
simular_b7.py — SÓ LEITURA. Fase 0 do B7 (`claude/arquitetura_multiplas_b7.md §5`).

Nada é gravado em tabela nenhuma: a sessão é READ ONLY (conferida com
`SHOW transaction_read_only`) e o script termina em rollback incondicional.

    python simular_b7.py verificar [--desde AAAA-MM-DD] [--ate AAAA-MM-DD]
                                   [--casa superbet.rs] [--limiar 0.95] [--mesmo-assim]

`--desde`/`--ate`: datas (BRT) do PRIMEIRO apito das múltiplas que entram na
conta. Padrão: 16/10 a 31/10 (rodadas 31 a 33 da superbet.rs).

--------------------------------------------------------------------------
O QUE FAZ
--------------------------------------------------------------------------

1. Reconstrói, para cada execução do cron (`lote` = minuto de `registrado_em`
   no `registro_publico` do Brasileirão nessa casa), o conjunto de
   recomendações que estava em `recomendacoes` naquele momento:
     - para cada aposta (jogo_id, tipo, linha, direção, jogador, casa), o
       último evento até o lote é `ativa`;
     - e o jogo ainda não foi arquivado: começa depois do arquivamento
       daquele dia (o cron das 12:00 UTC arquiva o que já começou; o que
       começou entre 12:00 e o lote continua na tabela e entra na montagem,
       como em produção).
   Os campos do jogo (adversário, data, time) vêm de `jogos`. A deduplicação
   é a de produção (`deduplicar_recomendacoes`, importada).

2. Roda a `montar_combinacoes` de PRODUÇÃO (importada) duas vezes:
     - ANTIGA: como está no ar (até 5 pernas pelo teto adaptativo, faixa
       permitida, 300 múltiplas);
     - NOVA (B7): sem faixa e no máximo 2 pernas. Feito trocando, só durante
       a chamada, `LARGURA_MINIMA_FAIXA` por infinito (nenhum par vira faixa
       permitida, então o pool fica com as 2 melhores linhas por mercado e
       qualquer par do mesmo mercado é recusado pela regra que já existe) e
       `combinations` por uma que não gera combinação com mais de 2 pernas.
       É exatamente o `min(adaptativo, 2)` e o `faixa_permitida` vazio da
       arquitetura §4.

3. Simula a captura de produção (`capturar_candidatas_multiplas` +
   `congelar_candidatas_vencidas`): upsert por assinatura
   (`_assinatura_combo`, importada) enquanto não congelada; congela quando
   o primeiro apito <= o lote. Nunca apaga — como em produção (§5.18).

4. CONTROLE (para antes de avaliar): a reconstrução ANTIGA tem de reproduzir
   as `multiplas_candidatas` reais da janela, por assinatura. Se a
   reprodução ficar abaixo de `--limiar` (nos dois sentidos), a simulação
   não vale e o script para ali (ou segue com `--mesmo-assim`, só para
   diagnóstico, com o aviso impresso).

5. Avalia cada múltipla congelada da janela: veredito de cada perna por
   `buscar_resultado_perna` (importada, o mesmo do arquivamento); perna sem
   veredito no histórico (retirada antes do apito, ou que só a regra nova
   montaria) é avaliada direto por `avaliar_resultado` (importada), como o
   `resultado_sombra` do C4. Múltipla: acertou se todas acertaram; errou se
   alguma errou; senão pendente.

6. Imprime, para as duas regras: N, avaliadas, previsto, observado, ROI, no
   total e por rodada; o que FICA (nas duas), o que SAI (só antiga) e o que
   ENTRA (só nova); e o M2 do C4 dentro da simulação (múltipla que já tinha
   saído do top 300 antes do apito × a que estava nele no último lote).

Critério escrito antes (arquitetura §5): a regra NOVA tem de ter ROI melhor
que a ANTIGA no total e em pelo menos 2 das 3 rodadas (ou na maioria das
rodadas com avaliadas, se houver menos de 3). Senão o B7 para.

Variáveis de ambiente: DATABASE_URL (obrigatória).
"""

import argparse
import contextlib
import itertools
import os
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

import psycopg2

import combinacoes
from arquivar_recomendacoes import buscar_resultado_perna
from avaliacao import avaliar_resultado

DATABASE_URL = os.environ["DATABASE_URL"]
VERSAO = "simular_b7 29/10/2026 v1"
LIGA = "brasileirao"
ODD_MINIMA_CAPTURA = 1.01      # as mesmas do motor_combinacoes
ODD_MAXIMA_CAPTURA = 1000.0
HORA_ARQUIVAMENTO_UTC = 12     # cron `corinthians-analise` (0 12 * * * UTC)
MAX_PERNAS_NOVA = 2
COLUNAS_RECOMENDACAO = 14      # as de buscar_recomendacoes


# ---------------------------------------------------------------------------
# a regra nova, aplicada só durante a chamada
# ---------------------------------------------------------------------------

def _combinations_ate(maximo):
    def _comb(pool, tamanho):
        if tamanho > maximo:
            return iter(())
        return itertools.combinations(pool, tamanho)
    return _comb


@contextlib.contextmanager
def regra_nova():
    largura, comb = combinacoes.LARGURA_MINIMA_FAIXA, combinacoes.combinations
    combinacoes.LARGURA_MINIMA_FAIXA = float("inf")
    combinacoes.combinations = _combinations_ate(MAX_PERNAS_NOVA)
    try:
        yield
    finally:
        combinacoes.LARGURA_MINIMA_FAIXA, combinacoes.combinations = largura, comb


def montar(recs, nova):
    if nova:
        with regra_nova():
            saida = combinacoes.montar_combinacoes(recs, ODD_MINIMA_CAPTURA, ODD_MAXIMA_CAPTURA)
    else:
        saida = combinacoes.montar_combinacoes(recs, ODD_MINIMA_CAPTURA, ODD_MAXIMA_CAPTURA)
    return [c for c in saida if len(c["pernas"]) > 1]


# ---------------------------------------------------------------------------
# leitura
# ---------------------------------------------------------------------------

def conectar_leitura():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    conn.commit()  # só vale da PRÓXIMA transação (1_ESSENCIAL §6.2)
    cur.execute("SHOW transaction_read_only")
    if cur.fetchone()[0] != "on":
        conn.close()
        raise SystemExit("🔴 a sessão NÃO ficou só-leitura — parando sem ler nada.")
    return conn, cur


def utc_ingenuo(dt):
    """timestamptz -> timestamp UTC sem fuso (as colunas de jogo são assim, §5.7)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def janela_utc(desde, ate):
    """Datas BRT -> intervalo UTC sem fuso [início, fim)."""
    inicio = datetime.combine(desde, time(0)) + timedelta(hours=3)
    fim = datetime.combine(ate + timedelta(days=1), time(0)) + timedelta(hours=3)
    return inicio, fim


def ler_lotes(cur, casa, ate_utc):
    cur.execute("""
        SELECT MAX(registrado_em) FROM registro_publico
        WHERE liga = %s AND casa_aposta = %s
        GROUP BY date_trunc('minute', registrado_em) ORDER BY 1
    """, (LIGA, casa))
    return [t for t in (utc_ingenuo(r[0]) for r in cur.fetchall()) if t < ate_utc]


def ler_eventos(cur, casa):
    cur.execute("""
        SELECT rp.id, rp.registrado_em, rp.evento, rp.jogo_id, rp.fixture_id_api, rp.tipo_padrao,
               rp.linha, rp.direcao, rp.jogador_id, rp.descricao, rp.odd, rp.probabilidade
        FROM registro_publico rp
        WHERE rp.liga = %s AND rp.casa_aposta = %s
        ORDER BY rp.registrado_em, rp.id
    """, (LIGA, casa))
    return cur.fetchall()


def ler_jogos(cur, jogo_ids):
    if not jogo_ids:
        return {}
    cur.execute("""
        SELECT j.id, j.adversario, j.data_jogo, t.nome, j.datahora_jogo, j.fixture_id_api
        FROM jogos j JOIN times t ON t.id = j.nosso_time_id
        WHERE j.id = ANY(%s)
    """, (sorted(jogo_ids),))
    return {r[0]: r[1:] for r in cur.fetchall()}


def ler_rodadas(cur, fixtures):
    if not fixtures:
        return {}
    cur.execute("SELECT fixture_id_api, rodada_numero FROM jogos_liga WHERE fixture_id_api = ANY(%s)",
                (sorted(fixtures),))
    return dict(cur.fetchall())


def ler_reais(cur, casa, inicio, fim):
    cur.execute("""
        SELECT assinatura, probabilidade_combinada, resultado FROM multiplas_candidatas
        WHERE casa_aposta = %s AND primeiro_apito >= %s AND primeiro_apito < %s
    """, (casa, inicio, fim))
    return {a: (float(p), r) for a, p, r in cur.fetchall()}


# ---------------------------------------------------------------------------
# reconstrução e captura
# ---------------------------------------------------------------------------

def corte_arquivamento(t):
    """Até quando o cron das 12:00 UTC já tinha arquivado no momento t."""
    hoje = datetime.combine(t.date(), time(HORA_ARQUIVAMENTO_UTC))
    return hoje if t >= hoje else hoje - timedelta(days=1)


def reconstruir(eventos, jogos, t):
    ultimo = {}
    for (_id, reg, evento, jogo_id, fixture, tipo, linha, direcao, jogador_id, descricao,
         odd, prob) in eventos:
        if utc_ingenuo(reg) > t:
            break
        chave = (jogo_id, tipo, linha, (direcao or ""), jogador_id)
        ultimo[chave] = (evento, jogo_id, fixture, tipo, linha, direcao, jogador_id, descricao, odd, prob)
    corte = corte_arquivamento(t)
    linhas = []
    for (evento, jogo_id, fixture, tipo, linha, direcao, jogador_id, descricao, odd, prob) in ultimo.values():
        if evento != "ativa" or jogo_id not in jogos:
            continue
        adversario, data_jogo, nosso_time, datahora, fixture_jogos = jogos[jogo_id]
        if datahora is None or datahora < corte:
            continue
        linhas.append((jogo_id, jogador_id, descricao, CASA, odd, prob, adversario, data_jogo,
                       tipo, linha, direcao, nosso_time, datahora, fixture_jogos or fixture))
    return combinacoes.deduplicar_recomendacoes(linhas, colunas_a_manter=COLUNAS_RECOMENDACAO)


def capturar(estado, multiplas, t):
    """Mesmo efeito de capturar_candidatas_multiplas + congelar, num dict."""
    for c in multiplas:
        horarios = [j["datahora_jogo"] for j in c["jogos"] if j.get("datahora_jogo")]
        if not horarios:
            continue
        assinatura = combinacoes._assinatura_combo(c["casa_aposta"], c["pernas"])
        atual = estado.get(assinatura)
        if atual and atual["congelada"]:
            continue
        estado[assinatura] = {"item": c, "primeiro_apito": min(horarios), "congelada": False,
                              "visto_em": t, "visto_primeiro": atual["visto_primeiro"] if atual else t}
    for m in estado.values():
        if not m["congelada"] and m["primeiro_apito"] <= t:
            m["congelada"] = True


# ---------------------------------------------------------------------------
# avaliação
# ---------------------------------------------------------------------------

class Avaliador:
    def __init__(self, cur):
        self.cur = cur
        self.cache = {}
        self.sombra = 0

    def perna(self, p):
        chave = (p["jogo_id"], p.get("fixture_id_api"), p["tipo_padrao"], p.get("jogador_id"),
                 p.get("linha"), p.get("direcao"))
        if chave not in self.cache:
            v = buscar_resultado_perna(self.cur, p["tipo_padrao"], p["jogo_id"], p.get("fixture_id_api"),
                                       p.get("jogador_id"), p.get("linha"), p.get("direcao"))
            if v not in ("acertou", "errou"):
                v = avaliar_resultado(self.cur, p["tipo_padrao"], p.get("jogador_id"), p["jogo_id"],
                                      p.get("linha"), p.get("descricao"), p.get("direcao"))
                if v in ("acertou", "errou"):
                    self.sombra += 1
            self.cache[chave] = v
        return self.cache[chave]

    def multipla(self, item):
        vs = [self.perna(p) for p in item["pernas"]]
        if any(v == "errou" for v in vs):
            return "errou"
        if all(v == "acertou" for v in vs):
            return "acertou"
        return "pendente"


def resumo(itens):
    """itens: lista de (item, resultado). Devolve dict com os números."""
    av = [(i, r) for i, r in itens if r in ("acertou", "errou")]
    n_av = len(av)
    ac = sum(1 for _, r in av if r == "acertou")
    retorno = sum(i["odd_combinada"] for i, r in av if r == "acertou")
    return {
        "n": len(itens), "aval": n_av, "pend": len(itens) - n_av, "ac": ac,
        "prev": (sum(i["probabilidade_combinada"] for i, _ in av) / n_av) if n_av else None,
        "obs": (100.0 * ac / n_av) if n_av else None,
        "roi": (100.0 * (retorno - n_av) / n_av) if n_av else None,
    }


def fmt(r):
    if not r["aval"]:
        return f"N {r['n']:>5} · avaliadas 0 · pendentes {r['pend']}"
    return (f"N {r['n']:>5} · avaliadas {r['aval']:>5} · acertos {r['ac']:>5} · "
            f"previsto {r['prev']:5.1f} · observado {r['obs']:5.1f} · ROI {r['roi']:+6.1f}%"
            + (f" · pendentes {r['pend']}" if r["pend"] else ""))


def rodada_da(item, rodadas):
    primeiro = min(item["jogos"], key=lambda j: j["datahora_jogo"] or datetime.max)
    return rodadas.get(primeiro.get("fixture_id_api"))


# ---------------------------------------------------------------------------

CASA = None


def main(argv):
    global CASA
    ap = argparse.ArgumentParser(prog="simular_b7.py")
    ap.add_argument("fase", choices=["verificar"])
    ap.add_argument("--desde", type=date.fromisoformat, default=date(2026, 10, 16))
    ap.add_argument("--ate", type=date.fromisoformat, default=date(2026, 10, 31))
    ap.add_argument("--casa", default="superbet.rs")
    ap.add_argument("--limiar", type=float, default=0.95)
    ap.add_argument("--mesmo-assim", action="store_true")
    a = ap.parse_args(argv[1:])
    CASA = a.casa
    print(f"=== {VERSAO} · fase {a.fase.upper()} · argv {argv[1:]} · {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    print(f"janela (1º apito, BRT): {a.desde:%d/%m} a {a.ate:%d/%m} · casa {CASA} · limiar {a.limiar:.0%}")
    sys.stdout.flush()

    conn, cur = conectar_leitura()
    try:
        inicio, fim = janela_utc(a.desde, a.ate)
        lotes = ler_lotes(cur, CASA, fim)
        eventos = ler_eventos(cur, CASA)
        jogos = ler_jogos(cur, {e[3] for e in eventos})
        print(f"\n{len(eventos)} evento(s) no registro público · {len(lotes)} lote(s) de cron até o fim da janela")
        if not lotes:
            print("nada a simular.")
            return 0

        estados = {"antiga": {}, "nova": {}}
        for t in lotes:
            recs = reconstruir(eventos, jogos, t)
            if not recs:
                continue
            t0 = datetime.now()
            antigas = montar(recs, nova=False)
            t1 = datetime.now()
            novas = montar(recs, nova=True)
            t2 = datetime.now()
            capturar(estados["antiga"], antigas, t)
            capturar(estados["nova"], novas, t)
            print(f"  lote {t:%d/%m %H:%M} UTC · {len(recs)} recomendação(ões) · "
                  f"antiga {len(antigas)} ({(t1 - t0).seconds}s) · nova {len(novas)} ({(t2 - t1).seconds}s)")
            sys.stdout.flush()

        # Congelamento final: o cron roda todo dia, mas só aparece como lote
        # quando muda alguma recomendação. Toda múltipla cujo primeiro apito
        # já passou estaria congelada no cron seguinte.
        agora = datetime.now(timezone.utc).replace(tzinfo=None)
        for est in estados.values():
            for m in est.values():
                if not m["congelada"] and m["primeiro_apito"] <= agora:
                    m["congelada"] = True

        def da_janela(est):
            return {s: m for s, m in est.items()
                    if m["congelada"] and inicio <= m["primeiro_apito"] < fim}

        sim_antiga, sim_nova = da_janela(estados["antiga"]), da_janela(estados["nova"])

        # --- 4. controle: a antiga reproduz as reais?
        reais = ler_reais(cur, CASA, inicio, fim)
        comuns = set(reais) & set(sim_antiga)
        cobre_reais = len(comuns) / len(reais) if reais else 0.0
        cobre_sim = len(comuns) / len(sim_antiga) if sim_antiga else 0.0
        difs = [abs(reais[s][0] - sim_antiga[s]["item"]["probabilidade_combinada"]) for s in comuns]
        print("\n== CONTROLE: a reconstrução antiga reproduz as multiplas_candidatas reais? ==")
        print(f"  reais na janela {len(reais)} · simuladas (antiga) {len(sim_antiga)} · em comum {len(comuns)}")
        print(f"  das reais, reproduzidas: {cobre_reais:.1%} · das simuladas, existem de verdade: {cobre_sim:.1%}")
        if difs:
            print(f"  probabilidade combinada nas comuns: diferença máxima {max(difs):.2f} ponto(s); "
                  f"iguais {sum(1 for d in difs if d < 0.005)} de {len(difs)}")
        reproduz = cobre_reais >= a.limiar and cobre_sim >= a.limiar
        if not reproduz:
            print(f"\n🔴 A RECONSTRUÇÃO NÃO REPRODUZ A PRODUÇÃO (limiar {a.limiar:.0%}). "
                  "Pela arquitetura §5, a simulação não vale.")
            if not a.mesmo_assim:
                print("Parando antes de avaliar. (--mesmo-assim segue só para diagnóstico.)")
                return 2
            print("⚠️ --mesmo-assim: os números abaixo são DIAGNÓSTICO, não valem para decidir o B7.")
        else:
            print("✅ reprodução dentro do limiar.")

        # --- 5. avaliação
        av = Avaliador(cur)
        res = {}
        for nome, est in (("antiga", sim_antiga), ("nova", sim_nova)):
            res[nome] = {s: av.multipla(m["item"]) for s, m in est.items()}
        fixtures = {j.get("fixture_id_api") for est in (sim_antiga, sim_nova)
                    for m in est.values() for j in m["item"]["jogos"]}
        rodadas = ler_rodadas(cur, fixtures)

        print("\n== RESULTADO (a mesma avaliação para as duas regras) ==")
        for nome, est in (("antiga", sim_antiga), ("nova", sim_nova)):
            print(f"  {nome:<6} " + fmt(resumo([(m["item"], res[nome][s]) for s, m in est.items()])))
        print(f"  pernas avaliadas fora do histórico (sombra, avaliar_resultado): {av.sombra}")

        print("\n== POR RODADA (rodada do primeiro jogo da múltipla) ==")
        por_rodada = {}
        todas_rodadas = sorted({rodada_da(m["item"], rodadas) for est in (sim_antiga, sim_nova)
                                for m in est.values()}, key=lambda x: (x is None, x))
        for rod in todas_rodadas:
            linha = {}
            for nome, est in (("antiga", sim_antiga), ("nova", sim_nova)):
                linha[nome] = resumo([(m["item"], res[nome][s]) for s, m in est.items()
                                      if rodada_da(m["item"], rodadas) == rod])
                print(f"  rodada {rod} · {nome:<6} " + fmt(linha[nome]))
            por_rodada[rod] = linha

        print("\n== O QUE FICA, SAI E ENTRA ==")
        fica = set(sim_antiga) & set(sim_nova)
        sai = set(sim_antiga) - set(sim_nova)
        entra = set(sim_nova) - set(sim_antiga)
        print("  fica  (nas duas) " + fmt(resumo([(sim_antiga[s]["item"], res["antiga"][s]) for s in fica])))
        print("  sai   (só antiga) " + fmt(resumo([(sim_antiga[s]["item"], res["antiga"][s]) for s in sai])))
        print("  entra (só nova)  " + fmt(resumo([(sim_nova[s]["item"], res["nova"][s]) for s in entra])))
        for nome, est in (("antiga", sim_antiga), ("nova", sim_nova)):
            pernas = defaultdict(list)
            faixa = []
            for s, m in est.items():
                pernas[len(m["item"]["pernas"])].append((m["item"], res[nome][s]))
                if m["item"]["descricao"].endswith("(faixa)"):
                    faixa.append((m["item"], res[nome][s]))
            for k in sorted(pernas):
                print(f"  {nome:<6} {k} pernas · " + fmt(resumo(pernas[k])))
            if faixa:
                print(f"  {nome:<6} com faixa · " + fmt(resumo(faixa)))

        print("\n== M2 (C4) DENTRO DA SIMULAÇÃO: saiu do top 300 antes do apito? ==")
        for nome, est in (("antiga", sim_antiga), ("nova", sim_nova)):
            ficou, saiu = [], []
            for s, m in est.items():
                anteriores = [t for t in lotes if t < m["primeiro_apito"]]
                ultimo_lote = anteriores[-1] if anteriores else None
                (ficou if ultimo_lote is None or m["visto_em"] >= ultimo_lote else saiu).append(
                    (m["item"], res[nome][s]))
            print(f"  {nome:<6} no top 300 no último lote " + fmt(resumo(ficou)))
            print(f"  {nome:<6} já tinha saído           " + fmt(resumo(saiu)))

        # --- critério
        print("\n== CRITÉRIO (arquitetura §5, escrito antes) ==")
        tot_a = resumo([(m["item"], res["antiga"][s]) for s, m in sim_antiga.items()])
        tot_n = resumo([(m["item"], res["nova"][s]) for s, m in sim_nova.items()])
        rods = [r for r, l in por_rodada.items() if l["antiga"]["aval"] and l["nova"]["aval"]]
        ganhas = [r for r in rods if por_rodada[r]["nova"]["roi"] > por_rodada[r]["antiga"]["roi"]]
        print("  rodada | ROI antiga | ROI nova | nova melhor?")
        for r in rods:
            ra, rn = por_rodada[r]["antiga"]["roi"], por_rodada[r]["nova"]["roi"]
            print(f"  {str(r):>6} | {ra:+9.1f}% | {rn:+7.1f}% | {'sim' if rn > ra else 'não'}")
        precisa = 2 if len(rods) >= 3 else (len(rods) // 2 + 1)
        if tot_a["aval"] and tot_n["aval"]:
            ok_total = tot_n["roi"] > tot_a["roi"]
            ok_rod = len(ganhas) >= precisa
            print(f"  total: antiga {tot_a['roi']:+.1f}% × nova {tot_n['roi']:+.1f}% → "
                  f"{'nova melhor' if ok_total else 'nova NÃO é melhor'}")
            print(f"  rodadas em que a nova é melhor: {len(ganhas)} de {len(rods)} (precisa {precisa})")
            if not reproduz:
                print("  ⚠️ sem reprodução, o veredito abaixo não vale.")
            print("  " + ("🟢 APROVADO — segue para a implementação."
                          if ok_total and ok_rod else "🔴 REPROVADO — o B7 para aqui."))
            if any(l["antiga"]["pend"] or l["nova"]["pend"] for l in por_rodada.values()):
                print("  ⚠️ há múltiplas pendentes (jogo sem dado): rodar de novo quando a rodada fechar.")
        else:
            print("  sem múltiplas avaliadas nas duas regras — nada a decidir.")
        return 0
    finally:
        conn.rollback()  # incondicional: este script nunca grava
        cur.close()
        conn.close()
        print("\nSÓ LEITURA — nenhuma tabela foi tocada (sessão READ ONLY, rollback).")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
