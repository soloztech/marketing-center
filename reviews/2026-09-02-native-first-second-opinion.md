# Segunda opinião — plano de adaptação native-first (2026-09-02)

> **Addendum:** este documento avaliou a versão anterior do plano. Depois da incorporação
> dos bloqueadores, a direção e a matriz de ownership tornaram-se normativas, mas escrita
> UTM e cutover continuam bloqueados. A disposição vigente está em
> `../../contact-center/reviews/2026-09-02-native-first-roadmap-and-ux-disposition.md`.

- Data: 2026-09-02
- Origem: disposição de segunda opinião entregue pelo operador ao Claude nesta data;
  registrada aqui para não permanecer apenas no chat. Spot-checks de código no anexo
  foram executados de forma independente pelo Claude na árvore local.
- Alvo: `reviews/2026-09-01-native-first-adaptation-plan.md`.

## Veredito

**Aceitar com alterações obrigatórias.** A direção híbrida/native-first é correta,
mas o plano ainda não deve virar normativo nem autorizar escrita UTM/cutover.
Pode avançar apenas com **ADR, inventário, correções isoladas e shadow em
homologação**.

## Bloqueadores principais

1. **A cadeia atual realmente ignora o controller de `website_crm`.** O controller
   genérico herda diretamente de `website.controllers.form.WebsiteForm`
   (`marketing_center_website/controllers/website_action.py`); o bridge CRM herda
   esse controller (`marketing_center_website_crm/controllers/website_form.py`) e nem
   depende de `website_crm` no manifesto. Ficam fora do MRO os hooks nativos de
   normalização e associação visitante–lead do OCB 16. Correção: dependência
   explícita em `marketing_center_website_crm`, controller final contendo a classe
   nativa no MRO, `@http.route()` e `super()` cooperativos, comprovados por
   `HttpCase`.
2. **`link.tracker.click` não representa "um clique físico".** O Odoo deduplica por
   `(link_id, ip)`, sem janela temporal, constraint SQL ou lock. Isso colapsa cliques
   repetidos/NAT e permite duplicação concorrente. Os critérios "um clique físico"
   das Fases 2 e 5 devem virar: **"para cada clique nativo elegível, o Marketing
   Center não cria um segundo fato canônico."** `link_tracker` deve continuar
   opcional; para uso puramente nativo o `website_links` já funciona como glue
   auto-instalável; correlação customizada justifica bridge separado.
3. **`first_known_acquisition` não corresponde à semântica UTM nativa.** Os três
   cookies UTM são atualizados independentemente por 31 dias; o mixin lê cada cookie
   e pode criar registros por nome automaticamente — visitas parcialmente tagueadas
   podem formar tuple híbrida. Substituir por **`first_trusted_assignment`**: tuple
   atômica, autoridade comprovada, primeiro assignment aceito congelando a projeção
   operacional.
4. **UTM nativo é global, não multiempresa.** `utm.campaign/source/medium` não têm
   `company_id` e os nomes são globais. O mapping company-scoped controla qual
   relação aplicar, mas não torna o cadastro UTM privado. O plano precisa assumir
   formalmente "taxonomia global compartilhada, sem nomes confidenciais" ou aceitar
   customização mais profunda. O mapping também precisa de revisão imutável,
   vigência, precedência, índices únicos parciais, fencing e receipt com snapshot.
5. **A propagação CRM → Venda → Financeiro é apenas parcial.** O botão padrão do CRM
   transfere UTMs à cotação, mas associar `opportunity_id` posteriormente não
   transfere. Pedido copia UTM para fatura, porém faturas agrupadas
   (empresa/parceiro/moeda) podem combinar pedidos de UTMs diferentes conservando
   uma única tuple; pagamentos não herdam UTM da fatura. Documentos financeiros são
   canônicos para valor/estado, mas a atribuição deve usar causalidade
   pedido–linha–fatura–reconciliação ou o ledger — nunca a tuple da fatura isolada.
6. **Fill-only e rollback ainda não são seguros.** Faltam: assignment/receipt com
   preimage, after-image, policy e mapping revision; lock ou CAS atômico com um único
   write da tuple; override/tombstone persistente para edição ou limpeza humana;
   fencing de jobs e flags; parcial sem proveniência comum tratado como conflito.
   **Feature flag é kill switch, não rollback.** Upgrade/backfill exige backup
   restaurável, restore testado e compensação que só reverta o valor se ele ainda
   for exatamente o aplicado (gates do `AGENTS.md` raiz).
