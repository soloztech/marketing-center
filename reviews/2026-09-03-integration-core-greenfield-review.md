# Integration Core — revisão greenfield

Data: 2026-09-03  
Escopo: `google_api_base`, `meta_api_base`, `meta_webhook_base`  
Natureza: revisão de código e correção local; sem deploy e sem commit

## Veredito

O recorte permanece corretamente técnico e compartilhável: credenciais e
transportes ficam no Integration Core; conversa, campanha, CRM e Website ficam nos
addons consumidores. Não foi encontrada dependência circular nem regra de domínio
de negócio nos três addons.

A revisão encontrou e corrigiu riscos concretos em seis áreas: serialização de
segredos, taxonomia de retry, isolamento de namespaces no roteamento, corrida de
fila/reconciliação, ciclo de vida da configuração e limites de entrada/paginação. A remoção do aplicativo/menu
standalone **Meta Webhooks** foi preservada; a migration `16.0.1.3.0` não foi
alterada.

Depois das correções, o estado proposto é:

| Addon | Versão | Testes declarados no código |
| --- | --- | ---: |
| `google_api_base` | `16.0.1.1.2` | 42 |
| `meta_api_base` | `16.0.1.1.1` | 45 |
| `meta_webhook_base` | `16.0.1.4.2` | 73 |

Os números acima são contagem AST de métodos `test_*`, não alegação de execução em
Odoo. Este WSL não contém um runtime Odoo importável; portanto os testes integrados
precisam ser executados pelo release canônico antes do deploy.

## Correções aplicadas

### 1. Contratos secretos e runtime

- `MetaRuntimeApp` agora rejeita `pickle` explicitamente, inclusive o protocolo
  estendido. Um App Secret resolvido não pode atravessar por acidente um payload de
  fila/RPC.
- As duas capabilities internas do webhook Meta passaram de `object()` para tokens
  process-local com representação segura e serialização recusada.
- Segredos Meta rejeitam caracteres de controle e DEL, além dos limites de tamanho
  já existentes.
- IDs de App/Page/Customer são ASCII canônicos. Algarismos Unicode visualmente
  semelhantes não atravessam o contrato e não criam identidades ambíguas.
- Chave privada e access token Google inválidos em UTF-8 falham dentro do contrato
  tipado, sem escapar como `UnicodeEncodeError` nem chegar ao transporte.
- O transporte de service account Google força `allow_redirects=False`. O
  `token_uri` já é allow-listed como `https://oauth2.googleapis.com/token`, e um
  redirect não pode ampliar essa fronteira.

### 2. Limites e taxonomia de transporte

- Requisições Graph agora validam caminho, profundidade, total de nós, inteiros,
  floats finitos e limite agregado de 2 MiB antes do I/O.
- O canal de corpo é definido pela presença explícita de `data`/`json_data`, não por
  truthiness: JSON vazio continua JSON, dois canais explícitos falham e GET com corpo
  é recusado antes da rede.
- Metadados de erro Meta são limitados a intervalos estáveis; booleanos não são
  aceitos como inteiros incidentais.
- Números não finitos vindos de JSON (`1e999`, `Infinity` ou equivalentes já
  materializados) falham fechado nos códigos Meta, timestamps de webhook,
  expiração OAuth e totais paginados Google. Eles não escapam como
  `OverflowError` fora do contrato tipado.
- Códigos Meta de business-use-case throttling (`80000..80014`) entram na classe de
  rate limit.
- `MetaApiRateLimitError` é retryable (`MetaApiTransientError`), não uma falha de
  autenticação pausada. Pausa continua reservada para credencial/configuração.
- HTTP 425 segue a mesma semântica conservadora do 408: retryable em leitura e
  `uncertain` em mutação.
- Exceções inesperadas/retry de consumidores são substituídas por erros estáveis ao
  atravessar a fila. Texto e cadeia de exceção do consumidor não sobrevivem no
  diagnóstico compartilhado; um atraso explícito é validado e limitado a 24 h.
