"""
Rota SÓ-LEITURA de consulta ao banco, pra auditoria de rodada.

POR QUE EXISTE
--------------
A auditoria de rodada (seção 23 / `protocolo_medicao_rodada_26.md`) roda
sobre 17 consultas SQL. Sem esta rota, cada uma exige copiar do chat,
colar no console do Railway, copiar o resultado e colar de volta. Com
ela, a consulta é feita direto e o ciclo de investigação deixa de custar
tempo humano.

TRÊS TRAVAS INDEPENDENTES
-------------------------
1. ACESSO    - token certo OU sessão logada no site. Sem nenhum dos
               dois, 401. Nada além disso é avaliado.
2. VALIDAÇÃO - só passa UMA instrução, começando em SELECT ou WITH, sem
               ponto e vírgula e sem nenhuma palavra de escrita.
3. PERMISSÃO - a conexão usa o papel `claude_leitura`, que tem apenas
               SELECT. Mesmo que 1 e 2 falhem, o Postgres recusa.

Precisa das três falharem ao mesmo tempo pra virar problema. E a conexão
ainda abre em modo read-only com timeout, então nem consulta pesada
segura o banco.

DOIS JEITOS DE PASSAR PELA TRAVA 1 (29/09/2026)
-----------------------------------------------
- TOKEN na URL (`?token=...`): o caminho original, pra uso manual.
- SESSÃO do site: quem fez login em /login (cookie de sessão do Flask,
  `session["usuario_id"]`) consulta sem token. Existe porque o Claude,
  pelo navegador, não pode mais colocar credencial em URL; com a sessão,
  ele consulta sem nunca manusear o token. Também desacopla a rotação do
  token (item 9.10b) do acesso do Claude ao banco.

A rota continua fora do `exigir_login` do app (rotas_livres) de
propósito: sem isso, uma chamada só com token seria redirecionada pro
login. A checagem de sessão é feita AQUI, dentro da rota.

⚠️ NÃO USA A URL DE ADMIN. Se `DATABASE_URL_LEITURA` não estiver
configurada, a rota se recusa a funcionar em vez de cair pra
`DATABASE_URL` — cair pro admin seria transformar uma rota de leitura
numa porta de escrita sem ninguém perceber.

VARIÁVEIS DE AMBIENTE (Railway → serviço do site → Variables)
------------------------------------------------------------
  TOKEN_CONSULTA_LEITURA -> string secreta longa (opcional desde 29/09:
                            sem ela, só o caminho por sessão funciona)
  DATABASE_URL_LEITURA   -> postgresql://claude_leitura:SENHA@postgres.railway.internal:5432/railway

O host é o INTERNO. Não precisa de Public Networking ligado.

COMO LIGAR NO APP (2 linhas em app.py)
-------------------------------------
    from api_consulta import api_consulta
    app.register_blueprint(api_consulta)

USO
---
    GET /api/consulta?token=XXX&sql=SELECT%20...          (com token)
    GET /api/consulta?sql=SELECT%20...                    (logado no site)
    GET /api/consulta?sql=...&formato=json

Sem `formato`, devolve tabela em texto (mais fiel de ler). Com
`formato=json`, devolve JSON.
"""

import hmac
import os
import re

import psycopg2
from flask import Blueprint, request, Response, jsonify, session

api_consulta = Blueprint("api_consulta", __name__)

# Teto de linhas devolvidas. As consultas de auditoria têm de 5 a 30
# linhas; 200 dá folga sem permitir despejo da base inteira.
LIMITE_LINHAS = 200

# Teto de tempo por consulta, no próprio Postgres. Protege contra
# consulta mal escrita segurar o banco enquanto o cron tenta rodar.
TIMEOUT_MS = 15000

# Teto de tamanho do texto da consulta. Nenhuma consulta do protocolo
# passa de ~2 KB.
TAMANHO_MAXIMO_SQL = 8000

# Palavras que não podem aparecer NEM COMO SUBSTRING ISOLADA. A checagem
# é por palavra inteira (\b), então "select" não bloqueia "selecionado"
# nem "update" bloqueia uma coluna chamada "atualizado_em".
PALAVRAS_PROIBIDAS = {
    "insert", "update", "delete", "drop", "alter", "create", "grant",
    "revoke", "truncate", "copy", "vacuum", "call", "do", "merge",
    "refresh", "comment", "set", "reset", "begin", "commit", "rollback",
    "lock", "notify", "listen", "unlisten", "prepare", "execute",
    "deallocate", "discard", "reindex", "cluster", "checkpoint", "load",
    "import", "security", "definer", "pg_read_file",
    "pg_read_binary_file", "pg_ls_dir", "lo_import", "lo_export",
    "dblink", "postgres_fdw",
}


def _acesso_autorizado():
    """Devolve True se a requisição passou pela trava 1.

    Dois caminhos, qualquer um basta:
    - sessão logada no site (`session["usuario_id"]`, gravada pelo /login);
    - token igual a TOKEN_CONSULTA_LEITURA, SE a variável existir.

    Sem a variável de ambiente, o caminho por token fica DESLIGADO (não
    aberto): um deploy sem a variável não deixa a porta escancarada, só
    restringe a rota a quem está logado.

    `hmac.compare_digest` compara em tempo constante — evita que o tempo
    de resposta vaze quantos caracteres do token estavam certos.
    """
    if "usuario_id" in session:
        return True

    token_esperado = os.environ.get("TOKEN_CONSULTA_LEITURA")
    token_recebido = request.args.get("token")
    if token_esperado and token_recebido:
        return hmac.compare_digest(
            token_recebido.encode("utf-8"), token_esperado.encode("utf-8")
        )
    return False


