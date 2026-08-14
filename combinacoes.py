"""
Módulo compartilhado com a lógica de montar múltiplas a partir de
recomendações individuais - usado tanto pelo app.py (página principal, ao
vivo, quando alguém abre o site) quanto pelo motor_combinacoes.py (cron,
roda sozinho pra abastecer o histórico de Múltiplas em Destaque, mesmo
sem ninguém abrir o site).

EXTRAÍDO (antes vivia só dentro do app.py): existir em dois lugares
diferentes é exatamente o tipo de armadilha que já causou bug grande no
passado (funções duplicadas divergindo aos poucos) - então isso aqui é a
ÚNICA fonte de verdade da lógica de combinação. Qualquer correção futura
nessa lógica só precisa acontecer aqui, em um lugar só, e vale
automaticamente pros dois consumidores.

Não depende de Flask nem de nada específico de request web - só
psycopg2 (recebe um cursor já aberto) e a biblioteca padrão.
"""

import hashlib
import json
from itertools import combinations

MERCADOS_JOGO_INTEIRO = {"escanteio_total", "cartao_total"}

# NOVO (correção de bug real): mercados que pertencem a um JOGADOR
# específico - cartão, falta, chute, chute no gol, desarme, impedimento.
# Precisam do MESMO tipo de deduplicação que MERCADOS_JOGO_INTEIRO já
# tinha, mas por um motivo ligeiramente diferente: não é que a aposta seja
# "do jogo inteiro" - é que o JOGADOR é sempre a mesma pessoa,
# independente de qual dos dois times está sendo tratado como "nosso"
# naquela linha específica. Sem isso, quando os DOIS times de um jogo são
# rastreados, a estatística do mesmo jogador aparecia 2x na lista (uma por
# perspectiva) - e pior, as duas cópias podiam entrar JUNTAS na mesma
# múltipla, contando a mesma perna 2x na probabilidade combinada.
MERCADOS_JOGADOR = {"cartao", "falta_cometida", "chute_no_gol", "chute_total", "desarme", "impedimento"}

# largura mínima de uma faixa, em "unidades de linha" (como as linhas são
# sempre .5, isso equivale ao número mínimo de valores inteiros que a
# faixa precisa cobrir pra ser aceita). Ex: "mais de 9.5" + "menos de
# 11.5" cobre só {10, 11} -> largura 2, fica de fora com o padrão de 3.
LARGURA_MINIMA_FAIXA = 3.0

# limites de pool (por casa de apostas) pro tamanho máximo adaptativo de
# múltipla - calculados pra manter combinations() sempre abaixo de umas
# 2-3 milhões de combinações testadas por tamanho, mesmo no pior caso.
# C(50,5) ≈ 2.1 milhões, C(90,4) ≈ 2.55 milhões, C(200,3) ≈ 1.3 milhão -
# todos dentro da margem.
LIMITE_POOL_PARA_5_PERNAS = 50
LIMITE_POOL_PARA_4_PERNAS = 90

# teto de quantas múltiplas (2+ pernas) a página mostra no total, mesmo
# com pool pequeno - protege contra faixa de odd muito larga (ex: 10 a
# 1000) gerando dezenas de milhares de combinações válidas. Sempre mantém
# as de maior probabilidade histórica.
MAX_MULTIPLAS_RESULTADO = 300


