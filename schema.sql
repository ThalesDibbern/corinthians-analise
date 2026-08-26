-- ============================================================
-- SCHEMA DO PROJETO - gerado automaticamente por gerar_schema.py
-- Gerado em: 2026-08-26 02:14 UTC
--
-- Este arquivo descreve o ESTADO do banco, não as mudanças.
-- Serve pra reconstruir tudo do zero e pra consultar o tipo real
-- de uma coluna sem precisar abrir o Postgres.
--
-- NÃO editar à mão: rodar gerar_schema.py de novo depois de
-- qualquer migração, pra manter fiel ao banco de verdade.
-- ============================================================

-- 49 tabelas encontradas.


-- ---------- apostas_salvas ----------
CREATE TABLE IF NOT EXISTS apostas_salvas (
    id SERIAL,
    descricao text NOT NULL,
    casa_aposta text,
    odd_combinada numeric NOT NULL,
    probabilidade_combinada numeric,
    valor_apostado numeric NOT NULL,
    pernas jsonb NOT NULL,
    resultado text DEFAULT 'pendente'::text NOT NULL,
    retorno numeric,
    criado_em timestamp without time zone DEFAULT now() NOT NULL,
    resolvido_em timestamp without time zone,
    usuario_id integer,
    CONSTRAINT apostas_salvas_pkey PRIMARY KEY (id),
    CONSTRAINT apostas_salvas_usuario_id_fkey FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
);

-- ---------- atualizacoes_odds ----------
CREATE TABLE IF NOT EXISTS atualizacoes_odds (
    id SERIAL,
    usuario_id integer,
    forcado boolean DEFAULT false NOT NULL,
    criado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT atualizacoes_odds_pkey PRIMARY KEY (id),
    CONSTRAINT atualizacoes_odds_usuario_id_fkey FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
);

-- ---------- banca_movimentos ----------
CREATE TABLE IF NOT EXISTS banca_movimentos (
    id SERIAL,
    usuario_id integer NOT NULL,
    tipo character varying(20) NOT NULL,
    valor numeric(12,2) NOT NULL,
    saldo_apos numeric(12,2) NOT NULL,
    aposta_id integer,
    criado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT banca_movimentos_pkey PRIMARY KEY (id),
    CONSTRAINT banca_movimentos_aposta_id_fkey FOREIGN KEY (aposta_id) REFERENCES apostas_salvas(id) ON DELETE SET NULL,
    CONSTRAINT banca_movimentos_usuario_id_fkey FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
);
CREATE INDEX idx_banca_movimentos_usuario ON public.banca_movimentos USING btree (usuario_id, criado_em DESC);

-- ---------- cartoes ----------
CREATE TABLE IF NOT EXISTS cartoes (
    id SERIAL,
    jogo_id integer,
    jogador_id integer,
    lado character varying(10) NOT NULL,
    cor character varying(10) NOT NULL,
    minuto integer,
    periodo character varying(30),
    CONSTRAINT cartoes_pkey PRIMARY KEY (id),
    CONSTRAINT cartoes_jogador_id_fkey FOREIGN KEY (jogador_id) REFERENCES jogadores(id),
    CONSTRAINT cartoes_jogo_id_fkey FOREIGN KEY (jogo_id) REFERENCES jogos(id)
);

-- ---------- combinacoes_sugeridas ----------
CREATE TABLE IF NOT EXISTS combinacoes_sugeridas (
    id SERIAL,
    jogo_id integer,
    casa_aposta character varying(50) NOT NULL,
    descricao character varying(500) NOT NULL,
    odd_combinada numeric(6,2) NOT NULL,
    probabilidade_combinada numeric(5,2) NOT NULL,
    valor_esperado_combinado numeric(5,3) NOT NULL,
    gerado_em timestamp without time zone DEFAULT now(),
    CONSTRAINT combinacoes_sugeridas_pkey PRIMARY KEY (id),
    CONSTRAINT combinacoes_sugeridas_jogo_id_fkey FOREIGN KEY (jogo_id) REFERENCES jogos(id)
);

-- ---------- controle_api_uso ----------
CREATE TABLE IF NOT EXISTS controle_api_uso (
    dia date NOT NULL,
    requisicoes integer DEFAULT 0 NOT NULL,
    CONSTRAINT controle_api_uso_pkey PRIMARY KEY (dia)
);

-- ---------- escalacoes ----------
CREATE TABLE IF NOT EXISTS escalacoes (
    id SERIAL,
    jogo_id integer,
    jogador_id integer,
    titular boolean DEFAULT true,
    minuto_saida integer,
    CONSTRAINT escalacoes_pkey PRIMARY KEY (id),
    CONSTRAINT escalacoes_jogador_id_fkey FOREIGN KEY (jogador_id) REFERENCES jogadores(id),
    CONSTRAINT escalacoes_jogo_id_fkey FOREIGN KEY (jogo_id) REFERENCES jogos(id)
);

