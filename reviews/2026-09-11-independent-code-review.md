# Revisão independente de código — marketing-center

- Data: 2026-09-11
- Revisor: Claude (quatro leituras por lente + verificação pontual final)
- Método: quatro revisores independentes, cada um com uma lente (núcleo e pontes de
  negócio; integrações e segurança; ingresso web e site; processo, testes, documentação
  e o catálogo em voo), com acesso ao código local, aos documentos do repositório e ao
  código-fonte do Odoo 16 e da OCA no GitHub. Os achados de maior peso foram conferidos
  pessoalmente no código; estão marcados como *verificado*. Não houve verificação
  adversarial por achado.
- Escopo: os 21 addons do repositório (18 da suíte analítica e fundações + 3 do catálogo
  não rastreado), ~74k linhas de Python incluindo ~30k de testes, JS dos addons de
  website e catálogo, CI, docs e `reviews/`.
- Árvore observada: branch `16.0` em `0a62448`; catálogo em
  `release/content-catalog-20260911` (`8093b88`, local), idêntico aos arquivos não
  rastreados no working tree. Peers: `contact-center` `61fdce5`, `soloz-industrial`
  `695c93a`.
- Não houve acesso ao banco nem aos servidores; o estado de produção vem de incidentes e
  scans sanitizados de 08–09/09. Nenhum arquivo do repositório foi alterado além deste.

## 1. Parecer

**Está bem construído, acima da média do ecossistema Odoo/OCA em rigor de engenharia.**
Ledgers append-only com token de processo, idempotência por chaves naturais com
reversões exatas, concorrência tratada com locks advisory e testes com transações reais,
segredos fora do banco com resolução defensiva, HMAC sobre bytes brutos, ACL somente
leitura em ledgers e regras por empresa e roster, ~814 testes comportamentais e uma
cultura de evidência incomum (41 reviews com disposições verificáveis).

Os problemas não são de qualidade de execução. São de quatro tipos:

1. **Promessas que ainda não rodam.** O motor de atribuição (first/last-touch) não tem
   produtor em produção e o dashboard não lê resultado algum.
2. **Semânticas de métrica que enganam um gestor.** Re-postagem de fatura duplica
   receita; funil conta transições; arquivar vira perda; dia de empresa em UTC.
3. **Operação e conformidade.** Canais de fila sem capacidade sob `root:1` em produção
   (marketing pode degradar o atendimento); PII em claro sem retenção; tracking sem
   consentimento; loop do WhatsApp aberto.
4. **Processo.** QUnit citado como evidência e nunca executado na CI; documentação
   desatualizada e contraditória; catálogo de 9,3k linhas construído em um dia, em
   branch local sem CI, com pin do peer diferente do validado.

## 2. Pontos fortes (verificados)

- Append-only real: mixins exigem token `is` no `create` e levantam `AccessError` em
  `write`/`unlink`, testado sob `sudo()`
  (`marketing_center_base/models/attribution.py:56-75`;
  `marketing_center_base/tests/test_attribution_security.py:62-71`). Nenhum ACL
  concede escrita em ledger (`marketing_center_base/security/ir.model.access.csv`).
- Idempotência e reversões: chaves por ocorrência, valores em `numeric(21,0)` micros,
  índice único parcial garantindo uma reversão por original
  (`marketing_center_base/models/business_event.py:15-18, 164-171`;
  `services/business_event_service.py:146-219`).
- Concorrência: `pg_try_advisory_xact_lock` + `SerializationFailure`
  (`marketing_center_base/services/serialization.py:21-37`), cercas MVCC com no-op
  UPDATE, watermark de `mail.tracking.value`
  (`marketing_center_sale/services/service.py:453-466`); testes com threads e cursores
  reais em `base`, `meta`, `web_ingress` e `website`.
- Segredos: apenas referências a env/arquivo, com `O_NOFOLLOW`, rejeição de symlink/FIFO,
  checagem de modo e tamanho (`meta_api_base/services/credentials.py:333-418`;
  `google_api_base/services/credentials.py:75-235`); runtime `frozen`, `repr=False`,
  `__reduce__` que lança; campos de referência com `groups="base.group_system"`. Args de
  job só com revisões/cursores/hashes.
