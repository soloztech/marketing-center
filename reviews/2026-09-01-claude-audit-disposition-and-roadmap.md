# Disposição da auditoria independente e roteiro de consolidação

Data: 2026-09-01 Escopo: `marketing-center`, contratos consumidos de `contact-center` e
runtime Meta compartilhado. Este documento descreve o candidato local; implantação no
servidor05 só é considerada concluída depois dos gates Odoo e do release registrado.

## Veredito

A auditoria encontrou problemas reais de concorrência, precisão, migração e
manutenibilidade. A arquitetura provider-neutral não precisa ser reescrita: DTO, ledger
imutável, adapters e bridges continuam sendo as fronteiras corretas. O ajuste necessário
é transformar a fundação técnica em uma projeção gerencial semanticamente verdadeira,
sem confundir revisão física, correlação M:N e crédito de atribuição.

## Achados aceitos e tratados no candidato local

- códigos Meta `80000..80014` são transitórios/rate-limit, não falhas permanentes;
- cursor de catálogo/Insights é reiniciado de forma explícita em nova varredura;
- páginas de catálogo e performance aplicam projeção, cursor e sucessor atomicamente;
- cancelamento e watchdog serializam com a aplicação por `FOR UPDATE`; o watchdog
  considera progresso (`write_date`), e o cancelamento respeita ACL/record rules;
- crons diários enfileiram catálogo Meta e sete dias fechados de Insights por fonte;
- fronteiras diárias usam o primeiro instante civil válido e tratam gaps/ambiguidades de
  timezone, inclusive overflow em `date.max`;
- touchpoints preservam A→B→A e classificam observação, enriquecimento, correção e
  conflito; a identidade do bridge Contact Center usa a chave canônica completa;
- business events validam matriz classe/tipo, pares de reversão e preservam o snapshot
  divergente da observação;
- valores de business events deixam de depender de `float`: o canônico é inteiro
  escalado por 1.000.000 em PostgreSQL `NUMERIC`, com float apenas para exibição;
- extensão exige namespace segmentado (`dominio.campo`), sem segmentos vazios;
- CRM registra criação/transições semânticas, usa ocorrência monotônica, tem backfill
  completo e uma empresa de ledger estável mesmo para lead global;
- o glue Contact Center–CRM materializa correlação M:N idempotente nos dois sentidos,
  inclusive quando a conversa é resolvida depois da projeção do touchpoint;
- os scripts de release passam a empacotar/testar base, bridge CC, CRM, glue e Meta.

## Achados aceitos que continuam como gate do corte

- criar uma projeção efetiva de touchpoint: uma ocorrência canônica deve contar uma vez,
  enquanto conflitos e revisões continuam preservados no ledger;
- resolver asset refs para `marketing.center.source`/entidade e manter estados
  `resolved`, `unresolved`, `ambiguous` e `unsupported` explícitos;
- executar instalação limpa, testes integrados, upgrade, replay e smoke no servidor05;
- validar credencial Meta Ads dedicada com `ads_read` em discovery, catálogo e Insights
  reais antes de chamar o conector de operacional;
- emitir `conversation_started` e `first_human_response` no bridge do Contact Center;
- criar a primeira visão gerencial read-only de cobertura/freshness, custo, clique,
  touchpoint efetivo, conversa e lead.

## Refutações e qualificações

- não será permitido validar leitor Ads apenas com `ads_management`; `ads_read` é a
  capability mínima explícita do reader;
- versões Graph futuras não são aceitas automaticamente: versão suportada faz parte do
  fencing e do contexto reproduzível do fato;
- versão Graph não é removida do contexto de relatório enquanto puder alterar a
  semântica ou o formato observado;
- backfill não será paralelizado em múltiplas janelas antes de medir quota e JobRunner;
  a primeira implementação é sequencial, bounded e retomável;
- código Meta `368` não é classificado genericamente como rate-limit sem subcódigo;
- associação M:N conversa↔lead↔touchpoint é correlação/candidato, não crédito causal;
  atribuição requer política/modelo posterior;
- PII/LGPD permanece backlog por decisão do proprietário e não bloqueia este lab, sem
  alterar o princípio de não copiar payload bruto para o ledger gerencial.

## Ordem de desenvolvimento consolidada

1. fechar projeção efetiva, resolução de source e release reproduzível;
2. instalar CRM/glue, emitir lifecycle do atendimento e validar Meta real/backfill;
3. entregar dashboard operacional de cobertura e linha explícita não atribuível;
4. concluir Lead Ads via `meta_webhook_base` e, se necessário, ingresso first-party;
5. adicionar Sale e Accounting para proposta, pedido, fatura e alocação de pagamento;
6. implementar Google read-only; só então chamar o conjunto de MVP multicanal;
7. depois: atribuição causal, conversões outbound, mutações e automação controlada.