-- ---------- estatisticas_jogo ----------
CREATE TABLE IF NOT EXISTS estatisticas_jogo (
    id SERIAL,
    jogo_id integer,
    lado character varying(10) NOT NULL,
    posse_de_bola numeric(5,2),
    escanteios integer,
    faltas integer,
    passes integer,
    finalizacoes integer,
    desarmes integer,
    CONSTRAINT estatisticas_jogo_pkey PRIMARY KEY (id),
    CONSTRAINT estatisticas_jogo_jogo_id_fkey FOREIGN KEY (jogo_id) REFERENCES jogos(id)
);

-- ---------- execucoes_atualizacao ----------
CREATE TABLE IF NOT EXISTS execucoes_atualizacao (
    id SERIAL,
    status character varying(20) DEFAULT 'rodando'::character varying NOT NULL,
    iniciada_em timestamp with time zone DEFAULT now() NOT NULL,
    finalizada_em timestamp with time zone,
    usuario_id integer,
    forcada boolean DEFAULT false NOT NULL,
    detalhe text,
    CONSTRAINT execucoes_atualizacao_pkey PRIMARY KEY (id),
    CONSTRAINT execucoes_atualizacao_usuario_id_fkey FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE SET NULL
);
CREATE INDEX idx_execucoes_atualizacao_concluidas ON public.execucoes_atualizacao USING btree (finalizada_em DESC) WHERE ((status)::text = 'concluida'::text);
CREATE INDEX idx_execucoes_atualizacao_status ON public.execucoes_atualizacao USING btree (status, iniciada_em DESC);

-- ---------- gols ----------
CREATE TABLE IF NOT EXISTS gols (
    id SERIAL,
    jogo_id integer,
    jogador_id integer,
    lado character varying(10) NOT NULL,
    minuto integer NOT NULL,
    periodo character varying(30) NOT NULL,
    penalti boolean DEFAULT false,
    gol_contra boolean DEFAULT false,
    CONSTRAINT gols_pkey PRIMARY KEY (id),
    CONSTRAINT gols_jogador_id_fkey FOREIGN KEY (jogador_id) REFERENCES jogadores(id),
    CONSTRAINT gols_jogo_id_fkey FOREIGN KEY (jogo_id) REFERENCES jogos(id)
);

-- ---------- historico_multiplas_destaque ----------
CREATE TABLE IF NOT EXISTS historico_multiplas_destaque (
    id SERIAL,
    jogo_id integer NOT NULL,
    rodada character varying(50),
    casa_aposta character varying(100),
    descricao text,
    odd_combinada numeric(10,2),
    jogos jsonb,
    probabilidade_combinada numeric(6,2) NOT NULL,
    resultado character varying(10) NOT NULL,
    criada_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT historico_multiplas_destaque_pkey PRIMARY KEY (id),
    CONSTRAINT historico_multiplas_destaque_jogo_id_fkey FOREIGN KEY (jogo_id) REFERENCES jogos(id)
);
CREATE INDEX idx_historico_multiplas_jogo ON public.historico_multiplas_destaque USING btree (jogo_id);
CREATE INDEX idx_historico_multiplas_rodada ON public.historico_multiplas_destaque USING btree (rodada);

-- ---------- historico_recomendacoes ----------
CREATE TABLE IF NOT EXISTS historico_recomendacoes (
    id SERIAL,
    jogo_id integer,
    jogador_id integer,
    tipo_padrao character varying(30) NOT NULL,
    descricao character varying(255) NOT NULL,
    casa_aposta character varying(50) NOT NULL,
    odd_oferecida numeric(6,2) NOT NULL,
    probabilidade_historica numeric(5,2) NOT NULL,
    valor_esperado numeric(5,3) NOT NULL,
    linha numeric(4,1),
    resultado character varying(10) DEFAULT 'pendente'::character varying NOT NULL,
    data_jogo date NOT NULL,
    arquivado_em timestamp without time zone DEFAULT now(),
    direcao text,
    CONSTRAINT historico_recomendacoes_pkey PRIMARY KEY (id),
    CONSTRAINT historico_recomendacoes_jogador_id_fkey FOREIGN KEY (jogador_id) REFERENCES jogadores(id),
    CONSTRAINT historico_recomendacoes_jogo_id_fkey FOREIGN KEY (jogo_id) REFERENCES jogos(id)
);

