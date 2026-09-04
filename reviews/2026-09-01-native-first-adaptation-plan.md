# Plano de adaptação native-first do Marketing Center

> Status: **direção arquitetural aceita em 2026-09-02, com gates obrigatórios**. Este
> documento é normativo para a matriz de autoridade e para a sequência de adaptação. A
> escrita UTM e o cutover continuam bloqueados até shadow, reconciliação e go/no-go.
> Disposição em `reviews/2026-09-02-native-first-second-opinion.md` e consolidação em
> `../../contact-center/reviews/2026-09-02-native-first-roadmap-and-ux-disposition.md`.
>
> Data: 2026-09-01.
>
> Objetivo da revisão externa: confirmar se o corte entre Odoo nativo e os ledgers
> próprios está correto antes de ampliar Performance, Meta CAPI, Google Data Manager e
> escrita em campanhas.
>
> Árvore analisada: `infra-ai-ops`, branch `main`, commit de infraestrutura
> `aaaf0733d8717f89515ff3c0bbdeb24e4c78d1cc`. Como `odoo16/addons/marketing-center` é
> uma árvore local ignorada pelo repositório pai, o fingerprint determinístico do
> código/configuração analisados é
> `32cfa6f30d297b7190a636780259b2a6601b1cbbd08ddf1803d8e14f2023a85c`.

## 1. Veredito aprovado

A adaptação é recomendada, mas deve ser **híbrida e incremental**:

- os modelos nativos do Odoo passam a ser a fonte canônica para classificação UTM, links
  controlados pelo Odoo e documentos de negócio;
- o Marketing Center permanece responsável por dados que o Odoo 16 não representa:
  identidade externa, métricas das plataformas, click IDs, sinais de webhook, correlação
  M:N, evidência imutável e atribuição multi-touch;
- Touchpoints, Business Events e Web Ingress deixam de ser a experiência principal do
  usuário e passam a ser infraestrutura técnica/auditável;
- nenhum histórico é apagado ou reescrito durante a adaptação; seu ciclo de vida futuro
  continua sujeito a uma política explícita de retenção/expurgo antes do cutover
  produtivo.

Não se propõe substituir todo o ledger por `utm.*` ou `link.tracker`. Isso perderia Lead
Ads, Click-to-WhatsApp, IDs opacos, múltiplos contatos e evidências externas. Também não
se propõe manter a arquitetura atual sem mudanças, porque ela duplica conceitos nativos
e expõe detalhes técnicos como se fossem objetos operacionais.

## 2. Evidências que motivam a adaptação

1. `marketing_center_base` já depende de `utm`, mas o touchpoint replica `utm_source`,
   `utm_medium` e `utm_campaign` em campos texto.
2. `marketing_center_website` captura landing/UTMs por um ingresso próprio e seu teste
   de contrato proíbe deliberadamente dependência de `link_tracker`.
3. A projeção de Meta Lead Ads cria `crm.lead`, mas não preenche os campos UTM nativos.
4. O bridge de vendas lê `campaign_id`, `medium_id` e `source_id` já presentes no pedido
   para gravá-los no evento técnico; ele não materializa campanhas externas nos modelos
   UTM.
5. Business Events está exposto em `Funnel` para analistas, embora seja um ledger
   técnico com chaves, hashes, reversões e snapshots.
6. O controller customizado de formulário herda diretamente o controller base de
   `website`; é necessário provar que a extensão de `website_crm` continua na cadeia
   efetiva. Sem essa prova há risco de perder associação visitante-lead e outros
   comportamentos nativos.

Referências locais:

- `marketing_center_base/__manifest__.py`;
- `marketing_center_base/models/attribution.py`;
- `marketing_center_website/tests/test_contract.py`;
- `marketing_center_website/README.rst`;
- `marketing_center_meta_crm/models/service.py`;
- `marketing_center_sale/models/service.py`;
- `marketing_center_base/views/menus.xml`;
- `marketing_center_website/controllers/website_action.py`.

## 2.1 Contradições confrontadas pela segunda opinião

- o plano atual declara o ledger próprio como canônico para a jornada e trata
  `link.tracker` como opcional; esta proposta torna UTM/Website/Link Tracker canônicos
  na operação, sem retirar do ledger a evidência histórica;
