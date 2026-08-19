"""
Script PONTUAL (não faz parte do encadeamento diário do cron) - resolve a
última pendência do jogo Athletico-PR x RB Bragantino, 15/08/2026:
a estatística de JOGADOR, que continua contaminada pelo bug de "estatística
coletada no meio do jogo" (a API-Football marcou o fixture como "FT" antes
do endpoint de estatísticas terminar de agregar os números finais).

POR QUE UM SCRIPT NOVO, E NÃO O `corrigir_estatisticas_pontual.py`:
    Aquele script apaga e regrava as DUAS coisas ao mesmo tempo
    (estatisticas_jogo E jogador_estatisticas_jogo). Como o nível de TIME
    desse jogo já foi corrigido MANUALMENTE (números conferidos no
    Sofascore/Ogol, via corrigir_manual_athletico_bragantino.py), rodar ele
    hoje desfaria esse trabalho - regravaria o dado parcial da API por
    cima da correção manual. Este script aqui NUNCA toca em
    `estatisticas_jogo`: mexe só no nível de jogador.

DUAS FASES SEPARADAS (rodar uma de cada vez, de propósito):

  FASE 1 (`verificar`) - NÃO GRAVA NADA NO BANCO:
    Busca a estatística de jogador na API-Football e imprime lado a lado
    com o que está salvo hoje, marcando cada diferença. Serve pra responder
    a pergunta que decide tudo: "a API-Football finalmente agregou os
    números certos, 3 dias depois do jogo?"
      - Se os números novos baterem com o Sofascore -> roda a fase 2,
        resolvido sem precisar de fonte manual nenhuma.
      - Se vierem IGUAIS aos salvos (ou seja: continuam errados) -> a API
        simplesmente não vai corrigir esse fixture. Não rode a fase 2;
        nesse caso a correção teria que ser manual, jogador a jogador.
    Custo: 1 chamada de API. Nenhuma escrita, nenhum risco.

  FASE 2 (`aplicar`):
    Apaga as linhas de `jogador_estatisticas_jogo` desse jogo (nos 2
    jogo_ids que representam ele) e regrava com o dado novo da API.
    Continua sem tocar em `estatisticas_jogo`.

Depois da fase 2, rodar a reavaliação que já existe, pra o histórico e a
banca acompanharem a correção:
    python corrigir_estatisticas_pontual.py reavaliar

Ordem completa:
    python corrigir_jogadores_athletico_bragantino.py verificar
    ... conferir contra o Sofascore ...
    python corrigir_jogadores_athletico_bragantino.py aplicar
    python corrigir_estatisticas_pontual.py reavaliar

Variáveis de ambiente necessárias (mesmas do popular_banco.py/app.py):
  - API_FOOTBALL_KEY
  - DATABASE_URL
"""

import os
import sys
import psycopg2

from popular_banco import (
    buscar_estatisticas_jogadores,
    salvar_estatisticas_jogadores,
)

DATABASE_URL = os.environ["DATABASE_URL"]

# Athletico-PR x RB Bragantino, 15/08/2026. Os dois times são rastreados,
# então o mesmo jogo real tem 2 linhas em `jogos` (uma por perspectiva) -
# a correção precisa ser aplicada nas duas, senão as telas mostram números
# diferentes dependendo de por qual time você chegou nelas.
FIXTURE_ID = 1492330
JOGO_IDS = [34, 1492330]

# Estatísticas que realmente importam pra alguma recomendação hoje - são
# as que aparecem na comparação da fase 1. As outras colunas continuam
# sendo gravadas normalmente na fase 2, só não poluem a tela da conferência.
COLUNAS_COMPARADAS = [
    ("chutes", "chutes"),
    ("chutes_no_gol", "chutes no gol"),
    ("faltas_cometidas", "faltas cometidas"),
    ("desarmes", "desarmes"),
    ("impedimentos", "impedimentos"),
]


def buscar_home_team_id(cur, jogo_id):
    """api_football_team_id do mandante desse jogo_id - direto do banco,
    sem gastar chamada de API (o dado já está lá)."""
    cur.execute(
        """
        SELECT tm.api_football_team_id
        FROM jogos j JOIN times tm ON tm.id = j.mandante_id
        WHERE j.id = %s
        """,
        (jogo_id,),
    )
    row = cur.fetchone()
    if row is None or row[0] is None:
        raise RuntimeError(f"Não encontrei api_football_team_id do mandante do jogo_id {jogo_id}.")
    return row[0]


def buscar_salvo_por_jogador(cur, jogo_id):
    """{nome_do_jogador: {coluna: valor}} do que está salvo hoje."""
    colunas = [c for c, _ in COLUNAS_COMPARADAS]
    cur.execute(
        f"""
        SELECT jg.nome, {', '.join('jej.' + c for c in colunas)}
        FROM jogador_estatisticas_jogo jej
        JOIN jogadores jg ON jg.id = jej.jogador_id
        WHERE jej.jogo_id = %s
        ORDER BY jg.nome
        """,
        (jogo_id,),
    )
    salvo = {}
    for linha in cur.fetchall():
        nome = linha[0]
        salvo[nome] = {coluna: linha[i + 1] for i, coluna in enumerate(colunas)}
    return salvo


