# Arquitetura do Marketing Center

## Um produto, não vários sistemas

O Marketing Center é um único produto executado no mesmo Odoo, no mesmo banco PostgreSQL
e no mesmo `queue_job`. Seus addons são limites internos de dependência: eles permitem
instalar apenas os provedores e os aplicativos Odoo necessários sem acoplar Google,
Meta, Website, CRM, Vendas, Contabilidade e Contact Center ao núcleo.

Um addon não representa um microserviço, uma base separada ou outra interface para o
usuário. Os registros se relacionam no mesmo ORM e os trabalhos assíncronos usam o mesmo
JobRunner. A divisão existe para preservar responsabilidades e permitir que cada
integração seja portada ou substituída sem reescrever o domínio central.

## Componentes funcionais

Existem 14 componentes funcionais. O `marketing_center_suite` descrito adiante é apenas
um perfil de instalação; ele não acrescenta um 15º domínio.

| Camada       | Addon                                 | Responsabilidade e motivo da separação                                                                                                                                                                              |
| ------------ | ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Núcleo       | `marketing_center_base`               | Define contratos neutros, DTOs, fontes, conexões, catálogo externo, métricas, touchpoints, atribuição e eventos de negócio. Não depende de provedores nem de CRM, Website, Vendas, Contabilidade ou Contact Center. |
| Interface    | `marketing_center_dashboard`          | Projeta os ledgers em uma visão gerencial somente leitura. Fica separado para que o núcleo possa operar sem uma interface analítica específica.                                                                     |
| Provedor     | `marketing_center_google`             | Lê catálogo, desempenho, histórico de alterações e diagnósticos do Google Ads e os traduz para os contratos do núcleo. Credenciais e transporte pertencem ao `google_api_base`.                                     |
| Provedor     | `marketing_center_meta`               | Lê catálogo/desempenho da Meta e trata Lead Ads sobre o webhook compartilhado. Traduz objetos Meta para DTOs canônicos; transporte e recepção técnica ficam em `meta_api_base` e `meta_webhook_base`.               |
| Ingresso     | `marketing_center_web_ingress`        | Recebe evidência first-party de qualquer site por um contrato neutro. Não conhece Odoo Website nem CRM, podendo atender futuramente site externo ou aplicação headless.                                             |
| Adapter      | `marketing_center_website`            | Captura sessões e ações no Website nativo do Odoo e usa o Web Ingress. Mantém a extensão de `website` fora do núcleo.                                                                                               |
| Domínio Odoo | `marketing_center_crm`                | Liga evidência de marketing a `crm.lead` e registra o ciclo comercial sem substituir o CRM como fonte canônica do lead.                                                                                             |
| Domínio Odoo | `marketing_center_sale`               | Projeta estados e valores de `sale.order` no ledger de eventos. Só é instalado quando Vendas participa da jornada.                                                                                                  |
| Domínio Odoo | `marketing_center_account`            | Projeta faturamento e recebimentos realizados no ledger, mantendo `account.move` e pagamentos como fontes canônicas.                                                                                                |
| Domínio Odoo | `marketing_center_contact_center`     | Converte aquisição e episódios de atendimento do Contact Center em evidência/eventos de marketing, sem mover a mensageria para este projeto.                                                                        |
| Cola         | `marketing_center_website_crm`        | Correlaciona, de forma idempotente, o sucesso de um formulário nativo com o lead criado. Existe porque Website e CRM continuam opcionais e nenhum deve depender do outro.                                           |
| Cola         | `marketing_center_meta_crm`           | Projeta uma submissão autenticada de Meta Lead Ads no CRM quando essa política estiver habilitada.                                                                                                                  |
| Cola         | `marketing_center_contact_center_crm` | Converge casos/leads do Contact Center com a atribuição de marketing quando os dois domínios estão instalados.                                                                                                      |
| Cola         | `marketing_center_sale_account`       | Preserva a ligação causal tipada entre pedido e fatura/recebimento sem fazer Vendas depender da Contabilidade, ou o inverso.                                                                                        |

Os addons compartilhados `google_api_base`, `meta_api_base` e `meta_webhook_base` são
fundações técnicas, não componentes funcionais do Marketing Center. Eles ficam
fisicamente neste repositório para simplificar versionamento e release, mas continuam
oferecendo credenciais, transporte e ingresso autenticado reutilizáveis por outros
produtos, inclusive o Contact Center. Nenhum deles depende de `marketing_center_base`.