-- ---------- jogador_estatisticas_jogo ----------
CREATE TABLE IF NOT EXISTS jogador_estatisticas_jogo (
    id SERIAL,
    jogo_id integer,
    jogador_id integer,
    lado character varying(10) NOT NULL,
    minutos integer,
    posicao character varying(20),
    nota numeric(3,1),
    chutes integer,
    chutes_no_gol integer,
    gols integer,
    assistencias integer,
    passes integer,
    passes_certos integer,
    desarmes integer,
    interceptacoes integer,
    duelos_total integer,
    duelos_vencidos integer,
    dribles_tentados integer,
    dribles_sucesso integer,
    faltas_cometidas integer,
    faltas_sofridas integer,
    impedimentos integer,
    cartao_amarelo integer,
    cartao_vermelho integer,
    penalti_marcado integer,
    penalti_perdido integer,
    penalti_sofrido integer,
    CONSTRAINT jogador_estatisticas_jogo_pkey PRIMARY KEY (id),
    CONSTRAINT jogador_estatisticas_jogo_jogador_id_fkey FOREIGN KEY (jogador_id) REFERENCES jogadores(id),
    CONSTRAINT jogador_estatisticas_jogo_jogo_id_fkey FOREIGN KEY (jogo_id) REFERENCES jogos(id)
);

-- ---------- jogadores ----------
CREATE TABLE IF NOT EXISTS jogadores (
    id SERIAL,
    nome character varying(100) NOT NULL,
    posicao character varying(50),
    numero_camisa integer,
    ativo boolean DEFAULT true,
    api_football_id integer,
    time_atual_id integer,
    CONSTRAINT jogadores_pkey PRIMARY KEY (id),
    CONSTRAINT jogadores_api_football_id_key UNIQUE (api_football_id),
    CONSTRAINT jogadores_time_atual_id_fkey FOREIGN KEY (time_atual_id) REFERENCES times(id)
);

-- ---------- jogos ----------
CREATE TABLE IF NOT EXISTS jogos (
    id SERIAL,
    data_jogo date NOT NULL,
    adversario character varying(100) NOT NULL,
    mandante boolean NOT NULL,
    competicao character varying(100),
    placar_corinthians integer,
    placar_adversario integer,
    arbitro character varying(255),
    mandante_id integer,
    visitante_id integer,
    datahora_jogo timestamp without time zone,
    fixture_id_api integer NOT NULL,
    nosso_time_id integer NOT NULL,
    rodada character varying(50),
    rodada_numero integer,
    placar_corinthians_intervalo integer,
    placar_adversario_intervalo integer,
    CONSTRAINT jogos_pkey PRIMARY KEY (id),
    CONSTRAINT jogos_mandante_id_fkey FOREIGN KEY (mandante_id) REFERENCES times(id),
    CONSTRAINT jogos_nosso_time_id_fkey FOREIGN KEY (nosso_time_id) REFERENCES times(id),
    CONSTRAINT jogos_visitante_id_fkey FOREIGN KEY (visitante_id) REFERENCES times(id)
);
CREATE INDEX idx_jogos_arbitro ON public.jogos USING btree (arbitro) WHERE (arbitro IS NOT NULL);
CREATE UNIQUE INDEX jogos_fixture_time_key ON public.jogos USING btree (fixture_id_api, nosso_time_id);

-- ---------- jogos_liga ----------
CREATE TABLE IF NOT EXISTS jogos_liga (
    id SERIAL,
    fixture_id_api integer NOT NULL,
    temporada integer NOT NULL,
    rodada character varying(50),
    rodada_numero integer,
    data_jogo date NOT NULL,
    mandante_api_id integer NOT NULL,
    mandante_nome character varying(100) NOT NULL,
    visitante_api_id integer NOT NULL,
    visitante_nome character varying(100) NOT NULL,
    placar_mandante integer,
    placar_visitante integer,
    status character varying(10),
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT jogos_liga_pkey PRIMARY KEY (id),
    CONSTRAINT jogos_liga_fixture_id_api_key UNIQUE (fixture_id_api)
);
CREATE INDEX idx_jogos_liga_mandante ON public.jogos_liga USING btree (mandante_api_id, temporada);
CREATE INDEX idx_jogos_liga_temporada_rodada ON public.jogos_liga USING btree (temporada, rodada_numero);
CREATE INDEX idx_jogos_liga_visitante ON public.jogos_liga USING btree (visitante_api_id, temporada);

-- ---------- lesoes_suspensoes ----------
CREATE TABLE IF NOT EXISTS lesoes_suspensoes (
    id SERIAL,
    jogo_id integer NOT NULL,
    jogador_id integer,
    jogador_nome_api character varying(255),
    tipo character varying(255),
    motivo character varying(255),
    dados_brutos jsonb,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT lesoes_suspensoes_pkey PRIMARY KEY (id),
    CONSTRAINT lesoes_suspensoes_jogador_id_fkey FOREIGN KEY (jogador_id) REFERENCES jogadores(id),
    CONSTRAINT lesoes_suspensoes_jogo_id_fkey FOREIGN KEY (jogo_id) REFERENCES jogos(id) ON DELETE CASCADE
);
CREATE INDEX idx_lesoes_suspensoes_jogador ON public.lesoes_suspensoes USING btree (jogador_id);
CREATE INDEX idx_lesoes_suspensoes_jogo ON public.lesoes_suspensoes USING btree (jogo_id);