def deduplicar_recomendacoes(recomendacoes, colunas_a_manter):
    """Remove as duplicatas causadas pela arquitetura multi-time: o mesmo
    jogo real entre DOIS times rastreados gera 2 linhas em `jogos` (uma
    por perspectiva), e isso faz alguns mercados aparecerem 2x nas
    recomendações - mesmo sendo literalmente a MESMA aposta real. Dois
    grupos afetados, cada um com sua identidade de "mesma aposta":

    - MERCADOS_JOGO_INTEIRO (escanteio total, cartão total): mesma aposta
      em qualquer perspectiva - identidade = (fixture_id_api, descricao,
      casa). A estimativa de probabilidade pode divergir entre as duas
      visões (vem do histórico de times diferentes) - fica com a maior.

    - MERCADOS_JOGADOR (cartão, falta, chute, chute no gol, desarme,
      impedimento): o jogador é a mesma pessoa em qualquer perspectiva -
      identidade = (fixture_id_api, tipo_padrao, jogador_id, linha,
      direção, casa). A probabilidade AQUI é sempre idêntica entre as
      duas cópias (vem só de padroes_jogador_*, que não depende de qual
      time é "nosso" na linha) - mas a deduplicação continua necessária
      pra não contar a mesma perna 2x numa múltipla.

    Mercados de TIME específico (escanteio_time, resultado_final) NÃO
    entram nessa deduplicação de propósito - são legitimamente diferentes
    por perspectiva (escanteio do Fluminense ≠ escanteio do Palmeiras,
    mesmo jogo real).

    `colunas_a_manter` é quantas colunas manter no resultado final (a
    última coluna da query sempre precisa ser fixture_id_api, usado só
    aqui pra deduplicar e descartado depois)."""
    melhores = {}
    resultado = []
    for rec in recomendacoes:
        tipo_padrao = rec[8]
        fixture_id_api, descricao, casa, prob = rec[-1], rec[2], rec[3], rec[5]
        jogador_id, linha, direcao = rec[1], rec[9], rec[10]

        if tipo_padrao in MERCADOS_JOGO_INTEIRO:
            chave = ("jogo_inteiro", fixture_id_api, descricao, casa)
        elif tipo_padrao in MERCADOS_JOGADOR:
            chave = ("jogador", fixture_id_api, tipo_padrao, jogador_id, linha, direcao, casa)
        else:
            resultado.append(rec[:colunas_a_manter])
            continue

        if chave not in melhores or prob > melhores[chave][5]:
            melhores[chave] = rec

    resultado.extend(rec[:colunas_a_manter] for rec in melhores.values())
    return resultado


# NOVO: nome antigo mantido como apelido, pra não quebrar nada que ainda
# importe pelo nome anterior - só chama a versão nova (escopo ampliado).
deduplicar_mercados_jogo_inteiro = deduplicar_recomendacoes


def buscar_recomendacoes(cur):
    """Busca as recomendações ATIVAS (jogos futuros, geradas pelo
    motor_recomendacoes.py) - usada tanto pra exibir odds individuais
    quanto como matéria-prima pra montar_combinacoes()."""
    cur.execute(
        """
        SELECT r.jogo_id, r.jogador_id, r.descricao, r.casa_aposta,
               r.odd_oferecida, r.probabilidade_historica, j.adversario, j.data_jogo,
               r.tipo_padrao, r.linha, r.direcao, t.nome, j.datahora_jogo, j.fixture_id_api
        FROM recomendacoes r
        JOIN jogos j ON j.id = r.jogo_id
        JOIN times t ON t.id = j.nosso_time_id
        """
    )
    return deduplicar_recomendacoes(cur.fetchall(), colunas_a_manter=14)


def identidade_jogo(p):
    """Identidade do jogo REAL por trás de uma perna - usa fixture_id_api
    (idêntico nas duas linhas quando os dois times de um confronto são
    rastreados, ver arquitetura multi-time), com fallback pro trio
    (nosso_time, adversario, data_jogo) quando fixture_id_api não está
    preenchido (jogos antigos)."""
    return p["fixture_id_api"] or (p["nosso_time"], p["adversario"], p["data_jogo"])


def combo_tem_conflito_de_time_mesma_data(combo):
    """Impede uma múltipla de combinar pernas de dois jogos DIFERENTES DE
    VERDADE (fixture_id_api diferente - não é só a mesma partida vista
    pelas 2 perspectivas) que envolvam o MESMO time na MESMA data - isso é
    fisicamente impossível (um time não pode disputar duas partidas reais
    no mesmo dia), então a combinação nunca poderia ter acontecido de
    verdade."""
    for i in range(len(combo)):
        for j in range(i + 1, len(combo)):
            a, b = combo[i], combo[j]
            if identidade_jogo(a) == identidade_jogo(b):
                continue  # mesmo jogo real (só perspectivas diferentes) - ok
            if a["data_jogo"] == b["data_jogo"] and (
                {a["nosso_time"], a["adversario"]} & {b["nosso_time"], b["adversario"]}
            ):
                return True
    return False


