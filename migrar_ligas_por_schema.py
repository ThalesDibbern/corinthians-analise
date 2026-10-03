"""
migrar_ligas_por_schema.py — copia UMA liga do schema legado `ligas` para o
schema próprio dela (`liga_premier`, `liga_laliga`, ...).

Fase 1 da `claude/arquitetura_ligas_extras.md` (decisão do usuário em 01/10:
um schema por liga). Duas fases:

    python migrar_ligas_por_schema.py verificar --liga premier
    python migrar_ligas_por_schema.py aplicar   --liga premier

`verificar` NÃO grava em tabela nenhuma (nem em `public`, nem em `ligas`, nem
no schema novo — nem cria o schema): só conta o que seria copiado.

`aplicar`, numa transação só:
  1. cria o schema da liga com a MESMA função do coletar_ligas (importada,
     não copiada) e passa pela MESMA trava de isolamento;
  2. copia as linhas da liga preservando os ids (FKs internas continuam
     batendo; `jogos.id = fixture_id_api` continua valendo);
  3. acerta as sequências do schema novo para depois do maior id copiado;
  4. CONFERE: contagem E md5 do conteúdo, tabela por tabela, origem x destino;
  5. só faz COMMIT se tudo bater. Qualquer diferença = ROLLBACK, nada fica.

O QUE NÃO FAZ (de propósito):
  - NÃO apaga nada do schema legado `ligas`. Ele fica intacto como cópia de
    segurança até o usuário decidir apagá-lo (passo separado, depois que as
    três ligas estiverem migradas e conferidas).
  - NÃO toca no `public` (Brasileirão): contagens conferidas antes e depois.
  - NÃO chama API nenhuma.

Rodar de novo uma liga já migrada é seguro: ele só confere e sai.

⚠️ Não rodar enquanto o `coleta-ligas` estiver coletando ESSA liga no `ligas`:
a cópia pegaria um retrato no meio da coleta. A trava do coletar_ligas novo
impede o caminho contrário (coletar no schema novo sem ter migrado).
"""

import sys
from datetime import datetime

import coletar_ligas as cl   # funções de PRODUÇÃO: estrutura, trava, conexão

LEGADO = cl.SCHEMA_LEGADO


class DivergenciaNaCopia(Exception):
    """Contagem ou conteúdo do destino não bate com a origem."""


# Ordem de cópia e o filtro de cada tabela, sobre o schema LEGADO.
# `{L}` = schema legado. Todo filtro parte dos jogos da liga.
JOGOS_DA_LIGA = "SELECT id FROM {L}.jogos WHERE liga_api_id = %(liga)s"

FILTROS = {
    # times: os que aparecem em algum jogo da liga (mandante, visitante ou perspectiva).
    "times": """id IN (SELECT mandante_id FROM {L}.jogos WHERE liga_api_id = %(liga)s
                       UNION SELECT visitante_id FROM {L}.jogos WHERE liga_api_id = %(liga)s
                       UNION SELECT nosso_time_id FROM {L}.jogos WHERE liga_api_id = %(liga)s)""",
    "jogos": "liga_api_id = %(liga)s",
    "gols": f"jogo_id IN ({JOGOS_DA_LIGA})",
    "cartoes": f"jogo_id IN ({JOGOS_DA_LIGA})",
    "substituicoes": f"jogo_id IN ({JOGOS_DA_LIGA})",
    "estatisticas_jogo": f"jogo_id IN ({JOGOS_DA_LIGA})",
    "jogador_estatisticas_jogo": f"jogo_id IN ({JOGOS_DA_LIGA})",
    "escalacoes": f"jogo_id IN ({JOGOS_DA_LIGA})",
    # jogadores: todo jogador referenciado por um evento da liga, mais os que
    # têm como time atual um time da liga. Jogador que trocou de liga entra
    # nos DOIS schemas (mesmo id) — cada liga tem a sua cópia.
    # `time_atual_id` é copiado como está, mesmo apontando para time de outra
    # liga: o LIKE não copia FK, e para esta liga isso significa "não está em
    # nenhum time daqui" — que é verdade.
    "jogadores": f"""id IN (
        SELECT jogador_id FROM {{L}}.gols WHERE jogo_id IN ({JOGOS_DA_LIGA})
        UNION SELECT jogador_id FROM {{L}}.cartoes WHERE jogo_id IN ({JOGOS_DA_LIGA})
        UNION SELECT jogador_saiu_id FROM {{L}}.substituicoes WHERE jogo_id IN ({JOGOS_DA_LIGA})
        UNION SELECT jogador_entrou_id FROM {{L}}.substituicoes WHERE jogo_id IN ({JOGOS_DA_LIGA})
        UNION SELECT jogador_id FROM {{L}}.jogador_estatisticas_jogo WHERE jogo_id IN ({JOGOS_DA_LIGA})
        UNION SELECT jogador_id FROM {{L}}.escalacoes WHERE jogo_id IN ({JOGOS_DA_LIGA})
        UNION SELECT id FROM {{L}}.jogadores WHERE time_atual_id IN (
            SELECT mandante_id FROM {{L}}.jogos WHERE liga_api_id = %(liga)s
            UNION SELECT visitante_id FROM {{L}}.jogos WHERE liga_api_id = %(liga)s))""",
}
ORDEM = ["times", "jogadores", "jogos", "gols", "cartoes", "substituicoes",
         "estatisticas_jogo", "jogador_estatisticas_jogo", "escalacoes"]
