"""
Script de migração (rodar 1x, manualmente, no serviço de migração pontual
do Railway, ANTES de subir os outros arquivos dessa sessão) - prepara o
banco pro recurso de "Múltiplas em Destaque" de verdade (substitui a
reconstrução ao vivo que existia antes em montar_combinacoes_historico).

Faz três coisas:

1) Adiciona a coluna `rodada` em `jogos` - a API-Football já manda esse
   dado (fixture["league"]["round"], ex: "Regular Season - 20") em toda
   consulta que popular_banco.py já faz, só nunca foi salvo. É a partir
   dela que a política de retenção (2 rodadas completas) sabe decidir o
   que comprimir.

2) Cria `multiplas_candidatas` - tabela de RASCUNHO VIVO. Toda vez que o
   sistema gera recomendações (clique real na página, ou o cron rodando
   sozinho), cada combinação de pernas é gravada/atualizada aqui. A
   identidade de uma combinação (`assinatura`) é a casa + o conjunto de
   pernas (jogo+tipo+jogador+linha+direção) - NUNCA a odd nem a
   probabilidade, propositalmente: a mesma combinação pode aparecer várias
   vezes ao longo do dia com odd diferente (a odd muda, a combinação
   continua sendo "a mesma aposta"), e cada atualização SOBRESCREVE a
   linha existente em vez de duplicar. Assim que o jogo mais próximo
   envolvido (`primeiro_apito`) já começou, a linha congela
   (`congelada = TRUE`) e ninguém mais escreve em cima dela - garante que
   o que sobra é sempre "a última geração antes do apito", nunca uma odd
   de já-em-jogo (que a OddsPapi nem oferece mais, mas fica documentado
   como trava explícita mesmo assim).

3) Cria `historico_multiplas_destaque` - o que a página /historico
   realmente lê. Uma linha por (jogo, combinação vencedora) - se uma
   combinação cruza 2 jogos e entra no top-5 dos dois, gera 2 linhas, uma
   pra cada. Só é preenchida depois que TODAS as pernas de uma candidata
   já têm resultado conhecido (feito por arquivar_recomendacoes.py). Os
   campos de detalhe (descrição/casa/odd/jogos) ficam NULL depois que a
   combinação sai da janela das 2 rodadas mais recentes - só
   `probabilidade_combinada`/`resultado`/`rodada` sobrevivem pra sempre,
   o suficiente pro resumo de calibração continuar funcionando.

Seguro rodar mais de uma vez - usa IF NOT EXISTS / ADD COLUMN IF NOT EXISTS.

Variáveis de ambiente necessárias:
  - DATABASE_URL -> a URL de conexão do Postgres (mesma usada nos outros scripts)
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        # ---------- 1) coluna rodada em jogos ----------
        cur.execute("ALTER TABLE jogos ADD COLUMN IF NOT EXISTS rodada VARCHAR(50)")
        print("Coluna jogos.rodada OK (jogos já existentes ficam NULL até "
              "popular_banco.py passar de novo e fizer backfill).")

        # ---------- 2) multiplas_candidatas (rascunho vivo) ----------
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS multiplas_candidatas (
                id SERIAL PRIMARY KEY,
                assinatura VARCHAR(64) NOT NULL UNIQUE,
                casa_aposta VARCHAR(100) NOT NULL,
                descricao TEXT NOT NULL,
                odd_combinada NUMERIC(10,2) NOT NULL,
                probabilidade_combinada NUMERIC(6,2) NOT NULL,
                pernas JSONB NOT NULL,
                jogos JSONB NOT NULL,
                primeiro_apito TIMESTAMP NOT NULL,
                congelada BOOLEAN NOT NULL DEFAULT FALSE,
                avaliada BOOLEAN NOT NULL DEFAULT FALSE,
                criada_em TIMESTAMP NOT NULL DEFAULT NOW(),
                atualizada_em TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
        print("Tabela multiplas_candidatas OK.")

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_multiplas_candidatas_congelada "
            "ON multiplas_candidatas (congelada, avaliada) WHERE congelada = TRUE AND avaliada = FALSE"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_multiplas_candidatas_primeiro_apito "
            "ON multiplas_candidatas (primeiro_apito) WHERE congelada = FALSE"
        )
        print("Índices de multiplas_candidatas OK.")

        # ---------- 3) historico_multiplas_destaque (o que a tela lê) ----------
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS historico_multiplas_destaque (
                id SERIAL PRIMARY KEY,
                jogo_id INTEGER NOT NULL REFERENCES jogos(id),
                rodada VARCHAR(50),
                casa_aposta VARCHAR(100),
                descricao TEXT,
                odd_combinada NUMERIC(10,2),
                jogos JSONB,
                probabilidade_combinada NUMERIC(6,2) NOT NULL,
                resultado VARCHAR(10) NOT NULL,
                criada_em TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
        print("Tabela historico_multiplas_destaque OK.")

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_historico_multiplas_jogo "
            "ON historico_multiplas_destaque (jogo_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_historico_multiplas_rodada "
            "ON historico_multiplas_destaque (rodada)"
        )
        print("Índices de historico_multiplas_destaque OK.")

        conn.commit()
        print("\nMigração concluída com sucesso.")

    except Exception as e:
        conn.rollback()
        print(f"\nErro durante a migração: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
