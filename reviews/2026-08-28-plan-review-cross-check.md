# Cross-check da revisão do plano — Marketing Center

- Data: 2026-08-28
- Alvo: `../plan.md` e `2026-08-28-plan-review.md`
- Escopo: arquitetura, fronteiras de repositório, modelo de dados, APIs oficiais,
  segredos, fases e critérios de aceite
- Resultado: plano corrigido; nenhum addon, banco ou ambiente alterado

## Veredito

A revisão é tecnicamente forte. Os cinco P1 apontam lacunas reais, com duas nuances:

- P1-4 não autoriza concluir que `consent=true` seja obrigatório universalmente; o
  requisito correto é uma decisão de finalidade/base/policy obrigatória e fail-closed;
- duas correções P2 sugeridas literalmente deixariam falhas: unicidade apenas por
  `content_hash` perderia A→B→A, e supersession isolada não anonimiza o valor antigo.

Com esses refinamentos, os P1 e P2 confirmados foram incorporados ao plano.

## P1

| Achado | Veredito | Evidência do cross-check | Decisão aplicada |
|---|---|---|---|
| P1-1 — fronteira de repositório | **Confirmado** | `contact-center` é git root independente, ignorado pelo `infra-ai-ops`; CI, `setup/`, `oca_dependencies.txt` e deploy atuais só conhecem o próprio repo + OCA queue | `marketing_attribution_base`, `marketing_attribution_web` e `meta_api_base` pertencem ao release `contact-center`; `google_api_base` e `marketing_center_*` ao novo repo; dependência somente `marketing-center -> contact-center` |
| P1-2 — chave de evento/reversão | **Confirmado** | o plano tinha apenas uma constraint sem definir ocorrência, parcela ou reversão | criado `marketing.business.event`, `business_event_key`, `source_occurrence_ref`, `reverses_event_id`, `root_event_id` e tabela gatilho→chave por tipo |
| P1-3 — Meta Test Events | **Confirmado** | Meta informa que eventos com `test_event_code` não são descartados e podem entrar em mensuração/targeting | dataset/pixel `LAB` separado e allowlisted; Test Events somente para inspeção |
| P1-4 — policy/consentimento opcional | **Parcialmente confirmado na formulação** | Google/Meta exigem direitos, disclosures/termos e consentimento quando legalmente aplicável; consentimento não é a única base possível em todo caso | privacy snapshot nasce no contrato; ConversionDTO exige policy/base/consent status triestado; ausência incompatível gera `blocked_policy` |
| P1-5 — segredo de escrita | **Confirmado e ampliado** | `contact_center_meta` guarda app secret/token em `fields.Char`; `groups=`/`password=True` não protegem dump, réplica ou `sudo()` | todo segredo de produção, reader ou writer, fica fora do PostgreSQL; banco guarda referência/revisão/capabilities; writer não tem fallback DB |

### P1-1 — topologia comprovada

Estado local observado:

```text
infra-ai-ops/.git
  └── ignora odoo16/addons/contact-center/

odoo16/addons/contact-center/.git
  ├── oca_dependencies.txt: somente OCA queue
  ├── setup/: quatro addons atuais
  └── deploy: monta somente /mnt/outros/contact-center
```

Não era um ciclo de manifests ainda; seria um ciclo operacional de checkout,
packaging/release e deploy. O plano agora fecha a hospedagem antes da Fase 1 e exige
CI/deploy reproduzíveis com os dois commits.

### P1-2 — eventos financeiros

O review procede especialmente para pagamentos. `payment_state` é computado e não
serve como ocorrência. Uma fatura paga em três parcelas gera três
`account.partial.reconcile` e, portanto, três eventos `payment_allocated` diferentes.
Desfazer a reconciliação gera novo evento que aponta para o original.

O plano também separa `cash_received` de `payment_allocated`; dashboards não somam os
dois como receita adicional. A conversion delivery preserva sua correlação externa
determinística para ajustes/reversões.

### P1-5 — backend de segredos

Decisão aplicada:

```text
credential profile no Odoo
    -> backend/reference/purpose/scopes/revision/health
    -> secret file ou EnvironmentFile read-only no MVP
    -> cofre futuro pelo mesmo resolver
```

O job recebe apenas `credential_profile_id`, revalida empresa/capability/revision e
resolve o valor server-side. Rota de webhook usa backend local/cache seguro para não
depender de cofre remoto antes de verificar assinatura. A rotação incrementa fencing e
invalida jobs antigos.

## P2

