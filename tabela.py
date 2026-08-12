"""
Módulo compartilhado pra reconstruir a tabela do Brasileirão em QUALQUER
rodada do passado, a partir do calendário coletado por popular_tabela.py
(tabela `jogos_liga`). Usado pelas Fases C (comportamento por zona) e D
(assimetria de contexto entre dois jogos) - ver documentação do projeto.

Critério de desempate usado: pontos, depois vitórias, depois saldo de
gols, depois gols pró. NÃO é o critério oficial completo da CBF (que
inclui confronto direto e cartões antes do saldo/gols pró em alguns
casos) - pra decidir "zona da tabela" (G4/Z4/meio), essa aproximação é
suficiente; nunca é usado pra afirmar a posição oficial exata em uma
disputa de ponto a ponto.

ZONA_G4 = 4 primeiras posições (Libertadores direta)
ZONA_Z4 = 4 últimas posições (rebaixamento)
resto   = meio de tabela
"""

TOTAL_TIMES_LIGA = 20
TAMANHO_ZONA_G4 = 4
TAMANHO_ZONA_Z4 = 4


def calcular_tabela(cur, temporada, rodada_numero):
    """Retorna a tabela reconstruída como estava depois da rodada
    `rodada_numero` daquela temporada - lista de dicts, já ordenada da 1ª
    à última posição. Só considera jogos com status de FATO finalizado
    (FT/AET/PEN) - jogo adiado/cancelado não entra na conta."""
    cur.execute(
        """
        WITH jogos_considerados AS (
            SELECT * FROM jogos_liga
            WHERE temporada = %s AND rodada_numero <= %s
              AND status IN ('FT', 'AET', 'PEN')
        ),
        resultados AS (
            SELECT mandante_api_id AS time_api_id, mandante_nome AS nome,
                   CASE WHEN placar_mandante > placar_visitante THEN 3
                        WHEN placar_mandante = placar_visitante THEN 1 ELSE 0 END AS pontos,
                   CASE WHEN placar_mandante > placar_visitante THEN 1 ELSE 0 END AS vitorias,
                   CASE WHEN placar_mandante = placar_visitante THEN 1 ELSE 0 END AS empates,
                   CASE WHEN placar_mandante < placar_visitante THEN 1 ELSE 0 END AS derrotas,
                   placar_mandante AS gols_pro, placar_visitante AS gols_contra
            FROM jogos_considerados
            UNION ALL
            SELECT visitante_api_id, visitante_nome,
                   CASE WHEN placar_visitante > placar_mandante THEN 3
                        WHEN placar_visitante = placar_mandante THEN 1 ELSE 0 END,
                   CASE WHEN placar_visitante > placar_mandante THEN 1 ELSE 0 END,
                   CASE WHEN placar_visitante = placar_mandante THEN 1 ELSE 0 END,
                   CASE WHEN placar_visitante < placar_mandante THEN 1 ELSE 0 END,
                   placar_visitante, placar_mandante
            FROM jogos_considerados
        )
        SELECT time_api_id, MAX(nome) AS nome,
               SUM(pontos) AS pontos, SUM(vitorias) AS vitorias,
               SUM(empates) AS empates, SUM(derrotas) AS derrotas,
               SUM(gols_pro) AS gols_pro, SUM(gols_contra) AS gols_contra,
               SUM(gols_pro) - SUM(gols_contra) AS saldo, COUNT(*) AS jogos
        FROM resultados
        GROUP BY time_api_id
        ORDER BY pontos DESC, vitorias DESC, saldo DESC, gols_pro DESC
        """,
        (temporada, rodada_numero),
    )

    tabela = []
    for posicao, row in enumerate(cur.fetchall(), start=1):
        (time_api_id, nome, pontos, vitorias, empates, derrotas,
         gols_pro, gols_contra, saldo, jogos) = row
        tabela.append({
            "posicao": posicao,
            "time_api_id": time_api_id,
            "nome": nome,
            "pontos": pontos,
            "vitorias": vitorias,
            "empates": empates,
            "derrotas": derrotas,
            "gols_pro": gols_pro,
            "gols_contra": gols_contra,
            "saldo": saldo,
            "jogos": jogos,
        })

    total_times = len(tabela)
    for item in tabela:
        item["zona"] = classificar_zona(item["posicao"], total_times)

    return tabela


