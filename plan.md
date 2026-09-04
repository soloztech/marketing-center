# Marketing Center — plano de arquitetura e implementação

> Status: arquitetura atual implantada no laboratório; direção native-first aceita em
> 2026-09-02, com escrita UTM e cutover ainda bloqueados por shadow e go/no-go. A
> fundação greenfield consolidada de 2026-09-03 foi aplicada e validada no SERVIDOR05. O
> fechamento complementar dos 15 addons e o primeiro baseline sem migrations foram
> aplicados e validados em 2026-09-04; escrita UTM e cutover continuam bloqueados pelos
> gates próprios. Criado em 2026-08-28; arquitetura revisada em 2026-08-29.
>
> Escopo: Odoo 16 Industrial, Meta Ads, Meta Conversions API, Google Ads, Google Data
> Manager, GA4, site Odoo futuro, CRM e Contact Center.
>
> Este documento é normativo para a implementação. Os cortes executáveis atuais cobrem
> DTO/ledger de touchpoints, ponte opcional com o Contact Center e o núcleo operacional
> de source, connection, catálogo, revisões, sync e performance diária. Eventos de
> negócio, mutações e conectores restantes continuam organizados pelas fases abaixo.

> **Decisão de adaptação native-first — 2026-09-02:** antes de ampliar escrita,
> conversões ou atribuição, a arquitetura native-first torna `utm.*`, `link.tracker`,
> Website, CRM, Vendas e Financeiro as fontes operacionais canônicas, preservando os
> ledgers próprios como complemento técnico. A decisão foi aceita com oito gates; apenas
> ADR, inventário/baseline, correções isoladas, menus técnicos e shadow estão liberados.
> Escrita UTM, desligamento de captura e cutover não estão autorizados. Ver o
> [plano de adaptação native-first](reviews/2026-09-01-native-first-adaptation-plan.md)
> e a
> [disposição consolidada](../contact-center/reviews/2026-09-02-native-first-roadmap-and-ux-disposition.md).

> **Decisão de consolidação física — 2026-09-04:** `google_api_base`, `meta_api_base` e
> `meta_webhook_base` continuam sendo fundações técnicas independentes, mas passam a
> residir e ser versionadas no repositório `soloztech/marketing-center`. O histórico do
> antigo checkout local `integration-core` foi incorporado por merge de ancestries.
> Nomes técnicos, modelos, tabelas, imports e dependências Odoo não mudaram; não há
> migration de banco. Referências antigas a `integration-core` nas seções datadas e
> revisões abaixo são evidência histórica e não descrevem a topologia vigente.

## Estado implementado — 2026-08-29

Primeiro corte implantado no servidor05:

- `marketing_center_base` `16.0.1.0.0`: `MarketingTouchpointDTO`, API local de ingestão,
  ledger append-only de touchpoint/evidência/identificador, sanitização, idempotência,
  ACL multiempresa e telas administrativas;
- `marketing_center_contact_center` `16.0.1.0.0`: mapper versionado, fila OCA, vínculo
  imutável entre os dois ledgers e backfill/replay explícito;
- os dois cores continuam independentes; somente o bridge depende de ambos;
- instalação limpa: 11 testes do base e 13 testes integrados, sem falha;
- instalação na base neutralizada seguida de segundo `-u` idempotente, ambos sem erro;
- backfill real: 126 touchpoints operacionais produziram 126 touchpoints de marketing e
  126 links; replay completo manteve as mesmas contagens, com zero job falho;
- o grupo de administração do bridge foi atribuído ao usuário de laboratório Lucas
  Zotelli para validação da interface.

Naquele primeiro corte foram concluídas apenas a fundação de atribuição da Fase 2 e a
primeira fatia da Fase 5. O núcleo operacional veio no corte seguinte; métricas, eventos
de negócio e os conectores compartilhados Meta/Google permanecem nas próximas etapas.

## Estado implementado — 2026-08-30

Segundo corte implantado no servidor05:

- `marketing_center_base` `16.0.1.2.0`: source, connection, team/roster, catálogo
  provider-neutral, projeção de entidade, revisões append-only, sync run e cursor;
- sincronização com snapshot de revisão da source/connection, `window_key`, lock de
  escopo, run ativo exclusivo, `reporting_context_hash` e CAS obrigatório do cursor;
- mudanças de configuração usam revisão atômica e tornam runs antigos `stale` antes de
  qualquer projeção; source pausada e connection fora de `ready` são cercadas;
- roster materializa a correlação usuário ativo → time ativo → vínculo ativo → source,
  evitando que linhas One2many diferentes concedam acesso indevido;
- identidades de empresa/fonte do roster são imutáveis; migração exige operação
  explícita em vez de mover pais com filhos históricos;
- touchpoints ainda sem vínculo resolvido a uma `marketing.center.source` ficam
  admin-only. A projeção de acesso por source será criada junto do enriquecimento;
- 39 testes limpos do base e 41 testes integrados passaram; dois upgrades offline
  consecutivos foram idempotentes; 130 touchpoints e 130 links do bridge permaneceram
  reconciliados, sem job falho;
- evidência canônica do release:
  `scans/raw/20260829-odoo16-marketing-center-first-slice/release/20260831T022305825981Z`.
- `meta_api_base` `16.0.1.0.0` foi extraído para o repositório técnico independente
  `integration-core`, passou 23/23 testes, instalação e replay de upgrade, e está
  instalado isoladamente no servidor05. Ele ainda não substitui o facade do
  `contact_center_meta` neste corte.

## Estado implementado — 2026-08-31

Terceiro corte implantado no servidor05:

- `contact_center_meta` `16.0.1.10.0` passou a consumir `meta_api_base` por uma facade
  compatível, sem mudar o contrato de mensageria; 355 testes do base e 701 integrados
  passaram antes do release;
- `marketing_center_meta` `16.0.1.0.0` implementa o primeiro reader Meta: perfil
  administrativo multiempresa, referências externas a segredos, validação de App e
  scopes, descoberta paginada e limitada de ad accounts e projeção idempotente em
  `marketing.center.source`/`marketing.center.connection`;
- chamadas Graph usam o transporte comum de `meta_api_base`; perfil, autorização, estado
  e jobs de Marketing permanecem no addon consumidor porque ainda não há um segundo
  contrato persistente comprovadamente comum;
- o corte é estritamente read-only e não sincroniza ainda campanha, ad set, anúncio,
  criativo, formulário, Insights ou Lead Ads;
- o gate encontrou e corrigiu a pesquisa de conexão arquivada: o discovery agora usa
  `active_test=False` e preserva a conexão sem recriá-la ou reativá-la;
- instalação limpa: 39/39 testes do base e 64/64 integrados; instalação na base
  neutralizada e replay dos três addons foram idempotentes;
- `marketing_center_meta` ficou instalado no servidor05, com HTTP privado e público 200
  e restauração byte a byte da rota de teste;
- evidência canônica do release:
  `scans/raw/20260829-odoo16-marketing-center-first-slice/release/20260831T035525832765Z`.

Quarto corte implantado no servidor05:

- `marketing_center_base` `16.0.1.2.1` ganhou um ponto de extensão neutro no header da
  fonte, reutilizável por Meta, Google e outros providers;
- `marketing_center_meta` `16.0.1.1.0` sincroniza campaign, ad set (`group/meta_adset`),
  ad e creative em um único run ordenado por ad account;
- cada job lê uma página bounded com allow-list fixa, persiste somente o cursor opaco
  `after` dentro de um envelope local versionado e nunca segue/persiste `paging.next`;
- o cursor usa o CAS do core; UUID do job e revisões de source, connection e profile
  cercam o I/O. Retry é limitado a oito tentativas por página e fecha o run no teto;
- autorização revogada pausa profile/connections e torna o run stale sem projetar a
  página; erro no enqueue sucessor reverte entidade, revisão e cursor atomicamente;
- creative permanece raiz reutilizável, referenciado pelo ad em `meta.creative_ref`;
  ausência numa varredura não gera tombstone neste corte;
- o primeiro gate real encontrou o formato de timestamp Meta `+0000`, incompatível
  diretamente com `datetime.fromisoformat` no Python 3.10. O normalizador passou a
  convertê-lo de forma estrita para `+00:00` e o release foi repetido;
- 39/39 testes do base e 84/84 integrados passaram; upgrade offline e replay foram
  idempotentes, com HTTP privado/público 200 e produção intocada;
- evidência canônica do release:
  `scans/raw/20260831-odoo16-marketing-center-meta-catalog/release/20260831T042545536542Z`.

Quinto corte implantado no servidor05:

- `marketing_center_base` `16.0.1.3.0` adicionou o contrato provider-neutral
  `MarketingPerformanceDTO`, fatos diários de conta/campanha e revisões imutáveis;
- impressões, cliques e custo em micros usam PostgreSQL `BIGINT`; flags de presença
  distinguem campo ausente de zero reportado e o histórico preserva A→B→A;
- identidade inclui fonte, data, grain, referência externa, dimensões, origem e contexto
  de relatório. Entidade do catálogo é vínculo opcional, nunca identidade;
- páginas são aplicadas com fencing, CAS e atomicidade com o cursor. Execução vazia não
  fabrica zero, não cria tombstone e não renova artificialmente o fato anterior;
- `marketing_center_meta` `16.0.1.2.0` implementou Insights v26 diário para conta e
  campanha, somente impressões, cliques e gasto, sem actions, conversões, breakdowns ou
  parâmetros de atribuição;
- a janela é limitada a 31 dias e respeita o timezone IANA da conta, inclusive dias UTC
  de 23/25 horas. O botão administrativo agenda os sete dias fechados anteriores;
- paginação usa exclusivamente o cursor opaco `after`, com teto de 512 páginas, oito
  tentativas por página, fencing de profile/source/connection/job e recuperação
  explícita a partir da primeira página;
- o primeiro gate isolado detectou a representação física de `{}` como `NULL` em
  `fields.Json` no Odoo 16; o hash de dimensões permanece canônico e o modelo passou a
  aceitar `NULL` somente como representação de dimensões vazias;
- 53/53 testes do base e 116/116 integrados passaram; upgrade offline e replay foram
  idempotentes, HTTP privado/público retornou 200 e produção não foi tocada;
- evidência canônica do release:
  `scans/raw/20260831-odoo16-marketing-center-meta-insights/release/20260831T045958728100Z`.

O código está pronto, mas a leitura real permanece operacionalmente bloqueada até
existir um perfil Meta reader dedicado com `ads_read`. O token de mensageria atual não
deve ser reutilizado.

Pendências imediatas, em ordem:

1. evoluir a identidade do mesmo Meta App no `meta_api_base`, criar `meta_webhook_base`
   e fazer o cutover direto dos controllers de laboratório;
2. provisionar perfis separados Page, Ads reader e Lead reader, comprovar scopes, tasks
   e access tier e executar discovery/catálogo/Insights reais;
3. completar o catálogo Meta com lead forms e datasets/pixels e implementar o consumer
   `leadgen` + GET do lead + pull reconciliador;
4. fazer o spike do runtime Google e então criar `google_api_base` e
   `marketing_center_google`.

## Estado implementado — 2026-09-01

A auditoria independente foi confrontada com o código e resultou em consolidação, sem
troca das fronteiras arquiteturais:

- `marketing_center_base` `16.0.1.5.0` possui touchpoint efetivo, resolução explícita de
  source (`resolved`, `unresolved`, `ambiguous`, `unsupported`), histórico A→B→A, fatos
  de negócio imutáveis e valores canônicos em micros;
- `marketing_center_contact_center` `16.0.2.1.0` emite lifecycle de conversa e preserva
  a identidade completa da evidência operacional;
- `marketing_center_crm` e `marketing_center_contact_center_crm` materializam fatos de
  lead/etapa e correlação M:N conversa↔lead↔touchpoint, sem transformá-la em crédito
  causal;
- `marketing_center_sale`, `marketing_center_account` e `marketing_center_sale_account`
  registram proposta, pedido, fatura, crédito, pagamentos parciais e reversões como
  conceitos distintos e reconciliáveis;
- `marketing_center_meta` `16.0.2.0.0` recebe o hint `leadgen` pelo callback técnico,
  recupera o lead via Graph, sanitiza PII, converge webhook/pull por `leadgen_id` e
  projeta o touchpoint antes de qualquer integração futura com CRM;
- `marketing_center_dashboard` `16.0.1.0.0` entrega a primeira visão gerencial
  read-only, com linha não atribuível, freshness e ocultação de totais quando a
  cobertura de sync é parcial;
- o spike Google no runtime real escolheu REST oficial v25 atrás de `google_api_base`; o
  SDK permanece substituível e não é dependência do processo Odoo nesta fase.

O release canônico no servidor05 concluiu instalação, upgrade, replay e smoke com 92
testes isolados e 320 integrados, sem falhas ou erros, HTTP público 200 e produção
intocada:
`scans/raw/20260901-odoo16-marketing-center-leads-finance-dashboard/release/20260901T122900890762Z`.

Pendências reais após esse corte:

1. implementar `marketing_center_google` read-only sobre o `google_api_base` já
   instalado e validado no Odoo;
2. provisionar credenciais Meta Ads/Lead dedicadas e executar discovery, catálogo,
   Insights e Lead Ads reais;
3. projetar formulários e datasets/pixels por suas fronteiras corretas (Page e
   Business/ad account), sem inseri-los artificialmente no sweep de campanha;
4. implementar captura first-party (`marketing_center_web_ingress`) antes de conversões
   outbound ou atribuição causal;
5. somente depois: conversion events/deliveries, Data Manager, Meta CAPI e mudanças
   assistidas.

### Corte consolidado — Google observability, Website→CRM e dashboard

O sexto corte foi aplicado no servidor05 em 2026-09-01 e substitui a lista de pendências
operacionais do corte anterior:

- `marketing_center_base` `16.0.1.7.0`, bridges Contact Center/CRM e addons
  Sale/Accounting preservam fatos, correlações M:N e o ledger sem fabricar crédito
  causal;
- `marketing_center_dashboard` `16.0.1.1.0` distingue conversas da primeira resposta e
  ciclos operacionais, mantendo custo de plataforma, fatos Odoo e atribuição em linhas
  semanticamente separadas;
- `marketing_center_google` `16.0.1.1.0` entrega catálogo/performance read-only, Change
  History e Delivery Diagnostics via REST oficial v25, com paginação bounded, fencing,
  cooldown, retry e observações imutáveis;
- `marketing_center_web_ingress` e `marketing_center_website` `16.0.2.0.0` capturam
  landing, formulário e handoff WhatsApp sem ler valores do formulário nem copiar PII
  para o ledger;
- `marketing_center_website_crm` `16.0.1.2.2` mantém o `crm.lead` nativo como fonte
  canônica e cria intent/correlação durável a partir de comprovante assinado, com
  recuperação limitada e sem alterar UTMs nativas;
- o boundary HTTP aceita tanto a string usada por testes unitários quanto o objeto
  `Response` produzido pelos wrappers reais do Odoo; o mesmo response, status, headers e
  cookies são preservados;
- gates finais: 109/109 no base, 496/496 integrados e 89/89 Website/CRM; instalação
  offline e replay idempotente concluídos, HTTP privado/público 200 e console do
  navegador sem erros;
- validação Google real: dez janelas de Change History concluídas sem alteração no
  período e dois snapshots de Delivery Diagnostics concluídos, totalizando 124
  observações, sem run ativo ou falho;
- evidência canônica:
  `scans/raw/20260901-odoo16-marketing-center-website-crm-google-observability/release/20260901T184433945695Z`.

Próximos cortes, em ordem:

1. Performance DTO v2 com conversões e valor reportados pela plataforma, seguida de
   Google GAQL e apresentação de conversões/CPA sem misturar atribuição Odoo;
2. produtor automático de candidatos/resultados de atribuição sobre links já
   correlacionados, mantendo política/modelo/versionamento explícitos;
3. completar Meta com forms e datasets/pixels e validar Ads/Lead reader reais;
4. integrar `website.visitor`, `link.tracker`, `utm.*` e identificadores first-party
   adicionais no addon Website, sem tornar o Website dependência do core;
5. somente depois, conversões outbound, mutações assistidas e automação controlada.

### Consolidação da distribuição — um aplicativo, componentes internos

Em 2026-09-01, a árvore completa foi revisada antes de continuar a expansão. A decisão é
preservar os 14 componentes atuais: eles separam providers, aplicativos Odoo opcionais e
bridges entre domínios, sem criar outros serviços ou bancos.

- `marketing_center_suite` é a fachada de instalação completa da Soloz e o único módulo
  marcado como aplicativo no catálogo do Odoo;
- `marketing_center_base` continua dono do único menu raiz e de todos os contratos
  canônicos, mas passa a ser componente técnico;
