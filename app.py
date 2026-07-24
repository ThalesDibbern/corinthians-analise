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
from itertools import combinations

import psycopg2
from flask import Flask, render_template_string, request, redirect

DATABASE_URL = os.environ["DATABASE_URL"]

app = Flask(__name__)


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
    </style>
</head>
<body>
    <h1>⚫⚪ Análise de Apostas</h1>
    <p class="subtitulo">Recomendações de múltiplas do Corinthians baseadas em padrões históricos</p>
    <p>
        <a href="/historico" class="link-historico">📊 Ver histórico de acertos e erros</a>
        &nbsp;·&nbsp;
        <a href="/minhas-apostas" class="link-historico">💰 Minhas apostas (ROI)</a>
        &nbsp;·&nbsp;
        <a href="/jogadores" class="link-historico">📈 Estatísticas de jogadores</a>
    </p>

    <div class="aviso">
        ⚠️ Base de dados histórica ainda cobre 2022-2024. As recomendações abaixo
        servem para validar o sistema - use com cautela até os dados serem
        atualizados para a temporada atual.
    </div>

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
        </form>
    </div>

    {% if buscou %}
        {% if combinacoes %}
            {% for c in combinacoes %}
            <div class="cartao">
                <div class="cartao-topo">
                    <span class="jogo">{{ c.data_jogo }} · Corinthians x {{ c.adversario }}</span>
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
            {% endfor %}
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


def buscar_recomendacoes(cur):
    cur.execute(
        """
        SELECT r.jogo_id, r.jogador_id, r.descricao, r.casa_aposta,
               r.odd_oferecida, r.probabilidade_historica, j.adversario, j.data_jogo, r.tipo_padrao
        FROM recomendacoes r
        JOIN jogos j ON j.id = r.jogo_id
        """
    )
    return cur.fetchall()