def montar_combinacoes(recomendacoes, odd_min, odd_max):
    """Monta odds individuais + múltiplas (1 a 5 pernas, tamanho máximo
    adaptativo conforme o pool) a partir das recomendações ativas, dentro
    da faixa de odd pedida. Retorna uma lista de dicts (1 perna = odd
    individual, 2+ pernas = múltipla), cada um já com `pernas_json`
    pronto pra gravar em apostas_salvas.pernas se o usuário salvar."""
    grupos = {}
    for rec in recomendacoes:
        (jogo_id, jogador_id, descricao, casa, odd, prob, adversario, data_jogo,
         tipo_padrao, linha, direcao, nosso_time, datahora_jogo, fixture_id_api) = rec

        # resultado final (1X2) só entra como candidato quando a faixa pedida
        # permite odds acima de 5.0 (mercado de alta variância)
        if tipo_padrao == "resultado_final" and odd_max <= 5.0:
            continue

        # agrupa só por CASA de apostas, não por (jogo, casa) - permite
        # combinar pernas de jogos diferentes, contanto que sejam da mesma
        # casa (não dá pra apostar uma múltipla de verdade misturando
        # casas diferentes). `jogo_id` vai dentro de cada perna.
        grupos.setdefault(casa, []).append({
            "jogo_id": jogo_id,
            "jogador_id": jogador_id,
            "tipo_padrao": tipo_padrao,
            "descricao": descricao,
            "odd": float(odd),
            "probabilidade": float(prob) / 100,
            "linha": float(linha) if linha is not None else None,
            "direcao": (direcao or "").strip().lower(),
            "adversario": adversario,
            "data_jogo": data_jogo,
            "datahora_jogo": datahora_jogo,
            "nosso_time": nosso_time,
            "fixture_id_api": fixture_id_api,
        })

    resultado = []
    for casa, pernas in grupos.items():

        # pra cada mercado (JOGO + tipo_padrao + jogador_id), se a casa
        # oferece mais de uma linha "mais" e/ou "menos" pro mesmo mercado,
        # a ÚNICA combinação de faixa permitida é a mais ampla possível -
        # o corte "mais" mais baixo disponível combinado com o corte
        # "menos" mais alto disponível.
        faixa_permitida_por_mercado = {}
        pernas_por_mercado = {}
        for p in pernas:
            chave_mercado = (p["jogo_id"], p["tipo_padrao"], p["jogador_id"])
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

        # quando o mesmo jogo oferece várias linhas do mesmo mercado, cada
        # mercado entra com só as MAX_LINHAS_POR_MERCADO melhores (por
        # valor esperado individual) - ou os 2 extremos da faixa, quando
        # existe faixa permitida. As odds INDIVIDUAIS (1 perna) continuam
        # mostrando todas as linhas normalmente - essa redução só vale
        # pra montar múltiplas.
        MAX_LINHAS_POR_MERCADO = 2
        pernas_para_combo = []
        for chave_mercado, legs in pernas_por_mercado.items():
            par_faixa = faixa_permitida_por_mercado.get(chave_mercado)
            if par_faixa:
                pernas_para_combo.extend(p for p in legs if id(p) in par_faixa)
            else:
                melhores = sorted(legs, key=lambda p: p["probabilidade"] * p["odd"] - 1, reverse=True)
                pernas_para_combo.extend(melhores[:MAX_LINHAS_POR_MERCADO])

        # tamanho máximo adaptativo - pool grande -> combinação menor.
        tamanho_pool = len(pernas_para_combo)
        if tamanho_pool <= LIMITE_POOL_PARA_5_PERNAS:
            tamanho_maximo_combo = 5
        elif tamanho_pool <= LIMITE_POOL_PARA_4_PERNAS:
            tamanho_maximo_combo = 4
        else:
            tamanho_maximo_combo = 3

        for tamanho in range(1, tamanho_maximo_combo + 1):
            pool = pernas if tamanho == 1 else pernas_para_combo
            if len(pool) < tamanho:
                continue
            for combo in combinations(pool, tamanho):
                # bloqueia toda repetição de mercado, EXCETO duas pernas
                # do mesmo mercado que formam uma FAIXA coerente (ex:
                # "Mais de 3.5" + "Menos de 7.5" = "entre 4 e 7
                # escanteios"). A probabilidade da faixa não pode ser
                # calculada multiplicando as duas probabilidades
                # individuais (não são eventos independentes, são dois
                # cortes da MESMA variável) - fórmula correta:
                # P(faixa) = P(mais do corte menor) + P(menos do corte
                # maior) - 1.
                contagem_mercado = {}
                for p in combo:
                    chave_mercado = (p["jogo_id"], p["tipo_padrao"], p["jogador_id"])
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

                    par_permitido = faixa_permitida_por_mercado.get(chave_mercado)
                    if par_permitido is None or {id(a), id(b)} != par_permitido:
                        valido = False
                        break

                    leg_mais = a if a["direcao"] == "mais" else b
                    leg_menos = a if a["direcao"] == "menos" else b

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

                # rejeita combinações fisicamente impossíveis (mesmo time
                # em 2 jogos DIFERENTES na mesma data).
                if combo_tem_conflito_de_time_mesma_data(combo):
                    continue

                odd_combinada = 1.0
                prob_combinada = 1.0
                faixa_ja_contabilizada = False
                for p in combo:
                    odd_combinada *= p["odd"]
                    chave_mercado = (p["jogo_id"], p["tipo_padrao"], p["jogador_id"])
                    if faixa_chave is not None and chave_mercado == faixa_chave:
                        if not faixa_ja_contabilizada:
                            prob_combinada *= faixa_probabilidade
                            faixa_ja_contabilizada = True
                    else:
                        prob_combinada *= p["probabilidade"]

                if not (odd_min <= odd_combinada <= odd_max):
                    continue

                valor_esperado = round((prob_combinada * odd_combinada) - 1, 3)

                if valor_esperado <= 0:
                    continue

                descricao_final = " + ".join(p["descricao"] for p in combo)
                if faixa_chave is not None:
                    descricao_final += " (faixa)"

                jogos_vistos_chaves = set()
                jogos_vistos = []
                for p in combo:
                    chave_jogo = p["fixture_id_api"] or (p["nosso_time"], p["adversario"], p["data_jogo"])
                    if chave_jogo not in jogos_vistos_chaves:
                        jogos_vistos_chaves.add(chave_jogo)
                        jogos_vistos.append({
                            "jogo_id": p["jogo_id"], "nosso_time": p["nosso_time"],
                            "adversario": p["adversario"], "data_jogo": p["data_jogo"],
                            "datahora_jogo": p["datahora_jogo"],
                        })

                resultado.append({
                    "casa_aposta": casa,
                    "descricao": descricao_final,
                    "odd_combinada": round(odd_combinada, 2),
                    "probabilidade_combinada": round(prob_combinada * 100, 2),
                    "valor_esperado": valor_esperado,
                    "jogos": jogos_vistos,
                    "adversario": combo[0]["adversario"],
                    "data_jogo": combo[0]["data_jogo"],
                    "nosso_time": combo[0]["nosso_time"],
                    "pernas": [
                        {
                            "jogo_id": p["jogo_id"],
                            "jogador_id": p["jogador_id"],
                            "tipo_padrao": p["tipo_padrao"],
                            "descricao": p["descricao"],
                            "linha": p["linha"],
                            "direcao": p["direcao"],
                        }
                        for p in combo
                    ],
                })
    for c in resultado:
        c["pernas_json"] = json.dumps(c["pernas"], ensure_ascii=False)

    # ordena por probabilidade histórica (maior primeiro)
    resultado.sort(key=lambda c: c["probabilidade_combinada"], reverse=True)

    # teto de múltiplas exibidas (protege contra faixa de odd muito larga
    # gerando resultados demais) - individuais continuam sem teto.
    individuais_final = [c for c in resultado if len(c["pernas"]) == 1]
    multiplas_final = [c for c in resultado if len(c["pernas"]) > 1][:MAX_MULTIPLAS_RESULTADO]
    return individuais_final + multiplas_final


