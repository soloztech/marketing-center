# N1, A6, SEC-05 e M5 — implementação e validação local

Data: 12/09/2026. Worktree: `marketing-review-fixes-20260912`, base Marketing `3c082ad`
acrescida do diff desta tarefa. Não houve execução do GitHub Actions, implantação ou
alteração de produção.

## Alterações

- CI fixa Contact Center em `5203d61f1715311e8339e1ce268e4a312a6bba49` e verifica que o
  checkout é o SHA solicitado e contém `9d7049915dd9941ece1e5167c6ca7153e2d5e5e3`, que
  introduziu a API do painel lateral.
- Os addons Website, Catalog e Catalog Contact Center ganharam `HttpCase`
  `post_install`, com tag `marketing_qunit`, nos modos minificado e `debug=assets`. Os
  filtros usam os nomes reais dos módulos QUnit e a conclusão exige ao menos um caso
  aprovado, sem `skip`/`todo`, em cada módulo obrigatório. O código espera a conclusão
  do guard mesmo quando o core já emitiu seu sinal de sucesso.
- A CI exige os seis marcadores de sucesso, conserva o exit code do runner com
  `pipefail` e publica o log contendo as contagens. Uma suíte omitida ou pulada por
  falta de navegador/dependência não deixa o job verde. `websocket-client` está
  declarado em `test-requirements.txt`.
- `meta.webhook.endpoint.webhook_url` e sua view agora exigem `base.group_system`, como
  a `routing_key` de que dependem. Nenhum `compute_sudo` foi introduzido.
- Testes com Marketing Admin **sem System** verificam metadados, leitura dos campos
  solicitados pelo formulário e rejeição de leituras explícitas da URL, routing key e
  referência do token. O papel System continua vendo a URL correta.
- Seis testes HTTP novos cobrem rejeições `404`, `411`, `413`, `415`, `409` e `503`, sem
  persistir delivery. O cenário `503` prova rollback após falha de consumer e ausência
  do valor privado da exceção no corpo e no log.

