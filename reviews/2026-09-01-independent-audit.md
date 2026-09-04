# Auditoria independente e imparcial — marketing-center + integration-core

- Data: 2026-09-01
- Auditor: Claude (coordenação e verificação final)
- Método: 6 auditores cegos por dimensão (segurança, integração Meta, modelo de
  dados/idempotência, testes, performance/escala, ORM) + 5 lentes complementares
  (plano×código, manutenibilidade, LGPD, topologia/CI, operabilidade) + verificação
  adversarial por cluster (3 lentes por P1) + catálogo de ideias com fact-check de
  APIs em fontes oficiais. Auditores cegos a `reviews/` e `plan.md`.
- Escopo auditado: `marketing_center_base`, `marketing_center_meta`,
  `marketing_center_contact_center` (~8,9k linhas de produção + ~3,9k de testes) e
  `integration-core` (`meta_api_base`, `meta_webhook_base`; ~4,4k + 2,1k).
- Consumidor previsto: **Codex**, para implementar as correções.

## ⚠️ Limitações desta auditoria (leia primeiro)

1. **O código foi um alvo em movimento.** A auditoria começou sobre o HEAD `b62b7ed`
   (3 módulos); durante sua execução a árvore de trabalho ganhou correções e **11
   módulos novos não rastreados** (`_account`, `_crm`, `_contact_center_crm`,
   `_dashboard`, `_google`, `_meta_crm`, `_sale`, `_sale_account`, `_web_ingress`,
   `_website`, `_website_crm`) — fingerprint da árvore no fechamento: `5d932a8d`,
   121 caminhos sujos. **Vários achados P1 da 1ª rodada já estavam corrigidos na
   árvore quando os verificadores chegaram** (detalhe abaixo). Os 11 módulos novos
   (~fora dos 3 auditados) receberam apenas as lentes LGPD/topologia/manutenibilidade —
   **não** receberam auditoria dedicada de segurança/ORM/dados.
2. **Cobertura da verificação adversarial:** os clusters C01–C04 foram verificados
   com múltiplas lentes; C05–C30 e os achados da 2ª rodada **não** passaram por
   verificação adversarial (limites de sessão + encerramento solicitado). Cada achado
   abaixo carrega seu status. Dado o padrão observado (4 de 4 clusters verificados já
   estavam total ou parcialmente corrigidos na árvore), trate os não-verificados como
   **hipóteses fortes a confirmar contra a árvore atual**, não como fatos.
3. Não concluídas: auditoria ORM dedicada, lente de migrações, lente
   `meta_webhook_base` dedicada, 3 geradores de ideias por persona.

## Sumário executivo

**Veredito técnico: fundação de engenharia excepcional — e o risco dominante não é
código, é processo.** Os núcleos exibem disciplina rara (ledgers imutáveis com
tokens de identidade não-forjáveis, fencing por revisões + CAS de cursor, segredos
jamais no banco, DTOs validados, SQL parametrizado, minimização de PII no bridge), e
a velocidade de correção é notável: 4 clusters P1 apontados pela 1ª rodada já
estavam corrigidos na árvore horas depois, com testes.

Os problemas reais, em ordem:

| # | Achado | Sev | Status de verificação |
| --- | --- | --- | --- |
| 1 | **TOPO-01/PLAN-01 — Todo o código implantado existe só em árvores não commitadas, sem remote, nos 3 repos** | **P1** | Verificado diretamente (git) |
| 2 | **LGPD-01 — Respostas brutas de Lead Ads (nome/e-mail/telefone/texto livre) em claro, imutáveis, sem apagamento nem retenção** | **P1** | Evidência forte; não adversarial |
| 3 | **LGPD-02 — FKs `ondelete=restrict` + links imutáveis impedem excluir `crm.lead`/`sale.order`/`account.move`** | **P1** | Evidência forte; não adversarial |
| 4 | **DATA-01 — Identidade do bridge mais fraca que a do CC: dois touchpoints do CC podem colapsar num só no Marketing, silenciosamente** | **P1** | Não adversarial |
| 5 | **C05 (DATA-02/META-07) — DST à meia-noite torna dias insincronizáveis (janela de 7 dias falha inteira)** | **P1** | Reproduzido com pytz pelo auditor; não adversarial |
| 6 | **C07 (TST-02) — CAS/locks/run-exclusivo sem nenhum teste de concorrência real** — remover qualquer `FOR UPDATE` passa a suíte | P1 | Não adversarial |
| 7 | PERF-01/03/04 — touch de duplicatas em colunas indexadas (bloat), backfill estritamente serial por janela, bridge 1-job-por-write com starvation | P1 | Não adversarial |