def montar_combinacoes(recomendacoes, odd_min, odd_max):
    grupos = {}
    for rec in recomendacoes:
        (jogo_id, jogador_id, descricao, casa, odd, prob, adversario, data_jogo, tipo_padrao) = rec

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
            "adversario": adversario,
            "data_jogo": data_jogo,
        })

    resultado = []
    for (jogo_id, casa), pernas in grupos.items():
        for tamanho in (1, 2, 3, 4, 5):
            if len(pernas) < tamanho:
                continue
            for combo in combinations(pernas, tamanho):
                # NOVO: antes só verificava se o mesmo JOGADOR aparecia duas
                # vezes na múltipla. Isso não pegava o caso de duas pernas do
                # mesmo mercado de TIME (ex: duas linhas diferentes de
                # escanteio do mesmo jogo) - que são fortemente
                # correlacionadas (medem a mesma coisa em pontos de corte
                # diferentes), violando a suposição de independência usada
                # no cálculo de probabilidade/valor esperado combinado, e
                # inflando o VE de forma artificial. Agora a checagem é pelo
                # par (tipo_padrao, jogador_id), que cobre tanto jogador
                # repetido quanto mercado de time repetido.
                chaves_mercado = [(p["tipo_padrao"], p["jogador_id"]) for p in combo]
                if len(chaves_mercado) != len(set(chaves_mercado)):
                    continue

                odd_combinada = 1.0
                prob_combinada = 1.0
                for p in combo:
                    odd_combinada *= p["odd"]
                    prob_combinada *= p["probabilidade"]

                if not (odd_min <= odd_combinada <= odd_max):
                    continue

                valor_esperado = round((prob_combinada * odd_combinada) - 1, 3)
                resultado.append({
                    "jogo_id": jogo_id,
                    "casa_aposta": casa,
                    "descricao": " + ".join(p["descricao"] for p in combo),
                    "odd_combinada": round(odd_combinada, 2),
                    "probabilidade_combinada": round(prob_combinada * 100, 2),
                    "valor_esperado": valor_esperado,
                    "adversario": combo[0]["adversario"],
                    "data_jogo": combo[0]["data_jogo"],
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
    </style>
</head>
<body>
    <a href="/" class="link-voltar">← Voltar</a>
    <h1>📊 Histórico de Acertos e Erros</h1>
    <p class="subtitulo">Recomendações já avaliadas contra o resultado real dos jogos</p>

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

    {% if itens %}
        {% for i in itens %}
        <div class="cartao">
            <div class="cartao-topo">
                <span class="jogo">{{ i.data_jogo }} · Corinthians x {{ i.adversario }}</span>
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
        {% endfor %}
    {% else %}
        <div class="vazio">Ainda não há recomendações avaliadas - isso acontece automaticamente
        depois que um jogo termina e o script de arquivamento processa o resultado.</div>
    {% endif %}

    {% for c in multiplas_destaque %}
        <div class="cartao">
            <div class="cartao-topo">
                <span class="jogo">{{ c.data_jogo }} · Corinthians x {{ c.adversario }}</span>
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


def buscar_historico(cur, limite=100):
    cur.execute(
        """
        SELECT h.data_jogo, j.adversario, h.descricao, h.casa_aposta,
               h.odd_oferecida, h.probabilidade_historica, h.valor_esperado, h.resultado
        FROM historico_recomendacoes h
        JOIN jogos j ON j.id = h.jogo_id
        ORDER BY h.data_jogo DESC, h.id DESC
        LIMIT %s
        """,
        (limite,),
    )
    colunas = ["data_jogo", "adversario", "descricao", "casa_aposta",
               "odd_oferecida", "probabilidade_historica", "valor_esperado", "resultado"]
    return [dict(zip(colunas, row)) for row in cur.fetchall()]


def buscar_resumo_historico(cur):
    cur.execute("SELECT resultado, COUNT(*) FROM historico_recomendacoes GROUP BY resultado")
    contagem = dict(cur.fetchall())
    acertou = contagem.get("acertou", 0)
    errou = contagem.get("errou", 0)
    pendente = contagem.get("pendente", 0)
    total_avaliado = acertou + errou
    taxa = round(100 * acertou / total_avaliado, 1) if total_avaliado else 0
    return {"acertou": acertou, "errou": errou, "pendente": pendente, "taxa": taxa}


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
               h.tipo_padrao, h.resultado
        FROM historico_recomendacoes h
        JOIN jogos j ON j.id = h.jogo_id
        """
    )
    return cur.fetchall()


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
         tipo_padrao, resultado_perna) = rec

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
        resumo = buscar_resumo_historico(cur)
        multiplas_destaque = buscar_multiplas_destaque(cur)
        cur.close()
    finally:
        conn.close()

    return render_template_string(
        PAGINA_HISTORICO, itens=itens, resumo=resumo,
        multiplas_destaque=multiplas_destaque,
        piso=PISO_PROBABILIDADE_MULTIPLAS_DESTAQUE,
    )


@app.route("/salvar-aposta", methods=["POST"])
def salvar_aposta():
    """NOVO: salva uma aposta (individual ou múltipla) que o usuário decidiu
    apostar de verdade, com o valor apostado - alimenta a página de ROI
    (/minhas-apostas). Sem piso de probabilidade nenhum aqui - o usuário
    pode salvar qualquer odd/múltipla mostrada em qualquer parte do site."""
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
        cur.execute(
            """INSERT INTO apostas_salvas
               (descricao, casa_aposta, odd_combinada, probabilidade_combinada,
                valor_apostado, pernas)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (descricao, casa_aposta, odd_combinada, probabilidade_combinada,
             valor_apostado, pernas_json),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()

    return redirect(voltar)


@app.route("/cancelar-aposta", methods=["POST"])
def cancelar_aposta():
    """NOVO: cancela (apaga) uma aposta salva, só se ela ainda estiver
    'pendente' - não deixa cancelar uma aposta que já foi resolvida
    (acertou/errou), já que isso já aconteceu de verdade."""
    aposta_id = request.form["aposta_id"]

    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM apostas_salvas WHERE id = %s AND resultado = 'pendente'", (aposta_id,))
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
    </style>
</head>
<body>
    <a href="/" class="link-voltar">← Voltar</a>
    <h1>💰 Minhas Apostas</h1>
    <p class="subtitulo">Só o que você salvou com valor apostado - não inclui recomendações não salvas</p>

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


def resolver_apostas_pendentes(cur):
    """NOVO: pra cada aposta salva ainda 'pendente', confere se TODAS as
    pernas dela já têm resultado em historico_recomendacoes - só resolve
    (acertou/errou) quando não sobrar nenhuma perna pendente, já que uma
    múltipla só acerta se todas as pernas acertarem."""
    cur.execute("SELECT id, pernas, odd_combinada, valor_apostado FROM apostas_salvas WHERE resultado = 'pendente'")
    pendentes = cur.fetchall()

    for aposta_id, pernas_json, odd_combinada, valor_apostado in pendentes:
        pernas = pernas_json if isinstance(pernas_json, list) else json.loads(pernas_json)

        resultados_pernas = []
        for perna in pernas:
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


def buscar_apostas_salvas(cur):
    cur.execute(
        """
        SELECT id, descricao, casa_aposta, odd_combinada, valor_apostado, resultado, retorno, criado_em
        FROM apostas_salvas
        ORDER BY criado_em DESC
        """
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


@app.route("/minhas-apostas")
def minhas_apostas():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        resolver_apostas_pendentes(cur)
        conn.commit()

        apostas = buscar_apostas_salvas(cur)
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

    # pontos do gráfico: retorno acumulado, em ordem cronológica (mais antiga primeiro)
    resolvidas_ordem_cronologica = sorted(resolvidas, key=lambda a: a["criado_em"])
    pontos_grafico = []
    acumulado = 0
    for a in resolvidas_ordem_cronologica:
        acumulado += float(a["retorno"])
        pontos_grafico.append((a["criado_em"], round(acumulado, 2)))

    svg_grafico = montar_svg_grafico(pontos_grafico) if len(pontos_grafico) > 1 else ""

    return render_template_string(
        PAGINA_ROI, resumo=resumo, apostas=apostas,
        pontos_grafico=pontos_grafico, svg_grafico=svg_grafico,
    )


def buscar_totais_apostados(cur):
    """NOVO: soma o valor já apostado por (descricao, casa_aposta), pra
    mostrar um aviso tipo "R$ X já apostado nessa odd" - não impede apostar
    de novo na mesma odd, é só informativo."""
    cur.execute("SELECT descricao, casa_aposta, SUM(valor_apostado) FROM apostas_salvas GROUP BY descricao, casa_aposta")
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
    <title>Estatísticas de Jogadores - Análise de Apostas</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
            background: #0d1117; color: #e6edf3; max-width: 900px;
            margin: 0 auto; padding: 32px 20px 80px;
        }
        h1 { font-size: 1.5rem; margin: 0 0 4px; }
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
    </style>
</head>
<body>
    <a href="/" class="link-voltar">← Voltar</a>
    <h1>📈 Estatísticas de Jogadores</h1>
    <p class="subtitulo">Frequência histórica de cada jogador, direto dos padrões calculados - sem depender de odd disponível na casa de apostas.</p>

    {% if ultima_escalacao %}
    <div class="cartao">
        <div class="nome-jogador">🟢 Última escalação titular</div>
        <div class="bloco-titulo">{{ ultima_escalacao.data_jogo }} · Corinthians x {{ ultima_escalacao.adversario }}</div>
        <div class="linhas-grid">
            {% for nome in ultima_escalacao.titulares %}
            <div class="linha-item">{{ nome }}</div>
            {% endfor %}
        </div>
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
        <div class="vazio">Ainda não há padrões calculados pra nenhum jogador (o motor_padroes.py
        precisa de pelo menos alguns jogos analisados por jogador).</div>
    {% endif %}
    </div>

    <script>
        function filtrar() {
            const termo = document.getElementById('busca').value.toLowerCase();
            document.querySelectorAll('.jogador-card').forEach(function(card) {
                const nome = card.querySelector('.nome-jogador').textContent.toLowerCase();
                card.style.display = nome.includes(termo) ? '' : 'none';
            });
        }
    </script>
</body>
</html>
"""

NOMES_TIPO_LINHA = {
    "falta_cometida": "Faltas cometidas",
    "desarme": "Desarmes",
    "chute_no_gol": "Chutes no gol",
}


def buscar_estatisticas_jogadores(cur):
    """NOVO: monta a frequência histórica de cada jogador (cartão, faltas,
    desarmes, chutes no gol, impedimento), lendo direto das tabelas de
    padrão já calculadas pelo motor_padroes.py - não depende de nenhuma
    odd estar disponível na casa de apostas."""
    jogadores_dict = {}

    def garantir(jogador_id, nome):
        jogadores_dict.setdefault(jogador_id, {
            "nome": nome, "cartao": None, "linhas": {}, "impedimento": None,
        })

    cur.execute(
        """
        SELECT j.id, j.nome, p.jogos_analisados, p.frequencia
        FROM padroes_jogador_cartao p
        JOIN jogadores j ON j.id = p.jogador_id
        """
    )
    for jogador_id, nome, jogos_analisados, frequencia in cur.fetchall():
        garantir(jogador_id, nome)
        jogadores_dict[jogador_id]["cartao"] = {
            "jogos_analisados": jogos_analisados, "frequencia": float(frequencia),
        }

    cur.execute(
        """
        SELECT j.id, j.nome, p.tipo, p.linha, p.jogos_analisados, p.frequencia
        FROM padroes_jogador_linha p
        JOIN jogadores j ON j.id = p.jogador_id
        ORDER BY p.linha
        """
    )
    for jogador_id, nome, tipo, linha, jogos_analisados, frequencia in cur.fetchall():
        garantir(jogador_id, nome)
        jogadores_dict[jogador_id]["linhas"].setdefault(tipo, []).append({
            "linha": float(linha), "frequencia": float(frequencia),
        })

    cur.execute(
        """
        SELECT j.id, j.nome, p.jogos_analisados, p.frequencia
        FROM padroes_jogador_frequencia p
        JOIN jogadores j ON j.id = p.jogador_id
        WHERE p.tipo = 'impedimento'
        """
    )
    for jogador_id, nome, jogos_analisados, frequencia in cur.fetchall():
        garantir(jogador_id, nome)
        jogadores_dict[jogador_id]["impedimento"] = {
            "jogos_analisados": jogos_analisados, "frequencia": float(frequencia),
        }

    lista = []
    for dados in jogadores_dict.values():
        blocos_linha = [
            {"titulo": NOMES_TIPO_LINHA.get(tipo, tipo), "itens": itens}
            for tipo, itens in dados["linhas"].items()
        ]
        lista.append({
            "nome": dados["nome"],
            "cartao": dados["cartao"],
            "impedimento": dados["impedimento"],
            "blocos_linha": blocos_linha,
        })

    lista.sort(key=lambda p: p["nome"])
    return lista


def buscar_ultima_escalacao_titular(cur):
    """NOVO: busca os titulares do último jogo já concluído do Corinthians,
    cruzando escalacoes com jogador_estatisticas_jogo.lado (pra saber se
    aquele titular jogava pelo Corinthians ou pelo adversário naquele
    jogo específico) e jogos.mandante (pra saber qual lado é o do
    Corinthians nesse jogo)."""
    cur.execute(
        """
        SELECT id, data_jogo, adversario FROM jogos
        WHERE (datahora_jogo IS NOT NULL AND datahora_jogo < NOW())
           OR (datahora_jogo IS NULL AND data_jogo < CURRENT_DATE)
        ORDER BY COALESCE(datahora_jogo, data_jogo::timestamp) DESC
        LIMIT 1
        """
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


@app.route("/jogadores")
def jogadores():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        lista = buscar_estatisticas_jogadores(cur)
        ultima_escalacao = buscar_ultima_escalacao_titular(cur)
        cur.close()
    finally:
        conn.close()

    return render_template_string(PAGINA_JOGADORES, jogadores=lista, ultima_escalacao=ultima_escalacao)


@app.route("/")
def index():
    odd_min = request.args.get("odd_min", "1.5")
    odd_max = request.args.get("odd_max", "5.0")
    buscou = "odd_min" in request.args

    combinacoes = []
    motivo = ""
    if buscou:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            cur = conn.cursor()
            recomendacoes = buscar_recomendacoes(cur)
            combinacoes = montar_combinacoes(recomendacoes, float(odd_min), float(odd_max))
            aplicar_totais_apostados(combinacoes, buscar_totais_apostados(cur))
            if not combinacoes:
                motivo = descobrir_motivo(cur)
            cur.close()
        finally:
            conn.close()

    return render_template_string(
        PAGINA, odd_min=odd_min, odd_max=odd_max, buscou=buscou,
        combinacoes=combinacoes, motivo=motivo,
    )


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta)