- Webhook Meta: `routing_key` aleatória e imutável, sem fallback para chave desconhecida,
  `Content-Length` obrigatório e limitado a 2 MiB, HMAC-SHA256 sobre bytes brutos antes
  do `json.loads` com `compare_digest`, dedupe por `(endpoint, sha256)`, colisão
  concorrente tratada por retry de serialização
  (`meta_webhook_base/controllers/webhook.py:127-144, 196-294`;
  `meta_api_base/services/signature.py:53-95`).
- Ingresso web: `Origin` em allowlist, corpo limitado, JSON sem chaves duplicadas,
  allowlist de 20 campos, respostas opacas com CSP restritiva, nenhum cookie próprio,
  hash de sessão apenas (`marketing_center_web_ingress/controllers/ingress.py:80-191`;
  `marketing_center_website/services/contracts.py:222-360`).
- As correções declaradas na disposição da auditoria de 01/09 (SEC-03, SEC-06, META-06,
  TST-05, C07) estão implementadas.
- CI com peer privado pinado por SHA, importação da pilha TLS antes dos testes,
  `manifestoo` para licenças e status, asserção de que `contact_center_kanban` não é
  instalado (`.github/workflows/test.yml:47-107`).

## 3. Achados — alta

### A1 — `invoice_posted` duplica receita em re-postagem (*verificado*)
`marketing_center_account/models/account_move.py:107-133`: `_post` emite um evento com
sequência nova sempre que um movimento sai de não-postado para postado. Não existe
override de `button_draft` em nenhum addon do repositório. Postar → rascunho → repostar
gera dois `+valor` sem reversão. Vendas tem o par `order_confirmed`/`order_cancelled`
(`marketing_center_sale/services/service.py:203-300`); Contabilidade não. O dashboard
conta `invoice_posted_count = COUNT(*)` (`marketing_center_dashboard/models/dashboard.py:434`).
O teste existente valida o oposto (cada re-postagem gera ocorrência nova).
**Correção:** reversão em `button_draft` no mesmo padrão de `order_cancelled`, ou, no
mínimo, contadores por `COUNT(DISTINCT source_res_id)`; teste que reprove o
double-count.

### A2 — Motor de atribuição sem produtor (*verificado*)
`marketing_center_base/services/attribution_calculation_service.py:370-420` implementa
first, last, linear e `platform_reported` com alocação exata em micros. Fora dos testes,
nenhum caller de `_calculate`/`_register_model`; o dashboard não referencia
`attribution.result`/`contribution`. O que roda é a projeção de vínculos
touchpoint↔lead (`marketing_center_crm/models/links.py:286-345`), não crédito.
ARCHITECTURE.md promete "atribuição e métricas" (`:85, :100`).
**Correção:** job por `won`/`invoice_posted` que monte candidatos a partir de
`marketing.attribution.crm.effective.link` e chame `_calculate`, expondo
`attributed_amount` — ou retirar o motor do release até existir.

### A3 — Canais de fila sem capacidade sob `root:1` em produção (*verificado*)
`meta_webhook_base/data/queue_job.xml:357-360`, `marketing_center_meta/data/queue_job.xml:130-133`,
`marketing_center_google/data/queue_job.xml:107-110`,
`marketing_center_meta_crm/data/queue_job.xml:49-52` declaram canais filhos de `root`
sem `capacity`. Produção roda o runner com `ODOO_QUEUE_JOB_CHANNELS=root:1` no serviço
principal com 2 workers HTTP (`odoo16/incidents/2026-09-08-contact-center-official-install.md:54, 273`).
Subcanais sem capacidade herdam `None`: um único job simultâneo para webhook, sync de
marketing e fan-out de mensagens do Contact Center. Uma página de Insights/Google
(timeout 20–30 s) atrasa o atendimento; só as prioridades separam.
**Correção:** declarar capacidade por canal e registrar a topologia como contrato
operacional no README/runbook.

### A4 — LGPD: tracking sem consentimento e PII sem retenção
- Ingresso web: o evento é enviado no primeiro pageview com `consent_state: "unknown"`
  (`marketing_center_website/static/src/js/landing_bootstrap.esm.js:139`), gravando
  `gclid`/`fbclid` em claro (`click.value.protected_value`) e hash de sessão; não há
  caminho para anexar a decisão depois e `retain_until` existe sem nada que o aplique
  (`marketing_center_web_ingress/models/event.py:180`;
  `marketing_center_base/models/attribution.py:53-55, 193`).
- Lead Ads: campos privados em claro, imutáveis, sem tombstone/expurgo
  (`marketing_center_meta/models/lead_ads.py:946, 969-973`). LGPD-01 continua como
  "decisão de produto adiada" (`reviews/2026-09-01-independent-audit-disposition.md`).