-- ---------- multiplas_candidatas ----------
CREATE TABLE IF NOT EXISTS multiplas_candidatas (
    id SERIAL,
    assinatura character varying(64) NOT NULL,
    casa_aposta character varying(100) NOT NULL,
    descricao text NOT NULL,
    odd_combinada numeric(10,2) NOT NULL,
    probabilidade_combinada numeric(6,2) NOT NULL,
    pernas jsonb NOT NULL,
    jogos jsonb NOT NULL,
    primeiro_apito timestamp without time zone NOT NULL,
    congelada boolean DEFAULT false NOT NULL,
    avaliada boolean DEFAULT false NOT NULL,
    criada_em timestamp without time zone DEFAULT now() NOT NULL,
    atualizada_em timestamp without time zone DEFAULT now() NOT NULL,
    resultado character varying(10),
    CONSTRAINT multiplas_candidatas_pkey PRIMARY KEY (id),
    CONSTRAINT multiplas_candidatas_assinatura_key UNIQUE (assinatura)
);
CREATE INDEX idx_multiplas_candidatas_congelada ON public.multiplas_candidatas USING btree (congelada, avaliada) WHERE ((congelada = true) AND (avaliada = false));
CREATE INDEX idx_multiplas_candidatas_primeiro_apito ON public.multiplas_candidatas USING btree (primeiro_apito) WHERE (congelada = false);

-- ---------- odds ----------
CREATE TABLE IF NOT EXISTS odds (
    id SERIAL,
    jogo_id integer,
    casa_aposta character varying(50) NOT NULL,
    mercado character varying(255) NOT NULL,
    valor_odd numeric(6,2) NOT NULL,
    coletado_em timestamp without time zone DEFAULT now(),
    jogador_id integer,
    linha numeric(4,1),
    direcao character varying(20),
    CONSTRAINT odds_pkey PRIMARY KEY (id),
    CONSTRAINT odds_jogador_id_fkey FOREIGN KEY (jogador_id) REFERENCES jogadores(id),
    CONSTRAINT odds_jogo_id_fkey FOREIGN KEY (jogo_id) REFERENCES jogos(id)
);

-- ---------- padroes_ambas_marcam_tempo ----------
CREATE TABLE IF NOT EXISTS padroes_ambas_marcam_tempo (
    id SERIAL,
    time_id integer NOT NULL,
    periodo character varying(2) NOT NULL,
    lado character varying(10) NOT NULL,
    jogos_analisados integer NOT NULL,
    jogos_com_ambas integer NOT NULL,
    frequencia numeric(5,2) NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT padroes_ambas_marcam_tempo_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_ambas_marcam_tempo_time_id_periodo_lado_key UNIQUE (time_id, periodo, lado),
    CONSTRAINT padroes_ambas_marcam_tempo_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);

-- ---------- padroes_arbitro ----------
CREATE TABLE IF NOT EXISTS padroes_arbitro (
    arbitro character varying(255) NOT NULL,
    jogos_analisados integer NOT NULL,
    media_cartoes numeric(5,2) NOT NULL,
    media_faltas numeric(5,2),
    jogos_com_vermelho integer DEFAULT 0 NOT NULL,
    frequencia_vermelho numeric(5,2) DEFAULT 0 NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT padroes_arbitro_pkey PRIMARY KEY (arbitro)
);

-- ---------- padroes_cartao_total ----------
CREATE TABLE IF NOT EXISTS padroes_cartao_total (
    linha numeric NOT NULL,
    jogos_analisados integer NOT NULL,
    jogos_acima_da_linha integer NOT NULL,
    frequencia numeric NOT NULL,
    media numeric,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    time_id integer NOT NULL,
    CONSTRAINT padroes_cartao_total_pkey PRIMARY KEY (time_id, linha),
    CONSTRAINT padroes_cartao_total_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);

-- ---------- padroes_confronto_direto ----------
CREATE TABLE IF NOT EXISTS padroes_confronto_direto (
    adversario_id integer NOT NULL,
    mandante_filtro text NOT NULL,
    tipo_padrao text NOT NULL,
    linha numeric DEFAULT 0 NOT NULL,
    resultado text DEFAULT ''::text NOT NULL,
    jogos_analisados integer NOT NULL,
    ocorrencias integer NOT NULL,
    frequencia numeric NOT NULL,
    amostra_pequena boolean NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    nosso_time_id integer NOT NULL,
    CONSTRAINT padroes_confronto_direto_pkey PRIMARY KEY (nosso_time_id, adversario_id, mandante_filtro, tipo_padrao, linha, resultado),
    CONSTRAINT padroes_confronto_direto_adversario_id_fkey FOREIGN KEY (adversario_id) REFERENCES times(id),
    CONSTRAINT padroes_confronto_direto_nosso_time_id_fkey FOREIGN KEY (nosso_time_id) REFERENCES times(id)
);

