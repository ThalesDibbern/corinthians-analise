"""
registrar_publico.py — grava em `public.registro_publico` as recomendações
vivas de jogos FUTUROS, e cada mudança delas, ANTES do apito.

Três modos:

    python registrar_publico.py verificar   # mostra o que gravaria; não grava nada
    python registrar_publico.py aplicar     # grava (vai no FIM do cron de odds)
    python registrar_publico.py cadeia      # só leitura: confere a cadeia de hash inteira

Lugar no cron (`refreshing-freedom`), DEPOIS de tudo:
    ... && python motor_combinacoes.py && python registrar_publico.py aplicar
No fim da cadeia, uma falha dele não impede nada do que vem antes.

--------------------------------------------------------------------------
O QUE GRAVA
--------------------------------------------------------------------------

Chave de uma aposta: (jogo_id, tipo_padrao, linha, direcao, jogador_id,
casa_aposta). `jogo_id`, e não `fixture_id_api`, porque mercado de TIME é
aposta diferente em cada perspectiva (`1_ESSENCIAL §5.3`).

  - aposta viva sem registro, ou cujo último registro é 'retirada'  -> 'ativa'
  - aposta viva cuja odd, probabilidade, VE ou descrição mudou     -> 'ativa' (nova versão)
  - aposta cujo último registro é 'ativa' e que SUMIU de
    `recomendacoes`, com o jogo ainda no futuro                     -> 'retirada'
  - nada mudou                                                      -> nada

O que vale para um jogo é o ÚLTIMO evento de cada aposta antes do apito.

Não lê nem escreve nenhuma outra tabela além de `recomendacoes`/`jogos`
(leitura) e `registro_publico` (inclusão). Não muda nada do que o motor gera.

Liga: `LIGA_CHAVE` no ambiente (padrão `brasileirao`). `recomendacoes` e
`jogos` são lidas SEM schema — no pipeline das ligas, o `search_path` aponta
para o schema da liga; `registro_publico` é sempre `public` (um histórico só).
"""

import os
import sys
from datetime import datetime

import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]
LIGA = os.environ.get("LIGA_CHAVE", "brasileirao")
VERSAO_CODIGO = (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "desconhecida")[:12]

CHAVE = ("jogo_id", "tipo_padrao", "linha", "direcao", "jogador_id", "casa_aposta")
VALORES = ("odd", "probabilidade", "valor_esperado", "descricao")

SQL_VIVAS = """
    SELECT DISTINCT ON (r.jogo_id, r.tipo_padrao, r.linha, r.direcao, r.jogador_id, r.casa_aposta)
           r.jogo_id, r.tipo_padrao, r.linha, r.direcao, r.jogador_id, r.casa_aposta,
           r.odd_oferecida, r.probabilidade_historica, r.valor_esperado, r.descricao,
           j.fixture_id_api, j.datahora_jogo
    FROM recomendacoes r
    JOIN jogos j ON j.id = r.jogo_id
    WHERE j.datahora_jogo > (now() AT TIME ZONE 'UTC')
    ORDER BY r.jogo_id, r.tipo_padrao, r.linha, r.direcao, r.jogador_id, r.casa_aposta, r.id DESC
"""

SQL_SEM_DATAHORA = """
    SELECT COUNT(*) FROM recomendacoes r JOIN jogos j ON j.id = r.jogo_id
    WHERE j.datahora_jogo IS NULL
"""

SQL_ULTIMO_ESTADO = """
    SELECT DISTINCT ON (jogo_id, tipo_padrao, linha, direcao, jogador_id, casa_aposta)
           jogo_id, tipo_padrao, linha, direcao, jogador_id, casa_aposta,
           odd, probabilidade, valor_esperado, descricao, evento, fixture_id_api, datahora_jogo,
           datahora_jogo > (now() AT TIME ZONE 'UTC') AS futuro
    FROM public.registro_publico
    WHERE liga = %s
    ORDER BY jogo_id, tipo_padrao, linha, direcao, jogador_id, casa_aposta, id DESC
"""

SQL_INSERIR = """
    INSERT INTO public.registro_publico
        (liga, evento, fixture_id_api, jogo_id, datahora_jogo, tipo_padrao, linha, direcao,
         jogador_id, casa_aposta, descricao, odd, probabilidade, valor_esperado, versao_codigo, hash_linha)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '')
"""
# hash_linha '' é só para passar o NOT NULL: o trigger calcula o valor real.

# Uma consulta, nada mais. Serve também pela /api/consulta (só leitura).
SQL_CADEIA = """
    SELECT COUNT(*) AS linhas,
           COUNT(*) FILTER (WHERE r.hash_anterior IS DISTINCT FROM l.anterior
               OR r.hash_linha IS DISTINCT FROM encode(sha256(convert_to(
                      COALESCE(l.anterior, '') || public.registro_publico_conteudo(r), 'UTF8')), 'hex')) AS quebradas,
           MIN(r.id) FILTER (WHERE r.hash_anterior IS DISTINCT FROM l.anterior
               OR r.hash_linha IS DISTINCT FROM encode(sha256(convert_to(
                      COALESCE(l.anterior, '') || public.registro_publico_conteudo(r), 'UTF8')), 'hex')) AS primeira_quebrada,
           MAX(r.id) AS ultimo_id
    FROM public.registro_publico r
    JOIN (SELECT id, lag(hash_linha) OVER (ORDER BY id) AS anterior FROM public.registro_publico) l USING (id)
"""