O runner OCA executa os testes dos addons selecionados com `--test-enable`; a inclusão
explícita dos HttpCase fornece a execução JS que faltava. Referências primárias:
[oca_run_tests](https://github.com/OCA/oca-ci/blob/master/bin/oca_run_tests),
[oca_init_test_database](https://github.com/OCA/oca-ci/blob/master/bin/oca_init_test_database).
O contrato `browser_js` e o comportamento de QUnit foram conferidos também no snapshot
OCB local utilizado pelos testes.

A instalação de `test-requirements.txt` foi conferida na fonte oficial do OCA CI:
[`oca_install_addons`](https://github.com/OCA/oca-ci/blob/master/bin/oca_install_addons)
chama o
[helper de dependências](https://github.com/OCA/oca-ci/blob/master/bin/oca_install_addons__deps_and_addons_path),
que acrescenta as dependências descobertas ao arquivo existente e executa
`pip install -r test-requirements.txt -c test-constraints.txt`. Assim, o pin
`websocket-client==1.8.0` participa da instalação normal, sem uma segunda instalação
redundante no workflow. Essa verificação é da fonte oficial; a imagem remota `latest` do
GitHub Actions não foi executada nesta tarefa.

## Dependências efetivamente testadas

O checkout canônico do Contact Center tinha alterações locais alheias à tarefa. A
homologação usou um `git archive` do SHA exato, materializado em:

`/home/lucaszotelli/infra-ai-ops/scans/raw/20260912-marketing-review-fixes/peer-5203d61`

O snapshot OCB e os pacotes existentes foram lidos de diretórios anteriores, sem
instalar pacotes neles. `websocket-client==1.8.0` foi instalado somente no overlay
`packages-ci` da tarefa. PostgreSQL, banco `mc_review_20260912_ci`, socket, filestore e
HTTP `127.0.0.1:18185` foram locais e exclusivos desta validação. Cron, queue runner e
envio externo de e-mail estavam desativados pelo harness.

## Resultados

| Suíte QUnit                                          | Casos minificados | Casos debug assets |
| ---------------------------------------------------- | ----------------: | -----------------: |
| Website: landing capture + technical actions         |                14 |                 14 |
| Catalog: browser + registration views                |                11 |                 11 |
| Catalog Contact Center: composer bridge + side panel |                13 |                 13 |
| **Total**                                            |            **38** |             **38** |

Foram **76 execuções de casos JavaScript**, com 382 assertions. A contagem foi obtida
dos casos concluídos; os logs nativos do Odoo chamam assertions de "tests", por isso não
foram usados para inflar o número de casos.

- Instalação/reinstalação selecionada: **21/21 testes Python aprovados** (6 HttpCase JS,
  13 HTTP webhook e 2 de acesso).
- Upgrade `-u` do mesmo conjunto: **21/21 aprovados**, com os seis marcadores JS.
- Suíte Python existente do catálogo e ponte, excluindo `marketing_qunit`: **35/35
  aprovados**, incluindo autorização, empresa, preview/download e preparação de conteúdo
  para a conversa.
- Black 22.8.0, flake8, Python AST/compilação, YAML/XML e `git diff --check` passaram
  nos arquivos desta frente.

Provas negativas foram executadas e removidas em `finally`, restaurando os arquivos
originais byte a byte:

1. Adicionar um módulo obrigatório inexistente manteve os testes reais aprovados pelo
   core, mas o guard rejeitou sua contagem zero e Odoo encerrou com **exit 1**.
2. Inserir uma assertion falsa na suíte real do catálogo produziu **exit 1**.
3. Executar o trecho Python exato do workflow contra o primeiro log, no qual os seis
   HttpCase tinham sido pulados por falta de `websocket-client`, produziu **exit 1**;
   contra o log final completo, produziu **exit 0**.

Após a remoção das falhas deliberadas, a execução completa e o upgrade voltaram a ficar
verdes. Todos os Chromium iniciados por HttpCase foram encerrados por seu teardown; o
cluster compartilhado entre as frentes da tarefa não foi parado.

## Reprodução e evidências

Com o cluster da tarefa inicializado:

```bash
python3 tools/local_review_tests.py test \
  --runtime /home/lucaszotelli/infra-ai-ops/scans/raw/20260905-centers-greenfield-audit/runtime \
  --packages /home/lucaszotelli/infra-ai-ops/scans/raw/20260912-marketing-review-fixes/packages-ci \
  --peer /home/lucaszotelli/infra-ai-ops/scans/raw/20260912-marketing-review-fixes/peer-5203d61 \
  --state /home/lucaszotelli/infra-ai-ops/scans/raw/20260912-marketing-review-fixes \
  --suite ci --http-port 18185 \
  --modules marketing_center_catalog,marketing_center_catalog_contact_center,marketing_center_catalog_sale,marketing_center_website,marketing_center_website_crm,marketing_center_meta,meta_webhook_base \
  --tags marketing_qunit,/marketing_center_meta:TestMarketingWebhookAccess,/meta_webhook_base:TestMetaWebhookController
```

Trocar `test` por `upgrade` executa a validação de atualização. Para a suíte de servidor
do catálogo, usar
`--modules marketing_center_catalog,marketing_center_catalog_contact_center` e
`--tags /marketing_center_catalog,/marketing_center_catalog_contact_center,-marketing_qunit`.
Em bancos já instalados, incluir os addons com testes explicitamente em `--modules`:
selecionar somente um addon dependente não reexecuta todos os testes de dependências
anteriores.

Evidências locais em
`/home/lucaszotelli/infra-ai-ops/scans/raw/20260912-marketing-review-fixes/`:

- `ci-qunit-install-final.log`: execução final após remover as provas negativas.
- `ci-qunit-upgrade-final.log`: upgrade completo e repetição das seis suítes.
- `ci-catalog-server-tests.log`: 35 testes existentes do catálogo/ponte.
- `ci-negative-zero.log` e `ci-negative-assertion.log`: rejeições deliberadas.
- `ci-first-diagnostic.log`: prova de detecção de suites puladas.
- `qunit_negative_proofs.py`: procedimento local das duas injeções temporárias.

Esta evidência cobre o diff local e o peer exato. A execução remota da CI no commit que
consolidar a tarefa continua sendo uma etapa de publicação. Os testes de `409` e `503`
usam falhas injetadas no servidor, e o teste de `413` reduz o limite em memória para
verificar a fronteira HTTP; não simulam a infraestrutura da Meta nem indisponibilidade
real de produção. A frente de sincronização e busca de leads históricos permaneceu fora
dos arquivos modificados aqui.

## M5 — orçamento de falhas de banco e recuperação

A revisão final identificou uma distinção concreta do OCA local: `Job.perform` aplica
`max_retries` ao capturar `RetryableJobError`. A conversão automática de
`OperationalError` no controller ocorre depois desse ponto. Por isso, apenas definir
`max_retries=8` não limita a serialização que escapa crua do método.

- Fanout e processamento de consumer Meta agora classificam SQLSTATEs e exceções tipadas
  de banco dentro de um decorator local, sem dependência do Contact Center. Tanto locks
  iniciais quanto o processamento são cobertos.
- Novos jobs usam oito tentativas. Falhas permanentes/não classificadas falham
  diretamente. Uma falha de banco não grava `dead` como se fosse falha do consumer; a
  transação é revertida e o esgotamento aparece no `queue.job.failed`. O teto aplicativo
  anterior de oito tentativas e sua classificação permanecem.
- O cron de órfãos e o enqueue não criam orçamento novo quando o ponteiro atual
  referencia um job `failed`. Jobs ausentes e cancelados continuam recuperáveis;
  `action_requeue` explícito limpa o ponteiro e permite outra tentativa operacional.
- O manifesto `meta_webhook_base` foi atualizado para `16.0.1.0.2`.
- Os testes ORM compartilhados Meta são `post_install`: criar empresas durante um
  upgrade parcial, antes de `account` entrar no registry, falhava nos defaults
  obrigatórios de `res.company` apesar da coluna já existir no banco instalado.

Validações adicionais concluídas:

- **10/10 testes** de bootstrap CC/CRM: `Job.perform` sobre cinco entrypoints, sétima
  tentativa recuperável, oitava terminal e erro permanente na primeira.
- **95/95 testes** do addon `meta_webhook_base`, incluindo classificação, orçamento real
  do Job, preservação do estado aplicativo, órfãos, requeue, subscriptions e rejeições
  HTTP.
- **97/97 testes no upgrade conjunto** Meta Webhook + Marketing Meta em banco com
  catálogo, Website e Account instalados: 95 do webhook mais 2 de SEC-05.

Logs: `ci_retry-test.log`, `ci_webhook-test.log` e `ci-upgrade.log` no mesmo diretório
de evidências. A prova de limite de Job usa injeção de exceções nos métodos de
banco/consumer e `Job.perform` real; a recuperação usa jobs persistidos e as consultas
SQL reais do cron. O cron de recuperação não foi executado por um runner externo. Jobs
antigos já persistidos com `max_retries=0` não são migrados silenciosamente; seguem o
inventário e a atualização operacional explícita descritos no runbook.