## Fluxo ponta a ponta

```text
Google / Meta / Website / webhook autenticado
                    |
                    v
          adapter específico do canal
                    |
                    v
        DTO canônico + deduplicação técnica
                    |
                    v
   ledgers do Marketing Center (evidência imutável)
             |                     |
             v                     v
        CRM / lead            Contact Center
             |                     |
             +----------+----------+
                        v
               venda -> fatura -> recebimento
                        |
                        v
          eventos, atribuição e métricas diárias
                        |
                        v
                dashboard gerencial
```

1. O provider ou o Website recebe/lê o evento original.
2. O adapter valida o contrato do canal e produz um DTO padronizado. Detalhes do
   provider não vazam para CRM, Vendas ou Dashboard.
3. O núcleo persiste evidência deduplicada. Ledgers históricos são imutáveis; correções
   entram como nova evidência ou projeção, sem reescrever o fato original.
4. Bridges associam a evidência a conversas, pessoas, empresas ou leads. Os modelos
   nativos continuam canônicos em seus domínios.
5. Alterações relevantes do lead, pedido, fatura e recebimento geram eventos de negócio
   tipados e relacionados à jornada.
6. O motor de atribuição e as métricas produzem leituras gerenciais reproduzíveis.

Nem todo provider precisa implementar todas as etapas de uma vez. O contrato comum
permite evoluir catálogo, desempenho, conversões e atribuição sem criar caminhos
paralelos para cada plataforma.

## Perfis de instalação

Os perfis abaixo expressam capacidades, não bancos ou serviços diferentes.

| Perfil           | Componentes principais                                                                                                      | Uso                                                                                                         |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Núcleo analítico | `marketing_center_base`, `marketing_center_dashboard`                                                                       | Modelo canônico e leitura gerencial, sem integrações externas.                                              |
| Mídia paga       | Núcleo + `marketing_center_google` e/ou `marketing_center_meta`                                                             | Catálogo, métricas e observabilidade das plataformas escolhidas.                                            |
| Website e CRM    | Núcleo + `marketing_center_web_ingress`, `marketing_center_website`, `marketing_center_crm`, `marketing_center_website_crm` | Jornada first-party do acesso ao lead nativo.                                                               |
| Receita          | Perfil com CRM + `marketing_center_sale`, `marketing_center_account`, `marketing_center_sale_account`                       | Acompanha lead, venda, faturamento e recebimento.                                                           |
| Atendimento      | Núcleo + `marketing_center_contact_center` e, com CRM, `marketing_center_contact_center_crm`                                | Conecta aquisição e atendimento à jornada comercial.                                                        |
| Completo Soloz   | `marketing_center_suite`                                                                                                    | Instala e atualiza, por uma única seleção, todos os 14 componentes adotados pela Soloz e suas dependências. |

O `marketing_center_suite` deve ser um metapacote: sem modelos, tabelas, regras de
negócio ou menus próprios. Seu manifest apenas declara o conjunto completo. Assim, a
experiência é “instalar Marketing Center”, enquanto os limites técnicos continuam
preservados. Perfis menores permanecem disponíveis para implantação, testes e futura
portabilidade.

Não se deve usar `auto_install` em integrações que criem projeções ou efeitos de
negócio. A instalação explícita ou pelo `suite` evita ativar comportamento apenas porque
dois aplicativos passaram a coexistir no banco.

## Contrato de acesso por fonte

O grupo do Odoo e o roster têm responsabilidades independentes. O grupo global
(`Viewer`, `Analyst`, `Operator`, `Manager`) define o teto funcional do usuário; o
`role` da associação ao time define o teto dentro daquele time; e o `access_mode` do
vínculo time-fonte define o teto naquela fonte. A capacidade efetiva é sempre a menor
das três. Administradores do Marketing Center dispensam roster, mas continuam limitados
às empresas ativas.

Na entrega read-only atual, `read` já é materializado em
`marketing.center.source.access_user_ids` e consumido pelas record rules. Fonte, time,
membership, vínculo ou usuário inativo revogam essa projeção. Capacidades superiores não
autorizam mutação por si sós: qualquer ação futura de escrita deverá chamar o mesmo
verificador central e também satisfazer ACL, policy, aprovação e capability do provider.

