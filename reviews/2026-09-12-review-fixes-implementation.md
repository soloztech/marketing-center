# Implementação do contra-check — 12/09/2026

Estado: correções prioritárias implementadas e validadas localmente. Implantação não realizada. Autorização: implementar as correções do contra-check, preservando a frente paralela de sincronização/consulta histórica de leads.

## Estado inicial e isolamento

- Worktree desta tarefa: `/home/lucaszotelli/worktrees/marketing-review-fixes-20260912`.
- Branch: `codex/marketing-review-fixes-20260912`; base `3c082ad` (catálogo com painel lateral e formatação já consolidados).
- Checkout canônico permanece em `0a62448`, com mudanças locais anteriores, preservadas.
- Peer Contact Center observado: `5203d61f1715311e8339e1ce268e4a312a6bba49`.
- Trabalho paralelo identificado: `feat/meta-forms-history-discovery-20260912`, observado em `b3a892c`, no worktree `marketing-meta-forms-20260912`. Seus arquivos de descoberta, histórico, rotas, views e testes não pertencem a esta tarefa.
- Ambiente: alterações locais de código e testes sintéticos. Sem implantação, alteração de dados de produção ou documentos fiscais autorizados. Backup de produção não se aplica; commits e diff delimitado permitem reversão do código local.

## Plano de execução

1. Corrigir anulação/repostagem de faturas e créditos com histórico imutável e testes integrados.
2. Fixar peer compatível e executar QUnit pela CI com proteção contra zero testes.
3. Corrigir contagem de episódios e explicitar semântica de transições, moeda e janela do dashboard.
4. Limitar retries das pontes, classificar falhas transitórias e documentar capacidade do runner, alertas e rotação.
5. Implementar política executável de captura/retenção web e a restrição de acesso do campo webhook URL, preservando a criação nativa de leads.
6. Implementar melhorias de observabilidade e saúde Meta em arquivos separados da sincronização/histórico, integrando somente após conferir compatibilidade com a outra branch.
7. Documentar as capacidades presentes/futuras, validar instalação/upgrade e cenários de falha nas dependências exatas; registrar resultados e limitações.

O motor de atribuição permanece fundação sem produtor operacional: não gerar crédito a partir de mera correlação. A aprovação editorial descartada não volta ao catálogo. Mudanças operacionais em produção e desenho de novos produtos/jornadas seguem entregas próprias, conforme o contra-check.

## Resultados

### Correções implementadas

| Achado | Resultado implementado | Evidência principal |
|---|---|---|
| A1 | Anulações explícitas e imutáveis de postagem de faturas/créditos; cálculo de créditos vigentes; repostagem idempotente; operação fiscal nativa preservada. | `marketing_center_account/docs/review-a1-20260912.md` |
| A6 / N1 | Peer Contact Center `5203d61` fixado; QUnit real nos três addons, assets e minificado; ausência de módulo/testes reprova a CI. | `reviews/2026-09-12-qunit-webhook-validation.md` |
| A4 | Captura web desabilitada por padrão e condicionada à política administrativa explícita; prazo por evento/intent, expurgo em lotes, tombstones e aplicação revisável de política ao legado. A cópia UUID/hash da sessão na ponte Website CRM também é expurgada, incluindo intents pendentes sem evento. | READMEs Web Ingress/Website; regressões de ingresso, expurgo e formulário nativo. |
| M1–M4 | Rótulos de transições e ocorrências financeiras; timezone UTC explícito; bases monetárias separadas; um fato `response_episode_answered` por episódio independentemente da ordem dos jobs. | Testes Dashboard/Lifecycle; migração paginada dos episódios retidos. |
| M5 | Oito tentativas nas pontes CC/CRM, classificação SQLSTATE dentro do job, falha não transitória visível e fim do retry ao esgotar orçamento. Webhook Meta também limita fanout/consumer e impede recriação automática de um job failed. | Testes reais de `Job.perform`, incluindo bootstrap e projeção de episódios. |
| M9 / M10 | Diagnósticos Meta tipados/sanitizados; cooldown; cron Ads reader limitado, escalonado, deduplicado e cercado por revisões; alerta local de expiração. | `marketing_center_meta/docs/review-m9-m10-20260912.md` |
| SEC-05 / M15 | Webhook URL protegido por campo/grupo; formulário real de Marketing Admin sem grupo System; HTTP 404/411/413/415/409/503 cobertos. | Validação CI/webhook. |
| M13 / M14 | Documentação vigente separada dos registros históricos; semântica do dashboard e tradução pt_BR. | README, este registro e catálogo de tradução. |
| A3 / M11 | Runbook de capacidade, proteção no Traefik, alertas e recuperação, com medições necessárias e reversão definida. | `docs/queue-and-credential-operations.md`; configuração de produção não foi alterada. |

