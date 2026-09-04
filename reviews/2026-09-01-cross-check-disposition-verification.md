# Verificação da disposição do cross-check — marketing-center — 2026-09-01 (noite)

- Fonte: `reviews/2026-09-01-independent-audit-cross-check-disposition.md`
- Método: verificação direta no código atual, símbolo a símbolo, pelo coordenador.
- Árvore verificada: worktree de 22:40 (release aceito `20260902T003626527291Z`,
  tree `04dabe9d…` conferido; base 16.0.1.7.2, contact_center 16.0.3.0.1,
  meta 16.0.2.0.3 — manifests conferem).

## Veredito

**Todas as correções alegadas existem e estão corretas — e esta disposição eleva o
padrão do ciclo:** o Codex retirou a própria refutação anterior (META-06) diante da
evidência, aceitou o achado novo (v1/v2) com uma contenção melhor que a proposta, e
**corrigiu com razão duas afirmações excessivas do meu próprio cross-check**. Com
isso o ciclo auditoria → disposição → cross-check → disposição está fechado no
código, restando apenas o backlog reconhecido por ambos os lados.

## Correções verificadas

| Item | Verificado no código |
| --- | --- |
| **C28** | Pin real agora: `SUPPORTED_CONTACT_CENTER_ATTRIBUTION_SCHEMA_VERSIONS = (1,)` + `_contact_center_attribution_schema_version()` falhando fechado com mensagem clara (`mapper.py:1-31`), com teste. Era exatamente a lacuna apontada ("propagação, não pin") |
| **META-06/TOPO-06** | Refutação retirada e corrigido: novo `services/graph_contract.py` com `META_MARKETING_GRAPH_VERSION = "v26.0"` único, `require_marketing_graph_version(value, consumer)` com mensagem acionável ("Upgrade every Marketing Center Meta adapter before changing the App version"), importado por **catálogo, Insights e Lead Ads** — os três consumidores agora movem juntos. A separação do baseline do `meta_api_base` está documentada e é defensável |
| **XC-nova (v1/v2)** | Contenção prospectiva **melhor que a recomendada** (`attribution_bridge.py:240-310`): replay de cadeia v1 inequívoca **retém a occurrence legada** (sem dupla contagem, evidência registra mapping v2); cadeia colidida → `ValidationError` "explicit collision migration"; fonte já dividida v1/v2 → `ValidationError` "explicit bridge migration"; checagem extra de compatibilidade de identidade. Testes: `test_unambiguous_v1_replay_stays_on_the_legacy_canonical_chain` e `test_collided_v1_chain_requires_explicit_migration`. Diagnóstico do lab declarado (166 v1 / 23 v2 / 1 fonte dividida, bloqueada) — não verificável daqui, coerente com o desenho |
| **C05 ambíguo** | `test_ambiguous_midnight_chooses_the_earliest_utc_occurrence` com `America/Havana` (`test_performance_dto.py:113+`) — fecha a lacuna de teste do ramo fall-back |
| **OPS-09** | Teste real: viewer negado + ação abrindo o `queue.job` com `res_id` exato (`test_meta_service.py:119-121+`) |
| **PLAN-03** | Plano alinhado ao contrato efetivo (`plan.md:1390-1391`): Viewer lê superfícies roster-scoped; ledger/projeção efetiva admin-only e sync como operação de administrador viram **decisão documentada**, não divergência |

## Qualificações da disposição ao meu cross-check — ambas procedem

1. **C07 "coberto" era excessivo**: os 3 testes concorrentes reais existem
   (criação de runs, ocorrência, lock do cursor), mas **não** há dois workers
   disputando o CAS de `_apply_*_page` na mesma página — consistente com a própria
   evidência do meu verificador, que listou os três cenários sem esse quarto.
2. **DATA-05 "teste prova o contrato" era excessivo**: o teste recebe
   `SerializationFailure` **no próprio lock**, antes da invalidação — prova o
   comportamento sob REPEATABLE READ, não isola a regressão da invalidação. O
   código está correto; a cobertura dirigida fica no backlog.
3. Correção factual adicional aceita: `marketing_center_google` tem **4 crons**
   ativos (`data/sync_cron.xml`), não 2 como meu bloco F reportou.

## Corroborações numéricas

- Release: diretório existe, `tree_hash 04dabe9d…` idêntico, status
  `applied_and_validated`, logs das suítes presentes.
- Contagens: base declara 113 no release; a worktree tem 114 `def test_` — os
  arquivos de teste têm mtime (21:44) posterior ao release (21:36), consistente
  com um ajuste pós-release, não com divergência.
- As 3 tentativas `failed_recovered` anteriores estão declaradas como evidência de
  recuperação, não como releases — postura correta.

## Estado do backlog (consolidado e concordado pelos dois lados)

1. Migração explícita dos históricos v1/v2 divididos/colididos (1 caso no lab, bloqueado).
2. Teste de dois workers na mesma página + teste dirigido à invalidação pós-lock.
3. Planner sequencial/resumível de backfill; coordenador tolerante a falha por item.
4. Runner neutro antes do 3º provedor.
5. Headers de uso Meta, tombstones, code/subcode/fbtrace, health periódico,
   retomada de página, justiça dos crons, capacidade do JobRunner.
6. **Topologia Git (remotes/histórico/tags/CI/off-host) — segue o item nº 1 de
   risco e segue sendo decisão do proprietário.**
7. C22 E2E e fixtures reais sanitizadas.
8. LGPD — fora do escopo por decisão do proprietário; gate antes de produção com
   dados reais.

Com isso, todos os achados de código do ciclo estão **fechados ou formalmente
aceitos como backlog** com donos e condições claras. O que resta antes de dados
reais é: reader Meta `ads_read` provisionado, itens 1–2 do backlog conforme uso, e
a decisão de processo do git.