Uma rota operacional de Meta Lead Ads pertence obrigatoriamente a uma fonte `meta.ads`.
Rotas e submissões usam o roster dessa fonte; somente o administrador tem visão completa
da empresa. Uma rota histórica sem fonte é pausada na migração e não recebe uma
identidade publicitária inventada: deve ser vinculada explicitamente antes de voltar a
operar.

## Quando criar um novo addon

Um novo addon é justificável quando pelo menos um destes critérios for atendido:

1. introduz dependência opcional de outro aplicativo Odoo, SDK ou provider;
2. implementa um adapter substituível para um contrato já definido pelo núcleo;
3. conecta dois domínios opcionais que não podem depender um do outro;
4. precisa ter ciclo próprio de instalação, segurança, filas, migração ou testes;
5. será reutilizado por outro produto e, portanto, deve permanecer numa fundação técnica
   independente, mesmo quando co-localizada neste repositório.

Não criar novo addon somente para:

- adicionar um modelo, menu, relatório ou job à mesma responsabilidade;
- separar arquivos grandes — isso deve ser resolvido com serviços/pacotes internos;
- criar uma tela alternativa sobre o mesmo domínio;
- contornar um contrato inadequado sem primeiro corrigir o contrato no núcleo.

Convenções:

- provider: `marketing_center_<provider>`;
- integração com aplicativo Odoo: `marketing_center_<aplicativo>`;
- cola entre capacidades opcionais: `marketing_center_<a>_<b>`;
- transporte compartilhado: `<provider>_api_base` na camada de fundações técnicas;
- UI gerencial comum: evoluir `marketing_center_dashboard` antes de criar outra UI.

## O que manter e o que consolidar

| Decisão         | Escopo                                                   | Motivo                                                                               |
| --------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Manter separado | `marketing_center_base` dos providers                    | O núcleo não deve conhecer APIs externas.                                            |
| Manter separado | Google e Meta                                            | Credenciais, limites, objetos e ritmos de evolução são diferentes.                   |
| Manter separado | `web_ingress` e `website`                                | O ingresso neutro poderá servir sites que não usam Odoo.                             |
| Manter separado | CRM, Vendas e Contabilidade                              | São aplicativos opcionais e fontes canônicas distintas.                              |
| Manter separado | addons de cola                                           | Evitam dependências reversas e ciclos entre domínios opcionais.                      |
| Manter separado | API/webhook base como addons técnicos independentes      | Meta webhook também atende o Contact Center; co-localização não transfere o domínio. |
| Consolidar      | Instalação no `marketing_center_suite`                   | Um ponto de instalação sem perder modularidade interna.                              |
| Consolidar      | Navegação sob o menu Marketing Center                    | O usuário não deve precisar conhecer a divisão em addons.                            |
| Consolidar      | Padrões de DTO, ledger, fila e observabilidade no núcleo | Providers devem compartilhar contratos, não copiar implementações.                   |

Não há benefício atual em fundir os addons entre si. A consolidação é apenas do
repositório físico: os 14 componentes funcionais e as três fundações técnicas mantêm
manifests e ciclos de instalação próprios. Se o Dashboard se tornar obrigatório em todas
as instalações, `base` e `dashboard` poderão ser reavaliados; até lá, mantê-los
separados permite uso headless e testes do domínio sem carregar a UI.

## Menus funcionais e técnicos

A navegação deve comunicar um único produto:

- **Visão gerencial, Funil e Performance:** uso cotidiano de gestores e analistas;
- **Catálogo:** entidades externas e estado de sincronização relevante ao analista;
- **Configuração:** fontes, conexões, equipes, perfis e rotas, apenas para
  administradores;
- **Técnico:** entregas de webhook, cursores, runs, intents, projeções, erros e ações de
  recuperação, restrito ao administrador do sistema.

`Meta Webhooks` não deve parecer uma aplicação de negócio independente. O ledger técnico
continua único em `meta_webhook_base`, mas seus atalhos devem aparecer em uma área
técnica do produto consumidor. Marketing Center e Contact Center podem apontar para os
mesmos registros sem duplicá-los.

Menus de addons não devem criar novas raízes salvo quando representarem outro produto.
Também devem usar sequências únicas dentro de cada grupo para que sua ordem não varie
conforme a combinação de módulos instalada.
