"""
marcar_multiplas_vigentes.py — o acervo das múltiplas vigentes
(`claude/arquitetura_multiplas_vigentes.md §3.3`). Duas fases:

    python marcar_multiplas_vigentes.py verificar [--desde AAAA-MM-DD] [--ate AAAA-MM-DD]
                                                  [--casa superbet.rs] [--limiar 0.95]
                                                  [--controle-desde 2026-10-16]
                                                  [--controle-ate 2026-10-31] [--mesmo-assim]
    python marcar_multiplas_vigentes.py aplicar   [mesmas opções, menos --mesmo-assim]

`--desde`/`--ate`: datas (BRT) do PRIMEIRO apito das múltiplas a marcar.
Padrão: 14/10/2026 (primeiro dia do registro público da superbet.rs) até hoje.

O QUE FAZ
  1. Reconstrói o top 300 de cada execução do cron pelo `registro_publico`,
     IMPORTANDO do `simular_b7.py` (`ler_lotes`, `ler_eventos`, `ler_jogos`,
     `reconstruir`, `montar` com a regra antiga, `capturar`). É a reconstrução
     que reproduziu as `multiplas_candidatas` reais em 03/11.
  2. Para cada múltipla CONGELADA da casa, com `vigente_no_apito` NULL e
     `capturada_cron_em` NULL (o que o código novo já marcou não se toca):
       vigente = a assinatura estava no top 300 do ÚLTIMO lote antes do
       primeiro apito. É a regra do código novo (o cron de depois do apito,
       que ainda recaptura o jogo começado, não conta).
     Múltipla real que a reconstrução não montou em lote nenhum, ou sem lote
     antes do apito: fica NULL (desconhecido).
  3. CONTROLE (para antes de gravar): a reconstrução tem de reproduzir as
     reais da janela por assinatura, nos dois sentidos, acima de `--limiar`.
  4. V4 (arquitetura §5): na janela de controle (padrão 16-31/10) imprime
     também a conta do simulador do B7 (M2 "dentro da simulação", que conta
     como vigente quem foi recapturada depois do apito). Ela tem de dar os
     2.440 / 1.276 do log de 03/11. O número que é GRAVADO é o da regra
     estrita; a diferença entre as duas é impressa.

`verificar`: sessão READ ONLY conferida, rollback no fim. Não grava nada.
`aplicar`: grava SÓ `multiplas_candidatas.vigente_no_apito` (de NULL para
TRUE/FALSE), numa transação, com as provas antes do commit:
  - exatamente as linhas planejadas mudaram;
  - todas as outras colunas de `multiplas_candidatas` com md5 igual;
  - `recomendacoes`, `historico_recomendacoes`, `registro_publico` e
    `registro_modelo` com contagem e md5 iguais (V6).
Superbet BR (antes de 14/10) não tem registro público: fica NULL.
Só o Brasileirão (`public`); as ligas ficam para uma fase 2, como o C4.

Variáveis de ambiente: DATABASE_URL.
"""

import argparse
import sys
from bisect import bisect_left
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import psycopg2

import combinacoes
import simular_b7 as sb

DATABASE_URL = sb.DATABASE_URL
VERSAO = "marcar_multiplas_vigentes 03/11/2026 v1"

VIGIADAS = {
    "recomendacoes": "t.*",
    "historico_recomendacoes": "t.*",
    "registro_publico": "t.*",
    "registro_modelo": "t.*",
    "multiplas_candidatas": ("t.id, t.assinatura, t.casa_aposta, t.descricao, t.odd_combinada, "
                             "t.probabilidade_combinada, t.pernas, t.jogos, t.primeiro_apito, t.congelada, "
                             "t.avaliada, t.criada_em, t.atualizada_em, t.resultado, t.capturada_cron_em"),
}


def foto(cur):
    out = {}
    for t, cols in VIGIADAS.items():
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{t}",))
        if not cur.fetchone()[0]:
            continue
        cur.execute(f"SELECT COUNT(*), md5(COALESCE(string_agg(md5(ROW({cols})::text), '' ORDER BY t.id), '')) "
                    f"FROM public.{t} t")
        out[t] = tuple(cur.fetchone())
    return out


def ler_alvo(cur, casa):
    cur.execute("""
        SELECT id, assinatura, primeiro_apito, resultado, odd_combinada, probabilidade_combinada,
               congelada, vigente_no_apito, capturada_cron_em
        FROM multiplas_candidatas WHERE casa_aposta = %s
    """, (casa,))
    return cur.fetchall()


def resumo(linhas):
    """linhas: (resultado, odd). ROI só com acertou/errou (retirada fica fora)."""
    av = [(r, float(o)) for r, o in linhas if r in ("acertou", "errou")]
    n = len(av)
    if not n:
        return f"N {len(linhas):>5} · avaliadas 0"
    ac = sum(1 for r, _ in av if r == "acertou")
    ret = sum(o for r, o in av if r == "acertou")
    return (f"N {len(linhas):>5} · avaliadas {n:>5} · acertos {ac:>5} · "
            f"observado {100.0 * ac / n:5.1f} · ROI {100.0 * (ret - n) / n:+6.1f}%")


