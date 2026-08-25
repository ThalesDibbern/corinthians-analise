"""
Script PONTUAL (não faz parte do encadeamento diário do cron) - corrige um
cartão que a API-Football atribuiu ao JOGADOR ERRADO, de outro time, no
jogo Chapecoense x São Paulo (23/08/2026).

O CASO:
    A linha do tempo do ge mostra QUATRO cartões, todos da Chapecoense:

        62'  Marcinho     (Chapecoense)  Falta
        71'  Anderson     (Chapecoense)  Perda de tempo
        73'  Yago Felipe  (Chapecoense)  Falta
        88'  Camilo       (Chapecoense)  Perda de tempo

    O banco tem quatro cartões também - nenhum evento se perdeu - mas o
    segundo está gravado como:

        72'  Luciano      (São Paulo)

    Jogador errado, time errado, minuto deslocado em 1. O total do jogo
    fecha (4), só a divisão entre os times é que está errada: banco diz
    Chapecoense 3 / São Paulo 1, o real é 4 / 0.

    O desvio de 1 minuto aparece em dois eventos (71->72, 73->74) e é só
    cosmético - minuto não entra em cálculo nenhum. O que importa é o
    `lado` e o `jogador_id`.

POR QUE NÃO É O MESMO PROBLEMA DO VITÓRIA x BAHIA:
    Lá faltam eventos (a API entregou 5 de 7). Aqui não falta nada - a
    API entregou os 4 e errou a atribuição de um. São falhas diferentes:
    "incompleto" se corrige sozinho quando a API agrega; "errado" pode
    ficar errado pra sempre, porque um valor errado-e-estável nunca muda.

    Por isso este script tem um caminho manual, e o do Vitória não tem.

POR QUE NÃO É CARTÃO DE COMISSÃO TÉCNICA:
    Hipótese levantada e descartada. Os 4 cartões têm nome de jogador e
    `api_football_id` real. Cartão de comissão vem com `player` nulo e é
    descartado na coleta. Não é viés sistemático de escopo, é erro
    pontual de fonte - mesma família do Athletico-PR x Bragantino.

TRÊS FASES SEPARADAS (rodar uma de cada vez, de propósito):

  FASE 1 (`verificar`) - NÃO GRAVA NADA:
    Mostra, lado a lado:
      (a) o que está em `cartoes` hoje (vem de /fixtures/events)
      (b) o que está em `jogador_estatisticas_jogo` hoje (vem de
          /fixtures/players - FONTE INDEPENDENTE do mesmo fato)
      (c) o que /fixtures/events devolve AGORA
      (d) o que /fixtures/players devolve AGORA

    O item (b) é o ponto importante: se a estatística de jogador já
    marcava cartão pro Anderson e não pro Luciano, a contradição estava
    DENTRO do próprio banco desde o começo, detectável sem nenhuma fonte
    externa. Esse é o teste em miniatura da checagem de coerência entre
    endpoints que está sendo desenhada pra depois da auditoria.

    A fase também decide, e imprime, qual caminho a fase 2 vai seguir:
      - CAMINHO A (refetch): a API já se corrigiu -> apagar e regravar
      - CAMINHO B (manual):  a API insiste no erro -> aplicar a correção
                             declarada em CORRECAO

  FASE 2 (`aplicar`):
    Executa o caminho que a fase 1 apontou. No caminho B, resolve o
    `jogador_id` do Anderson pelo `api_football_id` que vem do próprio
    /fixtures/players daquele jogo - nunca por comparação de nome solta,
    que é justamente o que já gerou jogador duplicado neste projeto.
    Aborta se não achar, em vez de chutar.

  FASE 3 (`reavaliar`):
    Reavalia via `avaliacao.avaliar_resultado` as linhas de
    `historico_recomendacoes` desse jogo, gravando só onde mudou.

IMPACTO ESPERADO NA AVALIAÇÃO:
    Uma linha só: "Cartões - Mais/Menos Sao Paulo - Menos de 0.5", hoje
    marcada `errou` (banco tem 1 cartão), passa a `acertou` (real 0).

    A linha de "Menos de 1.5" do mesmo mercado NÃO muda - com 0 ou com 1
    cartão ela acerta igual. E nenhuma linha de `cartao_total` muda,
    porque o total do jogo já estava certo.

Rodar:
    python corrigir_cartao_atribuicao.py verificar
    python corrigir_cartao_atribuicao.py aplicar
    python corrigir_cartao_atribuicao.py reavaliar

Variáveis de ambiente necessárias: API_FOOTBALL_KEY, DATABASE_URL
"""