- o contrato automatizado do Website hoje exige ausência de dependência de
  `link_tracker` e `utm`; ele precisará ser substituído por um contrato native-first,
  não apenas removido para o teste passar;
- o touchpoint preserva UTMs textuais imutáveis. Elas continuam como snapshot do que foi
  observado, mesmo depois de existirem relações com `utm.*`;
- catálogo Meta/Google e `utm.campaign` têm granularidades e identidades diferentes; um
  não pode substituir o outro;
- `link.tracker.click` não identifica sozinho visitante, lead, click ID externo ou
  jornada multi-touch e possui deduplicação própria;
- a propagação CRM → Venda ocorre em fluxos nativos específicos. Associar uma
  oportunidade posteriormente a um pedido não deve ser presumido equivalente;
- o Website Odoo não substitui o caminho WordPress/Web Ingress enquanto ambos
  coexistirem;
- Business Events duplicam parte do estado atual, mas ainda preservam ocorrência,
  parcela, reversão, idempotência e replay. Ocultá-los não significa apagá-los.

## 2.2 Escopo de supersessão aprovado

Esta decisão substitui especificamente as decisões do `plan.md` que dizem que:

- o ledger próprio deve ser a única fonte canônica de atribuição;
- `link.tracker`, `website.visitor` e `utm.*` são apenas integrações opcionais;
- Business Events e Touchpoints são telas operacionais do funil.

Permanecem válidas as decisões sobre independência dos domínios, DTOs, transporte Meta
compartilhado, `queue_job`, catálogo/performance externos, segredos, fencing,
idempotência e conectores read-only. O histórico de releases não será reescrito; a
aprovação será registrada como uma nova decisão arquitetural datada.

## 3. Princípios da arquitetura-alvo

1. **Nativo primeiro:** não recriar no Marketing Center uma capacidade adequada do Odoo.
2. **Complemento, não concorrência:** o ledger próprio registra somente o que o modelo
   nativo não consegue representar ou precisa preservar como evidência.
3. **Documentos são a verdade operacional:** lead, pedido, fatura e pagamento são
   consultados em seus modelos de origem.
4. **Imutabilidade seletiva:** evidências externas, ocorrências de conversão e reversões
   podem ser imutáveis; cadastros e projeções operacionais não devem fingir ser ledgers.
5. **Fill-only:** uma integração não sobrescreve classificação UTM existente sem uma
   operação humana explícita.
6. **ID estável vence nome:** campanha externa é identificada por conta/provedor/ID,
   nunca por seu nome mutável.
7. **Uma ocorrência, um dono:** um clique ou evento físico não pode ser persistido como
   dois fatos canônicos independentes.
8. **Falha explícita:** mapeamento ambíguo produz diagnóstico; não produz associação por
   aproximação.
9. **Portabilidade:** bridges usam modelos e extensões públicas do Odoo; evitam copiar
   controllers privados ou depender da ordem acidental de carregamento.
10. **Duas verdades de naturezas diferentes:** UTM nativo é a projeção operacional
    canônica; touchpoint próprio é a evidência histórica canônica.

## 4. Matriz de autoridade canônica

