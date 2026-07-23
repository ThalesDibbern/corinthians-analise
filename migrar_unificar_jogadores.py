"""
Script de migração (rodar 1x, manualmente) - unifica jogadores duplicados
criados pelo atualizar_odds.py antes da correção do formato de nome.

A OddsPapi manda nome no formato "Sobrenome, Nome" (ex: "Alberto, Yuri"),
diferente da API-Football, que usa "Nome Sobrenome" (ex: "Yuri Alberto").
Antes da correção, isso criava um registro duplicado pra cada jogador com
prop bet: um vindo da API-Football (com api_football_id e escalação
completa), outro vindo da OddsPapi (sem api_football_id, sem escalação
nenhuma - por isso jogadores titulares apareciam como "indisponíveis" nas
recomendações).

Esse script:
  1. Acha jogadores cadastrados no formato "Sobrenome, Nome" (tem vírgula)
  2. Converte pro formato normal ("Nome Sobrenome")
  3. Se já existir outro jogador com esse nome normalizado (o registro
     "bom", vindo da API-Football), migra todas as referências (odds,
     recomendações, escalações, gols, cartões, etc.) pro registro bom, e
     apaga o duplicado
  4. Se NÃO existir outro registro com esse nome, só renomeia o próprio
     registro pro formato normalizado (não há nada pra unificar ainda,
     mas fica pronto pra casar automaticamente se a API-Football cadastrar
     esse jogador no futuro)

Seguro rodar mais de uma vez - já não sobra jogador com vírgula depois da
primeira execução bem-sucedida.

Variáveis de ambiente necessárias:
  - DATABASE_URL -> a URL de conexão do Postgres (mesma usada nos outros scripts)
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]

# Tabelas que referenciam jogadores.id, e o(s) nome(s) da coluna em cada uma
TABELAS_COM_JOGADOR_ID = [
    ("odds", ["jogador_id"]),
    ("recomendacoes", ["jogador_id"]),
    ("historico_recomendacoes", ["jogador_id"]),
    ("escalacoes", ["jogador_id"]),
    ("gols", ["jogador_id"]),
    ("cartoes", ["jogador_id"]),
    ("substituicoes", ["jogador_saiu_id", "jogador_entrou_id"]),
    ("jogador_estatisticas_jogo", ["jogador_id"]),
]


def normalizar_nome_oddspapi(nome):
    if "," in nome:
        partes = [p.strip() for p in nome.split(",", 1)]
        if len(partes) == 2 and partes[0] and partes[1]:
            sobrenome, nome_proprio = partes
            return f"{nome_proprio} {sobrenome}".strip()
    return nome


def migrar_referencias(cur, id_antigo, id_novo):
    """Repassa todas as referências de um jogador duplicado pro jogador
    correto, tabela por tabela."""
    for tabela, colunas in TABELAS_COM_JOGADOR_ID:
        for coluna in colunas:
            cur.execute(
                f"UPDATE {tabela} SET {coluna} = %s WHERE {coluna} = %s",
                (id_novo, id_antigo),
            )


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        cur.execute("SELECT id, nome FROM jogadores WHERE nome LIKE '%%,%%'")
        duplicados = cur.fetchall()

        if not duplicados:
            print("Nenhum jogador no formato 'Sobrenome, Nome' encontrado - nada a fazer.")
            return

        print(f"Encontrados {len(duplicados)} jogador(es) no formato 'Sobrenome, Nome'.")

        unificados = 0
        só_renomeados = 0

        for jogador_id, nome_original in duplicados:
            nome_normalizado = normalizar_nome_oddspapi(nome_original)
            if nome_normalizado == nome_original:
                continue  # não deveria acontecer (já filtrado por ter vírgula), só por segurança

            cur.execute(
                "SELECT id FROM jogadores WHERE nome = %s AND id != %s",
                (nome_normalizado, jogador_id),
            )
            row = cur.fetchone()

            if row:
                jogador_id_correto = row[0]
                migrar_referencias(cur, jogador_id, jogador_id_correto)
                cur.execute("DELETE FROM jogadores WHERE id = %s", (jogador_id,))
                print(f"  Unificado: '{nome_original}' (id {jogador_id}) -> "
                      f"'{nome_normalizado}' (id {jogador_id_correto})")
                unificados += 1
            else:
                cur.execute(
                    "UPDATE jogadores SET nome = %s WHERE id = %s",
                    (nome_normalizado, jogador_id),
                )
                print(f"  Renomeado (sem duplicata pra unificar ainda): "
                      f"'{nome_original}' -> '{nome_normalizado}'")
                só_renomeados += 1

        conn.commit()
        print(f"\nConcluído! {unificados} jogador(es) unificado(s), "
              f"{só_renomeados} só renomeado(s) (sem duplicata encontrada).")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a execução: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
