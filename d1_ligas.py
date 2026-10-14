"""
d1_ligas.py — aplica a decisão D1 (12/10/2026) nos schemas das ligas
extras: tira da janela de 50 jogos o que não é jogo normal da liga.

    python d1_ligas.py verificar            # mostra o que seria movido; NÃO grava nada
    python d1_ligas.py aplicar              # move para o arquivo do próprio schema
    python d1_ligas.py reverter             # devolve do arquivo para as tabelas

--------------------------------------------------------------------------
A DECISÃO D1 (`2_ABERTO §3.10`)
--------------------------------------------------------------------------

  - playoff de rebaixamento (contra time da 2ª divisão) .......... FORA
      rodada "Relegation Round" ou "Final" na API-Football
      (Bundesliga e Ligue 1, 2 jogos por temporada)
  - desempate entre times da liga ("Relegation Decider", Serie A) . DENTRO
  - jogo interrompido sem estatística ............................ FORA
      lista explícita, decidida caso a caso (hoje: Nantes x Toulouse,
      Ligue 1, 17/05/2026, fixture 1388000 — 0-0 homologado, sem evento)

--------------------------------------------------------------------------
POR QUE PELO DADO E NÃO PELO CÓDIGO
--------------------------------------------------------------------------

O `motor_padroes` tem ~49 consultas em `jogos` e nenhum ponto único de
filtro; filtrar no código mexeria no código que o Brasileirão roda.
Tirando as linhas do schema da liga, o motor (que roda lá pelo
`search_path`) simplesmente não as vê — sem mudar uma linha dele.

--------------------------------------------------------------------------
GARANTIAS
--------------------------------------------------------------------------

  - Só toca schemas `liga_*`. Todo nome é qualificado pelo schema; o
    `public` nunca é tocado (contagens conferidas antes e depois).
  - MOVER, não apagar: as linhas vão para `<schema>.d1_arquivo_<tabela>`
    (mesmas colunas). Por liga, numa transação só: copia → confere
    contagem e md5 entre a origem e o arquivo → remove da origem →
    confere que não sobrou nada → commit. Qualquer divergência = rollback.
  - Remoção SEM CASCADE: se alguma tabela de fora apontar para o jogo
    (ex.: odds da liga depois da Fase 2), o banco recusa e nada muda.
  - `reverter` faz o caminho inverso, com as mesmas provas.
  - Idempotente: o que já está no arquivo não é movido de novo.

O `coletar_ligas.py` (versão de 13/10) não coleta mais playoff de
rebaixamento, então o que sai daqui não volta na coleta diária.
"""

import os
import sys
from datetime import datetime

import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

SCHEMAS = ["liga_premier", "liga_laliga", "liga_seriea", "liga_bundesliga", "liga_ligue1"]

# Rodadas de playoff de rebaixamento (D1: fora). O desempate da Serie A
# ("Relegation Decider") NÃO está aqui de propósito (D1: dentro).
RODADAS_PLAYOFF = ("Relegation Round", "Final")

# Jogos interrompidos sem estatística (D1: fora) — lista explícita por schema.
INTERROMPIDOS = {"liga_ligue1": [1388000]}

# Ordem importa: dependentes antes de `jogos` na remoção; o contrário na volta.
DEPENDENTES = ["gols", "cartoes", "substituicoes", "estatisticas_jogo",
               "jogador_estatisticas_jogo", "escalacoes"]

PUBLIC_VIGIADAS = ["jogos", "gols", "cartoes", "estatisticas_jogo", "jogador_estatisticas_jogo", "escalacoes"]


class ProvaFalhou(Exception):
    pass


def cabecalho(fase):
    print("=" * 78)
    print(f"d1_ligas.py | fase: {fase}")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 78)
    sys.stdout.flush()


def um(cur, sql, params=None):
    cur.execute(sql, params)
    return cur.fetchone()


def existe(cur, nome):
    return um(cur, "SELECT to_regclass(%s) IS NOT NULL", (nome,))[0]


def contagens_public(cur):
    return {t: um(cur, f"SELECT COUNT(*) FROM public.{t}")[0] for t in PUBLIC_VIGIADAS}


def arquivo(schema, tabela):
    return f"{schema}.d1_arquivo_{tabela}"


def criar_arquivos(cur, schema):
    for t in ["jogos"] + DEPENDENTES:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {arquivo(schema, t)} (LIKE {schema}.{t})")
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'claude_leitura'")
    if cur.fetchone():
        for t in ["jogos"] + DEPENDENTES:
            cur.execute(f"GRANT SELECT ON {arquivo(schema, t)} TO claude_leitura")


def alvos(cur, schema, origem):
    """ids de `jogos` (na tabela `origem`) que a D1 tira da janela."""
    fixtures = INTERROMPIDOS.get(schema, [])
    cur.execute(
        f"""SELECT id FROM {origem}
            WHERE rodada = ANY(%s) OR fixture_id_api = ANY(%s)
            ORDER BY id""",
        (list(RODADAS_PLAYOFF), fixtures or [-1]),
    )
    return [r[0] for r in cur.fetchall()]


