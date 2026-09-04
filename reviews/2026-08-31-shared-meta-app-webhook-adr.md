# ADR — App e ingresso de webhooks Meta compartilhados

- Data: 2026-08-31
- Status: aceito
- Escopo: `integration-core`, `contact_center_meta` e `marketing_center_meta`
- Substitui: premissa de um controller Meta público por consumer

## Decisão

Contact Center e Marketing Center continuam domínios, instalações, DTOs e ledgers
independentes. Eles compartilham somente infraestrutura Meta no repositório
`integration-core`:

- `meta_api_base`: identidade técnica do Meta App, referências externas de segredo,
  transporte Graph versionado, `appsecret_proof`, HMAC e taxonomia de erros;
- `meta_webhook_base`: único ingresso técnico de webhooks desse App, envelope/dedupe
  técnico, roteamento e estado de entrega;
- `contact_center_meta`: consumer de eventos de mensagens e proprietário dos DTOs,
  filas e projeções de atendimento;
- `marketing_center_meta`: consumer de `leadgen`, proprietário da recuperação do lead,
  do DTO e do ledger de marketing.

O mesmo Meta App será usado pelos dois consumers. As credenciais serão distintas por
finalidade, ainda que pertençam ao mesmo App:

| Perfil | Uso | Regra |
|---|---|---|
| Page | instalar/consultar subscriptions e operações autorizadas de Page/messaging | não é reutilizado como reader de Ads ou Lead por conveniência |
| Ads reader | catálogo e Insights | somente capacidades de leitura aprovadas |
| Lead reader | recuperar dados de Lead Ads | `leads_retrieval` e demais permissões/tarefas comprovadas |

Tokens e App Secret ficam fora do PostgreSQL. O Odoo persiste somente referência
opaca allowlisted, purpose/capabilities, fingerprint mascarado e revisão de fencing.

## Topologia

```text
Meta App único
    |
    +-- App identity / Graph transport ----------> meta_api_base
    |
    +-- callback técnico único ------------------> meta_webhook_base
                                                    |
                                                    +-- messages
                                                    |     -> contact_center_meta
                                                    |     -> Contact Center DTO/ledger
                                                    |
                                                    +-- leadgen
                                                    |     -> marketing_center_meta
                                                    |     -> GET /v26.0/{leadgen_id}
                                                    |     -> Marketing DTO/ledger
                                                    |
                                                    +-- desconhecido -> unrouted
```

`meta_webhook_base` não depende de nenhum domínio. Os addons consumidores dependem
dele e registram handlers por chave de rota. A ausência de um consumer deixa a
entrega em `unrouted`; não quebra nem instala implicitamente o outro domínio.

## Contrato do ingresso

### Handshake

O endpoint valida `hub.mode=subscribe` e o `hub.verify_token` da identidade técnica,
e somente então devolve `hub.challenge`. O verify token serve ao handshake; não
autentica deliveries POST.

### Delivery POST

Antes de qualquer parse ou roteamento funcional, o ingresso:

1. limita método, content type e tamanho;
2. resolve a identidade do App por referência pública não secreta;
3. valida `X-Hub-Signature-256` sobre os bytes exatos do corpo com o App Secret;
4. valida o envelope Meta e persiste uma delivery técnica bounded e sanitizada;
5. cria, na mesma transação curta, os itens de rota e jobs idempotentes;
6. responde `2xx` sem chamar Graph API nem executar regra de domínio.

O envelope técnico mínimo registra App/revisão, `object`, identificadores e tempo da
entry, índice/`field` da change, digest do corpo, horários e estado. O corpo bruto não
é logado e não é fonte canônica de mensagens, leads ou atribuição.

Como a Meta não fornece uma chave de delivery universal para esse fluxo, existem
duas camadas de idempotência:

- técnica: identidade do App + digest do envelope/change;
- semântica no consumer: ID externo da mensagem ou `leadgen_id`.

### Roteamento

| Objeto/campo Meta | Consumer | Resultado |
|---|---|---|
| eventos de mensagem suportados | `contact_center_meta` | normalização e ledger do Contact Center |
| `page/leadgen` | `marketing_center_meta` | job de recuperação do Lead Ads |
| não suportado ou consumer ausente | nenhum | `unrouted`, visível e reprocessável |

O router não cria `EventDTO`, `AttributionDTO`, `MarketingTouchpointDTO`, conversa,
lead CRM ou campanha. Isso pertence exclusivamente ao consumer correspondente.

## Contrato Lead Ads

O webhook `leadgen` é um **hint**, não o lead completo. Ele entrega, conforme
disponibilidade, `leadgen_id`, `page_id`, `form_id`, `ad_id`, o legado
`adgroup_id` e `created_time`. O `marketing_center_meta`, depois do acknowledge,
executa:

```text
GET /v26.0/{leadgen_id}
    ?fields=id,created_time,form_id,ad_id,adset_id,campaign_id,
            is_organic,platform,field_data,custom_disclaimer_responses
```

Regras:

