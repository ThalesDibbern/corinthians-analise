"""
varrer_multiplas_presas.py — tira do limbo as múltiplas presas por perna
RETIRADA antes do apito (C4).

Arquitetura: `claude/arquitetura_varredor_multiplas.md` (27/10/2026).

--------------------------------------------------------------------------
O PROBLEMA
--------------------------------------------------------------------------

`arquivar_recomendacoes.avaliar_e_selecionar_top5` só avalia uma múltipla
congelada quando TODAS as pernas têm veredito em `historico_recomendacoes`.
Faltando uma, faz `continue` e não marca nada: a múltipla fica
`congelada = TRUE, avaliada = FALSE` para sempre (1_ESSENCIAL §5.18).

A causa dominante é a perna que saiu de `recomendacoes` ANTES do apito (o
motor recalculou e ela deixou de passar no filtro). Ela nunca é arquivada.
Uma múltipla assim não estava mais sendo recomendada no apito - por isso ela
sai como `resultado = 'retirada'`, FORA do ROI, e o acerto/erro dela vai
para a tabela à parte `multiplas_retiradas` (`resultado_sombra`).

--------------------------------------------------------------------------
CLASSES DE PERNA SEM VEREDITO
--------------------------------------------------------------------------

O veredito vem da função de PRODUÇÃO `buscar_resultado_perna` (importada).
Quando ela devolve None ou 'pendente', a perna é classificada:

  AINDA_SEM_DADO     a recomendação ainda está viva em `recomendacoes`, OU o
                     jogo não tem placar, OU o apito foi há menos de
                     FOLGA_HORAS, OU o histórico diz 'pendente'. Não toca:
                     o arquivamento normal ainda pode resolver.
  CASAMENTO_FALHOU   o histórico tem a MESMA aposta (mesmo jogo, mercado e
                     linha) com outra direção ou outro jogador - consequência
                     das contraditórias (5.9). Não toca.
  RETIRADA_PROVADA   o último evento dessa aposta no `registro_publico` é
                     'retirada' (o registro começa em 14/10).
  SUMIU_SEM_PROVA    não está em `recomendacoes` nem no histórico, o jogo tem
                     placar há mais de FOLGA_HORAS, e o `registro_publico`
                     não tem evento dela (era da Superbet BR).

Identidade do "mesmo jogo": jogo inteiro e jogador por `fixture_id_api`;
TIME (inclusive cartão sem jogador) por `jogo_id`, porque a outra
perspectiva é OUTRA aposta (§5.3, §5.20). Por isso este script NÃO usa o
casamento frouxo do `diagnostico_multiplas_presas` (que olha o jogo pelas
duas chaves e sem a linha, §5.18).

A múltipla só é marcada quando TODAS as pernas sem veredito são
RETIRADA_PROVADA ou SUMIU_SEM_PROVA. Qualquer AINDA_SEM_DADO ou
CASAMENTO_FALHOU → a múltipla fica como está (e é contada).

--------------------------------------------------------------------------
O QUE GRAVA (só na fase `aplicar`)
--------------------------------------------------------------------------

  - cria (se faltar) `public.multiplas_retiradas` + GRANT SELECT ao
    claude_leitura (se o role existir);
  - `multiplas_candidatas`: só `avaliada = TRUE, resultado = 'retirada'`,
    só em linha `congelada AND NOT avaliada`;
  - `multiplas_retiradas`: uma linha por múltipla marcada.

Não toca `recomendacoes`, `historico_recomendacoes`, `registro_publico`,
`registro_modelo`. As provas A1-A4 da arquitetura rodam DENTRO da mesma
transação, e o commit só acontece se todas passarem.

--------------------------------------------------------------------------
COMO RODAR (serviço `abundant-abundance`)
--------------------------------------------------------------------------

    python varrer_multiplas_presas.py verificar
    python varrer_multiplas_presas.py verificar --limite 200
    python varrer_multiplas_presas.py aplicar

`verificar` abre a transação como READ ONLY (conferido) e termina em
rollback. `--limite N` olha só as N presas de apito mais recente (fatia).

Variáveis de ambiente: DATABASE_URL.
"""

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

import psycopg2

# A função de PRODUÇÃO, não uma cópia (1_ESSENCIAL §1: "se a função pode ser
# importada, importar").
from arquivar_recomendacoes import buscar_resultado_perna, e_aposta_de_jogador
from avaliacao import avaliar_resultado
from combinacoes import MERCADOS_JOGO_INTEIRO

