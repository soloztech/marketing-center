# Contact Center + Marketing Center — entendimento e próximas etapas

Data: 2026-09-01

## Leitura arquitetural

O Contact Center já é o sistema operacional da conversa: recebe, identifica, cria
guest/canal/mensagem, controla caixa/equipe/responsável/caso e preserva sua própria
evidência de origem. Ele não deve ganhar campanha, gasto, modelo de atribuição ou
credenciais Ads.

O Marketing Center é o sistema analítico e operacional de mídia: recebe projeções de
catálogo/performance dos providers, traduz evidências first-party em touchpoints e
registra fatos internos de CRM, venda, faturamento e recebimento. Ele não deve enviar
mensagem nem tornar o Contact Center dependente de Ads.

As pontes são opcionais e tipadas:

```text
Contact Center evidence ── marketing_center_contact_center ──> touchpoint
CRM / Sale / Account    ── addons próprios ──────────────────> business event
Meta / Google Ads       ── adapters read-only ───────────────> catalog + metric
                                                        
touchpoint + business event + policy/model ─────────────> attribution result
business event + destination policy ────────────────────> conversion delivery
```

Correlação M:N entre conversa, lead, pessoa e touchpoint não é crédito causal. Os
fatos da empresa contam uma vez; nunca são duplicados por source para completar um
dashboard.

## Estado real

- Contact Center: core, WuzAPI, Meta Messaging, UI, grupos, mídia, realtime,
  permissões por caixa, dono/equipe, autoatribuição, pipeline/casos e ponte CRM estão
  homologados. Restam principalmente aceite operacional do piloto e credenciais/
  permissões externas Meta.
- Marketing Center: core provider-neutral, touchpoint efetivo, resolução de source,
  bridge Contact Center, CRM, Sale, Accounting, Meta catálogo/Insights/Lead Ads e
  primeira visão gerencial estão implantados no servidor05.
- Infra compartilhada Meta: `meta_api_base` e `meta_webhook_base` estão em uso pelos
  dois domínios sem unificá-los.
- Infra Google: spike concluído; `google_api_base` REST v25 implementado e em gate
  isolado antes do consumer.

## Sequência correta

1. **Paridade read-only Google.** Instalar a fundação técnica e implementar discovery,
   catálogo e performance no mesmo DTO do core. Sem mutações.
2. **Evidência externa real.** Perfis dedicados Meta Ads/Lead e Google reader; validar
   scopes, developer token, MCC/login customer, contas, timezone e moeda.
3. **Captura first-party.** Implementar `marketing_center_web_ingress` e depois o
   adapter Website Odoo; preservar UTM, click ID, sessão e clique para WhatsApp antes
   de depender do relatório das plataformas.
4. **Cobertura e atribuição.** Criar modelo/resultados versionados somente quando os
   três lados — custo, touchpoint e fato Odoo — tiverem cobertura mensurável.
5. **Conversões outbound.** Derivar conversion events por policy, enviar primeiro a
   destinos LAB via Google Data Manager e Meta CAPI, com outbox e erro parcial.
6. **Mudanças assistidas.** Change request, aprovação, objetos novos `PAUSED`, tetos e
   kill switch. Automação autônoma fica por último.

## Gates que não se resolvem apenas com código

- credencial Google reader, developer token e login customer válidos;
- perfil Meta Ads com `ads_read` e perfil Lead com Page tasks/Leads Access;
- dataset/pixel de laboratório e destino Google controlado;
- inventário de GTM/GA4/Pixel e WordPress/Website;
- capacidade real do JobRunner e baseline de quota;
- decisão posterior de modelo de atribuição, janela e política de conversão.

Esses gates não justificam acoplar domínios nem usar token de mensageria para Ads. O
código deve ficar instalável e testável com fixtures até as credenciais existirem.