- `adgroup_id` permanece legado/opaque; nunca é convertido em `adset_id`;
- IDs de anúncio, adset e campanha são opcionais;
- `field_data` preserva lista e `values[]`, sem flatten destrutivo;
- webhook e pull reconciliador por form/ad convergem pelo mesmo `leadgen_id`;
- falha temporária no GET recebe retry bounded; revogação pausa o perfil Lead;
- ausência numa varredura nunca apaga um lead já observado.

## Ownership

| Artefato | Owner |
|---|---|
| Meta App ID, versão Graph, App Secret ref e transporte | `meta_api_base` |
| callback, verify-token ref, Page subscription/token ref, assinatura, delivery técnica, dedupe e rota | `meta_webhook_base` |
| Ads reader e Lead reader credential refs/capabilities | respectivos consumers do Marketing Center, separados por purpose |
| mensagem, identidade externa, conversa e mídia | Contact Center |
| formulário, lead de marketing, touchpoint e atribuição | Marketing Center |
| vínculo entre conversa e jornada de marketing | `marketing_center_contact_center` |

Nenhum ledger de domínio migra para `integration-core`. O ingresso técnico também não
substitui o bridge opcional entre Contact Center e Marketing Center.

## Sequência de implementação e cutover

1. evoluir `meta_api_base` de transporte stateless para identidade técnica
   revisionada, preservando sua API de transporte;
2. criar `meta_webhook_base`, controller único, delivery/route ledger, ACL, fila e
   registry sem dependência de domínio;
3. registrar o handler de mensagens em `contact_center_meta`;
4. registrar o handler `leadgen` e o pull do lead em `marketing_center_meta`;
5. configurar no mesmo Meta App o callback único e os campos necessários;
6. instalar o App na Page com a credencial Page e testar ambos os tipos de evento;
7. desativar os controllers antigos e observar fila, `unrouted`, retries e health.

O ambiente é de desenvolvimento. O cutover será direto: não haverá dual-write,
espelhamento temporário nem migração obrigatória do histórico técnico dos controllers
antigos. Registros canônicos já persistidos nos domínios permanecem intactos; somente
novas deliveries passam pelo ingresso compartilhado.

## Critérios de aceite

- existe um único callback público Meta por identidade de App;
- handshake inválido e POST sem assinatura válida são rejeitados sem persistir job;
- nenhum Graph GET ou processamento de domínio ocorre antes do acknowledge;
- uma delivery repetida não duplica rota, mensagem, lead ou touchpoint;
- mensagem chega somente ao consumer do Contact Center;
- `page/leadgen` chega somente ao consumer do Marketing Center;
- `leadgen` só vira DTO após GET autenticado do lead e validação do payload;
- evento desconhecido/consumer ausente fica `unrouted` e pode ser reprocessado;
- Page, Ads reader e Lead reader são perfis/referências separados no mesmo App;
- token, App Secret, PII e payload bruto não aparecem em banco funcional, job args,
  URL, log, chatter, bus ou diagnóstico;
- `contact_center_base` e `marketing_center_base` continuam instaláveis isoladamente;
- instalar apenas um consumer não exige nem importa modelos do outro;
- o cutover direto é exercitado em laboratório e os endpoints antigos deixam de
  receber novas deliveries.

## Consequências

Benefícios:

- HMAC, limites, dedupe técnico e callback são implementados uma vez;
- uma única configuração de Webhooks no Meta App atende mensagens e Lead Ads;
- rotação do App Secret e versionamento Graph têm um owner técnico;
- os domínios mantêm ACL, lifecycle e fontes canônicas independentes.

Custos:

- `integration-core` passa a possuir pequena persistência técnica;
- consumers precisam de registry e replay explícitos;
- indisponibilidade do ingresso afeta os dois fluxos, exigindo health, fila e alerta
  por rota.

## Alternativas rejeitadas

- **Controller por consumer:** duplica exposição pública, HMAC, App identity, dedupe e
  configuração de callback para o mesmo App.
- **Um token para tudo:** amplia blast radius e mistura Page, Ads e PII de Lead.
- **Transformar `meta_api_base` em domínio Meta completo:** acopla conversas,
  campanhas e leads no shared core.
- **Dual-write durante o cutover de desenvolvimento:** adiciona reconciliação sem
  benefício proporcional; os ledgers de domínio já preservam o histórico relevante.

## Fontes oficiais

- [Meta — Webhooks for Leads](https://developers.facebook.com/docs/graph-api/webhooks/getting-started/webhooks-for-leadgen)
- [Meta — Retrieving Leads, atualizado em 2026](https://developers.facebook.com/documentation/ads-commerce/marketing-api/guides/lead-ads/retrieving)
- [Meta — permissões e `leads_retrieval`](https://developers.facebook.com/docs/permissions#leads_retrieval)
- [Meta Business SDK v26 — Page forms e subscriptions](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.0/facebook_business/adobjects/page.py#L2572)
- [Meta Business SDK v26 — campos do Lead](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.0/facebook_business/adobjects/lead.py#L29)
- [Meta — exemplo oficial de Lead Ads webhook](https://github.com/fbsamples/lead-ads-webhook-sample)