- o suite não possui modelos, tabelas, regras, menus, controllers ou jobs: suas seis
  dependências-folha resolvem exatamente os 14 componentes funcionais;
- módulos de cola permanecem com instalação explícita (`auto_install=False`) para não
  ativar efeitos de negócio de forma implícita;
- instalações menores continuam possíveis selecionando os componentes técnicos;
- a dependência direta de `marketing_center_website_crm` sobre
  `marketing_center_web_ingress` passa a ser declarada no manifest;
- a instalação limpa do suite e o fechamento exato das dependências passam a fazer parte
  do gate canônico de release.

O release consolidado foi aplicado no servidor05 e validou:

- 112 testes do núcleo, 507 integrados, 89 de Website/CRM e 4 do contrato do suite,
  todos sem falhas ou erros;
- os 15 módulos instalados nas versões esperadas (14 componentes funcionais e a fachada
  técnica), seguidos por replay integral idempotente;
- somente `marketing_center_suite` com `application=True`; todos os componentes internos
  permanecem fora do catálogo de aplicativos;
- Odoo e DB manager em execução, HTTP privado e público 200, rota Traefik restaurada
  byte a byte e produção intocada.

Evidência canônica:
`scans/raw/20260901-odoo16-marketing-center-suite-consolidation/release/20260901T195842795779Z`.

O mapa normativo de camadas, fluxo ponta a ponta e critérios para novos addons está em
[`ARCHITECTURE.md`](ARCHITECTURE.md). O número de addons não deve ser apresentado ao
usuário como número de aplicações: todos executam no mesmo Odoo, banco e JobRunner.

## Objetivo

Criar uma central de marketing dentro do Odoo para medir e operar midia paga de ponta a
ponta:

```text
investimento
    -> impressao / clique / formulario / conversa / visita
    -> oportunidade CRM
    -> atendimento
    -> proposta
    -> venda
    -> faturamento / recebimento
    -> feedback de conversao para Google e Meta
```

O sistema deve responder, por empresa, plataforma, conta, campanha, grupo/adset,
anuncio, criativo, formulario, landing page e periodo:

- quanto foi investido;
- quantos leads/conversas foram gerados;
- quantos viraram oportunidade valida, proposta e venda;
- qual tempo de primeira resposta e ciclo comercial;
- qual receita atribuída e, quando houver fonte auditável de custo, qual margem;
- qual CPL, CPQL, CAC e ROAS; ROAS de margem é opcional até a fonte ser instalada;
- quais dados sao completos, parciais ou nao atribuiveis.

O primeiro marco operacional será integralmente **read-only**. Escrita em campanhas,
orçamentos, lances, objetivos de otimização e conversões primárias permanecerá desligada
até que leitura, rastreamento, CRM, reconciliação e mecanismos de segurança tenham
evidência suficiente no ambiente de teste.

## Escopo e Não Objetivos Iniciais

Entram no produto, por fases:

- Google Ads e Meta Ads: inventário, performance, diagnósticos e operação assistida;
- Meta Lead Ads, Meta Conversions API e Google Data Manager;
- atribuição first-party entre site, Contact Center, CRM, vendas e financeiro;
- GA4 e Search Console como fontes complementares de comportamento e SEO;
- Website Odoo, `utm.*` e `link.tracker` como integrações nativas;
- dashboards gerenciais e operacionais com granularidade e origem explícitas.

Não entram no MVP:

- substituir Meta Ads Manager ou Google Ads por uma réplica completa de suas UIs;
- automação autônoma de verba sem aprovação, limites e kill switch;
- scraping de interfaces ou APIs privadas/reverse-engineered como dependência de
  produção;
- usar Odoo como data lake de eventos brutos em alta cardinalidade;
- somar receitas atribuídas por Google e Meta como se fossem vendas diferentes;
- criar automaticamente contato, lead ou oportunidade apenas porque um identificador de
  publicidade foi observado.

O ambiente inicial é Odoo 16 Community/OCB. Os addons Enterprise `marketing_automation`
e `social` estão indisponíveis e não serão dependências; um eventual
`marketing_center_social` será addon próprio e opcional.

## Relação com Decisões Anteriores

Este plano substitui, para o domínio de marketing, as arquiteturas candidatas dos
runbooks de 2026-08-04. Evidências e inventários desses documentos continuam válidos.

| Tema anterior                     | Decisão vigente                                                                                                                                    |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| n8n como orquestrador             | somente periférico/protótipo; não é ledger nem caminho crítico                                                                                     |
| Chatwoot como cockpit             | fora do Marketing Center; o Contact Center Odoo é a fonte de conversa                                                                              |
| Evolution/gateway isolado         | transportes legados não definem atribuição; adapters emitem o DTO do seu domínio e bridges traduzem contratos                                      |
| API gateway de marketing separado | não criar agora; captura first-party fica em `marketing_center_web_ingress` e webhooks Meta entram por `meta_webhook_base`, ambos atrás do Traefik |
| panorama martech/open source      | referência de componentes, não dependência runtime                                                                                                 |
| upload legado Google Ads          | novos fluxos usam Data Manager; exceção exige ADR e token allowlisted                                                                              |

## Decisão Arquitetural

O Marketing Center terá um core de domínio próprio. Ele não será uma extensão do Contact
Center nem do CRM. Somente os clientes técnicos das plataformas serão compartilhados.
Cada domínio mantém seu próprio contrato: o Contact Center conserva seu `AttributionDTO`
e sua evidência operacional; o Marketing Center possui `MarketingTouchpointDTO` e o
ledger canônico de evidência da jornada de marketing. Esse ledger não substitui a
classificação operacional nativa em `utm.*`. A integração entre eles ocorre
exclusivamente pelo addon opcional `marketing_center_contact_center`.

```text
Meta App / Graph APIs
    -> meta_api_base                    # identidade técnica + transporte
    -> meta_webhook_base                # callback técnico único
        -> messages -> contact_center_meta -> contact_center_base
        -> leadgen  -> marketing_center_meta -> marketing_center_base

Google APIs
    -> google_api_base
        -> marketing_center_google ------------+
        -> marketing_center_ga4 ---------------+-> marketing_center_base
        -> marketing_center_google_data_manager+

contact_center_base
    -> AttributionDTO + evidência operacional

marketing_center_base
    -> MarketingTouchpointDTO + ledger canônico de evidência de marketing

contact_center_base <--- marketing_center_contact_center ---> marketing_center_base
                       depende dos dois cores

Fluxo da ponte: evidência operacional -> mapper versionado -> touchpoint de marketing
```

Regras:

- `marketing_center_base` não conhece campos crus de Meta ou Google.
- Cada plataforma entra por addon próprio e implementa contratos do base.
- `meta_api_base`, `meta_webhook_base` e `google_api_base` são infraestrutura técnica,
  não domínio de marketing. Compartilham identidade/autenticação, transporte, quota,
  health e ingresso quando aplicável, mas não compartilham campanhas com conversas.
- Contact Center e Marketing Center usam o mesmo Meta App e o mesmo callback técnico,
  mas credenciais Page, Ads reader e Lead reader são perfis/referências distintos por
  purpose, capability e blast radius.
- `queue_job` da OCA sera o executor padrao para ingestao, sync, export, retry e
  reconciliacao.
- Toda chamada externa ocorre fora da transacao da UI.
- Toda mutacao em campanha nasce como proposta ou objeto `PAUSED`, nunca ativa por
  padrao.
- Odoo continua a fonte canonica para funil, pedido, fatura e recebimento.
- Plataformas continuam fontes de custo, impressao, clique, reach, conversoes reportadas
  e status de entrega.
- O Contact Center continua a fonte operacional das conversas; o Marketing Center
  consome sua evidência por ponte explícita e idempotente.
- `contact_center_base` e `marketing_center_base` são instaláveis isoladamente e nenhum
  deles depende do outro.
- O bridge nunca faz dual-write da ingestão do Contact Center diretamente no Marketing
  Center; ele traduz evidência já persistida depois do commit e também suporta
  backfill/replay.
- Instalar, pausar ou remover o bridge não altera o funcionamento canônico do Contact
  Center.
- O WordPress atual e, depois, o Website Odoo devem capturar UTMs/click IDs antes de
  depender de atribuição de plataforma.
- Nomes de campanha/adset/anuncio sao snapshots de apresentacao, nunca chaves.
- Providers e bridges apenas propõem atribuição. Somente `marketing_center_crm` aplica a
  tupla nativa `campaign_id/source_id/medium_id`, de forma atômica, fill-only, com
  lock/CAS, revisão, receipt e tombstone de override humano.
- `utm.*` usa taxonomia global compartilhada no Odoo 16; o mapping company-scoped
  controla aplicação e visibilidade, mas não transforma os cadastros UTM em registros
  privados por empresa. Nomes UTM não podem carregar informação confidencial.

## Fontes Canônicas

| Dado                                                        | Fonte canônica                       | Observação                                                                  |
| ----------------------------------------------------------- | ------------------------------------ | --------------------------------------------------------------------------- |
| estrutura, status e gasto de campanha                       | Google/Meta                          | Odoo guarda projeção e revisões                                             |
| impressões, cliques e atribuição reportada                  | Google/Meta                          | não equivale à receita real                                                 |
| UTM e clique de link controlado pelo Odoo                   | `utm.*` e `link.tracker.click`       | classificação/operação nativas; não prometem identificar cada clique físico |
| landing, referrer, click ID e sessão não cobertos pelo Odoo | ledger do Marketing Center           | evidência própria para externo/headless/handoff                             |
| referral/origem observada em mensagem                       | Contact Center                       | evidência operacional, importável pelo bridge                               |
| conversa e SLA                                              | Contact Center                       | não pertence ao Marketing Center                                            |
| lead, etapa, responsável e oportunidade                     | CRM                                  | fatos comerciais internos                                                   |
| pedido, receita, fatura e recebimento                       | Sale/Accounting                      | receita nunca nasce do Ads Insights                                         |
| atribuição calculada                                        | Marketing Center + modelo versionado | sempre exibir modelo e cobertura                                            |

Métricas `platform_reported`, fatos `odoo_actual` e resultados `attribution_modelled`
nunca serão armazenados ou apresentados como a mesma medida.

## Glossário

- **source**: conta/serviço externo observado, com moeda e timezone próprios;
- **connection**: liga uma source a um perfil técnico/credencial revisionado;
- **external entity**: campanha, grupo/adset, anúncio, criativo, form, dataset etc.;
- **touchpoint**: evidência de entrada/origem, não conclusão de causalidade;
- **identifier**: ID namespaced de ativo, clique, sessão ou pessoa, com papel/ACL;
- **business event**: fato interno imutável do funil ou da receita;
- **conversion event**: tradução provider-neutral de um business event elegível;
- **delivery**: tentativa/resultado de enviar a conversão a um destino;
- **metric fact**: observação reportada por plataforma em grain/contexto definidos;
- **attribution result**: cálculo versionado que distribui crédito, não fato bruto;
- **change request**: intenção aprovada; **command**: execução externa derivada;
- **provider boundary**: ponto a partir do qual uma chamada mutante pode ter surtido
  efeito mesmo sem resposta local.

## Relações Principais

```text
res.company
  +-- marketing.center.team --< member / source roster
  +-- marketing.center.source --< connection
            |
            +--< external.entity --< entity.revision
            |          |
            |          +--< metric.daily --< metric.action.daily
            |                     +--< metric.revision
            |
            +--< sync.run / sync.cursor

contact.center.attribution.*        # evidência operacional independente
            |
            +--< marketing_center_contact_center (tradução + link)
                            |
                            v
marketing.attribution.touchpoint --< identifier/evidence/enrichment
            |
            +--< bridge links --> conversation / lead / order / invoice
            +--< attribution contribution --> attribution.result

marketing.business.event --< marketing.conversion.event --< delivery

change.request --< change.item --< approval --< platform.command
```

## Exemplo Ponta a Ponta

1. Google reporta campanha `customers/.../campaigns/123` e custo diário no timezone da
   conta; sync cria entidade, revisão e métrica `platform_reported`.
2. Clique chega ao WordPress com UTM e `gclid`; o `marketing_center_web_ingress` cria
   touchpoint no ledger do Marketing Center, extrai o click ID para o identifier
   restrito e salva URL canônica sanitizada.
3. Formulário cria `crm.lead`; o bridge cria o link sem alterar o touchpoint.
4. Entrada em etapa qualificada cria `marketing.business.event` com ocorrência
   idempotente.
5. Pedido, fatura e cada `account.partial.reconcile` criam eventos distintos; desfazer
   uma conciliação cria evento que referencia o original.
6. Policy transforma o evento elegível em conversion event e deliveries Google/Meta;
   cada adapter resolve a credencial fora do banco e envia depois do commit.
7. Dashboard mostra lado a lado custo Google, fatos Odoo e atribuição calculada, com
   moeda, timezone, janela, cobertura e freshness.

## Relação com Contact Center

O Contact Center já possui `AttributionDTO`, `contact.center.attribution.touchpoint` e
`contact.center.attribution.identifier`, criados a partir de payloads de WhatsApp/Meta
Messaging. Esses artefatos continuam pertencendo ao Contact Center e são sua evidência
operacional canônica. Não serão removidos, migrados para outro addon ou substituídos por
uma dependência do Marketing Center.

O Marketing Center possui um contrato e um ledger distintos, voltados à jornada de
marketing. O addon opcional `marketing_center_contact_center` traduz entre os dois
domínios:

```text
contact_center_meta / contact_center_wuzapi
    -> Contact Center AttributionDTO
    -> contact.center.attribution.*          # evidência operacional preservada
    -> marketing_center_contact_center
    -> MarketingTouchpointDTO
    -> marketing.attribution.*               # ledger canônico de marketing
    -> link tipado para conversa/mensagem/identity/caixa
```

O bridge depende de `contact_center_base` e `marketing_center_base`; nenhum core depende
dele nem um do outro. Ele lê evidência já persistida, cria o touchpoint de marketing e o
vínculo tipado numa transação curta. A tela de atendimento permanece autônoma e não
consulta o Marketing Center para receber ou enviar mensagens.

Integração e backfill:

1. definir mapper versionado entre `AttributionDTO` e `MarketingTouchpointDTO`;
2. criar chave idempotente com referência pública da origem, versão do mapper e digest
   da evidência;
3. executar backfill sem modificar registros do Contact Center;
4. reconciliar contagens, identificadores, links e conflitos;
5. processar novas evidências assincronamente depois do commit;
6. permitir replay seguro após evoluções do mapper.

Regras da ponte:

- Nunca copiar mensagem, midia ou telefone cru para o Marketing Center.
- Preservar somente identificadores tecnicos necessarios e com namespace.
- Manter referência opaca à evidência original no Marketing Center e FKs reais no
  bridge.
- Permitir varios touchpoints para o mesmo lead/conversa.
- Permitir um mesmo guest/partner ter touchpoints em varias caixas.
- Nao promover guest para contato por causa de marketing.
- Preservar append-only e dedupe; enriquecimento monotônico passa a ser evidência
  adicional/projeção, não update destrutivo do touchpoint.
- Nunca usar o vínculo com `res.partner` para colapsar conversas ou caixas distintas.
- Nunca alterar ou remover `contact.center.attribution.*` durante ingestão, backfill ou
  desinstalação do bridge.

## Relação com `contact_center_meta`

`contact_center_meta` e `marketing_center_meta` compartilham a identidade/transporte do
`meta_api_base` e o ingresso técnico do `meta_webhook_base`, mas continuam sendo
consumidores independentes. Essa separação é obrigatória porque:

- Messaging usa Page/Instagram messaging permissions.
- Ads/Insights usa ad accounts, Business Manager e Marketing API.
- CAPI usa dataset/pixel e outro conjunto de eventos.
- rate limit, health, escopos e risco de escrita são diferentes por perfil/conexão.

O release instalado de `meta_api_base` começou stateless, com Graph versionado,
`appsecret_proof`, limites de resposta, assinatura/HMAC e taxonomia neutra de erros. A
evolução aprovada no ADR de 2026-08-31 acrescenta identidade técnica revisionada do Meta
App e referências externas de segredo, sem incorporar modelos de conversa, campanha,
lead ou atribuição.

`meta_webhook_base` adiciona o único callback público do App, delivery/dedupe técnico e
registry de rotas. Ele não conhece DTOs de domínio: `messages` é processado por
`contact_center_meta`; `page/leadgen`, por `marketing_center_meta`; consumer ausente ou
campo desconhecido permanece `unrouted` e reprocessável.

O ledger técnico de delivery não substitui nenhum ledger de domínio: ele prova que um
POST externo chegou e foi roteado. Cada consumer conserva dedupe semântico, fila e
estado de processamento próprios. O `contact_center_meta` emite `EventDTO` e
`AttributionDTO`; o `marketing_center_meta` emite `MarketingTouchpointDTO` depois de
recuperar e validar o Lead Ads. A tradução da evidência operacional do Contact Center
cabe somente ao bridge.