**Já corrigidos na árvore (não commitados!)** — a 1ª rodada os reportou como P1 e a
verificação adversarial os **refutou contra a árvore atual**: cursor de catálogo
envenenado (C01 → agora há `_restart_catalog_cursor` + cron + teste), run preso sem
cancel/reaper (C02 → `action_cancel` + watchdog 15 min + testes), discovery quebrando
com moeda/source arquivada (C04 → `active_test=False` + reativação + savepoint por
conta + testes), e a classificação BUC de rate limit (parte do C03 → família
80000–80014 já classificada, com teste). **Nada disso sobrevive a um
`git checkout`.**

---

## O achado nº 1 — processo, não código

### TOPO-01 · P1 · Código implantado sem commit, sem remote, sem backup, nos três repositórios

Verificado diretamente: `git remote -v` vazio em marketing-center, integration-core
**e** contact-center. Estado no fechamento:

- **contact-center**: 120 arquivos com +11.590/−9.877 sobre o HEAD, 63 não
  rastreados (o módulo `contact_center_crm` inteiro, `hooks.py`, migrations
  1.30.0–1.35.0, toda a nova camada `contact_center_meta` 2.0.0); manifest da árvore
  em 16.0.1.35.0 vs HEAD 16.0.1.28.5 **vs index 16.0.1.29.1** — um commit do index
  atual produziria um estado que **não instala** (TOPO-10).
- **marketing-center**: HEAD `b62b7ed` tem 3 módulos; a árvore tem 14 (57 arquivos
  modificados + 110 não rastreados).
- **integration-core**: `meta_webhook_base` inteiro, models/, security/ e
  `credentials.py` não rastreados; consumidores da árvore importam símbolos que o
  HEAD do integration-core **não tem** → um checkout misto derruba o servidor Odoo
  inteiro com ImportError no registry (TOPO-04).
- Os scripts de deploy exigem exatamente as versões não commitadas e empacotam a
  working tree **sem registrar SHA** (TOPO-03) — dois deploys "16.0.1.35.0" podem
  conter código diferente; rollback para um commit conhecido é impossível.
- Únicos bundles existentes: contact-center 1.22.x, defasados, dentro de
  `scans/raw/` (ignorado pelo repo pai).

**Cenário:** perda/corrupção do disco WSL, um `git checkout`/`git stash` acidental,
ou simplesmente o próximo `git add` parcial destroem ou corrompem a única cópia do
que está rodando no laboratório — incluindo todas as correções desta auditoria.

**Correção (meio dia de trabalho, valor máximo):**
1. Commit por marco lógico nos 3 repos (revisar `git diff --cached` — o index do
   contact-center está inconsistente com a árvore);
2. criar remotes (`lcsztl/*` já referenciados nos manifests) + push + tags
   `16.0.x.y.z-lab` de cada versão implantada;
3. scripts de deploy passam a exigir árvore limpa e a gravar `git rev-parse HEAD`
   no evidence JSON;
4. resolver a instalabilidade cross-repo: `oca_dependencies.txt` apontando
   integration-core (e contact-center no marketing-center), `setup/` nos dois repos
   novos (TOPO-02); pre-commit/CI copiados do contact-center (TOPO-12).

## P1 de conformidade (LGPD) — decisão de produto urgente

### LGPD-01 · P1 · Lead Ads: dado pessoal em claro, imutável, sem ciclo de vida