- Reconciliação respeita o `Retry-After` tipado e limitado de rate limit/transiente;
  quando o provedor não fornece atraso positivo, `seconds=None` mantém o
  `retry_pattern` exponencial do OCA.
- Os antigos `except: pass` no caminho Google foram substituídos por estados
  explícitos, mantendo a sanitização sem esconder fluxo de controle.

### 3. Roteamento Meta correto por namespace

- O ativo de roteamento agora é único por
  `(endpoint_id, object_type, external_asset_id)`, e o fan-out filtra os mesmos três
  componentes.
- Isso impede que IDs numericamente iguais de uma Facebook Page e de uma conta
  Instagram encaminhem um evento ao consumidor errado.
- A regex do routing key é um contrato único compartilhado por controller e model;
  chaves inválidas são rejeitadas antes da consulta ao banco.

### 4. Filas, locks e recovery

- Enqueue de delivery e dispatch agora toma lock da linha, invalida cache e decide a
  partir do estado persistido antes de pesquisar/criar o job.
- Workers de subscription, delivery e dispatch só executam quando existe um UUID
  persistido e ele é exatamente o `job_uuid` da execução. UUID vazio não é mais uma
  autorização implícita para um job órfão/stale.
- Lookup e recovery aceitam apenas `identity_key` canônica; o fallback por um
  ponteiro UUID sem identidade foi removido. O enqueue grava job e ponteiro na mesma
  transação, portanto não existe caminho funcional legítimo que dependa do fallback.
- Requeue também revalida o estado persistido sob lock; uma tela com cache antigo não
  revive evidência já terminal.
- Escritas de configuração de Endpoint/Page comparam o valor desejado ao snapshot SQL
  adquirido sob lock, e não ao cache ORM potencialmente antigo. Um no-op concorrente
  não avança revision nem invalida observações sem necessidade.
- Subscription aplica a mesma disciplina `Endpoint -> Page -> Subscription` e decide
  mudança pelo snapshot SQL. Reativar uma subscription a partir de cache antigo não
  deixa de avançar os fences da união.
- A ordem global do dispatch ficou `Endpoint -> App -> Page`, alinhada às mutações de
  configuração e removendo a inversão `Page -> Endpoint`.
- A reconciliação de subscriptions serializa workers concorrentes por Endpoint com
  advisory lock transacional, valida sob `FOR SHARE` a propriedade exata do UUID e
  revalida Endpoint/App antes do efeito. O `identity_key` do OCA deixa de ser a única
  barreira contra duplicação concorrente, sem bloquear os locks compartilhados do
  challenge/webhook público.
- A revision do App agora integra a identidade do job. Rotacionar/pausar o App não
  pode fazer uma reconciliação nova reutilizar um job criado para a credencial
  anterior.
- Mudança material em `meta.api.app` invalida a observação dos endpoints dependentes
  usando ordem `Endpoint -> App`: pausa projeta erro explícito; retomada/rotação volta
  a `unknown` até novo readback. Isso corrige inclusive o caso em que o cron exclui um
  App pausado e, antes, deixaria `in_sync` visível indefinidamente.
- Arquivamento de asset segue `Endpoint -> Page -> Asset`; fan-out e dispatch travam e
  revalidam o asset corrente. Uma entrega já separada para consumo não atravessa um
  asset aposentado por uma corrida de booleano sem lock.
- O antigo `queue_job_uuid` de Page foi removido. A reconciliação sempre foi por
  Endpoint, portanto esse campo era um segundo estado sem escritor/leitor canônico.

### 5. Paginação, volume e custo

- Readback de App subscriptions e Page `subscribed_apps` segue no máximo 10 páginas,
  apenas por cursor opaco `after`, sempre no mesmo path. A URL `paging.next` do
  provedor nunca é seguida.
