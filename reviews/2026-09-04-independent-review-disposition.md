# Disposição crítica da revisão independente — fechamento greenfield

Data: 2026-09-04 Escopo primário: `meta_api_base`, `meta_webhook_base`,
`google_api_base` Insumo: parecer estático anexado à sessão de 04/09 Estado: claims
confrontados com a árvore final; baseline sem migrations `applied_and_validated`; nenhum
novo defeito P1/P2 aberto no Integration Core

## Veredito executivo

O parecer é útil, mas não representa integralmente o estado atual. Ele identificou três
problemas que existiram durante o desenvolvimento — lock exclusivo no fluxo Meta,
seleção injusta/contexto incompleto nos conectores de Marketing e fallback incorreto no
clique de WhatsApp — e os apresentou como se ainda estivessem abertos. Os três já foram
corrigidos na árvore corrente.

Dois alertas permanecem pertinentes:

1. ainda falta uma política cross-repo de retenção e privacidade para ledgers;
2. componentes extensos e SQL complexo exigem disciplina de evolução e benchmark, mas
   tamanho por si só não demonstra defeito.

Portanto, o parecer não exige uma nova mudança de runtime no Integration Core. Ele gera
documentação de não regressão, mantém retenção/LGPD como gate produtivo e exige prova de
escala para o dashboard.

## Matriz de disposição

| Claim do parecer                                                                      | Disposição                                              | Estado e ação                                                                                                                                                                                                                                                                                                                                |
| ------------------------------------------------------------------------------------- | ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Lock de banco durante I/O Meta bloqueia challenge/webhook                             | **Válido historicamente; não procede no desenho atual** | O antigo `FOR UPDATE` foi substituído por advisory lock por Endpoint entre workers e `FOR SHARE` para provar propriedade/configuração. GET/POST público também usa leitura compartilhada, portanto é compatível. Manter como teste de regressão.                                                                                             |
| Remover todo lock durante a chamada externa                                           | **Refutado**                                            | Isso abriria TOCTOU: credencial, política ou revisão poderiam mudar durante o efeito remoto e o resultado seria projetado sob outra configuração. O fence compartilhado é intencional.                                                                                                                                                       |
| Crons de Marketing sempre escolhem os primeiros 25/50                                 | **Válido historicamente; corrigido**                    | Os conectores usam seleção rotativa/justa e atualizam sua fronteira de observação. O tema pertence ao Marketing Center, não a este core.                                                                                                                                                                                                     |
| Lead Ads perde contexto de empresa                                                    | **Válido historicamente; corrigido**                    | Jobs são reancorados na empresa proprietária com `allowed_company_ids` exato.                                                                                                                                                                                                                                                                |
| Falha da atribuição pode redirecionar clique de WhatsApp para `/`                     | **Válido historicamente; corrigido**                    | O fallback preserva o `href` original validado e same-origin; telemetria falha aberta sem sacrificar a conversão.                                                                                                                                                                                                                            |
| Métodos/arquivos grandes provam que o Contact Center precisa de decomposição imediata | **Parcial**                                             | Há concentração real e ela aumenta o custo de revisão. Não é prova de bug nem justificativa para split cosmético. A dependência reversa Base→UI e o acoplamento WuzAPI foram corrigidos; permanecem políticas de plataforma WhatsApp no Base, deliberadas hoje e candidatas a registro quando outra plataforma exigir semântica equivalente. |
| Ledgers sem retenção crescerão indefinidamente                                        | **Procede**                                             | É gate real de capacidade e LGPD. Requer desenho explícito antes de produção.                                                                                                                                                                                                                                                                |
| View SQL de cerca de 550 linhas é risco de desempenho                                 | **Hipótese válida, defeito não demonstrado**            | O arquivo corrente tem 764 linhas; tamanho não equivale ao custo do plano. No laboratório, `EXPLAIN (ANALYZE, BUFFERS)` foi documentado em 18,414 ms com 310 métricas, 229 touchpoints efetivos, 40 resolutions e 3.945 eventos. Exigir nova medição com cardinalidade produtiva e orçamento definido.                                       |