`marketing.center.meta.lead.field.values_json` recebe **todos** os campos do
formulário sem allowlist (nome, e-mail, telefone, respostas livres de até 4.096
chars), `groups="base.group_system"`, com `write()`/`unlink()` bloqueados **até para
sudo** e nenhum cron de expurgo (`lead_ads.py:757, 780-784, 1229-1243`;
`services/lead_ads.py:147-158`). Pedido de eliminação de titular = impossível pelo
ORM; único caminho é SQL manual quebrando os hashes de auditoria.

**Correção:** serviço tokenizado `_erase_subject()`/`_expire()` que substitui
`values_json` por tombstone preservando `values_sha256`/`value_count` + trilha;
`retain_until` na submission calculado na ingestão + cron. O mesmo padrão resolve
LGPD-03 (identificadores pseudonimizados sem retenção — `retain_until` existe no
modelo e **nunca é preenchido por nenhum produtor**) e LGPD-05 (payload integral de
mensagens Messenger/Instagram retido para sempre em `meta.webhook.item`).

### LGPD-02 · P1 · Erasure bloqueada no registro de negócio

`marketing.attribution.crm.link.lead_id` (e equivalentes em sale/account) usam
`ondelete="restrict"` com `unlink()` bloqueado no mixin — um `crm.lead` com
touchpoint vinculado **não pode mais ser excluído** (IntegrityError), e o link não
pode ser removido antes (AccessError). Correção: `ondelete="cascade"` nos links (são
projeção, o ledger de evidência continua íntegro) **ou** unlink tokenizado com
tombstone + teste `test_lead_unlink_with_marketing_links`.

Complementos LGPD (P2/P3): consentimento nunca capturado (`consent_state` sempre
"unknown" — o Lead Ads **tem** `custom_disclaimer_responses` disponível na API e não
o pede, PLAN-02); pré-requisitos de Customer Match/CAPI ausentes (purpose, E.164,
opt-out por titular — LGPD-06); sem trilha de auditoria de leitura/exportação
(LGPD-07); hash sem sal = pseudonimização, não anonimização — documentar no
ROPA/RIPD (LGPD-08); submissions legíveis por todo Viewer (LGPD-09); UTM sem scrub
de PII injetada (LGPD-10).

## P1 técnicos confirmados por evidência (não adversarial)

### DATA-01 · Identidade do bridge mais fraca que a origem

O CC inclui `conversation_address_fingerprint` no `canonical_key` de eventos; o
mapper do bridge deriva `source_occurrence_ref` **sem** o fingerprint e sem o
`public_ref`/`canonical_key` do CC, e o link não tem unique em
`marketing_touchpoint_id`. Dois touchpoints distintos do CC (mesmo event_id, conversas
diferentes) colapsam num só no Marketing como revisão "conflict" — **silencioso**.
Correção: derivar do `canonical_key`/`public_ref` do CC + `unique(marketing_touchpoint_id)`
+ `MAPPING_VERSION=2` com migração.

### C05 · DST à meia-noite torna dias insincronizáveis

`localize(..., is_dst=None)` levanta em transição de DST à meia-noite
(`America/Santiago` 2026-09-06 reproduzido com pytz). A janela de 7 dias que contém
o dia falha **inteira** (página atômica) e o dia nunca pode existir no ledger.
Irrelevante para contas só-Brasil hoje (BR sem DST desde 2019); bloqueador antes de
conectar contas de Chile/Paraguai/etc. Correção: regra determinística única
(`is_dst=False` + normalize) nos dois pontos + teste com os três fusos.

### C07 · Garantias de concorrência sem rede de teste

Todos os testes de "CAS" rodam numa única transação; **remover qualquer
`FOR UPDATE` passa a suíte inteira**. O harness real existe no contact-center
(threads + cursores + barrier) — portar o padrão para: dois workers no mesmo job de
página; índice parcial de run ativo violado por SQL direto; `cursor_changed` sem
I/O. Adicionalmente `_locked_cursor` não invalida o cache ORM após o `FOR UPDATE`
(DATA-05) — hoje protegido em camadas, mas é exatamente o tipo de coisa que só um
teste de concorrência real segura.

