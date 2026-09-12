# Contra-check da revisão independente — Marketing Center

Data: 11/09/2026. Revisor: Codex, com três verificações paralelas por área e
consolidação contra código, testes e contratos. Documento analisado:
[revisão independente](2026-09-11-independent-code-review.md). Este parecer preserva a
revisão original e registra o que implementar, corrigir na análise ou manter como
decisão de escopo.

**Concordo com várias preocupações, mas não com o pacote de correções proposto sem
ajustes.** Há problemas reais no ciclo financeiro, na contagem de respostas, na operação
de filas e no ciclo de vida de dados pessoais. Também há falsos positivos e conclusões
mais fortes do que a evidência: o acesso direto do Editor ao anexo mestre esbarra em uma
proteção do Odoo; o produtor de atribuição foi explicitamente adiado; existem
formulários Soloz instrumentados no piloto; e capacidade de canais não é um campo dos
registros XML do OCA.

## 1. Base e limites da verificação

- Marketing Center: branch `16.0`, HEAD `0a62448503ad7d61cadfb7c609206692d9222e20`, com
  alterações locais preexistentes, inclusive catálogo não rastreado. Foram analisados os
  arquivos atuais, não apenas o commit. `catalog-plan.md` já se apresenta como **v0.8**;
  referências a v0.2/v0.3 precisam ser entendidas historicamente.
- Peers observados: Soloz `695c93a1c72bd47c4825790d10072aa632980756`; Contact Center
  `5203d61f1715311e8339e1ce268e4a312a6bba49`. O peer do relatório original era outro.
  Pins de CI e validações históricas continuam sendo evidências distintas do checkout
  atual.
- Site: consultados também `odoo16/website/README.md`, os registros das LPs e o worktree
  `soloz-website-pilot-20260907`, em `9c2f6dd78c5b03d650e1d8d7896733c1d4834551`.
- Revisão estática de implementação e testes; fontes primárias do Odoo/OCB e OCA para
  comportamentos herdados. Provas isoladas são identificadas abaixo. **Não foi executada
  a suíte integrada Odoo, QUnit, instalação, upgrade ou teste HTTP contra banco nesta
  tarefa.** O Python padrão consultado não tem o pacote `odoo` instalado.
- Nenhum servidor ou banco foi consultado. Configuração de produção, execução anterior
  de testes e instalação de módulos são informações documentais, não uma nova atestação
  do runtime.
- Contagem estática por AST: 21 manifests e 816 definições Python `test_*`, das quais 39
  no catálogo e suas pontes. Isso não mede cobertura, testes executados ou casos
  parametrizados; não sustenta por si só a comparação “acima da média do ecossistema”.
- A única alteração produzida por esta tarefa é este parecer. Código, documentos
  anteriores e mudanças preexistentes foram preservados.

**A árvore não coincide integralmente com o release `8093b88`.** A comparação por
conteúdo, incluindo arquivos não rastreados, encontrou cinco diferenças na ponte do
catálogo. A interface atual usa painel lateral; a descrição anterior de popup não
identifica mais todo o código local. Blobs Git observados, relativos a
`marketing_center_catalog_contact_center/`:

| Arquivo                                      | Blob observado                             |
| -------------------------------------------- | ------------------------------------------ |
| `static/src/js/catalog_dialog.esm.js`        | `edcc41e3ad64aef52feac178d44a8677fc56fa8d` |
| `static/src/js/message_composer.esm.js`      | `515d7c6024674a949227e284fb32007351603b1f` |
| `static/src/scss/catalog_dialog.scss`        | `1b9bd8fc835ad8f1115024cce31490b3c651abe2` |
| `static/src/xml/catalog_dialog.xml`          | `38a0fc3341050f264475730b68a1c30e7094eb47` |
| `static/tests/catalog_composer_tests.esm.js` | `c4f0b9a49c961177253ce95efb645836d5f85d06` |

As conclusões valem para essa fotografia. Evidência do release anterior não homologa
automaticamente essas diferenças.

“Confirmado” significa que o núcleo do problema se sustenta; “parcial” significa que há
fato válido, mas impacto, escopo ou solução precisam de correção; “refutado” significa
que o mecanismo alegado contraria a evidência encontrada. Uma recomendação de teste não
equivale a teste executado.

## 2. Decisões principais

### A1 — Postagem, retorno a rascunho e repostagem: parcial, prioridade alta

**Confirmo a falta de tratamento do desfazimento da postagem.** `_post()` emite uma nova
ocorrência após cada transição para postado, em
`marketing_center_account/models/account_move.py:107`. Não há contrapartida
correspondente para o retorno a rascunho. Contudo, o dashboard atual conta ocorrências:
a revisão não demonstrou receita monetária duplicada sendo exibida. O problema é a
incompletude do ciclo no ledger e a possível interpretação errada do contador.

Há um impacto adicional: a guarda de crédito acumulado considera todos os
`credit_note_posted` ligados à fatura
(`marketing_center_base/models/business_event_service.py:185`). Fatura de 100 e crédito
vinculado de 100, seguido de crédito → rascunho → repostagem, podem tentar registrar
-200 contra a mesma fatura e levantar `ValidationError`. Como a emissão acontece dentro
de `_post`, esse erro pode desfazer a operação nativa. É um cenário derivado do caminho
de código, pendente de reprodução integrada.