## Addons Previstos

### `marketing_center_web_ingress`

Ingress público opcional do Marketing Center para o site atual e para o futuro Website
Odoo. Ele permanece separado do core para que instalar `marketing_center_base` não
exponha controllers públicos por efeito colateral.

Responsabilidades:

- criar sessão/submissão/touchpoint pela API local versionada do
  `marketing_center_base`;
- receber captura first-party de WordPress sem depender de `website`;
- emitir redirects opacos e seguros para WhatsApp;
- rate limit, filtro de bot/prefetch, dedupe e diagnóstico sanitizado.

Dependências: `marketing_center_base`, `web` e a infraestrutura de fila usada pelo
ingress. Hospedagem: repositório `marketing-center`.

### `meta_api_base`

Infraestrutura Meta compartilhável por mensageria e marketing.

Responsabilidades:

- identidade técnica revisionada do Meta App, com App ID e referência externa
  allowlisted para App Secret;
- cliente HTTP Graph versionado e bounded;
- `appsecret_proof` e extração segura de request metadata;
- classificação neutra de falhas, `Retry-After` e respostas malformadas;
- primitives de assinatura/HMAC para uso pelo ingresso compartilhado;
- contrato comum para resolução de referências de segredo fora do PostgreSQL.

Não possui segredo em texto claro, webhook público, regra de roteamento, paginação
específica de recurso, conversa, campanha, lead, touchpoint ou regra comercial.
Capabilities funcionais, jobs e projeções continuam no consumer.

Hospedagem: addon técnico independente co-localizado no repositório
`soloztech/marketing-center`. Tanto o Contact Center quanto os addons funcionais de
Marketing o consomem por dependência técnica; nenhum consumer passa a depender de
`marketing_center_base` apenas por causa da localização física.

### `meta_webhook_base`

Ingresso técnico único para os webhooks do Meta App compartilhado.

Responsabilidades:

- handshake `hub.*` e validação de `X-Hub-Signature-256` sobre o corpo exato;
- endpoint revisionado com referência externa do verify token;
- Page/asset, referência externa do Page token e reconciliação da união de subscriptions
  solicitada pelos consumers;
- limite de método/content type/tamanho antes de parsear;
- delivery técnica sanitizada, digest, dedupe, estados e replay;
- decomposição bounded de `entry[]/changes[]` e registry aditivo de consumers;
- roteamento `messages -> contact_center_meta` e
  `page/leadgen -> marketing_center_meta`;
- acknowledge rápido, sem Graph API nem regra de domínio no request síncrono;
- estado `unrouted` quando o campo é desconhecido ou o consumer não está instalado.

Não cria DTO, conversa, mensagem, lead, campanha ou touchpoint. Não depende de Contact
Center nem do core funcional de Marketing; os addons consumers dependem dele e registram
seus handlers. Hospedagem: camada técnica do repositório `soloztech/marketing-center`,
ao lado de `meta_api_base`.

Contrato e cutover: ver
[`reviews/2026-08-31-shared-meta-app-webhook-adr.md`](reviews/2026-08-31-shared-meta-app-webhook-adr.md).

### `google_api_base`

Infraestrutura Google compartilhável pelos conectores de Ads, Data Manager, GA4, Search
Console e outros serviços futuros.

Responsabilidades:

- projeto/cliente OAuth, service account quando suportada, developer token e login
  customer, sempre como perfis de credencial;
- factory de clientes por serviço e escopo;
- quota, `Retry-After`, request IDs, health e erros seguros;
- separação de credenciais reader/writer e capability registry.

Antes de fixar o SDK Google Ads dentro do processo Odoo 16, executar um spike de
compatibilidade de `grpcio`/`protobuf`. Se houver conflito incontornável com o runtime,
o adapter poderá executar em worker isolado, preservando exatamente o mesmo DTO e
contrato do addon.

Hospedagem: addon técnico independente na camada compartilhada do repositório
`soloztech/marketing-center`, permitindo reuso sem deslocar a fronteira funcional do
Marketing Center.

### `marketing_center_base`

Core de dominio.

Responsabilidades:

- contas de marketing por empresa;
- cadastro de fontes, plataformas e entidades externas;
- registry de conectores;
- DTOs de performance, touchpoint, conversão e mudança;
- `MarketingTouchpointDTO` e API local versionada de ingestão;
- ledger append-only de touchpoints, identifiers, evidence e supersession;
- jornadas, modelos, contribuições e resultados de atribuição;
- dedupe, conflitos, enriquecimento monotônico e consulta do ledger canônico de
  marketing;
- snapshots diarios de performance;
- reconciliacao entre gasto, touchpoints, CRM e receita;
- outbox de conversoes e comandos;
- politicas de alteracao, aprovacao e tetos de risco;
- dashboards e modelos administrativos basicos;
- ACL, record rules, queue_job e observabilidade.

Nao deve conter:

- endpoints ou parametros crus de Google/Meta;
- webhooks publicos especificos;
- token de plataforma em texto claro;
- log com payload bruto ou PII nao mascarada.

Dependências do primeiro corte: `base` e `utm`. `queue_job`, `mail` e `web` entram no
core somente quando os respectivos serviços assíncronos, comunicação e cockpit forem
implementados; o bridge já depende diretamente de `queue_job`. Não depende de Contact
Center, CRM, Website, Sale, Accounting, Meta ou Google.

### `marketing_center_google`

Conector Google Ads.

Responsabilidades:

- inventario de MCC, customer, campanha, ad group, criterio, asset e conversao;
- performance via GAQL;
- change history;
- recomendacoes e diagnostico;
- mutacoes assistidas em campanhas, budgets, status, assets, keywords e negativas;
- reconciliacao de IDs Google: `customer_id`, `campaign_id`, `ad_group_id`, `ad_id`,
  `asset_id`, `conversion_action_id`;
- classificacao de erros, quotas e timeouts.

Biblioteca preferida: `google-ads` oficial para Python, condicionada ao spike de
compatibilidade do `google_api_base`.

Uso inicial:

- leitura e diagnostico;
- mutacoes somente via `marketing.change.request`;
- toda campanha/ad group/ad novo criado `PAUSED`;
- proibido aumentar budget sem aprovacao e teto.

### `marketing_center_google_data_manager`

Conector Google Data Manager.

Responsabilidades:

- envio de conversoes offline e Enhanced Conversions for Leads;
- envio futuro de audiencias/Customer Match quando aprovado;
- hashing/normalizacao de dados de usuario antes do envio;
- idempotencia por evento de negocio;
- monitoramento de lote e erro parcial.

Decisao: novos fluxos de conversao/audiencia devem priorizar Data Manager. O Google Ads
API fica para leitura, estrutura e mutacoes de campanha.

O fluxo distingue:

- conversão offline correlacionada por click ID;
- Enhanced Conversions for Leads com first-party matching, normalmente apoiada pela
  Google tag/GTM;
- quando não houver tag capturando user-provided data, o fluxo ECL exige a correlação
  por GCLID prevista pela configuração Google.

`transaction_id` é determinístico e persistido por delivery. `validateOnly` valida a
requisição, mas não é tratado como sandbox de atribuição. Limites por request,
identificadores, destinos, minuto e dia são capabilities/configuração versionada do
adapter, não literais espalhados no domínio.

### `marketing_center_ga4`

Conector GA4.

Responsabilidades:

- leitura de relatorios `runReport`, realtime e funis quando necessario;
- descoberta de propriedades, streams, links Google Ads e key events;
- importacao de metricas por landing page, source/medium/campaign e eventos;
- comparacao GA4 x Odoo x Ads.

Uso inicial:

- leitura;
- nenhum evento sera enviado para GA4 na primeira fase;
- quotas por propriedade devem ser respeitadas e registradas.

Funis `runFunnelReport` estão em API `v1alpha`/preview e não são contrato crítico do
MVP. O conector os trata como capability experimental isolada.

### `marketing_center_meta`

Conector Meta Marketing API.

Estado entregue no primeiro corte:

- perfil reader consumer-specific com referências externas a segredos;
- validação de App/token/scopes;
- discovery paginado e bounded de `GET /me/adaccounts`;
- projeção idempotente de ad account em source/connection;
- nenhuma mutação e nenhum processamento de Lead Ads.

Roadmap do addon:

Responsabilidades:

- inventario de Business, ad accounts, campaigns, ad sets, ads, creatives, forms,
  pixels/datasets e custom conversions;
- Insights por nivel e breakdown aprovado;
- leitura de lead forms e webhooks de Lead Ads;
- mutacoes assistidas em campanha/adset/ad/creative/status/budget;
- status, learning, delivery, rejeicoes e diagnosticos;
- idempotencia de criacao quando a API nao oferecer idempotency-key real.

Cliente preferido: cliente Graph fino de `meta_api_base`; o Business SDK oficial pode
ser usado quando reduzir complexidade sem duplicar autenticação, retry e diagnóstico.

Uso inicial após completar o roadmap read-only:

- leitura, Insights e Lead Ads;
- escrita somente por proposta aprovada;
- objetos novos `PAUSED`;
- `appsecret_proof` em chamadas servidor-servidor quando suportado.

Lead Ads usa webhook **e** pull de reconciliação. O inventário registra
`pages_manage_metadata`, `leads_retrieval`, tasks da Page, access tier, App Review e
Business Verification aplicáveis. Nenhum prazo de retenção de lead é presumido a partir
da fórmula de rate limit; o sync imediato e o prazo operacional devem ser confirmados na
documentação/painel vigente da conta.

### `marketing_center_meta_capi`

Conector Meta Conversions API.

Responsabilidades:

- envio de eventos server-side para dataset/pixel;
- dedupe com `event_id`;
- normalizacao e hash de dados de usuario;
- `action_source`, `event_source_url`, `fbc`, `fbp`, click IDs e UTMs;
- dataset/pixel de laboratório separado e Test Events apenas para inspeção;
- monitoramento de match quality e erros.

Uso inicial:

- eventos `Lead`, `Contact`, `QualifiedLead`, `Schedule`, `Purchase` ou equivalentes
  serao mapeados por politica, nao hardcoded no conector;
- comecar como observacao/teste antes de usar para otimizacao.

`test_event_code` não cria sandbox: o evento não é descartado e pode participar de
mensuração/targeting. Fixtures, E2E e eventos sintéticos só usam dataset/pixel marcado
`LAB` e allowlisted. `event_time` com mais de sete dias é bloqueado antes do envio e vai
para estado terminal/diagnóstico; não recebe retry cego.

### `marketing_center_crm`

Ponte com CRM.

Responsabilidades:

- vincular touchpoints a `crm.lead`;
- criar `marketing.business.event` de classe lifecycle;
- reconstruir historico de estagios a partir do chatter quando possivel;
- deduplicar leads por regras configuraveis;
- ser o único aplicador da tupla UTM nativa no lead, a partir de propostas de
  providers/bridges e do contrato `first_trusted_assignment`;
- aplicar a tupla em um único write fill-only com lock/CAS, preimage, after-image,
  policy/mapping revision, receipt idempotente, fencing e tombstone de edição humana;
- preservar IDs externos em modelos proprios, nao apenas nos campos UTM nativos.

Regra: `utm.campaign`, `utm.source` e `utm.medium` do Odoo continuam uteis para
interface nativa, mas nao sao suficientes como ledger de atribuicao.

### `marketing_center_sale`

Ponte opcional com vendas. Cria vínculos tipados com `sale.order` e eventos de proposta,
pedido confirmado, cancelamento, receita e margem disponível, sem obrigar o core a
instalar Sale.

### `marketing_center_account`

Ponte opcional com faturamento. Cria vínculos tipados com `account.move` e pagamentos,
eventos de fatura/recebimento/estorno e reconciliação de receita realizada, sem obrigar
o core a instalar Accounting.

### `marketing_center_contact_center`

Ponte opcional com atendimento.

Responsabilidades:

- depender explicitamente de `contact_center_base` e `marketing_center_base`, sem
  transformar essa dependência opcional em requisito de nenhum core;
- ler `AttributionDTO` e evidências operacionais já persistidas pelo Contact Center;
- mapear o contrato para `MarketingTouchpointDTO` e criar touchpoints canônicos de forma
  idempotente;
- criar links tipados de conversa, mensagem, identity e conta/caixa;
- executar ingestão contínua, backfill e replay versionado sem modificar a origem;
- expor no Marketing Center os fatos agregados de conversa/SLA autorizados;
- adicionar drill-down de Marketing para Atendimento sem alterar a UI canônica do
  Contact Center;
- manter conversas diferentes quando a mesma pessoa fala com várias caixas;
- permanecer desinstalável sem afetar recebimento, envio ou ledger operacional do
  Contact Center.

### `marketing_center_website`

Ponte com site Odoo.

Responsabilidades:

- capturar UTMs, `gclid`, `gbraid`, `wbraid`, `fbclid`, `fbc`, `fbp` e referrer;
- gerar token first-party para sessao/visitante;
- criar redirect controlado para WhatsApp com token de atribuicao;
- integrar formulários do site ao ledger antes de criar lead;
- reutilizar controllers e serviços de `marketing_center_web_ingress` quando aplicável;
- opcionalmente reaproveitar `link.tracker` do Odoo quando ele preservar a granularidade
  necessaria.

Decisão: recursos nativos do Odoo são canônicos no seu domínio. `link.tracker.click` é a
fonte do clique de links controlados pelo Odoo; para cada clique nativo elegível, o
Marketing Center não cria um segundo fato canônico. O ledger próprio permanece para
click IDs, externo/headless, referrer, handoff WhatsApp, correlação M:N e evidência que
o modelo nativo não representa. `link_tracker` permanece dependência opcional; uma
correlação customizada deve usar bridge próprio.

Dependências do adapter atual: `marketing_center_web_ingress` e `website` (o primeiro já
traz o base). `link_tracker` permanece opcional e, quando integrado, entra por bridge
próprio; não deve ser dependência obrigatória do Website adapter.

### `marketing_center_ui`

Interface operacional.

Responsabilidades:

- dashboards de performance e funil;
- tela de contas/conexoes/health;
- explorador de campanhas;
- analise por campanha/adset/ad/landing/conversa;
- tela de touchpoints e atribuicao de um lead;
- proposals/mudancas com aprovacao;
- logs de jobs e falhas tecnicas;
- comparativo Ads x CRM x Receita.

Começa com list/graph e pivot nativo apenas para medidas comprovadamente aditivas.
Reach, frequency, usuários e outras não aditivas usam relatório guardado que bloqueia
soma inválida. Um dashboard OWL próprio só entra quando definições e grains estiverem
estabilizados.

## Grafo de Dependências

```text
contact_center_meta
        -> contact_center_base
        -> meta_api_base
        -> meta_webhook_base

marketing_center_meta
        -> marketing_center_base
        -> meta_api_base
        -> meta_webhook_base

marketing_center_google
marketing_center_ga4
marketing_center_google_data_manager
        -> marketing_center_base
        -> google_api_base

marketing_center_ui
marketing_center_crm
marketing_center_sale
marketing_center_account
        -> marketing_center_base

marketing_center_contact_center
        -> contact_center_base + marketing_center_base

marketing_center_web_ingress
        -> marketing_center_base + web

marketing_center_website
        -> marketing_center_base + marketing_center_web_ingress + website/link_tracker
```

O sentido acima é `addon -> dependências`, não fluxo de dados. Nenhum bridge é
dependência de core, e os dois cores instalam e operam isoladamente.

Topologia de release obrigatória:

- repositório `contact-center`: somente `contact_center_*`;
- repositório `marketing-center`: `marketing_center_*`, `meta_api_base`,
  `meta_webhook_base` e `google_api_base`;
- releases autônomos de Contact Center e Marketing Center dependem somente das bases
  técnicas que efetivamente utilizarem. Addons OCA permanecem em `oca_dependencies.txt`;
  repositórios internos são pinados por commit/versão no manifesto e no script de
  release, com source e versão instalados verificados;
- um perfil integrado instala os dois repositórios e `marketing_center_contact_center`,
  sem transformar o bridge em dependência de core;
- `setup/`, CI, `addons_path`, script de deploy e manifesto de release registram cada
  commit efetivamente usado pelo perfil implantado.

Se for exigida independência estrita também do checkout completo dos repositórios, o
addon `marketing_center_contact_center` será empacotado num pequeno repositório de
bridges. Essa decisão de empacotamento não altera suas dependências Odoo nem os
contratos dos cores.

Esta pasta é o repositório OCA independente da suíte na branch `16.0`. Remotes e tags
canônicos de `contact-center` e `marketing-center`, além da matriz de release
autônoma/integrada e do checkout privado cruzado no CI, são gates do primeiro release.

