# Google API Base — fundação e disposição adversarial

Data: 2026-09-01  
Escopo: somente `integration-core/google_api_base`; nenhuma instalação, alteração de
banco, deploy ou mudança em Marketing Center/Contact Center/scripts.

## Resultado

Foi criado o addon técnico `google_api_base` `16.0.1.0.0`, dependente apenas de
`base` e `requests`. Ele contém:

- `google.api.identity` multiempresa, com `public_ref` imutável e revisão monotônica;
- referências environment/file para OAuth client ID, OAuth client secret, refresh
  token e developer token; nenhum valor de credencial é persistido;
- capability process-local e revisão esperada obrigatória para resolver um runtime;
- OAuth refresh no endpoint fixo oficial, sem persistir access token;
- REST Google Ads v25 fixo para `customers:listAccessibleCustomers` e
  `GoogleAdsService.Search` paginado;
- headers `developer-token` e `login-customer-id` normalizado quando aplicável;
- DTOs técnicos sem campanha, atribuição, lead ou qualquer domínio de negócio;
- respostas, páginas, linhas, query, cursor, tempo agregado e timeouts limitados;
- erros tipados com request ID, status/reason allow-listed e cooldown sanitizado;
- ACL exclusiva de `base.group_system` e regra por empresas ativas.

O transporte não depende de `queue_job`: o futuro addon consumidor deve chamar a
fachada dentro de jobs OCA. Assim o core continua neutro e não ganha fila ou regra de
sincronização de um domínio específico.

## Achados aceitos e corrigidos

1. **Contexto oculto de exceção sensível.** `raise ... from None` ainda preservava a
   exceção original em `__context__`. Os limites de rede, JSON, encoding e arquivo
   agora saem do bloco `except` antes de levantar o erro sanitizado; testes verificam
   `__cause__` e `__context__` vazios.
2. **Runtime serializável.** `GoogleRuntimeIdentity` e `GoogleOAuthToken` bloqueiam
   pickle por `__reduce__`/`__reduce_ex__`; representação e comparação também não
   carregam os valores protegidos.
3. **Fence opcional.** `_resolve_runtime()` agora exige uma revisão inteira positiva;
   `None`, booleano, zero, tipo incorreto e revisão obsoleta falham antes da resolução
   das credenciais.
4. **OAuth antes de validar conta.** A fachada normaliza e valida o customer ID antes
   de adquirir token, então entrada inválida não provoca IO.
5. **Orçamento agregado.** O iterador aplica teto de páginas, linhas e tempo, limitado
   também pela vida restante do access token.
6. **Cooldown de quota insuficiente.** A resposta v25 agora considera, nesta ordem,
   `Retry-After`, `quotaErrorDetails.retryDelay` estritamente sanitizado e fallback de
   300 s para excesso curto, 1.800 s para excesso longo ou 60 s genérico. `rateName` e
   mensagens do provedor não são retidos.

Referências oficiais do último item:

- https://developers.google.com/google-ads/api/reference/rpc/v25/QuotaErrorDetails
- https://developers.google.com/google-ads/api/docs/productionize/manage-data-efficiently

## Refutações e qualificações

- Não restou refutação funcional P1/P2: todos os achados concretos foram aceitos.
- A ausência de `TransactionCase` concorrente não contradiz o locking implementado,
  mas continua como gate de integração para comprovar a serialização real do
  `FOR UPDATE` no PostgreSQL/Odoo.
- O read timeout de `requests` não é um deadline de socket contra slow-drip. URL fixa,
  limite de bytes e orçamento agregado reduzem o risco, mas um worker/SDK isolado
  poderá ser reavaliado se o volume real justificar.
- O core não tenta provar que está dentro de um job OCA. Essa política pertence ao
  consumidor `marketing_center_google`, que ainda não faz parte deste corte.

## Evidência local

- 33 testes Odoo/unitários escritos;
- 26 testes puros de resolver/OAuth/transporte/fachada executados sem rede: verdes;
- `compileall`, AST do manifest, XML, CSV, Black, isort, `git diff --check` e flake8
  com `max-complexity=16`: verdes;
- revisão adversarial final: nenhum P1 e nenhum P2 remanescente após a correção de
  quota.

Os sete testes de ORM, ACL, record rule, revisão e multiempresa foram escritos, mas
exigem instalação em uma base Odoo real. Eles ficam explicitamente pendentes para o
primeiro release integrado; este corte não autorizava instalar ou fazer deploy.