def cabecalho(fase):
    print("=" * 78)
    print(f"registrar_publico.py | fase: {fase} | liga: {LIGA} | versão do código: {VERSAO_CODIGO}")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 78)
    sys.stdout.flush()


def chave(linha):
    return tuple(linha[c] for c in CHAVE)


def como_dict(cur):
    nomes = [d[0] for d in cur.description]
    return [dict(zip(nomes, r)) for r in cur.fetchall()]


def planejar(cur):
    """Compara o vivo com o último estado registrado. Só leitura."""
    cur.execute(SQL_VIVAS)
    vivas = como_dict(cur)
    for v in vivas:
        v["odd"] = v.pop("odd_oferecida")
        v["probabilidade"] = v.pop("probabilidade_historica")

    cur.execute(SQL_SEM_DATAHORA)
    sem_datahora = cur.fetchone()[0]

    cur.execute(SQL_ULTIMO_ESTADO, (LIGA,))
    ultimos = {chave(u): u for u in como_dict(cur)}

    novas, mudadas, retiradas = [], [], []
    chaves_vivas = set()
    for v in vivas:
        k = chave(v)
        chaves_vivas.add(k)
        u = ultimos.get(k)
        if u is None or u["evento"] == "retirada":
            novas.append(v)
        elif any(u[c] != v[c] for c in VALORES):
            mudadas.append(v)
    for k, u in ultimos.items():
        if u["evento"] == "ativa" and k not in chaves_vivas and u["futuro"]:
            retiradas.append(u)
    return vivas, novas, mudadas, retiradas, sem_datahora


def gravar(cur, linha, evento):
    valores = (None, None, None) if evento == "retirada" else (
        linha["odd"], linha["probabilidade"], linha["valor_esperado"])
    cur.execute(SQL_INSERIR, (
        LIGA, evento, linha["fixture_id_api"], linha["jogo_id"], linha["datahora_jogo"],
        linha["tipo_padrao"], linha["linha"], linha["direcao"], linha["jogador_id"],
        linha["casa_aposta"], linha["descricao"], *valores, VERSAO_CODIGO))


def conferir_cadeia(cur):
    cur.execute(SQL_CADEIA)
    total, quebradas, primeira, ultimo = cur.fetchone()
    if quebradas:
        print(f"🔴 CADEIA QUEBRADA: {quebradas} de {total} linha(s) não conferem; a primeira é o id {primeira}.")
    else:
        print(f"✅ cadeia íntegra: {total} linha(s), até o id {ultimo}.")
    return quebradas


def resumo(vivas, novas, mudadas, retiradas, sem_datahora):
    print(f"recomendações vivas de jogos futuros: {len(vivas)} "
          f"(em {len({v['fixture_id_api'] for v in vivas})} jogo(s))")
    print(f"  novas: {len(novas)} · mudaram: {len(mudadas)} · retiradas: {len(retiradas)} · "
          f"sem mudança: {len(vivas) - len(novas) - len(mudadas)}")
    if sem_datahora:
        print(f"  ⚠️ {sem_datahora} recomendação(ões) de jogo SEM datahora_jogo — não registradas "
              f"(não há como provar que foi antes do apito).")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("verificar", "aplicar", "cadeia"):
        print(__doc__)
        sys.exit(1)
    fase = sys.argv[1]
    cabecalho(fase)
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.registro_publico') IS NOT NULL")
        if not cur.fetchone()[0]:
            print("`public.registro_publico` não existe — rode antes: python criar_registro_publico.py aplicar")
            sys.exit(1)

        if fase == "cadeia":
            quebradas = conferir_cadeia(cur)
            conn.rollback()
            sys.exit(2 if quebradas else 0)

        vivas, novas, mudadas, retiradas, sem_datahora = planejar(cur)
        resumo(vivas, novas, mudadas, retiradas, sem_datahora)

        if fase == "verificar":
            conn.rollback()
            print("\n" + "=" * 78)
            print("FASE VERIFICAR — `registro_publico` NÃO foi tocada; nenhuma outra tabela também.")
            print("=" * 78)
            return

        for v in novas + mudadas:
            gravar(cur, v, "ativa")
        for u in retiradas:
            gravar(cur, u, "retirada")
        conn.commit()
        print(f"\ngravadas: {len(novas) + len(mudadas)} 'ativa' + {len(retiradas)} 'retirada'.")
        conferir_cadeia(cur)
        conn.rollback()
    except SystemExit:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
