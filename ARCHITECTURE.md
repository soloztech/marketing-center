# Marketing Center — arquitetura atual

A aplicação usa o CRM nativo e OCA `queue_job`. Cada addon declara suas dependências,
segurança e testes. O recebimento de mensagens WhatsApp pertence ao addon independente
`contact_center`; não há dependência entre ele e Marketing Center.

| Addon                           | Responsabilidade                                                                                                    |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `marketing_center_base`         | Fontes, conexões, catálogo externo, métricas, touchpoints, eventos, atribuição CRM, UTMs nativas e visão gerencial. |
| `marketing_center_meta`         | Catálogo e Insights Meta, Lead Ads, projeção ao CRM e revisão comercial.                                            |
| `marketing_center_google`       | Catálogo, métricas e resolução de cliques Google.                                                                   |
| `marketing_center_website`      | Ingresso web, consentimento, captura e projeção dos formulários nativos ao CRM.                                     |
| `marketing_center_sale`         | Eventos de vendas sobre documentos nativos.                                                                         |
| `marketing_center_account`      | Eventos financeiros sobre documentos nativos.                                                                       |
| `marketing_center_sale_account` | Relações entre os eventos de vendas e contabilidade.                                                                |
| `meta_api_base`                 | Transporte e credenciais Meta.                                                                                      |
| `meta_webhook_base`             | Recebimento e despacho dos webhooks Meta dos consumidores instalados.                                               |
| `google_api_base`               | Transporte e credenciais Google.                                                                                    |

As pontes Contact Center, Website–WhatsApp e Automation foram retiradas. Não há Jornada
WhatsApp, enriquecimento de conversas, envio de mensagens ou sincronização conversa–CRM
nesta aplicação. Os documentos, leads e evidências nativas existentes não são apagados
para retirar essas integrações.

## Limites dos dados

O catálogo mantém identidades externas e evidências; `crm.lead` mantém o negócio. A
classificação de Campanha, Origem e Meio usa vínculos admitidos, a política da fonte e
recibos com os valores anteriores e posteriores. Edições manuais são preservadas. A
retirada de um produtor exige verificar os vínculos e as classificações existentes antes
de remover seu código.

As credenciais ficam nas fundações e nos perfis dos providers. A revisão comercial
mostra os dados comerciais permitidos sem conceder acesso ao cofre Meta. Sem uma fonte
de candidatos, a revisão oferece criar demanda nova ou encerrar sem criar; liberação
técnica exige administração de Sistema e Marketing, com proteção desativada.

## Filas e interfaces

Marketing usa os canais e métodos dos módulos que permanecem instalados. Não depende dos
antigos canais de mensagens do Contact Center. O runner OCA precisa de capacidade HTTP e
PostgreSQL; nomes de canais não reservam trabalhadores.

Os formulários são nativos de Website/CRM. Vendas e contabilidade continuam nos seus
modelos nativos. Content Center fica em repositório próprio, com `content_center_base` e
a integração opcional `content_center_sale`.

## Publicação e registros anteriores

A branch oficial é `16.0`. Mudanças de versões instaladas exigem upgrade nativo e
verificação de código, banco e providers. Preservar trabalho não relacionado. Os
procedimentos antigos de extração e fusão são registros históricos; não são instruções
para reinstalar módulos retirados. Consulte
[operações de filas e credenciais](docs/queue-and-credential-operations.md) e os
registros da entrega.