-- ---------- padroes_correlacao_categoria ----------
CREATE TABLE IF NOT EXISTS padroes_correlacao_categoria (
    id SERIAL,
    par character varying(60) NOT NULL,
    categoria_a character varying(5) NOT NULL,
    estatistica_a character varying(20) NOT NULL,
    categoria_b character varying(5) NOT NULL,
    estatistica_b character varying(20) NOT NULL,
    media_a numeric(8,3) NOT NULL,
    valor_b_acima numeric(8,3) NOT NULL,
    valor_b_abaixo numeric(8,3) NOT NULL,
    jogos_acima integer NOT NULL,
    jogos_abaixo integer NOT NULL,
    jogos_total integer NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT padroes_correlacao_categoria_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_correlacao_categoria_par_key UNIQUE (par)
);

-- ---------- padroes_correlacao_categoria_time ----------
CREATE TABLE IF NOT EXISTS padroes_correlacao_categoria_time (
    id SERIAL,
    time_id integer NOT NULL,
    par character varying(60) NOT NULL,
    categoria_a character varying(5) NOT NULL,
    estatistica_a character varying(20) NOT NULL,
    categoria_b character varying(5) NOT NULL,
    estatistica_b character varying(20) NOT NULL,
    media_a numeric(8,3) NOT NULL,
    valor_b_acima numeric(8,3) NOT NULL,
    valor_b_abaixo numeric(8,3) NOT NULL,
    jogos_acima integer NOT NULL,
    jogos_abaixo integer NOT NULL,
    jogos_total integer NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT padroes_correlacao_categoria_time_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_correlacao_categoria_time_time_id_par_key UNIQUE (time_id, par),
    CONSTRAINT padroes_correlacao_categoria_time_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);
CREATE INDEX idx_padroes_correlacao_categoria_time_time ON public.padroes_correlacao_categoria_time USING btree (time_id);

-- ---------- padroes_correlacao_estatisticas ----------
CREATE TABLE IF NOT EXISTS padroes_correlacao_estatisticas (
    id SERIAL,
    par character varying(40) NOT NULL,
    estatistica_a character varying(20) NOT NULL,
    estatistica_b character varying(20) NOT NULL,
    media_a numeric(8,3) NOT NULL,
    valor_b_acima numeric(8,3) NOT NULL,
    valor_b_abaixo numeric(8,3) NOT NULL,
    jogos_acima integer NOT NULL,
    jogos_abaixo integer NOT NULL,
    jogos_total integer NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT padroes_correlacao_estatisticas_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_correlacao_estatisticas_par_key UNIQUE (par)
);

-- ---------- padroes_correlacao_jogador ----------
CREATE TABLE IF NOT EXISTS padroes_correlacao_jogador (
    id SERIAL,
    jogador_id integer NOT NULL,
    par character varying(60) NOT NULL,
    estatistica_a character varying(20) NOT NULL,
    categoria_b character varying(5) NOT NULL,
    estatistica_b character varying(20) NOT NULL,
    media_pessoal numeric(8,3) NOT NULL,
    valor_b_acima numeric(8,3) NOT NULL,
    valor_b_abaixo numeric(8,3) NOT NULL,
    jogos_acima integer NOT NULL,
    jogos_abaixo integer NOT NULL,
    jogos_total integer NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT padroes_correlacao_jogador_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_correlacao_jogador_jogador_id_par_key UNIQUE (jogador_id, par),
    CONSTRAINT padroes_correlacao_jogador_jogador_id_fkey FOREIGN KEY (jogador_id) REFERENCES jogadores(id)
);
CREATE INDEX idx_padroes_correlacao_jogador_jogador ON public.padroes_correlacao_jogador USING btree (jogador_id);

-- ---------- padroes_correlacao_time ----------
CREATE TABLE IF NOT EXISTS padroes_correlacao_time (
    id SERIAL,
    time_id integer NOT NULL,
    par character varying(40) NOT NULL,
    estatistica_a character varying(20) NOT NULL,
    estatistica_b character varying(20) NOT NULL,
    media_a numeric(8,3) NOT NULL,
    valor_b_acima numeric(8,3) NOT NULL,
    valor_b_abaixo numeric(8,3) NOT NULL,
    jogos_acima integer NOT NULL,
    jogos_abaixo integer NOT NULL,
    jogos_total integer NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT padroes_correlacao_time_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_correlacao_time_time_id_par_key UNIQUE (time_id, par),
    CONSTRAINT padroes_correlacao_time_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);
