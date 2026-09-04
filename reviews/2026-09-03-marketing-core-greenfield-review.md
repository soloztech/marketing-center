# Pente-fino greenfield do núcleo do Marketing Center — 2026-09-03

## Escopo e premissas

Esta revisão cobriu exclusivamente:

- `marketing_center_base`;
- `marketing_center_meta`;
- `marketing_center_meta_crm`;
- `marketing_center_google`;
- `marketing_center_dashboard`;
- `marketing_center_sale`;
- `marketing_center_account`;
- `marketing_center_sale_account`;
- `marketing_center_suite`.

Os bridges de CRM, Contact Center e Website, bem como `web_ingress`, ficaram fora do
escopo porque estavam sob responsabilidade de outros processos. Nenhum deploy, commit ou
alteração de banco foi realizado nesta rodada.

A régua usada foi greenfield para código interno: não manter caminhos runtime obsoletos
apenas por compatibilidade. Foram preservados, porém, os contratos externos do
Meta/Google e as migrations necessárias para convergir bancos que já receberam versões
do laboratório.

## Veredito executivo

A arquitetura dos nove addons é consistente e pode crescer sem uma consolidação
artificial em um único módulo. Os limites seguem dependências reais do Odoo:
provider-neutral no base; conectores Meta/Google; projeções opcionais para CRM, Venda e
Contabilidade; dashboard somente leitor; e suite somente como facade de instalação.

O pente-fino encontrou quatro problemas estruturais objetivos:

1. dois fluxos novos de fila ainda não exigiam a propriedade exata do UUID antes de
   cruzar sua fronteira de efeito;
2. a projeção Meta para CRM podia esgotar retries de concorrência sem persistir um
   estado terminal recuperável;
3. vinte regras dos bridges de Venda/Contabilidade estavam carregadas sob `noupdate=1`,
   congelando correções futuras de segurança em bancos instalados;
4. migrations antigas interpolavam identificadores SQL por string, apesar de os nomes
   serem internos e validados.

Todos foram corrigidos. Não foi encontrada violação atual de isolamento entre empresas,
ACL global, regra global, mutação do ledger por usuário ou dependência oculta do
dashboard em addons opcionais.

## Correções aplicadas

### 1. Propriedade exata dos jobs de Lead Ads

Em `marketing_center_meta/models/lead_ads.py`:

- o job de recuperação de uma submissão somente inicia quando o `job_uuid` do contexto é
  uma string não vazia e coincide exatamente com `submission.queue_job_uuid` sob
  `FOR UPDATE`;
- a reconciliação paginada aplica a mesma trava antes de qualquer chamada ao Graph API;
- a identidade da reconciliação agora inclui rota, revisões esperadas de rota, perfil e
  App, instante inicial e digest do cursor `after`;
- a adoção de job ativo usa somente a identidade canônica e o estado real no OCA
  `queue_job`, sem o atalho permissivo por ponteiro antigo;
- a captura de colisão na criação foi estreitada de qualquer `IntegrityError` para
  `UniqueViolation`. Se a identidade natural procurada não existir após o rollback do
  savepoint, a exceção original volta a subir; assim, uma violação de outra constraint
  nunca é mascarada como replay.

Foram adicionadas regressões que comprovam que um UUID substituído, ausente ou órfão não
instancia o adapter e não cruza a fronteira do provider.

### 2. Propriedade e terminalização da projeção Meta para CRM

Em `marketing_center_meta_crm/models/projection.py`:

- `_job_project_to_crm` reclama a linha sob `FOR UPDATE` e exige a coincidência exata do
  UUID persistido antes de criar/alterar um lead;
- a contagem de tentativa considera tanto o contador da projeção quanto o retry real do
  OCA;
- o teste de tentativa terminal valida UUID, estado `started`, modelo, método, limite e
  recordset do `queue.job` real;
- ao esgotar o retry por `OperationalError`, a projeção passa para `failed`, limpa o
  ponteiro do job e grava erro operacional sanitizado. A ação administrativa existente
  permite recuperção explícita;
- a busca de job ativo também passou a usar apenas a identidade canônica.

Foram adicionadas regressões para job órfão e para o último retry usando um objeto
`queue_job.job.Job` real.

### 3. Regras de acesso atualizáveis em upgrades

As regras de `marketing_center_sale`, `marketing_center_account` e
`marketing_center_sale_account` deixaram de ser dados `noupdate`.

