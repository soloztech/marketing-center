# Current integration release

The compatibility addons described in this earlier core-fusion record have now been
retired. Follow [CRM integration fusion](crm-integration-fusion.md) for the mandatory
two-step migration, current ownership and supported lineage. Historical implementation
details below describe the preceding release.

# Núcleo unificado do Marketing Center

A partir de 16.0.2.0.0, `marketing_center_base` reúne evidências de aquisição, vínculos
e eventos do CRM, processamento UTM e visão gerencial. Depende de `crm` nativo e
`queue_job`. Instalações novas selecionam apenas esse núcleo e as integrações
necessárias.

`marketing_center_crm` e `marketing_center_dashboard` permanecem como pacotes de
compatibilidade para bancos existentes. Não contêm a implementação funcional. Seus XML
IDs e imports Python continuam resolvendo os mesmos registros e objetos. Não desinstale
esses pacotes para concluir a fusão.

## Atualização de um banco existente

1. Conferir providers únicos, versões instaladas e fonte oficial `16.0`.
2. Registrar a decisão de backup e o estado anterior dos registros e serviços.
3. Atualizar `marketing_center_base` junto com os pacotes antigos instalados e os
   consumidores alterados: Automation, Meta CRM, Website CRM, Contact Center CRM e
   Sales. Incluir outros módulos cujo código publicado exija atualização.
4. Conferir versões persistidas, aliases, IDs, regras, vínculos, filas e saúde.

A migração mantém nomes de modelos, tabelas e campos. Transfere a propriedade dos
metadados ao núcleo e conserva aliases antigos. Regras canônicas continuam atualizáveis.
Metadados antigos desconhecidos, conflitos de XML ID e versões superiores à linhagem
suportada bloqueiam a migração para exigir reconciliação.

Capture CRM e captura geral têm políticas independentes. A fusão preserva os valores
existentes e os marcos históricos; não altera a decisão de criar leads nem associa
automaticamente conversas ao CRM. No núcleo novo, a captura CRM mantém o padrão
habilitado que já existia no módulo CRM.

## Reversão

Antes da migração, é possível retornar ao commit anterior. Depois de uma migração
persistida, retornar apenas o código não restaura a propriedade dos metadados. A
recuperação completa exige banco e filestore compatíveis ou uma migração inversa
previamente validada. A dispensa expressa de backup significa que esta entrega não cria
uma nova cópia para recuperação.

O catálogo de conteúdo segue independente. Sua extração para Content Center é a próxima
entrega, com migração própria.