assert sorted(ORDEM) == sorted(cl.TABELAS_COPIADAS), "ORDEM precisa cobrir exatamente as tabelas copiadas"


def filtro(tabela):
    return FILTROS[tabela].replace("{L}", LEGADO)


def colunas(cur, schema, tabela):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, tabela),
    )
    return [r[0] for r in cur.fetchall()]


def contar_origem(cur, tabela, liga):
    cur.execute(f"SELECT COUNT(*) FROM {LEGADO}.{tabela} WHERE {filtro(tabela)}", {"liga": liga})
    return cur.fetchone()[0]


def assinatura(cur, schema, tabela, cols, onde=None, params=None):
    """md5 do conteúdo inteiro, linha a linha, em ordem de id."""
    lista = ", ".join(cols)
    sql = (f"SELECT COUNT(*), md5(COALESCE(string_agg(ROW({lista})::text, '|' ORDER BY id), '')) "
           f"FROM {schema}.{tabela}")
    if onde:
        sql += f" WHERE {onde}"
    cur.execute(sql, params or {})
    return cur.fetchone()


def cabecalho(fase, chave):
    print("=" * 78)
    print(f"migrar_ligas_por_schema.py | fase: {fase} | liga: {chave}")
    print(f"argv: {sys.argv}")
    print(f"relógio do processo: {datetime.now().isoformat(timespec='seconds')}")
    print(f"origem: `{LEGADO}` (não é alterada) → destino: `{cl.SCHEMA}`")
    print("=" * 78)
    sys.stdout.flush()


def estado(cur, liga):
    """Contagens da origem e (se existir) do destino, por tabela."""
    destino_existe = cl.schema_existe(cur) and all(
        cl.tabela_existe(cur, cl.SCHEMA, t) for t in cl.TABELAS_COPIADAS)
    linhas = []
    for t in ORDEM:
        origem = contar_origem(cur, t, liga)
        destino = None
        if destino_existe:
            cur.execute(f"SELECT COUNT(*) FROM {cl.SCHEMA}.{t}")
            destino = cur.fetchone()[0]
        linhas.append((t, origem, destino))
    return destino_existe, linhas


def imprimir_estado(linhas):
    print(f"\n  {'tabela':<28} {'origem (`' + LEGADO + '`)':>18} {'destino':>10}")
    for t, o, d in linhas:
        print(f"  {t:<28} {o:>18} {('—' if d is None else d):>10}")


def conferir(cur, liga):
    """Origem x destino, contagem e md5, tabela por tabela. Levanta se divergir."""
    problemas = []
    print(f"\n  {'tabela':<28} {'linhas':>8}  conteúdo")
    for t in ORDEM:
        cols = colunas(cur, LEGADO, t)
        cols_destino = colunas(cur, cl.SCHEMA, t)
        if cols != cols_destino:
            problemas.append(f"{t}: colunas diferentes ({cols} x {cols_destino})")
            continue
        n_o, h_o = assinatura(cur, LEGADO, t, cols, filtro(t), {"liga": liga})
        n_d, h_d = assinatura(cur, cl.SCHEMA, t, cols)
        ok = (n_o == n_d and h_o == h_d)
        print(f"  {t:<28} {n_d:>8}  {'✅ idêntico' if ok else f'❌ origem {n_o} / md5 difere'}")
        if not ok:
            problemas.append(f"{t}: origem {n_o} linhas, destino {n_d}, md5 {'igual' if h_o == h_d else 'diferente'}")
    if problemas:
        raise DivergenciaNaCopia("; ".join(problemas))