## Âncoras verificadas na árvore final

- `meta_webhook_base/models/subscription_service.py::_job_reconcile_endpoint` usa
  advisory lock por Endpoint e comprova a propriedade do job sob `FOR SHARE`;
  `meta_webhook_base/models/endpoint.py::_locked_runtime`, `_lock_fence` e
  `_lock_active_policy` compõem o fence compartilhado usado também pelo ingresso.
- `marketing_center_base/services/scheduler.py::fair_scheduler_batch` mantém cursor
  persistente e round-robin. Google, Meta Catalog e Lead Ads o consomem com chaves
  independentes; os jobs de Lead Ads reancoram o ambiente com `allowed_company_ids` da
  rota proprietária.
- `marketing_center_website/static/src/js/action_capture.esm.js::whatsappFallbackPath`
  preserva o destino original validado; `action_bootstrap.esm.js::claimWhatsApp` aplica
  o fallback nos erros e respostas transitórias.
- `contact_center_base/models/onboarding.py::_contact_center_inbox_action` é o hook
  neutro e `contact_center_ui/models/onboarding.py` contém o override do XMLID da UI,
  eliminando a dependência reversa.
- As políticas ainda específicas de plataforma estão em
  `application.py::_COMPANY_PORTABLE_IDENTITY_NAMESPACES`,
  `identity.py::_contact_center_format_whatsapp_pn`,
  `identity_avatar.py::_IDENTITY_AVATAR_NAMESPACE_PRIORITY`,
  `ui_api.py::_suggested_phone` e `account.py::_contact_center_display_address`. Elas
  não importam WuzAPI, mas também não constituem abstração multicanal genérica.

## Por que o desenho atual de locks Meta é correto

Há três objetivos de concorrência diferentes, e tratá-los como um único “lock durante
rede” produz a conclusão errada:

1. **Deduplicar workers do mesmo Endpoint.** Um `pg_advisory_xact_lock`, namespaced pelo
   ID do Endpoint, serializa apenas a reconciliação de subscriptions. Um job duplicado
   acorda, relê o UUID e termina antes de repetir o I/O.
2. **Manter propriedade e revisão estáveis.** A linha do Endpoint e a App são lidas sob
   `FOR SHARE`. Escritas de configuração aguardam, impedindo misturar o efeito remoto
   iniciado na revisão A com a configuração B.
3. **Manter ingresso público disponível.** Challenge e webhook assinado também fazem
   leituras compartilhadas. Como `FOR SHARE` é compatível com `FOR SHARE`, não são
   bloqueados pela reconciliação como eram sob o antigo `FOR UPDATE`.

Mover toda a chamada para fora de qualquer fence resolveria um bloqueio já resolvido ao
custo de reintroduzir corrida de configuração. A combinação atual delimita a
exclusividade ao worker e deixa o callback concorrente.

A cobertura atual impede a regressão estrutural em
`test_subscription_worker_uses_webhook_compatible_ownership_lock`, mas esse teste
inspeciona o SQL; ainda não há um teste com duas transações que suspenda o transporte e
prove o callback concorrente de ponta a ponta. É uma lacuna de evidência, não um defeito
reproduzido. Além disso, subscription, delivery e dispatch usam o mesmo canal
`root.meta_webhook`; um ensaio de capacidade deve verificar se I/O lento de
reconciliação atrasa o fan-out, mesmo sem bloquear a persistência HTTP.

## Retenção e LGPD: ação ainda aberta

Imutabilidade lógica não significa preservação eterna do conteúdo pessoal. A solução
deve separar, por classe de registro:

- payload e identificadores pessoais apagáveis;
- hash/prova mínima necessária para dedupe, replay e auditoria;
- base legal, prazo e `retain_until`;
- `legal_hold` que suspenda o expurgo;
- purge paginado, idempotente, multiempresa e observável;
- comportamento de projeções, replays e diagnósticos depois do purge.

Não foi aplicado `unlink()` genérico porque isso destruiria exatamente as garantias que
os ledgers oferecem e poderia introduzir duplicação em replay. Este item precisa de
contrato cross-repo antes de implementação.