| Conceito                                                                  | Fonte canônica                                                 | Papel do Marketing Center                                                           |
| ------------------------------------------------------------------------- | -------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Campanha, origem e meio de um lead/pedido                                 | `utm.campaign`, `utm.source`, `utm.medium` e campos nativos    | Mapear entidades externas e projetar de forma controlada                            |
| Link encurtado criado pelo Odoo                                           | `link.tracker`                                                 | Associar o link à entidade externa quando necessário                                |
| Clique em link controlado pelo Odoo                                       | `link.tracker.click`                                           | Consumir/relacionar; não criar um segundo clique canônico                           |
| Visitante e páginas no site Odoo                                          | `website.visitor`, `website.track` e sessão nativa             | Acrescentar apenas identificadores/sinais ausentes                                  |
| Lead/oportunidade                                                         | `crm.lead`                                                     | Correlação com touchpoints e projeção UTM fill-only                                 |
| Cotação/pedido                                                            | `sale.order`                                                   | Usar propagação nativa e registrar apenas ocorrência de conversão quando necessária |
| Fatura/pagamento/estorno                                                  | `account.move` e reconciliações                                | Registrar snapshots/reversões necessários à atribuição e ao envio offline           |
| Conta/campanha/adset/ad externos                                          | `marketing.center.source` e `marketing.center.external.entity` | Dono integral do catálogo externo                                                   |
| Custo, impressões e cliques reportados                                    | `marketing.center.metric.daily` e observações do provider      | Dono integral, sem gravar como UTM                                                  |
| `gclid`, `gbraid`, `wbraid`, `dclid`, `fbclid`, `fbc`, `fbp`, `ctwa_clid` | Identificadores protegidos do Marketing Center                 | Dono integral e com acesso restrito                                                 |
| Lead Ads, Click-to-WhatsApp e webhooks externos                           | Touchpoint/evidência técnica                                   | Dono integral da evidência e da deduplicação                                        |
| Jornada multi-touch e revisões                                            | Ledger de touchpoints                                          | Dono integral; UTM nativo contém somente uma classificação efetiva                  |
| Entrega de conversões para Meta/Google                                    | Outbox/delivery própria futura                                 | Dono da idempotência, retry, policy e diagnóstico                                   |
| SLA e tempo de primeira resposta                                          | Contact Center                                                 | Marketing consome somente projeção/agregado se houver caso de uso                   |

### Esclarecimento de nomenclatura

`marketing.center.source` representa uma **conta/fonte técnica do provider**, como uma
conta Meta Ads ou Google Ads. Ela não é equivalente a `utm.source`, que é uma dimensão
de aquisição legível pelo negócio, como Google, Facebook ou Instagram.

## 5. Fluxos-alvo

### 5.1 Link, landing e formulário no Website Odoo

```text
link.tracker
  -> link.tracker.click
  -> landing/sessão/website.visitor
  -> UTM nativo
  -> formulário website_crm
  -> crm.lead com campaign/source/medium
  -> cotação/pedido/fatura por fluxo nativo
```

O Marketing Center participa somente quando existir um identificador externo, uma ação
material especial ou necessidade de correlação não atendida pelo Odoo:

```text
click ID / formulário material / handoff WhatsApp
  -> Web Ingress sanitizado
  -> touchpoint técnico
  -> vínculo imutável com o lead/conversa
```

Não deve existir touchpoint próprio para cada pageview nem um segundo registro de clique
quando `link.tracker.click` já for o fato canônico.

A integração deverá possuir uma chave única de correlação para o clique nativo e um
vínculo tipado com a eventual evidência complementar. Não será usada a deduplicação por
IP do Link Tracker como identidade de pessoa ou sessão.

### 5.2 Meta Lead Ads

```text
webhook leadgen
  -> GET autenticado do lead
  -> submissão/evidência imutável
  -> campanha externa mapeada para utm.*
  -> touchpoint Lead Ads
  -> crm.lead
  -> projeção UTM fill-only + vínculo touchpoint/lead
```

### 5.3 Google/Meta Ads para site

```text
landing com UTM + click ID
  -> UTM nativo para classificação operacional
  -> click ID protegido para correlação/conversão offline
  -> formulário/lead nativo
  -> vínculo técnico com a evidência externa
```

### 5.4 Click-to-WhatsApp e Contact Center

```text
anúncio/redirect controlado
  -> token/click ID/touchpoint
  -> conversa no Contact Center
  -> vínculo com lead quando aplicável
  -> UTM nativo somente se o mapeamento for inequívoco e os campos estiverem livres
```

Uma conversa não precisa virar lead. Uma conversa pode ser vinculada a mais de um lead,
e o mesmo lead pode acumular vários touchpoints. Os campos UTM nativos continuam
representando a classificação operacional escolhida, não toda a jornada.

## 6. Mapeamento de entidades externas para UTM nativo

### 6.1 Modelo proposto

Criar no `marketing_center_base` um mapeamento company-scoped e versionado, sem novo
addon nesta fase. Nome conceitual: `marketing.center.utm.mapping`.

Campos mínimos a confirmar durante a implementação:

- empresa;
- source técnica/provider;
- entidade externa opcional de campanha;
- `utm.source`, `utm.medium` e `utm.campaign` nativos;
- método: manual, provisionado, importado ou regra;
- estado: draft, active, ambiguous, archived;
- revisão/configuração e timestamps;
- referência estável da autoridade que criou o mapeamento.