DATABASE_URL = os.environ["DATABASE_URL"]

VERSAO = "varrer_multiplas_presas 27/10/2026 v1"
FOLGA_HORAS = 72          # mesma janela de eventos do popular_banco
LIGA = "brasileirao"      # registro_publico.liga deste schema (ligas: fase 2)
RESULTADO_RETIRADA = "retirada"

CLASSES_QUE_LIBERAM = ("RETIRADA_PROVADA", "SUMIU_SEM_PROVA")

SQL_CRIAR_TABELA = """
CREATE TABLE IF NOT EXISTS public.multiplas_retiradas (
    id                     bigserial PRIMARY KEY,
    candidata_id           integer     NOT NULL UNIQUE,
    casa_aposta            varchar(50),
    primeiro_apito         timestamp,
    odd_combinada          numeric,
    probabilidade_combinada numeric,
    classe                 text        NOT NULL,
    pernas_retiradas       jsonb       NOT NULL,
    prova_registro_publico bigint[],
    resultado_sombra       text        NOT NULL,
    varrido_em             timestamptz NOT NULL DEFAULT now(),
    versao_codigo          text        NOT NULL,
    CONSTRAINT multiplas_retiradas_classe_ck CHECK (classe IN ('RETIRADA_PROVADA', 'SUMIU_SEM_PROVA')),
    CONSTRAINT multiplas_retiradas_sombra_ck CHECK (resultado_sombra IN ('acertou', 'errou', 'pendente'))
)"""


# ---------------------------------------------------------------------------
# identidade e consultas por perna
# ---------------------------------------------------------------------------

def e_por_fixture(perna):
    """Jogo inteiro e jogador se identificam pelo fixture; TIME pelo jogo_id."""
    return (perna.get("fixture_id_api") is not None
            and (perna["tipo_padrao"] in MERCADOS_JOGO_INTEIRO
                 or e_aposta_de_jogador(perna["tipo_padrao"], perna.get("jogador_id"))))


def chave_perna(casa, perna):
    jogo = ("fx", perna.get("fixture_id_api")) if e_por_fixture(perna) else ("jg", perna["jogo_id"])
    return (casa, jogo, perna["tipo_padrao"], perna.get("jogador_id"),
            None if perna.get("linha") is None else float(perna["linha"]),
            (perna.get("direcao") or "").strip().lower())


def filtro_jogo(alias_rec, perna):
    """Pedaço de WHERE + parâmetro para 'o mesmo jogo' desta perna."""
    if e_por_fixture(perna):
        return f"j.fixture_id_api = %s", perna["fixture_id_api"]
    return f"{alias_rec}.jogo_id = %s", perna["jogo_id"]


def ainda_viva(cur, perna):
    cond, p = filtro_jogo("r", perna)
    cur.execute(
        f"""SELECT COUNT(*) FROM recomendacoes r JOIN jogos j ON j.id = r.jogo_id
            WHERE {cond} AND r.tipo_padrao = %s
              AND r.jogador_id IS NOT DISTINCT FROM %s
              AND r.linha IS NOT DISTINCT FROM %s
              AND LOWER(TRIM(COALESCE(r.direcao, ''))) = LOWER(TRIM(%s))""",
        (p, perna["tipo_padrao"], perna.get("jogador_id"), perna.get("linha"), perna.get("direcao") or ""),
    )
    return cur.fetchone()[0] > 0


def jogo_encerrado_ha_folga(cur, perna):
    """O jogo da perna tem placar e o apito foi há mais de FOLGA_HORAS?"""
    cur.execute(
        """SELECT placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL,
                  COALESCE(datahora_jogo, data_jogo::timestamp) < (NOW() AT TIME ZONE 'UTC') - %s * interval '1 hour'
           FROM jogos WHERE id = %s""",
        (FOLGA_HORAS, perna["jogo_id"]),
    )
    row = cur.fetchone()
    return bool(row and row[0] and row[1])


def mesma_aposta_no_historico(cur, perna):
    """Mesmo jogo + mercado + linha, com outra direção ou outro jogador."""
    cond, p = filtro_jogo("hr", perna)
    cur.execute(
        f"""SELECT COUNT(*) FROM historico_recomendacoes hr JOIN jogos j ON j.id = hr.jogo_id
            WHERE {cond} AND hr.tipo_padrao = %s
              AND hr.linha IS NOT DISTINCT FROM %s""",
        (p, perna["tipo_padrao"], perna.get("linha")),
    )
    return cur.fetchone()[0] > 0


