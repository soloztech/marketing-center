# Disposição da verificação da resposta — 2026-09-02

Fonte confrontada:
`reviews/2026-09-02-verification-response-check.md`.

## Veredito

A verificação confirma corretamente a maior parte do release anterior e encontrou
duas correções de código pertinentes: a corrida de revisão canônica e a regressão
da prioridade do backfill. D5 e as três lacunas de teste também procedem.

A crítica à cobertura dos sete campos invalidados, porém, não procede. O documento
inspecionou o teste concorrente, mas ignorou
`test_locked_cursor_invalidates_every_preloaded_mutable_field` em
`marketing_center_base/tests/test_sync_service.py`. Esse teste pré-carrega os sete
campos no cache, altera a mesma linha por SQL na mesma transação, chama
`_locked_cursor` e exige os sete valores novos; remover `invalidate_recordset` o
faz falhar.

O contra-check ao vivo encontrou ainda uma falha histórica não mencionada no
documento: 363 jobs de response episodes falhavam ao tratar mensagens enviadas
pelo celular como se o recibo de delivery fosse a evidência da resposta.

## Correções aplicadas

- tráfego vivo de atribuição permanece em prioridade 40 e backfill voltou à
  prioridade 55;
- somente a `UniqueViolation` da constraint
  `marketing_attribution_touchpoint_canonical_revision_unique` vira
  `RetryableJobError`; violações de outras constraints continuam falhando de forma
  visível;
- validação de empresa no backfill usa `getattr(company, "_name", "")` e falha
  fechada para objetos inválidos;
- foram adicionados testes para write não mapeado sem enqueue, criação de
  identifier com enqueue vivo, conflito inicial via bridge e corrida da constraint
  canônica;
- o ledger de resposta passou a associar delivery somente a mensagens `agent`;
  mensagens `external_device` usam a própria mensagem/eco como evidência, conforme
  a constraint já exigia;
- foi adicionada regressão dirigida para `external_device` com delivery existente.

## Recuperação histórica validada

O release não alterou mensagens históricas. Depois de instalar o código, os 363
jobs falhos foram reencaminhados pelo ORM do OCA `queue_job` e processados pelo
JobRunner normal.

- 363 jobs reencaminhados; zero ativos e zero falhos ao final;
- 6.224 sinais históricos criados;
- 600 cursores de conversa convergidos;
- 3.070 sinais `external_device`, todos sem ponteiro indevido para delivery;
- zero mensagem `external_device` confirmada sem signal;
- atribuição permaneceu íntegra: 188 fontes, 191 links e zero fonte sem link.

## Itens mantidos como backlog deliberado

- D3 procede, mas requer um coordenador paginado/resumível para instalações com
  grande histórico. Não foi substituído por outro loop síncrono nesta correção.
- D4 procede como lacuna documental de upgrade. Não foi alterada uma migration já
  liberada nem inferida migração automática das cadeias v1/v2; o próximo bump que
  exigir remapeamento deverá incluir planner/migration explícito e auditável.
- resolução de `conflict_state` permanece um workflow de domínio futuro; conflito
  não entra silenciosamente na projeção efetiva.

## Validação e release

- Black, isort, flake8 e compileall: limpos;
- orquestrador local: 4/4;
- Odoo base: 117/117;
- Odoo integrado: 533/533;
- Odoo Website: 89/89;
- Odoo suite: 4/4;
- total somado: 743 execuções, zero falhas e zero erros; as suítes se sobrepõem e
  representam 591 métodos de teste distintos;
- versão instalada: `marketing_center_contact_center` 16.0.3.0.3;
- árvore implantada: 335 arquivos, SHA-256
  `432a4e85dba0aef1c4c60f53e5045c82adcf01e437b8a49b917d1570a64496e7`;
- upgrade e replay offline idempotentes;
- container saudável, sem `ERROR`, `CRITICAL` ou traceback após o release;
- HTTP público 200; produção não tocada; backup do laboratório dispensado.

Evidência canônica:
`/home/lucaszotelli/infra-ai-ops/scans/raw/20260902-odoo16-marketing-center-verification-response-check/release/20260902T023622492596Z`.