def classificar_zona(posicao, total_times=TOTAL_TIMES_LIGA):
    """G4 = 4 primeiras posições, Z4 = 4 últimas, resto = meio de tabela.
    `total_times` é o tamanho real da tabela naquele momento (pode ser
    menor que 20 numa rodada muito inicial, se algum time ainda não jogou
    nada - tratado à parte, não deveria acontecer em rodada >= 1 de uma
    temporada completa, mas protege contra dado incompleto)."""
    if posicao <= TAMANHO_ZONA_G4:
        return "g4"
    if posicao > total_times - TAMANHO_ZONA_Z4:
        return "z4"
    return "meio"


def buscar_posicao_time(cur, temporada, rodada_numero, api_football_team_id):
    """Atalho pra pegar só a linha de UM time na tabela reconstruída
    dessa rodada - devolve None se o time não tiver jogado nada até essa
    rodada (dado insuficiente, ou time que não disputou essa temporada)."""
    tabela = calcular_tabela(cur, temporada, rodada_numero)
    for item in tabela:
        if item["time_api_id"] == api_football_team_id:
            return item
    return None


def classificar_forca_adversario(posicao, total_times=TOTAL_TIMES_LIGA):
    """NOVO (Fase D - Parte 2): classifica o adversário como 'forte'
    (5 primeiras posições), 'fraco' (5 últimas) ou 'medio' (resto) -
    medida simples de propósito: só posição na tabela, sem inventar
    índice de força mais elaborado (decisão documentada)."""
    if posicao <= 5:
        return "forte"
    if posicao > total_times - 5:
        return "fraco"
    return "medio"


def calcular_contexto_jogo(cur, api_football_team_id, adversario_api_team_id, temporada, rodada_numero):
    """NOVO (Fase D - Parte 2, só exibição): monta o "contexto" de um jogo
    específico - onde o nosso time está na tabela, a distância até o
    objetivo mais próximo (G4 se estiver de fora, ou até a saída seguro do
    Z4 se estiver dentro), e a força do adversário (posição dele). Serve
    pra comparar dois jogos da mesma rodada lado a lado (ex: "Palmeiras a
    3 pontos do G4 contra o Mirassol (fraco)" vs "Flamengo líder contra o
    Cruzeiro (forte)") - não calcula nada sozinho sobre qual jogo é "mais
    fácil", só organiza o dado pra quem está olhando decidir.
    Devolve None se faltar dado (mesmas condições de calcular_jogo_morto)."""
    rodada_anterior = rodada_numero - 1
    if rodada_anterior < 1:
        return None

    tab = calcular_tabela(cur, temporada, rodada_anterior)
    if not tab:
        return None

    item_nosso = next((t for t in tab if t["time_api_id"] == api_football_team_id), None)
    item_adversario = next((t for t in tab if t["time_api_id"] == adversario_api_team_id), None)
    if item_nosso is None:
        return None

    total_times = len(tab)
    if item_nosso["zona"] == "g4":
        distancia_objetivo = 0
        objetivo = "manter no G4"
    else:
        quarto_colocado = next((t for t in tab if t["posicao"] == TAMANHO_ZONA_G4), None)
        distancia_objetivo = (quarto_colocado["pontos"] - item_nosso["pontos"]) if quarto_colocado else None
        objetivo = "alcançar o G4"

    distancia_z4 = None
    if item_nosso["zona"] == "z4":
        posicao_referencia = total_times - TAMANHO_ZONA_Z4  # primeiro posto seguro (16º de 20)
        referencia = next((t for t in tab if t["posicao"] == posicao_referencia), None)
        distancia_z4 = (item_nosso["pontos"] - referencia["pontos"]) if referencia else None

    return {
        "posicao": item_nosso["posicao"],
        "pontos": item_nosso["pontos"],
        "zona": item_nosso["zona"],
        "objetivo": objetivo,
        "distancia_objetivo": distancia_objetivo,
        "distancia_z4": distancia_z4,
        "adversario_posicao": item_adversario["posicao"] if item_adversario else None,
        "adversario_forca": classificar_forca_adversario(item_adversario["posicao"], total_times) if item_adversario else None,
    }


