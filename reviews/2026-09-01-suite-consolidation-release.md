# Marketing Center — consolidação da distribuição

Data: 2026-09-01 Ambiente: `odoo16-teste.soloz.com.br` / servidor05 Resultado:
**aplicado e validado**

## Decisão

O Marketing Center permanece um único produto com 14 componentes funcionais no mesmo
Odoo, banco PostgreSQL e JobRunner. Os componentes continuam separados porque
representam providers, aplicativos Odoo opcionais ou bridges entre domínios opcionais;
eles não são microserviços nem aplicações independentes para o usuário.

Foi criado `marketing_center_suite` como fachada de instalação completa. Ele não possui
modelo, tabela, regra de negócio, segurança, menu, controller ou job próprio. Suas seis
dependências-folha fecham exatamente os 14 componentes adotados pela Soloz.

No catálogo de aplicativos do Odoo:

- `marketing_center_suite` é o único módulo com `application=True`;
- `marketing_center_base` e os outros 13 componentes têm `application=False`;
- o único menu raiz funcional continua pertencendo ao base;
- instalações técnicas menores continuam possíveis quando desejadas.

O mapa de responsabilidades e os critérios para criar novos addons estão em
[`../ARCHITECTURE.md`](../ARCHITECTURE.md).

## Ajustes incluídos

- criado o addon agregador `marketing_center_suite` e seu contrato de instalação;
- alterado `marketing_center_base` para componente técnico;
- declarada a dependência direta de `marketing_center_website_crm` sobre
  `marketing_center_web_ingress`;
- inventário, hashes, versões, ordem de instalação e validação do catálogo de Apps
  adicionados aos scripts de deploy/test/release;
- adicionada uma quarta base efêmera para provar instalação limpa e fechamento exato do
  suite;
- preservado `auto_install=False` nos bridges para impedir ativação implícita de efeitos
  de negócio.

## Correção encontrada pelo gate integrado

Três testes concorrentes do núcleo usavam `service="meta.ads"` e
`adapter_key="meta.graph"` apenas como valores ilustrativos. Como são testes
`post_install`, a instalação integrada carregou a extensão Meta, que corretamente exigiu
um perfil Meta real.

A fixture foi tornada genuinamente provider-neutral com `test.concurrent` nos dois
discriminadores. A validação do Meta não foi afrouxada. Os mesmos três testes agora
provam que o contrato neutro continua composável quando todos os providers estão no
registry.

## Validação final

| Gate                              |                                      Resultado |
| --------------------------------- | ---------------------------------------------: |
| Núcleo                            |                  112 testes, 0 falhas, 0 erros |
| Integração completa               |                  507 testes, 0 falhas, 0 erros |
| Website e CRM                     |                   89 testes, 0 falhas, 0 erros |
| Contrato do suite                 |                    4 testes, 0 falhas, 0 erros |
| Instalação/upgrade principal      |                                      concluído |
| Replay integral                   |                        concluído e idempotente |
| Inventário                        | 15/15 módulos instalados nas versões esperadas |
| Catálogo de Apps                  |   somente `marketing_center_suite` é aplicação |
| Disponibilidade privada e pública |                                       HTTP 200 |
| Produção                          |                                     não tocada |

Os `ERROR` de serialização, violação de unicidade e falha controlada que aparecem em
alguns logs são estímulos intencionais dos testes de concorrência/retry. O resultado
canônico do Odoo é zero falhas e zero erros em todas as quatro suítes.

## Segurança operacional do release

Os gates impediram três tentativas anteriores antes de uma mudança válida na base:

1. teste concorrente inicialmente incorreto no núcleo;
2. alteração concorrente do worktree detectada por divergência de hash;
3. fixture do núcleo acoplada semanticamente ao adapter Meta no teste integrado.

Em cada caso o script restaurou as fontes anteriores, reiniciou Odoo/DB manager,
restaurou a rota e confirmou HTTP 200. A execução final instalou a fachada, atualizou os
componentes, repetiu o mesmo conjunto como gate de idempotência e removeu o workspace
temporário de rollback.

Evidência final:
`scans/raw/20260901-odoo16-marketing-center-suite-consolidation/release/20260901T195842795779Z`.

Hash da árvore implantada:
`7ce6821803cfcfedb754f3f0655db135f317e26af833b5ef4aa338f35983db90`.