**Correção:** gate de consentimento no JS + `consent_state` real no servidor; cron de
retenção/pseudonimização; tombstone tokenizado preservando `values_sha256` (mesmo padrão
de `_erase_consumer_message_content` em `meta_webhook_base/models/delivery.py:579-652`).

### A5 — Loop do WhatsApp aberto e site não instrumentado
O handoff cria touchpoint `organic_link` e redireciona para `wa.me/<número>` sem código
opaco (`marketing_center_website/controllers/website_action.py:307`); a conversa que
nasce no Contact Center só carrega `entry_point_*`/`utm_*` do referral Meta
(`contact-center/contact_center_base/models/attribution.py:204-219`) e não é joinável à
sessão web — contra o master-plan do site (`odoo16/website/master-plan.md:284-285`).
`soloz_website` não usa `data-marketing-form-action`/marcador de WhatsApp nem declara
`google_recaptcha` (`soloz-industrial/soloz_website/__manifest__.py`).
**Correção:** código opaco derivado do grant em `wa.me?text=`, parseado pelo Contact
Center na primeira mensagem; marcadores nos formulários e CTAs do `soloz_website` com
dependência explícita de `marketing_center_website_crm`; reCAPTCHA como dependência.

### A6 — QUnit nunca roda na CI (*verificado*)
`marketing_center_website`, `marketing_center_catalog` e
`marketing_center_catalog_contact_center` declaram `web.qunit_suite_tests`, mas não há
nenhum `HttpCase` com `browser_js`/`/web/tests` no repositório; `oca_run_tests` não
aciona a suíte JS. Todos os números de QUnit citados (`plan.md:2745-2747`,
`README.md:94`, `catalog-plan.md` §9) são execuções locais. O patch de `web.ajax.post`
(`marketing_center_website/static/src/js/action_capture.esm.js:255-280`) e o widget
`catalog_x2many` que substitui o `ArchParser` do form
(`marketing_center_catalog/static/src/js/catalog_x2many.js:81-84`) só são cobertos por
esses testes.
**Correção:** um `HttpCase` com `browser_js('/web/tests?module=…')` por addon com JS.

## 4. Achados — média

### Núcleo e métricas
- **M1 — Funil conta transições.** `won`/`qualified` são emitidos a cada
  `lead_stage_changed` para estágio ganho/semântico
  (`marketing_center_crm/services/service.py:619-634`); A→Ganho→A→Ganho = 2 `won`.
  `lost` = qualquer `active True→False` (`marketing_center_crm/models/crm_lead.py:139`):
  arquivar vira perda. Dashboard expõe como `won_count/lost_count`
  (`dashboard.py:422-427`). Sem testes para os dois casos.
- **M2 — Dia de empresa em UTC.** Linhas de fonte usam o tz da fonte
  (`dashboard.py:191-209`, testado), mas leads, won, faturas e respostas fecham o dia em
  UTC (`dashboard.py:171-179`; resumo declara `timezone = 'UTC'`, `:582`). Para
  America/Sao_Paulo o dia fecha às 21h.
- **M3 — Moedas misturadas no ledger.** `invoice_posted` em moeda do documento,
  `payment_allocated` em moeda da empresa (`marketing_center_account/services/service.py:436`);
  sem conversão, somas cross-moeda são impossíveis (o dashboard só conta).
  `occurred_at` de fatura é `now()` ao vivo e `invoice_date` 00:00 UTC no backfill
  (`account_move.py:129`; `service.py:380-386`).
- **M4 — Contagem de episódios respondidos pode somar +1** quando a resposta de
  conversa e a do episódio 1 divergem
  (`marketing_center_dashboard/models/dashboard.py:420-421`;
  `marketing_center_contact_center/models/response_episode.py:1128-1147`).
- **M5 — Retries infinitos.** Todo `with_delay` dos bridges usa `max_retries=0`
  (infinito no OCA, `queue_job/job.py:538`) e converte `OperationalError` em
  `RetryableJobError` (`marketing_center_contact_center/models/attribution_bridge.py:202-205`,
  `lifecycle_bridge.py:126-130`, `response_episode.py:655-658`). Sem teto nem
  dead-letter, um problema persistente vira loop horário. Contraste: `meta_webhook_base`
  tem teto aplicativo de 8 tentativas (`delivery.py:22, 236-257`).