def main(argv):
    ap = argparse.ArgumentParser(prog="marcar_multiplas_vigentes.py")
    ap.add_argument("fase", choices=["verificar", "aplicar"])
    ap.add_argument("--desde", type=date.fromisoformat, default=date(2026, 10, 14))
    ap.add_argument("--ate", type=date.fromisoformat, default=None)
    ap.add_argument("--casa", default="superbet.rs")
    ap.add_argument("--limiar", type=float, default=0.95)
    ap.add_argument("--controle-desde", type=date.fromisoformat, default=date(2026, 10, 16))
    ap.add_argument("--controle-ate", type=date.fromisoformat, default=date(2026, 10, 31))
    ap.add_argument("--mesmo-assim", action="store_true")
    a = ap.parse_args(argv[1:])
    if a.fase == "aplicar" and a.mesmo_assim:
        print("--mesmo-assim só vale no verificar.")
        return 2
    ate = a.ate or (datetime.now(timezone.utc) - timedelta(hours=3)).date()
    sb.CASA = a.casa
    print(f"=== {VERSAO} · fase {a.fase.upper()} · argv {argv[1:]} · {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    print(f"janela (1º apito, BRT): {a.desde:%d/%m/%Y} a {ate:%d/%m/%Y} · casa {a.casa} · limiar {a.limiar:.0%}")
    sys.stdout.flush()

    if a.fase == "verificar":
        conn, cur = sb.conectar_leitura()
    else:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        cur = conn.cursor()
    try:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
                    "AND table_name = 'multiplas_candidatas' AND column_name IN ('vigente_no_apito', 'capturada_cron_em')")
        if len(cur.fetchall()) != 2:
            print("🔴 as colunas novas não existem — rode antes `criar_vigencia_multiplas.py aplicar`.")
            return 2

        inicio, fim = sb.janela_utc(a.desde, ate)
        lotes = sb.ler_lotes(cur, a.casa, fim)
        eventos = sb.ler_eventos(cur, a.casa)
        jogos = sb.ler_jogos(cur, {e[3] for e in eventos})
        print(f"\n{len(eventos)} evento(s) no registro público · {len(lotes)} lote(s) até o fim da janela")
        if not lotes:
            print("nada a reconstruir.")
            return 0

        # top 300 de cada lote (assinaturas) + o estado do simulador (para o V4)
        top = []
        estado = {}
        for t in lotes:
            recs = sb.reconstruir(eventos, jogos, t)
            antigas = sb.montar(recs, nova=False) if recs else []
            assin = set()
            for c in antigas:
                if any(j.get("datahora_jogo") for j in c["jogos"]):
                    assin.add(combinacoes._assinatura_combo(c["casa_aposta"], c["pernas"]))
            top.append(assin)
            sb.capturar(estado, antigas, t)
            print(f"  lote {t:%d/%m %H:%M} UTC · {len(recs)} recomendação(ões) · top {len(assin)}")
            sys.stdout.flush()
        agora = datetime.now(timezone.utc).replace(tzinfo=None)
        for m in estado.values():
            if not m["congelada"] and m["primeiro_apito"] <= agora:
                m["congelada"] = True

        def ultimo_lote_antes(apito):
            i = bisect_left(lotes, apito)  # lotes[i-1] < apito
            return i - 1 if i > 0 else None

        reais = ler_alvo(cur, a.casa)
        na_janela = [r for r in reais if r[6] and inicio <= r[2] < fim]

        # --- controle de reprodução (todas as congeladas da janela)
        sim = {s for s, m in estado.items() if m["congelada"] and inicio <= m["primeiro_apito"] < fim}
        assin_reais = {r[1] for r in na_janela}
        comuns = sim & assin_reais
        cobre_reais = len(comuns) / len(assin_reais) if assin_reais else 0.0
        cobre_sim = len(comuns) / len(sim) if sim else 0.0
        print("\n== CONTROLE: a reconstrução reproduz as multiplas_candidatas reais? ==")
        print(f"  reais congeladas na janela {len(assin_reais)} · reconstruídas {len(sim)} · em comum {len(comuns)}")
        print(f"  das reais, reproduzidas {cobre_reais:.1%} · das reconstruídas, existem {cobre_sim:.1%}")
        reproduz = cobre_reais >= a.limiar and cobre_sim >= a.limiar
        if not reproduz:
            print(f"🔴 abaixo do limiar ({a.limiar:.0%}): a marcação não vale.")
            if not a.mesmo_assim:
                return 2
            print("⚠️ --mesmo-assim: números de DIAGNÓSTICO.")
        else:
            print("✅ reprodução dentro do limiar.")

        # --- plano
        plano = {}          # id -> True/False
        motivo_null = defaultdict(int)
        ja_marcadas = 0
        linhas_por = defaultdict(list)   # (rodada?, classe) -> (resultado, odd); por data BRT do apito
        for (mid, assin, apito, res, odd, _p, _cong, vig, cap) in na_janela:
            if vig is not None or cap is not None:
                ja_marcadas += 1
                continue
            i = ultimo_lote_antes(apito)
            if assin not in estado:
                motivo_null["não reconstruída"] += 1
                classe = "NULL"
            elif i is None:
                motivo_null["sem lote antes do apito"] += 1
                classe = "NULL"
            else:
                plano[mid] = assin in top[i]
                classe = "vigente" if plano[mid] else "não vigente"
            semana = (apito - timedelta(hours=3)).date()
            linhas_por[classe].append((res, odd))
            linhas_por[(semana.isocalendar()[1], classe)].append((res, odd))

        print("\n== O QUE SERÁ GRAVADO (regra estrita: top 300 do último lote antes do apito) ==")
        for classe in ("vigente", "não vigente", "NULL"):
            print(f"  {classe:<12} " + resumo(linhas_por[classe]))
        if motivo_null:
            print("  ficam NULL: " + " · ".join(f"{k} {v}" for k, v in sorted(motivo_null.items())))
        if ja_marcadas:
            print(f"  já marcadas pelo código novo (não se tocam): {ja_marcadas}")
        print("\n  por semana ISO do apito (BRT):")
        for sem in sorted({k[0] for k in linhas_por if isinstance(k, tuple)}):
            partes = [f"{c}: " + resumo(linhas_por[(sem, c)]) for c in ("vigente", "não vigente")
                      if linhas_por[(sem, c)]]
            print(f"   semana {sem} · " + " | ".join(partes))

        # --- V4: a conta do simulador na janela de controle
        ci, cf = sb.janela_utc(a.controle_desde, a.controle_ate)
        gen_ficou = gen_saiu = est_v = est_nv = 0
        for s, m in estado.items():
            if not (m["congelada"] and ci <= m["primeiro_apito"] < cf):
                continue
            i = ultimo_lote_antes(m["primeiro_apito"])
            if i is None or m["visto_em"] >= lotes[i]:
                gen_ficou += 1
            else:
                gen_saiu += 1
            if i is not None and s in top[i]:
                est_v += 1
            else:
                est_nv += 1
        print(f"\n== V4: janela de controle {a.controle_desde:%d/%m}-{a.controle_ate:%d/%m} (todas as reconstruídas) ==")
        print(f"  conta do simulador do B7 (M2): no top 300 {gen_ficou} · já tinha saído {gen_saiu}"
              "   ← tem de ser 2.440 / 1.276 (log de 03/11)")
        print(f"  regra estrita (a gravada):     vigentes {est_v} · não vigentes {est_nv}"
              f"   (diferença: {gen_ficou - est_v} recapturada(s) só depois do apito)")

        if a.fase == "verificar":
            print("\nVERIFICAR — nada foi gravado.")
            return 0

        # --- aplicar
        antes = foto(cur)
        verdadeiros = sorted(k for k, v in plano.items() if v)
        falsos = sorted(k for k, v in plano.items() if not v)
        mudou = 0
        for valor, ids in ((True, verdadeiros), (False, falsos)):
            for k in range(0, len(ids), 1000):
                cur.execute("UPDATE multiplas_candidatas SET vigente_no_apito = %s "
                            "WHERE id = ANY(%s) AND vigente_no_apito IS NULL AND capturada_cron_em IS NULL "
                            "AND congelada AND casa_aposta = %s",
                            (valor, ids[k:k + 1000], a.casa))
                mudou += cur.rowcount
        depois = foto(cur)
        problemas = []
        if mudou != len(plano):
            problemas.append(f"linhas alteradas {mudou} ≠ planejadas {len(plano)}")
        for t in sorted(set(antes) | set(depois)):
            if antes.get(t) != depois.get(t):
                problemas.append(f"`{t}` mudou ({antes.get(t)} → {depois.get(t)})")
        cur.execute("SELECT vigente_no_apito, COUNT(*) FROM multiplas_candidatas WHERE id = ANY(%s) GROUP BY 1",
                    (sorted(plano),))
        conferido = dict(cur.fetchall())
        if conferido.get(True, 0) != len(verdadeiros) or conferido.get(False, 0) != len(falsos):
            problemas.append(f"conferência pelo banco: {conferido} × planejado "
                             f"{{True: {len(verdadeiros)}, False: {len(falsos)}}}")
        if problemas:
            conn.rollback()
            print("\n🔴 PROVAS FALHARAM — rollback, nada foi gravado:\n   - " + "\n   - ".join(problemas))
            return 1
        conn.commit()
        print(f"\n✅ gravado: {len(verdadeiros)} vigente(s) · {len(falsos)} não vigente(s) · "
              f"só `vigente_no_apito`; as outras colunas e as tabelas vigiadas iguais (md5).")
        return 0
    finally:
        conn.rollback()
        cur.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