### Testes locais já concluídos

Os bancos são descartáveis, no PostgreSQL exclusivo desta tarefa, escutando apenas em socket local privado. Cron, runner e envio de e-mail foram desabilitados; APIs de provedores usam fixtures/mocks. O peer é um arquivo imutável do commit exato, sem ler alterações não commitadas do Contact Center.

| Frente | Resultado |
|---|---|
| Financeiro + Base, instalação limpa | 161 testes, zero falhas/erros. |
| Financeiro, upgrade e cenários adicionais | 56 testes inicialmente; 58 testes finais Account/Sale/SaleAccount com causalidade histórica e convergência simétrica. Zero falhas/erros. |
| Métricas/CC/CRM | 77 testes na instalação e 80 no upgrade; tradução pt_BR e cache de idioma: 14 testes adicionais do Dashboard. Zero falhas/erros nas rodadas finais. |
| Privacidade Web/Website/CRM, upgrade | 135 testes na primeira entrega; 42 testes da ponte após inclusão da retenção de sessões/intents e filtro de landing vencido antes do cron. Zero falhas/erros. |
| QUnit + HTTP + ACL | 21 testes Python na instalação e 21 no upgrade, ambos verdes. Cada rodada executou 76 casos JavaScript / 382 assertions; catálogo servidor: 35 testes verdes. |
| Retry CC/CRM e webhook | 10 testes de bootstrap/limite; 95 testes webhook na instalação; 97 no upgrade conjunto com SEC-05. Todos verdes. |
| Provas negativas do QUnit | Módulo inexistente e assertion falsa causaram exit 1; arquivos restaurados e rerun positivo concluído. |
| API/saúde Meta, instalação e upgrade | 179 e 157 testes respectivamente, zero falhas/erros. |

Essas contagens se sobrepõem; não são uma soma de testes únicos. Logs em `/home/lucaszotelli/infra-ai-ops/scans/raw/20260912-marketing-review-fixes/`. Arquivos de log cumulativos também conservam rodadas de desenvolvimento com falhas; considerar o resumo final e o processo indicado no relatório de cada frente.

Uma execução intermediária de upgrade do Dashboard encontrou fixture de usuário executada durante um registry parcial, antes de extensões do Contact Center. A suíte foi marcada `post_install`, pois exercita integrações opcionais e deve rodar com o registry completo. Não foi removida nenhuma assertiva.

### Aplicação e limites