TOTAL_RODADAS_LIGA = 38

# NOVO: só considera "jogo morto" dentro das últimas N rodadas do
# campeonato - mesmo quando a matemática já permite concluir isso mais
# cedo (o que pode acontecer a partir de ~19-20 rodadas jogadas, num
# cenário bem extremo de um time vencendo tudo enquanto outro perde
# tudo), a intenção do rótulo é sinalizar "reta final decidida", não
# "tecnicamente já dava pra saber lá atrás" - dispara cedo demais tira a
# credibilidade do aviso.
RODADAS_RESTANTES_MAXIMO_PARA_JOGO_MORTO = 3


def calcular_jogo_morto(cur, api_football_team_id, temporada, rodada_numero):
    """NOVO (Fase D - Parte 1, só rótulo informativo, NÃO afeta nenhuma
    recomendação/VE): diz se, entrando nessa rodada, o time já não tem
    mais nada em jogo - matematicamente não pode mais alcançar o G4 nem
    cair no Z4, mesmo no cenário mais favorável/desfavorável possível daqui
    pra frente.

    Aproximação simples de propósito: só olha o 4º colocado (corte do G4)
    e o time na primeira posição do Z4 (corte do rebaixamento) - não
    considera ainda vagas de Libertadores/Sul-Americana (que mudam de
    tamanho a cada temporada, dependendo de outras competições - decisão
    documentada de deixar pra uma fase futura) nem os jogos restantes dos
    times de referência (assume o cenário mais simples: só olha os pontos
    ATUAIS deles, sem projetar o que ainda podem ganhar). Isso torna o
    cálculo um pouco mais "generoso" (marca como morto um pouco depois do
    que seria o rigor matemático completo) - aceitável pra um rótulo
    informativo, não pra uma trava definitiva.

    Devolve None se não tiver dado suficiente ainda (rodada muito no
    início, temporada sem jogos concluídos, ou campeonato já encerrado)."""
    rodada_anterior = rodada_numero - 1
    if rodada_anterior < 1:
        return None

    tab = calcular_tabela(cur, temporada, rodada_anterior)
    if not tab:
        return None

    item_nosso = next((t for t in tab if t["time_api_id"] == api_football_team_id), None)
    if item_nosso is None:
        return None

    rodadas_restantes = TOTAL_RODADAS_LIGA - rodada_anterior
    if rodadas_restantes <= 0:
        return None

    pontos_max_nosso = item_nosso["pontos"] + rodadas_restantes * 3

    quarto_colocado = next((t for t in tab if t["posicao"] == TAMANHO_ZONA_G4), None)
    pode_alcancar_g4 = quarto_colocado is None or pontos_max_nosso >= quarto_colocado["pontos"]

    total_times = len(tab)
    posicao_referencia_z4 = total_times - TAMANHO_ZONA_Z4 + 1
    referencia_z4 = next((t for t in tab if t["posicao"] == posicao_referencia_z4), None)
    if referencia_z4 is None:
        pode_cair_z4 = False
    else:
        pontos_max_referencia = referencia_z4["pontos"] + rodadas_restantes * 3
        pode_cair_z4 = item_nosso["pontos"] <= pontos_max_referencia

    # NOVO: mesmo se a matemática já permitir concluir "morto", só marca
    # de verdade dentro da janela final (ver RODADAS_RESTANTES_MAXIMO_PARA_
    # JOGO_MORTO) - fora dela, o campo "morto" fica sempre False, mas o
    # resto da informação (zona atual, distância etc.) continua disponível
    # normalmente pra quem quiser usar de outro jeito no futuro.
    dentro_da_janela_final = rodadas_restantes <= RODADAS_RESTANTES_MAXIMO_PARA_JOGO_MORTO

    return {
        "morto": dentro_da_janela_final and not pode_alcancar_g4 and not pode_cair_z4,
        "zona_atual": item_nosso["zona"],
        "posicao_atual": item_nosso["posicao"],
        "pode_alcancar_g4": pode_alcancar_g4,
        "pode_cair_z4": pode_cair_z4,
        "rodadas_restantes": rodadas_restantes,
    }