7. **Bloqueador de LGPD/retenção.** `protected_value` é `Char` recuperável e não
   pode ser apagado (`marketing_center_web_ingress/models/event.py`); valores de
   Lead Ads também são imutáveis (`marketing_center_meta/models/lead_ads.py`);
   `website.track` guarda URL completa, que pode conter click IDs. "Histórico
   intacto" não pode impedir expiração, erasure ou crypto-shred autorizado. Fechar
   purpose, `retain_until`, legal hold, expurgo e ACL **antes** do cutover.
8. **Fase 4 e baseline ainda não são executáveis.** O dashboard deveria consultar
   CRM/Sale/Account mas depende apenas de `marketing_center_base` e é view SQL —
   escolher dependências obrigatórias/adapters; record rules dos modelos subjacentes
   não são herdadas pela view. A árvore interna está em commit dirty/untracked e o
   fingerprint `32cfa6…` não informa algoritmo/escopo/manifesto: antes da aprovação
   normativa deve existir commit/tag recuperável, hash do OCB implantado e manifesto
   reproduzível.

## Respostas às 12 questões do plano

| # | Disposição |
|---|---|
| 1 | Parcialmente. A separação é boa, corrigindo autoridade de clique, UTM global, finanças e tracking limitado. |
| 2 | Manter touchpoints em `marketing_center_base`, reduzindo produtores e tipos redundantes, não o histórico. |
| 3 | Manter eventos consumidos pela atribuição ou que preservem ocorrência/valor/reversão. Eventos puramente duplicadores de estado só com consumidor explícito. |
| 4 | Sim, mapping genérico no base; aplicação/receipt no CRM. Exigir invariantes e aceitar formalmente a taxonomia UTM global. |
| 5 | Opcional. Usar `website_links` nativo; criar bridge próprio apenas para correlação customizada. |
| 6 | Não como escrito. Precisa assignment atômico, lock/CAS, manual lock e fencing versionado. |
| 7 | Não. O comportamento específico do controller `website_crm` não está na cadeia atual. |
| 8 | Substitui formulário/UTM/pageview Odoo e observação aproximada de links. Não substitui externos/headless, click IDs, referrer, WhatsApp, webhooks, snapshots ou M:N. |
| 9 | Fatos realizados vêm dos documentos. Business Events ficam para atribuição temporal, ocorrência, replay, reversão e futura outbox. |
| 10 | Faltam backup/restore, LGPD, dedup de legado, ACL financeira, jobs em voo, performance, rate limit, RTO e artifact recuperável. |
| 11 | Usar `first_trusted_assignment`, com elegibilidade variando por origem; não políticas concorrentes por source. |
| 12 | Ainda não integralmente. As principais afirmações foram provadas/refutadas estaticamente, mas faltam hash do runtime e testes end-to-end no OCB implantado. |

## Correções editoriais aplicadas ao plano (2026-09-02)

- `marketing.center.entity` → `marketing.center.external.entity`;
- `marketing.metric.daily` → `marketing.center.metric.daily`;
- inventário de addons impactados corrigido para os nomes reais
  (`marketing_center_sale`, `marketing_center_sale_account`,
  `marketing_center_account`) e completado com
  `marketing_center_contact_center_crm` e `marketing_center_suite`.

## Anexo — spot-checks independentes (Claude, 2026-09-02)

Confirmados por leitura direta da árvore local:

- `MarketingWebsiteFormController(WebsiteForm)` importa e herda diretamente de
  `odoo.addons.website.controllers.form`
  (`marketing_center_website/controllers/website_action.py:17,155`); o bridge
  `MarketingWebsiteCrmFormController` herda dele
  (`marketing_center_website_crm/controllers/website_form.py:35`) e o manifest do
  bridge depende de `marketing_center_website`, `marketing_center_web_ingress`,
  `marketing_center_crm`, `queue_job` — **sem `website_crm`**. Bloqueador 1
  confirmado.
- `protected_value = fields.Char(...)` em
  `marketing_center_web_ingress/models/event.py:139`. Bloqueador 7 confirmado no
  ponto verificável.
- `marketing_center_dashboard/__manifest__.py` depende apenas de
  `marketing_center_base`. Bloqueador 8 confirmado no ponto verificável.
- Nomes reais dos modelos: `marketing.center.external.entity`
  (`catalog.py:14`) e `marketing.center.metric.daily` (`performance.py:20`).
  Correções editoriais procedem.

Não foram re-verificados nesta passada (aceitos da segunda opinião, com fonte OCB
citada por ela): semântica de dedupe do `link.tracker.click`, cookies UTM de 31
dias/mixin, comportamento de fatura agrupada e propagação de `opportunity_id`
tardia. Devem ser provados por `HttpCase`/testes de integração na Fase 2, como o
próprio plano exige.
