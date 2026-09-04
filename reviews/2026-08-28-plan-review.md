# Revisão do `plan.md` — Marketing Center

- Data: 2026-08-28
- Revisor: Claude (coordenação); 6 lentes independentes (arquitetura, modelo de dados,
  fatos de plataforma com fontes oficiais, segurança/privacidade, risco de entrega,
  consistência) + 47 refutadores adversariais (14 P0/P1 na 1ª rodada, 33 temas na 2ª)
- Alvo: `plan.md` (1 510 linhas, criado em 2026-08-28), nenhum código existente
- Fatos verificados pelo coordenador: Odoo 16 **Community/OCB**, Python 3.10 no lab;
  `marketing_automation`/`social` (Enterprise) `uninstallable`; instalados `crm`, `utm`,
  `link_tracker`, `website`, `website_crm`, `sale_management`, `account`, `queue_job`; 3
  736 leads, 41 `utm.campaign`, 1 239 pedidos confirmados; credenciais Google
  (reader/writer SA, dev token nível Explorer) e Meta já existem; `fields.Integer` do
  Odoo 16 é `int4`, `Float(digits)`/`Monetary` são `numeric`; `requirements.txt` do Odoo
  16 não traz protobuf/grpcio; os 6 links locais do plano resolvem.

## Veredito

**GO para a Fase 0 e para a escrita da Fase 1, com 5 correções P1 antes de qualquer
código.** É um plano de qualidade alta: separação de domínios (Marketing × Contact
Center × CRM), tríade `platform_reported` / `odoo_actual` / `modelled` que nunca se
mistura, ledger append-only com revisões para restatements, read-only antes de escrita,
objetos `PAUSED` por padrão, `uncertain` em timeout, `queue_job` para todo I/O, Data
Manager em vez do upload legado. A maioria dos 47 achados iniciais **não sobreviveu à
refutação** (25 refutados, 23 confirmados com severidade rebaixada em quase todos): o
desenho está certo; o que falta é fechar definições que hoje só existem como princípio.

## P1 — fechar antes da Fase 1

| #        | Achado                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Linhas                          | Correção                                                                                                                                                                                                             |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **P1-1** | **Grafo cruza fronteira de repositório.** `contact_center_base` passaria a depender de `marketing_attribution_base`, hospedado "inicialmente nesta pasta" — dentro do `infra-ai-ops`, que ignora o repo `contact-center` (`.gitignore:30`). A CI/`oca_dependencies`/`setup/` do contact-center não resolvem a dependência; release e tag quebram; nasce um ciclo entre repositórios.                                                                                                              | 150-154, 452-479, 1067-1086     | Decidir a hospedagem **antes** da Fase 1: `marketing_attribution_base` e `meta_api_base` nascem no repo `contact-center` (ou num repo AGPL-3 dedicado) e são consumidos pelos dois lados via `oca_dependencies.txt`. |
| **P1-2** | **`business_event_key` nunca é definido e não há correlação de estorno.** Fatura paga em 3 parcelas gera 3 `payment_received` que colidem ou duplicam; `refund_or_reversal` não tem `reversal_of_id` (o runbook de 04/08 tinha, o plano regrediu); Data Manager deduplica por `transactionId` e retração exige o `order_id` original.                                                                                                                                                             | 591-603, 810-811, 876           | Tabela evento → gatilho Odoo → chave natural → regra de reversão; campo `reverses_event_id`; definir `business_event_key` por tipo (ex.: `payment:<partial_reconcile_id>`).                                          |
| **P1-3** | **"Test Events" da Meta não é sandbox.** Doc oficial CAPI (consultada em 2026-08-28): eventos com `test_event_code` "flow into Events Manager and are used for targeting and ads measurement". O E2E "landing fake → venda → outbox" e as fixtures de `Purchase`/`Lead` contaminariam o dataset/pixel de produção.                                                                                                                                                                                | 370, 377, 996, 1205, 1330, 1437 | Dataset/pixel **de teste** separado para lab e E2E; `test_event_code` só para inspeção visual; promover por policy com allow-list de origem.                                                                         |
| **P1-4** | **Consentimento/base legal tratados como opcionais.** `consent_policy opcional` no DTO de conversão; nenhum campo de base legal/versão do aviso no touchpoint ou no DTO v2; "LGPD não é o foco". Business Tools Terms (Meta), Customer Data Terms (Google) e a LGPD (pseudonimização = dado pessoal; transferência internacional, Res. ANPD 19/2024) exigem base legal **antes** de enviar e-mail/telefone hasheado. Regressão frente ao runbook de 04/08 (snapshot de consentimento por pessoa). | 653-672, 692, 959, 1422-1430    | Campo de base legal/consentimento nasce no touchpoint (Fase 1) e é obrigatório no `MarketingConversionDTO`; outbox nasce **negando** por padrão.                                                                     |
| **P1-5** | **Onde mora a credencial de escrita não está decidido** ("segredo externo OU campo restrito"). `groups=` protege UI/ORM, não dump, réplica ou `sudo()` (todos os jobs). O precedente que será extraído (`contact_center_meta/models/meta_app.py`) guarda `app_secret`/`access_token` em `Char` só com `groups=`. Token de escrita move verba.                                                                                                                                                     | 229-230, 240-244, 273, 1077     | Decisão fechada: writer em cofre externo/variável de ambiente referenciada por perfil; no banco só o reader, e mesmo assim com `groups=` + auditoria de leitura. Registrar em "Decisões Fechadas".                   |