### PERF-01/03/04 · Padrões que não escalam (limiar: dezenas de contas / 2 anos)

- **PERF-01**: duplicata (hash igual) ainda escreve `last_observed_at`/
  `last_sync_run_id`, ambos indexados, em tabelas com ~18-20 colunas indexadas →
  UPDATE não-HOT em massa (63k/dia no cenário-alvo) + bloat. Correção: não tocar a
  linha em duplicata (o `sync_run` já conta) ou desindexar essas colunas + fillfactor.
- **PERF-03**: exclusividade de run ignora `window_key` → backfill de 2 anos = 24
  janelas **em série por conta/grain, cada uma exigindo clique humano**. Correção:
  incluir `window_key` no escopo de `metrics` + planejador de backfill encadeado.
- **PERF-04**: bridge = 1 job por write do touchpoint do CC (prioridade 40, acima
  dos Insights 45), sem lote, backfill sem auto-continuação, `OperationalError` vira
  FAILED definitivo → lacuna silenciosa entre ledgers. Correção: lote com savepoint
  por item, retry para OperationalError, prioridade 80 para backfill, cron de
  reconciliação (OPS-07).

## Corrigidos na árvore durante a auditoria (verificação adversarial)

| Cluster | Achado original (1ª rodada) | Veredito contra a árvore | O que existe agora |
| --- | --- | --- | --- |
| C01 | Cursor de catálogo envenenado sem reset → source travada até SQL | **REFUTADO** (3 lentes, alta) | `_restart_catalog_cursor` chamado pelo cron diário novo e pela ação manual; teste `test_new_full_sweep_discards_failed_predecessor_cursor` |
| C02 | Run preso em running sem cancel/reaper → escopo bloqueado para sempre | **REFUTADO/P3** | `action_cancel` + botão, watchdog `_recover_stuck_runs` (planned>30min, ativo>6h) + cron 15 min + testes |
| C04 | Discovery inteira falha com moeda/source arquivada | **REFUTADO/P3** | `active_test=False` + reativação + savepoint por conta + advisory lock por (company, external_ref) + testes |
| C03 | Rate limits BUC classificados como permanentes; 8×60s | **PARCIAL → P2** | Família 80000–80014 **já** era rate limit (com teste). Resta real: nenhum header de uso lido (`X-Business-Use-Case-Usage`/`estimated_time_to_regain_access`), backoff fixo de 60 s insuficiente para bloqueios BUC de até 60 min, 341/368 permanentes salvo `is_transient` |

Correções do C03 remanescente: parsear os headers de uso e derivar o retry deles;
não impor `seconds` fixo (deixar o `retry_pattern` crescer); teto próprio maior para
`rate_limited`.

## Consolidação P2 (por lente — não adversarial; conferir contra a árvore)

**Integração Meta:** META-06 (v26.0 hard-coded em Insights, divergente do
`graph_version` do App — TOPO-06 confirma pins divergentes entre consumidores do
mesmo App), META-08 (sem backfill além dos 7 dias — parcialmente mitigado pelos
crons novos), META-09/C15 (entidades apagadas nunca reconciliadas; tombstone nunca
produzido; testes passam trivialmente), META-10/OPS-02 (código/subcódigo/fbtrace_id
descartados — o operador não distingue 190 de 10 de 613), META-11/PLAN-11
(`ads_management` exigido onde `ads_read` deveria bastar; Lead reader declarado
read-only exigindo escopo de escrita), META-12 (except Exception com retry cego
~70 min), META-13/C18 (contas removidas não refletidas), META-15/C30 (write do
perfil bumpa revisão por presença de chave; validação não reabilita conexões).