CREATE INDEX idx_padroes_correlacao_time_time ON public.padroes_correlacao_time USING btree (time_id);

-- ---------- padroes_dupla_chance_tempo ----------
CREATE TABLE IF NOT EXISTS padroes_dupla_chance_tempo (
    id SERIAL,
    time_id integer NOT NULL,
    periodo character varying(2) NOT NULL,
    lado character varying(10) NOT NULL,
    resultado character varying(3) NOT NULL,
    jogos_analisados integer NOT NULL,
    frequencia numeric(5,2) NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT padroes_dupla_chance_tempo_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_dupla_chance_tempo_time_id_periodo_lado_resultado_key UNIQUE (time_id, periodo, lado, resultado),
    CONSTRAINT padroes_dupla_chance_tempo_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);

-- ---------- padroes_escanteio_total ----------
CREATE TABLE IF NOT EXISTS padroes_escanteio_total (
    linha numeric NOT NULL,
    jogos_analisados integer NOT NULL,
    jogos_acima_da_linha integer NOT NULL,
    frequencia numeric NOT NULL,
    media numeric,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    time_id integer NOT NULL,
    CONSTRAINT padroes_escanteio_total_pkey PRIMARY KEY (time_id, linha),
    CONSTRAINT padroes_escanteio_total_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);

-- ---------- padroes_estilo_time ----------
CREATE TABLE IF NOT EXISTS padroes_estilo_time (
    id SERIAL,
    time_id integer NOT NULL,
    tipo character varying(20) NOT NULL,
    papel character varying(20) NOT NULL,
    media_time numeric(6,3) NOT NULL,
    media_liga numeric(6,3) NOT NULL,
    jogos_analisados integer NOT NULL,
    fator numeric(6,3) NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT padroes_estilo_time_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_estilo_time_time_id_tipo_papel_key UNIQUE (time_id, tipo, papel),
    CONSTRAINT padroes_estilo_time_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);
CREATE INDEX idx_padroes_estilo_time_time_id ON public.padroes_estilo_time USING btree (time_id);

-- ---------- padroes_forma_recente ----------
CREATE TABLE IF NOT EXISTS padroes_forma_recente (
    janela integer NOT NULL,
    resultado text NOT NULL,
    jogos_analisados integer NOT NULL,
    ocorrencias integer NOT NULL,
    frequencia numeric NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    time_id integer NOT NULL,
    CONSTRAINT padroes_forma_recente_pkey PRIMARY KEY (time_id, janela, resultado),
    CONSTRAINT padroes_forma_recente_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);

-- ---------- padroes_gols_total ----------
CREATE TABLE IF NOT EXISTS padroes_gols_total (
    id SERIAL,
    time_id integer NOT NULL,
    linha numeric(5,2) NOT NULL,
    jogos_analisados integer NOT NULL,
    jogos_acima_da_linha integer NOT NULL,
    frequencia numeric(5,2) NOT NULL,
    media numeric(6,2) NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT padroes_gols_total_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_gols_total_time_id_linha_key UNIQUE (time_id, linha),
    CONSTRAINT padroes_gols_total_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);

-- ---------- padroes_jogador_cartao ----------
CREATE TABLE IF NOT EXISTS padroes_jogador_cartao (
    id SERIAL,
    jogador_id integer,
    jogos_analisados integer NOT NULL,
    jogos_com_cartao integer NOT NULL,
    frequencia numeric(5,2) NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now(),
    CONSTRAINT padroes_jogador_cartao_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_jogador_cartao_jogador_id_key UNIQUE (jogador_id),
    CONSTRAINT padroes_jogador_cartao_jogador_id_fkey FOREIGN KEY (jogador_id) REFERENCES jogadores(id)
);

-- ---------- padroes_jogador_frequencia ----------
CREATE TABLE IF NOT EXISTS padroes_jogador_frequencia (
    id SERIAL,
    jogador_id integer,
    tipo character varying(30) NOT NULL,
    jogos_analisados integer NOT NULL,
    jogos_com_evento integer NOT NULL,
    frequencia numeric(5,2) NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now(),
    CONSTRAINT padroes_jogador_frequencia_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_jogador_frequencia_jogador_id_tipo_key UNIQUE (jogador_id, tipo),
    CONSTRAINT padroes_jogador_frequencia_jogador_id_fkey FOREIGN KEY (jogador_id) REFERENCES jogadores(id)
);

