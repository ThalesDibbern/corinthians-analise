"""
Script PONTUAL (não faz parte do encadeamento diário do cron) - reabre
apostas em `apostas_salvas` cujo resultado (acertou/errou) foi gravado com
dado que DEPOIS foi corrigido em `historico_recomendacoes`.

DE ONDE VEM O BUG:
    `resolver_apostas_pendentes()` (em app.py) só olha apostas com
    `resultado = 'pendente'`. Assim que uma aposta é resolvida (acertou ou
    errou), ela nunca mais é revisitada - mesmo que uma correção de dado
    (auditoria, refetch, correção de atribuição) mude depois o `resultado`
    da linha correspondente em `historico_recomendacoes`.

    Caso real que expôs o problema (25-26/08/2026): a aposta salva
    "Escanteios Total do Jogo - Menos de 11.5" do jogo Fluminense x Remo
    foi resolvida como ACERTOU em 22/08, quando o banco ainda tinha o
    escanteio parcial do jogo (4 e <=3, total <=7). A Fase 1.5 da
    auditoria corrigiu o dado (real: 11 e 2, total 13) e mudou a linha
    correspondente de `historico_recomendacoes` de 'acertou' pra 'errou' -
    mas a aposta salva, já resolvida, ficou com o resultado velho, com o
    retorno já creditado errado na banca.

    Não é um caso isolado: qualquer uma das 18 divergências corrigidas na
    auditoria de 22-24/08 (lado invertido no cartão de time, atribuição
    errada no Chapecoense x São Paulo) pode ter aposta salva na mesma
    situação, e nenhum dos scripts que corrigiram `historico_recomendacoes`
    (corrigir_lado_cartao_time.py, refetch_rodada_pontual.py,
    corrigir_cartao_atribuicao.py) tocou em `apostas_salvas`.

POR QUE ISSO IMPORTA:
    R$, ROI e taxa de acerto em /minhas-apostas ficam errados até isso ser
    corrigido - não é só um número de calibração, é a banca (fictícia, mas
    a que o app usa pra tudo) com valor creditado que não deveria.

COMO RESOLVE:
    Reusa `avaliar_pernas_aposta()` e `resultado_final_das_pernas()` de
    app.py - a MESMA lógica de casamento que resolver_apostas_pendentes()
    usa (lógica de avaliação nunca deve existir em mais de um lugar). Pra
    cada aposta já resolvida (`resultado` em 'acertou'/'errou'):
      1. Recalcula o resultado de cada perna contra o `historico_
         recomendacoes` de HOJE.
      2. Se o resultado final recalculado for igual ao salvo, não mexe.
      3. Se for diferente, essa aposta está DESATUALIZADA: se ela tinha
         resultado 'acertou', estorna da banca o que foi creditado
         (stake + lucro); se era 'errou', não tinha creditado nada, não
         estorna nada. Depois volta ela pra 'pendente' e deixa
         `resolver_apostas_pendentes()` (a função de sempre) resolver de
         novo com o dado certo - inclusive recreditando a banca se o novo
         resultado for 'acertou'.
      4. Se alguma perna vier 'pendente' na recontagem (a linha
         correspondente sumiu de historico_recomendacoes - não deveria
         acontecer, mas não é impossível), a aposta NÃO é reaberta
         automaticamente - fica listada à parte pra checar na mão.

DUAS FASES SEPARADAS (rodar uma de cada vez, de propósito):

  FASE 1 (`verificar`) - NÃO GRAVA NADA:
    Lista toda aposta desatualizada, o resultado salvo vs. o recalculado,
    e o valor de banca que seria estornado. Serve pra ver o tamanho do
    estrago antes de aplicar.

  FASE 2 (`aplicar`):
    Estorna a banca das apostas que tinham creditado errado, reabre todas
    as desatualizadas como 'pendente', e roda resolver_apostas_pendentes()
    pra fechar de novo com o dado certo. Tudo numa transação só - se
    qualquer coisa falhar no meio, `rollback` e nada muda.

GRUPO DE CONTROLE (aprendizado da correção do lado do cartão): toda
aposta com resultado salvo IGUAL ao recalculado é contada e mostrada no
resumo - se esse número não bater com "quase todas as apostas", é sinal
de bug NESTE script, não motivo pra aplicar.

⚠️ ORDEM OBRIGATÓRIA (26/08/2026): rodar
`migrar_apostas_resolvido_manualmente.py` ANTES deste script - ele passou
a excluir da reavaliação toda aposta com `resolvido_manualmente = TRUE`
(correção manual feita em /minhas-apostas), e essa coluna só existe depois
da migração. Sem ela, a query quebra - de propósito, não passa a
"funcionar sem a trava" silenciosamente.

Rodar:
    python reabrir_apostas_desatualizadas.py verificar
    python reabrir_apostas_desatualizadas.py aplicar

Variáveis de ambiente necessárias: DATABASE_URL
"""

