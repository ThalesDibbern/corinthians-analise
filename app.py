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
import subprocess
import sys
import threading
from functools import wraps
from itertools import combinations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import psycopg2
from flask import Flask, render_template_string, request, redirect, Response, session, url_for, flash, get_flashed_messages, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

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

    esquerda = f'<span>Olá, {session.get("usuario_nome", "")}</span><a href="/logout" class="nav-sair">🚪 Sair</a>'
    direita = ""
    if banca_atual is not None:
        direita = f'<a href="/minhas-apostas" class="nav-banca">🏦 Banca: R$ {banca_atual:.2f}</a>'

    meta = f'<div class="nav-meta-esquerda">{esquerda}</div><div class="nav-meta-direita">{direita}</div>'

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
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Análise de Apostas</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
            background: #0d1117;
            color: #e6edf3;
            max-width: 1500px;
            margin: 0 auto;
            padding: 32px 20px 80px;
        }
        h1 {
            font-size: 1.6rem;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            margin: 0;
        }
        .subtitulo { color: #8b949e; margin: 4px 0 28px; text-align: center; }
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
        /* NOVO (acompanhamento em tempo real): painel que substitui as
           listas de odds enquanto a atualização está rodando. */
        .painel-progresso {
            background: #161b22; border: 1px solid #30363d; border-radius: 10px;
            padding: 28px 24px; margin-bottom: 22px; text-align: center;
        }
        .painel-progresso h3 { margin: 0 0 6px 0; font-size: 1.05rem; color: #e6edf3; }
        .painel-progresso .sub { margin: 0 0 20px 0; font-size: 0.85rem; color: #8b949e; }
        .barra-progresso-fundo {
            width: 100%; max-width: 520px; height: 10px; background: #0d1117;
            border: 1px solid #30363d; border-radius: 999px;
            margin: 0 auto 12px auto; overflow: hidden;
        }
        .barra-progresso-preenchida {
            height: 100%; width: 0%; border-radius: 999px;
            background: linear-gradient(90deg, #1f6feb, #388bfd);
            transition: width 0.6s ease;
        }
        /* Passou da estimativa: para de crescer e vira listrada animada -
           sinaliza "ainda rodando, só demorando mais que o previsto" sem
           mentir que está quase acabando nem travar em 100%. */
        .barra-progresso-preenchida.estourou {
            background: repeating-linear-gradient(45deg, #1f6feb, #1f6feb 10px, #388bfd 10px, #388bfd 20px);
            background-size: 28px 28px;
            animation: desliza-listras 1s linear infinite;
        }
        @keyframes desliza-listras {
            from { background-position: 0 0; }
            to { background-position: 28px 0; }
        }
        .progresso-tempos {
            display: flex; justify-content: center; gap: 18px; flex-wrap: wrap;
            font-size: 0.85rem; color: #8b949e;
        }
        .progresso-tempos b { color: #e6edf3; }
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
        .coluna-verde-jogos { background: #23863633; color: #3fb950; border: 1px solid #3fb95055; }

        /* NOVO: seção "Jogos disponíveis" - um card por confronto que
           expande mostrando as odds daquele jogo. */
        .secao-jogos { margin-top: 26px; }
        .jogo-bloco {
            background: #0d1117; border: 1px solid #30363d; border-radius: 12px;
            margin-bottom: 10px; overflow: hidden;
        }
        .jogo-cabecalho {
            display: flex; align-items: center; justify-content: space-between; gap: 12px;
            padding: 14px 18px; cursor: pointer; user-select: none;
            background: #161b22; transition: background 0.15s;
        }
        .jogo-cabecalho:hover { background: #1c2230; }
        .jogo-titulo { font-weight: 600; font-size: 1rem; color: #e6edf3; }
        .jogo-data { font-size: 0.82rem; color: #8b949e; margin-top: 2px; }
        .jogo-contagem {
            background: #1f6feb33; color: #58a6ff; border: 1px solid #58a6ff55;
            border-radius: 999px; padding: 3px 12px; font-size: 0.8rem; white-space: nowrap;
        }
        .jogo-seta { color: #8b949e; font-size: 0.9rem; transition: transform 0.2s; }
        .jogo-bloco.aberto .jogo-seta { transform: rotate(180deg); }
        .jogo-corpo { display: none; padding: 12px 14px 14px; }
        .jogo-bloco.aberto .jogo-corpo { display: block; }
        .jogo-aviso-ocultas {
            font-size: 0.8rem; color: #d29922; text-align: center;
            padding: 8px; border-top: 1px solid #30363d; margin-top: 6px;
        }
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
                    window.location.replace('/?odd_min=' + encodeURIComponent(oddMin) + '&odd_max=' + encodeURIComponent(oddMax) + '&auto=1');
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
                <button type="submit">Gerar recomendações da rodada</button>
            </div>
            <p class="nota-espera">Se fizer mais de 1h desde a última atualização, pode demorar alguns segundos
                (o app aciona a busca de odds mais recentes antes de mostrar o resultado).</p>
        </form>
        {% if ultima_atualizacao_odds %}
        <div class="linha-atualizacao">
            <span>🔄 Última atualização de odds gerada às <b>{{ ultima_atualizacao_odds }}</b></span>
            <a href="/?odd_min={{ odd_min }}&odd_max={{ odd_max }}&forcar=1&token_busca={{ token_busca }}" class="link-atualizar">Atualizar recomendações</a>
        </div>
        {% endif %}
    </div>

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
                    {% for j in c.jogos %}{{ j.data_jogo }} · {{ j.nosso_time }} x {{ j.adversario }}{% if not loop.last %} + {% endif %}{% endfor %}
                </span>
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
        <script>
            try {
                localStorage.setItem('ultima_busca_odd_min', '{{ odd_min }}');
                localStorage.setItem('ultima_busca_odd_max', '{{ odd_max }}');
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
            <h3>🔄 Atualizando odds e recalculando recomendações...</h3>
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

        {% if jogos_disponiveis %}
        <div class="secao-jogos">
            <div class="coluna-cabecalho coluna-verde-jogos">⚽ Jogos disponíveis ({{ jogos_disponiveis|length }})</div>
            {% for jd in jogos_disponiveis %}
            <div class="jogo-bloco" id="jogo-bloco-{{ loop.index }}">
                <div class="jogo-cabecalho" onclick="alternarJogo({{ loop.index }})">
                    <div>
                        <div class="jogo-titulo">{{ jd.rotulo }}</div>
                        <div class="jogo-data">{{ jd.data_jogo }}</div>
                    </div>
                    <div style="display:flex; align-items:center; gap:12px;">
                        <span class="jogo-contagem">{{ jd.total_odds }} odd{{ 's' if jd.total_odds != 1 else '' }}</span>
                        <span class="jogo-seta">▼</span>
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
</body>
</html>
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
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Histórico - Análise de Apostas</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
            background: #0d1117;
            color: #e6edf3;
            max-width: 1500px;
            margin: 0 auto;
            padding: 32px 20px 80px;
        }
        h1 { font-size: 1.5rem; margin: 0 0 4px; text-align: center; }
        .subtitulo { color: #8b949e; margin: 0 0 20px; text-align: center; }
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
            padding: 20px 24px; max-width: 1100px; width: 92%; max-height: 80vh; overflow-y: auto;
        }
        .modal-topo { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
        .modal-titulo { font-weight: 700; font-size: 0.95rem; }
        .modal-fechar { cursor: pointer; color: #8b949e; font-size: 1.1rem; }
        .modal-fechar:hover { color: #e6edf3; }
        .modal-linha {
            display: flex; justify-content: space-between; padding: 8px 0;
            border-bottom: 1px solid #21262d; font-size: 0.85rem;
        }
        /* NOVO (17/08/2026): tabela expansível por faixa de probabilidade */
        .calibracao-faixa { border-bottom: 1px solid #21262d; }
        .calibracao-faixa .modal-linha { border-bottom: none; }
        .modal-linha-clicavel { cursor: pointer; }
        .modal-linha-clicavel:hover { background: #1c2128; }
        .faixa-seta { display: inline-block; font-size: 0.7rem; color: #8b949e; transition: transform 0.15s; margin-left: 4px; }
        .faixa-detalhe { padding: 4px 0 12px; }
        .tabela-faixa-wrap { overflow-x: auto; max-width: 100%; }
        .tabela-faixa { border-collapse: collapse; width: 100%; font-size: 0.74rem; white-space: nowrap; }
        .tabela-faixa th {
            text-align: left; color: #8b949e; font-weight: 600; padding: 6px 10px;
            border-bottom: 1px solid #30363d; position: sticky; top: 0; background: #161b22;
        }
        .tabela-faixa td { padding: 6px 10px; border-bottom: 1px solid #21262d; color: #c9d1d9; }
        .tabela-faixa tr.linha-acertou td:last-child { color: #3fb950; }
        .tabela-faixa tr.linha-errou td:last-child { color: #f85149; }
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
            <div class="calibracao-faixa">
                <div class="modal-linha modal-linha-clicavel" onclick="toggleFaixaCalibracao({{ item.inicio }})">
                    <span>{{ item.inicio }}% - {{ item.fim }}%
                        <span class="faixa-seta" id="seta-faixa-{{ item.inicio }}">▾</span>
                    </span>
                    <span>
                        <b style="color:{{ '#3fb950' if item.taxa >= 50 else '#f85149' }}">{{ item.taxa }}% de acerto</b>
                        <span style="color:#8b949e; font-size: 0.85em;">
                            ({{ item.acertou }} acerto(s) / {{ item.errou }} erro(s))
                        </span>
                    </span>
                </div>
                <div class="faixa-detalhe" id="detalhe-faixa-{{ item.inicio }}" style="display:none;"></div>
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


        // NOVO (17/08/2026): tabela expansivel por faixa de probabilidade,
        // no card "Acertos e erros por probabilidade historica" - busca
        // sob demanda (so no primeiro clique de cada faixa) e guarda em
        // cache pra nao rebuscar se abrir/fechar de novo.
        const CACHE_FAIXA_CALIBRACAO = {};

        function toggleFaixaCalibracao(inicioFaixa) {
            const detalheDiv = document.getElementById('detalhe-faixa-' + inicioFaixa);
            const seta = document.getElementById('seta-faixa-' + inicioFaixa);
            const abrindo = detalheDiv.style.display === 'none';
            detalheDiv.style.display = abrindo ? 'block' : 'none';
            seta.style.transform = abrindo ? 'rotate(180deg)' : 'rotate(0deg)';
            if (!abrindo) return;

            if (CACHE_FAIXA_CALIBRACAO[inicioFaixa]) {
                renderizarTabelaFaixa(inicioFaixa, CACHE_FAIXA_CALIBRACAO[inicioFaixa]);
                return;
            }
            detalheDiv.innerHTML = '<div class="vazio">Carregando...</div>';
            fetch(`/api/calibracao/${inicioFaixa}`)
                .then(r => r.json())
                .then(itens => {
                    CACHE_FAIXA_CALIBRACAO[inicioFaixa] = itens;
                    renderizarTabelaFaixa(inicioFaixa, itens);
                });
        }

        function renderizarTabelaFaixa(inicioFaixa, itens) {
            const detalheDiv = document.getElementById('detalhe-faixa-' + inicioFaixa);
            if (!itens.length) {
                detalheDiv.innerHTML = '<div class="vazio">Nenhuma recomendacao avaliada nessa faixa ainda.</div>';
                return;
            }
            const escapar = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
            }[c]));
            const linhas = itens.map(i => `
                <tr class="linha-${i.resultado}">
                    <td>${i.data_jogo} \u00b7 ${escapar(i.nosso_time)} x ${escapar(i.adversario)}</td>
                    <td>${escapar(i.descricao)}</td>
                    <td>${escapar(i.casa_aposta)}</td>
                    <td>${i.odd_oferecida}</td>
                    <td>${i.probabilidade_historica}%</td>
                    <td>${i.valor_esperado}</td>
                    <td>${i.resultado}</td>
                </tr>
            `).join('');
            detalheDiv.innerHTML = `
                <div class="tabela-faixa-wrap">
                    <table class="tabela-faixa">
                        <thead>
                            <tr><th>Jogo</th><th>Mercado</th><th>Casa</th><th>Odd</th><th>Prob.</th><th>VE</th><th>Resultado</th></tr>
                        </thead>
                        <tbody>${linhas}</tbody>
                    </table>
                </div>
            `;
        }

        function mudarPagina(listaId, direcao) {
            const total = totalPaginas(listaId);
            let pagina = (paginaAtual[listaId] || 0) + direcao;
            pagina = Math.max(0, Math.min(total - 1, pagina));
            paginaAtual[listaId] = pagina;
            salvarPaginaNoNavegador(listaId, pagina);
            renderizarPagina(listaId);
        }

        ['lista-acertou', 'lista-errou', 'lista-pendente'].forEach(renderizarPagina);
    </script>

    {% if multiplas_destaque or resumo_multiplas.acertou + resumo_multiplas.errou > 0 %}
    <div class="coluna-cabecalho coluna-roxa" style="margin-top: 8px;">🎯 Múltiplas em Destaque ({{ multiplas_destaque|length }})</div>
    <p class="secao-subtitulo">As 5 melhores múltiplas (por probabilidade histórica) de cada jogo já concluído -
        capturadas de verdade ANTES do apito inicial (não é mais uma reconstrução hipotética depois do jogo já ter
        acontecido). Não tem relação com sua banca/ROI nem botão de salvar. Só as rodadas mais recentes mostram o
        card com detalhe completo; rodadas mais antigas saem da lista abaixo, mas continuam contando no percentual
        a seguir.</p>
    {% if resumo_multiplas.acertou + resumo_multiplas.errou > 0 %}
    <p class="secao-subtitulo" style="margin-top: -8px;">
        No total (incluindo rodadas já fora do detalhe): <b style="color:#3fb950">{{ resumo_multiplas.acertou }} acerto(s)</b> ·
        <b style="color:#f85149">{{ resumo_multiplas.errou }} erro(s)</b> ·
        <b>{{ resumo_multiplas.taxa }}%</b> de acerto — <u>esse percentual é só entre o top-5 de cada jogo;
        não soma com a taxa de acerto geral lá em cima, porque são grupos diferentes (esse aqui já vem pré-filtrado
        pras 5 melhores de cada jogo, o de cima não é).</u>
    </p>
    {% endif %}

    {% macro cartao_multipla(c) %}
        <div class="cartao item-pagina">
            <div class="cartao-topo">
                <span class="jogo">
                    {% for j in c.jogos %}{{ j.data_jogo }} · {{ j.nosso_time }} x {{ j.adversario }}{% if not loop.last %} + {% endif %}{% endfor %}
                </span>
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
    {% endmacro %}

    <div class="colunas-resultado">
        <div class="coluna">
            <div class="coluna-cabecalho coluna-verde">✅ Acertou ({{ multiplas_acertou|length }})</div>
            <div id="lista-multiplas-acertou">
                {% if multiplas_acertou %}
                    {% for c in multiplas_acertou %}{{ cartao_multipla(c) }}{% endfor %}
                {% else %}
                    <div class="vazio">Nenhum acerto ainda.</div>
                {% endif %}
            </div>
            {% if multiplas_acertou|length > 10 %}
            <div class="paginacao">
                <button class="btn-pagina" id="anterior-lista-multiplas-acertou" onclick="mudarPagina('lista-multiplas-acertou', -1)">← Anterior</button>
                <span id="label-lista-multiplas-acertou"></span>
                <button class="btn-pagina" id="proximo-lista-multiplas-acertou" onclick="mudarPagina('lista-multiplas-acertou', 1)">Próxima →</button>
            </div>
            {% endif %}
        </div>
        <div class="coluna">
            <div class="coluna-cabecalho coluna-vermelha">❌ Errou ({{ multiplas_errou|length }})</div>
            <div id="lista-multiplas-errou">
                {% if multiplas_errou %}
                    {% for c in multiplas_errou %}{{ cartao_multipla(c) }}{% endfor %}
                {% else %}
                    <div class="vazio">Nenhum erro ainda.</div>
                {% endif %}
            </div>
            {% if multiplas_errou|length > 10 %}
            <div class="paginacao">
                <button class="btn-pagina" id="anterior-lista-multiplas-errou" onclick="mudarPagina('lista-multiplas-errou', -1)">← Anterior</button>
                <span id="label-lista-multiplas-errou"></span>
                <button class="btn-pagina" id="proximo-lista-multiplas-errou" onclick="mudarPagina('lista-multiplas-errou', 1)">Próxima →</button>
            </div>
            {% endif %}
        </div>
    </div>

    <div class="secao-pendentes">
        <div class="coluna-cabecalho coluna-cinza">⏳ Pendente ({{ multiplas_pendente|length }})</div>
        <div id="lista-multiplas-pendente">
            {% if multiplas_pendente %}
                {% for c in multiplas_pendente %}{{ cartao_multipla(c) }}{% endfor %}
            {% else %}
                <div class="vazio">Nenhuma múltipla pendente no momento.</div>
            {% endif %}
        </div>
        {% if multiplas_pendente|length > 10 %}
        <div class="paginacao">
            <button class="btn-pagina" id="anterior-lista-multiplas-pendente" onclick="mudarPagina('lista-multiplas-pendente', -1)">← Anterior</button>
            <span id="label-lista-multiplas-pendente"></span>
            <button class="btn-pagina" id="proximo-lista-multiplas-pendente" onclick="mudarPagina('lista-multiplas-pendente', 1)">Próxima →</button>
        </div>
        {% endif %}
    </div>

    <script>
        ['lista-multiplas-acertou', 'lista-multiplas-errou', 'lista-multiplas-pendente'].forEach(renderizarPagina);
    </script>
    {% endif %}
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
    daria um número mais bonito, mas enganoso."""
    cur.execute(
        "SELECT resultado, COUNT(*) FROM historico_multiplas_destaque "
        "WHERE resultado IN ('acertou', 'errou') GROUP BY resultado"
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
    montar_resumo_multiplas, lido direto do banco)."""
    cur.execute(
        """
        SELECT jogo_id, rodada, casa_aposta, descricao, odd_combinada, jogos,
               probabilidade_combinada, resultado
        FROM historico_multiplas_destaque
        WHERE casa_aposta IS NOT NULL
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
        multiplas_destaque = buscar_multiplas_destaque(cur)
        resumo_multiplas = montar_resumo_multiplas(cur)
        multiplas_acertou = [c for c in multiplas_destaque if c["resultado"] == "acertou"]
        multiplas_errou = [c for c in multiplas_destaque if c["resultado"] == "errou"]
        multiplas_pendente = [c for c in multiplas_destaque if c["resultado"] == "pendente"]
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
        multiplas_destaque=multiplas_destaque, resumo_multiplas=resumo_multiplas,
        multiplas_acertou=multiplas_acertou, multiplas_errou=multiplas_errou,
        multiplas_pendente=multiplas_pendente,
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
            background: #0d1117; color: #e6edf3; max-width: 1500px;
            margin: 0 auto; padding: 32px 20px 80px;
        }
        h1 { font-size: 1.5rem; margin: 0 0 4px; text-align: center; }
        .subtitulo { color: #8b949e; margin: 0 0 20px; text-align: center; }
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

        /* NOVO (Criador de Odd) */
        .criador-odd-box {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 16px 20px; margin-bottom: 28px; display: flex; align-items: center;
            gap: 14px; cursor: pointer; transition: border-color 0.15s;
        }
        .criador-odd-box:hover { border-color: #58a6ff; }
        .criador-odd-icone { font-size: 1.6rem; }
        .criador-odd-titulo { font-weight: 700; font-size: 0.95rem; }
        .criador-odd-sub { color: #8b949e; font-size: 0.8rem; margin-top: 2px; }
        .modal-overlay {
            display: none; position: fixed; inset: 0; background: #000000aa;
            align-items: center; justify-content: center; z-index: 1000; padding: 20px;
        }
        .modal-overlay.aberto { display: flex; }
        .modal-caixa {
            background: #0d1117; border: 1px solid #30363d; border-radius: 14px;
            padding: 24px; max-width: 560px; width: 100%; max-height: 88vh; overflow-y: auto;
        }
        .modal-caixa h2 { margin: 0 0 4px; font-size: 1.15rem; }
        .modal-caixa .modal-sub { color: #8b949e; font-size: 0.8rem; margin: 0 0 18px; }
        .modal-campo { margin-bottom: 14px; }
        .modal-campo label {
            display: block; font-size: 0.78rem; color: #8b949e; margin-bottom: 5px;
        }
        .modal-campo select, .modal-campo input {
            width: 100%; background: #161b22; border: 1px solid #30363d; border-radius: 8px;
            color: #e6edf3; padding: 9px 12px; font-size: 0.88rem;
        }
        .modal-linha-dupla { display: flex; gap: 10px; }
        .modal-linha-dupla > div { flex: 1; }
        .btn-adicionar-perna {
            width: 100%; background: #1f6feb22; border: 1px solid #1f6feb66; color: #58a6ff;
            border-radius: 8px; padding: 10px; font-size: 0.85rem; font-weight: 600;
            cursor: pointer; margin-top: 4px;
        }
        .btn-adicionar-perna:hover { background: #1f6feb33; }
        .pernas-adicionadas { margin: 16px 0; }
        .perna-adicionada-item {
            background: #161b22; border: 1px solid #21262d; border-radius: 8px;
            padding: 8px 12px; font-size: 0.8rem; margin-bottom: 6px;
            display: flex; justify-content: space-between; align-items: center; gap: 8px;
        }
        .perna-adicionada-remover {
            color: #f85149; cursor: pointer; font-size: 0.75rem; flex-shrink: 0;
        }
        .modal-botoes-finais { display: flex; gap: 10px; margin-top: 18px; }
        .modal-botoes-finais button {
            flex: 1; padding: 11px; border-radius: 8px; font-size: 0.85rem; font-weight: 600;
            cursor: pointer; border: none;
        }
        .btn-fechar-modal { background: #21262d; color: #e6edf3; }
        .btn-salvar-odd { background: #238636; color: white; }
        .btn-salvar-odd:disabled { background: #21262d; color: #8b949e; cursor: not-allowed; }

        .cartao {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 16px 20px; margin-bottom: 12px;
        }
        .cartao-topo {
            display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 8px; flex-wrap: wrap; gap: 8px;
        }
        .descricao { color: #c9d1d9; font-size: 0.88rem; margin-bottom: 8px; line-height: 1.5; }
        .jogos-label { color: #58a6ff; font-size: 0.78rem; margin-bottom: 8px; font-weight: 600; }
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

        // NOVO (Criador de Odd) --------------------------------------------
        const JOGOS_CRIADOR_ODD = {{ jogos_criador_odd|tojson }};
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
            const selectJogo = document.getElementById('criador-odd-jogo');
            selectJogo.innerHTML = JOGOS_CRIADOR_ODD.map(j =>
                `<option value="${j.id}">${j.data_jogo} · ${j.nosso_time} x ${j.adversario}</option>`
            ).join('');

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
            atualizarJogadoresCriadorOdd();
            document.getElementById('modal-criador-odd').classList.add('aberto');
        }

        function fecharCriadorOdd() {
            document.getElementById('modal-criador-odd').classList.remove('aberto');
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
            const jogoInfo = JOGOS_CRIADOR_ODD.find(j => String(j.id) === String(jogoId));
            const tipoPadrao = document.getElementById('criador-odd-mercado').value;
            const mercado = MERCADOS_CRIADOR_ODD[tipoPadrao];
            const direcao = document.getElementById('criador-odd-direcao').value;
            const linha = document.getElementById('criador-odd-linha').value;
            const selectJogador = document.getElementById('criador-odd-jogador');
            const jogadorId = mercado.jogador ? parseInt(selectJogador.value) : null;
            const jogadorNome = mercado.jogador ? selectJogador.options[selectJogador.selectedIndex].text : null;

            if (mercado.modo === 'linha' && !linha) {
                alert('Preenche a linha (ex: 3.5).');
                return;
            }
            if (mercado.jogador && !jogadorId) {
                alert('Escolhe um jogador.');
                return;
            }

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
                    <span>${p.descricao}</span>
                    <span class="perna-adicionada-remover" onclick="removerPernaCriadorOdd(${i})">✕ remover</span>
                </div>`
            ).join('');
            document.getElementById('btn-salvar-criador-odd').disabled = pernasCriadorOdd.length === 0;
        }

        function salvarCriadorOdd() {
            if (pernasCriadorOdd.length === 0) return;
            const valor = document.getElementById('criador-odd-valor').value;
            const odd = document.getElementById('criador-odd-odd').value;
            if (!valor || !odd) {
                alert('Preenche o valor apostado e a odd.');
                return;
            }
            document.getElementById('campo-criador-odd-descricao').value = pernasCriadorOdd.map(p => p.descricao).join(' + ');
            document.getElementById('campo-criador-odd-pernas').value = JSON.stringify(pernasCriadorOdd);
            document.getElementById('campo-criador-odd-valor').value = valor;
            document.getElementById('campo-criador-odd-odd').value = odd;
            document.getElementById('form-criador-odd').submit();
        }
    </script>
</head>
<body>
    <h1>💰 Minhas Apostas</h1>
    <p class="subtitulo">Só o que você salvou com valor apostado - não inclui recomendações não salvas</p>

    <div class="modal-overlay" id="modal-criador-odd">
        <div class="modal-caixa">
            <h2>💡 Criador de Odd</h2>
            <p class="modal-sub">Apostou fora do app? Monta aqui, perna por perna, e salva - entra no seu ROI normalmente.</p>

            <div class="modal-campo">
                <label>Jogo</label>
                <select id="criador-odd-jogo" onchange="atualizarJogadoresCriadorOdd()"></select>
            </div>
            <div class="modal-campo">
                <label>Mercado</label>
                <select id="criador-odd-mercado" onchange="atualizarCamposCriadorOdd()"></select>
            </div>
            <div class="modal-campo" id="campo-criador-odd-jogador">
                <label>Jogador</label>
                <select id="criador-odd-jogador"></select>
            </div>
            <div class="modal-linha-dupla">
                <div class="modal-campo" id="campo-criador-odd-linha">
                    <label>Linha</label>
                    <input type="number" step="0.5" id="criador-odd-linha" placeholder="ex: 3.5">
                </div>
                <div class="modal-campo">
                    <label>Direção</label>
                    <select id="criador-odd-direcao"></select>
                </div>
            </div>
            <button type="button" class="btn-adicionar-perna" onclick="adicionarPernaCriadorOdd()">+ Adicionar perna</button>

            <div class="pernas-adicionadas" id="criador-odd-pernas-lista"></div>

            <div class="modal-linha-dupla">
                <div class="modal-campo">
                    <label>Valor apostado (R$)</label>
                    <input type="number" step="0.01" min="0.01" id="criador-odd-valor">
                </div>
                <div class="modal-campo">
                    <label>Odd final (a combinada, se for múltipla)</label>
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
                <button type="button" class="btn-salvar-odd" id="btn-salvar-criador-odd" onclick="salvarCriadorOdd()" disabled>💾 Salvar aposta</button>
            </div>
        </div>
    </div>

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

    <div class="criador-odd-box" onclick="abrirCriadorOdd()">
        <div class="criador-odd-icone">💡</div>
        <div>
            <div class="criador-odd-titulo">Criador de Odd</div>
            <div class="criador-odd-sub">Apostou fora do app (na Superbet, por exemplo)? Anota aqui pra entrar no seu ROI.</div>
        </div>
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
            {% if a.jogos_label %}
            <div class="jogos-label">🏟️ {{ a.jogos_label }}</div>
            {% endif %}
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
               resultado, retorno, criado_em, pernas
        FROM apostas_salvas
        WHERE usuario_id = %s
        ORDER BY criado_em DESC
        """,
        (usuario_id,),
    )
    colunas = ["id", "descricao", "casa_aposta", "odd_combinada", "valor_apostado",
               "resultado", "retorno", "criado_em", "pernas"]
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


def buscar_jogos_para_criador_odd(cur):
    """NOVO (Criador de Odd): jogos dos times rastreados, numa janela
    razoável em torno de "agora" (45 dias pra trás, 30 pra frente) - usado
    no seletor de jogo do popup. Não lista TODOS os jogos já coletados
    (seriam milhares, com 10 times x 5 temporadas) - só uma janela prática
    que cobre tanto uma aposta recente (já feita, só precisa ser anotada)
    quanto um jogo que ainda vai acontecer."""
    cur.execute(
        """
        SELECT j.id, t.nome, j.adversario, j.data_jogo
        FROM jogos j
        JOIN times t ON t.id = j.nosso_time_id
        WHERE j.data_jogo BETWEEN CURRENT_DATE - INTERVAL '45 days' AND CURRENT_DATE + INTERVAL '30 days'
        ORDER BY j.data_jogo DESC
        """
    )
    return [
        {"id": row[0], "nosso_time": row[1], "adversario": row[2], "data_jogo": str(row[3])}
        for row in cur.fetchall()
    ]


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
        jogos_criador_odd = buscar_jogos_para_criador_odd(cur)
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
        jogos_criador_odd=jogos_criador_odd,
        nav_html=barra_navegacao("roi", round(banca_atual, 2)),
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
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Jogadores - Análise de Apostas</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
            background: #0d1117; color: #e6edf3; max-width: 1500px;
            margin: 0 auto; padding: 32px 24px 80px;
        }
        h1 { font-size: 1.6rem; margin: 0 0 4px; text-align: center; }
        .subtitulo { color: #8b949e; margin: 0 0 16px; font-size: 0.88rem; text-align: center; }
        .subtitulo a { color: #58a6ff; }
        .titulo-coluna { font-size: 1.05rem; font-weight: 700; margin: 0 0 14px; text-align: center; }
        .vazio {
            text-align: center; color: #8b949e; padding: 24px; margin-top: 10px;
            background: #161b22; border: 1px dashed #30363d; border-radius: 12px; font-size: 0.9rem;
        }
        .busca {
            width: 100%; padding: 10px 14px; margin-bottom: 14px;
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
        .nenhum-resultado { display: none; }

        /* coluna 1: clubes (busca + lista, igual /time/<id>) */
        .clube-btn {
            display: flex; align-items: center; gap: 12px;
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 12px 16px; margin-bottom: 10px; text-decoration: none;
            color: #e6edf3; transition: border-color 0.15s;
        }
        .clube-btn:hover { border-color: #58a6ff; }
        .clube-selo {
            width: 34px; height: 34px; border-radius: 50%;
            background: transparent;
            flex-shrink: 0; object-fit: contain;
        }
        .clube-nome { font-weight: 700; font-size: 0.92rem; }
        .clube-sub { color: #8b949e; font-size: 0.75rem; }
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

        .grid-jogadores {
            display: grid; grid-template-columns: 1fr 1.5fr 1fr; gap: 22px; align-items: start; margin-top: 12px;
        }
        @media (max-width: 1000px) {
            .grid-jogadores { grid-template-columns: 1fr; }
        }
        """ + NAV_CSS + """
    </style>
</head>
<body>
    <h1>📈 Estatísticas de Jogadores</h1>
    <p class="subtitulo">Digite pra filtrar entre todos os jogadores de todos os clubes rastreados, na hora.
        Procurando estatística do TIME inteiro? <a href="/times">Ver Estatísticas de Times →</a></p>
    {{ nav_html|safe }}
    <script>
        // NOVO: se a pessoa já tinha um clube aberto antes (ex: Corinthians)
        // e voltou pra essa lista pelo botão da barra de navegação (sem
        // "?geral=1"), leva direto pro clube de novo, em vez de mostrar a
        // lista geral - o "📊 Estatísticas Gerais" (link com ?geral=1) é a
        // saída explícita pra ver a lista de verdade.
        if (!window.location.search.includes('geral=1')) {
            try {
                const clubeId = localStorage.getItem('ultimo_clube_id');
                if (clubeId) { window.location.replace('/clube/' + encodeURIComponent(clubeId)); }
            } catch (e) {}
        }
    </script>

    <div class="grid-jogadores">
        <div class="coluna-clubes">
            <input type="text" class="busca" id="busca-time" placeholder="Buscar times..." onkeyup="filtrarTimes()">
            <div id="lista-times">
            {% if clubes %}
                {% for c in clubes %}
                <a href="/clube/{{ c.id }}" class="clube-btn item-time item-pagina">
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
            {% else %}
                <div class="vazio">Nenhum clube rastreado ainda.</div>
            {% endif %}
            </div>
            <div class="vazio" id="nenhum-resultado-time" style="display:none;">Nenhum time encontrado com esse nome.</div>
            {% if clubes|length > 10 %}
            <div class="paginacao">
                <button class="btn-pagina" id="anterior-lista-times" onclick="mudarPagina('lista-times', -1)">← Anterior</button>
                <span id="label-lista-times"></span>
                <button class="btn-pagina" id="proximo-lista-times" onclick="mudarPagina('lista-times', 1)">Próxima →</button>
            </div>
            {% endif %}
        </div>

        <div class="coluna-jogadores">
            <input type="text" class="busca" id="busca" placeholder="Buscar jogador por nome (ex: Yuri Alberto)..." onkeyup="filtrar()">
            <div id="lista">
            {% if jogadores %}
                {% for j in jogadores %}
                <div class="cartao jogador-card item-pagina">
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

                    <div class="bloco">
                        <div class="bloco-titulo">Cartões rumo à suspensão</div>
                        <div class="binario-texto">
                            {% if j.cartoes_suspensao == 2 %}
                            <span style="color:#f85149; font-weight:700;">⚠️ 2/3 - a 1 cartão da suspensão</span>
                            {% elif j.cartoes_suspensao == 1 %}
                            <span style="color:#d29922; font-weight:700;">1/3</span>
                            {% else %}
                            <span style="color:#3fb950;">0/3</span>
                            {% endif %}
                            <span style="color:#8b949e;"> · zera após a suspensão ser cumprida (temporada atual)</span>
                        </div>
                    </div>
                </div>
                {% endfor %}
            {% else %}
                <div class="vazio">Ainda não há padrões calculados pra nenhum jogador ainda.</div>
            {% endif %}
            </div>
            <div class="vazio nenhum-resultado" id="nenhum-resultado">Nenhum jogador encontrado com esse nome.</div>
            {% if jogadores|length > 10 %}
            <div class="paginacao">
                <button class="btn-pagina" id="anterior-lista" onclick="mudarPagina('lista', -1)">← Anterior</button>
                <span id="label-lista"></span>
                <button class="btn-pagina" id="proximo-lista" onclick="mudarPagina('lista', 1)">Próxima →</button>
            </div>
            {% endif %}
        </div>

        <div class="coluna-lideres">
            <div class="titulo-coluna">Líderes de Estatísticas do Campeonato</div>
            {% for l in lideres %}
            <div class="lider-card">
                <div class="lider-titulo lider-cor-{{ loop.index0 % 6 }}">{{ l.titulo }}</div>
                {% if l.jogador_nome %}
                <table class="tabela-lider">
                    <thead>
                        <tr><th>Jogador</th><th>Últimos 5 jogos</th><th>Total</th></tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td>{{ l.jogador_nome }}</td>
                            <td>{{ l.media_5 }}</td>
                            <td>{{ l.total }}</td>
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
        function filtrar() {
            const termo = document.getElementById('busca').value.toLowerCase();
            let visiveis = 0;
            document.querySelectorAll('.jogador-card').forEach(function(card) {
                const nome = card.querySelector('.nome-jogador').textContent.toLowerCase();
                const bate = nome.includes(termo);
                card.dataset.escondidoBusca = bate ? '0' : '1';
                if (bate) visiveis++;
            });
            document.getElementById('nenhum-resultado').style.display =
                (termo && visiveis === 0) ? '' : 'none';
            paginaAtual['lista'] = 0;
            renderizarPagina('lista');
        }

        function filtrarTimes() {
            const termo = document.getElementById('busca-time').value.toLowerCase();
            let visiveis = 0;
            document.querySelectorAll('.item-time').forEach(function(card) {
                const nome = card.querySelector('.clube-nome').textContent.toLowerCase();
                const bate = nome.includes(termo);
                card.dataset.escondidoBusca = bate ? '0' : '1';
                if (bate) visiveis++;
            });
            document.getElementById('nenhum-resultado-time').style.display = (termo && visiveis === 0) ? '' : 'none';
            paginaAtual['lista-times'] = 0;
            renderizarPagina('lista-times');
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
                const paginaDoItem = Math.floor(visivelIndice / TAMANHO_PAGINA);
                item.style.display = (paginaDoItem === pagina) ? '' : 'none';
                visivelIndice++;
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

        renderizarPagina('lista-times');
        renderizarPagina('lista');
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
            background: #0d1117; color: #e6edf3; max-width: 1500px;
            margin: 0 auto; padding: 32px 24px 80px;
        }
        h1 { font-size: 1.6rem; margin: 0 0 4px; text-align: center; }
        .selo-titulo {
            width: 34px; height: 34px; border-radius: 50%;
            background: transparent;
            flex-shrink: 0; object-fit: contain;
        }
        .subtitulo { color: #8b949e; margin: 0 0 16px; font-size: 0.88rem; text-align: center; }
        .link-voltar { color: #8b949e; text-decoration: none; font-size: 0.85rem; }
        .link-voltar:hover { text-decoration: underline; }
        .titulo-coluna { font-size: 1.05rem; font-weight: 700; margin: 0 0 14px; text-align: center; }

        .grid-clube {
            display: grid; grid-template-columns: 1fr 1.6fr 1fr; gap: 22px; align-items: start; margin-top: 12px;
        }
        @media (max-width: 1000px) {
            .grid-clube { grid-template-columns: 1fr; }
        }

        /* coluna 1: geral + elenco por posição */
        .btn-geral {
            display: block; text-align: center; background: #161b22; border: 1px solid #30363d;
            color: #c9d1d9; border-radius: 10px; padding: 10px; margin-bottom: 16px;
            text-decoration: none; font-weight: 600; font-size: 0.85rem;
        }
        .btn-geral:hover { border-color: #58a6ff; }
        .grupo-posicao { margin-bottom: 20px; }
        .grupo-posicao-titulo { font-weight: 700; font-size: 0.95rem; margin-bottom: 8px; }
        .paginacao {
            display: flex; align-items: center; justify-content: center; gap: 14px;
            margin: 14px 0 4px; font-size: 0.82rem; color: #8b949e;
        }
        .btn-pagina {
            background: #161b22; border: 1px solid #30363d; color: #e6edf3;
            border-radius: 8px; padding: 6px 14px; font-size: 0.85rem; cursor: pointer;
        }
        .btn-pagina:hover:not(:disabled) { border-color: #58a6ff; }
        .btn-pagina:disabled { opacity: 0.35; cursor: default; }
        .lista-elenco { list-style: none; margin: 0; padding: 0; }
        .lista-elenco li {
            padding: 6px 0 6px 8px; font-size: 0.88rem; color: #c9d1d9;
            border-left: 2px solid #30363d; margin-bottom: 2px;
        }

        /* coluna 2: cabeçalho do clube */
        .cabecalho-clube {
            display: flex; align-items: center; gap: 14px;
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 18px 22px; margin-bottom: 14px; font-size: 1.15rem; font-weight: 700;
        }

        /* coluna 3: líderes de estatística do time */
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

        /* NOVO (Últimos 5 jogos do jogador) */
        .ultimos-jogos-jogador-toggle {
            display: flex; justify-content: space-between; align-items: center;
            cursor: pointer; font-weight: 600; font-size: 0.85rem; padding: 4px 0;
        }
        .jogo-seta { color: #8b949e; transition: transform 0.15s; }
        .ultimos-jogos-jogador-lista {
            display: none; margin-top: 8px;
        }
        .ultimos-jogos-jogador-lista.aberto { display: block; }
        .jogo-jogador-card {
            background: #0d1117; border: 1px solid #21262d; border-radius: 8px;
            padding: 8px 10px; margin-bottom: 6px; cursor: pointer;
        }
        .jogo-jogador-linha {
            display: flex; justify-content: space-between; align-items: center;
            font-size: 0.8rem; gap: 8px;
        }
        .jogo-jogador-data { color: #8b949e; font-size: 0.74rem; white-space: nowrap; }
        .jogo-jogador-detalhe { display: none; margin-top: 8px; }
        .jogo-jogador-card.aberto .jogo-jogador-detalhe { display: block; }
        .tabela-detalhe-jogo { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
        .tabela-detalhe-jogo td { padding: 5px 4px; border-top: 1px solid #21262d; }
        .tabela-detalhe-jogo td:first-child { color: #8b949e; }
        .tabela-detalhe-jogo td:last-child { text-align: right; font-weight: 600; }

            border-radius: 8px; font-size: 0.9rem;
        }
        .cartao {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 16px 20px; margin-bottom: 12px;
        }
        /* NOVO: card de jogador com separação bem mais forte que o .cartao
           genérico - cada jogador tem bastante conteúdo empilhado
           (cartão, faltas, chutes, desarmes...), então a borda sutil de
           1px do .cartao normal se perdia de vista ao rolar a página.
           Mais espaço entre um jogador e outro (28px) + borda mais clara
           + fundo levemente diferente do resto da página, pra ficar
           óbvio onde um jogador termina e o outro começa. */
        .jogador-card {
            background: #161b22; border: 1px solid #3d444d; border-radius: 14px;
            padding: 20px 22px; margin-bottom: 28px;
            box-shadow: 0 0 0 1px #ffffff08, 0 4px 12px #00000033;
        }
        .nome-jogador {
            display: inline-block; font-weight: 700; font-size: 1rem;
            background: #1f6feb1f; color: #79c0ff; padding: 5px 14px;
            border-radius: 999px; margin-bottom: 14px;
        }
        .busca {
            width: 100%; padding: 10px 14px; margin-bottom: 14px;
            background: #161b22; border: 1px solid #30363d; color: #e6edf3;
            border-radius: 8px; font-size: 0.9rem;
        }
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
        /* NOVO (Fase D, só exibição) */
        .jogo-morto-aviso {
            background: #f8514912; border: 1px solid #f8514944; border-radius: 12px;
            padding: 10px 18px; margin: -12px 0 20px; font-size: 0.8rem; color: #ffa198;
        }
        .contexto-jogo-aviso {
            background: #16212e; border: 1px solid #21262d; border-radius: 12px;
            padding: 10px 18px; margin: -12px 0 20px; font-size: 0.8rem; color: #c9d1d9;
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
        """ + NAV_CSS + """
    </style>
</head>
<body>
    <h1>📈 Estatísticas de Jogadores</h1>
    <p class="subtitulo">Elenco completo, última escalação titular confirmada e líderes de estatística do time.</p>
    {{ nav_html|safe }}
    <script>
        try { localStorage.setItem('ultimo_clube_id', '{{ time_id }}'); } catch (e) {}
    </script>

    {% with mensagens = get_flashed_messages(with_categories=true) %}
        {% for categoria, texto in mensagens %}
        <div class="flash flash-{{ categoria }}">{{ texto }}</div>
        {% endfor %}
    {% endwith %}

    <div class="grid-clube">
        <div class="coluna-elenco">
            <a href="/jogadores?geral=1" class="btn-geral">📊 Estatísticas Gerais</a>
            {% if elenco_por_posicao %}
                {% for grupo in elenco_por_posicao %}
                <div class="grupo-posicao">
                    <div class="grupo-posicao-titulo">{{ grupo.icone }} {{ grupo.titulo }}</div>
                    <ul class="lista-elenco">
                        {% for nome in grupo.jogadores %}
                        <li>{{ nome }}</li>
                        {% endfor %}
                    </ul>
                </div>
                {% endfor %}
            {% else %}
                <div class="vazio">Elenco ainda não disponível.</div>
            {% endif %}
        </div>

        <div class="coluna-principal">
            <div class="cabecalho-clube">
                {% if escudo_url %}
                <img src="{{ escudo_url }}" alt="{{ nome_clube }}" class="selo-titulo" onerror="this.outerHTML='<span class=&quot;selo-titulo&quot;></span>'">
                {% else %}
                <span class="selo-titulo"></span>
                {% endif %}
                {{ nome_clube }}
            </div>

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
    {% if jogo_morto and jogo_morto.morto %}
    <div class="jogo-morto-aviso">
        ⚠️ Jogo sem nada em jogo pro {{ nome_clube }} - matematicamente não pode mais alcançar o G4 nem cair no Z4
        ({{ jogo_morto.rodadas_restantes }} rodada(s) restante(s), atualmente na {{ jogo_morto.posicao_atual }}ª
        posição). Cenário simplificado (não considera Libertadores/Sul-Americana ainda) - só informativo.
    </div>
    {% endif %}
    {% if contexto_jogo %}
    <div class="contexto-jogo-aviso">
        📊 Contexto: {{ nome_clube }} está na {{ contexto_jogo.posicao }}ª posição ({{ contexto_jogo.pontos }} pts)
        {% if contexto_jogo.distancia_objetivo is not none and contexto_jogo.distancia_objetivo > 0 %}
            · a {{ contexto_jogo.distancia_objetivo }} ponto(s) de {{ contexto_jogo.objetivo }}
        {% elif contexto_jogo.zona == 'g4' %}
            · já está no G4
        {% endif %}
        {% if contexto_jogo.distancia_z4 is not none %}
            · {{ contexto_jogo.distancia_z4 }} ponto(s) acima da saída do Z4
        {% endif %}
        · adversário ({{ proximo_jogo.adversario }}) está na {{ contexto_jogo.adversario_posicao }}ª posição
        {% if contexto_jogo.adversario_forca %}(considerado <b>{{ contexto_jogo.adversario_forca }}</b>){% endif %}.
    </div>
    {% endif %}
    <div class="botoes-topo">
        <button class="btn-acao" onclick="gerarTresApostas()">🎯 Gerar 3 apostas automáticas</button>
        <button class="btn-acao" onclick="limparSelecao()">🧹 Limpar seleção</button>
    </div>
    {% endif %}

    {% if jogadores_na_regua %}
    <div class="cartao" style="border-color:#f8514966; background:#f8514912; margin-bottom: 16px;">
        <b style="color:#f85149;">⚠️ {{ jogadores_na_regua|length }} jogador(es) a 1 cartão da suspensão:</b>
        <span style="color:#c9d1d9;">{{ jogadores_na_regua|join(", ") }}</span>
        <div style="color:#8b949e; font-size:0.8rem; margin-top:6px;">
            Com tanta gente na régua, o time tende a evitar risco - considera isso ao olhar previsão de cartões desse jogo.
        </div>
    </div>
    {% endif %}

    <input type="text" class="busca" id="busca" placeholder="Buscar jogador..." onkeyup="filtrar()">

    <div id="lista">
    {% if jogadores %}
        {% for j in jogadores %}
        <div class="cartao jogador-card item-pagina">
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

            <div class="bloco">
                <div class="bloco-titulo">Cartões rumo à suspensão</div>
                <div class="binario-texto">
                    {% if j.cartoes_suspensao == 2 %}
                    <span style="color:#f85149; font-weight:700;">⚠️ 2/3 - a 1 cartão da suspensão</span>
                    {% elif j.cartoes_suspensao == 1 %}
                    <span style="color:#d29922; font-weight:700;">1/3</span>
                    {% else %}
                    <span style="color:#3fb950;">0/3</span>
                    {% endif %}
                </div>
            </div>

            {% if j.correlacao_jogador %}
            <div class="bloco">
                <div class="bloco-titulo">🧩 Efeito no adversário quando sofre muita falta</div>
                <div class="binario-texto" style="line-height:1.5;">
                    Média pessoal: <b>{{ j.correlacao_jogador.media_pessoal }}</b> falta(s) sofrida(s)/jogo.
                    Nos jogos em que sofreu <b>acima</b> dessa média, os defensores do adversário levaram
                    <b class="{{ 'acima' if j.correlacao_jogador.sobe_junto else 'abaixo' }}">{{ j.correlacao_jogador.valor_acima }} cartão(ões)/jogo</b>
                    · abaixo da média: <b class="{{ 'abaixo' if j.correlacao_jogador.sobe_junto else 'acima' }}">{{ j.correlacao_jogador.valor_abaixo }}</b>.
                    <span style="color:#8b949e; font-size:0.75rem;">({{ j.correlacao_jogador.jogos_total }} jogo(s) - não indica QUAL defensor especificamente, a API não informa quem marcou quem.)</span>
                </div>
            </div>
            {% endif %}

            {% if j.ultimos_jogos is not none %}
            <div class="bloco">
                <div class="ultimos-jogos-jogador-toggle" onclick="toggleUltimosJogosJogador({{ j.jogador_id }})">
                    🗓️ Últimos 5 jogos <span class="jogo-seta">▾</span>
                </div>
                <div class="ultimos-jogos-jogador-lista" id="ultimos-jogos-jogador-{{ j.jogador_id }}">
                    {% if j.ultimos_jogos %}
                        {% for jg in j.ultimos_jogos %}
                        <div class="jogo-jogador-card" id="jogo-jogador-{{ j.jogador_id }}-{{ jg.jogo_id }}"
                             onclick="toggleJogoJogador({{ j.jogador_id }}, {{ jg.jogo_id }})">
                            <div class="jogo-jogador-linha">
                                {% if jg.mandante %}
                                    <span>{{ jg.nosso_time_nome }} {{ jg.placar_nosso }} x {{ jg.placar_adversario }} {{ jg.adversario }}</span>
                                {% else %}
                                    <span>{{ jg.adversario }} {{ jg.placar_adversario }} x {{ jg.placar_nosso }} {{ jg.nosso_time_nome }}</span>
                                {% endif %}
                                <span class="jogo-jogador-data">{{ jg.data_jogo }}</span>
                            </div>
                            <div class="jogo-jogador-detalhe">
                                {% if jg.estatisticas %}
                                <table class="tabela-detalhe-jogo">
                                    <tbody>
                                        <tr><td>Minutos jogados</td><td>{{ jg.estatisticas.minutos if jg.estatisticas.minutos is not none else "-" }}</td></tr>
                                        <tr><td>Cartões</td><td>{{ jg.estatisticas.cartoes }}</td></tr>
                                        <tr><td>Faltas cometidas</td><td>{{ jg.estatisticas.faltas_cometidas if jg.estatisticas.faltas_cometidas is not none else "-" }}</td></tr>
                                        <tr><td>Faltas sofridas</td><td>{{ jg.estatisticas.faltas_sofridas if jg.estatisticas.faltas_sofridas is not none else "-" }}</td></tr>
                                        <tr><td>Chutes (total)</td><td>{{ jg.estatisticas.chutes if jg.estatisticas.chutes is not none else "-" }}</td></tr>
                                        <tr><td>Chutes no gol</td><td>{{ jg.estatisticas.chutes_no_gol if jg.estatisticas.chutes_no_gol is not none else "-" }}</td></tr>
                                        <tr><td>Desarmes</td><td>{{ jg.estatisticas.desarmes if jg.estatisticas.desarmes is not none else "-" }}</td></tr>
                                        <tr><td>Impedimentos</td><td>{{ jg.estatisticas.impedimentos if jg.estatisticas.impedimentos is not none else "-" }}</td></tr>
                                    </tbody>
                                </table>
                                {% else %}
                                <div class="vazio-pequeno">Sem dado detalhado desse jogo.</div>
                                {% endif %}
                            </div>
                        </div>
                        {% endfor %}
                    {% else %}
                        <div class="vazio-pequeno">Nenhum jogo com dado ainda pra esse jogador.</div>
                    {% endif %}
                </div>
            </div>
            {% endif %}
        </div>
        {% endfor %}
    {% else %}
        <div class="vazio">Ainda não há padrões calculados pra nenhum jogador desse clube.</div>
    {% endif %}
    </div>
    {% if jogadores|length > 10 %}
    <div class="paginacao">
        <button class="btn-pagina" id="anterior-lista" onclick="mudarPagina('lista', -1)">← Anterior</button>
        <span id="label-lista"></span>
        <button class="btn-pagina" id="proximo-lista" onclick="mudarPagina('lista', 1)">Próxima →</button>
    </div>
    {% endif %}
        </div>

        <div class="coluna-lideres">
            <div class="titulo-coluna">Líderes de Estatísticas do Time</div>
            {% for l in lideres_time %}
            <div class="lider-card">
                <div class="lider-titulo lider-cor-{{ loop.index0 % 6 }}">{{ l.titulo }}</div>
                {% if l.jogador_nome %}
                <table class="tabela-lider">
                    <thead>
                        <tr><th>Jogador</th><th>Últimos 5 jogos</th><th>Total</th></tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td>{{ l.jogador_nome }}</td>
                            <td>{{ l.media_5 }}</td>
                            <td>{{ l.total }}</td>
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
                const paginaDoItem = Math.floor(visivelIndice / TAMANHO_PAGINA);
                item.style.display = (paginaDoItem === pagina) ? '' : 'none';
                visivelIndice++;
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

        function filtrar() {
            const termo = document.getElementById('busca').value.toLowerCase();
            document.querySelectorAll('.jogador-card').forEach(function(card) {
                const nome = card.querySelector('.nome-jogador').textContent.toLowerCase();
                card.dataset.escondidoBusca = nome.includes(termo) ? '0' : '1';
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

        // NOVO (Últimos 5 jogos do jogador)
        function toggleUltimosJogosJogador(jogadorId) {
            document.getElementById('ultimos-jogos-jogador-' + jogadorId).classList.toggle('aberto');
        }
        function toggleJogoJogador(jogadorId, jogoId) {
            document.getElementById('jogo-jogador-' + jogadorId + '-' + jogoId).classList.toggle('aberto');
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

        /* NOVO (Correlação entre estatísticas, só exibição) */
        .cartao-correlacao {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 18px 20px; margin-top: 20px;
        }
        .correlacao-aviso { font-size: 0.8rem; color: #8b949e; margin: 0 0 14px; }
        .correlacao-grid {
            display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px;
        }
        @media (max-width: 1000px) {
            .correlacao-grid { grid-template-columns: 1fr; }
        }
        .correlacao-item {
            background: #0d1117; border: 1px solid #21262d; border-radius: 10px; padding: 12px 14px;
        }
        .correlacao-titulo { font-size: 0.88rem; font-weight: 700; margin-bottom: 8px; }
        .correlacao-linha { font-size: 0.8rem; color: #c9d1d9; margin-bottom: 4px; }
        .correlacao-linha .acima { color: #f85149; }
        .correlacao-linha .abaixo { color: #3fb950; }
        .correlacao-detalhe { font-size: 0.74rem; color: #8b949e; margin-top: 6px; }

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
            background: transparent;
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
    <script>
        // NOVO: mesma lógica de /jogadores, mas pro time - ver comentário lá
        if (!window.location.search.includes('geral=1')) {
            try {
                const timeId = localStorage.getItem('ultimo_time_id');
                if (timeId) { window.location.replace('/time/' + encodeURIComponent(timeId)); }
            } catch (e) {}
        }
    </script>

    {% if correlacoes %}
    <div class="cartao-correlacao">
        <div class="titulo-coluna" style="margin-bottom: 8px;">🔗 Correlação entre estatísticas (times rastreados)</div>
        <p class="correlacao-aviso">
            Dentro do MESMO jogo, como uma estatística tende a se mover junto com outra - "acima/abaixo da média"
            se refere ao total do jogo (mandante + visitante somados). <b>Dois destes pares JÁ afetam as
            recomendações</b> (chutes → escanteios entra no mercado de Escanteios Total do Jogo; faltas → cartões
            entra no de Cartões Total do Jogo): o sistema estima quanto de chute/falta esse confronto tende a
            produzir somando a média histórica dos dois times, e aplica o efeito correspondente. O par
            desarmes → faltas continua só informativo, porque não existe mercado de "Faltas Total do Jogo" pra
            apostar.
        </p>
        <div class="correlacao-grid">
            {% for c in correlacoes %}
            <div class="correlacao-item">
                <div class="correlacao-titulo">{{ c.titulo_a }} → {{ c.titulo_b }}</div>
                <div class="correlacao-linha">
                    Quando {{ c.titulo_a|lower }} fica <b>acima</b> da média ({{ c.media_a }}/jogo):
                    {{ c.titulo_b|lower }} médio é
                    <b class="{{ 'acima' if c.sobe_junto else 'abaixo' }}">{{ c.valor_acima }}/jogo</b>
                </div>
                <div class="correlacao-linha">
                    Quando fica <b>abaixo</b>: {{ c.titulo_b|lower }} médio é
                    <b class="{{ 'abaixo' if c.sobe_junto else 'acima' }}">{{ c.valor_abaixo }}/jogo</b>
                </div>
                <div class="correlacao-detalhe">{{ c.jogos_total }} jogo(s) analisados</div>
            </div>
            {% endfor %}
        </div>
    </div>
    {% endif %}

    {% if correlacoes_categoria %}
    <div class="cartao-correlacao" style="margin-top: 16px;">
        <div class="titulo-coluna" style="margin-bottom: 8px;">🧩 Correlação entre categorias de jogador (times rastreados)</div>
        <p class="correlacao-aviso">
            Cruza a estatística de um GRUPO de jogadores (por posição - Goleiro/Defensor/Meio-campista/Atacante,
            a granularidade máxima disponível) com a de outro grupo, dos dois times somados, no mesmo jogo.
            <b>Informativo por enquanto, não afeta nenhuma recomendação.</b>
        </p>
        <div class="correlacao-grid">
            {% for c in correlacoes_categoria %}
            <div class="correlacao-item">
                <div class="correlacao-titulo">{{ c.titulo_a }} → {{ c.titulo_b }}</div>
                <div class="correlacao-linha">
                    Quando {{ c.titulo_a|lower }} fica <b>acima</b> da média ({{ c.media_a }}/jogo):
                    {{ c.titulo_b|lower }} médio é
                    <b class="{{ 'acima' if c.sobe_junto else 'abaixo' }}">{{ c.valor_acima }}/jogo</b>
                </div>
                <div class="correlacao-linha">
                    Quando fica <b>abaixo</b>: {{ c.titulo_b|lower }} médio é
                    <b class="{{ 'abaixo' if c.sobe_junto else 'acima' }}">{{ c.valor_abaixo }}/jogo</b>
                </div>
                <div class="correlacao-detalhe">{{ c.jogos_total }} jogo(s) analisados</div>
            </div>
            {% endfor %}
        </div>
    </div>
    {% endif %}

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
                                {% if l.forma %}
                                <div class="forma-cel">
                                    {% for r in l.forma %}
                                    <span class="bola-forma bola-{{ 'v' if r == 'W' else ('e' if r == 'D' else 'd') }}">{{ '✓' if r == 'W' else ('–' if r == 'D' else '✕') }}</span>
                                    {% endfor %}
                                </div>
                                {% else %}
                                <span style="color:#8b949e; font-size:0.75rem;" title="A API-Football não mandou esse dado pra esse time">—</span>
                                {% endif %}
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
            background: #0d1117; color: #e6edf3; max-width: 1500px;
            margin: 0 auto; padding: 32px 24px 80px;
        }
        h1 { font-size: 1.6rem; margin: 0 0 4px; text-align: center; }
        .subtitulo { color: #8b949e; margin: 0 0 16px; font-size: 0.88rem; text-align: center; }
        .vazio {
            text-align: center; color: #8b949e; padding: 24px; margin-top: 10px;
            background: #161b22; border: 1px dashed #30363d; border-radius: 12px; font-size: 0.85rem;
        }
        .titulo-coluna { font-size: 1.05rem; font-weight: 700; margin: 0 0 14px; text-align: center; }
        """ + NAV_CSS + """

        .grid-time {
            display: grid; grid-template-columns: 1fr 1.4fr 1fr; gap: 22px; align-items: start; margin-top: 12px;
        }
        @media (max-width: 1000px) {
            .grid-time { grid-template-columns: 1fr; }
        }

        /* coluna 1: geral + busca + lista de times */
        .btn-geral {
            display: block; text-align: center; background: #161b22; border: 1px solid #30363d;
            color: #c9d1d9; border-radius: 10px; padding: 10px; margin-bottom: 14px;
            text-decoration: none; font-weight: 600; font-size: 0.85rem;
        }
        .btn-geral:hover { border-color: #58a6ff; }
        .busca {
            width: 100%; padding: 10px 14px; margin-bottom: 14px;
            background: #161b22; border: 1px solid #30363d; color: #e6edf3;
            border-radius: 8px; font-size: 0.9rem;
        }
        .clube-btn {
            display: flex; align-items: center; gap: 12px;
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 12px 16px; margin-bottom: 10px; text-decoration: none;
            color: #e6edf3; transition: border-color 0.15s;
        }
        .clube-btn:hover { border-color: #58a6ff; }
        .clube-btn.item-ativo { background: #1f6feb26; border: 1px solid #58a6ff88; }
        .clube-selo {
            width: 34px; height: 34px; border-radius: 50%;
            background: transparent;
            flex-shrink: 0; object-fit: contain;
        }
        .clube-nome { font-weight: 700; font-size: 0.92rem; }
        .clube-sub { color: #8b949e; font-size: 0.75rem; }
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

        /* coluna 2: cabeçalho do time + blocos de estatística */
        .selo-titulo {
            width: 34px; height: 34px; border-radius: 50%;
            background: transparent;
            flex-shrink: 0; object-fit: contain;
        }
        .cabecalho-time {
            display: flex; align-items: center; gap: 14px;
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 18px 22px; margin-bottom: 14px; font-size: 1.15rem; font-weight: 700;
        }
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

        /* NOVO (estilo de jogo - Fase 1, só exibição): cartão com o
           "jeito de jogar" do time (ofensivo/defensivo), em impedimento
           e cartão - nada aqui influencia recomendação/VE, é só
           informativo. */
        .estilo-aviso {
            color: #8b949e; font-size: 0.78rem; margin: 6px 0 0;
        }
        .estilo-grid {
            display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 10px;
        }
        @media (max-width: 700px) {
            .estilo-grid { grid-template-columns: 1fr; }
        }
        .estilo-papel-titulo {
            font-size: 0.78rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.03em;
            margin-bottom: 6px;
        }
        .estilo-valor {
            font-size: 1.05rem; font-weight: 700; margin-bottom: 2px;
        }
        .estilo-valor.acima { color: #f85149; }
        .estilo-valor.abaixo { color: #3fb950; }
        .estilo-valor.neutro { color: #8b949e; }
        .estilo-detalhe { font-size: 0.76rem; color: #8b949e; }

        /* NOVO (Fase B - padrão por rodada, só exibição) */
        .quebra-rodada-item {
            padding: 10px 0; border-top: 1px solid #21262d;
        }
        .quebra-rodada-item:first-of-type { border-top: none; padding-top: 4px; }
        .quebra-rodada-titulo { font-size: 0.85rem; font-weight: 600; margin-bottom: 3px; }
        .quebra-rodada-texto { font-size: 0.82rem; color: #c9d1d9; }
        .quebra-rodada-texto .acima { color: #f85149; }
        .quebra-rodada-texto .abaixo { color: #3fb950; }

        /* NOVO (Fase C - comportamento por zona, só exibição) */
        .zona-momento-grid {
            display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px;
        }
        @media (max-width: 700px) {
            .zona-momento-grid { grid-template-columns: 1fr; }
        }
        .zona-outros-mercados { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px; }
        .zona-outros-item {
            font-size: 0.78rem; color: #c9d1d9; background: #161b22;
            border: 1px solid #21262d; border-radius: 6px; padding: 4px 8px;
        }

        /* NOVO (Correlação entre estatísticas, só exibição) */
        .cartao-correlacao {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            padding: 18px 20px;
        }
        .correlacao-aviso { font-size: 0.8rem; color: #8b949e; margin: 0 0 14px; }
        .correlacao-grid { display: grid; gap: 14px; }
        .correlacao-item {
            background: #0d1117; border: 1px solid #21262d; border-radius: 10px; padding: 12px 14px;
        }
        .correlacao-titulo { font-size: 0.88rem; font-weight: 700; margin-bottom: 8px; }
        .correlacao-linha { font-size: 0.8rem; color: #c9d1d9; margin-bottom: 4px; }
        .correlacao-linha .acima { color: #f85149; }
        .correlacao-linha .abaixo { color: #3fb950; }
        .correlacao-detalhe { font-size: 0.74rem; color: #8b949e; margin-top: 6px; }

        /* coluna 3: posição na tabela + últimos jogos */
        .tabela-vizinhos { width: 100%; border-collapse: collapse; margin-bottom: 26px; }
        .linha-vizinho td {
            padding: 10px 8px; border-bottom: 1px solid #21262d; font-size: 0.92rem;
        }
        .linha-vizinho.ativa td { background: #1f6feb26; border-radius: 8px; font-weight: 700; }
        .pos-vizinho { font-size: 1.3rem; font-weight: 700; color: #8b949e; width: 36px; }
        .linha-vizinho.ativa .pos-vizinho { color: #58a6ff; }
        .escudo-vizinho { width: 26px; height: 26px; object-fit: contain; vertical-align: middle; margin-right: 8px; }
        .jogo-card {
            background: #161b22; border: 1px solid #30363d; border-radius: 12px;
            margin-bottom: 10px; cursor: pointer; overflow: hidden;
        }
        .jogo-placar-linha {
            display: flex; align-items: center; justify-content: space-between; gap: 8px;
            padding: 12px 16px;
        }
        .jogo-time { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 0.88rem; flex: 1; }
        .jogo-time.jogo-time-direita { justify-content: flex-end; text-align: right; }
        .escudo-jogo { width: 24px; height: 24px; object-fit: contain; flex-shrink: 0; }
        .jogo-placar { font-weight: 700; font-size: 1rem; white-space: nowrap; padding: 0 6px; }
        .jogo-seta { color: #8b949e; transition: transform 0.15s; }
        .jogo-card.aberto .jogo-seta { transform: rotate(180deg); }
        .jogo-detalhe { display: none; padding: 0 16px 14px; }
        .jogo-card.aberto .jogo-detalhe { display: block; }
        .tabela-detalhe-jogo { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
        .tabela-detalhe-jogo th { color: #8b949e; font-weight: 600; padding: 6px 4px; text-align: center; }
        .tabela-detalhe-jogo th:first-child { text-align: left; }
        .tabela-detalhe-jogo td { padding: 6px 4px; text-align: center; border-top: 1px solid #21262d; }
        .tabela-detalhe-jogo td:first-child { text-align: left; color: #8b949e; }

        /* NOVO (busca de confronto direto) */
        .confronto-busca-wrap { position: relative; margin-bottom: 12px; }
        .confronto-busca-wrap .busca { margin-bottom: 0; padding-right: 76px; }
        .confronto-limpar {
            position: absolute; right: 10px; top: 50%; transform: translateY(-50%);
            color: #8b949e; font-size: 0.72rem; cursor: pointer; white-space: nowrap;
        }
        .confronto-limpar:hover { color: #f85149; }
        .confronto-data { color: #8b949e; font-size: 0.74rem; padding: 0 16px 10px; margin-top: -6px; }
        .btn-ver-mais-confronto {
            display: block; width: 100%; background: #161b22; border: 1px solid #30363d;
            color: #58a6ff; border-radius: 8px; padding: 10px; margin-bottom: 10px;
            font-size: 0.82rem; font-weight: 600; cursor: pointer;
        }
        .btn-ver-mais-confronto:hover { border-color: #58a6ff; }
    </style>
</head>
<body>
    <h1>
        {% if escudo_url %}
        <img src="{{ escudo_url }}" alt="{{ nome_time }}" class="selo-titulo" style="display:inline-block; vertical-align:middle; margin-right:8px;" onerror="this.outerHTML='<span class=&quot;selo-titulo&quot;></span>'">
        {% endif %}
        Estatísticas de Time
    </h1>
    <p class="subtitulo">{{ nome_time }} - frequência histórica (só o lado do {{ nome_time }}), posição no
        Brasileirão e últimos jogos - estatísticas normais, sem depender de nenhuma odd disponível na casa de apostas.</p>
    {{ nav_html|safe }}
    <script>
        try { localStorage.setItem('ultimo_time_id', '{{ time_id }}'); } catch (e) {}
    </script>

    <div class="grid-time">
        <div class="coluna-lista">
            <a href="/times?geral=1" class="btn-geral">📊 Estatísticas Gerais</a>
            <input type="text" class="busca" id="busca-time" placeholder="Buscar times..." onkeyup="filtrarTimes()">
            <div id="lista-times">
            {% if clubes %}
                {% for c in clubes %}
                <a href="/time/{{ c.id }}" class="clube-btn item-time item-pagina{{ ' item-ativo' if c.id == time_id else '' }}">
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
            {% else %}
                <div class="vazio">Nenhum clube rastreado ainda.</div>
            {% endif %}
            </div>
            <div class="vazio" id="nenhum-resultado-time" style="display:none;">Nenhum time encontrado com esse nome.</div>
            {% if clubes|length > 10 %}
            <div class="paginacao">
                <button class="btn-pagina" id="anterior-lista-times" onclick="mudarPagina('lista-times', -1)">← Anterior</button>
                <span id="label-lista-times"></span>
                <button class="btn-pagina" id="proximo-lista-times" onclick="mudarPagina('lista-times', 1)">Próxima →</button>
            </div>
            {% endif %}
        </div>

        <div class="coluna-principal">
            <div class="cabecalho-time">
                {% if escudo_url %}
                <img src="{{ escudo_url }}" alt="{{ nome_time }}" class="selo-titulo" onerror="this.outerHTML='<span class=&quot;selo-titulo&quot;></span>'">
                {% else %}
                <span class="selo-titulo"></span>
                {% endif %}
                {{ nome_time }}
            </div>

            {% if estilo_time %}
            <div class="cartao">
                <div class="bloco-topo">
                    <div class="bloco-titulo">🧬 Estilo de jogo</div>
                </div>
                <p class="estilo-aviso">
                    Ofensivo = o quanto o próprio {{ nome_time }} gera esse evento. Defensivo = o quanto jogar
                    CONTRA o {{ nome_time }} faz o adversário gerar mais ou menos esse evento. Comparado só entre
                    os times rastreados hoje - <b>já usado como ajuste na fórmula de recomendação (cartão e
                    impedimento de jogador), além de aparecer aqui pra consulta.</b>
                </p>
                {% for tipo, papeis in estilo_time.items() %}
                {% set titulo_tipo = papeis.ofensivo.titulo if papeis.ofensivo is defined else papeis.defensivo.titulo %}
                <div style="margin-top: 16px;">
                    <div class="bloco-titulo" style="font-size: 0.92rem; margin-bottom: 6px;">{{ titulo_tipo }}</div>
                    <div class="estilo-grid">
                        {% if papeis.ofensivo is defined %}
                        {% set d = papeis.ofensivo %}
                        <div>
                            <div class="estilo-papel-titulo">Ofensivo</div>
                            <div class="estilo-valor {{ 'acima' if d.desvio_pct > 0 else ('abaixo' if d.desvio_pct < 0 else 'neutro') }}">
                                {{ '+' if d.desvio_pct >= 0 else '' }}{{ d.desvio_pct }}% vs. média
                            </div>
                            <div class="estilo-detalhe">{{ d.media_time }}/jogo (liga: {{ d.media_liga }}) · {{ d.jogos_analisados }} jogo(s)</div>
                        </div>
                        {% endif %}
                        {% if papeis.defensivo is defined %}
                        {% set d = papeis.defensivo %}
                        <div>
                            <div class="estilo-papel-titulo">Defensivo (efeito no adversário)</div>
                            <div class="estilo-valor {{ 'acima' if d.desvio_pct > 0 else ('abaixo' if d.desvio_pct < 0 else 'neutro') }}">
                                {{ '+' if d.desvio_pct >= 0 else '' }}{{ d.desvio_pct }}% vs. média
                            </div>
                            <div class="estilo-detalhe">{{ d.media_time }}/jogo (liga: {{ d.media_liga }}) · {{ d.jogos_analisados }} jogo(s)</div>
                        </div>
                        {% endif %}
                    </div>
                </div>
                {% endfor %}
            </div>
            {% endif %}

            {% if quebras_rodada %}
            <div class="cartao">
                <div class="bloco-topo">
                    <div class="bloco-titulo">📈 Padrão por rodada</div>
                </div>
                <p class="estilo-aviso">
                    Onde o comportamento do {{ nome_time }} muda de forma mais nítida ao longo do campeonato,
                    juntando as temporadas coletadas - detectado automaticamente, sem faixa fixa definida na mão.
                    <b>Informativo por enquanto, não afeta nenhuma recomendação.</b>
                </p>
                {% for q in quebras_rodada %}
                <div class="quebra-rodada-item">
                    <div class="quebra-rodada-titulo">{{ q.titulo }}</div>
                    <div class="quebra-rodada-texto">
                        Quebra detectada na rodada <b>{{ q.rodada_quebra }}</b>: antes,
                        <b>{{ q.valor_antes }}{{ '%' if q.eh_percentual else '/jogo' }}</b> · depois,
                        <b class="{{ 'acima' if q.subiu else 'abaixo' }}">{{ q.valor_depois }}{{ '%' if q.eh_percentual else '/jogo' }}</b>
                    </div>
                </div>
                {% endfor %}
            </div>
            {% endif %}

            {% if padroes_zona %}
            <div class="cartao">
                <div class="bloco-topo">
                    <div class="bloco-titulo">🎯 Comportamento por zona da tabela</div>
                </div>
                <p class="estilo-aviso">
                    Como o {{ nome_time }} se sai dependendo de onde estava na tabela ANTES de cada jogo (G4/meio/
                    Z4/rebaixamento) - "depois de vitória" vs "depois de derrota" mostra se ele tende a embalar ou
                    afundar dentro da mesma zona. Zonas simplificadas (não distingue Libertadores/Sul-Americana
                    ainda). <b>Já usado como ajuste na fórmula de recomendação (resultado final e escanteio de
                    time), além de aparecer aqui pra consulta.</b>
                </p>
                {% for zona_key, z in padroes_zona.items() %}
                <div style="margin-top: 16px;">
                    <div class="bloco-titulo" style="font-size: 0.92rem; margin-bottom: 6px;">{{ z.titulo }}</div>

                    {% if z.resultado %}
                    <div class="zona-momento-grid">
                        {% if z.resultado.geral %}
                        <div>
                            <div class="estilo-papel-titulo">Geral nessa zona</div>
                            <div class="estilo-valor neutro">{{ z.resultado.geral.valor }}% vitória</div>
                            <div class="estilo-detalhe">{{ z.resultado.geral.jogos_amostra }} jogo(s)</div>
                        </div>
                        {% endif %}
                        {% if z.resultado.apos_vitoria %}
                        <div>
                            <div class="estilo-papel-titulo">Depois de vitória</div>
                            <div class="estilo-valor abaixo">{{ z.resultado.apos_vitoria.valor }}% vitória</div>
                            <div class="estilo-detalhe">{{ z.resultado.apos_vitoria.jogos_amostra }} jogo(s)</div>
                        </div>
                        {% endif %}
                        {% if z.resultado.apos_derrota %}
                        <div>
                            <div class="estilo-papel-titulo">Depois de derrota</div>
                            <div class="estilo-valor acima">{{ z.resultado.apos_derrota.valor }}% vitória</div>
                            <div class="estilo-detalhe">{{ z.resultado.apos_derrota.jogos_amostra }} jogo(s)</div>
                        </div>
                        {% endif %}
                    </div>
                    {% endif %}

                    {% if z.outros %}
                    <div class="zona-outros-mercados">
                        {% for o in z.outros %}
                        <span class="zona-outros-item">{{ o.titulo }}: <b>{{ o.valor }}/jogo</b> ({{ o.jogos_amostra }})</span>
                        {% endfor %}
                    </div>
                    {% endif %}
                </div>
                {% endfor %}
            </div>
            {% endif %}

            {% if correlacoes_time %}
            <div class="cartao-correlacao" style="margin-top: 16px;">
                <div class="bloco-titulo" style="margin-bottom: 8px;">🔗 Correlação entre estatísticas ({{ nome_time }})</div>
                <p class="correlacao-aviso">
                    A mesma relação da liga inteira (ver "Estatísticas de Times"), mas só com os jogos do
                    {{ nome_time }} - revela se esse time tem essa relação mais forte, mais fraca, ou diferente
                    do padrão geral. <b>Informativo por enquanto, não afeta nenhuma recomendação.</b>
                </p>
                <div class="correlacao-grid" style="grid-template-columns: 1fr;">
                    {% for c in correlacoes_time %}
                    <div class="correlacao-item">
                        <div class="correlacao-titulo">{{ c.titulo_a }} → {{ c.titulo_b }}</div>
                        <div class="correlacao-linha">
                            Acima da média ({{ c.media_a }}/jogo): {{ c.titulo_b|lower }} médio é
                            <b class="{{ 'acima' if c.sobe_junto else 'abaixo' }}">{{ c.valor_acima }}/jogo</b>
                            · abaixo: <b class="{{ 'abaixo' if c.sobe_junto else 'acima' }}">{{ c.valor_abaixo }}/jogo</b>
                        </div>
                        <div class="correlacao-detalhe">{{ c.jogos_total }} jogo(s) analisados</div>
                    </div>
                    {% endfor %}
                </div>
            </div>
            {% endif %}

            {% if correlacoes_categoria_time %}
            <div class="cartao-correlacao" style="margin-top: 16px;">
                <div class="bloco-titulo" style="margin-bottom: 8px;">🧩 Correlação entre categorias ({{ nome_time }})</div>
                <p class="correlacao-aviso">
                    Isola a direção: só o ATAQUE do {{ nome_time }} contra a DEFESA do adversário, no mesmo jogo -
                    diferente da versão geral, que soma os dois lados juntos. <b>Informativo por enquanto, não
                    afeta nenhuma recomendação.</b>
                </p>
                <div class="correlacao-grid" style="grid-template-columns: 1fr;">
                    {% for c in correlacoes_categoria_time %}
                    <div class="correlacao-item">
                        <div class="correlacao-titulo">{{ c.titulo_a }} do {{ nome_time }} → {{ c.titulo_b }} do adversário</div>
                        <div class="correlacao-linha">
                            Acima da média ({{ c.media_a }}/jogo): {{ c.titulo_b|lower }} médio do adversário é
                            <b class="{{ 'acima' if c.sobe_junto else 'abaixo' }}">{{ c.valor_acima }}/jogo</b>
                            · abaixo: <b class="{{ 'abaixo' if c.sobe_junto else 'acima' }}">{{ c.valor_abaixo }}/jogo</b>
                        </div>
                        <div class="correlacao-detalhe">{{ c.jogos_total }} jogo(s) analisados</div>
                    </div>
                    {% endfor %}
                </div>
            </div>
            {% endif %}


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
        </div>

        <div class="coluna-lateral">
            {% if vizinhos_tabela %}
            <div class="titulo-coluna">Posição no Brasileirão</div>
            <table class="tabela-vizinhos">
                {% for l in vizinhos_tabela %}
                <tr class="linha-vizinho{{ ' ativa' if l.team_id == api_football_team_id else '' }}">
                    <td class="pos-vizinho">{{ l.posicao }}</td>
                    <td>
                        <img src="/escudo/{{ l.team_id }}.png" class="escudo-vizinho" onerror="this.style.display='none'">
                        {{ l.nome }}
                    </td>
                </tr>
                {% endfor %}
            </table>
            {% endif %}

            <div class="titulo-coluna" id="titulo-ultimos-jogos">Últimos Jogos</div>

            <div class="confronto-busca-wrap">
                <input type="text" class="busca" id="busca-confronto" list="lista-adversarios-confronto"
                       placeholder="Buscar confronto direto (ex: Palmeiras)..."
                       oninput="buscarConfrontoDireto()" autocomplete="off">
                <datalist id="lista-adversarios-confronto">
                    {% for t in times_serie_a %}
                    <option value="{{ t.nome }}">
                    {% endfor %}
                </datalist>
                <span class="confronto-limpar" id="confronto-limpar" onclick="limparConfrontoDireto()" style="display:none;">✕ limpar</span>
            </div>

            <div id="lista-ultimos-jogos">
            {% if ultimos_jogos %}
                {% for jogo in ultimos_jogos %}
                <div class="jogo-card" id="card-jogo-{{ jogo.jogo_id }}" onclick="toggleJogo({{ jogo.jogo_id }})">
                    <div class="jogo-placar-linha">
                        {% if jogo.mandante %}
                            <div class="jogo-time">
                                {% if escudo_url %}<img src="{{ escudo_url }}" class="escudo-jogo">{% endif %}
                                {{ nome_time }}
                            </div>
                            <div class="jogo-placar">{{ jogo.placar_nosso }} x {{ jogo.placar_adversario }}</div>
                            <div class="jogo-time jogo-time-direita">
                                {{ jogo.adversario }}
                                {% if jogo.escudo_adversario %}<img src="{{ jogo.escudo_adversario }}" class="escudo-jogo">{% endif %}
                            </div>
                        {% else %}
                            <div class="jogo-time">
                                {% if jogo.escudo_adversario %}<img src="{{ jogo.escudo_adversario }}" class="escudo-jogo">{% endif %}
                                {{ jogo.adversario }}
                            </div>
                            <div class="jogo-placar">{{ jogo.placar_adversario }} x {{ jogo.placar_nosso }}</div>
                            <div class="jogo-time jogo-time-direita">
                                {{ nome_time }}
                                {% if escudo_url %}<img src="{{ escudo_url }}" class="escudo-jogo">{% endif %}
                            </div>
                        {% endif %}
                        <span class="jogo-seta">▾</span>
                    </div>
                    <div class="jogo-detalhe">
                        <table class="tabela-detalhe-jogo">
                            <thead>
                                <tr><th>Estatística</th><th>{{ nome_time }}</th><th>{{ jogo.adversario }}</th></tr>
                            </thead>
                            <tbody>
                                <tr><td>Posse de bola</td><td>{{ jogo.estatisticas.nosso.posse if jogo.estatisticas.nosso.posse is not none else "-" }}%</td><td>{{ jogo.estatisticas.adversario.posse if jogo.estatisticas.adversario.posse is not none else "-" }}%</td></tr>
                                <tr><td>Escanteios</td><td>{{ jogo.estatisticas.nosso.escanteios if jogo.estatisticas.nosso.escanteios is not none else "-" }}</td><td>{{ jogo.estatisticas.adversario.escanteios if jogo.estatisticas.adversario.escanteios is not none else "-" }}</td></tr>
                                <tr><td>Chutes (total)</td><td>{{ jogo.estatisticas.nosso.chutes if jogo.estatisticas.nosso.chutes is not none else "-" }}</td><td>{{ jogo.estatisticas.adversario.chutes if jogo.estatisticas.adversario.chutes is not none else "-" }}</td></tr>
                                <tr><td>Chutes no gol</td><td>{{ jogo.estatisticas.nosso.chutes_no_gol if jogo.estatisticas.nosso.chutes_no_gol is not none else "-" }}</td><td>{{ jogo.estatisticas.adversario.chutes_no_gol if jogo.estatisticas.adversario.chutes_no_gol is not none else "-" }}</td></tr>
                                <tr><td>Faltas</td><td>{{ jogo.estatisticas.nosso.faltas if jogo.estatisticas.nosso.faltas is not none else "-" }}</td><td>{{ jogo.estatisticas.adversario.faltas if jogo.estatisticas.adversario.faltas is not none else "-" }}</td></tr>
                                <tr><td>Desarmes</td><td>{{ jogo.estatisticas.nosso.desarmes if jogo.estatisticas.nosso.desarmes is not none else "-" }}</td><td>{{ jogo.estatisticas.adversario.desarmes if jogo.estatisticas.adversario.desarmes is not none else "-" }}</td></tr>
                                <tr><td>Cartões</td><td>{{ jogo.estatisticas.nosso.cartoes }}</td><td>{{ jogo.estatisticas.adversario.cartoes }}</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>
                {% endfor %}
            {% else %}
                <div class="vazio">Nenhum jogo concluído ainda pra esse time.</div>
            {% endif %}
            </div>
            <button class="btn-ver-mais-confronto" id="confronto-ver-mais" onclick="mostrarTodosConfronto()" style="display:none;">Ver todos os confrontos ↓</button>
        </div>
    </div>

    <script>
        // NOVO (busca de confronto direto)
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

            document.getElementById('confronto-limpar').style.display = '';
            document.getElementById('titulo-ultimos-jogos').textContent = `Confrontos: ${NOME_TIME_ATUAL} x ${encontrado.nome}`;
            document.getElementById('lista-ultimos-jogos').innerHTML = '<div class="vazio">Buscando confrontos...</div>';
            document.getElementById('confronto-ver-mais').style.display = 'none';

            fetch(`/api/confronto/${TIME_ID_ATUAL}/${encontrado.id}`)
                .then(r => r.json())
                .then(jogos => {
                    confrontoJogosCompletos = jogos;
                    renderizarConfrontoDireto(jogos.slice(0, 5));
                    if (jogos.length > 5) {
                        document.getElementById('confronto-ver-mais').style.display = '';
                    }
                });
        }

        function mostrarTodosConfronto() {
            renderizarConfrontoDireto(confrontoJogosCompletos);
            document.getElementById('confronto-ver-mais').style.display = 'none';
        }

        function limparConfrontoDireto() {
            document.getElementById('busca-confronto').value = '';
            document.getElementById('confronto-limpar').style.display = 'none';
            document.getElementById('confronto-ver-mais').style.display = 'none';
            document.getElementById('titulo-ultimos-jogos').textContent = 'Últimos Jogos';
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
            const val = (v, suf) => (v === null || v === undefined) ? '-' : (v + (suf || ''));
            cont.innerHTML = jogos.map(jogo => {
                const escudoNosso = ESCUDO_URL_ATUAL ? `<img src="${ESCUDO_URL_ATUAL}" class="escudo-jogo">` : '';
                const escudoAdv = jogo.escudo_adversario ? `<img src="${jogo.escudo_adversario}" class="escudo-jogo">` : '';
                const nomeAdv = escapar(jogo.adversario);
                const linhaPlacar = jogo.mandante
                    ? `<div class="jogo-time">${escudoNosso}${escapar(NOME_TIME_ATUAL)}</div>
                       <div class="jogo-placar">${jogo.placar_nosso} x ${jogo.placar_adversario}</div>
                       <div class="jogo-time jogo-time-direita">${nomeAdv}${escudoAdv}</div>`
                    : `<div class="jogo-time">${escudoAdv}${nomeAdv}</div>
                       <div class="jogo-placar">${jogo.placar_adversario} x ${jogo.placar_nosso}</div>
                       <div class="jogo-time jogo-time-direita">${escapar(NOME_TIME_ATUAL)}${escudoNosso}</div>`;
                const en = (jogo.estatisticas && jogo.estatisticas.nosso) || {};
                const ea = (jogo.estatisticas && jogo.estatisticas.adversario) || {};
                return `<div class="jogo-card" id="card-confronto-${jogo.jogo_id}" onclick="toggleJogoConfronto(${jogo.jogo_id})">
                    <div class="jogo-placar-linha">
                        ${linhaPlacar}
                        <span class="jogo-seta">▾</span>
                    </div>
                    <div class="confronto-data">${jogo.data_jogo}</div>
                    <div class="jogo-detalhe">
                        <table class="tabela-detalhe-jogo">
                            <thead><tr><th>Estatística</th><th>${escapar(NOME_TIME_ATUAL)}</th><th>${nomeAdv}</th></tr></thead>
                            <tbody>
                                <tr><td>Posse de bola</td><td>${val(en.posse, '%')}</td><td>${val(ea.posse, '%')}</td></tr>
                                <tr><td>Escanteios</td><td>${val(en.escanteios)}</td><td>${val(ea.escanteios)}</td></tr>
                                <tr><td>Chutes (total)</td><td>${val(en.chutes)}</td><td>${val(ea.chutes)}</td></tr>
                                <tr><td>Chutes no gol</td><td>${val(en.chutes_no_gol)}</td><td>${val(ea.chutes_no_gol)}</td></tr>
                                <tr><td>Faltas</td><td>${val(en.faltas)}</td><td>${val(ea.faltas)}</td></tr>
                                <tr><td>Desarmes</td><td>${val(en.desarmes)}</td><td>${val(ea.desarmes)}</td></tr>
                                <tr><td>Cartões</td><td>${en.cartoes != null ? en.cartoes : 0}</td><td>${ea.cartoes != null ? ea.cartoes : 0}</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>`;
            }).join('');
        }

        function toggleJogoConfronto(jogoId) {
            document.getElementById('card-confronto-' + jogoId).classList.toggle('aberto');
        }

        function toggleJogo(jogoId) {
            document.getElementById('card-jogo-' + jogoId).classList.toggle('aberto');
        }

        function filtrarTimes() {
            const termo = document.getElementById('busca-time').value.toLowerCase();
            let visiveis = 0;
            document.querySelectorAll('.item-time').forEach(function(card) {
                const nome = card.querySelector('.clube-nome').textContent.toLowerCase();
                const bate = nome.includes(termo);
                card.dataset.escondidoBusca = bate ? '0' : '1';
                if (bate) visiveis++;
            });
            document.getElementById('nenhum-resultado-time').style.display = (termo && visiveis === 0) ? '' : 'none';
            paginaAtual['lista-times'] = 0;
            renderizarPagina('lista-times');
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
                const paginaDoItem = Math.floor(visivelIndice / TAMANHO_PAGINA);
                item.style.display = (paginaDoItem === pagina) ? '' : 'none';
                visivelIndice++;
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

        renderizarPagina('lista-times');
    </script>
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
            "titulo": titulo, "tipo": tipo, "time_nome": time_nome,
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
        SELECT j.id, j.data_jogo, j.adversario, j.mandante,
               j.placar_corinthians, j.placar_adversario, t.nome
        FROM jogador_estatisticas_jogo jeg
        JOIN jogos j ON j.id = jeg.jogo_id
        JOIN times t ON t.id = j.nosso_time_id
        WHERE jeg.jogador_id = %s
          AND j.placar_corinthians IS NOT NULL AND j.placar_adversario IS NOT NULL
        ORDER BY j.data_jogo DESC
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
        nav_html=barra_navegacao("jogadores", round(banca_atual, 2)),
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
        nav_html=barra_navegacao("jogadores", round(banca_atual, 2)),
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
        nav_html=barra_navegacao("times", round(banca_atual, 2)),
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
        nav_html=barra_navegacao("times", round(banca_atual, 2)),
    )


@app.route("/")
def index():
    odd_min = request.args.get("odd_min", "1.5")
    odd_max = request.args.get("odd_max", "5.0")
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
    eh_clique_real = validar_e_consumir_token_busca(request.args.get("token_busca"))

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
            combinacoes = montar_combinacoes(recomendacoes, float(odd_min), float(odd_max))

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
                if disparou_agora:
                    motivo = ("🔄 A atualização de odds foi disparada agora mesmo e está rodando em segundo "
                               "plano. A página se atualiza sozinha quando terminar.")
                elif atualizacao_rodando:
                    motivo = ("⏳ Já tem uma atualização rodando agora - só uma roda por vez, pra não "
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
    LIMITE_ODDS_POR_JOGO = 100
    jogos_agrupados = {}
    for c in individuais:
        if not c.get("jogos"):
            continue
        j = c["jogos"][0]
        dupla = tuple(sorted([j["nosso_time"], j["adversario"]]))
        chave = (j["data_jogo"], dupla)
        if chave not in jogos_agrupados:
            jogos_agrupados[chave] = {
                "data_jogo": j["data_jogo"],
                "datahora_jogo": j.get("datahora_jogo"),
                "rotulo": f"{dupla[0]} x {dupla[1]}",
                "odds": [],
            }
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
        PAGINA, odd_min=odd_min, odd_max=odd_max, buscou=buscou,
        individuais=individuais, multiplas=multiplas, motivo=motivo, banca_atual=round(banca_atual, 2),
        jogos_disponiveis=jogos_disponiveis,
        ultima_atualizacao_odds=ultima_atualizacao_odds, token_busca=token_busca,
        atualizacao_rodando=atualizacao_rodando,
        progresso_decorridos=progresso_decorridos,
        progresso_estimativa=progresso_estimativa,
        nav_html=barra_navegacao("index", round(banca_atual, 2)),
    )


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta)