- **M6 — Acoplamento a internals do Contact Center.** Import de token privado
  (`response_episode.py:10-12, 52-58`), import de função de módulo
  (`marketing_center_contact_center_crm/models/crm_lead.py:3`), SQL cru sobre
  `contact_center_message_binding`/`contact_center_delivery_event`/`mail_message`
  (`response_episode.py:767-890`; `lifecycle_bridge.py:187-269`) e criação de índice em
  tabela do CC a partir do `init()` do MC (`response_episode.py:128-134`). Os hooks
  públicos (`_contact_center_apply_delivery`, `_before_tombstone`,
  `_prepare_conversation_deletion_dependencies`) são o modelo certo; faltam
  equivalentes para deleção e lock do grafo.
- **M7 — Duplicação interna.** O mixin imutável + token é copiado 8 vezes e há ~20
  tokens em 6 arquivos `tokens.py`; `TRANSITION_GUARD` de sale/account só é setado em
  testes; dependência `utm` declarada e não usada; nenhum `display_name` humano nos
  ledgers. O touchpoint do CC é re-ingerido como touchpoint do MC (dois ledgers
  imutáveis para o mesmo fato), justificado pela neutralidade de provedor.
- **M8 — Caminhos síncronos custosos.** `crm.lead.create` ingere síncrono por lead
  (~8 queries + 2 locks; `crm_lead.py:78-91`); backfill de até 100×500 tracking rows
  síncrono (`:327-344`); view `attribution.effective.touchpoint` com window function
  sobre toda a tabela consultada a cada lead
  (`marketing_center_base/models/effective_touchpoint.py:90-103`;
  `marketing_center_website_crm/models/service.py:398-405`).

### Integrações
- **M9 — Diagnóstico Meta pobre.** `fbtrace_id` nunca capturado
  (`meta_api_base/services/graph.py:313-327`); `provider_code/subcode` existem na
  exceção mas nenhum consumer persiste; só a classe da exceção vai ao log
  (`meta_webhook_base/models/delivery.py:262-267`); headers `X-App-Usage`/
  `X-Business-Use-Case-Usage` ignorados (só `Retry-After`, `graph.py:74-79`). META-10 e
  C03 seguem abertos. Google faz melhor (`request-id` persistido,
  `google_api_base/models/google_service.py:348, 610, 631`).
- **M10 — Sem revalidação periódica de credenciais Meta.** Expirações gravadas
  (`marketing_center_meta/services/meta_service.py:334-337`) e nunca revisitadas; cron
  só de catálogo/insights/lead (`marketing_center_meta/data/sync_cron.xml`). OPS-04
  aberto.
- **M11 — Ingressos sem rate limit por IP.** Web ingress: admissão global por
  endpoint+tipo (1200/min; `marketing_center_web_ingress/models/admission.py:93-158`),
  um bot derruba visitantes reais com 429 e enche tabelas imutáveis sem purga; o código
  delega ao proxy (`endpoint.py:86-88`) e não há `limit_req` evidenciado na infra.
  Webhook Meta: custo por POST não autenticado inclui leitura de até 2 MiB, resolução
  do segredo e HMAC.
- **M12 — `sessionStorage` por aba** (`landing_bootstrap.esm.js:12, 22-36`): nova
  aba/app-browser = sessão nova; entry_point perde o lead; first-touch impossível por
  design. Família de produto não capturada; landing do `form_submission` é o
  `source_path` configurado, não a página real
  (`marketing_center_website/services/action_service.py:273`); sem dedup de lead.

### Processo e documentação
- **M13 — Documentação divergente do código.** `README.md:75-76` diz que os dezoito
  addons estão em `16.0.1.0.0`, mas `marketing_center_contact_center` e
  `meta_webhook_base` estão em `1.0.1` desde `3591efa`. `plan.md` parou em 04/09,
  contradiz a si mesmo na Fase 7 (`:2053-2057` vs `:2458-2466`), cita versões
  inexistentes após o reset de 05/09 e lista addons "previstos" que não existem sem
  marcá-los como futuros. `reviews/2026-09-05-greenfield-audit.md:46` diz que a CI
  instala `contact_center_kanban`; `25861b7` inverteu isso. `oca_dependencies.txt`
  aponta branches por URL pública, não é consumido pelos scripts oca-ci e diverge do
  SHA do workflow.