Regras:

- o escopo externo usa empresa + source técnica + ID externo;
- nome de campanha não participa da identidade;
- renomear campanha externa atualiza apresentação, não cria outra identidade;
- `utm.*` não possui o mesmo isolamento multiempresa do Marketing Center, portanto o
  mapeamento sempre preserva empresa e controla visibilidade;
- criação automática de `utm.campaign` deve ser configurável. O padrão inicial é mapear
  explicitamente ou provisionar apenas campanhas selecionadas/ativas;
- ausência ou ambiguidade deixa o registro como não atribuído; não há fuzzy match.

### 6.2 Política de projeção em documentos

Ordem de autoridade:

1. valor existente editado/capturado no documento nativo;
2. UTM capturado pelo Website nativo;
3. mapeamento externo verificado;
4. inferência de touchpoint somente quando inequívoca.

Política aprovada: `first_trusted_assignment`. `campaign_id`, `source_id` e `medium_id`
formam uma única tupla: a primeira proposta completa, inequívoca e de autoridade
elegível que for aceita congela a projeção operacional. Contatos posteriores permanecem
na jornada técnica e nos modelos de atribuição, mas não reescrevem silenciosamente os
campos UTM.

Política de escrita:

- nunca sobrescrever campo UTM não vazio automaticamente;
- se um valor existente conflitar com o mapeamento proposto, não completar os demais
  campos com uma combinação híbrida; registrar conflito e manter o documento intacto;
- preencher campos vazios somente quando todos os valores existentes forem compatíveis
  com a proposta;
- aplicar a tupla em um único write protegido por lock/CAS; nunca preencher os três
  campos por operações independentes;
- repetição/replay deve ser idempotente;
- alteração ou limpeza manual posterior permanece soberana e cria um override/tombstone
  que impede reaplicação automática.

O mapeamento possui revisão imutável, vigência, precedência, constraint/índice de
unicidade no escopo ativo e fencing para jobs antigos. Uma projeção técnica registra, ao
menos, alvo, policy e revisão do mapeamento, preimage, after-image, valores propostos,
resultado (`shadow`, `applied`, `skipped`, `conflict`), chave idempotente e fencing
revision. Assignment e receipt são atômicos. Esse registro não substitui os campos
nativos; explica e, quando seguro, permite compensar exatamente o que a integração
aplicou.

### 6.3 Merge, duplicação e reabertura de leads

- merge: o lead sobrevivente mantém seus campos UTM nativos; as evidências/assertions
  dos leads incorporados são relacionadas ao sobrevivente e conflitos ficam explícitos,
  sem escolher uma nova UTM automaticamente;
- duplicação: não clona evidência histórica nem click IDs por padrão; uma nova
  associação exige uma ocorrência/autoridade explícita;
- reabertura ou mudança de estágio: não recalcula aquisição inicial;
- conversão de conversa em lead: usa o mesmo contrato de projeção de Website e Meta Lead
  Ads, sem regra especial que possa sobrescrever dados.

Esses comportamentos precisam ser provados contra os wizards/fluxos efetivos do Odoo 16
antes do cutover.

## 7. Tratamento dos modelos próprios atuais

### 7.1 Touchpoints

Permanecem para:

- sinais externos e materiais;
- click IDs e referências de anúncio/formulário;
- Click-to-WhatsApp e Lead Ads;
- correlação M:N;
- revisões, enriquecimento e conflitos;
- atribuição first/last/multi-touch.

Deixam de ser criados quando só repetirem um clique, pageview ou UTM já plenamente
representado pelo Odoo. A tela fica técnica/admin; no lead pode existir um smart button
opcional “Jornada”.

Os valores textuais originalmente observados (`utm_*`, landing e referrer) continuam
imutáveis no touchpoint. A relação com `utm.*` é uma resolução/projeção versionada,
nunca uma conversão destrutiva do histórico.

### 7.2 Business Events

Não serão removidos nesta adaptação. Permanecem candidatos a alimentar atribuição e
conversões offline, principalmente onde há valor, snapshot, estorno ou replay:

- lead criado/qualificado/ganho/perdido;
- pedido confirmado/cancelado;
- fatura, recebimento e reversões;
- ocorrências escolhidas pela policy de Meta/Google.