**Dados:** C19 (`_apply_entity_page` sem savepoint — assimetria com performance),
C25 (A→B→A da atribuição diverge do padrão dos outros ledgers), C26 (identidade do
fato inclui `graph_version` → upgrade v27 **duplica a tabela de fatos**), C27
(identidade externa da source mutável), C28 (acoplamento de schema CC→Marketing sem
pin de `ATTRIBUTION_SCHEMA_VERSION`; bridge herda create/write do ledger do CC),
C22 (enriquecimento legítimo do CC vira `disposition="conflict"`).

**Operabilidade (OPS):** watchdog ignora o estado do queue.job → falso "orphaned"
com jobrunner parado (OPS-01, agravado por OPS-10: Retry-After 1h×7 > 6h do
watchdog); sem ação "retomar" — reexecução sempre da página zero (OPS-03); nenhum
cron de saúde/alerta — token expira numa sexta e ninguém sabe (OPS-04); **crons
processam no máximo 50 fontes e sempre as mesmas 50** (OPS-05); canais sem
capacidade competindo com o contact-center no `root:1` do lab (OPS-06/PLAN-10);
segredos: rotação/permissões não documentadas no help (OPS-08); UI de runs sem
filtros/estágio/link para o job (OPS-09); resultados silenciosos `{'cursor_changed'}`
deixam o run ativo 6h sem log (OPS-11); teto fixo de 512 páginas sem config (OPS-12).

**Plano×código (PLAN):** os 5 P1 do plano de 28/08 estão honrados em grau alto
(P1-2 e P1-5 completos; P1-1 resolvido diferente — integration-core — mas
**documentado em ADR**; P1-4 existe no modelo e falta o produtor preencher; P1-3
ainda não exercitável). Divergências a corrigir no plano ou no código: ACL do
Viewer/Analyst anulada na prática (tudo exige admin — PLAN-03), chaves de
idempotência normativas ≠ constraints reais (PLAN-04), grafo de addons desatualizado
(PLAN-05), contrato de adapter nunca materializado — **sem registry, a fase Google
copia ~900 linhas de orquestração** (PLAN-07 + MNT-01/04: catalog_sync e
insights_sync são clones estruturais com 9 pares ≥0.81 de similaridade; MNT-02: o
serviço de Insights nem tem `_name` próprio).

**Manutenibilidade (MNT):** além dos clones acima — helpers duplicados 4-10×
(MNT-05), `_plan_run` com 172 linhas/14 parâmetros (MNT-08), `lead_ads.py` monólito
de 1.737 linhas (MNT-09), fencing em 7 camadas onde 2 bastariam (MNT-19 — consolidar
em `fence_hash` + CAS antes da fase Google), estados/erros como strings mágicas com
allow-list que **silenciosamente re-rotula** `uncertain`→`permanent` (MNT-12),
docstrings 4% (MNT-15), specs Meta dentro do core "provider-neutral" (MNT-22).

**Segurança (round 1 — postura geral excepcional, 0 P0/P1):** SEC-03
(`access_token_ref` editável por admin de marketing aponta para qualquer env
var/arquivo — alinhar a `base.group_system` como os módulos irmãos), SEC-04 (grupo
do bridge implica Contact Center Admin inteiro), SEC-05/06 (webhook_url sem groups;
`compare_digest` com str não-ASCII → 500), SEC-08/09 (regras mortas; checagens de
empresa vazias sob sudo — documentar contrato).

**Testes:** TST-04 (assert de segredo que não pode falhar), TST-05 (teto de retry
simulado à mão; `queue_job` real nunca exercido; validação esgotada deixa health
`unknown` para sempre), TST-08/C23 (migrações jamais executadas — e TOPO-09 mostra
que a migration 16.0.1.2.0 do base **nunca executará**: é a versão do commit
inicial), TST-17 (nenhum caminho raw-Graph→banco; fixtures inventadas), mais os P3
de higiene (SavepointCase deprecado, zero @tagged, duplicação de setup).

## Forças confirmadas (não "corrigir")

- Segredos jamais no banco; backend de arquivo defensivo (O_NOFOLLOW, modo 0600,
  parent check); `appsecret_proof` em toda chamada; exceções cortadas com
  `from None` para não vazar URL; HMAC do webhook validado antes do parse.