- **M14 — i18n ausente.** Só o catálogo tem `pt_BR.po`; os 18 restantes não têm
  tradução; duas strings em português hardcoded no SQL/XML do dashboard
  (`dashboard.py:575, 649`; `dashboard_views.xml:557, 568`); notificações do composer em
  pt-BR (CC) e inglês (ponte) na mesma tela.
- **M15 — Cobertura desigual.** `google` sem testes de concorrência; `crm` com a menor
  razão testes/código do núcleo (0,38); nenhum teste HTTP para 404/411/413/415/409/503
  do webhook; nenhuma fixture capturada do provedor (TST-17 aberto).
- **M16 — ARCHITECTURE.md do working tree** acrescenta o catálogo sem reconciliar "14
  componentes", sem explicar por que ele vive aqui (a justificativa está só em
  `catalog-plan.md` §5 e em `soloz-industrial/docs/plans/2026-09-11-central-conteudo-mvp.md`),
  cria raiz de menu contra a própria regra (`:212`) e passa a ter dois "Catálogo" no
  mesmo produto (`:181` vs "Catálogo de Conteúdo").

## 5. Achados — baixa

- `webhook_url` computado sem `groups` a partir de `routing_key` (system)
  (`meta_webhook_base/models/endpoint.py:72, 92`): hoje o admin de marketing recebe
  `AccessError` ao abrir o form; se o compute virar sudo, vaza a chave (SEC-05).
- Grupo do bridge implica `group_contact_center_admin` inteiro
  (`marketing_center_contact_center/security/marketing_center_contact_center_security.xml:10-11`) (SEC-04).
- `input_token` na query string de `debug_token` (restrição da API Meta,
  `graph.py:511-518`); manter `urllib3` fora de DEBUG.
- Rotação de segredos não documentada (OPS-08); jobs do ingresso nascem com `user_id`
  NULL e rodam como superuser do runner (`queue_job/job.py:668`).
- `config` do website buscado em todo pageview com `no-store`; `Sec-Fetch-Site` vazio
  aceito; UTM livre ≤ 512 chars imutável.
- `_table = "marketing_attr_cc_link"` foge da convenção
  (`marketing_center_contact_center/models/attribution_bridge.py:28`).
- Copier: `test.yml` fortemente customizado (conflito em `copier update`); 12 addons sem
  `readme/` nem `static/description/index.html`; `.esm.js` inconsistente no catálogo com
  override de eslint; isort não aplicado em
  `marketing_center_catalog_contact_center/tests/test_catalog_bridge.py:4-5`.
- `mail` declarado e não usado no manifest do catálogo; `catalog-plan.md` §8/§9 com
  versão e contagens de testes diferentes dos manifests e da árvore.

## 6. Catálogo em voo (`marketing_center_catalog` + duas pontes)

Construído em 11/09 entre 11:39 e 17:15 (9.352 linhas, commit `8093b88` em branch
local). Segue a diretriz v0.2 de `soloz-industrial/docs/plans/2026-09-11-central-conteudo-mvp.md`
("sem validação/aprovação"), um escopo **menor** que o corte de MVP da revisão de
11/09 (`soloz-industrial/docs/plans/2026-09-11-central-conteudo-revisao.md` §7).

**Sólido para o escopo escolhido:** `company_id` obrigatório com produtos compartilhados
ou da mesma empresa por constraint (`catalog_subject.py:183-196`;
`catalog_item.py:248-260`) e igualdade estrita com `contact_center_company_id`
(`marketing_center_catalog_contact_center/models/ui_api.py:37, 58, 79`), testado com
duas empresas ativas; arquivo privado com sniffing de conteúdo, rotas próprias
`auth=user` via `ir.binary._find_record`, CSP `sandbox`, SVG/HTML forçados a download
(`marketing_center_catalog/controllers/catalog.py:9-57`;
`tests/test_catalog_http.py:68-101`); ponte de atendimento pelo caminho existente
`store.addDraftAttachment` com URL autenticada e revalidação por requisição, gerando
cópia independente pela fila normal, sem nenhuma mudança no core do Contact Center;
ponte Vendas só consulta (30 linhas).

**Riscos:**
- **`access_token` não fechado (*verificado*).** Não há override de
  `generate_access_token` nem de escrita em `public`/`access_token` para anexos do
  catálogo; um Editor (write no item ⇒ `attachment.check('write')`) pode gerar token por
  RPC e tornar `/web/content/<mestre>` público. Só o estado inicial é testado
  (`tests/test_catalog.py:245-247`).