Remover apenas o atributo dos XMLs seria insuficiente para bancos já instalados: os
respectivos XML IDs continuariam marcados no banco e seriam ignorados no primeiro
upgrade. Por isso, cada addon recebeu uma pre-migration no novo patch que marca somente
seus XML IDs conhecidos de `ir.rule` como atualizáveis antes da carga dos dados.

O teste da facade enumera as vinte regras exatas, exige a presença de todas e falha se
qualquer uma voltar a `noupdate`. Os quatro crons continuam intencionalmente em
`noupdate=1`, pois seus intervalos/ativação são configuração operacional, não política
de autorização.

### 4. SQL, i18n e higiene objetiva

- identificadores dinâmicos nas migrations do base, Venda, Contabilidade e
  Venda+Contabilidade agora usam `psycopg2.sql.Identifier`;
- uma constante de lookback não usada no Lead Ads foi removida;
- a documentação do Meta deixou de amarrar a instalação a versões intermediárias e
  documenta o contrato atual de App compartilhado e upgrade coordenado;
- interpolações traduzíveis no serviço de sync passaram a placeholders nomeados;
- um `except: pass` equivalente na resolução de DST foi tornado explícito sem alterar a
  semântica;
- avisos honestos de lint em teste e na imutabilidade do ledger Google foram corrigidos,
  sem desabilitação ampla.

## Invariantes verificadas

### Segurança e multiempresa

- 72 `ir.rule`, todas associadas explicitamente a grupos;
- 56 linhas de ACL, nenhuma global/sem grupo;
- campos privados de Lead Ads permanecem restritos a `base.group_system`;
- models transacionais mantêm `company_id`, `check_company` e constraints de escopo nos
  vínculos relevantes;
- dashboard, catálogos, métricas, revisões e runs continuam limitados por empresa e
  roster;
- nenhum payload bruto ou segredo foi adicionado a DTO de UI ou log.

### Ledgers, DTOs e idempotência

- touchpoints e business events preservam identidade canônica e revisões imutáveis;
- valores monetários continuam normalizados por `Decimal`/micros, sem `float` na
  fronteira persistente;
- upserts usam identidade natural, hash de conteúdo, constraints e locks;
- cursores e runs mantêm fences separados de source, binding, perfil, revisão, janela,
  sequência e job. Esses fences protegem autoridades diferentes e não devem ser
  colapsados num único hash;
- snapshots de CRM/Venda/Contabilidade sobrevivem corretamente à exclusão do registro
  operacional sem inventar uma referência viva.

### Fila e performance

- catálogo, performance e observabilidade Meta/Google usam páginas limitadas, cursor
  monotônico, revisões e identidade OCA;
- chamadas externas dos fluxos com ponteiro persistido estão atrás da propriedade exata
  do job;
- loops de cron usam lotes e isolamento por savepoint;
- consultas operacionais importantes possuem índices/constraints e locks explícitos;
- o dashboard depende apenas de `marketing_center_base` e consulta somente suas tabelas,
  portanto não cria dependência opcional escondida.

## Refutações e decisões preservadas

| Hipótese                                                             | Disposição                                                                                                                                                                                                                                                        |
| -------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| “Há addons demais; unificar reduziria complexidade”                  | **Refutada.** A separação acompanha dependências opcionais reais (`crm`, `sale`, `account`, Meta, Google). Unificar tornaria todos obrigatórios e pioraria upgrades e portabilidade.                                                                              |
| Alertas `R8180` do pylint para juntar classes com o mesmo `_inherit` | **Refutados.** São extensões independentes por capacidade (catálogo, performance, observabilidade e Lead Ads). O ORM as compõe; juntá-las por arquivo apagaria limites de domínio sem corrigir defeito.                                                           |
| Quebrar arquivos grandes somente pela contagem de linhas             | **Não aplicado.** Não houve evidência de ciclo, violação de dependência ou complexidade acima de 16 que justificasse um split amplo nesta rodada.                                                                                                                 |
| Substituir os ledgers por `utm.*` ou `link.tracker`                  | **Refutada.** Os modelos nativos representam classificação operacional e cliques do Odoo; os ledgers guardam evidência multicanal, revisões, fatos de provider e business events. A projeção para UTM continua sendo um consumidor gated, não a fonte de verdade. |
| Remover `legacy_adgroup_id_hint`                                     | **Refutada.** O nome é interno, mas o campo captura uma variante ainda recebida no protocolo externo Meta. Ele fica somente como diagnóstico administrativo e nunca vira identidade canônica de ad set.                                                           |
| Eliminar migrations por o projeto ser greenfield                     | **Refutada para o estado atual.** O laboratório já instalou versões anteriores. Removê-las impediria convergência reproduzível; caminhos runtime mortos podem ser removidos, migrations liberadas não.                                                            |
| Criar ponteiro de job em validação/discovery de perfis               | **Não necessário agora.** Não há cursor ou projeção de domínio por execução. A identidade OCA deduplica, revisões invalidam trabalho antigo e a saída de health é idempotente. Os fluxos que possuem estado de execução persistido agora têm ponteiro exato.      |
| Aplicar cascade/tombstone automático aos dados privados de Lead Ads  | **Não aplicado.** Sem retenção, legal hold e revogação definidos, a mudança poderia apagar evidência comercial ou manter hashes reidentificáveis e chamar isso incorretamente de anonimização.                                                                    |
| Fazer escrita nativa em `utm.*` nesta rodada                         | **Não aplicado.** O contrato native-first exige primeiro a atribuição confiável/fill-only e um único escritor. Criar mais um escritor concorrente seria regressão arquitetural.                                                                                   |

