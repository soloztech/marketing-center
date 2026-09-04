# Resposta crítica à revisão estática independente — Integration Core

- Data: 2026-09-03
- Escopo: `meta_api_base`, `meta_webhook_base` e `google_api_base`
- Origem confrontada: parecer estático anexado à sessão de 03/09
- Estado: correções estáticas concluídas; runtime no SERVIDOR05 ainda pendente

## Veredito

O alerta principal procedia parcialmente. A reconciliação de subscriptions mantinha um
`FOR UPDATE` na linha do endpoint enquanto fazia I/O Meta. A rota pública lê a mesma
linha sob lock compartilhado, portanto uma Graph API lenta podia atrasar o GET de
challenge e o POST do webhook.

A solução correta não é remover toda coordenação durante o I/O. A reconciliação precisa
impedir que credencial, asset, revision ou política sejam trocados no meio de um efeito
externo e depois persistidos como se pertencessem à nova configuração. O lock de
propriedade passou de exclusivo para `FOR SHARE`:

- vários callbacks públicos e snapshots internos continuam concorrentes;
- uma alteração de configuração continua aguardando o efeito em voo;
- o resultado ainda é persistido contra a mesma revisão que autorizou a chamada.

Assim, foi removida a incompatibilidade que causava indisponibilidade pública sem abrir
uma janela de configuração mista.

O contra-check adversarial encontrou ainda uma consequência importante dessa troca: dois
jobs já enfileirados para o mesmo UUID poderiam manter locks compartilhados ao mesmo
tempo e repetir o I/O remoto. A versão final adquire primeiro um `pg_advisory_xact_lock`
transacional, namespaced por Endpoint. Ele serializa somente os workers de
reconciliação; ao acordar, o duplicado relê o UUID já limpo/substituído e termina antes
da rede. A rota pública não participa desse namespace e continua compatível com o
`FOR SHARE`.

## Hardening adicional encontrado no fechamento

Jobs e dispatch interno podiam entrar com `allowed_company_ids` herdado do worker.
Embora as buscas usassem IDs estáveis e houvesse validações de empresa, o ambiente do
registro não era reancorado de forma uniforme antes de chamar extensões consumidoras.

Agora controller, delivery, item e dispatcher trabalham no ambiente da empresa dona do
endpoint, com `allowed_company_ids` contendo exatamente essa empresa. Testes executam
jobs a partir da empresa errada e verificam o escopo recebido pelo consumidor.

## Revisão por addon

| Addon               | Resultado                                                                                                                                                                           |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `meta_api_base`     | Contrato de credencial/Graph, assinatura, limites e erros permanece provider-core e sem dependência de Contact/Marketing. Nenhum P1/P2 novo.                                        |
| `meta_webhook_base` | Lock incompatível corrigido; workers duplicados serializados antes do I/O; contexto multiempresa de callbacks/jobs endurecido; ledger e fan-out permanecem únicos e compartilhados. |
| `google_api_base`   | Credencial, transporte e erro compartilhados continuam independentes do Marketing Center. Nenhum P1/P2 novo.                                                                        |

## Retenção e imutabilidade

A ausência de uma política completa de retenção é um risco verdadeiro de capacidade e
LGPD, não um motivo para tornar deliveries mutáveis ou apagá-los por idade sem contrato.
Delivery/item, hash, dedupe e dispatch auditam o que entrou e quem consumiu. O
fechamento produtivo deverá separar payload/identificadores apagáveis da prova
operacional mínima, com prazo, legal hold e purge paginado. Essa política é um gate
cross-repo e não foi fingida como resolvida por um `unlink()` genérico.

## Dashboard

A view SQL citada pertence a `marketing_center_dashboard`, não ao Integration Core. O
tamanho aproximado de 550 linhas é hipótese de manutenção/desempenho, não evidência de
lentidão. O gate correto é `EXPLAIN (ANALYZE, BUFFERS)` com cardinalidade
representativa, acompanhado de índices e tempo-alvo; dividir o SQL por LOC sem plano de
execução não melhora desempenho por si só.

## Validação

- Versões candidatas: `meta_api_base` `16.0.1.1.1`, `meta_webhook_base` `16.0.1.4.2` e
  `google_api_base` `16.0.1.1.2`.
- Inventário atual: 45 testes em `meta_api_base`, 73 em `meta_webhook_base` e 42 em
  `google_api_base` (160 declarados no total).
- Formatação, imports, lint, compilação e XML passaram no pente-fino estático.
- Suíte Odoo e release atômico ainda serão executados e terão evidência própria.
- Produção não foi acessada nem alterada.

## Fechamento runtime — 2026-09-04

O limite temporal acima foi encerrado pelo release canônico no SERVIDOR05: 160/160
testes Odoo passaram (45 Meta API, 73 Meta Webhook e 42 Google API), apply e replay
terminaram com status zero, as versões instaladas foram confirmadas e a rota pública
retornou HTTP 200 após a restauração. Produção não foi tocada.

Evidência:
`scans/raw/20260903-odoo16-integration-core-greenfield-closeout/release/20260904T025541471633Z/summary.json`.
