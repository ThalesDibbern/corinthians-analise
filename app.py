"""
Interface web do projeto - Análise Corinthians.

Mostra um botão "Gerar recomendações da rodada" com um filtro de faixa de
odd. Ao clicar, busca as recomendações individuais já calculadas (pelo
motor_recomendacoes.py, que roda todo dia) e monta apostas simples e
múltiplas (1 a 5 pernas) na hora, dentro da faixa de odd que o usuário
escolheu. NOVO: antes só considerava múltiplas (2+ pernas) - agora também
mostra a aposta individual quando ela sozinha já cai dentro da faixa
pedida, em vez de forçar sempre uma combinação.

Não recalcula os padrões nem busca odds novas - isso já é feito pelos
scripts automáticos. Essa interface só CONSULTA o que já está pronto no
banco e faz a combinação ao vivo (rápido, porque são poucos dados).

Variáveis de ambiente necessárias:
  - DATABASE_URL -> a URL de conexão do Postgres
  - PORT         -> porta onde o site vai rodar (o Railway define isso sozinho)
"""

import os
import json
from functools import wraps
from itertools import combinations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import psycopg2
from flask import Flask, render_template_string, request, redirect, Response, session, url_for, flash, get_flashed_messages
from werkzeug.security import generate_password_hash, check_password_hash

DATABASE_URL = os.environ["DATABASE_URL"]

app = Flask(__name__)
# NOVO: chave usada pra assinar o cookie de sessão (login). Configure a
# variável de ambiente SECRET_KEY no Railway com um valor aleatório - sem
# isso, a sessão de todo mundo seria invalidada (logout forçado) toda vez
# que o serviço reiniciar/fizer novo deploy.
app.secret_key = os.environ.get("SECRET_KEY", "troque-essa-chave-numa-variavel-de-ambiente-SECRET_KEY")


# ---------- Banca (dinheiro fictício, não real) ----------
#
# Cada usuário tem uma banca própria (usuarios.banca_atual). Ela SÓ muda em
# 4 situações, sempre através de registrar_movimento_banca (que também
# grava o extrato em banca_movimentos, pra dar pra conferir depois):
#   - "deposito"/"resgate": o usuário mexe manualmente, na página de ROI
#   - "aposta": sai da banca o valor apostado, no momento em que a aposta
#     é salva (dinheiro "reservado" pra aposta, igual acontece de verdade
#     numa casa de apostas)
#   - "retorno": quando uma aposta pendente é resolvida como "acertou",
#     volta pra banca o valor apostado x a odd (stake + lucro). Se
#     "errou", não volta nada (o valor já tinha saído quando a aposta foi
#     salva) - não precisa de nenhum movimento extra nesse caso.
#   - "cancelamento": se uma aposta pendente é cancelada, devolve o valor
#     que tinha saído quando ela foi salva.
# ---------- Barra de navegação compartilhada (botões grandes, com destaque na página atual) ----------
NAV_CSS = """
        .nav-principal {
            display: flex; gap: 10px; flex-wrap: wrap; margin: 20px 0 24px;
            justify-content: center;
        }
        .nav-btn {
            background: #161b22; border: 1px solid #30363d; color: #c9d1d9;
            border-radius: 10px; padding: 10px 18px; font-size: 0.85rem; font-weight: 600;
            text-decoration: none; transition: border-color 0.15s, background 0.15s;
        }
        .nav-btn:hover { border-color: #58a6ff; background: #1c2531; }
        .nav-btn.nav-ativo {
            background: #1f6feb33; color: #58a6ff; border: 1px solid #58a6ff88;
        }
        .nav-meta {
            display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
            margin-bottom: 8px; font-size: 0.82rem; color: #8b949e;
        }
        .nav-banca {
            background: #161b22; border: 1px solid #30363d; color: #e6edf3;
            border-radius: 999px; padding: 6px 16px; font-size: 0.8rem; font-weight: 600;
            text-decoration: none;
        }
        .nav-banca:hover { border-color: #58a6ff; }
        .nav-sair { color: #8b949e; text-decoration: none; font-size: 0.82rem; }
        .nav-sair:hover { text-decoration: underline; }
"""


def barra_navegacao(pagina_atual, banca_atual=None):
    """NOVO: barra de navegação compartilhada entre as 5 páginas principais
    (Gerador de Recomendações, Histórico, Estatísticas de Times,
    Estatísticas de Jogadores, Minhas Apostas) - botões grandes em vez de
    link de texto simples, com a página atual destacada em azul, pra
    sempre dar pra saber onde você está. `banca_atual` é opcional: quando
    informado, mostra o botão da banca ao lado do nome do usuário (só faz
    sentido em páginas onde já buscamos a banca mesmo)."""
    itens = [
        ("historico", "/historico", "📊 Histórico de Acertos e Erros"),
        ("times", "/times", "🏟️ Estatísticas de Times"),
        ("index", "/", "🎯 Gerador de Recomendações"),
        ("jogadores", "/jogadores", "📈 Estatísticas de Jogadores"),
        ("roi", "/minhas-apostas", "💰 Minhas Apostas (ROI)"),
    ]
    botoes = "".join(
        f'<a href="{href}" class="nav-btn{" nav-ativo" if chave == pagina_atual else ""}">{rotulo}</a>'
        for chave, href, rotulo in itens
    )

    meta = f'<span>Olá, {session.get("usuario_nome", "")}</span><a href="/logout" class="nav-sair">🚪 Sair</a>'
    if banca_atual is not None:
        meta = (
            f'<a href="/minhas-apostas" class="nav-banca">🏦 Banca: R$ {banca_atual:.2f}</a>'
            + meta
        )

    return f'<div class="nav-meta">{meta}</div><div class="nav-principal">{botoes}</div>'


def buscar_banca(cur, usuario_id):
    cur.execute("SELECT banca_atual FROM usuarios WHERE id = %s", (usuario_id,))
    row = cur.fetchone()
    return float(row[0]) if row else 0.0


def registrar_movimento_banca(cur, usuario_id, tipo, valor, aposta_id=None):
    """Aplica `valor` (pode ser negativo) na banca do usuário e grava a
    linha correspondente no extrato, já com o saldo resultante - facilita
    conferir depois se algo parecer errado, sem precisar recalcular tudo."""
    cur.execute(
        "UPDATE usuarios SET banca_atual = banca_atual + %s WHERE id = %s RETURNING banca_atual",
        (valor, usuario_id),
    )
    novo_saldo = cur.fetchone()[0]
    cur.execute(
        """INSERT INTO banca_movimentos (usuario_id, tipo, valor, aposta_id, saldo_apos)
           VALUES (%s, %s, %s, %s, %s)""",
        (usuario_id, tipo, valor, aposta_id, novo_saldo),
    )
    return float(novo_saldo)


# ---------- Atualização de odds sob demanda (Railway API) ----------
#
# Em vez de deixar o serviço de odds (`refreshing-freedom`) rodando de hora
# em hora o dia inteiro (inclusive de madrugada e em dias sem jogo, gastando
# cota da OddsPapi à toa), o app dispara um "Run Now" desse serviço via API
# do Railway na primeira vez que alguém clica em "Gerar recomendações da
# rodada" depois de 1 hora sem nenhum disparo. Cliques dentro dessa 1 hora
# não disparam de novo sozinhos - mas o botão "🔄 Atualizar recomendações"
# permite forçar manualmente a qualquer momento.
INTERVALO_MINIMO_ATUALIZACAO_ODDS = timedelta(hours=1)
RAILWAY_GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"

# NOVO: corrige bug de fuso horário - o container do Railway roda em UTC
# por padrão, então `.astimezone()` sem argumento (que converte pro fuso
# LOCAL do servidor) não convertia nada de verdade, ficava mostrando a
# hora em UTC mesmo (ex: 18:27 UTC em vez de 15:27 horário de Brasília).
# Fixando o fuso explicitamente aqui, não depende mais do fuso do servidor.
FUSO_BRASIL = ZoneInfo("America/Sao_Paulo")