- Ledgers imutáveis com tokens `object()` não-forjáveis via RPC; minimização real no
  bridge (só hash+máscara+ref opaca; fbclid/gclid da URL **não** chegam ao ledger).
- Fencing por revisões de source/connection/profile/app + CAS de cursor + ownership
  de job + índice parcial de run ativo; I/O de rede sempre fora de locks; ordem de
  locks consistente (nenhuma inversão encontrada).
- Micros em BIGINT com `Decimal` exato; presença ≠ zero com CHECKs; A→B→A preservado
  em catálogo/fatos; página vazia não fabrica zero.
- Grafo de dependências sem ciclos; cores instaláveis isolados; facade Meta real.
- Testes com asserts sobre estado persistido e cenários de fencing sofisticados
  (rotação de perfil durante I/O, job órfão) — e int8 verificado via
  information_schema.

---

# Catálogo de ideias (com fact-check em fontes oficiais)

Contexto verificado: Google gastou R$ 22,5k/ano com **zero conversão primária**;
60-74% de impressões perdidas por ranking; LPs WordPress sem tag; Lead Ads entra por
n8n descartando IDs; 20 públicos Meta sem uso; 6.559 leads (UTM completa em 2,5% dos
ganhos); pedido médio R$ 33k; 1.495 pedidos / 357 ganhos ligados ao CRM em 45,6%.

## Correções de premissas (fact-check 2026-08-31, fontes oficiais)

1. **Explorer PODE mutar contas de produção** (pausar campanha, orçamento,
   negativas) — 2.880 ops/dia; bloqueios são só criação de contas/usuários/
   planning/billing. Basic: ~5 dias úteis de análise. → **H3.1 (mutações) é viável
   já com o token atual**, ao contrário do que se supunha.
2. O bloqueio de conversões offline pela Ads API é por **histórico do token** (sem
   atividade na janela 2025-12→2026-06), não por nível; **Data Manager API é o
   caminho: GA desde 2025-10, NÃO exige developer token**, aceita `gclid/gbraid/
   wbraid`, `transactionId`, consent, 100k req/dia. Ajustes/retratações continuam
   pela Ads API.
3. Customer Match: exclusões e Observation disponíveis com histórico de conformidade;
   **targeting pleno exige 90 dias + US$ 50k de gasto vitalício** (Soloz não tem) —
   priorizar **exclusão de clientes**, que já funciona.
4. Meta mudou os tiers em 2026-05: **Limited/Full** (Full = ≥500 chamadas em 15 dias,
   erro <15%). `ads_read`/`leads_retrieval`/`pages_*` exigem App Review + Business
   Verification.
5. **Conversion Leads**: `event_time` ≤ 7 dias; `user_data.lead_id` casa sozinho (sem
   e-mail/telefone hasheados); mas a **otimização** exige ~200 leads/mês e conversão
   em 28 dias — **volume que a Soloz talvez não tenha**: começar enviando os estágios
   (custo zero) e só prometer a otimização após medir volume.
6. **CAPI for Business Messaging existe** (`action_source="business_messaging"`,
   `ctwa_clid`) **mas exige WABA/Cloud API + dataset** — confirmado o bloqueio do
   WuzAPI/whatsmeow. Medir CTWA no Odoo (já funciona) ≠ devolver eventos à Meta.
7. Lead Ads: retenção ~90 dias; rate limit 200×24×leads90d por Page; **Leads Access
   Manager precisa atribuir o CRM** — item de checklist que costuma travar tudo.
8. **Ad Rules API da Meta existe** (`adrules_library`: PAUSE, CHANGE_BUDGET,
   NOTIFICATION...) → guardrail nativo como backstop do kill switch é viável (H3.3).
   Google não tem API de regras (só Scripts/UI).
9. Merchant Center **exige preço** — inviável para produto sob orçamento; PMax de
   leads **não precisa de feed**, e page feeds/custom business data cobrem DSA/PMax
   (H4.3 reformulada).
