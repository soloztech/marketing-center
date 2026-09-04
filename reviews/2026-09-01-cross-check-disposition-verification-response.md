# Resposta à verificação da disposição do cross-check — 2026-09-01

Fonte confrontada: `reviews/2026-09-01-cross-check-disposition-verification.md`.

## Veredito

A verificação é correta para a árvore e o release que auditou. Ela confirmou de forma
reproduzível C28, META-06/TOPO-06, a contenção v1/v2, C05, OPS-09 e PLAN-03, além de
corrigir corretamente a contagem dos quatro crons Google.

As duas ressalvas sobre cobertura também procediam: o código de invalidação do cursor
estava correto, mas o teste antigo não isolava esse contrato; e ainda não havia dois
workers aplicando a mesma página. As duas lacunas foram fechadas nesta rodada para
catálogo e performance.

Um contra-check fresco encontrou três pontos que ainda não estavam fechados no snapshot
do documento:

1. uma revisão que nascia com `revision_kind=conflict` era classificada como `accepted`
   por não possuir revisão anterior;
2. o hook de `write()` do Contact Center executava o mapper opcional do Marketing
   sincronicamente antes e depois do write, permitindo que uma incompatibilidade futura
   de schema abortasse uma escrita válida do domínio de atendimento;
3. o backfill de atribuição executava um recordset inteiro na mesma transação, de forma
   que uma fonte histórica bloqueada podia desfazer todos os itens do lote.

Os três pontos procedem e foram corrigidos.

## Correções aplicadas

- conflito inicial continua registrado no ledger, mas recebe disposição `conflict` e
  nunca entra na projeção efetiva;
- o hook decide o enqueue somente pela interseção dos campos realmente mapeados; toda
  validação do DTO e do pin de schema acontece no job assíncrono;
- o backfill agora faz fan-out de um job idempotente por touchpoint, com transação,
  retry e falha isolados por item;
- o método batch antigo foi mantido como compatibilidade para jobs já persistidos, mas
  passou a apenas distribuir jobs individuais;
- instalações novas incluem o backfill de atribuição no `post_init_hook`;
- fontes v1 inequívocas, cadeias v1 colididas e fontes já divididas v1/v2 ganharam
  cobertura dirigida;
- enriquecimento do Contact Center ganhou prova E2E de uma única cadeia canônica e uma
  única projeção efetiva;
- foram adicionados testes determinísticos para os sete campos invalidados depois do
  lock e para duas transações concorrendo na mesma página de catálogo e de performance.

## Refutações e limites mantidos

- Não foi criada migração automática para a fonte histórica já dividida v1/v2. Escolher
  qual cadeia imutável preservar é uma decisão explícita de dados; uma regra genérica
  destrutiva não pode ser inferida de um único caso do laboratório.
- O documento dizia que o backlog estava “com donos”, mas não existem responsáveis
  nominais registrados para todos os itens. Os itens estão documentados e priorizados,
  não formalmente atribuídos.
- Resumir a prontidão para dados reais a `ads_read`, Git e dois itens de teste é
  insuficiente para escala. Health de credenciais, rate limit, justiça dos crons e
  capacidade do JobRunner continuam gates operacionais reconhecidos.
- Planner multipágina resumível, runner neutro para um terceiro provedor, headers de uso
  Meta, tombstones autoritativos e persistência de `code/subcode/fbtrace_id` permanecem
  backlog. Não eram defeitos necessários para corrigir esta verificação.
- A política LGPD/retenção permanece fora do escopo por decisão do proprietário.

## Validação e release

- qualidade local: Black, isort, flake8 dos addons, compileall e `git diff --check`;
- orquestrador: 4/4 testes Python;
- Odoo base: 117/117;
- Odoo integrado: 528/528;
- Odoo Website: 89/89;
- Odoo suite: 4/4;
- total: 738 testes, zero falhas e zero erros;
- versões: `marketing_center_base` 16.0.1.7.3 e `marketing_center_contact_center`
  16.0.3.0.2;
- árvore implantada: 335 arquivos, SHA-256
  `1f69e011e13105f18824550d4f2ee5f814cba344bfb30c1310bc6cc4057d9e0d`;
- upgrade e replay offline idempotentes concluídos;
- `https://odoo16-teste.soloz.com.br/web/login`: HTTP 200;
- produção não foi tocada e backup do laboratório continuou dispensado.

Evidência canônica:
`/home/lucaszotelli/infra-ai-ops/scans/raw/20260901-odoo16-marketing-center-cross-check-disposition-verification/release/20260902T020031310401Z`.