def extrair_da_api(dados_jogadores):
    """{nome_do_jogador: {coluna: valor}} do que a API está mandando AGORA.
    Mesma leitura de campos que salvar_estatisticas_jogadores faz - só que
    aqui é só pra comparar na tela, sem gravar nada."""
    da_api = {}
    for bloco_time in dados_jogadores:
        for bloco_jogador in bloco_time.get("players") or []:
            nome = (bloco_jogador.get("player") or {}).get("name")
            if not nome:
                continue
            stats_lista = bloco_jogador.get("statistics") or []
            if not stats_lista:
                continue
            s = stats_lista[0]
            if (s.get("games") or {}).get("minutes") is None:
                continue  # não entrou em campo

            shots = s.get("shots") or {}
            tackles = s.get("tackles") or {}
            fouls = s.get("fouls") or {}

            da_api[nome] = {
                "chutes": shots.get("total"),
                "chutes_no_gol": shots.get("on"),
                "faltas_cometidas": fouls.get("committed"),
                "desarmes": tackles.get("total"),
                "impedimentos": s.get("offsides"),
            }
    return da_api


def fase_verificar():
    """Compara o salvo x o que a API manda agora. NÃO grava nada."""
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    try:
        print(f"Buscando estatística de jogador do fixture {FIXTURE_ID} na API-Football...")
        dados_jogadores = buscar_estatisticas_jogadores(FIXTURE_ID)
        if not dados_jogadores:
            print("\n❌ A API não devolveu estatística de jogador nenhuma pra esse fixture.")
            print("   Nada a fazer por aqui - a correção teria que ser manual.")
            return

        da_api = extrair_da_api(dados_jogadores)
        # os 2 jogo_ids devem ter exatamente o mesmo conteúdo de jogador -
        # basta comparar contra um deles (o outro é a mesma coisa por outra
        # perspectiva).
        salvo = buscar_salvo_por_jogador(cur, JOGO_IDS[0])

        print(f"\nJogadores no banco: {len(salvo)} | Jogadores na resposta da API: {len(da_api)}")
        print("\n" + "=" * 78)
        print("COMPARAÇÃO (só linhas com diferença aparecem)")
        print("=" * 78)

        nomes = sorted(set(salvo) | set(da_api))
        diferencas = 0
        for nome in nomes:
            antigo = salvo.get(nome)
            novo = da_api.get(nome)

            if antigo is None:
                print(f"\n  {nome}: só existe na API (não está salvo no banco)")
                diferencas += 1
                continue
            if novo is None:
                print(f"\n  {nome}: só existe no banco (a API não mandou agora)")
                diferencas += 1
                continue

            mudou = []
            for coluna, rotulo in COLUNAS_COMPARADAS:
                v_antigo = antigo.get(coluna)
                v_novo = novo.get(coluna)
                # a API manda null pra zero em várias estatísticas - tratar
                # os dois como equivalentes evita "diferença" que não é
                # diferença nenhuma (mesmo motivo do COALESCE no projeto).
                a = 0 if v_antigo is None else int(v_antigo)
                b = 0 if v_novo is None else int(v_novo)
                if a != b:
                    mudou.append(f"{rotulo}: {a} -> {b}")

            if mudou:
                print(f"\n  {nome}")
                for m in mudou:
                    print(f"      {m}")
                diferencas += 1

        print("\n" + "=" * 78)
        if diferencas == 0:
            print("⚠️  NENHUMA DIFERENÇA. A API-Football está devolvendo exatamente o")
            print("    mesmo dado que já está salvo - ou seja, ela NÃO corrigiu esse")
            print("    fixture, mesmo 3 dias depois do jogo.")
            print("    NÃO rode a fase `aplicar` (não mudaria nada). A correção teria")
            print("    que ser manual, jogador a jogador.")
        else:
            print(f"✅ {diferencas} jogador(es) com diferença. A API mudou o dado desde a")
            print("    última coleta.")
            print("    CONFIRA os números da direita (->) contra o Sofascore do jogo.")
            print("    Se baterem, rode:  python corrigir_jogadores_athletico_bragantino.py aplicar")
            print("    Lembrete de referência: Kevin Viveros deve ter 1 chute no gol.")

    finally:
        cur.close()
        conn.close()


def fase_aplicar():
    """Apaga e regrava SÓ a estatística de jogador. Nunca toca em
    `estatisticas_jogo` (nível de time), que já foi corrigido à mão."""
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        print(f"Buscando estatística de jogador do fixture {FIXTURE_ID} na API-Football...")
        dados_jogadores = buscar_estatisticas_jogadores(FIXTURE_ID)
        if not dados_jogadores:
            raise RuntimeError(
                "A API não devolveu estatística de jogador pro fixture - parando sem "
                "commitar. Rode a fase `verificar` antes."
            )

        for jogo_id in JOGO_IDS:
            cur.execute("DELETE FROM jogador_estatisticas_jogo WHERE jogo_id = %s", (jogo_id,))
            home_team_id = buscar_home_team_id(cur, jogo_id)
            salvos = salvar_estatisticas_jogadores(cur, jogo_id, dados_jogadores, home_team_id)
            print(f"  jogo_id {jogo_id}: {salvos} jogador(es) regravados.")

        conn.commit()
        print("\n✅ Estatística de jogador regravada e commitada.")
        print("   `estatisticas_jogo` (nível de time) NÃO foi tocada - a correção")
        print("   manual de posse/escanteio/falta/chute continua intacta.")
        print("\n   Próximo passo, pra o histórico e a banca acompanharem:")
        print("       python corrigir_estatisticas_pontual.py reavaliar")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Erro, nada foi salvo (rollback): {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("verificar", "aplicar"):
        print("Uso:")
        print("  python corrigir_jogadores_athletico_bragantino.py verificar   "
              "(só lê e compara, não grava nada)")
        print("  python corrigir_jogadores_athletico_bragantino.py aplicar     "
              "(regrava a estatística de jogador)")
        sys.exit(1)

    if sys.argv[1] == "verificar":
        fase_verificar()
    else:
        fase_aplicar()