**Prova isolada executada:** o método real `_reversed_event`, extraído por AST e
alimentado com respostas ORM simuladas de fatura +100, crédito anterior -100 e novo
crédito -100, levantou a exceção de crédito acumulado. Isso confirma a rejeição do
validador; não substitui executar todo o ciclo no Odoo. Também não foi localizado o
teste que a revisão afirma validar explicitamente “cada repostagem gera nova
ocorrência”: os testes encontrados cobrem idempotência de `_ensure_move_event` e
rascunho seguido de exclusão.

**Implementar:** um contrato explícito para anulação/repostagem de faturas e créditos,
preservando ocorrências históricas e calculando o efeito vigente. Não basta copiar a
reversão de vendas: hoje o serviço proíbe reverter uma reversão
(`business_event_service.py:163`), e um crédito já pode referenciar a fatura como
reversão. Rever tipos/DTOs, pares permitidos, índices, limite de créditos e projeções
conjuntamente.

**Não implementar como remendo:** `COUNT(DISTINCT source_res_id)` no lugar da correção
do ledger. Isso não anula valores antigos, não resolve créditos, não define o que
acontece entre períodos e pode colidir entre modelos se usado sem escopo. Um indicador
de documentos distintos pode existir separadamente, com definição explícita.

Aceitação: fatura postar → rascunho → repostar sem mudança e com mudança de valor;
crédito parcial/integral → rascunho → repostar; múltiplos créditos; cancelamento quando
permitido; repetição idempotente; concorrência/backfill; efeito vigente correto por
moeda e período. Usar documentos sintéticos em base descartável, sem alterar documentos
fiscais autorizados.

### A2 — Motor sem produtor: parcial; refutada a classificação como falha alta de entrega

A ausência de produtor automático e de leitura dos resultados pelo dashboard é real.
Porém, [o contrato da fundação](2026-09-01-attribution-calculation-foundation.md)
declara expressamente, nas linhas 64–69, que produtor e projeção do dashboard ficaram
fora daquele corte. As linhas 24–45 também impedem transformar mera correlação M:N em
crédito.

**Manter o motor e esclarecer sua disponibilidade.** Documentar “fundação de cálculo
implementada; atribuição operacional ainda não habilitada”, sem anunciar ROAS/receita
atribuída como funcionalidade entregue. Implementar produtor em etapa própria, com
evidência elegível, janela temporal, origem autorizada, empresa/moeda e tratamento de
reversões.

Não aprovo a sugestão de simplesmente alimentar `_calculate` com todos os registros de
`marketing.attribution.crm.effective.link`. Um vínculo efetivo pode continuar sendo
apenas correlação. O produtor precisa demonstrar a base de elegibilidade exigida pelo
motor. Critério: candidatos não elegíveis nunca recebem crédito; ausência de evidência
permanece explícita; pesos e valores fecham; reprocessamento/revisão não duplica
resultados.

### A3 — Concorrência de filas: parcial; correção operacional necessária

Sob `root:1`, jobs de canais distintos disputam o mesmo limite global. A preocupação com
latência do atendimento procede **se essa ainda for a configuração do ambiente**. O
incidente citado não mede backlog ou degradação atual.

A correção por `capacity` em `data/queue_job.xml` é tecnicamente incorreta para o OCA 16
consultado: o modelo `queue.job.channel` não tem esse campo. A capacidade é configurada
no runner. Além disso, limitar filhos sem ampliar/dimensionar o ancestral mantém o
gargalo.