## Modelos Canonicos

Regra de namespace:

- `marketing.attribution.*`: atribuição e evidência first-party pertencentes ao
  `marketing_center_base`;
- `meta.api.*` / `meta.webhook.*` / `google.api.*`: namespace reservado somente à
  identidade, transporte, credenciais por purpose, deliveries e estado técnico realmente
  compartilhados; nunca contém conversa, campanha, lead ou atribuição;
- `marketing.center.*`: domínio de campanha, métrica, sync e operação;
- `marketing.business.*` / `marketing.conversion.*`: fatos internos e deliveries;
- modelos bridge mantêm o prefixo do domínio que os possui.

`entity_type` classifica a entidade externa. `grain` define a granularidade de uma linha
de métrica. `level` é vocabulário eventual do provider e deve ser normalizado para
`grain`, não persistido como um terceiro conceito equivalente.

### Configuracao

- `marketing.center.source`: fonte lógica por empresa, serviço e conta externa; guarda
  moeda, timezone, capabilities efetivas e flags independentes de leitura/escrita.
- `marketing.center.connection`: guarda identidade/revisão opacas do perfil técnico; o
  addon provider cria a FK concreta consumer-specific, como
  `marketing.center.meta.profile`. Trocar token não cria nova fonte de negócio.
- `marketing.center.external.entity`: projeção atual de account, campaign, group/adset,
  ad, creative, asset, form, conversion action, dataset/pixel ou outra entidade.
- `marketing.center.external.entity.revision`: observação append-only dos campos
  externos relevantes e hash do snapshot.
- `marketing.center.sync.cursor`: cursor por conector, fonte, entidade, granularidade e
  janela.
- `marketing.center.sync.run`: execução auditável, janela, job UUID, request IDs,
  contagens, hash, duração e erro seguro.

Campos externos são `Char`, mesmo quando parecem números. A unicidade mínima de uma
entidade é `(source_id, entity_type, external_ref)`. Nome nunca participa da chave. A
hierarquia usa `parent_id`, enquanto `group_type` diferencia `google_ad_group`,
`google_asset_group` e `meta_adset` sem contaminar o core.

`external_ref` é o resource name completo do provider quando existir; `external_id` é
apenas o ID curto de apresentação/consulta. Sem resource name, o adapter compõe uma
referência estável com o pai, por exemplo `{ad_group_id}~{criterion_id}`. Google
`ad_group_ad`, criteria e associações de asset são entidades compostas. IDs opacos
preservam caixa e bytes normalizados pelo contrato; nunca passam por `lower()`. Entidade
que desaparece do provider recebe tombstone/revisão (`remote_missing_at`) e sai da
projeção operacional; não é apagada nem inferida como removida por uma única página
incompleta.

### Performance

- `marketing.center.metric.daily`: projeção corrente do fato diário por fonte, entidade,
  grain e conjunto controlado de dimensões.
- `marketing.center.metric.revision`: observação imutável de cada valor recebido;
  permite auditar restatements tardios das plataformas.
- `marketing.center.metric.action.daily`: ações variáveis por namespace, como tipos Meta
  e conversion actions Google, sem transformar o core em um JSON opaco.
- `marketing.center.metric.import.batch`: lote de importação, janela, cursor, status,
  quota e request IDs.

Metricas minimas:

- custo, moeda e timezone;
- impressions, clicks, CTR, CPC, CPM;
- reach e frequency quando a plataforma fornecer;
- conversions, conversion_value e all_conversions quando aplicavel;
- leads/forms/messages reportados pela plataforma;
- impression share e perdas por budget/rank no Google;
- quality score/landing page experience quando disponivel;
- delivery/learning/relevance diagnostics no Meta quando disponivel.

Regras semânticas:

- custo recebido em micros é persistido em coluna PostgreSQL `bigint`/`numeric`, nunca
  no `int4` padrão de `fields.Integer`; valores monetários projetados usam precisão
  decimal explícita;
- contagens fracionárias de conversão usam `Decimal`;
- CTR, CPC, CPM, CPA, frequência e ROAS são calculados a partir dos componentes
  armazenados, não tratados como fatos aditivos;
- `reach`, `frequency`, usuários e outras medidas não aditivas carregam sua janela e não
  podem ser somadas entre breakdowns;
- moeda e timezone pertencem a toda linha; conversão cambial é uma projeção separada;
- `marketing.center.metric.*` guarda somente `platform_reported`; fatos `odoo_actual`
  permanecem em `marketing.business.event` e resultados `modelled` em
  `marketing.attribution.result`. Um UiDTO unificado informa a origem, sem fundir os
  diferentes tipos de fatos;
- breakdowns são combinações allowlisted. Consultas arbitrárias e de alta cardinalidade
  não entram no banco operacional;
- a plataforma pode revisar dias anteriores; o job grava uma nova revision e atualiza
  atomicamente a projeção corrente, sem apagar a observação anterior.

Identidade do fato corrente:

```text
(source_id, report_date, grain, entity_id, dimension_hash,
 metric_origin, reporting_context_hash)
```

`reporting_context_hash` inclui timezone, janela/modelo de atribuição, interaction date
versus conversion date, conjunto de conversion actions e qualquer opção que altere a
semântica. Ações variáveis usam `(metric_daily_id, action_key_hash)`; o hash inclui
namespace, action type, conversion action, attribution window e value kind.

Revisões usam sequência por projeção, não `observed_at` nem apenas `content_hash`:

1. sob lock curto da projeção, hash igual ao atual não cria revisão e apenas atualiza
   metadados `first/last_observed_at`/batch;
2. hash diferente cria `revision_sequence + 1` e troca a projeção atomicamente;
3. a sequência A→B→A continua registrada como três estados.

O mesmo algoritmo vale para `marketing.center.external.entity.revision`.

Fronteira temporal de cada fato:

- `report_date` usa o calendário da conta externa;
- `report_timezone` guarda o timezone IANA e sua revisão observada;
- `period_start_utc`/`period_end_utc` tornam a fronteira reproduzível;
- eventos Odoo continuam armazenados em UTC;
- no comparativo Ads × Odoo, eventos internos são agrupados pelo timezone da fonte
  publicitária atribuída;
- uma visão pelo timezone da companhia é outra projeção, nunca reinterpreta ou
  sobrescreve o fato da plataforma.

### Atribuicao

Os modelos abaixo pertencem ao `marketing_center_base` e formam o ledger canônico da
jornada de marketing. Eles não substituem `contact.center.attribution.*`, cuja
finalidade é preservar evidência operacional de atendimento.

- `marketing.attribution.touchpoint`: evidência append-only de origem/entrada.
- `marketing.attribution.identifier`: ID externo namespaced e classificado por papel.
- `marketing.attribution.evidence`: referência opaca à fonte, fingerprint e digest
  sanitizado; não é um vínculo ORM genérico para qualquer registro.
- `marketing.attribution.supersession`: declara correção, conflito ou invalidação sem
  editar/destruir o touchpoint original.
- `marketing.attribution.model`: definição e versão do modelo de atribuição.
- `marketing.attribution.result`: resultado materializado por entidade de negócio,
  janela e versão do modelo.

`marketing.attribution.result` tem cardinalidade explícita por
`(company_id, subject_kind, subject_key, model_id, model_version, window_start, window_end, calculation_run_id)`.
O resultado aponta para contribuições/touchpoints com pesos separados; uma nova execução
não sobrescreve o cálculo anterior.

Vínculos concretos vivem nos respectivos addons de bridge, fora do base:

- `marketing.attribution.contact.center.link`, declarado em
  `marketing_center_contact_center`: conversa, mensagem, identity e caixa;
- `marketing.attribution.crm.link`, declarado em `marketing_center_crm`: lead e
  oportunidade;
- `marketing.attribution.sale.link`, declarado em `marketing_center_sale`:
  cotação/pedido;
- `marketing.attribution.account.link`, declarado em `marketing_center_account`:
  fatura/pagamento;
- `marketing.attribution.website.link`, declarado em `marketing_center_website`:
  visitante, sessão, submissão e link.

Campos importantes do touchpoint:

- empresa;
- source system;
- referência opaca à evidência de origem;
- occurred_at;
- observed_at;
- platform;
- channel;
- touchpoint_type;
- evidence_level;
- privacy/purpose snapshot quando conhecido: policy/notice version, legal basis code,
  consent status triestado, origem e data da decisão;
- landing URL/referrer normalizados;
- UTMs completas;
- IDs de ativo não pessoais (`campaign_id`, `adset/ad_group_id`, `ad_id`, `creative_id`,
  `form_id`, `source_id`);
- fingerprint canonico;
- `canonical_key`, schema version e hash do conteúdo.

Click/person/session IDs (`gclid`, `gbraid`, `wbraid`, `dclid`, `fbclid`, `fbc`, `fbp`,
`ctwa_clid`) ficam exclusivamente em `marketing.attribution.identifier`, com ACL,
finalidade e retenção próprias. A URL canônica é persistida sem fragmento, credenciais,
PII ou esses parâmetros; UTMs extraídas ficam nos campos próprios.

O touchpoint original não muda de estado nem recebe update de enriquecimento. Correções,
conflitos e enriquecimentos monotônicos são novos registros relacionados; uma projeção
ORM comum expõe o estado efetivo atual.

### Funil e Receita

- `marketing.business.event`: ledger imutável comum, com `event_class` igual a
  `lifecycle` ou `revenue`.
- `marketing.lead.score.snapshot`: qualidade do lead por regra versionada.
- `marketing.reconciliation.run`: conciliacao entre Ads, touchpoints, CRM e receita.

Campos essenciais de `marketing.business.event`:

- `company_id`, `event_class`, `event_type` e `source_system`;
- `source_model`, `source_res_id` e `source_occurrence_ref` tipados pelo bridge;
- `business_event_key`, `occurred_at` UTC e evidence level;
- `amount_signed`/`currency_id` quando houver valor;
- `reverses_event_id` e `root_event_id` para correção/estorno.

```text
UNIQUE(company_id, source_system, business_event_key)
```

Tabela normativa de gatilhos e chaves:

| Evento                        | Gatilho Odoo 16                                      | Chave natural                                                             |
| ----------------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------- |
| `lead_created`                | `crm.lead.create` concluído                          | `crm.lead:<id>:created`                                                   |
| `conversation_started`        | primeira mensagem externa projetada na conversa      | `contact.center:<channel_public_ref>:started`                             |
| `first_human_response`        | primeiro outbound humano confirmado da conversa      | `contact.center:<channel_public_ref>:first_response:<message_public_ref>` |
| `qualified`/`won`/`lost`      | transição semântica real, comparando antes/depois    | `crm.lead:<id>:transition:<source_sequence>`                              |
| `proposal_sent`               | primeira transição real da cotação para enviada      | `sale.order:<id>:proposal_sent:<source_sequence>`                         |
| `order_confirmed`             | transição real para `sale`/`done`                    | `sale.order:<id>:confirmed:<source_sequence>`                             |
| `order_cancelled`             | transição real para cancelada                        | nova chave; reverte a confirmação aplicável                               |
| `invoice_posted`              | `draft -> posted` em `account.move.action_post`      | `account.move:<id>:posted:<source_sequence>`                              |
| `credit_note_posted`          | postagem de `out_refund`                             | chave da nota; vincula `reversed_entry_id` quando houver                  |
| `payment_allocated`           | criação de `account.partial.reconcile` em recebíveis | `account.partial.reconcile:<id>:created`                                  |
| `payment_allocation_reversed` | remoção do partial reconcile                         | `account.partial.reconcile:<id>:removed`; reverte a alocação              |

`source_sequence` é uma ocorrência persistida na mesma transação; em backfill, deriva de
evidência estável como `mail.tracking.value.id`. Reexecutar um método sem transição não
cria evento. `payment_state` é computado e não é gatilho. Três parcelas geram três
`payment_allocated`; `cash_received` e `payment_allocated` são conceitos diferentes e
nunca são somados no mesmo KPI.

### Operacao e Mutacoes

- `marketing.change.request`: proposta de mudanca em campanha.
- `marketing.change.item`: delta estruturado por objeto/campo.
- `marketing.approval`: aprovador, politica, validade e escopo.
- `marketing.platform.command`: comando externo a executar.
- `marketing.conversion.event`: evento provider-neutral que referencia exatamente um
  `marketing.business.event`, ainda sem escolher o destino.
- `marketing.conversion.outbox`: evento de conversao a enviar.
- `marketing.job.audit`: resumo tecnico de execucoes criticas.

Estados:

- change request: `draft`, `analysis`, `pending_approval`, `approved`, `scheduled`,
  `running`, `applied`, `partially_applied`, `stale`, `uncertain`, `rejected`,
  `cancelled`, `failed`, `rolled_back`;
- command: `pending`, `processing`, `retry`, `uncertain`, `done`, `dead`, `cancelled`;
- conversion outbox: `pending`, `sent`, `accepted`, `partial_error`, `retry`, `dead`,
  `uncertain`, `cancelled`.

Um `marketing.conversion.event` pode gerar zero ou várias deliveries. Por exemplo, a
mesma venda pode ser elegível para Meta CAPI e Google Data Manager, mas continua sendo
um único fato Odoo. O resultado de um destino nunca altera o fato nem marca o outro como
entregue.

Cada delivery persiste uma referência externa determinística e imutável. A delivery de
reversão reutiliza a correlação exigida pelo destino: por exemplo, `transaction_id` no
Data Manager e, quando aplicável a um fluxo legado permitido, `order_id` Google. O
adapter nunca recalcula essa referência a partir do nome ou do estado atual do pedido.

## DTOs

DTO é a API local e versionada entre o core e os adapters. Ele não é o payload da
Meta/Google, nem um endpoint HTTP público. O provider normaliza inbound para DTO e
traduz Command/Conversion DTO para outbound. Campos desconhecidos ficam em extensão
namespaced, nunca promovidos silenciosamente ao contrato comum.

### `MarketingPerformanceDTO`

- schema_version;
- platform;
- account_ref;
- asset_ref;
- report_date ou date_start/date_stop;
- currency;
- report_timezone IANA;
- grain: `account`, `campaign`, `adgroup`, `ad`, `creative`, `keyword`, `landing_page`;
- dimensions;
- metrics;
- metric_origin, fixado em `platform_reported` neste DTO;
- reporting_context e attribution window/model;
- external_ids;
- provider_schema_version;
- request_metadata sanitizado;
- row_fingerprint.

`dimensions` e `metrics` são enums/objetos validados pelo schema, não dicionários
arbitrários aceitos do provider.

### `MarketingTouchpointDTO`

- schema_version;
- source_system;
- platform;
- channel;
- touchpoint_type;
- evidence_level;
- occurred_at;
- identifiers[];
- utm;
- landing;
- `source_evidence_ref` opaca;
- `correlation_refs[]` opcionais e namespaced;
- privacy snapshot opcional da evidência: policy/notice version, legal basis code e
  consent status triestado (`granted`, `denied`, `unknown`);
- extensions namespaced.

Este é o contrato de ingestão do ledger do Marketing Center e não evolui nem substitui o
`AttributionDTO` usado no Contact Center. O `marketing_center_contact_center` mantém um
mapper versionado entre os dois contratos. Referências a conversa, identidade, caixa,
CRM ou outro domínio são correlações opacas; somente o bridge autorizado pode
convertê-las em FKs Odoo.

### `MarketingConversionDTO`

- schema_version;
- event_id;
- event_name;
- occurred_at;
- source_touchpoints[];
- lead/order/invoice refs;
- value/currency;
- match-data references autorizadas;
- click identifiers;
- action_source;
- event_source_url;
- `data_use_policy_ref` obrigatória;
- legal basis code e consent status triestado;
- destination policy.

O evento canônico não guarda payload Meta/Google. Cada adapter busca somente os dados de
matching autorizados, normaliza e aplica hash imediatamente antes de montar sua
delivery. Payload transmitido e diagnóstico são restritos por grupo e retenção.
Consentimento não é presumido como a única base possível, mas nenhuma delivery nasce
elegível sem decisão explícita da policy para a finalidade/destino; ausência ou
`unknown` incompatível gera `blocked_policy`, sem envio nem retry.

### `SyncPageDTO`

Envelope de retorno obrigatório para sync síncrono, paginado ou assíncrono:

- `items` tipados;
- `next_cursor` e `has_more`;
- `provider_request_id`;
- `provider_job_ref` e estado quando o provider gerar relatório assíncrono;
- `watermark`;
- `reporting_context_hash`;
- `retry_after` opcional e erro seguro por item/página.

O cursor canônico avança somente na mesma transação que persiste os `items`. Um
`provider_job_ref` pendente agenda polling e não é tratado como página vazia.

### `MarketingChangeDTO`