## P2 — corrigir na próxima revisão do plano

- **`external_ref` colapsa keywords**: no Google Ads só `(ad_group_id, criterion_id)` é
  único; `ad_id` cru idem. `external_ref` deve ser o resource name / ID composto
  (`{ad_group}~{criterion}`), com lista dos casos compostos. (498-501, 872)
- **Fase 7 depende de um site que não existe** (programa do site não aprovado, 8-12
  semanas) e o site real é WordPress sem tag. Separar "captura de origem" de "site
  novo": instrumentar o WordPress agora (já era P1 na auditoria de 04/08) com endpoint
  de captura no Odoo. (114-115, 911-931, 1177-1191)
- **`metric_origin` e janela de atribuição fora da chave da métrica**;
  `metric.action.daily` sem chave; Meta muda `actions` por `action_attribution_windows`
  (desde 06/2025 espelha o ad set). Registrar janela/modelo por lote e linha; se
  `odoo_actual` vive em `attribution.result`, dizer isso. (505-513, 534, 872)
- **Click IDs**: classificar quais IDs são "sensíveis" (click/person IDs → `identifier`
  com `groups=`; IDs de ativo → colunas) e **remover parâmetros de clique da URL
  armazenada**, não só na UI. (561-578, 742, 1226)
- **`observed_at` na chave da revisão** faz cada releitura inserir linha idêntica. Chave
  `(metric_daily_id, content_hash)` + `first/last_observed_at`. (871-873)
- **Lifecycle/revenue sem gatilho nem chave natural**: `won/lost`, `action_confirm`,
  `action_post` são reexecutáveis; `payment_state` é computado. Backfill rodado 2×
  duplica. Tabela gatilho→chave→reversão + "estado atual vs contagem de eventos" nos
  dashboards. (591-603, 799-808, 1166)
- **Dependência invertida**: após a migração, o painel de atribuição do atendimento
  passaria a precisar do bridge `marketing_center_contact_center` (que depende de
  `marketing_center_base`). Manter no `contact_center_*` uma projeção lida do ledger
  neutro sem o app de marketing. (161, 411-415, 470-471)
- **`MarketingPerformanceDTO` sem `currency`** (a fonte pode divergir da conta) e
  **contrato de adapter sem envelope de retorno/cursor/job assíncrono** — "o cursor só
  avança com a persistência" não é implementável sem isso. (640-654, 715-736, 781)
- **Fronteira do dia para `odoo_actual`** (Datetime UTC) vs dia da plataforma (fuso da
  conta) indefinida — afeta todo "Ads × Odoo". (516, 533, 1218)
- **`canonical_key` v2**: normatizar a composição; hoje embute `connection.id`.
  (578, 874)
- **Margem**: `sale_margin` não instalado; definir fonte/pré-condição ou tirar "ROAS de
  margem" do MVP. (34-35, 398, 1216)
- **Roster sem modelo**: as record rules por roster não têm entidade para referenciar.
  Criar `marketing.center.team` (ou reusar padrão do Contact Center). (753-755)
- **Aprovação sem ponto de imposição**: transição para `approved` só por método com
  checagem de grupo/policy; ACL de `marketing.approval`; revalidação de validade/autoria
  no job. (618-620, 758, 820)