def eventos_no_registro(cur, casa, perna):
    """Eventos desta aposta no registro_publico, do mais novo ao mais velho:
    lista de (id, evento)."""
    cond, p = filtro_jogo("rp", perna)
    cond = cond.replace("j.fixture_id_api", "rp.fixture_id_api")
    cur.execute(
        f"""SELECT rp.id, rp.evento FROM registro_publico rp
            WHERE rp.liga = %s AND {cond} AND rp.casa_aposta = %s AND rp.tipo_padrao = %s
              AND rp.jogador_id IS NOT DISTINCT FROM %s
              AND rp.linha IS NOT DISTINCT FROM %s
              AND LOWER(TRIM(COALESCE(rp.direcao, ''))) = LOWER(TRIM(%s))
            ORDER BY rp.registrado_em DESC, rp.id DESC""",
        (LIGA, p, casa, perna["tipo_padrao"], perna.get("jogador_id"), perna.get("linha"),
         perna.get("direcao") or ""),
    )
    return cur.fetchall()


def classificar_perna(cur, casa, perna):
    """Devolve (classe, veredito, prova_ids). classe None = a perna tem
    veredito final no histórico."""
    veredito = buscar_resultado_perna(
        cur, perna["tipo_padrao"], perna["jogo_id"], perna.get("fixture_id_api"),
        perna.get("jogador_id"), perna.get("linha"), perna.get("direcao"),
    )
    if veredito in ("acertou", "errou"):
        return None, veredito, []
    if veredito is not None:                       # 'pendente' no histórico
        return "AINDA_SEM_DADO", None, []
    if ainda_viva(cur, perna) or not jogo_encerrado_ha_folga(cur, perna):
        return "AINDA_SEM_DADO", None, []
    if mesma_aposta_no_historico(cur, perna):
        return "CASAMENTO_FALHOU", None, []
    eventos = eventos_no_registro(cur, casa, perna)
    if eventos:
        if eventos[0][1] == "retirada":
            return "RETIRADA_PROVADA", None, [eventos[0][0]]
        # o último evento é 'ativa' e mesmo assim nada foi arquivado: não é
        # retirada provada, e não é "sem prova" - fica de fora e é contado.
        return "ATIVA_NO_REGISTRO", None, []
    return "SUMIU_SEM_PROVA", None, []


def veredito_sombra(cur, perna):
    """Acerto/erro da perna retirada, pela fonte única de veredito."""
    return avaliar_resultado(cur, perna["tipo_padrao"], perna.get("jogador_id"), perna["jogo_id"],
                             perna.get("linha"), perna.get("descricao"), perna.get("direcao"))


# ---------------------------------------------------------------------------
# varredura
# ---------------------------------------------------------------------------

def buscar_presas(cur, limite):
    sql = ("SELECT id, casa_aposta, pernas, primeiro_apito, odd_combinada, probabilidade_combinada "
           "FROM multiplas_candidatas WHERE congelada = TRUE AND avaliada = FALSE "
           "ORDER BY primeiro_apito DESC, id DESC")
    if limite:
        sql += f" LIMIT {int(limite)}"
    cur.execute(sql)
    return cur.fetchall()


def varrer(cur, limite=None):
    """Classifica todas as presas. Devolve (decisoes, contadores)."""
    cache = {}
    decisoes = []          # múltiplas a marcar
    por_multipla = Counter()
    por_perna = Counter()
    por_casa = defaultdict(Counter)

    for cid, casa, pernas, apito, odd, prob in buscar_presas(cur, limite):
        if isinstance(pernas, str):
            pernas = json.loads(pernas)
        classes, vereditos, provas, retiradas = [], [], [], []
        for perna in pernas:
            k = chave_perna(casa, perna)
            if k not in cache:
                cache[k] = classificar_perna(cur, casa, perna)
                if cache[k][0] is not None:
                    por_perna[cache[k][0]] += 1
            classe, veredito, prova = cache[k]
            if classe is None:
                vereditos.append(veredito)
                continue
            classes.append(classe)
            provas.extend(prova)
            retiradas.append(perna)

        if not classes:
            rotulo = "TODAS_COM_VEREDITO"          # o próximo arquivamento resolve
        elif all(c in CLASSES_QUE_LIBERAM for c in classes):
            rotulo = "SUMIU_SEM_PROVA" if "SUMIU_SEM_PROVA" in classes else "RETIRADA_PROVADA"
        else:
            rotulo = next(c for c in ("CASAMENTO_FALHOU", "ATIVA_NO_REGISTRO", "AINDA_SEM_DADO") if c in classes)
        por_multipla[rotulo] += 1
        por_casa[casa][rotulo] += 1

        if rotulo in CLASSES_QUE_LIBERAM:
            sombra = [veredito_sombra(cur, p) for p in retiradas] + vereditos
            if "errou" in sombra:
                res = "errou"
            elif all(s == "acertou" for s in sombra):
                res = "acertou"
            else:
                res = "pendente"
            decisoes.append({
                "id": cid, "casa": casa, "apito": apito, "odd": odd, "prob": prob,
                "classe": rotulo, "retiradas": retiradas, "provas": sorted(set(provas)),
                "sombra": res,
            })
    return decisoes, por_multipla, por_perna, por_casa