Eventos que apenas duplicam estado operacional devem ser reavaliados depois do dashboard
native-first. `interaction_started` e `first_human_response` pertencem ao domínio do
Contact Center; o Marketing Center não deve ser sua fonte operacional.

Até existir uma outbox de conversões, o ledger continua preservado, mas não deve ser
ampliado por reflexo nem apresentado como tela de negócio.

### 7.3 Web Ingress

Permanece para:

- sites externos/headless;
- click IDs que o Odoo não persiste;
- handoff seguro para WhatsApp;
- correlação material de formulário/lead;
- ações explicitamente configuradas.

No Website Odoo, deixa gradualmente de duplicar landing, UTM, visitor e cliques que o
stack nativo já representa.

## 8. Interface final proposta

Menus para usuários:

1. **Visão geral** — investimento, leads, qualificados, propostas, vendas, receita e
   cobertura de atribuição.
2. **Campanhas** — abre/enriquece `utm.campaign` e mostra o mapeamento Meta/Google.
3. **Aquisição / Performance** — métricas reportadas por provider e comparação com
   resultados nativos.
4. **Funil** — dados de CRM, Vendas e Financeiro, não linhas cruas do ledger.
5. **Links rastreados** — `link.tracker` quando instalado.
6. **Integrações / Configuração** — contas, conexões, mapeamentos e health.

Menu técnico, apenas para administrador/debug:

- Touchpoints/evidências/identificadores;
- Business Events/observações/reversões;
- Web Ingress/intents/deliveries;
- runs, cursors, jobs, payloads e conflitos;
- resultados internos de atribuição.

Ocultar menus não substitui ACL ou record rules. Modelos técnicos continuam cercados por
grupo, empresa e source autorizada, inclusive quando acessados por URL/RPC.

## 9. Plano de execução por fases

Os números abaixo preservam os pacotes originalmente revisados, mas não definem a ordem
de liberação. A sequência vigente é: Fase 0 → correção MRO/contrato de clique da Fase 2
→ mapping/assignment da Fase 1 somente em shadow → Fases 3/4 → dry-run e go/no-go →
Fase 5. Nenhuma etapa intermediária autoriza escrita UTM.

### Fase 0 — ADR, baseline e contenção de escopo

Objetivo: fechar a matriz de autoridade antes de ampliar funcionalidades.

Entregas:

- incorporar a decisão aprovada ao `plan.md`;
- inventariar contagens e hashes dos ledgers atuais;
- inventariar leads, pedidos, links/clicks e visitantes nativos;
- definir feature flags para projeção UTM, captura nativa de links, captura legada e
  dashboard native-first;
- registrar a política inicial de aquisição e o comportamento de merge/duplicação;
- mover imediatamente as telas cruas para menu técnico sem alterar dados.

Aceite:

- baseline reproduzível;
- nenhum registro funcional alterado;
- rollback de menu/configuração comprovado.

### Fase 1 — UTM nativo e mapeamento externo

Entregas:

- modelo versionado de mapeamento;
- UI simples de mapeamento por source/campanha;
- dry-run/backfill por ID externo;
- projeção em shadow para CRM;
- Meta Lead Ads e Contact Center produzindo proposta de UTM;
- diagnósticos de conflito e não atribuído.

Aceite:

- zero overwrite de valor nativo existente;
- renome de campanha não muda identidade;
- replay idempotente;
- isolamento multiempresa testado;
- ambiguidades nunca resolvidas automaticamente.

### Fase 2 — Website nativo e Link Tracker

Entregas:

- dependência/integração efetiva com `website_crm` no bridge de formulário;
- prova da cadeia de controllers e do comportamento visitor → lead;
- adoção de `link.tracker` para links controlados pelo Odoo;
- uso de `website.visitor`/`website.track` para navegação comum;
- redução do JavaScript/ingress próprio ao complemento necessário;
- preservação do redirect seguro para WhatsApp.

Aceite:

- para cada clique nativo elegível, o Marketing Center não cria um segundo fato canônico
  de clique;
- formulário cria o mesmo lead e associação nativa esperados;
- UTMs chegam ao lead;
- click IDs permanecem preservados e protegidos;
- sites externos continuam funcionando pelo Web Ingress.

