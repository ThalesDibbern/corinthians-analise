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