# ---------------------------------------------------------------------------
# provas (A1-A4)
# ---------------------------------------------------------------------------

TABELAS_INTOCADAS = ("recomendacoes", "historico_recomendacoes", "registro_publico", "registro_modelo")


def md5_tabela(cur, tabela):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{tabela}",))
    if not cur.fetchone()[0]:
        return None
    cur.execute(f"SELECT COUNT(*), md5(COALESCE(string_agg(t::text, '|' ORDER BY t.id), '')) FROM {tabela} t")
    return tuple(cur.fetchone())


def md5_candidatas(cur, ids):
    """(md5 das colunas que não podem mudar em linha nenhuma,
        md5 de avaliada/resultado das linhas fora do conjunto marcado)."""
    cur.execute(
        """SELECT md5(COALESCE(string_agg(concat_ws('#', id, assinatura, casa_aposta, descricao, odd_combinada,
                       probabilidade_combinada, pernas::text, jogos::text, primeiro_apito, congelada,
                       criada_em, atualizada_em), '|' ORDER BY id), '')),
                  md5(COALESCE(string_agg(concat_ws('#', id, avaliada, resultado), '|' ORDER BY id)
                      FILTER (WHERE NOT (id = ANY(%s))), ''))
           FROM multiplas_candidatas""",
        (list(ids) or [0],),
    )
    return tuple(cur.fetchone())


def provar(cur, antes_tab, antes_cand, ids, decisoes):
    falhas = []
    for t, v in antes_tab.items():
        if md5_tabela(cur, t) != v:
            falhas.append(f"A1: {t} mudou")
    if md5_candidatas(cur, ids) != antes_cand:
        falhas.append("A2: multiplas_candidatas mudou fora de avaliada/resultado das linhas marcadas")
    cur.execute("SELECT COUNT(*) FROM multiplas_candidatas WHERE id = ANY(%s) "
                "AND avaliada = TRUE AND resultado = %s AND congelada = TRUE", (list(ids) or [0], RESULTADO_RETIRADA))
    if cur.fetchone()[0] != len(ids):
        falhas.append("A2: nem toda linha marcada ficou avaliada/retirada")
    for d in decisoes:
        if d["classe"] == "RETIRADA_PROVADA":
            cur.execute("SELECT COUNT(*) FROM registro_publico WHERE id = ANY(%s) AND evento = 'retirada'",
                        (d["provas"] or [0],))
            if cur.fetchone()[0] < 1:
                falhas.append(f"A3: candidata {d['id']} sem prova no registro_publico")
                break
    return falhas


# ---------------------------------------------------------------------------
# relatório
# ---------------------------------------------------------------------------

def roi(itens):
    itens = [d for d in itens if d["sombra"] in ("acertou", "errou")]
    if not itens:
        return "—"
    ganho = sum(float(d["odd"]) for d in itens if d["sombra"] == "acertou")
    acertos = sum(1 for d in itens if d["sombra"] == "acertou")
    return f"{len(itens)} avaliáveis · {acertos} acertos · ROI {100 * (ganho / len(itens) - 1):+.1f}%"