### Fase 3 — CRM, Vendas e Financeiro native-first

Entregas:

- ativar projeção CRM fill-only após o shadow;
- testar a propagação padrão lead → cotação/pedido → fatura;
- criar glue mínimo apenas para fluxos não nativos comprovadamente necessários;
- nunca usar `sale.order.origin` como substituto de campanha;
- manter vínculos M:N de touchpoint/lead fora dos campos UTM.

Aceite:

- documentos criados pelo fluxo padrão preservam campanha/origem/meio;
- documentos criados por outros fluxos têm comportamento documentado;
- nenhuma edição humana é revertida pela integração.

### Fase 4 — Dashboard e UX native-first

Entregas:

- dashboards lendo diretamente CRM, Sale e Account;
- custo/performance externa agregados pelo mapeamento UTM;
- “não atribuído”, “ambíguo” e “sem mapeamento” explícitos;
- touchpoints apenas como cobertura/jornada avançada;
- remoção de Business Events da navegação operacional.

Aceite:

- totais globais batem exatamente com os aplicativos de origem nos mesmos filtros;
- diferenças de período, timezone e estado são explicáveis;
- receita não é multiplicada por quantidade de touchpoints;
- nenhum resultado estimado é apresentado como fato Odoo.

Antes desta fase, `marketing_center_dashboard` deve declarar dependências reais ou usar
adapters opcionais explícitos. Views SQL não herdam record rules dos modelos de origem:
ACL, empresa e domínio de cada leitura precisam de teste próprio.

### Fase 5 — Cutover e racionalização dos ledgers

Entregas:

- ativação por website/source, não global;
- desligamento de produtores duplicados somente depois de comparação;
- compatibilidade de leitura para o histórico;
- decisão explícita sobre quais Business Events alimentarão atribuição e outbox;
- especificação posterior da outbox Meta CAPI/Google Data Manager.

Aceite:

- histórico e hashes anteriores ao cutover permanecem verificáveis; qualquer expurgo
  futuro autorizado deve deixar tombstone/receipt verificável, não fingir que o registro
  nunca existiu;
- zero clique/touchpoint duplicado no caminho migrado;
- kill switch por flag testado e compensação por receipt validada; a flag interrompe
  novas escritas, mas não é rollback;
- nenhum consumidor runtime órfão.

## 10. Estratégia de migração

- somente mudanças aditivas no primeiro ciclo;
- nenhum `unlink`, truncate ou reescrita de evidência;
- backfill começa em dry-run e produz relatório de `matched`, `unmatched`, `ambiguous`,
  `conflict` e `would_apply`;
- match automático usa ID externo/mapeamento explícito, nunca proximidade de nome ou
  horário;
- registros históricos sem vínculo confiável permanecem não atribuídos;
- dual-read/shadow temporário compara resultado antigo e nativo;
- dual-write só é usado onde existir uma necessidade de compatibilidade claramente
  documentada;
- produtores duplicados são desligados por origem após um volume de validação
  suficiente, não por uma troca global.

## 11. Testes obrigatórios

### Contratos e unidade

- identidade por provider/conta/ID externo;
- renome de campanha;
- mapeamento ambíguo ou arquivado;
- fill-only e conflito parcial;
- multiempresa com `utm.*` global;
- idempotência, replay e concorrência;
- nenhuma PII nova em modelos de mapeamento.

### Integração Odoo

- URL/redirect → link click → landing → visitor → formulário → lead;
- lead → cotação/pedido → fatura com UTM nativo;
- Meta Lead Ads → campanha externa → lead nativo;
- Google/Meta landing com click ID → lead → vínculo técnico;
- Click-to-WhatsApp → conversa → lead, quando vinculado;
- pedidos criados fora do botão padrão do CRM;
- alteração humana depois de uma projeção;
- merge, duplicação e reabertura de lead com múltiplos touchpoints;
- instalação limpa e upgrade sobre a base atual.

### Reconciliação

- leads, pedidos, faturamento e receita por filtros equivalentes;
- nenhum segundo fato canônico criado pelo Marketing Center para clique nativo elegível;
- ledgers e hashes históricos inalterados;
- segunda execução da migração sem efeito;
- dashboard sem multiplicação por joins M:N.

## 12. Go/no-go para o cutover

