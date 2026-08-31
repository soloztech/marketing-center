# Meta read foundation — implementação e release

Data: 2026-08-31
Ambiente: `odoo16-teste.soloz.com.br` / servidor05
Produção tocada: não

## Veredito

O primeiro corte read-only de `marketing_center_meta` foi implementado, revisado,
testado e instalado no laboratório. O addon depende de `marketing_center_base`,
`meta_api_base` e `queue_job`, sem criar dependência entre os cores do Marketing
Center e do Contact Center.

## Escopo entregue

- perfil Meta reader administrativo e multiempresa;
- armazenamento apenas de referências externas a App Secret e access token;
- validação assíncrona de App, token e scopes;
- descoberta paginada e limitada de ad accounts;
- projeção idempotente em `marketing.center.source` e
  `marketing.center.connection`;
- fencing por revisão antes e depois de I/O;
- preservação de connections desabilitadas ou arquivadas;
- retries pela OCA `queue_job` e erros seguros do `meta_api_base`;
- views, ACLs e regras multiempresa.

Este corte não sincroniza campaign, adset, ad, creative, form, dataset, Insights ou
Lead Ads e não executa mutações externas.

## Cross-check

Black, isort, flake8, AST, XML e manifesto passaram. A revisão adversarial final não
encontrou P1/P2 restante e confirmou:

- segredos fora do banco e dos argumentos dos jobs;
- paginação por cursor bounded, com detecção de loop;
- locks e fencing contra resultados atrasados;
- no-op sem incremento de revisão funcional;
- conexão `disabled` ou `active=False` nunca é reativada pelo discovery;
- ACL administrativa e regra por `company_ids`.

## Falha encontrada pelo gate real

A primeira suíte integrada encontrou que o domínio implícito `active_test=True`
ocultava uma connection arquivada. O discovery tentava criar outra linha com a mesma
chave `(source_id, adapter_key, purpose)` e o PostgreSQL recusava a duplicação.

A busca técnica passou a usar `active_test=False`. Assim a linha arquivada é
observada e preservada sem update, recriação ou reativação. A primeira tentativa não
alterou a base principal; o orquestrador restaurou source, serviços e rota antes da
correção.

Evidência da recuperação automática:

`scans/raw/20260829-odoo16-marketing-center-first-slice/release/20260831T034546236198Z`

## Release aceito

- base: 39 testes, 0 falhas, 0 erros;
- integrado: 64 testes, 0 falhas, 0 erros, incluindo 23 do
  `marketing_center_meta`;
- instalação offline de `marketing_center_meta` `16.0.1.0.0`: concluída;
- upgrade de `marketing_center_base` e `marketing_center_contact_center`: concluído;
- replay dos três addons: concluído;
- bases temporárias e containers efêmeros: removidos;
- Odoo e dbmanager: `running`;
- HTTP privado e público: 200;
- rota de teste restaurada com SHA-256 idêntico;
- backup: dispensado pelo operador para o laboratório descartável;
- produção: não tocada.

Evidência canônica:

`scans/raw/20260829-odoo16-marketing-center-first-slice/release/20260831T035525832765Z`

## Próximo gate externo

Provisionar credenciais reader próprias para Marketing/Ads, comprovar o App ID e o
scope `ads_read` e executar o primeiro discovery real. Credenciais de Messaging do
Contact Center não devem ser reutilizadas implicitamente.
