# Cross-check da disposição da auditoria — marketing-center — 2026-09-01

- Fonte confrontada: `reviews/2026-09-01-independent-audit-disposition.md`
- Método: 6 verificadores céticos por bloco (correções de dados, testes de concorrência,
  correções operacionais, lote "já atendidos", refutações, fase Google) + verificações
  diretas do coordenador (git, versões, ACLs, evidência de release). Verificadores
  instruídos a localizar símbolos por grep e a julgar cada refutação pelo mérito.
- Nota de escopo: **LGPD foi retirada do escopo por decisão do proprietário** ("não se
  preocupar com LGPD neste momento") — os itens LGPD ficam registrados como adiamento de
  produto, sem contestação, a resolver antes de produção com dados reais (posição que a
  própria disposição também adota).

## Veredito

**A disposição é honesta, competente e — na maior parte — verificada como verdadeira
contra o código.** Das 9 correções alegadas, 8 estão implementadas corretamente (várias
com qualidade acima do pedido); das 12 "já atendidas", 8 conferem integralmente e 4 têm
lacunas materiais; das refutações, **5 de 6 procedem** — e uma não procede (META-06). A
evidência de release é real (tree hash `ce7f0217...` conferido, logs das 4 suítes
presentes, contagens exatas de base=112, website=89, suite=4).

Três fatos dominam o saldo:

1. **TOPO-01 continua integralmente aberto** — 0 commits novos, 0 remotes, árvores
   crescendo (marketing-center 128 sujos, contact-center 163). A disposição não o nega e
   o argumento (autoria/divisão de commits é decisão do dono; commit automático de
   árvore mista pioraria a revisão) é defensável — mas a refutação parcial do
   "indistinguível sem SHA" está corroborada: os manifests de hash do release tornam o
   conteúdo identificável. O que falta segue faltando: recuperação off-host e histórico.
2. **A fase Google nasceu — sólida e duplicada.** `google_api_base` está no padrão do
   Meta ou melhor (segredos por referência, `developer_token_ref`, transporte
   endurecido, 39 testes). Mas o runner de orquestração foi **copiado, não extraído**
   (F1): `marketing.center.google.sync.service` com 647 linhas, `_finish_failure`
   verbatim com strings trocadas, ~2.650 L de camada de orquestração vs ~2.700 do Meta.
   O risco PLAN-07/MNT-01/04 (~900 linhas por provedor) **se materializou**; um 3º
   provedor triplica o custo. Mitigantes reais: o Google consolidou tudo num único mixin
   (o Meta duplica por família) e o planejamento/fencing/páginas do base são
   genuinamente compartilhados.
3. **XC-nova (P2): convivência v1/v2 do bridge pode duplicar o ledger.** A correção do
   DATA-01 está certa para ocorrências novas (MAPPING_VERSION=2 com a chave canônica
   completa do CC, índice parcial, teste do cenário exato), mas um touchpoint histórico
   v1 que sofra enrichment/replay/backfill é reingerido sob v2 com `occurrence_ref`
   diferente → **cadeia canônica nova sem superseder a v1 → dupla contagem na view
   efetiva**. Não há migração nem supersessão. Aceitável só enquanto o backfill de
   atribuição não rodar fora do laboratório — registrar como pré-condição.

## Correções alegadas — vereditos

| Claim        | Veredito      | Essência verificada                                                                                                                                                                                                                                                 |
| ------------ | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| DATA-01 (v2) | ✅ com lacuna | Núcleo correto + teste 2-conversas; **lacuna v1/v2 acima (XC-nova)**                                                                                                                                                                                                |
| C05/DST      | ✅ correto    | Helper único `services/timezone.py` compartilhado pelos 4 pontos; testes Santiago/Havana/Beirut **validados contra tzdata real**; "data inexistente" = travessia da Linha de Data (comportamento correto). Resíduo: ramo ambíguo (fall-back) sem teste automatizado |
| C07/TST-02   | ✅ correto    | 3 testes de concorrência **real** (threads + `registry.cursor()` + commits + Events determinísticos), amarrados ao índice parcial, à unique de ocorrência e ao CAS — cada um falharia sem o mecanismo                                                               |
| DATA-05      | ✅ correto    | `invalidate_recordset` de todos os campos mutáveis após o `FOR UPDATE`; teste multi-thread prova o contrato sob REPEATABLE READ (SerializationFailure, não dado stale)                                                                                              |
| OPS-11       | ✅ correto    | `_reschedule_current_cursor` sob locks; loop impossível (posse por `queue_job_uuid` + sequência monotônica + identity_key); espelhado no Google com teste                                                                                                           |
| META-15/C30  | ✅ correto    | write compara valor persistido sob `FOR UPDATE`, com normalizações; espelho Google idem                                                                                                                                                                             |
| SEC-03       | ✅ correto    | `groups="base.group_system"` nos dois campos + guarda de servidor (verificado pelo coordenador)                                                                                                                                                                     |
| OPS-09       | ✅ correto    | Search view real + ação system-only com guard sem dependência de queue_job; lacuna menor: sem teste da ação                                                                                                                                                         |
| TST-05       | ✅ correto    | `Job.perform()` real do OCA (contexto `job_uuid` injetado pelo próprio queue_job); 7 testes = 4 Meta + 3 Google; caso "retry antigo × credencial rotacionada" existe                                                                                                |

## "Já atendidos" — vereditos

| Item            | Veredito                  | Nota                                                                                                                                                                                                                                                                                                                                                                                               |
| --------------- | ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| C01/C02/C04     | ✅ (já verificados antes) | Continuam íntegros                                                                                                                                                                                                                                                                                                                                                                                 |
| META-13/C18     | ✅                        | Reconciliação distingue pausa manual de pausa de sistema nos dois sentidos                                                                                                                                                                                                                                                                                                                         |
| C19             | ✅                        | `_apply_entity_page` com savepoint, simétrico ao de performance                                                                                                                                                                                                                                                                                                                                    |
| C25             | ✅                        | Compara com a revisão **corrente**; teste A→B→A afirma [1,2,3]                                                                                                                                                                                                                                                                                                                                     |
| C27             | ✅                        | Identidade congela após `_identity_has_evidence()`; testes nos 2 módulos                                                                                                                                                                                                                                                                                                                           |
| C28             | ⚠️ lacuna                 | É **propagação** de versão, não pin: nenhum `assert` contra valor esperado — bump do CC passaria mudando semântica em silêncio. Correção barata: `_EXPECTED_CC_SCHEMA = 1` + assert                                                                                                                                                                                                                |
| C22             | ⚠️ lacuna                 | Disposição `enriched`/`revised` correta e testada; **mas os flags de estado do CC continuam dentro do content_hash** (via extensions + revision_kind) → churn de revisões, limitado pela monotonicidade dos estados (~2 extras por touchpoint); sem teste E2E CC→MC                                                                                                                                |
| PERF-04 parcial | ⚠️ lacuna                 | Os 4 itens existem (lote, retry OperationalError, enqueue on-change, backfill priority 55>40). Lacunas: backfill de atribuição **sem chamador** (só shell), lote sem savepoint por item, zero testes novos. Risco novo a observar: o mapper roda 2× síncrono em TODO write de touchpoint do CC — uma validação não capturada abortaria o write de negócio do CC (mitigado por campos required)     |
| PLAN-03         | ⚠️ lacuna                 | Refutação parcial procede: Viewer **tem** superfície real de leitura roster-scoped (source/entity/metric/run/dashboard). Mas os dois pontos específicos seguem negados **por design testado**: effective touchpoints são admin-only (teste assevera AccessError p/ Viewer) e nenhum papel abaixo de Admin dispara sync. Se é o modelo pretendido, atualizar o plano; senão, os dois gaps persistem |
| SEC-06          | ✅                        | UTF-8 encode antes do compare_digest (GET) + regex hex antes do HMAC (POST), com teste de regressão unicode                                                                                                                                                                                                                                                                                        |

## Refutações — julgamento

| Refutação             | Veredito                 | Por quê                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| --------------------- | ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| PERF-01 rebaixado     | **PROCEDE**              | `last_sync_run_id` é **comprovadamente** usado para freshness/gating no dashboard (`dashboard.py:236-267`: `IS DISTINCT FROM applicable_run_id` → `partial`); remover o touch quebraria o contrato — a auditoria errou ao propor remoção simples. Ressalva: o risco em escala (63k UPDATEs não-HOT/dia no alvo) ficou registrado só na tabela de refutações, sem entrada de backlog nem gatilho de reavaliação                                                                                                                                                               |
| PERF-03 rejeição      | **PROCEDE**, com dívida  | `window_key` no índice permitiria janelas **sobrepostas** (não só disjuntas) tocando o mesmo fato → a rejeição da correção mecânica é certa. Mas o **planner sequencial prometido não existe na árvore**: a dor original (backfill de 2 anos = 24 janelas em série com clique humano) permanece integral                                                                                                                                                                                                                                                                     |
| C26 refutado          | **PROCEDE**, com resíduo | O dashboard **realmente** seleciona um único contexto por (source, dia) via `ROW_NUMBER` — nem duas linhas nem soma dobrada. O resíduo verdadeiro do achado permanece na TABELA: re-sync de histórico sob v27 duplica linhas dos dias re-observados, e consumidores fora do dashboard precisam replicar a seleção                                                                                                                                                                                                                                                            |
| META-12 superestimado | **PROCEDE**              | Bounded (8), savepoint, sanitização — tudo confere. Lacuna menor: bug determinístico morre como "permanent" sem nem a classe da exceção no log                                                                                                                                                                                                                                                                                                                                                                                                                               |
| MNT-19 rejeitado      | **PROCEDE**              | Os 7 fences têm semânticas distintas (CAS incremental, igualdade de uuid, pausa-sob-lock não colapsam num hash); ganho de performance seria nulo. O problema real que motivou o achado — fencing **clonado** entre Meta e agora Google — persiste e a melhoria correta é extrair helper compartilhado, não colapsar                                                                                                                                                                                                                                                          |
| META-06 intencional   | **IMPROCEDE**            | O princípio fail-closed é defensável, mas o mérito verificável do achado está intacto: pin **triplicado** em 3 lugares sem constante compartilhada (`insights.py:21`, `lead_ads.py:228-230` literal inline, `signature.py:5`), e o **catálogo segue o App enquanto Insights/Lead Ads pinam** — a divergência entre consumidores do mesmo App (núcleo do META-06/TOPO-06) continua; um bump para v27 degrada Insights com mensagens opacas/enganosas ("profile changed before execution") enquanto o catálogo funciona. Correção barata: constante única + mensagem explícita |

## Fase Google (nova, revisão rápida — F1–F4)

- **F2 ✅** `google_api_base` sólido: segredos só por referência (env/file com recusa de
  symlink/permissões), `developer_token_ref`, identidade de runtime não-serializável,
  `token_uri` pinado, Retry-After limitado, 39 testes.
- **F3 ✅** As correções espelhadas (OPS-11, META-15, TST-05) existem no lado Google com
  testes reais.
- **F1 ✗** Runner neutro **não** foi extraído — segunda cópia da orquestração (~647 L de
  mixin + famílias). Extrair **antes** do 3º provedor.
- **F4 ⚠️** Sem P0 aparente (sem rotas públicas, SQL parametrizado, sudo fenced,
  ACL/rules presentes); herda PLAN-03 (profile admin-only) e OPS-06 (canal sem
  capacity). ~5.900 linhas + 2 crons diários **sem auditoria dedicada** — Onda 3.

## Pendências consolidadas (pós-cross-check)

1. **TOPO-01/02/03/04/12** — commits/remotes/tags/pins/CI (decisão do dono; inalterado).
2. **XC-nova** — supersessão/migração v1→v2 do bridge antes de qualquer backfill de
   atribuição fora do lab.
3. **Planner sequencial de backfill** — prometido na rejeição do PERF-03, não existe (a
   própria disposição o lista como "próximo lote", corretamente).
4. **META-06/TOPO-06** — constante única de versão Graph + mensagem clara (refutação
   improcedente).
5. Runner neutro antes do 3º provedor (F1); auditoria dedicada do
   marketing_center_google e dos demais módulos novos (F4/Onda 3).
6. Backlog que a disposição já reconhece e o cross-check confirma como real: headers de
   uso da Meta (C03), tombstones (META-09/C15), diagnóstico code/subcode/fbtrace
   (META-10/OPS-02), health de credenciais + retomada (OPS-01/03/04), capacidade de
   canais (OPS-06), fixtures reais e testes de migração (TST-08/17). PERF-01 em escala:
   criar entrada de backlog com gatilho (ex.: ao passar de N contas), hoje está só na
   tabela de refutações.
7. LGPD — adiada por decisão do proprietário; pré-condição de produção com dados reais
   (posição compartilhada pela disposição).

## Rastreabilidade

- Estado no cross-check: marketing-center HEAD `b62b7ed` + 128 sujos; integration-core
  `2236af3` + 19; contact-center `885a1c2` + 163; nenhum remote.
- Release conferido:
  `scans/raw/20260901-odoo16-marketing-center-audit-remediation/ release/20260901T204613034659Z`
  — `tree_hash ce7f0217…` idêntico ao declarado; contagens exatas de base/website/suite;
  versões meta 16.0.2.0.2 e google 16.0.1.1.2 conferem nos manifests.
- Vereditos completos por claim no journal do workflow `wf_695f3cbc-122`.