10. GA4 standard: 200k tokens/dia, 10 requests concorrentes — suficiente.

## O que já existe (mapa)

Ledger de atribuição + bridge CC (CTWA `ctwa_clid` já capturado); catálogo Meta
campaign→adset→ad→creative; Insights diários conta/campanha (impressões, cliques,
gasto); Lead Ads (rota, submission, touchpoint) na árvore nova; business events
CRM/Sale/Account + dashboard nos módulos novos; leitura real **bloqueada até existir
System User `ads_read`** (pendência nº 1 operacional).

## Ideias por horizonte (resumo priorizado; esforço P≤1sem, M=2-4sem, G>4sem)

**H1 — 30 dias, só leitura** (destrava com o reader Meta):
| Ideia | Valor | Esf. |
| --- | --- | --- |
| H1.1 Custo por conversa/lead REAL por anúncio (Insights × ledger CTWA/Lead Ads × CRM), com linha "não atribuível" e discrepância vs plataforma | Alto | M |
| H1.2 Alertas de anomalia: gasto >2× mediana 7d, campanha vencida, anúncio reprovado, gasto sem touchpoint | Alto | P-M |
| H1.3 Pacing de orçamento mensal (teto por source × projeção) | Médio-alto | P |
| H1.5 Relatório semanal automático (e-mail; carimbo de frescor obrigatório) | Alto | P-M |
| H1.9 **Monitor de cobertura de rastreio** (% leads com touchpoint, % CTWA com clid, % UTM válida) — teria detectado em dias a perda que durou um ano | Alto | P |
| H1.6 Reconciliação fatura×gasto (R$ 156k já lançados só agregados) | Médio-alto | P-M |
| H1.7 Google read-only mínimo no teto Explorer (4-6 GAQL/dia: conta/campanha/dia + impression share + QS) | Alto | M-G |
| H1.8 Diagnóstico de termos de busca → lista de negativas sugeridas (aplicação manual até H3) | Alto | P |
| H1.4 Fadiga de criativo (frequência/CTR 7d vs 28d; com 15 anúncios, mostrar intervalo, não veredito) | Médio | M |

**H2 — fechar o loop:**
- **H2.6 Eventos de negócio do funil (CRM/Sale/Account) + `first_human_response`
  derivado do ledger do CC** — 100% Odoo, sem gate externo, alimenta tudo abaixo.
  (Módulos novos já esboçam isso — auditar antes de confiar.)
- **H2.1 Lead Ads→CRM aposentando o n8n** (que grava nome de anúncio como campanha e
  descarta IDs) — dedupe por hash em janela + cutover com janela; pedir App
  Review/Business Verification **já** (prazos de semanas).
- **H2.2 Captura first-party no WordPress agora** + redirect `/m/r/<token>` — a
  causa-raiz do "zero conversão" do Google; pré-requisito de gclid p/ Data Manager.
- **H2.3 Conversões offline via Data Manager** (sem dev token!) em
  `lead_qualificado`/`proposta`/`pedido` — `validateOnly` primeiro.
- **H2.4 Meta Conversion Leads** (estágios do CRM por `lead_id`) — enviar sempre;
  otimização condicionada a volume (fact-check nº 5).
- H2.5 Públicos: **começar por EXCLUSÃO de clientes atuais** (viável hoje; lookalike
  precisa de ≥100 e targeting Google exige tier que a Soloz não tem; **não
  reaproveitar as 4 listas da agência** — proveniência desconhecida).

**H3 — operar:** H3.1 mutações com aprovação + kill switch (**viável com Explorer**,
começar por pausar/orçamento com teto), H3.4 negativas como change request, H3.3
guardrail nativo Meta (`adrules_library`) como backstop, H3.5 score ICP v0 por
regras (estado SE/S = 87% dos ganhos; estrutura fotovoltaica = 96% do valor), H3.7
alarme speed-to-lead (2.814 leads "não responsivos" — vale mais que qualquer lance),
H3.2 regras por CPQL/valor de pipeline (janelas 30-90d; com pedido de R$ 33k, olhar
valor, não CPL), H3.8 geo-holdout simples (**não** MMM — sem potência estatística).