import json
import os
import sys
from datetime import datetime

import psycopg2

# Trava contra rodar com módulo desatualizado: se app.py ainda não tiver
# passado pela refatoração que extraiu avaliar_pernas_aposta/
# resultado_final_das_pernas, o import falha aqui e avisa - em vez de
# duplicar a lógica de casamento numa cópia divergente (aprendizado
# antigo: lógica de avaliação nunca deve existir em mais de um lugar).
try:
    from app import (
        avaliar_pernas_aposta,
        resultado_final_das_pernas,
        registrar_movimento_banca,
        resolver_apostas_pendentes,
    )
except ImportError as exc:
    sys.exit(
        "Não consegui importar de app.py (avaliar_pernas_aposta / "
        "resultado_final_das_pernas / registrar_movimento_banca / "
        "resolver_apostas_pendentes). Confirme que o deploy do app já "
        f"subiu essas funções antes de rodar este script.\nErro original: {exc}"
    )

DATABASE_URL = os.environ["DATABASE_URL"]


def imprimir_cabecalho(fase):
    """Todo script pontual novo imprime fase, argv e relógio - o log se
    identifica sozinho, evita o caso já visto de confundir o deploy/log
    ANTERIOR com o atual e achar que uma fase não rodou quando rodou."""
    print("=" * 78)
    print("reabrir_apostas_desatualizadas.py")
    print(f"  fase:   {fase}")
    print(f"  argv:   {sys.argv}")
    print(f"  agora:  {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 78)


def buscar_apostas_resolvidas(cur):
    # NOVO (26/08/2026): exclui resolvido_manualmente = TRUE - uma
    # correção manual (botão "Corrigir manualmente" em /minhas-apostas)
    # é uma decisão explícita do usuário, muitas vezes justamente porque
    # o dado automático está errado ou nunca vai existir (evento que a
    # API-Football nunca gravou). Reavaliar por cima desfaria a correção
    # sem avisar. Requer a migração migrar_apostas_resolvido_manualmente.py
    # já aplicada - se a coluna não existir, essa query falha, e é
    # melhor falhar alto do que rodar sem essa trava.
    cur.execute(
        """SELECT id, pernas, odd_combinada, valor_apostado, usuario_id,
                  resultado, retorno, descricao, resolvido_em
           FROM apostas_salvas
           WHERE resultado IN ('acertou', 'errou')
             AND resolvido_manualmente = FALSE
           ORDER BY id"""
    )
    return cur.fetchall()


def diagnosticar(cur):
    """Recalcula o resultado de cada aposta já resolvida e separa em três
    grupos: inalteradas (grupo de controle), desatualizadas (a reabrir) e
    inconclusivas (perna sumiu, não mexe sozinho). Não grava nada - usada
    pelas duas fases."""
    resolvidas = buscar_apostas_resolvidas(cur)

    inalteradas = []
    desatualizadas = []
    inconclusivas = []

    for (aposta_id, pernas_json, odd_combinada, valor_apostado, usuario_id,
         resultado_salvo, retorno_salvo, descricao, resolvido_em) in resolvidas:

        pernas = pernas_json if isinstance(pernas_json, list) else json.loads(pernas_json)
        resultados_pernas = avaliar_pernas_aposta(cur, pernas)
        resultado_recalculado = resultado_final_das_pernas(resultados_pernas)

        registro = {
            "aposta_id": aposta_id,
            "descricao": descricao,
            "usuario_id": usuario_id,
            "odd_combinada": odd_combinada,
            "valor_apostado": valor_apostado,
            "resultado_salvo": resultado_salvo,
            "retorno_salvo": retorno_salvo,
            "resultado_recalculado": resultado_recalculado,
            "resolvido_em": resolvido_em,
        }

        if resultado_recalculado is None:
            inconclusivas.append(registro)
        elif resultado_recalculado == resultado_salvo:
            inalteradas.append(registro)
        else:
            desatualizadas.append(registro)

    return inalteradas, desatualizadas, inconclusivas


def fase_verificar():
    """Só lê e simula. Nenhuma escrita no banco."""
    imprimir_cabecalho("verificar")

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    try:
        inalteradas, desatualizadas, inconclusivas = diagnosticar(cur)
        total = len(inalteradas) + len(desatualizadas) + len(inconclusivas)

        print(f"\n{total} aposta(s) salva(s) já resolvida(s) (acertou/errou) no total.")
        print(f"  Inalteradas (grupo de controle - resultado bate):   {len(inalteradas)}")
        print(f"  Desatualizadas (serão reabertas na fase aplicar):  {len(desatualizadas)}")
        print(f"  Inconclusivas (perna sumiu - NÃO mexidas sozinho): {len(inconclusivas)}")

        if desatualizadas:
            print("\n" + "=" * 78)
            print("DESATUALIZADAS")
            print("=" * 78)
            estorno_total = 0.0
            for r in desatualizadas:
                print(f"\n  #{r['aposta_id']} usuário {r['usuario_id']} - {r['descricao']}")
                print(f"      resolvida em {r['resolvido_em']}")
                print(f"      salvo: {r['resultado_salvo']} (retorno {r['retorno_salvo']})  "
                      f"-> recalculado: {r['resultado_recalculado']}")
                if r["resultado_salvo"] == "acertou":
                    estorno = -(float(r["valor_apostado"]) + float(r["retorno_salvo"] or 0))
                    estorno_total += estorno
                    print(f"      banca: estorna R$ {estorno:.2f} (stake + retorno creditado errado)")
                else:
                    print("      banca: nada a estornar (era 'errou', nada tinha sido creditado)")
            print(f"\nEstorno total de banca previsto: R$ {estorno_total:.2f}")

        if inconclusivas:
            print("\n" + "=" * 78)
            print("INCONCLUSIVAS (checar na mão - alguma perna não achou linha em "
                  "historico_recomendacoes)")
            print("=" * 78)
            for r in inconclusivas:
                print(f"  #{r['aposta_id']} usuário {r['usuario_id']} - {r['descricao']} "
                      f"(salvo: {r['resultado_salvo']})")

        print("\nSe fizer sentido, rode:  python reabrir_apostas_desatualizadas.py aplicar")

    finally:
        cur.close()
        conn.close()


def fase_aplicar():
    """Estorna a banca das apostas desatualizadas, reabre como 'pendente'
    e deixa resolver_apostas_pendentes() fechar de novo com o dado certo -
    tudo numa transação só."""
    imprimir_cabecalho("aplicar")

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        inalteradas, desatualizadas, inconclusivas = diagnosticar(cur)

        if not desatualizadas:
            print("\nNenhuma aposta desatualizada encontrada - nada a reabrir.")
            print(f"({len(inalteradas)} inalterada(s), {len(inconclusivas)} inconclusiva(s) "
                  "- essas não são mexidas por este script.)")
            cur.close()
            conn.close()
            return

        print(f"\n{len(desatualizadas)} aposta(s) serão reabertas. "
              f"{len(inalteradas)} inalterada(s) ficam como estão (grupo de controle).")

        for r in desatualizadas:
            aposta_id = r["aposta_id"]

            if r["resultado_salvo"] == "acertou":
                estorno = -(float(r["valor_apostado"]) + float(r["retorno_salvo"] or 0))
                registrar_movimento_banca(cur, r["usuario_id"], "estorno", estorno, aposta_id)
                print(f"  #{aposta_id}: estornado R$ {estorno:.2f} da banca do usuário {r['usuario_id']}")
            else:
                print(f"  #{aposta_id}: sem estorno (era 'errou')")

            cur.execute(
                "UPDATE apostas_salvas SET resultado = 'pendente', retorno = NULL, "
                "resolvido_em = NULL WHERE id = %s",
                (aposta_id,),
            )

        print("\nReabertas. Rodando resolver_apostas_pendentes() pra fechar de novo "
              "com o dado corrigido...")
        resolver_apostas_pendentes(cur)

        conn.commit()
        print(f"\n✅ Concluído e commitado. {len(desatualizadas)} aposta(s) reabertas e "
              "reavaliadas.")
        if inconclusivas:
            print(f"⚠️  {len(inconclusivas)} aposta(s) inconclusiva(s) continuam com o "
                  "resultado antigo - alguma perna delas não achou linha correspondente "
                  "em historico_recomendacoes. Checar na mão.")

    except Exception:
        conn.rollback()
        print("\n❌ Erro no meio da aplicação - rollback, nada foi gravado.")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("verificar", "aplicar"):
        sys.exit("Uso: python reabrir_apostas_desatualizadas.py [verificar|aplicar]")

    if sys.argv[1] == "verificar":
        fase_verificar()
    else:
        fase_aplicar()