def _validar_sql(sql):
    """Devolve (ok, motivo). Só aceita uma instrução de leitura.

    A ordem das checagens importa: o ponto e vírgula é testado ANTES do
    resto porque é ele que permitiria empilhar uma segunda instrução
    ("SELECT 1; DROP TABLE x"). Sem ele, tudo que sobra tem que caber
    numa única expressão que começa em SELECT/WITH.
    """
    if not sql or not sql.strip():
        return False, "consulta vazia"

    texto = sql.strip()

    if len(texto) > TAMANHO_MAXIMO_SQL:
        return False, f"consulta acima de {TAMANHO_MAXIMO_SQL} caracteres"

    # Ponto e vírgula: proibido em qualquer posição, inclusive no fim.
    # Permitir só no fim abriria espaço pra truque de espaço/comentário.
    if ";" in texto:
        return False, "ponto e vírgula não é permitido"

    # Comentários poderiam esconder palavra proibida da checagem abaixo.
    if "--" in texto or "/*" in texto:
        return False, "comentário não é permitido"

    if not re.match(r"^\s*(select|with)\b", texto, re.IGNORECASE):
        return False, "a consulta precisa começar com SELECT ou WITH"

    palavras = set(re.findall(r"[a-zA-Z_][a-zA-Z_0-9]*", texto.lower()))
    encontradas = palavras & PALAVRAS_PROIBIDAS
    if encontradas:
        return False, f"palavra não permitida: {', '.join(sorted(encontradas))}"

    return True, ""


def _conectar():
    """Conexão só-leitura, com timeout, usando o papel `claude_leitura`.

    `default_transaction_read_only=on` é cinto e suspensório: o papel já
    não tem permissão de escrita, mas se um dia alguém der GRANT por
    engano, a conexão continua recusando escrita.
    """
    url = os.environ.get("DATABASE_URL_LEITURA")
    if not url:
        return None
    return psycopg2.connect(
        url,
        options=(
            f"-c statement_timeout={TIMEOUT_MS} "
            "-c default_transaction_read_only=on "
            "-c idle_in_transaction_session_timeout=30000"
        ),
    )


def _tabela_texto(colunas, linhas, truncado):
    """Tabela em texto puro, alinhada por pipe. Formato escolhido porque
    é o que sobrevive melhor à leitura automatizada da página."""
    if not linhas:
        return "(nenhuma linha)"

    valores = [[("" if v is None else str(v)) for v in linha] for linha in linhas]
    larguras = [
        max(len(str(colunas[i])), *(len(l[i]) for l in valores))
        for i in range(len(colunas))
    ]

    def formata(campos):
        return " | ".join(str(c).ljust(larguras[i]) for i, c in enumerate(campos))

    partes = [
        formata(colunas),
        "-+-".join("-" * w for w in larguras),
    ]
    partes.extend(formata(v) for v in valores)
    partes.append("")
    partes.append(f"({len(valores)} linha(s))")
    if truncado:
        partes.append(
            f"AVISO: resultado cortado em {LIMITE_LINHAS} linhas. "
            "Refaça a consulta com filtro ou agregação."
        )
    return "\n".join(partes)


@api_consulta.route("/api/consulta")
def consultar():
    if not _acesso_autorizado():
        return Response("nao autorizado (faca login no site ou envie o token)\n",
                        status=401, mimetype="text/plain")

    sql = request.args.get("sql", "")
    ok, motivo = _validar_sql(sql)
    if not ok:
        return Response(f"consulta recusada: {motivo}\n",
                        status=400, mimetype="text/plain")

    conn = None
    try:
        conn = _conectar()
        if conn is None:
            return Response("rota desativada (sem DATABASE_URL_LEITURA)\n",
                            status=503, mimetype="text/plain")

        with conn.cursor() as cur:
            cur.execute(sql)
            if cur.description is None:
                return Response("a consulta nao devolveu colunas\n",
                                status=400, mimetype="text/plain")
            colunas = [d[0] for d in cur.description]
            linhas = cur.fetchmany(LIMITE_LINHAS + 1)

        truncado = len(linhas) > LIMITE_LINHAS
        linhas = linhas[:LIMITE_LINHAS]

        if request.args.get("formato") == "json":
            return jsonify({
                "colunas": colunas,
                "linhas": [[(None if v is None else str(v)) for v in l] for l in linhas],
                "total": len(linhas),
                "truncado": truncado,
            })

        return Response(_tabela_texto(colunas, linhas, truncado) + "\n",
                        mimetype="text/plain; charset=utf-8")

    except psycopg2.Error as e:
        # Devolve a mensagem do Postgres de propósito: é ela que diz
        # "permission denied" quando a trava de permissão age, e é ela
        # que aponta erro de sintaxe numa consulta de auditoria.
        return Response(f"erro do banco: {e}\n", status=400,
                        mimetype="text/plain; charset=utf-8")
    except Exception as e:
        return Response(f"erro: {e.__class__.__name__}\n", status=500,
                        mimetype="text/plain")
    finally:
        if conn is not None:
            conn.close()