**H4 — diferenciais B2B:** H4.1 atribuição por coorte ao ciclo longo (separar o
legado de 487 dias), H4.2 LTV por origem via Accounting (`payment_allocated`, não
cash), H4.4 CTWA como conversão principal do funil (medição já existe; retorno à
Meta bloqueado pelo WuzAPI — decisão de negócio: número dedicado na Cloud API?),
H4.3 page feeds/custom data p/ PMax-DSA (Merchant exige preço — descartado), H4.5
retenção/upsell por família comprada.

**Anti-ideias (não fazer):** CAPI de mensagens/catálogo WhatsApp sem Cloud API;
reaproveitar listas da agência; Test Events como sandbox; promover clique/page view
a conversão primária; rodar n8n e consumer em paralelo sem dedupe; click IDs em
campos do `crm.lead`; somar receita atribuída das plataformas com a do Odoo; MMM
nesta escala; aumentar verba antes de H2.2+H2.6; dados do portal do cliente para
anúncios sem base legal própria.

**Sequência recomendada:** (1) reader `ads_read` + pacote de olhos H1.1/2/3/5/9;
(2) H2.6 + H3.5/H3.7 (100% Odoo); (3) `meta_webhook_base`+Lead Ads aposentando o
n8n; (4) H2.2 no WordPress sem esperar o site Odoo; (5) loop de qualidade
H2.4b+H2.3 com policy de dados aprovada. Google read-only (H1.7) em paralelo.

**Perguntas ao dono (mudam a priorização):** orçamento mensal pós-VMX? o que é
"lead qualificado" e quem carimba? aceita número WhatsApp dedicado na Cloud API?
existe política de privacidade cobrindo públicos/conversões e encarregado nomeado?
quem opera e quem aprova verba? instala `sale_margin`? prioridade
estrutura/carport/componentes e estados atendidos? site Odoo tem data?

---

## Recomendação em ondas

**Onda 0 — hoje (processo):** TOPO-01 completo (commits + remotes + tags + deploy
com SHA e árvore limpa). Sem isso, qualquer outra correção pode evaporar.

**Onda 1 — antes de dados reais:** LGPD-01/02/03 (ciclo de vida tokenizado +
`retain_until` + política de links); DATA-01 (identidade do bridge; barato agora,
caro depois de popular); C05 se for conectar conta fora do Brasil; C03 residual
(headers de uso); consentimento no produtor Lead Ads (PLAN-02 — o dado está na API).

**Onda 2 — antes da fase Google:** extrair o runner neutro
(PLAN-07+MNT-01/02/04 — senão são ~900 linhas copiadas); C26 (tirar `graph_version`
da identidade do fato — senão o upgrade v27 duplica a tabela); C07 (testes de
concorrência reais); PERF-01/03/04; OPS-01/03/04/05 (watchdog×queue_job, retomar,
cron de saúde, paginação das fontes).

**Onda 3 — auditoria dedicada dos 11 módulos novos** (hoje só passaram pelas lentes
transversais) + migrações + `meta_webhook_base` + ACL Viewer/Analyst (PLAN-03).

## Anexo — rastreabilidade

- Árvore auditada: HEAD `b62b7ed` + worktree fingerprint `5d932a8d`
  (2026-09-01 14:56, 121 caminhos sujos).
- Verificação adversarial executada: C01 (3 lentes), C02 (3), C03 (3), C04 (1).
  Registro de clusters da 1ª rodada:
  `/tmp/.../scratchpad/mc-audit/findings_round1.json` (30 clusters).
- Fact-check de APIs: 12 itens confirmados em fontes oficiais
  (developers.google.com / developers.facebook.com / support.google.com),
  consultados em 2026-08-31.
- Não executado (registrar antes de tratar como completo): ORM dedicada, lente de
  migrações, lente meta_webhook_base, verificação adversarial de C05–C30 e da 2ª
  rodada, auditoria dos 11 módulos novos.