Um caminho pode abandonar a captura duplicada somente quando:

1. nenhum campo UTM preenchido foi sobrescrito;
2. 100% dos formulários do cenário validado continuam criando e correlacionando o
   registro correto;
3. não há clique duplicado;
4. totais nativos de CRM/Sale/Account reconciliam;
5. click IDs necessários continuam disponíveis;
6. histórico técnico permanece íntegro;
7. kill switch por feature flag funciona e a compensação baseada em receipt só reverte
   valores ainda idênticos ao after-image aplicado;
8. conflitos e não atribuídos são visíveis, não mascarados.

Enquanto um critério falhar, o caminho continua em shadow/dual-read.

## 13. Impacto nos addons

Não se propõe criar outro addon no primeiro ciclo:

- `marketing_center_base`: mapeamento e contratos provider-neutral;
- `marketing_center_crm`: projeção UTM e explicabilidade no lead;
- `marketing_center_meta_crm`: fornecimento da proposta verificada de Meta Lead Ads;
- `marketing_center_contact_center`: fornecimento da proposta derivada do
  AttributionDTO;
- `marketing_center_website`: integração nativa de Website/Link Tracker e redução da
  captura duplicada;
- `marketing_center_website_crm`: correlação e extensão correta de `website_crm`;
- `marketing_center_contact_center_crm`: convergência caso CRM ↔ atribuição sob o mesmo
  contrato de projeção;
- `marketing_center_sale`, `marketing_center_sale_account` e `marketing_center_account`:
  validação do fluxo nativo, vínculos causais pedido↔fatura e eventos materiais;
- `marketing_center_dashboard`: leitura native-first;
- `marketing_center_web_ingress`: complemento para externos/click IDs/handoffs;
- `marketing_center_suite`: refletir dependências/feature flags da adaptação.

`link_tracker` permanece opcional. O caminho puramente nativo pode usar `website_links`;
correlação customizada, click IDs e vínculos adicionais justificam um bridge separado,
sem tornar `link_tracker` dependência do core.

## 14. Fora do escopo desta adaptação

- apagar ou fundir os addons existentes;
- reescrever conectores Meta/Google já validados;
- criar/editar campanhas externas;
- implementar Meta CAPI ou Google Data Manager antes do cutover;
- escolher agora o modelo definitivo de atribuição;
- reconstruir CRM, Vendas, Financeiro ou Link Tracker;
- transformar todos os pageviews em touchpoints;
- mover o SLA operacional do Contact Center para Marketing Center.

## 15. Disposição da segunda opinião e gates vigentes

A segunda opinião foi aceita com estas decisões:

- manter touchpoints como evidência técnica e reduzir somente produtores redundantes;
- manter Business Events apenas quando houver ocorrência, valor, reversão, replay,
  atribuição ou futuro consumidor de conversão explícito;
- aceitar formalmente que `utm.campaign/source/medium` usam uma taxonomia global
  compartilhada, sem nomes confidenciais; o mapping company-scoped controla aplicação e
  visibilidade, não torna `utm.*` multiempresa;
- corrigir a cadeia de controllers para incluir `website_crm` no MRO cooperativo e
  provar visitor → formulário → lead por `HttpCase`;
- validar causalidade pedido–linha–fatura–reconciliação. A tupla UTM isolada de uma
  fatura agrupada ou de um pagamento não prova atribuição financeira;
- tratar feature flag como kill switch; rollback exige compensação segura e, no cutover
  produtivo, backup restaurável com restore testado;
- definir ciclo de vida de `protected_value`, Lead Ads e URLs com identificadores antes
  do cutover/outbox. Por decisão de produto, isso não bloqueia iterações locais nem as
  melhorias independentes do Contact Center;
- tornar baseline e release reproduzíveis, com os três repositórios `contact-center`,
  `marketing-center` e `integration-core` recuperáveis;
- corrigir dependências e ACL do dashboard antes de apresentá-lo como native-first.

Pode avançar agora: ADR, inventário/baseline, menus técnicos, correções isoladas e
shadow. Continuam bloqueados: escrita UTM, desligamento de captura antiga, Meta
CAPI/Google Data Manager e qualquer cutover. Nenhum produtor, ledger ou histórico será
removido antes dos respectivos gates.