def imprimir(cur, decisoes, por_multipla, por_perna, por_casa):
    print("\n== Presas por classe da múltipla ==")
    for casa, cont in sorted(por_casa.items()):
        print(f"  {casa}: " + " · ".join(f"{k} {v}" for k, v in cont.most_common()))
    print("  total: " + " · ".join(f"{k} {v}" for k, v in por_multipla.most_common()))

    print("\n== Pernas distintas sem veredito, por classe ==")
    print("  " + " · ".join(f"{k} {v}" for k, v in por_perna.most_common()))

    print(f"\n== A marcar como '{RESULTADO_RETIRADA}': {len(decisoes)} múltipla(s) ==")
    for casa in sorted({d["casa"] for d in decisoes}):
        dc = [d for d in decisoes if d["casa"] == casa]
        print(f"  {casa}: {len(dc)} · sombra: {roi(dc)}")
        for classe in CLASSES_QUE_LIBERAM:
            sub = [d for d in dc if d["classe"] == classe]
            if sub:
                print(f"     {classe}: {len(sub)} · sombra: {roi(sub)}")

    print("\n== Para comparar: ROI das avaliadas de verdade (acertou/errou), por casa ==")
    cur.execute("""SELECT casa_aposta, COUNT(*), COUNT(*) FILTER (WHERE resultado = 'acertou'),
                          SUM(CASE WHEN resultado = 'acertou' THEN odd_combinada ELSE 0 END)
                   FROM multiplas_candidatas WHERE avaliada AND resultado IN ('acertou', 'errou')
                   GROUP BY 1 ORDER BY 1""")
    for casa, n, ac, ganho in cur.fetchall():
        print(f"  {casa}: {n} · {ac} acertos · ROI {100 * (float(ganho or 0) / n - 1):+.1f}%")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def ler_argumentos(argv):
    if len(argv) < 2 or argv[1] not in ("verificar", "aplicar"):
        raise SystemExit("uso: python varrer_multiplas_presas.py verificar|aplicar [--limite N]")
    fase, limite = argv[1], None
    if "--limite" in argv:
        limite = int(argv[argv.index("--limite") + 1])
    if fase == "aplicar" and limite:
        raise SystemExit("--limite só vale no verificar (o aplicar varre tudo)")
    return fase, limite


def main(argv):
    fase, limite = ler_argumentos(argv)
    print(f"=== {VERSAO} · fase {fase.upper()} · argv {argv[1:]} · "
          f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC ===")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        if fase == "verificar":
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SHOW transaction_read_only")
            if cur.fetchone()[0] != "on":
                raise SystemExit("🔴 a transação não ficou READ ONLY — parando.")

        decisoes, por_multipla, por_perna, por_casa = varrer(cur, limite)
        imprimir(cur, decisoes, por_multipla, por_perna, por_casa)

        if fase == "verificar":
            print("\nFASE VERIFICAR — nada gravado: multiplas_candidatas e multiplas_retiradas "
                  "não foram tocadas (transação READ ONLY, rollback).")
            return 0

        antes_tab = {t: md5_tabela(cur, t) for t in TABELAS_INTOCADAS}
        ids = [d["id"] for d in decisoes]
        antes_cand = md5_candidatas(cur, ids)
        cur.execute(SQL_CRIAR_TABELA)
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'claude_leitura'")
        if cur.fetchone():
            cur.execute("GRANT SELECT ON public.multiplas_retiradas TO claude_leitura")

        for d in decisoes:
            cur.execute(
                "UPDATE multiplas_candidatas SET avaliada = TRUE, resultado = %s "
                "WHERE id = %s AND congelada = TRUE AND avaliada = FALSE",
                (RESULTADO_RETIRADA, d["id"]),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"candidata {d['id']} mudou durante a varredura (rowcount {cur.rowcount})")
            cur.execute(
                """INSERT INTO multiplas_retiradas
                   (candidata_id, casa_aposta, primeiro_apito, odd_combinada, probabilidade_combinada,
                    classe, pernas_retiradas, prova_registro_publico, resultado_sombra, versao_codigo)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::bigint[], %s, %s)""",
                (d["id"], d["casa"], d["apito"], d["odd"], d["prob"], d["classe"],
                 json.dumps(d["retiradas"], ensure_ascii=False, default=str),
                 d["provas"] or None, d["sombra"], VERSAO),
            )

        falhas = provar(cur, antes_tab, antes_cand, ids, decisoes)
        if falhas:
            conn.rollback()
            print("\n🔴 PROVAS FALHARAM — rollback, nada gravado:")
            for f in falhas:
                print("   " + f)
            return 1
        conn.commit()
        print(f"\n✅ FASE APLICAR — {len(ids)} múltipla(s) marcadas como '{RESULTADO_RETIRADA}' e "
              f"registradas em multiplas_retiradas. A1-A3 ok; rodar de novo deve marcar 0 (A4).")
        return 0
    finally:
        try:
            conn.rollback()
        finally:
            cur.close()
            conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
