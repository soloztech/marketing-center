# Contra-check e disposição — auditoria independente de 2026-09-01

- Data do contra-check: 2026-09-01
- Auditoria confrontada: `reviews/2026-09-01-independent-audit.md`
- Árvore confrontada: estado consolidado após as alterações paralelas do Marketing
  Center, não o fingerprint intermediário usado no início da auditoria
- Escopo de implantação: somente `odoo16-teste.soloz.com.br` / servidor05

## Veredito

A auditoria é útil, mas não pode ser aplicada como uma lista literal de correções. Ela
declara que analisou uma árvore em movimento e que 11 módulos novos não receberam todas
as lentes. O contra-check confirmou quatro classes de itens:

1. defeitos reais corrigidos neste ciclo;
2. achados que já estavam corrigidos na árvore consolidada;
3. riscos reais que dependem de decisão de processo ou produto;
4. recomendações cuja correção proposta enfraqueceria as garantias atuais.

Nenhum código produzido pela outra IA foi descartado. O release canônico empacotou e
verificou os 334 arquivos da árvore consolidada por hash antes de instalar.

## Correções aplicadas

| Achado                  | Disposição                                      | Implementação                                                                                                                                                                                                                                                                                                           |
| ----------------------- | ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| DATA-01                 | **Procede para novas ocorrências; corrigido**   | O bridge passou ao `MAPPING_VERSION = 2` e deriva `source_occurrence_ref` da chave canônica completa do Contact Center. Índice parcial garante um destino único para mapeamentos v2. Foi adicionado teste para o mesmo evento externo em conversas distintas. Dados históricos v1 não foram reescritos automaticamente. |
| C05 / DATA-02 / META-07 | **Procede; corrigido**                          | A fronteira diária agora procura o primeiro instante civil válido, resolve horários ambíguos de forma determinística e rejeita apenas datas totalmente inexistentes. Cobertura para Santiago, Havana e Beirut.                                                                                                          |
| C07 / TST-02            | **A premissa ficou obsoleta; lacuna corrigida** | Foram adicionados três testes Odoo com transações, cursores e threads independentes: colisão do escopo ativo, colisão da ocorrência e releitura do cursor após avanço concorrente.                                                                                                                                      |
| DATA-05                 | **Procede; corrigido**                          | Após `FOR UPDATE`, o serviço invalida todos os campos mutáveis do cursor antes de decidir ou cruzar a fronteira de I/O.                                                                                                                                                                                                 |
| OPS-11                  | **Procede; corrigido em Meta e Google**         | Um `cursor_changed` deixa de manter o run órfão: o código relê run/configuração/cursor sob locks, reaplica os fences e agenda exatamente a sequência autoritativa corrente, sem repetir I/O dentro do lock.                                                                                                             |
| META-15 / C30           | **Procede; corrigido e espelhado em Google**    | `write()` compara a configuração persistida real. Salvar valores idênticos não incrementa revisão, não apaga health e não pausa conexões. Validação continua sem reativar configuração manualmente pausada; discovery é o fluxo que reconcilia a disponibilidade.                                                       |
| SEC-03                  | **Procede; corrigido**                          | `credential_backend` e `access_token_ref` do perfil Meta ficaram restritos a `base.group_system`, com guarda no servidor em `create/write`, restrição na view e testes de ACL.                                                                                                                                          |
| OPS-09                  | **Procede; corrigido**                          | A busca de runs ganhou filtros de ativos/falhos/finalizados e agrupamentos. O run ganhou ação system-only para abrir o `queue.job`, sem criar dependência obrigatória do base em `queue_job`.                                                                                                                           |
| TST-05                  | **Procede; corrigido em Meta e Google**         | O teto agora é exercitado pelo objeto real `queue_job.job.Job`. No último retry, a falha transitória é persistida como health degradado em transação que confirma; um retry antigo não pode contaminar um perfil cuja credencial/revisão foi rotacionada. Foram adicionados sete testes reais do runner.                |

Versões resultantes:

- `marketing_center_meta`: `16.0.2.0.3` após o follow-up do cross-check;
- `marketing_center_google`: `16.0.1.1.2`.

## Achados já atendidos na árvore consolidada

Os itens abaixo descrevem problemas legítimos de versões intermediárias, mas não
permanecem como defeito na árvore instalada:

- C01: restart de cursor de catálogo, ação manual, cron e teste de predecessor falho;
- C02: cancelamento e watchdog para runs abandonados;
- C04: discovery com `active_test=False`, reativação, savepoint por conta e lock
  consultivo;
- META-13/C18: reconciliação de conta ausente, pausa manual e reaparecimento;
- C19: aplicação de entidade isolada por savepoint;
- C25: histórico A-B-A preservado;
- C27: identidade externa da source protegida contra mutação;
- C28: pin explícito de schema do Contact Center no mapper;
- C22: enriquecimento legítimo tratado como revisão de enriquecimento, não conflito;
- PERF-04, parcialmente: bridge em lote, retry de `OperationalError`, enqueue apenas
  quando o conteúdo muda e prioridade de backfill inferior ao tráfego vivo;
- PLAN-03: grupos Viewer/Analyst e ACLs existem; a descrição da auditoria foi feita
  antes da consolidação das regras;
- SEC-06: comparação de assinatura foi normalizada para não produzir 500 com texto não
  ASCII.

## Achados válidos mantidos como backlog consciente

### Processo e topologia

TOPO-01/02/04/12 procedem como risco de continuidade: os repositórios aninhados
continuam com árvores extensas, sem remote configurado e sem uma política única de
CI/pinning cross-repo. Isso não foi “corrigido” automaticamente porque escolher remotes,
autoria, divisão de commits e tags é uma decisão do proprietário. Um commit automático
da árvore mista também tornaria a revisão pior, não melhor.

TOPO-03 é apenas parcial. O release ainda não possui um Git SHA canônico, mas já
registra a lista exata de arquivos, SHA-256 individual, hash global da árvore,
verificação remota e evidência de instalação. Portanto, dois pacotes de mesma versão são
distinguíveis; o que falta é recuperação off-host e histórico de autoria.

### Dados e operação

- DATA-01 legado: replay v1 inequívoco permanece na cadeia histórica; cadeias colididas
  ou já divididas entre v1/v2 agora falham fechado. A migração desses casos continua
  exigindo diagnóstico e política explícita antes do backfill.
- C03: ainda vale capturar headers de uso/tempo estimado da Meta e definir uma política
  própria para rate limit longo. Não se deve substituir cegamente todo `Retry-After`
  pelo retry pattern: o header autoritativo precisa continuar tendo precedência quando
  válido.
- META-08: falta um planejador encadeado para histórico acima da janela operacional.
- META-09/C15: não existe produtor autoritativo de tombstone para exclusões externas.
- META-10/OPS-02: código, subcódigo e trace id ainda não possuem diagnóstico estruturado
  persistido.
- OPS-01/03/04: o watchdog pode cruzar o estado do job com mais detalhe; falta ação de
  retomada da página corrente e monitor periódico de validade de credenciais.
- OPS-05/06: a seleção limitada pode causar starvation apenas acima do volume atual;
  existem canais por provider, mas a capacidade deve ser declarada na topologia do
  jobrunner antes de escala real.
- OPS-08/12: documentação de rotação e limite de páginas configurável permanecem
  pendentes.
- TST-04/08/17: permanecem lacunas de teste de migração e de fixtures capturadas do
  provider real.
- manutenção: `lead_ads.py`, planners e alguns helpers ainda devem ser quebrados de
  forma incremental, junto das próximas alterações funcionais.

### LGPD

LGPD-01/02 e os complementos descrevem decisões de ciclo de vida verdadeiras, mas LGPD
foi explicitamente deixada fora do escopo inicial deste desenvolvimento. Não foi
aplicada a proposta de tombstone/cascade porque ela é incompleta sem política de
retenção, legal hold, revogação e definição do que constitui evidência fiscal ou
comercial.

Em particular, trocar todos os `ondelete=restrict` por `cascade` poderia apagar uma
projeção auditável sem registrar a revogação; substituir `values_json` mantendo hashes
determinísticos não garante anonimização. Esses itens ficam como decisão de produto
anterior à produção com dados reais, não como correção mecânica deste ciclo.

## Refutações e qualificações

