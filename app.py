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
import re
import secrets
import unicodedata
import subprocess
import sys
import threading
import csv
import io
from decimal import Decimal, ROUND_FLOOR
from urllib.parse import quote
from functools import wraps
from itertools import combinations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import psycopg2
from flask import Flask, render_template_string, request, redirect, Response, session, url_for, flash, get_flashed_messages, jsonify, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from api_consulta import api_consulta

DATABASE_URL = os.environ["DATABASE_URL"]

# NOVO (busca de confronto direto, "Estatísticas de Times"): os 20 times da
# Série A atual (api_football_team_id), pra alimentar o campo de busca de
# confronto direto em /time/<id> - aceita como adversário só um desses 20,
# mesmo que o time selecionado já tenha enfrentado outros clubes (copas,
# estaduais etc.) que não entram na busca. Precisa ser atualizada na mão se
# a composição da Série A mudar (acesso/queda) - não é lida do banco porque
# não existe hoje uma tabela de temporada->divisão.
TIMES_SERIE_A_API_IDS = [
    131, 134, 794, 128, 124, 121, 133, 135, 127, 7848,   # rastreados
    118, 120, 1062, 147, 126, 136, 119, 130, 1198, 132,  # aguardando ativação
]

app = Flask(__name__)
# NOVO: chave usada pra assinar o cookie de sessão (login). Configure a
# variável de ambiente SECRET_KEY no Railway com um valor aleatório - sem
# isso, a sessão de todo mundo seria invalidada (logout forçado) toda vez
# que o serviço reiniciar/fizer novo deploy.
# 23/10/2026: SEM chave reserva escrita no código. Até 22/10 o serviço rodou
# sem a variável e usava o texto fixo que estava aqui — e quem tem o código
# poderia forjar um login. Sem SECRET_KEY, agora cada processo sorteia uma
# chave própria: ninguém consegue forjar sessão, e o sintoma vira "o login cai
# a cada deploy/reinício" (e o aviso abaixo no log). Não levanta erro porque
# scripts avulsos importam funções do app (`corrigir_estatisticas_pontual`,
# `reabrir_apostas_desatualizadas`) e rodam sem a variável.
_SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
if len(_SECRET_KEY) < 32:
    print("⚠️ SECRET_KEY ausente ou curta (< 32 caracteres): usando chave aleatória deste "
          "processo — os logins caem a cada reinício. Configure SECRET_KEY no Railway.",
          file=sys.stderr, flush=True)
    _SECRET_KEY = secrets.token_hex(32)
app.secret_key = _SECRET_KEY
# 13/10/2026: o login dura 90 dias. Antes o cookie de sessão não era
# "permanente" e sumia quando o navegador fechava - e é por essa sessão que a
# leitura do banco (`/api/consulta`) funciona. Vale só para logins feitos
# depois deste deploy (`session.permanent = True` no /login).
app.permanent_session_lifetime = timedelta(days=90)
app.register_blueprint(api_consulta)


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
            display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap;
            margin-bottom: 8px; font-size: 0.82rem; color: #8b949e;
        }
        .nav-meta-esquerda, .nav-meta-direita {
            display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
        }
        .nav-banca {
            background: #161b22; border: 1px solid #30363d; color: #e6edf3;
            border-radius: 999px; padding: 6px 16px; font-size: 0.8rem; font-weight: 600;
            text-decoration: none;
        }
        .nav-banca:hover { border-color: #58a6ff; }
        .nav-sair { color: #8b949e; text-decoration: none; font-size: 0.82rem; }
        .nav-sair:hover { text-decoration: underline; }

        /* NOVO (30/09/2026 - seletor de liga, SÓ VISUAL): fileira de cards
           logo abaixo dos botões principais. 6 colunas no desktop, 3 no
           tablet, 2 no celular. */
        .nav-ligas {
            display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px;
            max-width: 1100px; margin: -8px auto 26px;
        }
        @media (max-width: 900px) { .nav-ligas { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
        @media (max-width: 520px) { .nav-ligas { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
        .liga-card {
            display: flex; align-items: center; gap: 10px; min-width: 0;
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 10px 12px; text-decoration: none; color: #c9d1d9;
            transition: border-color 0.15s, background 0.15s;
        }
        .liga-card:hover { border-color: #58a6ff; background: #1c2531; }
        .liga-card.liga-ativa { background: #1f6feb33; border-color: #58a6ff88; color: #58a6ff; }
        .liga-card.liga-em-breve { opacity: 0.55; }
        .liga-card.liga-em-breve.liga-ativa { opacity: 1; }
        .liga-bandeira {
            width: 24px; height: 16px; flex-shrink: 0; border-radius: 2px;
            box-shadow: 0 0 0 1px #30363d; display: block;
        }
        .liga-textos { display: flex; flex-direction: column; min-width: 0; }
        .liga-nome {
            font-size: 0.82rem; font-weight: 700; line-height: 1.2;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }
        .liga-selo { font-size: 0.66rem; color: #8b949e; font-weight: 600; letter-spacing: 0.02em; }
        .liga-selo-ok { color: #3fb950; }

        /* aviso de liga ainda sem dados, no lugar do conteúdo da aba */
        .liga-aviso {
            max-width: 620px; margin: 10px auto 0; text-align: center;
            background: #161b22; border: 1px dashed #30363d; border-radius: 14px;
            padding: 36px 28px;
        }
        .liga-aviso .liga-bandeira { width: 48px; height: 32px; margin: 0 auto 16px; }
        .liga-aviso h2 { margin: 0 0 8px; font-size: 1.15rem; color: #e6edf3; }
        .liga-aviso p { margin: 0 0 22px; color: #8b949e; font-size: 0.88rem; line-height: 1.5; }
        .liga-aviso-botao {
            display: inline-block; background: #238636; color: #fff; text-decoration: none;
            border-radius: 10px; padding: 10px 20px; font-weight: 700; font-size: 0.88rem;
        }
        .liga-aviso-botao:hover { background: #2ea043; }
"""


# ---------- Seletor de liga (NOVO 30/09/2026 - SÓ VISUAL) ----------
#
# A liga escolhida fica na SESSÃO (`session["liga"]`) e vale para todas as
# abas até ser trocada. Sem escolha, vale o Brasileirão - quem só usa o
# Brasileirão não vê diferença nenhuma.
#
# ⚠️ Isto NÃO muda consulta, fórmula nem dado. As páginas do Brasileirão
# continuam exatamente iguais. As outras 5 ligas ainda não têm dado no
# banco: escolher uma delas faz as abas dependentes de liga mostrarem um
# aviso no lugar do conteúdo (ver `bloquear_liga_nao_implementada`).
# "Minhas Apostas (ROI)" e a banca são globais e nunca mudam.
#
# As bandeiras são SVG desenhado aqui, não emoji: o Windows não desenha
# bandeira em emoji (mostra "BR", "ES" em letras).
LIGA_PADRAO = "brasileirao"

LIGAS = [
    # (chave, nome, implementada, svg da bandeira)
    ("brasileirao", "Brasileirão", True,
     '<svg class="liga-bandeira" viewBox="0 0 24 16" aria-hidden="true">'
     '<rect width="24" height="16" fill="#009c3b"/>'
     '<polygon points="12,2 22,8 12,14 2,8" fill="#ffdf00"/>'
     '<circle cx="12" cy="8" r="3.6" fill="#002776"/></svg>'),
    ("premier", "Premier League", False,
     '<svg class="liga-bandeira" viewBox="0 0 24 16" aria-hidden="true">'
     '<rect width="24" height="16" fill="#ffffff"/>'
     '<rect x="10" width="4" height="16" fill="#ce1124"/>'
     '<rect y="6" width="24" height="4" fill="#ce1124"/></svg>'),
    ("laliga", "La Liga", False,
     '<svg class="liga-bandeira" viewBox="0 0 24 16" aria-hidden="true">'
     '<rect width="24" height="16" fill="#aa151b"/>'
     '<rect y="4" width="24" height="8" fill="#f1bf00"/></svg>'),
    ("bundesliga", "Bundesliga", False,
     '<svg class="liga-bandeira" viewBox="0 0 24 16" aria-hidden="true">'
     '<rect width="24" height="5.34" fill="#000000"/>'
     '<rect y="5.33" width="24" height="5.34" fill="#dd0000"/>'
     '<rect y="10.66" width="24" height="5.34" fill="#ffce00"/></svg>'),
    ("ligue1", "Ligue 1", False,
     '<svg class="liga-bandeira" viewBox="0 0 24 16" aria-hidden="true">'
     '<rect width="8" height="16" fill="#0055a4"/>'
     '<rect x="8" width="8" height="16" fill="#ffffff"/>'
     '<rect x="16" width="8" height="16" fill="#ef4135"/></svg>'),
    ("seriea", "Serie A (Itália)", False,
     '<svg class="liga-bandeira" viewBox="0 0 24 16" aria-hidden="true">'
     '<rect width="8" height="16" fill="#009246"/>'
     '<rect x="8" width="8" height="16" fill="#ffffff"/>'
     '<rect x="16" width="8" height="16" fill="#ce2b37"/></svg>'),
]
LIGAS_POR_CHAVE = {chave: (nome, implementada, svg) for chave, nome, implementada, svg in LIGAS}

# REDESIGN (15/10/2026): id da liga na API-Football, para a logo (servida
# pelo próprio site em /escudo-liga/<id>.png - a CDN deles recusa o
# pedido direto do navegador). Usado só pelas páginas no visual novo.
# 15/10/2026: o site passa a se chamar "Achismo Zero" ("achismo" = opinião
# sem base, chute; "Achismo Zero" = nada de chute, só o que os números
# dizem). O nome NÃO se traduz; só a frase de apoio muda de língua (para o
# futuro seletor de idioma).
MARCA = "Achismo Zero"
SLOGAN = {"pt": "só o que os números dizem",
          "en": "no guesswork, just numbers",
          "es": "sin corazonadas, solo números"}

LIGA_API_ID = {"brasileirao": 71, "premier": 39, "laliga": 140,
               "bundesliga": 78, "ligue1": 61, "seriea": 135}

# Endpoints cujo conteúdo depende da liga. "minhas_apostas" fica de fora de
# propósito: ROI e banca são globais. Rotas de API/POST também ficam de
# fora - só são chamadas de dentro das páginas do Brasileirão.
ENDPOINTS_POR_LIGA = {
    "index": ("index", "🎯 Gerador de Recomendações"),
    "historico": ("historico", "📊 Histórico de Acertos e Erros"),
    "download_rodada_csv": ("historico", "📊 Histórico de Acertos e Erros"),
    "times_lista": ("times", "🏟️ Estatísticas de Times"),
    "time_detalhe": ("times", "🏟️ Estatísticas de Times"),
    "jogadores": ("jogadores", "📈 Estatísticas de Jogadores"),
    "clube": ("jogadores", "📈 Estatísticas de Jogadores"),
}


def liga_atual():
    """Chave da liga escolhida na sessão. Valor ausente ou desconhecido
    (sessão antiga, cookie mexido) cai no Brasileirão."""
    chave = session.get("liga", LIGA_PADRAO)
    return chave if chave in LIGAS_POR_CHAVE else LIGA_PADRAO


def _caminho_local_seguro(caminho):
    """Só aceita caminho do próprio site ("/times", "/clube/3"). Evita que
    `?voltar=` vire redirecionamento para outro domínio."""
    if not caminho or not caminho.startswith("/") or caminho.startswith("//") or "\\" in caminho:
        return "/"
    return caminho


def barra_ligas(visual_novo=False):
    """Fileira de cards das 6 ligas. O link leva o caminho atual (sem a
    query string, de propósito: no Gerador a query pode disparar geração)
    para voltar à MESMA aba depois de trocar de liga.

    REDESIGN (15/10/2026): com `visual_novo=True`, a bandeira dá lugar à
    logo da liga num selo de vidro. As páginas ainda no visual antigo
    chamam sem o parâmetro e não mudam."""
    atual = liga_atual()
    voltar = quote(request.path or "/", safe="/")
    cards = []
    for chave, nome, implementada, svg in LIGAS:
        classes = "liga-card"
        if chave == atual:
            classes += " liga-ativa"
        if not implementada:
            classes += " liga-em-breve"
        selo = ('<span class="liga-selo liga-selo-ok">disponível</span>' if implementada
                else '<span class="liga-selo">em breve</span>')
        if visual_novo:
            svg = (f'<span class="liga-selo-logo"><img alt="" loading="lazy" '
                   f'src="/escudo-liga/{LIGA_API_ID[chave]}.png"></span>')
        cards.append(
            f'<a href="/liga/{chave}?voltar={voltar}" class="{classes}" title="{nome}">'
            f'{svg}<span class="liga-textos"><span class="liga-nome">{nome}</span>{selo}</span></a>'
        )
    return f'<div class="nav-ligas">{"".join(cards)}</div>'


def barra_navegacao_nova(pagina_atual, banca_atual=None):
    """REDESIGN (15/10/2026): barra do visual novo, no estilo "clean".
    Nome do site à esquerda, os links num grupo só no meio (sem ícones,
    com nomes curtos) e, à direita, a banca e o botão "Conta". "Sair" saiu
    daqui: vai morar na página de Conta. Enquanto ela não existe, "Conta"
    leva a Minhas Apostas - o mesmo que a /inicio já faz."""
    itens = [
        ("historico", "/historico", "Histórico"),
        ("times", "/times", "Times"),
        ("index", "/", "Recomendações"),
        ("jogadores", "/jogadores", "Jogadores"),
        ("roi", "/minhas-apostas", "Minhas Apostas"),
    ]
    # 22/10/2026: "Ligas globais" saiu do menu; a logo leva à /inicio.
    links = ""
    for chave, href, rotulo in itens:
        ativo = chave == pagina_atual
        classe = "topo-link topo-ativo" if ativo else "topo-link"
        atual = ' aria-current="page"' if ativo else ""
        links += f'<a href="{href}" class="{classe}" data-prev="{chave}"{atual}>{rotulo}</a>'
    banca = ""
    if banca_atual is not None:
        banca = f'<a href="/minhas-apostas" class="topo-banca">Banca R$ {banca_atual:.2f}</a>'
    return (
        '<header class="topo">'
        f'<a href="/inicio" class="topo-marca" aria-label="{MARCA} - Ligas globais" title="{MARCA}, {SLOGAN["pt"]} - Ligas globais">'
        'Achismo <span class="logo-zero" aria-hidden="true"></span></a>'
        f'<nav class="topo-nav" aria-label="Páginas">{links}</nav>'
        f'<div class="topo-direita">{banca}<a href="/minhas-apostas" class="topo-conta">Conta</a></div>'
        '</header>'
        # 22/10/2026: prévia das páginas ao passar o mouse (static/topo/)
        '<link rel="stylesheet" href="/static/topo/previas.css">'
        '<div id="prev" class="prev" aria-hidden="true"><div class="prev-palco"></div></div>'
        '<script src="/static/topo/previas.js" defer></script>'
        f'{barra_ligas(visual_novo=True)}'
    )


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

    esquerda = (f'<span>Olá, {session.get("usuario_nome", "")}</span>'
                '<a href="/inicio" class="nav-sair">🌍 Ligas globais</a>'
                '<a href="/logout" class="nav-sair">🚪 Sair</a>')
    direita = ""
    if banca_atual is not None:
        direita = f'<a href="/minhas-apostas" class="nav-banca">🏦 Banca: R$ {banca_atual:.2f}</a>'

    meta = f'<div class="nav-meta-esquerda">{esquerda}</div><div class="nav-meta-direita">{direita}</div>'

    # NOVO (30/09/2026): seletor de liga logo abaixo dos botões principais.
    return f'<div class="nav-meta">{meta}</div><div class="nav-principal">{botoes}</div>{barra_ligas()}'


def _float_ou_none(texto):
    """NOVO (30/08/2026): converte o texto de um campo opcional do
    formulário em float, devolvendo None quando o campo está em branco
    (= "sem filtro") ou quando o conteúdo não é um número.

    Tolerar lixo em vez de estourar é proposital: esses campos vêm da URL,
    então qualquer pessoa pode chegar com `?prob_min=abc` - e um
    ValueError ali derrubaria a página inteira do gerador, não só o
    filtro. Aceita vírgula como separador decimal porque é o que o teclado
    brasileiro entrega."""
    if texto is None:
        return None
    texto = str(texto).strip().replace(",", ".")
    if not texto:
        return None
    try:
        return float(texto)
    except ValueError:
        return None


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

# NOVO: token de "clique real", de uso único, guardado na sessão do
# usuário (server-side, não em cookie/localStorage). Existe porque a trava
# de "auto=1" só protegia o redirecionamento automático via JS (voltar
# navegando pra página) - ela NÃO protegia um F5 direto numa URL que já
# tinha odd_min/odd_max (de um clique real anterior) ou num link
# "Atualizar recomendações" (que carrega forcar=1, e esse ignora até a
# trava de 1h). Nos dois casos, a URL sozinha não muda com F5, então o
# servidor não tinha como distinguir "cliquei de novo" de "só recarreguei
# a página".
#
# Funcionamento: toda vez que a página "/" é renderizada, um token novo é
# gerado e guardado em session["token_busca_pendente"], e o MESMO token é
# embutido no HTML (campo oculto do formulário de busca e no link
# "Atualizar recomendações"). Quando uma requisição chega com
# token_busca=X e X bate com o que está guardado na sessão, isso prova
# que veio de um clique de verdade nessa página específica (não um F5,
# que reenviaria um token JÁ CONSUMIDO) - e o token é imediatamente
# removido da sessão (uso único), então um F5 logo em seguida chega com
# um token que não bate mais com nada, e não dispara a atualização de
# odds (mesmo que a URL pareça idêntica). O `auto=1` do redirecionamento
# automático continua existindo só de referência/log; quem decide de
# verdade se dispara é o token.
def gerar_e_guardar_token_busca():
    token = secrets.token_urlsafe(16)
    session["token_busca_pendente"] = token
    return token


def validar_e_consumir_token_busca(token_recebido):
    """Devolve True só se `token_recebido` bate com o token pendente da
    sessão - nesse caso, já consome (remove) o token, pra não poder ser
    reaproveitado por um F5/reenvio da mesma URL."""
    if not token_recebido:
        return False
    valido = token_recebido == session.get("token_busca_pendente")
    if valido:
        session.pop("token_busca_pendente", None)
    return valido


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


# NOVO: trava simples pra impedir que duas atualizações rodem ao mesmo
# tempo (aconteceu de verdade: dois cliques próximos geraram duas rodadas
# em paralelo, duplicando chamadas à OddsPapi e intercalando os logs dos
# dois processos). threading.Event é seguro entre threads sem precisar de
# lock manual - is_set()/set()/clear() são atômicos.
_atualizacao_em_andamento = threading.Event()

# NOVO (acompanhamento em tempo real): quanto tempo uma execução pode
# ficar marcada como 'rodando' antes de ser considerada MORTA. Existe pro
# caso do container reiniciar no meio (deploy, queda, restart do Railway):
# sem isso, a linha ficaria 'rodando' pra sempre, o app acharia que tem
# atualização em andamento eternamente, e todo clique futuro seria
# bloqueado com um timer infinito na tela - trocando o problema atual por
# um pior. 15 minutos dá ~3x de folga sobre a execução mais longa já
# medida (4m37s) sem deixar o usuário travado muito tempo num caso raro.
LIMITE_EXECUCAO_ABANDONADA_MINUTOS = 15

# NOVO: quantas execuções concluídas entram na média que estima a duração
# da próxima (alimenta a barra de progresso). Poucas o bastante pra
# acompanhar mudança real de tempo (mais times, mais jogos na rodada),
# muitas o bastante pra uma execução atípica não dominar a estimativa.
EXECUCOES_PARA_ESTIMATIVA = 5

# NOVO: chute inicial, usado só enquanto não existe nenhuma execução
# concluída registrada pra calcular a média de verdade.
ESTIMATIVA_PADRAO_SEGUNDOS = 300

# NOVO (26/08/2026): janela de idempotência do "Salvar aposta" - ver
# comentário em salvar_aposta(). Segundos o bastante pra cobrir um
# duplo-clique/duplo-submit (sempre <2s na prática), curta o bastante
# pra nunca bloquear uma aposta legítima repetida de propósito minutos
# depois.
SEGUNDOS_JANELA_DUPLICATA = 10


def marcar_execucoes_abandonadas(cur):
    """NOVO: fecha execuções que ficaram 'rodando' além do limite - quase
    sempre porque o processo morreu junto com o container. Chamada antes
    de qualquer leitura de estado, pra que uma execução fantasma nunca
    seja confundida com uma de verdade."""
    cur.execute(
        """
        UPDATE execucoes_atualizacao
        SET status = 'abandonada',
            finalizada_em = NOW(),
            detalhe = 'Execução não finalizou dentro do limite - provavelmente o processo foi interrompido.'
        WHERE status = 'rodando'
          AND iniciada_em < NOW() - (%s * INTERVAL '1 minute')
        """,
        (LIMITE_EXECUCAO_ABANDONADA_MINUTOS,),
    )
    return cur.rowcount


def buscar_execucao_em_andamento(cur):
    """NOVO: devolve (id, iniciada_em) da execução viva, ou None. Fonte
    ÚNICA da verdade sobre "tem atualização rodando?" - o threading.Event
    antigo continua existindo só como trava rápida dentro do mesmo
    processo, mas quem responde pra interface é sempre o banco (funciona
    com qualquer número de workers do Gunicorn)."""
    marcar_execucoes_abandonadas(cur)
    cur.execute(
        """
        SELECT id, iniciada_em
        FROM execucoes_atualizacao
        WHERE status = 'rodando'
        ORDER BY iniciada_em DESC
        LIMIT 1
        """
    )
    return cur.fetchone()


def estimar_duracao_atualizacao(cur):
    """NOVO: média de duração das últimas execuções concluídas, em
    segundos - base da barra de progresso. A estimativa melhora sozinha
    conforme o histórico cresce; enquanto não houver nenhuma execução
    concluída, usa ESTIMATIVA_PADRAO_SEGUNDOS."""
    cur.execute(
        """
        SELECT AVG(EXTRACT(EPOCH FROM (finalizada_em - iniciada_em)))
        FROM (
            SELECT iniciada_em, finalizada_em
            FROM execucoes_atualizacao
            WHERE status = 'concluida' AND finalizada_em IS NOT NULL
            ORDER BY finalizada_em DESC
            LIMIT %s
        ) AS ultimas
        """,
        (EXECUCOES_PARA_ESTIMATIVA,),
    )
    row = cur.fetchone()
    if not row or row[0] is None:
        return ESTIMATIVA_PADRAO_SEGUNDOS
    return max(1, int(round(float(row[0]))))


def abrir_execucao_atualizacao(cur, usuario_id, forcada):
    """NOVO: registra o início de uma execução e devolve o id, usado
    depois pra fechá-la."""
    cur.execute(
        """
        INSERT INTO execucoes_atualizacao (status, iniciada_em, usuario_id, forcada)
        VALUES ('rodando', NOW(), %s, %s)
        RETURNING id
        """,
        (usuario_id, bool(forcada)),
    )
    return cur.fetchone()[0]


def fechar_execucao_atualizacao(execucao_id, status, detalhe=None):
    """NOVO: fecha a execução. Abre conexão PRÓPRIA de propósito - roda na
    thread de segundo plano, e a conexão da requisição que disparou já
    morreu há muito tempo quando os scripts terminam (a requisição
    responde em milissegundos; os scripts levam minutos)."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE execucoes_atualizacao
                SET status = %s, finalizada_em = NOW(), detalhe = %s
                WHERE id = %s AND status = 'rodando'
                """,
                (status, detalhe, execucao_id),
            )
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        # Não deixa um erro ao FECHAR o registro derrubar a thread - o
        # varredor de abandonadas resolve o caso mais tarde.
        print(f"[atualizacao_odds] Falha ao fechar a execução {execucao_id}: {e}")


def atualizacao_em_andamento():
    return _atualizacao_em_andamento.is_set()


def disparar_atualizacao_odds_railway(usuario_id=None, forcada=False):
    """NOVO (troca de abordagem): antes isso chamava a API do Railway pra
    pedir pra rodar o serviço `refreshing-freedom` remotamente
    (serviceInstanceRedeploy) - só que, na prática, testamos várias vezes
    e o disparo por essa API NÃO produzia o mesmo efeito de clicar "Run
    Now" no painel do Railway (a documentação pública deles também não
    lista nenhum mutation específico pra "rodar um Cron agora", só pra
    criar/redeployar - o botão do painel parece usar um caminho interno
    que não está disponível de fora).

    Em vez de continuar dependendo desse comportamento não confiável e não
    documentado, essa função agora roda o `atualizar_odds.py` e o
    `motor_recomendacoes.py` DIRETO nesse mesmo serviço (a Interface),
    como um processo em segundo plano (thread) - já que os dois scripts
    já ficam no mesmo repositório e esse serviço já tem tudo que eles
    precisam pra funcionar (DATABASE_URL, API_FOOTBALL_KEY). Só falta uma
    variável nova aqui na Interface: ODDSPAPI_KEY (a mesma chave que o
    refreshing-freedom já usa).

    Roda numa thread separada (não trava a resposta da página) e devolve
    True imediatamente, assim que consegue INICIAR os scripts - não espera
    eles terminarem (isso ainda leva os mesmos ~20-90s de sempre, só que
    agora rodando de verdade, sem depender de nenhuma API externa). Se já
    tiver uma atualização em andamento, não inicia outra - devolve False."""
    if _atualizacao_em_andamento.is_set():
        print("[atualizacao_odds] Já tem uma atualização em andamento - ignorando esse novo pedido "
              "(evita rodar em paralelo e duplicar chamadas à OddsPapi).")
        return False

    def rodar_em_segundo_plano():
        pasta = os.path.dirname(os.path.abspath(__file__))
        _atualizacao_em_andamento.set()
        # NOVO: status final da execução, gravado no banco pra que a
        # interface saiba quando parar de mostrar o progresso.
        status_final = "concluida"
        detalhe_final = None
        try:
            print("[atualizacao_odds] Rodando atualizar_odds.py...")
            subprocess.run(
                [sys.executable, os.path.join(pasta, "atualizar_odds.py")],
                check=True, timeout=600, cwd=pasta,
            )
            print("[atualizacao_odds] atualizar_odds.py concluído. Rodando motor_recomendacoes.py...")
            subprocess.run(
                [sys.executable, os.path.join(pasta, "motor_recomendacoes.py")],
                check=True, timeout=600, cwd=pasta,
            )
            print("[atualizacao_odds] motor_recomendacoes.py concluído - atualização completa.")
        except subprocess.CalledProcessError as e:
            print(f"[atualizacao_odds] Um dos scripts terminou com erro (código {e.returncode}).")
            status_final = "erro"
            detalhe_final = f"Script terminou com código {e.returncode}."
        except subprocess.TimeoutExpired:
            print("[atualizacao_odds] Um dos scripts passou de 10 minutos rodando - abortado.")
            status_final = "erro"
            detalhe_final = "Um dos scripts passou de 10 minutos rodando e foi abortado."
        except Exception as e:
            print(f"[atualizacao_odds] Falha inesperada ao rodar em segundo plano: {e}")
            status_final = "erro"
            detalhe_final = f"Falha inesperada: {e}"
        finally:
            _atualizacao_em_andamento.clear()
            # NOVO: fecha o registro SEMPRE, inclusive em erro - se ficasse
            # aberto, a interface mostraria progresso pra sempre até o
            # varredor de abandonadas agir 15 minutos depois.
            if execucao_id is not None:
                fechar_execucao_atualizacao(execucao_id, status_final, detalhe_final)

    # NOVO: registra o início ANTES de subir a thread. Se o INSERT falhar,
    # nada é disparado - melhor não rodar do que rodar sem a interface
    # conseguir acompanhar (o usuário ficaria de novo sem saber se o botão
    # fez alguma coisa, que é exatamente o problema que isso resolve).
    execucao_id = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            cur = conn.cursor()
            execucao_id = abrir_execucao_atualizacao(cur, usuario_id, forcada)
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        print(f"[atualizacao_odds] Falha ao registrar o início da execução: {e}")
        return False

    try:
        threading.Thread(target=rodar_em_segundo_plano, daemon=True).start()
        return True
    except Exception as e:
        print(f"[atualizacao_odds] Falha ao iniciar a thread de atualização: {e}")
        fechar_execucao_atualizacao(execucao_id, "erro", f"Não foi possível iniciar a thread: {e}")
        return False


def processar_atualizacao_odds(cur, usuario_id, forcar):
    """Decide se dispara o Run Now do refreshing-freedom (dispara se nunca
    rodou, se já faz mais de 1h do último disparo, ou se `forcar=True`) e
    devolve (ultima, disparou_agora) - `disparou_agora` diz se ESSA
    chamada especificamente acabou de mandar o pedido pro Railway (usado
    pra explicar ao usuário por que ainda não tem recomendação nova: o
    disparo em si é rápido, mas o deploy real no Railway leva bem mais
    tempo pra concluir - não dá pra esperar isso terminar na mesma
    requisição HTTP sem travar a página por 1-2 minutos)."""
    ultima = buscar_ultima_atualizacao_odds(cur)
    ja_passou_1h = ultima is None or (datetime.now(timezone.utc) - ultima) >= INTERVALO_MINIMO_ATUALIZACAO_ODDS

    disparou_agora = False
    if forcar or ja_passou_1h:
        if disparar_atualizacao_odds_railway(usuario_id, forcar):
            registrar_atualizacao_odds(cur, usuario_id, forcar)
            ultima = datetime.now(timezone.utc)
            disparou_agora = True

    return ultima, disparou_agora


PAGINA = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <link rel="icon" href="/static/marca/favicon.svg" type="image/svg+xml">
    <link rel="icon" href="/static/marca/favicon-32.png" sizes="32x32" type="image/png">
    <link rel="apple-touch-icon" href="/static/marca/apple-touch-icon.png">
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Recomendações - Achismo Zero</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Unbounded:wght@500;700&family=IBM+Plex+Sans:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
    <style>
        /* REDESIGN (15/10/2026): mesma paleta, fontes, vidro e fundo da
           página inicial pública (/inicio, paleta "b"). Muda só a aparência:
           formulários, campos, rotas e scripts da página são os de antes.
           Tamanhos em rem; o rem cresce com a largura da tela (14 a 18px).
           Layout: topo na largura toda; abaixo dele, em "escadinha", ligas
           (90%) e filtro + listas (80%), centralizados. */
        :root {
            --noite: #01050f;
            --linha: rgba(189, 191, 199, .22);
            --linha-fraca: rgba(189, 191, 199, .12);
            --texto: #f1f2f6;
            --apagado: #aeb3c3;
            --azul: #3f6fd1;
            --azul-claro: #b4c8f2;
            --ouro: #ffd20a;
            --ouro-claro: #ffe066;
            --cinza: #bdbfc7;
            --cta-texto: #1d1600;
            --verde: #5fd38d;
            --vermelho: #ff7a7a;
            --f-titulo: "Unbounded", "Arial Black", system-ui, sans-serif;
            --f-corpo: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
            --f-dado: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
            --raio: 0.875rem;
            --margem: clamp(1rem, 2.5vw, 3rem);
            --degrau1: 90%;
            --degrau2: 80%;
            /* vidro sem backdrop-filter: sobre o degradê (já desfocado) fica
               igual e não custa quadro (ver /inicio) */
            --vidro: linear-gradient(140deg, rgba(255,255,255,.13), rgba(255,255,255,.035) 55%, rgba(255,255,255,.07)), rgba(4, 14, 38, .42);
            --vidro-borda: rgba(255, 255, 255, .17);
            --vidro-sombra: inset 0 1px 0 rgba(255,255,255,.25), inset 0 -1px 0 rgba(255,255,255,.04), 0 0.875rem 2.75rem -1rem rgba(0,0,0,.6);
        }
        * { box-sizing: border-box; }
        *::before, *::after { box-sizing: border-box; }
        html {
            font-size: clamp(14px, calc(100vw / 112), 18px);
            background: var(--noite);
        }
        body {
            font-family: var(--f-corpo);
            font-size: 0.9375rem;
            line-height: 1.5;
            background: transparent;
            color: var(--texto);
            width: calc(100% - 2 * var(--margem));
            margin: 0 auto;
            padding: 1rem 0 6rem;
        }
        a { color: var(--azul-claro); }
        ::selection { background: rgba(180, 200, 242, .35); }
        button:focus-visible, a:focus-visible { outline: 2px solid var(--azul-claro); outline-offset: 2px; }

        /* ---------- fundo: o degradê vivo da /inicio ----------
           WebGL a ~24 quadros por segundo, em resolução baixa (script no
           fim da página). Sem WebGL, fica o degradê parado abaixo. */
        .fundo {
            position: fixed; inset: 0; z-index: -1; overflow: hidden; pointer-events: none;
            background:
                radial-gradient(60vmax 45vmax at 8% -6%, rgba(63, 111, 209, .30), rgba(63, 111, 209, 0) 70%),
                radial-gradient(55vmax 40vmax at 104% 18%, rgba(92, 74, 0, .55), rgba(92, 74, 0, 0) 70%),
                radial-gradient(70vmax 50vmax at 70% 112%, rgba(6, 32, 79, .85), rgba(6, 32, 79, 0) 70%),
                linear-gradient(180deg, #020a1f 0%, var(--noite) 60%);
        }
        #fundo-gl { position: absolute; inset: 0; width: 100%; height: 100%; display: block; }

        /* ---------- filtro (caixa de gerar recomendações) ---------- */
        .painel {
            background: var(--vidro);
            border: 1px solid var(--vidro-borda);
            box-shadow: var(--vidro-sombra);
            border-radius: 0.75rem;
            padding: 1rem 1.125rem;
            width: var(--degrau2);
            margin: 0 auto 3.5rem;
        }
        .linha-filtro {
            display: flex;
            gap: 0.625rem;
            align-items: flex-end;
            flex-wrap: wrap;
        }
        /* campos à esquerda, os dois botões à direita */
        .linha-filtro button[type=submit]:not(.btn-so-mostrar) { margin-left: auto; }
        .nota-espera {
            color: var(--apagado); font-size: 0.6875rem; line-height: 1.55;
            margin: 0.625rem 0 0; max-width: 62rem;
        }
        .nota-espera b { color: var(--texto); }
        .linha-atualizacao {
            display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.625rem;
            margin-top: 0.75rem; padding-top: 0.75rem; border-top: 1px solid var(--linha-fraca);
            font-size: 0.75rem; color: var(--apagado);
        }
        .linha-atualizacao b { color: var(--texto); font-family: var(--f-dado); font-weight: 500; }
        .link-atualizar {
            background: rgba(255,255,255,.05); border: 1px solid var(--linha); color: var(--azul-claro);
            border-radius: 999px; padding: 0.3125rem 0.75rem; font-size: 0.75rem; font-weight: 600;
            text-decoration: none; transition: border-color .15s, background .15s;
        }
        .link-atualizar:hover { border-color: var(--azul-claro); background: rgba(180, 200, 242, .1); }
        label {
            display: block;
            font-family: var(--f-corpo);
            font-size: 0.75rem;
            letter-spacing: 0;
            text-transform: none;
            color: var(--apagado);
            margin-bottom: 0.3125rem;
            white-space: nowrap;
        }
        input[type=number] {
            background: rgba(1, 5, 15, .55);
            border: 1px solid var(--linha);
            color: var(--texto);
            border-radius: 0.5rem;
            padding: 0.4375rem 0.625rem;
            width: 5.5rem;
            font-family: var(--f-dado);
            font-size: 0.8125rem;
            font-variant-numeric: tabular-nums;
            transition: border-color .15s, box-shadow .15s;
        }
        input[type=number]::placeholder { color: #7d849c; font-family: var(--f-corpo); font-size: 0.75rem; }
        input[type=number]:focus {
            outline: none; border-color: var(--azul-claro);
            box-shadow: 0 0 0 3px rgba(180, 200, 242, .18);
        }
        button {
            background: var(--ouro);
            color: var(--cta-texto);
            border: 1px solid transparent;
            border-radius: 999px;
            padding: 0.5rem 1rem;
            font-family: var(--f-corpo);
            font-size: 0.8125rem;
            font-weight: 600;
            cursor: pointer;
            transition: background .15s, transform .1s;
        }
        button:hover { background: var(--ouro-claro); }
        button:active { transform: translateY(1px); }
        /* NOVO (29/08/2026): "Só mostrar (sem gerar)" - de propósito
           visualmente SECUNDÁRIO (contorno, sem preenchimento) em relação
           ao botão cheio. O cheio (ouro) é a ação que gasta cota da
           OddsPapi e regera tudo; este aqui é o inofensivo, que só relê o
           que já existe. A diferença visual evita clicar no caro achando
           que é o barato. */
        .btn-so-mostrar {
            background: transparent;
            color: var(--azul-claro);
            border: 1px solid rgba(180, 200, 242, .55);
        }
        .btn-so-mostrar:hover { background: rgba(180, 200, 242, .1); }

        """ + NAV_CSS + """
        /* O NAV_CSS acima é o das páginas ainda no visual antigo; daqui em
           diante ele é ajustado SÓ para esta página. */

        /* ---------- topo: nome | links no centro | banca e Conta ---------- */
        .topo {
            display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 1rem;
            margin: 0 0 1.25rem;
        }
        .topo-marca {
            justify-self: start;
            font-family: var(--f-titulo); font-weight: 700; font-size: 0.875rem; letter-spacing: -.01em;
            color: var(--texto); text-decoration: none; white-space: nowrap;
        }
        /* 16/10/2026: "Zero" vira o logo desenhado pelo usuário - o desenho
           recorta um degradê ouro -> azul (mesmas cores do brilho da /inicio) */
        .logo-zero {
            display: inline-block; height: 0.86em; aspect-ratio: 1928.53 / 369.92;
            vertical-align: -0.04em; margin-left: 0.12em;
            background: linear-gradient(100deg, #ffd20a 0%, #fff3b0 35%, #bdbfc7 65%, #7fa2ea 100%);
            -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='3.47 307.06 1928.53 369.92'%3E%3Cg fill='%23000'%3E%3Cpath d='M563.46,314.21l-293.13,266.25c-5.76,5.23-2.06,14.81,5.72,14.81h250.01c8.12,0,11.63,10.28,5.2,15.25l-82.69,63.89c-1.49,1.15-3.32,1.78-5.2,1.78H22c-7.78,0-11.48-9.57-5.73-14.81l284.32-258.81c5.75-5.24,2.05-14.81-5.73-14.81h-163.65c-7.78,0-11.48-9.58-5.72-14.81l68.31-62.07c1.56-1.42,3.59-2.21,5.7-2.21l361.38,-1.13c5.48,-0.02,6.64,2.98,2.59,6.66Z' transform='translate(-10 0)'/%3E%3Cpath d='M238.26,652.63l380.83-341.3c2.43-2.18,5.58-3.38,8.84-3.38h279.74c12.16,0,17.89,15,8.84,23.11l-43.83,39.28c-2.43,2.18-5.58,3.38-8.84,3.38h-206.71c-3.3,0-6.48,1.23-8.92,3.46l-48.78,44.47c-8.98,8.19-3.12,23.14,9.03,23.04l146.45-1.19c12.11-.1,17.99,14.76,9.1,22.97l-40.54,37.49c-2.45,2.26-5.66,3.52-8.99,3.52h-205.62c-3.28,0-6.44,1.22-8.88,3.42l-47.32,42.73c-9,8.13-3.25,23.08,8.88,23.08h146.16c12.15,0,17.89,14.99,8.85,23.11l-80.85,72.54c-2.43,2.18-5.58,3.39-8.85,3.39H247.1c-12.16,0-17.89-15-8.84-23.11Z'/%3E%3Cpath d='M1901.71,307.06l-404.11.29c-3.96,0-10.53,5.28-15.79,10.78l-362.08,326.91c-14.03,11.22-9.92,31.62,7.63,31.58l362.77-.81c3.95,0,52.41,2.59,55.57.07l366.99-333.87c14-11.22,6.55-34.95-10.98-34.95ZM1710.9,358.2h43.17l-496.68,217.3,126.27-101.01c94.36-75.48,209.2-116.29,327.24-116.29ZM1790.45,393.15l-144.2,115.57c-94.12,75.43-208.68,116.35-326.49,116.61l-21.56.05,495.02-234.95c-.81.96-1.72,1.88-2.77,2.72Z' transform='translate(12 0)'/%3E%3Cpath d='M1239.16,507.79l202.79-183.3c5.68-5.29,4.71-17.42-2.83-17.42l-447.04,1.67c-1.94,0-3.82.74-5.3,2.08l-384.98,350.66c-5.77,5.25-2.29,15.33,5.3,15.33h115.11c1.94,0,3.81-.74,5.29-2.08l178.91-162.72c1.48-1.34,3.35-2.08,5.29-2.08h66.31c2.44,0,4.75,1.17,6.3,3.18l123.71,160.52c1.55,2.01,3.86,3.18,6.3,3.18h128.81c7.12,0,10.82-9.05,5.99-14.63l-132-152.25h116.68c1.97,0,3.88-.76,5.36-2.14ZM977.69,423.26l43.55-46.49c1.53-1.63,3.61-2.55,5.77-2.55h207.61c7.45,0,11,9.79,5.49,15.15l-47.85,46.49c-1.5,1.46-3.46,2.27-5.49,2.27h-203.31c-7.27,0-10.91-9.38-5.77-14.87Z'/%3E%3C/g%3E%3C/svg%3E") no-repeat center / contain;
            mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='3.47 307.06 1928.53 369.92'%3E%3Cg fill='%23000'%3E%3Cpath d='M563.46,314.21l-293.13,266.25c-5.76,5.23-2.06,14.81,5.72,14.81h250.01c8.12,0,11.63,10.28,5.2,15.25l-82.69,63.89c-1.49,1.15-3.32,1.78-5.2,1.78H22c-7.78,0-11.48-9.57-5.73-14.81l284.32-258.81c5.75-5.24,2.05-14.81-5.73-14.81h-163.65c-7.78,0-11.48-9.58-5.72-14.81l68.31-62.07c1.56-1.42,3.59-2.21,5.7-2.21l361.38,-1.13c5.48,-0.02,6.64,2.98,2.59,6.66Z' transform='translate(-10 0)'/%3E%3Cpath d='M238.26,652.63l380.83-341.3c2.43-2.18,5.58-3.38,8.84-3.38h279.74c12.16,0,17.89,15,8.84,23.11l-43.83,39.28c-2.43,2.18-5.58,3.38-8.84,3.38h-206.71c-3.3,0-6.48,1.23-8.92,3.46l-48.78,44.47c-8.98,8.19-3.12,23.14,9.03,23.04l146.45-1.19c12.11-.1,17.99,14.76,9.1,22.97l-40.54,37.49c-2.45,2.26-5.66,3.52-8.99,3.52h-205.62c-3.28,0-6.44,1.22-8.88,3.42l-47.32,42.73c-9,8.13-3.25,23.08,8.88,23.08h146.16c12.15,0,17.89,14.99,8.85,23.11l-80.85,72.54c-2.43,2.18-5.58,3.39-8.85,3.39H247.1c-12.16,0-17.89-15-8.84-23.11Z'/%3E%3Cpath d='M1901.71,307.06l-404.11.29c-3.96,0-10.53,5.28-15.79,10.78l-362.08,326.91c-14.03,11.22-9.92,31.62,7.63,31.58l362.77-.81c3.95,0,52.41,2.59,55.57.07l366.99-333.87c14-11.22,6.55-34.95-10.98-34.95ZM1710.9,358.2h43.17l-496.68,217.3,126.27-101.01c94.36-75.48,209.2-116.29,327.24-116.29ZM1790.45,393.15l-144.2,115.57c-94.12,75.43-208.68,116.35-326.49,116.61l-21.56.05,495.02-234.95c-.81.96-1.72,1.88-2.77,2.72Z' transform='translate(12 0)'/%3E%3Cpath d='M1239.16,507.79l202.79-183.3c5.68-5.29,4.71-17.42-2.83-17.42l-447.04,1.67c-1.94,0-3.82.74-5.3,2.08l-384.98,350.66c-5.77,5.25-2.29,15.33,5.3,15.33h115.11c1.94,0,3.81-.74,5.29-2.08l178.91-162.72c1.48-1.34,3.35-2.08,5.29-2.08h66.31c2.44,0,4.75,1.17,6.3,3.18l123.71,160.52c1.55,2.01,3.86,3.18,6.3,3.18h128.81c7.12,0,10.82-9.05,5.99-14.63l-132-152.25h116.68c1.97,0,3.88-.76,5.36-2.14ZM977.69,423.26l43.55-46.49c1.53-1.63,3.61-2.55,5.77-2.55h207.61c7.45,0,11,9.79,5.49,15.15l-47.85,46.49c-1.5,1.46-3.46,2.27-5.49,2.27h-203.31c-7.27,0-10.91-9.38-5.77-14.87Z'/%3E%3C/g%3E%3C/svg%3E") no-repeat center / contain;
        }
        .topo-nav {
            justify-self: center;
            display: flex; align-items: center; gap: 0.125rem; padding: 0.25rem;
            border-radius: 999px; min-width: 0;
            background: var(--vidro); border: 1px solid var(--vidro-borda); box-shadow: var(--vidro-sombra);
        }
        .topo-link {
            padding: 0.375rem 0.75rem; border-radius: 999px; white-space: nowrap;
            color: var(--apagado); font-size: 0.8125rem; font-weight: 500; text-decoration: none;
            transition: color .15s, background .15s;
        }
        .topo-link:hover { color: var(--texto); background: rgba(255, 255, 255, .06); }
        .topo-link.topo-ativo { color: var(--texto); background: rgba(255, 255, 255, .1); }
        .topo-direita { justify-self: end; display: flex; align-items: center; gap: 0.5rem; flex-shrink: 0; }
        .topo-banca {
            font-family: var(--f-dado); font-size: 0.6875rem; color: var(--texto); text-decoration: none; white-space: nowrap;
            padding: 0.375rem 0.625rem; border-radius: 999px; border: 1px solid var(--linha);
        }
        .topo-banca:hover { border-color: var(--azul-claro); }
        .topo-conta {
            padding: 0.4375rem 0.9375rem; border-radius: 999px; white-space: nowrap;
            color: var(--texto); font-size: 0.8125rem; font-weight: 600; text-decoration: none;
            background: var(--vidro); border: 1px solid var(--vidro-borda); box-shadow: var(--vidro-sombra);
            transition: border-color .15s;
        }
        .topo-conta:hover { border-color: var(--azul-claro); }

        /* ---------- ligas ---------- */
        .nav-ligas { max-width: none; width: var(--degrau1); gap: 0.5rem; margin: 0 auto 3rem; }
        .liga-card {
            background: var(--vidro); border: 1px solid var(--vidro-borda); border-radius: 0.625rem;
            padding: 0.4375rem 0.5625rem; color: var(--cinza); gap: 0.5rem;
        }
        .liga-card:hover { border-color: var(--azul-claro); background: rgba(180, 200, 242, .08); }
        .liga-card.liga-ativa { background: rgba(63, 111, 209, .22); border-color: rgba(180, 200, 242, .6); color: var(--texto); }
        /* logo da liga num selo de vidro (mesmo selo da /inicio), sem fundo branco */
        .liga-selo-logo {
            width: 1.625rem; height: 1.625rem; flex-shrink: 0; border-radius: 0.5rem;
            display: grid; place-items: center;
            background: linear-gradient(140deg, rgba(255,255,255,.34), rgba(255,255,255,.16));
            border: 1px solid rgba(255,255,255,.28); box-shadow: inset 0 1px 0 rgba(255,255,255,.35);
        }
        .liga-selo-logo img {
            width: 1.125rem; height: 1.125rem; object-fit: contain; display: block;
            filter: drop-shadow(0 0 1px rgba(255,255,255,.55)) drop-shadow(0 1px 3px rgba(0,0,0,.45));
        }
        .liga-nome { font-size: 0.75rem; font-weight: 600; }
        .liga-selo { font-family: var(--f-corpo); font-size: 0.6875rem; letter-spacing: 0; text-transform: none; color: var(--apagado); font-weight: 500; }
        .liga-selo-ok { color: var(--azul-claro); }

        /* ---------- mensagens ---------- */
        .flash {
            width: var(--degrau2); margin: 0 auto 1.5rem;
            padding: 0.625rem 0.875rem; border-radius: 0.625rem; font-size: 0.75rem;
        }
        .flash-erro { background: rgba(255, 122, 122, .12); color: var(--vermelho); border: 1px solid rgba(255, 122, 122, .4); }
        .flash-sucesso { background: rgba(95, 211, 141, .12); color: var(--verde); border: 1px solid rgba(95, 211, 141, .4); }
        .aviso {
            background: rgba(255, 210, 10, .08);
            border: 1px solid rgba(255, 210, 10, .45);
            color: var(--ouro-claro);
            border-radius: 0.625rem;
            padding: 0.75rem 1rem;
            font-size: 0.8125rem;
            margin-bottom: 1.5rem;
        }

        /* ---------- NOVO (acompanhamento em tempo real): painel que
           substitui as listas de odds enquanto a atualização está rodando */
        .painel-progresso {
            width: var(--degrau2); margin: 0 auto 1.375rem;
            background: var(--vidro); border: 1px solid var(--vidro-borda); box-shadow: var(--vidro-sombra);
            border-radius: var(--raio); padding: 2.5rem 1.5rem; text-align: center;
        }
        .painel-progresso h3 {
            margin: 0 0 0.5rem 0; color: var(--texto);
            font-family: var(--f-corpo); font-weight: 300; font-size: 1.5rem; letter-spacing: -.02em;
        }
        .painel-progresso .sub { margin: 0 0 1.25rem 0; font-size: 0.75rem; color: var(--apagado); }
        .barra-progresso-fundo {
            width: 100%; max-width: 32.5rem; height: 0.625rem; background: rgba(1, 5, 15, .6);
            border: 1px solid var(--linha); border-radius: 999px;
            margin: 0 auto 0.75rem auto; overflow: hidden;
        }
        .barra-progresso-preenchida {
            height: 100%; width: 0%; border-radius: 999px;
            background: linear-gradient(90deg, var(--azul), var(--azul-claro));
            transition: width 0.6s ease;
        }
        /* Passou da estimativa: para de crescer e vira listrada animada -
           sinaliza "ainda rodando, só demorando mais que o previsto" sem
           mentir que está quase acabando nem travar em 100%. */
        .barra-progresso-preenchida.estourou {
            background: repeating-linear-gradient(45deg, var(--azul), var(--azul) 10px, var(--ouro) 10px, var(--ouro) 20px);
            background-size: 28px 28px;
            animation: desliza-listras 1s linear infinite;
        }
        @keyframes desliza-listras {
            from { background-position: 0 0; }
            to { background-position: 28px 0; }
        }
        .progresso-tempos {
            display: flex; justify-content: center; gap: 1.125rem; flex-wrap: wrap;
            font-size: 0.75rem; color: var(--apagado);
        }
        .progresso-tempos b { color: var(--texto); font-family: var(--f-dado); font-weight: 500; }

        /* ---------- listas: Odds Individuais | Múltiplas ---------- */
        .colunas-resultado {
            display: grid; grid-template-columns: 1fr 1fr; gap: 2rem; align-items: start;
            width: var(--degrau2); margin: 0 auto 4rem;
        }
        @media (max-width: 640px) {
            .colunas-resultado { grid-template-columns: 1fr; }
        }
        .coluna { min-width: 0; }
        /* títulos de seção: grandes, leves, à esquerda, sem caixa */
        .coluna-cabecalho {
            text-align: left; color: var(--texto);
            font-family: var(--f-corpo); font-weight: 300; font-size: clamp(1.125rem, 1.5vw, 1.4375rem);
            letter-spacing: -.025em; line-height: 1.1; margin: 0 0 1.125rem;
        }

        /* cartões: o que é a aposta vem primeiro; o resto, menor e em cinza */
        .cartao {
            background: var(--vidro);
            border: 1px solid var(--vidro-borda);
            box-shadow: var(--vidro-sombra);
            border-radius: 0.75rem;
            padding: 0.875rem 1rem;
            margin-bottom: 0.625rem;
            transition: border-color .15s;
        }
        .cartao:hover { border-color: rgba(180, 200, 242, .35); }
        .cartao-topo {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.5rem;
            flex-wrap: wrap;
            gap: 0.375rem;
        }
        .jogo { font-size: 0.6875rem; font-weight: 500; color: var(--apagado); flex: 1 1 12rem; min-width: 0; }
        .casa {
            background: rgba(63, 111, 209, .18);
            color: var(--azul-claro);
            border: 1px solid rgba(180, 200, 242, .25);
            font-family: var(--f-corpo);
            font-size: 0.6875rem;
            padding: 0.125rem 0.5rem;
            border-radius: 999px;
            text-transform: none;
            letter-spacing: 0;
        }
        .odd-tag {
            background: rgba(255, 210, 10, .1);
            color: var(--ouro);
            border: 1px solid rgba(255, 210, 10, .4);
            font-family: var(--f-dado);
            font-weight: 600;
            font-size: 0.8125rem;
            font-variant-numeric: tabular-nums;
            padding: 0.125rem 0.5rem;
            border-radius: 0.375rem;
        }
        .descricao { color: var(--texto); font-size: 0.875rem; font-weight: 500; line-height: 1.45; margin-bottom: 0.5rem; }
        .metricas { display: flex; gap: 1rem; font-size: 0.6875rem; color: var(--apagado); flex-wrap: wrap; }
        .metricas b { color: var(--texto); font-family: var(--f-dado); font-weight: 500; font-variant-numeric: tabular-nums; }
        .ja-apostado { color: var(--apagado); font-family: var(--f-dado); font-size: 0.6875rem; margin-top: 0.5rem; }
        .salvar-linha {
            display: flex;
            gap: 0.375rem;
            align-items: center;
            margin-top: 0.625rem;
            padding-top: 0.625rem;
            border-top: 1px solid var(--linha-fraca);
        }
        .salvar-linha input[type=number] { width: 6rem; font-size: 0.75rem; padding: 0.3125rem 0.5rem; }
        .btn-salvar {
            background: var(--azul);
            color: #fff;
            border: none;
            border-radius: 999px;
            padding: 0.375rem 0.875rem;
            font-size: 0.75rem;
            font-weight: 700;
            cursor: pointer;
        }
        .btn-salvar:hover { background: #5683dd; }
        /* escudo dos times ao lado do nome (só nas apostas individuais) */
        .escudo {
            width: 1rem; height: 1rem; object-fit: contain; display: inline-block;
            vertical-align: -0.1875rem; margin-right: 0.3125rem;
            filter: drop-shadow(0 1px 2px rgba(0,0,0,.45));
        }
        .paginacao {
            display: flex; align-items: center; justify-content: center; gap: 0.875rem;
            margin-top: 0.75rem; font-family: var(--f-dado); font-size: 0.6875rem; color: var(--apagado);
        }
        .btn-pagina {
            background: rgba(255,255,255,.05); border: 1px solid var(--linha); color: var(--texto);
            border-radius: 999px; padding: 0.3125rem 0.75rem; font-family: var(--f-corpo); font-size: 0.75rem; font-weight: 600; cursor: pointer;
        }
        .btn-pagina:hover:not(:disabled) { border-color: var(--azul-claro); background: rgba(180, 200, 242, .1); }
        .btn-pagina:disabled { opacity: 0.35; cursor: default; }

        /* ---------- NOVO: seção "Jogos disponíveis" - um card por
           confronto que expande mostrando as odds daquele jogo */
        .secao-jogos { width: var(--degrau2); margin: 0 auto; }
        .jogo-bloco {
            background: rgba(1, 5, 15, .35); border: 1px solid var(--vidro-borda); border-radius: 0.75rem;
            margin-bottom: 0.5rem; overflow: hidden;
        }
        .jogo-cabecalho {
            display: flex; align-items: center; justify-content: space-between; gap: 0.75rem;
            padding: 0.75rem 1rem; cursor: pointer; user-select: none;
            background: var(--vidro); transition: background 0.15s;
        }
        .jogo-cabecalho:hover { background: linear-gradient(140deg, rgba(255,255,255,.14), rgba(255,255,255,.05)), rgba(3, 12, 32, .55); }
        .jogo-titulo { font-family: var(--f-corpo); font-weight: 600; font-size: 0.875rem; color: var(--texto); }
        .jogo-titulo .escudo { width: 1.375rem; height: 1.375rem; vertical-align: -0.375rem; margin-right: 0.4375rem; }
        .jogo-titulo .x { color: var(--apagado); font-weight: 400; margin: 0 0.25rem; }
        .jogo-data { font-family: var(--f-dado); font-size: 0.6875rem; color: var(--apagado); margin-top: 0.125rem; }
        .jogo-contagem {
            background: rgba(63, 111, 209, .2); color: var(--azul-claro); border: 1px solid rgba(180, 200, 242, .35);
            border-radius: 999px; padding: 0.125rem 0.5rem; font-family: var(--f-dado); font-size: 0.625rem; white-space: nowrap;
        }
        /* seta desenhada em CSS (sem ícone): fica dourada com o jogo aberto */
        .jogo-seta {
            width: 0.5rem; height: 0.5rem; border-right: 2px solid var(--apagado); border-bottom: 2px solid var(--apagado);
            transform: translateY(-0.125rem) rotate(45deg); transition: transform 0.2s;
        }
        .jogo-bloco.aberto .jogo-seta { transform: translateY(0.125rem) rotate(-135deg); border-color: var(--azul-claro); }
        .jogo-bloco.aberto { border-color: rgba(180, 200, 242, .3); }
        .jogo-corpo { display: none; padding: 0.625rem 0.75rem 0.75rem; }
        .jogo-bloco.aberto .jogo-corpo { display: block; }
        .jogo-aviso-ocultas {
            font-size: 0.75rem; color: var(--apagado); text-align: center;
            padding: 0.5rem; border-top: 1px solid var(--linha-fraca); margin-top: 0.375rem;
        }

        /* ---------- sem recomendação ---------- */
        .vazio {
            text-align: center;
            color: var(--apagado);
            padding: 2.5rem 1.5rem;
            background: rgba(3, 12, 32, .4);
            border: 1px dashed var(--linha);
            border-radius: var(--raio);
            font-size: 0.8125rem;
            line-height: 1.6;
        }
        body > .vazio { width: var(--degrau2); margin: 0 auto; }
        .vazio-titulo {
            color: var(--texto); margin-bottom: 0.5rem;
            font-family: var(--f-corpo); font-weight: 300; font-size: 1.375rem; letter-spacing: -.02em;
        }

        /* ---------- telas menores ---------- */
        @media (max-width: 1099px) {
            /* sem escadinha; o menu desce para a 2ª linha, centralizado e
               do tamanho dos links (rola para o lado só se não couber) */
            :root { --degrau1: 100%; --degrau2: 100%; }
            .topo { grid-template-columns: 1fr auto; row-gap: 0.75rem; }
            .topo-nav {
                grid-column: 1 / -1; grid-row: 2;
                max-width: 100%; overflow-x: auto; scrollbar-width: none; justify-content: flex-start;
            }
            .topo-nav::-webkit-scrollbar { display: none; }
        }
        @media (max-width: 520px) {
            .painel { padding: 1rem; }
            .linha-filtro > div { flex: 1 1 calc(50% - 0.5rem); }
            .linha-filtro input[type=number] { width: 100%; }
            .linha-filtro button { flex: 1 1 100%; }
            .cartao { padding: 1rem; }
            .salvar-linha input[type=number] { flex: 1; width: auto; }
        }
        @media (max-width: 420px) {
            .topo-banca { display: none; }
        }
        @media (prefers-reduced-motion: reduce) {
            .barra-progresso-preenchida.estourou { animation: none; }
            * { transition: none !important; }
        }
    </style>
</head>
<body>
    <div class="fundo" aria-hidden="true"><canvas id="fundo-gl"></canvas></div>
    {{ nav_html|safe }}

    <script>
        // NOVO: o link "Gerador de Recomendações" da barra de navegação leva
        // pra "/" sem nenhum parâmetro de odd - sem isso, o site não sabia
        // que você já tinha feito uma busca, e voltava pro formulário vazio
        // (parecia que "reiniciava" a página, incluindo perder a paginação,
        // porque a lista de recomendações inteira desaparecia). Agora, se
        // não tem odd_min/odd_max na URL mas o navegador lembra da última
        // busca feita, redireciona sozinho pra ela.
        // O "&auto=1" só marca, pra referência/log, que foi ESSE
        // redirecionamento automático que trouxe você pra cá (não um
        // clique real no formulário). Quem realmente decide se dispara
        // atualização de odds é o token de uso único (token_busca, campo
        // oculto do formulário) - esse redirecionamento automático nunca
        // tem esse token, então nunca dispara nada, mesmo que o parâmetro
        // "auto=1" seja removido ou forjado na URL manualmente.
        if (!window.location.search.includes('odd_min')) {
            try {
                const oddMin = localStorage.getItem('ultima_busca_odd_min');
                const oddMax = localStorage.getItem('ultima_busca_odd_max');
                if (oddMin && oddMax) {
                    // NOVO (30/08/2026): restaura junto a faixa de
                    // probabilidade, se houver. Os parâmetros são
                    // adicionados só quando têm valor - assim uma busca
                    // sem filtro de probabilidade continua sem ele na URL.
                    const probMin = localStorage.getItem('ultima_busca_prob_min') || '';
                    const probMax = localStorage.getItem('ultima_busca_prob_max') || '';
                    let destino = '/?odd_min=' + encodeURIComponent(oddMin) + '&odd_max=' + encodeURIComponent(oddMax);
                    if (probMin) destino += '&prob_min=' + encodeURIComponent(probMin);
                    if (probMax) destino += '&prob_max=' + encodeURIComponent(probMax);
                    window.location.replace(destino + '&auto=1');
                }
            } catch (e) {}
        }
    </script>

    {% with mensagens = get_flashed_messages(with_categories=true) %}
        {% for categoria, texto in mensagens %}
        <div class="flash flash-{{ categoria }}">{{ texto }}</div>
        {% endfor %}
    {% endwith %}

    <div class="painel">
        <form method="GET" action="/">
            <!-- NOVO: token_busca de uso único - prova que essa submissão
                 veio de um clique de verdade nesse formulário, nessa
                 página específica. Sobrevive a "editar odd e clicar de
                 novo" (o token só é consumido no servidor, o campo
                 continua com o mesmo valor até a página recarregar), mas
                 NÃO sobrevive a um F5 puro na URL resultante, porque o
                 servidor já terá consumido/trocado o token na resposta
                 anterior. -->
            <input type="hidden" name="token_busca" value="{{ token_busca }}">
            <div class="linha-filtro">
                <div>
                    <label for="odd_min">Odd mínima</label>
                    <input type="number" step="0.01" min="1.01" name="odd_min" id="odd_min" value="{{ odd_min }}">
                </div>
                <div>
                    <label for="odd_max">Odd máxima</label>
                    <input type="number" step="0.01" min="1.01" name="odd_max" id="odd_max" value="{{ odd_max }}">
                </div>
                <!-- NOVO (30/08/2026): faixa de probabilidade histórica.
                     `placeholder` em vez de valor padrão de propósito -
                     campo em branco = sem filtro, então quem não usa não
                     é afetado. Filtra pela probabilidade COMBINADA da
                     aposta: uma múltipla com pernas de 95%/60%/88% é
                     julgada pelos ~50% do conjunto, não perna a perna. -->
                <div>
                    <label for="prob_min">Probabilidade mín. (%)</label>
                    <input type="number" step="0.01" min="0" max="100" name="prob_min" id="prob_min"
                           value="{{ prob_min }}" placeholder="qualquer">
                </div>
                <div>
                    <label for="prob_max">Probabilidade máx. (%)</label>
                    <input type="number" step="0.01" min="0" max="100" name="prob_max" id="prob_max"
                           value="{{ prob_max }}" placeholder="qualquer">
                </div>
                <button type="submit">Gerar recomendações da rodada</button>
                <!-- NOVO (29/08/2026): mesmo formulário, mesmos campos de
                     odd mínima/máxima - a única diferença é que este botão
                     envia `apenas_mostrar=1`, e o servidor usa isso pra
                     NÃO disparar atualização de odds nem regerar
                     recomendação: só relê o que já está gravado e refiltra
                     pela faixa. Serve pra trocar de faixa no meio de uma
                     investigação sem gastar cota da OddsPapi e sem mudar o
                     dado que está sendo investigado. -->
                <button type="submit" name="apenas_mostrar" value="1" class="btn-so-mostrar">Só mostrar (sem gerar)</button>
            </div>
            <p class="nota-espera">Se fizer mais de 1h desde a última atualização, pode demorar alguns segundos
                (o app aciona a busca de odds mais recentes antes de mostrar o resultado).
                <b>"Só mostrar"</b> nunca busca odds nem recalcula nada — exibe apenas o que já foi gerado antes.</p>
        </form>
        {% if ultima_atualizacao_odds %}
        <div class="linha-atualizacao">
            <span>Última atualização de odds gerada às <b>{{ ultima_atualizacao_odds }}</b></span>
            <a href="/?odd_min={{ odd_min }}&odd_max={{ odd_max }}&prob_min={{ prob_min }}&prob_max={{ prob_max }}&forcar=1&token_busca={{ token_busca }}" class="link-atualizar">Atualizar recomendações</a>
        </div>
        {% endif %}
    </div>

    {% macro escudo(nome) %}{% set eid = escudos.get(nome) if escudos else None %}{% if eid %}<img class="escudo" src="/escudo/{{ eid }}.png" alt="" loading="lazy">{% endif %}{% endmacro %}

    {% macro cartao_combo(c) %}
        <div class="cartao item-pagina">
            <div class="cartao-topo">
                <span class="jogo">
                    <!-- CORRIGIDO: antes mostrava só a data do PRIMEIRO jogo
                         da combinação (c.jogos[0].data_jogo) pra todos -
                         quando a múltipla cruza jogos diferentes (recurso
                         que já existe no projeto), isso fazia parecer que
                         os dois jogos aconteceram no mesmo dia, mesmo
                         quando não tinham nada a ver um com o outro. Agora
                         cada jogo mostra a PRÓPRIA data. -->
                    {% if c.pernas|length == 1 %}{% for j in c.jogos %}{{ j.data_jogo }} · {{ escudo(j.nosso_time) }}{{ j.nosso_time }} x {{ escudo(j.adversario) }}{{ j.adversario }}{% endfor %}{% else %}{% for j in c.jogos %}{{ j.data_jogo }} · {{ j.nosso_time }} x {{ j.adversario }}{% if not loop.last %} + {% endif %}{% endfor %}{% endif %}
                </span>
                <span class="casa">{{ c.casa_aposta }}</span>
                <span class="odd-tag">{{ c.odd_combinada }}</span>
            </div>
            <div class="descricao">{{ c.descricao }}</div>
            <div class="metricas">
                <span>Probabilidade histórica: <b>{{ c.probabilidade_combinada }}%</b></span>
                <span>Valor esperado: <b>{{ c.valor_esperado }}</b></span>
            </div>
            {% if c.ja_apostado %}
            <div class="ja-apostado">R$ {{ "%.2f"|format(c.ja_apostado) }} já apostado nessa odd</div>
            {% endif %}
            <form method="POST" action="/salvar-aposta" class="salvar-linha">
                <input type="hidden" name="descricao" value="{{ c.descricao }}">
                <input type="hidden" name="casa_aposta" value="{{ c.casa_aposta }}">
                <input type="hidden" name="odd_combinada" value="{{ c.odd_combinada }}">
                <input type="hidden" name="probabilidade_combinada" value="{{ c.probabilidade_combinada }}">
                <input type="hidden" name="pernas" value='{{ c.pernas_json }}'>
                <input type="hidden" name="voltar" value="/?odd_min={{ odd_min }}&odd_max={{ odd_max }}&prob_min={{ prob_min }}&prob_max={{ prob_max }}">
                <input type="number" step="0.01" min="0.01" name="valor_apostado" placeholder="Valor (R$)" required>
                <button type="submit" class="btn-salvar">Salvar</button>
            </form>
        </div>
    {% endmacro %}

    {% if buscou %}
        <script>
            try {
                localStorage.setItem('ultima_busca_odd_min', '{{ odd_min }}');
                localStorage.setItem('ultima_busca_odd_max', '{{ odd_max }}');
                // NOVO (30/08/2026): a faixa de probabilidade também é
                // lembrada. Guardada mesmo quando vazia, de propósito -
                // "sem filtro" é uma escolha tão válida quanto uma faixa,
                // e precisa ser restaurada como tal.
                localStorage.setItem('ultima_busca_prob_min', '{{ prob_min }}');
                localStorage.setItem('ultima_busca_prob_max', '{{ prob_max }}');
            } catch (e) {}
        </script>

        <!-- NOVO (acompanhamento em tempo real): enquanto a atualização
             roda, as listas antigas ficam ESCONDIDAS e esse painel aparece
             no lugar. Antes, a página mostrava o resultado velho como se
             fosse o novo, e a pessoa precisava clicar de novo depois de
             alguns minutos pra ver o de verdade.

             Aparece tanto logo após o clique quanto num F5 no meio do
             processo (o estado vem do banco, não de quem clicou). -->
        {% if atualizacao_rodando %}
        <div class="painel-progresso" id="painel-progresso">
            <h3>Atualizando odds e recalculando recomendações...</h3>
            <p class="sub">Buscando as odds mais recentes na casa e recalculando tudo.
                A página se atualiza sozinha quando terminar - não precisa clicar de novo.</p>
            <div class="barra-progresso-fundo">
                <div class="barra-progresso-preenchida" id="barra-progresso"></div>
            </div>
            <div class="progresso-tempos">
                <span>Tempo decorrido: <b id="tempo-decorrido">--</b></span>
                <span>Estimativa: <b id="tempo-estimado">--</b></span>
            </div>
        </div>
        {% endif %}

        {% if not atualizacao_rodando %}
        {% if individuais or multiplas %}
        <div class="colunas-resultado">
            <div class="coluna">
                <div class="coluna-cabecalho coluna-azul">Odds Individuais ({{ individuais|length }})</div>
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
                <div class="coluna-cabecalho coluna-roxa">Múltiplas ({{ multiplas|length }})</div>
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

        {% if jogos_disponiveis %}
        <div class="secao-jogos">
            <div class="coluna-cabecalho coluna-verde-jogos">Jogos disponíveis ({{ jogos_disponiveis|length }})</div>
            {% for jd in jogos_disponiveis %}
            <div class="jogo-bloco" id="jogo-bloco-{{ loop.index }}">
                <div class="jogo-cabecalho" onclick="alternarJogo({{ loop.index }})">
                    <div>
                        <div class="jogo-titulo">{{ escudo(jd.time_a) }}{{ jd.time_a }} <span class="x">x</span> {{ escudo(jd.time_b) }}{{ jd.time_b }}</div>
                        <div class="jogo-data">{{ jd.data_jogo }}</div>
                    </div>
                    <div style="display:flex; align-items:center; gap:12px;">
                        <span class="jogo-contagem">{{ jd.total_odds }} odd{{ 's' if jd.total_odds != 1 else '' }}</span>
                        <span class="jogo-seta" aria-hidden="true"></span>
                    </div>
                </div>
                <div class="jogo-corpo">
                    <div id="lista-jogo-{{ loop.index }}">
                        {% for c in jd.odds %}{{ cartao_combo(c) }}{% endfor %}
                    </div>
                    {% if jd.odds|length > 10 %}
                    <div class="paginacao">
                        <button class="btn-pagina" id="anterior-lista-jogo-{{ loop.index }}" onclick="mudarPagina('lista-jogo-{{ loop.index }}', -1)">← Anterior</button>
                        <span id="label-lista-jogo-{{ loop.index }}"></span>
                        <button class="btn-pagina" id="proximo-lista-jogo-{{ loop.index }}" onclick="mudarPagina('lista-jogo-{{ loop.index }}', 1)">Próxima →</button>
                    </div>
                    {% endif %}
                    {% if jd.ocultas %}
                    <div class="jogo-aviso-ocultas">
                        + {{ jd.ocultas }} odd(s) não exibida(s) - mostrando as {{ jd.odds|length }} de maior probabilidade
                    </div>
                    {% endif %}
                </div>
            </div>
            {% endfor %}
        </div>
        {% endif %}

        <script>
            const TAMANHO_PAGINA = 10;
            const paginaAtual = {};

            function salvarPaginaNoNavegador(listaId, pagina) {
                try { localStorage.setItem('pagina:' + window.location.pathname + ':' + listaId, String(pagina)); } catch (e) {}
            }
            function carregarPaginaDoNavegador(listaId) {
                try {
                    const v = localStorage.getItem('pagina:' + window.location.pathname + ':' + listaId);
                    return v !== null ? parseInt(v, 10) : 0;
                } catch (e) { return 0; }
            }

            function totalPaginas(listaId) {
                const n = document.querySelectorAll('#' + listaId + ' .item-pagina').length;
                return Math.max(1, Math.ceil(n / TAMANHO_PAGINA));
            }

            function renderizarPagina(listaId) {
                if (!(listaId in paginaAtual)) { paginaAtual[listaId] = carregarPaginaDoNavegador(listaId); }
                const total = totalPaginas(listaId);
                if (paginaAtual[listaId] > total - 1) paginaAtual[listaId] = total - 1;
                if (paginaAtual[listaId] < 0) paginaAtual[listaId] = 0;
                const pagina = paginaAtual[listaId];
                const itens = document.querySelectorAll('#' + listaId + ' .item-pagina');
                itens.forEach(function(item, i) {
                    const paginaDoItem = Math.floor(i / TAMANHO_PAGINA);
                    item.style.display = (paginaDoItem === pagina) ? '' : 'none';
                });
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
                salvarPaginaNoNavegador(listaId, pagina);
                renderizarPagina(listaId);
            }

            ['lista-individuais', 'lista-multiplas'].forEach(renderizarPagina);

            // NOVO: cada jogo da seção "Jogos disponíveis" tem a PRÓPRIA
            // lista paginada (lista-jogo-1, lista-jogo-2, ...), usando a
            // mesma infraestrutura de paginação das colunas de cima -
            // nenhuma função nova, só registrar os ids que existem na
            // página. A quantidade de jogos varia por rodada, então a
            // lista de ids é montada do próprio DOM.
            document.querySelectorAll('[id^="lista-jogo-"]').forEach(function(lista) {
                renderizarPagina(lista.id);
            });

            // NOVO: abre/fecha o card de um jogo na seção "Jogos
            // disponíveis". Vários podem ficar abertos ao mesmo tempo,
            // de propósito - facilita comparar dois jogos lado a lado.
            // O estado fica no localStorage pra sobreviver ao reload.
            function alternarJogo(indice) {
                const bloco = document.getElementById('jogo-bloco-' + indice);
                if (!bloco) return;
                bloco.classList.toggle('aberto');
                try {
                    const chave = 'jogo-aberto:' + window.location.pathname + ':' + indice;
                    if (bloco.classList.contains('aberto')) {
                        localStorage.setItem(chave, '1');
                    } else {
                        localStorage.removeItem(chave);
                    }
                } catch (e) {}
            }

            document.querySelectorAll('.jogo-bloco').forEach(function(bloco) {
                const indice = bloco.id.replace('jogo-bloco-', '');
                try {
                    if (localStorage.getItem('jogo-aberto:' + window.location.pathname + ':' + indice) === '1') {
                        bloco.classList.add('aberto');
                    }
                } catch (e) {}
            });
        </script>
        {% else %}
            <div class="vazio">
                <div class="vazio-titulo">Nenhuma recomendação disponível no momento</div>
                {{ motivo }}
            </div>
        {% endif %}
        {% endif %}{# fim de "not atualizacao_rodando" #}
    {% endif %}

    {% if atualizacao_rodando %}
    <script>
        // NOVO (acompanhamento em tempo real): pergunta ao servidor, a
        // cada 5 segundos, se a atualização já terminou. Quando terminar,
        // recarrega a página sozinha - é isso que elimina o "espera 5
        // minutos e clica de novo" que existia antes.
        //
        // O reload vai pra URL SEM token_busca de propósito: sem token, o
        // servidor não trata como clique real, então NÃO dispara uma nova
        // rodada de atualização. Só exibe o resultado que acabou de ficar
        // pronto. (Ver gerar_e_guardar_token_busca no app.)
        (function () {
            const INTERVALO_MS = 5000;
            const barra = document.getElementById('barra-progresso');
            const elDecorrido = document.getElementById('tempo-decorrido');
            const elEstimado = document.getElementById('tempo-estimado');

            // Contador local, pra barra andar de segundo em segundo mesmo
            // entre uma consulta e outra (senão ela pularia de 5 em 5s).
            let decorridos = {{ progresso_decorridos }};
            let estimativa = {{ progresso_estimativa }};

            function formatar(segundos) {
                segundos = Math.max(0, Math.round(segundos));
                const m = Math.floor(segundos / 60);
                const s = segundos % 60;
                return m + ':' + String(s).padStart(2, '0');
            }

            function desenhar() {
                if (elDecorrido) elDecorrido.textContent = formatar(decorridos);
                if (elEstimado) elEstimado.textContent = '~' + formatar(estimativa);
                if (!barra) return;
                if (estimativa > 0 && decorridos < estimativa) {
                    // Trava em 97% mesmo perto do fim: chegar a 100% e
                    // continuar rodando passa a impressão de travamento.
                    const pct = Math.min(97, (decorridos / estimativa) * 100);
                    barra.style.width = pct + '%';
                    barra.classList.remove('estourou');
                } else {
                    // Passou da estimativa - barra cheia e listrada, pra
                    // dizer "ainda rodando" sem fingir progresso.
                    barra.style.width = '100%';
                    barra.classList.add('estourou');
                }
            }

            desenhar();
            setInterval(function () { decorridos += 1; desenhar(); }, 1000);

            function recarregarComResultado() {
                const params = new URLSearchParams(window.location.search);
                params.delete('token_busca');   // não dispara nova atualização
                params.delete('forcar');        // idem
                params.set('auto', '1');
                window.location.replace(window.location.pathname + '?' + params.toString());
            }

            function consultarStatus() {
                fetch('/api/status-atualizacao', { cache: 'no-store' })
                    .then(function (r) { return r.json(); })
                    .then(function (dados) {
                        if (!dados.em_andamento) {
                            recarregarComResultado();
                            return;
                        }
                        // Resincroniza com o servidor - o contador local
                        // pode ter derivado (aba em segundo plano etc).
                        decorridos = dados.segundos_decorridos;
                        if (dados.estimativa_segundos) estimativa = dados.estimativa_segundos;
                        desenhar();
                    })
                    .catch(function () {
                        // Falha de rede não interrompe o acompanhamento -
                        // a próxima consulta tenta de novo.
                    });
            }

            setInterval(consultarStatus, INTERVALO_MS);
        })();
    </script>
    {% endif %}
    <script>
        // REDESIGN (15/10/2026): o degradê vivo da /inicio (paleta "b"):
        // azul-prússia bem escuro com manchas de ouro escuro que passeiam.
        // Desenhado em 28% da resolução (é um degradê, não precisa de mais)
        // e a no máximo ~24 quadros por segundo; para com a aba escondida.
        // Sem WebGL, ou com "reduzir movimento" no sistema, fica parado.
        (function fundoVivo() {
            const cv = document.getElementById("fundo-gl");
            if (!cv) return;
            let gl = null;
            try { gl = cv.getContext("webgl", { antialias: false, alpha: false, powerPreference: "low-power" }); } catch (e) {}
            if (!gl) { cv.remove(); return; }
            const vs = "attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}";
            const fs = "precision mediump float;uniform vec2 r;uniform float t;uniform vec3 u0,u1,u2,u3,u4,u5;" +
                "float h(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}" +
                "float n(vec2 p){vec2 i=floor(p),f=fract(p);vec2 u=f*f*(3.-2.*f);" +
                "return mix(mix(h(i),h(i+vec2(1,0)),u.x),mix(h(i+vec2(0,1)),h(i+vec2(1,1)),u.x),u.y);}" +
                "float fbm(vec2 p){float v=0.,a=.5;for(int k=0;k<4;k++){v+=a*n(p);p=p*2.02+vec2(1.7,9.2);a*=.5;}return v;}" +
                "vec3 pal(float x){x=clamp(x,0.,1.);" +
                "if(x<.3)return mix(u0,u1,x/.3);if(x<.58)return mix(u1,u2,(x-.3)/.28);" +
                "if(x<.82)return mix(u2,u3,(x-.58)/.24);return mix(u3,u4,(x-.82)/.18);}" +
                "void main(){vec2 uv=gl_FragCoord.xy/r;vec2 q=uv*vec2(r.x/r.y,1.);float s=t*.11;" +
                "vec2 w=vec2(fbm(q*1.5+vec2(s,-s*.7)),fbm(q*1.5+vec2(-s*.8,s*.6)+3.1));" +
                "float m=fbm(q*1.1+w*2.+vec2(s*.6,s*.35));float g=uv.x*.5+(1.-uv.y)*.62-.12;g=g*.8+(m-.5)*.85;" +
                "vec3 col=pal(g);float m2=fbm(q*1.8-w*1.4+vec2(-s*.55,s*.45)+7.3);" +
                "float zona=smoothstep(.15,1.05,uv.x*.7+(1.-uv.y)*.55);" +
                "col=mix(col,u5,smoothstep(.48,.86,m2)*zona*.85);gl_FragColor=vec4(col,1.);}";
            const sh = (tipo, src) => { const s = gl.createShader(tipo); gl.shaderSource(s, src); gl.compileShader(s); return s; };
            const pr = gl.createProgram();
            gl.attachShader(pr, sh(gl.VERTEX_SHADER, vs)); gl.attachShader(pr, sh(gl.FRAGMENT_SHADER, fs)); gl.linkProgram(pr);
            if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) { cv.remove(); return; }
            gl.useProgram(pr);
            const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b);
            gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
            const lp = gl.getAttribLocation(pr, "p"); gl.enableVertexAttribArray(lp); gl.vertexAttribPointer(lp, 2, gl.FLOAT, false, 0, 0);
            const ur = gl.getUniformLocation(pr, "r"), ut = gl.getUniformLocation(pr, "t");
            const CORES = [[.004, .012, .04], [.01, .05, .14], [.03, .12, .3], [.07, .22, .48], [.13, .32, .6], [.42, .3, .02]];
            CORES.forEach((c, i) => gl.uniform3f(gl.getUniformLocation(pr, "u" + i), c[0], c[1], c[2]));
            const reduz = matchMedia("(prefers-reduced-motion: reduce)").matches;
            const ESCALA = .28;
            const desenha = s => { gl.uniform1f(ut, s); gl.drawArrays(gl.TRIANGLES, 0, 3); };
            function medir() {
                cv.width = Math.max(2, Math.round(innerWidth * ESCALA)); cv.height = Math.max(2, Math.round(innerHeight * ESCALA));
                gl.viewport(0, 0, cv.width, cv.height); gl.uniform2f(ur, cv.width, cv.height);
            }
            medir(); addEventListener("resize", () => { medir(); if (reduz) desenha(40); });
            const t0 = performance.now(); let ult = 0;
            function quadro(agora) {
                if (!document.hidden && agora - ult > 41) { ult = agora; desenha((agora - t0) / 1000 + 40); }
                requestAnimationFrame(quadro);
            }
            if (reduz) desenha(40); else requestAnimationFrame(quadro);
        })();
    </script>
</body>
</html>
"""


# REDESIGN (16/10/2026): pedaços do visual novo compartilhados pelas páginas
# redesenhadas depois da de Recomendações. São cópias exatas do que está no
# PAGINA (tokens/fundo, topo/ligas, script do fundo vivo); o PAGINA não foi
# mexido, para não arriscar a página que já está no ar.
CSS_VISUAL_NOVO = """
        :root {
            --noite: #01050f;
            --linha: rgba(189, 191, 199, .22);
            --linha-fraca: rgba(189, 191, 199, .12);
            --texto: #f1f2f6;
            --apagado: #aeb3c3;
            --azul: #3f6fd1;
            --azul-claro: #b4c8f2;
            --ouro: #ffd20a;
            --ouro-claro: #ffe066;
            --cinza: #bdbfc7;
            --cta-texto: #1d1600;
            --verde: #5fd38d;
            --vermelho: #ff7a7a;
            --f-titulo: "Unbounded", "Arial Black", system-ui, sans-serif;
            --f-corpo: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
            --f-dado: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
            --raio: 0.875rem;
            --margem: clamp(1rem, 2.5vw, 3rem);
            --degrau1: 90%;
            --degrau2: 80%;
            /* vidro sem backdrop-filter: sobre o degradê (já desfocado) fica
               igual e não custa quadro (ver /inicio) */
            --vidro: linear-gradient(140deg, rgba(255,255,255,.13), rgba(255,255,255,.035) 55%, rgba(255,255,255,.07)), rgba(4, 14, 38, .42);
            --vidro-borda: rgba(255, 255, 255, .17);
            --vidro-sombra: inset 0 1px 0 rgba(255,255,255,.25), inset 0 -1px 0 rgba(255,255,255,.04), 0 0.875rem 2.75rem -1rem rgba(0,0,0,.6);
        }
        * { box-sizing: border-box; }
        *::before, *::after { box-sizing: border-box; }
        html {
            font-size: clamp(14px, calc(100vw / 112), 18px);
            background: var(--noite);
        }
        body {
            font-family: var(--f-corpo);
            font-size: 0.9375rem;
            line-height: 1.5;
            background: transparent;
            color: var(--texto);
            width: calc(100% - 2 * var(--margem));
            margin: 0 auto;
            padding: 1rem 0 6rem;
        }
        a { color: var(--azul-claro); }
        ::selection { background: rgba(180, 200, 242, .35); }
        button:focus-visible, a:focus-visible { outline: 2px solid var(--azul-claro); outline-offset: 2px; }

        /* ---------- fundo: o degradê vivo da /inicio ----------
           WebGL a ~24 quadros por segundo, em resolução baixa (script no
           fim da página). Sem WebGL, fica o degradê parado abaixo. */
        .fundo {
            position: fixed; inset: 0; z-index: -1; overflow: hidden; pointer-events: none;
            background:
                radial-gradient(60vmax 45vmax at 8% -6%, rgba(63, 111, 209, .30), rgba(63, 111, 209, 0) 70%),
                radial-gradient(55vmax 40vmax at 104% 18%, rgba(92, 74, 0, .55), rgba(92, 74, 0, 0) 70%),
                radial-gradient(70vmax 50vmax at 70% 112%, rgba(6, 32, 79, .85), rgba(6, 32, 79, 0) 70%),
                linear-gradient(180deg, #020a1f 0%, var(--noite) 60%);
        }
        #fundo-gl { position: absolute; inset: 0; width: 100%; height: 100%; display: block; }

        /* ---------- topo: nome | links no centro | banca e Conta ---------- */
        .topo {
            display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 1rem;
            margin: 0 0 1.25rem;
        }
        .topo-marca {
            justify-self: start;
            font-family: var(--f-titulo); font-weight: 700; font-size: 0.875rem; letter-spacing: -.01em;
            color: var(--texto); text-decoration: none; white-space: nowrap;
        }
        /* 16/10/2026: "Zero" vira o logo desenhado pelo usuário - o desenho
           recorta um degradê ouro -> azul (mesmas cores do brilho da /inicio) */
        .logo-zero {
            display: inline-block; height: 0.86em; aspect-ratio: 1928.53 / 369.92;
            vertical-align: -0.04em; margin-left: 0.12em;
            background: linear-gradient(100deg, #ffd20a 0%, #fff3b0 35%, #bdbfc7 65%, #7fa2ea 100%);
            -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='3.47 307.06 1928.53 369.92'%3E%3Cg fill='%23000'%3E%3Cpath d='M563.46,314.21l-293.13,266.25c-5.76,5.23-2.06,14.81,5.72,14.81h250.01c8.12,0,11.63,10.28,5.2,15.25l-82.69,63.89c-1.49,1.15-3.32,1.78-5.2,1.78H22c-7.78,0-11.48-9.57-5.73-14.81l284.32-258.81c5.75-5.24,2.05-14.81-5.73-14.81h-163.65c-7.78,0-11.48-9.58-5.72-14.81l68.31-62.07c1.56-1.42,3.59-2.21,5.7-2.21l361.38,-1.13c5.48,-0.02,6.64,2.98,2.59,6.66Z' transform='translate(-10 0)'/%3E%3Cpath d='M238.26,652.63l380.83-341.3c2.43-2.18,5.58-3.38,8.84-3.38h279.74c12.16,0,17.89,15,8.84,23.11l-43.83,39.28c-2.43,2.18-5.58,3.38-8.84,3.38h-206.71c-3.3,0-6.48,1.23-8.92,3.46l-48.78,44.47c-8.98,8.19-3.12,23.14,9.03,23.04l146.45-1.19c12.11-.1,17.99,14.76,9.1,22.97l-40.54,37.49c-2.45,2.26-5.66,3.52-8.99,3.52h-205.62c-3.28,0-6.44,1.22-8.88,3.42l-47.32,42.73c-9,8.13-3.25,23.08,8.88,23.08h146.16c12.15,0,17.89,14.99,8.85,23.11l-80.85,72.54c-2.43,2.18-5.58,3.39-8.85,3.39H247.1c-12.16,0-17.89-15-8.84-23.11Z'/%3E%3Cpath d='M1901.71,307.06l-404.11.29c-3.96,0-10.53,5.28-15.79,10.78l-362.08,326.91c-14.03,11.22-9.92,31.62,7.63,31.58l362.77-.81c3.95,0,52.41,2.59,55.57.07l366.99-333.87c14-11.22,6.55-34.95-10.98-34.95ZM1710.9,358.2h43.17l-496.68,217.3,126.27-101.01c94.36-75.48,209.2-116.29,327.24-116.29ZM1790.45,393.15l-144.2,115.57c-94.12,75.43-208.68,116.35-326.49,116.61l-21.56.05,495.02-234.95c-.81.96-1.72,1.88-2.77,2.72Z' transform='translate(12 0)'/%3E%3Cpath d='M1239.16,507.79l202.79-183.3c5.68-5.29,4.71-17.42-2.83-17.42l-447.04,1.67c-1.94,0-3.82.74-5.3,2.08l-384.98,350.66c-5.77,5.25-2.29,15.33,5.3,15.33h115.11c1.94,0,3.81-.74,5.29-2.08l178.91-162.72c1.48-1.34,3.35-2.08,5.29-2.08h66.31c2.44,0,4.75,1.17,6.3,3.18l123.71,160.52c1.55,2.01,3.86,3.18,6.3,3.18h128.81c7.12,0,10.82-9.05,5.99-14.63l-132-152.25h116.68c1.97,0,3.88-.76,5.36-2.14ZM977.69,423.26l43.55-46.49c1.53-1.63,3.61-2.55,5.77-2.55h207.61c7.45,0,11,9.79,5.49,15.15l-47.85,46.49c-1.5,1.46-3.46,2.27-5.49,2.27h-203.31c-7.27,0-10.91-9.38-5.77-14.87Z'/%3E%3C/g%3E%3C/svg%3E") no-repeat center / contain;
            mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='3.47 307.06 1928.53 369.92'%3E%3Cg fill='%23000'%3E%3Cpath d='M563.46,314.21l-293.13,266.25c-5.76,5.23-2.06,14.81,5.72,14.81h250.01c8.12,0,11.63,10.28,5.2,15.25l-82.69,63.89c-1.49,1.15-3.32,1.78-5.2,1.78H22c-7.78,0-11.48-9.57-5.73-14.81l284.32-258.81c5.75-5.24,2.05-14.81-5.73-14.81h-163.65c-7.78,0-11.48-9.58-5.72-14.81l68.31-62.07c1.56-1.42,3.59-2.21,5.7-2.21l361.38,-1.13c5.48,-0.02,6.64,2.98,2.59,6.66Z' transform='translate(-10 0)'/%3E%3Cpath d='M238.26,652.63l380.83-341.3c2.43-2.18,5.58-3.38,8.84-3.38h279.74c12.16,0,17.89,15,8.84,23.11l-43.83,39.28c-2.43,2.18-5.58,3.38-8.84,3.38h-206.71c-3.3,0-6.48,1.23-8.92,3.46l-48.78,44.47c-8.98,8.19-3.12,23.14,9.03,23.04l146.45-1.19c12.11-.1,17.99,14.76,9.1,22.97l-40.54,37.49c-2.45,2.26-5.66,3.52-8.99,3.52h-205.62c-3.28,0-6.44,1.22-8.88,3.42l-47.32,42.73c-9,8.13-3.25,23.08,8.88,23.08h146.16c12.15,0,17.89,14.99,8.85,23.11l-80.85,72.54c-2.43,2.18-5.58,3.39-8.85,3.39H247.1c-12.16,0-17.89-15-8.84-23.11Z'/%3E%3Cpath d='M1901.71,307.06l-404.11.29c-3.96,0-10.53,5.28-15.79,10.78l-362.08,326.91c-14.03,11.22-9.92,31.62,7.63,31.58l362.77-.81c3.95,0,52.41,2.59,55.57.07l366.99-333.87c14-11.22,6.55-34.95-10.98-34.95ZM1710.9,358.2h43.17l-496.68,217.3,126.27-101.01c94.36-75.48,209.2-116.29,327.24-116.29ZM1790.45,393.15l-144.2,115.57c-94.12,75.43-208.68,116.35-326.49,116.61l-21.56.05,495.02-234.95c-.81.96-1.72,1.88-2.77,2.72Z' transform='translate(12 0)'/%3E%3Cpath d='M1239.16,507.79l202.79-183.3c5.68-5.29,4.71-17.42-2.83-17.42l-447.04,1.67c-1.94,0-3.82.74-5.3,2.08l-384.98,350.66c-5.77,5.25-2.29,15.33,5.3,15.33h115.11c1.94,0,3.81-.74,5.29-2.08l178.91-162.72c1.48-1.34,3.35-2.08,5.29-2.08h66.31c2.44,0,4.75,1.17,6.3,3.18l123.71,160.52c1.55,2.01,3.86,3.18,6.3,3.18h128.81c7.12,0,10.82-9.05,5.99-14.63l-132-152.25h116.68c1.97,0,3.88-.76,5.36-2.14ZM977.69,423.26l43.55-46.49c1.53-1.63,3.61-2.55,5.77-2.55h207.61c7.45,0,11,9.79,5.49,15.15l-47.85,46.49c-1.5,1.46-3.46,2.27-5.49,2.27h-203.31c-7.27,0-10.91-9.38-5.77-14.87Z'/%3E%3C/g%3E%3C/svg%3E") no-repeat center / contain;
        }
        .topo-nav {
            justify-self: center;
            display: flex; align-items: center; gap: 0.125rem; padding: 0.25rem;
            border-radius: 999px; min-width: 0;
            background: var(--vidro); border: 1px solid var(--vidro-borda); box-shadow: var(--vidro-sombra);
        }
        .topo-link {
            padding: 0.375rem 0.75rem; border-radius: 999px; white-space: nowrap;
            color: var(--apagado); font-size: 0.8125rem; font-weight: 500; text-decoration: none;
            transition: color .15s, background .15s;
        }
        .topo-link:hover { color: var(--texto); background: rgba(255, 255, 255, .06); }
        .topo-link.topo-ativo { color: var(--texto); background: rgba(255, 255, 255, .1); }
        .topo-direita { justify-self: end; display: flex; align-items: center; gap: 0.5rem; flex-shrink: 0; }
        .topo-banca {
            font-family: var(--f-dado); font-size: 0.6875rem; color: var(--texto); text-decoration: none; white-space: nowrap;
            padding: 0.375rem 0.625rem; border-radius: 999px; border: 1px solid var(--linha);
        }
        .topo-banca:hover { border-color: var(--azul-claro); }
        .topo-conta {
            padding: 0.4375rem 0.9375rem; border-radius: 999px; white-space: nowrap;
            color: var(--texto); font-size: 0.8125rem; font-weight: 600; text-decoration: none;
            background: var(--vidro); border: 1px solid var(--vidro-borda); box-shadow: var(--vidro-sombra);
            transition: border-color .15s;
        }
        .topo-conta:hover { border-color: var(--azul-claro); }

        /* ---------- ligas ---------- */
        .nav-ligas { max-width: none; width: var(--degrau1); gap: 0.5rem; margin: 0 auto 3rem; }
        .liga-card {
            background: var(--vidro); border: 1px solid var(--vidro-borda); border-radius: 0.625rem;
            padding: 0.4375rem 0.5625rem; color: var(--cinza); gap: 0.5rem;
        }
        .liga-card:hover { border-color: var(--azul-claro); background: rgba(180, 200, 242, .08); }
        .liga-card.liga-ativa { background: rgba(63, 111, 209, .22); border-color: rgba(180, 200, 242, .6); color: var(--texto); }
        /* logo da liga num selo de vidro (mesmo selo da /inicio), sem fundo branco */
        .liga-selo-logo {
            width: 1.625rem; height: 1.625rem; flex-shrink: 0; border-radius: 0.5rem;
            display: grid; place-items: center;
            background: linear-gradient(140deg, rgba(255,255,255,.34), rgba(255,255,255,.16));
            border: 1px solid rgba(255,255,255,.28); box-shadow: inset 0 1px 0 rgba(255,255,255,.35);
        }
        .liga-selo-logo img {
            width: 1.125rem; height: 1.125rem; object-fit: contain; display: block;
            filter: drop-shadow(0 0 1px rgba(255,255,255,.55)) drop-shadow(0 1px 3px rgba(0,0,0,.45));
        }
        .liga-nome { font-size: 0.75rem; font-weight: 600; }
        .liga-selo { font-family: var(--f-corpo); font-size: 0.6875rem; letter-spacing: 0; text-transform: none; color: var(--apagado); font-weight: 500; }
        .liga-selo-ok { color: var(--azul-claro); }

        @media (max-width: 1099px) {
            .topo { grid-template-columns: 1fr auto; row-gap: 0.75rem; }
            .topo-nav {
                grid-column: 1 / -1; grid-row: 2;
                max-width: 100%; overflow-x: auto; scrollbar-width: none; justify-content: flex-start;
            }
            .topo-nav::-webkit-scrollbar { display: none; }
        }
        @media (max-width: 420px) { .topo-banca { display: none; } }
"""

FONTES_VISUAL_NOVO = """    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Unbounded:wght@500;700&family=IBM+Plex+Sans:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
"""

FUNDO_VISUAL_NOVO = """    <div class="fundo" aria-hidden="true"><canvas id="fundo-gl"></canvas></div>
"""

SCRIPT_FUNDO_VIVO = """    <script>
        // REDESIGN (15/10/2026): o degradê vivo da /inicio (paleta "b"):
        // azul-prússia bem escuro com manchas de ouro escuro que passeiam.
        // Desenhado em 28% da resolução (é um degradê, não precisa de mais)
        // e a no máximo ~24 quadros por segundo; para com a aba escondida.
        // Sem WebGL, ou com "reduzir movimento" no sistema, fica parado.
        (function fundoVivo() {
            const cv = document.getElementById("fundo-gl");
            if (!cv) return;
            let gl = null;
            try { gl = cv.getContext("webgl", { antialias: false, alpha: false, powerPreference: "low-power" }); } catch (e) {}
            if (!gl) { cv.remove(); return; }
            const vs = "attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}";
            const fs = "precision mediump float;uniform vec2 r;uniform float t;uniform vec3 u0,u1,u2,u3,u4,u5;" +
                "float h(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}" +
                "float n(vec2 p){vec2 i=floor(p),f=fract(p);vec2 u=f*f*(3.-2.*f);" +
                "return mix(mix(h(i),h(i+vec2(1,0)),u.x),mix(h(i+vec2(0,1)),h(i+vec2(1,1)),u.x),u.y);}" +
                "float fbm(vec2 p){float v=0.,a=.5;for(int k=0;k<4;k++){v+=a*n(p);p=p*2.02+vec2(1.7,9.2);a*=.5;}return v;}" +
                "vec3 pal(float x){x=clamp(x,0.,1.);" +
                "if(x<.3)return mix(u0,u1,x/.3);if(x<.58)return mix(u1,u2,(x-.3)/.28);" +
                "if(x<.82)return mix(u2,u3,(x-.58)/.24);return mix(u3,u4,(x-.82)/.18);}" +
                "void main(){vec2 uv=gl_FragCoord.xy/r;vec2 q=uv*vec2(r.x/r.y,1.);float s=t*.11;" +
                "vec2 w=vec2(fbm(q*1.5+vec2(s,-s*.7)),fbm(q*1.5+vec2(-s*.8,s*.6)+3.1));" +
                "float m=fbm(q*1.1+w*2.+vec2(s*.6,s*.35));float g=uv.x*.5+(1.-uv.y)*.62-.12;g=g*.8+(m-.5)*.85;" +
                "vec3 col=pal(g);float m2=fbm(q*1.8-w*1.4+vec2(-s*.55,s*.45)+7.3);" +
                "float zona=smoothstep(.15,1.05,uv.x*.7+(1.-uv.y)*.55);" +
                "col=mix(col,u5,smoothstep(.48,.86,m2)*zona*.85);gl_FragColor=vec4(col,1.);}";
            const sh = (tipo, src) => { const s = gl.createShader(tipo); gl.shaderSource(s, src); gl.compileShader(s); return s; };
            const pr = gl.createProgram();
            gl.attachShader(pr, sh(gl.VERTEX_SHADER, vs)); gl.attachShader(pr, sh(gl.FRAGMENT_SHADER, fs)); gl.linkProgram(pr);
            if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) { cv.remove(); return; }
            gl.useProgram(pr);
            const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b);
            gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
            const lp = gl.getAttribLocation(pr, "p"); gl.enableVertexAttribArray(lp); gl.vertexAttribPointer(lp, 2, gl.FLOAT, false, 0, 0);
            const ur = gl.getUniformLocation(pr, "r"), ut = gl.getUniformLocation(pr, "t");
            const CORES = window.CORES_FUNDO || [[.004, .012, .04], [.01, .05, .14], [.03, .12, .3], [.07, .22, .48], [.13, .32, .6], [.42, .3, .02]];
            const poeCores = cs => cs.forEach((c, i) => gl.uniform3f(gl.getUniformLocation(pr, "u" + i), c[0], c[1], c[2]));
            poeCores(CORES);
            const reduz = matchMedia("(prefers-reduced-motion: reduce)").matches;
            const ESCALA = .28;
            const desenha = s => { gl.uniform1f(ut, s); gl.drawArrays(gl.TRIANGLES, 0, 3); };
            const t0 = performance.now();
            window.trocarCoresFundo = cs => { poeCores(cs); desenha((performance.now() - t0) / 1000 + 40); };
            function medir() {
                cv.width = Math.max(2, Math.round(innerWidth * ESCALA)); cv.height = Math.max(2, Math.round(innerHeight * ESCALA));
                gl.viewport(0, 0, cv.width, cv.height); gl.uniform2f(ur, cv.width, cv.height);
            }
            medir(); addEventListener("resize", () => { medir(); if (reduz) desenha(40); });
            let ult = 0;
            function quadro(agora) {
                if (!document.hidden && agora - ult > 41) { ult = agora; desenha((agora - t0) / 1000 + 40); }
                requestAnimationFrame(quadro);
            }
            if (reduz) desenha(40); else requestAnimationFrame(quadro);
        })();
    </script>
"""


from combinacoes import (
    MERCADOS_JOGO_INTEIRO, deduplicar_mercados_jogo_inteiro, buscar_recomendacoes,
    LARGURA_MINIMA_FAIXA, LIMITE_POOL_PARA_5_PERNAS, LIMITE_POOL_PARA_4_PERNAS,
    MAX_MULTIPLAS_RESULTADO, identidade_jogo, combo_tem_conflito_de_time_mesma_data,
    montar_combinacoes, capturar_candidatas_multiplas,
)
# NOVO: a lógica de montar múltiplas foi extraída pro módulo combinacoes.py,
# compartilhado com motor_combinacoes.py (cron) - existir em dois lugares
# diferentes já causou bug grande no passado (funções duplicadas divergindo aos
# poucos), então agora essa é a ÚNICA fonte de verdade dessa lógica.

from tabela import calcular_jogo_morto, calcular_contexto_jogo
# NOVO (Fase D): mesmo módulo compartilhado da Fase A/C - "jogo morto" e o
# contexto de tabela de um confronto são calculados aqui, reaproveitando a
# tabela reconstruída (jogos_liga), sem duplicar a lógica de reconstrução.

from avaliacao import avaliar_resultado
# NOVO (Criador de Odd): a lógica de avaliação (comparar aposta com
# resultado real) foi extraída pro módulo avaliacao.py, compartilhado com
# arquivar_recomendacoes.py - usada tanto pras recomendações automáticas
# quanto pras apostas manuais (Criador de Odd), cobrindo os 10 tipos de
# mercado, com a mesma correção de bug pros dois (jogador que ficou no
# banco sem entrar não fica mais "pendente" pra sempre).


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
    <link rel="icon" href="/static/marca/favicon.svg" type="image/svg+xml">
    <link rel="icon" href="/static/marca/favicon-32.png" sizes="32x32" type="image/png">
    <link rel="apple-touch-icon" href="/static/marca/apple-touch-icon.png">
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Histórico - Achismo Zero</title>
""" + FONTES_VISUAL_NOVO + """    <style>
        /* REDESIGN (18/10/2026): Histórico no visual novo. Dados e rotas são
           os de antes (mesmas listas, mesma paginação de 10, mesmos CSV por
           rodada, mesmo "carregar mais rodadas" e as mesmas tabelas por
           faixa de /api/calibracao e /api/calibracao-odd). O que muda é a
           forma: as duas calibrações viraram barras "prometido x
           acontecido" na própria página, e a tabela de cada faixa abre numa
           janela só. */
        """ + NAV_CSS + """
""" + CSS_VISUAL_NOVO + """
        .conteudo { width: var(--degrau1); margin: 0 auto; }
        .rotulo { font-family: var(--f-corpo); font-size: 0.625rem; letter-spacing: 0; text-transform: none; color: var(--apagado); }
        .vidro { background: var(--vidro); border: 1px solid var(--vidro-borda); box-shadow: var(--vidro-sombra); border-radius: var(--raio); }
        .num { font-family: var(--f-dado); font-variant-numeric: tabular-nums; }
        .secao { margin-bottom: 3.5rem; }
        .secao-titulo {
            margin: 0 0 0.5rem; font-family: var(--f-corpo); font-weight: 300;
            font-size: clamp(1.25rem, 1.8vw, 1.75rem); letter-spacing: -.03em; line-height: 1.1;
        }
        .nota { color: var(--apagado); font-size: 0.75rem; line-height: 1.6; margin: 0 0 1.25rem; max-width: 56rem; }
        .nota b { color: var(--texto); font-weight: 600; }
        .vazio {
            text-align: center; color: var(--apagado); padding: 1.75rem 1.25rem; font-size: 0.8125rem;
            background: rgba(3, 12, 32, .4); border: 1px dashed var(--linha); border-radius: var(--raio);
        }
        .escudo { width: 1rem; height: 1rem; object-fit: contain; vertical-align: -0.1875rem; margin-right: 0.25rem; filter: drop-shadow(0 1px 2px rgba(0,0,0,.45)); }
        button {
            background: var(--ouro); color: var(--cta-texto); border: 1px solid transparent; border-radius: 999px;
            padding: 0.5rem 1rem; font-family: var(--f-corpo); font-size: 0.8125rem; font-weight: 600; cursor: pointer;
        }

        /* ---------- cabeçalho + placar ---------- */
        .cabeca { margin: 0.5rem 0 1.75rem; }
        .cabeca h1 { margin: 0; font-family: var(--f-corpo); font-weight: 300; font-size: clamp(2rem, 3.2vw, 3rem); letter-spacing: -.035em; line-height: 1; }
        .cabeca p { margin: 0.5rem 0 0; color: var(--apagado); font-size: 0.8125rem; }
        .placar { padding: 1.25rem 1.5rem 1.375rem; margin-bottom: 3.5rem; }
        .placar-numeros { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1rem; }
        .placar-numeros b { display: block; font-family: var(--f-dado); font-weight: 500; font-size: clamp(1.75rem, 2.6vw, 2.5rem); line-height: 1; margin-top: 0.375rem; font-variant-numeric: tabular-nums; }
        .placar .ouro b { color: var(--ouro); }
        .placar .azul b { color: var(--azul-claro); }
        .placar-barra { display: flex; height: 0.5rem; border-radius: 999px; overflow: hidden; margin-top: 1.25rem; background: rgba(255,255,255,.06); }
        .placar-barra i { display: block; height: 100%; }
        .placar-barra .b-ac { background: var(--ouro); }
        .placar-barra .b-er { background: rgba(180, 200, 242, .45); }

        /* ---------- calibração: prometido x acontecido ---------- */
        .calib { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 1.25rem; align-items: start; }
        .calib-painel { padding: 1.125rem 1.25rem 1.25rem; min-width: 0; }
        .calib-painel h3 { margin: 0 0 0.25rem; font-size: 0.9375rem; font-weight: 600; }
        .calib-destaque { font-size: 0.75rem; color: var(--apagado); margin: 0 0 1rem; line-height: 1.5; }
        .calib-destaque b { color: var(--ouro); font-weight: 600; }
        .legenda { display: flex; gap: 1rem; flex-wrap: wrap; font-size: 0.625rem; color: var(--apagado); margin-bottom: 0.75rem; }
        .legenda span { display: inline-flex; align-items: center; gap: 0.375rem; }
        .leg-fx { width: 1rem; height: 0.5rem; border-radius: 0.125rem; background: rgba(180, 200, 242, .14); border: 1px dashed rgba(180, 200, 242, .5); }
        .leg-real { width: 1rem; height: 0.375rem; border-radius: 999px; background: var(--azul); }
        .leg-marca { width: 2px; height: 0.75rem; background: var(--azul-claro); }
        .calib-linhas { display: grid; gap: 0.125rem; }
        .calib-rolagem { max-height: 26rem; overflow-y: auto; padding-right: 0.375rem; scrollbar-width: thin; }
        .faixa {
            width: 100%; display: grid; grid-template-columns: 5.5rem minmax(0, 1fr) 7.25rem; gap: 0.75rem; align-items: center;
            background: none; border: 0; border-radius: 0.5rem; padding: 0.4375rem 0.5rem; color: var(--texto);
            font-family: var(--f-corpo); font-weight: 400; text-align: left; cursor: pointer; transition: background .15s;
        }
        .faixa:hover { background: rgba(255,255,255,.05); }
        .faixa:focus-visible { outline: 2px solid var(--azul-claro); outline-offset: 1px; }
        .faixa-rot { font-family: var(--f-dado); font-size: 0.6875rem; color: var(--apagado); white-space: nowrap; }
        .trilho { position: relative; height: 0.875rem; border-radius: 0.25rem; background: rgba(255,255,255,.04); }
        .trilho .fx { position: absolute; top: 0; bottom: 0; background: rgba(180, 200, 242, .12); border-left: 1px dashed rgba(180, 200, 242, .5); border-right: 1px dashed rgba(180, 200, 242, .5); }
        .trilho .marca { position: absolute; top: -0.1875rem; bottom: -0.1875rem; width: 2px; margin-left: -1px; background: var(--azul-claro); }
        .trilho .real { position: absolute; left: 0; top: 0.25rem; height: 0.375rem; border-radius: 999px; background: var(--azul); }
        .trilho .real.bom { background: var(--ouro); }
        .faixa-num { text-align: right; font-size: 0.6875rem; color: var(--apagado); white-space: nowrap; }
        .faixa-num b { font-family: var(--f-dado); font-weight: 600; font-size: 0.8125rem; color: var(--texto); margin-right: 0.25rem; }

        /* ---------- colunas acertou / errou ---------- */
        .colunas { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 1.5rem; align-items: start; }
        .coluna { min-width: 0; }
        .coluna-topo { display: flex; align-items: baseline; justify-content: space-between; gap: 0.75rem; margin-bottom: 0.875rem; flex-wrap: wrap; }
        .coluna-topo h3 { margin: 0; font-size: 1rem; font-weight: 600; display: flex; align-items: center; gap: 0.5rem; }
        .coluna-topo h3 .ponto { width: 0.5rem; height: 0.5rem; border-radius: 50%; }
        .c-ac .ponto { background: var(--ouro); }
        .c-er .ponto { background: var(--azul-claro); }
        .c-pe .ponto { background: var(--apagado); }
        .coluna-topo h3 small { font-family: var(--f-dado); font-weight: 500; font-size: 0.8125rem; color: var(--apagado); }
        .btn-baixar {
            font-size: 0.6875rem; font-weight: 600; color: var(--azul-claro); text-decoration: none;
            border: 1px solid rgba(180, 200, 242, .4); border-radius: 999px; padding: 0.25rem 0.75rem; white-space: nowrap;
        }
        .btn-baixar:hover { background: rgba(180, 200, 242, .1); }

        /* cartão de uma recomendação */
        .cartao {
            background: var(--vidro); border: 1px solid var(--vidro-borda); box-shadow: var(--vidro-sombra);
            border-radius: 0.75rem; padding: 0.75rem 0.9375rem 0.8125rem; margin-bottom: 0.5rem;
            border-left: 3px solid var(--linha);
        }
        .cartao.r-acertou { border-left-color: var(--ouro); }
        .cartao.r-errou { border-left-color: rgba(180, 200, 242, .55); }
        .cartao-topo { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; margin-bottom: 0.375rem; }
        .jogo { font-size: 0.6875rem; color: var(--apagado); min-width: 0; }
        .jogo .data { font-family: var(--f-dado); margin-right: 0.375rem; }
        .selo { font-family: var(--f-corpo); font-size: 0.5625rem; letter-spacing: 0; text-transform: none; padding: 0.125rem 0.4375rem; border-radius: 999px; white-space: nowrap; }
        .selo-acertou { color: var(--cta-texto); background: var(--ouro); }
        .selo-errou { color: var(--azul-claro); border: 1px solid rgba(180, 200, 242, .5); }
        .selo-pendente { color: var(--apagado); border: 1px solid var(--linha); }
        .descricao { font-size: 0.8125rem; font-weight: 500; line-height: 1.45; margin-bottom: 0.4375rem; }
        .metricas { display: flex; gap: 0.25rem 0.875rem; flex-wrap: wrap; align-items: center; font-size: 0.6875rem; color: var(--apagado); }
        .metricas b { font-family: var(--f-dado); font-weight: 500; color: var(--texto); font-variant-numeric: tabular-nums; }
        .casa { font-family: var(--f-corpo); font-size: 0.5625rem; letter-spacing: 0; text-transform: none; color: var(--azul-claro); }

        .paginacao { display: flex; align-items: center; justify-content: center; gap: 0.875rem; margin-top: 0.75rem; font-family: var(--f-dado); font-size: 0.6875rem; color: var(--apagado); }
        .btn-pagina {
            background: rgba(255,255,255,.05); border: 1px solid var(--linha); color: var(--texto);
            padding: 0.3125rem 0.75rem; font-size: 0.75rem;
        }
        .btn-pagina:hover:not(:disabled) { border-color: var(--azul-claro); background: rgba(180, 200, 242, .1); }
        .btn-pagina:disabled { opacity: .35; cursor: default; }

        /* ---------- por rodada ---------- */
        .bloco-rodada { margin-bottom: 0.625rem; padding: 0; overflow: hidden; }
        .rodada-titulo {
            width: 100%; display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 1rem; align-items: center;
            padding: 0.875rem 1.125rem; background: none; border: 0; border-radius: 0; color: var(--texto);
            font-family: var(--f-corpo); font-weight: 400; text-align: left; cursor: pointer;
        }
        .rodada-titulo:hover { background: rgba(255,255,255,.04); }
        .rodada-nome { font-size: 0.9375rem; font-weight: 600; }
        .rodada-resumo { display: flex; align-items: center; gap: 0.75rem; font-size: 0.6875rem; color: var(--apagado); white-space: nowrap; }
        .rodada-resumo b { font-family: var(--f-dado); font-weight: 600; color: var(--texto); margin-right: 0.25rem; }
        .mini-barra { display: flex; width: 6rem; height: 0.375rem; border-radius: 999px; overflow: hidden; background: rgba(255,255,255,.06); }
        .mini-barra i { display: block; height: 100%; }
        .rodada-seta {
            width: 0.5rem; height: 0.5rem; border-right: 2px solid var(--apagado); border-bottom: 2px solid var(--apagado);
            transform: translateY(-0.125rem) rotate(45deg); transition: transform .2s;
        }
        .bloco-rodada.aberto .rodada-seta { transform: translateY(0.125rem) rotate(-135deg); }
        .bloco-rodada .colunas { display: none; padding: 0.25rem 1.125rem 1.125rem; }
        .bloco-rodada.aberto .colunas { display: grid; }
        .carregar-mais-wrap { text-align: center; margin-top: 1rem; }
        .btn-carregar-mais { background: transparent; color: var(--azul-claro); border: 1px solid rgba(180, 200, 242, .55); }
        .btn-carregar-mais:hover:not(:disabled) { background: rgba(180, 200, 242, .1); }
        .btn-carregar-mais:disabled { opacity: .6; cursor: default; }

        /* ---------- múltiplas ---------- */
        .mult-resumo { display: flex; gap: 1.5rem; flex-wrap: wrap; align-items: center; padding: 0.875rem 1.125rem; margin-bottom: 1.25rem; font-size: 0.75rem; color: var(--apagado); }
        .mult-resumo b { font-family: var(--f-dado); font-weight: 600; font-size: 1rem; color: var(--texto); margin-right: 0.25rem; }
        .mult-resumo .ouro b { color: var(--ouro); }
        .mult-resumo .azul b { color: var(--azul-claro); }
        .mult-resumo p { flex: 1 1 22rem; margin: 0; line-height: 1.5; }
        .jogos-mult { display: grid; gap: 0.125rem; }

        /* ---------- janela da tabela por faixa ---------- */
        .modal-fundo { display: none; position: fixed; inset: 0; z-index: 100; align-items: center; justify-content: center; background: rgba(1, 5, 15, .72); padding: 1rem; }
        .modal-caixa {
            width: 100%; max-width: 72rem; max-height: 85vh; display: flex; flex-direction: column; padding: 1.125rem 1.25rem;
            border-radius: var(--raio); border: 1px solid var(--vidro-borda); box-shadow: var(--vidro-sombra);
            background: linear-gradient(140deg, rgba(255,255,255,.1), rgba(255,255,255,.03)), #06122e;
        }
        .modal-topo { display: flex; justify-content: space-between; align-items: center; gap: 1rem; margin-bottom: 0.875rem; }
        .modal-titulo { font-size: 1.125rem; font-weight: 300; letter-spacing: -.02em; }
        .modal-fechar { background: none; color: var(--apagado); font-size: 0.75rem; padding: 0.25rem; }
        .modal-fechar:hover { color: var(--texto); }
        #modal-faixa-corpo { overflow: auto; min-height: 0; }
        .tabela-faixa { border-collapse: collapse; width: 100%; font-size: 0.6875rem; white-space: nowrap; }
        .tabela-faixa th { text-align: left; font-family: var(--f-corpo); font-weight: 500; font-size: 0.5625rem; letter-spacing: 0; text-transform: none; color: var(--apagado); padding: 0.375rem 0.625rem; border-bottom: 1px solid var(--linha); position: sticky; top: 0; background: #0a1838; }
        .tabela-faixa td { padding: 0.375rem 0.625rem; border-bottom: 1px solid var(--linha-fraca); }
        .tabela-faixa td.n { font-family: var(--f-dado); text-align: right; }
        .tabela-faixa tr.linha-acertou td:last-child { color: var(--ouro); font-weight: 600; }
        .tabela-faixa tr.linha-errou td:last-child { color: var(--azul-claro); }

        @media (max-width: 1099px) {
            :root { --degrau1: 100%; --degrau2: 100%; }
            .calib { grid-template-columns: minmax(0, 1fr); }
        }
        @media (max-width: 760px) {
            .colunas, .bloco-rodada.aberto .colunas { grid-template-columns: minmax(0, 1fr); }
            .placar-numeros { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .faixa { grid-template-columns: 4.5rem minmax(0, 1fr) 6.25rem; gap: 0.5rem; }
            .rodada-titulo { grid-template-columns: minmax(0, 1fr) auto; }
            .rodada-resumo .mini-barra { display: none; }
        }
        @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
    </style>
</head>
<body>
""" + FUNDO_VISUAL_NOVO + """    {{ nav_html|safe }}
    {% macro dec(v, casas=0) %}{{ (('%.' ~ casas ~ 'f')|format(v or 0)).replace('.', ',') }}{% endmacro %}
    {% macro milhar(v) %}{{ '{:,}'.format(v).replace(',', '.') }}{% endmacro %}
    {% macro data_curta(d) %}{% if d is string %}{{ d[8:10] }}/{{ d[5:7] }}{% elif d %}{{ d.strftime('%d/%m') }}{% endif %}{% endmacro %}
    {% macro escudo(nome) %}{% set eid = escudos.get(nome) if escudos else None %}{% if eid %}<img class="escudo" src="/escudo/{{ eid }}.png" alt="" loading="lazy">{% endif %}{% endmacro %}
    {% macro cartao_item(i) %}
        <article class="cartao item-pagina r-{{ i.resultado }}">
            <div class="cartao-topo">
                <span class="jogo"><span class="data">{{ data_curta(i.data_jogo) }}</span>{{ escudo(i.nosso_time) }}{{ i.nosso_time }} x {{ escudo(i.adversario) }}{{ i.adversario }}</span>
                <span class="selo selo-{{ i.resultado }}">{{ i.resultado }}</span>
            </div>
            <div class="descricao">{{ i.descricao }}</div>
            <div class="metricas">
                <span class="casa">{{ i.casa_aposta }}</span>
                <span>odd <b>{{ dec(i.odd_oferecida, 2) }}</b></span>
                <span>prob. <b>{{ dec(i.probabilidade_historica, 1) }}%</b></span>
                <span>VE <b>{{ '+' if (i.valor_esperado or 0) >= 0 else '' }}{{ dec(i.valor_esperado, 3) }}</b></span>
            </div>
        </article>
    {% endmacro %}
    {% macro cartao_multipla(c) %}
        <article class="cartao item-pagina r-{{ c.resultado }}">
            <div class="cartao-topo">
                <span class="jogo jogos-mult">{% for j in c.jogos %}<span><span class="data">{{ data_curta(j.data_jogo) }}</span>{{ j.nosso_time }} x {{ j.adversario }}</span>{% endfor %}</span>
                <span class="selo selo-{{ c.resultado }}">{{ c.resultado }}</span>
            </div>
            <div class="descricao">{{ c.descricao }}</div>
            <div class="metricas">
                <span class="casa">{{ c.casa_aposta }}</span>
                <span>odd <b>{{ dec(c.odd_combinada, 2) }}</b></span>
                <span>prob. <b>{{ dec(c.probabilidade_combinada, 1) }}%</b></span>
                <span>VE <b>{{ '+' if (c.valor_esperado or 0) >= 0 else '' }}{{ dec(c.valor_esperado, 3) }}</b></span>
            </div>
        </article>
    {% endmacro %}
    {% macro paginacao(lista_id, n) %}
        {% if n > 10 %}
        <div class="paginacao">
            <button type="button" class="btn-pagina" id="anterior-{{ lista_id }}" onclick="mudarPagina('{{ lista_id }}', -1)">Anterior</button>
            <span id="label-{{ lista_id }}"></span>
            <button type="button" class="btn-pagina" id="proximo-{{ lista_id }}" onclick="mudarPagina('{{ lista_id }}', 1)">Próxima</button>
        </div>
        {% endif %}
    {% endmacro %}

    <main class="conteudo">
        <header class="cabeca">
            <h1>Histórico</h1>
            <p>Toda recomendação, depois do jogo: o que o sistema disse e o que aconteceu.</p>
        </header>

        {% set avaliadas = resumo.acertou + resumo.errou %}
        <section class="vidro placar" aria-label="Resumo">
            <div class="placar-numeros">
                <div class="ouro"><span class="rotulo">Acertou</span><b>{{ milhar(resumo.acertou) }}</b></div>
                <div class="azul"><span class="rotulo">Errou</span><b>{{ milhar(resumo.errou) }}</b></div>
                <div><span class="rotulo">Pendente</span><b>{{ milhar(resumo.pendente) }}</b></div>
                <div><span class="rotulo">Taxa de acerto</span><b>{{ dec(resumo.taxa, 1) }}%</b></div>
            </div>
            {% if avaliadas %}
            <div class="placar-barra" role="img" aria-label="{{ dec(resumo.taxa, 1) }}% de acerto entre as avaliadas">
                <i class="b-ac" style="width: {{ 100 * resumo.acertou / avaliadas }}%"></i><i class="b-er" style="width: {{ 100 * resumo.errou / avaliadas }}%"></i>
            </div>
            {% endif %}
        </section>

        {% if calibracao.detalhamento or calibracao_odd.detalhamento %}
        <section class="secao" aria-labelledby="t-calib">
            <h2 class="secao-titulo" id="t-calib">Prometido × acontecido</h2>
            <p class="nota">Se a probabilidade do sistema está certa, as apostas de 60-80% acertam entre 60 e 80% das vezes. A faixa tracejada é o que foi prometido; a barra é o que aconteceu. Clique numa linha para ver as apostas dela.</p>
            <div class="calib">
                {% if calibracao.detalhamento %}
                <div class="vidro calib-painel">
                    <h3>Por probabilidade histórica</h3>
                    {% if calibracao.melhor_faixa %}
                    <p class="calib-destaque">Maior acerto entre <b>{{ calibracao.melhor_faixa.inicio }}% e {{ calibracao.melhor_faixa.fim }}%</b>: {{ dec(calibracao.melhor_faixa.taxa, 1) }}% em {{ milhar(calibracao.melhor_faixa.total) }} apostas.</p>
                    {% endif %}
                    <div class="legenda"><span><i class="leg-fx"></i>prometido</span><span><i class="leg-real"></i>aconteceu</span></div>
                    <div class="calib-linhas">
                        {% for item in calibracao.detalhamento|sort(attribute='inicio') %}
                        <button type="button" class="faixa" onclick="abrirFaixaProb({{ item.inicio }})">
                            <span class="faixa-rot">{{ item.inicio }}-{{ item.fim }}%</span>
                            <span class="trilho">
                                <span class="fx" style="left: {{ item.inicio }}%; width: {{ item.fim - item.inicio }}%"></span>
                                <span class="real{{ ' bom' if item.inicio <= item.taxa <= item.fim else '' }}" style="width: {{ item.taxa }}%"></span>
                            </span>
                            <span class="faixa-num"><b>{{ dec(item.taxa, 1) }}%</b>{{ milhar(item.acertou + item.errou) }}</span>
                        </button>
                        {% endfor %}
                    </div>
                </div>
                {% endif %}
                {% if calibracao_odd.detalhamento %}
                <div class="vidro calib-painel">
                    <h3>Por odd</h3>
                    {% if calibracao_odd.melhor_faixa %}
                    <p class="calib-destaque">Maior acerto na odd <b>{{ calibracao_odd.melhor_faixa.rotulo|replace('.', ',') }}</b>: {{ dec(calibracao_odd.melhor_faixa.taxa, 1) }}% em {{ milhar(calibracao_odd.melhor_faixa.total) }} apostas.</p>
                    {% endif %}
                    <div class="legenda"><span><i class="leg-marca"></i>ponto de lucro (1 ÷ odd)</span><span><i class="leg-real"></i>aconteceu: em ouro quando passou do ponto</span></div>
                    <div class="calib-linhas calib-rolagem">
                        {% for item in calibracao_odd.detalhamento|sort(attribute='indice') %}
                        {# inicio/fim chegam como texto ("1.80"); a conta usa o índice (18 = 1,80 a 1,90) #}
                        {% set meio = ((item.indice + 0.5) / 10) if item.fim else (item.indice / 10) %}
                        {% set empate = 100 / meio if meio else 0 %}
                        <button type="button" class="faixa" onclick="abrirFaixaOdd({{ item.indice }})">
                            <span class="faixa-rot">{{ item.rotulo|replace('.', ',') }}</span>
                            <span class="trilho">
                                <span class="real{{ ' bom' if item.taxa >= empate else '' }}" style="width: {{ item.taxa }}%"></span>
                                <span class="marca" style="left: {{ [empate, 100]|min }}%" title="{{ dec(empate, 1) }}%"></span>
                            </span>
                            <span class="faixa-num"><b>{{ dec(item.taxa, 1) }}%</b>{{ milhar(item.acertou + item.errou) }}</span>
                        </button>
                        {% endfor %}
                    </div>
                </div>
                {% endif %}
            </div>
        </section>
        {% endif %}

        <section class="secao" aria-labelledby="t-todas">
            <h2 class="secao-titulo" id="t-todas">Todas as recomendações</h2>
            <p class="nota">Da mais recente para a mais antiga. Mercado do jogo inteiro aparece uma vez por jogo.</p>
            <div class="colunas">
                <div class="coluna c-ac">
                    <div class="coluna-topo"><h3><i class="ponto"></i>Acertou <small>{{ milhar(acertos|length) }}</small></h3></div>
                    <div id="lista-acertou">
                        {% for i in acertos %}{{ cartao_item(i) }}{% else %}<div class="vazio">Nenhum acerto ainda.</div>{% endfor %}
                    </div>
                    {{ paginacao('lista-acertou', acertos|length) }}
                </div>
                <div class="coluna c-er">
                    <div class="coluna-topo"><h3><i class="ponto"></i>Errou <small>{{ milhar(erros|length) }}</small></h3></div>
                    <div id="lista-errou">
                        {% for i in erros %}{{ cartao_item(i) }}{% else %}<div class="vazio">Nenhum erro ainda.</div>{% endfor %}
                    </div>
                    {{ paginacao('lista-errou', erros|length) }}
                </div>
            </div>
            <div class="coluna c-pe" style="margin-top: 1.75rem">
                <div class="coluna-topo"><h3><i class="ponto"></i>Pendente <small>{{ milhar(pendentes|length) }}</small></h3></div>
                <div id="lista-pendente">
                    {% for i in pendentes %}{{ cartao_item(i) }}{% else %}<div class="vazio">Nenhuma recomendação esperando resultado agora.</div>{% endfor %}
                </div>
                {{ paginacao('lista-pendente', pendentes|length) }}
            </div>
        </section>

        <section class="secao" aria-labelledby="t-rodadas">
            <h2 class="secao-titulo" id="t-rodadas">Por rodada</h2>
            <p class="nota">As mesmas recomendações, separadas por rodada, com cada lado para baixar em CSV. As de antes de 21/08/2026 (sem a rodada gravada) ficam em "Sem rodada identificada", no fim.</p>
            <div id="secao-rodadas">
                {% for bloco in blocos_rodadas %}
                {% set n_ac = bloco.acertou|length %}{% set n_er = bloco.errou|length %}{% set n_tot = n_ac + n_er %}
                <div class="vidro bloco-rodada{{ ' aberto' if loop.first else '' }}" id="bloco-rodada-{{ bloco.rodada_id }}">
                    <button type="button" class="rodada-titulo" onclick="toggleRodadaBloco('{{ bloco.rodada_id }}')">
                        <span class="rodada-nome">{{ bloco.rotulo }}</span>
                        <span class="rodada-resumo">
                            <span><b>{{ n_ac }}</b>acertos · <b>{{ n_er }}</b>erros{% if n_tot %} · <b>{{ dec(100 * n_ac / n_tot, 1) }}%</b>{% endif %}</span>
                            {% if n_tot %}<span class="mini-barra"><i style="width: {{ 100 * n_ac / n_tot }}%; background: var(--ouro)"></i><i style="width: {{ 100 * n_er / n_tot }}%; background: rgba(180, 200, 242, .45)"></i></span>{% endif %}
                        </span>
                        <span class="rodada-seta" aria-hidden="true"></span>
                    </button>
                    <div class="colunas">
                        <div class="coluna c-ac">
                            <div class="coluna-topo"><h3><i class="ponto"></i>Acertou <small>{{ n_ac }}</small></h3><a class="btn-baixar" href="/historico/rodada/{{ bloco.rodada_url }}/download/acertou.csv">Baixar certas</a></div>
                            <div id="lista-rodada-{{ bloco.rodada_id }}-acertou">
                                {% for i in bloco.acertou %}{{ cartao_item(i) }}{% else %}<div class="vazio">Nenhum acerto nessa rodada.</div>{% endfor %}
                            </div>
                            {{ paginacao('lista-rodada-' ~ bloco.rodada_id ~ '-acertou', n_ac) }}
                        </div>
                        <div class="coluna c-er">
                            <div class="coluna-topo"><h3><i class="ponto"></i>Errou <small>{{ n_er }}</small></h3><a class="btn-baixar" href="/historico/rodada/{{ bloco.rodada_url }}/download/errou.csv">Baixar erradas</a></div>
                            <div id="lista-rodada-{{ bloco.rodada_id }}-errou">
                                {% for i in bloco.errou %}{{ cartao_item(i) }}{% else %}<div class="vazio">Nenhum erro nessa rodada.</div>{% endfor %}
                            </div>
                            {{ paginacao('lista-rodada-' ~ bloco.rodada_id ~ '-errou', n_er) }}
                        </div>
                    </div>
                </div>
                {% endfor %}
            </div>
            {% if tem_mais_rodadas %}
            <div class="carregar-mais-wrap">
                <button type="button" class="btn-carregar-mais" id="btn-carregar-mais-rodadas" onclick="carregarMaisRodadas()">Carregar mais rodadas</button>
            </div>
            {% endif %}
        </section>

        {% if multiplas_destaque or resumo_multiplas.acertou + resumo_multiplas.errou > 0 %}
        <section class="secao" aria-labelledby="t-mult">
            <h2 class="secao-titulo" id="t-mult">Múltiplas em destaque</h2>
            <p class="nota">As 5 melhores múltiplas (por probabilidade histórica) de cada jogo já concluído, capturadas antes do apito. Não mexem na banca e não têm botão de salvar. Só as rodadas recentes aparecem com o cartão; as antigas saem da lista, mas continuam no total.</p>
            {% if resumo_multiplas.acertou + resumo_multiplas.errou > 0 %}
            <div class="vidro mult-resumo">
                <span class="ouro"><b>{{ milhar(resumo_multiplas.acertou) }}</b>acertos</span>
                <span class="azul"><b>{{ milhar(resumo_multiplas.errou) }}</b>erros</span>
                <span><b>{{ dec(resumo_multiplas.taxa, 1) }}%</b>de acerto</span>
                <p>Esse percentual é só do top 5 de cada jogo e não se soma com a taxa geral lá de cima: são grupos diferentes.</p>
            </div>
            {% endif %}
            <div class="colunas">
                <div class="coluna c-ac">
                    <div class="coluna-topo"><h3><i class="ponto"></i>Acertou <small>{{ multiplas_acertou|length }}</small></h3></div>
                    <div id="lista-multiplas-acertou">
                        {% for c in multiplas_acertou %}{{ cartao_multipla(c) }}{% else %}<div class="vazio">Nenhum acerto ainda.</div>{% endfor %}
                    </div>
                    {{ paginacao('lista-multiplas-acertou', multiplas_acertou|length) }}
                </div>
                <div class="coluna c-er">
                    <div class="coluna-topo"><h3><i class="ponto"></i>Errou <small>{{ multiplas_errou|length }}</small></h3></div>
                    <div id="lista-multiplas-errou">
                        {% for c in multiplas_errou %}{{ cartao_multipla(c) }}{% else %}<div class="vazio">Nenhum erro ainda.</div>{% endfor %}
                    </div>
                    {{ paginacao('lista-multiplas-errou', multiplas_errou|length) }}
                </div>
            </div>
            {% if multiplas_pendente %}
            <div class="coluna c-pe" style="margin-top: 1.75rem">
                <div class="coluna-topo"><h3><i class="ponto"></i>Pendente <small>{{ multiplas_pendente|length }}</small></h3></div>
                <div id="lista-multiplas-pendente">
                    {% for c in multiplas_pendente %}{{ cartao_multipla(c) }}{% endfor %}
                </div>
                {{ paginacao('lista-multiplas-pendente', multiplas_pendente|length) }}
            </div>
            {% endif %}
        </section>
        {% endif %}
    </main>

    <div id="modal-faixa" class="modal-fundo" onclick="if (event.target === this) fecharFaixa()">
        <div class="modal-caixa" role="dialog" aria-modal="true" aria-labelledby="modal-faixa-titulo">
            <div class="modal-topo">
                <span class="modal-titulo" id="modal-faixa-titulo"></span>
                <button type="button" class="modal-fechar" onclick="fecharFaixa()">Fechar</button>
            </div>
            <div id="modal-faixa-corpo"></div>
        </div>
    </div>

    <script>
        const ESCUDOS = {{ (escudos or {})|tojson }};
        const TAMANHO_PAGINA = 10;
        const paginaAtual = {};

        function salvarPaginaNoNavegador(listaId, pagina) {
            try { localStorage.setItem('pagina:' + window.location.pathname + ':' + listaId, String(pagina)); } catch (e) {}
        }
        function carregarPaginaDoNavegador(listaId) {
            try {
                const v = localStorage.getItem('pagina:' + window.location.pathname + ':' + listaId);
                return v !== null ? parseInt(v, 10) : 0;
            } catch (e) { return 0; }
        }
        function totalPaginas(listaId) {
            const n = document.querySelectorAll('#' + listaId + ' .item-pagina').length;
            return Math.max(1, Math.ceil(n / TAMANHO_PAGINA));
        }
        function renderizarPagina(listaId) {
            if (!document.getElementById(listaId)) return;
            if (!(listaId in paginaAtual)) { paginaAtual[listaId] = carregarPaginaDoNavegador(listaId); }
            const total = totalPaginas(listaId);
            if (paginaAtual[listaId] > total - 1) paginaAtual[listaId] = total - 1;
            if (paginaAtual[listaId] < 0) paginaAtual[listaId] = 0;
            const pagina = paginaAtual[listaId];
            document.querySelectorAll('#' + listaId + ' .item-pagina').forEach(function(item, i) {
                item.style.display = (Math.floor(i / TAMANHO_PAGINA) === pagina) ? '' : 'none';
            });
            const label = document.getElementById('label-' + listaId);
            if (label) label.textContent = 'Página ' + (pagina + 1) + ' de ' + total;
            const a = document.getElementById('anterior-' + listaId), p = document.getElementById('proximo-' + listaId);
            if (a) a.disabled = (pagina === 0);
            if (p) p.disabled = (pagina >= total - 1);
        }
        function mudarPagina(listaId, direcao) {
            const total = totalPaginas(listaId);
            const pagina = Math.max(0, Math.min(total - 1, (paginaAtual[listaId] || 0) + direcao));
            paginaAtual[listaId] = pagina;
            salvarPaginaNoNavegador(listaId, pagina);
            renderizarPagina(listaId);
        }

        function escaparHtml(s) {
            return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
            }[c]));
        }
        function br(v, casas) { return Number(v || 0).toFixed(casas).replace('.', ','); }
        function dataCurta(d) { const s = String(d || ''); return s.length >= 10 ? s.slice(8, 10) + '/' + s.slice(5, 7) : s; }
        function escudoJS(nome) { const id = ESCUDOS[nome]; return id ? '<img class="escudo" src="/escudo/' + id + '.png" alt="" loading="lazy">' : ''; }

        // ---- tabela de uma faixa (probabilidade ou odd), numa janela só ----
        const CACHE_FAIXA_CALIBRACAO = {};
        const CACHE_FAIXA_CALIBRACAO_ODD = {};
        function abrirJanelaFaixa(titulo) {
            document.getElementById('modal-faixa-titulo').textContent = titulo;
            document.getElementById('modal-faixa-corpo').innerHTML = '<div class="vazio">Carregando...</div>';
            document.getElementById('modal-faixa').style.display = 'flex';
            return document.getElementById('modal-faixa-corpo');
        }
        function fecharFaixa() { document.getElementById('modal-faixa').style.display = 'none'; }
        document.addEventListener('keydown', function(e) { if (e.key === 'Escape') fecharFaixa(); });
        function abrirFaixaProb(inicioFaixa) {
            const corpo = abrirJanelaFaixa('Apostas com probabilidade entre ' + inicioFaixa + '% e ' + (inicioFaixa + 20) + '%');
            if (CACHE_FAIXA_CALIBRACAO[inicioFaixa]) { renderizarTabelaFaixaEm(corpo, CACHE_FAIXA_CALIBRACAO[inicioFaixa]); return; }
            fetch('/api/calibracao/' + inicioFaixa).then(r => r.json()).then(itens => {
                CACHE_FAIXA_CALIBRACAO[inicioFaixa] = itens;
                renderizarTabelaFaixaEm(corpo, itens);
            }).catch(() => { corpo.innerHTML = '<div class="vazio">Não deu para carregar agora.</div>'; });
        }
        function abrirFaixaOdd(indiceFaixa) {
            const inicio = (indiceFaixa / 10).toFixed(2).replace('.', ',');
            const corpo = abrirJanelaFaixa('Apostas com odd ' + (indiceFaixa >= 40 ? inicio + ' ou mais' : 'entre ' + inicio + ' e ' + ((indiceFaixa + 1) / 10).toFixed(2).replace('.', ',')));
            if (CACHE_FAIXA_CALIBRACAO_ODD[indiceFaixa]) { renderizarTabelaFaixaEm(corpo, CACHE_FAIXA_CALIBRACAO_ODD[indiceFaixa]); return; }
            fetch('/api/calibracao-odd/' + indiceFaixa).then(r => r.json()).then(itens => {
                CACHE_FAIXA_CALIBRACAO_ODD[indiceFaixa] = itens;
                renderizarTabelaFaixaEm(corpo, itens);
            }).catch(() => { corpo.innerHTML = '<div class="vazio">Não deu para carregar agora.</div>'; });
        }
        function renderizarTabelaFaixaEm(alvo, itens) {
            if (!itens.length) { alvo.innerHTML = '<div class="vazio">Nenhuma recomendação avaliada nessa faixa ainda.</div>'; return; }
            const ac = itens.filter(i => i.resultado === 'acertou').length;
            const linhas = itens.map(i => `
                <tr class="linha-${i.resultado}">
                    <td>${dataCurta(i.data_jogo)} · ${escaparHtml(i.nosso_time)} x ${escaparHtml(i.adversario)}</td>
                    <td>${escaparHtml(i.descricao)}</td>
                    <td>${escaparHtml(i.casa_aposta)}</td>
                    <td class="n">${br(i.odd_oferecida, 2)}</td>
                    <td class="n">${br(i.probabilidade_historica, 1)}%</td>
                    <td class="n">${br(i.valor_esperado, 3)}</td>
                    <td>${i.resultado}</td>
                </tr>`).join('');
            alvo.innerHTML = `
                <p class="nota" style="margin: 0 0 0.75rem">${itens.length} apostas · ${ac} acertos · ${br(100 * ac / itens.length, 1)}% · da maior probabilidade para a menor</p>
                <table class="tabela-faixa">
                    <thead><tr><th>Jogo</th><th>Mercado</th><th>Casa</th><th>Odd</th><th>Prob.</th><th>VE</th><th>Resultado</th></tr></thead>
                    <tbody>${linhas}</tbody>
                </table>`;
        }

        ['lista-acertou', 'lista-errou', 'lista-pendente'].forEach(renderizarPagina);
        [
            {% for bloco in blocos_rodadas %}
            'lista-rodada-{{ bloco.rodada_id }}-acertou',
            'lista-rodada-{{ bloco.rodada_id }}-errou',
            {% endfor %}
        ].forEach(renderizarPagina);
        ['lista-multiplas-acertou', 'lista-multiplas-errou', 'lista-multiplas-pendente'].forEach(renderizarPagina);

        // ---- rodadas carregadas depois (mesmo markup do Jinja, montado em JS) ----
        let offsetRodadas = {{ blocos_rodadas|length }};
        function construirCartaoItemJS(i) {
            const ve = Number(i.valor_esperado || 0);
            return `
                <article class="cartao item-pagina r-${i.resultado}">
                    <div class="cartao-topo">
                        <span class="jogo"><span class="data">${dataCurta(i.data_jogo)}</span>${escudoJS(i.nosso_time)}${escaparHtml(i.nosso_time)} x ${escudoJS(i.adversario)}${escaparHtml(i.adversario)}</span>
                        <span class="selo selo-${i.resultado}">${i.resultado}</span>
                    </div>
                    <div class="descricao">${escaparHtml(i.descricao)}</div>
                    <div class="metricas">
                        <span class="casa">${escaparHtml(i.casa_aposta)}</span>
                        <span>odd <b>${br(i.odd_oferecida, 2)}</b></span>
                        <span>prob. <b>${br(i.probabilidade_historica, 1)}%</b></span>
                        <span>VE <b>${ve >= 0 ? '+' : ''}${br(ve, 3)}</b></span>
                    </div>
                </article>`;
        }
        function construirColunaRodadaJS(bloco, resultado, titulo, textoBotao) {
            const itens = bloco[resultado];
            const listaId = 'lista-rodada-' + bloco.rodada_id + '-' + resultado;
            const itensHtml = itens.length ? itens.map(construirCartaoItemJS).join('')
                : `<div class="vazio">Nenhum${resultado === 'acertou' ? ' acerto' : ' erro'} nessa rodada.</div>`;
            const paginacaoHtml = itens.length > 10 ? `
                <div class="paginacao">
                    <button type="button" class="btn-pagina" id="anterior-${listaId}" onclick="mudarPagina('${listaId}', -1)">Anterior</button>
                    <span id="label-${listaId}"></span>
                    <button type="button" class="btn-pagina" id="proximo-${listaId}" onclick="mudarPagina('${listaId}', 1)">Próxima</button>
                </div>` : '';
            return `
                <div class="coluna ${resultado === 'acertou' ? 'c-ac' : 'c-er'}">
                    <div class="coluna-topo"><h3><i class="ponto"></i>${titulo} <small>${itens.length}</small></h3><a class="btn-baixar" href="/historico/rodada/${bloco.rodada_url}/download/${resultado}.csv">${textoBotao}</a></div>
                    <div id="${listaId}">${itensHtml}</div>
                    ${paginacaoHtml}
                </div>`;
        }
        function construirBlocoRodadaJS(bloco) {
            const nAc = bloco.acertou.length, nEr = bloco.errou.length, nTot = nAc + nEr;
            const taxa = nTot ? ` · <b>${br(100 * nAc / nTot, 1)}%</b>` : '';
            const barra = nTot ? `<span class="mini-barra"><i style="width: ${100 * nAc / nTot}%; background: var(--ouro)"></i><i style="width: ${100 * nEr / nTot}%; background: rgba(180, 200, 242, .45)"></i></span>` : '';
            return `
                <div class="vidro bloco-rodada" id="bloco-rodada-${bloco.rodada_id}">
                    <button type="button" class="rodada-titulo" onclick="toggleRodadaBloco('${bloco.rodada_id}')">
                        <span class="rodada-nome">${escaparHtml(bloco.rotulo)}</span>
                        <span class="rodada-resumo"><span><b>${nAc}</b>acertos · <b>${nEr}</b>erros${taxa}</span>${barra}</span>
                        <span class="rodada-seta" aria-hidden="true"></span>
                    </button>
                    <div class="colunas">
                        ${construirColunaRodadaJS(bloco, 'acertou', 'Acertou', 'Baixar certas')}
                        ${construirColunaRodadaJS(bloco, 'errou', 'Errou', 'Baixar erradas')}
                    </div>
                </div>`;
        }
        function toggleRodadaBloco(rodadaId) {
            document.getElementById('bloco-rodada-' + rodadaId).classList.toggle('aberto');
        }
        function carregarMaisRodadas() {
            const btn = document.getElementById('btn-carregar-mais-rodadas');
            btn.disabled = true;
            btn.textContent = 'Carregando...';
            fetch('/api/historico-rodadas?offset=' + offsetRodadas)
                .then(r => r.json())
                .then(dados => {
                    const container = document.getElementById('secao-rodadas');
                    dados.blocos.forEach(bloco => {
                        container.insertAdjacentHTML('beforeend', construirBlocoRodadaJS(bloco));
                        renderizarPagina('lista-rodada-' + bloco.rodada_id + '-acertou');
                        renderizarPagina('lista-rodada-' + bloco.rodada_id + '-errou');
                    });
                    offsetRodadas += dados.blocos.length;
                    if (dados.tem_mais) { btn.disabled = false; btn.textContent = 'Carregar mais rodadas'; }
                    else { btn.remove(); }
                })
                .catch(() => { btn.disabled = false; btn.textContent = 'Carregar mais rodadas'; });
        }
    </script>
""" + SCRIPT_FUNDO_VIVO + """</body>
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


# NOVO (17/08/2026): largura da faixa usada no card "Acertos e erros por
# probabilidade histórica" - trocar pra 10 quando o histórico tiver volume
# suficiente pra cada faixa continuar com amostra confiável (hoje, com
# ~250 recomendações avaliadas, 20% dá uma média de ~50 por faixa; 10%
# cairia pra ~25, mais instável nas pontas).
FAIXA_CALIBRACAO_LARGURA = 20


def buscar_calibracao(cur):
    """ATUALIZADO (17/08/2026): antes mostrava, no detalhamento do pop-up,
    cada valor EXATO de probabilidade histórica separado (ex: 90.23% e
    90.22% apareciam como duas linhas diferentes, mesmo sendo
    praticamente a mesma coisa) - agora agrupa em faixas de
    FAIXA_CALIBRACAO_LARGURA% (0-20%, 20-40%, ...), mostrando a taxa de
    acerto de cada faixa - mesmo agrupamento que já era usado só pra achar
    a "melhor faixa" do card de destaque, agora reaproveitado pro
    detalhamento inteiro também. Com poucas apostas resolvidas ainda, isso
    é instável - fica mais confiável conforme o histórico crescer."""
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

    # agrupa em faixas de FAIXA_CALIBRACAO_LARGURA% - 100% cai na última
    # faixa (senão viraria uma faixa "100-120" sozinha, sem sentido).
    faixas = {}
    for prob, dados in por_valor.items():
        inicio_faixa = min(
            int(prob // FAIXA_CALIBRACAO_LARGURA) * FAIXA_CALIBRACAO_LARGURA,
            100 - FAIXA_CALIBRACAO_LARGURA,
        )
        faixas.setdefault(inicio_faixa, {"acertou": 0, "errou": 0})
        faixas[inicio_faixa]["acertou"] += dados["acertou"]
        faixas[inicio_faixa]["errou"] += dados["errou"]

    detalhamento = []
    for inicio_faixa, dados in sorted(faixas.items(), reverse=True):
        total = dados["acertou"] + dados["errou"]
        taxa = round(100 * dados["acertou"] / total, 1) if total else 0.0
        detalhamento.append({
            "inicio": inicio_faixa,
            "fim": inicio_faixa + FAIXA_CALIBRACAO_LARGURA,
            "acertou": dados["acertou"],
            "errou": dados["errou"],
            "total": total,
            "taxa": taxa,
        })

    melhor_faixa = None
    for item in detalhamento:
        if item["total"] == 0:
            continue
        if melhor_faixa is None or item["taxa"] > melhor_faixa["taxa"]:
            melhor_faixa = {
                "inicio": item["inicio"], "fim": item["fim"],
                "taxa": item["taxa"], "total": item["total"],
            }

    return {"detalhamento": detalhamento, "melhor_faixa": melhor_faixa}


def buscar_detalhamento_faixa(cur, inicio_faixa):
    """NOVO (17/08/2026): detalhamento completo (jogo, mercado, casa, odd,
    probabilidade, VE, resultado) de todas as recomendações avaliadas
    dentro de uma faixa de probabilidade específica - usado pelo endpoint
    AJAX que abre a tabela ao clicar numa faixa do card "Acertos e erros
    por probabilidade histórica". Mesma query "crua" (sem a deduplicação
    de mercados do jogo inteiro que buscar_historico aplica) que
    buscar_calibracao já usa pra contar - os números batem certinho com o
    resumo que já aparece por faixa, sem essa tabela mostrar uma
    quantidade diferente do que o card já prometeu.

    ATUALIZADO (20/08/2026, corrigido no mesmo dia): ordenação trocada de
    data (mais recente primeiro) pra probabilidade histórica decrescente.
    Primeira tentativa tinha ordenado por ODD por engano - o pedido real
    era ordenar pela força da probabilidade histórica dentro da faixa,
    não pelo valor da odd. Essa tabela existe pra ver as recomendações
    ordenadas pela probabilidade, não pra acompanhar cronologia - só
    nessa tela específica; o resto do /historico continua por data."""
    fim_faixa = inicio_faixa + FAIXA_CALIBRACAO_LARGURA
    cur.execute(
        """
        SELECT h.data_jogo, t.nome, j.adversario, h.descricao, h.casa_aposta,
               h.odd_oferecida, h.probabilidade_historica, h.valor_esperado, h.resultado
        FROM historico_recomendacoes h
        JOIN jogos j ON j.id = h.jogo_id
        JOIN times t ON t.id = j.nosso_time_id
        WHERE h.resultado IN ('acertou', 'errou')
          AND h.probabilidade_historica >= %s
          AND (h.probabilidade_historica < %s OR %s >= 100)
        ORDER BY h.probabilidade_historica DESC, h.id DESC
        """,
        (inicio_faixa, fim_faixa, fim_faixa),
    )
    colunas = ["data_jogo", "nosso_time", "adversario", "descricao", "casa_aposta",
               "odd_oferecida", "probabilidade_historica", "valor_esperado", "resultado"]
    return [dict(zip(colunas, row)) for row in cur.fetchall()]


# NOVO (26/08/2026): seção "Odds por Rodada" em /historico - mesmas
# recomendações individuais (nunca múltipla), agrupadas por j.rodada em
# vez de misturadas, com botão de baixar CSV de acertos/erros separado
# por rodada. Rodadas de antes da migração de 21/08/2026 (jogos.rodada
# ainda NULL na época) caem juntas no grupo "Sem rodada identificada",
# no fim da lista - decisão combinada com Thales: não faz backfill agora,
# a rodada de 22-24/08 já é a primeira completa com o dado certo.
QUANTIDADE_RODADAS_POR_CARGA = 3


def _numero_rodada(rodada):
    """Extrai o número final de uma rodada tipo 'Regular Season - 20' -> 20.
    A API-Football manda esse texto pronto (fixture['league']['round']),
    nunca só o número, então precisa extrair pra poder ordenar direito
    (ordenar o TEXTO colocaria 'Regular Season - 9' depois de 'Regular
    Season - 20', porque '9' > '2' como string)."""
    if not rodada:
        return None
    m = re.search(r"(\d+)\s*$", rodada)
    return int(m.group(1)) if m else None


def _rotulo_rodada(rodada):
    if not rodada:
        return "Sem rodada identificada"
    numero = _numero_rodada(rodada)
    return f"Rodada {numero}" if numero is not None else rodada


def _slug_rodada(rodada):
    """Versão só com [a-z0-9_] de uma rodada, pra usar em id de elemento
    HTML (não pode ter espaço/acento/hífen sem risco de quebrar seletor
    JS) - separado de rodada_url (que preserva o texto original, só
    percent-encoded, pra bater com o que tá gravado no banco)."""
    bruto = rodada if rodada else "sem_rodada"
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", bruto).strip("_").lower()
    return slug or "sem_rodada"


def buscar_rodadas_ordenadas(cur):
    """Lista de toda rodada (texto bruto de jogos.rodada) com pelo menos
    uma recomendação individual avaliada, da mais recente pra mais
    antiga (pelo número extraído - ver _numero_rodada). Rodada sem número
    reconhecível (incluindo NULL) vai pro fim."""
    cur.execute(
        """
        SELECT DISTINCT j.rodada
        FROM historico_recomendacoes h
        JOIN jogos j ON j.id = h.jogo_id
        WHERE h.resultado IN ('acertou', 'errou')
        """
    )
    rodadas = [row[0] for row in cur.fetchall()]
    rodadas.sort(key=lambda r: (_numero_rodada(r) is None, -(_numero_rodada(r) or 0)))
    return rodadas


def buscar_itens_da_rodada(cur, rodada):
    """Mesmas colunas de buscar_detalhamento_faixa, filtradas por rodada -
    `IS NOT DISTINCT FROM` pra funcionar tanto pra rodada normal quanto
    pro grupo NULL ('Sem rodada identificada')."""
    cur.execute(
        """
        SELECT h.data_jogo, t.nome, j.adversario, h.descricao, h.casa_aposta,
               h.odd_oferecida, h.probabilidade_historica, h.valor_esperado, h.resultado
        FROM historico_recomendacoes h
        JOIN jogos j ON j.id = h.jogo_id
        JOIN times t ON t.id = j.nosso_time_id
        WHERE h.resultado IN ('acertou', 'errou')
          AND j.rodada IS NOT DISTINCT FROM %s
        ORDER BY h.data_jogo DESC, h.id DESC
        """,
        (rodada,),
    )
    colunas = ["data_jogo", "nosso_time", "adversario", "descricao", "casa_aposta",
               "odd_oferecida", "probabilidade_historica", "valor_esperado", "resultado"]
    return [dict(zip(colunas, row)) for row in cur.fetchall()]


def montar_blocos_rodadas(cur, todas_rodadas, offset, quantidade):
    """Monta o bloco (rótulo, ids, itens separados por resultado) de um
    LOTE de rodadas - usado tanto no primeiro carregamento de /historico
    quanto no botão "Carregar mais rodadas" via AJAX. offset/quantidade
    paginam a lista de RODADAS; dentro de cada rodada, a paginação de
    itens continua sendo a de sempre (client-side, 10 por página)."""
    lote = todas_rodadas[offset:offset + quantidade]
    blocos = []
    for rodada in lote:
        itens = buscar_itens_da_rodada(cur, rodada)
        blocos.append({
            "rotulo": _rotulo_rodada(rodada),
            "rodada_id": _slug_rodada(rodada),
            "rodada_url": quote(rodada, safe="") if rodada else "_sem_rodada_",
            "acertou": [i for i in itens if i["resultado"] == "acertou"],
            "errou": [i for i in itens if i["resultado"] == "errou"],
        })
    tem_mais = (offset + quantidade) < len(todas_rodadas)
    return blocos, tem_mais


# igual ao de probabilidade histórica em espírito, mas agrupando por
# faixa de odd de 0.10 em 0.10 (ex: 1.30-1.40) em vez de faixa de
# probabilidade - pedido explícito pra não misturar os dois cards.
#
# Faixas combinadas com Thales (26/08/2026):
#   - piso: qualquer odd abaixo de 1.20 cai na mesma primeira faixa
#     "1.10 - 1.20" (não existe faixa própria pra odd de 1.01 até 1.19,
#     viraria uma faixa quase vazia isolada)
#   - teto: qualquer odd a partir de 4.00 cai numa faixa só "4.00+"
#     (aberta, sem limite superior) - odd alta é rara aqui, uma faixa por
#     0.10 lá em cima ficaria com 0-1 amostra cada, sem informar nada
#   - ORDEM: decrescente (odd mais alta primeiro), igual o card de
#     probabilidade
FAIXA_ODD_LARGURA = Decimal("0.10")
INDICE_ODD_MINIMO = 11   # 1.10 * 10 - piso: tudo abaixo cai aqui também
INDICE_ODD_TETO = 40     # 4.00 * 10 - teto: tudo a partir daqui cai aqui também


def _indice_faixa_odd(odd_decimal):
    """Índice inteiro da faixa de odd (largura 0.10). Trabalha em Decimal
    o tempo todo, nunca converte pra float antes de bucketizar - odd é
    NUMERIC(10,2) no banco, e binário não representa 0.1 exato (1.30 * 10
    pode virar 12.999999999998 em float e cair na faixa errada). Decimal
    com 2 casas não tem esse problema: 1.30 * 10 = 13.0 exato."""
    indice = int((odd_decimal * 10).to_integral_value(rounding=ROUND_FLOOR))
    if indice < INDICE_ODD_MINIMO:
        return INDICE_ODD_MINIMO
    if indice >= INDICE_ODD_TETO:
        return INDICE_ODD_TETO
    return indice


def _rotular_faixa_odd(indice):
    """Devolve (inicio, fim, rotulo) - fim é None na faixa-teto aberta
    ('4.00+'), pra não ter que fazer aritmética condicional dentro do
    Jinja (que fica ilegível pra concatenar string com condicional)."""
    inicio_decimal = Decimal(indice) / Decimal(10)
    inicio = f"{inicio_decimal:.2f}"
    if indice >= INDICE_ODD_TETO:
        return inicio, None, f"{inicio}+"
    fim_decimal = inicio_decimal + FAIXA_ODD_LARGURA
    fim = f"{fim_decimal:.2f}"
    return inicio, fim, f"{inicio} - {fim}"


def buscar_calibracao_odd(cur):
    """Mesma ideia de buscar_calibracao, mas por LINHA DE ODD em vez de
    probabilidade histórica - card separado em /historico, logo abaixo
    do de probabilidade."""
    cur.execute(
        """
        SELECT odd_oferecida, resultado, COUNT(*)
        FROM historico_recomendacoes
        WHERE resultado IN ('acertou', 'errou')
        GROUP BY odd_oferecida, resultado
        """
    )
    faixas = {}
    for odd, resultado, contagem in cur.fetchall():
        indice = _indice_faixa_odd(odd)
        faixas.setdefault(indice, {"acertou": 0, "errou": 0})
        faixas[indice][resultado] += contagem

    detalhamento = []
    for indice, dados in sorted(faixas.items(), reverse=True):
        total = dados["acertou"] + dados["errou"]
        taxa = round(100 * dados["acertou"] / total, 1) if total else 0.0
        inicio, fim, rotulo = _rotular_faixa_odd(indice)
        detalhamento.append({
            "indice": indice,
            "inicio": inicio,
            "fim": fim,
            "rotulo": rotulo,
            "acertou": dados["acertou"],
            "errou": dados["errou"],
            "total": total,
            "taxa": taxa,
        })

    melhor_faixa = None
    for item in detalhamento:
        if item["total"] == 0:
            continue
        if melhor_faixa is None or item["taxa"] > melhor_faixa["taxa"]:
            melhor_faixa = {
                "rotulo": item["rotulo"], "taxa": item["taxa"], "total": item["total"],
            }

    return {"detalhamento": detalhamento, "melhor_faixa": melhor_faixa}


def buscar_detalhamento_faixa_odd(cur, indice_faixa):
    """Detalhamento completo de uma faixa de odd específica - mesmo
    formato de buscar_detalhamento_faixa, usado pelo endpoint AJAX do
    card de linha de odd. Só individual (historico_recomendacoes) - não
    entra nenhuma múltipla, igual o card de probabilidade."""
    if indice_faixa >= INDICE_ODD_TETO:
        condicao = "h.odd_oferecida >= %s"
        params = (Decimal(INDICE_ODD_TETO) / Decimal(10),)
    elif indice_faixa <= INDICE_ODD_MINIMO:
        condicao = "h.odd_oferecida < %s"
        params = (Decimal(INDICE_ODD_MINIMO) / Decimal(10) + FAIXA_ODD_LARGURA,)
    else:
        inicio_decimal = Decimal(indice_faixa) / Decimal(10)
        fim_decimal = inicio_decimal + FAIXA_ODD_LARGURA
        condicao = "h.odd_oferecida >= %s AND h.odd_oferecida < %s"
        params = (inicio_decimal, fim_decimal)

    cur.execute(
        f"""
        SELECT h.data_jogo, t.nome, j.adversario, h.descricao, h.casa_aposta,
               h.odd_oferecida, h.probabilidade_historica, h.valor_esperado, h.resultado
        FROM historico_recomendacoes h
        JOIN jogos j ON j.id = h.jogo_id
        JOIN times t ON t.id = j.nosso_time_id
        WHERE h.resultado IN ('acertou', 'errou')
          AND {condicao}
        ORDER BY h.odd_oferecida DESC, h.id DESC
        """,
        params,
    )
    colunas = ["data_jogo", "nosso_time", "adversario", "descricao", "casa_aposta",
               "odd_oferecida", "probabilidade_historica", "valor_esperado", "resultado"]
    return [dict(zip(colunas, row)) for row in cur.fetchall()]


# NOVO: piso de probabilidade histórica pra uma múltipla aparecer na lista
# de "múltiplas em destaque" do /historico. Só controla ESSA lista - não
# afeta a página principal (onde as odds são geradas, sem piso nenhum) nem
# o botão de salvar aposta (que funciona em qualquer probabilidade). Existe
# só pra evitar que combinações de chance muito baixa (que erram na maioria
# das vezes só por natureza estatística, mesmo estando matematicamente
# corretas) dominem essa lista de referência.
def montar_resumo_multiplas(cur):
    """Taxa de acerto entre TODAS as Múltiplas em Destaque já avaliadas
    (top-5 de cada jogo, calculado por
    arquivar_recomendacoes.py/selecionar_top5_do_jogo) - inclui também as
    que já foram comprimidas (perderam descrição/odd/casa, mas o
    resultado continua contando pra esse percentual). De propósito NUNCA
    somado com montar_resumo_historico(itens): essa lista é pré-filtrada
    (só as 5 melhores de cada jogo por probabilidade histórica), então é
    uma amostra enviesada pra cima - misturar com a taxa de acerto geral
    daria um número mais bonito, mas enganoso.

    NOVO (26/08/2026 - correção de duplicata): uma combinação que cruza 2
    jogos gera 2 LINHAS em historico_multiplas_destaque, de propósito (uma
    por jogo - ver migrar_assinatura_multiplas_destaque.py). Sem isso, ela
    contava 2x nesse resumo, inflando acertou/errou/taxa. `COUNT(DISTINCT
    COALESCE(assinatura, 'id-'||id))` conta cada combinação UMA vez: linhas
    com a MESMA assinatura colapsam numa só; linhas sem assinatura ainda
    (não passaram pelo backfill) cada uma conta por si - nunca colide com
    outra linha sem assinatura só porque as duas são NULL."""
    cur.execute(
        """
        SELECT resultado, COUNT(DISTINCT COALESCE(assinatura, 'id-' || id::text))
        FROM historico_multiplas_destaque
        WHERE resultado IN ('acertou', 'errou')
        GROUP BY resultado
        """
    )
    contagem = dict(cur.fetchall())
    acertou = contagem.get("acertou", 0)
    errou = contagem.get("errou", 0)
    total_avaliado = acertou + errou
    taxa = round(100 * acertou / total_avaliado, 1) if total_avaliado else 0
    return {"acertou": acertou, "errou": errou, "taxa": taxa}


def buscar_multiplas_destaque(cur):
    """NOVO: lê o top-5 de cada jogo já concluído, calculado de verdade
    ANTES do apito inicial (não é mais uma reconstrução hipotética depois
    do fato - ver arquivar_recomendacoes.py/selecionar_top5_do_jogo).
    Só as linhas AINDA com detalhe completo aparecem como card (as
    comprimidas, fora da janela de rodadas recentes, não têm mais
    descrição/odd pra mostrar - elas só entram no resumo agregado de
    montar_resumo_multiplas, lido direto do banco).

    NOVO (26/08/2026 - correção de duplicata): mesma correção de
    montar_resumo_multiplas, aplicada à lista detalhada - DISTINCT ON
    (COALESCE(assinatura, 'id-'||id)) devolve só UMA linha por combinação
    real, mesmo que ela exista fisicamente 2x (cruzando 2 jogos). Escolhe
    a linha de menor id como representante - tanto faz qual das 2, o
    conteúdo (descrição/odd/jogos/probabilidade) é idêntico nas duas por
    construção, o campo `jogos` já lista os jogos envolvidos."""
    cur.execute(
        """
        SELECT jogo_id, rodada, casa_aposta, descricao, odd_combinada, jogos,
               probabilidade_combinada, resultado
        FROM (
            SELECT DISTINCT ON (COALESCE(assinatura, 'id-' || id::text))
                   jogo_id, rodada, casa_aposta, descricao, odd_combinada, jogos,
                   probabilidade_combinada, resultado, id
            FROM historico_multiplas_destaque
            WHERE casa_aposta IS NOT NULL
            ORDER BY COALESCE(assinatura, 'id-' || id::text), id
        ) dedup
        ORDER BY probabilidade_combinada DESC
        """
    )
    detalhadas = []
    for jogo_id, rodada, casa, descricao, odd, jogos, prob, resultado in cur.fetchall():
        odd = float(odd)
        prob_pct = float(prob)
        detalhadas.append({
            "jogo_id": jogo_id,
            "rodada": rodada,
            "casa_aposta": casa,
            "descricao": descricao,
            "odd_combinada": odd,
            "jogos": jogos,
            "probabilidade_combinada": prob_pct,
            "valor_esperado": round((prob_pct / 100 * odd) - 1, 3),
            "resultado": resultado,
        })
    return detalhadas


@app.route("/historico")
def historico():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        itens = buscar_historico(cur)
        calibracao = buscar_calibracao(cur)
        calibracao_odd = buscar_calibracao_odd(cur)
        todas_rodadas = buscar_rodadas_ordenadas(cur)
        blocos_rodadas, tem_mais_rodadas = montar_blocos_rodadas(
            cur, todas_rodadas, 0, QUANTIDADE_RODADAS_POR_CARGA
        )
        multiplas_destaque = buscar_multiplas_destaque(cur)
        resumo_multiplas = montar_resumo_multiplas(cur)
        multiplas_acertou = [c for c in multiplas_destaque if c["resultado"] == "acertou"]
        multiplas_errou = [c for c in multiplas_destaque if c["resultado"] == "errou"]
        multiplas_pendente = [c for c in multiplas_destaque if c["resultado"] == "pendente"]
        banca_atual = buscar_banca(cur, session["usuario_id"])
        # REDESIGN (18/10/2026): escudo ao lado do nome do time, como na
        # página de Recomendações (nome do time -> id na API-Football)
        cur.execute("SELECT nome, api_football_team_id FROM times WHERE api_football_team_id IS NOT NULL")
        escudos = {nome: api_id for nome, api_id in cur.fetchall()}
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
        resumo=resumo, calibracao=calibracao, calibracao_odd=calibracao_odd,
        blocos_rodadas=blocos_rodadas, tem_mais_rodadas=tem_mais_rodadas,
        multiplas_destaque=multiplas_destaque, resumo_multiplas=resumo_multiplas,
        multiplas_acertou=multiplas_acertou, multiplas_errou=multiplas_errou,
        multiplas_pendente=multiplas_pendente, escudos=escudos,
        nav_html=barra_navegacao_nova("historico", round(banca_atual, 2)),
    )


@app.before_request
def exigir_login():
    """NOVO: protege o app INTEIRO (não só ROI/apostas) - qualquer página,
    sem estar logado, redireciona pro login. Exceções: a própria página de
    login, e o proxy de escudo (é só uma imagem pública, sem dado
    pessoal)."""
    rotas_livres = ("login", "api_consulta.consultar", "inicio", "numeros_home", "bilhetes_home", "favicon")
    if request.endpoint in rotas_livres or (request.endpoint or "").startswith("escudo"):
        return
    if request.endpoint == "static":
        return
    if "usuario_id" not in session:
        # 14/10/2026: quem abre o endereço principal sem login cai na página
        # inicial pública (o planeta), não no login.
        if request.endpoint == "index":
            return redirect(url_for("inicio"))
        return redirect(url_for("login"))


# NOVO (30/09/2026 - seletor de liga, SÓ VISUAL). Registrado DEPOIS do
# `exigir_login`, então só roda para quem já está logado.
PAGINA_LIGA_NAO_IMPLEMENTADA = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <link rel="icon" href="/static/marca/favicon.svg" type="image/svg+xml">
    <link rel="icon" href="/static/marca/favicon-32.png" sizes="32x32" type="image/png">
    <link rel="apple-touch-icon" href="/static/marca/apple-touch-icon.png">
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ liga_nome }} - Achismo Zero</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
            background: #0d1117; color: #e6edf3; max-width: 1500px;
            margin: 0 auto; padding: 32px 24px 80px;
        }
        h1 { font-size: 1.5rem; margin: 0 0 4px; text-align: center; }
        .subtitulo { color: #8b949e; margin: 0 0 16px; font-size: 0.88rem; text-align: center; }
        """ + NAV_CSS + """
    </style>
</head>
<body>
    <h1>{{ titulo_aba }}</h1>
    <p class="subtitulo">{{ liga_nome }}</p>
    {{ nav_html|safe }}
    <div class="liga-aviso">
        {{ bandeira|safe }}
        <h2>{{ liga_nome }} ainda não está implementada</h2>
        <p>Por enquanto só o Brasileirão tem dados. Esta liga ainda não foi
        coletada no banco, então esta aba não tem o que mostrar.
        <br>"Minhas Apostas (ROI)" continua funcionando normalmente.</p>
        <a class="liga-aviso-botao" href="/liga/brasileirao?voltar={{ voltar }}">Voltar ao Brasileirão</a>
    </div>
</body>
</html>
"""


@app.route("/liga/<chave>")
def trocar_liga(chave):
    """Grava a liga escolhida na sessão e volta para a aba de onde veio.
    Chave desconhecida é ignorada (continua a liga que estava)."""
    if chave in LIGAS_POR_CHAVE:
        session["liga"] = chave
    return redirect(_caminho_local_seguro(request.args.get("voltar")))


# ---------- 14/10/2026: página inicial pública (planeta das ligas) ----------
#
# O HTML mora em paginas/inicio.html (gerado a partir do protótipo) e as
# texturas do planeta em static/inicio/. A página é pública; o único dado
# que o servidor injeta é se a pessoa está logada, para o clique num país ir
# direto à liga (logado) ou ao login com ?next= (não logado).
CAMINHO_PAGINA_INICIO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paginas", "inicio.html")
_pagina_inicio_cache = {"mtime": None, "html": None}


def _ler_pagina_inicio():
    mtime = os.path.getmtime(CAMINHO_PAGINA_INICIO)
    if _pagina_inicio_cache["mtime"] != mtime:
        with open(CAMINHO_PAGINA_INICIO, encoding="utf-8") as f:
            _pagina_inicio_cache["html"] = f.read()
        _pagina_inicio_cache["mtime"] = mtime
    return _pagina_inicio_cache["html"]


@app.route("/inicio")
def inicio():
    sessao = json.dumps({"logado": "usuario_id" in session})
    html = _ler_pagina_inicio().replace("__SESSAO__", sessao, 1)
    resposta = Response(html, mimetype="text/html")
    resposta.headers["Cache-Control"] = "no-store"  # muda com o login
    return resposta


# Números da página inicial, contados no banco. Cache de 15 min por processo:
# a página é pública e não deve gerar consulta a cada visita.
NUMEROS_HOME_VALIDADE_S = 900
_numeros_home_cache = {"quando": 0.0, "dados": None}
_numeros_home_trava = threading.Lock()

# (código do país na página, schema). Nomes fixos aqui, nunca vindos da URL.
SCHEMAS_NUMEROS_HOME = (
    ("BRA", "public"), ("ENG", "liga_premier"), ("ESP", "liga_laliga"),
    ("ITA", "liga_seriea"), ("DEU", "liga_bundesliga"), ("FRA", "liga_ligue1"),
)

# Cada perspectiva (linha de `jogos`) é um time num jogo: somar por
# nosso_time_id dá a tabela. Temporada atual = ano do último jogo com placar
# (Brasileirão) ou a maior `temporada` (ligas). Jogo sem placar fica fora.
# 21/10/2026: além de pontos e jogos, V-E-D, gols pró/contra, a forma (os 5 últimos,
# do mais recente para o mais antigo) e o artilheiro do time na temporada (gols
# marcados PELO time: `gols.lado = 'mandante'` é o time da perspectiva, §5.2).
SQL_LIDERES_HOME = """
    WITH b AS (
        SELECT j.id, j.nosso_time_id AS t, j.data_jogo AS d,
               j.placar_corinthians AS gp, j.placar_adversario AS gc
        FROM {s}.jogos j
        WHERE j.placar_corinthians IS NOT NULL AND j.placar_adversario IS NOT NULL AND {filtro}
    ), tab AS (
        SELECT t, SUM(CASE WHEN gp > gc THEN 3 WHEN gp = gc THEN 1 ELSE 0 END) AS pts, COUNT(*) AS jg,
               SUM(CASE WHEN gp > gc THEN 1 ELSE 0 END) AS v, SUM(CASE WHEN gp = gc THEN 1 ELSE 0 END) AS e,
               SUM(CASE WHEN gp < gc THEN 1 ELSE 0 END) AS dd, SUM(gp) AS gp, SUM(gc) AS gc
        FROM b GROUP BY t
    ), topo AS (
        SELECT tab.*, tm.nome FROM tab JOIN {s}.times tm ON tm.id = tab.t
        ORDER BY pts DESC, gp - gc DESC, gp DESC, tm.nome LIMIT 5
    ), forma AS (
        SELECT t, LEFT(string_agg(CASE WHEN gp > gc THEN 'V' WHEN gp = gc THEN 'E' ELSE 'D' END, ''
                                  ORDER BY d DESC, id DESC), 20) AS f
        FROM b WHERE t IN (SELECT t FROM topo) GROUP BY t
    ), art AS (
        SELECT DISTINCT ON (b.t) b.t, jg.nome, COUNT(*) AS n
        FROM b
        JOIN {s}.gols g ON g.jogo_id = b.id AND g.lado = 'mandante' AND g.gol_contra IS NOT TRUE
        JOIN {s}.jogadores jg ON jg.id = g.jogador_id
        WHERE b.t IN (SELECT t FROM topo)
        GROUP BY b.t, jg.id, jg.nome
        ORDER BY b.t, COUNT(*) DESC, jg.nome
    )
    SELECT topo.nome, topo.pts, topo.jg, topo.v, topo.e, topo.dd, topo.gp, topo.gc,
           COALESCE(forma.f, ''), art.nome, art.n
    FROM topo LEFT JOIN forma ON forma.t = topo.t LEFT JOIN art ON art.t = topo.t
    ORDER BY topo.pts DESC, topo.gp - topo.gc DESC, topo.gp DESC, topo.nome
"""

# Odds por jogo da casa em uso (a da odd mais recente), contando o jogo por fixture.
SQL_ODDS_POR_JOGO_HOME = """
    SELECT ROUND(COUNT(*)::numeric / NULLIF(COUNT(DISTINCT j.fixture_id_api), 0))
    FROM odds o JOIN jogos j ON j.id = o.jogo_id
    WHERE o.casa_aposta = (SELECT casa_aposta FROM odds ORDER BY coletado_em DESC LIMIT 1)
"""
# "Quando o sistema deu entre 70% e 80%, aconteceu em X%": a rodada mais recente
# (2026) com pelo menos 20 recomendações arquivadas nessa faixa.
SQL_CALIBRACAO_HOME = """
    WITH h AS (
        SELECT j.rodada_numero AS rn, h.resultado AS r
        FROM historico_recomendacoes h JOIN jogos j ON j.id = h.jogo_id
        WHERE h.data_jogo >= date_trunc('year', CURRENT_DATE)::date
          AND h.resultado IN ('acertou', 'errou')
          AND h.probabilidade_historica >= 70 AND h.probabilidade_historica < 80
    )
    SELECT rn, COUNT(*), ROUND(100.0 * AVG(CASE WHEN r = 'acertou' THEN 1 ELSE 0 END))
    FROM h GROUP BY rn HAVING COUNT(*) >= 20
    ORDER BY rn DESC LIMIT 1
"""
FILTRO_TEMPORADA_BRASILEIRAO = ("j.data_jogo >= date_trunc('year', (SELECT MAX(data_jogo) FROM public.jogos "
                                "WHERE placar_corinthians IS NOT NULL))::date")
FILTRO_TEMPORADA_LIGA = "j.temporada = (SELECT MAX(temporada) FROM {s}.jogos)"


def _contar_numeros_home():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        conn.set_session(readonly=True)
        cur = conn.cursor()
        jogos = 0
        lideres = {}
        for pais, s in SCHEMAS_NUMEROS_HOME:
            # um jogo = duas linhas em `jogos`: conta por fixture_id_api
            cur.execute(f"SELECT COUNT(DISTINCT fixture_id_api) FROM {s}.jogos WHERE placar_corinthians IS NOT NULL")
            jogos += cur.fetchone()[0] or 0
            filtro = FILTRO_TEMPORADA_BRASILEIRAO if s == "public" else FILTRO_TEMPORADA_LIGA.format(s=s)
            cur.execute(SQL_LIDERES_HOME.format(s=s, filtro=filtro))
            lideres[pais] = [[nome, int(pts), int(jg), int(v), int(e), int(d), int(gp), int(gc), forma or "",
                              art, int(n_art) if n_art is not None else None]
                             for nome, pts, jg, v, e, d, gp, gc, forma, art, n_art in cur.fetchall()]
        cur.execute("SELECT COUNT(*) FROM odds")
        odds = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM historico_recomendacoes")
        recomendacoes = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM multiplas_candidatas WHERE avaliada AND resultado IN ('acertou', 'errou')")
        multiplas = cur.fetchone()[0]
        cur.execute(SQL_ODDS_POR_JOGO_HOME)
        odds_por_jogo = cur.fetchone()[0]
        previsoes = 0
        cur.execute("SELECT to_regclass('public.registro_modelo') IS NOT NULL")
        if cur.fetchone()[0]:
            cur.execute("SELECT COUNT(*) FROM registro_modelo")
            previsoes = cur.fetchone()[0]
        cur.execute(SQL_CALIBRACAO_HOME)
        linha = cur.fetchone()
        calibracao = ({"faixa": "70-80", "rodada": int(linha[0]), "n": int(linha[1]), "pct": int(linha[2])}
                      if linha else None)
        cur.close()
        conn.rollback()
    finally:
        conn.close()
    return {
        "atualizado": datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="minutes"),
        "jogos": int(jogos), "odds": int(odds),
        "recomendacoes": int(recomendacoes), "multiplas": int(multiplas),
        "odds_por_jogo": int(odds_por_jogo or 0), "previsoes": int(previsoes),
        "calibracao": calibracao,
        "lideres": lideres,
    }


@app.route("/api/numeros-home")
def numeros_home():
    agora = datetime.now(timezone.utc).timestamp()
    with _numeros_home_trava:
        if _numeros_home_cache["dados"] is None or agora - _numeros_home_cache["quando"] > NUMEROS_HOME_VALIDADE_S:
            try:
                _numeros_home_cache["dados"] = _contar_numeros_home()
                _numeros_home_cache["quando"] = agora
            except Exception as erro:  # a página tem os números fixos como reserva
                print(f"[numeros-home] falhou: {erro!r}", file=sys.stderr)
                if _numeros_home_cache["dados"] is None:
                    return jsonify({"erro": "indisponivel"}), 503
        dados = _numeros_home_cache["dados"]
    resposta = jsonify(dados)
    resposta.headers["Cache-Control"] = "public, max-age=300"
    return resposta


# ---------- 20/10/2026: bilhetes da página inicial ("Múltiplas que acertaram") ----------
#
# Rota pública com as múltiplas que acertaram na última rodada avaliada: uma
# por jogo (a de maior odd entre as que têm 2+ pernas e incluem o jogo), mais
# o total da rodada (acertos, avaliadas, retorno), que a página mostra ao lado
# para não parecer que toda múltipla acerta. Só leitura, cache de 15 min.
# "Rodada" = rodada_numero do jogo da múltipla avaliada mais recente; entram
# as múltiplas cujas pernas são TODAS de jogos dessa rodada com placar.
BILHETES_HOME_VALIDADE_S = 900
_bilhetes_home_cache = {"quando": 0.0, "dados": None}
_bilhetes_home_trava = threading.Lock()
BILHETES_HOME_MAXIMO = 10

# nome de exibição e sigla por id da API-Football (o banco guarda o nome da API)
NOMES_EXIBICAO_TIMES = {
    127: ("Flamengo", "FLA"), 121: ("Palmeiras", "PAL"), 134: ("Athletico-PR", "CAP"),
    1062: ("Atlético-MG", "CAM"), 147: ("Coritiba", "CFC"), 120: ("Botafogo", "BOT"),
    132: ("Chapecoense", "CHA"), 1198: ("Remo", "REM"), 794: ("Bragantino", "RBB"),
    118: ("Bahia", "BAH"), 124: ("Fluminense", "FLU"), 128: ("Santos", "SAN"),
    126: ("São Paulo", "SAO"), 133: ("Vasco", "VAS"), 130: ("Grêmio", "GRE"),
    135: ("Cruzeiro", "CRU"), 131: ("Corinthians", "COR"), 136: ("Vitória", "VIT"),
    7848: ("Mirassol", "MIR"), 119: ("Internacional", "INT"),
}


def _nome_exibicao(api_id, nome_banco):
    if api_id in NOMES_EXIBICAO_TIMES:
        return NOMES_EXIBICAO_TIMES[api_id]
    base = unicodedata.normalize("NFKD", nome_banco or "").encode("ascii", "ignore").decode()
    return nome_banco, (re.sub(r"[^A-Za-z]", "", base)[:3] or "???").upper()


def _perna_curta(descricao, nomes, mandante, visitante):
    """Descrição da perna em português curto, para o bilhete. O que não
    tiver forma conhecida fica com a descrição original, sem os parênteses
    de ajuste."""
    d = re.sub(r"\s*\([^)]*\)", "", descricao or "").strip()
    nm = lambda s: nomes.get(s.strip(), s.strip())
    n = lambda s: s.replace(".", ",")
    regras = (
        (r"Resultado Final - Vitória do (.+)$", lambda m: f"Vitória do {nm(m[1])}"),
        (r"Handicap - (.+) ([+-][\d.]+)$", lambda m: f"{nm(m[1])} {n(m[2])} (handicap)"),
        (r"Gols do Time - (.+) - (Mais|Menos) de ([\d.]+)$", lambda m: f"{nm(m[1])}: {m[2].lower()} de {n(m[3])} gols"),
        (r"Gols Total do Jogo - (Mais|Menos) de ([\d.]+)$", lambda m: f"{m[1]} de {n(m[2])} gols no jogo"),
        (r"Escanteios Total do Jogo - (Mais|Menos) de ([\d.]+)$", lambda m: f"{m[1]} de {n(m[2])} escanteios no jogo"),
        (r"Escanteios - Mais/Menos (.+) - (Mais|Menos) de ([\d.]+)$", lambda m: f"{nm(m[1])}: {m[2].lower()} de {n(m[3])} escanteios"),
        (r"Cartões Total do Jogo - (Mais|Menos) de ([\d.]+)$", lambda m: f"{m[1]} de {n(m[2])} cartões no jogo"),
        (r"Cartões - Mais/Menos (.+) - (Mais|Menos) de ([\d.]+)$", lambda m: f"{nm(m[1])}: {m[2].lower()} de {n(m[3])} cartões"),
        (r"Ambas Marcam (Primeiro|Segundo) Tempo - (Sim|Não)$",
         lambda m: f"Ambas marcam no {'1º' if m[1] == 'Primeiro' else '2º'} tempo: {m[2].lower()}"),
        (r"Dupla Chance (Primeiro|Segundo) Tempo - (1X|X2|12)$",
         lambda m: ("1º" if m[1] == "Primeiro" else "2º") + " tempo: " +
                   {"1X": f"{mandante} ou empate", "X2": f"{visitante} ou empate", "12": "sem empate"}[m[2]]),
    )
    for padrao, monta in regras:
        m = re.match(padrao, d)
        if m:
            return monta(m)
    for banco, exib in nomes.items():
        d = d.replace(banco, exib)
    return re.sub(r"(\d)\.(\d)", r"\1,\2", d)


def _montar_bilhetes_home():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        conn.set_session(readonly=True)
        cur = conn.cursor()
        cur.execute("""
            SELECT jl.temporada, jl.rodada_numero
            FROM multiplas_candidatas m
            CROSS JOIN LATERAL jsonb_array_elements(m.pernas) p
            JOIN jogos_liga jl ON jl.fixture_id_api = (p->>'fixture_id_api')::bigint
            WHERE m.avaliada AND m.resultado IN ('acertou', 'errou')
            ORDER BY m.primeiro_apito DESC
            LIMIT 1
        """)
        linha = cur.fetchone()
        if not linha:
            return {"multiplas": []}
        temporada, rodada = linha
        cur.execute("""
            SELECT fixture_id_api, mandante_api_id, visitante_api_id, mandante_nome, visitante_nome,
                   placar_mandante, placar_visitante, data_jogo
            FROM jogos_liga
            WHERE temporada = %s AND rodada_numero = %s
              AND placar_mandante IS NOT NULL AND placar_visitante IS NOT NULL
            ORDER BY data_jogo, fixture_id_api
        """, (temporada, rodada))
        jogos = {}
        ordem = []
        for fx, ida, idb, na, nb, ga, gb, dia in cur.fetchall():
            ea, eb = _nome_exibicao(ida, na), _nome_exibicao(idb, nb)
            jogos[int(fx)] = [ea[0], eb[0], ida, idb, int(ga), int(gb), ea[1], eb[1], dia]
            ordem.append(int(fx))
        if not jogos:
            return {"multiplas": []}
        datas = [j[8] for j in jogos.values()]
        cur.execute("SELECT nome, api_football_team_id FROM times WHERE api_football_team_id IS NOT NULL")
        nomes = {nome: _nome_exibicao(api, nome)[0] for nome, api in cur.fetchall()}
        cur.execute("""
            SELECT id, odd_combinada, probabilidade_combinada, resultado, casa_aposta, pernas
            FROM multiplas_candidatas
            WHERE avaliada AND resultado IN ('acertou', 'errou')
              AND primeiro_apito >= %s::date - 1 AND primeiro_apito < %s::date + 2
        """, (min(datas), max(datas)))
        da_rodada = []
        for mid, odd, prob, res, casa, pernas in cur.fetchall():
            if isinstance(pernas, str):
                pernas = json.loads(pernas)
            fxs = [int(p.get("fixture_id_api") or 0) for p in pernas]
            if pernas and all(fx in jogos for fx in fxs):
                da_rodada.append((int(mid), float(odd), float(prob), res, casa, pernas, fxs))
        cur.close()
        conn.rollback()
    finally:
        conn.close()

    acertos = [m for m in da_rodada if m[3] == "acertou"]
    retorno = sum((m[1] - 1) if m[3] == "acertou" else -1 for m in da_rodada)
    escolhidas, vistos = [], set()
    for fx in ordem:
        cands = [m for m in acertos if fx in m[6] and len(m[5]) >= 2 and m[0] not in vistos]
        if not cands:
            continue
        m = max(cands, key=lambda x: (x[1], -x[0]))
        vistos.add(m[0])
        escolhidas.append({
            "id": m[0], "odd": round(m[1], 2), "prob": round(m[2], 2), "casa": m[4],
            "pernas": [[fx_p, _perna_curta(p.get("descricao"), nomes, jogos[fx_p][0], jogos[fx_p][1])]
                       for p, fx_p in zip(m[5], m[6])],
        })
        if len(escolhidas) >= BILHETES_HOME_MAXIMO:
            break
    usados = {fx for e in escolhidas for fx, _ in e["pernas"]}
    return {
        "atualizado": datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="minutes"),
        "rodada": int(rodada),
        "total": {"acertou": len(acertos), "avaliadas": len(da_rodada),
                  "roi": round(retorno / len(da_rodada) * 100, 1) if da_rodada else 0.0},
        "jogos": {str(fx): jogos[fx][:8] for fx in usados},
        "multiplas": escolhidas,
    }


@app.route("/api/bilhetes-home")
def bilhetes_home():
    agora = datetime.now(timezone.utc).timestamp()
    with _bilhetes_home_trava:
        if _bilhetes_home_cache["dados"] is None or agora - _bilhetes_home_cache["quando"] > BILHETES_HOME_VALIDADE_S:
            try:
                _bilhetes_home_cache["dados"] = _montar_bilhetes_home()
                _bilhetes_home_cache["quando"] = agora
            except Exception as erro:  # sem dado, a página esconde a seção
                print(f"[bilhetes-home] falhou: {erro!r}", file=sys.stderr)
                if _bilhetes_home_cache["dados"] is None:
                    return jsonify({"erro": "indisponivel"}), 503
        dados = _bilhetes_home_cache["dados"]
    resposta = jsonify(dados)
    resposta.headers["Cache-Control"] = "public, max-age=300"
    return resposta

@app.context_processor
def injetar_liga_atual():
    """Nome da liga escolhida, disponível em todo template (usado no
    subtítulo do Gerador)."""
    if not session.get("usuario_id"):
        return {}
    return {"liga_nome_atual": LIGAS_POR_CHAVE[liga_atual()][0]}


@app.before_request
def bloquear_liga_nao_implementada():
    """Liga sem dado no banco: as abas que dependem de liga mostram o aviso
    no lugar do conteúdo. A função da página NEM É CHAMADA - então nenhuma
    consulta, geração de recomendação ou busca de odds acontece para uma
    liga não implementada. Brasileirão passa direto, sem mudança."""
    if request.endpoint not in ENDPOINTS_POR_LIGA or "usuario_id" not in session:
        return None
    chave = liga_atual()
    nome, implementada, svg = LIGAS_POR_CHAVE[chave]
    if implementada:
        return None

    chave_nav, titulo_aba = ENDPOINTS_POR_LIGA[request.endpoint]
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        banca_atual = buscar_banca(cur, session["usuario_id"])
        cur.close()
    finally:
        conn.close()

    return render_template_string(
        PAGINA_LIGA_NAO_IMPLEMENTADA,
        liga_nome=nome, bandeira=svg, titulo_aba=titulo_aba,
        voltar=quote(request.path or "/", safe="/"),
        nav_html=barra_navegacao(chave_nav, round(banca_atual, 2)),
    )


PAGINA_LOGIN = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <link rel="icon" href="/static/marca/favicon.svg" type="image/svg+xml">
    <link rel="icon" href="/static/marca/favicon-32.png" sizes="32x32" type="image/png">
    <link rel="apple-touch-icon" href="/static/marca/apple-touch-icon.png">
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - Achismo Zero</title>
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
    <h1>Achismo Zero</h1>
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

    session.permanent = True  # dura app.permanent_session_lifetime (90 dias)
    session["usuario_id"] = row[0]
    session["usuario_nome"] = row[1]
    # 14/10/2026: a página inicial manda para o login com ?next= (ex.: a liga
    # clicada no planeta). O form não tem action, então o POST mantém a query.
    # Só aceita caminho do próprio site.
    return redirect(_caminho_local_seguro(request.args.get("next")))


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/inicio")


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

        # NOVO (26/08/2026): trava de idempotência contra duplo-clique/
        # duplo-submit no botão "Salvar aposta" - o formulário não tinha
        # NENHUMA proteção (nem debounce no JS, nem checagem no servidor),
        # e um clique duplo criava 2 linhas idênticas em apostas_salvas,
        # descontando a banca 2x pra uma aposta real só. Se já existe uma
        # aposta do MESMO usuário, com a MESMA descrição/odd/valor/pernas,
        # criada nos últimos SEGUNDOS_JANELA_DUPLICATA segundos, trata
        # como o mesmo clique reenviado e não insere de novo - só avisa e
        # volta pra tela. Janela curta de propósito: uma aposta legítima
        # repetida minutos depois (o usuário decidiu apostar de novo no
        # mesmo mercado) deve passar normalmente.
        cur.execute(
            """SELECT id FROM apostas_salvas
               WHERE usuario_id = %s AND descricao = %s AND casa_aposta = %s
                 AND odd_combinada = %s AND valor_apostado = %s AND pernas = %s::jsonb
                 AND criado_em > NOW() - (%s || ' seconds')::interval
               ORDER BY id DESC LIMIT 1""",
            (session["usuario_id"], descricao, casa_aposta, odd_combinada,
             valor_apostado, pernas_json, SEGUNDOS_JANELA_DUPLICATA),
        )
        se_ja_existe = cur.fetchone()
        if se_ja_existe:
            cur.close()
            flash(
                "Essa aposta já tinha sido salva agora há pouco (clique duplicado "
                "ignorado) - confira em Minhas apostas.",
                "sucesso",
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


@app.route("/corrigir-aposta-manual", methods=["POST"])
def corrigir_aposta_manual():
    """NOVO (26/08/2026): correção manual pra aposta JÁ RESOLVIDA (acertou/
    errou) - não serve pra pendente, que já tem o cancelamento normal via
    /cancelar-aposta. Existe pra dois casos que o casamento automático
    contra historico_recomendacoes não cobre: (1) uma duplicata de
    duplo-clique que já "terminou o jogo" e não pode mais ser cancelada
    pelo caminho antigo (ele só apaga pendente); (2) um resultado que o
    usuário sabe estar errado por um motivo que o dado automático nunca
    vai ter (ex: evento que a API-Football nunca gravou - seção 27).

    Três ações (`acao` no form): 'cancelar' (apaga a linha, como se a
    aposta nunca tivesse existido), 'acertou', 'errou' (troca o resultado
    e ajusta a banca pro valor certo). Toda aposta tocada aqui é marcada
    `resolvido_manualmente = TRUE` - fica IMUNE a
    reabrir_apostas_desatualizadas.py dali pra frente, porque uma decisão
    manual não deve ser desfeita sem avisar por uma reavaliação
    automática que rodar depois.

    Trava de banca: o efeito líquido de UMA aposta resolvida na banca,
    do momento em que foi salva até agora, é sempre exatamente o valor
    do campo `retorno` (lucro se acertou, -stake se errou - conferido:
    -stake + (stake+lucro) = lucro; -stake + 0 = -stake). Por isso
    zerar o efeito antigo é sempre `registrar_movimento_banca(..., -retorno_
    salvo, ...)`, e aplicar um novo resultado é só a diferença entre o
    retorno novo e o antigo numa tacada só - não precisa de dois
    movimentos separados (estorno + novo crédito)."""
    aposta_id = request.form.get("aposta_id")
    acao = request.form.get("acao")

    if acao not in ("cancelar", "acertou", "errou"):
        flash("Ação de correção inválida.", "erro")
        return redirect("/minhas-apostas")

    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()

        # Só mexe em aposta do próprio usuário, e só se ela já estiver
        # resolvida - pendente usa o fluxo de /cancelar-aposta de sempre.
        cur.execute(
            """SELECT resultado, retorno, valor_apostado, odd_combinada
               FROM apostas_salvas
               WHERE id = %s AND usuario_id = %s AND resultado IN ('acertou', 'errou')""",
            (aposta_id, session["usuario_id"]),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            flash(
                "Aposta não encontrada, não é sua, ou ainda está pendente "
                "(pendente cancela pelo botão normal).",
                "erro",
            )
            return redirect("/minhas-apostas")

        resultado_salvo, retorno_salvo, valor_apostado, odd_combinada = row
        retorno_salvo = float(retorno_salvo or 0)
        valor_apostado = float(valor_apostado)

        if acao == "cancelar":
            # Zera o efeito líquido que essa aposta teve na banca até
            # agora e apaga a linha - fica como se nunca tivesse existido.
            ajuste = round(-retorno_salvo, 2)
            if ajuste != 0:
                registrar_movimento_banca(cur, session["usuario_id"], "estorno_manual", ajuste, aposta_id)
            cur.execute(
                "DELETE FROM apostas_salvas WHERE id = %s AND usuario_id = %s",
                (aposta_id, session["usuario_id"]),
            )
            conn.commit()
            cur.close()
            flash("Aposta cancelada manualmente e banca ajustada.", "sucesso")
            return redirect("/minhas-apostas")

        # acao in ('acertou', 'errou'): recalcula o retorno pro resultado
        # NOVO e move só a diferença em relação ao que já estava aplicado.
        novo_resultado = acao
        if novo_resultado == "acertou":
            novo_retorno = round(valor_apostado * (float(odd_combinada) - 1), 2)
        else:
            novo_retorno = round(-valor_apostado, 2)

        ajuste = round(novo_retorno - retorno_salvo, 2)
        if ajuste != 0:
            registrar_movimento_banca(cur, session["usuario_id"], "correcao_manual", ajuste, aposta_id)

        cur.execute(
            """UPDATE apostas_salvas
               SET resultado = %s, retorno = %s, resolvido_em = NOW(),
                   resolvido_manualmente = TRUE
               WHERE id = %s AND usuario_id = %s""",
            (novo_resultado, novo_retorno, aposta_id, session["usuario_id"]),
        )
        conn.commit()
        cur.close()
        flash(f"Aposta corrigida manualmente pra '{novo_resultado}' e banca ajustada.", "sucesso")
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
    <link rel="icon" href="/static/marca/favicon.svg" type="image/svg+xml">
    <link rel="icon" href="/static/marca/favicon-32.png" sizes="32x32" type="image/png">
    <link rel="apple-touch-icon" href="/static/marca/apple-touch-icon.png">
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Minhas Apostas - Achismo Zero</title>
""" + FONTES_VISUAL_NOVO + """    <style>
        /* REDESIGN (18/10/2026): Minhas Apostas (ROI) no visual novo. Os
           formulários, rotas e campos são os de antes (/banca-movimento,
           /salvar-aposta pelo Criador de Odd, /cancelar-aposta,
           /corrigir-aposta-manual, /api/jogadores-jogo). Novos: o gráfico é
           desenhado na página na largura real (antes vinha pronto do
           servidor) e a lista de apostas tem filtro por resultado. */
        """ + NAV_CSS + """
""" + CSS_VISUAL_NOVO + """
        .conteudo { width: var(--degrau1); margin: 0 auto; }
        .rotulo { font-family: var(--f-corpo); font-size: 0.625rem; letter-spacing: 0; text-transform: none; color: var(--apagado); }
        .vidro { background: var(--vidro); border: 1px solid var(--vidro-borda); box-shadow: var(--vidro-sombra); border-radius: var(--raio); }
        .secao { margin-bottom: 3rem; }
        .secao-titulo { margin: 0 0 0.875rem; font-family: var(--f-corpo); font-weight: 300; font-size: clamp(1.25rem, 1.8vw, 1.75rem); letter-spacing: -.03em; line-height: 1.1; }
        .vazio { text-align: center; color: var(--apagado); padding: 1.75rem 1.25rem; font-size: 0.8125rem; background: rgba(3, 12, 32, .4); border: 1px dashed var(--linha); border-radius: var(--raio); }
        .pos { color: var(--ouro); }
        .neg { color: var(--azul-claro); }
        button {
            background: var(--ouro); color: var(--cta-texto); border: 1px solid transparent; border-radius: 999px;
            padding: 0.5rem 1rem; font-family: var(--f-corpo); font-size: 0.8125rem; font-weight: 600; cursor: pointer;
            transition: background .15s, transform .1s;
        }
        button:hover { background: var(--ouro-claro); }
        button:active { transform: translateY(1px); }
        .btn-contorno { background: transparent; color: var(--azul-claro); border-color: rgba(180, 200, 242, .5); }
        .btn-contorno:hover { background: rgba(180, 200, 242, .1); }
        input[type=number], select {
            width: 100%; padding: 0.5625rem 0.75rem; border-radius: 0.5rem;
            background: rgba(1, 5, 15, .55); border: 1px solid var(--linha); color: var(--texto);
            font-family: var(--f-corpo); font-size: 0.8125rem;
        }
        input[type=number] { font-family: var(--f-dado); }
        input[type=number]:focus, select:focus { outline: none; border-color: var(--azul-claro); box-shadow: 0 0 0 3px rgba(180, 200, 242, .18); }
        select option { background: #06122e; }
        .flash { padding: 0.625rem 0.875rem; border-radius: 0.625rem; margin-bottom: 1.25rem; font-size: 0.75rem; }
        .flash-erro { background: rgba(255, 122, 122, .12); color: var(--vermelho); border: 1px solid rgba(255, 122, 122, .4); }
        .flash-sucesso { background: rgba(95, 211, 141, .12); color: var(--verde); border: 1px solid rgba(95, 211, 141, .4); }

        .cabeca { margin: 0.5rem 0 1.75rem; }
        .cabeca h1 { margin: 0; font-family: var(--f-corpo); font-weight: 300; font-size: clamp(2rem, 3.2vw, 3rem); letter-spacing: -.035em; line-height: 1; }
        .cabeca p { margin: 0.5rem 0 0; color: var(--apagado); font-size: 0.8125rem; }

        /* ---------- banca + números ---------- */
        .topo-grade { display: grid; grid-template-columns: minmax(0, 22rem) minmax(0, 1fr); gap: 1.25rem; align-items: stretch; margin-bottom: 1.25rem; }
        .banca-box { padding: 1.25rem 1.375rem; display: flex; flex-direction: column; gap: 1rem; }
        .banca-valor { font-family: var(--f-dado); font-weight: 500; font-size: clamp(2.25rem, 3.4vw, 3.25rem); line-height: 1; margin-top: 0.5rem; font-variant-numeric: tabular-nums; }
        .banca-valor small { font-size: 0.45em; color: var(--apagado); margin-right: 0.375rem; }
        .banca-nota { font-size: 0.6875rem; color: var(--apagado); margin-top: 0.375rem; }
        .banca-botoes { display: flex; gap: 0.5rem; }
        .banca-botoes button { flex: 1; }
        .banca-forma { display: none; gap: 0.5rem; }
        .banca-forma.aberta { display: flex; }
        .banca-forma input { flex: 1; min-width: 0; }
        .numeros { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 0.75rem; }
        .numero { padding: 1rem 1.125rem; }
        .numero b { display: block; margin-top: 0.4375rem; font-family: var(--f-dado); font-weight: 500; font-size: clamp(1.375rem, 2vw, 1.875rem); line-height: 1; font-variant-numeric: tabular-nums; }
        .numero span.det { display: block; margin-top: 0.375rem; font-size: 0.6875rem; color: var(--apagado); }
        .numeros > .numero:nth-child(-n+3) { grid-column: span 2; }
        .numeros > .numero:nth-child(n+4) { grid-column: span 3; }

        /* ---------- gráfico ---------- */
        .grafico-box { padding: 1.125rem 1.25rem 0.875rem; margin-bottom: 1.25rem; }
        .grafico-topo { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; flex-wrap: wrap; margin-bottom: 0.625rem; }
        .grafico-topo h3 { margin: 0; font-size: 0.9375rem; font-weight: 600; }
        .grafico-topo span { font-size: 0.6875rem; color: var(--apagado); }
        #grafico { position: relative; height: 13rem; }
        #grafico svg { display: block; width: 100%; height: 100%; overflow: visible; }
        #grafico text { font-family: var(--f-dado); font-size: 10px; fill: var(--apagado); }
        .dica {
            position: absolute; pointer-events: none; transform: translate(-50%, -120%); white-space: nowrap;
            background: #06122e; border: 1px solid var(--vidro-borda); border-radius: 0.375rem; padding: 0.25rem 0.5rem;
            font-family: var(--f-dado); font-size: 0.6875rem; opacity: 0; transition: opacity .1s;
        }

        /* ---------- criador de odd + extrato ---------- */
        .faixa-acoes { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 1.25rem; align-items: start; margin-bottom: 3rem; }
        .criador-odd-box { padding: 1.125rem 1.25rem; display: flex; justify-content: space-between; align-items: center; gap: 1rem; flex-wrap: wrap; }
        .criador-odd-titulo { font-weight: 600; font-size: 0.9375rem; }
        .criador-odd-sub { color: var(--apagado); font-size: 0.75rem; margin-top: 0.25rem; max-width: 26rem; line-height: 1.5; }
        .extrato-box { padding: 0; overflow: hidden; }
        .extrato-titulo {
            width: 100%; display: flex; justify-content: space-between; align-items: center; gap: 1rem;
            padding: 1.125rem 1.25rem; background: none; border: 0; border-radius: 0; color: var(--texto);
            font-size: 0.9375rem; font-weight: 600; text-align: left;
        }
        .extrato-titulo:hover { background: rgba(255,255,255,.04); }
        .extrato-titulo small { font-weight: 400; font-size: 0.6875rem; color: var(--apagado); }
        .seta { width: 0.5rem; height: 0.5rem; border-right: 2px solid var(--apagado); border-bottom: 2px solid var(--apagado); transform: translateY(-0.125rem) rotate(45deg); transition: transform .2s; flex-shrink: 0; }
        .extrato-box.aberto .seta { transform: translateY(0.125rem) rotate(-135deg); }
        .extrato-lista { display: none; padding: 0 1.25rem 0.75rem; max-height: 22rem; overflow-y: auto; }
        .extrato-lista.aberta { display: block; }
        .extrato-linha { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 0.75rem; align-items: baseline; font-size: 0.75rem; padding: 0.4375rem 0; border-top: 1px solid var(--linha-fraca); }
        .extrato-linha .quando { font-family: var(--f-dado); font-size: 0.625rem; color: var(--apagado); margin-right: 0.5rem; }
        .extrato-linha b { font-family: var(--f-dado); font-weight: 600; text-align: right; }
        .extrato-linha .saldo { font-family: var(--f-dado); font-size: 0.6875rem; color: var(--apagado); text-align: right; min-width: 6.5rem; }

        /* ---------- apostas ---------- */
        .lista-topo { display: flex; justify-content: space-between; align-items: center; gap: 1rem; flex-wrap: wrap; margin-bottom: 0.875rem; }
        .lista-topo .secao-titulo { margin: 0; }
        .filtros { display: flex; gap: 0.25rem; padding: 0.25rem; border-radius: 999px; background: rgba(1, 5, 15, .45); border: 1px solid var(--linha-fraca); }
        .filtros button { background: none; color: var(--apagado); font-weight: 500; font-size: 0.75rem; padding: 0.3125rem 0.75rem; }
        .filtros button:hover { color: var(--texto); background: rgba(255,255,255,.05); }
        .filtros button.ativo { background: rgba(255, 210, 10, .14); color: var(--ouro); }
        .filtros small { font-family: var(--f-dado); margin-left: 0.25rem; opacity: .75; }
        .apostas { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 26rem), 1fr)); gap: 0.75rem; align-items: start; }
        .cartao { padding: 0.9375rem 1.0625rem 1rem; border-left: 3px solid var(--linha); }
        .cartao.r-acertou { border-left-color: var(--ouro); }
        .cartao.r-errou { border-left-color: rgba(180, 200, 242, .55); }
        .cartao-topo { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; }
        .quando { font-family: var(--f-dado); font-size: 0.6875rem; color: var(--apagado); }
        .selo { font-family: var(--f-corpo); font-size: 0.5625rem; letter-spacing: 0; text-transform: none; padding: 0.125rem 0.4375rem; border-radius: 999px; white-space: nowrap; }
        .selo-acertou { color: var(--cta-texto); background: var(--ouro); }
        .selo-errou { color: var(--azul-claro); border: 1px solid rgba(180, 200, 242, .5); }
        .selo-pendente { color: var(--apagado); border: 1px solid var(--linha); }
        .jogos-label { font-size: 0.6875rem; color: var(--azul-claro); margin-bottom: 0.3125rem; }
        .descricao { font-size: 0.8125rem; font-weight: 500; line-height: 1.45; margin-bottom: 0.625rem; }
        .metricas { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.5rem; padding: 0.625rem 0.75rem; border-radius: 0.5rem; background: rgba(1, 5, 15, .35); }
        .metricas span { font-size: 0.625rem; color: var(--apagado); }
        .metricas b { display: block; margin-top: 0.125rem; font-family: var(--f-dado); font-weight: 500; font-size: 0.875rem; color: var(--texto); font-variant-numeric: tabular-nums; }
        .metricas b.pos { color: var(--ouro); }
        .metricas b.neg { color: var(--azul-claro); }
        .casa { font-family: var(--f-corpo); font-size: 0.5625rem; letter-spacing: 0; text-transform: none; color: var(--apagado); margin-top: 0.5rem; }
        .salvar-linha { display: flex; gap: 0.5rem; align-items: center; margin-top: 0.75rem; flex-wrap: wrap; }
        .btn-pequeno { font-size: 0.6875rem; padding: 0.3125rem 0.75rem; }
        .btn-cancelar { background: transparent; color: var(--azul-claro); border-color: rgba(180, 200, 242, .5); }
        .btn-cancelar:hover { background: rgba(180, 200, 242, .1); }
        .btn-corrigir { background: transparent; color: var(--apagado); border-color: var(--linha); }
        .btn-corrigir:hover { background: rgba(255,255,255,.05); color: var(--texto); }
        .painel-corrigir { display: none; gap: 0.375rem; flex-wrap: wrap; margin-top: 0.5rem; padding: 0.625rem; border-radius: 0.5rem; background: rgba(1, 5, 15, .35); }
        .painel-corrigir.aberto { display: flex; }
        .btn-corrigir-opcao { font-size: 0.6875rem; padding: 0.3125rem 0.75rem; background: transparent; }
        .btn-corrigir-cancelar { color: var(--apagado); border-color: var(--linha); }
        .btn-corrigir-cancelar:hover { background: rgba(255,255,255,.05); }
        .btn-corrigir-acertou { color: var(--ouro); border-color: rgba(255, 210, 10, .5); }
        .btn-corrigir-acertou:hover { background: rgba(255, 210, 10, .1); }
        .btn-corrigir-errou { color: var(--azul-claro); border-color: rgba(180, 200, 242, .5); }
        .btn-corrigir-errou:hover { background: rgba(180, 200, 242, .1); }
        .tag-manual { font-size: 0.625rem; color: var(--apagado); margin-top: 0.5rem; }

        /* ---------- janelas do Criador de Odd ---------- */
        .modal-overlay { display: none; position: fixed; inset: 0; z-index: 1000; align-items: center; justify-content: center; padding: 1rem; background: rgba(1, 5, 15, .72); }
        .modal-overlay.aberto { display: flex; }
        .modal-caixa {
            width: 100%; max-width: 34rem; max-height: 88vh; overflow-y: auto; padding: 1.375rem 1.5rem;
            border-radius: var(--raio); border: 1px solid var(--vidro-borda); box-shadow: var(--vidro-sombra);
            background: linear-gradient(140deg, rgba(255,255,255,.1), rgba(255,255,255,.03)), #06122e;
        }
        .modal-caixa h2 { margin: 0 0 0.25rem; font-size: 1.25rem; font-weight: 300; letter-spacing: -.02em; }
        .modal-sub { color: var(--apagado); font-size: 0.75rem; margin: 0 0 1.125rem; line-height: 1.5; }
        .modal-campo { margin-bottom: 0.875rem; }
        .modal-campo label { display: block; margin-bottom: 0.3125rem; font-family: var(--f-corpo); font-size: 0.5625rem; letter-spacing: 0; text-transform: none; color: var(--apagado); }
        .modal-linha-dupla { display: flex; gap: 0.625rem; }
        .modal-linha-dupla > div { flex: 1; min-width: 0; }
        .btn-adicionar-perna { width: 100%; background: rgba(63, 111, 209, .2); color: var(--azul-claro); border: 1px solid rgba(180, 200, 242, .4); border-radius: 0.5rem; }
        .btn-adicionar-perna:hover { background: rgba(63, 111, 209, .32); }
        .pernas-adicionadas { margin: 0.875rem 0; display: grid; gap: 0.375rem; }
        .perna-adicionada-item { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; padding: 0.5rem 0.75rem; border-radius: 0.5rem; background: rgba(1, 5, 15, .45); border: 1px solid var(--linha-fraca); font-size: 0.75rem; }
        .perna-adicionada-remover { color: var(--azul-claro); cursor: pointer; font-size: 0.6875rem; flex-shrink: 0; }
        .modal-botoes-finais { display: flex; gap: 0.625rem; margin-top: 1.125rem; }
        .modal-botoes-finais button { flex: 1; }
        .btn-fechar-modal { background: transparent; color: var(--texto); border-color: var(--linha); }
        .btn-fechar-modal:hover { background: rgba(255,255,255,.06); }
        .btn-salvar-odd:disabled { background: rgba(255,255,255,.08); color: var(--apagado); cursor: not-allowed; }
        .modal-caixa-jogos { max-width: 40rem; }
        .jogo-escolhido-box { display: flex; justify-content: space-between; align-items: center; gap: 0.625rem; padding: 0.5625rem 0.75rem; border-radius: 0.5rem; background: rgba(1, 5, 15, .55); border: 1px solid var(--linha); font-size: 0.8125rem; }
        .jogo-escolhido-trocar { color: var(--ouro); cursor: pointer; font-size: 0.75rem; font-weight: 600; flex-shrink: 0; white-space: nowrap; }
        .lista-rodadas-jogo { max-height: 58vh; overflow-y: auto; margin-top: 0.25rem; display: grid; gap: 0.375rem; }
        .rodada-jogo-item { border: 1px solid var(--linha-fraca); border-radius: 0.625rem; overflow: hidden; }
        .rodada-jogo-cabecalho { padding: 0.6875rem 0.875rem; cursor: pointer; display: flex; justify-content: space-between; align-items: center; gap: 0.75rem; background: rgba(255,255,255,.03); }
        .rodada-jogo-cabecalho:hover { background: rgba(255,255,255,.07); }
        .rodada-jogo-titulo { font-weight: 600; font-size: 0.8125rem; }
        .rodada-jogo-periodo { color: var(--apagado); font-size: 0.6875rem; }
        .rodada-jogo-lista { display: none; padding: 0.375rem 0.5rem 0.5rem; }
        .rodada-jogo-lista.aberta { display: block; }
        .jogo-escolher-item { padding: 0.4375rem 0.625rem; border-radius: 0.375rem; cursor: pointer; font-size: 0.8125rem; display: flex; justify-content: space-between; gap: 0.625rem; }
        .jogo-escolher-item:hover { background: rgba(63, 111, 209, .22); }
        .jogo-escolher-item span:last-child { color: var(--apagado); font-family: var(--f-dado); font-size: 0.6875rem; white-space: nowrap; }

        /* o fundo acompanha o ROI: o mesmo degradê vivo, com as cores trocadas
           (verde no positivo, vermelho no negativo). Aqui fica só o fundo de
           reserva, para quando não há WebGL; as cores do degradê vêm do script. */
        body.tema-pos .fundo { background: radial-gradient(60vmax 45vmax at 8% -6%, rgba(40, 190, 100, .30), rgba(40, 190, 100, 0) 70%), radial-gradient(55vmax 40vmax at 104% 18%, rgba(92, 74, 0, .55), rgba(92, 74, 0, 0) 70%), radial-gradient(70vmax 50vmax at 70% 112%, rgba(6, 79, 32, .85), rgba(6, 79, 32, 0) 70%), linear-gradient(180deg, #021f0a 0%, #010f05 60%); }
        body.tema-neg .fundo { background: radial-gradient(60vmax 45vmax at 8% -6%, rgba(209, 63, 63, .30), rgba(209, 63, 63, 0) 70%), radial-gradient(55vmax 40vmax at 104% 18%, rgba(92, 74, 0, .55), rgba(92, 74, 0, 0) 70%), radial-gradient(70vmax 50vmax at 70% 112%, rgba(79, 6, 12, .85), rgba(79, 6, 12, 0) 70%), linear-gradient(180deg, #1f0205 0%, #0f0103 60%); }

        @media (max-width: 1099px) {
            :root { --degrau1: 100%; --degrau2: 100%; }
            .topo-grade, .faixa-acoes { grid-template-columns: minmax(0, 1fr); }
        }
        @media (max-width: 640px) {
            .numeros { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .numeros > .numero:nth-child(n) { grid-column: span 1; }
            .numeros > .numero:first-child { grid-column: 1 / -1; }
            .modal-linha-dupla { flex-direction: column; gap: 0; }
        }
        @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
    </style>
    <script>
        function alternarFormaBanca(tipo) {
            const alvo = document.getElementById('forma-' + tipo);
            const abrir = !alvo.classList.contains('aberta');
            document.getElementById('forma-deposito').classList.remove('aberta');
            document.getElementById('forma-resgate').classList.remove('aberta');
            if (abrir) { alvo.classList.add('aberta'); alvo.querySelector('input[type=number]').focus(); }
        }
        function alternarExtrato() {
            document.getElementById('extrato-lista').classList.toggle('aberta');
            document.getElementById('extrato-box').classList.toggle('aberto');
        }

        // Criador de Odd -----------------------------------------------------
        const RODADAS_CRIADOR_ODD = {{ jogos_criador_odd|tojson }};
        const JOGOS_CRIADOR_ODD = RODADAS_CRIADOR_ODD.flatMap(r => r.jogos);
        const MERCADOS_CRIADOR_ODD = {
            "cartao": {"label": "Cartão de Jogador", "jogador": true, "modo": "binario"},
            "impedimento": {"label": "Impedimento (Jogador)", "jogador": true, "modo": "binario"},
            "falta_cometida": {"label": "Faltas Cometidas (Jogador)", "jogador": true, "modo": "linha"},
            "falta_sofrida": {"label": "Faltas Sofridas (Jogador)", "jogador": true, "modo": "linha"},
            "desarme": {"label": "Desarmes (Jogador)", "jogador": true, "modo": "linha"},
            "chute_no_gol": {"label": "Chutes no Gol (Jogador)", "jogador": true, "modo": "linha"},
            "chute_total": {"label": "Chutes Total (Jogador)", "jogador": true, "modo": "linha"},
            "escanteio_time": {"label": "Escanteios do Nosso Time", "jogador": false, "modo": "linha"},
            "escanteio_total": {"label": "Escanteios Total do Jogo", "jogador": false, "modo": "linha"},
            "cartao_total": {"label": "Cartões Total do Jogo", "jogador": false, "modo": "linha"},
            "resultado_final": {"label": "Resultado Final", "jogador": false, "modo": "resultado"},
        };
        let pernasCriadorOdd = [];

        function abrirCriadorOdd() {
            document.getElementById('criador-odd-jogo').value = '';
            document.getElementById('criador-odd-jogo-label').textContent = 'Nenhum jogo escolhido';
            const selectMercado = document.getElementById('criador-odd-mercado');
            selectMercado.innerHTML = Object.entries(MERCADOS_CRIADOR_ODD).map(([chave, m]) =>
                `<option value="${chave}">${m.label}</option>`
            ).join('');
            pernasCriadorOdd = [];
            renderizarPernasCriadorOdd();
            document.getElementById('criador-odd-linha').value = '';
            document.getElementById('criador-odd-valor').value = '';
            document.getElementById('criador-odd-odd').value = '';
            atualizarCamposCriadorOdd();
            document.getElementById('modal-criador-odd').classList.add('aberto');
        }
        function fecharCriadorOdd() {
            document.getElementById('modal-criador-odd').classList.remove('aberto');
        }
        function abrirEscolherJogo() {
            const container = document.getElementById('lista-rodadas-jogo');
            container.innerHTML = RODADAS_CRIADOR_ODD.map((r, idx) => `
                <div class="rodada-jogo-item">
                    <div class="rodada-jogo-cabecalho" onclick="toggleRodadaJogo(${idx})">
                        <span class="rodada-jogo-titulo">${escaparHtmlCriadorOdd(r.rotulo)}</span>
                        <span class="rodada-jogo-periodo">${escaparHtmlCriadorOdd(r.periodo)}</span>
                    </div>
                    <div class="rodada-jogo-lista" id="rodada-jogo-lista-${idx}">
                        ${r.jogos.map(j => `
                            <div class="jogo-escolher-item" onclick="selecionarJogoCriadorOdd(${j.id})">
                                <span>${escaparHtmlCriadorOdd(j.nosso_time)} x ${escaparHtmlCriadorOdd(j.adversario)}</span>
                                <span>${j.data_jogo}</span>
                            </div>
                        `).join('')}
                    </div>
                </div>
            `).join('');
            document.getElementById('modal-escolher-jogo').classList.add('aberto');
        }
        function fecharEscolherJogo() {
            document.getElementById('modal-escolher-jogo').classList.remove('aberto');
        }
        function toggleRodadaJogo(idx) {
            document.getElementById('rodada-jogo-lista-' + idx).classList.toggle('aberta');
        }
        function selecionarJogoCriadorOdd(jogoId) {
            const jogo = JOGOS_CRIADOR_ODD.find(j => String(j.id) === String(jogoId));
            document.getElementById('criador-odd-jogo').value = jogoId;
            document.getElementById('criador-odd-jogo-label').textContent = jogo
                ? `${jogo.data_jogo} · ${jogo.nosso_time} x ${jogo.adversario}`
                : ('Jogo #' + jogoId);
            fecharEscolherJogo();
            atualizarJogadoresCriadorOdd();
        }
        function escaparHtmlCriadorOdd(s) {
            return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
            }[c]));
        }
        function atualizarCamposCriadorOdd() {
            const mercado = MERCADOS_CRIADOR_ODD[document.getElementById('criador-odd-mercado').value];
            document.getElementById('campo-criador-odd-jogador').style.display = mercado.jogador ? 'block' : 'none';
            document.getElementById('campo-criador-odd-linha').style.display = mercado.modo === 'linha' ? 'block' : 'none';
            const selectDirecao = document.getElementById('criador-odd-direcao');
            if (mercado.modo === 'binario') {
                selectDirecao.innerHTML = '<option value="sim">Sim</option><option value="não">Não</option>';
            } else if (mercado.modo === 'resultado') {
                selectDirecao.innerHTML = '<option value="vitoria">Vitória</option><option value="empate">Empate</option><option value="derrota">Derrota</option>';
            } else {
                selectDirecao.innerHTML = '<option value="mais">Mais de</option><option value="menos">Menos de</option>';
            }
            if (mercado.jogador) atualizarJogadoresCriadorOdd();
        }
        function atualizarJogadoresCriadorOdd() {
            const mercado = MERCADOS_CRIADOR_ODD[document.getElementById('criador-odd-mercado').value];
            if (!mercado.jogador) return;
            const jogoId = document.getElementById('criador-odd-jogo').value;
            const selectJogador = document.getElementById('criador-odd-jogador');
            if (!jogoId) {
                selectJogador.innerHTML = '<option value="">Escolhe um jogo primeiro</option>';
                return;
            }
            selectJogador.innerHTML = '<option>Carregando...</option>';
            fetch(`/api/jogadores-jogo/${jogoId}`)
                .then(r => r.json())
                .then(jogadores => {
                    selectJogador.innerHTML = jogadores.map(j => `<option value="${j.id}">${j.nome}</option>`).join('')
                        || '<option value="">Nenhum jogador com dado nesse jogo ainda</option>';
                });
        }
        function adicionarPernaCriadorOdd() {
            const jogoId = document.getElementById('criador-odd-jogo').value;
            if (!jogoId) { alert('Escolhe um jogo primeiro.'); return; }
            const jogoInfo = JOGOS_CRIADOR_ODD.find(j => String(j.id) === String(jogoId));
            const tipoPadrao = document.getElementById('criador-odd-mercado').value;
            const mercado = MERCADOS_CRIADOR_ODD[tipoPadrao];
            const direcao = document.getElementById('criador-odd-direcao').value;
            const linha = document.getElementById('criador-odd-linha').value;
            const selectJogador = document.getElementById('criador-odd-jogador');
            const jogadorId = mercado.jogador ? parseInt(selectJogador.value) : null;
            const jogadorNome = mercado.jogador ? selectJogador.options[selectJogador.selectedIndex].text : null;
            if (mercado.modo === 'linha' && !linha) { alert('Preenche a linha (ex: 3.5).'); return; }
            if (mercado.jogador && !jogadorId) { alert('Escolhe um jogador.'); return; }
            let descricao;
            if (mercado.modo === 'binario') {
                descricao = `${mercado.label} - ${direcao === 'sim' ? 'Sim' : 'Não'} - ${jogadorNome}`;
            } else if (mercado.modo === 'resultado') {
                const rotulos = {"vitoria": `Vitória do ${jogoInfo.nosso_time}`, "empate": "Empate", "derrota": `Derrota do ${jogoInfo.nosso_time}`};
                descricao = `Resultado Final - ${rotulos[direcao]}`;
            } else {
                const rotuloDirecao = direcao === 'mais' ? 'Mais de' : 'Menos de';
                descricao = `${mercado.label} - ${rotuloDirecao} ${linha}` + (jogadorNome ? ` - ${jogadorNome}` : '');
            }
            pernasCriadorOdd.push({
                jogo_id: parseInt(jogoId),
                jogador_id: jogadorId,
                tipo_padrao: tipoPadrao,
                linha: mercado.modo === 'linha' ? parseFloat(linha) : null,
                direcao: direcao,
                descricao: descricao,
                fonte: "manual",
            });
            renderizarPernasCriadorOdd();
        }
        function removerPernaCriadorOdd(indice) {
            pernasCriadorOdd.splice(indice, 1);
            renderizarPernasCriadorOdd();
        }
        function renderizarPernasCriadorOdd() {
            const container = document.getElementById('criador-odd-pernas-lista');
            container.innerHTML = pernasCriadorOdd.map((p, i) =>
                `<div class="perna-adicionada-item">
                    <span>${escaparHtmlCriadorOdd(p.descricao)}</span>
                    <span class="perna-adicionada-remover" onclick="removerPernaCriadorOdd(${i})">remover</span>
                </div>`
            ).join('');
            document.getElementById('btn-salvar-criador-odd').disabled = pernasCriadorOdd.length === 0;
        }
        function salvarCriadorOdd() {
            if (pernasCriadorOdd.length === 0) return;
            const valor = document.getElementById('criador-odd-valor').value;
            const odd = document.getElementById('criador-odd-odd').value;
            if (!valor || !odd) { alert('Preenche o valor apostado e a odd.'); return; }
            document.getElementById('campo-criador-odd-descricao').value = pernasCriadorOdd.map(p => p.descricao).join(' + ');
            document.getElementById('campo-criador-odd-pernas').value = JSON.stringify(pernasCriadorOdd);
            document.getElementById('campo-criador-odd-valor').value = valor;
            document.getElementById('campo-criador-odd-odd').value = odd;
            document.getElementById('form-criador-odd').submit();
        }
    </script>
</head>
<body class="{{ 'tema-neg' if resumo.roi < 0 else ('tema-pos' if resumo.roi > 0 else '') }}">
""" + FUNDO_VISUAL_NOVO + """    {% macro reais(v, sinal=false) %}{% set x = (v or 0)|float %}{{ ('+' if sinal and x > 0 else ('−' if x < 0 else '')) }}R$ {{ ('%.2f'|format(x|abs)).replace('.', ',') }}{% endmacro %}
    {% macro dec(v, casas=1) %}{{ (('%.' ~ casas ~ 'f')|format((v or 0)|float)).replace('.', ',') }}{% endmacro %}
    {% macro quando(d) %}{% if d is string %}{{ d[8:10] }}/{{ d[5:7] }}/{{ d[2:4] }} {{ d[11:16] }}{% elif d %}{{ d.strftime('%d/%m/%y %H:%M') }}{% endif %}{% endmacro %}

    <div class="modal-overlay" id="modal-criador-odd" onclick="if (event.target === this) fecharCriadorOdd()">
        <div class="modal-caixa" role="dialog" aria-modal="true" aria-labelledby="t-criador">
            <h2 id="t-criador">Criador de Odd</h2>
            <p class="modal-sub">Apostou fora do app? Monte aqui, perna por perna, e salve: entra no seu ROI normalmente.</p>
            <div class="modal-campo">
                <label>Jogo</label>
                <div class="jogo-escolhido-box">
                    <span id="criador-odd-jogo-label">Nenhum jogo escolhido</span>
                    <span class="jogo-escolhido-trocar" onclick="abrirEscolherJogo()">Escolher jogo</span>
                </div>
                <input type="hidden" id="criador-odd-jogo">
            </div>
            <div class="modal-campo">
                <label for="criador-odd-mercado">Mercado</label>
                <select id="criador-odd-mercado" onchange="atualizarCamposCriadorOdd()"></select>
            </div>
            <div class="modal-campo" id="campo-criador-odd-jogador">
                <label for="criador-odd-jogador">Jogador</label>
                <select id="criador-odd-jogador"></select>
            </div>
            <div class="modal-linha-dupla">
                <div class="modal-campo" id="campo-criador-odd-linha">
                    <label for="criador-odd-linha">Linha</label>
                    <input type="number" step="0.5" id="criador-odd-linha" placeholder="ex: 3.5">
                </div>
                <div class="modal-campo">
                    <label for="criador-odd-direcao">Direção</label>
                    <select id="criador-odd-direcao"></select>
                </div>
            </div>
            <button type="button" class="btn-adicionar-perna" onclick="adicionarPernaCriadorOdd()">Adicionar perna</button>
            <div class="pernas-adicionadas" id="criador-odd-pernas-lista"></div>
            <div class="modal-linha-dupla">
                <div class="modal-campo">
                    <label for="criador-odd-valor">Valor apostado (R$)</label>
                    <input type="number" step="0.01" min="0.01" id="criador-odd-valor">
                </div>
                <div class="modal-campo">
                    <label for="criador-odd-odd">Odd final (a combinada, se for múltipla)</label>
                    <input type="number" step="0.01" min="1.01" id="criador-odd-odd">
                </div>
            </div>
            <form method="POST" action="/salvar-aposta" id="form-criador-odd">
                <input type="hidden" name="descricao" id="campo-criador-odd-descricao">
                <input type="hidden" name="casa_aposta" value="Anotado manualmente">
                <input type="hidden" name="pernas" id="campo-criador-odd-pernas">
                <input type="hidden" name="valor_apostado" id="campo-criador-odd-valor">
                <input type="hidden" name="odd_combinada" id="campo-criador-odd-odd">
                <input type="hidden" name="voltar" value="/minhas-apostas">
            </form>
            <div class="modal-botoes-finais">
                <button type="button" class="btn-fechar-modal" onclick="fecharCriadorOdd()">Cancelar</button>
                <button type="button" class="btn-salvar-odd" id="btn-salvar-criador-odd" onclick="salvarCriadorOdd()" disabled>Salvar aposta</button>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="modal-escolher-jogo" style="z-index: 1100;" onclick="if (event.target === this) fecharEscolherJogo()">
        <div class="modal-caixa modal-caixa-jogos" role="dialog" aria-modal="true" aria-labelledby="t-escolher">
            <h2 id="t-escolher">Escolher jogo</h2>
            <p class="modal-sub">Jogos da temporada atual, por rodada. Clique numa rodada para ver os jogos dela.</p>
            <div class="lista-rodadas-jogo" id="lista-rodadas-jogo"></div>
            <div class="modal-botoes-finais">
                <button type="button" class="btn-fechar-modal" onclick="fecharEscolherJogo()">Fechar</button>
            </div>
        </div>
    </div>

    {{ nav_html|safe }}

    <main class="conteudo">
        {% with mensagens = get_flashed_messages(with_categories=true) %}
            {% for categoria, texto in mensagens %}
            <div class="flash flash-{{ categoria }}">{{ texto }}</div>
            {% endfor %}
        {% endwith %}

        <header class="cabeca">
            <h1>Minhas apostas</h1>
            <p>Só o que você salvou com valor apostado. Recomendação não salva não entra aqui.</p>
        </header>

        <div class="topo-grade">
            <section class="vidro banca-box" aria-label="Banca">
                <div>
                    <span class="rotulo">Banca atual</span>
                    <div class="banca-valor {{ 'pos' if banca_atual >= 0 else 'neg' }}"><small>R$</small>{{ ('%.2f'|format(banca_atual|abs)).replace('.', ',') }}</div>
                    <div class="banca-nota">Dinheiro fictício, não real.</div>
                </div>
                <div class="banca-botoes">
                    <button type="button" onclick="alternarFormaBanca('deposito')">Depositar</button>
                    <button type="button" class="btn-contorno" onclick="alternarFormaBanca('resgate')">Resgatar</button>
                </div>
                <form method="POST" action="/banca-movimento" class="banca-forma" id="forma-deposito">
                    <input type="hidden" name="tipo" value="deposito">
                    <input type="number" step="0.01" min="0.01" name="valor" placeholder="Valor a depositar (R$)" required>
                    <button type="submit" class="btn-confirmar-deposito">Confirmar</button>
                </form>
                <form method="POST" action="/banca-movimento" class="banca-forma" id="forma-resgate">
                    <input type="hidden" name="tipo" value="resgate">
                    <input type="number" step="0.01" min="0.01" name="valor" placeholder="Valor a resgatar (R$)" required>
                    <button type="submit" class="btn-contorno btn-confirmar-resgate">Confirmar</button>
                </form>
            </section>

            <section class="numeros" aria-label="Resumo">
                <div class="vidro numero" id="cartao-roi">
                    <span class="rotulo">ROI</span>
                    <b>{{ '+' if resumo.roi > 0 else '' }}{{ dec(resumo.roi, 2) }}%</b>
                    <span class="det">só das apostas já resolvidas</span>
                </div>
                <div class="vidro numero">
                    <span class="rotulo">Taxa de acerto</span>
                    <b>{{ dec(resumo.taxa_acerto, 1) }}%</b>
                </div>
                <div class="vidro numero">
                    <span class="rotulo">Retorno</span>
                    <b class="{{ 'pos' if resumo.retorno_total >= 0 else 'neg' }}">{{ reais(resumo.retorno_total, true) }}</b>
                    <span class="det">lucro ou prejuízo</span>
                </div>
                <div class="vidro numero">
                    <span class="rotulo">Total apostado</span>
                    <b>{{ reais(resumo.total_apostado) }}</b>
                </div>
                <div class="vidro numero">
                    <span class="rotulo">Apostas salvas</span>
                    <b>{{ resumo.total_apostas }}</b>
                </div>
            </section>
        </div>

        {% if pontos_grafico|length > 1 %}
        {% set serie = [] %}{% for rot, v in pontos_grafico %}{% set _ = serie.append([rot if rot is string else rot.strftime('%d/%m %H:%M'), v|float]) %}{% endfor %}
        <section class="vidro grafico-box" aria-labelledby="t-grafico">
            <div class="grafico-topo">
                <h3 id="t-grafico">Retorno acumulado</h3>
                <span>{{ pontos_grafico|length - 1 }} apostas resolvidas, da mais antiga para a mais nova</span>
            </div>
            <div id="grafico" role="img" aria-label="Retorno acumulado: {{ reais(serie[-1][1], true) }} na última aposta resolvida"><div class="dica" id="grafico-dica"></div></div>
            <script>window.SERIE_RETORNO = {{ serie|tojson }};</script>
        </section>
        {% endif %}

        <div class="faixa-acoes">
            <section class="vidro criador-odd-box" aria-label="Criador de Odd">
                <div>
                    <div class="criador-odd-titulo">Criador de Odd</div>
                    <div class="criador-odd-sub">Apostou fora do app (na casa, por exemplo)? Anote aqui para entrar no seu ROI.</div>
                </div>
                <button type="button" onclick="abrirCriadorOdd()">Anotar aposta</button>
            </section>
            {% if movimentos_banca %}
            <section class="vidro extrato-box" id="extrato-box">
                <button type="button" class="extrato-titulo" onclick="alternarExtrato()">
                    <span>Extrato da banca <small>últimos {{ movimentos_banca|length }} movimentos</small></span>
                    <span class="seta" aria-hidden="true"></span>
                </button>
                <div class="extrato-lista" id="extrato-lista">
                    {% for m in movimentos_banca %}
                    <div class="extrato-linha">
                        <span><span class="quando">{{ quando(m.criado_em) }}</span>{{ {"deposito": "Depósito", "resgate": "Resgate", "aposta": "Aposta salva", "retorno": "Retorno (aposta ganha)", "cancelamento": "Cancelamento"}.get(m.tipo, m.tipo) }}</span>
                        <b class="{{ 'pos' if m.valor|float >= 0 else 'neg' }}">{{ reais(m.valor, true) }}</b>
                        <span class="saldo">saldo {{ reais(m.saldo_apos) }}</span>
                    </div>
                    {% endfor %}
                </div>
            </section>
            {% endif %}
        </div>

        <section class="secao" aria-labelledby="t-apostas">
            {% set n_ac = apostas|selectattr('resultado', 'equalto', 'acertou')|list|length %}
            {% set n_er = apostas|selectattr('resultado', 'equalto', 'errou')|list|length %}
            {% set n_pe = apostas|selectattr('resultado', 'equalto', 'pendente')|list|length %}
            <div class="lista-topo">
                <h2 class="secao-titulo" id="t-apostas">Apostas salvas</h2>
                {% if apostas %}
                <div class="filtros" role="group" aria-label="Filtrar por resultado">
                    <button type="button" class="ativo" data-filtro="todas" onclick="filtrarApostas('todas')">Todas<small>{{ apostas|length }}</small></button>
                    <button type="button" data-filtro="acertou" onclick="filtrarApostas('acertou')">Acertou<small>{{ n_ac }}</small></button>
                    <button type="button" data-filtro="errou" onclick="filtrarApostas('errou')">Errou<small>{{ n_er }}</small></button>
                    <button type="button" data-filtro="pendente" onclick="filtrarApostas('pendente')">Pendente<small>{{ n_pe }}</small></button>
                </div>
                {% endif %}
            </div>
            {% if apostas %}
            <div class="apostas" id="lista-apostas">
                {% for a in apostas %}
                <article class="vidro cartao r-{{ a.resultado }}" data-resultado="{{ a.resultado }}">
                    <div class="cartao-topo">
                        <span class="quando">{{ quando(a.criado_em) }}</span>
                        <span class="selo selo-{{ a.resultado }}">{{ a.resultado }}</span>
                    </div>
                    {% if a.jogos_label %}<div class="jogos-label">{{ a.jogos_label }}</div>{% endif %}
                    <div class="descricao">{{ a.descricao }}</div>
                    <div class="metricas">
                        <span>Odd<b>{{ dec(a.odd_combinada, 2) }}</b></span>
                        <span>Apostado<b>{{ reais(a.valor_apostado) }}</b></span>
                        <span>Retorno{% if a.retorno is not none %}<b class="{{ 'pos' if a.retorno|float >= 0 else 'neg' }}">{{ reais(a.retorno, true) }}</b>{% else %}<b>-</b>{% endif %}</span>
                    </div>
                    <div class="casa">{{ a.casa_aposta }}</div>
                    {% if a.resultado == 'pendente' %}
                    <form method="POST" action="/cancelar-aposta" class="salvar-linha">
                        <input type="hidden" name="aposta_id" value="{{ a.id }}">
                        <button type="submit" class="btn-pequeno btn-cancelar">Cancelar aposta</button>
                    </form>
                    {% else %}
                    <div class="salvar-linha">
                        <button type="button" class="btn-pequeno btn-corrigir"
                                onclick="document.getElementById('painel-corrigir-{{ a.id }}').classList.toggle('aberto')">Corrigir manualmente</button>
                    </div>
                    <div class="painel-corrigir" id="painel-corrigir-{{ a.id }}">
                        <form method="POST" action="/corrigir-aposta-manual"
                              onsubmit="return confirm('Cancelar esta aposta? A linha vai desaparecer e a banca será ajustada de volta como se ela nunca tivesse existido.');">
                            <input type="hidden" name="aposta_id" value="{{ a.id }}">
                            <input type="hidden" name="acao" value="cancelar">
                            <button type="submit" class="btn-corrigir-opcao btn-corrigir-cancelar">Cancelar aposta</button>
                        </form>
                        <form method="POST" action="/corrigir-aposta-manual"
                              onsubmit="return confirm('Marcar esta aposta como ACERTOU? A banca será ajustada pro novo resultado.');">
                            <input type="hidden" name="aposta_id" value="{{ a.id }}">
                            <input type="hidden" name="acao" value="acertou">
                            <button type="submit" class="btn-corrigir-opcao btn-corrigir-acertou">Deu certo</button>
                        </form>
                        <form method="POST" action="/corrigir-aposta-manual"
                              onsubmit="return confirm('Marcar esta aposta como ERROU? A banca será ajustada pro novo resultado.');">
                            <input type="hidden" name="aposta_id" value="{{ a.id }}">
                            <input type="hidden" name="acao" value="errou">
                            <button type="submit" class="btn-corrigir-opcao btn-corrigir-errou">Deu errado</button>
                        </form>
                    </div>
                    {% if a.resolvido_manualmente %}
                    <div class="tag-manual">Corrigido manualmente: não é reavaliado automaticamente.</div>
                    {% endif %}
                    {% endif %}
                </article>
                {% endfor %}
            </div>
            <div class="vazio" id="filtro-vazio" hidden>Nenhuma aposta com esse resultado.</div>
            {% else %}
            <div class="vazio">Você ainda não salvou nenhuma aposta. Use o botão "Salvar" nas recomendações, ou o Criador de Odd acima, para começar a acompanhar seu ROI.</div>
            {% endif %}
        </section>
    </main>

    <script>
        function filtrarApostas(filtro) {
            let n = 0;
            document.querySelectorAll('#lista-apostas .cartao').forEach(function (c) {
                const mostra = filtro === 'todas' || c.dataset.resultado === filtro;
                c.style.display = mostra ? '' : 'none';
                if (mostra) n++;
            });
            document.querySelectorAll('.filtros button').forEach(function (b) { b.classList.toggle('ativo', b.dataset.filtro === filtro); });
            const vazio = document.getElementById('filtro-vazio');
            if (vazio) vazio.hidden = n > 0;
        }

        // gráfico do retorno acumulado, desenhado na largura real da caixa
        (function () {
            const serie = window.SERIE_RETORNO;
            const caixa = document.getElementById('grafico');
            if (!serie || !caixa) return;
            const NS = 'http://www.w3.org/2000/svg';
            const dica = document.getElementById('grafico-dica');
            const brl = v => (v > 0 ? '+' : v < 0 ? '−' : '') + 'R$ ' + Math.abs(v).toFixed(2).replace('.', ',');
            function el(nome, attrs, pai) {
                const e = document.createElementNS(NS, nome);
                for (const k in attrs) e.setAttribute(k, attrs[k]);
                if (pai) pai.appendChild(e);
                return e;
            }
            function desenhar() {
                const antigo = caixa.querySelector('svg');
                if (antigo) antigo.remove();
                const L = caixa.clientWidth, A = caixa.clientHeight;
                const esq = 56, dir = 12, cima = 12, baixo = 22;
                const vals = serie.map(p => p[1]);
                let min = Math.min(0, ...vals), max = Math.max(0, ...vals);
                if (max === min) { max += 1; min -= 1; }
                const folga = (max - min) * 0.12; max += folga; min -= folga;
                const x = i => esq + i * (L - esq - dir) / Math.max(serie.length - 1, 1);
                const y = v => cima + (max - v) * (A - cima - baixo) / (max - min);
                const svg = el('svg', {viewBox: `0 0 ${L} ${A}`, 'aria-hidden': 'true'});
                const final = vals[vals.length - 1];
                const cor = final >= 0 ? 'var(--ouro)' : 'var(--azul-claro)';
                const corHex = getComputedStyle(document.body).getPropertyValue(final >= 0 ? '--ouro' : '--azul-claro').trim() || '#ffd20a';
                const defs = el('defs', {}, svg);
                const grad = el('linearGradient', {id: 'grad-retorno', x1: 0, y1: 0, x2: 0, y2: 1}, defs);
                el('stop', {offset: '0%', 'stop-color': corHex, 'stop-opacity': .28}, grad);
                el('stop', {offset: '100%', 'stop-color': corHex, 'stop-opacity': 0}, grad);
                // grade: 4 linhas com o valor em reais
                for (let k = 0; k <= 3; k++) {
                    const v = min + (max - min) * k / 3, yy = y(v);
                    el('line', {x1: esq, x2: L - dir, y1: yy, y2: yy, stroke: 'rgba(255,255,255,.06)'}, svg);
                    el('text', {x: esq - 8, y: yy + 3, 'text-anchor': 'end'}, svg).textContent = 'R$ ' + v.toFixed(2).replace('.', ',');
                }
                el('line', {x1: esq, x2: L - dir, y1: y(0), y2: y(0), stroke: 'rgba(180,200,242,.45)', 'stroke-dasharray': '4 4'}, svg);
                const pts = serie.map((p, i) => [x(i), y(p[1])]);
                const linha = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
                el('path', {d: linha + ` L${pts[pts.length - 1][0].toFixed(1)} ${y(min).toFixed(1)} L${pts[0][0].toFixed(1)} ${y(min).toFixed(1)} Z`, fill: 'url(#grad-retorno)'}, svg);
                el('path', {d: linha, fill: 'none', stroke: cor, 'stroke-width': 2, 'stroke-linejoin': 'round'}, svg);
                el('text', {x: esq, y: A - 4}, svg).textContent = serie[0][0];
                el('text', {x: L - dir, y: A - 4, 'text-anchor': 'end'}, svg).textContent = serie[serie.length - 1][0];
                const ponto = el('circle', {r: 4, fill: cor, stroke: '#01050f', 'stroke-width': 2, cx: pts[pts.length - 1][0], cy: pts[pts.length - 1][1]}, svg);
                caixa.insertBefore(svg, dica);
                svg.addEventListener('pointermove', function (ev) {
                    const r = svg.getBoundingClientRect();
                    const i = Math.max(0, Math.min(serie.length - 1, Math.round((ev.clientX - r.left - esq) / ((L - esq - dir) / Math.max(serie.length - 1, 1)))));
                    ponto.setAttribute('cx', pts[i][0]); ponto.setAttribute('cy', pts[i][1]);
                    dica.style.left = pts[i][0] + 'px'; dica.style.top = pts[i][1] + 'px';
                    dica.textContent = serie[i][0] + ' · ' + brl(serie[i][1]);
                    dica.style.opacity = 1;
                });
                svg.addEventListener('pointerleave', function () {
                    dica.style.opacity = 0;
                    ponto.setAttribute('cx', pts[pts.length - 1][0]); ponto.setAttribute('cy', pts[pts.length - 1][1]);
                });
            }
            desenhar();
            window.desenharGraficoRetorno = desenhar;
            let t;
            window.addEventListener('resize', function () { clearTimeout(t); t = setTimeout(desenhar, 120); });
        })();
    </script>
    <script>
        // cores do degradê vivo para esta página: as mesmas 6 do padrão (do
        // mais escuro ao mais claro, mais as manchas de ouro), em verde ou vermelho
        window.CORES_FUNDO_TEMAS = {
            pos: [[.004, .03, .012], [.01, .13, .05], [.03, .3, .11], [.07, .48, .2], [.13, .6, .28], [.42, .3, .02]],
            neg: [[.04, .004, .008], [.14, .012, .02], [.3, .03, .05], [.48, .07, .08], [.6, .13, .13], [.42, .3, .02]]
        };
        window.CORES_FUNDO = window.CORES_FUNDO_TEMAS[document.body.classList.contains('tema-neg') ? 'neg' : (document.body.classList.contains('tema-pos') ? 'pos' : '')] || null;
    </script>
""" + SCRIPT_FUNDO_VIVO + """</body>
</html>
"""


def _extrair_linha_direcao_da_descricao(descricao):
    """NOVO (correção, 4ª tentativa de casamento): tenta extrair linha e
    direção de dentro do TEXTO de uma descrição salva antes da correção
    que passou a gravar linha/direção estruturados numa perna (ex:
    "Cartões Total do Jogo - Mais de 5.5" -> linha=5.5, direção='mais') -
    só usado como último recurso, quando as tentativas anteriores não
    encontraram (ou encontraram de forma ambígua, mais de uma linha
    candidata) o resultado real dessa perna."""
    if not descricao:
        return None, None
    m = re.search(r"(Mais|Menos) de (\d+(?:\.\d+)?)", descricao, re.IGNORECASE)
    if not m:
        return None, None
    direcao = "mais" if m.group(1).lower() == "mais" else "menos"
    return float(m.group(2)), direcao


def avaliar_pernas_aposta(cur, pernas):
    """NOVO (extraído de resolver_apostas_pendentes em 26/08/2026): casa
    cada perna de uma aposta salva (individual ou múltipla) com o
    resultado real, e devolve a lista de resultados ('acertou'/'errou'/
    'pendente'), um por perna, na mesma ordem.

    Única fonte da lógica de casamento - usada tanto por
    resolver_apostas_pendentes (apostas ainda pendentes) quanto por
    reabrir_apostas_desatualizadas.py (apostas JÁ resolvidas, pra
    conferir se uma correção de dado em historico_recomendacoes mudou o
    resultado por baixo delas). Antes essa lógica só existia dentro de
    resolver_apostas_pendentes, e uma aposta que já tinha resultado
    gravado nunca era reavaliada - ver seção 27/pendência do ROI."""
    resultados_pernas = []
    for perna in pernas:
        if perna.get("fonte") == "manual":
            # NOVO: usa a mesma função de avaliação compartilhada com
            # arquivar_recomendacoes.py (avaliacao.py) - antes usava
            # avaliar_perna_manual, uma cópia divergente que não
            # cobria todos os mercados nem tinha a correção de
            # "jogador ficou no banco sem entrar" que a versão
            # automática já tinha ganho.
            resultados_pernas.append(avaliar_resultado(
                cur, perna["tipo_padrao"], perna.get("jogador_id"), perna["jogo_id"],
                perna.get("linha"), perna.get("descricao"), perna.get("direcao"),
            ))
            continue

        # NOVO (correção): casar por (tipo_padrao, linha, direção) em
        # vez de só descrição em texto - a descrição pode mudar entre
        # o momento em que a aposta foi salva e o momento em que a
        # recomendação é arquivada de verdade (ex: um ajuste como
        # suspensão sendo desativado no meio do caminho muda o texto
        # do sufixo "(ajustado por...)", e o texto salvo nunca mais
        # bate com o texto final - a aposta ficava pendente pra
        # sempre). `linha`/`direcao` identificam o mercado de verdade,
        # sem depender de como ele foi descrito em cada geração.
        # Fallback pra descrição só pra apostas salvas ANTES dessa
        # correção (pernas antigas, sem linha/direcao gravados).
        if perna.get("linha") is not None or perna.get("direcao") is not None:
            cur.execute(
                """SELECT resultado FROM historico_recomendacoes
                   WHERE jogo_id = %s AND tipo_padrao = %s
                     AND jogador_id IS NOT DISTINCT FROM %s
                     AND linha IS NOT DISTINCT FROM %s
                     AND direcao IS NOT DISTINCT FROM %s
                   ORDER BY id DESC LIMIT 1""",
                (perna["jogo_id"], perna["tipo_padrao"], perna.get("jogador_id"),
                 perna.get("linha"), perna.get("direcao")),
            )
        else:
            cur.execute(
                """SELECT resultado FROM historico_recomendacoes
                   WHERE jogo_id = %s AND descricao = %s
                     AND jogador_id IS NOT DISTINCT FROM %s
                   ORDER BY id DESC LIMIT 1""",
                (perna["jogo_id"], perna["descricao"], perna.get("jogador_id")),
            )
        row = cur.fetchone()

        # NOVO (correção, 3ª tentativa - só pra apostas salvas ANTES
        # dessa correção, sem linha/direção gravados): se a descrição
        # exata não bateu, tenta por (jogo_id, tipo_padrao, jogador_id)
        # - só aceita se isso encontrar EXATAMENTE UMA linha em
        # historico_recomendacoes (sem ambiguidade entre "mais de 3.5"
        # e "mais de 4.5" do mesmo jogador/mercado, por exemplo). Corre
        # o risco de não resolver uma aposta salva antiga que tinha
        # mais de uma linha do mesmo mercado disponível - mas é melhor
        # que ficar pendente pra sempre.
        if row is None and perna.get("linha") is None and perna.get("direcao") is None:
            cur.execute(
                """SELECT resultado FROM historico_recomendacoes
                   WHERE jogo_id = %s AND tipo_padrao = %s
                     AND jogador_id IS NOT DISTINCT FROM %s""",
                (perna["jogo_id"], perna["tipo_padrao"], perna.get("jogador_id")),
            )
            candidatos = cur.fetchall()
            if len(candidatos) == 1:
                row = candidatos[0]

        # NOVO (correção, 4ª tentativa): se a 3ª deu ambígua (mais de
        # uma linha candidata pro mesmo mercado/jogo/jogador) ou não
        # achou nada, tenta extrair a linha/direção de dentro do
        # próprio texto salvo (ex: "Mais de 5.5") e casar com precisão
        # - resolve o caso comum de um jogo com várias linhas do mesmo
        # mercado arquivadas (ex: cartão total "mais de 4.5" e "mais
        # de 5.5" do mesmo jogo, cada uma uma recomendação diferente).
        if row is None and perna.get("linha") is None and perna.get("direcao") is None:
            linha_extraida, direcao_extraida = _extrair_linha_direcao_da_descricao(perna.get("descricao"))
            if linha_extraida is not None:
                cur.execute(
                    """SELECT resultado FROM historico_recomendacoes
                       WHERE jogo_id = %s AND tipo_padrao = %s
                         AND jogador_id IS NOT DISTINCT FROM %s
                         AND linha = %s AND direcao = %s
                       ORDER BY id DESC LIMIT 1""",
                    (perna["jogo_id"], perna["tipo_padrao"], perna.get("jogador_id"),
                     linha_extraida, direcao_extraida),
                )
                row = cur.fetchone()

        resultados_pernas.append(row[0] if row else "pendente")

    return resultados_pernas


def resultado_final_das_pernas(resultados_pernas):
    """Regra de múltipla: qualquer perna errada já erra tudo; só acerta se
    TODAS acertaram; se sobrar pendente sem nenhuma errada, ainda não dá
    pra fechar. Devolve None nesse último caso (não resolve ainda)."""
    if any(r == "errou" for r in resultados_pernas):
        return "errou"
    if all(r == "acertou" for r in resultados_pernas):
        return "acertou"
    return None


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

        resultados_pernas = avaliar_pernas_aposta(cur, pernas)
        resultado_final = resultado_final_das_pernas(resultados_pernas)
        if resultado_final is None:
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


def buscar_nomes_jogos(cur, jogo_ids):
    """NOVO: monta um rótulo legível ("Time A x Time B (dd/mm)") pra cada
    jogo_id - usado pra mostrar em /minhas-apostas a qual jogo real cada
    aposta salva se refere. Antes a tela só mostrava a descrição do mercado
    e a data em que a APOSTA foi salva (criado_em), sem indicar o jogo."""
    jogo_ids = [jid for jid in jogo_ids if jid is not None]
    if not jogo_ids:
        return {}
    cur.execute(
        """
        SELECT j.id, tm.nome, tv.nome, j.data_jogo
        FROM jogos j
        LEFT JOIN times tm ON tm.id = j.mandante_id
        LEFT JOIN times tv ON tv.id = j.visitante_id
        WHERE j.id = ANY(%s)
        """,
        (jogo_ids,),
    )
    nomes = {}
    for jogo_id, nome_mandante, nome_visitante, data_jogo in cur.fetchall():
        data_str = data_jogo.strftime("%d/%m") if data_jogo else "?"
        if nome_mandante and nome_visitante:
            nomes[jogo_id] = f"{nome_mandante} x {nome_visitante} ({data_str})"
        else:
            # fallback de segurança - não deveria acontecer em jogo já
            # processado (mandante_id/visitante_id sempre preenchidos),
            # mas evita quebrar a tela se algum dado antigo não tiver isso
            nomes[jogo_id] = f"Jogo de {data_str}"
    return nomes


def buscar_apostas_salvas(cur, usuario_id):
    cur.execute(
        """
        SELECT id, descricao, casa_aposta, odd_combinada, valor_apostado,
               resultado, retorno, criado_em, pernas, resolvido_manualmente
        FROM apostas_salvas
        WHERE usuario_id = %s
        ORDER BY criado_em DESC
        """,
        (usuario_id,),
    )
    colunas = ["id", "descricao", "casa_aposta", "odd_combinada", "valor_apostado",
               "resultado", "retorno", "criado_em", "pernas", "resolvido_manualmente"]
    apostas = [dict(zip(colunas, row)) for row in cur.fetchall()]

    # NOVO: extrai o(s) jogo_id de cada perna (uma aposta pode ser uma
    # múltipla cruzando jogos diferentes, ver seção 6-B do Criador de Odd)
    # e monta o rótulo de cada jogo envolvido, numa única consulta pra
    # todas as apostas da página (evita 1 consulta por aposta).
    todos_jogo_ids = set()
    for a in apostas:
        pernas = a["pernas"] if isinstance(a["pernas"], list) else json.loads(a["pernas"])
        a["_jogo_ids"] = sorted({p.get("jogo_id") for p in pernas if p.get("jogo_id") is not None})
        todos_jogo_ids.update(a["_jogo_ids"])

    nomes_jogos = buscar_nomes_jogos(cur, todos_jogo_ids)
    for a in apostas:
        a["jogos_label"] = " + ".join(
            nomes_jogos.get(jid, f"jogo #{jid}") for jid in a["_jogo_ids"]
        )
        del a["_jogo_ids"]
        del a["pernas"]

    return apostas


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


_MESES_PT = {
    1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril", 5: "maio", 6: "junho",
    7: "julho", 8: "agosto", 9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro",
}


def _periodo_datas_rodada(datas):
    """Formata a janela de datas de uma rodada ('27 a 30 de outubro') a
    partir de uma lista de datetime.date já ordenada. Rodada de 1 jogo só
    (ou todos no mesmo dia) mostra só a data única; janela cruzando meses
    diferentes escreve o mês nas duas pontas."""
    if not datas:
        return ""
    primeira, ultima = datas[0], datas[-1]
    if primeira == ultima:
        return f"{primeira.day} de {_MESES_PT[primeira.month]}"
    if primeira.month == ultima.month:
        return f"{primeira.day} a {ultima.day} de {_MESES_PT[ultima.month]}"
    return f"{primeira.day} de {_MESES_PT[primeira.month]} a {ultima.day} de {_MESES_PT[ultima.month]}"


def buscar_rodadas_para_criador_odd(cur):
    """NOVO (Criador de Odd, 26/08/2026): jogos da TEMPORADA ATUAL dos
    times rastreados, agrupados por rodada - substitui a janela de -45/
    +30 dias que jogava tudo achatado num <select> só (quase impossível
    de achar o jogo certo, especialmente com jogo futuro sem nada a ver
    com o que a pessoa queria anotar misturado junto).

    Usa `jogos.rodada_numero` (coluna já pronta, extraída pelo
    popular_banco.py a partir do texto da API - não precisa reparsear
    aqui). Escopado no ANO CORRENTE, não no histórico completo: rodada é
    numerada de 1 a 38 TODO ano, então sem esse corte a rodada 20 de 2022
    colidiria com a rodada 20 de 2026 no agrupamento."""
    cur.execute(
        """
        SELECT j.id, j.rodada, j.rodada_numero, t.nome, j.adversario, j.data_jogo
        FROM jogos j
        JOIN times t ON t.id = j.nosso_time_id
        WHERE EXTRACT(YEAR FROM j.data_jogo) = EXTRACT(YEAR FROM CURRENT_DATE)
        ORDER BY j.data_jogo
        """
    )

    grupos = {}
    for jogo_id, rodada_texto, rodada_numero, nosso_time, adversario, data_jogo in cur.fetchall():
        chave = rodada_numero if rodada_numero is not None else (rodada_texto or "_sem_rodada_")
        grupo = grupos.setdefault(chave, {
            "rodada_numero": rodada_numero, "rodada_texto": rodada_texto, "datas": [], "jogos": [],
        })
        grupo["datas"].append(data_jogo)
        grupo["jogos"].append({
            "id": jogo_id, "nosso_time": nosso_time, "adversario": adversario, "data_jogo": str(data_jogo),
        })

    rodadas = []
    for grupo in grupos.values():
        if grupo["rodada_numero"] is not None:
            rotulo = f"Rodada {grupo['rodada_numero']}"
        else:
            rotulo = grupo["rodada_texto"] or "Sem rodada identificada"
        rodadas.append({
            "rodada_numero": grupo["rodada_numero"],
            "rotulo": rotulo,
            "periodo": _periodo_datas_rodada(sorted(grupo["datas"])),
            "jogos": grupo["jogos"],  # já vêm ordenados por data_jogo (ORDER BY da query)
        })

    rodadas.sort(key=lambda r: (r["rodada_numero"] is None, -(r["rodada_numero"] or 0)))
    return rodadas


@app.route("/api/jogadores-jogo/<int:jogo_id>")
def api_jogadores_jogo(jogo_id):
    """NOVO (Criador de Odd): lista os jogadores com dado registrado nesse
    jogo específico (os DOIS lados - não só o nosso time, já que dá pra
    apostar em jogador do adversário também) - usado pelo popup só quando
    o mercado escolhido é de jogador, buscado sob demanda (AJAX) porque
    cada jogo tem um elenco diferente."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT jeg.jogador_id, jog.nome
            FROM jogador_estatisticas_jogo jeg
            JOIN jogadores jog ON jog.id = jeg.jogador_id
            WHERE jeg.jogo_id = %s
            ORDER BY jog.nome
            """,
            (jogo_id,),
        )
        jogadores = [{"id": row[0], "nome": row[1]} for row in cur.fetchall()]
        cur.close()
    finally:
        conn.close()
    return jsonify(jogadores)


@app.route("/api/confronto/<int:time_id>/<int:adversario_id>")
def api_confronto_direto(time_id, adversario_id):
    """NOVO (busca de confronto direto): endpoint AJAX chamado pelo campo
    de busca do card "Últimos Jogos" em /time/<id> - devolve TODOS os
    confrontos diretos já concluídos entre os dois times, cada um já com as
    estatísticas completas do jogo embutidas (mesmo formato usado pela
    lista padrão de "Últimos Jogos"). Buscado sob demanda porque só uma
    fração pequena dos confrontos possíveis (10 times rastreados x 20 da
    Série A) vai ser consultada de fato."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        jogos = buscar_confronto_direto(cur, time_id, adversario_id)
        for jogo in jogos:
            jogo["estatisticas"] = buscar_estatisticas_jogo_completo(cur, jogo["jogo_id"], jogo["mandante"])
        cur.close()
    finally:
        conn.close()
    return jsonify(jogos)


@app.route("/api/status-atualizacao")
def api_status_atualizacao():
    """NOVO (acompanhamento em tempo real): responde se existe atualização
    de odds/recomendações rodando agora, há quanto tempo, e quanto ela
    deve demorar no total.

    Consultado pelo polling da página inicial a cada 5 segundos. Lê o
    estado do BANCO (não do threading.Event em memória), então funciona
    igual com qualquer número de workers do Gunicorn - o navegador pode
    cair num worker diferente do que está rodando a thread e ainda assim
    receber a resposta certa.

    Só leitura do ponto de vista do usuário. O único write possível é o
    varredor de execuções abandonadas, que é justamente uma limpeza de
    estado inconsistente (ver marcar_execucoes_abandonadas)."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        execucao = buscar_execucao_em_andamento(cur)
        estimativa = estimar_duracao_atualizacao(cur)
        conn.commit()  # o varredor de abandonadas pode ter escrito
        cur.close()
    finally:
        conn.close()

    if not execucao:
        return jsonify({
            "em_andamento": False,
            "segundos_decorridos": 0,
            "estimativa_segundos": estimativa,
        })

    _, iniciada_em = execucao
    decorridos = int((datetime.now(timezone.utc) - iniciada_em).total_seconds())
    return jsonify({
        "em_andamento": True,
        "segundos_decorridos": max(0, decorridos),
        "estimativa_segundos": estimativa,
    })


@app.route("/api/calibracao/<int:inicio_faixa>")
def api_calibracao_detalhamento(inicio_faixa):
    """NOVO (17/08/2026): endpoint AJAX chamado ao clicar numa faixa do
    card "Acertos e erros por probabilidade histórica" em /historico -
    devolve o detalhamento completo (jogo, mercado, casa, odd,
    probabilidade, VE, resultado) de todas as recomendações avaliadas
    daquela faixa. Buscado sob demanda (só quando o usuário clica pra
    abrir aquela faixa específica), não pré-carregado - evita pesar a
    página conforme o histórico for crescendo com o tempo."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        itens = buscar_detalhamento_faixa(cur, inicio_faixa)
        cur.close()
    finally:
        conn.close()
    for item in itens:
        item["data_jogo"] = str(item["data_jogo"])
        item["odd_oferecida"] = float(item["odd_oferecida"])
        item["probabilidade_historica"] = float(item["probabilidade_historica"])
        item["valor_esperado"] = float(item["valor_esperado"])
    return jsonify(itens)


@app.route("/api/calibracao-odd/<int:indice_faixa>")
def api_calibracao_odd_detalhamento(indice_faixa):
    """NOVO (26/08/2026): mesma ideia de api_calibracao_detalhamento, mas
    pro card separado "Acertos e erros por linha de odd" - indice_faixa é
    inteiro (1.30 vira 13), nunca a odd em si, pra não arriscar
    arredondamento de float na URL."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        itens = buscar_detalhamento_faixa_odd(cur, indice_faixa)
        cur.close()
    finally:
        conn.close()
    for item in itens:
        item["data_jogo"] = str(item["data_jogo"])
        item["odd_oferecida"] = float(item["odd_oferecida"])
        item["probabilidade_historica"] = float(item["probabilidade_historica"])
        item["valor_esperado"] = float(item["valor_esperado"])
    return jsonify(itens)


@app.route("/api/historico-rodadas")
def api_historico_rodadas():
    """NOVO (26/08/2026): endpoint AJAX do botão "Carregar mais rodadas"
    na seção "Odds por Rodada" de /historico - devolve o próximo lote de
    rodadas (QUANTIDADE_RODADAS_POR_CARGA por vez) a partir de `offset`,
    já separadas em acertou/errou. offset conta RODADAS, não itens."""
    offset = request.args.get("offset", 0, type=int)
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        todas_rodadas = buscar_rodadas_ordenadas(cur)
        blocos, tem_mais = montar_blocos_rodadas(cur, todas_rodadas, offset, QUANTIDADE_RODADAS_POR_CARGA)
        cur.close()
    finally:
        conn.close()

    for bloco in blocos:
        for lista in (bloco["acertou"], bloco["errou"]):
            for item in lista:
                item["data_jogo"] = str(item["data_jogo"])
                item["odd_oferecida"] = float(item["odd_oferecida"])
                item["probabilidade_historica"] = float(item["probabilidade_historica"])
                item["valor_esperado"] = float(item["valor_esperado"])
    return jsonify({"blocos": blocos, "tem_mais": tem_mais})


@app.route("/historico/rodada/<rodada_url>/download/<resultado>.csv")
def download_rodada_csv(rodada_url, resultado):
    """NOVO (26/08/2026): baixa em CSV as recomendações individuais
    (nunca múltipla) de UMA rodada, já filtradas por acertou/errou -
    botão "⬇️ Baixar certas"/"⬇️ Baixar erradas" de cada bloco de rodada
    em /historico. `rodada_url` chega aqui já decodificado de volta pro
    texto original de jogos.rodada (o Werkzeug decodifica o segmento da
    URL antes de rotear) - '_sem_rodada_' é o sentinel pro grupo sem
    rodada identificada (rodada NULL no banco)."""
    if resultado not in ("acertou", "errou"):
        return "resultado inválido - use 'acertou' ou 'errou'", 400

    rodada = None if rodada_url == "_sem_rodada_" else rodada_url

    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        itens = buscar_itens_da_rodada(cur, rodada)
        cur.close()
    finally:
        conn.close()
    itens = [i for i in itens if i["resultado"] == resultado]

    # BOM no início: sem isso o Excel abre acento/ç como caractere quebrado
    # em CSV UTF-8 (comportamento conhecido do Excel, não bug do projeto).
    buffer = io.StringIO()
    buffer.write("\ufeff")
    writer = csv.writer(buffer)
    writer.writerow(["Data", "Nosso time", "Adversário", "Mercado", "Casa de apostas",
                      "Odd", "Probabilidade histórica (%)", "Valor esperado", "Resultado"])
    for i in itens:
        writer.writerow([
            i["data_jogo"], i["nosso_time"], i["adversario"], i["descricao"],
            i["casa_aposta"], i["odd_oferecida"], i["probabilidade_historica"],
            i["valor_esperado"], i["resultado"],
        ])

    nome_arquivo = f"odds_{_slug_rodada(rodada)}_{resultado}.csv"
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}"'},
    )


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
        rodadas_criador_odd = buscar_rodadas_para_criador_odd(cur)
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
        jogos_criador_odd=rodadas_criador_odd,
        nav_html=barra_navegacao_nova("roi", round(banca_atual, 2)),
    )


def buscar_totais_apostados(cur, usuario_id):
    """Soma o valor já apostado por (descricao, casa_aposta, jogos), pra
    mostrar um aviso tipo "R$ X já apostado nessa odd" - só das apostas do
    usuário logado (cada um vê só o próprio "já apostado", não o dos
    outros). Não impede apostar de novo na mesma odd, é só informativo.
    CORRIGIDO: antes comparava só (descricao, casa_aposta) - como o texto
    de um mercado (ex: "Cartões Total do Jogo - Mais de 5.5") se repete
    em jogos DIFERENTES, isso fazia o aviso de "já apostado" aparecer em
    jogos que a pessoa nunca apostou, só porque tinha apostado no mesmo
    tipo de mercado em outro jogo. Agora o conjunto de jogos envolvidos
    (via `pernas`) também entra na comparação."""
    cur.execute(
        "SELECT descricao, casa_aposta, pernas, valor_apostado FROM apostas_salvas WHERE usuario_id = %s",
        (usuario_id,),
    )
    totais = {}
    for descricao, casa_aposta, pernas_json, valor_apostado in cur.fetchall():
        pernas = pernas_json if isinstance(pernas_json, list) else json.loads(pernas_json)
        jogos = tuple(sorted({p.get("jogo_id") for p in pernas}))
        chave = (descricao, casa_aposta, jogos)
        totais[chave] = totais.get(chave, 0) + float(valor_apostado)
    return totais


def aplicar_totais_apostados(combinacoes, totais_apostados):
    for c in combinacoes:
        jogos = tuple(sorted({p["jogo_id"] for p in c["pernas"]}))
        c["ja_apostado"] = totais_apostados.get((c["descricao"], c["casa_aposta"], jogos))


PAGINA_JOGADORES = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <link rel="icon" href="/static/marca/favicon.svg" type="image/svg+xml">
    <link rel="icon" href="/static/marca/favicon-32.png" sizes="32x32" type="image/png">
    <link rel="apple-touch-icon" href="/static/marca/apple-touch-icon.png">
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Jogadores - Achismo Zero</title>
""" + FONTES_VISUAL_NOVO + """    <style>
        /* REDESIGN (16/10/2026): lista geral de jogadores no visual novo.
           Dados e comportamento são os de antes (busca na hora, páginas de
           10, líderes, redirecionamento para o último clube aberto). A lista
           de clubes da esquerda virou a fileira de escudos do topo. */
        """ + NAV_CSS + """
""" + CSS_VISUAL_NOVO + """
        .conteudo { width: var(--degrau1); margin: 0 auto; }
        .rotulo {
            font-family: var(--f-corpo); font-size: 0.625rem; letter-spacing: 0; text-transform: none;
            color: var(--apagado);
        }
        .secao-titulo {
            margin: 0 0 0.875rem; font-family: var(--f-corpo); font-weight: 300;
            font-size: clamp(1.125rem, 1.5vw, 1.4375rem); letter-spacing: -.025em; line-height: 1.1;
        }
        .nota { color: var(--apagado); font-size: 0.75rem; line-height: 1.6; margin: -0.375rem 0 1rem; max-width: 52rem; }
        .nota b { color: var(--texto); font-weight: 600; }
        .vidro { background: var(--vidro); border: 1px solid var(--vidro-borda); box-shadow: var(--vidro-sombra); border-radius: var(--raio); }
        .vazio {
            text-align: center; color: var(--apagado); padding: 2rem 1.5rem; font-size: 0.8125rem;
            background: rgba(3, 12, 32, .4); border: 1px dashed var(--linha); border-radius: var(--raio);
        }
        .num { font-family: var(--f-dado); font-variant-numeric: tabular-nums; }
        .escudo { object-fit: contain; flex-shrink: 0; filter: drop-shadow(0 1px 2px rgba(0,0,0,.45)); }

        /* ---------- fileira de escudos (trocar de clube) ---------- */
        .fileira {
            display: flex; align-items: center; justify-content: space-between; gap: 0.25rem; padding: 0.375rem;
            overflow-x: auto; scrollbar-width: thin; margin-bottom: 2rem;
        }
        .fileira-todos {
            flex-shrink: 0; padding: 0.4375rem 0.75rem; margin-right: 0.25rem; border-radius: 999px; white-space: nowrap;
            font-size: 0.75rem; font-weight: 600; color: var(--azul-claro); text-decoration: none;
            border: 1px solid rgba(180, 200, 242, .35);
        }
        .fileira-todos:hover { background: rgba(180, 200, 242, .1); }
        .fileira-todos.ativo { color: var(--texto); border-color: rgba(180, 200, 242, .6); background: rgba(63, 111, 209, .22); }
        .fileira-time {
            flex-shrink: 0; width: 2.375rem; height: 2.375rem; border-radius: 0.625rem; display: grid; place-items: center;
            border: 1px solid transparent; transition: background .15s, border-color .15s, transform .15s;
        }
        .fileira-time img { width: 1.5rem; height: 1.5rem; }
        .fileira-time:hover { background: rgba(255,255,255,.07); transform: translateY(-1px); }
        .fileira-time.ativo { background: rgba(63, 111, 209, .22); border-color: rgba(180, 200, 242, .6); }

        /* ---------- duas colunas ---------- */
        .grade { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 21rem); gap: 2.5rem; align-items: start; }
        .lateral > section { margin-bottom: 2.5rem; }

        .busca {
            width: 100%; margin: 0 0 0.875rem; padding: 0.5625rem 0.875rem;
            background: rgba(1, 5, 15, .55); border: 1px solid var(--linha); color: var(--texto);
            border-radius: 999px; font-family: var(--f-corpo); font-size: 0.8125rem;
            transition: border-color .15s, box-shadow .15s;
        }
        .busca::placeholder { color: #7d849c; }
        .busca:focus { outline: none; border-color: var(--azul-claro); box-shadow: 0 0 0 3px rgba(180, 200, 242, .18); }

        /* ---------- cartão do jogador ---------- */
        .jogador { padding: 1rem 1.125rem 1.125rem; margin-bottom: 0.75rem; }
        .jog-topo { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; flex-wrap: wrap; margin-bottom: 0.875rem; }
        .jog-nome { display: flex; align-items: center; gap: 0.625rem; min-width: 0; }
        .jog-nome .escudo { width: 1.5rem; height: 1.5rem; }
        .jog-nome b { font-size: 0.9375rem; font-weight: 700; }
        .jog-nome span { font-size: 0.75rem; color: var(--apagado); }
        .regua { display: inline-flex; align-items: center; gap: 0.375rem; font-size: 0.6875rem; color: var(--apagado); white-space: nowrap; }
        .regua i { width: 0.5625rem; height: 0.75rem; border-radius: 0.125rem; border: 1px solid rgba(255, 210, 10, .5); display: inline-block; }
        .regua i.cheio { background: var(--ouro); border-color: var(--ouro); }
        .regua.alerta { color: var(--ouro); font-weight: 600; }
        .jog-linhas { display: grid; gap: 0.375rem; }
        .jog-linha { display: grid; grid-template-columns: 8.5rem minmax(0, 1fr); align-items: center; gap: 0.75rem; }
        .jog-linha > span:first-child { font-size: 0.6875rem; color: var(--apagado); }
        .celulas { display: flex; flex-wrap: wrap; gap: 0.3125rem; }
        .cel {
            --p: 0;
            position: relative; display: inline-flex; align-items: baseline; gap: 0.375rem;
            padding: 0.25rem 0.5rem; border-radius: 0.4375rem; font-size: 0.6875rem; white-space: nowrap;
            border: 1px solid var(--linha-fraca);
            background: linear-gradient(90deg, rgba(63, 111, 209, .32) calc(var(--p) * 1%), rgba(255,255,255,.03) calc(var(--p) * 1%));
        }
        .cel.forte { background: linear-gradient(90deg, rgba(255, 210, 10, .26) calc(var(--p) * 1%), rgba(255,255,255,.03) calc(var(--p) * 1%)); }
        .cel span { font-family: var(--f-dado); color: var(--apagado); }
        .cel b { font-family: var(--f-dado); font-weight: 600; color: var(--texto); }
        .jog-extra { margin-top: 0.75rem; padding-top: 0.75rem; border-top: 1px solid var(--linha-fraca); font-size: 0.6875rem; color: var(--apagado); line-height: 1.6; }
        .jog-extra b { font-family: var(--f-dado); font-weight: 500; color: var(--texto); }

        /* paginação */
        .paginacao { display: flex; align-items: center; justify-content: center; gap: 0.875rem; margin-top: 1rem; font-family: var(--f-dado); font-size: 0.6875rem; color: var(--apagado); }
        .btn-pagina {
            background: rgba(255,255,255,.05); border: 1px solid var(--linha); color: var(--texto);
            border-radius: 999px; padding: 0.3125rem 0.75rem; font-family: var(--f-corpo); font-size: 0.75rem; font-weight: 600; cursor: pointer;
        }
        .btn-pagina:hover:not(:disabled) { border-color: var(--azul-claro); background: rgba(180, 200, 242, .1); }
        .btn-pagina:disabled { opacity: .35; cursor: default; }

        /* líderes (lateral) */
        .lideres { display: grid; gap: 0.5rem; }
        .lider { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 0.25rem 0.75rem; padding: 0.75rem 0.875rem; }
        .lider .rotulo { grid-column: 1 / -1; }
        .lider-quem { display: flex; align-items: center; gap: 0.5rem; min-width: 0; font-size: 0.8125rem; font-weight: 600; }
        .lider-quem span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .lider-quem .escudo { width: 1.25rem; height: 1.25rem; }
        .lider-valor { text-align: right; }
        .lider-valor b { font-family: var(--f-dado); font-weight: 500; font-size: 1.25rem; display: block; line-height: 1; }
        .lider-valor small { font-size: 0.625rem; color: var(--apagado); }

        @media (max-width: 1099px) {
            :root { --degrau1: 100%; --degrau2: 100%; }
            .grade { grid-template-columns: minmax(0, 1fr); }
        }
        @media (max-width: 560px) {
            .jog-linha { grid-template-columns: minmax(0, 1fr); gap: 0.25rem; }
        }
        @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }

        /* foto do jogador (pelo servidor; sem foto, ficam as iniciais) */
        .foto {
            position: relative; flex-shrink: 0; width: 2.75rem; height: 2.75rem; border-radius: 50%;
            display: grid; place-items: center; background: linear-gradient(150deg, #1d3a7a, #0a1a3f);
            box-shadow: 0 0 0 1px rgba(180, 200, 242, .35), 0 0.25rem 0.75rem -0.25rem rgba(0,0,0,.6);
        }
        .foto .foto-iniciais { font-family: var(--f-titulo); font-size: 0.75rem; font-weight: 700; color: var(--azul-claro); letter-spacing: .02em; }
        .foto > img:not(.foto-escudo) { position: absolute; inset: 0; width: 100%; height: 100%; border-radius: 50%; object-fit: cover; object-position: center top; background: #e9edf5; }
        .foto-escudo { position: absolute; right: -0.25rem; bottom: -0.25rem; width: 1.125rem; height: 1.125rem; object-fit: contain; filter: drop-shadow(0 1px 2px rgba(0,0,0,.7)); }
        .cabeca { margin: 0 0 1.5rem; }
        .cabeca h1 { margin: 0; font-family: var(--f-corpo); font-weight: 300; font-size: clamp(1.75rem, 2.6vw, 2.5rem); letter-spacing: -.03em; line-height: 1; }
        .cabeca p { margin: 0.375rem 0 0; color: var(--apagado); font-size: 0.8125rem; max-width: 44rem; }
        .cabeca a { color: var(--azul-claro); }
    </style>
</head>
<body>
""" + FUNDO_VISUAL_NOVO + """    {{ nav_html|safe }}
    <script>
        // se a pessoa já tinha um clube aberto e voltou pela barra (sem ?geral=1),
        // leva direto para ele; "Todos os jogadores" (com ?geral=1) é a saída
        if (!window.location.search.includes('geral=1')) {
            try {
                const clubeId = localStorage.getItem('ultimo_clube_id');
                if (clubeId) { window.location.replace('/clube/' + encodeURIComponent(clubeId)); }
            } catch (e) {}
        }
    </script>
    {% macro foto(j, escudo=None) %}
        {%- set partes = j.nome.replace('.', ' ').split() -%}
        <span class="foto" aria-hidden="true">
            <span class="foto-iniciais">{{ (partes[0][0] ~ (partes[-1][0] if partes|length > 1 else '')) | upper }}</span>
            {% if j.jogador_id %}<img src="/foto-jogador/{{ j.jogador_id }}.png" alt="" loading="lazy" onerror="this.remove()">{% endif %}
            {% if escudo %}<img src="{{ escudo }}" alt="" class="foto-escudo" loading="lazy">{% endif %}
        </span>
    {% endmacro %}
    {% macro dec(v, casas=0) %}{{ (('%.' ~ casas ~ 'f')|format(v)).replace('.', ',') }}{% endmacro %}
    {% set escudos = {} %}{% for c in clubes %}{% set _ = escudos.update({c.nome: c.escudo_url}) %}{% endfor %}

    <main class="conteudo">
        <nav class="vidro fileira" aria-label="Escolher clube">
            <a href="/jogadores?geral=1" class="fileira-todos ativo" aria-current="page">Todos os jogadores</a>
            {% for c in clubes %}
            <a href="/clube/{{ c.id }}" class="fileira-time" title="{{ c.nome }}" aria-label="{{ c.nome }}">
                {% if c.escudo_url %}<img src="{{ c.escudo_url }}" alt="" class="escudo" loading="lazy">{% else %}<span class="rotulo">{{ c.nome[:3] }}</span>{% endif %}
            </a>
            {% endfor %}
        </nav>

        <div class="cabeca">
            <h1>Jogadores</h1>
            <p>Em quantos dos últimos jogos cada jogador passou de cada linha, entre todos os clubes rastreados. Clique num escudo para ver o elenco do clube e montar apostas. Estatística do time inteiro fica em <a href="/times">Times</a>.</p>
        </div>

        <div class="grade">
            <section aria-labelledby="t-lista">
                <h2 class="secao-titulo" id="t-lista">Todos os jogadores <span class="rotulo" style="margin-left: 0.5rem">{{ jogadores|length }}</span></h2>
                <input type="search" class="busca" id="busca" placeholder="Buscar jogador (ex: Yuri Alberto)" aria-label="Buscar jogador" oninput="filtrar()">
                <div id="lista">
                {% if jogadores %}
                    {% for j in jogadores %}
                    <article class="vidro jogador jogador-card item-pagina">
                        <div class="jog-topo">
                            <div class="jog-nome">
                                {{ foto(j, escudos.get(j.time_nome)) }}
                                <div><b class="nome-jogador">{{ j.nome }}</b><br><span>{{ j.time_nome }}</span></div>
                            </div>
                            <span class="regua{{ ' alerta' if j.cartoes_suspensao == 2 else '' }}" title="Cartões rumo à suspensão (zera depois de cumprir)">
                                {% for k in range(3) %}<i class="{{ 'cheio' if k < j.cartoes_suspensao else '' }}"></i>{% endfor %}
                                {{ 'a 1 cartão da suspensão' if j.cartoes_suspensao == 2 else (j.cartoes_suspensao ~ '/3 cartões') }}
                            </span>
                        </div>
                        <div class="jog-linhas">
                            {% if j.cartao or j.impedimento %}
                            <div class="jog-linha">
                                <span>Cartão · impedimento</span>
                                <div class="celulas">
                                    {% if j.cartao %}<span class="cel" style="--p: {{ j.cartao.frequencia }}" title="últimos {{ j.cartao.jogos_analisados }} jogos"><span>cartão</span><b>{{ dec(j.cartao.frequencia) }}%</b></span>{% endif %}
                                    {% if j.impedimento %}<span class="cel" style="--p: {{ j.impedimento.frequencia }}" title="últimos {{ j.impedimento.jogos_analisados }} jogos"><span>impedimento</span><b>{{ dec(j.impedimento.frequencia) }}%</b></span>{% endif %}
                                </div>
                            </div>
                            {% endif %}
                            {% for bloco in j.blocos_linha|sort(attribute='titulo') %}
                            <div class="jog-linha">
                                <span>{{ bloco.titulo }}</span>
                                <div class="celulas">
                                    {% for item in bloco.itens %}<span class="cel{{ ' forte' if item.frequencia >= 60 else '' }}" style="--p: {{ item.frequencia }}"><span>+{{ dec(item.linha, 1) }}</span><b>{{ dec(item.frequencia) }}%</b></span>{% endfor %}
                                </div>
                            </div>
                            {% endfor %}
                        </div>
                    </article>
                    {% endfor %}
                {% else %}
                    <div class="vazio">Ainda não há padrões calculados para nenhum jogador.</div>
                {% endif %}
                </div>
                <div class="vazio" id="nenhum-resultado" hidden>Nenhum jogador encontrado com esse nome.</div>
                {% if jogadores|length > 10 %}
                <div class="paginacao">
                    <button type="button" class="btn-pagina" id="anterior-lista" onclick="mudarPagina('lista', -1)">Anterior</button>
                    <span id="label-lista"></span>
                    <button type="button" class="btn-pagina" id="proximo-lista" onclick="mudarPagina('lista', 1)">Próxima</button>
                </div>
                {% endif %}
            </section>

            <aside class="lateral">
                <section aria-labelledby="t-lideres">
                    <h2 class="secao-titulo" id="t-lideres">Líderes do campeonato</h2>
                    <p class="nota">Entre os jogadores dos clubes rastreados: o total somado e a média nos últimos 5 jogos.</p>
                    <div class="lideres">
                        {% for l in lideres %}
                        <div class="vidro lider">
                            <span class="rotulo">{{ l.titulo }}</span>
                            {% if l.jogador_nome %}
                            <div class="lider-quem">
                                {% if l.time_nome and escudos.get(l.time_nome) %}<img src="{{ escudos.get(l.time_nome) }}" class="escudo" alt="" title="{{ l.time_nome }}">{% endif %}
                                <span>{{ l.jogador_nome }}</span>
                            </div>
                            <div class="lider-valor"><b>{{ l.total }}</b><small>últimos 5: {{ dec(l.media_5 or 0, 1) }}</small></div>
                            {% else %}
                            <div class="lider-quem" style="color: var(--apagado); font-weight: 400">Ainda não há dados suficientes.</div>
                            {% endif %}
                        </div>
                        {% endfor %}
                    </div>
                </section>
            </aside>
        </div>
    </main>

    <script>
        function filtrar() {
            const termo = document.getElementById('busca').value.toLowerCase().trim();
            let visiveis = 0;
            document.querySelectorAll('.jogador-card').forEach(function(card) {
                const bate = card.querySelector('.nome-jogador').textContent.toLowerCase().includes(termo);
                card.dataset.escondidoBusca = bate ? '0' : '1';
                if (bate) visiveis++;
            });
            document.getElementById('nenhum-resultado').hidden = !(termo && visiveis === 0);
            paginaAtual['lista'] = 0;
            renderizarPagina('lista');
        }

        const TAMANHO_PAGINA = 10;
        const paginaAtual = {};
        function salvarPaginaNoNavegador(listaId, pagina) {
            try { localStorage.setItem('pagina:' + window.location.pathname + ':' + listaId, String(pagina)); } catch (e) {}
        }
        function carregarPaginaDoNavegador(listaId) {
            try {
                const v = localStorage.getItem('pagina:' + window.location.pathname + ':' + listaId);
                return v !== null ? parseInt(v, 10) : 0;
            } catch (e) { return 0; }
        }
        function totalPaginas(listaId) {
            let n = 0;
            document.querySelectorAll('#' + listaId + ' .item-pagina').forEach(function(item) {
                if (item.dataset.escondidoBusca !== '1') n++;
            });
            return Math.max(1, Math.ceil(n / TAMANHO_PAGINA));
        }
        function renderizarPagina(listaId) {
            if (!(listaId in paginaAtual)) { paginaAtual[listaId] = carregarPaginaDoNavegador(listaId); }
            const total = totalPaginas(listaId);
            if (paginaAtual[listaId] > total - 1) paginaAtual[listaId] = total - 1;
            if (paginaAtual[listaId] < 0) paginaAtual[listaId] = 0;
            const pagina = paginaAtual[listaId];
            let visivelIndice = 0;
            document.querySelectorAll('#' + listaId + ' .item-pagina').forEach(function(item) {
                if (item.dataset.escondidoBusca === '1') { item.style.display = 'none'; return; }
                item.style.display = (Math.floor(visivelIndice / TAMANHO_PAGINA) === pagina) ? '' : 'none';
                visivelIndice++;
            });
            const label = document.getElementById('label-' + listaId);
            if (label) label.textContent = 'Página ' + (pagina + 1) + ' de ' + total;
            const a = document.getElementById('anterior-' + listaId), p = document.getElementById('proximo-' + listaId);
            if (a) a.disabled = (pagina === 0);
            if (p) p.disabled = (pagina >= total - 1);
        }
        function mudarPagina(listaId, direcao) {
            const total = totalPaginas(listaId);
            const pagina = Math.max(0, Math.min(total - 1, (paginaAtual[listaId] || 0) + direcao));
            paginaAtual[listaId] = pagina;
            salvarPaginaNoNavegador(listaId, pagina);
            renderizarPagina(listaId);
        }
        renderizarPagina('lista');
    </script>
""" + SCRIPT_FUNDO_VIVO + """</body>
</html>
"""

PAGINA_CLUBE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <link rel="icon" href="/static/marca/favicon.svg" type="image/svg+xml">
    <link rel="icon" href="/static/marca/favicon-32.png" sizes="32x32" type="image/png">
    <link rel="apple-touch-icon" href="/static/marca/apple-touch-icon.png">
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ nome_clube }} · Jogadores - Achismo Zero</title>
""" + FONTES_VISUAL_NOVO + """    <style>
        /* REDESIGN (16/10/2026): jogadores de um clube no visual novo. Dados
           e comportamento são os de antes: escalação, próximo jogo, contexto,
           régua de cartões, seleção de estatísticas para aposta manual
           (carrinho, modal e "3 apostas automáticas", mesmos campos e a mesma
           rota /salvar-aposta), últimos 5 jogos de cada jogador, elenco e
           líderes do time. */
        """ + NAV_CSS + """
""" + CSS_VISUAL_NOVO + """
        .conteudo { width: var(--degrau1); margin: 0 auto; }
        .rotulo {
            font-family: var(--f-corpo); font-size: 0.625rem; letter-spacing: 0; text-transform: none;
            color: var(--apagado);
        }
        .secao-titulo {
            margin: 0 0 0.875rem; font-family: var(--f-corpo); font-weight: 300;
            font-size: clamp(1.125rem, 1.5vw, 1.4375rem); letter-spacing: -.025em; line-height: 1.1;
        }
        .nota { color: var(--apagado); font-size: 0.75rem; line-height: 1.6; margin: -0.375rem 0 1rem; max-width: 52rem; }
        .nota b { color: var(--texto); font-weight: 600; }
        .vidro { background: var(--vidro); border: 1px solid var(--vidro-borda); box-shadow: var(--vidro-sombra); border-radius: var(--raio); }
        .vazio {
            text-align: center; color: var(--apagado); padding: 2rem 1.5rem; font-size: 0.8125rem;
            background: rgba(3, 12, 32, .4); border: 1px dashed var(--linha); border-radius: var(--raio);
        }
        .num { font-family: var(--f-dado); font-variant-numeric: tabular-nums; }
        .escudo { object-fit: contain; flex-shrink: 0; filter: drop-shadow(0 1px 2px rgba(0,0,0,.45)); }

        /* ---------- fileira de escudos (trocar de clube) ---------- */
        .fileira {
            display: flex; align-items: center; justify-content: space-between; gap: 0.25rem; padding: 0.375rem;
            overflow-x: auto; scrollbar-width: thin; margin-bottom: 2rem;
        }
        .fileira-todos {
            flex-shrink: 0; padding: 0.4375rem 0.75rem; margin-right: 0.25rem; border-radius: 999px; white-space: nowrap;
            font-size: 0.75rem; font-weight: 600; color: var(--azul-claro); text-decoration: none;
            border: 1px solid rgba(180, 200, 242, .35);
        }
        .fileira-todos:hover { background: rgba(180, 200, 242, .1); }
        .fileira-todos.ativo { color: var(--texto); border-color: rgba(180, 200, 242, .6); background: rgba(63, 111, 209, .22); }
        .fileira-time {
            flex-shrink: 0; width: 2.375rem; height: 2.375rem; border-radius: 0.625rem; display: grid; place-items: center;
            border: 1px solid transparent; transition: background .15s, border-color .15s, transform .15s;
        }
        .fileira-time img { width: 1.5rem; height: 1.5rem; }
        .fileira-time:hover { background: rgba(255,255,255,.07); transform: translateY(-1px); }
        .fileira-time.ativo { background: rgba(63, 111, 209, .22); border-color: rgba(180, 200, 242, .6); }

        /* ---------- duas colunas ---------- */
        .grade { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 21rem); gap: 2.5rem; align-items: start; }
        .lateral > section { margin-bottom: 2.5rem; }

        .busca {
            width: 100%; margin: 0 0 0.875rem; padding: 0.5625rem 0.875rem;
            background: rgba(1, 5, 15, .55); border: 1px solid var(--linha); color: var(--texto);
            border-radius: 999px; font-family: var(--f-corpo); font-size: 0.8125rem;
            transition: border-color .15s, box-shadow .15s;
        }
        .busca::placeholder { color: #7d849c; }
        .busca:focus { outline: none; border-color: var(--azul-claro); box-shadow: 0 0 0 3px rgba(180, 200, 242, .18); }

        /* ---------- cartão do jogador ---------- */
        .jogador { padding: 1rem 1.125rem 1.125rem; margin-bottom: 0.75rem; }
        .jog-topo { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; flex-wrap: wrap; margin-bottom: 0.875rem; }
        .jog-nome { display: flex; align-items: center; gap: 0.625rem; min-width: 0; }
        .jog-nome .escudo { width: 1.5rem; height: 1.5rem; }
        .jog-nome b { font-size: 0.9375rem; font-weight: 700; }
        .jog-nome span { font-size: 0.75rem; color: var(--apagado); }
        .regua { display: inline-flex; align-items: center; gap: 0.375rem; font-size: 0.6875rem; color: var(--apagado); white-space: nowrap; }
        .regua i { width: 0.5625rem; height: 0.75rem; border-radius: 0.125rem; border: 1px solid rgba(255, 210, 10, .5); display: inline-block; }
        .regua i.cheio { background: var(--ouro); border-color: var(--ouro); }
        .regua.alerta { color: var(--ouro); font-weight: 600; }
        .jog-linhas { display: grid; gap: 0.375rem; }
        .jog-linha { display: grid; grid-template-columns: 8.5rem minmax(0, 1fr); align-items: center; gap: 0.75rem; }
        .jog-linha > span:first-child { font-size: 0.6875rem; color: var(--apagado); }
        .celulas { display: flex; flex-wrap: wrap; gap: 0.3125rem; }
        .cel {
            --p: 0;
            position: relative; display: inline-flex; align-items: baseline; gap: 0.375rem;
            padding: 0.25rem 0.5rem; border-radius: 0.4375rem; font-size: 0.6875rem; white-space: nowrap;
            border: 1px solid var(--linha-fraca);
            background: linear-gradient(90deg, rgba(63, 111, 209, .32) calc(var(--p) * 1%), rgba(255,255,255,.03) calc(var(--p) * 1%));
        }
        .cel.forte { background: linear-gradient(90deg, rgba(255, 210, 10, .26) calc(var(--p) * 1%), rgba(255,255,255,.03) calc(var(--p) * 1%)); }
        .cel span { font-family: var(--f-dado); color: var(--apagado); }
        .cel b { font-family: var(--f-dado); font-weight: 600; color: var(--texto); }
        .jog-extra { margin-top: 0.75rem; padding-top: 0.75rem; border-top: 1px solid var(--linha-fraca); font-size: 0.6875rem; color: var(--apagado); line-height: 1.6; }
        .jog-extra b { font-family: var(--f-dado); font-weight: 500; color: var(--texto); }

        /* paginação */
        .paginacao { display: flex; align-items: center; justify-content: center; gap: 0.875rem; margin-top: 1rem; font-family: var(--f-dado); font-size: 0.6875rem; color: var(--apagado); }
        .btn-pagina {
            background: rgba(255,255,255,.05); border: 1px solid var(--linha); color: var(--texto);
            border-radius: 999px; padding: 0.3125rem 0.75rem; font-family: var(--f-corpo); font-size: 0.75rem; font-weight: 600; cursor: pointer;
        }
        .btn-pagina:hover:not(:disabled) { border-color: var(--azul-claro); background: rgba(180, 200, 242, .1); }
        .btn-pagina:disabled { opacity: .35; cursor: default; }

        /* líderes (lateral) */
        .lideres { display: grid; gap: 0.5rem; }
        .lider { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 0.25rem 0.75rem; padding: 0.75rem 0.875rem; }
        .lider .rotulo { grid-column: 1 / -1; }
        .lider-quem { display: flex; align-items: center; gap: 0.5rem; min-width: 0; font-size: 0.8125rem; font-weight: 600; }
        .lider-quem span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .lider-quem .escudo { width: 1.25rem; height: 1.25rem; }
        .lider-valor { text-align: right; }
        .lider-valor b { font-family: var(--f-dado); font-weight: 500; font-size: 1.25rem; display: block; line-height: 1; }
        .lider-valor small { font-size: 0.625rem; color: var(--apagado); }

        @media (max-width: 1099px) {
            :root { --degrau1: 100%; --degrau2: 100%; }
            .grade { grid-template-columns: minmax(0, 1fr); }
        }
        @media (max-width: 560px) {
            .jog-linha { grid-template-columns: minmax(0, 1fr); gap: 0.25rem; }
        }
        @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }

        /* foto do jogador (pelo servidor; sem foto, ficam as iniciais) */
        .foto {
            position: relative; flex-shrink: 0; width: 2.75rem; height: 2.75rem; border-radius: 50%;
            display: grid; place-items: center; background: linear-gradient(150deg, #1d3a7a, #0a1a3f);
            box-shadow: 0 0 0 1px rgba(180, 200, 242, .35), 0 0.25rem 0.75rem -0.25rem rgba(0,0,0,.6);
        }
        .foto .foto-iniciais { font-family: var(--f-titulo); font-size: 0.75rem; font-weight: 700; color: var(--azul-claro); letter-spacing: .02em; }
        .foto > img:not(.foto-escudo) { position: absolute; inset: 0; width: 100%; height: 100%; border-radius: 50%; object-fit: cover; object-position: center top; background: #e9edf5; }
        .foto-escudo { position: absolute; right: -0.25rem; bottom: -0.25rem; width: 1.125rem; height: 1.125rem; object-fit: contain; filter: drop-shadow(0 1px 2px rgba(0,0,0,.7)); }
        button {
            background: var(--ouro); color: var(--cta-texto); border: 1px solid transparent; border-radius: 999px;
            padding: 0.5rem 1rem; font-family: var(--f-corpo); font-size: 0.8125rem; font-weight: 600;
            cursor: pointer; transition: background .15s, transform .1s;
        }
        button:hover { background: var(--ouro-claro); }
        button:active { transform: translateY(1px); }

        /* cabeçalho do clube */
        .heroi { display: flex; align-items: center; gap: 1.5rem; flex-wrap: wrap; margin-bottom: 1.5rem; }
        .heroi .escudo-grande { width: 4.5rem; height: 4.5rem; }
        .heroi h1 { margin: 0; font-family: var(--f-corpo); font-weight: 300; font-size: clamp(2rem, 3.2vw, 3rem); letter-spacing: -.035em; line-height: 1; }
        .heroi h1 small { display: block; font-size: 0.8125rem; color: var(--apagado); letter-spacing: 0; margin-top: 0.375rem; }

        /* faixa de cima: próximo jogo, contexto, avisos */
        .faixa { display: grid; grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr)); gap: 0.75rem; margin-bottom: 1.25rem; }
        .faixa > div { padding: 0.875rem 1rem; }
        .faixa .rotulo { display: block; margin-bottom: 0.375rem; }
        .faixa-valor { font-size: 0.875rem; font-weight: 600; }
        .confronto { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
        .confronto .escudo { width: 1.75rem !important; height: 1.75rem !important; margin: 0 !important; vertical-align: 0 !important; }
        .confronto i { font-style: normal; font-family: var(--f-dado); font-weight: 400; color: var(--apagado); font-size: 0.75rem; }
        .faixa-valor .escudo { width: 1.25rem; height: 1.25rem; vertical-align: -0.3125rem; margin: 0 0.25rem; }
        .faixa-detalhe { font-size: 0.6875rem; color: var(--apagado); margin-top: 0.25rem; line-height: 1.5; }
        .faixa-detalhe b { color: var(--texto); font-family: var(--f-dado); font-weight: 500; }
        .aviso-ouro { border-color: rgba(255, 210, 10, .45) !important; background: linear-gradient(140deg, rgba(255, 210, 10, .12), rgba(255, 210, 10, .03)), rgba(4, 14, 38, .42) !important; }
        .aviso-ouro .faixa-valor { color: var(--ouro); }
        .escalacao { display: flex; flex-wrap: wrap; gap: 0.3125rem; margin-top: 0.25rem; }
        .escalacao span { font-size: 0.6875rem; padding: 0.1875rem 0.5rem; border-radius: 999px; background: rgba(255,255,255,.06); border: 1px solid var(--linha-fraca); }

        .acoes { display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; margin-bottom: 1.5rem; }
        .acoes p { margin: 0 0 0 0.25rem; font-size: 0.6875rem; color: var(--apagado); }
        .btn-contorno { background: transparent; color: var(--azul-claro); border: 1px solid rgba(180, 200, 242, .55); }
        .btn-contorno:hover { background: rgba(180, 200, 242, .1); }

        /* células que se escolhem (com próximo jogo) */
        label.cel { cursor: pointer; user-select: none; transition: border-color .15s, box-shadow .15s; }
        label.cel:hover { border-color: rgba(180, 200, 242, .55); }
        label.cel input { position: absolute; opacity: 0; width: 1px; height: 1px; pointer-events: none; }
        label.cel:has(input:checked) { border-color: var(--ouro); box-shadow: 0 0 0 1px var(--ouro) inset; }
        label.cel:has(input:focus-visible) { outline: 2px solid var(--azul-claro); outline-offset: 2px; }
        label.cel:has(input:checked) b { color: var(--ouro); }

        /* últimos 5 jogos do jogador */
        .ult-botao {
            margin-top: 0.75rem; padding: 0; background: none; border: 0; color: var(--azul-claro);
            font-size: 0.6875rem; font-weight: 600; display: inline-flex; align-items: center; gap: 0.375rem; cursor: pointer;
        }
        .ult-botao:hover { background: none; color: var(--texto); }
        .seta {
            width: 0.4375rem; height: 0.4375rem; border-right: 2px solid currentColor; border-bottom: 2px solid currentColor;
            transform: translateY(-0.125rem) rotate(45deg); transition: transform .2s; display: inline-block;
        }
        .ultimos-jogos-jogador-lista { display: none; margin-top: 0.625rem; }
        .ultimos-jogos-jogador-lista.aberto { display: grid; gap: 0.375rem; }
        .ult-jogo { border: 1px solid var(--linha-fraca); border-radius: 0.5rem; background: rgba(1, 5, 15, .35); cursor: pointer; }
        .ult-linha { display: flex; justify-content: space-between; align-items: center; gap: 0.75rem; padding: 0.4375rem 0.625rem; font-size: 0.6875rem; }
        .ult-linha .data { font-family: var(--f-dado); color: var(--apagado); white-space: nowrap; }
        .ult-detalhe { display: none; padding: 0 0.625rem 0.5rem; }
        .ult-jogo.aberto .ult-detalhe { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0.25rem 0.75rem; }
        .ult-detalhe div { font-size: 0.625rem; color: var(--apagado); }
        .ult-detalhe b { display: block; font-family: var(--f-dado); font-size: 0.8125rem; font-weight: 500; color: var(--texto); }

        /* elenco */
        .elenco { padding: 0.875rem 1rem; }
        .elenco-grupo + .elenco-grupo { margin-top: 0.875rem; padding-top: 0.875rem; border-top: 1px solid var(--linha-fraca); }
        .elenco-grupo .rotulo { display: block; margin-bottom: 0.4375rem; }
        .elenco-nomes { display: flex; flex-wrap: wrap; gap: 0.25rem 0.75rem; font-size: 0.75rem; }
        .elenco-nomes span.regua-2 { color: var(--ouro); }

        /* carrinho e janelas */
        .carrinho-flutuante {
            position: fixed; left: 50%; transform: translateX(-50%);
            bottom: calc(1.25rem + env(safe-area-inset-bottom, 0px)); z-index: 90;
            display: none; align-items: center; gap: 1rem; padding: 0.625rem 0.625rem 0.625rem 1.125rem; border-radius: 999px;
            background: linear-gradient(140deg, rgba(255,255,255,.16), rgba(255,255,255,.05)), rgba(4, 14, 38, .92);
            border: 1px solid rgba(255, 210, 10, .55); box-shadow: 0 1rem 3rem -0.75rem rgba(0,0,0,.8);
        }
        .carrinho-flutuante.ativo { display: flex; }
        .carrinho-contagem { font-size: 0.8125rem; }
        .carrinho-contagem b { font-family: var(--f-dado); color: var(--ouro); }
        .modal-fundo {
            display: none; position: fixed; inset: 0; z-index: 100; align-items: center; justify-content: center;
            background: rgba(1, 5, 15, .72); padding: 1rem;
        }
        .modal-caixa {
            width: 100%; max-width: 28rem; max-height: 85vh; overflow-y: auto; padding: 1.25rem 1.375rem;
            border-radius: var(--raio); border: 1px solid var(--vidro-borda); box-shadow: var(--vidro-sombra);
            background: linear-gradient(140deg, rgba(255,255,255,.1), rgba(255,255,255,.03)), #06122e;
        }
        .modal-caixa-grande { max-width: 46rem; }
        .modal-topo { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.875rem; }
        .modal-titulo { font-size: 1.125rem; font-weight: 300; letter-spacing: -.02em; }
        .modal-fechar { background: none; border: 0; color: var(--apagado); font-size: 0.75rem; font-weight: 600; padding: 0.25rem; cursor: pointer; }
        .modal-fechar:hover { background: none; color: var(--texto); }
        .modal-pernas-lista, .retangulo-pernas { font-size: 0.75rem; color: var(--apagado); line-height: 1.7; max-height: 10rem; overflow-y: auto; }
        .retangulo-prob { font-family: var(--f-dado); font-size: 0.8125rem; color: var(--ouro); margin: 0.625rem 0; }
        .modal-form-linha { display: flex; gap: 0.5rem; margin-top: 0.75rem; flex-wrap: wrap; }
        .modal-form-linha input[type=number] {
            flex: 1; min-width: 7rem; padding: 0.5rem 0.75rem; border-radius: 0.5rem;
            background: rgba(1, 5, 15, .55); border: 1px solid var(--linha); color: var(--texto);
            font-family: var(--f-dado); font-size: 0.8125rem;
        }
        .modal-form-linha input[type=number]:focus { outline: none; border-color: var(--azul-claro); box-shadow: 0 0 0 3px rgba(180, 200, 242, .18); }
        .btn-salvar { width: 100%; background: var(--azul); color: #fff; }
        .btn-salvar:hover { background: #5683dd; }
        .aviso-tres-apostas { color: var(--apagado); font-size: 0.75rem; margin: 0 0 1rem; }
        .retangulo-aposta { padding: 1rem 1.125rem; margin-bottom: 0.75rem; border-radius: 0.75rem; border: 1px solid var(--linha); background: rgba(1, 5, 15, .4); }
        .retangulo-titulo { font-weight: 600; font-size: 0.875rem; margin-bottom: 0.375rem; }
        .retangulo-vazio { color: var(--apagado); font-size: 0.75rem; font-style: italic; }
        .flash { padding: 0.625rem 0.875rem; border-radius: 0.625rem; margin-bottom: 1.25rem; font-size: 0.75rem; }
        .flash-erro { background: rgba(255, 122, 122, .12); color: var(--vermelho); border: 1px solid rgba(255, 122, 122, .4); }
        .flash-sucesso { background: rgba(95, 211, 141, .12); color: var(--verde); border: 1px solid rgba(95, 211, 141, .4); }
        @media (max-width: 1099px) { .lateral { order: 2; } }
        @media (max-width: 640px) {
            .ult-jogo.aberto .ult-detalhe { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .heroi .escudo-grande { width: 3.25rem; height: 3.25rem; }
        }
    </style>
</head>
<body>
""" + FUNDO_VISUAL_NOVO + """    {{ nav_html|safe }}
    <script>
        try { localStorage.setItem('ultimo_clube_id', '{{ time_id }}'); } catch (e) {}
    </script>
    {% macro foto(j, escudo=None) %}
        {%- set partes = j.nome.replace('.', ' ').split() -%}
        <span class="foto" aria-hidden="true">
            <span class="foto-iniciais">{{ (partes[0][0] ~ (partes[-1][0] if partes|length > 1 else '')) | upper }}</span>
            {% if j.jogador_id %}<img src="/foto-jogador/{{ j.jogador_id }}.png" alt="" loading="lazy" onerror="this.remove()">{% endif %}
            {% if escudo %}<img src="{{ escudo }}" alt="" class="foto-escudo" loading="lazy">{% endif %}
        </span>
    {% endmacro %}
    {% macro dec(v, casas=0) %}{{ (('%.' ~ casas ~ 'f')|format(v)).replace('.', ',') }}{% endmacro %}
    {% macro data_br(d) %}{% if d is string %}{{ d[8:10] }}/{{ d[5:7] }}/{{ d[0:4] }}{% elif d %}{{ d.strftime('%d/%m/%Y') }}{% endif %}{% endmacro %}
    {% macro celula(j, tipo, linha, direcao, freq, desc, rotulo, forte=false) %}
        {% if proximo_jogo %}
        <label class="cel{{ ' forte' if forte else '' }}" style="--p: {{ freq }}">
            <input type="checkbox" class="item-selecionavel"
                data-jogador-id="{{ j.jogador_id }}" data-jogador-nome="{{ j.nome }}"
                data-tipo="{{ tipo }}" data-linha="{{ linha }}" data-direcao="{{ direcao }}"
                data-frequencia="{{ freq }}" data-descricao="{{ desc }}"
                onchange="atualizarCarrinho()">
            <span>{{ rotulo }}</span><b>{{ dec(freq) }}%</b>
        </label>
        {% else %}
        <span class="cel{{ ' forte' if forte else '' }}" style="--p: {{ freq }}"><span>{{ rotulo }}</span><b>{{ dec(freq) }}%</b></span>
        {% endif %}
    {% endmacro %}
    {% set na_regua = jogadores_na_regua or [] %}

    <main class="conteudo">
        {% with mensagens = get_flashed_messages(with_categories=true) %}
            {% for categoria, texto in mensagens %}
            <div class="flash flash-{{ categoria }}">{{ texto }}</div>
            {% endfor %}
        {% endwith %}

        <nav class="vidro fileira" aria-label="Escolher clube">
            <a href="/jogadores?geral=1" class="fileira-todos">Todos os jogadores</a>
            {% for c in clubes %}
            <a href="/clube/{{ c.id }}" class="fileira-time{{ ' ativo' if c.id == time_id else '' }}" title="{{ c.nome }}" aria-label="{{ c.nome }}"{{ ' aria-current="page"'|safe if c.id == time_id else '' }}>
                {% if c.escudo_url %}<img src="{{ c.escudo_url }}" alt="" class="escudo" loading="lazy">{% else %}<span class="rotulo">{{ c.nome[:3] }}</span>{% endif %}
            </a>
            {% endfor %}
        </nav>

        <header class="heroi">
            {% if escudo_url %}<img src="{{ escudo_url }}" alt="" class="escudo escudo-grande" onerror="this.style.display='none'">{% endif %}
            <h1>{{ nome_clube }}<small>Jogadores · {{ jogadores|length }} com histórico</small></h1>
        </header>

        <div class="faixa">
            {% if proximo_jogo %}
            <div class="vidro">
                <span class="rotulo">Próximo jogo</span>
                <div class="faixa-valor confronto">
                    {% if escudo_url %}<img src="{{ escudo_url }}" alt="" class="escudo" onerror="this.style.visibility='hidden'">{% endif %}
                    <span>{{ nome_clube }}</span><i>x</i><span>{{ proximo_jogo.adversario }}</span>
                    {% if proximo_jogo.adversario_api_id %}<img src="/escudo/{{ proximo_jogo.adversario_api_id }}.png" alt="" class="escudo" onerror="this.style.visibility='hidden'">{% endif %}
                </div>
                <div class="faixa-detalhe"><b>{{ data_br(proximo_jogo.data_jogo) }}</b>{% if proximo_jogo.rodada_numero %} · rodada {{ proximo_jogo.rodada_numero }}{% endif %} · as apostas manuais desta página são para esse jogo</div>
            </div>
            {% endif %}
            {% if contexto_jogo %}
            <div class="vidro">
                <span class="rotulo">Contexto</span>
                <div class="faixa-valor">{{ contexto_jogo.posicao }}º · {{ contexto_jogo.pontos }} pts</div>
                <div class="faixa-detalhe">
                    {% if contexto_jogo.distancia_objetivo is not none and contexto_jogo.distancia_objetivo > 0 %}a <b>{{ contexto_jogo.distancia_objetivo }}</b> pts de {{ contexto_jogo.objetivo }}{% elif contexto_jogo.zona == 'g4' %}já está no G4{% endif %}{% if contexto_jogo.distancia_z4 is not none %} · <b>{{ contexto_jogo.distancia_z4 }}</b> pts acima do Z4{% endif %}
                    · adversário em {{ contexto_jogo.adversario_posicao }}º{% if contexto_jogo.adversario_forca %} ({{ contexto_jogo.adversario_forca }}){% endif %}
                </div>
            </div>
            {% endif %}
            {% if jogo_morto and jogo_morto.morto %}
            <div class="vidro aviso-ouro">
                <span class="rotulo">Jogo sem nada em jogo</span>
                <div class="faixa-valor">Não alcança mais o G4 nem cai</div>
                <div class="faixa-detalhe">{{ jogo_morto.rodadas_restantes }} rodada(s) restante(s), hoje em {{ jogo_morto.posicao_atual }}º. Cenário simplificado, só informativo.</div>
            </div>
            {% endif %}
            {% if na_regua %}
            <div class="vidro aviso-ouro">
                <span class="rotulo">A 1 cartão da suspensão</span>
                <div class="faixa-valor">{{ na_regua|length }} jogador{{ 'es' if na_regua|length != 1 else '' }}</div>
                <div class="faixa-detalhe">{{ na_regua|join(', ') }}. Com muita gente na régua, o time tende a evitar cartão.</div>
            </div>
            {% endif %}
            {% if ultima_escalacao %}
            <div class="vidro" style="grid-column: 1 / -1">
                <span class="rotulo">Última escalação titular · {{ data_br(ultima_escalacao.data_jogo) }} · {{ nome_clube }} x {{ ultima_escalacao.adversario }}</span>
                <div class="escalacao">{% for nome in ultima_escalacao.titulares %}<span>{{ nome }}</span>{% endfor %}</div>
            </div>
            {% endif %}
        </div>

        {% if proximo_jogo %}
        <div class="acoes">
            <button type="button" onclick="gerarTresApostas()">Gerar 3 apostas automáticas</button>
            <button type="button" class="btn-contorno" onclick="limparSelecao()">Limpar seleção</button>
            <p>Toque nas linhas dos jogadores para montar uma aposta manual.</p>
        </div>
        {% endif %}

        <div class="grade">
            <section aria-labelledby="t-lista">
                <h2 class="secao-titulo" id="t-lista">Jogadores</h2>
                <input type="search" class="busca" id="busca" placeholder="Buscar jogador" aria-label="Buscar jogador" oninput="filtrar()">
                <div id="lista">
                {% if jogadores %}
                    {% for j in jogadores %}
                    <article class="vidro jogador jogador-card item-pagina">
                        <div class="jog-topo">
                            <div class="jog-nome">
                                {{ foto(j) }}
                                <b class="nome-jogador">{{ j.nome }}</b>
                            </div>
                            <span class="regua{{ ' alerta' if j.cartoes_suspensao == 2 else '' }}" title="Cartões rumo à suspensão (zera depois de cumprir)">
                                {% for k in range(3) %}<i class="{{ 'cheio' if k < j.cartoes_suspensao else '' }}"></i>{% endfor %}
                                {{ 'a 1 cartão da suspensão' if j.cartoes_suspensao == 2 else (j.cartoes_suspensao ~ '/3 cartões') }}
                            </span>
                        </div>
                        <div class="jog-linhas">
                            {% if j.cartao or j.impedimento %}
                            <div class="jog-linha">
                                <span>Cartão · impedimento</span>
                                <div class="celulas">
                                    {% if j.cartao %}{{ celula(j, 'cartao', '', 'sim', j.cartao.frequencia, j.nome ~ ' - Receberá cartão', 'cartão') }}{% endif %}
                                    {% if j.impedimento %}{{ celula(j, 'impedimento', '', 'sim', j.impedimento.frequencia, j.nome ~ ' - Ficará em impedimento', 'impedimento') }}{% endif %}
                                </div>
                            </div>
                            {% endif %}
                            {% for bloco in j.blocos_linha|sort(attribute='titulo') %}
                            <div class="jog-linha">
                                <span>{{ bloco.titulo }}</span>
                                <div class="celulas">
                                    {% for item in bloco.itens %}{{ celula(j, bloco.tipo, item.linha, 'mais', item.frequencia, j.nome ~ ' - ' ~ bloco.titulo ~ ' - Mais de ' ~ item.linha, '+' ~ dec(item.linha, 1), item.frequencia >= 60) }}{% endfor %}
                                </div>
                            </div>
                            {% endfor %}
                        </div>
                        {% if j.correlacao_jogador %}
                        {% set cj = j.correlacao_jogador %}
                        <div class="jog-extra">
                            Sofre em média <b>{{ dec(cj.media_pessoal, 1) }}</b> falta(s) por jogo. Quando sofre mais que isso, os defensores do adversário levam
                            <b>{{ dec(cj.valor_acima, 2) }}</b> cartão(ões) por jogo; quando sofre menos, <b>{{ dec(cj.valor_abaixo, 2) }}</b>. {{ cj.jogos_total }} jogos (a API não diz qual defensor marcou).
                        </div>
                        {% endif %}
                        {% if j.ultimos_jogos is not none %}
                        <button type="button" class="ult-botao" onclick="toggleUltimosJogosJogador({{ j.jogador_id }}); this.querySelector('.seta').style.transform = document.getElementById('ultimos-jogos-jogador-{{ j.jogador_id }}').classList.contains('aberto') ? 'translateY(0.0625rem) rotate(-135deg)' : ''">Últimos 5 jogos <span class="seta" aria-hidden="true"></span></button>
                        <div class="ultimos-jogos-jogador-lista" id="ultimos-jogos-jogador-{{ j.jogador_id }}">
                            {% if j.ultimos_jogos %}
                                {% for jg in j.ultimos_jogos %}
                                <div class="ult-jogo" id="jogo-jogador-{{ j.jogador_id }}-{{ jg.jogo_id }}" onclick="toggleJogoJogador({{ j.jogador_id }}, {{ jg.jogo_id }})">
                                    <div class="ult-linha">
                                        <span>{% if jg.mandante %}{{ jg.nosso_time_nome }} <b class="num">{{ jg.placar_nosso }} x {{ jg.placar_adversario }}</b> {{ jg.adversario }}{% else %}{{ jg.adversario }} <b class="num">{{ jg.placar_adversario }} x {{ jg.placar_nosso }}</b> {{ jg.nosso_time_nome }}{% endif %}</span>
                                        <span class="data">{{ data_br(jg.data_jogo) }}</span>
                                    </div>
                                    <div class="ult-detalhe">
                                        {% if jg.estatisticas %}
                                        {% set e = jg.estatisticas %}
                                        {% for chave, rotulo in [('minutos', 'Minutos'), ('cartoes', 'Cartões'), ('faltas_cometidas', 'Faltas cometidas'), ('faltas_sofridas', 'Faltas sofridas'), ('chutes', 'Chutes'), ('chutes_no_gol', 'Chutes no gol'), ('desarmes', 'Desarmes'), ('impedimentos', 'Impedimentos')] %}
                                        <div>{{ rotulo }}<b>{{ e[chave] if e[chave] is not none else '-' }}</b></div>
                                        {% endfor %}
                                        {% else %}
                                        <div>Sem dado detalhado desse jogo.</div>
                                        {% endif %}
                                    </div>
                                </div>
                                {% endfor %}
                            {% else %}
                                <div class="vazio" style="padding: 0.75rem">Nenhum jogo com dado ainda para esse jogador.</div>
                            {% endif %}
                        </div>
                        {% endif %}
                    </article>
                    {% endfor %}
                {% else %}
                    <div class="vazio">Ainda não há padrões calculados para nenhum jogador desse clube.</div>
                {% endif %}
                </div>
                {% if jogadores|length > 10 %}
                <div class="paginacao">
                    <button type="button" class="btn-pagina" id="anterior-lista" onclick="mudarPagina('lista', -1)">Anterior</button>
                    <span id="label-lista"></span>
                    <button type="button" class="btn-pagina" id="proximo-lista" onclick="mudarPagina('lista', 1)">Próxima</button>
                </div>
                {% endif %}
            </section>

            <aside class="lateral">
                <section aria-labelledby="t-lideres">
                    <h2 class="secao-titulo" id="t-lideres">Líderes do time</h2>
                    <div class="lideres">
                        {% for l in lideres_time %}
                        <div class="vidro lider">
                            <span class="rotulo">{{ l.titulo }}</span>
                            {% if l.jogador_nome %}
                            <div class="lider-quem"><span>{{ l.jogador_nome }}</span></div>
                            <div class="lider-valor"><b>{{ l.total }}</b><small>últimos 5: {{ dec(l.media_5 or 0, 1) }}</small></div>
                            {% else %}
                            <div class="lider-quem" style="color: var(--apagado); font-weight: 400">Ainda não há dados suficientes.</div>
                            {% endif %}
                        </div>
                        {% endfor %}
                    </div>
                </section>
                <section aria-labelledby="t-elenco">
                    <h2 class="secao-titulo" id="t-elenco">Elenco</h2>
                    {% if elenco_por_posicao %}
                    <div class="vidro elenco">
                        {% for grupo in elenco_por_posicao %}
                        <div class="elenco-grupo">
                            <span class="rotulo">{{ grupo.titulo }} · {{ grupo.jogadores|length }}</span>
                            <div class="elenco-nomes">{% for nome in grupo.jogadores %}<span class="{{ 'regua-2' if nome in na_regua else '' }}"{{ (' title="a 1 cartão da suspensão"')|safe if nome in na_regua else '' }}>{{ nome }}</span>{% endfor %}</div>
                        </div>
                        {% endfor %}
                    </div>
                    {% else %}
                    <div class="vazio">Elenco ainda não disponível.</div>
                    {% endif %}
                </section>
            </aside>
        </div>
    </main>

    {% if proximo_jogo %}
    <div class="carrinho-flutuante" id="carrinho">
        <span class="carrinho-contagem"><b id="carrinho-count">0</b> estatística(s) selecionada(s)</span>
        <button type="button" onclick="abrirModalAposta()">Criar aposta</button>
    </div>

    <div id="modal-aposta" class="modal-fundo" onclick="if(event.target===this) this.style.display='none'">
        <div class="modal-caixa" role="dialog" aria-modal="true" aria-labelledby="titulo-modal-aposta">
            <div class="modal-topo">
                <span class="modal-titulo" id="titulo-modal-aposta">Criar aposta manual</span>
                <button type="button" class="modal-fechar" onclick="document.getElementById('modal-aposta').style.display='none'">Fechar</button>
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
                    <button type="submit" class="btn-salvar">Salvar aposta</button>
                </div>
            </form>
        </div>
    </div>
    <div id="modal-tres-apostas" class="modal-fundo" onclick="if(event.target===this) this.style.display='none'">
        <div class="modal-caixa modal-caixa-grande" role="dialog" aria-modal="true" aria-labelledby="titulo-modal-tres">
            <div class="modal-topo">
                <span class="modal-titulo" id="titulo-modal-tres">3 apostas geradas automaticamente</span>
                <button type="button" class="modal-fechar" onclick="document.getElementById('modal-tres-apostas').style.display='none'">Fechar</button>
            </div>
            <p class="aviso-tres-apostas">No máximo 1 jogador pode se repetir entre as 3 apostas (sempre o de
                maior probabilidade histórica); os demais aparecem em só uma delas.</p>
            <div id="tres-apostas-container"></div>
        </div>
    </div>
    {% endif %}

    <script>
        const TAMANHO_PAGINA = 10;
        const paginaAtual = {};
        function salvarPaginaNoNavegador(listaId, pagina) {
            try { localStorage.setItem('pagina:' + window.location.pathname + ':' + listaId, String(pagina)); } catch (e) {}
        }
        function carregarPaginaDoNavegador(listaId) {
            try {
                const v = localStorage.getItem('pagina:' + window.location.pathname + ':' + listaId);
                return v !== null ? parseInt(v, 10) : 0;
            } catch (e) { return 0; }
        }
        function totalPaginas(listaId) {
            let n = 0;
            document.querySelectorAll('#' + listaId + ' .item-pagina').forEach(function(item) {
                if (item.dataset.escondidoBusca !== '1') n++;
            });
            return Math.max(1, Math.ceil(n / TAMANHO_PAGINA));
        }
        function renderizarPagina(listaId) {
            if (!(listaId in paginaAtual)) { paginaAtual[listaId] = carregarPaginaDoNavegador(listaId); }
            const total = totalPaginas(listaId);
            if (paginaAtual[listaId] > total - 1) paginaAtual[listaId] = total - 1;
            if (paginaAtual[listaId] < 0) paginaAtual[listaId] = 0;
            const pagina = paginaAtual[listaId];
            let visivelIndice = 0;
            document.querySelectorAll('#' + listaId + ' .item-pagina').forEach(function(item) {
                if (item.dataset.escondidoBusca === '1') { item.style.display = 'none'; return; }
                item.style.display = (Math.floor(visivelIndice / TAMANHO_PAGINA) === pagina) ? '' : 'none';
                visivelIndice++;
            });
            const label = document.getElementById('label-' + listaId);
            if (label) label.textContent = 'Página ' + (pagina + 1) + ' de ' + total;
            const a = document.getElementById('anterior-' + listaId), p = document.getElementById('proximo-' + listaId);
            if (a) a.disabled = (pagina === 0);
            if (p) p.disabled = (pagina >= total - 1);
        }
        function mudarPagina(listaId, direcao) {
            const total = totalPaginas(listaId);
            const pagina = Math.max(0, Math.min(total - 1, (paginaAtual[listaId] || 0) + direcao));
            paginaAtual[listaId] = pagina;
            salvarPaginaNoNavegador(listaId, pagina);
            renderizarPagina(listaId);
        }
        function filtrar() {
            const termo = document.getElementById('busca').value.toLowerCase().trim();
            document.querySelectorAll('.jogador-card').forEach(function(card) {
                card.dataset.escondidoBusca = card.querySelector('.nome-jogador').textContent.toLowerCase().includes(termo) ? '0' : '1';
            });
            paginaAtual['lista'] = 0;
            renderizarPagina('lista');
        }
        renderizarPagina('lista');

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
        function toggleUltimosJogosJogador(jogadorId) {
            document.getElementById('ultimos-jogos-jogador-' + jogadorId).classList.toggle('aberto');
        }
        function toggleJogoJogador(jogadorId, jogoId) {
            document.getElementById('jogo-jogador-' + jogadorId + '-' + jogoId).classList.toggle('aberto');
        }

        function gerarTresApostas() {
            // 3 apostas menores (3-4 pernas) em vez de uma múltipla grande; no
            // máximo 1 jogador (o de maior probabilidade) se repete entre elas
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
                        <div class="retangulo-vazio">Não há candidatos suficientes para montar essa aposta agora.</div>
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
                    <div class="retangulo-prob">Probabilidade histórica: ${probPct}%</div>
                    <form method="POST" action="/salvar-aposta">
                        <input type="hidden" name="descricao" value="${descricaoCompleta}">
                        <input type="hidden" name="casa_aposta" value="Anotado manualmente">
                        <input type="hidden" name="probabilidade_combinada" value="${probPct}">
                        <input type="hidden" name="pernas" value='${pernasJson}'>
                        <input type="hidden" name="voltar" value="/clube/{{ time_id }}">
                        <div class="modal-form-linha">
                            <input type="number" step="0.01" min="0.01" name="valor_apostado" placeholder="Valor (R$)" required>
                            <input type="number" step="0.01" min="1.01" name="odd_combinada" placeholder="Odd da casa" required>
                        </div>
                        <div class="modal-form-linha"><button type="submit" class="btn-salvar">Salvar aposta ${i + 1}</button></div>
                    </form>
                </div>`;
            }).join('');
        }

        function abrirModalAposta() {
            const itens = itensSelecionados();
            if (itens.length === 0) return;
            document.getElementById('modal-pernas-lista').innerHTML = itens.map(el => `• ${el.dataset.descricao} (${el.dataset.frequencia}%)`).join('<br>');
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
                `Probabilidade histórica combinada: ${(probCombinada * 100).toFixed(2)}%`;
            document.getElementById('campo-pernas').value = JSON.stringify(pernas);
            document.getElementById('modal-aposta').style.display = 'flex';
        }

        document.addEventListener('DOMContentLoaded', function() {
            const form = document.getElementById('form-aposta-manual');
            if (!form) return;
            form.addEventListener('submit', function() {
                document.getElementById('campo-odd').value = document.getElementById('input-odd').value;
                const valorHidden = document.createElement('input');
                valorHidden.type = 'hidden';
                valorHidden.name = 'valor_apostado';
                valorHidden.value = document.getElementById('input-valor').value;
                form.appendChild(valorHidden);
            });
        });

        (function () {
            const ativo = document.querySelector('.fileira-time.ativo');
            if (ativo) { const f = ativo.parentElement; f.scrollLeft = ativo.offsetLeft - f.clientWidth / 2 + ativo.clientWidth / 2; }
        })();
    </script>
""" + SCRIPT_FUNDO_VIVO + """</body>
</html>
"""

PAGINA_TIMES = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <link rel="icon" href="/static/marca/favicon.svg" type="image/svg+xml">
    <link rel="icon" href="/static/marca/favicon-32.png" sizes="32x32" type="image/png">
    <link rel="apple-touch-icon" href="/static/marca/apple-touch-icon.png">
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Times - Achismo Zero</title>
""" + FONTES_VISUAL_NOVO + """    <style>
        /* REDESIGN (16/10/2026): página de Times no visual da de Recomendações
           (paleta, fontes, vidro, fundo vivo, topo e ligas vêm das constantes
           compartilhadas). Os dados e o comportamento são os de antes: tabela,
           busca de clubes, líderes e correlações; o redirecionamento para o
           último time visto continua igual.
           Layout: título; tabela (esquerda) + clubes (direita); líderes em
           seis quadros; correlações no fim. Largura do conteúdo = degrau 1. */
        """ + NAV_CSS + """
""" + CSS_VISUAL_NOVO + """
        .conteudo { width: var(--degrau1); margin: 0 auto; }
        .cabeca { display: flex; align-items: flex-end; justify-content: space-between; gap: 1rem; flex-wrap: wrap; margin: 0 0 1.5rem; }
        .cabeca h1 {
            margin: 0; font-family: var(--f-corpo); font-weight: 300; font-size: clamp(1.75rem, 2.6vw, 2.5rem);
            letter-spacing: -.03em; line-height: 1;
        }
        .cabeca p { margin: 0.375rem 0 0; color: var(--apagado); font-size: 0.8125rem; max-width: 40rem; }
        .rotulo {
            font-family: var(--f-corpo); font-size: 0.625rem; letter-spacing: 0; text-transform: none;
            color: var(--apagado);
        }
        .secao-titulo {
            margin: 0 0 0.875rem; font-family: var(--f-corpo); font-weight: 300;
            font-size: clamp(1.125rem, 1.5vw, 1.4375rem); letter-spacing: -.025em; line-height: 1.1;
        }
        .vidro {
            background: var(--vidro); border: 1px solid var(--vidro-borda); box-shadow: var(--vidro-sombra);
            border-radius: var(--raio);
        }
        .vazio {
            text-align: center; color: var(--apagado); padding: 2rem 1.5rem; font-size: 0.8125rem;
            background: rgba(3, 12, 32, .4); border: 1px dashed var(--linha); border-radius: var(--raio);
        }

        /* ---------- tabela + clubes ---------- */
        .grade-topo { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr); gap: 2rem; align-items: start; margin-bottom: 3.5rem; }
        .tabela-caixa { overflow-x: auto; padding: 0.375rem 0.25rem; }
        .classificacao { width: 100%; border-collapse: collapse; font-size: 0.8125rem; white-space: nowrap; }
        .classificacao th {
            font-family: var(--f-corpo); font-weight: 500; font-size: 0.625rem; letter-spacing: 0; text-transform: none;
            color: var(--apagado); text-align: right; padding: 0.5rem 0.5rem 0.625rem; border-bottom: 1px solid var(--linha);
        }
        .classificacao td { text-align: right; padding: 0.4375rem 0.5rem; border-bottom: 1px solid var(--linha-fraca); font-family: var(--f-dado); font-variant-numeric: tabular-nums; color: var(--apagado); }
        .classificacao tr:last-child td { border-bottom: none; }
        .classificacao tbody tr { transition: background .15s; }
        .classificacao tbody tr:hover { background: rgba(255, 255, 255, .04); }
        .classificacao th.esq, .classificacao td.esq { text-align: left; }
        .classificacao .c-pos { width: 2.25rem; text-align: left; position: relative; padding-left: 0.875rem; color: var(--texto); }
        /* marca da zona: ouro nos 4 primeiros, cinza-azulado nos 4 últimos */
        .classificacao .c-pos::before {
            content: ""; position: absolute; left: 0.25rem; top: 0.5rem; bottom: 0.5rem; width: 3px; border-radius: 2px; background: transparent;
        }
        .zona-topo .c-pos::before { background: var(--ouro); }
        .zona-baixo .c-pos::before { background: #6b7488; }
        .classificacao .c-time { font-family: var(--f-corpo); font-weight: 600; color: var(--texto); }
        .classificacao .c-time a { color: inherit; text-decoration: none; display: inline-flex; align-items: center; gap: 0.5rem; }
        .classificacao .c-time a:hover { color: var(--azul-claro); }
        .classificacao .c-pts { color: var(--ouro); font-weight: 600; }
        .escudo { width: 1.25rem; height: 1.25rem; object-fit: contain; flex-shrink: 0; filter: drop-shadow(0 1px 2px rgba(0,0,0,.45)); }
        .forma { display: inline-flex; gap: 0.1875rem; }
        .forma i {
            font-style: normal; width: 1.125rem; height: 1.125rem; border-radius: 0.3125rem; display: inline-grid; place-items: center;
            font-family: var(--f-dado); font-size: 0.5625rem; font-weight: 600;
        }
        .forma .v { background: var(--ouro); color: var(--cta-texto); }
        .forma .e { background: rgba(189, 191, 199, .22); color: var(--texto); }
        .forma .d { border: 1px solid rgba(180, 200, 242, .35); color: var(--apagado); }
        .legenda { display: flex; gap: 1rem; flex-wrap: wrap; margin-top: 0.625rem; font-size: 0.6875rem; color: var(--apagado); }
        .legenda span { display: inline-flex; align-items: center; gap: 0.375rem; }
        .legenda b { width: 3px; height: 0.75rem; border-radius: 2px; display: inline-block; }

        .busca {
            width: 100%; margin: 0 0 0.875rem; padding: 0.5625rem 0.875rem;
            background: rgba(1, 5, 15, .55); border: 1px solid var(--linha); color: var(--texto);
            border-radius: 999px; font-family: var(--f-corpo); font-size: 0.8125rem;
            transition: border-color .15s, box-shadow .15s;
        }
        .busca::placeholder { color: #7d849c; }
        .busca:focus { outline: none; border-color: var(--azul-claro); box-shadow: 0 0 0 3px rgba(180, 200, 242, .18); }
        .clubes { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.5rem; }
        .clube {
            display: flex; align-items: center; gap: 0.625rem; min-width: 0;
            padding: 0.625rem 0.75rem; border-radius: 0.75rem; text-decoration: none; color: var(--texto);
            background: var(--vidro); border: 1px solid var(--vidro-borda);
            transition: border-color .15s, background .15s;
        }
        .clube:hover { border-color: var(--azul-claro); background: rgba(180, 200, 242, .08); }
        .clube .escudo { width: 1.75rem; height: 1.75rem; }
        .clube-sem-escudo { width: 1.75rem; height: 1.75rem; flex-shrink: 0; border-radius: 50%; background: rgba(255,255,255,.08); }
        .clube-nome { font-size: 0.8125rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .clube-seta { margin-left: auto; color: var(--apagado); font-size: 0.75rem; transition: color .15s, transform .15s; }
        .clube:hover .clube-seta { color: var(--azul-claro); transform: translateX(2px); }

        /* ---------- líderes ---------- */
        .lideres { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 0.75rem; margin-bottom: 3.5rem; }
        .lider { padding: 0.875rem 1rem 1rem; display: flex; flex-direction: column; gap: 0.5rem; min-width: 0; }
        .lider-valor { font-family: var(--f-dado); font-weight: 500; font-size: 1.75rem; line-height: 1; color: var(--texto); font-variant-numeric: tabular-nums; }
        .lider-valor small { font-family: var(--f-corpo); font-size: 0.6875rem; font-weight: 400; color: var(--apagado); margin-left: 0.25rem; }
        .lider-time { display: flex; align-items: center; gap: 0.5rem; font-size: 0.8125rem; font-weight: 600; min-width: 0; }
        .lider-time span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .lider-recente { font-size: 0.6875rem; color: var(--apagado); border-top: 1px solid var(--linha-fraca); padding-top: 0.5rem; margin-top: auto; }
        .lider-recente b { font-family: var(--f-dado); font-weight: 500; color: var(--ouro); }

        /* ---------- correlações ---------- */
        .correl-intro { color: var(--apagado); font-size: 0.75rem; line-height: 1.6; max-width: 62rem; margin: -0.25rem 0 1rem; }
        .correl-intro b { color: var(--texto); font-weight: 600; }
        .correl-grade { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0.75rem; }
        .correl { padding: 1rem; display: flex; flex-direction: column; gap: 0.75rem; min-width: 0; }
        .correl-titulo { font-size: 0.875rem; font-weight: 600; line-height: 1.35; }
        .correl-titulo em { font-style: normal; color: var(--apagado); font-weight: 400; margin: 0 0.25rem; }
        .correl-tipo { align-self: flex-start; }
        .barra-linha { display: grid; grid-template-columns: 4.25rem 1fr auto; align-items: center; gap: 0.5rem; font-size: 0.6875rem; color: var(--apagado); }
        .barra { height: 0.5rem; border-radius: 999px; background: rgba(255,255,255,.06); overflow: hidden; }
        .barra b { display: block; height: 100%; border-radius: 999px; }
        .barra-acima b { background: var(--ouro); }
        .barra-abaixo b { background: var(--azul-claro); }
        .barra-linha .num { font-family: var(--f-dado); color: var(--texto); font-variant-numeric: tabular-nums; }
        .correl-pe { font-size: 0.6875rem; color: var(--apagado); margin-top: auto; }
        .correl-pe b { font-family: var(--f-dado); font-weight: 500; color: var(--texto); }

        @media (max-width: 1280px) {
            .lideres { grid-template-columns: repeat(3, minmax(0, 1fr)); }
            .correl-grade { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        }
        @media (max-width: 1099px) {
            :root { --degrau1: 100%; --degrau2: 100%; }
            .grade-topo { grid-template-columns: minmax(0, 1fr); }
            .clubes { grid-template-columns: repeat(3, minmax(0, 1fr)); }
        }
        @media (max-width: 640px) {
            .lideres, .correl-grade { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .clubes { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        }
        @media (max-width: 420px) {
            .lideres, .correl-grade { grid-template-columns: 1fr; }
        }
        @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
    </style>
</head>
<body>
""" + FUNDO_VISUAL_NOVO + """    {{ nav_html|safe }}
    <script>
        // mesma lógica de /jogadores: volta para o último time visto, a não ser
        // que a página tenha sido pedida com ?geral=1
        if (!window.location.search.includes('geral=1')) {
            try {
                const timeId = localStorage.getItem('ultimo_time_id');
                if (timeId) { window.location.replace('/time/' + encodeURIComponent(timeId)); }
            } catch (e) {}
        }
    </script>

    <main class="conteudo">
        <div class="cabeca">
            <div>
                <h1>Times</h1>
                <p>Tabela do Brasileirão, estatísticas por clube e líderes entre os times rastreados. Nada aqui depende das odds da casa.</p>
            </div>
        </div>

        <div class="grade-topo">
            <section aria-labelledby="t-tabela">
                <h2 class="secao-titulo" id="t-tabela">Tabela do Brasileirão</h2>
                {% if tabela %}
                <div class="vidro tabela-caixa">
                    <table class="classificacao">
                        <thead>
                            <tr>
                                <th class="esq">#</th><th class="esq">Clube</th><th>Pts</th><th>J</th><th>V</th><th>E</th><th>D</th>
                                <th>GP</th><th>GC</th><th>SG</th><th>Últimos 5</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% set total = tabela|length %}
                            {% for l in tabela %}
                            {% set clube = (clubes|selectattr('escudo_url', 'equalto', '/escudo/' ~ l.team_id ~ '.png')|list|first) if clubes else none %}
                            <tr class="{{ 'zona-topo' if l.posicao <= 4 else ('zona-baixo' if l.posicao > total - 4 else '') }}">
                                <td class="c-pos">{{ l.posicao }}</td>
                                <td class="esq c-time">
                                    {% if clube %}<a href="/time/{{ clube.id }}">{% else %}<a>{% endif %}
                                        <img src="/escudo/{{ l.team_id }}.png" class="escudo" alt="" loading="lazy" onerror="this.style.visibility='hidden'">
                                        {{ l.nome }}
                                    </a>
                                </td>
                                <td class="c-pts">{{ l.pontos }}</td>
                                <td>{{ l.jogos }}</td>
                                <td>{{ l.vitorias }}</td>
                                <td>{{ l.empates }}</td>
                                <td>{{ l.derrotas }}</td>
                                <td>{{ l.gols_pro }}</td>
                                <td>{{ l.gols_contra }}</td>
                                <td>{{ ('+' ~ l.saldo) if l.saldo > 0 else l.saldo }}</td>
                                <td>
                                    {% if l.forma %}
                                    <span class="forma" aria-label="Últimos 5 jogos">
                                        {% for r in l.forma %}<i class="{{ 'v' if r == 'W' else ('e' if r == 'D' else 'd') }}" title="{{ 'Vitória' if r == 'W' else ('Empate' if r == 'D' else 'Derrota') }}">{{ 'V' if r == 'W' else ('E' if r == 'D' else 'D') }}</i>{% endfor %}
                                    </span>
                                    {% else %}
                                    <span title="A API-Football não mandou esse dado para esse time">—</span>
                                    {% endif %}
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                <div class="legenda">
                    <span><b style="background: var(--ouro)"></b>4 primeiros</span>
                    <span><b style="background: #6b7488"></b>4 últimos (rebaixamento)</span>
                </div>
                {% else %}
                <div class="vazio">Não foi possível carregar a tabela agora. Tente de novo em alguns minutos.</div>
                {% endif %}
            </section>

            <section aria-labelledby="t-clubes">
                <h2 class="secao-titulo" id="t-clubes">Clubes</h2>
                <input type="search" class="busca" id="busca-time" placeholder="Buscar time" aria-label="Buscar time" oninput="filtrarTimes()">
                <div class="clubes" id="lista-times">
                {% if clubes %}
                    {% for c in clubes %}
                    <a href="/time/{{ c.id }}" class="clube item-time">
                        {% if c.escudo_url %}
                        <img src="{{ c.escudo_url }}" alt="" class="escudo" loading="lazy" onerror="this.outerHTML='<span class=&quot;clube-sem-escudo&quot;></span>'">
                        {% else %}
                        <span class="clube-sem-escudo"></span>
                        {% endif %}
                        <span class="clube-nome">{{ c.nome }}</span>
                        <span class="clube-seta" aria-hidden="true">→</span>
                    </a>
                    {% endfor %}
                {% endif %}
                </div>
                {% if not clubes %}<div class="vazio">Nenhum clube rastreado ainda.</div>{% endif %}
                <div class="vazio" id="nenhum-resultado-time" hidden>Nenhum time encontrado com esse nome.</div>
            </section>
        </div>

        <section aria-labelledby="t-lideres">
            <h2 class="secao-titulo" id="t-lideres">Líderes por estatística</h2>
            <div class="lideres">
                {% for l in lideres %}
                {% set clube = (clubes|selectattr('id', 'equalto', l.time_id)|list|first) if (clubes and l.time_id) else none %}
                <div class="vidro lider">
                    <div class="rotulo">{{ l.titulo }}</div>
                    {% if l.time_nome %}
                    <div class="lider-valor">{{ ('%.2f'|format(l.media_total)).replace('.', ',') }}<small>por jogo</small></div>
                    <div class="lider-time">
                        {% if clube and clube.escudo_url %}<img src="{{ clube.escudo_url }}" class="escudo" alt="" loading="lazy">{% endif %}
                        <span>{{ l.time_nome }}</span>
                    </div>
                    <div class="lider-recente">Últimos 5: <b>{{ ('%.1f'|format(l.media_5)).replace('.', ',') if l.media_5 is not none else '—' }}</b></div>
                    {% else %}
                    <div class="lider-recente">Ainda não há dados suficientes.</div>
                    {% endif %}
                </div>
                {% endfor %}
            </div>
        </section>

        {% if correlacoes or correlacoes_categoria %}
        <section aria-labelledby="t-correl">
            <h2 class="secao-titulo" id="t-correl">Correlações</h2>
            <p class="correl-intro">
                No mesmo jogo, como uma estatística se move junto com outra. "Acima" e "abaixo" se referem à média do total do jogo
                (os dois times somados). <b>Chutes → escanteios e faltas → cartões já entram nas recomendações</b> (escanteios e
                cartões totais do jogo); desarmes → faltas e a correlação entre posições são só informativas.
            </p>
            <div class="correl-grade">
                {% for grupo, rotulo in [(correlacoes, 'Entre estatísticas'), (correlacoes_categoria, 'Entre posições')] %}
                {% for c in grupo or [] %}
                {% set maior = [c.valor_acima, c.valor_abaixo]|max %}
                <div class="vidro correl">
                    <span class="rotulo correl-tipo">{{ rotulo }}</span>
                    <div class="correl-titulo">{{ c.titulo_a }}<em>→</em>{{ c.titulo_b }}</div>
                    <div>
                        <div class="barra-linha">
                            <span>Acima</span>
                            <span class="barra barra-acima"><b style="width: {{ (100 * c.valor_acima / maior)|round(1) if maior else 0 }}%"></b></span>
                            <span class="num">{{ ('%.2f'|format(c.valor_acima)).replace('.', ',') }}</span>
                        </div>
                        <div class="barra-linha" style="margin-top: 0.375rem">
                            <span>Abaixo</span>
                            <span class="barra barra-abaixo"><b style="width: {{ (100 * c.valor_abaixo / maior)|round(1) if maior else 0 }}%"></b></span>
                            <span class="num">{{ ('%.2f'|format(c.valor_abaixo)).replace('.', ',') }}</span>
                        </div>
                    </div>
                    <div class="correl-pe">
                        {{ c.titulo_b }} por jogo quando {{ c.titulo_a|lower }} fica acima ou abaixo da média
                        (<b>{{ ('%.1f'|format(c.media_a)).replace('.', ',') }}</b>). <b>{{ '{:,}'.format(c.jogos_total).replace(',', '.') }}</b> jogos.
                    </div>
                </div>
                {% endfor %}
                {% endfor %}
            </div>
        </section>
        {% endif %}
    </main>

    <script>
        function filtrarTimes() {
            const termo = document.getElementById('busca-time').value.toLowerCase().trim();
            let visiveis = 0;
            document.querySelectorAll('.item-time').forEach(function(card) {
                const bate = card.querySelector('.clube-nome').textContent.toLowerCase().includes(termo);
                card.style.display = bate ? '' : 'none';
                if (bate) visiveis++;
            });
            document.getElementById('nenhum-resultado-time').hidden = !(termo && visiveis === 0);
        }
    </script>
""" + SCRIPT_FUNDO_VIVO + """</body>
</html>
"""

PAGINA_TIME = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <link rel="icon" href="/static/marca/favicon.svg" type="image/svg+xml">
    <link rel="icon" href="/static/marca/favicon-32.png" sizes="32x32" type="image/png">
    <link rel="apple-touch-icon" href="/static/marca/apple-touch-icon.png">
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ nome_time }} - Achismo Zero</title>
""" + FONTES_VISUAL_NOVO + """    <style>
        /* REDESIGN (16/10/2026): página de um time no visual novo. Os dados e
           o comportamento são os de antes (estatísticas por linha, estilo de
           jogo, zona da tabela, padrão por rodada, correlações, posição,
           últimos jogos e busca de confronto direto). A lista de clubes da
           esquerda virou uma fileira de escudos no topo.
           Layout: fileira de escudos; cabeçalho do time; coluna principal
           (estatísticas) + coluna lateral (tabela e jogos). */
        """ + NAV_CSS + """
""" + CSS_VISUAL_NOVO + """
        .conteudo { width: var(--degrau1); margin: 0 auto; }
        .rotulo {
            font-family: var(--f-corpo); font-size: 0.625rem; letter-spacing: 0; text-transform: none;
            color: var(--apagado);
        }
        .secao-titulo {
            margin: 0 0 0.875rem; font-family: var(--f-corpo); font-weight: 300;
            font-size: clamp(1.125rem, 1.5vw, 1.4375rem); letter-spacing: -.025em; line-height: 1.1;
        }
        .secao-titulo small { font-size: 0.75rem; color: var(--apagado); letter-spacing: 0; margin-left: 0.5rem; font-weight: 400; }
        .nota { color: var(--apagado); font-size: 0.75rem; line-height: 1.6; margin: -0.375rem 0 1rem; max-width: 52rem; }
        .nota b { color: var(--texto); font-weight: 600; }
        .vidro { background: var(--vidro); border: 1px solid var(--vidro-borda); box-shadow: var(--vidro-sombra); border-radius: var(--raio); }
        .vazio {
            text-align: center; color: var(--apagado); padding: 2rem 1.5rem; font-size: 0.8125rem;
            background: rgba(3, 12, 32, .4); border: 1px dashed var(--linha); border-radius: var(--raio);
        }
        .num { font-family: var(--f-dado); font-variant-numeric: tabular-nums; }
        .sobe { color: var(--ouro); }
        .desce { color: var(--azul-claro); }
        .escudo { object-fit: contain; flex-shrink: 0; filter: drop-shadow(0 1px 2px rgba(0,0,0,.45)); }

        /* ---------- fileira de escudos (trocar de time) ---------- */
        .fileira {
            display: flex; align-items: center; justify-content: space-between; gap: 0.25rem; padding: 0.375rem;
            overflow-x: auto; scrollbar-width: thin; margin-bottom: 2rem;
        }
        .fileira-todos {
            flex-shrink: 0; padding: 0.4375rem 0.75rem; margin-right: 0.25rem; border-radius: 999px; white-space: nowrap;
            font-size: 0.75rem; font-weight: 600; color: var(--azul-claro); text-decoration: none;
            border: 1px solid rgba(180, 200, 242, .35);
        }
        .fileira-todos:hover { background: rgba(180, 200, 242, .1); }
        .fileira-time {
            flex-shrink: 0; width: 2.375rem; height: 2.375rem; border-radius: 0.625rem; display: grid; place-items: center;
            border: 1px solid transparent; transition: background .15s, border-color .15s, transform .15s;
        }
        .fileira-time img { width: 1.5rem; height: 1.5rem; }
        .fileira-time:hover { background: rgba(255,255,255,.07); transform: translateY(-1px); }
        .fileira-time.ativo { background: rgba(63, 111, 209, .22); border-color: rgba(180, 200, 242, .6); }

        /* ---------- cabeçalho do time ---------- */
        .heroi { display: flex; align-items: center; gap: 1.5rem; flex-wrap: wrap; margin-bottom: 2.5rem; }
        .heroi .escudo-grande { width: 4.5rem; height: 4.5rem; }
        .heroi h1 { margin: 0; font-family: var(--f-corpo); font-weight: 300; font-size: clamp(2rem, 3.2vw, 3rem); letter-spacing: -.035em; line-height: 1; }
        .heroi-info { display: flex; align-items: center; gap: 0.625rem; flex-wrap: wrap; margin-top: 0.625rem; }
        .chip {
            display: inline-flex; align-items: baseline; gap: 0.375rem; padding: 0.3125rem 0.75rem; border-radius: 999px;
            font-size: 0.75rem; color: var(--apagado); background: rgba(255,255,255,.05); border: 1px solid var(--linha);
        }
        .chip b { font-family: var(--f-dado); font-weight: 600; color: var(--texto); }
        .chip.ouro b { color: var(--ouro); }
        .forma { display: inline-flex; gap: 0.1875rem; }
        .forma i {
            font-style: normal; width: 1.25rem; height: 1.25rem; border-radius: 0.3125rem; display: inline-grid; place-items: center;
            font-family: var(--f-dado); font-size: 0.625rem; font-weight: 600;
        }
        .forma .v, .res.v { background: var(--ouro); color: var(--cta-texto); }
        .forma .e, .res.e { background: rgba(189, 191, 199, .22); color: var(--texto); }
        .forma .d, .res.d { border: 1px solid rgba(180, 200, 242, .35); color: var(--apagado); }

        /* ---------- duas colunas ---------- */
        .grade { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 22rem); gap: 2.5rem; align-items: start; }
        .principal > section, .lateral > section { margin-bottom: 3rem; }

        /* frequência por linha */
        .blocos { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.75rem; }
        .bloco { padding: 1rem 1.125rem; min-width: 0; }
        .bloco-topo { display: flex; justify-content: space-between; align-items: baseline; gap: 0.75rem; margin-bottom: 0.75rem; }
        .bloco-titulo { font-size: 0.875rem; font-weight: 600; }
        .bloco-media { text-align: right; white-space: nowrap; }
        .bloco-media b { font-family: var(--f-dado); font-weight: 500; font-size: 1.25rem; color: var(--texto); }
        .bloco-media small { display: block; font-size: 0.625rem; color: var(--apagado); }
        .linha-freq { display: grid; grid-template-columns: 3rem 1fr 3rem; align-items: center; gap: 0.625rem; font-size: 0.75rem; margin-top: 0.375rem; }
        .linha-freq .lbl { font-family: var(--f-dado); color: var(--apagado); }
        .linha-freq .pct { font-family: var(--f-dado); text-align: right; color: var(--texto); }
        .barra { height: 0.4375rem; border-radius: 999px; background: rgba(255,255,255,.06); overflow: hidden; }
        .barra b { display: block; height: 100%; border-radius: 999px; background: linear-gradient(90deg, var(--azul), var(--azul-claro)); }
        .barra.forte b { background: linear-gradient(90deg, #c9a400, var(--ouro)); }
        .posse-grande { font-family: var(--f-dado); font-size: 2.25rem; font-weight: 500; line-height: 1; }
        .posse-barra { margin-top: 0.875rem; height: 0.5rem; border-radius: 999px; background: rgba(180, 200, 242, .22); overflow: hidden; }
        .posse-barra b { display: block; height: 100%; background: var(--ouro); border-radius: 999px; }

        /* estilo de jogo */
        .estilo { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.75rem; }
        .estilo-cartao { padding: 1rem 1.125rem; }
        .estilo-linhas { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: 0.75rem; }
        .estilo-valor { font-family: var(--f-dado); font-size: 1.375rem; font-weight: 500; line-height: 1.1; margin: 0.25rem 0; }
        .estilo-detalhe { font-size: 0.6875rem; color: var(--apagado); }

        /* zona da tabela */
        .zonas { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.75rem; }
        .zona { padding: 1rem 1.125rem; display: flex; flex-direction: column; gap: 0.875rem; min-width: 0; }
        .zona-titulo { font-size: 0.875rem; font-weight: 600; }
        .zona-res { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.5rem; }
        .zona-res div { min-width: 0; }
        .zona-res b { display: block; font-family: var(--f-dado); font-size: 1.125rem; font-weight: 500; }
        .zona-res span { display: block; font-size: 0.625rem; color: var(--apagado); line-height: 1.35; }
        .zona-outros { display: grid; grid-template-columns: 1fr auto; gap: 0.1875rem 0.75rem; font-size: 0.6875rem; color: var(--apagado); border-top: 1px solid var(--linha-fraca); padding-top: 0.75rem; }
        .zona-outros b { font-family: var(--f-dado); font-weight: 500; color: var(--texto); text-align: right; }

        /* padrão por rodada */
        .quebras { padding: 0.375rem 1.125rem; }
        .quebra { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 1rem; align-items: center; padding: 0.625rem 0; border-bottom: 1px solid var(--linha-fraca); font-size: 0.8125rem; }
        .quebra:last-child { border-bottom: none; }
        .quebra-rodada { font-size: 0.6875rem; color: var(--apagado); white-space: nowrap; }
        .quebra-rodada b { font-family: var(--f-dado); font-weight: 500; color: var(--texto); }
        .quebra-valores { font-family: var(--f-dado); white-space: nowrap; }
        .quebra-valores .antes { color: var(--apagado); }

        /* correlações */
        .correl-grade { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.75rem; }
        .correl { padding: 1rem; display: flex; flex-direction: column; gap: 0.75rem; min-width: 0; }
        .correl-titulo { font-size: 0.8125rem; font-weight: 600; line-height: 1.35; }
        .correl-titulo em { font-style: normal; color: var(--apagado); font-weight: 400; margin: 0 0.25rem; }
        .barra-linha { display: grid; grid-template-columns: 3.5rem 1fr auto; align-items: center; gap: 0.5rem; font-size: 0.6875rem; color: var(--apagado); }
        .barra-linha + .barra-linha { margin-top: 0.375rem; }
        .barra-acima b { background: var(--ouro); }
        .barra-abaixo b { background: var(--azul-claro); }
        .barra-linha .num { color: var(--texto); }
        .correl-pe { font-size: 0.6875rem; color: var(--apagado); }
        .correl-pe b { font-family: var(--f-dado); font-weight: 500; color: var(--texto); }

        /* lateral: posição */
        .vizinhos { width: 100%; border-collapse: collapse; font-size: 0.8125rem; }
        .vizinhos td { padding: 0.5rem 0.625rem; border-bottom: 1px solid var(--linha-fraca); }
        .vizinhos tr:last-child td { border-bottom: none; }
        .vizinhos .pos { width: 2.25rem; font-family: var(--f-dado); color: var(--apagado); }
        .vizinhos .pts { text-align: right; font-family: var(--f-dado); color: var(--apagado); }
        .vizinhos .nome a { color: var(--texto); text-decoration: none; display: inline-flex; align-items: center; gap: 0.5rem; }
        .vizinhos .nome a:hover { color: var(--azul-claro); }
        .vizinhos .escudo { width: 1.25rem; height: 1.25rem; }
        .vizinhos tr.eu td { background: rgba(255, 210, 10, .08); }
        .vizinhos tr.eu .pos, .vizinhos tr.eu .pts { color: var(--ouro); }
        .vizinhos tr.eu .nome a { font-weight: 700; }
        .tabela-caixa { padding: 0.25rem; }

        /* lateral: jogos */
        .busca-wrap { position: relative; margin-bottom: 0.75rem; }
        .busca {
            width: 100%; padding: 0.5625rem 4.5rem 0.5625rem 0.875rem;
            background: rgba(1, 5, 15, .55); border: 1px solid var(--linha); color: var(--texto);
            border-radius: 999px; font-family: var(--f-corpo); font-size: 0.8125rem;
            transition: border-color .15s, box-shadow .15s;
        }
        .busca::placeholder { color: #7d849c; }
        .busca:focus { outline: none; border-color: var(--azul-claro); box-shadow: 0 0 0 3px rgba(180, 200, 242, .18); }
        .limpar {
            position: absolute; right: 0.875rem; top: 50%; transform: translateY(-50%);
            background: none; border: 0; padding: 0; font-size: 0.6875rem; font-weight: 600; color: var(--azul-claro); cursor: pointer;
        }
        .limpar:hover { color: var(--texto); background: none; }
        .jogo { margin-bottom: 0.5rem; overflow: hidden; cursor: pointer; }
        .jogo-linha { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: center; gap: 0.5rem; padding: 0.75rem 0.875rem 0.375rem; }
        .jogo-time { display: flex; align-items: center; gap: 0.4375rem; font-size: 0.75rem; font-weight: 600; min-width: 0; }
        .jogo-time span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .jogo-time.dir { justify-content: flex-end; text-align: right; }
        .jogo-time .escudo { width: 1.375rem; height: 1.375rem; }
        .placar { font-family: var(--f-dado); font-size: 1rem; font-weight: 600; white-space: nowrap; }
        .jogo-rodape { display: flex; justify-content: space-between; align-items: center; padding: 0 0.875rem 0.625rem; font-size: 0.625rem; color: var(--apagado); }
        .res { font-family: var(--f-dado); font-size: 0.5625rem; font-weight: 600; padding: 0.0625rem 0.375rem; border-radius: 0.25rem; }
        .seta {
            width: 0.4375rem; height: 0.4375rem; border-right: 2px solid var(--apagado); border-bottom: 2px solid var(--apagado);
            transform: translateY(-0.125rem) rotate(45deg); transition: transform .2s;
        }
        .jogo.aberto .seta { transform: translateY(0.0625rem) rotate(-135deg); border-color: var(--ouro); }
        .jogo-detalhe { display: none; padding: 0.625rem 0.875rem 0.875rem; border-top: 1px solid var(--linha-fraca); }
        .jogo.aberto .jogo-detalhe { display: block; }
        .comp { display: grid; grid-template-columns: 2.25rem 1fr 2.25rem; align-items: center; gap: 0.5rem; font-size: 0.6875rem; margin-top: 0.4375rem; }
        .comp .n1 { font-family: var(--f-dado); color: var(--texto); }
        .comp .n2 { font-family: var(--f-dado); color: var(--apagado); text-align: right; }
        .comp-meio { display: flex; flex-direction: column; gap: 0.1875rem; }
        .comp-lbl { text-align: center; color: var(--apagado); font-size: 0.625rem; }
        .comp-barra { display: flex; height: 0.3125rem; border-radius: 999px; overflow: hidden; background: rgba(255,255,255,.06); }
        .comp-barra .a { background: var(--ouro); }
        .comp-barra .b { background: rgba(180, 200, 242, .45); }
        .ver-mais {
            width: 100%; background: transparent; border: 1px solid rgba(180, 200, 242, .45); color: var(--azul-claro);
        }
        .ver-mais:hover { background: rgba(180, 200, 242, .1); }

        @media (max-width: 1280px) {
            .zonas { grid-template-columns: minmax(0, 1fr); }
            .zona-outros { grid-template-columns: repeat(2, minmax(0, 1fr) auto); }
        }
        @media (max-width: 1099px) {
            :root { --degrau1: 100%; --degrau2: 100%; }
            .grade { grid-template-columns: minmax(0, 1fr); }
            .lateral { order: -1; }
        }
        @media (max-width: 640px) {
            .blocos, .estilo, .correl-grade { grid-template-columns: minmax(0, 1fr); }
            .zona-outros { grid-template-columns: 1fr auto; }
            .heroi .escudo-grande { width: 3.25rem; height: 3.25rem; }
        }
        @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
    </style>
</head>
<body>
""" + FUNDO_VISUAL_NOVO + """    {{ nav_html|safe }}
    <script>
        try { localStorage.setItem('ultimo_time_id', '{{ time_id }}'); } catch (e) {}
    </script>
    {% macro dec(v, casas=2) %}{{ (('%.' ~ casas ~ 'f')|format(v)).replace('.', ',') }}{% endmacro %}
    {% set eu = (vizinhos_tabela|selectattr('team_id', 'equalto', api_football_team_id)|list|first) if vizinhos_tabela else none %}

    <main class="conteudo">
        <nav class="vidro fileira" aria-label="Trocar de time">
            <a href="/times?geral=1" class="fileira-todos">Todos os times</a>
            {% for c in clubes %}
            <a href="/time/{{ c.id }}" class="fileira-time{{ ' ativo' if c.id == time_id else '' }}" title="{{ c.nome }}" aria-label="{{ c.nome }}"{{ ' aria-current="page"'|safe if c.id == time_id else '' }}>
                {% if c.escudo_url %}<img src="{{ c.escudo_url }}" alt="" class="escudo" loading="lazy">{% else %}<span class="rotulo">{{ c.nome[:3] }}</span>{% endif %}
            </a>
            {% endfor %}
        </nav>

        <header class="heroi">
            {% if escudo_url %}<img src="{{ escudo_url }}" alt="" class="escudo escudo-grande" onerror="this.style.display='none'">{% endif %}
            <div>
                <h1>{{ nome_time }}</h1>
                {% if eu %}
                <div class="heroi-info">
                    <span class="chip ouro"><b>{{ eu.posicao }}º</b> no Brasileirão</span>
                    <span class="chip"><b>{{ eu.pontos }}</b> pts em <b>{{ eu.jogos }}</b> jogos</span>
                    <span class="chip"><b>{{ eu.vitorias }}</b>V <b>{{ eu.empates }}</b>E <b>{{ eu.derrotas }}</b>D</span>
                    <span class="chip">gols <b>{{ eu.gols_pro }}</b>–<b>{{ eu.gols_contra }}</b></span>
                    {% if eu.forma %}
                    <span class="forma" aria-label="Últimos 5 jogos">{% for r in eu.forma %}<i class="{{ 'v' if r == 'W' else ('e' if r == 'D' else 'd') }}" title="{{ 'Vitória' if r == 'W' else ('Empate' if r == 'D' else 'Derrota') }}">{{ 'V' if r == 'W' else ('E' if r == 'D' else 'D') }}</i>{% endfor %}</span>
                    {% endif %}
                </div>
                {% endif %}
            </div>
        </header>

        <div class="grade">
            <div class="principal">
                <section aria-labelledby="t-freq">
                    <h2 class="secao-titulo" id="t-freq">Frequência por linha</h2>
                    <p class="nota">Em quantos dos últimos jogos o {{ nome_time }} passou de cada linha, contando só o lado dele. Não depende de nenhuma odd.</p>
                    {% if blocos %}
                    <div class="blocos">
                        {% for bloco in blocos %}
                        <div class="vidro bloco">
                            <div class="bloco-topo">
                                <div>
                                    <div class="bloco-titulo">{{ 'Gols do time' if bloco.titulo == 'gols' else bloco.titulo }}</div>
                                    <div class="rotulo" style="margin-top: 0.25rem">Últimos {{ bloco.jogos_analisados }} jogos</div>
                                </div>
                                {% if bloco.tipo != 'posse' %}
                                <div class="bloco-media"><b>{{ dec(bloco.media) }}</b><small>média por jogo</small></div>
                                {% endif %}
                            </div>
                            {% if bloco.tipo == 'posse' %}
                            <div class="posse-grande">{{ dec(bloco.media, 1) }}%</div>
                            <div class="posse-barra" aria-hidden="true"><b style="width: {{ bloco.media }}%"></b></div>
                            {% else %}
                            {% for item in bloco.itens %}
                            <div class="linha-freq">
                                <span class="lbl">+{{ dec(item.linha, 1) }}</span>
                                <span class="barra{{ ' forte' if item.frequencia >= 60 else '' }}"><b style="width: {{ item.frequencia }}%"></b></span>
                                <span class="pct">{{ dec(item.frequencia, 0) }}%</span>
                            </div>
                            {% endfor %}
                            {% endif %}
                        </div>
                        {% endfor %}
                    </div>
                    {% else %}
                    <div class="vazio">Ainda não há dados suficientes para as estatísticas desse time (precisa de pelo menos 5 jogos concluídos).</div>
                    {% endif %}
                </section>

                {% if estilo_time %}
                <section aria-labelledby="t-estilo">
                    <h2 class="secao-titulo" id="t-estilo">Estilo de jogo</h2>
                    <p class="nota">Ofensivo: quanto o próprio {{ nome_time }} gera o evento. Defensivo: quanto o adversário gera jogando contra ele. Comparado com os times rastreados. <b>Já entra como ajuste nas recomendações de cartão e impedimento de jogador.</b></p>
                    <div class="estilo">
                        {% for tipo, papeis in estilo_time.items() %}
                        <div class="vidro estilo-cartao">
                            <div class="bloco-titulo">{{ papeis.ofensivo.titulo if papeis.ofensivo is defined else papeis.defensivo.titulo }}</div>
                            <div class="estilo-linhas">
                                {% for chave, rotulo in [('ofensivo', 'Ofensivo'), ('defensivo', 'Defensivo')] %}
                                {% if papeis[chave] is defined %}
                                {% set d = papeis[chave] %}
                                <div>
                                    <div class="rotulo">{{ rotulo }}</div>
                                    <div class="estilo-valor {{ 'sobe' if d.desvio_pct > 0 else ('desce' if d.desvio_pct < 0 else '') }}">{{ '+' if d.desvio_pct >= 0 else '' }}{{ dec(d.desvio_pct, 1) }}%</div>
                                    <div class="estilo-detalhe"><span class="num">{{ dec(d.media_time) }}</span>/jogo · liga <span class="num">{{ dec(d.media_liga) }}</span> · {{ d.jogos_analisados }} jogos</div>
                                </div>
                                {% endif %}
                                {% endfor %}
                            </div>
                        </div>
                        {% endfor %}
                    </div>
                </section>
                {% endif %}

                {% if padroes_zona %}
                <section aria-labelledby="t-zona">
                    <h2 class="secao-titulo" id="t-zona">Por zona da tabela</h2>
                    <p class="nota">Como o {{ nome_time }} se sai conforme onde estava na tabela antes de cada jogo, e se embala ou afunda depois de vitória ou derrota. <b>Já entra como ajuste nas recomendações de resultado final e escanteio de time.</b></p>
                    <div class="zonas">
                        {% for zona_key, z in padroes_zona.items() %}
                        <div class="vidro zona">
                            <div class="zona-titulo">{{ z.titulo }}</div>
                            {% if z.resultado %}
                            <div class="zona-res">
                                {% for chave, rotulo in [('geral', 'vitórias na zona'), ('apos_vitoria', 'depois de vitória'), ('apos_derrota', 'depois de derrota')] %}
                                {% if z.resultado[chave] %}
                                <div><b>{{ dec(z.resultado[chave].valor, 1) }}%</b><span>{{ rotulo }} · {{ z.resultado[chave].jogos_amostra }} jogos</span></div>
                                {% endif %}
                                {% endfor %}
                            </div>
                            {% endif %}
                            {% if z.outros %}
                            <div class="zona-outros">
                                {% for o in z.outros %}<span>{{ o.titulo }}</span><b>{{ dec(o.valor) }}</b>{% endfor %}
                            </div>
                            {% endif %}
                        </div>
                        {% endfor %}
                    </div>
                </section>
                {% endif %}

                {% if quebras_rodada %}
                <section aria-labelledby="t-rodada">
                    <h2 class="secao-titulo" id="t-rodada">Mudança ao longo do campeonato</h2>
                    <p class="nota">Onde o comportamento do {{ nome_time }} muda de forma mais nítida, juntando as temporadas coletadas, detectado automaticamente. Só informativo.</p>
                    <div class="vidro quebras">
                        {% for q in quebras_rodada %}
                        <div class="quebra">
                            <span>{{ q.titulo }}</span>
                            <span class="quebra-rodada">a partir da rodada <b>{{ q.rodada_quebra }}</b></span>
                            <span class="quebra-valores"><span class="antes">{{ dec(q.valor_antes) }}{{ '%' if q.eh_percentual else '' }}</span> → <span class="{{ 'sobe' if q.subiu else 'desce' }}">{{ dec(q.valor_depois) }}{{ '%' if q.eh_percentual else '' }}</span></span>
                        </div>
                        {% endfor %}
                    </div>
                </section>
                {% endif %}

                {% if correlacoes_time or correlacoes_categoria_time %}
                <section aria-labelledby="t-correl">
                    <h2 class="secao-titulo" id="t-correl">Correlações do {{ nome_time }}</h2>
                    <p class="nota">As mesmas relações da página de Times, só com os jogos do {{ nome_time }}. Na de posições, só o ataque dele contra a defesa do adversário. Só informativo.</p>
                    <div class="correl-grade">
                        {% for grupo, eh_cat in [(correlacoes_time, false), (correlacoes_categoria_time, true)] %}
                        {% for c in grupo or [] %}
                        {% set maior = [c.valor_acima, c.valor_abaixo]|max %}
                        <div class="vidro correl">
                            <div class="correl-titulo">
                                {% if eh_cat %}{{ c.titulo_a }} do {{ nome_time }}<em>→</em>{{ c.titulo_b }} do adversário{% else %}{{ c.titulo_a }}<em>→</em>{{ c.titulo_b }}{% endif %}
                            </div>
                            <div>
                                <div class="barra-linha"><span>Acima</span><span class="barra barra-acima"><b style="width: {{ (100 * c.valor_acima / maior)|round(1) if maior else 0 }}%"></b></span><span class="num">{{ dec(c.valor_acima) }}</span></div>
                                <div class="barra-linha"><span>Abaixo</span><span class="barra barra-abaixo"><b style="width: {{ (100 * c.valor_abaixo / maior)|round(1) if maior else 0 }}%"></b></span><span class="num">{{ dec(c.valor_abaixo) }}</span></div>
                            </div>
                            <div class="correl-pe">{{ c.titulo_b }} por jogo com {{ c.titulo_a|lower }} acima ou abaixo da média (<b>{{ dec(c.media_a, 1) }}</b>). <b>{{ c.jogos_total }}</b> jogos.</div>
                        </div>
                        {% endfor %}
                        {% endfor %}
                    </div>
                </section>
                {% endif %}
            </div>

            <aside class="lateral">
                {% if vizinhos_tabela %}
                <section aria-labelledby="t-pos">
                    <h2 class="secao-titulo" id="t-pos">Posição no Brasileirão</h2>
                    <div class="vidro tabela-caixa">
                        <table class="vizinhos">
                            {% for l in vizinhos_tabela %}
                            {% set clube = (clubes|selectattr('escudo_url', 'equalto', '/escudo/' ~ l.team_id ~ '.png')|list|first) if clubes else none %}
                            <tr class="{{ 'eu' if l.team_id == api_football_team_id else '' }}">
                                <td class="pos">{{ l.posicao }}</td>
                                <td class="nome">{% if clube %}<a href="/time/{{ clube.id }}">{% else %}<a>{% endif %}<img src="/escudo/{{ l.team_id }}.png" class="escudo" alt="" loading="lazy" onerror="this.style.visibility='hidden'">{{ l.nome }}</a></td>
                                <td class="pts">{{ l.pontos }} pts</td>
                            </tr>
                            {% endfor %}
                        </table>
                    </div>
                </section>
                {% endif %}

                <section aria-labelledby="titulo-ultimos-jogos">
                    <h2 class="secao-titulo" id="titulo-ultimos-jogos">Últimos jogos</h2>
                    <div class="busca-wrap">
                        <input type="text" class="busca" id="busca-confronto" list="lista-adversarios-confronto"
                               placeholder="Confronto direto (ex: Palmeiras)" aria-label="Buscar confronto direto"
                               oninput="buscarConfrontoDireto()" autocomplete="off">
                        <datalist id="lista-adversarios-confronto">
                            {% for t in times_serie_a %}<option value="{{ t.nome }}">{% endfor %}
                        </datalist>
                        <button type="button" class="limpar" id="confronto-limpar" onclick="limparConfrontoDireto()" hidden>limpar</button>
                    </div>
                    <div id="lista-ultimos-jogos">
                    {% if ultimos_jogos %}
                        {% for jogo in ultimos_jogos %}
                        {% set res = 'v' if jogo.placar_nosso > jogo.placar_adversario else ('e' if jogo.placar_nosso == jogo.placar_adversario else 'd') %}
                        <div class="vidro jogo" id="card-jogo-{{ jogo.jogo_id }}" onclick="toggleJogo({{ jogo.jogo_id }})">
                            <div class="jogo-linha">
                                {% if jogo.mandante %}
                                <div class="jogo-time">{% if escudo_url %}<img src="{{ escudo_url }}" class="escudo" alt="">{% endif %}<span>{{ nome_time }}</span></div>
                                <div class="placar">{{ jogo.placar_nosso }} x {{ jogo.placar_adversario }}</div>
                                <div class="jogo-time dir"><span>{{ jogo.adversario }}</span>{% if jogo.escudo_adversario %}<img src="{{ jogo.escudo_adversario }}" class="escudo" alt="">{% endif %}</div>
                                {% else %}
                                <div class="jogo-time">{% if jogo.escudo_adversario %}<img src="{{ jogo.escudo_adversario }}" class="escudo" alt="">{% endif %}<span>{{ jogo.adversario }}</span></div>
                                <div class="placar">{{ jogo.placar_adversario }} x {{ jogo.placar_nosso }}</div>
                                <div class="jogo-time dir"><span>{{ nome_time }}</span>{% if escudo_url %}<img src="{{ escudo_url }}" class="escudo" alt="">{% endif %}</div>
                                {% endif %}
                            </div>
                            <div class="jogo-rodape">
                                <span><span class="res {{ res }}">{{ res|upper }}</span> {{ jogo.data_jogo.strftime('%d/%m/%Y') if jogo.data_jogo else '' }} · {{ 'em casa' if jogo.mandante else 'fora' }}</span>
                                <span class="seta" aria-hidden="true"></span>
                            </div>
                            <div class="jogo-detalhe">
                                {% set en = jogo.estatisticas.nosso %}{% set ea = jogo.estatisticas.adversario %}
                                {% for chave, rotulo, suf in [('posse', 'Posse de bola', '%'), ('escanteios', 'Escanteios', ''), ('chutes', 'Chutes', ''), ('chutes_no_gol', 'Chutes no gol', ''), ('faltas', 'Faltas', ''), ('desarmes', 'Desarmes', ''), ('cartoes', 'Cartões', '')] %}
                                {% set a = en[chave] %}{% set b = ea[chave] %}
                                {% set soma = ((a or 0)|float) + ((b or 0)|float) %}
                                <div class="comp">
                                    <span class="n1">{{ ((a|float)|round|int) if a is not none else '-' }}{{ suf if a is not none else '' }}</span>
                                    <span class="comp-meio">
                                        <span class="comp-lbl">{{ rotulo }}</span>
                                        <span class="comp-barra"><span class="a" style="width: {{ (100 * (a or 0)|float / soma)|round(1) if soma else 50 }}%"></span><span class="b" style="flex: 1"></span></span>
                                    </span>
                                    <span class="n2">{{ ((b|float)|round|int) if b is not none else '-' }}{{ suf if b is not none else '' }}</span>
                                </div>
                                {% endfor %}
                            </div>
                        </div>
                        {% endfor %}
                    {% else %}
                        <div class="vazio">Nenhum jogo concluído ainda para esse time.</div>
                    {% endif %}
                    </div>
                    <button type="button" class="ver-mais" id="confronto-ver-mais" onclick="mostrarTodosConfronto()" hidden>Ver todos os confrontos</button>
                </section>
            </aside>
        </div>
    </main>

    <script>
        // busca de confronto direto (mesma rota de antes: /api/confronto/<time>/<adversário>)
        const NOME_TIME_ATUAL = {{ nome_time|tojson }};
        const ESCUDO_URL_ATUAL = {{ (escudo_url or "")|tojson }};
        const TIMES_SERIE_A_CONFRONTO = {{ times_serie_a|tojson }};
        const TIME_ID_ATUAL = {{ time_id }};
        const ULTIMOS_JOGOS_HTML_ORIGINAL = document.getElementById('lista-ultimos-jogos').innerHTML;
        let confrontoJogosCompletos = [];

        function buscarConfrontoDireto() {
            const termo = document.getElementById('busca-confronto').value.trim();
            if (!termo) { limparConfrontoDireto(); return; }
            const encontrado = TIMES_SERIE_A_CONFRONTO.find(t => t.nome.toLowerCase() === termo.toLowerCase());
            if (!encontrado) return; // ainda digitando - espera bater com uma opção da lista
            document.getElementById('confronto-limpar').hidden = false;
            document.getElementById('titulo-ultimos-jogos').textContent = 'Confrontos com ' + encontrado.nome;
            document.getElementById('lista-ultimos-jogos').innerHTML = '<div class="vazio">Buscando confrontos...</div>';
            document.getElementById('confronto-ver-mais').hidden = true;
            fetch(`/api/confronto/${TIME_ID_ATUAL}/${encontrado.id}`)
                .then(r => r.json())
                .then(jogos => {
                    confrontoJogosCompletos = jogos;
                    renderizarConfrontoDireto(jogos.slice(0, 5));
                    if (jogos.length > 5) document.getElementById('confronto-ver-mais').hidden = false;
                });
        }

        function mostrarTodosConfronto() {
            renderizarConfrontoDireto(confrontoJogosCompletos);
            document.getElementById('confronto-ver-mais').hidden = true;
        }

        function limparConfrontoDireto() {
            document.getElementById('busca-confronto').value = '';
            document.getElementById('confronto-limpar').hidden = true;
            document.getElementById('confronto-ver-mais').hidden = true;
            document.getElementById('titulo-ultimos-jogos').textContent = 'Últimos jogos';
            document.getElementById('lista-ultimos-jogos').innerHTML = ULTIMOS_JOGOS_HTML_ORIGINAL;
        }

        function renderizarConfrontoDireto(jogos) {
            const cont = document.getElementById('lista-ultimos-jogos');
            if (!jogos.length) {
                cont.innerHTML = '<div class="vazio">Nenhum confronto direto registrado ainda entre esses dois times.</div>';
                return;
            }
            const escapar = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
            }[c]));
            const num = (v) => (v === null || v === undefined || v === '') ? null : Number(v);
            const data = (d) => { const p = String(d || '').slice(0, 10).split('-'); return p.length === 3 ? `${p[2]}/${p[1]}/${p[0]}` : escapar(d); };
            const LINHAS = [['posse', 'Posse de bola', '%'], ['escanteios', 'Escanteios', ''], ['chutes', 'Chutes', ''],
                ['chutes_no_gol', 'Chutes no gol', ''], ['faltas', 'Faltas', ''], ['desarmes', 'Desarmes', ''], ['cartoes', 'Cartões', '']];
            cont.innerHTML = jogos.map(jogo => {
                const escNosso = ESCUDO_URL_ATUAL ? `<img src="${ESCUDO_URL_ATUAL}" class="escudo" alt="">` : '';
                const escAdv = jogo.escudo_adversario ? `<img src="${escapar(jogo.escudo_adversario)}" class="escudo" alt="">` : '';
                const nosso = `<span>${escapar(NOME_TIME_ATUAL)}</span>`, adv = `<span>${escapar(jogo.adversario)}</span>`;
                const linha = jogo.mandante
                    ? `<div class="jogo-time">${escNosso}${nosso}</div><div class="placar">${jogo.placar_nosso} x ${jogo.placar_adversario}</div><div class="jogo-time dir">${adv}${escAdv}</div>`
                    : `<div class="jogo-time">${escAdv}${adv}</div><div class="placar">${jogo.placar_adversario} x ${jogo.placar_nosso}</div><div class="jogo-time dir">${nosso}${escNosso}</div>`;
                const r = jogo.placar_nosso > jogo.placar_adversario ? 'v' : (jogo.placar_nosso === jogo.placar_adversario ? 'e' : 'd');
                const en = (jogo.estatisticas && jogo.estatisticas.nosso) || {};
                const ea = (jogo.estatisticas && jogo.estatisticas.adversario) || {};
                const comp = LINHAS.map(([k, rot, suf]) => {
                    const a = num(en[k]), b = num(ea[k]); const soma = (a || 0) + (b || 0);
                    const pa = soma ? (100 * (a || 0) / soma).toFixed(1) : 50;
                    const fmt = (v) => v === null ? '-' : Math.round(v) + suf;
                    return `<div class="comp"><span class="n1">${fmt(a)}</span><span class="comp-meio"><span class="comp-lbl">${rot}</span><span class="comp-barra"><span class="a" style="width:${pa}%"></span><span class="b" style="flex:1"></span></span></span><span class="n2">${fmt(b)}</span></div>`;
                }).join('');
                return `<div class="vidro jogo" id="card-confronto-${jogo.jogo_id}" onclick="toggleJogoConfronto(${jogo.jogo_id})">
                    <div class="jogo-linha">${linha}</div>
                    <div class="jogo-rodape"><span><span class="res ${r}">${r.toUpperCase()}</span> ${data(jogo.data_jogo)} · ${jogo.mandante ? 'em casa' : 'fora'}</span><span class="seta" aria-hidden="true"></span></div>
                    <div class="jogo-detalhe">${comp}</div>
                </div>`;
            }).join('');
        }

        function toggleJogoConfronto(jogoId) { document.getElementById('card-confronto-' + jogoId).classList.toggle('aberto'); }
        function toggleJogo(jogoId) { document.getElementById('card-jogo-' + jogoId).classList.toggle('aberto'); }

        // a fileira de escudos começa mostrando o time aberto
        (function () {
            const ativo = document.querySelector('.fileira-time.ativo');
            if (ativo) { const f = ativo.parentElement; f.scrollLeft = ativo.offsetLeft - f.clientWidth / 2 + ativo.clientWidth / 2; }
        })();
    </script>
""" + SCRIPT_FUNDO_VIVO + """</body>
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
    "gols": "Gols do time",
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
        # CORRIGIDO: mesmo bug de contagem duplicada de motor_padroes.py
        # (JOIN direto com `cartoes` depois de `estatisticas_jogo`, que tem
        # 2 linhas por jogo, duplicava cada cartão) - agora conta numa
        # subconsulta separada.
        cur.execute(
            """
            SELECT contagem.total_cartoes
            FROM (
                SELECT j.id AS jogo_id, j.data_jogo,
                       (SELECT COUNT(*) FROM cartoes c WHERE c.jogo_id = j.id AND c.lado = 'mandante') AS total_cartoes,
                       COUNT(DISTINCT eg.lado) AS lados
                FROM jogos j
                JOIN estatisticas_jogo eg ON eg.jogo_id = j.id
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
            "titulo": titulo, "tipo": tipo, "time_nome": time_nome, "time_id": time_id,
            "media_total": float(media_total),
            "media_5": buscar_media_ultimos_5_jogos(cur, time_id, tipo),
        })
    return lideres


NOMES_ESTATISTICA_CORRELACAO = {
    "chutes": "Chutes (total)", "escanteios": "Escanteios", "faltas": "Faltas",
    "cartoes": "Cartões", "desarmes": "Desarmes",
}


def buscar_correlacoes_estatisticas(cur):
    """NOVO (Correlação entre estatísticas, só exibição): lê
    `padroes_correlacao_estatisticas` (calculada por
    motor_padroes.py/calcular_correlacoes_estatisticas) - dentro do MESMO
    jogo, como uma estatística tende a se mover junto com outra (ex: jogo
    com muito chute tende a ter mais escanteio também). NÃO afeta nenhum
    cálculo de recomendação/VE - é só informativo."""
    try:
        cur.execute(
            """SELECT estatistica_a, estatistica_b, media_a, valor_b_acima, valor_b_abaixo,
                      jogos_acima, jogos_abaixo, jogos_total
               FROM padroes_correlacao_estatisticas ORDER BY id"""
        )
        linhas = cur.fetchall()
    except Exception as e:
        if "does not exist" not in str(e).lower():
            raise
        cur.connection.rollback()
        return []

    resultado = []
    for (a, b, media_a, valor_acima, valor_abaixo, jogos_acima, jogos_abaixo, jogos_total) in linhas:
        resultado.append({
            "titulo_a": NOMES_ESTATISTICA_CORRELACAO.get(a, a),
            "titulo_b": NOMES_ESTATISTICA_CORRELACAO.get(b, b),
            "media_a": float(media_a),
            "valor_acima": float(valor_acima),
            "valor_abaixo": float(valor_abaixo),
            "sobe_junto": float(valor_acima) >= float(valor_abaixo),
            "jogos_acima": jogos_acima, "jogos_abaixo": jogos_abaixo, "jogos_total": jogos_total,
        })
    return resultado


NOMES_TITULO_PAR_CATEGORIA = {
    "falta_sofrida_atacante_cartao_defensor": ("Faltas sofridas (atacantes)", "Cartões (defensores)"),
}


def buscar_correlacoes_categoria(cur):
    """NOVO (Correlação entre CATEGORIAS de jogador, só exibição): lê
    `padroes_correlacao_categoria` - cruza a estatística de um grupo de
    jogadores (por posição) com a de outro grupo, dos dois times somados,
    no mesmo jogo (ex: atacantes que sofrem falta x cartão dos
    defensores). NÃO afeta nenhum cálculo de recomendação/VE - é só
    informativo."""
    try:
        cur.execute(
            """SELECT par, media_a, valor_b_acima, valor_b_abaixo,
                      jogos_acima, jogos_abaixo, jogos_total
               FROM padroes_correlacao_categoria ORDER BY id"""
        )
        linhas = cur.fetchall()
    except Exception as e:
        if "does not exist" not in str(e).lower():
            raise
        cur.connection.rollback()
        return []

    resultado = []
    for (par, media_a, valor_acima, valor_abaixo, jogos_acima, jogos_abaixo, jogos_total) in linhas:
        titulo_a, titulo_b = NOMES_TITULO_PAR_CATEGORIA.get(par, (par, par))
        resultado.append({
            "titulo_a": titulo_a, "titulo_b": titulo_b,
            "media_a": float(media_a),
            "valor_acima": float(valor_acima),
            "valor_abaixo": float(valor_abaixo),
            "sobe_junto": float(valor_acima) >= float(valor_abaixo),
            "jogos_acima": jogos_acima, "jogos_abaixo": jogos_abaixo, "jogos_total": jogos_total,
        })
    return resultado



# nome do rótulo -> coluna correspondente em jogador_estatisticas_jogo
# (cartão é especial: soma amarelo + vermelho, não é uma coluna única)
CATEGORIAS_LIDERANCA_JOGADOR = [
    ("chutes", "Chutes (Total)"),
    ("chutes_no_gol", "Chutes no Gol"),
    ("impedimentos", "Impedimento"),
    ("faltas_cometidas", "Faltas Cometidas"),
    ("desarmes", "Desarmes"),
    ("cartao", "Cartões"),
]


def buscar_media_ultimos_5_jogos_jogador(cur, jogador_id, coluna):
    """Média (não soma) da estatística do jogador só nos últimos 5 jogos em
    que ele entrou em campo - mesmo critério usado na liderança de time
    (buscar_media_ultimos_5_jogos), pra manter as duas tabelas de líderes
    consistentes entre si."""
    if coluna == "cartao":
        cur.execute(
            """
            SELECT jeg.cartao_amarelo + jeg.cartao_vermelho
            FROM jogador_estatisticas_jogo jeg
            JOIN jogos g ON g.id = jeg.jogo_id
            WHERE jeg.jogador_id = %s
            ORDER BY g.data_jogo DESC LIMIT 5
            """,
            (jogador_id,),
        )
    else:
        cur.execute(
            f"""
            SELECT jeg.{coluna}
            FROM jogador_estatisticas_jogo jeg
            JOIN jogos g ON g.id = jeg.jogo_id
            WHERE jeg.jogador_id = %s AND jeg.{coluna} IS NOT NULL
            ORDER BY g.data_jogo DESC LIMIT 5
            """,
            (jogador_id,),
        )
    valores = [row[0] for row in cur.fetchall() if row[0] is not None]
    if not valores:
        return 0
    return round(sum(valores) / len(valores), 2)


def buscar_lideres_estatisticas_jogadores(cur):
    """Pra cada categoria, acha qual JOGADOR (de time rastreado, ativo) tem
    o maior TOTAL somado - diferente da liderança de time (que usa média),
    aqui o mockup pede número bruto mesmo (ex: "10 nos últimos 5, 60 no
    total"). A tabela jogador_estatisticas_jogo já é podada pra manter só
    os últimos 50 jogos por jogador (ver limpar_historico.py), então somar
    tudo que está na tabela já equivale a somar sobre essa janela, sem
    precisar de LIMIT explícito aqui."""
    lideres = []
    for coluna, titulo in CATEGORIAS_LIDERANCA_JOGADOR:
        if coluna == "cartao":
            cur.execute(
                """
                SELECT j.id, j.nome, t.nome, SUM(jeg.cartao_amarelo + jeg.cartao_vermelho) AS total
                FROM jogador_estatisticas_jogo jeg
                JOIN jogadores j ON j.id = jeg.jogador_id
                JOIN times t ON t.id = j.time_atual_id
                WHERE j.ativo = TRUE AND t.rastreado = TRUE
                GROUP BY j.id, j.nome, t.nome
                ORDER BY total DESC
                LIMIT 1
                """
            )
        else:
            cur.execute(
                f"""
                SELECT j.id, j.nome, t.nome, SUM(jeg.{coluna}) AS total
                FROM jogador_estatisticas_jogo jeg
                JOIN jogadores j ON j.id = jeg.jogador_id
                JOIN times t ON t.id = j.time_atual_id
                WHERE j.ativo = TRUE AND t.rastreado = TRUE AND jeg.{coluna} IS NOT NULL
                GROUP BY j.id, j.nome, t.nome
                ORDER BY total DESC
                LIMIT 1
                """
            )
        row = cur.fetchone()
        if not row or row[3] is None:
            lideres.append({"titulo": titulo, "coluna": coluna, "jogador_nome": None})
            continue
        jogador_id, jogador_nome, time_nome, total = row
        lideres.append({
            "titulo": titulo, "coluna": coluna, "jogador_nome": jogador_nome,
            "time_nome": time_nome, "total": int(total),
            "media_5": buscar_media_ultimos_5_jogos_jogador(cur, jogador_id, coluna),
        })
    return lideres


def buscar_lideres_estatisticas_jogadores_time(cur, time_id):
    """Igual buscar_lideres_estatisticas_jogadores, mas só entre os
    jogadores DESSE clube (não o campeonato inteiro) - usado em
    /clube/<id> ("Líderes de Estatísticas do Time")."""
    lideres = []
    for coluna, titulo in CATEGORIAS_LIDERANCA_JOGADOR:
        if coluna == "cartao":
            cur.execute(
                """
                SELECT j.id, j.nome, SUM(jeg.cartao_amarelo + jeg.cartao_vermelho) AS total
                FROM jogador_estatisticas_jogo jeg
                JOIN jogadores j ON j.id = jeg.jogador_id
                WHERE j.ativo = TRUE AND j.time_atual_id = %s
                GROUP BY j.id, j.nome
                ORDER BY total DESC
                LIMIT 1
                """,
                (time_id,),
            )
        else:
            cur.execute(
                f"""
                SELECT j.id, j.nome, SUM(jeg.{coluna}) AS total
                FROM jogador_estatisticas_jogo jeg
                JOIN jogadores j ON j.id = jeg.jogador_id
                WHERE j.ativo = TRUE AND j.time_atual_id = %s AND jeg.{coluna} IS NOT NULL
                GROUP BY j.id, j.nome
                ORDER BY total DESC
                LIMIT 1
                """,
                (time_id,),
            )
        row = cur.fetchone()
        if not row or row[2] is None:
            lideres.append({"titulo": titulo, "coluna": coluna, "jogador_nome": None})
            continue
        jogador_id, jogador_nome, total = row
        lideres.append({
            "titulo": titulo, "coluna": coluna, "jogador_nome": jogador_nome,
            "total": int(total),
            "media_5": buscar_media_ultimos_5_jogos_jogador(cur, jogador_id, coluna),
        })
    return lideres


# posição predominante do jogador, a partir de jogador_estatisticas_jogo.posicao
# (vem do endpoint de estatísticas da API-Football - só distingue goleiro/
# defensor/meio-campo/atacante, não dá pra separar lateral de zagueiro com
# esse dado, então o elenco fica agrupado nessas 4 categorias)
NOMES_POSICAO = {
    "G": ("🧤", "Goleiros"),
    "D": ("🛡️", "Defensores"),
    "M": ("⚙️", "Meio-campistas"),
    "F": ("⚽", "Atacantes"),
}
ORDEM_POSICAO = ["G", "D", "M", "F"]


def buscar_elenco_por_posicao(cur, time_id):
    """Elenco completo do clube (titulares E reservas, todo mundo ativo),
    agrupado pela posição mais frequente de cada jogador nos jogos em que
    entrou em campo."""
    cur.execute(
        """
        SELECT j.id, j.nome,
               (SELECT jeg.posicao FROM jogador_estatisticas_jogo jeg
                WHERE jeg.jogador_id = j.id AND jeg.posicao IS NOT NULL
                GROUP BY jeg.posicao ORDER BY COUNT(*) DESC LIMIT 1) AS posicao
        FROM jogadores j
        WHERE j.time_atual_id = %s AND j.ativo = TRUE
        """,
        (time_id,),
    )
    grupos = {}
    for jogador_id, nome, posicao in cur.fetchall():
        chave = posicao if posicao in NOMES_POSICAO else "?"
        grupos.setdefault(chave, []).append(nome)

    resultado = []
    for chave in ORDEM_POSICAO:
        if chave in grupos:
            icone, titulo = NOMES_POSICAO[chave]
            resultado.append({"icone": icone, "titulo": titulo, "jogadores": sorted(grupos[chave])})
    if "?" in grupos:
        resultado.append({"icone": "❓", "titulo": "Posição não identificada", "jogadores": sorted(grupos["?"])})
    return resultado


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

    # NOVO (25/08/2026): `padroes_time_escanteio` passou a guardar TRÊS
    # recortes por (time, linha) - 'geral', 'mandante' e 'visitante' - pra
    # corrigir o mercado de escanteio, que ignorava mando de campo (na
    # base: mandante 5.82 x visitante 4.54 escanteios por jogo).
    #
    # Aqui a tela continua mostrando só o 'geral', que é exatamente o que
    # ela mostrava antes. Sem o filtro, o card passaria a exibir 15 linhas
    # em vez de 5. Mostrar o recorte por mando na interface é melhoria
    # separada, de propósito: não entra no mesmo deploy que mexe em
    # fórmula.
    cur.execute(
        """SELECT linha, jogos_analisados, frequencia, media
           FROM padroes_time_escanteio WHERE time_id = %s AND lado = 'geral'
           ORDER BY linha""",
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


NOMES_TIPO_ESTILO = {"impedimento": "Impedimento", "cartao": "Cartão"}


def buscar_estilo_time(cur, time_id):
    """NOVO (estilo de jogo - Fase 1, só exibição): lê `padroes_estilo_time`
    (calculada pelo motor_padroes.py/calcular_estilo_times) e monta um
    resumo pronto pra tela do time - "ofensivo" (o quanto o time gera esse
    evento) e "defensivo" (o quanto ele influencia o ADVERSÁRIO a gerar
    mais/menos esse evento), pra impedimento e cartão. Só entre times
    rastreados, já que só eles têm o dado real coletado. Devolve lista
    vazia se a tabela ainda não existe ou não tiver dado suficiente ainda
    (ex: menos de 5 times rastreados com jogos suficientes) - a tela trata
    isso mostrando um aviso, sem quebrar.
    NÃO afeta nenhum cálculo de recomendação/VE - é só informativo."""
    try:
        cur.execute(
            """SELECT tipo, papel, media_time, media_liga, jogos_analisados, fator
               FROM padroes_estilo_time WHERE time_id = %s ORDER BY tipo, papel""",
            (time_id,),
        )
        linhas = cur.fetchall()
    except Exception as e:
        # NOVO: se a tabela ainda não foi migrada nesse banco
        # (migrar_estilo_time.py não rodou ainda), trata como "sem dado"
        # em vez de derrubar a página inteira - não depende de checar o
        # tipo exato da exceção (nome do erro varia entre versões do
        # psycopg2), só olha a mensagem.
        if "does not exist" not in str(e).lower():
            raise
        cur.connection.rollback()
        return {}

    resultado = {}
    for tipo, papel, media_time, media_liga, jogos_analisados, fator in linhas:
        fator = float(fator)
        desvio_pct = round((fator - 1) * 100, 1)
        resultado.setdefault(tipo, {})[papel] = {
            "titulo": NOMES_TIPO_ESTILO.get(tipo, tipo),
            "media_time": float(media_time),
            "media_liga": float(media_liga),
            "jogos_analisados": jogos_analisados,
            "fator": fator,
            "desvio_pct": desvio_pct,
        }
    return resultado


NOMES_TIPO_QUEBRA_RODADA = {
    "resultado": "Resultado (vitória)", "cartao": "Cartão", "escanteio": "Escanteio",
    "falta": "Falta", "chute": "Chute", "chute_no_gol": "Chute no gol",
    "impedimento": "Impedimento", "desarme": "Desarme",
}


def buscar_quebras_rodada(cur, time_id):
    """NOVO (Fase B - padrão por rodada, só exibição): lê
    `padroes_quebra_rodada` (calculada por
    motor_padroes.py/calcular_padroes_rodada_time) - em qual rodada cada
    mercado desse time muda de comportamento de forma mais nítida, achado
    automaticamente (sem faixa fixa definida na mão). "resultado" é taxa
    de vitória (0 a 1); os outros mercados são média de eventos por jogo.
    NÃO afeta nenhum cálculo de recomendação/VE - é só informativo."""
    try:
        cur.execute(
            """SELECT tipo_padrao, rodada_quebra, valor_antes, valor_depois, diferenca
               FROM padroes_quebra_rodada WHERE time_id = %s ORDER BY diferenca DESC""",
            (time_id,),
        )
        linhas = cur.fetchall()
    except Exception as e:
        if "does not exist" not in str(e).lower():
            raise
        cur.connection.rollback()
        return []

    resultado = []
    for tipo, rodada_quebra, valor_antes, valor_depois, diferenca in linhas:
        eh_percentual = tipo == "resultado"
        resultado.append({
            "tipo": tipo,
            "titulo": NOMES_TIPO_QUEBRA_RODADA.get(tipo, tipo),
            "rodada_quebra": rodada_quebra,
            "eh_percentual": eh_percentual,
            "valor_antes": round(float(valor_antes) * 100, 1) if eh_percentual else round(float(valor_antes), 2),
            "valor_depois": round(float(valor_depois) * 100, 1) if eh_percentual else round(float(valor_depois), 2),
            "subiu": float(valor_depois) >= float(valor_antes),
        })
    return resultado


NOMES_ZONA = {"g4": "G4 (topo)", "meio": "Meio de tabela", "z4": "Z4 (rebaixamento)"}
NOMES_TIPO_ZONA = {
    "resultado": "Resultado (vitória)", "cartao": "Cartão", "escanteio": "Escanteio",
    "falta": "Falta", "chute": "Chute", "chute_no_gol": "Chute no gol",
    "impedimento": "Impedimento", "desarme": "Desarme",
}
ORDEM_ZONA = ["g4", "meio", "z4"]


def buscar_padroes_zona(cur, time_id):
    """NOVO (Fase C - comportamento por zona da tabela, só exibição): lê
    `padroes_zona_time` (calculada por
    motor_padroes.py/calcular_padroes_zona_time) e monta um resumo pronto
    pra tela do time, organizado por zona (G4/meio/Z4) - dentro de cada
    zona, "resultado" aparece com a comparação de momento (depois de
    vitória vs depois de derrota, a mesma zona) e os outros mercados
    aparecem só com a média geral naquela zona (evita um card gigante).
    NÃO afeta nenhum cálculo de recomendação/VE - é só informativo."""
    try:
        cur.execute(
            """SELECT tipo_padrao, zona, condicao, valor, jogos_amostra
               FROM padroes_zona_time WHERE time_id = %s""",
            (time_id,),
        )
        linhas = cur.fetchall()
    except Exception as e:
        if "does not exist" not in str(e).lower():
            raise
        cur.connection.rollback()
        return {}

    bruto = {}
    for tipo, zona, condicao, valor, jogos_amostra in linhas:
        eh_percentual = tipo == "resultado"
        bruto.setdefault(zona, {}).setdefault(tipo, {})[condicao] = {
            "valor": round(float(valor) * 100, 1) if eh_percentual else round(float(valor), 2),
            "jogos_amostra": jogos_amostra,
            "eh_percentual": eh_percentual,
        }

    resultado = {}
    for zona in ORDEM_ZONA:
        if zona not in bruto:
            continue
        mercados = bruto[zona]
        resultado[zona] = {
            "titulo": NOMES_ZONA[zona],
            "resultado": mercados.get("resultado"),
            "outros": [
                {"titulo": NOMES_TIPO_ZONA.get(tipo, tipo), **dados["geral"]}
                for tipo, dados in sorted(mercados.items())
                if tipo != "resultado" and "geral" in dados
            ],
        }
    return resultado


def buscar_correlacoes_time(cur, time_id):
    """NOVO (Correlação entre estatísticas - POR TIME, só exibição): lê
    `padroes_correlacao_time` - mesma pergunta da versão geral (que
    aparece em /times), mas só com os jogos DESSE time, pra ver se ele
    tem essa relação mais forte/fraca/invertida em relação ao padrão da
    liga. NÃO afeta nenhum cálculo de recomendação/VE - é só informativo."""
    try:
        cur.execute(
            """SELECT estatistica_a, estatistica_b, media_a, valor_b_acima, valor_b_abaixo,
                      jogos_acima, jogos_abaixo, jogos_total
               FROM padroes_correlacao_time WHERE time_id = %s ORDER BY id""",
            (time_id,),
        )
        linhas = cur.fetchall()
    except Exception as e:
        if "does not exist" not in str(e).lower():
            raise
        cur.connection.rollback()
        return []

    resultado = []
    for (a, b, media_a, valor_acima, valor_abaixo, jogos_acima, jogos_abaixo, jogos_total) in linhas:
        resultado.append({
            "titulo_a": NOMES_ESTATISTICA_CORRELACAO.get(a, a),
            "titulo_b": NOMES_ESTATISTICA_CORRELACAO.get(b, b),
            "media_a": float(media_a),
            "valor_acima": float(valor_acima),
            "valor_abaixo": float(valor_abaixo),
            "sobe_junto": float(valor_acima) >= float(valor_abaixo),
            "jogos_acima": jogos_acima, "jogos_abaixo": jogos_abaixo, "jogos_total": jogos_total,
        })
    return resultado


def buscar_correlacoes_categoria_time(cur, time_id):
    """NOVO (Correlação entre categorias - POR TIME, só exibição): lê
    `padroes_correlacao_categoria_time` - diferente da versão geral (que
    soma os dois lados juntos), essa isola a DIREÇÃO: só os jogadores da
    categoria A DO PRÓPRIO time, contra os da categoria B DO ADVERSÁRIO.
    NÃO afeta nenhum cálculo de recomendação/VE - é só informativo."""
    try:
        cur.execute(
            """SELECT par, media_a, valor_b_acima, valor_b_abaixo,
                      jogos_acima, jogos_abaixo, jogos_total
               FROM padroes_correlacao_categoria_time WHERE time_id = %s ORDER BY id""",
            (time_id,),
        )
        linhas = cur.fetchall()
    except Exception as e:
        if "does not exist" not in str(e).lower():
            raise
        cur.connection.rollback()
        return []

    resultado = []
    for (par, media_a, valor_acima, valor_abaixo, jogos_acima, jogos_abaixo, jogos_total) in linhas:
        titulo_a, titulo_b = NOMES_TITULO_PAR_CATEGORIA.get(par, (par, par))
        resultado.append({
            "titulo_a": titulo_a, "titulo_b": titulo_b,
            "media_a": float(media_a),
            "valor_acima": float(valor_acima),
            "valor_abaixo": float(valor_abaixo),
            "sobe_junto": float(valor_acima) >= float(valor_abaixo),
            "jogos_acima": jogos_acima, "jogos_abaixo": jogos_abaixo, "jogos_total": jogos_total,
        })
    return resultado


# ---------- Página de time: vizinhos na tabela + últimos jogos ----------
def buscar_vizinhos_tabela(tabela, api_football_team_id, redor=2):
    """Recorta um pedaço da tabela do Brasileirão em volta do time (até
    `redor` times acima e abaixo) - pra dar contexto de posição sem repetir
    a tabela inteira dentro da página do time."""
    if not tabela or not api_football_team_id:
        return []
    indice = next((i for i, l in enumerate(tabela) if l["team_id"] == api_football_team_id), None)
    if indice is None:
        return []
    inicio = max(0, indice - redor)
    fim = min(len(tabela), indice + redor + 1)
    faltam = (2 * redor + 1) - (fim - inicio)
    if faltam > 0:
        if inicio == 0:
            fim = min(len(tabela), fim + faltam)
        elif fim == len(tabela):
            inicio = max(0, inicio - faltam)
    return tabela[inicio:fim]


def buscar_ultimos_jogos_time(cur, time_id, limite=5):
    """Últimos jogos já concluídos desse time (com placar), mais recentes
    primeiro - alimenta a coluna "Últimos Jogos" da página do time."""
    cur.execute(
        """
        SELECT id, data_jogo, adversario, mandante, placar_corinthians, placar_adversario
        FROM jogos
        WHERE nosso_time_id = %s
          AND placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL
        ORDER BY data_jogo DESC
        LIMIT %s
        """,
        (time_id, limite),
    )
    jogos = []
    for jogo_id, data_jogo, adversario, mandante, placar_nosso, placar_adv in cur.fetchall():
        jogos.append({
            "jogo_id": jogo_id,
            "data_jogo": data_jogo,
            "adversario": adversario,
            "mandante": mandante,
            "escudo_adversario": buscar_escudo_url(cur, adversario),
            "placar_nosso": placar_nosso,
            "placar_adversario": placar_adv,
        })
    return jogos


def buscar_times_serie_a(cur, excluir_time_id=None):
    """NOVO (busca de confronto direto): times da Série A pra alimentar o
    campo de busca em /time/<id> - lista fixa por api_football_team_id (ver
    TIMES_SERIE_A_API_IDS), não pela coluna `rastreado` - o adversário
    buscado pode não estar entre os 10 rastreados hoje e ainda assim ser um
    dos 20 da Série A. `excluir_time_id` tira o próprio time da lista (não
    faz sentido buscar confronto de um time contra ele mesmo)."""
    cur.execute(
        "SELECT id, nome FROM times WHERE api_football_team_id = ANY(%s) ORDER BY nome",
        (TIMES_SERIE_A_API_IDS,),
    )
    return [
        {"id": tid, "nome": nome}
        for tid, nome in cur.fetchall()
        if tid != excluir_time_id
    ]


def buscar_confronto_direto(cur, time_id, adversario_id):
    """NOVO (busca de confronto direto): todos os jogos já concluídos entre
    `time_id` e um adversário específico (um dos 20 times da Série A),
    mais recentes primeiro - alimenta a busca dentro do card "Últimos
    Jogos" de /time/<id>. Mesmo formato de buscar_ultimos_jogos_time, mas
    filtra pelo ADVERSÁRIO via ID (mandante_id/visitante_id), não por texto
    - evita o mesmo problema de nome grafado diferente entre fontes já
    corrigido antes com a coluna `apelidos` (ver motor_recomendacoes.py).
    Sem LIMIT aqui de propósito - quem decide quantos exibir de cara e
    quando liberar o "ver mais" é o front-end, em cima da lista completa."""
    cur.execute(
        """
        SELECT id, data_jogo, adversario, mandante, placar_corinthians, placar_adversario
        FROM jogos
        WHERE nosso_time_id = %s
          AND (CASE WHEN mandante THEN visitante_id ELSE mandante_id END) = %s
          AND placar_corinthians IS NOT NULL AND placar_adversario IS NOT NULL
        ORDER BY data_jogo DESC
        """,
        (time_id, adversario_id),
    )
    jogos = []
    for jogo_id, data_jogo, adversario, mandante, placar_nosso, placar_adv in cur.fetchall():
        jogos.append({
            "jogo_id": jogo_id,
            "data_jogo": str(data_jogo),
            "adversario": adversario,
            "mandante": mandante,
            "escudo_adversario": buscar_escudo_url(cur, adversario),
            "placar_nosso": placar_nosso,
            "placar_adversario": placar_adv,
        })
    return jogos


def buscar_estatisticas_jogo_completo(cur, jogo_id, mandante_bool):
    """Estatísticas completas de UM jogo específico, já separadas em
    "nosso" e "adversário" - é o "retângulo" que abre ao clicar num jogo em
    "Últimos Jogos". Mistura 3 fontes, cada uma com semântica própria pro
    campo `lado`:
      - estatisticas_jogo: posse, escanteios, faltas, chutes totais - lado
        é baseado no mandante/visitante REAL do jogo, precisa traduzir
        pro nosso lado via `mandante_bool` (mesma lógica usada nos
        padrões de time em motor_padroes.py)
      - jogador_estatisticas_jogo, somado: chutes no gol, desarmes - mesma
        tradução de lado que estatisticas_jogo
      - cartoes: já vem gravado direto como "nosso time" (lado=mandante)
        vs "adversário" (lado=visitante) - NÃO precisa de tradução (ver
        popular_banco.py/salvar_eventos)"""
    lado_nosso = "mandante" if mandante_bool else "visitante"
    lado_adversario = "visitante" if mandante_bool else "mandante"

    cur.execute(
        "SELECT lado, posse_de_bola, escanteios, faltas, finalizacoes "
        "FROM estatisticas_jogo WHERE jogo_id = %s",
        (jogo_id,),
    )
    base = {
        lado: {"posse": posse, "escanteios": escanteios, "faltas": faltas, "chutes": chutes}
        for lado, posse, escanteios, faltas, chutes in cur.fetchall()
    }

    cur.execute(
        "SELECT lado, SUM(chutes_no_gol), SUM(desarmes) "
        "FROM jogador_estatisticas_jogo WHERE jogo_id = %s GROUP BY lado",
        (jogo_id,),
    )
    soma_jogador = {
        lado: {"chutes_no_gol": chutes_no_gol, "desarmes": desarmes}
        for lado, chutes_no_gol, desarmes in cur.fetchall()
    }

    cur.execute("SELECT lado, COUNT(*) FROM cartoes WHERE jogo_id = %s GROUP BY lado", (jogo_id,))
    cartoes_dict = dict(cur.fetchall())

    def montar(lado_traduzido, lado_cartao):
        b = base.get(lado_traduzido, {})
        s = soma_jogador.get(lado_traduzido, {})
        return {
            "posse": b.get("posse"),
            "escanteios": b.get("escanteios"),
            "faltas": b.get("faltas"),
            "chutes": b.get("chutes"),
            "chutes_no_gol": s.get("chutes_no_gol"),
            "desarmes": s.get("desarmes"),
            "cartoes": cartoes_dict.get(lado_cartao, 0),
        }

    return {
        "nosso": montar(lado_nosso, "mandante"),
        "adversario": montar(lado_adversario, "visitante"),
    }


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


_escudos_cache = {}


# 16/10/2026: ícone do site (o "0" do logo Achismo Zero, em pé). As páginas
# apontam para /static/marca/ no <head>; esta rota cobre quem pede o
# /favicon.ico direto (navegador antigo, leitor de feed, aba de JSON).
@app.route("/favicon.ico")
def favicon():
    resposta = send_from_directory(os.path.join(app.root_path, "static", "marca"), "favicon-32.png", mimetype="image/png")
    resposta.headers["Cache-Control"] = "public, max-age=604800"
    return resposta


@app.route("/escudo/<int:team_id>.png")
def escudo(team_id):
    """NOVO: busca a imagem do escudo no servidor (não no navegador do
    usuário) e repassa pro cliente - evita bloqueio de hotlink da CDN da
    API-Football, que parece exigir que o pedido não pareça vir de um site
    externo."""
    # REDESIGN (15/10/2026): a página de Recomendações mostra o escudo em
    # cada card - guarda em memória (no máximo ~500 times) e deixa o
    # navegador guardar por 7 dias, em vez de ir à CDN a cada imagem.
    conteudo = _escudos_cache.get(team_id)
    if conteudo is None:
        try:
            resp = requests.get(
                f"https://media.api-sports.io/football/teams/{team_id}.png",
                timeout=5,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 200:
                conteudo = resp.content
                if len(_escudos_cache) < 500:
                    _escudos_cache[team_id] = conteudo
        except Exception:
            pass
    if conteudo is None:
        return "", 404
    r = Response(conteudo, mimetype="image/png")
    r.headers["Cache-Control"] = "public, max-age=604800"
    return r


# 14/10/2026: logo das ligas para a página inicial, pelo mesmo caminho do
# escudo (a CDN recusa o pedido feito direto do navegador). Pública porque o
# endpoint começa com "escudo" (ver exigir_login). Guarda em memória: são
# no máximo ~50 ligas, e cada uma não muda.
_logos_liga_cache = {}


@app.route("/escudo-liga/<int:liga_id>.png")
def escudo_liga(liga_id):
    if liga_id in _logos_liga_cache:
        conteudo = _logos_liga_cache[liga_id]
    else:
        conteudo = None
        try:
            resp = requests.get(
                f"https://media.api-sports.io/football/leagues/{liga_id}.png",
                timeout=5,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 200 and resp.content:
                conteudo = resp.content
                if len(_logos_liga_cache) < 200:
                    _logos_liga_cache[liga_id] = conteudo
        except Exception:
            pass
    if conteudo is None:
        return "", 404
    resposta = Response(conteudo, mimetype="image/png")
    resposta.headers["Cache-Control"] = "public, max-age=604800"
    return resposta


# REDESIGN (16/10/2026): foto do jogador nas páginas de Jogadores, pelo mesmo
# caminho do escudo (a CDN da API-Football recusa o pedido feito direto do
# navegador). Recebe o id do NOSSO banco e procura o api_football_id. Só para
# quem está logado (não começa com "escudo"). Guarda em memória até 1.500 fotos
# e deixa o navegador guardar por 7 dias. Sem foto, a página mostra as iniciais.
_fotos_jogador_cache = {}


@app.route("/foto-jogador/<int:jogador_id>.png")
def foto_jogador(jogador_id):
    conteudo = _fotos_jogador_cache.get(jogador_id)
    if conteudo is None:
        api_id = None
        try:
            conn = psycopg2.connect(DATABASE_URL)
            try:
                cur = conn.cursor()
                cur.execute("SELECT api_football_id FROM jogadores WHERE id = %s", (jogador_id,))
                linha = cur.fetchone()
                api_id = linha[0] if linha else None
                cur.close()
            finally:
                conn.close()
        except Exception:
            api_id = None
        if api_id:
            try:
                resp = requests.get(
                    f"https://media.api-sports.io/football/players/{int(api_id)}.png",
                    timeout=5,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                if resp.status_code == 200 and resp.content:
                    conteudo = resp.content
                    if len(_fotos_jogador_cache) < 1500:
                        _fotos_jogador_cache[jogador_id] = conteudo
            except Exception:
                pass
    if conteudo is None:
        return "", 404
    resposta = Response(conteudo, mimetype="image/png")
    resposta.headers["Cache-Control"] = "public, max-age=604800"
    return resposta


# ---------- Cartões acumulados rumo à suspensão automática ----------
def buscar_cartoes_para_suspensao(cur):
    """Cartões amarelos acumulados rumo à suspensão automática (regra do
    Brasileirão: 3 cartões amarelos = 1 jogo de suspensão; depois de
    cumprida, a contagem zera e recomeça do zero). Devolve
    {jogador_id: contagem_atual} - só pra jogadores ativos.

    Como não temos como confirmar se a suspensão foi realmente CUMPRIDA
    (o jogador pode ter ficado de fora do time por lesão, decisão
    técnica etc., não necessariamente suspensão), a contagem é simulada
    de forma direta: zera automaticamente assim que bate 3 cartões,
    presumindo que a suspensão é cumprida em seguida - na prática isso
    equivale a "total de cartões amarelos da temporada, módulo 3". Só
    conta a partir do início da TEMPORADA_ATUAL - a contagem de cartões
    não carrega de um ano pro outro no Brasileirão."""
    cur.execute(
        """
        SELECT c.jogador_id, j.data_jogo
        FROM cartoes c
        JOIN jogos j ON j.id = c.jogo_id
        JOIN jogadores jog ON jog.id = c.jogador_id
        WHERE c.cor = 'amarelo'
          AND EXTRACT(YEAR FROM j.data_jogo) = %s
          AND jog.ativo = TRUE
        ORDER BY c.jogador_id, j.data_jogo ASC, j.id ASC
        """,
        (TEMPORADA_ATUAL,),
    )
    contagem_atual = {}
    for jogador_id, _data_jogo in cur.fetchall():
        atual = contagem_atual.get(jogador_id, 0) + 1
        contagem_atual[jogador_id] = 0 if atual >= 3 else atual
    return contagem_atual


def buscar_ultimos_jogos_jogador(cur, jogador_id, limite=5):
    """NOVO: últimos jogos já concluídos em que esse JOGADOR tem dado
    registrado (jogou), mais recentes primeiro - mesma ideia de
    buscar_ultimos_jogos_time, só que no nível do jogador. Alimenta o
    retângulo "🗓️ Últimos 5 jogos" de cada jogador em /clube/<id>."""
    cur.execute(
        """
        SELECT id, data_jogo, adversario, mandante,
               placar_corinthians, placar_adversario, nome
        FROM (
            -- Um jogo real tem DUAS linhas em jogos (uma por perspectiva) e o
            -- jogador aparece nas duas (1_ESSENCIAL §5.1): uma linha por
            -- fixture, preferindo a perspectiva do time do jogador
            -- (jeg.lado = mando real). Em anos antigos pode haver só a outra.
            SELECT DISTINCT ON (j.fixture_id_api)
                   j.id, j.data_jogo, j.adversario, j.mandante,
                   j.placar_corinthians, j.placar_adversario, t.nome
            FROM jogador_estatisticas_jogo jeg
            JOIN jogos j ON j.id = jeg.jogo_id
            JOIN times t ON t.id = j.nosso_time_id
            WHERE jeg.jogador_id = %s
              AND j.placar_corinthians IS NOT NULL AND j.placar_adversario IS NOT NULL
            ORDER BY j.fixture_id_api, (j.mandante = (jeg.lado = 'mandante')) DESC
        ) um_por_jogo
        ORDER BY data_jogo DESC
        LIMIT %s
        """,
        (jogador_id, limite),
    )
    jogos = []
    for jogo_id, data_jogo, adversario, mandante, placar_nosso, placar_adv, nosso_time_nome in cur.fetchall():
        jogos.append({
            "jogo_id": jogo_id,
            "data_jogo": data_jogo,
            "adversario": adversario,
            "mandante": mandante,
            "nosso_time_nome": nosso_time_nome,
            "placar_nosso": placar_nosso,
            "placar_adversario": placar_adv,
            "escudo_adversario": buscar_escudo_url(cur, adversario),
            "estatisticas": buscar_estatisticas_jogador_jogo(cur, jogador_id, jogo_id),
        })
    return jogos


def buscar_estatisticas_jogador_jogo(cur, jogador_id, jogo_id):
    """NOVO: estatísticas desse JOGADOR específico, NESSE jogo específico -
    o "retângulo" que abre ao clicar num jogo dentro de "Últimos 5 jogos"
    de um jogador (equivalente ao que já existe pra time, só que no nível
    individual)."""
    cur.execute(
        """
        SELECT minutos, faltas_cometidas, faltas_sofridas, chutes, chutes_no_gol,
               desarmes, impedimentos, posicao
        FROM jogador_estatisticas_jogo
        WHERE jogador_id = %s AND jogo_id = %s
        """,
        (jogador_id, jogo_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    (minutos, faltas_cometidas, faltas_sofridas, chutes,
     chutes_no_gol, desarmes, impedimentos, posicao) = row

    cur.execute("SELECT COUNT(*) FROM cartoes WHERE jogo_id = %s AND jogador_id = %s", (jogo_id, jogador_id))
    total_cartoes = cur.fetchone()[0]

    return {
        "minutos": minutos, "faltas_cometidas": faltas_cometidas, "faltas_sofridas": faltas_sofridas,
        "chutes": chutes, "chutes_no_gol": chutes_no_gol, "desarmes": desarmes,
        "impedimentos": impedimentos, "cartoes": total_cartoes, "posicao": posicao,
    }


def buscar_estatisticas_jogadores(cur, time_id=None, busca=None, incluir_ultimos_jogos=False):
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

    cartoes_suspensao = buscar_cartoes_para_suspensao(cur)

    # NOVO (Correlação por jogador nomeado, só exibição): só existe pra
    # atacantes com amostra própria suficiente - a maioria dos jogadores
    # não vai ter essa chave preenchida, e tudo bem (fica None).
    correlacoes_jogador = {}
    try:
        cur.execute(
            "SELECT jogador_id, media_pessoal, valor_b_acima, valor_b_abaixo, jogos_total "
            "FROM padroes_correlacao_jogador"
        )
        for jogador_id, media_pessoal, valor_acima, valor_abaixo, jogos_total in cur.fetchall():
            correlacoes_jogador[jogador_id] = {
                "media_pessoal": float(media_pessoal),
                "valor_acima": float(valor_acima),
                "valor_abaixo": float(valor_abaixo),
                "sobe_junto": float(valor_acima) >= float(valor_abaixo),
                "jogos_total": jogos_total,
            }
    except Exception as e:
        if "does not exist" not in str(e).lower():
            raise
        cur.connection.rollback()

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
            "cartoes_suspensao": cartoes_suspensao.get(jogador_id, 0),
            "correlacao_jogador": correlacoes_jogador.get(jogador_id),
            "ultimos_jogos": buscar_ultimos_jogos_jogador(cur, jogador_id) if incluir_ultimos_jogos else None,
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


def buscar_proximo_jogo(cur, time_id):
    """Busca o próximo jogo AINDA NÃO disputado do time - usado pra
    associar apostas manuais de estatística de jogador a um jogo específico
    (necessário pra conseguir avaliar acerto/erro depois que o jogo
    acontecer).
    CORRIGIDO: antes filtrava por mandante_id/visitante_id, que são os
    mesmos nas DUAS linhas que um jogo entre dois times rastreados gera
    (uma por perspectiva - ver arquitetura multi-time na documentação).
    Isso podia pegar a linha do ADVERSÁRIO por engano e mostrar "Santos x
    Santos" na página do Santos (quando o adversário também é rastreado).
    Agora filtra direto por `nosso_time_id`, que é único por perspectiva.
    NOVO (Fase D): também traz rodada_numero, temporada (derivada do ano
    de data_jogo) e o api_football_team_id do adversário - usado pra
    calcular "jogo morto" e o contexto de tabela desse confronto."""
    cur.execute(
        """
        SELECT j.id, j.data_jogo, j.adversario, j.rodada_numero,
               EXTRACT(YEAR FROM j.data_jogo)::int AS temporada,
               CASE WHEN j.mandante THEN j.visitante_id ELSE j.mandante_id END AS adversario_time_id
        FROM jogos j
        WHERE j.nosso_time_id = %s
          AND ((j.datahora_jogo IS NOT NULL AND j.datahora_jogo >= NOW())
            OR (j.datahora_jogo IS NULL AND j.data_jogo >= CURRENT_DATE))
        ORDER BY COALESCE(j.datahora_jogo, j.data_jogo::timestamp) ASC
        LIMIT 1
        """,
        (time_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    jogo_id, data_jogo, adversario, rodada_numero, temporada, adversario_time_id = row

    adversario_api_id = None
    if adversario_time_id:
        cur.execute("SELECT api_football_team_id FROM times WHERE id = %s", (adversario_time_id,))
        r = cur.fetchone()
        adversario_api_id = r[0] if r else None

    return {
        "jogo_id": jogo_id, "data_jogo": data_jogo, "adversario": adversario,
        "rodada_numero": rodada_numero, "temporada": temporada, "adversario_api_id": adversario_api_id,
    }


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
        lideres = buscar_lideres_estatisticas_jogadores(cur)
        banca_atual = buscar_banca(cur, session["usuario_id"])
        cur.close()
    finally:
        conn.close()

    return render_template_string(
        PAGINA_JOGADORES, clubes=clubes, jogadores=jogadores_lista, lideres=lideres,
        nav_html=barra_navegacao_nova("jogadores", round(banca_atual, 2)),
    )


@app.route("/clube/<int:time_id>")
def clube(time_id):
    """NOVO (multi-time): página específica do clube - antes só existia
    /clube/corinthians fixo; agora funciona pra qualquer time rastreado,
    identificado pelo id. Mostra todos os jogadores ativos DESSE clube +
    a última escalação titular confirmada + o próximo jogo (usado pra
    montar apostas manuais de estatística), além do elenco completo
    agrupado por posição e os líderes de estatística do time."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute("SELECT nome, api_football_team_id FROM times WHERE id = %s AND rastreado = TRUE", (time_id,))
        row = cur.fetchone()
        if not row:
            cur.close()
            return "Clube não encontrado.", 404
        nome_clube, api_football_team_id = row

        lista = buscar_estatisticas_jogadores(cur, time_id, incluir_ultimos_jogos=True)
        ultima_escalacao = buscar_ultima_escalacao_titular(cur, time_id)
        proximo_jogo = buscar_proximo_jogo(cur, time_id)
        escudo_url = buscar_escudo_url(cur, nome_clube)
        elenco_por_posicao = buscar_elenco_por_posicao(cur, time_id)
        lideres_time = buscar_lideres_estatisticas_jogadores_time(cur, time_id)
        banca_atual = buscar_banca(cur, session["usuario_id"])
        # REDESIGN (16/10/2026): a fileira de escudos do topo troca de clube
        cur.execute("SELECT id, nome FROM times WHERE rastreado = TRUE ORDER BY nome")
        clubes = [
            {"id": c_id, "nome": c_nome, "escudo_url": buscar_escudo_url(cur, c_nome)}
            for c_id, c_nome in cur.fetchall()
        ]

        # NOVO (Fase D, só exibição - não afeta recomendação/VE): "jogo
        # morto" (o time já não tem mais nada em jogo matematicamente) e o
        # contexto de tabela desse confronto específico (posição, distância
        # até o objetivo, força do adversário) - só calcula se o próximo
        # jogo e o api_football_team_id do time estiverem disponíveis.
        jogo_morto = None
        contexto_jogo = None
        if proximo_jogo and api_football_team_id and proximo_jogo["rodada_numero"]:
            jogo_morto = calcular_jogo_morto(
                cur, api_football_team_id, proximo_jogo["temporada"], proximo_jogo["rodada_numero"]
            )
            if proximo_jogo["adversario_api_id"]:
                contexto_jogo = calcular_contexto_jogo(
                    cur, api_football_team_id, proximo_jogo["adversario_api_id"],
                    proximo_jogo["temporada"], proximo_jogo["rodada_numero"],
                )

        cur.close()
    finally:
        conn.close()

    # NOVO: quantos jogadores do elenco estão a 1 cartão amarelo da
    # suspensão automática - jogo com muita gente nessa situação tende a
    # ter menos cartão de verdade (ninguém quer arriscar ficar de fora),
    # então vale destacar isso na tela.
    jogadores_na_regua = sorted(
        [j["nome"] for j in lista if j["cartoes_suspensao"] == 2]
    )

    return render_template_string(
        PAGINA_CLUBE, jogadores=lista, ultima_escalacao=ultima_escalacao,
        proximo_jogo=proximo_jogo, nome_clube=nome_clube, escudo_url=escudo_url,
        time_id=time_id, elenco_por_posicao=elenco_por_posicao, lideres_time=lideres_time,
        jogadores_na_regua=jogadores_na_regua, jogo_morto=jogo_morto, contexto_jogo=contexto_jogo,
        clubes=clubes,
        nav_html=barra_navegacao_nova("jogadores", round(banca_atual, 2)),
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
        correlacoes = buscar_correlacoes_estatisticas(cur)
        correlacoes_categoria = buscar_correlacoes_categoria(cur)
        banca_atual = buscar_banca(cur, session["usuario_id"])
        cur.close()
    finally:
        conn.close()

    tabela = buscar_tabela_brasileirao()

    return render_template_string(
        PAGINA_TIMES, clubes=clubes, tabela=tabela, lideres=lideres, correlacoes=correlacoes,
        correlacoes_categoria=correlacoes_categoria,
        nav_html=barra_navegacao_nova("times", round(banca_atual, 2)),
    )


@app.route("/time/<int:time_id>")
def time_detalhe(time_id):
    """NOVO (estatísticas de time): página com a frequência histórica de
    escanteios, faltas, chutes e cartões de UM time rastreado, agora com
    contexto extra: posição na tabela do Brasileirão (com vizinhos),
    últimos jogos (com estatística completa ao clicar), e a mesma lista de
    times com busca que a página inicial de Estatísticas de Times tem."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT nome, api_football_team_id FROM times WHERE id = %s AND rastreado = TRUE",
            (time_id,),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            return "Time não encontrado.", 404
        nome_time, api_football_team_id = row

        blocos = buscar_estatisticas_time(cur, time_id)
        escudo_url = buscar_escudo_url(cur, nome_time)
        estilo_time = buscar_estilo_time(cur, time_id)
        quebras_rodada = buscar_quebras_rodada(cur, time_id)
        padroes_zona = buscar_padroes_zona(cur, time_id)
        correlacoes_time = buscar_correlacoes_time(cur, time_id)
        correlacoes_categoria_time = buscar_correlacoes_categoria_time(cur, time_id)

        cur.execute("SELECT id, nome FROM times WHERE rastreado = TRUE ORDER BY nome")
        times_rastreados = cur.fetchall()
        clubes = [
            {"id": tid, "nome": nome, "escudo_url": buscar_escudo_url(cur, nome)}
            for tid, nome in times_rastreados
        ]

        ultimos_jogos = buscar_ultimos_jogos_time(cur, time_id)
        for jogo in ultimos_jogos:
            jogo["estatisticas"] = buscar_estatisticas_jogo_completo(cur, jogo["jogo_id"], jogo["mandante"])

        # NOVO (busca de confronto direto): times da Série A pra alimentar
        # o campo de busca do card "Últimos Jogos" (ver seção 6-C/documentação).
        times_serie_a = buscar_times_serie_a(cur, excluir_time_id=time_id)

        banca_atual = buscar_banca(cur, session["usuario_id"])
        cur.close()
    finally:
        conn.close()

    tabela = buscar_tabela_brasileirao()
    vizinhos_tabela = buscar_vizinhos_tabela(tabela, api_football_team_id)

    return render_template_string(
        PAGINA_TIME, blocos=blocos, nome_time=nome_time, escudo_url=escudo_url,
        clubes=clubes, time_id=time_id, vizinhos_tabela=vizinhos_tabela,
        api_football_team_id=api_football_team_id, ultimos_jogos=ultimos_jogos,
        estilo_time=estilo_time, quebras_rodada=quebras_rodada, padroes_zona=padroes_zona,
        correlacoes_time=correlacoes_time, correlacoes_categoria_time=correlacoes_categoria_time,
        times_serie_a=times_serie_a,
        nav_html=barra_navegacao_nova("times", round(banca_atual, 2)),
    )


@app.route("/")
def index():
    odd_min = request.args.get("odd_min", "1.5")
    odd_max = request.args.get("odd_max", "5.0")
    # NOVO (30/08/2026): faixa de PROBABILIDADE HISTÓRICA, em % (ex: 70 a
    # 83). Diferente da faixa de odd, o padrão é VAZIO = sem filtro, pra
    # não mudar o comportamento de quem não usa o campo. Filtra pelo valor
    # COMBINADO da aposta (ver montar_combinacoes), então uma múltipla é
    # julgada pela probabilidade dela inteira, não perna a perna.
    prob_min = request.args.get("prob_min", "")
    prob_max = request.args.get("prob_max", "")
    buscou = "odd_min" in request.args
    forcar_atualizacao = request.args.get("forcar") == "1"
    # NOVO (troca de abordagem, ver comentário perto de
    # gerar_e_guardar_token_busca): antes disso, a proteção contra
    # disparo indevido era só o marcador "auto=1" do redirecionamento
    # automático via JS - mas isso deixava passar um F5 direto numa URL
    # com odd_min/odd_max (clique real anterior) ou no link "Atualizar
    # recomendações", já que a URL não muda com F5 e o servidor não tinha
    # como saber que não foi um clique novo. Agora quem decide é um token
    # de uso único: só é um "clique real" se veio com o token certo, e
    # esse token nunca sobrevive a um F5 (é consumido no primeiro uso).
    # NOVO (29/08/2026): botão "🔍 Só mostrar (sem gerar)". Durante o
    # desenvolvimento é comum passar mais de uma hora investigando um bug
    # ou uma probabilidade estranha e, no meio disso, querer olhar OUTRA
    # faixa de odd. Com os dois botões antigos, isso disparava uma
    # atualização de odds nova (gasta cota da OddsPapi - 41 requisições
    # por rodada completa) e ainda REGERAVA as recomendações, mudando o
    # dado que estava sendo investigado no meio da investigação.
    #
    # Este caminho não dispara nada: só relê o que já está gravado em
    # `recomendacoes` e refiltra pela faixa pedida. Tecnicamente ele
    # reaproveita o caminho passivo que já existia (o do F5, que chega sem
    # token válido) - a diferença é que agora dá pra pedir isso DE
    # PROPÓSITO, com uma faixa de odd diferente, em vez de depender de
    # recarregar a página.
    #
    # Como `eh_clique_real` fica False, três coisas deixam de acontecer, e
    # é exatamente esse o objetivo: (1) `processar_atualizacao_odds` não
    # roda, (2) as recomendações não são regeradas, (3)
    # `capturar_candidatas_multiplas` não grava nada - então ficar testando
    # faixas aqui não polui o ranking de Múltiplas em Destaque do
    # /historico com combinações que só foram olhadas de passagem.
    #
    # ORDEM IMPORTA: a validação do token CONSOME o token (é de uso
    # único). Por isso ela só é chamada quando NÃO é "só mostrar" - assim
    # o token continua válido pro próximo clique de verdade, em vez de ser
    # gasto por uma consulta passiva.
    apenas_mostrar = request.args.get("apenas_mostrar") == "1"
    eh_clique_real = (
        False if apenas_mostrar
        else validar_e_consumir_token_busca(request.args.get("token_busca"))
    )

    combinacoes = []
    motivo = ""
    ultima_atualizacao_odds = None
    disparou_agora = False
    # NOVO (acompanhamento em tempo real): estado da barra de progresso.
    atualizacao_rodando = False
    progresso_decorridos = 0
    progresso_estimativa = ESTIMATIVA_PADRAO_SEGUNDOS
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        banca_atual = buscar_banca(cur, session["usuario_id"])
        # REDESIGN (15/10/2026): nome do time -> id na API-Football, para o
        # escudo ao lado do nome. Os nomes das recomendações vêm de
        # times.nome (nosso_time) e de jogos.adversario, que em 2026 batem
        # 100% com times.nome. Time sem id fica só com o nome.
        cur.execute("SELECT nome, api_football_team_id FROM times WHERE api_football_team_id IS NOT NULL")
        escudos = {nome: api_id for nome, api_id in cur.fetchall()}

        # NOVO: só mexe na atualização de odds quando a pessoa realmente
        # pediu recomendações de propósito, comprovado pelo token de
        # clique real (cobre tanto o botão "Gerar recomendações da
        # rodada" quanto o link "Atualizar recomendações", os dois
        # embutem o mesmo token) - um F5 na mesma URL chega sem um token
        # válido e cai no "else" (só mostra o que já existe), mesmo que
        # buscou/forcar continuem True na URL.
        if buscou and eh_clique_real:
            ultima, disparou_agora = processar_atualizacao_odds(cur, session["usuario_id"], forcar_atualizacao)
            conn.commit()
            if ultima:
                ultima_atualizacao_odds = ultima.astimezone(FUSO_BRASIL).strftime("%H:%M")
        else:
            ultima = buscar_ultima_atualizacao_odds(cur)
            if ultima:
                ultima_atualizacao_odds = ultima.astimezone(FUSO_BRASIL).strftime("%H:%M")

        # NOVO (acompanhamento em tempo real): lê do BANCO se existe
        # atualização rodando. Fica DEPOIS do bloco de disparo de
        # propósito, pra já enxergar a execução que acabou de ser criada
        # neste mesmo clique.
        #
        # Vale pros dois caminhos: o clique que disparou agora, e um F5 no
        # meio de uma execução iniciada antes (aí o painel reaparece
        # sozinho, sem depender de quem clicou).
        execucao_atual = buscar_execucao_em_andamento(cur)
        progresso_estimativa = estimar_duracao_atualizacao(cur)
        conn.commit()  # o varredor de abandonadas pode ter escrito
        if execucao_atual:
            atualizacao_rodando = True
            progresso_decorridos = max(
                0, int((datetime.now(timezone.utc) - execucao_atual[1]).total_seconds())
            )

        if buscou:
            recomendacoes = buscar_recomendacoes(cur)
            # NOVO (30/08/2026): campo em branco vira None = sem limite
            # daquele lado. `_float_ou_none` também tolera lixo digitado
            # (texto, vírgula sozinha) devolvendo None, em vez de quebrar
            # a página inteira com ValueError.
            combinacoes = montar_combinacoes(
                recomendacoes, float(odd_min), float(odd_max),
                prob_min=_float_ou_none(prob_min), prob_max=_float_ou_none(prob_max),
            )

            # NOVO: captura cada múltipla gerada num clique de verdade em
            # `multiplas_candidatas` - abastece o ranking de Múltiplas em
            # Destaque do /historico, junto com o mesmo trabalho que o
            # motor_combinacoes.py já faz sozinho no cron. Só em clique
            # real (não em F5/navegação passiva) - mesmo critério que já
            # decide se dispara atualização de odds.
            if eh_clique_real:
                capturar_candidatas_multiplas(cur, combinacoes)
                conn.commit()

            aplicar_totais_apostados(combinacoes, buscar_totais_apostados(cur, session["usuario_id"]))
            if not combinacoes:
                # NOVO: se a gente acabou de disparar o pedido de atualização
                # NESSA MESMA requisição, o motivo real de não ter
                # recomendação ainda não é "faltam odds coletadas" (mensagem
                # antiga, confusa aqui) - é que o disparo em si é rápido, mas
                # o processo real (baixar odds + recalcular recomendações)
                # ainda está rodando em segundo plano e pode levar 1-2
                # minutos pra terminar. Sem isso, a pessoa clica, não vê
                # nada, e acha que o botão não fez nada - quando na
                # verdade só ainda não deu tempo.
                # NOVO: quando a atualização está rodando, quem explica a
                # situação é o painel de progresso (que substitui as
                # listas), não uma mensagem de "clica de novo" - o clique
                # manual deixou de ser necessário. As mensagens antigas
                # ficam só como reserva, pro caso raro de o painel não
                # aparecer (ex: execução que fechou entre o disparo e essa
                # consulta).
                if apenas_mostrar:
                    # NOVO (29/08/2026): no modo "só mostrar" as mensagens
                    # abaixo não servem - elas explicam uma atualização que
                    # este caminho deliberadamente NÃO dispara. Dizer "está
                    # rodando em segundo plano" aqui seria mentira, e
                    # "faltam odds coletadas" mandaria investigar coleta
                    # quando o problema pode ser só a faixa de odd pedida.
                    motivo = ("Não tem nenhuma odd gerada nessa faixa. Este botão só mostra o que "
                              "já foi gerado antes - ele não gera nada novo. Use \"Gerar recomendações "
                              "da rodada\" pra buscar odds e calcular as recomendações.")
                elif disparou_agora:
                    motivo = ("A atualização de odds foi disparada agora mesmo e está rodando em segundo "
                               "plano. A página se atualiza sozinha quando terminar.")
                elif atualizacao_rodando:
                    motivo = ("Já tem uma atualização rodando agora - só uma roda por vez, pra não "
                               "duplicar chamada na OddsPapi. A página se atualiza sozinha quando terminar.")
                else:
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

    # NOVO: agrupa as odds individuais POR JOGO, pra seção "Jogos
    # disponíveis" no fim da página (um card por confronto, que expande
    # mostrando as odds daquele jogo).
    #
    # CUIDADO ESTRUTURAL: quando os DOIS times de um confronto são
    # rastreados, o mesmo jogo real existe 2x em `jogos` (uma linha por
    # perspectiva) - então as odds chegam aqui rotuladas ora como
    # "Cruzeiro x Flamengo", ora como "Flamengo x Cruzeiro". Agrupar pelo
    # texto criaria DOIS cards pro mesmo confronto. Por isso a chave usa o
    # par de times ORDENADO + a data: as duas perspectivas caem no mesmo
    # balde, e o rótulo exibido é sempre o mesmo (ordem alfabética, já que
    # a lista de recomendação não carrega quem é mandante).
    # CORRIGIDO (04/09/2026): a chave era `(data_jogo, dupla)`, e isso
    # quebrava quando as DUAS perspectivas do mesmo jogo real tinham
    # `datahora_jogo` diferente - o card mostrava o mesmo confronto duas
    # vezes, e a contagem do cabeçalho ficava maior que a rodada real
    # (11 jogos numa rodada de 10, em 04/09/2026).
    #
    # POR QUE as datas divergem: `popular_banco.py` grava
    # `fixture["fixture"]["date"]` da API-Football, que traz data
    # PROVISÓRIA enquanto a CBF não confirma os horários da rodada
    # (medido: os 10 jogos da rodada 26 vieram todos como "06/09 20:00").
    # Já `atualizar_odds.py` cria o jogo com o horário real da OddsPapi.
    # Quando as duas datas caem em dias diferentes, a chave antiga
    # separava as perspectivas.
    #
    # A chave certa é `fixture_id_api`, a identidade canônica do jogo real
    # em todo o projeto - a mesma razão pela qual `chave_mercado_da_perna`
    # deixou de usar `jogo_id` em 20/08. `identidade_jogo` já faz isso,
    # com fallback pro trio (nosso_time, adversario, data_jogo) quando o
    # fixture não está preenchido (jogos antigos), então é reusada aqui em
    # vez de repetir a regra.
    #
    # ⚠️ NÃO resolve a causa raiz: `datahora_jogo` está gravado em UTC e
    # exibido como se fosse BRT (confirmado - converter UTC->America/
    # Sao_Paulo bate com o horário real em 7 de 8 jogos verificáveis da
    # rodada 26). Isso é a mesma causa da pendência 57
    # (`recomendacoes.gerado_em` ~3h à frente) e fica para depois da
    # medição de 05-06/09, porque mexer em data agora afetaria também o
    # arquivamento no meio da coleta que importa.
    # ⚠️ Usa `.get()` em vez de `identidade_jogo(j)` de propósito: essa
    # função assume uma PERNA (que sempre tem `fixture_id_api`), e o dict
    # de `c["jogos"]` é outro objeto, montado em `montar_combinacoes`. Ele
    # passou a carregar o campo em 04/09/2026, mas usar `.get()` aqui
    # garante que uma combinação vinda de um caminho que não o preencha
    # caia no fallback em vez de derrubar a página inteira com KeyError -
    # foi exatamente o que aconteceu na primeira versão desta correção.
    LIMITE_ODDS_POR_JOGO = 100
    jogos_agrupados = {}
    for c in individuais:
        if not c.get("jogos"):
            continue
        j = c["jogos"][0]
        dupla = tuple(sorted([j["nosso_time"], j["adversario"]]))
        chave = j.get("fixture_id_api") or (j["nosso_time"], j["adversario"], j["data_jogo"])
        if chave not in jogos_agrupados:
            jogos_agrupados[chave] = {
                "data_jogo": j["data_jogo"],
                "datahora_jogo": j.get("datahora_jogo"),
                "rotulo": f"{dupla[0]} x {dupla[1]}",
                "time_a": dupla[0], "time_b": dupla[1],
                "odds": [],
            }
        else:
            # As duas perspectivas caíram no mesmo balde. Quando as datas
            # divergem, a MENOR é a que veio da OddsPapi com horário real
            # (a da API-Football é o placeholder da rodada, sempre no
            # domingo). Escolher explicitamente evita que a ordenação
            # final da lista dependa de qual perspectiva chegou primeiro.
            atual = jogos_agrupados[chave]
            nova_dh = j.get("datahora_jogo")
            if nova_dh and (not atual["datahora_jogo"] or nova_dh < atual["datahora_jogo"]):
                atual["datahora_jogo"] = nova_dh
                atual["data_jogo"] = j["data_jogo"]
        jogos_agrupados[chave]["odds"].append(c)

    jogos_disponiveis = []
    for dados in jogos_agrupados.values():
        # mesma ordenação da aba principal de odds/múltiplas: maior
        # probabilidade histórica primeiro (ver ordenação final em
        # combinacoes.montar_combinacoes).
        dados["odds"].sort(key=lambda x: x["probabilidade_combinada"], reverse=True)
        dados["total_odds"] = len(dados["odds"])
        # teto de segurança: mostra as melhores e AVISA quantas ficaram de
        # fora, em vez de esconder silenciosamente.
        dados["ocultas"] = max(0, dados["total_odds"] - LIMITE_ODDS_POR_JOGO)
        dados["odds"] = dados["odds"][:LIMITE_ODDS_POR_JOGO]
        jogos_disponiveis.append(dados)
    jogos_disponiveis.sort(key=lambda d: (d["datahora_jogo"] or d["data_jogo"], d["rotulo"]))

    # NOVO: gera o token dessa renderização (o que vai pro form e pro link
    # "Atualizar recomendações" nessa página) só agora, no fim - assim o
    # token que sobra pendente na sessão é sempre o da ÚLTIMA página
    # mostrada, e qualquer token de uma página antiga (inclusive o que
    # acabou de ser consumido, se foi o caso) já não serve mais.
    token_busca = gerar_e_guardar_token_busca()

    return render_template_string(
        PAGINA, odd_min=odd_min, odd_max=odd_max,
        prob_min=prob_min, prob_max=prob_max, buscou=buscou,
        individuais=individuais, multiplas=multiplas, motivo=motivo, banca_atual=round(banca_atual, 2),
        jogos_disponiveis=jogos_disponiveis,
        ultima_atualizacao_odds=ultima_atualizacao_odds, token_busca=token_busca,
        atualizacao_rodando=atualizacao_rodando,
        progresso_decorridos=progresso_decorridos,
        progresso_estimativa=progresso_estimativa,
        nav_html=barra_navegacao_nova("index", round(banca_atual, 2)),
        escudos=escudos,
    )


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta)