def _assinatura_combo(casa_aposta, pernas):
    """Identidade de uma múltipla pro recurso de Múltiplas em Destaque -
    SÓ pelas pernas (jogo + tipo + jogador + descrição), NUNCA pela odd
    nem probabilidade. É isso que garante que a mesma combinação, mesmo
    aparecendo com odd diferente 4x no mesmo dia (odd oscila, a aposta em
    si continua sendo "a mesma"), sempre resolve pra a mesma linha no
    banco em vez de virar registro duplicado."""
    partes = sorted(
        f"{p['jogo_id']}|{p['tipo_padrao']}|{p['jogador_id']}|{p['descricao']}"
        for p in pernas
    )
    bruto = casa_aposta + "||" + "||".join(partes)
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()


def capturar_candidatas_multiplas(cur, combinacoes_geradas):
    """NOVO: grava/atualiza (upsert) cada múltipla (2+ pernas) gerada em
    `multiplas_candidatas` - chamada tanto pelo app.py (quando alguém
    clica "Gerar/Atualizar recomendações" de verdade) quanto pelo
    motor_combinacoes.py (cron, sem depender de clique nenhum, garante que
    o ranking do histórico é abastecido mesmo se ninguém abrir o site).

    Cada geração nova SOBRESCREVE a linha existente da mesma assinatura
    (ON CONFLICT), nunca duplica. Só sobrescreve enquanto
    `congelada = FALSE` - depois que o jogo mais próximo envolvido já
    começou (ver congelar_candidatas_vencidas), a linha para de mudar,
    garantindo que o que sobra é sempre "a última geração antes do
    apito"."""
    capturadas = 0
    for c in combinacoes_geradas:
        if len(c["pernas"]) < 2:
            continue  # só múltiplas (2+ pernas) entram nesse recurso

        jogos = c["jogos"]
        horarios = [j["datahora_jogo"] for j in jogos if j.get("datahora_jogo")]
        if not horarios:
            continue  # sem dado de horário, não dá pra decidir congelamento com segurança
        primeiro_apito = min(horarios)

        assinatura = _assinatura_combo(c["casa_aposta"], c["pernas"])
        cur.execute(
            """
            INSERT INTO multiplas_candidatas
                (assinatura, casa_aposta, descricao, odd_combinada, probabilidade_combinada,
                 pernas, jogos, primeiro_apito, congelada, atualizada_em)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, FALSE, NOW())
            ON CONFLICT (assinatura) DO UPDATE SET
                odd_combinada = EXCLUDED.odd_combinada,
                probabilidade_combinada = EXCLUDED.probabilidade_combinada,
                pernas = EXCLUDED.pernas,
                jogos = EXCLUDED.jogos,
                primeiro_apito = EXCLUDED.primeiro_apito,
                atualizada_em = NOW()
            WHERE multiplas_candidatas.congelada = FALSE
            """,
            (
                assinatura, c["casa_aposta"], c["descricao"], c["odd_combinada"],
                c["probabilidade_combinada"], json.dumps(c["pernas"], ensure_ascii=False),
                json.dumps(jogos, ensure_ascii=False, default=str), primeiro_apito,
            ),
        )
        capturadas += 1
    return capturadas


def congelar_candidatas_vencidas(cur):
    """NOVO: marca como congeladas (`congelada = TRUE`) as candidatas cujo
    jogo mais próximo (`primeiro_apito`) já começou - a partir daqui,
    nenhuma geração nova pode mais sobrescrever essas linhas. Existe como
    trava explícita mesmo sabendo que a OddsPapi já bloqueia atualização
    de odds de jogo em andamento (o projeto evita depender cegamente de
    comportamento de API de terceiro sem essa segunda camada de
    segurança)."""
    cur.execute(
        "UPDATE multiplas_candidatas SET congelada = TRUE "
        "WHERE congelada = FALSE AND primeiro_apito <= NOW()"
    )
    return cur.rowcount
