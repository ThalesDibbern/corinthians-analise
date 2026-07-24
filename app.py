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
from itertools import combinations

import psycopg2
from flask import Flask, render_template_string, request

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
    <p><a href="/historico" class="link-historico">📊 Ver histórico de acertos e erros</a></p>

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
                    "casa_aposta": casa,
                    "descricao": " + ".join(p["descricao"] for p in combo),
                    "odd_combinada": round(odd_combinada, 2),
                    "probabilidade_combinada": round(prob_combinada * 100, 2),
                    "valor_esperado": valor_esperado,
                    "adversario": combo[0]["adversario"],
                    "data_jogo": combo[0]["data_jogo"],
                })

    resultado.sort(key=lambda c: c["valor_esperado"], reverse=True)
    return resultado[:10]


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


@app.route("/historico")
def historico():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        itens = buscar_historico(cur)
        resumo = buscar_resumo_historico(cur)
        cur.close()
    finally:
        conn.close()

    return render_template_string(PAGINA_HISTORICO, itens=itens, resumo=resumo)


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