| Achado/proposta                                      | Disposição                                                                                                                                                                                                                                                                                  |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| TOPO-10                                              | **Refutado para a árvore consolidada.** O Contact Center usado pelo release era instalável e já tinha evidência canônica completa; a afirmação descrevia um index intermediário inconsistente.                                                                                              |
| “Sem SHA, dois deploys iguais são indistinguíveis”   | **Parcialmente refutado.** Falta SHA Git, mas os manifests de hashes tornam o conteúdo exato identificável e verificável.                                                                                                                                                                   |
| PERF-01 como P1 imediato                             | **Rebaixado.** Há updates não-HOT possíveis, porém a medição do laboratório mostrou volume baixo, nenhuma linha morta relevante e tabelas pequenas. `last_sync_run_id` também é usado para ownership/freshness; removê-lo sem redesenhar esse contrato seria incorreto.                     |
| PERF-03: incluir `window_key` no índice de run ativo | **Correção proposta rejeitada.** Permitir janelas concorrentes sobre a mesma source/grain pode produzir fatos sobrepostos e resultados dependentes de ordem. A solução segura é um planner sequencial, limitado e retomável.                                                                |
| C26: remover `graph_version` da identidade           | **Refutado como correção mecânica.** A versão pertence ao contexto de observação reproduzível. O dashboard seleciona um contexto coerente; não soma indistintamente fatos de versões diferentes. Uma futura migração de API precisa de política de convivência, não de remoção da dimensão. |
| META-06                                              | **Revisado pelo cross-check; achado aceito e corrigido.** O contrato funcional Graph v26 passou a ser compartilhado por catálogo, Insights e Lead Ads, com falha explícita. O baseline genérico da infraestrutura permanece separado.                                                       |
| META-11                                              | **Não comprovado.** `ads_reader` usa leitura; `lead_reader` é separado. A remoção de permissões só deve ocorrer depois de validar o contrato efetivo do App Review e do endpoint live.                                                                                                      |
| META-12                                              | **Superestimado.** O `except` é limitado, sanitiza o erro, faz rollback e usa retry finito. Refinar exceções melhora manutenção, mas não há retry infinito nem vazamento comprovado.                                                                                                        |
| MNT-19: reduzir fences a dois                        | **Rejeitado.** App, perfil, conexão, source, run, cursor e job protegem autoridades/configurações diferentes. Colapsá-los num único hash reduz observabilidade e pode aceitar trabalho stale.                                                                                               |
| SEC-08/09                                            | **Sem exploração demonstrada.** São oportunidades de documentar invariantes, não vulnerabilidades confirmadas.                                                                                                                                                                              |
| “zero `@tagged`”                                     | **Obsoleto.** A suíte atual possui testes post-install reais, inclusive concorrência e runner OCA.                                                                                                                                                                                          |

## Evidência de validação e implantação

Release canônico:

`/home/lucaszotelli/infra-ai-ops/scans/raw/20260901-odoo16-marketing-center-audit-remediation/release/20260901T204613034659Z`

- status: `applied_and_validated`;
- árvore implantada: 334 arquivos verificados;
- SHA-256 da árvore: `ce7f0217ba67fa4429771ca247def3746d5594b34f3113f7000e07a6dcce5769`;
- base: 112 testes, 0 falhas, 0 erros;
- integração: 514 testes, 0 falhas, 0 erros;
- website: 89 testes, 0 falhas, 0 erros;
- facade/suite: 4 testes, 0 falhas, 0 erros;
- total: 719 testes;
- upgrade offline e replay idempotente concluídos;
- containers do Odoo e dbmanager em execução;
- acesso privado e público retornando HTTP 200;
- rota Traefik restaurada com o mesmo hash;
- produção não foi tocada.

### Follow-up do cross-check

O cross-check posterior corrigiu C28, META-06, a continuidade segura v1/v2, o teste de
DST ambíguo e a cobertura da ação OPS-09. O release canônico desse estado está em
`scans/raw/20260901-odoo16-marketing-center-audit-cross-check/release/ 20260902T003626527291Z`:
335 arquivos, hash `04dabe9d95f63b74586a10486b8a7ea0006e9fff5b3941a91edac924455365fe` e
726 testes verdes. A disposição detalhada está em
`reviews/2026-09-01-independent-audit-cross-check-disposition.md`.

## Próxima decisão recomendada

Antes da próxima expansão funcional, fechar uma política de versionamento/remotes para
os três repositórios. Em código, o próximo lote de maior retorno é:

1. observabilidade de erros Meta (`code/subcode/fbtrace_id`) e rate limit longo;
2. health periódico de credenciais e retomada de sync;
3. planejador sequencial de backfill;
4. testes de migração e fixtures reais sanitizadas.