Prova isolada executada com o scheduler do OCA/queue em
`4ea642c3930bc2bbaeb760c3410714c2ec9143f1`, sem Odoo/DB: duas filas prontas produziram
um job com `root:1`; continuaram produzindo um com `root:1,A:1,B:1`; produziram dois com
`root:2,A:1,B:1`. A/B são canais sintéticos, não configuração sugerida para produção.
Fontes:
[modelo de canal](https://github.com/OCA/queue/blob/4ea642c3930bc2bbaeb760c3410714c2ec9143f1/queue_job/models/queue_job_channel.py),
[scheduler](https://github.com/OCA/queue/blob/4ea642c3930bc2bbaeb760c3410714c2ec9143f1/queue_job/jobrunner/channels.py).

**Implementar:** inventário do runner ativo, hierarquia completa, capacidade
global/filhos, concorrência HTTP/DB e prioridades; política documentada e teste de
saturação com atendimento e sync simultâneos. Canais controlam admissão, não reservam
workers exclusivos. Não fixar um novo número de slots sem medir capacidade do serviço.

### A4 — Privacidade e retenção: parcial na conclusão jurídica; lacuna técnica confirmada e prioritária

O bootstrap chama a captura automaticamente, **quando existe binding ativo e a
configuração permite**, e o payload inclui `consent_state: "unknown"`
(`marketing_center_website/static/src/js/landing_capture.esm.js:132`;
`landing_bootstrap.esm.js:85`). As ações de formulário/WhatsApp também fixam `unknown`
(`marketing_center_website/models/action_service.py:267`). Click IDs são armazenados sem
criptografia de aplicação em `protected_value`; há restrição de acesso, portanto “em
claro” não significa “publicamente legível”
(`marketing_center_web_ingress/models/event.py:180`).

A solução precisa ir além de mudar o JS para `granted`: o servidor **rejeita decisões de
consentimento declaradas pelo ingresso público**
(`marketing_center_web_ingress/models/service.py:132`). O núcleo aceita snapshots e
revisões, mas falta o produtor confiável que represente a decisão do titular no fluxo
web. Permitir que qualquer POST declare uma autorização seria regressão.

`retain_until` é persistido, mas não executa retenção. Os valores privados de Lead Ads
têm escrita/exclusão bloqueadas (`marketing_center_meta/models/lead_ads.py:944`).
Confirmo a falta de mecanismo de eliminação desse conteúdo, inclusive por requisição do
titular. O apagamento por consumidor existente no webhook não resolve automaticamente as
cópias nos ledgers de marketing.

O código sozinho não permite concluir que todo tratamento é ilícito ou que consentimento
seja a única base possível. A definição depende de finalidade e hipótese legal; a ANPD
trata consentimento e legítimo interesse e estende as orientações a tecnologias
similares de rastreamento. Isso não elimina a lacuna de finalidade, decisão e retenção
executável.
[Guia da ANPD](https://www.gov.br/anpd/pt-br/centrais-de-conteudo/materiais-educativos-e-publicacoes/guia-orientativo-cookies-e-protecao-de-dados-pessoais.pdf/@@display-file/file).

**Implementar:** política por finalidade, versão do aviso/decisão, verificação no
servidor e bloqueio da captura opcional enquanto a política não autorizar;
atualização/revogação por canal confiável; expurgo/tombstone restrito a campos sensíveis
com auditoria e lotes limitados; prazo definido na criação e job que o aplique. Mapear
todas as cópias e impedir que retry/backfill restaurem conteúdo eliminado. Preservar
hashes não equivale automaticamente a anonimizar dados de baixa entropia.

Aceitação: navegação sem decisão, recusa, concessão e revogação; POST forjado `granted`;
formulário necessário funcionando conforme sua própria finalidade; expiração real;
repetição segura do expurgo; tentativa de reingestão após eliminação; preservação apenas
da evidência mínima definida na política.

### A6 — QUnit fora da CI: confirmado; prioridade alta para publicação do catálogo

Os três addons com testes JavaScript declaram assets de teste, mas o repositório não
contém `browser_js` em um `HttpCase` que execute suas suítes JavaScript. Existem
`HttpCase` para outras verificações HTTP. `.github/workflows/test.yml:107` inicializa
dependências e executa os testes dos addons locais; declarar o bundle não faz a suíte
JavaScript rodar. O core do Odoo usa uma invocação explícita em
[web/tests/test_js.py](https://github.com/odoo/odoo/blob/16.0/addons/web/tests/test_js.py).
Os scripts
[oca_init_test_database](https://github.com/OCA/oca-ci/blob/master/bin/oca_init_test_database)
e [oca_run_tests](https://github.com/OCA/oca-ci/blob/master/bin/oca_run_tests),
consultados como comportamento upstream, também não demonstram execução automática
desses assets pelo workflow atual.

**Implementar:** `HttpCase` pós-instalação por addon, ou harness comum que selecione os
módulos QUnit reais e falhe se nenhum teste rodar. Cobrir o patch do formulário, o
`catalog_x2many` e o composer/painel. Validar assets de produção e modo debug quando
aplicável. Um ensaio com falha deliberada deve provar que o job da CI fica vermelho;
depois remover a falha e obter execução verde com contagem registrada. Não transportar
resultados locais como se fossem resultado da CI.

### Novo achado N1 — Ponte atual do catálogo incompatível com os peers anteriores: prioridade alta

A divergência deixou de ser apenas “compatibilidade não provada” **na árvore atual**.
`marketing_center_catalog_contact_center/static/src/js/message_composer.esm.js:136` usa
`this.ui.sidePanel`, e a linha 141 chama `this.toggleSidePanel("catalog")`. Essas APIs
não aparecem no `contact_center_ui`/`contact_center_crm` dos commits `1e6fb3d` (pin do
workflow) e `1ef3743` (peer registrado na validação do catálogo). Elas foram
introduzidas no Contact Center em `9d70499` e existem no peer atual, em
`contact_center_ui/static/src/js/contact_center_app.esm.js:37,89`.

Isso demonstra uma incompatibilidade de contrato para o caminho de abertura do catálogo
com os peers antigos: `this.ui` existe, mas `toggleSidePanel` não. A chamada deve falhar
ao abrir o painel; a reprodução no navegador continua pendente. A afirmação da revisão
de que a compatibilidade é “provável” não pode ser mantida para esses cinco arquivos
modificados.

**Implementar antes de publicar:** consolidar a alteração atual em commit revisável;
selecionar e testar um SHA do Contact Center que ofereça a API usada; atualizar
pin/manifesto de compatibilidade e o registro de validação conjuntamente. Não basta
escolher o HEAD mais novo sem homologar o par. Testar abrir/fechar painel, alternar
painéis, seleção, inclusão de texto/anexo no rascunho, troca de conversa e permissão
empresarial.

O workflow já permite `workflow_dispatch` com `peer_ref` e PR para `16.0*`. Push em
`release/*` realmente não dispara sozinho, mas não é preciso ampliar triggers se o
processo usar as vias existentes. O commit `8093b88` preserva a versão anterior do
catálogo; não preserva automaticamente as cinco mudanças locais acima.

### A5 — WhatsApp e instrumentação Soloz: parcial

**WhatsApp:** confirmado que o redirect entrega somente `https://wa.me/<número>`
(`marketing_center_website/controllers/website_action.py:297`). Há prova do handoff, não
vínculo determinístico com a conversa posterior. O grant atual é uma capacidade de
redirecionamento de uso único, consumida antes de abrir o WhatsApp e sujeita a purga
(`models/redirect_grant.py:260`).

Um código opaco no texto pré-preenchido é uma possibilidade, mas deve ser um contrato de
correlação próprio, com empresa/destino, expiração, consumo idempotente e proteção
contra replay/encaminhamento. Não reutilizar o segredo do grant nem dar a esse código
poder de acessar conversa/lead. O usuário pode apagar o texto ou não enviar a mensagem;
código ausente deve resultar em “sem correlação”, nunca em atribuição inventada. O
master-plan citado pede clique rastreável sem PII na URL; não especifica esse protocolo
de junção.

**Site:** a falta de marcadores no checkout canônico não prova inexistência de
instrumentação no projeto. O
[registro das LPs](../../../website/landing-pages-2026-09-08.md), linhas 164–220,
documenta três ações e views locais que acrescentam `data-marketing-form-action`, três
jornadas CRM/Marketing e UTMs verificadas. Essa é evidência histórica de piloto, não
publicação atual. O addon continuar dependendo apenas de `website`/`website_crm` é uma
separação declarada, não erro automático.

**Implementar:** promover configuração reproduzível de ações/markers por ambiente,
usando configuração versionada ou uma ponte específica se houver necessidade recorrente;
testar o HTML efetivamente servido, lead, intenção e correlação. Evitar embutir UUIDs de
um banco nos seeds genéricos. Antispam/reCAPTCHA deve estar instalado, configurado e
testado no destino; a ausência no manifesto Soloz, isoladamente, não comprova ausência,
e o instalador de homologação já lista `google_recaptcha`
(`odoo16/scripts/odoo16_website_homolog_install.py:43`).

### Catálogo — token do anexo: refutado o ataque atribuído ao Editor; proteção adicional condicional

O argumento “Editor escreve no item, logo pode gerar token no anexo mestre” ignora uma
guarda anterior do core. `file_data` é um `Binary(attachment=True)`: o anexo tem
`res_field=file_data`. No Odoo/OCB 16 consultado, `ir.attachment.check()` nega acesso
direto a anexos com `res_field` para usuário que não seja system, antes de avaliar
permissões no registro pai. `generate_access_token` passa pelos controles de
leitura/escrita.
[Fonte OCB, SHA fixo](https://github.com/OCA/OCB/blob/45f8ce2c8626c8de40915439ad2b828762a63e78/odoo/addons/base/models/ir_attachment.py#L444-L485).

**Prova isolada executada:** extração AST do método real `check`, com dublês de
ambiente/cursor e decorador. Editor com `res_field=file_data` recebeu `AccessError`, com
zero chamadas de acesso ao pai; controles sem `res_field` ou com usuário system chegaram
às permissões do pai. Não é prova HTTP integrada nem certificação de toda combinação de
addons instalada.

Resta uma decisão de contrato: se o mestre deve permanecer privado **inclusive contra
publicação acidental por administrador técnico ou tokens preexistentes**, vale
implementar proteção adicional. Ela precisa cobrir `create`, `write`, `public`,
`access_token`, geração de token e comportamento de leitura de anexos antigos. Somente
sobrescrever `generate_access_token` não fecha essas variantes. O core permite leitura
por token válido/public quando esse estado existe; não foi demonstrado que tais anexos
existam no catálogo atual.
[Geração e validação no core](https://github.com/OCA/OCB/blob/45f8ce2c8626c8de40915439ad2b828762a63e78/odoo/addons/base/models/ir_attachment.py#L678-L704).

Aceitação antes de declarar o ataque possível ou corrigido: Editor comum, administrador
técnico, usuário externo e outra empresa; GET anônimo pelo ID do attachment e pela rota
do campo; token inexistente/preexistente; `public=True`; vínculo/revínculo do anexo.
Consultar o estado real e revogar flags/tokens existentes somente por migração
explicitamente planejada, se forem encontrados.

### M12 — Sessão, página e duplicidade: parcial

`sessionStorage` não fornece identidade persistente entre visitas/dispositivos. Isso
está documentado em `marketing_center_website/README.rst:18`. **Não torna first-touch
impossível dentro da jornada observada:** `_session_touchpoints` busca evidência
anterior da mesma sessão por até 24 horas
(`marketing_center_website_crm/models/service.py:392`). Tampouco toda nova aba
necessariamente começa com outro identificador: havendo `opener`, o armazenamento pode
começar como cópia.
[MDN](https://developer.mozilla.org/en-US/docs/Web/API/Window/sessionStorage).

A página do evento de formulário é o `source_path` da ação, não uma captura arbitrária
do DOM. Isso é um contrato estável e deliberado, mas exige ação própria por página
quando esse detalhe importa. As LPs do piloto fazem isso. Família/aplicação chegam ao
lead no piloto; não há, porém, dimensão genérica de produto/família estruturada no
contrato do ingresso de marketing.

“Sem dedup de lead” precisa separar fatos: há idempotência de intenção/correlação, com
testes de replay e de retry transacional (`tests/test_website_crm.py:207,1300`); ela não
evita dois POSTs nativos independentes. O controller chama a criação nativa antes de
capturar a intenção (`controllers/website_form.py:54`). A mesma sessão pode
legitimamente gerar dois leads, inclusive com teste explícito
(`test_website_crm.py:419`).

**Implementar:** declarar o alcance da atribuição por sessão, manter contexto de
página/produto confiável e adicionar idempotência por submissão ao fluxo nativo se o
gate de retry do site exigir isso. Não fundir leads simplesmente porque têm a mesma
sessão ou e-mail. Testar duplo clique, perda de resposta/reenvio HTTP, nova solicitação
legítima, aba duplicada, armazenamento indisponível e contexto de outra empresa.

## 3. Disposição dos demais achados médios

| Item                              | Disposição                                              | Evidência e melhoria adequada                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| --------------------------------- | ------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **M1 — funil/transições**         | **Parcial; média**                                      | Reentradas geram ocorrências e o dashboard usa `COUNT(*)` (`marketing_center_crm/models/service.py:599`; `marketing_center_dashboard/models/dashboard.py:424`). Isso segue o contrato de eventos por transição, não prova duplicação indevida de fatos. Arquivar como perda também acompanha o CRM nativo: `toggle_active`/`action_set_lost` em [Odoo CRM](https://github.com/odoo/odoo/blob/16.0/addons/crm/models/crm_lead.py). Criar/rótular separadamente transições, oportunidades únicas e coortes/estado atual. Testar A→ganho→A→ganho, ganho→outro estágio ganho, arquivar/desarquivar. Não suprimir histórico para ajustar KPI.                                           |
| **M2 — janela UTC**               | **Parcial; média**                                      | Janela do resumo em UTC e fontes por timezone estão explícitas (`dashboard.py:171,191,582`); não há evidência de timestamp corrompido. Em São Paulo a fronteira ocorre às 21h locais. Exibir timezone/janela e, se necessário, configurar calendário gerencial da empresa. Testar fronteiras locais, empresas distintas e horário de verão; preservar timestamps UTC e calendários originais das plataformas.                                                                                                                                                                                                                                                                      |
| **M3 — moedas/tempo**             | **Parcial; mistura monetária atual refutada**           | Documento em sua moeda e alocação na moeda empresarial são bases distintas deliberadas (`marketing_center_account/README.rst:7`; `models/service.py:319,436`; teste de moeda estrangeira em `tests/test_marketing_center_account.py:292`). O dashboard não soma esses valores. Manter a separação. A data de backfill é aproximada por `invoice_date/date` (`models/service.py:380`), enquanto live observa o instante da postagem: registrar origem/precisão do tempo ou data de negócio separada. Conversão futura exige política de taxa/data e valor convertido separado.                                                                                                      |
| **M4 — resposta de episódio**     | **Confirmado; média, corrigir antes de confiar no KPI** | O dashboard conta `first_human_response` (`dashboard.py:420`). Lifecycle escolhe `origin='agent'` (`marketing_center_contact_center/models/lifecycle_bridge.py:213`); episódio também aceita dispositivo externo e só reaproveita evento da mesma mensagem (`models/response_episode.py:1120`). Com resposta externa anterior à resposta agent, lifecycle executado primeiro pode produzir dois eventos para um episódio. Contar pela identidade/projeção do episódio e testar ambas as ordens, concorrência e múltiplos episódios. Cenário derivado estaticamente; não reproduzido integrado.                                                                                     |
| **M5 — retries**                  | **Confirmado no bridge CC; média operacional**          | `attribution_bridge.py:170`, `lifecycle_bridge.py:98` e `response_episode.py:626` usam zero e convertem todo `OperationalError` em retry. No OCA, [zero significa sem limite](https://github.com/OCA/queue/blob/4ea642c3930bc2bbaeb760c3410714c2ec9143f1/queue_job/job.py#L534-L548). Classificar erros transitórios por SQLSTATE, definir orçamento de tentativas/tempo, alerta e reprocessamento. Usar `failed` nativo se suficiente; não criar obrigatoriamente um estado `dead`. A ponte Website CRM já usa oito tentativas (`models/intent.py:224`); o webhook Meta também deixa `OperationalError` fora de seu teto aplicativo (`meta_webhook_base/models/delivery.py:258`). |
| **M6 — internals do CC**          | **Parcial; média de manutenção**                        | Token privado, SQL e índice em tabela alheia existem (`response_episode.py:10,128,767`). Porém já há hook de dependências da exclusão (`marketing_center_contact_center/models/conversation_actions.py:7`) e uso de `_contact_center_lock_conversation_graph` (`marketing_center_contact_center_crm/models/crm_lead.py:34`). Estabilizar contratos de capacidade/consulta e mover gestão de índices para o dono da tabela; não criar hooks duplicados nem retirar locks.                                                                                                                                                                                                           |
| **M7 — repetição**                | **Parcial; baixa**                                      | Mixins parecidos são candidatos a helper comum, preservando tokens/autoridades separados. Guards usados pelos testes constroem histórico anterior à instalação; não são automaticamente código morto. A cópia CC→MC possui proveniência/mapping (`attribution_bridge.py:247`) e separa fatos operacionais da análise. Não eliminá-la apenas por DRY. Revisar dependência `utm` sem consumidor localizado e melhorar rótulos sem PII; validar instalação mínima.                                                                                                                                                                                                                    |
| **M8 — custo síncrono**           | **Parcial; média condicionada a medição**               | Criação por lead na transação e backfill até 100×500 linhas existem (`marketing_center_crm/models/crm_lead.py:75,327`). Mover o backfill longo para lotes retomáveis é justificável. A view usa window function, mas afirmar varredura de toda a tabela a cada lead exige plano real; há filtros e limites no consumidor (`marketing_center_website_crm/models/service.py:392`). Medir latência/queries e `EXPLAIN` em homologação antes de materializar views ou tornar toda ingestão assíncrona. Preservar captura transacional do fato se usar fila.                                                                                                                            |
| **M9 — diagnóstico Meta**         | **Confirmado; média**                                   | `meta_api_base/services/graph.py:313` não captura `fbtrace_id`, e os consumidores não persistem código/subcódigo; headers de uso não são tratados como `Retry-After`. Já existe classificação/mensagem sanitizada (`marketing_center_meta/models/meta_service.py:342`), portanto não é ausência total de diagnóstico. Persistir campos tipados e limitados de trace, códigos e uso/backoff; não salvar corpo de erro/headers brutos com dados sensíveis.                                                                                                                                                                                                                           |
| **M10 — saúde proativa Meta**     | **Confirmado para Ads reader; média**                   | Expirações são gravadas (`marketing_center_meta/models/meta_service.py:322`), mas os crons não fazem revalidação proativa desse perfil. Existe validação manual e atualização por falha real. Adicionar cron escalonado/deduplicado com alertas, revisões e backoff. Reconciliar subscriptions do webhook é outra função e não substitui validade/escopos do token Ads.                                                                                                                                                                                                                                                                                                            |
| **M11 — rate limit**              | **Parcial; média/alta conforme exposição**              | Cota compartilhada por endpoint/tipo permite disputar capacidade (`marketing_center_web_ingress/models/admission.py:129`). Não foi comprovada ausência de proteção no proxy ativo: a borda documentada é Traefik, e ausência de `limit_req` de Nginx não resolve essa pergunta. Validar limiter e IP confiável na borda, NAT e retries legítimos. A tabela de admissões tem autovacuum (`:160`), assim como grants; eventos/click IDs precisam da política A4. Routing key desconhecida é rejeitada antes do corpo no webhook (`controllers/webhook.py:214`).                                                                                                                      |
| **M12 — sessão/contexto/dedup**   | **Parcial**                                             | Disposição detalhada na seção 2. Separar janela da sessão, idempotência de correlação, repetição de POST nativo e deduplicação comercial.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| **M13 — documentos**              | **Parcial; média para contrato de release**             | README ainda generaliza versões 1.0.0 apesar de manifests 1.0.1; `catalog-plan.md:228` também diverge da versão do núcleo. Há contradição entre marcos de Fase 7. Corrigir documento vigente com matriz versão/SHA/teste e marcar registros históricos como substituídos, preservando seu conteúdo. `oca_dependencies.txt` com branches não é a fonte efetiva do pin da CI; documentar sua finalidade ou reconciliá-lo. Versões históricas antigas não são, por si, erro.                                                                                                                                                                                                          |
| **M14 — i18n**                    | **Confirmado; média de experiência do usuário**         | Só o catálogo e pontes possuem `pt_BR.po`; há literais PT no SQL (`dashboard.py:575,649`) e mistura de idiomas. Priorizar telas e mensagens usadas por gestor/atendente. Strings XML são extraíveis pelo Odoo; string gerada diretamente em SQL precisa solução própria. Padronizar idioma-fonte e catálogo de traduções, sem reescrever chaves técnicas.                                                                                                                                                                                                                                                                                                                          |
| **M15 — cobertura**               | **Parcial; média**                                      | Faltam cenários concorrentes do Google e assertivas HTTP específicas do webhook; o webhook já tem `HttpCase` com 200/403 e retries. Razão linhas de teste/código não mede cobertura. Implementar cenários de falha/concorrência e fixtures representativas sanitizadas, reais quando úteis; payload capturado não é requisito universal de qualidade. O objetivo é verificar contrato/status e ausência de efeitos em rejeições.                                                                                                                                                                                                                                                   |
| **M16 — arquitetura do catálogo** | **Predominantemente refutado; ajuste editorial baixo**  | `ARCHITECTURE.md:45` exclui os três addons de catálogo da contagem dos 14 componentes; `:56` descreve aplicação independente e menu próprio. A regra da raiz de menu contém exceção para outro produto, aplicável aqui; README também explica independência e cadastro direto. Resta explicitar melhor a razão de coexistirem no mesmo repositório e distinguir “catálogo de anúncios” de “Catálogo de Conteúdo”.                                                                                                                                                                                                                                                                  |

## 4. Achados baixos e decisões do catálogo

| Item                                                    | Disposição e ação                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| ------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **SEC-05 — `webhook_url`**                              | Confirmado estruturalmente: o campo calculado lê `routing_key` restrita a System, mas não repete a restrição (`meta_webhook_base/models/endpoint.py:65,92,139`; view `:51`). O `AccessError` ao abrir o formulário é inferência, sem reprodução integrada. Ajustar grupos do campo/view e testar Marketing Admin sem System; não usar `compute_sudo` como remendo. A URL com routing key não substitui a assinatura HMAC.                                                                                                                                                                                                     |
| **SEC-04 — grupo implica CC Admin**                     | Confirmado como papel de administração conjunta (`marketing_center_contact_center/security/marketing_center_contact_center_security.xml:9`), sem escalada não autorizada demonstrada. Documentar permissões e separar perfil de consulta/operação se houver necessidade concreta. Não retirar implicação sem revisar fluxos que dependem dela.                                                                                                                                                                                                                                                                                |
| **`debug_token` na query**                              | Confirmada superfície de log em `meta_api_base/services/graph.py:505`, não vazamento demonstrado. Já há sanitização de exceções de transporte. Garantir logging redigido e configuração operacional; não afirmar que a API exige essa forma sem revalidar seu contrato específico.                                                                                                                                                                                                                                                                                                                                            |
| **OPS-08 — rotação**                                    | Parcial: existem referências/revisões, permissões de arquivos e fences documentados. Falta procedimento completo com validação, troca, jobs antigos, revogação e rollback. Evitar substituição silenciosa do conteúdo sob a mesma referência quando o mecanismo acompanha revisões da configuração.                                                                                                                                                                                                                                                                                                                           |
| **Job com `user_id` nulo**                              | Parcial e restrito ao caminho do webhook anônimo `auth=none`; não vale para todo ingresso, pois o web ingress é síncrono e Website tem usuário público definido. OCA serializa UID **e** `su`; `sudo()` não cria identidade técnica. Definir usuário explicitamente no enqueue e testar payload serializado, restauração e empresa. Nenhum job real nulo foi consultado. [Serialização OCA](https://github.com/OCA/queue/blob/4ea642c3930bc2bbaeb760c3410714c2ec9143f1/queue_job/fields.py#L74-L83), [restauração](https://github.com/OCA/queue/blob/4ea642c3930bc2bbaeb760c3410714c2ec9143f1/queue_job/fields.py#L108-L116). |
| **Config `no-store`, `Sec-Fetch-Site`, UTM**            | Não são vulnerabilidades autossuficientes. `no-store` ajuda a revogar configuração; ausência de `Sec-Fetch-Site` mantém compatibilidade e há validação de Origin; UTM é dado livre limitado, não segredo confiável. Medir custo de configuração, testar borda e orientar minimização de PII em parâmetros. Cache, se introduzido, deve preservar fences de revisão.                                                                                                                                                                                                                                                           |
| **Tabela curta, copier, README, lint, manifest `mail`** | Baixa prioridade. Nome curto de tabela não demonstra defeito. São 11 addons sem ambos `readme/` e descrição HTML, não 12. O override ESLint para módulos em `.js` é legítimo; `.esm.js` não é requisito do framework. Corrigir ordenação de imports e documentar manutenção do workflow customizado. Remover dependências somente após conferir uso indireto e instalação mínima; não fazer refatoração transversal apenas para uniformizar nomes.                                                                                                                                                                            |
| **Escopo simples do catálogo**                          | A edição direta sem aprovação é orientação atual do proprietário, registrada em `catalog-plan.md:5,15` e no plano Soloz transferido. Não reintroduzir publicação, aprovação, congelamento obrigatório de bytes ou recibo como “correção” desta revisão. O critério de primeira entrega é busca/consulta/compartilhamento autorizado e seguro, não um fluxo editorial que foi descartado.                                                                                                                                                                                                                                      |
| **Catálogo público/comunitário e menu**                 | “Distribuição pública” refere-se ao código do addon, não a tornar anexos públicos. README/arquitetura já descrevem produto independente. Melhorar a nomenclatura e o contrato de compatibilidade sem recolocar dependência desnecessária de `marketing_center_base`.                                                                                                                                                                                                                                                                                                                                                          |
| **Branches, CI e arquivos não rastreados**              | A versão anterior está preservada em branch local, mas a árvore atual inclui alterações posteriores. Consolidar estado exato e validar o par de SHAs, como N1. Idade do código, volume de linhas e velocidade de implementação não provam defeito; ausência de execução representativa e dependência incompatível são evidências objetivas.                                                                                                                                                                                                                                                                                   |

## 5. Sequência recomendada de implementação

Esta sequência é um backlog técnico revisável; não representa mudanças já aplicadas.

| Ordem                                        | Entrega                                                   | Critério de conclusão                                                                                                                                                                                                                                |
| -------------------------------------------- | --------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1 — próximo ciclo de correção**            | **A1: ciclo financeiro completo**                         | Teste integrado primeiro demonstra o problema; implementação trata invoice/crédito, anulação, repostagem e crédito acumulado vigente sem interferir indevidamente na operação nativa. Ledger e leitura econômica concordam.                          |
| **2 — antes de promover catálogo**           | **N1 + A6: commits compatíveis e QUnit na CI**            | Código local consolidado; SHA do peer escolhido por compatibilidade; instalação/upgrade e navegador no mesmo par; CI realmente executa JS e falha quando há regressão. Smoke do painel/composer e permissões passa.                                  |
| **3 — operação de filas**                    | **A3 + M5: orçamento e recuperação**                      | Configuração atual coletada; runner/filhos/HTTP dimensionados com carga; sync pesado não excede orçamento de latência do atendimento; falha persistente tem fim, alerta e reprocessamento controlado.                                                |
| **4 — dados pessoais e abertura de tráfego** | **A4 + M11: política executável e controle da borda**     | Fluxo opcional respeita decisão/política; expurgo/eliminação alcança cópias; retry não restaura PII; limiter é validado no proxy real. Prioridade de prontidão do site e de operação dos leads existentes, não apenas uma melhoria futura de banner. |
| **5 — confiança nos indicadores**            | **M4, M1–M3: contrato e projeções**                       | Uma identidade por episódio; KPI de transições separado de oportunidade única; janela/timezone visíveis; nenhuma soma entre moedas/bases incompatíveis; precisão temporal do backfill declarada.                                                     |
| **6 — ajustes pequenos e observabilidade**   | **SEC-05, M9, M10, M13, M14**                             | Formulário funciona para papéis corretos; diagnósticos úteis sem segredo; alerta proativo de credencial; docs alinhadas ao SHA e traduções dos fluxos usados. Podem correr em paralelo às entregas anteriores.                                       |
| **7 — jornada do site**                      | **A5 + M12: configuração promovível, reenvio e WhatsApp** | Markers/configuração reproduzíveis; reenvio de submissão segue regra explícita; mensagem com código é correlacionada idempotentemente, e ausência de código não cria vínculo presumido.                                                              |
| **8 — evolução medida**                      | **A2, M6–M8, M15 e proteção adicional do catálogo**       | Produtor de crédito com política/evidência própria quando priorizado; backfill retomável; desempenho medido; contratos entre addons estáveis; testes de risco; regra sobre publicação por System do anexo formalizada antes de endurecer o core.     |

Não aprovo como correções isoladas: `COUNT(DISTINCT)` para saldo financeiro; campo XML
`capacity`; crédito a partir de todo link CRM; confiança em `consent_state` enviado pelo
browser; dependência obrigatória de Marketing no addon visual sem necessidade; remoção
do motor de atribuição; unificação de tokens entre domínios; aprovação editorial de
volta ao MVP; `compute_sudo` para esconder erro de permissões.

## 6. Evidência e encerramento desta revisão

Foram executadas três provas isoladas de código real, com ambiente mínimo simulado:
guarda de anexos `res_field`, scheduler hierárquico do OCA e validador de crédito
acumulado. Elas sustentam decisões específicas; não validam instalação, ORM completo,
navegador ou produção. Também foi feita a contagem AST e a comparação de blobs dos
arquivos não rastreados com o release.

Os pontos positivos de isolamento empresarial, guardas de escrita, proveniência e
idempotência têm suporte no código inspecionado e devem ser preservados nas correções.
Porém este contra-check não certifica todo o conjunto de claims de segurança ou reabre
integralmente cada auditoria antiga. TST-05 e a presença dos testes concorrentes C07
foram reconferidos por leitura, não reexecutados; SEC-03, SEC-06 e META-06 não receberam
nova verificação adversarial completa. A tabela de disposições antigas continua sendo
histórico, não nova evidência de encerramento.

Há referências inexatas na análise original: vários serviços citados sob `services/`
vivem em `models/`; o exemplo mais importante é
`marketing_center_base/models/attribution_calculation_service.py`. Este parecer usa os
caminhos atuais. Antes de executar o backlog, fixar os SHAs e repetir os cenários
integrados relevantes nessa base, especialmente A1, M4 e N1.