| Achado | Veredito | Ajuste aplicado |
|---|---|---|
| `external_ref` composto | **Confirmado** | resource name completo; sem ele, chave pai+filho como `{ad_group_id}~{criterion_id}` |
| WordPress atual versus Website Odoo futuro | **Confirmado** | criado `marketing_attribution_web` e Fase 2.1 para captura WordPress; Website Odoo apenas troca o bridge depois |
| origem/janela fora da chave da métrica | **Confirmado** | `reporting_context_hash` inclui janela/modelo/timezone/conversion set; origem entra na identidade |
| action daily sem chave | **Confirmado** | `(metric_daily_id, action_key_hash)` |
| click IDs e URL | **Confirmado** | IDs de clique no identifier restrito; URL persistida sem fragmento, PII e click params |
| revisão com `observed_at` | **Confirmado; solução refinada** | `revision_sequence` sob lock; hash igual só atualiza observação; A→B→A permanece completo |
| lifecycle/revenue sem gatilho | **Confirmado** | ledger comum e tabela normativa de gatilhos/chaves/reversões |
| dependência da UI do atendimento | **Confirmado** | projeção operacional fica no repo Contact Center; bridge Marketing não é requisito da UI |
| DTO sem moeda/envelope | **Confirmado** | currency/report timezone/context + `SyncPageDTO` paginado/assíncrono |
| fronteira diária/fuso | **Confirmado** | `report_date`, timezone IANA, limites UTC e projeção separada por timezone empresarial |
| canonical key v2 | **Confirmado por código** | remove `connection.id`; usa refs externas estáveis, versão e mapa v1→v2 |
| margem sem fonte | **Confirmado** | margem/ROAS de margem condicionados a `sale_margin` ou fonte auditável instalada |
| roster sem modelo | **Confirmado** | team/member/source roster próprio do Marketing Center |
| aprovação sem enforcement | **Confirmado** | somente `action_approve()`, approval append-only e revalidação/consumo atômico no job |
| retenção/anonimização | **Confirmado; solução ampliada** | identifier vault cifrado, `retain_until`, HMAC, expurgo/crypto-shred, legal hold e supersession auditável |
| redirect público | **Confirmado** | token por digest, destino fixo/allowlist, expiração, bot/prefetch, rate limit, no-store e fallback seguro |

## P3 e fatos externos

| Comentário | Cross-check |
|---|---|
| Explorer = 2.880 operações/24 h | **Confirmado** e incluído como gate da Fase 0 |
| Data Manager é o caminho novo | **Confirmado para o token novo/sem allowlist**; upload Ads API não foi removido universalmente para tokens legados |
| ECL exige tag em todo cenário | **Parcial**; fluxo normal usa tag/GTM, mas sem tag a documentação prevê correlação obrigatória por GCLID |
| CAPI `event_time` até sete dias | **Confirmado**; pre-send terminal no plano |
| Lead Ads webhook e pull | **Confirmado**; adicionadas permissões/tasks e reconciliação |
| retenção Lead Ads de 90 dias | **Não comprovada pela página técnica citada**; não registrada como fato normativo |
| Meta Limited/Full Access, App Review/BV | **Confirmado** e incluído no inventário |
| GA4 funnels em `v1alpha` | **Confirmado**; capability experimental fora do contrato crítico |
| links Meta v25/v26 | **Higiene válida**, não prova incompatibilidade; referências normalizadas e versão pinada por perfil |
| mudança `action_attribution_windows` em 06/2025 | **Não comprovada**; o plano preserva contexto/janela sem repetir a causa/data |
| `fields.Integer`/micros | **Refutação do review mantida**; o plano agora exige `bigint`/`numeric`, não `int4` |
| pivots e não aditivas | **Confirmado**; pivot nativo limitado a medidas aditivas |
| search terms e volume | **Confirmado**; diagnóstico bounded/on-demand ou warehouse |
| entidade removida | **Confirmado**; tombstone somente após observação suficiente |
| namespace/grain/level | **Confirmado**; regra de ownership e vocabulário adicionados |
| runner compartilhado | **Confirmado**; lab `root:1`, ensaio de capacidade na Fase 0 |
| tenant/test assets/runtime | **Confirmado**; Industrial/Odoo16 inicial, ativos `LAB`, Energia/Odoo18 fora do primeiro deploy e risco Python 3.10 inventariado |

## Pontos em que a sugestão original não foi copiada literalmente

1. `UNIQUE(metric_daily_id, content_hash)` não preserva A→B→A. Foi adotada sequência
   de revisão e supressão apenas de releitura consecutiva idêntica.
2. Supersession não anonimiza valor antigo. Foi adotado expurgo/crypto-shred do
   identifier recuperável, preservando somente auditoria não reversível.
3. `consent=true` universal seria uma simplificação jurídica incorreta. A policy/base
   é obrigatória; o status de consentimento é triestado e avaliado por finalidade.
4. A retenção Lead Ads de 90 dias não foi afirmada sem fonte normativa adequada.
5. A data/causa específica de `action_attribution_windows` não foi repetida sem
   changelog oficial; o contexto reportado continua obrigatoriamente versionado.

## Fontes oficiais do cross-check externo

- Meta CAPI/Test Events: https://developers.facebook.com/documentation/ads-commerce/conversions-api/using-the-api.md
- Meta Marketing authorization: https://developers.facebook.com/documentation/ads-commerce/marketing-api/get-started/authorization.md
- Meta Lead Ads retrieval: https://developers.facebook.com/documentation/ads-commerce/marketing-api/guides/lead-ads/retrieving.md
- Google Ads access levels: https://developers.google.com/google-ads/api/docs/api-policy/access-levels
- Google Data Manager send events: https://developers.google.com/data-manager/api/devguides/events/send-events
- Google Data Manager limits: https://developers.google.com/data-manager/api/devguides/limits
- Google Data Manager errors: https://developers.google.com/data-manager/api/reference/rest/v1/ErrorReason
- Google ECL setup: https://support.google.com/google-ads/answer/15713840?hl=en
- GA4 funnel reports: https://developers.google.com/analytics/devguides/reporting/data/v1/funnels

## Conclusão

**GO para a Fase 0 depois desta correção documental.** Antes de escrever a Fase 1,
os gates concretos são: criar/versionar o repositório Marketing Center, declarar o
remote/tag do Contact Center, provar `oca_dependencies`/CI/deploy, definir os backends
de segredo no servidor e concluir o inventário read-only/ativos `LAB`/capacidade da
fila.