- Antes de habilitar captura em uma implantação, o responsável configura propósito, fundamento documentado, versões de política/aviso e prazo. A aplicação não oferece um prazo jurídico presumido nem aceita um `granted` declarado pelo browser. A modalidade baseada em consentimento depende de produtor confiável de decisão individual/CMP.
- Intents legados sem prazo não recebem novo vínculo de sessão até aplicação administrativa de política. A prévia usa a data original; a ação permite manter captura desativada. O expurgo preserva o lead nativo e fatos CRM já existentes; UUID/hash de sessão não voltam por retry.
- O upgrade agenda a projeção dos episódios antigos em páginas; o KPI converge quando esses jobs terminam. Evidência já excluída não é reconstruída por inferência. Fatos e links originais permanecem preservados.
- Anulações financeiras históricas não são inventadas por migração. A instalação passa a observar transições posteriores; qualquer saneamento histórico exige levantamento e política próprios.
- Jobs já persistidos com `max_retries=0` precisam inventário/reprocessamento deliberado. Código novo não modifica automaticamente essas linhas.
- Capacidade de workers, alertas externos e rate limit/IP confiável no Traefik continuam dependentes de validação operacional. Este trabalho não prova que a borda ativa está configurada corretamente.
- O motor de atribuição permanece sem produtor de crédito operacional; correlação com CRM não concede crédito a campanhas. O catálogo mantém cadastro direto e não ganha aprovação editorial.
- A5/M12 (promoção dos markers Soloz, código de correlação no WhatsApp, idempotência do POST nativo), M6–M8 (contratos entre addons, refatorações e desempenho medido/backfill CRM) e cobertura concorrente Google adicional permanecem evolução separada conforme a disposição do contra-check. Não foram declarados concluídos por estes patches.
- A retenção de CRM nativo, Lead Ads, arquivos de backup e demais provedores continua separada. Em especial, não alterar retenção ou captura da nova sincronização/histórico de leads sem reconciliar seu contrato na frente correspondente.

### Compatibilidade e fechamento

Combinação isolada com `feat/meta-forms-history-discovery-20260912` em `b3a892c`: **215 testes verdes**, incluindo descoberta, histórico, formulários, saúde e Graph. Único conflito textual foi a versão do manifesto; no snapshot combinado ficou `16.0.1.1.1`. Imports e recursos dos dois lados foram preservados, com `credential_health` após `meta_profile`. Não houve merge no worktree do outro chat. Ver `reviews/2026-09-12-leads-compatibility.md` para hashes, comandos e limites.

Validação do conjunto: **875 testes, zero falhas/erros**, em instalação limpa dos 21 addons selecionados (`marketing_center_suite` e pontes do catálogo), banco `mc_review_20260912_full_final`, resultado em `2026-09-12 04:09:20 UTC`, log `full_final-test.log`. QUnit foi excluído dessa rodada global porque já havia passado nas duas modalidades; os testes HTTP de servidor foram executados. Após essa rodada, o filtro adicional de retenção do landing passou nos **42 testes Website CRM** (`privacy-upgrade.log`, processo `375338`, `04:09:58 UTC`).

A revisão financeira adicional reproduziu um saldo artificial na associação de lead posterior à anulação. A solução copia o grafo original ao criar o contraevento e mantém a convergência posterior simétrica entre postagem/anulação. Reprodução negativa preservada em `ci_causal-test.log`; confirmação verde com **58 testes** em `ci_causal-upgrade.log`. Não se congela apenas um lado do par.

Parser Python/XML, Black 22.8, isort 5.12, flake8 5.0, catálogo Babel e `git diff --check` aprovados. O código está no worktree/branch indicados no início; não houve push, merge em outras frentes, CI remota ou implantação. O PostgreSQL exclusivo dos testes foi encerrado ao concluir.


## Versões dos addons alterados neste corte

| Addon | Versão |
|---|---|
| `marketing_center_account` | `16.0.1.0.1` |
| `marketing_center_base` | `16.0.1.0.1` |
| `marketing_center_contact_center` | `16.0.1.0.2` |
| `marketing_center_contact_center_crm` | `16.0.1.0.1` |
| `marketing_center_dashboard` | `16.0.1.0.1` |
| `marketing_center_meta` | `16.0.1.0.1` |
| `marketing_center_sale` | `16.0.1.0.1` |
| `marketing_center_sale_account` | `16.0.1.0.1` |
| `marketing_center_web_ingress` | `16.0.1.1.0` |
| `marketing_center_website` | `16.0.1.1.0` |
| `marketing_center_website_crm` | `16.0.1.1.0` |
| `meta_api_base` | `16.0.1.0.1` |
| `meta_webhook_base` | `16.0.1.0.2` |
