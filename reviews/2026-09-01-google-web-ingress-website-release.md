# Google read-only, Web Ingress e Website — release de fundação

Data: 2026-09-01  
Ambiente: `odoo16-teste.soloz.com.br` / SERVIDOR05, base neutralizada  
Produção: não tocada

## Resultado consolidado

O primeiro corte multicanal e first-party foi instalado com sucesso:

- `marketing_center_google` `16.0.1.0.2`;
- `marketing_center_web_ingress` `16.0.1.0.2`;
- `marketing_center_website` `16.0.1.0.0`;
- `google_api_base` usa REST oficial v25 com identidade de service account e
  referências externas aos segredos;
- Web Ingress recebe envelope provider-neutral bounded, valida origin/host, aplica
  deduplicação first-wins e projeta o DTO no ledger canônico;
- Website publica somente uma configuração mínima para visitante anônimo e captura
  uma entrada de landing por sessão, sem ler formulário, cookie, PII ou DOM.

Release canônica:
`scans/raw/20260901-odoo16-marketing-center-website-first-party/release/20260901T151930953941Z`.

Gates executados em bancos descartáveis:

- core: 94 testes;
- integrações: 440 testes;
- Website/Web Ingress: 40 testes;
- instalação e replay de upgrade na base principal: concluídos;
- HTTP privado e público: 200;
- rota Traefik restaurada byte a byte.

## Problemas encontrados pelos gates

Três candidatos foram rejeitados antes de tocar a base principal:

1. HttpCases de dependências iniciavam HTTP enquanto o Website ainda estava sendo
   carregado; as suítes foram separadas e os HttpCases marcados `post_install`.
2. A view escondia `company_id` por grupo, mas o campo era necessário no domínio do
   endpoint; o campo passou a estar disponível a todo administrador autorizado.
3. O teste proibia qualquer `Set-Cookie`, mas o dispatcher Website pode criar somente
   `frontend_lang`; o contrato agora proíbe sessão/autenticação e aceita apenas essa
   preferência técnica de idioma.

O terceiro gate também revelou que nove contratos puros herdavam diretamente de
`unittest.TestCase` e não eram executados pelo loader do Odoo. Eles agora herdam de
`TransactionCase` no runtime Odoo e permanecem executáveis sem Odoo; a contagem
integrada subiu de 431 para 440.

## Validação externa Google

A identidade real validou autenticação e discovery com uma raiz e uma conta leaf.
Cinco runs bounded de performance terminaram e projetaram 245 fatos. O primeiro
catálogo revelou incompatibilidades GAQL v25; a correção está no candidato
`marketing_center_google` `16.0.1.0.3`, com contrato
`google.ads.catalog.v25.2`, e será promovida no próximo release consolidado.

Evidência sanitizada da leitura real:
`scans/raw/20260901-odoo16-marketing-google-live-read/20260901T152458016665Z`.
Nenhum token, customer ID, resource name ou credencial é gravado nessa evidência.

## Ativação do Website no laboratório

O `website.default_website` foi ligado a um endpoint exclusivo da mesma empresa, com
origin e host exatos de `odoo16-teste.soloz.com.br`. O provisionador executou primeiro
uma transação forçada a rollback, aplicou endpoint+binding uma vez e comprovou que a
segunda passagem não faria alteração.

- configuração anônima: HTTP 200, `enabled=true`, somente três chaves;
- configuração autenticada: HTTP 200, somente `enabled=false`;
- ingresso sintético e replay: HTTP 202 nas duas chamadas, um evento e um touchpoint;
- Chromium real: home pública e assets carregados, zero erro e zero warning no
  console;
- nenhum deploy, restart ou acesso à produção.

Evidência sanitizada:
`scans/sanitized/20260901-odoo16-marketing-center-website-ingress-summary.json`.

## Limites deste corte

- captura de landing está entregue; submissão de formulário e redirect/click para
  WhatsApp permanecem no próximo slice Website;
- correlação touchpoint↔fato não significa crédito causal;
- nenhum evento é enviado de volta a Google ou Meta;
- nenhuma campanha, verba, lance ou status é alterado.