- Cursor vazio, repetido, não textual, de controle ou maior que 4 KiB falha fechado.
- Respostas de subscription são validadas estruturalmente; `active` precisa ser bool
  e nomes de campo precisam respeitar o contrato tokenizado.
- Readback duplicado para o mesmo `object` Meta nunca é colapsado como se estivesse
  em sincronia; a reconciliação marca drift e exige uma observação não ambígua.
- A configuração local limita 200 Pages, 10.000 subscriptions por Endpoint e 64
  campos por objeto/Page.
- O agrupamento de subscriptions por Page deixou de fazer um filtro integral para
  cada Page.
- Um webhook inteiro admite no máximo 1.000 itens somando `changes` e `messaging`,
  não 1.000 por entrada.
- A defesa secundária de payloads de consumers rejeita fragmentos de nomes de chave
  sensíveis (`access_token`, `authorization`, `cookie`, `password`, URL etc.), não
  somente o nome exato; chaves/valores inválidos em UTF-8 e chaves com controles/DEL
  também falham fechado.

### 6. Ciclo de vida e observabilidade da configuração

- Endpoint e Page recusam injeção de identidade, revision, estado observado e UUID
  de fila no `create`; esses campos só podem ser produzidos pelo core.
- Criar Endpoint/Page sob App já pausado projeta imediatamente `AppPaused`; retomar
  o App os devolve a `unknown` até readback real.
- Mudança material do App invalida tanto Endpoint quanto Pages ativas. Uma Page não
  conserva `in_sync` de uma revision de credencial anterior.
- Erro/resultado incerto de Page limpa `observed_fields_json`; a interface não
  mistura observação antiga com timestamp de verificação novo.
- O POST assinado do webhook não depende da disponibilidade do verify token usado
  somente no challenge GET. Uma falha desse segredo não derruba ingress autenticado
  por HMAC; challenge e reconciliação continuam exigindo o token.

## Migration

Foi criada `meta_webhook_base/migrations/16.0.1.4.0/pre-migration.py` para:

1. remover a definição anterior da constraint `endpoint_asset_unique`, permitindo
   que o ORM a recrie com `object_type` no namespace;
2. remover a coluna morta `meta_webhook_page.queue_job_uuid`.

A migration é não destrutiva para ledgers: nenhuma delivery, item ou dispatch é
apagada. A mudança de unicidade apenas separa namespaces que antes colidiam.

## Pontos analisados e mantidos

### Snapshot Google sem lock durante I/O

Não é TOCTOU pendente. `google.api.identity._resolve_runtime()` exige a revision
esperada, lê um snapshot coerente sem manter lock durante rede e os consumidores
revalidam a revision antes de projetar. Manter `FOR SHARE` durante OAuth/Search
bloquearia rotação de credencial por toda a latência externa.

### Locks Meta durante mutação de subscription

Foi mantido um fence compartilhado de configuração. Ao contrário da leitura
analítica Google, a reconciliação faz mutações remotas cuja coerência depende de
Endpoint/App/Page permanecerem estáveis na transação. O antigo lock exclusivo do
Endpoint foi removido porque bloqueava o ingresso público; `FOR SHARE` mantém o
fence contra escrita e um advisory lock separado evita I/O duplicado entre workers.

### Ledgers sem unlink e configuração archive-only

É política intencional, não legado. Deliveries/items/dispatches são evidência
imutável. Endpoint/Page/assets/subscriptions são identidade técnica referenciada por
essa evidência e por consumidores; arquivar preserva integridade e auditabilidade.

### ACL somente para administrador de sistema

É adequado ao core técnico. Todos os modelos persistentes têm ACL explícita para
`base.group_system` e regras por `company_ids`; ingress público resolve uma chave
global imprevisível sob `sudo` e autentica o corpo por HMAC antes de persistir.
Consumidores devem expor suas próprias telas operacionais, nunca conceder acesso
direto a este ledger.

