"""
Cria a tabela `execucoes_atualizacao` - controle de estado da atualização
de odds/recomendações disparada pelo botão "Gerar recomendações da rodada"
(e pelo link "Atualizar recomendações").

POR QUE ESSA TABELA EXISTE
--------------------------
Até aqui, o "tem uma atualização rodando?" vivia só num threading.Event em
memória do processo Flask. Isso tinha dois problemas:

1. Gunicorn pode rodar mais de um worker, e cada worker tem a PRÓPRIA
   memória. O navegador perguntando "já acabou?" podia cair num worker
   diferente do que estava rodando a thread - e receber "não tem nada
   rodando" na hora, quebrando o acompanhamento.
2. Não dava pra saber QUANTO TEMPO cada execução levou, então não havia
   como estimar a duração da próxima (a barra de progresso precisa disso).

Com o estado no banco, os dois problemas somem de uma vez: o dado é
compartilhado por todos os workers, e o histórico de duração fica salvo.

COLUNAS
-------
- `status`: 'rodando' | 'concluida' | 'erro' | 'abandonada'
- `iniciada_em` / `finalizada_em`: pra medir a duração real
- `usuario_id`: quem disparou (pode ser NULL se vier de outro caminho)
- `detalhe`: mensagem curta de erro, quando houver

'abandonada' cobre o caso do container reiniciar no meio da execução
(deploy, queda, restart do Railway). Sem isso, a linha ficaria 'rodando'
pra sempre e o app acharia que tem uma atualização em andamento
eternamente - bloqueando todo clique futuro e mostrando um timer infinito.
Ver LIMITE_EXECUCAO_ABANDONADA_MINUTOS em app.py (15 minutos).

Rodar UMA vez antes de subir a versão nova do app:
    python migrar_execucoes_atualizacao.py

Variáveis de ambiente necessárias: DATABASE_URL
"""

import os

import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]


def main():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS execucoes_atualizacao (
                id SERIAL PRIMARY KEY,
                status VARCHAR(20) NOT NULL DEFAULT 'rodando',
                iniciada_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finalizada_em TIMESTAMPTZ,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
                forcada BOOLEAN NOT NULL DEFAULT FALSE,
                detalhe TEXT
            )
            """
        )
        print("Tabela execucoes_atualizacao OK.")

        # Índice pro caminho quente: "tem alguma execução rodando agora?"
        # é a pergunta feita a cada 5 segundos pelo polling, e também em
        # toda renderização da página inicial.
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_execucoes_atualizacao_status
            ON execucoes_atualizacao (status, iniciada_em DESC)
            """
        )
        print("Índice idx_execucoes_atualizacao_status OK.")

        # Índice pro cálculo da estimativa (média das últimas concluídas).
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_execucoes_atualizacao_concluidas
            ON execucoes_atualizacao (finalizada_em DESC)
            WHERE status = 'concluida'
            """
        )
        print("Índice idx_execucoes_atualizacao_concluidas OK.")

        conn.commit()
        print("\nMigração concluída com sucesso.")

    except Exception as e:
        conn.rollback()
        print(f"ERRO na migração: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