- Renúncia deliberada a estado de publicação, bytes congelados e recibo
  (`catalog-plan.md` §1, §5): decisão do proprietário, mas ainda não registrada em
  ARCHITECTURE/README com a razão.
- Validado localmente contra `contact-center` `1ef3743` (`contact_center_ui` 16.0.1.3.0)
  enquanto a CI pina `1e6fb3d` (16.0.1.2.0); provavelmente compatível, não provado. O
  push em `release/content-catalog-*` não dispara a CI (`test.yml:9-15`).
- Widget `catalog_x2many` substitui o `ArchParser` do form (`catalog_x2many.js:81-84`)
  — customização de framework coberta só por QUnit que a CI não executa.
- Working tree da `16.0` com três addons e docs não rastreados: propenso a acidente de
  checkout/clean.

## 7. O que eu faria primeiro

1. Reversão de `invoice_posted`/`credit_note_posted` em `button_draft` + teste de
   double-count; contadores do dashboard por registro distinto (A1, M1).
2. Capacidade por canal no jobrunner de produção, registrada como contrato operacional;
   teto finito de retries nos bridges com estado `dead` visível (A3, M5).
3. Consentimento e retenção: gate no JS, `consent_state` real, cron honrando
   `retain_until`, tombstone para PII de Lead Ads (A4).
4. Produtor de atribuição ou retirada do motor do release (A2).
5. QUnit na CI; PR do catálogo para `16.0` com `PEER_REF` atualizado; override de
   `generate_access_token` nos anexos do catálogo com teste de GET anônimo (A6, §6).
6. Fechar o loop do WhatsApp com código opaco no `wa.me?text=` e instrumentar o
   `soloz_website` (A5).
7. Reconciliação documental: README (versões), plan.md (Fase 7, entradas de 05–10/09 ou
   congelamento declarado), ARCHITECTURE ("por que o catálogo aqui", colisão de nome),
   `oca_dependencies.txt`, strings pt no dashboard, `fbtrace_id`/code/subcode
   persistidos e cron de saúde de credencial (M9, M10, M13, M14).

## 8. Disposições anteriores — situação verificada

| Item (auditoria 01/09) | Declarado | No código |
| --- | --- | --- |
| SEC-03 refs de credencial editáveis | corrigido | sim (`marketing_center_meta/models/meta_profile.py:78-89, 259-266`) |
| SEC-06 `compare_digest` não-ASCII | corrigido | sim (`meta_webhook_base/controllers/webhook.py:71-79, 221`) |
| META-06 versão Graph hard-coded | corrigido | sim (`marketing_center_meta/services/graph_contract.py:242-262`) |
| TST-05 teto de retry simulado | corrigido | sim (`meta_profile.py:420-457`) |
| C07 concorrência real | corrigido | sim (`tests/test_lead_ads_concurrency.py:170-282`) |
| C03 headers BUC/backoff | backlog | aberto |
| META-10 `fbtrace_id`/code/subcode | backlog | aberto |
| OPS-04 saúde de credencial | backlog | aberto |
| OPS-06 capacidade de canais | backlog | aberto (A3) |
| OPS-08 rotação documentada | backlog | aberto |
| SEC-04 grupo do bridge ⇒ CC Admin | sem exploração | inalterado |
| SEC-05 `webhook_url` sem `groups` | — | não corrigido |
| LGPD-01/05 | adiado | LGPD-01 aberto; LGPD-05 tem apagamento por consumer |
| Native-first B7 (LGPD ingresso) | gate de produção | aberto (`event.py:180`) |
| Native-first B8 (dashboard `depends: [base]`) | — | mitigado por testes, não redesenhado |

## 9. Limites

Quatro leituras independentes, sem verificação adversarial por achado. Verifiquei
pessoalmente: A1 (`_post` e ausência de `button_draft`), A2 (ausência de callers de
`_calculate` e de leitura de resultados no dashboard), A3 (ausência de `capacity` nos
`data/queue_job.xml`), A6 (ausência de `browser_js`), o `access_token` do catálogo
(ausência de override), a existência da diretriz v0.2 e do cross-check da revisão de
11/09. As afirmações sobre o core do Odoo 16 e do OCA `queue_job` vieram das lentes,
conferidas contra o GitHub por elas, e não foram reverificadas por mim. Severidades são
minhas: alta = dado/segurança/disponibilidade ou promessa não cumprida; média = escopo,
esforço ou semântica; baixa = higiene.