import os
import sys
from datetime import datetime

import psycopg2

from popular_banco import (
    buscar_eventos,
    buscar_estatisticas_jogadores,
    get_or_create_jogador,
)
from avaliacao import avaliar_resultado

DATABASE_URL = os.environ["DATABASE_URL"]

# Alvo, resolvido por data + nomes (ILIKE) pra não depender de decorar id.
ALVO = {
    "rotulo": "Chapecoense x São Paulo (23/08) - cartão atribuído ao time errado",
    "data": "2026-08-23",
    "mandante": "Chape",
    "visitante": "Paulo",
}

# A correção declarada, conferida contra a linha do tempo do ge. Só é
# usada no CAMINHO B (quando a API insiste no erro). Escrita como dado, e
# não espalhada pelo código, pra ficar auditável.
CORRECAO = {
    # como está errado no banco hoje
    "errado": {"jogador_nome": "Luciano", "minuto": 72},
    # como deveria estar
    "certo": {"jogador_nome": "Anderson", "minuto": 71, "time": "Chapecoense"},
    # o que a fonte externa diz do resultado final por time
    "esperado_por_time": {"Chapecoense": 4, "Sao Paulo": 0},
}


def cabecalho(fase):
    """Primeira linha da saída identifica a fase. Existe porque no Railway
    a fase vem do Custom Start Command, e já houve confusão entre o log do
    deploy novo e o do anterior."""
    print("=" * 92)
    print(f"corrigir_cartao_atribuicao.py | FASE: {fase.upper()} | "
          f"iniciado em {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"argv recebido: {sys.argv}")
    if fase == "verificar":
        print("Esta fase NÃO grava nada no banco.")
    else:
        print("Esta fase GRAVA no banco.")
    print("=" * 92)


# ---------- resolução ----------

def resolver_alvo(cur):
    cur.execute(
        """
        SELECT DISTINCT j.fixture_id_api
        FROM jogos j
        JOIN times m ON m.id = j.mandante_id
        JOIN times v ON v.id = j.visitante_id
        WHERE j.data_jogo = %s AND m.nome ILIKE %s AND v.nome ILIKE %s
        """,
        (ALVO["data"], f"%{ALVO['mandante']}%", f"%{ALVO['visitante']}%"),
    )
    fixtures = [r[0] for r in cur.fetchall()]
    if len(fixtures) != 1:
        raise RuntimeError(
            f"Alvo resolveu pra {len(fixtures)} fixture(s): {fixtures}. "
            "Ajuste ALVO antes de continuar."
        )
    fixture_id = fixtures[0]

    cur.execute(
        """
        SELECT j.id, t.nome, t.api_football_team_id
        FROM jogos j
        LEFT JOIN times t ON t.id = j.nosso_time_id
        WHERE j.fixture_id_api = %s ORDER BY j.id
        """,
        (fixture_id,),
    )
    return fixture_id, cur.fetchall()


# ---------- leitura do banco ----------

def cartoes_do_banco(cur, jogo_id, nome_nosso_time):
    """`lado` em `cartoes` é relativo ao NOSSO time ('mandante' = nosso
    time), não ao mando real. Traduz pra nome antes de imprimir."""
    cur.execute(
        """
        SELECT c.lado, c.minuto, c.cor, jg.nome, jg.api_football_id, j.adversario
        FROM cartoes c
        JOIN jogos j ON j.id = c.jogo_id
        LEFT JOIN jogadores jg ON jg.id = c.jogador_id
        WHERE c.jogo_id = %s
        ORDER BY c.minuto NULLS LAST, c.id
        """,
        (jogo_id,),
    )
    saida = []
    for lado, minuto, cor, nome, api_id, adversario in cur.fetchall():
        time = nome_nosso_time if lado == "mandante" else adversario
        saida.append((time, minuto, cor, nome, api_id))
    return saida


def cartoes_da_estatistica_de_jogador(cur, jogo_id):
    """FONTE INDEPENDENTE: /fixtures/players, já coletada. Aqui `lado` é o
    mando REAL (salvar_estatisticas_jogadores compara com home_team_id),
    diferente de `cartoes`."""
    cur.execute(
        """
        SELECT jej.lado, jg.nome, jej.cartao_amarelo, jej.cartao_vermelho
        FROM jogador_estatisticas_jogo jej
        LEFT JOIN jogadores jg ON jg.id = jej.jogador_id
        WHERE jej.jogo_id = %s
          AND (COALESCE(jej.cartao_amarelo, 0) + COALESCE(jej.cartao_vermelho, 0)) > 0
        ORDER BY jej.lado, jg.nome
        """,
        (jogo_id,),
    )
    return cur.fetchall()


# ---------- leitura da API ----------

def cartoes_da_api_eventos(eventos):
    saida = []
    for ev in eventos:
        if ev.get("type") != "Card":
            continue
        nome = (ev.get("player") or {}).get("name")
        api_id = (ev.get("player") or {}).get("id")
        saida.append((ev["team"]["name"], ev["team"]["id"], ev["time"]["elapsed"],
                      ev.get("detail"), nome, api_id))
    return sorted(saida, key=lambda x: (x[2] if x[2] is not None else 999))


def cartoes_da_api_jogadores(dados_jogadores):
    """Mesma leitura que salvar_estatisticas_jogadores faz do bloco
    `cards`, só que sem gravar."""
    saida = []
    for bloco_time in dados_jogadores:
        for bloco_jogador in bloco_time.get("players") or []:
            stats = bloco_jogador.get("statistics") or []
            if not stats:
                continue
            cards = stats[0].get("cards") or {}
            amarelo = cards.get("yellow") or 0
            vermelho = cards.get("red") or 0
            if amarelo or vermelho:
                saida.append((bloco_time["team"]["name"],
                              bloco_jogador["player"]["name"],
                              bloco_jogador["player"].get("id"),
                              amarelo, vermelho))
    return sorted(saida)


def contar_por_time(lista_api_eventos):
    contagem = {}
    for time_nome, _tid, _min, _det, _nome, _pid in lista_api_eventos:
        contagem[time_nome] = contagem.get(time_nome, 0) + 1
    return contagem


def api_ja_corrigiu(lista_api_eventos):
    """A API se corrigiu se nenhum cartão estiver atribuído ao São Paulo
    e o total continuar sendo 4."""
    contagem = contar_por_time(lista_api_eventos)
    total = sum(contagem.values())
    do_sao_paulo = sum(v for k, v in contagem.items() if "Paulo" in k)
    return total == 4 and do_sao_paulo == 0


# ---------- FASE 1 ----------

def fase_verificar():
    cabecalho("verificar")
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    try:
        fixture_id, perspectivas = resolver_alvo(cur)
        print(f"\n{ALVO['rotulo']}")
        print(f"fixture_id_api {fixture_id} | {len(perspectivas)} perspectiva(s)")
        print(f"esperado (fonte externa): {CORRECAO['esperado_por_time']}\n")

        jogo_id_ref, nome_ref, _api_ref = perspectivas[0]

        print("-- (a) TABELA `cartoes` HOJE  [origem: /fixtures/events] --")
        for time, minuto, cor, nome, api_id in cartoes_do_banco(cur, jogo_id_ref, nome_ref):
            print(f"    {minuto}' {cor:9} {time:18} {nome} (api_id {api_id})")

        print("\n-- (b) `jogador_estatisticas_jogo` HOJE  [origem: /fixtures/players] --")
        print("    FONTE INDEPENDENTE do mesmo fato - se discordar de (a),")
        print("    a contradição já estava dentro do banco.")
        linhas_b = cartoes_da_estatistica_de_jogador(cur, jogo_id_ref)
        if not linhas_b:
            print("    (nenhum cartão registrado nessa fonte - ela pode não ter sido")
            print("     coletada, ou vir com NULL, que o projeto trata como zero)")
        for lado, nome, amarelo, vermelho in linhas_b:
            print(f"    lado_real={lado:10} {nome:22} amarelo={amarelo} vermelho={vermelho}")

        eventos = buscar_eventos(fixture_id)
        api_eventos = cartoes_da_api_eventos(eventos)
        print("\n-- (c) /fixtures/events AGORA --")
        for time_nome, _tid, minuto, detalhe, nome, api_id in api_eventos:
            print(f"    {minuto}' {time_nome:18} {nome} (api_id {api_id}) [{detalhe}]")
        print(f"    contagem por time: {contar_por_time(api_eventos)}")

        dados_jog = buscar_estatisticas_jogadores(fixture_id)
        print("\n-- (d) /fixtures/players AGORA --")
        for time_nome, nome, api_id, amarelo, vermelho in cartoes_da_api_jogadores(dados_jog):
            print(f"    {time_nome:18} {nome:22} (api_id {api_id}) "
                  f"amarelo={amarelo} vermelho={vermelho}")

        print("\n" + "=" * 92)
        if api_ja_corrigiu(api_eventos):
            print("CAMINHO A (refetch): a API já se corrigiu. A fase 2 vai APAGAR os")
            print("cartões desse jogo e regravar direto do /fixtures/events.")
            print("Mais seguro que a correção manual - o dado vem inteiro da fonte.")
        else:
            print("CAMINHO B (manual): a API AINDA atribui o cartão ao time errado.")
            print("A fase 2 vai aplicar a correção declarada em CORRECAO:")
            print(f"    remover: {CORRECAO['errado']}")
            print(f"    inserir: {CORRECAO['certo']}")
            print("O api_football_id do Anderson será resolvido a partir do próprio")
            print("/fixtures/players desse jogo - nunca por comparação de nome solta.")
            print("Se ele não aparecer lá, a fase 2 ABORTA em vez de chutar.")
        print("\nNada foi gravado. Se fizer sentido:")
        print("  python corrigir_cartao_atribuicao.py aplicar")

    finally:
        cur.close()
        conn.close()


# ---------- FASE 2 ----------

def achar_api_id_do_certo(dados_jogadores, nome_procurado, nome_time):
    """Procura o jogador no bloco do time certo do /fixtures/players.
    Devolve (api_id, nome_como_na_api) ou (None, None).

    Casar por nome aqui é aceitável porque o universo é o elenco de UM
    time em UM jogo, e o resultado é impresso pra conferência - bem
    diferente de casar nome entre duas APIs, que é o que já gerou jogador
    duplicado neste projeto."""
    candidatos = []
    for bloco_time in dados_jogadores:
        if nome_time.lower() not in bloco_time["team"]["name"].lower():
            continue
        for bloco_jogador in bloco_time.get("players") or []:
            nome = bloco_jogador["player"]["name"] or ""
            if nome_procurado.lower() in nome.lower():
                candidatos.append((bloco_jogador["player"].get("id"), nome))
    if len(candidatos) != 1:
        return None, candidatos
    return candidatos[0], candidatos


def fase_aplicar():
    cabecalho("aplicar")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        fixture_id, perspectivas = resolver_alvo(cur)
        eventos = buscar_eventos(fixture_id)
        api_eventos = cartoes_da_api_eventos(eventos)

        if api_ja_corrigiu(api_eventos):
            # ---------- CAMINHO A ----------
            print("CAMINHO A (refetch) - a API se corrigiu.\n")
            for jogo_id, nome, nosso_api_id in perspectivas:
                if nosso_api_id is None:
                    raise RuntimeError(
                        f"jogo_id {jogo_id} ({nome}) sem api_football_team_id do nosso "
                        "time - sem isso não dá pra decidir o `lado`. Parando sem commitar."
                    )
                antes = len(cartoes_do_banco(cur, jogo_id, nome))
                cur.execute("DELETE FROM cartoes WHERE jogo_id = %s", (jogo_id,))
                salvos = 0
                for ev in eventos:
                    if ev.get("type") != "Card":
                        continue
                    nome_jog = (ev.get("player") or {}).get("name")
                    if not nome_jog:
                        continue
                    jogador_id = get_or_create_jogador(
                        cur, (ev.get("player") or {}).get("id"), nome_jog)
                    minuto = ev["time"]["elapsed"]
                    cur.execute(
                        """INSERT INTO cartoes (jogo_id, jogador_id, lado, cor, minuto, periodo)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (jogo_id, jogador_id,
                         "mandante" if ev["team"]["id"] == nosso_api_id else "visitante",
                         "amarelo" if "Yellow" in (ev.get("detail") or "") else "vermelho",
                         minuto,
                         "1_tempo" if (minuto or 0) <= 45 else "2_tempo"),
                    )
                    salvos += 1
                print(f"  jogo_id {jogo_id} ({nome}): {antes} -> {salvos} cartão(ões)")

        else:
            # ---------- CAMINHO B ----------
            print("CAMINHO B (manual) - a API insiste no erro.\n")
            dados_jog = buscar_estatisticas_jogadores(fixture_id)
            achado, candidatos = achar_api_id_do_certo(
                dados_jog, CORRECAO["certo"]["jogador_nome"], CORRECAO["certo"]["time"])
            if achado is None:
                raise RuntimeError(
                    f"Não deu pra identificar '{CORRECAO['certo']['jogador_nome']}' "
                    f"no elenco do {CORRECAO['certo']['time']} de forma única. "
                    f"Candidatos: {candidatos}. Parando sem commitar."
                )
            api_id_certo, nome_na_api = achado
            print(f"  jogador certo identificado: {nome_na_api} (api_id {api_id_certo})")

            for jogo_id, nome_nosso, _api in perspectivas:
                # o cartão errado, localizado pelo nome do jogador errado
                cur.execute(
                    """SELECT c.id, c.lado, c.minuto
                       FROM cartoes c JOIN jogadores jg ON jg.id = c.jogador_id
                       WHERE c.jogo_id = %s AND jg.nome ILIKE %s""",
                    (jogo_id, f"%{CORRECAO['errado']['jogador_nome']}%"),
                )
                alvos = cur.fetchall()
                if len(alvos) != 1:
                    raise RuntimeError(
                        f"jogo_id {jogo_id}: esperava 1 cartão de "
                        f"'{CORRECAO['errado']['jogador_nome']}', achei {len(alvos)}. "
                        "Parando sem commitar."
                    )
                cartao_id, lado_antigo, minuto_antigo = alvos[0]

                jogador_id_certo = get_or_create_jogador(cur, api_id_certo, nome_na_api)
                # `lado` é relativo ao NOSSO time nessa perspectiva
                lado_novo = "mandante" if CORRECAO["certo"]["time"].lower() in \
                    (nome_nosso or "").lower() else "visitante"

                cur.execute(
                    """UPDATE cartoes
                       SET jogador_id = %s, lado = %s, minuto = %s, periodo = %s
                       WHERE id = %s""",
                    (jogador_id_certo, lado_novo, CORRECAO["certo"]["minuto"],
                     "1_tempo" if CORRECAO["certo"]["minuto"] <= 45 else "2_tempo",
                     cartao_id),
                )
                print(f"  jogo_id {jogo_id} ({nome_nosso}): cartão #{cartao_id} "
                      f"{minuto_antigo}' lado={lado_antigo} -> "
                      f"{CORRECAO['certo']['minuto']}' lado={lado_novo}, "
                      f"jogador -> {nome_na_api}")

        # conferência final, igual nas duas pontas
        print("\n-- CONFERÊNCIA (contagem por time depois da correção) --")
        jogo_id_ref, nome_ref, _ = perspectivas[0]
        contagem = {}
        for time, _min, _cor, _nome, _api in cartoes_do_banco(cur, jogo_id_ref, nome_ref):
            contagem[time] = contagem.get(time, 0) + 1
        print(f"    banco agora: {contagem}")
        print(f"    esperado:    {CORRECAO['esperado_por_time']}")

        print("\n-- O QUE NÃO FOI TOCADO --")
        print("    `jogador_estatisticas_jogo` fica como está. Ela vem de outro")
        print("    endpoint (/fixtures/players) e alimenta outras estatísticas.")
        print("    A avaliação de cartão - tanto de TIME quanto de JOGADOR - lê da")
        print("    tabela `cartoes`, que é a corrigida aqui. Se o bloco (b) da fase")
        print("    `verificar` mostrar que /fixtures/players também atribuiu o cartão")
        print("    ao jogador errado, isso é ruído nas frequências de jogador, não na")
        print("    avaliação - e vale tratar junto com o detector de coerência.")

        conn.commit()
        print("\n✅ Commitado. Confira a conferência acima antes de rodar:")
        print("  python corrigir_cartao_atribuicao.py reavaliar")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Erro, nada foi salvo (rollback): {e}")
        raise
    finally:
        cur.close()
        conn.close()


# ---------- FASE 3 ----------

def fase_reavaliar():
    cabecalho("reavaliar")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        _fixture_id, perspectivas = resolver_alvo(cur)
        jogo_ids = [p[0] for p in perspectivas]

        cur.execute(
            """SELECT id, jogo_id, jogador_id, tipo_padrao, descricao, linha,
                      direcao, resultado
               FROM historico_recomendacoes WHERE jogo_id = ANY(%s) ORDER BY id""",
            (jogo_ids,),
        )
        linhas = cur.fetchall()
        mudou = 0
        for rec_id, jogo_id, jogador_id, tipo_padrao, descricao, linha, direcao, antigo in linhas:
            novo = avaliar_resultado(cur, tipo_padrao, jogador_id, jogo_id,
                                     linha, descricao, direcao)
            if novo != antigo:
                cur.execute("UPDATE historico_recomendacoes SET resultado = %s WHERE id = %s",
                            (novo, rec_id))
                print(f"  #{rec_id} [{tipo_padrao}] \"{descricao}\": {antigo} -> {novo}")
                mudou += 1

        conn.commit()
        print(f"\n✅ {mudou} de {len(linhas)} linha(s) corrigida(s) e commitada(s).")
        print("   Esperado: 1 linha (\"Cartões - Sao Paulo - Menos de 0.5\", errou -> acertou).")
        print("   Se mudar mais que isso, confira antes de seguir.")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Erro, nada foi salvo (rollback): {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) == 1 and os.environ.get("FASE"):
        sys.argv.append(os.environ["FASE"].strip().lower())

    if len(sys.argv) != 2 or sys.argv[1] not in ("verificar", "aplicar", "reavaliar"):
        print("Uso:")
        print("  python corrigir_cartao_atribuicao.py verificar   (só lê)")
        print("  python corrigir_cartao_atribuicao.py aplicar     (corrige)")
        print("  python corrigir_cartao_atribuicao.py reavaliar   (reavalia o histórico)")
        print("")
        print("Alternativas que não dependem do argumento:")
        print("  FASE=aplicar como variável de ambiente do serviço")
        print("  python -c \"import corrigir_cartao_atribuicao as m; m.fase_aplicar()\"")
        print(f"(argv recebido: {sys.argv})")
        sys.exit(1)

    if sys.argv[1] == "verificar":
        fase_verificar()
    elif sys.argv[1] == "aplicar":
        fase_aplicar()
    else:
        fase_reavaliar()