-- ---------- padroes_jogador_linha ----------
CREATE TABLE IF NOT EXISTS padroes_jogador_linha (
    id SERIAL,
    jogador_id integer,
    tipo character varying(30) NOT NULL,
    linha numeric(3,1) NOT NULL,
    jogos_analisados integer NOT NULL,
    jogos_acima integer NOT NULL,
    frequencia numeric(5,2) NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now(),
    CONSTRAINT padroes_jogador_linha_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_jogador_linha_jogador_id_tipo_linha_key UNIQUE (jogador_id, tipo, linha),
    CONSTRAINT padroes_jogador_linha_jogador_id_fkey FOREIGN KEY (jogador_id) REFERENCES jogadores(id)
);

-- ---------- padroes_marca_ambos_tempos ----------
CREATE TABLE IF NOT EXISTS padroes_marca_ambos_tempos (
    id SERIAL,
    time_id integer NOT NULL,
    jogos_analisados integer NOT NULL,
    jogos_que_marcou_nos_dois integer NOT NULL,
    frequencia numeric(5,2) NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT padroes_marca_ambos_tempos_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_marca_ambos_tempos_time_id_key UNIQUE (time_id),
    CONSTRAINT padroes_marca_ambos_tempos_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);

-- ---------- padroes_quebra_rodada ----------
CREATE TABLE IF NOT EXISTS padroes_quebra_rodada (
    id SERIAL,
    time_id integer NOT NULL,
    tipo_padrao character varying(20) NOT NULL,
    rodada_quebra integer NOT NULL,
    valor_antes numeric(8,4) NOT NULL,
    valor_depois numeric(8,4) NOT NULL,
    diferenca numeric(8,4) NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT padroes_quebra_rodada_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_quebra_rodada_time_id_tipo_padrao_key UNIQUE (time_id, tipo_padrao),
    CONSTRAINT padroes_quebra_rodada_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);
CREATE INDEX idx_padroes_quebra_rodada_time ON public.padroes_quebra_rodada USING btree (time_id);

-- ---------- padroes_rodada_bruto ----------
CREATE TABLE IF NOT EXISTS padroes_rodada_bruto (
    id SERIAL,
    time_id integer NOT NULL,
    tipo_padrao character varying(20) NOT NULL,
    rodada_numero integer NOT NULL,
    valor numeric(8,4) NOT NULL,
    jogos_amostra integer NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT padroes_rodada_bruto_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_rodada_bruto_time_id_tipo_padrao_rodada_numero_key UNIQUE (time_id, tipo_padrao, rodada_numero),
    CONSTRAINT padroes_rodada_bruto_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);
CREATE INDEX idx_padroes_rodada_bruto_time ON public.padroes_rodada_bruto USING btree (time_id, tipo_padrao);

-- ---------- padroes_time_escanteio ----------
CREATE TABLE IF NOT EXISTS padroes_time_escanteio (
    id SERIAL,
    linha numeric(3,1) NOT NULL,
    jogos_analisados integer NOT NULL,
    jogos_acima_da_linha integer NOT NULL,
    frequencia numeric(5,2) NOT NULL,
    media numeric(4,2) NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now(),
    time_id integer NOT NULL,
    lado character varying(10) DEFAULT 'geral'::character varying NOT NULL,
    CONSTRAINT padroes_time_escanteio_time_linha_lado_key UNIQUE (time_id, linha, lado),
    CONSTRAINT padroes_time_escanteio_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);
CREATE INDEX idx_padroes_time_escanteio_time_lado_linha ON public.padroes_time_escanteio USING btree (time_id, lado, linha);

-- ---------- padroes_time_handicap ----------
CREATE TABLE IF NOT EXISTS padroes_time_handicap (
    id SERIAL,
    time_id integer NOT NULL,
    lado character varying(10) NOT NULL,
    linha numeric(4,2) NOT NULL,
    jogos_analisados integer NOT NULL,
    jogos_cobriu integer NOT NULL,
    frequencia numeric(5,2) NOT NULL,
    media_diferenca numeric(5,2) NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT padroes_time_handicap_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_time_handicap_time_id_lado_linha_key UNIQUE (time_id, lado, linha),
    CONSTRAINT padroes_time_handicap_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);

-- ---------- padroes_time_linha ----------
CREATE TABLE IF NOT EXISTS padroes_time_linha (
    id SERIAL,
    time_id integer NOT NULL,
    tipo character varying(20) NOT NULL,
    linha numeric(5,2) NOT NULL,
    jogos_analisados integer NOT NULL,
    jogos_acima integer NOT NULL,
    frequencia numeric(5,2) NOT NULL,
    media numeric(6,2) NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT padroes_time_linha_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_time_linha_time_id_tipo_linha_key UNIQUE (time_id, tipo, linha),
    CONSTRAINT padroes_time_linha_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);

-- ---------- padroes_time_marca ----------
CREATE TABLE IF NOT EXISTS padroes_time_marca (
    id SERIAL,
    time_id integer NOT NULL,
    jogos_analisados integer NOT NULL,
    jogos_que_marcou integer NOT NULL,
    frequencia numeric(5,2) NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT padroes_time_marca_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_time_marca_time_id_key UNIQUE (time_id),
    CONSTRAINT padroes_time_marca_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);

