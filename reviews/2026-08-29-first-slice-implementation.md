# Marketing Center — primeiro corte implantado

Data local: 2026-08-29  
Ambiente: `odoo16-teste.soloz.com.br` / servidor05 / base neutralizada

## Resultado

- `marketing_center_base` `16.0.1.0.0`: instalado;
- `marketing_center_contact_center` `16.0.1.0.0`: instalado;
- `contact_center_base`: somente dependência consumida pelo contrato persistido;
- nenhuma dependência entre `contact_center_base` e `marketing_center_base`;
- nenhum addon `contact_center_*` foi copiado ou atualizado por este release;
- árvore implantada: `aa4bea7351e495e42babcd4bc9f9302158b9d19db273e54f381c85589101eb21`.

## Gates executados

- Black 22.8, isort 5.12, flake8 e compilação Python: aprovados;
- instalação isolada do base: 11 testes, 0 falhas e 0 erros;
- instalação integrada do bridge: 13 testes, 0 falhas e 0 erros;
- primeiro upgrade offline da base principal: aprovado;
- segundo upgrade offline idempotente: aprovado;
- Odoo privado e rota pública: HTTP 200 após restauração;
- produção: não tocada;
- backup da base de laboratório: dispensado conforme decisão do proprietário.

Evidência canônica do release final:

`/home/lucaszotelli/infra-ai-ops/scans/raw/20260829-odoo16-marketing-center-first-slice/release/20260830T025312504382Z`

## Validação com dados do laboratório

O backfill explícito encontrou 126 touchpoints no ledger operacional do Contact
Center. Após drenagem da fila:

- 126 touchpoints no ledger de Marketing;
- 126 links imutáveis Contact Center → Marketing Center;
- 126 evidências e 50 identificadores sanitizados;
- 0 jobs ativos e 0 jobs falhos.

Um segundo backfill integral foi executado. Ele processou novamente as 126 origens e
manteve 126 touchpoints e 126 links, comprovando o replay idempotente.

## Intercorrências encontradas pelos gates

Antes de alcançar a base principal, os gates bloquearam e recuperaram duas tentativas:

1. uma view ocultava `company_id` apesar de um domínio automático `check_company`;
2. a fixture integrada dependia de um adapter fictício registrado por outra suíte.

As correções foram aplicadas, os testes tornaram-se autocontidos e o release final
passou integralmente. Nenhuma dessas tentativas alterou a base principal.

## Próximo corte

Implementar os modelos operacionais restantes do `marketing_center_base` — source,
connection, external entity/revision e sync run/cursor — e então iniciar os clientes
técnicos compartilhados `meta_api_base` e `google_api_base`.