def buscar_ultima_atualizacao_odds(cur):
    """Retorna o horário (com timezone) do último disparo registrado, ou
    None se nunca disparou ainda nessa instalação."""
    cur.execute("SELECT criado_em FROM atualizacoes_odds ORDER BY criado_em DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        return None
    return row[0].replace(tzinfo=timezone.utc) if row[0].tzinfo is None else row[0]


def registrar_atualizacao_odds(cur, usuario_id, forcado):
    cur.execute(
        "INSERT INTO atualizacoes_odds (usuario_id, forcado) VALUES (%s, %s)",
        (usuario_id, forcado),
    )


def disparar_atualizacao_odds_railway():
    """Chama a API do Railway pra rodar o serviço `refreshing-freedom`
    (atualizar_odds.py + motor_recomendacoes.py) agora, fora do horário
    programado - o mesmo efeito de clicar "Run Now" no dashboard, só que
    automático. Precisa de 3 variáveis de ambiente configuradas no serviço
    da INTERFACE (não no refreshing-freedom):
      - RAILWAY_API_TOKEN: token de conta/workspace criado em
        railway.app -> account settings -> tokens (token de PROJETO não
        funciona pra disparar deploy, tem que ser de conta ou workspace)
      - RAILWAY_SERVICE_ID_ODDS: o ID do serviço `refreshing-freedom`
        (Settings do serviço no Railway -> mostra o ID, ou copia da URL)
      - RAILWAY_ENVIRONMENT_ID_ODDS: o ID do ambiente (geralmente "production") -
        NÃO usar o nome "RAILWAY_ENVIRONMENT_ID" puro, porque o próprio Railway
        já injeta automaticamente uma variável com esse nome exato em todo
        serviço (o ambiente do PRÓPRIO serviço) - usar esse nome causaria
        conflito/sobrescrita da variável reservada do Railway
    Se qualquer uma faltar, ou a chamada falhar, só loga no console e
    retorna False - a página continua funcionando normalmente com o que
    já estiver no banco, só sem conseguir disparar a atualização."""
    # NOVO: mesmo .strip() de proteção contra espaço/quebra de linha
    # sobrando (ver comentário equivalente em buscar_tabela_brasileirao)
    token = (os.environ.get("RAILWAY_API_TOKEN") or "").strip()
    service_id = (os.environ.get("RAILWAY_SERVICE_ID_ODDS") or "").strip()
    environment_id = (os.environ.get("RAILWAY_ENVIRONMENT_ID_ODDS") or "").strip()

    if not (token and service_id and environment_id):
        print("[atualizacao_odds] RAILWAY_API_TOKEN/RAILWAY_SERVICE_ID_ODDS/RAILWAY_ENVIRONMENT_ID_ODDS "
              "não configurados - não é possível disparar o Run Now automaticamente.")
        return False

    query = """
        mutation Redeploy($serviceId: String!, $environmentId: String!) {
            serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
        }
    """
    try:
        resposta = requests.post(
            RAILWAY_GRAPHQL_URL,
            json={"query": query, "variables": {"serviceId": service_id, "environmentId": environment_id}},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=15,
        )
        resposta.raise_for_status()
        dados = resposta.json()
        if dados.get("errors"):
            print(f"[atualizacao_odds] API do Railway retornou erro: {dados['errors']}")
            return False
        return True
    except requests.RequestException as e:
        print(f"[atualizacao_odds] Falha ao chamar a API do Railway: {e}")
        return False


def processar_atualizacao_odds(cur, usuario_id, forcar):
    """Decide se dispara o Run Now do refreshing-freedom (dispara se nunca
    rodou, se já faz mais de 1h do último disparo, ou se `forcar=True`) e
    devolve o horário (local, string HH:MM) do último disparo conhecido pra
    mostrar na tela - já considerando o disparo que acabou de acontecer
    nessa mesma chamada, se for o caso."""
    ultima = buscar_ultima_atualizacao_odds(cur)
    ja_passou_1h = ultima is None or (datetime.now(timezone.utc) - ultima) >= INTERVALO_MINIMO_ATUALIZACAO_ODDS

    if forcar or ja_passou_1h:
        if disparar_atualizacao_odds_railway():
            registrar_atualizacao_odds(cur, usuario_id, forcar)
            ultima = datetime.now(timezone.utc)

    return ultima


PAGINA = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Análise de Apostas</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
            background: #0d1117;
            color: #e6edf3;
            max-width: 900px;
            margin: 0 auto;
            padding: 32px 20px 80px;
        }
        h1 {
            font-size: 1.6rem;
            display: flex;
            align-items: center;
            gap: 10px;
            margin: 0;
        }
        .subtitulo { color: #8b949e; margin: 4px 0 28px; }
        .painel {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 28px;
        }
        .linha-filtro {
            display: flex;
            gap: 16px;
            align-items: flex-end;
            flex-wrap: wrap;
        }
        .nota-espera {
            color: #8b949e; font-size: 0.76rem; margin: 8px 0 0;
        }
        .linha-atualizacao {
            display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;
            margin-top: 14px; padding-top: 14px; border-top: 1px solid #21262d;
            font-size: 0.82rem; color: #8b949e;
        }
        .linha-atualizacao b { color: #e6edf3; }
        .link-atualizar {
            background: #21262d; border: 1px solid #30363d; color: #58a6ff;
            border-radius: 8px; padding: 6px 14px; font-size: 0.8rem; font-weight: 600;
            text-decoration: none;
        }
        .link-atualizar:hover { border-color: #58a6ff; }
        label {
            display: block;
            font-size: 0.8rem;
            color: #8b949e;
            margin-bottom: 6px;
        }
        input[type=number] {
            background: #0d1117;
            border: 1px solid #30363d;
            color: #e6edf3;
            border-radius: 8px;
            padding: 9px 12px;
            width: 100px;
            font-size: 0.95rem;
        }
        button {
            background: #238636;
            color: white;
            border: none;
            border-radius: 8px;
            padding: 12px 24px;
            font-size: 0.95rem;
            font-weight: 600;
            cursor: pointer;
        }
        button:hover { background: #2ea043; }
        .cartao {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 12px;
            padding: 18px 20px;
            margin-bottom: 14px;
        }
        .cartao-topo {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
            flex-wrap: wrap;
            gap: 8px;
        }
        .jogo { font-weight: 600; font-size: 0.92rem; }
        """ + NAV_CSS + """
        .colunas-resultado {
            display: grid; grid-template-columns: 1fr 1fr; gap: 18px; align-items: start;
            margin-bottom: 28px;
        }
        @media (max-width: 640px) {
            .colunas-resultado { grid-template-columns: 1fr; }
        }
        .coluna-cabecalho {
            border-radius: 10px; padding: 10px 16px; font-weight: 700; font-size: 0.92rem;
            margin-bottom: 12px; text-align: center;
        }
        .coluna-azul { background: #1f6feb33; color: #58a6ff; border: 1px solid #58a6ff55; }
        .coluna-roxa { background: #a371f722; color: #a371f7; border: 1px solid #a371f755; }
        .paginacao {
            display: flex; align-items: center; justify-content: center; gap: 14px;
            margin-top: 4px; font-size: 0.82rem; color: #8b949e;
        }
        .btn-pagina {
            background: #161b22; border: 1px solid #30363d; color: #e6edf3;
            border-radius: 8px; padding: 6px 14px; font-size: 0.85rem; cursor: pointer;
        }
        .btn-pagina:hover:not(:disabled) { border-color: #58a6ff; }
        .btn-pagina:disabled { opacity: 0.35; cursor: default; }
        .casa {
            background: #1f6feb22;
            color: #58a6ff;
            font-size: 0.72rem;
            padding: 3px 10px;
            border-radius: 999px;
            text-transform: uppercase;
            letter-spacing: 0.03em;
        }
        .odd-tag {
            background: #23863622;
            color: #3fb950;
            font-weight: 700;
            font-size: 1.05rem;
            padding: 3px 12px;
            border-radius: 8px;
        }
        .descricao { color: #c9d1d9; line-height: 1.6; margin-bottom: 10px; font-size: 0.92rem; }
        .metricas { display: flex; gap: 20px; font-size: 0.8rem; color: #8b949e; flex-wrap: wrap; }
        .metricas b { color: #e6edf3; }
        .vazio {
            text-align: center;
            color: #8b949e;
            padding: 32px 24px;
            background: #161b22;
            border: 1px dashed #30363d;
            border-radius: 12px;
            font-size: 0.9rem;
            line-height: 1.6;
        }
        .vazio-titulo { color: #c9d1d9; font-weight: 600; margin-bottom: 6px; font-size: 0.95rem; }
        .aviso {
            background: #3d2b0033;
            border: 1px solid #9e6a03;
            color: #e3b341;
            border-radius: 8px;
            padding: 12px 16px;
            font-size: 0.82rem;
            margin-bottom: 24px;
        }
        .link-historico {
            color: #58a6ff;
            text-decoration: none;
            font-size: 0.88rem;
        }
        .link-historico:hover { text-decoration: underline; }
        .salvar-linha {
            display: flex;
            gap: 8px;
            align-items: center;
            margin-top: 10px;
            padding-top: 10px;
            border-top: 1px solid #21262d;
        }
        .salvar-linha input[type=number] {
            width: 110px;
            padding: 7px 10px;
            font-size: 0.85rem;
        }
        .btn-salvar {
            background: #1f6feb;
            color: white;
            border: none;
            border-radius: 8px;
            padding: 8px 16px;
            font-size: 0.82rem;
            font-weight: 600;
            cursor: pointer;
        }
        .btn-salvar:hover { background: #388bfd; }
        .ja-apostado {
            color: #d29922;
            font-size: 0.78rem;
            margin-top: 8px;
        }
        .link-voltar {
            color: #8b949e;
            text-decoration: none;
            font-size: 0.85rem;
        }
        .link-voltar:hover { text-decoration: underline; }
        .resumo-grid {
            display: flex;
            gap: 14px;
            margin-bottom: 24px;
            flex-wrap: wrap;
        }
        .resumo-card {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 12px;
            padding: 16px 22px;
            flex: 1;
            min-width: 130px;
            text-align: center;
        }
        .resumo-numero { font-size: 1.6rem; font-weight: 700; }
        .resumo-label { color: #8b949e; font-size: 0.78rem; margin-top: 4px; }
        .badge {
            font-size: 0.72rem;
            font-weight: 700;
            padding: 3px 10px;
            border-radius: 999px;
            text-transform: uppercase;
            letter-spacing: 0.03em;
        }
        .badge-acertou { background: #23863622; color: #3fb950; }
        .badge-errou { background: #f8514922; color: #f85149; }
        .badge-pendente { background: #8b949e22; color: #8b949e; }
        .flash {
            padding: 12px 16px; border-radius: 10px; margin-bottom: 18px; font-size: 0.85rem;
        }
        .flash-erro { background: #f8514922; color: #f85149; border: 1px solid #f8514955; }
        .flash-sucesso { background: #23863622; color: #3fb950; border: 1px solid #23863655; }
    </style>
</head>
<body>
    <h1>⚫⚪ Análise de Apostas</h1>
    <p class="subtitulo">Recomendações de múltiplas do Corinthians baseadas em padrões históricos</p>
    {{ nav_html|safe }}

    {% with mensagens = get_flashed_messages(with_categories=true) %}
        {% for categoria, texto in mensagens %}
        <div class="flash flash-{{ categoria }}">{{ texto }}</div>
        {% endfor %}
    {% endwith %}

    <div class="painel">
        <form method="GET" action="/">
            <div class="linha-filtro">
                <div>
                    <label for="odd_min">Odd mínima</label>
                    <input type="number" step="0.01" min="1.01" name="odd_min" id="odd_min" value="{{ odd_min }}">
                </div>
                <div>
                    <label for="odd_max">Odd máxima</label>
                    <input type="number" step="0.01" min="1.01" name="odd_max" id="odd_max" value="{{ odd_max }}">
                </div>
                <button type="submit">Gerar recomendações da rodada</button>
            </div>
            <p class="nota-espera">Se fizer mais de 1h desde a última atualização, pode demorar alguns segundos
                (o app aciona a busca de odds mais recentes antes de mostrar o resultado).</p>
        </form>
        {% if ultima_atualizacao_odds %}
        <div class="linha-atualizacao">
            <span>🔄 Última atualização de odds gerada às <b>{{ ultima_atualizacao_odds }}</b></span>
            <a href="/?odd_min={{ odd_min }}&odd_max={{ odd_max }}&forcar=1" class="link-atualizar">Atualizar recomendações</a>
        </div>
        {% endif %}
    </div>

    {% macro cartao_combo(c) %}
        <div class="cartao item-pagina">
            <div class="cartao-topo">
                <span class="jogo">{{ c.data_jogo }} · {{ c.nosso_time }} x {{ c.adversario }}</span>
                <span class="casa">{{ c.casa_aposta }}</span>
                <span class="odd-tag">ODD {{ c.odd_combinada }}</span>
            </div>
            <div class="descricao">{{ c.descricao }}</div>
            <div class="metricas">
                <span>Probabilidade histórica: <b>{{ c.probabilidade_combinada }}%</b></span>
                <span>Valor esperado: <b>{{ c.valor_esperado }}</b></span>
            </div>
            {% if c.ja_apostado %}
            <div class="ja-apostado">💰 R$ {{ "%.2f"|format(c.ja_apostado) }} já apostado nessa odd</div>
            {% endif %}
            <form method="POST" action="/salvar-aposta" class="salvar-linha">
                <input type="hidden" name="descricao" value="{{ c.descricao }}">
                <input type="hidden" name="casa_aposta" value="{{ c.casa_aposta }}">
                <input type="hidden" name="odd_combinada" value="{{ c.odd_combinada }}">
                <input type="hidden" name="probabilidade_combinada" value="{{ c.probabilidade_combinada }}">
                <input type="hidden" name="pernas" value='{{ c.pernas_json }}'>
                <input type="hidden" name="voltar" value="/?odd_min={{ odd_min }}&odd_max={{ odd_max }}">
                <input type="number" step="0.01" min="0.01" name="valor_apostado" placeholder="Valor (R$)" required>
                <button type="submit" class="btn-salvar">💾 Salvar</button>
            </form>
        </div>
    {% endmacro %}

    {% if buscou %}
        {% if individuais or multiplas %}
        <div class="colunas-resultado">
            <div class="coluna">
                <div class="coluna-cabecalho coluna-azul">🎯 Odds Individuais ({{ individuais|length }})</div>
                <div id="lista-individuais">
                    {% if individuais %}
                        {% for c in individuais %}{{ cartao_combo(c) }}{% endfor %}
                    {% else %}
                        <div class="vazio">Nenhuma odd individual disponível nessa faixa.</div>
                    {% endif %}
                </div>
                {% if individuais|length > 10 %}
                <div class="paginacao">
                    <button class="btn-pagina" id="anterior-lista-individuais" onclick="mudarPagina('lista-individuais', -1)">← Anterior</button>
                    <span id="label-lista-individuais"></span>
                    <button class="btn-pagina" id="proximo-lista-individuais" onclick="mudarPagina('lista-individuais', 1)">Próxima →</button>
                </div>
                {% endif %}
            </div>
            <div class="coluna">
                <div class="coluna-cabecalho coluna-roxa">🧩 Múltiplas ({{ multiplas|length }})</div>
                <div id="lista-multiplas">
                    {% if multiplas %}
                        {% for c in multiplas %}{{ cartao_combo(c) }}{% endfor %}
                    {% else %}
                        <div class="vazio">Nenhuma múltipla disponível nessa faixa.</div>
                    {% endif %}
                </div>
                {% if multiplas|length > 10 %}
                <div class="paginacao">
                    <button class="btn-pagina" id="anterior-lista-multiplas" onclick="mudarPagina('lista-multiplas', -1)">← Anterior</button>
                    <span id="label-lista-multiplas"></span>
                    <button class="btn-pagina" id="proximo-lista-multiplas" onclick="mudarPagina('lista-multiplas', 1)">Próxima →</button>
                </div>
                {% endif %}
            </div>
        </div>

        <script>
            const TAMANHO_PAGINA = 10;
            const paginaAtual = {};

            function totalPaginas(listaId) {
                const n = document.querySelectorAll('#' + listaId + ' .item-pagina').length;
                return Math.max(1, Math.ceil(n / TAMANHO_PAGINA));
            }

            function renderizarPagina(listaId) {
                const pagina = paginaAtual[listaId] || 0;
                const itens = document.querySelectorAll('#' + listaId + ' .item-pagina');
                itens.forEach(function(item, i) {
                    const paginaDoItem = Math.floor(i / TAMANHO_PAGINA);
                    item.style.display = (paginaDoItem === pagina) ? '' : 'none';
                });
                const total = totalPaginas(listaId);
                const label = document.getElementById('label-' + listaId);
                if (label) label.textContent = 'Página ' + (pagina + 1) + ' de ' + total;
                const btnAnterior = document.getElementById('anterior-' + listaId);
                const btnProximo = document.getElementById('proximo-' + listaId);
                if (btnAnterior) btnAnterior.disabled = (pagina === 0);
                if (btnProximo) btnProximo.disabled = (pagina >= total - 1);
            }

            function mudarPagina(listaId, direcao) {
                const total = totalPaginas(listaId);
                let pagina = (paginaAtual[listaId] || 0) + direcao;
                pagina = Math.max(0, Math.min(total - 1, pagina));
                paginaAtual[listaId] = pagina;
                renderizarPagina(listaId);
            }

            ['lista-individuais', 'lista-multiplas'].forEach(renderizarPagina);
        </script>
        {% else %}
            <div class="vazio">
                <div class="vazio-titulo">Nenhuma recomendação disponível no momento</div>
                {{ motivo }}
            </div>
        {% endif %}
    {% endif %}
</body>
</html>
"""


MERCADOS_JOGO_INTEIRO = {"escanteio_total", "cartao_total"}


def deduplicar_mercados_jogo_inteiro(recomendacoes, colunas_a_manter):
    """NOVO (multi-time): mercados "do jogo inteiro" (escanteio total,
    cartão total) descrevem a partida real inteira, não um lado específico
    - quando os DOIS times de um jogo são rastreados (ex: Corinthians x
    Athletico Paranaense), o mesmo jogo real gera uma linha separada por
    time (visões diferentes, por desenho), mas pra ESSES mercados
    específicos é literalmente A MESMA aposta real (mesma odd, mesmo
    evento) - só a estimativa de probabilidade diverge, vinda do histórico
    de times diferentes. Sem essa deduplicação, a mesma odd real aparecia
    duas vezes na tela, com "já apostado" compartilhado entre as duas
    cópias por engano (mesma descrição+casa, jogo_id diferente).
    Mantém só uma cópia por (fixture_id_api, descricao, casa) - a de maior
    probabilidade histórica, quando há divergência entre as duas visões.
    `colunas_a_manter` é quantas colunas manter no resultado final (a
    última coluna da query sempre precisa ser fixture_id_api, usado só
    aqui pra deduplicar e descartado depois)."""
    melhores = {}
    resultado = []
    for rec in recomendacoes:
        tipo_padrao = rec[8]
        if tipo_padrao not in MERCADOS_JOGO_INTEIRO:
            resultado.append(rec[:colunas_a_manter])
            continue
        fixture_id_api, descricao, casa, prob = rec[-1], rec[2], rec[3], rec[5]
        chave = (fixture_id_api, descricao, casa)
        if chave not in melhores or prob > melhores[chave][5]:
            melhores[chave] = rec

    resultado.extend(rec[:colunas_a_manter] for rec in melhores.values())
    return resultado


def buscar_recomendacoes(cur):
    cur.execute(
        """
        SELECT r.jogo_id, r.jogador_id, r.descricao, r.casa_aposta,
               r.odd_oferecida, r.probabilidade_historica, j.adversario, j.data_jogo,
               r.tipo_padrao, r.linha, r.direcao, t.nome, j.fixture_id_api
        FROM recomendacoes r
        JOIN jogos j ON j.id = r.jogo_id
        JOIN times t ON t.id = j.nosso_time_id
        """
    )
    return deduplicar_mercados_jogo_inteiro(cur.fetchall(), colunas_a_manter=12)


# NOVO: largura mínima de uma faixa, em "unidades de linha" (como as linhas
# são sempre .5, isso equivale ao número mínimo de valores inteiros que a
# faixa precisa cobrir pra ser aceita). Ex: "mais de 9.5" + "menos de 11.5"
# cobre só {10, 11} -> largura 2, fica de fora com o padrão de 3. Existe
# pra pegar o caso em que a casa só oferece linhas próximas mesmo pra faixa
# mais ampla possível - sem essa trava, uma faixa "tecnicamente a mais
# ampla disponível" ainda pode ser estreita demais pra ser uma aposta
# realista.
LARGURA_MINIMA_FAIXA = 3.0


def montar_combinacoes(recomendacoes, odd_min, odd_max):
    grupos = {}
    for rec in recomendacoes:
        (jogo_id, jogador_id, descricao, casa, odd, prob, adversario, data_jogo,
         tipo_padrao, linha, direcao, nosso_time) = rec

        # resultado final (1X2) só entra como candidato quando a faixa pedida
        # permite odds acima de 5.0 (mercado de alta variância)
        if tipo_padrao == "resultado_final" and odd_max <= 5.0:
            continue

        chave = (jogo_id, casa)
        grupos.setdefault(chave, []).append({
            "jogador_id": jogador_id,
            "tipo_padrao": tipo_padrao,
            "descricao": descricao,
            "odd": float(odd),
            "probabilidade": float(prob) / 100,
            "linha": float(linha) if linha is not None else None,
            "direcao": (direcao or "").strip().lower(),
            "adversario": adversario,
            "data_jogo": data_jogo,
            "nosso_time": nosso_time,
        })

    resultado = []
    for (jogo_id, casa), pernas in grupos.items():

        # NOVO: pra cada mercado (tipo_padrao + jogador_id) desse jogo, se a
        # casa oferece mais de uma linha "mais" e/ou mais de uma linha
        # "menos" pro mesmo mercado, a ÚNICA combinação de faixa permitida é
        # a mais ampla possível - o corte "mais" mais baixo disponível
        # combinado com o corte "menos" mais alto disponível. Isso evita
        # janelas estreitas tipo "mais de 9.5 + menos de 11.5" (só acerta
        # com 10 ou 11 escanteios exatos) quando a casa também oferecia,
        # por exemplo, "mais de 4.5" e "menos de 12.5" pro mesmo jogo - a
        # faixa estreita simplesmente não é gerada mais, só a ampla (e só se
        # ela também passar da largura mínima abaixo).
        faixa_permitida_por_mercado = {}
        pernas_por_mercado = {}
        for p in pernas:
            chave_mercado = (p["tipo_padrao"], p["jogador_id"])
            pernas_por_mercado.setdefault(chave_mercado, []).append(p)

        for chave_mercado, legs in pernas_por_mercado.items():
            candidatos_mais = [p for p in legs if p["direcao"] == "mais" and p["linha"] is not None]
            candidatos_menos = [p for p in legs if p["direcao"] == "menos" and p["linha"] is not None]
            if not candidatos_mais or not candidatos_menos:
                continue
            leg_mais = min(candidatos_mais, key=lambda p: p["linha"])
            leg_menos = max(candidatos_menos, key=lambda p: p["linha"])
            if leg_mais["linha"] < leg_menos["linha"] \
                    and (leg_menos["linha"] - leg_mais["linha"]) >= LARGURA_MINIMA_FAIXA:
                faixa_permitida_por_mercado[chave_mercado] = {id(leg_mais), id(leg_menos)}

        for tamanho in (1, 2, 3, 4, 5):
            if len(pernas) < tamanho:
                continue
            for combo in combinations(pernas, tamanho):
                # NOVO: em vez de bloquear TODA repetição de (tipo_padrao,
                # jogador_id), agora existe uma exceção específica: duas
                # pernas do mesmo mercado que formam uma FAIXA coerente
                # (ex: "Mais de 3.5" + "Menos de 7.5" escanteios do
                # Corinthians = "entre 4 e 7 escanteios"). Isso é uma
                # aposta genuinamente nova, não uma repetição redundante -
                # mas a probabilidade dela NÃO pode ser calculada
                # multiplicando as duas probabilidades individuais (elas
                # não são eventos independentes, são dois cortes da MESMA
                # variável). A fórmula certa: P(faixa) = P(mais do corte
                # menor) + P(menos do corte maior) - 1 - equivalente a
                # "P(mais do corte menor) menos P(mais do corte maior)",
                # calculada só com o que já temos, sem precisar contar
                # jogo por jogo de novo.
                #
                # Qualquer OUTRA repetição de mercado (duas pernas "mais",
                # duas "menos", ou mais de 2 pernas do mesmo mercado)
                # continua bloqueada, exatamente como antes.
                contagem_mercado = {}
                for p in combo:
                    chave_mercado = (p["tipo_padrao"], p["jogador_id"])
                    contagem_mercado.setdefault(chave_mercado, []).append(p)

                valido = True
                faixa_chave = None
                faixa_probabilidade = None

                for chave_mercado, pernas_do_mercado in contagem_mercado.items():
                    if len(pernas_do_mercado) == 1:
                        continue
                    if len(pernas_do_mercado) > 2:
                        valido = False
                        break

                    a, b = pernas_do_mercado
                    if a["linha"] is None or b["linha"] is None or a["direcao"] == b["direcao"] \
                            or {a["direcao"], b["direcao"]} != {"mais", "menos"}:
                        valido = False
                        break

                    # NOVO: só aceita esse par se ele for exatamente o par
                    # mais amplo (e largo o suficiente) calculado acima pra
                    # esse mercado - qualquer outro par "mais"/"menos" do
                    # mesmo mercado (mais estreito) é descartado aqui.
                    par_permitido = faixa_permitida_por_mercado.get(chave_mercado)
                    if par_permitido is None or {id(a), id(b)} != par_permitido:
                        valido = False
                        break

                    leg_mais = a if a["direcao"] == "mais" else b
                    leg_menos = a if a["direcao"] == "menos" else b

                    # garante que é uma faixa de verdade (corte de "mais"
                    # estritamente menor que o corte de "menos") - senão a
                    # combinação é impossível (ex: "mais de 7.5" + "menos
                    # de 3.5" nunca acontecem juntos)
                    if leg_mais["linha"] >= leg_menos["linha"]:
                        valido = False
                        break

                    prob_faixa = leg_mais["probabilidade"] + leg_menos["probabilidade"] - 1
                    if prob_faixa <= 0:
                        valido = False
                        break

                    faixa_chave = chave_mercado
                    faixa_probabilidade = prob_faixa

                if not valido:
                    continue

                odd_combinada = 1.0
                prob_combinada = 1.0
                faixa_ja_contabilizada = False
                for p in combo:
                    odd_combinada *= p["odd"]  # odd real de cada perna sempre multiplica normalmente
                    chave_mercado = (p["tipo_padrao"], p["jogador_id"])
                    if faixa_chave is not None and chave_mercado == faixa_chave:
                        if not faixa_ja_contabilizada:
                            prob_combinada *= faixa_probabilidade
                            faixa_ja_contabilizada = True
                        # a segunda perna da faixa não conta probabilidade
                        # de novo - já foi contabilizada junto, uma única vez
                    else:
                        prob_combinada *= p["probabilidade"]

                if not (odd_min <= odd_combinada <= odd_max):
                    continue

                valor_esperado = round((prob_combinada * odd_combinada) - 1, 3)

                # NOVO: mesmo com pernas individuais sempre positivas, a
                # combinação de "faixa" usa uma fórmula mais rigorosa
                # (subtração, não multiplicação) que pode revelar VE
                # negativo mesmo quando as duas pernas separadas eram boas
                # - sem esse filtro, o site mostrava múltiplas com VE
                # negativo como se fossem recomendação, contradizendo o
                # propósito de só sugerir apostas com vantagem matemática.
                if valor_esperado <= 0:
                    continue

                descricao_final = " + ".join(p["descricao"] for p in combo)
                if faixa_chave is not None:
                    descricao_final += " (faixa)"

                resultado.append({
                    "jogo_id": jogo_id,
                    "casa_aposta": casa,
                    "descricao": descricao_final,
                    "odd_combinada": round(odd_combinada, 2),
                    "probabilidade_combinada": round(prob_combinada * 100, 2),
                    "valor_esperado": valor_esperado,
                    "adversario": combo[0]["adversario"],
                    "data_jogo": combo[0]["data_jogo"],
                    "nosso_time": combo[0]["nosso_time"],
                    "pernas": [
                        {
                            "jogo_id": jogo_id,
                            "jogador_id": p["jogador_id"],
                            "tipo_padrao": p["tipo_padrao"],
                            "descricao": p["descricao"],
                        }
                        for p in combo
                    ],
                })
    for c in resultado:
        c["pernas_json"] = json.dumps(c["pernas"], ensure_ascii=False)

    # NOVO: ordena por probabilidade histórica (maior primeiro), não mais
    # por valor esperado - ajuda visualmente, sem precisar procurar a
    # aposta mais confiável no meio da lista.
    resultado.sort(key=lambda c: c["probabilidade_combinada"], reverse=True)
    return resultado


def descobrir_motivo(cur):
    """Quando não há recomendação, descobre e explica o motivo mais provável."""
    cur.execute("SELECT COUNT(*) FROM jogos WHERE data_jogo >= CURRENT_DATE")
    tem_jogo_proximo = cur.fetchone()[0] > 0

    if not tem_jogo_proximo:
        return "Não há jogo do Corinthians nos próximos dias no momento."

    cur.execute(
        "SELECT COUNT(*) FROM odds o JOIN jogos j ON j.id = o.jogo_id WHERE j.data_jogo >= CURRENT_DATE"
    )
    tem_odds = cur.fetchone()[0] > 0

    if not tem_odds:
        return ("Tem jogo próximo, mas as odds ainda não foram coletadas "
                "(isso acontece automaticamente a partir de 2 dias antes do jogo).")

    return ("Tem jogo e odds coletadas, mas nenhum padrão histórico correspondente "
            "foi encontrado ainda para os mercados disponíveis.")


PAGINA_HISTORICO = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Histórico - Análise de Apostas</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
            background: #0d1117;
            color: #e6edf3;
            max-width: 900px;
            margin: 0 auto;
            padding: 32px 20px 80px;
        }
        h1 { font-size: 1.5rem; margin: 0 0 4px; }
        .subtitulo { color: #8b949e; margin: 0 0 20px; }
        .link-voltar { color: #8b949e; text-decoration: none; font-size: 0.85rem; }
        .link-voltar:hover { text-decoration: underline; }
        .resumo-grid { display: flex; gap: 14px; margin: 20px 0 28px; flex-wrap: wrap; }
        .resumo-card {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 16px 22px; flex: 1; min-width: 130px; text-align: center;
        }
        .resumo-numero { font-size: 1.6rem; font-weight: 700; }
        .resumo-label { color: #8b949e; font-size: 0.78rem; margin-top: 4px; }
        .calibracao {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 14px 20px; margin-bottom: 24px; cursor: pointer; transition: border-color 0.15s;
        }
        .calibracao:hover { border-color: #58a6ff; }
        .calibracao-titulo { font-size: 0.88rem; font-weight: 700; margin-bottom: 6px; }
        .calibracao-nota { color: #8b949e; font-size: 0.75rem; }
        .modal-fundo {
            display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.6); z-index: 100; align-items: center; justify-content: center;
        }
        .modal-caixa {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 20px 24px; max-width: 420px; width: 90%; max-height: 70vh; overflow-y: auto;
        }
        .modal-topo { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
        .modal-titulo { font-weight: 700; font-size: 0.95rem; }
        .modal-fechar { cursor: pointer; color: #8b949e; font-size: 1.1rem; }
        .modal-fechar:hover { color: #e6edf3; }
        .modal-linha {
            display: flex; justify-content: space-between; padding: 8px 0;
            border-bottom: 1px solid #21262d; font-size: 0.85rem;
        }
        .cartao {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 16px 20px; margin-bottom: 12px;
        }
        .cartao-topo {
            display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 8px; flex-wrap: wrap; gap: 8px;
        }
        .jogo { font-weight: 600; font-size: 0.88rem; }
        .descricao { color: #c9d1d9; font-size: 0.88rem; margin-bottom: 8px; line-height: 1.5; }
        .metricas { display: flex; gap: 18px; font-size: 0.78rem; color: #8b949e; flex-wrap: wrap; }
        .metricas b { color: #e6edf3; }
        .badge {
            font-size: 0.72rem; font-weight: 700; padding: 3px 10px; border-radius: 999px;
            text-transform: uppercase; letter-spacing: 0.03em;
        }
        .badge-acertou { background: #23863622; color: #3fb950; }
        .badge-errou { background: #f8514922; color: #f85149; }
        .badge-pendente { background: #8b949e22; color: #8b949e; }
        .vazio {
            text-align: center; color: #8b949e; padding: 32px 24px;
            background: #161b22; border: 1px dashed #30363d; border-radius: 12px; font-size: 0.9rem;
        }
        .secao-titulo { font-size: 1.05rem; margin: 32px 0 4px; }
        .secao-subtitulo { color: #8b949e; font-size: 0.82rem; margin: 0 0 16px; }
        .salvar-linha {
            display: flex; gap: 8px; align-items: center; margin-top: 10px;
            padding-top: 10px; border-top: 1px solid #21262d;
        }
        .salvar-linha input[type=number] { width: 110px; padding: 7px 10px; font-size: 0.85rem;
            background: #0d1117; border: 1px solid #30363d; color: #e6edf3; border-radius: 8px; }
        .btn-salvar {
            background: #1f6feb; color: white; border: none; border-radius: 8px;
            padding: 8px 16px; font-size: 0.82rem; font-weight: 600; cursor: pointer;
        }
        .btn-salvar:hover { background: #388bfd; }
        .ja-apostado { color: #d29922; font-size: 0.78rem; margin-top: 8px; }
        .odd-tag {
            background: #23863622; color: #3fb950; font-weight: 700; font-size: 1rem;
            padding: 3px 12px; border-radius: 8px;
        }
        .casa {
            background: #1f6feb22; color: #58a6ff; font-size: 0.72rem; padding: 3px 10px;
            border-radius: 999px; text-transform: uppercase; letter-spacing: 0.03em;
        }
        .colunas-resultado {
            display: grid; grid-template-columns: 1fr 1fr; gap: 18px; align-items: start;
            margin-bottom: 28px;
        }
        @media (max-width: 640px) {
            .colunas-resultado { grid-template-columns: 1fr; }
        }
        .coluna-cabecalho {
            border-radius: 10px; padding: 10px 16px; font-weight: 700; font-size: 0.92rem;
            margin-bottom: 12px; text-align: center;
        }
        .coluna-verde { background: #23863633; color: #3fb950; border: 1px solid #3fb95055; }
        .coluna-vermelha { background: #f8514933; color: #f85149; border: 1px solid #f8514955; }
        .coluna-cinza { background: #8b949e22; color: #8b949e; border: 1px solid #8b949e55; }
        .coluna-roxa { background: #a371f722; color: #a371f7; border: 1px solid #a371f755; }
        .secao-pendentes { margin-bottom: 28px; }
        .paginacao {
            display: flex; align-items: center; justify-content: center; gap: 14px;
            margin-top: 4px; font-size: 0.82rem; color: #8b949e;
        }
        .btn-pagina {
            background: #161b22; border: 1px solid #30363d; color: #e6edf3;
            border-radius: 8px; padding: 6px 14px; font-size: 0.85rem; cursor: pointer;
        }
        .btn-pagina:hover:not(:disabled) { border-color: #58a6ff; }
        .btn-pagina:disabled { opacity: 0.35; cursor: default; }
        """ + NAV_CSS + """
    </style>
</head>
<body>
    <h1>📊 Histórico de Acertos e Erros</h1>
    <p class="subtitulo">Recomendações já avaliadas contra o resultado real dos jogos</p>
    {{ nav_html|safe }}

    <div class="resumo-grid">
        <div class="resumo-card">
            <div class="resumo-numero" style="color:#3fb950">{{ resumo.acertou }}</div>
            <div class="resumo-label">Acertou</div>
        </div>
        <div class="resumo-card">
            <div class="resumo-numero" style="color:#f85149">{{ resumo.errou }}</div>
            <div class="resumo-label">Errou</div>
        </div>
        <div class="resumo-card">
            <div class="resumo-numero" style="color:#8b949e">{{ resumo.pendente }}</div>
            <div class="resumo-label">Pendente</div>
        </div>
        <div class="resumo-card">
            <div class="resumo-numero">{{ resumo.taxa }}%</div>
            <div class="resumo-label">Taxa de acerto (avaliadas)</div>
        </div>
    </div>

    {% if calibracao.melhor_faixa %}
    <div class="calibracao" onclick="document.getElementById('modal-calibracao').style.display='flex'">
        <div class="calibracao-titulo">📐 A maior taxa de acerto está entre {{ calibracao.melhor_faixa.inicio }}%
            e {{ calibracao.melhor_faixa.fim }}% de probabilidade histórica</div>
        <div class="calibracao-nota">{{ calibracao.melhor_faixa.taxa }}% de acerto nessa faixa
            ({{ calibracao.melhor_faixa.total }} aposta(s) resolvida(s) nela) - clique pra ver o detalhamento
            completo por probabilidade. Com poucas apostas resolvidas ainda, isso é instável - fica mais
            confiável conforme o histórico crescer.</div>
    </div>

    <div id="modal-calibracao" class="modal-fundo" onclick="if(event.target===this) this.style.display='none'">
        <div class="modal-caixa">
            <div class="modal-topo">
                <span class="modal-titulo">Acertos e erros por probabilidade histórica</span>
                <span class="modal-fechar" onclick="document.getElementById('modal-calibracao').style.display='none'">✕</span>
            </div>
            {% for item in calibracao.detalhamento %}
            <div class="modal-linha">
                <span>{{ item.probabilidade }}%</span>
                <span><b style="color:#f85149">{{ item.errou }}</b> erro(s) /
                      <b style="color:#3fb950">{{ item.acertou }}</b> acerto(s)</span>
            </div>
            {% endfor %}
        </div>
    </div>
    {% endif %}

    {% macro cartao_item(i) %}
        <div class="cartao item-pagina">
            <div class="cartao-topo">
                <span class="jogo">{{ i.data_jogo }} · {{ i.nosso_time }} x {{ i.adversario }}</span>
                <span class="badge badge-{{ i.resultado }}">{{ i.resultado }}</span>
            </div>
            <div class="descricao">{{ i.descricao }}</div>
            <div class="metricas">
                <span>{{ i.casa_aposta }}</span>
                <span>Odd: <b>{{ i.odd_oferecida }}</b></span>
                <span>Probabilidade: <b>{{ i.probabilidade_historica }}%</b></span>
                <span>VE: <b>{{ i.valor_esperado }}</b></span>
            </div>
        </div>
    {% endmacro %}

    <div class="colunas-resultado">
        <div class="coluna">
            <div class="coluna-cabecalho coluna-verde">✅ Acertou ({{ acertos|length }})</div>
            <div id="lista-acertou">
                {% if acertos %}
                    {% for i in acertos %}{{ cartao_item(i) }}{% endfor %}
                {% else %}
                    <div class="vazio">Nenhum acerto ainda.</div>
                {% endif %}
            </div>
            {% if acertos|length > 10 %}
            <div class="paginacao">
                <button class="btn-pagina" id="anterior-lista-acertou" onclick="mudarPagina('lista-acertou', -1)">← Anterior</button>
                <span id="label-lista-acertou"></span>
                <button class="btn-pagina" id="proximo-lista-acertou" onclick="mudarPagina('lista-acertou', 1)">Próxima →</button>
            </div>
            {% endif %}
        </div>
        <div class="coluna">
            <div class="coluna-cabecalho coluna-vermelha">❌ Errou ({{ erros|length }})</div>
            <div id="lista-errou">
                {% if erros %}
                    {% for i in erros %}{{ cartao_item(i) }}{% endfor %}
                {% else %}
                    <div class="vazio">Nenhum erro ainda.</div>
                {% endif %}
            </div>
            {% if erros|length > 10 %}
            <div class="paginacao">
                <button class="btn-pagina" id="anterior-lista-errou" onclick="mudarPagina('lista-errou', -1)">← Anterior</button>
                <span id="label-lista-errou"></span>
                <button class="btn-pagina" id="proximo-lista-errou" onclick="mudarPagina('lista-errou', 1)">Próxima →</button>
            </div>
            {% endif %}
        </div>
    </div>

    <div class="secao-pendentes">
        <div class="coluna-cabecalho coluna-cinza">⏳ Pendente ({{ pendentes|length }})</div>
        <div id="lista-pendente">
            {% if pendentes %}
                {% for i in pendentes %}{{ cartao_item(i) }}{% endfor %}
            {% else %}
                <div class="vazio">Nenhuma recomendação pendente no momento.</div>
            {% endif %}
        </div>
        {% if pendentes|length > 10 %}
        <div class="paginacao">
            <button class="btn-pagina" id="anterior-lista-pendente" onclick="mudarPagina('lista-pendente', -1)">← Anterior</button>
            <span id="label-lista-pendente"></span>
            <button class="btn-pagina" id="proximo-lista-pendente" onclick="mudarPagina('lista-pendente', 1)">Próxima →</button>
        </div>
        {% endif %}
    </div>

    <script>
        const TAMANHO_PAGINA = 10;
        const paginaAtual = {};

        function totalPaginas(listaId) {
            const n = document.querySelectorAll('#' + listaId + ' .item-pagina').length;
            return Math.max(1, Math.ceil(n / TAMANHO_PAGINA));
        }

        function renderizarPagina(listaId) {
            const pagina = paginaAtual[listaId] || 0;
            const itens = document.querySelectorAll('#' + listaId + ' .item-pagina');
            itens.forEach(function(item, i) {
                const paginaDoItem = Math.floor(i / TAMANHO_PAGINA);
                item.style.display = (paginaDoItem === pagina) ? '' : 'none';
            });
            const total = totalPaginas(listaId);
            const label = document.getElementById('label-' + listaId);
            if (label) label.textContent = 'Página ' + (pagina + 1) + ' de ' + total;
            const btnAnterior = document.getElementById('anterior-' + listaId);
            const btnProximo = document.getElementById('proximo-' + listaId);
            if (btnAnterior) btnAnterior.disabled = (pagina === 0);
            if (btnProximo) btnProximo.disabled = (pagina >= total - 1);
        }

        function mudarPagina(listaId, direcao) {
            const total = totalPaginas(listaId);
            let pagina = (paginaAtual[listaId] || 0) + direcao;
            pagina = Math.max(0, Math.min(total - 1, pagina));
            paginaAtual[listaId] = pagina;
            renderizarPagina(listaId);
        }

        ['lista-acertou', 'lista-errou', 'lista-pendente'].forEach(renderizarPagina);
    </script>

    {% if multiplas_destaque %}
    <div class="coluna-cabecalho coluna-roxa" style="margin-top: 8px;">🎯 Múltiplas em Destaque ({{ multiplas_destaque|length }})</div>
    <p class="secao-subtitulo">Combinações de 2+ apostas com probabilidade histórica de {{ piso }}% ou mais, SÓ de jogos
        que já terminaram - é uma lista de referência pra ver como essas combinações teriam saído, não tem relação
        com sua banca/ROI nem botão de salvar (não dá pra apostar num jogo que já aconteceu).</p>
    {% endif %}
    {% for c in multiplas_destaque %}
        <div class="cartao">
            <div class="cartao-topo">
                <span class="jogo">{{ c.data_jogo }} · {{ c.nosso_time }} x {{ c.adversario }}</span>
                <span class="badge badge-{{ c.resultado }}">{{ c.resultado }}</span>
            </div>
            <div class="descricao">{{ c.descricao }}</div>
            <div class="metricas">
                <span>{{ c.casa_aposta }}</span>
                <span>Odd: <b>{{ c.odd_combinada }}</b></span>
                <span>Probabilidade histórica: <b>{{ c.probabilidade_combinada }}%</b></span>
                <span>Valor esperado: <b>{{ c.valor_esperado }}</b></span>
            </div>
        </div>
        {% endfor %}
</body>
</html>
"""


def buscar_historico(cur, limite=5000):
    """NOVO: limite subiu de 100 pra 5000 (na prática, "tudo") - agora que
    /historico pagina de 10 em 10 por coluna, não tem mais motivo pra
    cortar em 100. Isso também elimina a causa de um problema real: o
    resumo no topo da página contava TODAS as linhas do banco (sem
    limite), enquanto as colunas só mostravam as últimas 100 já
    deduplicadas - com o limite alto, as duas contagens usam a mesma base
    e nunca mais discordam (ver montar_resumo_historico, que agora deriva
    os números do MESMO `itens` que alimenta as colunas, em vez de rodar
    uma contagem separada no banco)."""
    cur.execute(
        """
        SELECT h.data_jogo, j.adversario, h.descricao, h.casa_aposta,
               h.odd_oferecida, h.probabilidade_historica, h.valor_esperado, h.resultado, t.nome,
               h.tipo_padrao, j.fixture_id_api
        FROM historico_recomendacoes h
        JOIN jogos j ON j.id = h.jogo_id
        JOIN times t ON t.id = j.nosso_time_id
        ORDER BY h.data_jogo DESC, h.id DESC
        LIMIT %s
        """,
        (limite,),
    )
    colunas = ["data_jogo", "adversario", "descricao", "casa_aposta",
               "odd_oferecida", "probabilidade_historica", "valor_esperado", "resultado", "nosso_time",
               "tipo_padrao", "fixture_id_api"]
    itens = [dict(zip(colunas, row)) for row in cur.fetchall()]

    # NOVO (multi-time): mesma deduplicação de mercados "do jogo inteiro"
    # aplicada em buscar_recomendacoes - ver docstring de
    # deduplicar_mercados_jogo_inteiro pra entender o motivo.
    melhores = {}
    resultado = []
    for item in itens:
        if item["tipo_padrao"] not in MERCADOS_JOGO_INTEIRO:
            resultado.append(item)
            continue
        chave = (item["fixture_id_api"], item["descricao"], item["casa_aposta"])
        if chave not in melhores or item["probabilidade_historica"] > melhores[chave]["probabilidade_historica"]:
            melhores[chave] = item
    resultado.extend(melhores.values())
    resultado.sort(key=lambda i: i["data_jogo"], reverse=True)
    return resultado


def montar_resumo_historico(itens):
    """NOVO: substitui buscar_resumo_historico (que fazia um COUNT(*) bruto,
    direto no banco, contando TODAS as linhas sem aplicar a deduplicação de
    "mercados do jogo inteiro" nem respeitar o mesmo recorte que as colunas
    mostram - por isso o número do topo às vezes não batia com a soma das
    colunas). Agora recebe o MESMO `itens` (já deduplicado) que alimenta as
    colunas Acertou/Errou/Pendente, então os números sempre batem."""
    acertou = sum(1 for i in itens if i["resultado"] == "acertou")
    errou = sum(1 for i in itens if i["resultado"] == "errou")
    pendente = sum(1 for i in itens if i["resultado"] == "pendente")
    total_avaliado = acertou + errou
    taxa = round(100 * acertou / total_avaliado, 1) if total_avaliado else 0
    return {"acertou": acertou, "errou": errou, "pendente": pendente, "taxa": taxa}


def buscar_calibracao(cur):
    """NOVO: checagem de calibração - mostra, pra cada valor EXATO de
    probabilidade histórica já visto, quantas vezes acertou e quantas errou
    (detalhamento, mostrado no pop-up), e identifica qual FAIXA de 20% em
    20% (0-20%, 20-40%, ...) teve a maior taxa de acerto (frase de
    destaque). Com poucas apostas resolvidas ainda, isso é instável - fica
    mais confiável conforme o histórico crescer."""
    cur.execute(
        """
        SELECT probabilidade_historica, resultado, COUNT(*)
        FROM historico_recomendacoes
        WHERE resultado IN ('acertou', 'errou')
        GROUP BY probabilidade_historica, resultado
        ORDER BY probabilidade_historica DESC
        """
    )
    por_valor = {}
    for prob, resultado, contagem in cur.fetchall():
        prob_float = float(prob)
        por_valor.setdefault(prob_float, {"acertou": 0, "errou": 0})
        por_valor[prob_float][resultado] = contagem

    detalhamento = [
        {"probabilidade": prob, "acertou": dados["acertou"], "errou": dados["errou"]}
        for prob, dados in sorted(por_valor.items(), reverse=True)
    ]

    # agrupa em faixas de 20% pra achar a de maior taxa de acerto
    faixas = {}
    for item in detalhamento:
        inicio_faixa = int(item["probabilidade"] // 20) * 20
        faixas.setdefault(inicio_faixa, {"acertou": 0, "errou": 0})
        faixas[inicio_faixa]["acertou"] += item["acertou"]
        faixas[inicio_faixa]["errou"] += item["errou"]

    melhor_faixa = None
    melhor_taxa = -1
    for inicio_faixa, dados in faixas.items():
        total = dados["acertou"] + dados["errou"]
        if total == 0:
            continue
        taxa_faixa = dados["acertou"] / total
        if taxa_faixa > melhor_taxa:
            melhor_taxa = taxa_faixa
            melhor_faixa = {
                "inicio": inicio_faixa, "fim": inicio_faixa + 20,
                "taxa": round(taxa_faixa * 100, 1), "total": total,
            }

    return {"detalhamento": detalhamento, "melhor_faixa": melhor_faixa}


# NOVO: piso de probabilidade histórica pra uma múltipla aparecer na lista
# de "múltiplas em destaque" do /historico. Só controla ESSA lista - não
# afeta a página principal (onde as odds são geradas, sem piso nenhum) nem
# o botão de salvar aposta (que funciona em qualquer probabilidade). Existe
# só pra evitar que combinações de chance muito baixa (que erram na maioria
# das vezes só por natureza estatística, mesmo estando matematicamente
# corretas) dominem essa lista de referência.
PISO_PROBABILIDADE_MULTIPLAS_DESTAQUE = 40


def buscar_recomendacoes_historico(cur):
    """NOVO: mesma estrutura de buscar_recomendacoes, mas lendo de
    historico_recomendacoes (jogos já concluídos) em vez de recomendacoes
    (jogos futuros ainda ativos)."""
    cur.execute(
        """
        SELECT h.jogo_id, h.jogador_id, h.descricao, h.casa_aposta,
               h.odd_oferecida, h.probabilidade_historica, j.adversario, j.data_jogo,
               h.tipo_padrao, h.resultado, t.nome, j.fixture_id_api
        FROM historico_recomendacoes h
        JOIN jogos j ON j.id = h.jogo_id
        JOIN times t ON t.id = j.nosso_time_id
        """
    )
    return deduplicar_mercados_jogo_inteiro(cur.fetchall(), colunas_a_manter=11)


def montar_combinacoes_historico(recomendacoes, piso_probabilidade):
    """NOVO: monta combinações (1 a 5 pernas) a partir de recomendações JÁ
    CONCLUÍDAS, calculando também o resultado real da combinação: só
    'acertou' se TODAS as pernas acertaram; 'errou' se qualquer perna
    errou; 'pendente' se sobrar alguma perna sem dado ainda. Filtra só as
    combinações com probabilidade histórica >= piso, pra não poluir a lista
    com combinações de chance muito baixa."""
    grupos = {}
    for rec in recomendacoes:
        (jogo_id, jogador_id, descricao, casa, odd, prob, adversario, data_jogo,
         tipo_padrao, resultado_perna, nosso_time) = rec

        chave = (jogo_id, casa)
        grupos.setdefault(chave, []).append({
            "jogador_id": jogador_id,
            "tipo_padrao": tipo_padrao,
            "descricao": descricao,
            "odd": float(odd),
            "probabilidade": float(prob) / 100,
            "adversario": adversario,
            "data_jogo": data_jogo,
            "resultado": resultado_perna,
            "nosso_time": nosso_time,
        })

    resultado_final = []
    for (jogo_id, casa), pernas in grupos.items():
        # NOVO: só combinações de 2+ pernas aqui - tamanho=1 seria a mesma
        # aposta individual já mostrada na lista principal do histórico,
        # gerando entrada duplicada.
        for tamanho in (2, 3, 4, 5):
            if len(pernas) < tamanho:
                continue
            for combo in combinations(pernas, tamanho):
                chaves_mercado = [(p["tipo_padrao"], p["jogador_id"]) for p in combo]
                if len(chaves_mercado) != len(set(chaves_mercado)):
                    continue

                odd_combinada = 1.0
                prob_combinada = 1.0
                for p in combo:
                    odd_combinada *= p["odd"]
                    prob_combinada *= p["probabilidade"]

                prob_pct = round(prob_combinada * 100, 2)
                if prob_pct < piso_probabilidade:
                    continue

                resultados_pernas = [p["resultado"] for p in combo]
                if any(r == "errou" for r in resultados_pernas):
                    resultado_combo = "errou"
                elif all(r == "acertou" for r in resultados_pernas):
                    resultado_combo = "acertou"
                else:
                    resultado_combo = "pendente"

                valor_esperado = round((prob_combinada * odd_combinada) - 1, 3)
                resultado_final.append({
                    "casa_aposta": casa,
                    "descricao": " + ".join(p["descricao"] for p in combo),
                    "odd_combinada": round(odd_combinada, 2),
                    "probabilidade_combinada": prob_pct,
                    "valor_esperado": valor_esperado,
                    "adversario": combo[0]["adversario"],
                    "data_jogo": combo[0]["data_jogo"],
                    "nosso_time": combo[0]["nosso_time"],
                    "resultado": resultado_combo,
                })

    resultado_final.sort(key=lambda c: c["probabilidade_combinada"], reverse=True)
    return resultado_final[:15]


def buscar_multiplas_destaque(cur):
    """NOVO: múltiplas de jogos JÁ CONCLUÍDOS (não jogos futuros ainda
    ativos), com probabilidade histórica >= piso, mostrando o resultado
    real de cada uma (acertou/errou/pendente) - lista de referência, sem
    relação com dinheiro/ROI (não dá pra "apostar" num jogo que já
    aconteceu, por isso essa lista não tem botão de salvar)."""
    recomendacoes = buscar_recomendacoes_historico(cur)
    return montar_combinacoes_historico(recomendacoes, PISO_PROBABILIDADE_MULTIPLAS_DESTAQUE)


@app.route("/historico")
def historico():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        itens = buscar_historico(cur)
        calibracao = buscar_calibracao(cur)
        multiplas_destaque = buscar_multiplas_destaque(cur)
        banca_atual = buscar_banca(cur, session["usuario_id"])
        cur.close()
    finally:
        conn.close()

    # NOVO: separa acertos, erros e pendentes em listas próprias, uma pra
    # cada coluna (ver PAGINA_HISTORICO) - antes vinham todos misturados na
    # ordem cronológica, dificultando enxergar o padrão de acerto/erro.
    acertos = [i for i in itens if i["resultado"] == "acertou"]
    erros = [i for i in itens if i["resultado"] == "errou"]
    pendentes = [i for i in itens if i["resultado"] == "pendente"]

    # NOVO (corrige contagem que não batia): o resumo do topo agora é
    # calculado a partir do MESMO `itens` que alimenta as colunas (ver
    # montar_resumo_historico), em vez de uma contagem separada no banco -
    # os números do topo e das colunas nunca mais vão discordar.
    resumo = montar_resumo_historico(itens)

    return render_template_string(
        PAGINA_HISTORICO, acertos=acertos, erros=erros, pendentes=pendentes,
        resumo=resumo, calibracao=calibracao,
        multiplas_destaque=multiplas_destaque,
        piso=PISO_PROBABILIDADE_MULTIPLAS_DESTAQUE,
        nav_html=barra_navegacao("historico", round(banca_atual, 2)),
    )


@app.before_request
def exigir_login():
    """NOVO: protege o app INTEIRO (não só ROI/apostas) - qualquer página,
    sem estar logado, redireciona pro login. Exceções: a própria página de
    login, e o proxy de escudo (é só uma imagem pública, sem dado
    pessoal)."""
    rotas_livres = ("login",)
    if request.endpoint in rotas_livres or (request.endpoint or "").startswith("escudo"):
        return
    if request.endpoint == "static":
        return
    if "usuario_id" not in session:
        return redirect(url_for("login"))


PAGINA_LOGIN = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - Análise de Apostas</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
            background: #0d1117; color: #e6edf3; max-width: 480px;
            margin: 80px auto; padding: 0 20px; text-align: center;
        }
        h1 { font-size: 1.4rem; margin-bottom: 4px; }
        .subtitulo { color: #8b949e; font-size: 0.85rem; margin-bottom: 32px; }
        .grid-usuarios {
            display: flex; flex-wrap: wrap; gap: 22px; justify-content: center;
        }
        .avatar-usuario {
            display: flex; flex-direction: column; align-items: center; gap: 8px;
            cursor: pointer; background: none; border: none; padding: 0;
        }
        .avatar-circulo {
            width: 64px; height: 64px; border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            color: white; font-size: 1.5rem; font-weight: 700;
            border: 2px solid transparent; transition: border-color 0.15s;
        }
        .avatar-usuario.ativo .avatar-circulo { border-color: #58a6ff; }
        .avatar-nome { font-size: 0.82rem; color: #c9d1d9; }
        .senha-box { display: none; margin-top: 20px; }
        .senha-box.ativo { display: block; }
        .senha-box input {
            width: 100%; padding: 10px 12px; margin-bottom: 10px;
            background: #161b22; border: 1px solid #30363d; color: #e6edf3;
            border-radius: 8px; font-size: 0.9rem; text-align: center;
        }
        .senha-box button {
            width: 100%; padding: 10px; background: #1f6feb; color: white;
            border: none; border-radius: 8px; font-size: 0.9rem; font-weight: 600;
            cursor: pointer;
        }
        .senha-box button:hover { background: #388bfd; }
        .erro { color: #f85149; font-size: 0.82rem; margin-top: 16px; }
        .vazio { color: #8b949e; font-size: 0.85rem; }
    </style>
</head>
<body>
    <h1>⚫⚪ Análise de Apostas</h1>
    <p class="subtitulo">Selecione seu usuário pra entrar</p>

    {% if usuarios %}
    <div class="grid-usuarios" id="grid-usuarios">
        {% for u in usuarios %}
        <button type="button" class="avatar-usuario" id="avatar-{{ u.id }}"
                onclick="selecionarUsuario({{ u.id }}, '{{ u.nome }}')">
            <div class="avatar-circulo" style="background:{{ u.cor_avatar }};">
                {{ u.nome[0]|upper }}
            </div>
            <div class="avatar-nome">{{ u.nome }}</div>
        </button>
        {% endfor %}
    </div>

    <div class="senha-box" id="senha-box">
        <form method="POST">
            <input type="hidden" name="usuario_id" id="campo-usuario-id">
            <input type="password" name="senha" id="campo-senha" placeholder="Senha" required autofocus>
            <button type="submit">Entrar</button>
        </form>
    </div>
    {% else %}
    <div class="vazio">Nenhum usuário cadastrado ainda.</div>
    {% endif %}

    {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}

    <script>
        function selecionarUsuario(id, nome) {
            document.querySelectorAll('.avatar-usuario').forEach(el => el.classList.remove('ativo'));
            document.getElementById('avatar-' + id).classList.add('ativo');
            document.getElementById('campo-usuario-id').value = id;
            document.getElementById('senha-box').classList.add('ativo');
            document.getElementById('campo-senha').focus();
        }
    </script>
</body>
</html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()

        if request.method == "GET":
            cur.execute("SELECT id, nome, cor_avatar FROM usuarios ORDER BY nome")
            usuarios = [{"id": r[0], "nome": r[1], "cor_avatar": r[2]} for r in cur.fetchall()]
            cur.close()
            return render_template_string(PAGINA_LOGIN, usuarios=usuarios, erro=None)

        usuario_id = request.form.get("usuario_id")
        senha = request.form.get("senha", "")

        cur.execute("SELECT id, nome, senha_hash, cor_avatar FROM usuarios WHERE id = %s", (usuario_id,))
        row = cur.fetchone()
        cur.execute("SELECT id, nome, cor_avatar FROM usuarios ORDER BY nome")
        usuarios = [{"id": r[0], "nome": r[1], "cor_avatar": r[2]} for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()

    if not row or not check_password_hash(row[2], senha):
        return render_template_string(PAGINA_LOGIN, usuarios=usuarios, erro="Senha incorreta.")

    session["usuario_id"] = row[0]
    session["usuario_nome"] = row[1]
    return redirect("/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/salvar-aposta", methods=["POST"])
def salvar_aposta():
    """NOVO: salva uma aposta (individual ou múltipla) que o usuário decidiu
    apostar de verdade, com o valor apostado - alimenta a página de ROI
    (/minhas-apostas). Sem piso de probabilidade nenhum aqui - o usuário
    pode salvar qualquer odd/múltipla mostrada em qualquer parte do site.
    Cada aposta salva pertence ao usuário logado nessa sessão (o app
    inteiro já exige login, via exigir_login)."""
    descricao = request.form["descricao"]
    casa_aposta = request.form.get("casa_aposta", "")
    odd_combinada = float(request.form["odd_combinada"])
    probabilidade_combinada = request.form.get("probabilidade_combinada")
    valor_apostado = float(request.form["valor_apostado"])
    pernas_json = request.form["pernas"]
    voltar = request.form.get("voltar", "/")

    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()

        # NOVO (banca): não deixa salvar uma aposta com valor maior do que
        # o que sobrou na banca - mesma trava que uma casa de apostas real
        # teria. Se a banca ainda não foi depositada (0), toda aposta cai
        # aqui também, o que é o comportamento certo.
        banca_atual = buscar_banca(cur, session["usuario_id"])
        if valor_apostado > banca_atual:
            cur.close()
            flash(
                f"Banca insuficiente: você tem R$ {banca_atual:.2f} na banca e tentou "
                f"apostar R$ {valor_apostado:.2f}. Deposite mais na banca (em Minhas "
                "apostas) ou aposte um valor menor.",
                "erro",
            )
            return redirect(voltar)

        cur.execute(
            """INSERT INTO apostas_salvas
               (descricao, casa_aposta, odd_combinada, probabilidade_combinada,
                valor_apostado, pernas, usuario_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (descricao, casa_aposta, odd_combinada, probabilidade_combinada,
             valor_apostado, pernas_json, session["usuario_id"]),
        )
        aposta_id = cur.fetchone()[0]
        registrar_movimento_banca(cur, session["usuario_id"], "aposta", -valor_apostado, aposta_id)
        conn.commit()
        cur.close()
    finally:
        conn.close()

    return redirect(voltar)


@app.route("/cancelar-aposta", methods=["POST"])
def cancelar_aposta():
    """NOVO: cancela (apaga) uma aposta salva, só se ela ainda estiver
    'pendente' - não deixa cancelar uma aposta que já foi resolvida
    (acertou/errou), já que isso já aconteceu de verdade. Também confere
    que a aposta pertence a quem está logado - evita cancelar aposta de
    outro usuário."""
    aposta_id = request.form["aposta_id"]

    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        # NOVO (banca): busca o valor ANTES de apagar (precisa existir
        # ainda quando o movimento é gravado, já que banca_movimentos.
        # aposta_id referencia apostas_salvas.id). Só devolve/apaga se a
        # aposta realmente existe, está pendente e é desse usuário.
        cur.execute(
            "SELECT valor_apostado FROM apostas_salvas "
            "WHERE id = %s AND resultado = 'pendente' AND usuario_id = %s",
            (aposta_id, session["usuario_id"]),
        )
        row = cur.fetchone()

        if row:
            registrar_movimento_banca(cur, session["usuario_id"], "cancelamento", float(row[0]), aposta_id)

        cur.execute(
            "DELETE FROM apostas_salvas WHERE id = %s AND resultado = 'pendente' AND usuario_id = %s",
            (aposta_id, session["usuario_id"]),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()

    return redirect("/minhas-apostas")


@app.route("/banca-movimento", methods=["POST"])
def banca_movimento():
    """NOVO (banca): deposita ou resgata um valor (fictício, não é dinheiro
    real) na banca do usuário logado - é a única forma de mexer na banca
    diretamente (fora os movimentos automáticos de aposta/retorno). Usado
    pelo usuário pra "igualar" a banca do app com o saldo real dele na casa
    de apostas."""
    tipo = request.form.get("tipo")
    if tipo not in ("deposito", "resgate"):
        flash("Tipo de movimento inválido.", "erro")
        return redirect("/minhas-apostas")

    try:
        valor = float(request.form["valor"])
    except (KeyError, ValueError):
        flash("Valor inválido.", "erro")
        return redirect("/minhas-apostas")

    if valor <= 0:
        flash("O valor precisa ser maior que zero.", "erro")
        return redirect("/minhas-apostas")

    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()

        if tipo == "resgate":
            banca_atual = buscar_banca(cur, session["usuario_id"])
            if valor > banca_atual:
                cur.close()
                flash(
                    f"Não dá pra resgatar R$ {valor:.2f} - a banca só tem R$ {banca_atual:.2f}.",
                    "erro",
                )
                return redirect("/minhas-apostas")
            novo_saldo = registrar_movimento_banca(cur, session["usuario_id"], "resgate", -valor)
            flash(f"R$ {valor:.2f} resgatado(s). Nova banca: R$ {novo_saldo:.2f}.", "sucesso")
        else:
            novo_saldo = registrar_movimento_banca(cur, session["usuario_id"], "deposito", valor)
            flash(f"R$ {valor:.2f} depositado(s). Nova banca: R$ {novo_saldo:.2f}.", "sucesso")

        conn.commit()
        cur.close()
    finally:
        conn.close()

    return redirect("/minhas-apostas")


PAGINA_ROI = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Minhas Apostas - Análise de Apostas</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
            background: #0d1117; color: #e6edf3; max-width: 900px;
            margin: 0 auto; padding: 32px 20px 80px;
        }
        h1 { font-size: 1.5rem; margin: 0 0 4px; }
        .subtitulo { color: #8b949e; margin: 0 0 20px; }
        .link-voltar { color: #8b949e; text-decoration: none; font-size: 0.85rem; }
        .link-voltar:hover { text-decoration: underline; }
        .resumo-grid { display: flex; gap: 14px; margin: 20px 0 28px; flex-wrap: wrap; }
        .resumo-card {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 16px 22px; flex: 1; min-width: 130px; text-align: center;
        }
        .resumo-numero { font-size: 1.5rem; font-weight: 700; }
        .resumo-label { color: #8b949e; font-size: 0.78rem; margin-top: 4px; }
        .grafico-box {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 18px; margin-bottom: 28px;
        }
        .grafico-titulo { font-size: 0.85rem; color: #8b949e; margin-bottom: 10px; }
        .cartao {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 16px 20px; margin-bottom: 12px;
        }
        .cartao-topo {
            display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 8px; flex-wrap: wrap; gap: 8px;
        }
        .descricao { color: #c9d1d9; font-size: 0.88rem; margin-bottom: 8px; line-height: 1.5; }
        .metricas { display: flex; gap: 18px; font-size: 0.78rem; color: #8b949e; flex-wrap: wrap; }
        .metricas b { color: #e6edf3; }
        .badge {
            font-size: 0.72rem; font-weight: 700; padding: 3px 10px; border-radius: 999px;
            text-transform: uppercase; letter-spacing: 0.03em;
        }
        .badge-acertou { background: #23863622; color: #3fb950; }
        .badge-errou { background: #f8514922; color: #f85149; }
        .badge-pendente { background: #8b949e22; color: #8b949e; }
        .retorno-positivo { color: #3fb950; }
        .retorno-negativo { color: #f85149; }
        .salvar-linha {
            display: flex; gap: 8px; align-items: center; margin-top: 10px;
            padding-top: 10px; border-top: 1px solid #21262d;
        }
        .btn-cancelar {
            background: transparent;
            color: #f85149;
            border: 1px solid #f85149;
            border-radius: 8px;
            padding: 7px 14px;
            font-size: 0.8rem;
            font-weight: 600;
            cursor: pointer;
        }
        .btn-cancelar:hover { background: #f8514922; }
        .vazio {
            text-align: center; color: #8b949e; padding: 32px 24px;
            background: #161b22; border: 1px dashed #30363d; border-radius: 12px; font-size: 0.9rem;
        }
        .flash {
            padding: 12px 16px; border-radius: 10px; margin-bottom: 18px; font-size: 0.85rem;
        }
        .flash-erro { background: #f8514922; color: #f85149; border: 1px solid #f8514955; }
        .flash-sucesso { background: #23863622; color: #3fb950; border: 1px solid #23863655; }
        .banca-box {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 18px 22px; margin-bottom: 24px;
        }
        .banca-topo { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
        .banca-valor { font-size: 1.7rem; font-weight: 700; }
        .banca-label { color: #8b949e; font-size: 0.78rem; margin-top: 2px; }
        .banca-botoes { display: flex; gap: 8px; }
        .btn-banca {
            border-radius: 8px; padding: 7px 14px; font-size: 0.8rem; font-weight: 600;
            cursor: pointer; border: 1px solid #30363d; background: #21262d; color: #e6edf3;
        }
        .btn-banca:hover { background: #30363d; }
        .banca-forma {
            display: none; gap: 8px; align-items: center; margin-top: 14px;
            padding-top: 14px; border-top: 1px solid #21262d;
        }
        .banca-forma.aberta { display: flex; flex-wrap: wrap; }
        .banca-forma input[type=number] {
            background: #0d1117; border: 1px solid #30363d; color: #e6edf3;
            border-radius: 8px; padding: 8px 10px; width: 140px;
        }
        .btn-confirmar-deposito { background: #23863622; color: #3fb950; border: 1px solid #3fb95055; }
        .btn-confirmar-resgate { background: #f8514922; color: #f85149; border: 1px solid #f8514955; }
        .extrato-box {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 6px 20px; margin-bottom: 28px;
        }
        .extrato-titulo {
            font-size: 0.82rem; color: #8b949e; padding: 12px 0; cursor: pointer; user-select: none;
        }
        .extrato-lista { display: none; padding-bottom: 10px; }
        .extrato-lista.aberta { display: block; }
        .extrato-linha {
            display: flex; justify-content: space-between; font-size: 0.8rem;
            padding: 7px 0; border-top: 1px solid #21262d; color: #8b949e;
        }
        .extrato-linha b { color: #e6edf3; }
        """ + NAV_CSS + """
    </style>
    <script>
        function alternarFormaBanca(tipo) {
            document.getElementById('forma-deposito').classList.remove('aberta');
            document.getElementById('forma-resgate').classList.remove('aberta');
            document.getElementById('forma-' + tipo).classList.add('aberta');
        }
        function alternarExtrato() {
            document.getElementById('extrato-lista').classList.toggle('aberta');
        }
    </script>
</head>
<body>
    <h1>💰 Minhas Apostas</h1>
    <p class="subtitulo">Só o que você salvou com valor apostado - não inclui recomendações não salvas</p>
    {{ nav_html|safe }}

    {% with mensagens = get_flashed_messages(with_categories=true) %}
        {% for categoria, texto in mensagens %}
        <div class="flash flash-{{ categoria }}">{{ texto }}</div>
        {% endfor %}
    {% endwith %}

    <div class="banca-box">
        <div class="banca-topo">
            <div>
                <div class="banca-valor {{ 'retorno-positivo' if banca_atual >= 0 else 'retorno-negativo' }}">
                    R$ {{ "%.2f"|format(banca_atual) }}
                </div>
                <div class="banca-label">🏦 Banca atual (dinheiro fictício, não real)</div>
            </div>
            <div class="banca-botoes">
                <button type="button" class="btn-banca" onclick="alternarFormaBanca('deposito')">➕ Depositar</button>
                <button type="button" class="btn-banca" onclick="alternarFormaBanca('resgate')">➖ Resgatar</button>
            </div>
        </div>
        <form method="POST" action="/banca-movimento" class="banca-forma" id="forma-deposito">
            <input type="hidden" name="tipo" value="deposito">
            <input type="number" step="0.01" min="0.01" name="valor" placeholder="Valor a depositar (R$)" required>
            <button type="submit" class="btn-banca btn-confirmar-deposito">Confirmar depósito</button>
        </form>
        <form method="POST" action="/banca-movimento" class="banca-forma" id="forma-resgate">
            <input type="hidden" name="tipo" value="resgate">
            <input type="number" step="0.01" min="0.01" name="valor" placeholder="Valor a resgatar (R$)" required>
            <button type="submit" class="btn-banca btn-confirmar-resgate">Confirmar resgate</button>
        </form>
    </div>

    {% if movimentos_banca %}
    <div class="extrato-box">
        <div class="extrato-titulo" onclick="alternarExtrato()">📜 Extrato da banca ({{ movimentos_banca|length }} últimos) - clique pra ver</div>
        <div class="extrato-lista" id="extrato-lista">
            {% for m in movimentos_banca %}
            <div class="extrato-linha">
                <span>{{ m.criado_em }} · {{ {"deposito": "➕ Depósito", "resgate": "➖ Resgate",
                    "aposta": "🎯 Aposta salva", "retorno": "🏆 Retorno (aposta ganha)",
                    "cancelamento": "↩️ Cancelamento"}.get(m.tipo, m.tipo) }}</span>
                <span>
                    <b class="{{ 'retorno-positivo' if m.valor >= 0 else 'retorno-negativo' }}">
                        {{ "+" if m.valor >= 0 else "" }}R$ {{ "%.2f"|format(m.valor) }}</b>
                    &nbsp;→&nbsp; saldo R$ {{ "%.2f"|format(m.saldo_apos) }}
                </span>
            </div>
            {% endfor %}
        </div>
    </div>
    {% endif %}


    <div class="resumo-grid">
        <div class="resumo-card">
            <div class="resumo-numero">R$ {{ resumo.total_apostado }}</div>
            <div class="resumo-label">Total apostado</div>
        </div>
        <div class="resumo-card">
            <div class="resumo-numero {{ 'retorno-positivo' if resumo.retorno_total >= 0 else 'retorno-negativo' }}">
                R$ {{ resumo.retorno_total }}
            </div>
            <div class="resumo-label">Retorno (lucro/prejuízo)</div>
        </div>
        <div class="resumo-card">
            <div class="resumo-numero {{ 'retorno-positivo' if resumo.roi >= 0 else 'retorno-negativo' }}">
                {{ resumo.roi }}%
            </div>
            <div class="resumo-label">ROI</div>
        </div>
        <div class="resumo-card">
            <div class="resumo-numero">{{ resumo.taxa_acerto }}%</div>
            <div class="resumo-label">Taxa de acerto</div>
        </div>
        <div class="resumo-card">
            <div class="resumo-numero">{{ resumo.total_apostas }}</div>
            <div class="resumo-label">Apostas salvas</div>
        </div>
    </div>

    {% if pontos_grafico|length > 1 %}
    <div class="grafico-box">
        <div class="grafico-titulo">Retorno acumulado ao longo do tempo</div>
        {{ svg_grafico|safe }}
    </div>
    {% endif %}

    {% if apostas %}
        {% for a in apostas %}
        <div class="cartao">
            <div class="cartao-topo">
                <span>{{ a.criado_em }}</span>
                <span class="badge badge-{{ a.resultado }}">{{ a.resultado }}</span>
            </div>
            <div class="descricao">{{ a.descricao }}</div>
            <div class="metricas">
                <span>{{ a.casa_aposta }}</span>
                <span>Odd: <b>{{ a.odd_combinada }}</b></span>
                <span>Apostado: <b>R$ {{ a.valor_apostado }}</b></span>
                {% if a.retorno is not none %}
                <span>Retorno: <b class="{{ 'retorno-positivo' if a.retorno >= 0 else 'retorno-negativo' }}">
                    R$ {{ a.retorno }}</b></span>
                {% endif %}
            </div>
            {% if a.resultado == 'pendente' %}
            <form method="POST" action="/cancelar-aposta" class="salvar-linha">
                <input type="hidden" name="aposta_id" value="{{ a.id }}">
                <button type="submit" class="btn-cancelar">❌ Cancelar aposta</button>
            </form>
            {% endif %}
        </div>
        {% endfor %}
    {% else %}
        <div class="vazio">Você ainda não salvou nenhuma aposta. Use o botão "💾 Salvar" nas
        recomendações da página principal ou do histórico pra começar a acompanhar seu ROI.</div>
    {% endif %}
</body>
</html>
"""


def avaliar_perna_manual(cur, perna):
    """NOVO: avalia uma perna de aposta MANUAL (montada a partir da
    estatística de jogador, sem odd real correspondente - ver 'Criar
    Aposta Manual' em /clube/<time>). Diferente das pernas normais (que
    vieram de uma recomendação real, já avaliada em historico_recomendacoes),
    essas nunca passaram pelo motor de recomendações - então a avaliação
    aqui vai direto nas tabelas de estatística real do jogo, mesma lógica
    usada em arquivar_recomendacoes.py."""
    jogo_id = perna["jogo_id"]
    jogador_id = perna.get("jogador_id")
    tipo = perna["tipo_padrao"]
    linha = perna.get("linha")
    direcao = (perna.get("direcao") or "").strip().lower()

    if tipo == "cartao":
        cur.execute("SELECT 1 FROM cartoes WHERE jogo_id = %s AND jogador_id = %s", (jogo_id, jogador_id))
        recebeu = cur.fetchone() is not None
        cur.execute(
            "SELECT 1 FROM jogador_estatisticas_jogo WHERE jogo_id = %s AND jogador_id = %s",
            (jogo_id, jogador_id),
        )
        tem_dado = cur.fetchone() is not None
        if not recebeu and not tem_dado:
            return "pendente"
        if direcao == "sim":
            return "acertou" if recebeu else "errou"
        return "acertou" if not recebeu else "errou"

    if tipo in ("falta_cometida", "desarme", "chute_no_gol", "chute_total", "falta_sofrida"):
        coluna = {
            "falta_cometida": "faltas_cometidas", "desarme": "desarmes",
            "chute_no_gol": "chutes_no_gol", "chute_total": "chutes",
            "falta_sofrida": "faltas_sofridas",
        }[tipo]
        cur.execute(
            f"SELECT {coluna} FROM jogador_estatisticas_jogo WHERE jogo_id = %s AND jogador_id = %s",
            (jogo_id, jogador_id),
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return "pendente"
        valor_real = float(row[0])
        if direcao == "mais":
            return "acertou" if valor_real > float(linha) else "errou"
        return "acertou" if valor_real <= float(linha) else "errou"

    if tipo == "impedimento":
        cur.execute(
            "SELECT impedimentos FROM jogador_estatisticas_jogo WHERE jogo_id = %s AND jogador_id = %s",
            (jogo_id, jogador_id),
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return "pendente"
        ocorreu = row[0] > 0
        if direcao == "sim":
            return "acertou" if ocorreu else "errou"
        return "acertou" if not ocorreu else "errou"

    return "pendente"


def resolver_apostas_pendentes(cur):
    """NOVO: pra cada aposta salva ainda 'pendente', confere se TODAS as
    pernas dela já têm resultado em historico_recomendacoes - só resolve
    (acertou/errou) quando não sobrar nenhuma perna pendente, já que uma
    múltipla só acerta se todas as pernas acertarem."""
    cur.execute(
        "SELECT id, pernas, odd_combinada, valor_apostado, usuario_id "
        "FROM apostas_salvas WHERE resultado = 'pendente'"
    )
    pendentes = cur.fetchall()

    for aposta_id, pernas_json, odd_combinada, valor_apostado, usuario_id in pendentes:
        pernas = pernas_json if isinstance(pernas_json, list) else json.loads(pernas_json)

        resultados_pernas = []
        for perna in pernas:
            if perna.get("fonte") == "manual":
                resultados_pernas.append(avaliar_perna_manual(cur, perna))
                continue

            cur.execute(
                """SELECT resultado FROM historico_recomendacoes
                   WHERE jogo_id = %s AND descricao = %s
                     AND jogador_id IS NOT DISTINCT FROM %s
                   ORDER BY id DESC LIMIT 1""",
                (perna["jogo_id"], perna["descricao"], perna.get("jogador_id")),
            )
            row = cur.fetchone()
            resultados_pernas.append(row[0] if row else "pendente")

        if any(r == "errou" for r in resultados_pernas):
            resultado_final = "errou"
        elif all(r == "acertou" for r in resultados_pernas):
            resultado_final = "acertou"
        else:
            continue  # ainda tem perna pendente - não resolve ainda

        if resultado_final == "acertou":
            retorno = round(float(valor_apostado) * (float(odd_combinada) - 1), 2)
        else:
            retorno = round(-float(valor_apostado), 2)

        cur.execute(
            "UPDATE apostas_salvas SET resultado = %s, retorno = %s, resolvido_em = NOW() WHERE id = %s",
            (resultado_final, retorno, aposta_id),
        )

        # NOVO (banca): se acertou, volta pra banca o valor apostado x a
        # odd (stake + lucro) - stake + retorno é exatamente isso, já que
        # retorno = valor_apostado * (odd - 1). Se errou, não volta nada -
        # o valor já saiu da banca no momento em que a aposta foi salva,
        # não precisa de nenhum movimento extra aqui.
        if resultado_final == "acertou":
            registrar_movimento_banca(
                cur, usuario_id, "retorno", float(valor_apostado) + retorno, aposta_id
            )


def buscar_apostas_salvas(cur, usuario_id):
    cur.execute(
        """
        SELECT id, descricao, casa_aposta, odd_combinada, valor_apostado, resultado, retorno, criado_em
        FROM apostas_salvas
        WHERE usuario_id = %s
        ORDER BY criado_em DESC
        """,
        (usuario_id,),
    )
    colunas = ["id", "descricao", "casa_aposta", "odd_combinada", "valor_apostado",
               "resultado", "retorno", "criado_em"]
    return [dict(zip(colunas, row)) for row in cur.fetchall()]


def montar_svg_grafico(pontos):
    """Gráfico de linha simples (SVG puro, sem biblioteca externa) do
    retorno acumulado ao longo do tempo."""
    largura, altura = 820, 180
    margem = 20

    valores = [p[1] for p in pontos]
    minimo, maximo = min(valores + [0]), max(valores + [0])
    faixa = (maximo - minimo) or 1

    def coord_x(i):
        return margem + i * (largura - 2 * margem) / max(len(pontos) - 1, 1)

    def coord_y(v):
        return altura - margem - (v - minimo) * (altura - 2 * margem) / faixa

    linha_zero_y = coord_y(0)
    pontos_svg = " ".join(f"{coord_x(i):.1f},{coord_y(v):.1f}" for i, (_, v) in enumerate(pontos))
    cor = "#3fb950" if valores[-1] >= 0 else "#f85149"

    return f'''<svg viewBox="0 0 {largura} {altura}" style="width:100%; height:auto;">
        <line x1="{margem}" y1="{linha_zero_y:.1f}" x2="{largura - margem}" y2="{linha_zero_y:.1f}"
              stroke="#30363d" stroke-width="1" stroke-dasharray="4,4" />
        <polyline points="{pontos_svg}" fill="none" stroke="{cor}" stroke-width="2.5" />
    </svg>'''


def buscar_movimentos_banca(cur, usuario_id, limite=20):
    """NOVO (banca): últimos movimentos da banca do usuário, pra mostrar um
    extrato simples na página de ROI (depósito, resgate, aposta, retorno,
    cancelamento)."""
    cur.execute(
        """SELECT tipo, valor, saldo_apos, criado_em FROM banca_movimentos
           WHERE usuario_id = %s ORDER BY criado_em DESC LIMIT %s""",
        (usuario_id, limite),
    )
    colunas = ["tipo", "valor", "saldo_apos", "criado_em"]
    return [dict(zip(colunas, row)) for row in cur.fetchall()]


@app.route("/minhas-apostas")
def minhas_apostas():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        resolver_apostas_pendentes(cur)
        conn.commit()

        apostas = buscar_apostas_salvas(cur, session["usuario_id"])
        banca_atual = buscar_banca(cur, session["usuario_id"])
        movimentos_banca = buscar_movimentos_banca(cur, session["usuario_id"])
        cur.close()
    finally:
        conn.close()

    resolvidas = [a for a in apostas if a["resultado"] != "pendente"]
    total_apostado_resolvidas = sum(float(a["valor_apostado"]) for a in resolvidas)
    retorno_total = sum(float(a["retorno"]) for a in resolvidas) if resolvidas else 0
    roi = round(100 * retorno_total / total_apostado_resolvidas, 2) if total_apostado_resolvidas else 0
    acertos = sum(1 for a in resolvidas if a["resultado"] == "acertou")
    taxa_acerto = round(100 * acertos / len(resolvidas), 1) if resolvidas else 0

    resumo = {
        "total_apostado": round(sum(float(a["valor_apostado"]) for a in apostas), 2),
        "retorno_total": round(retorno_total, 2),
        "roi": roi,
        "taxa_acerto": taxa_acerto,
        "total_apostas": len(apostas),
    }

    # pontos do gráfico: retorno acumulado, em ordem cronológica (mais antiga primeiro).
    # NOVO: começa com um ponto artificial em R$ 0 (a "linha de partida", antes
    # da primeira aposta resolvida) - antes o gráfico só aparecia a partir da
    # SEGUNDA aposta resolvida (precisa de 2 pontos pra desenhar uma linha),
    # então com só 1 aposta resolvida nada aparecia, mesmo já tendo resultado
    # pra mostrar. Com o ponto de partida, já dá pra ver a linha (zero até o
    # resultado da primeira aposta) assim que a primeira for resolvida.
    resolvidas_ordem_cronologica = sorted(resolvidas, key=lambda a: a["criado_em"])
    pontos_grafico = []
    if resolvidas_ordem_cronologica:
        pontos_grafico.append(("Início", 0))
    acumulado = 0
    for a in resolvidas_ordem_cronologica:
        acumulado += float(a["retorno"])
        pontos_grafico.append((a["criado_em"], round(acumulado, 2)))

    svg_grafico = montar_svg_grafico(pontos_grafico) if len(pontos_grafico) > 1 else ""

    return render_template_string(
        PAGINA_ROI, resumo=resumo, apostas=apostas,
        pontos_grafico=pontos_grafico, svg_grafico=svg_grafico,
        banca_atual=round(banca_atual, 2), movimentos_banca=movimentos_banca,
        nav_html=barra_navegacao("roi", round(banca_atual, 2)),
    )


def buscar_totais_apostados(cur, usuario_id):
    """NOVO: soma o valor já apostado por (descricao, casa_aposta), pra
    mostrar um aviso tipo "R$ X já apostado nessa odd" - só das apostas do
    usuário logado (cada um vê só o próprio "já apostado", não o dos
    outros). Não impede apostar de novo na mesma odd, é só informativo."""
    cur.execute(
        "SELECT descricao, casa_aposta, SUM(valor_apostado) FROM apostas_salvas "
        "WHERE usuario_id = %s GROUP BY descricao, casa_aposta",
        (usuario_id,),
    )
    return {(row[0], row[1]): float(row[2]) for row in cur.fetchall()}


def aplicar_totais_apostados(combinacoes, totais_apostados):
    for c in combinacoes:
        c["ja_apostado"] = totais_apostados.get((c["descricao"], c["casa_aposta"]))


PAGINA_JOGADORES = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Jogadores - Análise de Apostas</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
            background: #0d1117; color: #e6edf3; max-width: 900px;
            margin: 0 auto; padding: 32px 20px 80px;
        }
        h1 { font-size: 1.5rem; margin: 0 0 4px; }
        .subtitulo { color: #8b949e; margin: 0 0 20px; font-size: 0.88rem; }
        .subtitulo a { color: #58a6ff; }
        .link-voltar { color: #8b949e; text-decoration: none; font-size: 0.85rem; }
        .link-voltar:hover { text-decoration: underline; }
        .vazio {
            text-align: center; color: #8b949e; padding: 32px 24px;
            background: #161b22; border: 1px dashed #30363d; border-radius: 12px; font-size: 0.9rem;
        }
        .busca {
            width: 100%; padding: 10px 14px; margin-bottom: 20px;
            background: #161b22; border: 1px solid #30363d; color: #e6edf3;
            border-radius: 8px; font-size: 0.9rem;
        }
        .cartao {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 16px 20px; margin-bottom: 12px;
        }
        .nome-jogador { font-weight: 700; font-size: 1rem; margin-bottom: 4px; }
        .nome-jogador-time { color: #8b949e; font-weight: 400; font-size: 0.8rem; }
        .bloco { margin-bottom: 10px; margin-top: 10px; }
        .bloco-titulo { color: #8b949e; font-size: 0.78rem; text-transform: uppercase;
            letter-spacing: 0.03em; margin-bottom: 6px; }
        .linhas-grid { display: flex; gap: 10px; flex-wrap: wrap; }
        .linha-item {
            background: #0d1117; border: 1px solid #21262d; border-radius: 8px;
            padding: 6px 12px; font-size: 0.82rem;
        }
        .linha-item b { color: #3fb950; }
        .binario-texto { font-size: 0.88rem; color: #c9d1d9; }
        .binario-texto b { color: #3fb950; }
        .separador { border: none; border-top: 1px solid #21262d; margin: 28px 0; }
        .clubes-titulo { color: #8b949e; font-size: 0.8rem; margin-bottom: 10px; }
        .clube-btn {
            display: flex; align-items: center; gap: 12px;
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 14px 20px; margin-bottom: 12px; text-decoration: none;
            color: #e6edf3; transition: border-color 0.15s;
        }
        .clube-btn:hover { border-color: #58a6ff; }
        .clube-selo {
            width: 40px; height: 40px; border-radius: 50%;
            background: linear-gradient(135deg, #333 50%, #eee 50%);
            flex-shrink: 0; object-fit: contain;
        }
        .clube-nome { font-weight: 700; font-size: 1rem; }
        .clube-sub { color: #8b949e; font-size: 0.78rem; }
        .nenhum-resultado { display: none; }
        """ + NAV_CSS + """
    </style>
</head>
<body>
    <h1>📈 Estatísticas de Jogadores</h1>
    <p class="subtitulo">Digite pra filtrar entre todos os jogadores de todos os clubes rastreados, na hora.
        Procurando estatística do TIME inteiro? <a href="/times">Ver Estatísticas de Times →</a></p>
    {{ nav_html|safe }}

    <input type="text" class="busca" id="busca" placeholder="Buscar jogador por nome (ex: Yuri Alberto)..." onkeyup="filtrar()">

    {% if clubes %}
    <div class="clubes-titulo">Ou navegue por clube (elenco completo + última escalação):</div>
        {% for c in clubes %}
        <a href="/clube/{{ c.id }}" class="clube-btn">
            {% if c.escudo_url %}
            <img src="{{ c.escudo_url }}" alt="{{ c.nome }}" class="clube-selo" onerror="this.outerHTML='<div class=&quot;clube-selo&quot;></div>'">
            {% else %}
            <div class="clube-selo"></div>
            {% endif %}
            <div>
                <div class="clube-nome">{{ c.nome }}</div>
                <div class="clube-sub">Ver elenco e última escalação →</div>
            </div>
        </a>
        {% endfor %}
    <hr class="separador">
    {% endif %}

    <div id="lista">
    {% if jogadores %}
        {% for j in jogadores %}
        <div class="cartao jogador-card">
            <div class="nome-jogador">{{ j.nome }} <span class="nome-jogador-time">· {{ j.time_nome }}</span></div>

            {% if j.cartao %}
            <div class="bloco">
                <div class="bloco-titulo">Cartão</div>
                <div class="binario-texto">Recebeu cartão em <b>{{ j.cartao.frequencia }}%</b> dos últimos
                    {{ j.cartao.jogos_analisados }} jogos</div>
            </div>
            {% endif %}

            {% for bloco in j.blocos_linha %}
            <div class="bloco">
                <div class="bloco-titulo">{{ bloco.titulo }}</div>
                <div class="linhas-grid">
                    {% for item in bloco.itens %}
                    <div class="linha-item">+{{ item.linha }}: <b>{{ item.frequencia }}%</b></div>
                    {% endfor %}
                </div>
            </div>
            {% endfor %}

            {% if j.impedimento %}
            <div class="bloco">
                <div class="bloco-titulo">Impedimento</div>
                <div class="binario-texto">Ficou em impedimento em <b>{{ j.impedimento.frequencia }}%</b> dos
                    últimos {{ j.impedimento.jogos_analisados }} jogos</div>
            </div>
            {% endif %}
        </div>
        {% endfor %}
    {% else %}
        <div class="vazio">Ainda não há padrões calculados pra nenhum jogador ainda.</div>
    {% endif %}
    </div>
    <div class="vazio nenhum-resultado" id="nenhum-resultado">Nenhum jogador encontrado com esse nome.</div>

    <script>
        function filtrar() {
            const termo = document.getElementById('busca').value.toLowerCase();
            let visiveis = 0;
            document.querySelectorAll('.jogador-card').forEach(function(card) {
                const nome = card.querySelector('.nome-jogador').textContent.toLowerCase();
                const bate = nome.includes(termo);
                card.style.display = bate ? '' : 'none';
                if (bate) visiveis++;
            });
            document.getElementById('nenhum-resultado').style.display =
                (termo && visiveis === 0) ? '' : 'none';
        }
    </script>
</body>
</html>
"""

PAGINA_CLUBE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ nome_clube }} - Análise de Apostas</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
            background: #0d1117; color: #e6edf3; max-width: 900px;
            margin: 0 auto; padding: 32px 20px 80px;
        }
        h1 { font-size: 1.5rem; margin: 0 0 4px; display: flex; align-items: center; gap: 12px; }
        .selo-titulo {
            width: 34px; height: 34px; border-radius: 50%;
            background: linear-gradient(135deg, #333 50%, #eee 50%);
            flex-shrink: 0; object-fit: contain;
        }
        .subtitulo { color: #8b949e; margin: 0 0 20px; font-size: 0.88rem; }
        .link-voltar { color: #8b949e; text-decoration: none; font-size: 0.85rem; }
        .link-voltar:hover { text-decoration: underline; }
        .busca {
            width: 100%; padding: 10px 14px; margin: 16px 0 24px;
            background: #161b22; border: 1px solid #30363d; color: #e6edf3;
            border-radius: 8px; font-size: 0.9rem;
        }
        .cartao {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 16px 20px; margin-bottom: 12px;
        }
        .nome-jogador { font-weight: 700; font-size: 1rem; margin-bottom: 10px; }
        .bloco { margin-bottom: 10px; }
        .bloco-titulo { color: #8b949e; font-size: 0.78rem; text-transform: uppercase;
            letter-spacing: 0.03em; margin-bottom: 6px; }
        .linhas-grid { display: flex; gap: 10px; flex-wrap: wrap; }
        .linha-item {
            background: #0d1117; border: 1px solid #21262d; border-radius: 8px;
            padding: 6px 12px; font-size: 0.82rem;
        }
        .linha-item b { color: #3fb950; }
        .binario-texto { font-size: 0.88rem; color: #c9d1d9; }
        .binario-texto b { color: #3fb950; }
        .vazio {
            text-align: center; color: #8b949e; padding: 32px 24px;
            background: #161b22; border: 1px dashed #30363d; border-radius: 12px; font-size: 0.9rem;
        }
        .proximo-jogo {
            background: #1f6feb18; border: 1px solid #1f6feb44; border-radius: 12px;
            padding: 12px 18px; margin-bottom: 20px; font-size: 0.85rem;
        }
        .botoes-topo { display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }
        .btn-acao {
            background: #21262d; color: #e6edf3; border: 1px solid #30363d;
            border-radius: 8px; padding: 9px 16px; font-size: 0.85rem; font-weight: 600;
            cursor: pointer;
        }
        .btn-acao:hover { border-color: #58a6ff; }
        .item-linha-label {
            display: inline-flex; align-items: center; gap: 6px;
            background: #0d1117; border: 1px solid #21262d; border-radius: 8px;
            padding: 6px 12px; font-size: 0.82rem; cursor: pointer;
        }
        .item-linha-label:has(input:checked) { border-color: #3fb950; background: #3fb95018; }
        .item-linha-label b { color: #3fb950; }
        .item-linha-label input { accent-color: #3fb950; }
        .carrinho-flutuante {
            position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
            background: #161b22; border: 1px solid #3fb950; border-radius: 12px;
            padding: 12px 20px; display: none; align-items: center; gap: 16px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.4); z-index: 90;
        }
        .carrinho-flutuante.ativo { display: flex; }
        .carrinho-contagem { font-size: 0.85rem; }
        .carrinho-contagem b { color: #3fb950; }
        .btn-carrinho {
            background: #3fb950; color: #0d1117; border: none; border-radius: 8px;
            padding: 8px 16px; font-size: 0.85rem; font-weight: 700; cursor: pointer;
        }
        .modal-form-linha {
            display: flex; gap: 10px; margin-top: 12px; flex-wrap: wrap;
        }
        .modal-form-linha input {
            flex: 1; min-width: 120px; padding: 8px 12px;
            background: #0d1117; border: 1px solid #30363d; color: #e6edf3; border-radius: 8px;
            font-size: 0.85rem;
        }
        .modal-pernas-lista { font-size: 0.78rem; color: #8b949e; margin-top: 10px; max-height: 150px; overflow-y: auto; }
        .modal-fundo {
            display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.6); z-index: 100; align-items: center; justify-content: center;
        }
        .modal-caixa {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 20px 24px; max-width: 420px; width: 90%; max-height: 80vh; overflow-y: auto;
        }
        .modal-topo { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
        .modal-titulo { font-weight: 700; font-size: 0.95rem; }
        .modal-fechar { cursor: pointer; color: #8b949e; font-size: 1.1rem; }
        .modal-fechar:hover { color: #e6edf3; }
        .btn-salvar {
            background: #1f6feb; color: white; border: none; border-radius: 8px;
            padding: 10px 16px; font-size: 0.85rem; font-weight: 600; cursor: pointer;
            display: inline-flex; align-items: center; gap: 6px;
        }
        .btn-salvar:hover { background: #388bfd; }
        .modal-caixa-grande { max-width: 720px; }
        .aviso-tres-apostas { color: #8b949e; font-size: 0.8rem; margin-bottom: 16px; }
        .retangulo-aposta {
            background: #0d1117; border: 1px solid #30363d; border-radius: 12px;
            padding: 16px 18px; margin-bottom: 16px;
        }
        .retangulo-titulo { font-weight: 700; font-size: 0.95rem; margin-bottom: 8px; }
        .retangulo-pernas { font-size: 0.8rem; color: #c9d1d9; margin-bottom: 8px; line-height: 1.6; }
        .retangulo-prob {
            font-size: 0.82rem; color: #3fb950; font-weight: 700; margin-bottom: 10px;
        }
        .retangulo-vazio { color: #8b949e; font-size: 0.82rem; font-style: italic; }
        .flash {
            padding: 12px 16px; border-radius: 10px; margin-bottom: 18px; font-size: 0.85rem;
        }
        .flash-erro { background: #f8514922; color: #f85149; border: 1px solid #f8514955; }
        .flash-sucesso { background: #23863622; color: #3fb950; border: 1px solid #23863655; }
    </style>
</head>
<body>
    <a href="/jogadores" class="link-voltar">← Voltar</a>

    {% with mensagens = get_flashed_messages(with_categories=true) %}
        {% for categoria, texto in mensagens %}
        <div class="flash flash-{{ categoria }}">{{ texto }}</div>
        {% endfor %}
    {% endwith %}

    <h1>
        {% if escudo_url %}
        <img src="{{ escudo_url }}" alt="{{ nome_clube }}" class="selo-titulo" onerror="this.outerHTML='<span class=&quot;selo-titulo&quot;></span>'">
        {% else %}
        <span class="selo-titulo"></span>
        {% endif %}
        {{ nome_clube }}
    </h1>
    <p class="subtitulo">Elenco completo e última escalação titular confirmada.</p>

    {% if ultima_escalacao %}
    <div class="cartao">
        <div class="nome-jogador">🟢 Última escalação titular</div>
        <div class="bloco-titulo">{{ ultima_escalacao.data_jogo }} · {{ nome_clube }} x {{ ultima_escalacao.adversario }}</div>
        <div class="linhas-grid">
            {% for nome in ultima_escalacao.titulares %}
            <div class="linha-item">{{ nome }}</div>
            {% endfor %}
        </div>
    </div>
    {% endif %}

    {% if proximo_jogo %}
    <div class="proximo-jogo">
        📅 Próximo jogo: <b>{{ proximo_jogo.data_jogo }} · {{ nome_clube }} x {{ proximo_jogo.adversario }}</b> -
        as apostas manuais criadas abaixo são pra esse jogo.
    </div>
    <div class="botoes-topo">
        <button class="btn-acao" onclick="gerarTresApostas()">🎯 Gerar 3 apostas automáticas</button>
        <button class="btn-acao" onclick="limparSelecao()">🧹 Limpar seleção</button>
    </div>
    {% endif %}

    <input type="text" class="busca" id="busca" placeholder="Buscar jogador..." onkeyup="filtrar()">

    <div id="lista">
    {% if jogadores %}
        {% for j in jogadores %}
        <div class="cartao jogador-card">
            <div class="nome-jogador">{{ j.nome }}</div>

            {% if j.cartao %}
            <div class="bloco">
                <div class="bloco-titulo">Cartão</div>
                {% if proximo_jogo %}
                <label class="item-linha-label">
                    <input type="checkbox" class="item-selecionavel"
                        data-jogador-id="{{ j.jogador_id }}" data-jogador-nome="{{ j.nome }}"
                        data-tipo="cartao" data-linha="" data-direcao="sim"
                        data-frequencia="{{ j.cartao.frequencia }}"
                        data-descricao="{{ j.nome }} - Receberá cartão"
                        onchange="atualizarCarrinho()">
                    Receberá cartão: <b>{{ j.cartao.frequencia }}%</b> (últimos {{ j.cartao.jogos_analisados }} jogos)
                </label>
                {% else %}
                <div class="binario-texto">Recebeu cartão em <b>{{ j.cartao.frequencia }}%</b> dos últimos
                    {{ j.cartao.jogos_analisados }} jogos</div>
                {% endif %}
            </div>
            {% endif %}

            {% for bloco in j.blocos_linha %}
            <div class="bloco">
                <div class="bloco-titulo">{{ bloco.titulo }}</div>
                <div class="linhas-grid">
                    {% for item in bloco.itens %}
                        {% if proximo_jogo %}
                        <label class="item-linha-label">
                            <input type="checkbox" class="item-selecionavel"
                                data-jogador-id="{{ j.jogador_id }}" data-jogador-nome="{{ j.nome }}"
                                data-tipo="{{ bloco.tipo }}" data-linha="{{ item.linha }}" data-direcao="mais"
                                data-frequencia="{{ item.frequencia }}"
                                data-descricao="{{ j.nome }} - {{ bloco.titulo }} - Mais de {{ item.linha }}"
                                onchange="atualizarCarrinho()">
                            +{{ item.linha }}: <b>{{ item.frequencia }}%</b>
                        </label>
                        {% else %}
                        <div class="linha-item">+{{ item.linha }}: <b>{{ item.frequencia }}%</b></div>
                        {% endif %}
                    {% endfor %}
                </div>
            </div>
            {% endfor %}

            {% if j.impedimento %}
            <div class="bloco">
                <div class="bloco-titulo">Impedimento</div>
                {% if proximo_jogo %}
                <label class="item-linha-label">
                    <input type="checkbox" class="item-selecionavel"
                        data-jogador-id="{{ j.jogador_id }}" data-jogador-nome="{{ j.nome }}"
                        data-tipo="impedimento" data-linha="" data-direcao="sim"
                        data-frequencia="{{ j.impedimento.frequencia }}"
                        data-descricao="{{ j.nome }} - Ficará em impedimento"
                        onchange="atualizarCarrinho()">
                    Impedimento: <b>{{ j.impedimento.frequencia }}%</b> (últimos {{ j.impedimento.jogos_analisados }} jogos)
                </label>
                {% else %}
                <div class="binario-texto">Ficou em impedimento em <b>{{ j.impedimento.frequencia }}%</b> dos
                    últimos {{ j.impedimento.jogos_analisados }} jogos</div>
                {% endif %}
            </div>
            {% endif %}
        </div>
        {% endfor %}
    {% else %}
        <div class="vazio">Ainda não há padrões calculados pra nenhum jogador desse clube.</div>
    {% endif %}
    </div>

    {% if proximo_jogo %}
    <div class="carrinho-flutuante" id="carrinho">
        <span class="carrinho-contagem"><b id="carrinho-count">0</b> estatística(s) selecionada(s)</span>
        <button class="btn-carrinho" onclick="abrirModalAposta()">💾 Criar Aposta</button>
    </div>

    <div id="modal-aposta" class="modal-fundo" onclick="if(event.target===this) this.style.display='none'">
        <div class="modal-caixa">
            <div class="modal-topo">
                <span class="modal-titulo">Criar aposta manual</span>
                <span class="modal-fechar" onclick="document.getElementById('modal-aposta').style.display='none'">✕</span>
            </div>
            <div class="modal-pernas-lista" id="modal-pernas-lista"></div>
            <div class="retangulo-prob" id="modal-prob-combinada"></div>
            <form method="POST" action="/salvar-aposta" id="form-aposta-manual">
                <input type="hidden" name="descricao" id="campo-descricao">
                <input type="hidden" name="casa_aposta" value="Anotado manualmente">
                <input type="hidden" name="odd_combinada" id="campo-odd">
                <input type="hidden" name="probabilidade_combinada" id="campo-probabilidade">
                <input type="hidden" name="pernas" id="campo-pernas">
                <input type="hidden" name="voltar" value="/clube/{{ time_id }}">
                <div class="modal-form-linha">
                    <input type="number" step="0.01" min="0.01" id="input-valor" placeholder="Valor apostado (R$)" required>
                    <input type="number" step="0.01" min="1.01" id="input-odd" placeholder="Odd dada pela casa" required>
                </div>
                <div class="modal-form-linha">
                    <button type="submit" class="btn-salvar" style="width:100%; justify-content:center;">💾 Salvar aposta</button>
                </div>
            </form>
        </div>
    </div>
    <div id="modal-tres-apostas" class="modal-fundo" onclick="if(event.target===this) this.style.display='none'">
        <div class="modal-caixa modal-caixa-grande">
            <div class="modal-topo">
                <span class="modal-titulo">3 apostas geradas automaticamente</span>
                <span class="modal-fechar" onclick="document.getElementById('modal-tres-apostas').style.display='none'">✕</span>
            </div>
            <p class="aviso-tres-apostas">No máximo 1 jogador pode se repetir entre as 3 apostas (sempre o de
                maior probabilidade histórica) - os demais aparecem em só uma delas.</p>
            <div id="tres-apostas-container"></div>
        </div>
    </div>
    {% endif %}

    <script>
        function filtrar() {
            const termo = document.getElementById('busca').value.toLowerCase();
            document.querySelectorAll('.jogador-card').forEach(function(card) {
                const nome = card.querySelector('.nome-jogador').textContent.toLowerCase();
                card.style.display = nome.includes(termo) ? '' : 'none';
            });
        }

        function itensSelecionados() {
            return Array.from(document.querySelectorAll('.item-selecionavel:checked'));
        }

        function atualizarCarrinho() {
            const itens = itensSelecionados();
            const carrinho = document.getElementById('carrinho');
            if (!carrinho) return;
            document.getElementById('carrinho-count').textContent = itens.length;
            carrinho.classList.toggle('ativo', itens.length > 0);
        }

        function limparSelecao() {
            document.querySelectorAll('.item-selecionavel').forEach(el => el.checked = false);
            atualizarCarrinho();
        }

        function gerarTresApostas() {
            // NOVO: em vez de uma única múltipla com 6 estatísticas (muito
            // fácil de errar, já que uma múltipla só acerta se TODAS as
            // pernas acertarem), monta 3 apostas separadas e menores
            // (3-4 pernas cada), diversificando entre jogadores diferentes.
            // Regra: no máximo 1 jogador pode aparecer em mais de uma das
            // 3 apostas - e esse jogador tem que ser o de maior
            // probabilidade histórica entre todos os candidatos. Os demais
            // jogadores aparecem em, no máximo, uma aposta.
            const MAX_PERNAS_POR_APOSTA = 4;
            const PISO_FREQUENCIA = 85;

            const candidatos = Array.from(document.querySelectorAll('.item-selecionavel'))
                .map(el => ({
                    jogadorId: el.dataset.jogadorId,
                    jogadorNome: el.dataset.jogadorNome,
                    tipo: el.dataset.tipo,
                    linha: el.dataset.linha,
                    direcao: el.dataset.direcao,
                    frequencia: parseFloat(el.dataset.frequencia),
                    descricao: el.dataset.descricao,
                }))
                .filter(c => c.frequencia < PISO_FREQUENCIA)
                .sort((a, b) => b.frequencia - a.frequencia);

            if (candidatos.length === 0) {
                alert('Nenhuma estatística disponível abaixo de 85% de probabilidade no momento.');
                return;
            }

            const melhorJogadorId = candidatos[0].jogadorId;
            const apostas = [[], [], []];
            const jogadoresUsados = new Set();
            let melhorJogadorUsos = 0;

            for (const candidato of candidatos) {
                const jaUsado = jogadoresUsados.has(candidato.jogadorId);
                const podeRepetir = candidato.jogadorId === melhorJogadorId && melhorJogadorUsos < 2;
                if (jaUsado && !podeRepetir) continue;

                // acha a aposta com menos pernas que ainda não tem esse jogador e não está cheia
                let destino = null;
                for (const aposta of apostas) {
                    if (aposta.length >= MAX_PERNAS_POR_APOSTA) continue;
                    if (aposta.some(p => p.jogadorId === candidato.jogadorId)) continue;
                    if (destino === null || aposta.length < destino.length) destino = aposta;
                }
                if (destino === null) continue;

                destino.push(candidato);
                if (!jaUsado) {
                    jogadoresUsados.add(candidato.jogadorId);
                    if (candidato.jogadorId === melhorJogadorId) melhorJogadorUsos++;
                } else {
                    melhorJogadorUsos++;
                }
            }

            renderizarTresApostas(apostas);
            document.getElementById('modal-tres-apostas').style.display = 'flex';
        }

        function renderizarTresApostas(apostas) {
            const container = document.getElementById('tres-apostas-container');
            container.innerHTML = apostas.map((aposta, i) => {
                if (aposta.length < 3) {
                    return `<div class="retangulo-aposta">
                        <div class="retangulo-titulo">Aposta ${i + 1}</div>
                        <div class="retangulo-vazio">Não há candidatos suficientes pra montar essa aposta agora.</div>
                    </div>`;
                }

                let probCombinada = 1.0;
                const pernas = aposta.map(c => {
                    probCombinada *= c.frequencia / 100;
                    return {
                        jogo_id: {{ proximo_jogo.jogo_id if proximo_jogo else 'null' }},
                        jogador_id: parseInt(c.jogadorId),
                        tipo_padrao: c.tipo,
                        linha: c.linha ? parseFloat(c.linha) : null,
                        direcao: c.direcao,
                        descricao: c.descricao,
                        fonte: "manual"
                    };
                });
                const probPct = (probCombinada * 100).toFixed(2);
                const descricaoCompleta = aposta.map(c => c.descricao).join(' + ');
                const pernasJson = JSON.stringify(pernas).replace(/"/g, '&quot;');

                return `<div class="retangulo-aposta">
                    <div class="retangulo-titulo">Aposta ${i + 1}</div>
                    <div class="retangulo-pernas">${aposta.map(c => `• ${c.descricao} (${c.frequencia}%)`).join('<br>')}</div>
                    <div class="retangulo-prob">📐 Probabilidade histórica: ${probPct}%</div>
                    <form method="POST" action="/salvar-aposta" class="modal-form-linha" style="flex-direction:column;">
                        <input type="hidden" name="descricao" value="${descricaoCompleta}">
                        <input type="hidden" name="casa_aposta" value="Anotado manualmente">
                        <input type="hidden" name="probabilidade_combinada" value="${probPct}">
                        <input type="hidden" name="pernas" value='${pernasJson}'>
                        <input type="hidden" name="voltar" value="/clube/{{ time_id }}">
                        <div style="display:flex; gap:10px; width:100%;">
                            <input type="number" step="0.01" min="0.01" name="valor_apostado" placeholder="Valor (R$)" required>
                            <input type="number" step="0.01" min="1.01" name="odd_combinada" placeholder="Odd da casa" required>
                        </div>
                        <button type="submit" class="btn-salvar" style="width:100%; justify-content:center; margin-top:8px;">💾 Salvar Aposta ${i + 1}</button>
                    </form>
                </div>`;
            }).join('');
        }

        function abrirModalAposta() {
            const itens = itensSelecionados();
            if (itens.length === 0) return;

            const lista = document.getElementById('modal-pernas-lista');
            lista.innerHTML = itens.map(el => `• ${el.dataset.descricao} (${el.dataset.frequencia}%)`).join('<br>');

            document.getElementById('campo-descricao').value = itens.map(el => el.dataset.descricao).join(' + ');

            let probCombinada = 1.0;
            const pernas = itens.map(el => {
                probCombinada *= parseFloat(el.dataset.frequencia) / 100;
                return {
                    jogo_id: {{ proximo_jogo.jogo_id if proximo_jogo else 'null' }},
                    jogador_id: parseInt(el.dataset.jogadorId),
                    tipo_padrao: el.dataset.tipo,
                    linha: el.dataset.linha ? parseFloat(el.dataset.linha) : null,
                    direcao: el.dataset.direcao,
                    descricao: el.dataset.descricao,
                    fonte: "manual"
                };
            });
            document.getElementById('campo-probabilidade').value = (probCombinada * 100).toFixed(2);
            document.getElementById('modal-prob-combinada').textContent =
                `📐 Probabilidade histórica combinada: ${(probCombinada * 100).toFixed(2)}%`;
            document.getElementById('campo-pernas').value = JSON.stringify(pernas);

            document.getElementById('modal-aposta').style.display = 'flex';
        }

        document.addEventListener('DOMContentLoaded', function() {
            const form = document.getElementById('form-aposta-manual');
            if (!form) return;
            form.addEventListener('submit', function() {
                document.getElementById('campo-odd').value = document.getElementById('input-odd').value;
                document.getElementById('campo-descricao').value += ''; // já preenchido
                const valorInput = document.getElementById('input-valor');
                const valorHidden = document.createElement('input');
                valorHidden.type = 'hidden';
                valorHidden.name = 'valor_apostado';
                valorHidden.value = valorInput.value;
                form.appendChild(valorHidden);
            });
        });
    </script>
</body>
</html>
"""

PAGINA_TIMES = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Estatísticas de Times - Análise de Apostas</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
            background: #0d1117; color: #e6edf3; max-width: 1500px;
            margin: 0 auto; padding: 32px 24px 80px;
        }
        h1 { font-size: 1.5rem; margin: 0 0 4px; text-align: center; }
        .subtitulo { color: #8b949e; margin: 0 0 16px; font-size: 0.88rem; text-align: center; }
        .vazio {
            text-align: center; color: #8b949e; padding: 24px; margin-top: 10px;
            background: #161b22; border: 1px dashed #30363d; border-radius: 12px; font-size: 0.85rem;
        }
        .titulo-coluna { font-size: 1.05rem; font-weight: 700; margin: 0 0 14px; text-align: center; }

        /* layout de 3 colunas - usa a largura toda da página, não só o meio */
        .grid-times {
            display: grid; grid-template-columns: 1.3fr 1fr 1fr; gap: 22px; align-items: start; margin-top: 24px;
        }
        @media (max-width: 1000px) {
            .grid-times { grid-template-columns: 1fr; }
        }

        /* coluna 1: tabela de classificação */
        .tabela-scroll { overflow-x: auto; border: 1px solid #30363d; border-radius: 12px; background: #161b22; }
        .tabela-classificacao { width: 100%; border-collapse: collapse; font-size: 0.78rem; white-space: nowrap; }
        .tabela-classificacao th {
            text-align: center; padding: 10px 8px; color: #8b949e; font-weight: 600;
            border-bottom: 1px solid #30363d; position: sticky; top: 0; background: #161b22;
        }
        .tabela-classificacao td { text-align: center; padding: 8px; border-bottom: 1px solid #21262d; }
        .tabela-classificacao th:nth-child(2), .tabela-classificacao td:nth-child(2) { text-align: left; }
        .tabela-classificacao tr:last-child td { border-bottom: none; }
        .tabela-classificacao tr:hover td { background: #1c2531; }
        .pos-cel { font-weight: 700; color: #8b949e; }
        .time-cel { display: flex; align-items: center; gap: 8px; font-weight: 600; }
        .escudo-mini { width: 20px; height: 20px; object-fit: contain; flex-shrink: 0; }
        .pts-cel { font-weight: 700; }
        .forma-cel { display: flex; gap: 3px; justify-content: center; }
        .bola-forma {
            width: 18px; height: 18px; border-radius: 50%; display: inline-flex;
            align-items: center; justify-content: center; font-size: 0.62rem; font-weight: 700; color: white;
        }
        .bola-v { background: #238636; }
        .bola-e { background: #6e7681; }
        .bola-d { background: #da3633; }

        /* coluna 2: busca + lista de clubes */
        .busca {
            width: 100%; padding: 10px 14px; margin-bottom: 14px;
            background: #161b22; border: 1px solid #30363d; color: #e6edf3;
            border-radius: 8px; font-size: 0.9rem;
        }
        .clube-btn {
            display: flex; align-items: center; gap: 12px;
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 14px 20px; margin-bottom: 12px; text-decoration: none;
            color: #e6edf3; transition: border-color 0.15s;
        }
        .clube-btn:hover { border-color: #58a6ff; }
        .clube-selo {
            width: 40px; height: 40px; border-radius: 50%;
            background: linear-gradient(135deg, #333 50%, #eee 50%);
            flex-shrink: 0; object-fit: contain;
        }
        .clube-nome { font-weight: 700; font-size: 1rem; }
        .clube-sub { color: #8b949e; font-size: 0.78rem; }

        /* coluna 3: líderes de estatísticas */
        .lider-card {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 16px; margin-bottom: 16px;
        }
        .lider-titulo {
            display: inline-block; padding: 4px 14px; border-radius: 999px;
            font-size: 0.78rem; font-weight: 700; margin-bottom: 12px;
        }
        .lider-cor-0 { background: #9e6a0322; color: #d29922; }
        .lider-cor-1 { background: #a371f722; color: #a371f7; }
        .lider-cor-2 { background: #39c5cf22; color: #39c5cf; }
        .lider-cor-3 { background: #3fb95022; color: #3fb950; }
        .lider-cor-4 { background: #f8514922; color: #f85149; }
        .lider-cor-5 { background: #db61a222; color: #db61a2; }
        .tabela-lider { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
        .tabela-lider th { text-align: left; color: #8b949e; font-weight: 600; padding: 4px 6px; }
        .tabela-lider th:not(:first-child), .tabela-lider td:not(:first-child) { text-align: right; }
        .tabela-lider td { padding: 6px; font-weight: 600; border-top: 1px solid #21262d; }
        .vazio-pequeno { color: #8b949e; font-size: 0.8rem; font-style: italic; }
        """ + NAV_CSS + """
    </style>
</head>
<body>
    <h1>🏟️ Estatísticas de Times</h1>
    <p class="subtitulo">Tabela oficial do Brasileirão, estatísticas por clube e líderes entre os times rastreados
        (não depende de nenhuma odd disponível na casa de apostas)</p>
    {{ nav_html|safe }}

    <div class="grid-times">
        <div class="coluna-tabela">
            <div class="titulo-coluna">Tabela do Brasileirão</div>
            {% if tabela %}
            <div class="tabela-scroll">
                <table class="tabela-classificacao">
                    <thead>
                        <tr>
                            <th>#</th><th>Clube</th><th>Pts</th><th>PJ</th><th>VIT</th><th>E</th><th>DER</th>
                            <th>GM</th><th>GC</th><th>SG</th><th>Últimas 5</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for l in tabela %}
                        <tr>
                            <td class="pos-cel">{{ l.posicao }}</td>
                            <td class="time-cel">
                                <img src="/escudo/{{ l.team_id }}.png" class="escudo-mini" onerror="this.style.display='none'">
                                {{ l.nome }}
                            </td>
                            <td class="pts-cel">{{ l.pontos }}</td>
                            <td>{{ l.jogos }}</td>
                            <td>{{ l.vitorias }}</td>
                            <td>{{ l.empates }}</td>
                            <td>{{ l.derrotas }}</td>
                            <td>{{ l.gols_pro }}</td>
                            <td>{{ l.gols_contra }}</td>
                            <td>{{ l.saldo }}</td>
                            <td>
                                <div class="forma-cel">
                                    {% for r in l.forma %}
                                    <span class="bola-forma bola-{{ 'v' if r == 'W' else ('e' if r == 'D' else 'd') }}">{{ '✓' if r == 'W' else ('–' if r == 'D' else '✕') }}</span>
                                    {% endfor %}
                                </div>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
            {% else %}
            <div class="vazio">Não foi possível carregar a tabela agora - tenta de novo em alguns minutos.</div>
            {% endif %}
        </div>

        <div class="coluna-lista">
            <input type="text" class="busca" id="busca-time" placeholder="Buscar por time (ex: Corinthians)..." onkeyup="filtrarTimes()">
            <div id="lista-times">
            {% if clubes %}
                {% for c in clubes %}
                <a href="/time/{{ c.id }}" class="clube-btn item-time">
                    {% if c.escudo_url %}
                    <img src="{{ c.escudo_url }}" alt="{{ c.nome }}" class="clube-selo" onerror="this.outerHTML='<div class=&quot;clube-selo&quot;></div>'">
                    {% else %}
                    <div class="clube-selo"></div>
                    {% endif %}
                    <div>
                        <div class="clube-nome">{{ c.nome }}</div>
                        <div class="clube-sub">Ver estatísticas do time →</div>
                    </div>
                </a>
                {% endfor %}
            {% else %}
                <div class="vazio">Nenhum clube rastreado ainda.</div>
            {% endif %}
            </div>
            <div class="vazio" id="nenhum-resultado-time" style="display:none;">Nenhum time rastreado encontrado com esse nome.</div>
        </div>

        <div class="coluna-lideres">
            <div class="titulo-coluna">Líderes de Estatísticas (times rastreados)</div>
            {% for l in lideres %}
            <div class="lider-card">
                <div class="lider-titulo lider-cor-{{ loop.index0 % 6 }}">{{ l.titulo }}</div>
                {% if l.time_nome %}
                <table class="tabela-lider">
                    <thead>
                        <tr><th>Time</th><th>Média últimos 5</th><th>Média Total</th></tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td>{{ l.time_nome }}</td>
                            <td>{{ l.media_5 if l.media_5 is not none else "-" }}</td>
                            <td>{{ l.media_total }}</td>
                        </tr>
                    </tbody>
                </table>
                {% else %}
                <div class="vazio-pequeno">Ainda não há dados suficientes pra essa categoria.</div>
                {% endif %}
            </div>
            {% endfor %}
        </div>
    </div>

    <script>
        function filtrarTimes() {
            const termo = document.getElementById('busca-time').value.toLowerCase();
            let visiveis = 0;
            document.querySelectorAll('.item-time').forEach(function(card) {
                const nome = card.querySelector('.clube-nome').textContent.toLowerCase();
                const bate = nome.includes(termo);
                card.style.display = bate ? '' : 'none';
                if (bate) visiveis++;
            });
            document.getElementById('nenhum-resultado-time').style.display = (termo && visiveis === 0) ? '' : 'none';
        }
    </script>
</body>
</html>
"""

PAGINA_TIME = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ nome_time }} - Estatísticas de Time</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
            background: #0d1117; color: #e6edf3; max-width: 900px;
            margin: 0 auto; padding: 32px 20px 80px;
        }
        h1 { font-size: 1.5rem; margin: 0 0 4px; display: flex; align-items: center; gap: 12px; }
        .selo-titulo {
            width: 34px; height: 34px; border-radius: 50%;
            background: linear-gradient(135deg, #333 50%, #eee 50%);
            flex-shrink: 0; object-fit: contain;
        }
        .subtitulo { color: #8b949e; margin: 0 0 24px; font-size: 0.88rem; }
        .link-voltar { color: #8b949e; text-decoration: none; font-size: 0.85rem; }
        .link-voltar:hover { text-decoration: underline; }
        .cartao {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 18px 22px; margin-bottom: 14px;
        }
        .bloco-topo { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 10px; flex-wrap: wrap; gap: 6px; }
        .bloco-titulo { font-weight: 700; font-size: 1rem; }
        .bloco-media { color: #8b949e; font-size: 0.8rem; }
        .bloco-media b { color: #e6edf3; }
        .linhas-grid { display: flex; gap: 10px; flex-wrap: wrap; }
        .linha-item {
            background: #0d1117; border: 1px solid #21262d; border-radius: 8px;
            padding: 6px 12px; font-size: 0.85rem;
        }
        .linha-item b { color: #3fb950; }
        .vazio {
            text-align: center; color: #8b949e; padding: 32px 24px;
            background: #161b22; border: 1px dashed #30363d; border-radius: 12px; font-size: 0.9rem;
        }
    </style>
</head>
<body>
    <a href="/times" class="link-voltar">← Voltar</a>
    <h1>
        {% if escudo_url %}
        <img src="{{ escudo_url }}" alt="{{ nome_time }}" class="selo-titulo" onerror="this.outerHTML='<span class=&quot;selo-titulo&quot;></span>'">
        {% else %}
        <span class="selo-titulo"></span>
        {% endif %}
        {{ nome_time }}
    </h1>
    <p class="subtitulo">Frequência histórica do time inteiro (últimos jogos, só o lado do {{ nome_time }}) -
        cobre escanteios, chutes (total e no gol), faltas, cartões, impedimentos, desarmes e posse de bola.
        Faltas, chutes, impedimentos, desarmes e posse não têm odd real disponível hoje pra nenhum mercado.</p>

    {% if blocos %}
        {% for bloco in blocos %}
        <div class="cartao">
            <div class="bloco-topo">
                <div class="bloco-titulo">{{ bloco.titulo }}</div>
                <div class="bloco-media">Média: <b>{{ bloco.media }}{{ '%' if bloco.tipo == 'posse' else '' }}</b>
                    {{ 'por jogo' if bloco.tipo != 'posse' else '' }} · últimos {{ bloco.jogos_analisados }} jogo(s)</div>
            </div>
            {% if bloco.itens %}
            <div class="linhas-grid">
                {% for item in bloco.itens %}
                <div class="linha-item">+{{ item.linha }}: <b>{{ item.frequencia }}%</b></div>
                {% endfor %}
            </div>
            {% endif %}
        </div>
        {% endfor %}
    {% else %}
        <div class="vazio">Ainda não há dados suficientes pra calcular as estatísticas desse time
            (precisa de pelo menos 5 jogos concluídos).</div>
    {% endif %}
</body>
</html>
"""

NOMES_TIPO_LINHA = {
    "falta_cometida": "Faltas cometidas",
    "desarme": "Desarmes",
    "chute_no_gol": "Chutes no gol",
    "chute_total": "Chutes (total)",
    "falta_sofrida": "Faltas sofridas",
}

# NOVO (estatísticas de time)
NOMES_TIPO_LINHA_TIME = {
    "escanteio": "Escanteios do time",
    "chute": "Chutes (total) do time",
    "chute_no_gol": "Chutes no gol do time",
    "falta": "Faltas do time",
    "cartao": "Cartões do time (amarelo + vermelho)",
    "impedimento": "Impedimentos do time",
    "desarme": "Desarmes do time",
    "posse": "Posse de bola média",
}
ORDEM_BLOCOS_TIME = [
    "escanteio", "chute", "chute_no_gol", "falta", "cartao", "impedimento", "desarme", "posse",
]


# ---------- Tabela do Brasileirão (vem da API-Football, não do nosso banco) ----------
#
# A tabela de classificação precisa dos 20 times do campeonato, mas a gente
# só rastreia alguns (rastrear todos os 20 estouraria a cota mensal da
# OddsPapi - ver seção 11 da documentação). Por isso essa tabela busca
# direto na API-Football (endpoint /standings), que já dá a classificação
# oficial pronta, e guarda em cache por alguns minutos pra não gastar cota
# à toa a cada vez que alguém abre a página.
LEAGUE_ID_BRASILEIRAO = 71
TEMPORADA_ATUAL = 2026
_cache_tabela_brasileirao = {"dados": None, "dia": None}


def buscar_tabela_brasileirao():
    """NOVO: a tabela do Brasileirão só muda quando um jogo termina - não
    faz sentido buscar de novo a cada poucos minutos. Agora o cache é por
    DIA (fuso de Brasília): a primeira pessoa que abrir a página depois da
    meia-noite dispara uma busca nova, e o resto do dia usa essa mesma
    cópia - só 1 chamada à API-Football por dia (no máximo), em vez de uma
    a cada 15 minutos."""
    hoje = datetime.now(FUSO_BRASIL).date()
    cache = _cache_tabela_brasileirao
    if cache["dados"] is not None and cache["dia"] == hoje:
        return cache["dados"]

    # NOVO: .strip() de proteção - se a variável de ambiente vier com espaço
    # ou quebra de linha sobrando no final (comum ao colar de algum lugar),
    # isso quebrava a chamada HTTP inteira com um erro de "invalid header
    # value" difícil de entender à primeira vista.
    api_key = (os.environ.get("API_FOOTBALL_KEY") or "").strip()
    if not api_key:
        print("[tabela_brasileirao] API_FOOTBALL_KEY não configurada nesse serviço - "
              "sem isso não dá pra buscar a tabela do campeonato.")
        return cache["dados"] or []

    try:
        resposta = requests.get(
            "https://v3.football.api-sports.io/standings",
            params={"league": LEAGUE_ID_BRASILEIRAO, "season": TEMPORADA_ATUAL},
            headers={"x-apisports-key": api_key},
            timeout=15,
        )
        resposta.raise_for_status()
        dados = resposta.json()

        # NOVO: log detalhado em caso de resposta "vazia" (sem erro HTTP,
        # mas sem standings dentro) - ajuda a diagnosticar pelo log do
        # Railway em vez de só saber que "não carregou".
        if dados.get("errors"):
            print(f"[tabela_brasileirao] API-Football retornou erro: {dados['errors']}")
            return cache["dados"] or []
        if not dados.get("response"):
            print(f"[tabela_brasileirao] Resposta sem 'response' (temporada {TEMPORADA_ATUAL} "
                  f"pode ainda não estar disponível nesse plano) - corpo bruto: {dados}")
            return cache["dados"] or []

        standings = dados["response"][0]["league"]["standings"][0]

        tabela = []
        for linha in standings:
            tabela.append({
                "posicao": linha["rank"],
                "team_id": linha["team"]["id"],
                "nome": linha["team"]["name"],
                "pontos": linha["points"],
                "jogos": linha["all"]["played"],
                "vitorias": linha["all"]["win"],
                "empates": linha["all"]["draw"],
                "derrotas": linha["all"]["lose"],
                "gols_pro": linha["all"]["goals"]["for"],
                "gols_contra": linha["all"]["goals"]["against"],
                "saldo": linha["goalsDiff"],
                "forma": list(linha.get("form") or ""),
            })

        cache["dados"] = tabela
        cache["dia"] = hoje
        return tabela
    except (requests.RequestException, KeyError, IndexError) as e:
        print(f"[tabela_brasileirao] Falha ao buscar/interpretar a tabela: {e}")
        return cache["dados"] or []


# ---------- Líderes de estatísticas (só entre os times rastreados) ----------
CATEGORIAS_LIDERANCA = [
    ("chute", "Chutes (Total)"),
    ("chute_no_gol", "Chutes no Gol"),
    ("impedimento", "Impedimento"),
    ("falta", "Faltas Cometidas"),
    ("desarme", "Desarmes"),
    ("cartao", "Cartões"),
]

# de onde vem o dado bruto de cada categoria, pra poder recalcular a média
# "ao vivo" só dos últimos 5 jogos (padroes_time_linha guarda a média sobre
# a janela maior, até 50 jogos - ver motor_padroes.py - mas não guarda uma
# versão separada "últimos 5", então essa parte é calculada na hora aqui)
FONTE_CATEGORIA_LIDERANCA = {
    "chute": ("estatisticas_jogo", "finalizacoes"),
    "falta": ("estatisticas_jogo", "faltas"),
    "chute_no_gol": ("soma_jogador", "chutes_no_gol"),
    "impedimento": ("soma_jogador", "impedimentos"),
    "desarme": ("soma_jogador", "desarmes"),
    "cartao": ("cartao_time", None),
}


def buscar_media_ultimos_5_jogos(cur, time_id, tipo):
    fonte, coluna = FONTE_CATEGORIA_LIDERANCA[tipo]

    if fonte == "estatisticas_jogo":
        cur.execute(
            f"""
            SELECT eg.{coluna}
            FROM estatisticas_jogo eg
            JOIN jogos j ON j.id = eg.jogo_id
            WHERE j.nosso_time_id = %s
              AND ((j.mandante = TRUE AND eg.lado = 'mandante')
               OR (j.mandante = FALSE AND eg.lado = 'visitante'))
              AND eg.{coluna} IS NOT NULL
            ORDER BY j.data_jogo DESC LIMIT 5
            """,
            (time_id,),
        )
    elif fonte == "soma_jogador":
        cur.execute(
            f"""
            SELECT SUM(jeg.{coluna})
            FROM jogador_estatisticas_jogo jeg
            JOIN jogos j ON j.id = jeg.jogo_id
            WHERE j.nosso_time_id = %s
              AND ((j.mandante = TRUE AND jeg.lado = 'mandante')
               OR (j.mandante = FALSE AND jeg.lado = 'visitante'))
              AND jeg.{coluna} IS NOT NULL
            GROUP BY jeg.jogo_id, j.data_jogo
            ORDER BY j.data_jogo DESC LIMIT 5
            """,
            (time_id,),
        )
    else:  # cartao_time
        cur.execute(
            """
            SELECT contagem.total_cartoes
            FROM (
                SELECT j.id AS jogo_id, j.data_jogo,
                       COUNT(c.id) FILTER (WHERE c.lado = 'mandante') AS total_cartoes,
                       COUNT(DISTINCT eg.lado) AS lados
                FROM jogos j
                JOIN estatisticas_jogo eg ON eg.jogo_id = j.id
                LEFT JOIN cartoes c ON c.jogo_id = j.id
                WHERE j.nosso_time_id = %s AND j.data_jogo < CURRENT_DATE
                GROUP BY j.id, j.data_jogo
            ) contagem
            WHERE contagem.lados = 2
            ORDER BY contagem.data_jogo DESC LIMIT 5
            """,
            (time_id,),
        )

    valores = [row[0] for row in cur.fetchall()]
    if not valores:
        return None
    return round(sum(float(v) for v in valores) / len(valores), 2)


def buscar_lideres_estatisticas(cur):
    """Pra cada categoria, acha qual time RASTREADO tem a maior média
    "total" (a que o motor_padroes.py já calcula sobre a janela de até 50
    jogos) e busca ao vivo a média desse time só nos últimos 5 jogos.
    IMPORTANTE: é liderança só entre os times que a gente rastreia, não o
    campeonato inteiro - rastrear os 20 times só pra essa tabela estouraria
    a cota da OddsPapi (ver seção 11 da documentação)."""
    lideres = []
    for tipo, titulo in CATEGORIAS_LIDERANCA:
        cur.execute(
            """
            SELECT t.id, t.nome, p.media
            FROM padroes_time_linha p
            JOIN times t ON t.id = p.time_id
            WHERE p.tipo = %s AND t.rastreado = TRUE
            ORDER BY p.media DESC
            LIMIT 1
            """,
            (tipo,),
        )
        row = cur.fetchone()
        if not row:
            lideres.append({"titulo": titulo, "tipo": tipo, "time_nome": None})
            continue
        time_id, time_nome, media_total = row
        lideres.append({
            "titulo": titulo, "tipo": tipo, "time_nome": time_nome,
            "media_total": float(media_total),
            "media_5": buscar_media_ultimos_5_jogos(cur, time_id, tipo),
        })
    return lideres


def buscar_estatisticas_time(cur, time_id):
    """NOVO (estatísticas de time): monta a frequência histórica de
    escanteios, chutes (total e no gol), faltas, cartões, impedimentos,
    desarmes e a posse de bola média do time, lendo direto das tabelas de
    padrão já calculadas pelo motor_padroes.py - mesmo espírito de
    buscar_estatisticas_jogadores, mas em nível de time. Cobre justamente
    os mercados sem odd real disponível hoje (faltas e chutes de time), do
    mesmo jeito que /jogadores já cobre pro jogador (ver seção 2 da
    documentação - confirmação oficial da OddsPapi de que não existe preço
    real pra prop bet de jogador na Superbet; o mesmo vale a fortiori pra
    estatística de time inteiro, que nunca teve odd)."""
    blocos_dict = {}

    def adicionar(tipo, linha, jogos_analisados, frequencia, media):
        bloco = blocos_dict.setdefault(tipo, {
            "titulo": NOMES_TIPO_LINHA_TIME.get(tipo, tipo),
            "tipo": tipo, "itens": [], "media": media, "jogos_analisados": jogos_analisados,
        })
        # NOVO: "posse" é só uma média (percentual), não faz sentido testar
        # "mais de X.5 jogos" como as outras - não vira item de linha,
        # o bloco existe só pra mostrar a média no cabeçalho.
        if tipo != "posse":
            bloco["itens"].append({"linha": linha, "frequencia": frequencia})

    cur.execute(
        """SELECT linha, jogos_analisados, frequencia, media
           FROM padroes_time_escanteio WHERE time_id = %s ORDER BY linha""",
        (time_id,),
    )
    for linha, jogos_analisados, frequencia, media in cur.fetchall():
        adicionar("escanteio", float(linha), jogos_analisados, float(frequencia), float(media))

    cur.execute(
        """SELECT tipo, linha, jogos_analisados, frequencia, media
           FROM padroes_time_linha WHERE time_id = %s ORDER BY tipo, linha""",
        (time_id,),
    )
    for tipo, linha, jogos_analisados, frequencia, media in cur.fetchall():
        adicionar(tipo, float(linha), jogos_analisados, float(frequencia), float(media))

    blocos = [blocos_dict[tipo] for tipo in ORDEM_BLOCOS_TIME if tipo in blocos_dict]
    blocos += [b for tipo, b in blocos_dict.items() if tipo not in ORDEM_BLOCOS_TIME]
    return blocos


def buscar_escudo_url(cur, nome_time):
    """NOVO: usa o api_football_team_id já salvo em `times` pra montar a URL
    do escudo. Aponta pra ROTA DE PROXY LOCAL (/escudo/<id>.png), não direto
    pra media.api-sports.io - a CDN deles parece bloquear pedido vindo de
    fora do domínio deles (hotlink), então o navegador buscando direto não
    funcionava. Com o proxy, é o nosso próprio servidor que busca a imagem
    (pedido servidor-a-servidor, sem esse bloqueio), e devolve pro
    navegador."""
    cur.execute("SELECT api_football_team_id FROM times WHERE nome = %s", (nome_time,))
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    return f"/escudo/{row[0]}.png"


@app.route("/escudo/<int:team_id>.png")
def escudo(team_id):
    """NOVO: busca a imagem do escudo no servidor (não no navegador do
    usuário) e repassa pro cliente - evita bloqueio de hotlink da CDN da
    API-Football, que parece exigir que o pedido não pareça vir de um site
    externo."""
    try:
        resp = requests.get(
            f"https://media.api-sports.io/football/teams/{team_id}.png",
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code == 200:
            return Response(resp.content, mimetype="image/png")
    except Exception:
        pass
    return "", 404


def buscar_estatisticas_jogadores(cur, time_id=None, busca=None):
    """Monta a frequência histórica de cada jogador (cartão, faltas,
    desarmes, chutes no gol, impedimento), lendo direto das tabelas de
    padrão já calculadas pelo motor_padroes.py - não depende de nenhuma
    odd estar disponível na casa de apostas.
    NOVO (multi-time): filtra por time_atual_id - cada clube só mostra o
    próprio elenco, sem misturar jogadores de outro time rastreado.
    NOVO (busca global): agora aceita `busca` (nome, substring, sem
    diferenciar maiúscula/minúscula) - se informado, ignora `time_id` e
    procura em TODOS os clubes rastreados de uma vez, sem precisar entrar
    no clube primeiro. Cada jogador retornado inclui `time_nome`, útil
    pra saber de qual clube é quando o resultado vem de vários times."""
    condicoes = ["j.ativo = TRUE", "t.rastreado = TRUE"]
    params_base = []
    if busca:
        condicoes.append("j.nome ILIKE %s")
        params_base.append(f"%{busca}%")
    elif time_id is not None:
        condicoes.append("j.time_atual_id = %s")
        params_base.append(time_id)
    condicao_sql = " AND ".join(condicoes)

    jogadores_dict = {}

    def garantir(jogador_id, nome, time_nome):
        jogadores_dict.setdefault(jogador_id, {
            "nome": nome, "time_nome": time_nome, "cartao": None, "linhas": {}, "impedimento": None,
        })

    cur.execute(
        f"""
        SELECT j.id, j.nome, t.nome, p.jogos_analisados, p.frequencia
        FROM padroes_jogador_cartao p
        JOIN jogadores j ON j.id = p.jogador_id
        JOIN times t ON t.id = j.time_atual_id
        WHERE {condicao_sql}
        """,
        params_base,
    )
    for jogador_id, nome, time_nome, jogos_analisados, frequencia in cur.fetchall():
        garantir(jogador_id, nome, time_nome)
        jogadores_dict[jogador_id]["cartao"] = {
            "jogos_analisados": jogos_analisados, "frequencia": float(frequencia),
        }

    cur.execute(
        f"""
        SELECT j.id, j.nome, t.nome, p.tipo, p.linha, p.jogos_analisados, p.frequencia
        FROM padroes_jogador_linha p
        JOIN jogadores j ON j.id = p.jogador_id
        JOIN times t ON t.id = j.time_atual_id
        WHERE {condicao_sql}
        ORDER BY p.linha
        """,
        params_base,
    )
    for jogador_id, nome, time_nome, tipo, linha, jogos_analisados, frequencia in cur.fetchall():
        garantir(jogador_id, nome, time_nome)
        jogadores_dict[jogador_id]["linhas"].setdefault(tipo, []).append({
            "linha": float(linha), "frequencia": float(frequencia),
        })

    cur.execute(
        f"""
        SELECT j.id, j.nome, t.nome, p.jogos_analisados, p.frequencia
        FROM padroes_jogador_frequencia p
        JOIN jogadores j ON j.id = p.jogador_id
        JOIN times t ON t.id = j.time_atual_id
        WHERE {condicao_sql} AND p.tipo = 'impedimento'
        """,
        params_base,
    )
    for jogador_id, nome, time_nome, jogos_analisados, frequencia in cur.fetchall():
        garantir(jogador_id, nome, time_nome)
        jogadores_dict[jogador_id]["impedimento"] = {
            "jogos_analisados": jogos_analisados, "frequencia": float(frequencia),
        }

    lista = []
    for jogador_id, dados in jogadores_dict.items():
        blocos_linha = [
            {"titulo": NOMES_TIPO_LINHA.get(tipo, tipo), "tipo": tipo, "itens": itens}
            for tipo, itens in dados["linhas"].items()
        ]
        lista.append({
            "jogador_id": jogador_id,
            "nome": dados["nome"],
            "time_nome": dados["time_nome"],
            "cartao": dados["cartao"],
            "impedimento": dados["impedimento"],
            "blocos_linha": blocos_linha,
        })

    lista.sort(key=lambda p: p["nome"])
    return lista


def buscar_ultima_escalacao_titular(cur, time_id):
    """NOVO: busca os titulares do último jogo já concluído DESSE time,
    cruzando escalacoes com jogador_estatisticas_jogo.lado (pra saber se
    aquele titular jogava pelo time ou pelo adversário naquele jogo
    específico) e jogos.mandante (pra saber qual lado é o do time nesse
    jogo).
    NOVO (multi-time): filtra por nosso_time_id."""
    cur.execute(
        """
        SELECT id, data_jogo, adversario FROM jogos
        WHERE nosso_time_id = %s
          AND ((datahora_jogo IS NOT NULL AND datahora_jogo < NOW())
           OR (datahora_jogo IS NULL AND data_jogo < CURRENT_DATE))
        ORDER BY COALESCE(datahora_jogo, data_jogo::timestamp) DESC
        LIMIT 1
        """,
        (time_id,),
    )
    ultimo_jogo = cur.fetchone()
    if not ultimo_jogo:
        return None

    jogo_id, data_jogo, adversario = ultimo_jogo

    cur.execute(
        """
        SELECT j.nome
        FROM escalacoes e
        JOIN jogador_estatisticas_jogo jeg ON jeg.jogo_id = e.jogo_id AND jeg.jogador_id = e.jogador_id
        JOIN jogos jg ON jg.id = e.jogo_id
        JOIN jogadores j ON j.id = e.jogador_id
        WHERE e.jogo_id = %s AND e.titular = TRUE
          AND ((jeg.lado = 'mandante' AND jg.mandante = TRUE)
            OR (jeg.lado = 'visitante' AND jg.mandante = FALSE))
        ORDER BY j.nome
        """,
        (jogo_id,),
    )
    titulares = [row[0] for row in cur.fetchall()]

    return {"data_jogo": data_jogo, "adversario": adversario, "titulares": titulares}


def buscar_proximo_jogo(cur, nome_time):
    """NOVO: busca o próximo jogo AINDA NÃO disputado do time - usado pra
    associar apostas manuais de estatística de jogador a um jogo específico
    (necessário pra conseguir avaliar acerto/erro depois que o jogo
    acontecer)."""
    cur.execute(
        """
        SELECT j.id, j.data_jogo, j.adversario
        FROM jogos j
        WHERE (j.mandante_id = (SELECT id FROM times WHERE nome = %s)
            OR j.visitante_id = (SELECT id FROM times WHERE nome = %s))
          AND ((j.datahora_jogo IS NOT NULL AND j.datahora_jogo >= NOW())
            OR (j.datahora_jogo IS NULL AND j.data_jogo >= CURRENT_DATE))
        ORDER BY COALESCE(j.datahora_jogo, j.data_jogo::timestamp) ASC
        LIMIT 1
        """,
        (nome_time, nome_time),
    )
    row = cur.fetchone()
    if not row:
        return None
    jogo_id, data_jogo, adversario = row
    return {"jogo_id": jogo_id, "data_jogo": data_jogo, "adversario": adversario}


@app.route("/jogadores")
def jogadores():
    """NOVO: agora carrega TODOS os jogadores ativos de TODOS os clubes
    rastreados de uma vez (não só a lista de clubes) - o filtro por nome
    acontece no navegador (igual já funcionava dentro de /clube/<id>),
    instantâneo a cada letra digitada, sem precisar escolher um clube
    primeiro nem ir e voltar no servidor."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()

        jogadores_lista = buscar_estatisticas_jogadores(cur)

        cur.execute("SELECT id, nome FROM times WHERE rastreado = TRUE ORDER BY nome")
        times_rastreados = cur.fetchall()
        clubes = [
            {"id": time_id, "nome": nome, "escudo_url": buscar_escudo_url(cur, nome)}
            for time_id, nome in times_rastreados
        ]
        banca_atual = buscar_banca(cur, session["usuario_id"])
        cur.close()
    finally:
        conn.close()

    return render_template_string(
        PAGINA_JOGADORES, clubes=clubes, jogadores=jogadores_lista,
        nav_html=barra_navegacao("jogadores", round(banca_atual, 2)),
    )


@app.route("/clube/<int:time_id>")
def clube(time_id):
    """NOVO (multi-time): página específica do clube - antes só existia
    /clube/corinthians fixo; agora funciona pra qualquer time rastreado,
    identificado pelo id. Mostra todos os jogadores ativos DESSE clube +
    a última escalação titular confirmada + o próximo jogo (usado pra
    montar apostas manuais de estatística)."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute("SELECT nome FROM times WHERE id = %s AND rastreado = TRUE", (time_id,))
        row = cur.fetchone()
        if not row:
            cur.close()
            return "Clube não encontrado.", 404
        nome_clube = row[0]

        lista = buscar_estatisticas_jogadores(cur, time_id)
        ultima_escalacao = buscar_ultima_escalacao_titular(cur, time_id)
        proximo_jogo = buscar_proximo_jogo(cur, nome_clube)
        escudo_url = buscar_escudo_url(cur, nome_clube)
        cur.close()
    finally:
        conn.close()

    return render_template_string(
        PAGINA_CLUBE, jogadores=lista, ultima_escalacao=ultima_escalacao,
        proximo_jogo=proximo_jogo, nome_clube=nome_clube, escudo_url=escudo_url,
        time_id=time_id,
    )


@app.route("/times")
def times_lista():
    """NOVO (estatísticas de time): igual a /jogadores, mas mostra o grid
    de clubes pra ver estatística de TIME (escanteio, falta, chute,
    cartão) em vez de jogador."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, nome FROM times WHERE rastreado = TRUE ORDER BY nome")
        times_rastreados = cur.fetchall()
        clubes = [
            {"id": time_id, "nome": nome, "escudo_url": buscar_escudo_url(cur, nome)}
            for time_id, nome in times_rastreados
        ]
        lideres = buscar_lideres_estatisticas(cur)
        banca_atual = buscar_banca(cur, session["usuario_id"])
        cur.close()
    finally:
        conn.close()

    tabela = buscar_tabela_brasileirao()

    return render_template_string(
        PAGINA_TIMES, clubes=clubes, tabela=tabela, lideres=lideres,
        nav_html=barra_navegacao("times", round(banca_atual, 2)),
    )


@app.route("/time/<int:time_id>")
def time_detalhe(time_id):
    """NOVO (estatísticas de time): página com a frequência histórica de
    escanteios, faltas, chutes e cartões de UM time rastreado."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute("SELECT nome FROM times WHERE id = %s AND rastreado = TRUE", (time_id,))
        row = cur.fetchone()
        if not row:
            cur.close()
            return "Time não encontrado.", 404
        nome_time = row[0]

        blocos = buscar_estatisticas_time(cur, time_id)
        escudo_url = buscar_escudo_url(cur, nome_time)
        cur.close()
    finally:
        conn.close()

    return render_template_string(
        PAGINA_TIME, blocos=blocos, nome_time=nome_time, escudo_url=escudo_url,
    )


@app.route("/")
def index():
    odd_min = request.args.get("odd_min", "1.5")
    odd_max = request.args.get("odd_max", "5.0")
    buscou = "odd_min" in request.args
    forcar_atualizacao = request.args.get("forcar") == "1"

    combinacoes = []
    motivo = ""
    ultima_atualizacao_odds = None
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        banca_atual = buscar_banca(cur, session["usuario_id"])

        # NOVO: só mexe na atualização de odds quando a pessoa realmente
        # pediu recomendações (buscou=True) ou clicou em "Atualizar
        # recomendações" (forcar=True) - só entrar na página sem clicar em
        # nada não dispara nenhum Run Now.
        if buscou or forcar_atualizacao:
            ultima = processar_atualizacao_odds(cur, session["usuario_id"], forcar_atualizacao)
            conn.commit()
            if ultima:
                ultima_atualizacao_odds = ultima.astimezone(FUSO_BRASIL).strftime("%H:%M")
        else:
            ultima = buscar_ultima_atualizacao_odds(cur)
            if ultima:
                ultima_atualizacao_odds = ultima.astimezone(FUSO_BRASIL).strftime("%H:%M")

        if buscou:
            recomendacoes = buscar_recomendacoes(cur)
            combinacoes = montar_combinacoes(recomendacoes, float(odd_min), float(odd_max))
            aplicar_totais_apostados(combinacoes, buscar_totais_apostados(cur, session["usuario_id"]))
            if not combinacoes:
                motivo = descobrir_motivo(cur)
        cur.close()
    finally:
        conn.close()

    # NOVO: separa odds individuais (1 perna) de múltiplas (2+ pernas) em
    # listas próprias, uma pra cada coluna (ver PAGINA) - antes vinham
    # todas misturadas na mesma lista, ordenadas só por probabilidade,
    # dificultando separar rapidamente "aposta simples" de "combinação".
    individuais = [c for c in combinacoes if len(c["pernas"]) == 1]
    multiplas = [c for c in combinacoes if len(c["pernas"]) > 1]

    return render_template_string(
        PAGINA, odd_min=odd_min, odd_max=odd_max, buscou=buscou,
        individuais=individuais, multiplas=multiplas, motivo=motivo, banca_atual=round(banca_atual, 2),
        ultima_atualizacao_odds=ultima_atualizacao_odds,
        nav_html=barra_navegacao("index", round(banca_atual, 2)),
    )


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta)