- **Retenção/anonimização ausentes** num ledger que proíbe editar/destruir — repete e
  amplia a dívida aceita no Contact Center (28k eventos sem política). Definir ao menos
  anonimização por supersession. (580-581, 1392-1405)
- **`/m/r/<token>`**: endpoint público que escreve no ledger — destino fixo no servidor,
  token opaco com expiração, separado do token de visitante, filtro de bots/prefetch,
  rate limit (tudo já estava no runbook de 04/08). (426, 920, 1189)

## P3 e higiene

Nível do dev token como gate da Fase 0 (Explorer = 2 880 ops/dia, upgrade para Basic não
está no roadmap); tabela de supersessão das decisões de 04/08 (n8n, Chatwoot, Evolution,
gateway isolado, panorama martech); "Decimal" → `Float(digits)`/`numeric`; "não muda de
estado" vs enriquecimento monotônico (redação); remover a referência ao upload da Ads
API (bloqueado para este token desde 2026-06-15; Data Manager é o único caminho, e ECL
exige Google tag no site); "view materializada" não existe no ORM; pivots nativos somam
métricas não aditivas; search terms são alta cardinalidade (contradiz §Volume);
tombstone para entidade removida remotamente; cardinalidade de `attribution.result`;
dois prefixos de modelo (`marketing.*` × `marketing.center.*`) sem regra de dono;
`grain`/`level`/`entity_type` = 3 nomes; Meta access tiers/App Review/Business
Verification e Lead Ads (90 dias de retenção, `pages_manage_metadata`, "webhook **e**
pull"); CAPI `event_time` ≤ 7 dias; Python 3.10 EOL 2026-10; funis GA4 só em v1alpha;
versões Graph misturadas (v26/v25); 8 canais `queue_job` no mesmo runner do Contact
Center sem dimensionamento; contas/ativos de teste não nomeados; tenants (Industrial ×
Energia/Odoo 18) não declarados; falta glossário, ER e um registro exemplo ponta a
ponta; acentuação inconsistente e listas emendadas.

## Refutados — não alterar por esses motivos

- "Custo em micros é impossível (int4)": é regra semântica, não tipo; usar `int8` via
  `column_type`, ou `numeric`. Vale só registrar o tipo.
- "NULL nas chaves", "`utm.mixin` colide", "filhos sem `company_id`", "kill switch só na
  Fase 11" (flags por fonte já na Fase 2), "stale por hash remoto" (é revisão local),
  "Google antes de Meta" (Fase 0 já cobre acesso Meta), "sem estimativas" (plano se
  declara de arquitetura), "keyword sem entidade", "ingress herda defeitos da r3" (o
  código atual já quarentena item ruim), "migração do ledger na Fase 1 é cirurgia" (é
  aditiva, remoção só depois), "DTO v2 é reescrita" (mantém os campos centrais), "CPL só
  na Fase 9" (baseline na 0, dashboard na 3 e 6).
- **"Fases 10-11 não têm demanda"**: refutado pelo próprio repositório —
  `marketing/paid-media/agency-transition-baseline-2026-08-28.md` registra que a Soloz
  vai **assumir** a mídia paga da VMX. As mutações assistidas têm demanda; a sequência
  read-only → assistido do plano está certa.

## Forças a preservar

Tríade de origem das métricas; nomes nunca são chaves; revisões + projeção atômica;
cursor avança com persistência; `uncertain`/`stale`/fencing token; separação delivery
ledger × attribution ledger; Data Manager em vez de `ConversionUploadService`; spike do
SDK antes de fixar; views nativas antes de OWL; bridges opcionais sem inflar o core.

## Sugestão de sequência (sem mudar a arquitetura)

1. Aplicar P1-1…P1-5 no texto (½ dia) e registrar as decisões em "Decisões Fechadas".
2. Fase 0 como está, acrescentando: nível do token, dataset de teste Meta, instrumentar
   o WordPress atual, inventário de termos aceitos (Meta/Google) e tenants em escopo.
3. Fase 1 com `marketing_attribution_base` + `meta_api_base` **no repo contact-center**.
4. Primeira tela útil = Fase 3 (Google read-only) **e** Insights Meta read-only juntos,
   ambos no grão conta/campanha/dia, com linha "não atribuível" — é o que responde
   CPL/CAC por campanha com o CRM/UTM que já existem (62,9 % de cobertura UTM nos 90
   dias apontada na lente de entrega).
