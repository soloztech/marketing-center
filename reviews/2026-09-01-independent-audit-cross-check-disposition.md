# Disposição do cross-check da auditoria — 2026-09-01

- Documento confrontado: `2026-09-01-independent-audit-cross-check.md`
- Árvore verificada: estado consolidado após as alterações paralelas
- Ambiente implantado: somente `odoo16-teste.soloz.com.br` / servidor05

## Veredito

O cross-check é majoritariamente correto e encontrou lacunas reais na disposição
anterior. O contra-check direto do código aceitou C28, META-06 e a convivência v1/v2
como problemas relevantes. Também corrigiu duas afirmações excessivas do próprio
cross-check: ainda não existe teste de dois workers aplicando a mesma página e o teste
concorrente do cursor não isola especificamente a invalidação do cache posterior ao
lock.

As correções pequenas e seguras foram aplicadas e implantadas. Não houve reescrita nem
exclusão automática do ledger histórico.

## Correções aplicadas

| Item              | Disposição e implementação                                                                                                                                                                                                                                                         |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| C28               | **Procede; corrigido.** O bridge aceita explicitamente apenas o schema v1 do Contact Center. Uma mudança incompatível agora falha fechado, com mensagem clara e teste.                                                                                                             |
| META-06 / TOPO-06 | **Procede; refutação anterior retirada.** Catálogo, Insights e Lead Ads compartilham `META_MARKETING_GRAPH_VERSION = "v26.0"` e rejeitam versão divergente de forma explícita. O baseline genérico de `meta_api_base` continua separado do contrato funcional do Marketing Center. |
| XC-nova / DATA-01 | **Procede; contido prospectivamente.** Replay v1 inequívoco permanece na cadeia canônica histórica. Cadeia v1 colidida ou histórico já dividido entre v1/v2 falha fechado e exige migração explícita, em vez de criar nova dupla contagem silenciosa.                              |
| C05 / DST         | Foi incluído teste para meia-noite ambígua em `America/Havana`, escolhendo deterministicamente o primeiro instante UTC.                                                                                                                                                            |
| OPS-09            | A ação system-only passou a ter teste com um `queue.job` real de catálogo Meta e verifica o `res_id` exato aberto pela UI.                                                                                                                                                         |
| PLAN-03           | O plano foi alinhado ao contrato efetivo: Viewer lê superfícies roster-scoped, mas ledger/projeção efetiva continuam admin-only; disparar sync permanece operação de administrador.                                                                                                |

Diagnóstico read-only do laboratório antes da contenção v1/v2:

- links por versão: v1 = 166; v2 = 23;
- 165 fontes com link legado;
- nenhuma cadeia v1 compartilhada por fontes diferentes;
- uma fonte já possui simultaneamente v1 e v2 em cadeias canônicas distintas.

Essa única cadeia já dividida não foi alterada automaticamente. Ela fica bloqueada para
replay/backfill até uma migração diagnóstica própria decidir qual ocorrência supersede a
outra.

## Qualificações ao cross-check

- C07/TST-02 está **parcialmente** coberto. Existem testes concorrentes reais para
  criação de runs e lock do cursor, mas ainda não dois workers executando a mesma página
  e disputando o CAS de `_apply_*_page`.
- DATA-05 está corrigido no código: os sete campos mutáveis são invalidados após o
  `FOR UPDATE`. Porém o teste atual recebe `SerializationFailure` no próprio lock, antes
  da invalidação, e portanto não prova isoladamente essa regressão.
- C22 é dívida de contrato de baixo risco, não corrupção comprovada. Flags de
  enrichment/conflito ainda participam do hash e podem gerar revisões extras; falta um
  cenário E2E Contact Center → Marketing Center.
- PERF-04 permanece parcial: lote e retry existem, mas falta isolamento por item,
  continuação automática do backfill e teste específico do coordenador.
- O relatório cita dois crons Google; a árvore atual possui quatro crons ativos.

## Refutações que permanecem válidas

- PERF-01 não admite remoção simples de `last_sync_run_id`, porque o campo participa de
  freshness/ownership. O custo deve ser medido novamente quando o volume atingir um
  limiar definido.
- Incluir `window_key` no índice ativo de PERF-03 permitiria janelas sobrepostas. A
  solução continua sendo um planner sequencial, limitado e retomável.
- Remover `graph_version` da identidade de observação (C26) apagaria contexto
  reproduzível. Consumidores fora do dashboard ainda precisam aplicar a mesma regra de
  seleção de contexto.
- META-12 possui retry finito, transação e sanitização; cabe melhorar diagnóstico, não
  classificá-lo como loop infinito ou vazamento comprovado.
- Os sete fences de MNT-19 protegem autoridades diferentes. A dívida real é a duplicação
  de orquestração Meta/Google, a ser extraída antes do terceiro provedor.

## Backlog confirmado

1. migração explícita dos históricos v1/v2 já divididos ou colididos;
2. teste de dois workers na mesma página e teste dirigido à invalidação pós-lock;
3. planner sequencial/resumível e coordenador tolerante a falha por item;
4. runner neutro antes do terceiro provedor;
5. headers de uso Meta, tombstones, `code/subcode/fbtrace_id`, health periódico,
   retomada da página atual, justiça dos crons e capacidade do JobRunner;
6. topologia Git: remotes, histórico, tags/pins, CI e recuperação off-host;
7. C22 E2E e fixtures sanitizadas capturadas dos providers reais.

LGPD permanece fora do escopo deste ciclo por decisão do proprietário, mas continua
registrada como gate anterior ao uso produtivo com dados reais.

## Evidência canônica

Release aceito:

`/home/lucaszotelli/infra-ai-ops/scans/raw/20260901-odoo16-marketing-center-audit-cross-check/release/20260902T003626527291Z`

- status: `applied_and_validated`;
- árvore: 335 arquivos verificados local/remoto;
- SHA-256: `04dabe9d95f63b74586a10486b8a7ea0006e9fff5b3941a91edac924455365fe`;
- base: 113 testes; integração: 520; website: 89; facade: 4;
- total: 726 testes, zero falhas e zero erros;
- `marketing_center_base`: `16.0.1.7.2`;
- `marketing_center_contact_center`: `16.0.3.0.1`;
- `marketing_center_meta`: `16.0.2.0.3`;
- instalação offline e replay idempotente concluídos;
- Odoo e dbmanager ativos; HTTP privado e público 200;
- rota Traefik restaurada com o mesmo hash;
- produção não foi tocada.

Três tentativas anteriores terminaram `failed_recovered` enquanto o novo teste OPS-09
era ajustado. Em todas elas as fontes anteriores e a rota foram restauradas antes de
qualquer mudança de banco. Elas são evidência da recuperação segura, não releases
aceitos.