def descrever(cur, origem, ids):
    cur.execute(
        f"""SELECT temporada, rodada, COUNT(*), COUNT(DISTINCT fixture_id_api),
                   MIN(data_jogo), MAX(data_jogo)
            FROM {origem} WHERE id = ANY(%s) GROUP BY 1, 2 ORDER BY 1, 2""",
        (ids,),
    )
    for temporada, rodada, linhas, fixtures, dmin, dmax in cur.fetchall():
        print(f"    temporada {temporada} · {rodada}: {fixtures} jogo(s), {linhas} linha(s) · {dmin} a {dmax}")


def assinatura(cur, tabela, coluna, ids):
    """(contagem, md5) das linhas de `tabela` com `coluna` em ids."""
    return um(cur,
              f"""SELECT COUNT(*), md5(COALESCE(string_agg(t::text, '|' ORDER BY t::text), ''))
                  FROM {tabela} t WHERE {coluna} = ANY(%s)""",
              (ids,))


def mover(cur, schema, ids, de_arquivo):
    """Move as linhas dos `ids` entre o schema e o arquivo, com provas."""
    pares = [("jogos", "id")] + [(t, "jogo_id") for t in DEPENDENTES]
    resumo = []
    ordem_copia = pares   # pai antes na cópia (o arquivo não tem FK; a volta precisa)
    for t, col in ordem_copia:
        origem = arquivo(schema, t) if de_arquivo else f"{schema}.{t}"
        destino = f"{schema}.{t}" if de_arquivo else arquivo(schema, t)
        antes_origem = assinatura(cur, origem, col, ids)
        ja_no_destino = assinatura(cur, destino, col, ids)[0]
        if ja_no_destino:
            raise ProvaFalhou(f"{destino} já tem {ja_no_destino} linha(s) desses jogos — nada movido.")
        cur.execute(f"INSERT INTO {destino} SELECT * FROM {origem} WHERE {col} = ANY(%s)", (ids,))
        depois_destino = assinatura(cur, destino, col, ids)
        if depois_destino != antes_origem:
            raise ProvaFalhou(f"{t}: origem {antes_origem} ≠ destino {depois_destino} — rollback.")
        resumo.append((t, antes_origem[0]))
    for t, col in [(t, "jogo_id") for t in DEPENDENTES] + [("jogos", "id")]:   # filhos antes do pai
        origem = arquivo(schema, t) if de_arquivo else f"{schema}.{t}"
        cur.execute(f"DELETE FROM {origem} WHERE {col} = ANY(%s)", (ids,))
        if assinatura(cur, origem, col, ids)[0]:
            raise ProvaFalhou(f"{origem}: sobrou linha depois da remoção — rollback.")
    return resumo


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("verificar", "aplicar", "reverter"):
        print(__doc__)
        sys.exit(1)
    fase = sys.argv[1]
    cabecalho(fase)
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    houve_erro = False
    try:
        publico_antes = contagens_public(cur)
        for schema in SCHEMAS:
            print(f"\n## {schema}")
            if not existe(cur, f"{schema}.jogos"):
                print("  schema/tabela não existe — pulado.")
                continue
            tem_arquivo = existe(cur, arquivo(schema, "jogos"))
            if fase == "reverter":
                if not tem_arquivo:
                    print("  sem arquivo D1 — nada a devolver.")
                    continue
                cur.execute(f"SELECT id FROM {arquivo(schema, 'jogos')}")
                ids = sorted({r[0] for r in cur.fetchall()})
            else:
                ids = alvos(cur, schema, f"{schema}.jogos")
            if not ids:
                print("  nada a mover" + (" (o que a D1 tira já está no arquivo)" if tem_arquivo else "") + ".")
                continue
            origem_desc = arquivo(schema, "jogos") if fase == "reverter" else f"{schema}.jogos"
            print(f"  {len(ids)} linha(s) de jogos {'no arquivo' if fase == 'reverter' else 'a tirar da janela'}:")
            descrever(cur, origem_desc, ids)
            if fase == "verificar":
                for t, col in [("jogos", "id")] + [(t, "jogo_id") for t in DEPENDENTES]:
                    print(f"    {t}: {assinatura(cur, f'{schema}.{t}', col, ids)[0]} linha(s)")
                continue
            try:
                if fase == "aplicar":
                    criar_arquivos(cur, schema)
                resumo = mover(cur, schema, ids, de_arquivo=(fase == "reverter"))
                conn.commit()
                print("  ✅ " + ("devolvido" if fase == "reverter" else "movido para o arquivo") + ": "
                      + ", ".join(f"{t} {n}" for t, n in resumo))
            except Exception as erro:
                conn.rollback()
                houve_erro = True
                print(f"  🔴 {schema}: {erro}")
        publico_depois = contagens_public(cur)
        if publico_depois != publico_antes:
            print(f"\n⚠️ `public` mudou durante a execução (um cron rodando?): {publico_antes} → {publico_depois}")
        else:
            print(f"\n✅ `public` intocado ({len(publico_antes)} tabelas com a mesma contagem).")
    finally:
        conn.rollback()
        cur.close()
        conn.close()
    if fase == "verificar":
        print("\n" + "=" * 78)
        print("FASE VERIFICAR — nada foi criado, movido nem apagado (nem nos schemas liga_*, nem em `public`).")
        print("=" * 78)
    if houve_erro:
        sys.exit(1)


if __name__ == "__main__":
    main()