## Validações executadas

- Black: 174 arquivos Python conformes;
- isort: conforme;
- flake8 com complexidade máxima 16: zero ocorrências;
- pylint-odoo opcional e mandatory: somente três `R8180` deliberadamente refutados
  acima; nenhuma mensagem funcional/obrigatória;
- `compileall`: conforme;
- OCA module checks: conforme nos nove addons;
- XML: 39 arquivos parseados;
- CSV: 8 arquivos, cabeçalhos únicos e larguras consistentes;
- estrutura: manifests, caminhos, assets, migrations futuras e grafo de dependências
  conformes, sem ciclo;
- descoberta: 45 arquivos `test_*.py`, todos importados e sem imports obsoletos;
- inventário AST: 374 métodos de teste distintos;
- `git diff --check`: conforme.

Distribuição dos testes neste recorte:

| Addon                           | Arquivos | Métodos AST |
| ------------------------------- | -------: | ----------: |
| `marketing_center_base`         |       18 |         120 |
| `marketing_center_meta`         |       11 |         111 |
| `marketing_center_meta_crm`     |        2 |          14 |
| `marketing_center_google`       |        8 |          74 |
| `marketing_center_dashboard`    |        1 |          12 |
| `marketing_center_sale`         |        1 |          14 |
| `marketing_center_account`      |        2 |          15 |
| `marketing_center_sale_account` |        1 |           9 |
| `marketing_center_suite`        |        1 |           5 |

O ambiente WSL não possui o pacote/runner Odoo local. Portanto, os testes Odoo não foram
executados por este processo; os cinco novos testes foram revisados, compilados e
inventariados, mas precisam entrar na execução canônica integrada antes do release. Não
foi usado o servidor05 para contornar essa limitação, pois esta tarefa proibia deploy.

## Versões resultantes

| Addon                           | Versão       |
| ------------------------------- | ------------ |
| `marketing_center_base`         | `16.0.1.7.4` |
| `marketing_center_meta`         | `16.0.2.1.1` |
| `marketing_center_meta_crm`     | `16.0.1.2.1` |
| `marketing_center_google`       | `16.0.1.1.2` |
| `marketing_center_dashboard`    | `16.0.1.2.0` |
| `marketing_center_sale`         | `16.0.1.1.1` |
| `marketing_center_account`      | `16.0.1.1.1` |
| `marketing_center_sale_account` | `16.0.1.1.1` |
| `marketing_center_suite`        | `16.0.1.0.0` |

## Pendências conscientes antes de produção

1. executar upgrade limpo + replay de upgrade e a suíte Odoo integrada com os novos
   patches;
2. definir a política LGPD de retenção, revogação e legal hold para campos privados de
   Lead Ads;
3. validar fixtures reais sanitizadas dos providers, sobretudo paginação e erros de rate
   limit;
4. transformar o backfill histórico comercial acima do teto atual em planner
   paginado/retomável quando o volume justificar;
5. exportar/manter catálogo `pt_BR` antes da finalização de UX. As strings de servidor
   estão traduzíveis, mas estes addons ainda não versionam arquivos PO;
6. liberar escrita nativa em UTM somente depois do gate native-first e da definição do
   escritor único.

Esses itens não justificam remendos imediatos nem alterações destrutivas de schema. O
próximo gate objetivo é a validação Odoo integrada do conjunto já corrigido.
