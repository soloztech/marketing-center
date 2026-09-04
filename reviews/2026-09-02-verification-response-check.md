# Verificação da resposta — marketing-center — 2026-09-02 (madrugada)

- Fonte: `reviews/2026-09-01-cross-check-disposition-verification-response.md`
- Método: verificação direta do coordenador (símbolos, view SQL, campos, versões,
  release, compile) + 1 revisor adversarial lendo por inteiro os arquivos
  alterados da rodada.
- Árvore verificada: worktree 23:10 (release `20260902T020031310401Z`, tree
  `1f69e011…` conferido, `applied_and_validated`; base 16.0.1.7.3,
  contact_center 16.0.3.0.2 — manifests conferem; base com 117 `def test_`,
  exatamente o declarado; `compileall` limpo).

## Veredito

**O código ficou adequado.** As quatro alegações da resposta são verdadeiras na
substância e — ponto alto da rodada — quase todas provadas por testes que testam a
coisa certa. Os três defeitos que o próprio Codex encontrou no contra-check eram
reais e estão corrigidos com qualidade. A rodada também confirma o padrão do
ciclo: cada lado corrigindo os excessos do outro (desta vez, a resposta corrige
dois meus; esta verificação corrige dois dela).

## Alegações verificadas

| Alegação | Veredito | Evidência |
| --- | --- | --- |
| Conflito inicial → disposição `conflict`, nunca efetivo | ✅ correto e testado | `_revision_disposition` checa `conflict` **antes** do atalho `not previous` (`attribution_service.py:79-83`); projeção por **allowlist positiva** (`JOIN LATERAL ... disposition IN ('accepted','enriched','revised')`, `effective_touchpoint.py:96-103`) — sem linha, sem NULLs; revisão conflito mais nova não sombreia a aceita anterior (testado) |
| Hook do CC por interseção de campos; validação no job | ✅ correto | `MAPPED_TOUCHPOINT_WRITE_FIELDS` (17) confere **campo a campo** com o que `to_dto` lê (16/16; campos fora da lista não são lidos; merges que só tocam campos não-mapeados também escrevem `enrichment_state`, que está na lista → zero enqueue perdido); identifiers são create-only e o `create` deles enfileira o touchpoint — bridge não defasa; teste prova que o write do CC **nunca aborta** |
| Backfill: fan-out 1 job/touchpoint, batch como distribuidor, post_init | ✅ parcialmente | Fan-out idempotente (link por hash + ingest `duplicate`), batch distribui, post_init cobre install fresco. Ressalvas abaixo (prioridade, upgrade, D3) |
| Testes: mesma página ×2 workers (catálogo E performance), E2E enriquecimento, v1/v2 dirigidos | ✅ em grande parte | Mesma página é **concorrência real** (2 threads, cursores, commit, `SerializationFailure` real, CAS rejeitando com estado final conferido via SQL: `page_count=1`, `cursor_sequence=1`) — prova exatamente "não aplica 2×"; E2E: 2 revisões, 1 canonical_key, **1 linha efetiva** (`enriched`); v1/v2 dirigidos ok |
| Validação no job → e o erro? | ✅ desenho correto | `AttributionDTOValidationError` → job `failed` **visível** com traceback, requeuável; não retentar erro de schema é certo. OPS-07 mitigado, não eliminado (sem alerting do canal) |

## Correções desta verificação à resposta (2 alegações superestimadas)

1. **"Testes determinísticos para os sete campos invalidados depois do lock" —
   não isola o que nomeia.** O teste (`test_sync_concurrency.py:548-605`) prova o
   protocolo `SerializationFailure`→retry; a releitura pós-lock ocorre em
   transação nova com cache vazio — **remover o `invalidate_recordset` não faria
   o teste falhar**. Nota técnica: sob REPEATABLE READ o invalidate pós-lock é
   cinto-e-suspensório quase inalcançável (commit concorrente vira
   SerializationFailure antes); o código está certo, a alegação de cobertura não.
2. **A prioridade de backfill sumiu na refatoração.** A disposição anterior tinha
   backfill em 55 < tráfego vivo 40; o fan-out novo enfileira **tudo a 40**
   (grep: só 40/41/42/43 no módulo) — backfill volta a competir de igual com o
   tráfego vivo. Regressão pequena e fácil de restaurar.

## Defeitos novos encontrados pela revisão adversarial

| ID | Sev | Defeito |
| --- | --- | --- |
| **D1** | **Médio** | Corrida REPEATABLE READ no ingest de atribuição: dois jobs do mesmo touchpoint (live write durante backfill; identity_key não dedupa job `started`) → o 2º acorda do advisory com snapshot velho, tenta a mesma `revision_sequence` → `UniqueViolation`, que **não** é `OperationalError` e não é capturada (`attribution_bridge.py:150-153`) → job `failed` onde um retry resolveria. O planner já trata exatamente essa classe (`sync_service.py:311-329`); o ingest, não. Correção barata: capturar `UniqueViolation` das constraints conhecidas → `RetryableJobError` |
| D3 | Baixo | `post_init_hook` enfileira TODO o histórico das 3 pontes numa única transação de install (`hooks.py:8-27`) — ok no lab; em produção merece lotes/throttle |
| D4 | Baixo | Migração 16.0.3.0.0 não enfileira o backfill de atribuição em **upgrades** (só install fresco via post_init) — defensável (hooks v1 já enfileiravam; remap v1→v2 é lazy), mas não documentado |
| D5 | Baixo | `_enqueue_backfill` da atribuição usa `company._name` direto (quebra com input não-recordset), divergindo do padrão `getattr` das outras pontes |
| D6 | Info | Faltam: teste negativo do hook (campo não-mapeado ⇒ sem job), teste do enqueue via create de identifier, E2E do caminho conflito-via-bridge |

Observação de design registrada (não é defeito): `conflict_state` nunca volta a
`clean` no CC e o mapper prioriza conflito — um touchpoint conflitado fica
**permanentemente** fora da projeção efetiva, sem workflow de resolução. Decisão
consciente; documentar quando houver o primeiro caso real.

## Refutações mantidas na resposta — todas procedem

Sem migração automática do caso v1/v2 dividido (decisão de dados explícita — certo);
"backlog com donos" era excesso **meu** (itens documentados e priorizados, sem
responsáveis nominais — aceito); resumo de prontidão a `ads_read`+git+2 testes era
estreito **meu** (health de credenciais, rate limit, justiça de crons e capacidade
do JobRunner são gates igualmente reais — aceito).

## Estado do ciclo

Fechado no código com qualidade crescente a cada rodada. Fica para a próxima
rodada de código: **D1** (única correção de código recomendada antes de volume),
restauração da prioridade de backfill, e os itens de backlog já concordados
(planner resumível, runner neutro, headers Meta, tombstones, diagnóstico
code/subcode, health periódico, git/remotes — o nº 1 de sempre). LGPD segue fora
do escopo por decisão do proprietário, como gate de produção.