- schema_version;
- platform;
- account_ref;
- object_type;
- object_ref;
- operation;
- before_snapshot;
- after_snapshot;
- risk_summary;
- approval_ref;
- idempotency_key;
- expected_remote_revision/hash;
- requested_at/expires_at.

## Contratos de Adapter

Cada conector de marketing implementa um registry explícito, sem `if platform == ...`
espalhado pelo core:

```text
discover_sources()
check_health()
list_capabilities()
sync_entities(scope, cursor, provider_job_ref=None) -> SyncPageDTO
sync_metrics(query, cursor, provider_job_ref=None) -> SyncPageDTO
prepare_conversion(event, policy)
send_conversion(delivery)
prepare_command(change_request)
execute_command(command)
read_back(command)
```

Adapters podem declarar capabilities como `read_entities`, `read_metrics`,
`receive_leads`, `send_conversion`, `pause_entity`, `change_budget` e
`create_paused_campaign`. A UI deriva botões dessas capabilities e das ACLs; nunca
presume recurso apenas pelo nome da plataforma.

## ACL e Record Rules

Roster próprio, sem dependência do Contact Center:

- `marketing.center.team`;
- `marketing.center.team.member`: time, usuário, role e validade;
- `marketing.center.team.source`: time, fonte e modo `read|prepare|operate|approve`.

Grupos iniciais:

- `Marketing Viewer`: lê dashboards, fontes, entidades, performance e runs vinculados ao
  seu roster, sem identificadores sensíveis. O ledger bruto e a projeção de touchpoints
  efetivos permanecem admin-only.
- `Marketing Analyst`: cria análises e change requests draft sobre o escopo visível.
  Disparar sync continua reservado ao administrador porque cruza a fronteira de I/O e
  usa credenciais externas, ainda que o adapter seja read-only.
- `Marketing Operator`: prepara campanhas/alteracoes `PAUSED`, mas nao aprova sozinho.
- `Marketing Manager`: aprova mudancas dentro dos tetos configurados.
- `Marketing Administrator`: configura conexoes, credenciais, policies e scopes.
- `System Administrator`: acesso full por administracao tecnica.

Regras:

- Escopo sempre por `company_id`.
- Usuário vê somente fontes/contas vinculadas ao seu roster, exceto admin.
- Uma fonte pode ser compartilhada por vários times e um time pode operar várias fontes;
  conta publicitária não deve ser codificada diretamente no usuário.
- Identificadores sensiveis ficam em modelo separado e grupo restrito.
- Dados hashados de usuario nao aparecem em views de operador.
- Mutacoes exigem grupo, policy e aprovacao valida.
- Nenhum endpoint publico aceita token de usuario Odoo como autenticacao externa.
- Aprovação do próprio autor é bloqueada para mudança que aumenta gasto, altera
  bidding/targeting ou ativa entrega; a policy pode liberar self-approval apenas para
  operações explicitamente classificadas como baixo risco.
- Leitura e escrita externas exigem capabilities e credenciais separadas. Revogar o
  writer não pode interromper dashboards read-only.

Enforcement de aprovação:

- `state` e `marketing.approval` não aceitam escrita genérica da UI;
- somente `action_approve()` cria o registro append-only e faz a transição;
- o método valida grupo, roster, empresa, policy/teto, autoria e digest do diff;
- a aprovação não aceita `write()`/`unlink()` funcional;
- o job revalida validade, vínculo do aprovador, policy version, digest, revisão remota,
  capability, writer ativo e ausência de self-approval proibida;
- aprovação de uso único é reservada/consumida atomicamente antes do boundary.

## Credenciais e Segredos

Segredo de produção não é persistido no PostgreSQL, seja reader, writer, CAPI, Data
Manager, Meta app secret ou token de mensageria. O perfil guarda somente:

- backend e referência opaca allowlisted;
- purpose, scopes/capabilities, company e serviço;
- revision/fencing, expiração, fingerprint mascarado e health;
- metadados de rotação, nunca o valor.

O MVP suporta secret file/`EnvironmentFile` montado read-only e modo `0600`; um cofre
pode implementar o mesmo resolver. Jobs carregam somente a identidade do registro e a
revisão esperada, validam empresa/capability/revision e resolvem o valor server-side no
instante de uso. O segredo nunca entra em argumento do `queue_job`, RPC, chatter, log ou
diagnóstico. Um ledger específico de resolução de credenciais ainda é backlog; não faz
parte do corte entregue.

Webhook síncrono usa backend local montado/read-only ou cache seguro para não depender
de I/O remoto antes da verificação. Writer nunca tem fallback em campo `Char`. Os campos
legados do `contact_center_meta` são compatibilidade temporária exclusiva de
laboratório, sem capability de escrita de campanha, e devem ser migrados/limpos antes de
produção. Rotacionar a referência incrementa a revisão e invalida jobs antigos.

## Fluxos Transacionais

### Sync de estrutura e performance

```text
cron Odoo (somente agenda)
    -> cria/atualiza marketing.center.sync.run
    -> commit
    -> queue_job por fonte + serviço + janela
    -> adapter consulta provider fora da transação de persistência
    -> valida DTO, moeda, timezone, grain e limites
    -> transação curta grava revisões + projeção + cursor
    -> reconcilia contagem/hash e conclui o run
```

O cursor só avança junto com a persistência do lote. A janela recente é relida para
capturar conversões tardias; a janela histórica é reaberta por job explícito.

### Webhook Meta

```text
POST no callback único do meta_webhook_base
    -> limita tamanho e resolve a identidade revisionada do App
    -> meta_api_base fornece App Secret por referência + verificação HMAC
    -> persiste delivery/change sanitizada, deduplicada e roteável
    -> agenda consumer na mesma transação curta + 2xx
    -> messages -> queue_job do contact_center_meta
    -> leadgen  -> queue_job do marketing_center_meta
                    -> GET /v26.0/{leadgen_id} com o perfil Lead
                    -> valida DTO -> MarketingTouchpointDTO / ledger
```

Nenhum consumer nem o router faz I/O Graph antes do acknowledge. Evento desconhecido ou
sem consumer permanece `unrouted`; não é descartado nem convertido em touchpoint
genérico. O webhook `leadgen` é somente hint: PII e IDs canônicos adicionais vêm do GET
autenticado e o pull reconciliador converge pelo mesmo `leadgen_id`.

No laboratório o cutover dos controllers antigos é direto, sem dual-write e sem migração
obrigatória do histórico técnico. Ledgers canônicos já existentes em cada domínio
permanecem intactos.

### Conversão server-side

```text
transição CRM/Sale/Accounting
    -> business event + conversion event na mesma transação
    -> policy cria delivery por destino
    -> commit
    -> queue_job prepara dados, envia lote e persiste resultado por item
    -> diagnóstico assíncrono/read-back quando a API o oferecer
```

O evento de negócio jamais é recriado porque a API falhou. Ajuste, cancelamento ou
estorno produz novo evento correlacionado conforme a semântica do destino.

### Mutação de campanha

```text
UI cria change request + diff + hipótese
    -> policy classifica risco e aprovações necessárias
    -> aprovação gera command/outbox
    -> commit
    -> queue_job revalida capability, teto e precondition remota
    -> mutate
    -> read-back confirma ou marca uncertain
```

Aprovação expira. Se a revisão/hash remoto mudou depois da proposta, o comando fica
`stale` e volta para análise. Timeout após o boundary externo nunca causa retry cego.

## Queue Job

Canais sugeridos:

```text
root.marketing
root.marketing.sync
root.marketing.performance
root.marketing.webhook
root.marketing.conversion
root.marketing.command
root.marketing.reconcile
root.marketing.health
```

Padroes:

- Todo job possui chave natural de idempotencia.
- Cron e request apenas agendam; não fazem I/O de provider.
- Retry exponencial via `queue_job`, sem `seconds` fixo salvo quando a API retornar
  `Retry-After`.
- Rate limit por conexao/ativo, nao global.
- `429`, `RESOURCE_EXHAUSTED`, `User request limit reached` e equivalentes viram retry
  com cooldown.
- Timeout ambiguo de mutacao nunca e tratado como sucesso nem reexecutado cegamente:
  entra em `uncertain` e exige reconciliacao.
- Jobs de sync sao fatiados por conta, entidade e janela.
- Jobs de conversao aceitam erro parcial por item.
- Lock transacional nunca permanece aberto durante HTTP/SDK I/O.
- Cada job leva `connection_revision`/fencing token. Troca de credencial ou desativação
  invalida trabalho antigo antes do boundary.
- Concorrência e quota são limitadas por provider, credencial e conta externa; uma conta
  em cooldown não paralisa as demais.
- Payload grande é paginado/streamed e persistido em lotes curtos. Jobs não acumulam um
  relatório inteiro em memória.

## Idempotencia

Chaves minimas:

```text
UNIQUE(company_id, service, external_account_ref)         # source
UNIQUE(source_id, entity_type, external_ref)              # entity atual
UNIQUE(entity_id, revision_sequence)                      # entity revision
UNIQUE(source_id, report_date, grain, entity_id,
       dimension_hash, metric_origin, reporting_context_hash) # metrica atual
UNIQUE(metric_daily_id, action_key_hash)                  # action daily
UNIQUE(metric_daily_id, revision_sequence)                # metric revision
UNIQUE(company_id, source_system, canonical_key)          # touchpoint
UNIQUE(touchpoint_id, namespace, role, value_hash)        # identifier
UNIQUE(company_id, source_model, source_public_ref,
       mapping_version)                                   # importação pelo bridge
UNIQUE(company_id, source_system, business_event_key)     # business event
UNIQUE(business_event_id, conversion_semantic, policy_version) # conversion event
UNIQUE(conversion_event_id, destination, policy_version)  # delivery
UNIQUE(company_id, change_request_uuid)
UNIQUE(change_request_id, object_type, object_ref, field)
UNIQUE(platform_command_id, provider_request_fingerprint) # attempt
```

Regras:

- `gclid`, `fbclid`, `ctwa_clid` e IDs similares sao evidencia, nao a unica chave.
- Replay de webhook Lead Ads reutiliza o mesmo touchpoint.
- Enriquecimento cria evidência/projeção monotônica; não atualiza o touchpoint nem
  sobrescreve ID crítico divergente.
- Conflito vira registro explicito, nao merge silencioso.
- Constraints são materializadas no PostgreSQL, incluindo índices parciais quando o
  conceito de item ativo não puder ser expresso por `_sql_constraints`.
- Chaves de idempotência são determinísticas e independem do UUID do `queue_job`.

`canonical_key` v1 do Marketing Center não inclui `connection.id` nem outro ID ORM:

```text
sha256(canonical_json({
  "version": 1,
  "source_system": "...",
  "source_scope_ref": "external account/page/phone/dataset ref",
  "source_kind": "message|lead|form_submission|redirect|...",
  "source_event_ref": "stable external event ref",
  "touchpoint_type": "..."
}))
```

O JSON usa chaves ordenadas, distingue ausente de vazio e preserva IDs opacos
literalmente. `company_id` fica na constraint externa ao hash. O registro guarda
`canonical_key_version` para evoluções futuras. A chave de importação do bridge usa a
referência pública estável da evidência do Contact Center e a versão do mapper; ela não
reutiliza IDs ORM como identidade de negócio. Colisão com conteúdo diferente vira
conflito, nunca merge automático.

## Estrategia de Atribuicao

Modelos suportados desde cedo:

- first touch;
- last paid touch;
- last touch;
- linear simples;
- plataforma reportada;
- modelo custom Soloz, a definir depois de dados reais.

Nenhum modelo isolado sera apresentado como verdade unica. Dashboards devem exibir:

- atribuicao Odoo/CRM;
- atribuicao por touchpoints first-party;
- atribuicao reportada por Google/Meta;
- discrepancia e cobertura.

## Website e Rastreamento

Captura de origem não aguarda o futuro Website Odoo. O site atual em WordPress usa o
contrato HTTP do `marketing_center_web_ingress`; quando o site migrar, o
`marketing_center_website` troca apenas o adapter e adiciona integração nativa com
`utm.mixin`, `link.tracker` e `website.visitor`.

Componentes:

- cookie first-party de visitante;
- armazenamento de UTMs e click IDs;
- snippet/server proxy WordPress e, futuramente, controller Website Odoo;
- redirect `/m/r/<token>` para WhatsApp, preservando token antes de abrir o app;
- parametros em links internos sem poluir URLs finais desnecessariamente;
- integração opcional com `utm.mixin`, `link.tracker` e `website.visitor`.

Campos que devem ser preservados:

- `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term`;
- `gclid`, `gbraid`, `wbraid`, `dclid`;
- `fbclid`, `fbc`, `fbp`;
- landing page, referrer, user agent fingerprint reduzido, IP hash quando aprovado;
- form ID, button ID, page variant e experiment ID quando existir.

Click IDs são extraídos antes de sanitizar a URL e persistidos somente no identifier
restrito. A captura deve ocorrer sob origem first-party ou proxy server-side; habilitar
CORS para um host Odoo separado não transforma storage de terceiro em first-party.

Contrato de `/m/r/<token>`:

- token aleatório, opaco, de alta entropia e armazenado somente por digest;
- destino/número fixado server-side e allowlisted; não aceita `next` ou URL arbitrária;
- expiração, empresa/campanha, finalidade e limite de uso explícitos;
- token de redirect separado do identificador de visitante/sessão;
- rate limit por token/rede e classificação de bot/prefetch, que nunca conta como
  conversa ou conversão humana;
- persistência/dedupe antes do redirect, `Cache-Control: no-store` e referrer policy;
- fallback fixo e seguro para token inválido/expirado;
- correlação é best-effort: o usuário pode remover o token da mensagem pré-preenchida.

## Google - O que Da Para Fazer

Leitura:

- listar contas acessiveis, MCC e customers;
- campanhas, budgets, ad groups, ads, assets, keywords, negative keywords;
- conversion actions e goals;
- performance por GAQL em varios niveis;
- search terms, landing pages, segmentos de device/localizacao/rede;
- impression share e perdas por budget/rank;
- change events e change status;
- recomendacoes e diagnosticos suportados.

Escrita controlada:

- criar campanhas, ad groups, ads, assets e keywords;
- pausar/reativar objetos;
- alterar budget;
- alterar lances/estrategia quando policy permitir;
- adicionar negativas;
- ajustar URLs, tracking templates e final URL suffix;
- criar/editar conversion actions com aprovacao.

Conversoes:

- novos fluxos devem usar Google Data Manager para conversoes e audiencias;
- Google Ads API pode continuar para inventario e estrutura;
- Enhanced Conversions for Leads exige normalizacao/hash e regras de consentimento.

Limites relevantes:

- quotas por developer token e nivel de acesso;
- o token atual em Explorer tem 2.880 operações por janela móvel de 24 h; Basic ou
  topologia de sync compatível é gate antes de ampliar contas/granularidade;
- `SearchStream` conta como uma operacao de API;
- respostas grandes devem usar streaming ou reduzir campos;
- mutate tem limite de operacoes por request;
- Data Manager possui limites próprios por request, destino, identificador, minuto e
  dia, observados pelo adapter;
- `RESOURCE_EXHAUSTED` deve virar cooldown por conexao/token.

## Meta - O que Da Para Fazer

Leitura:

- Business, ad accounts, campanhas, ad sets, ads e creatives;
- Insights por nivel, periodo e breakdown permitido;
- status, delivery, learning e rejeicoes;
- forms e leads de Lead Ads;
- pixels/datasets, custom conversions e eventos;
- Page/Instagram assets quando escopo permitir;
- historico suficiente para reconciliar snapshots.

Escrita controlada:

- criar campanha/adset/ad/creative;
- pausar/ativar objetos com aprovacao;
- alterar budget, schedule, targeting e placement quando policy permitir;
- criar formulários Lead Ads se o escopo for aprovado;
- configurar ou auditar datasets/pixels/custom conversions;
- publicar CAPI server events.

Conversoes:

- Meta CAPI envia eventos com `event_id` para dedupe com Pixel;
- usar `fbc/fbp` e dados hashados quando disponiveis;
- `action_source` e `event_source_url` devem ser consistentes;
- começar em dataset/pixel `LAB`; Test Events serve para inspeção e não para isolamento.

Limites relevantes:

- rate limit varia por app, conta, usuario e endpoint;
- Insights grandes devem usar jobs async quando necessario;
- erros de permissionamento, token e asset mismatch bloqueiam somente a conexao afetada;
- Graph API deve ser sempre versionada.
- Limited Access é apenas para desenvolvimento; Full Access, App Review, permissões
  avançadas e Business Verification são gates observados por app/asset.

## Bibliotecas e Ferramentas

Preferidas para runtime:

- Google Ads:
  [`googleads/google-ads-python`](https://github.com/googleads/google-ads-python), SDK
  oficial, após o spike de dependências;
- GA4: clientes oficiais `google-analytics-data`;
- Google APIs gerais:
  [`googleapis/google-api-python-client`](https://github.com/googleapis/google-api-python-client)
  quando houver client gerado adequado;
- Google Data Manager: REST/client oficial disponível no Google Cloud;
- Meta: cliente fino extraído do Contact Center e, quando trouxer vantagem concreta,
  [`facebook/facebook-python-business-sdk`](https://github.com/facebook/facebook-python-business-sdk).

Ferramentas auxiliares que podem complementar, mas não comandar o domínio:

- [`airbytehq/airbyte`](https://github.com/airbytehq/airbyte) ou PyAirbyte para ELT
  read-only rumo ao warehouse;
- Singer/Meltano taps avaliados por conector e manutenção para cargas analíticas;
- [`n8n-io/n8n`](https://github.com/n8n-io/n8n) para alertas e protótipos periféricos;
- dbt no warehouse para modelos analíticos, sem substituir ledgers/outboxes do Odoo.

Permitidas somente para laboratorio/diagnostico:

- `googleads/google-ads-mcp`;
- MCPs comunitarios de Meta/Google;
- scripts exploratorios locais.

Nao usar como runtime canonico:

- MCP como executor permanente de campanha;
- SDK abandonado sem suporte a versao atual;
- scraping de UI;
- n8n como unico ledger;
- CSV manual como fonte de verdade.
- clientes privados de Instagram, como `instagrapi`, `aiograpi` e
  `instagram-private-api`, para Ads, autenticação ou operação de contas;
- qualquer biblioteca que dependa de cookies de navegador, scraping ou endpoint privado
  para mutação de verba/campanha.

Biblioteca não oficial pode inspirar fixture ou acelerar uma prova read-only isolada,
mas não define contrato, fonte canônica nem dependência runtime sem ADR específico.

## Fases

### Fase 0 - Decisao e inventario read-only

Entregas:

- fixar o tenant inicial em **Soloz Industrial / Odoo 16**; Soloz Energia/Odoo 18 fica
  fora do primeiro deploy, embora os contratos permaneçam multiempresa;
- confirmar empresas/tenants e contas Google/Meta;
- mapear credenciais existentes;
- listar campanhas, conversoes, pixels/datasets, GA4 e formularios;
- registrar baseline 30/90 dias;
- registrar lacunas do WordPress atual, Google tag/GTM/GA4 e Meta Pixel;
- inventariar aceite dos termos Google/Meta, ECL/Data Manager, App Review, Business
  Verification, permissões Lead Ads e access tier Meta;
- registrar o developer token Google atual `Explorer`, seu teto de 2.880 operações por
  janela móvel de 24 h e o caminho para Basic antes de excedê-lo;
- criar/confirmar ad account/dataset/pixel/GA4/test destinations com nome `LAB`, sem
  usar ativos de produção para fixtures;
- medir capacidade do JobRunner compartilhado com o Contact Center; o lab atual usa
  `root:1`, e oito nomes de canal não criam paralelismo sozinhos;
- registrar Python 3.10/OCB16 e plano de runtime suportado após o EOL do Python 3.10;
- confirmar se `sale_margin` ou outra fonte auditável de custo/margem será instalada;
- definir remote/branch/tag de `integration-core`, `contact-center` e
  `marketing-center`, com perfis de release autônomos e integrado.

Aceite:

- nenhum metodo de escrita chamado;
- relatorio com contas, ativos, permissoes e gaps;
- IDs sensiveis sanitizados;
- plano de credenciais reader/writer aprovado;
- dataset/pixel `LAB` e allowlist documentados;
- orçamento de operações Google e capacidade da fila aprovados;
- topologia de repositório/CI/deploy provada sem dependência circular.

### Fase 1 - Fundações compartilhadas

Status: **entregue**. `meta_api_base` revisionado, `meta_webhook_base` e o cutover do
`contact_center_meta` foram implantados sem regressão. O `google_api_base` REST v25
também foi instalado isoladamente e passou 33/33 testes Odoo, apply e replay no
servidor05. Os consumers e suas credenciais permanecem independentes.

Entregas:

- scaffold de `meta_api_base` e `google_api_base` no repositório `integration-core`;
- extração do transporte Graph e HMAC hoje presentes em `contact_center_meta`,
  preservando seus contratos públicos;
- identidade técnica do mesmo Meta App em `meta_api_base` e perfis Page/Ads/Lead
  separados por purpose, referência de segredo e capability;
- `meta_webhook_base` com callback, delivery/dedupe técnico, registry e replay sem
  importar nenhum domínio;
- handlers independentes: mensagens no `contact_center_meta` e `leadgen` no
  `marketing_center_meta`;
- spike real do SDK Google Ads no mesmo runtime do Odoo 16/OCB;
- resolvers, perfis reader/writer, capability, fencing e health consumer-specific sem
  segredo no banco/log;
- atualizar `setup/`, CI, addons paths, deploy e manifesto de release dos três
  repositórios e dos perfis autônomos/integrado; usar `oca_dependencies.txt` somente
  para dependências OCA.

Aceite:

- Contact Center continua recebendo/enviando Meta Messaging sem regressão;
- um único callback do Meta App valida/persiste uma vez e roteia mensagem e Lead Ads
  somente ao consumer correspondente;
- Page, Ads e Lead usam referências de token distintas no mesmo App;
- cutover de laboratório ocorre sem dual-write; endpoints antigos deixam de receber
  novas deliveries e histórico técnico antigo não precisa ser migrado;
- nenhuma base técnica compartilhada depende de Contact Center, Marketing Center, CRM ou
  Website;
- decisão documentada entre SDK Google no worker Odoo ou adapter isolado;
- jobs nunca serializam segredo e writer não tem fallback em campo `Char`;
- checkout limpo reproduz instalação/testes com as dependências pinadas.

### Fase 2 - `marketing_center_base`

Status: **núcleo read-only entregue**. DTO/ledger de atribuição, source/connection,
team/roster, catálogo/revisões, sync run/cursor, performance, touchpoint efetivo,
resolução de source e business events estão no laboratório. Conversion event/delivery e
change request/approval pertencem aos cortes posteriores de escrita e seguem pendentes.

Entregas:

- scaffold do addon de domínio;
- `MarketingTouchpointDTO` e API local versionada de ingestão;
- modelos canônicos de source, connection, external entity/revision, sync run/cursor,
  performance/revision/action, business event, conversion event/delivery, team/roster e
  change request/approval;
- modelos `marketing.attribution.*` de touchpoint, identifier, evidence, supersession,
  modelo, resultado e contribuição;
- ACL e menus administrativos;
- queue_job channels;
- helpers de idempotencia, fingerprint e masking;
- testes unitarios do core.

Aceite:

- modulo instala no Odoo 16 teste;
- record rules isolam empresa e grupos;
- replay do mesmo `MarketingTouchpointDTO` não duplica touchpoint e conflito não causa
  merge silencioso;
- business/conversion events e approvals são append-only;
- nenhuma credencial aparece em log, chatter ou UI normal;
- desinstalar CRM/Website/Contact Center não impede instalar o base;
- testes passam.

### Fase 2.1 - Captura first-party no WordPress atual

Status: **contrato e runtime entregues; integração WordPress legada não executada**. O
`marketing_center_web_ingress` está implantado e o Website Odoo já consome o mesmo
contrato. Um proxy WordPress só será criado se o legado continuar necessário durante a
migração.

Entregas:

- scaffold e contrato HTTP versionado do `marketing_center_web_ingress`;
- integração WordPress via first-party/server proxy;
- captura de UTMs/click IDs, submissão e redirect WhatsApp seguro;
- implantação coordenada de Google tag/GTM/GA4/Meta Pixel conforme a policy vigente;
- monitor de cobertura e endpoint sem open redirect.

Aceite:

- landing WordPress → formulário/conversa preserva evidência sem guardar click ID na URL
  canônica;
- bot/prefetch não conta como conversão humana;
- token expirado/inválido usa fallback fixo e não permite destino arbitrário;
- E2E sintético não envia evento a dataset/pixel de produção.

### Fase 3 - Google Ads read-only

Status: **primeiro corte entregue e validado externamente**. Discovery, catálogo,
performance nos cinco grains (`account`, `campaign`, `ad_group`, `ad`, `keyword`),
Change History e Delivery Diagnostics estão no servidor05. Conversões e valor reportados
pela plataforma permanecem no próximo corte do DTO de performance.

Entregas:

- conector Google Ads;
- sync de customers/campanhas/ad groups/ads/assets/keywords/conversion actions;
- performance diária por conta/campanha/ad group/ad/keyword; search terms ficam em
  consulta diagnóstica bounded/on-demand ou warehouse, não no fato padrão de alta
  cardinalidade;
- change history;
- health de token/developer token/customer.

Aceite:

- sync incremental reexecutavel sem duplicar;
- retry/cooldown em quota;
- dashboard basico compara custo, clique e conversoes;
- nenhuma mutacao executada.

### Fase 4 - Meta Ads read-only e Lead Ads

Status: **read-only implementado, validação externa parcial**. Perfil reader, validação
de App/scopes, discovery de ad accounts, projeção provider-neutral, catálogo ordenado de
campaign/adset/ad/creative, Insights diário de conta/campanha e Lead Ads webhook+pull
foram implantados. Forms/datasets permanecem pendentes; o Insights e Lead Ads aguardam
credenciais dedicadas para validação real.

Entregas:

- conector Meta Ads;
- sync de ad accounts/campaigns/adsets/ads/creatives/forms/datasets;
- Insights diarios;
- handler `page/leadgen` registrado no `meta_webhook_base`; o webhook é hint e o
  consumer recupera o lead por `GET /v26.0/{leadgen_id}` somente depois do acknowledge;
- pull seguro/reconciliador por form/ad, convergindo com webhook por `leadgen_id`;
- Lead Ads vira `MarketingTouchpointDTO` e `marketing.attribution.touchpoint` antes de
  criar/vincular CRM.

Aceite:

- replay de lead nao duplica;
- lead com campanha/adset/ad/form preserva IDs externos;
- `adgroup_id` legado não é promovido a `adset_id`; IDs canônicos vêm do pull;
- lead sem mapeamento fica em quarentena/review;
- Insights reconcilia gasto por dia/conta/campanha;
- access tier, App Review, Business Verification, Page tasks e scopes necessários ficam
  comprovados por conta/app.

As Fases 3 e 4 podem ser implementadas em paralelo depois do base. A primeira tela útil
só é considerada aceita quando mostra Google e Meta no grain conta/campanha/dia, com
linha explícita `não atribuível` em vez de descartar lacunas.

### Fase 5 - Ponte Contact Center

Status: **entregue no laboratório**, incluindo backfill/replay, resolução de source,
lifecycle de conversa e correlação posterior com CRM por addon glue.

Entregas:

- addon `marketing_center_contact_center`;
- mapper versionado do `AttributionDTO` do Contact Center para `MarketingTouchpointDTO`;
- ingestão contínua e backfill idempotente da evidência operacional já persistida;
- links tipados com conversa, mensagem, identity, conta/caixa, guest/partner e CRM
  quando existir;
- projeção opcional no painel de Marketing; a UI de atendimento continua autônoma.

Aceite:

- nenhum payload cru de mensagem e copiado;
- CTWA/referral vira touchpoint de marketing;
- uma pessoa em varias caixas gera touchpoints separados, reconciliaveis por lead;
- conflitos de identificador ficam explicitos;
- backfill/replay repetido não duplica e reconcilia contagem/digest por origem;
- nenhum registro `contact.center.attribution.*` é alterado ou removido;
- instalar, pausar ou desinstalar o bridge não afeta recebimento, envio nem o ledger
  operacional do Contact Center.

### Fase 6 - Pontes CRM, Sale e Accounting

Status: **primeiro corte entregue no laboratório**. Eventos e links tipados de CRM, Sale
e Accounting, incluindo pagamentos parciais e reversões, estão instalados; o crédito
causal permanece deliberadamente fora desta fase.

Entregas:

- addons `marketing_center_crm`, `marketing_center_sale` e `marketing_center_account`,
  instaláveis separadamente;
- business events por `crm.lead`, `sale.order`, `account.move` e
  `account.partial.reconcile`;
- backfill de stage tracking;
- vinculo com sale order, invoice e pagamentos;
- regras iniciais de dedupe e atribuicao.

Aceite:

- lead novo recebe touchpoint sem perder UTM nativo;
- mudanca de estagio cria evento imutavel;
- reexecução/backfill não duplica transição;
- cada parcela/alocação gera evento próprio e reversão referencia o original;
- venda/fatura/alocação entram em receita atribuível sem somar conceitos distintos;
- dashboard mostra funil e cobertura.

### Fase 7 - Migração para Website Odoo

Status: **primeiro bridge entregue no laboratório, ainda não native-first completo**.
Landing, formulário, handoff WhatsApp e correlação durável com o `crm.lead` usam o
ledger técnico. O controller atual ainda não inclui `website_crm` no MRO cooperativo;
integrações com `website.visitor`, `link.tracker`, `utm.*`, identificadores adicionais e
ferramental de migração permanecem pendentes e bloqueadas até os testes da adaptação.

Entregas:

- adapter nativo para `website`, `utm.*`, `link.tracker` e `website.visitor`;
- `marketing_center_website` reutilizando `marketing_center_web_ingress` e o ledger
  canônico criado na Fase 2;
- formulários Odoo consumindo o mesmo contrato HTTP da Fase 2.1 quando aplicável;
- migração de tags/redirects sem quebrar os identificadores de jornada.

Aceite:

- formulário Odoo bem-sucedido cria correlação exata e idempotente entre o lead nativo e
  o touchpoint, sem copiar valores do formulário;
- clique WhatsApp cria touchpoint mesmo se a conversa chegar depois;
- `gclid/gbraid/wbraid/fbclid/fbc/fbp` preservados quando presentes;
- teste end-to-end com landing -> lead/conversa.

### Fase 8 - Conversoes para Google e Meta

Entregas:

- `marketing_center_google_data_manager`;
- `marketing_center_meta_capi`;
- policies de evento, valor e destino;
- outbox com retry e erro parcial;
- telas de monitoramento.

Aceite:

- evento real controlado no dataset/pixel Meta `LAB`, usando Test Events apenas para
  inspeção;
- lote Google em `validateOnly` e depois destino controlado validado;
- dedupe por `event_id`;
- nenhuma conversao enviada duas vezes por retry;
- erros parciais visiveis e reprocessaveis;
- policy sem base/finalidade aplicável gera `blocked_policy` e não envia;
- CAPI com `event_time` acima de sete dias é terminal antes da chamada.

### Fase 9 - Dashboards operacionais

Status: **primeira visão gerencial entregue**. Cobertura, freshness, custo/clique,
touchpoints efetivos e fatos Odoo são exibidos sem crédito causal; filtros avançados,
alertas e exportação gerencial continuam evolutivos.

Entregas:

- visao executiva por periodo;
- campanhas com gasto, leads, propostas, vendas e receita; margem/ROAS de margem somente
  quando a fonte de custo/margem da Fase 0 estiver instalada e reconciliada;
- funil por origem/campanha;
- discrepancia Ads x Odoo;
- alertas de tracking quebrado.

Aceite:

- filtros por empresa, conta, plataforma e periodo;
- metricas batem com snapshots diarios;
- drill-down ate touchpoints e lead;
- export CSV sem identificadores restritos para usuario sem permissao.

### Fase 10 - Mudancas assistidas em campanhas

Entregas:

- change request com before/after;
- aprovacoes e tetos;
- execucao via queue_job;
- reconciliacao pos-mutacao;
- rollback assistido quando possivel.

Aceite:

- todo objeto novo nasce `PAUSED`;
- budget exige teto e aprovacao;
- timeout ambiguo vira `uncertain`;
- auditoria mostra quem aprovou, quando e o request externo;
- nenhuma mudanca ocorre fora de policy.

### Fase 11 - Automacao controlada

Entregas:

- regras versionadas de otimizacao;
- kill switch por plataforma/conta;
- limites de aumento/reducao de budget;
- janelas de avaliacao;
- experimentos e holdouts quando suportado.

Aceite:

- automacao opt-in por conta/campanha;
- teto mensal e diario aplicado;
- toda decisao gera change request auditavel;
- operador pode pausar automacao imediatamente.

### Fase 12 - Expansões opcionais

Possíveis addons, somente após o funil pago estar estável:

- `marketing_center_search_console`: consultas, páginas, país/dispositivo e sitemaps;
- `marketing_center_gtm`: inventário e publicação assistida de containers/tags;
- `marketing_center_merchant`: catálogo, produtos e diagnósticos do Merchant Center;
- `marketing_center_business_profile`: unidades, reviews, respostas e posts;
- `marketing_center_youtube`: canais, vídeos e Analytics;
- `marketing_center_social`: planejamento/publicação orgânica sem misturar Ads;
- exportação para BigQuery/warehouse quando volume e análise ultrapassarem o banco
  operacional do Odoo.

Aceite por expansão:

- addon opcional não aumenta dependências do base;
- DTO, quota, ACL e fonte canônica são definidos antes da implementação;
- mutação externa continua desligada por padrão.

## Politicas de Mutacao Inicial

Primeira versao:

- pode pausar campanha/adset/ad;
- pode criar rascunho `PAUSED`;
- pode adicionar negativas aprovadas;
- pode reduzir budget ate limite aprovado;
- nao pode ativar campanha nova automaticamente;
- nao pode trocar objetivo principal sem aprovacao explicita;
- nao pode excluir objeto externo;
- nao pode aumentar budget acima do teto;
- nao pode alterar conversao primaria para bidding sem checklist de tracking.

## Testes

Unitarios:

- DTO validation;
- fingerprints;
- idempotencia;
- ACL/record rules;
- append-only ledger;
- classificacao de erro;
- masking.
- validação de timezone, moeda, micros e conversões decimais;
- medidas aditivas e não aditivas;
- versionamento do `MarketingTouchpointDTO` e compatibilidade explícita dos mappers;
- policy/capability e expiração de aprovação.

Integracao com fixtures:

- Google GAQL pages e streaming;
- Google quota/resource exhausted;
- Google mutate timeout ambiguo;
- Meta Insights sync;
- Meta Lead Ads webhook replay;
- Meta CAPI partial error;
- fixtures reais do `AttributionDTO` do Contact Center e contract tests do mapper para
  `MarketingTouchpointDTO`;
- backfill/replay idempotente do Contact Center, com reconciliação por origem e digest;
- CRM/business event.
- metric restatement tardio e troca atômica da projeção corrente;
- lote parcialmente aceito sem reenvio dos itens que tiveram sucesso;
- cursor que não avança quando a transação falha;
- credencial rotacionada enquanto há jobs antigos;
- concorrência de webhook, sync, conversão e comando;
- multiempresa, multi-conta e record rules em todos os modelos indiretos.

End-to-end no Odoo teste:

- landing fake -> lead -> proposta -> venda -> conversion outbox;
- CTWA -> Contact Center -> Marketing touchpoint -> CRM;
- Google sync 30 dias;
- Meta sync 30 dias;
- change request `PAUSED`;
- export sem PII para usuario Viewer.

Backfill e regressão:

- snapshot antes/depois do backfill comprovando que o ledger operacional do Contact
  Center permaneceu intacto;
- comparação de contagem, digest, identifier e vínculo criado pelo bridge;
- replay do backfill com resultado idêntico e conflitos auditáveis;
- instalação isolada de cada core e instalação do perfil integrado;
- instalação limpa e upgrade em base representativa;
- Contact Center/WuzAPI/Meta Messaging continuam passando seus testes existentes.

Falhas injetadas:

- `429`, `5xx`, timeout antes/depois do boundary e resposta malformada;
- página repetida, cursor expirado, item removido remotamente e dados retroativos;
- worker morto entre chamada externa e persistência;
- segredo revogado, escopo insuficiente, app/account mismatch e quota compartilhada;
- comando aprovado que fica `stale` antes da execução.

UI:

- QUnit para store/serializers e estados vazios/erro/freshness;
- fluxo de browser por role, empresa e roster;
- acessibilidade básica e exportação sem campos restritos.

## Observabilidade

Registrar:

- sync duration;
- rows imported;
- API operations/tokens/quota consumed;
- cooldowns ativos;
- partial errors;
- conversion acceptance;
- jobs por estado;
- comandos uncertain/dead;
- cobertura de atribuicao;
- discrepancia custo x CRM.
- data freshness/lag por fonte, grain e janela;
- idade do job mais antigo e profundidade por canal;
- webhooks `unrouted`, duplicados e falhos por consumer;
- alterações remotas não originadas no Odoo;
- match/acceptance rate de conversões sem expor dados de matching.

Views iniciais:

- Connections Health;
- Sync Batches;
- Conversion Outbox;
- Change Requests;
- Touchpoint Conflicts;
- Tracking Coverage.

Cada dashboard exibe `updated_at`, timezone, moeda, origem da métrica e janela. Dado
stale é sinalizado; não se apresenta como zero.

## Volume, Retenção e Warehouse

- Odoo guarda configuração, estado operacional, entidades atuais, revisões relevantes,
  fatos diários, ledgers comerciais e auditoria.
- Payload cru de API não é modelo canônico. Quando indispensável para diagnóstico, fica
  sanitizado, restrito e com retenção configurável.
- Touchpoint imutável não contém PII nem click ID recuperável. Valores recuperáveis
  ficam no identifier vault restrito, cifrado, com `retain_until`, purpose e HMAC de
  comparação.
- Expiração apaga/crypto-shred o valor recuperável; supersession registra a anonimização
  e a projeção deixa de expor o vínculo. Manter apenas o registro de supersession não
  seria anonimização.
- Retenção é configurável por classe/purpose, admite legal hold e possui job auditável.
  Agregados financeiros podem permanecer sem vínculo identificável.
- Dimensões de alta cardinalidade e eventos de navegação completos não entram no
  PostgreSQL operacional sem teste de capacidade.
- Particionamento/arquivamento será avaliado por volume real, não implementado
  prematuramente.
- BigQuery ou outro warehouse passa a receber fatos analíticos quando uma consulta
  deixar de caber em agregados diários controlados ou afetar o Odoo transacional.
- Exportar para warehouse não muda a fonte canônica dos fatos CRM/financeiros nem a
  trilha das mutações.

## Portabilidade e Qualidade do Código

- Regras de domínio ficam em services pequenos; models persistem invariantes e adapters
  traduzem providers. Controllers apenas autenticam/validam/encaminham.
- DTOs e APIs da UI usam nomes neutros, sem acoplar `mail.channel`/`discuss.channel` ou
  detalhes de Odoo 16.
- Manifestos mantêm dependências opcionais nos bridges; nenhum addon base instala CRM,
  Website, Sale ou Accounting por efeito colateral.
- Clientes externos, serializers, sync, conversion e mutation ficam em arquivos
  separados e cobertos por contract tests; evitar novos arquivos-deus.
- Versões de Graph/Google e schema DTO são explícitas. Upgrade de API externa requer
  fixture da versão nova e janela de convivência quando o provider permitir.
- O porte futuro para versões novas do Odoo troca bridges e views, preservando modelos
  de domínio e contratos de adapter sempre que possível.

## Dados Sensiveis e Elegibilidade de Uso

Mesmo que LGPD nao seja o foco agora, o design deve evitar vazamento acidental:

- emails/telefones para Google/Meta devem ser normalizados e hashados antes de envio;
- token, secret, appsecret_proof e developer token nunca em logs;
- URLs podem conter identificadores e devem ser truncadas/normalizadas na UI;
- payload bruto tem retencao curta ou fica fora do core;
- identificadores crus ficam em modelo restrito.
- uma policy versionada decide se a finalidade e o destino podem usar cada classe de
  dado; hash não transforma automaticamente o dado em anônimo;
- ausência de policy/basis aplicável bloqueia a delivery por padrão, sem concluir que
  consentimento positivo seja a única base possível em todos os cenários.

## Criterios Antes de Produzir Escrita

- inventario read-only validado;
- snapshots de performance batendo com as plataformas;
- tracking do site e CRM funcionando;
- conversões no dataset/pixel `LAB` e validação Google aceitas; `test_event_code` não é
  considerado sandbox;
- policies e ACL revisadas;
- kill switch testado;
- rollback operacional documentado;
- primeiro lote de mudancas limitado a objetos `PAUSED`.

## Matriz de autoridade native-first

| Responsabilidade                                        | Autoridade                                                      |
| ------------------------------------------------------- | --------------------------------------------------------------- |
| classificação operacional de campanha/origem/meio       | `utm.*` e documentos nativos                                    |
| clique de link controlado pelo Odoo                     | `link.tracker.click`                                            |
| landing, click IDs, externo/headless e handoff WhatsApp | `marketing_center_web_ingress`                                  |
| evidência de jornada e atribuição calculada             | `marketing_center_base`                                         |
| mapeamento provider → UTM e política versionada         | `marketing_center_base`                                         |
| proposta de atribuição                                  | adapters e bridges de origem                                    |
| aplicação atômica/receipt da tupla UTM no lead          | `marketing_center_crm`                                          |
| convergência touchpoint↔caso↔lead                       | `marketing_center_contact_center_crm`                           |
| MRO/correlação Website→CRM                              | `marketing_center_website_crm`                                  |
| fatos comerciais                                        | CRM, Sale e Account nativos                                     |
| causalidade pedido→fatura→pagamento                     | bridges Sale/Account tipados                                    |
| conversa, caso, SLA e evidência operacional             | Contact Center                                                  |
| dashboard native-first                                  | `marketing_center_dashboard`, com dependências e ACL explícitas |

## Decisões Fechadas e Gates em Aberto

Fechadas neste plano:

- Contact Center e Marketing Center são domínios e instalações independentes;
- o Contact Center mantém seu `AttributionDTO` e sua evidência operacional;
- `marketing_center_base` possui `MarketingTouchpointDTO` e o ledger canônico da jornada
  de marketing;
- `marketing_center_contact_center` é a ponte opcional e versionada entre esses dois
  contratos; nenhum core depende do outro;
- não será criado um addon horizontal separado para atribuição;
- duas persistências não são ledgers canônicos concorrentes: uma preserva evidência
  operacional de atendimento e a outra modela a jornada de marketing;
- Meta e Google possuem bases técnicas compartilháveis, sem compartilhar domínio;
- Contact Center e Marketing Center usam o mesmo Meta App, com identidade/transporte em
  `meta_api_base`, callback único em `meta_webhook_base` e tokens Page/Ads/Lead
  separados por purpose;
- `messages` é rota exclusiva do consumer de Contact Center e `page/leadgen`, do
  consumer de Marketing; webhook Lead Ads é hint seguido de GET autenticado;
- o cutover de laboratório para o ingresso único é direto, sem dual-write e sem migração
  obrigatória do histórico técnico anterior;
- `queue_job` executa todo I/O assíncrono;
- read-only precede conversões e mutações;
- Odoo nativo (`utm.*`, `link.tracker`, CRM, Website) é integrado por projeção/bridge;
- `utm.*`, CRM, Sale e Account são as fontes operacionais; touchpoints e Business Events
  são evidência técnica, causalidade, ocorrência/reversão e insumo de
  atribuição/conversão, não telas substitutas dos documentos nativos;
- providers e bridges produzem propostas de atribuição; `marketing_center_crm` é o único
  escritor automático da tupla UTM do lead;
- a política de projeção é `first_trusted_assignment`: tupla atômica, fill-only,
  revisão/fencing, receipt e override humano soberano;
- `link_tracker` é opcional e canônico para cliques nativos elegíveis; Web Ingress
  permanece para click IDs, externo/headless, referrer e handoff WhatsApp;
- métricas de plataforma, fatos Odoo e atribuição calculada são categorias distintas;
- campanha criada por API nasce `PAUSED`; exclusão externa não faz parte do MVP;
- `marketing_center_web_ingress` pertence ao Marketing Center e não expõe controllers ao
  instalar somente o base;
- `meta_api_base` e `google_api_base` pertencem ao release técnico `integration-core`;
  Contact Center e Marketing Center não dependem do repositório um do outro;
- segredos de produção ficam fora do PostgreSQL e são resolvidos por referência;
- entrega de conversão é fail-closed por policy de finalidade/destino;
- testes Meta usam dataset/pixel de laboratório separado.

Gates que precisam de evidência na respectiva fase:

- SDK Google Ads dentro do processo Odoo ou worker isolado;
- scopes, Page tasks, Leads Access Manager e access tier efetivos de cada perfil Meta
  Page/Ads/Lead no App compartilhado;
- combinações de breakdowns e janelas de reprocessamento;
- thresholds de aprovação e tetos de budget;
- política de conversão, valor, dedupe e ajustes por destino;
- prazo de retenção de delivery/payload técnico;
- volume que dispara particionamento ou warehouse;
- modelo de atribuição default para cada relatório.

Gates específicos da adaptação native-first:

- baseline recuperável dos três repositórios (`contact-center`, `marketing-center` e
  `integration-core`) antes de release/cutover; isso não exige backup a cada iteração do
  laboratório;
- MRO cooperativo de `website_crm` e prova `HttpCase` de visitor → formulário → lead;
- mapping UTM versionado, taxonomia global aceita e assignment/receipt atômico em shadow
  antes de qualquer escrita;
- semântica de clique baseada em “não criar segundo fato para clique nativo elegível”,
  não em promessa de um registro por clique físico;
- causalidade pedido–linha–fatura–reconciliação validada; UTM isolada de fatura
  agrupada/pagamento não prova atribuição financeira;
- dependências e ACL explícitas do dashboard sobre CRM/Sale/Account;
- ciclo de vida de identificadores protegidos definido antes de cutover/outbox. Por
  decisão de produto, esse item não bloqueia o desenvolvimento read-only/shadow nem o
  Contact Center.

## Ordem de execução native-first liberada — 2026-09-02

1. Consolidar ADR, inventário, baseline reproduzível e mover telas cruas para menu
   técnico sem alterar registros.
2. Corrigir o MRO do Website CRM e o contrato do clique nativo.
3. Implementar mapping e assignment/receipt UTM somente em shadow.
4. Validar causalidade financeira e reconstruir o dashboard com dependências/ACL
   explícitas.
5. Executar dry-run/backfill e reconciliar resultados nativos com os ledgers.
6. Realizar go/no-go específico. Só depois liberar escrita UTM e desligamento seletivo
   de produtores duplicados.
7. Dashboard native-first definitivo e outboxes Meta CAPI/Google Data Manager ficam
   depois do cutover.

Bindings reais entre equipes CRM e Contact Center exigem mapeamento operacional validado
por uma pessoa; o código da ponte já existe, mas não deve inferir equivalência entre
times pelo nome ou pelo supervisor.

## Primeiro corte native-first aplicado — 2026-09-02

O primeiro corte executável da adaptação foi aplicado e validado no SERVIDOR05, sem
liberar escrita UTM ou cutover:

- `marketing_center_website 16.0.2.1.1` preserva o contrato de captura first-party e
  exclui de seu fato próprio o clique elegível que já pertence ao fluxo nativo de
  `link.tracker.click`. `link_tracker` permanece opcional e não se torna dependência do
  core ou dos addons Website;
- `marketing_center_website_crm 16.0.1.3.0` entra no MRO cooperativo do controller
  nativo `website_crm`, preservando seus hooks de telefone, geolocalização, visitante e
  criação do lead. O bridge correlaciona o formulário ao `crm.lead` nativo sem
  substituir o CRM como fonte operacional;
- o contrato deixa explícito que a exclusão de clique evita um segundo fato canônico
  para o fluxo nativo elegível; ela não promete um registro por clique físico;
- a suíte Website passou **93/93** testes. O release completo passou **117/117** base,
  **533/533** integrados e **4/4** da suíte;
- a árvore final aplicada tem SHA-256
  `c0fdc3ed8fde3f7bf13a5935581b2d82fc2029628c6d70b89e9573d8af68f36c` e a evidência
  canônica está em `scans/raw/20260902-native-first-marketing-qunit-cleanup`;
- a primeira execução foi abortada antes de alterar a base principal por uma assertion
  frágil sobre metadata. As fontes anteriores e a rota foram restauradas, os serviços
  voltaram com HTTP 200 e o ocorrido ficou preservado em
  `scans/raw/20260902-native-first-marketing-release`. Após corrigir a assertion, a
  segunda execução concluiu upgrade, replay e validação;
- nenhum produtor de tracking foi desligado, nenhum cutover foi realizado e nenhuma
  escrita UTM automática foi habilitada. Shadow, reconciliação e go/no-go continuam
  obrigatórios antes dessas mudanças;
- nenhum binding CRM é criado automaticamente. O inventário `CRM-MAP` pertence ao
  Contact Center e continua exigindo validação humana e ação administrativa explícita.
- o smoke QUnit inicial comprovou os **11/11** testes funcionais, mas encontrou o banner
  assíncrono da base neutralizada como poluição externa do DOM. O patch `16.0.2.1.1`
  limita a limpeza ao wrapper de `#oe_neutralize_banner`, sem remover `document.body`.
  Depois de novo release completo, QUnit passou **11/11**, **45/45** em minificado e
  `debug=assets`, e o painel gerencial abriu sem erro ou warning no console. Evidências
  visuais estão em `output/playwright/20260902-cc-native-first-release`.

Produção não foi acessada nem alterada.

## Definição de Pronto por Fase

Uma fase só é concluída quando:

- código, migration, ACL, dados demo/fixtures e documentação estão versionados;
- instalação limpa e upgrade passam no Odoo 16 teste;
- testes unitários, integração e cenário live previsto para a fase têm evidência;
- retries, replay, concorrência, multiempresa e falha parcial foram exercitados;
- health, freshness, dead/uncertain e reprocessamento estão operáveis pela UI;
- nenhum segredo ou PII indevida aparece em log, chatter, bus ou export;
- `plan.md` registra o entregue, desvios e próxima decisão;
- funcionalidades de fases posteriores continuam desabilitadas por capability/policy.

## Fechamento do Contra-check e Cross-check de 2026-09-01

O contra-check da auditoria independente foi concluído sobre a árvore consolidada,
incluindo as alterações paralelas. Foram corrigidos neste ciclo:

- identidade v2 do bridge Contact Center → Marketing Center;
- fronteiras diárias em fusos com transição à meia-noite;
- invalidação do cursor após lock e testes reais de concorrência;
- recuperação autoritativa de `cursor_changed` em Meta e Google;
- writes no-op de perfis sem revisão/health falsa;
- proteção system-only das referências de credencial Meta;
- filtros e navegação operacional dos sync runs;
- estado terminal após esgotar retries reais do OCA `queue_job` em Meta e Google.

O cross-check posterior também resultou em:

- pin fail-closed do schema v1 do Contact Center no bridge;
- contrato Graph v26 único para catálogo, Insights e Lead Ads;
- continuidade na cadeia v1 para replay histórico inequívoco e bloqueio explícito de
  cadeias colididas ou já divididas entre v1/v2;
- cobertura de meia-noite ambígua e da ação system-only que abre o `queue.job` exato.

O servidor05 recebeu a árvore exata de 335 arquivos, hash
`04dabe9d95f63b74586a10486b8a7ea0006e9fff5b3941a91edac924455365fe`, com 726 testes
verdes, upgrade e replay idempotente, HTTP público/privado 200 e nenhuma alteração em
produção. A disposição completa, inclusive refutações e backlog, está em
`reviews/2026-09-01-independent-audit-cross-check-disposition.md`.

Permanecem como decisões deliberadas, não correções automáticas: remotes/commits dos
repositórios, política LGPD/retenção, migração das cadeias v1/v2 já divididas,
planner/backfill multipágina resumível e política avançada de rate limit/health.

## Fechamento da Verificação da Disposição — 2026-09-01

A verificação posterior foi confrontada novamente com código e laboratório. As duas
lacunas de teste acima foram fechadas: há prova dirigida dos sete campos invalidados
após o lock e dois workers reais disputam a mesma página tanto no catálogo quanto em
performance.

O contra-check adicional também corrigiu três riscos de domínio/operação:

- conflito presente já na primeira revisão fica no ledger como `conflict` e não entra na
  projeção efetiva;
- o bridge opcional não executa mais mapper/validação durante o `write()` do Contact
  Center; a falha fica contida no job assíncrono;
- o backfill de atribuição distribui um job idempotente por fonte, evitando rollback do
  lote inteiro quando uma única cadeia histórica exige decisão explícita.

O caso histórico já dividido v1/v2 continua deliberadamente bloqueado para migração
auditada; não foi escolhida automaticamente uma das duas cadeias imutáveis.

O novo release do servidor05 instalou `marketing_center_base` 16.0.1.7.3 e
`marketing_center_contact_center` 16.0.3.0.2. Foram validados 117 testes base, 528
integrados, 89 Website e 4 da suíte (738 no total), sem falhas. A árvore implantada tem
335 arquivos e hash `1f69e011e13105f18824550d4f2ee5f814cba344bfb30c1310bc6cc4057d9e0d`;
upgrade, replay e HTTP 200 passaram, sem tocar produção. A disposição completa está em
`reviews/2026-09-01-cross-check-disposition-verification-response.md`.

Continuam backlog, sem serem mascarados como concluídos: migração histórica v1/v2,
planner multipágina resumível, runner neutro antes do terceiro provedor, headers de uso
Meta, tombstones autoritativos, persistência operacional de erros Graph, health
periódico, justiça/capacidade de crons e topologia Git/CI/off-host.

## Fechamento da Verificação da Resposta — 2026-09-02

O novo contra-check confirmou a corrida entre jobs de atribuição, restaurou a separação
de prioridade entre tráfego vivo (40) e backfill (55), endureceu a validação da empresa
e fechou três lacunas dirigidas de teste. A alegação de que os sete campos invalidados
não possuíam teste isolado foi refutada: o teste específico já pré-carrega e verifica
cada campo após o lock.

Uma inspeção ao vivo encontrou 363 jobs históricos de response episodes falhando por
associar delivery a mensagens enviadas pelo dispositivo. O SQL de seleção foi alinhado
ao invariante do ledger: delivery é evidência apenas para `agent`, enquanto
`external_device` usa a própria mensagem. Os 363 jobs foram reprocessados pelo
JobRunner, gerando 6.224 sinais; o estado final ficou com 600 cursores, zero job
ativo/falho e zero mensagem externa confirmada sem signal.

O servidor05 recebeu `marketing_center_contact_center` 16.0.3.0.3. Passaram 117 testes
base, 533 integrados, 89 Website e 4 da suíte, sem falhas. A árvore de 335 arquivos tem
SHA-256 `432a4e85dba0aef1c4c60f53e5045c82adcf01e437b8a49b917d1570a64496e7`; upgrade,
replay, convergência da fila e HTTP 200 foram comprovados sem tocar produção.

D3 (coordenador paginado/resumível para installs grandes), D4 (planner/migration
explícito em upgrades que exijam remapeamento) e o workflow de resolução de conflito
permanecem backlog deliberado. A disposição completa está em
`reviews/2026-09-02-verification-response-check-disposition.md`.

## Fundação Greenfield do Marketing Center — 2026-09-03

O pente-fino greenfield consolidou e implantou os 15 addons do Marketing Center nas
versões pinadas pelo release. A árvore aplicada contém **373 arquivos** e SHA-256
`be49fb73123af364e6e84d5bc8d7096d7d5e6cf1d2db70b086a953f49f9e570c`.

O release canônico no SERVIDOR05 concluiu todos os gates:

- **120/120** testes base, **633/633** integrados, **119/119** Website/HTTP e **5/5** da
  facade completa, sem falha ou erro;
- upgrade offline e replay idempotente da base principal com status **0**;
- os 15 addons instalados exatamente nas versões pinadas;
- convergência com zero job ativo, falho, inesperado ou projeção falha;
- QUnit `marketing_center_website` em minificado e `debug=assets`: **11/11** testes e
  **45/45** assertions em cada execução, sem falha de asset, página ou runtime;
- HTTP privado e público **200**, com a rota de teste restaurada ao mesmo hash;
- `status=applied_and_validated` e `production_touched=false`.

As cinco tentativas intermediárias que pararam nas suítes base, integrada ou Website
foram todas classificadas como `failed_recovered` e `pre_database_change`: as fontes
anteriores foram restauradas, os serviços voltaram com HTTP 200 e nenhuma delas alterou
a base principal. Elas serviram para corrigir testes e contratos antes do apply final,
sem mascarar falhas.

Evidência canônica:
`scans/raw/20260903-odoo16-marketing-center-greenfield-foundation/release/20260903T233620975902Z`.
O registro detalhado está em
`reviews/2026-09-03-greenfield-foundation-release-validation.md`.

## Fontes e Referencias

Documentos locais:

- [Auditoria Google Ads](../../../infra/runbooks/google-ads-live-audit-2026-08-04.md)
- [Operação direta de mídia paga](../../../infra/runbooks/paid-media-direct-odoo-ads-ops.md)
- [Atribuição n8n/Odoo/WhatsApp](../../../infra/runbooks/paid-media-attribution-n8n-odoo-whatsapp.md)
- [Panorama martech/omnichannel](../../../infra/runbooks/odoo-open-source-martech-omnichannel-landscape-2026-08-04.md)
- [Plano do Contact Center](../contact-center/plan.md)
- [Meta Click-to-WhatsApp](../contact-center/research/meta-click-to-whatsapp-attribution.md)
- [Meta Messenger e Instagram](../contact-center/research/meta-messenger-instagram.md)
- [ADR — App e ingresso de webhooks Meta compartilhados](reviews/2026-08-31-shared-meta-app-webhook-adr.md)

Fontes oficiais consultadas:

- Google Ads API quotas:
  https://developers.google.com/google-ads/api/docs/best-practices/quotas
- Google Ads API access levels:
  https://developers.google.com/google-ads/api/docs/api-policy/access-levels
- Google Ads create campaigns:
  https://developers.google.com/google-ads/api/docs/campaigns/create-campaigns
- Google Data Manager API: https://developers.google.com/data-manager/api
- Google Data Manager send events:
  https://developers.google.com/data-manager/api/devguides/events/send-events
- Google Data Manager limits:
  https://developers.google.com/data-manager/api/devguides/limits
- Google Data Manager error reasons:
  https://developers.google.com/data-manager/api/reference/rest/v1/ErrorReason
- Google Data Manager Customer Match migration:
  https://developers.google.com/data-manager/api/devguides/audiences/google-ads/customer-match/upgrade
- GA4 Data API overview:
  https://developers.google.com/analytics/devguides/reporting/data/v1
- GA4 Data API quotas:
  https://developers.google.com/analytics/devguides/reporting/data/v1/quotas
- GA4 funnel reports preview:
  https://developers.google.com/analytics/devguides/reporting/data/v1/funnels
- Meta Marketing API reference:
  https://developers.facebook.com/docs/marketing-api/reference
- Meta Marketing API authorization/access tiers:
  https://developers.facebook.com/documentation/ads-commerce/marketing-api/get-started/authorization.md
- Meta Ads Insights API:
  https://developers.facebook.com/documentation/ads-commerce/marketing-api/insights
- Meta Conversions API:
  https://developers.facebook.com/documentation/ads-commerce/conversions-api
- Meta Conversions API usage/Test Events:
  https://developers.facebook.com/documentation/ads-commerce/conversions-api/using-the-api.md
- Meta CAPI parameters:
  https://developers.facebook.com/docs/marketing-api/conversions-api/parameters
- Meta Lead Ads retrieval:
  https://developers.facebook.com/documentation/ads-commerce/marketing-api/guides/lead-ads/retrieving.md
- Meta Webhooks for Leads:
  https://developers.facebook.com/docs/graph-api/webhooks/getting-started/webhooks-for-leadgen
- Meta permissions / `leads_retrieval`:
  https://developers.facebook.com/docs/permissions#leads_retrieval

Bibliotecas/referencias de codigo:

- Google Ads Python client: https://github.com/googleads/google-ads-python
- Google Ads MCP oficial: https://github.com/googleads/google-ads-mcp
- Meta Business SDK Python: https://github.com/facebook/facebook-python-business-sdk

## Fechamento dos addons restantes e contra-check independente — candidato de 2026-09-03

O fechamento passa a cobrir também os seis addons que não receberam o mesmo inventário
módulo a módulo na primeira revisão:

- `marketing_center_contact_center`;
- `marketing_center_crm`;
- `marketing_center_contact_center_crm`;
- `marketing_center_web_ingress`;
- `marketing_center_website`;
- `marketing_center_website_crm`.

O contra-check confirmou três defeitos objetivos do parecer independente e duas lacunas
adicionais encontradas ao seguir os fluxos completos:

1. crons Meta/Google sem cursor justo foram centralizados em scheduler rotativo,
   transacional e separado por lane;
2. jobs Lead Ads são reancorados à empresa dona e detectam ciclos não imediatos de
   cursor, além de impor teto de páginas e tamanho;
3. o handoff WhatsApp do Website só intercepta links marcados/configurados e, em
   qualquer falha de telemetria, continua no `href` original validado e same-origin;
4. o bridge Contact Center + CRM deixou de fazer fan-out M×N síncrono e passou a jobs
   paginados, idempotentes e resumíveis, mantendo revogação síncrona;
5. a materialização dos episódios de resposta passa a ter cutoff e cursores seek
   persistentes, para que uma conversa grande não seja processada numa única transação e
   mensagens live não ultrapassem o histórico.

O alerta da view SQL do dashboard foi medido no SERVIDOR05: com 310 métricas, 229
touchpoints efetivos, 40 resolutions e 3.945 eventos de negócio, o plano completo
executou em **18,414 ms**. Isso refuta uma falha atual, mas não certifica escala. O
plano mostra um lateral lookup de sync run por métrica e deve ser repetido em
cardinalidade representativa, com orçamento de latência e buffers, antes de produção.
Nenhum split será feito apenas porque o SQL tem muitas linhas.

A ausência de retenção completa é aceita como gate real, não como autorização para
apagar ledgers imutáveis. O desenho produtivo deverá separar identificadores/payloads
apagáveis da prova mínima de dedupe, replay e auditoria, com `retain_until`, legal hold,
purge paginado e observabilidade.

Nesse ponto o bloco ainda era **candidato**, condicionado à prova das suítes, upgrade,
replay, QUnit, smoke, versões e hash exato no SERVIDOR05. O resultado final está
registrado na atualização de 2026-09-04 abaixo; produção não foi acessada por essa
validação.

## Atualização do fechamento greenfield — 2026-09-04

O inventário linha a linha dos seis addons restantes foi concluído. As críticas externas
sobre injustiça dos crons, contexto multiempresa e fallback do handoff WhatsApp
descreviam riscos reais de estados anteriores, mas já estão corrigidas na árvore
consolidada: schedulers usam rotação persistente por lane, jobs reancoram a empresa
exata e falhas de telemetria preservam o `href` original validado e same-origin.

O `marketing_center_contact_center` chegou à versão `16.0.3.3.0`, com **36** testes.
Após provar no banco a drenagem completa dos cursores e sinais, foram removidos os
estados/campo `legacy_*` e o runtime passou a ter uma única semântica de materialização.
A suíte integrada consolidada contém **660** testes.

Como este projeto ainda não possui baseline produtivo, a transição foi executada
primeiro no SERVIDOR05 e só então **33 hooks históricos de migration em 12 addons**
foram removidos. O release agora rejeita qualquer diretório `migrations/`: a primeira
instalação produtiva deverá nascer diretamente no schema corrente. Depois desse
baseline, toda evolução de schema voltará a exigir migrations versionadas.

A etapa transitória está comprovada por release `applied_and_validated`, com **123/123**
testes base, **660/660** integrados, **123/123** Website/HTTP e **5/5** da facade;
upgrade e replay offline passaram, as filas convergiram, QUnit passou em minificado e
`debug=assets` com **12/12 testes e 49/49 assertions** por modo, HTTP privado/público
respondeu 200 e produção não foi tocada. Evidência:
`scans/raw/20260903-odoo16-marketing-center-remaining-addons-closeout/release/20260904-stage-b-direct-r2/summary.json`.

O release canônico da árvore **já sem migrations** também concluiu com
`status=applied_and_validated`. A segunda execução confirmou **123/123** testes base,
**660/660** integrados, **123/123** Website/HTTP e **5/5** da facade; upgrade e replay
offline com status zero; zero job ativo/falho/inesperado; QUnit minificado e
`debug=assets` com **12/12 testes e 49/49 assertions** por modo; e HTTP privado e
público 200. A árvore final possui 346 arquivos e SHA-256
`ecc5e80744ba17a2133cc3fe4d4ff7f50ea3e180a1ec5cfda59ee50b00a5543e`.

Evidência canônica do primeiro baseline:
`scans/raw/20260903-odoo16-marketing-center-remaining-addons-closeout/release/20260904-final-no-migrations/summary.json`.

Com isso, o fechamento greenfield do Marketing Center está concluído no SERVIDOR05, sem
tocar produção. Os gates abaixo continuam deliberadamente abertos para a entrada
produtiva; eles não invalidam o baseline técnico comprovado.

Permanecem gates de produção, sem serem confundidos com defeitos já corrigidos:

- política LGPD/retenção com separação entre valor apagável e prova mínima, legal hold,
  purge paginado e observabilidade;
- rate limit autoritativo no edge para ingresso público;
- `EXPLAIN (ANALYZE, BUFFERS)` do dashboard em cardinalidade representativa; no
  laboratório o plano atual levou **18,414 ms**, o que não prova escala;
- os **3** registros históricos `ingress_provenance='unclassified'` são preservados como
  evidência imutável; o runtime atual não cria novos registros nessa condição.

A disposição detalhada está em
`reviews/2026-09-04-remaining-addons-greenfield-review.md`.

O índice conjunto deste baseline, incluindo os limites com Contact Center e Integration
Core, está em `../reviews/2026-09-04-greenfield-baseline-cross-repo.md`.