### Sanitizer allow-list pequeno

É deliberado. O base persiste somente o envelope comum mínimo e Lead Ads conhecido;
mensageria é sanitizada por cada consumer no mesmo request e validada novamente pelo
dispatcher. Expandir o base para todo payload Meta recriaria domínio de conversa ou
marketing dentro do Integration Core.

### Compatibilidade da nova taxonomia de rate limit

Não apareceu regressão nos consumidores atuais. O inventário de
`contact_center_meta` e `marketing_center_meta` confirma que eles testam/capturam
`MetaApiRateLimitError` antes de `MetaApiTransientError` (ou usam `isinstance`) e
tratam `MetaApiPausedError` separadamente. Portanto tornar rate limit transiente
remove uma pausa indevida sem esconder a distinção operacional de throttling.

## Riscos/gates externos ao patch

1. Os scripts de release fora deste repositório ainda fixam versões anteriores
   (`meta_api_base 16.0.1.1.0`, `meta_webhook_base 16.0.1.3.0`,
   `google_api_base 16.0.1.1.1`) e contagens antigas (39/41/39). Eles devem ser
   atualizados pelo owner do pipeline antes da próxima execução canônica.
2. O release deve provar upgrade a partir de `meta_webhook_base 16.0.1.3.0`, além de
   instalação limpa, para exercitar a nova pre-migration.
3. O smoke deve confirmar que nenhum menu/app **Meta Webhooks** reaparece após o
   upgrade.
4. Não houve chamada real aos provedores nesta revisão. As suítes usam doubles e o
   gate de laboratório deve permanecer sem tráfego externo.

## Validação local executada

- `black --check`: 62 arquivos, limpo;
- `isort --check-only`: limpo;
- `flake8` (`max-complexity=16`): limpo;
- `pylint-odoo` com a configuração do Contact Center: limpo;
- `python -m compileall`: limpo;
- `git diff --check`: limpo;
- manifests, arquivos `data`, XML e CSV referenciados: verificados estaticamente;
- busca de menus: nenhum `<menuitem>` ou `web_icon` nos três addons atuais; apenas a
  migration histórica `16.0.1.3.0` referencia `ir.ui.menu` para limpeza.
- inventário AST: 42 + 45 + 73 = 160 métodos `test_*`; inventário validado, não
  executado sem runtime Odoo local.

## Gate recomendado

**GO para o release canônico de laboratório após atualizar os pins/contagens do
pipeline; NO-GO para deploy direto sem executar install + upgrade replay + testes
Odoo.**

## Release canônico de laboratório — 2026-09-04

O gate acima foi executado no SERVIDOR05 e terminou como
`applied_and_validated`. Os três addons passaram em bancos isolados e foram
aplicados e reaplicados no banco principal para provar idempotência:

- `meta_api_base 16.0.1.1.1`: 45/45 testes;
- `meta_webhook_base 16.0.1.4.2`: 73/73 testes;
- `google_api_base 16.0.1.1.2`: 42/42 testes;
- total: 160/160, sem falha nem erro;
- 81 arquivos remotos conferidos contra o hash da árvore
  `5f06506eac8f64177eaad8fd7d83992f9f802de8cce3df27c301e36d4a28f9f7`;
- quantidade de módulos instalados permaneceu invariável;
- Odoo e DB manager voltaram a `running`, com HTTP privado e público 200;
- a rota exclusiva de laboratório foi restaurada e produção não foi tocada.

Evidência:
`scans/raw/20260903-odoo16-integration-core-greenfield-closeout/release/20260904T025541471633Z/summary.json`.

O release prova o estado instalado imediatamente anterior para cada addon e o
replay da versão final. Como o laboratório já não estava em `meta_webhook_base
16.0.1.3.0`, ele não deve ser apresentado isoladamente como nova prova direta de
todo o salto histórico `1.3.0 -> 1.4.2`; esse limite não afeta a validação da
árvore corrente.
