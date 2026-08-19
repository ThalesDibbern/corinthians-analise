"""
Script de DETECÇÃO (só leitura, nunca junta nada sozinho) de candidatos a
jogador duplicado - complementa o limpeza_jogadores_duplicados.py, que já
existe mas só executa uma lista de pares escrita à mão (PARES_PARA_JUNTAR).
 
Esse script aqui faz o trabalho chato de vasculhar a tabela `jogadores`
atrás de nomes parecidos (mesma lógica de sobrenome+inicial usada em
atualizar_odds.py) e IMPRIME os candidatos organizados por nível de
confiança - você decide manualmente quais colar em PARES_PARA_JUNTAR.
 
NUNCA faz UPDATE/DELETE em nada. É seguro rodar quantas vezes quiser.
 
Por que não junta sozinho: sobrenome+inicial batendo não é garantia de ser
a mesma pessoa (ex: "Claudio S." de um time e "Claudio Silva" de outro
time podem ser duas pessoas diferentes - "Silva" é um dos sobrenomes mais
comuns do Brasil). Por isso, além do nome, o script também compara o time
mais recente de cada um: mesmo time nos dois = indício forte (PROVÁVEL);
times diferentes = risco de falso positivo (SUSPEITO, olhar com cuidado);
sem jogo registrado ainda = não dá pra confirmar por time (SEM DADO).
 
Só entram na lista pares onde PELO MENOS UM dos dois não tem
api_football_id confirmado - dois jogadores que já têm api_football_id
cada um são, por definição, duas pessoas diferentes de verdade pra
API-Football, então não faz sentido sugerir juntar os dois (isso reduz
bastante o ruído de sobrenomes comuns entre jogadores que já sabemos
serem pessoas distintas).
 
Rodar manualmente, sob demanda (não faz parte de nenhum cron):
    python detectar_jogadores_duplicados.py
 
Variáveis de ambiente necessárias:
  - DATABASE_URL
"""
 
import os
import unicodedata
import psycopg2
 
DATABASE_URL = os.environ["DATABASE_URL"]
 
 
def sem_acento(texto):
    """Remove acentos pra comparação de sobrenome não falhar por causa de
    "Araújo" vs "Araujo" (mesmo caso que já resolvemos no Grupo 1 da
    limpeza manual)."""
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
 
 
def partes_nome(nome):
    """Primeiro nome + sobrenome (última palavra), sem acento, minúsculo."""
    palavras = sem_acento(nome).lower().split()
    if len(palavras) < 2:
        return None, None
    return palavras[0].rstrip("."), palavras[-1]
 
 
def nomes_batem(primeiro_a, primeiro_b):
    """Mesma regra de atualizar_odds.py: bate se o primeiro nome é igual,
    OU se um dos dois é uma inicial (<=2 letras) que é prefixo do outro."""
    if primeiro_a == primeiro_b:
        return True
    if len(primeiro_a) <= 2 and primeiro_b.startswith(primeiro_a):
        return True
    if len(primeiro_b) <= 2 and primeiro_a.startswith(primeiro_b):
        return True
    return False
 
 
def buscar_jogadores_com_time_recente(cur):
    cur.execute(
        """
        SELECT j.id, j.nome, j.api_football_id, t.nome AS time_recente, jg.data_jogo
        FROM jogadores j
        LEFT JOIN LATERAL (
            SELECT jej.lado, jej.jogo_id
            FROM jogador_estatisticas_jogo jej
            JOIN jogos jg2 ON jg2.id = jej.jogo_id
            WHERE jej.jogador_id = j.id
            ORDER BY jg2.data_jogo DESC
            LIMIT 1
        ) rec ON true
        LEFT JOIN jogos jg ON jg.id = rec.jogo_id
        LEFT JOIN times t ON t.id = (
            CASE WHEN rec.lado = 'mandante' THEN jg.mandante_id ELSE jg.visitante_id END
        )
        ORDER BY j.nome
        """
    )
    colunas = ["id", "nome", "api_football_id", "time_recente", "ultima_data"]
    return [dict(zip(colunas, row)) for row in cur.fetchall()]
 
 
def classificar_par(a, b):
    if a["time_recente"] is None or b["time_recente"] is None:
        return "SEM DADO", "pelo menos um dos dois ainda não tem jogo registrado - não dá pra confirmar por time"
    if a["time_recente"] == b["time_recente"]:
        return "PROVAVEL", f"mesmo time mais recente ({a['time_recente']}) - forte indício de ser a mesma pessoa"
    return "SUSPEITO", (f"times diferentes ({a['time_recente']} x {b['time_recente']}) - "
                         "risco de falso positivo por sobrenome comum, checar com cuidado")
 
 
def descricao_jogador(j):
    id_status = f"api_football_id {j['api_football_id']}" if j["api_football_id"] else "SEM api_football_id"
    time = j["time_recente"] or "nenhum jogo registrado ainda"
    return f"\"{j['nome']}\" (id {j['id']}, {id_status}, último time: {time})"
 
 
def main():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        jogadores = buscar_jogadores_com_time_recente(cur)
        cur.close()
    finally:
        conn.close()
 
    # pré-calcula sobrenome/primeiro nome normalizados de cada jogador
    for j in jogadores:
        j["_primeiro"], j["_sobrenome"] = partes_nome(j["nome"])
 
    candidatos = {"PROVAVEL": [], "SUSPEITO": [], "SEM DADO": []}
 
    for i, a in enumerate(jogadores):
        if a["_sobrenome"] is None:
            continue
        for b in jogadores[i + 1:]:
            if b["_sobrenome"] is None:
                continue
            if a["_sobrenome"] != b["_sobrenome"]:
                continue
            if not nomes_batem(a["_primeiro"], b["_primeiro"]):
                continue
            # só interessa se pelo menos um dos dois ainda não tem identidade
            # confirmada - dois jogadores já confirmados são, por definição,
            # duas pessoas diferentes de verdade
            if a["api_football_id"] and b["api_football_id"]:
                continue
 
            categoria, motivo = classificar_par(a, b)
            candidatos[categoria].append((a, b, motivo))
 
    total = sum(len(v) for v in candidatos.values())
    print(f"{total} candidato(s) encontrado(s) no total.\n")
 
    ordem = [
        ("PROVAVEL", "🟢 PROVÁVEL (mesmo time - forte indício de ser a mesma pessoa)"),
        ("SUSPEITO", "🟡 SUSPEITO (times diferentes - cuidado com falso positivo)"),
        ("SEM DADO", "⚪ SEM DADO SUFICIENTE (algum dos dois ainda não jogou)"),
    ]
 
    for chave, titulo in ordem:
        lista = candidatos[chave]
        print(f"\n===== {titulo} - {len(lista)} candidato(s) =====")
        if not lista:
            print("  (nenhum)")
        for a, b, motivo in lista:
            print(f"  {descricao_jogador(a)}")
            print(f"  {descricao_jogador(b)}")
            print(f"  -> {motivo}")
            print()
 
    print("\nLembrete: nada foi alterado no banco. Pra juntar algum par de "
          "verdade, adiciona (id_fantasma, id_real) em PARES_PARA_JUNTAR "
          "dentro de limpeza_jogadores_duplicados.py e roda esse script "
          "(o 'real' é o que já tem api_football_id, quando só um dos dois "
          "tiver).")
 
 
if __name__ == "__main__":
    main()
 