def fase_verificar(chave):
    liga, nome = cl.LIGAS[chave]
    conn = cl.conectar()
    cur = conn.cursor()
    try:
        if not cl.schema_existe(cur, LEGADO):
            print(f"Schema legado `{LEGADO}` não existe — nada a migrar.")
            return
        destino_existe, linhas = estado(cur, liga)
        print(f"{nome}: o que seria copiado de `{LEGADO}` para `{cl.SCHEMA}`")
        imprimir_estado(linhas)
        if destino_existe and all(o == d for _t, o, d in linhas):
            print("\n  As contagens já batem — o aplicar só vai conferir o conteúdo (md5) e sair.")
        elif destino_existe and any(d for _t, _o, d in linhas):
            print("\n  ⚠️ O destino já tem linhas e as contagens NÃO batem — o aplicar vai recusar. "
                  "Investigar antes.")
    finally:
        conn.rollback()  # incondicional: esta fase nunca grava
        cur.close()
        conn.close()
    print("\n" + "=" * 78)
    print(f"FASE VERIFICAR — nenhuma tabela foi tocada: nem `{LEGADO}`, nem `{cl.SCHEMA}` "
          f"(nem criado), nem `public`.")
    print("=" * 78)


def fase_aplicar(chave):
    liga, nome = cl.LIGAS[chave]
    conn = cl.conectar()
    cur = conn.cursor()
    publico_antes = cl.contagens_public(cur)
    try:
        if not cl.schema_existe(cur, LEGADO):
            print(f"Schema legado `{LEGADO}` não existe — nada a migrar.")
            return

        cl.criar_estrutura(cur)
        cl.garantir_isolamento(cur)   # 🔴 a mesma trava da coleta, antes de gravar

        destino_existe, linhas = estado(cur, liga)
        imprimir_estado(linhas)
        ja_tem = any(d for _t, _o, d in linhas)

        if ja_tem:
            print("\n  O destino já tem dado — NÃO copio de novo. Só confiro origem x destino:")
        else:
            print(f"\n  Copiando {nome}, preservando os ids:")
            for t in ORDEM:
                cols = ", ".join(colunas(cur, LEGADO, t))
                cur.execute(
                    f"INSERT INTO {cl.SCHEMA}.{t} ({cols}) "
                    f"SELECT {cols} FROM {LEGADO}.{t} WHERE {filtro(t)} ORDER BY id",
                    {"liga": liga},
                )
                print(f"    {t}: {cur.rowcount}")

            # Sequências do destino para depois do maior id copiado — senão o
            # próximo INSERT da coleta colidiria com um id migrado.
            for tabela, coluna, schema_seq, sequencia in cl.sequencias_do_schema(cur):
                cur.execute(f"SELECT COALESCE(MAX({coluna}), 0) FROM {cl.SCHEMA}.{tabela}")
                maximo = cur.fetchone()[0]
                if maximo > 0:
                    cur.execute(f"SELECT setval('{schema_seq}.{sequencia}', %s)", (maximo,))

        print("\n  Conferência origem x destino:")
        conferir(cur, liga)   # levanta DivergenciaNaCopia se qualquer coisa diferir

        publico_depois = cl.contagens_public(cur)
        if publico_depois != publico_antes:
            raise DivergenciaNaCopia(f"`public` mudou durante a migração: {publico_antes} → {publico_depois}")

        conn.commit()
        print(f"\n✅ {nome} migrada e conferida: `{cl.SCHEMA}` idêntico à fatia de `{LEGADO}`. "
              f"`{LEGADO}` não foi alterado; `public` intocado.")
    except Exception as e:
        conn.rollback()
        print(f"\n🔴 ROLLBACK — nada foi gravado: {e}")
        raise
    finally:
        cur.close()
        conn.close()


def main():
    argv = sys.argv
    if len(argv) < 4 or argv[1] not in ("verificar", "aplicar") or "--liga" not in argv:
        print(__doc__)
        sys.exit(1)
    chave = argv[argv.index("--liga") + 1]
    if chave not in cl.LIGAS:
        print(f"Liga desconhecida: {chave}. Opções: {', '.join(cl.LIGAS)}")
        sys.exit(1)
    cl.definir_liga(chave)
    cabecalho(argv[1], chave)
    if argv[1] == "verificar":
        fase_verificar(chave)
    else:
        fase_aplicar(chave)


if __name__ == "__main__":
    main()