-- ---------- padroes_time_resultado ----------
CREATE TABLE IF NOT EXISTS padroes_time_resultado (
    id SERIAL,
    lado character varying(10) NOT NULL,
    resultado character varying(10) NOT NULL,
    jogos_analisados integer NOT NULL,
    ocorrencias integer NOT NULL,
    frequencia numeric(5,2) NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now(),
    time_id integer NOT NULL,
    CONSTRAINT padroes_time_resultado_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_time_resultado_time_lado_resultado_key UNIQUE (time_id, lado, resultado),
    CONSTRAINT padroes_time_resultado_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);

-- ---------- padroes_zona_time ----------
CREATE TABLE IF NOT EXISTS padroes_zona_time (
    id SERIAL,
    time_id integer NOT NULL,
    tipo_padrao character varying(20) NOT NULL,
    zona character varying(10) NOT NULL,
    condicao character varying(20) NOT NULL,
    valor numeric(8,4) NOT NULL,
    jogos_amostra integer NOT NULL,
    atualizado_em timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT padroes_zona_time_pkey PRIMARY KEY (id),
    CONSTRAINT padroes_zona_time_time_id_tipo_padrao_zona_condicao_key UNIQUE (time_id, tipo_padrao, zona, condicao),
    CONSTRAINT padroes_zona_time_time_id_fkey FOREIGN KEY (time_id) REFERENCES times(id)
);
CREATE INDEX idx_padroes_zona_time_time ON public.padroes_zona_time USING btree (time_id);

-- ---------- recomendacoes ----------
CREATE TABLE IF NOT EXISTS recomendacoes (
    id SERIAL,
    jogo_id integer,
    jogador_id integer,
    tipo_padrao character varying(30) NOT NULL,
    descricao character varying(255) NOT NULL,
    casa_aposta character varying(50) NOT NULL,
    odd_oferecida numeric(6,2) NOT NULL,
    probabilidade_historica numeric(5,2) NOT NULL,
    valor_esperado numeric(5,3) NOT NULL,
    gerado_em timestamp without time zone DEFAULT now(),
    linha numeric(4,1),
    direcao text,
    CONSTRAINT recomendacoes_pkey PRIMARY KEY (id),
    CONSTRAINT recomendacoes_jogador_id_fkey FOREIGN KEY (jogador_id) REFERENCES jogadores(id),
    CONSTRAINT recomendacoes_jogo_id_fkey FOREIGN KEY (jogo_id) REFERENCES jogos(id)
);

-- ---------- substituicoes ----------
CREATE TABLE IF NOT EXISTS substituicoes (
    id SERIAL,
    jogo_id integer,
    jogador_saiu_id integer,
    jogador_entrou_id integer,
    lado character varying(10) NOT NULL,
    minuto integer NOT NULL,
    periodo character varying(30),
    CONSTRAINT substituicoes_pkey PRIMARY KEY (id),
    CONSTRAINT substituicoes_jogador_entrou_id_fkey FOREIGN KEY (jogador_entrou_id) REFERENCES jogadores(id),
    CONSTRAINT substituicoes_jogador_saiu_id_fkey FOREIGN KEY (jogador_saiu_id) REFERENCES jogadores(id),
    CONSTRAINT substituicoes_jogo_id_fkey FOREIGN KEY (jogo_id) REFERENCES jogos(id)
);

-- ---------- times ----------
CREATE TABLE IF NOT EXISTS times (
    id SERIAL,
    nome text NOT NULL,
    api_football_team_id integer,
    oddspapi_participant_id integer,
    criado_em timestamp without time zone DEFAULT now() NOT NULL,
    rastreado boolean DEFAULT false NOT NULL,
    apelidos text[] DEFAULT '{}'::text[] NOT NULL,
    CONSTRAINT times_pkey PRIMARY KEY (id),
    CONSTRAINT times_api_football_team_id_key UNIQUE (api_football_team_id),
    CONSTRAINT times_nome_key UNIQUE (nome),
    CONSTRAINT times_oddspapi_participant_id_key UNIQUE (oddspapi_participant_id)
);

-- ---------- usuarios ----------
CREATE TABLE IF NOT EXISTS usuarios (
    id SERIAL,
    nome text NOT NULL,
    senha_hash text NOT NULL,
    cor_avatar text DEFAULT '#1f6feb'::text NOT NULL,
    criado_em timestamp without time zone DEFAULT now() NOT NULL,
    banca_atual numeric(12,2) DEFAULT 0 NOT NULL,
    CONSTRAINT usuarios_pkey PRIMARY KEY (id),
    CONSTRAINT usuarios_nome_key UNIQUE (nome)
);