Há retenção parcial, não ausência total: Marketing já modela `retain_until` em
identificadores, e admissões/grants efêmeros possuem GC; Contact limpa uploads
temporários e URLs privadas Meta. Ainda faltam prazo, legal hold e purge para os ledgers
centrais. Em particular, `contact_center_meta/models/shared_webhook_consumer.py` copia
`meta_webhook_item.payload_json` para `contact.center.inbox.event.raw_envelope_json`; o
contrato deverá coordenar as duas cópias para não apagar uma e conservar PII na outra.

## Complexidade e dashboard

Contagem de linhas é um sinal para revisão, não uma métrica de correção. Dividir um
método transacional apenas para reduzir LOC pode esconder lock, savepoint ou ordem de
efeitos em helpers separados e tornar o sistema menos auditável. A regra adotada é
extrair responsabilidades quando houver coesão clara e testes que preservem as
invariantes, especialmente ao tocar funcionalmente o fluxo.

O mesmo vale para o dashboard: reescrever uma view por tamanho não melhora o plano de
execução. A medição documentada de 18,414 ms afasta urgência no volume do laboratório,
mas não certifica produção. O plano bruto do `EXPLAIN` não foi localizado nos artefatos
canônicos, portanto o número é uma observação registrada, não uma evidência
reexecutável. O gate correto é um dataset representativo, `EXPLAIN (ANALYZE, BUFFERS)`
preservado, índices observados, p95/p99 e orçamento de latência.

## Claims gerais e limite da evidência

Os elogios à separação de addons, ausência de ciclos e postura defensiva procedem. O
grafo corrente tem 23 manifests, 37 arestas internas e nenhum ciclo; os providers
dependem do Base, UI/CRM são consumidores opcionais e os três cores de integração não
dependem de Contact ou Marketing.

As contagens do parecer, contudo, misturavam momentos ou escopos. No inventário corrente
são 441 arquivos Python, 104 XML e 24 JavaScript, com 1.636 métodos Python `test_*` e
160 testes QUnit. Assim, “mais de 1.500” permanece verdadeiro; 104 XML e 24 JS conferem;
502 Python e 159 QUnit não descrevem a árvore final. AST Python e parse XML estão
limpos. Black, isort e flake8 passam no escopo configurado do pre-commit. Os cinco
`setup.py` de scaffold, normalmente excluídos pelos hooks, também foram verificados
diretamente; quatro foram normalizados pelo padrão Black e os cinco agora passam Black,
isort e flake8. Assim, o escopo Python conhecido desta árvore está coberto pela prova,
sem depender da exclusão dos scaffolds.

O fallback seguro possui teste direto do helper em
`marketing_center_website/static/tests/action_capture_tests.esm.js`, mas ainda não há um
QUnit que exercite `claimWhatsApp` ponta a ponta sob 429, 503 e falha de rede. O
controle de fluxo atual confirma a correção; o teste faltante é hardening de regressão
de baixa prioridade.

Para o Integration Core, o inventário autoritativo desta árvore é 45 + 73 + 42 = 160
métodos `test_*`. O release de laboratório executou os 160 já sobre os 79 arquivos da
árvore final sem migrations, com hash
`cec6ebd0d95a8797430163be30e1c25f834cb4de7532cc2e65eca463d766b3af`, apply e replay zero,
HTTP 200 e `production_touched: false`. A evidência está em
`scans/raw/20260903-odoo16-integration-core-greenfield-closeout/release/20260904-final-no-migrations/summary.json`.

## Baseline greenfield

As migrations pré-produtivas de `meta_webhook_base` foram removidas. Os três addons não
podem conter diretório `migrations/` no contrato greenfield corrente, e o release
codifica essa condição. O baseline inicial é composto diretamente pelas versões atuais
dos manifests; bases de desenvolvimento anteriores não são uma linhagem produtiva
suportada.

Este documento substitui a orientação de preservá-las contida em revisões
intermediárias. A regra atual não deve ser reinterpretada como autorização para apagar
histórico após o primeiro go-live: qualquer política futura de evolução de schema/dados
exigirá decisão explícita e contrato versionado.
