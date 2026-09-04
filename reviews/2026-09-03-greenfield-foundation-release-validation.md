# Fundação greenfield — validação do release

Data: 2026-09-03

Ambiente alvo: `odoo16-teste.soloz.com.br` / SERVIDOR05

Estado: **aplicado e validado no SERVIDOR05 (`applied_and_validated`)**

## Escopo congelado

O release greenfield do Marketing Center implantou somente os 15 addons do domínio:

| Addon | Versão aplicada |
| --- | --- |
| `marketing_center_base` | `16.0.1.7.4` |
| `marketing_center_contact_center` | `16.0.3.1.0` |
| `marketing_center_crm` | `16.0.1.3.0` |
| `marketing_center_contact_center_crm` | `16.0.1.2.0` |
| `marketing_center_meta` | `16.0.2.1.1` |
| `marketing_center_meta_crm` | `16.0.1.2.1` |
| `marketing_center_sale` | `16.0.1.1.1` |
| `marketing_center_account` | `16.0.1.1.1` |
| `marketing_center_sale_account` | `16.0.1.1.1` |
| `marketing_center_dashboard` | `16.0.1.2.0` |
| `marketing_center_google` | `16.0.1.1.2` |
| `marketing_center_web_ingress` | `16.0.2.1.0` |
| `marketing_center_website` | `16.0.2.2.0` |
| `marketing_center_website_crm` | `16.0.1.5.0` |
| `marketing_center_suite` | `16.0.1.0.0` |

O contexto técnico esperado mantém `meta_api_base` `16.0.1.1.1`,
`meta_webhook_base` `16.0.1.4.0` e `google_api_base` `16.0.1.1.2` como dependências
compartilhadas; elas não fazem parte da árvore promovida por este release.

## Evidência final

- contrato local do orquestrador de release: **14/14 verde**;
- árvore aplicada e verificada: **373 arquivos**;
- hash SHA-256 da árvore aplicada:
  `be49fb73123af364e6e84d5bc8d7096d7d5e6cf1d2db70b086a953f49f9e570c`;
- versões dos manifests, versões pinadas pelo deploy e versões esperadas pelo
  orquestrador: coerentes e instaladas na base principal;
- suítes canônicas em bancos efêmeros:

| Suíte | Resultado | Falhas | Erros |
| --- | ---: | ---: | ---: |
| base | 120/120 | 0 | 0 |
| integrada | 633/633 | 0 | 0 |
| Website/HTTP | 119/119 | 0 | 0 |
| facade completa | 5/5 | 0 | 0 |

O upgrade offline dos 15 addons encerrou com status **0**. O replay offline dos
mesmos addons também encerrou com status **0**, comprovando a repetição idempotente
do processo na base principal.

Após o replay, as versões instaladas coincidem com a tabela do escopo congelado. A
convergência terminou com:

- `active_count=0`;
- `failed_count=0`;
- `projection_failed_count=0`;
- `unexpected_count=0`.

## Validação no navegador e disponibilidade

O QUnit autenticado da suíte `marketing_center_website` passou nas duas formas de
assets:

| Modo | Testes | Assertions | Falhas |
| --- | ---: | ---: | ---: |
| minificado | 11/11 | 45/45 | 0 |
| `debug=assets` | 11/11 | 45/45 | 0 |

Não houve falha de asset, erro de página ou erro de runtime. A sessão exclusiva do
navegador foi encerrada e os artefatos de console e screenshots ficaram ligados ao
hash da árvore aplicada.

Um smoke autenticado adicional, somente leitura, abriu o painel gerencial, eventos
de negócio, touchpoints efetivos, performance diária, histórico e diagnósticos do
Google Ads, submissões Meta, projeções Meta CRM e vínculos do Contact Center. Todas
as views carregaram; o console terminou com **0 erros e 0 warnings**, e a sessão do
navegador foi encerrada.

O endpoint privado respondeu HTTP **200**. Depois do release, o endpoint público
`odoo16-teste.soloz.com.br` também respondeu HTTP **200**. A rota de teste foi
restaurada com SHA-256
`2fd9e569856478dfa336391bd226f3c8af05454db56033be6352ef80aad56403`, sem backend
ou hostname de produção. `production_touched=false`.

## Tentativas recuperadas antes do apply final

O orquestrador preservou cinco interrupções intermediárias de 2026-09-03:

| Evidência | Gate interrompido | Recuperação |
| --- | --- | --- |
| `20260903T224405439789Z` | base | `failed_recovered` / `pre_database_change` |
| `20260903T224913825625Z` | integrada | `failed_recovered` / `pre_database_change` |
| `20260903T230642322668Z` | integrada | `failed_recovered` / `pre_database_change` |
| `20260903T231910051116Z` | Website | `failed_recovered` / `pre_database_change` |
| `20260903T232923097301Z` | Website | `failed_recovered` / `pre_database_change` |

Em todas elas, as fontes anteriores foram restauradas, Odoo e dbmanager voltaram
ao estado `running`, a disponibilidade privada retornou HTTP 200, produção não foi
tocada e a base principal ainda não havia sido alterada. Essas execuções expuseram
testes e contratos inconsistentes, que foram corrigidos antes da execução final;
não representam upgrades parcialmente aplicados.

## Conclusão

Todos os gates previstos para esta fundação estão concluídos. O release está
instalado e validado no laboratório, sem implicar autorização de produção, escrita
UTM ou cutover native-first.

Evidência canônica do release aplicado:
`scans/raw/20260903-odoo16-marketing-center-greenfield-foundation/release/20260903T233620975902Z`.
