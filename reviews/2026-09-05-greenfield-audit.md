# Auditoria greenfield — Marketing Center

Data: 2026-09-05. Fonte inicial: `b86b4a3`, branch `16.0`, sem alterações locais.

## Escopo e parecer

Foram inspecionados os dezoito addons deste repositório e os seis addons do Contact
Center. O foco foi arquitetura/ORM, isolamento por empresa e fonte, ledgers, atribuição,
reconciliação de histórico, concorrência, controllers, integrações e capacidade de
instalar a release limpa.

A modularidade atual é justificável: provedores, Website, CRM, vendas, contabilidade e
bridges introduzem dependências opcionais reais. O metapacote `marketing_center_suite`
continua sendo o ponto de instalação completa. O grafo conjunto tem 38 dependências
internas, sem ciclos. Não foram criadas novas tabelas, camadas ou dependências Python
para as correções.

## Achados corrigidos

| Prioridade | Problema e efeito                                                                                                                                                                | Correção                                                                                                                                               |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Alta       | Campos internos de sequência/empresa protegidos em `write` eram aceitos em criação ou via `default_*` do contexto.                                                               | CRM, vendas e contabilidade protegem também criação/defaults; o claim técnico da conciliação segue a mesma regra.                                      |
| Alta       | Captura ao vivo ocorre antes de `mail.thread._track_finalize`; reconciliação posterior podia gerar nova identidade de evento para o mesmo fato e duplicar contagens/receita.     | Snapshot registra o limite do histórico anterior à captura ao vivo; o backfill só considera tracking anterior a essa fronteira. Sem novo campo/tabela. |
| Alta       | Cancelar um pedido preexistente podia criar confirmação sintética diferente da confirmação histórica; um cancelamento histórico também podia tentar reverter confirmação futura. | Reuso da identidade do tracking original e pareamento restrito à cronologia da ocorrência. Testes incluem confirmação/cancelamento/reconfirmação.      |
| Média      | Backfill de um lead convertido podia inventar fato de criação conforme o tipo atual.                                                                                             | Preservação da evidência de criação existente.                                                                                                         |
| Alta       | Retry de claim WhatsApp em outro segundo conflitava consigo mesmo porque `occurred_at` gerado no servidor entrava novamente no digest.                                           | Primeiro timestamp aceito permanece canônico; payload alterado continua rejeitado.                                                                     |
| Alta       | Emissão de redirect podia falhar depois de gravar o ingresso, deixando evento parcial quando o controller retornava 503.                                                         | Savepoint envolve criação do evento e grant; regressão confirma rollback e retry do mesmo UUID.                                                        |
| Média      | Concorrência na primeira admissão Meta Lead Ads podia falhar sob lock ocupado ou vencedor fora do snapshot.                                                                      | Sinalização `40001` pede retry transacional completo; dois testes usam cursores PostgreSQL independentes e provam deduplicação.                        |
| Média      | OAuth Google service account consumia corpo sem limite e podia transformar indisponibilidade temporária em pausa permanente.                                                     | Resposta em streaming limitada a 64 KiB, timeout, endpoint fixo, fechamento explícito e taxonomia compartilhada antes do SDK.                          |
| Média      | Credencial montada como FIFO bloqueava o worker antes da validação de arquivo regular.                                                                                           | Abertura não bloqueante nos fundamentos Google e Meta, mantendo validação de caminho/tipo/tamanho.                                                     |
| Média      | JSON profundamente aninhado escapava da resolução da credencial Google.                                                                                                          | Erro estável de credencial e tratamento do limite de recursão.                                                                                         |
| Média      | Paginação Meta seguia `cursors.after` mesmo na última página.                                                                                                                    | Só avança se `paging.next` existe; cursores isolados não indicam outra página.                                                                         |
| Alta       | Conflitos transacionais no webhook/fanout/consumidor/reconciliação podiam esgotar tentativas de negócio e marcar trabalho como morto.                                            | `OperationalError` alcança o retry nativo da transação, inclusive no teto de tentativas de negócio.                                                    |
| Alta       | Webhook compartilhado Meta consumia o corpo antes de um retry Odoo e podia reler vazio.                                                                                          | Corpo limitado preservado no cache da requisição; mesma correção do defeito reproduzido sob carga WuzAPI.                                              |
| Média      | Timeout do frontend WhatsApp terminava ao receber headers; um corpo de resposta parado podia bloquear a navegação indefinidamente.                                               | Prazo abrange fetch e leitura do JSON, com fallback mesmo sem `AbortController`. Regressões QUnit simulam corpo que nunca termina.                     |
| Baixa      | Teste de assinatura adulterada podia manter o mesmo caractere final e falhar aleatoriamente.                                                                                     | Fixture sempre troca o caractere; a verificação HMAC do produto permanece intacta.                                                                     |

As correções de backfill foram revisadas por um segundo agente. Essa revisão encontrou
mais dois cenários históricos de venda e asserts de testes deslocados; ambos foram
corrigidos antes do fechamento.

## Limpeza de release

Os dezoito manifests agora declaram `16.0.1.0.0`, alinhados aos seis addons CC.
Expectativas de versões, dependências e contagens de testes dos runners foram
atualizadas conjuntamente. A CI Marketing instala explicitamente `contact_center_kanban`
antes de `contact_center_crm`; ambos os workflows usam revisões imutáveis publicadas que
contêm as dependências necessárias.

O input opcional `peer_ref` do `workflow_dispatch` aceita somente SHA completo e permite
validar os commits finais dos dois repositórios sem reescrever o workflow. A CI usa
PostgreSQL 16, alinhada ao runtime validado; PostgreSQL 12 já está fora de suporte
conforme a [política oficial](https://www.postgresql.org/support/versioning/).

Versões de DTO/mapping foram preservadas: descrevem contratos atuais, não migrações de
banco. `legacy_adgroup_hint` é diagnóstico recebido do provedor, não um segundo caminho
de negócio. Remover ou renumerar esses itens não acrescentaria benefício operacional e
criaria mudanças desnecessárias.

Nenhum addon contém diretório de migração pre-production. Instalação limpa é a origem
suportada desta baseline. Alterações persistentes posteriores à primeira produção
voltarão a exigir migrações cumulativas normais.

## Concorrência HTTP já correta no Marketing funcional

O defeito de releitura vazia identificado no WuzAPI não se estende aos controllers
funcionais do Marketing. Web Ingress e Website Action já usavam `get_data(cache=True)` e
testes HTTP comprovam o mesmo corpo após `40001`:

- `test_serialization_signal_replays_the_same_bounded_request_body`;
- `test_serialization_retry_replays_the_same_bounded_action_body`;
- `test_native_create_and_serialization_retry_produce_one_correlation`.

Essa implementação foi mantida. Os fundamentos técnicos compartilhados Meta receberam a
correção onde o contrato ainda não era seguido.

A etapa seguinte do ensaio Contact Center expôs uma segunda falha de deduplicação,
também presente no fundamento Meta: capturar a violação de unicidade e buscar novamente
no mesmo snapshot `REPEATABLE READ` não permite enxergar um vencedor concorrente recém
confirmado. Para a constraint de deduplicação específica, a ausência do vencedor agora
sinaliza `40001` e solicita retry completo do Odoo. Outras violações de integridade
mantêm sua classificação original. A regressão usa conflito SQL real e replay HTTP.

## Validação e evidência

Evidência privada central: `scans/raw/20260905-centers-greenfield-audit/` no repositório
de infraestrutura.

| Verificação                    | Resultado                                                                                              |
| ------------------------------ | ------------------------------------------------------------------------------------------------------ |
| Contratos Web Ingress          | 18 testes direcionados passaram.                                                                       |
| QUnit Website                  | 14 testes/59 assertions, tanto minificados quanto debug; zero falhas/ignorados.                        |
| Navegador                      | Dashboard nativo carregou sem erro de página em 1440×1000; smoke de renderização com dados sintéticos. |
| Pilha TLS                      | Imports reais Google Auth, cryptography, OpenSSL e bridge urllib3 passaram com `constraints.txt`.      |
| Pre-commit                     | Hooks obrigatórios completos passaram.                                                                 |
| Instalação limpa dos 24 addons | 1.679 testes, zero falhas e zero erros.                                                                |
| Replay de atualização          | Todos os 24 addons instalados em `16.0.1.0.0` após atualização separada.                               |

O runtime usa fonte OCB copiada em leitura do laboratório, Python 3.10 e PostgreSQL 16
descartável. Bibliotecas binárias compatíveis com ARM64 diferem em alguns pins do host
do laboratório; versões exatas estão na evidência.

Uma tentativa de repetir `at_install` durante upgrade de banco completo expôs colunas
NOT NULL antes de carregar os addons donos delas no registry parcial. Os 16 erros foram
classificados integralmente: oito de `account`, seis de CC e dois de `mail`. Não foram
introduzidas dependências falsas nas fixtures. A instalação limpa final e o replay de
upgrade separado passaram, conforme `release-validation.json`.

O conjunto ganhou 37 testes Python e três JavaScript. Os scripts de operação passaram
180 testes. O ensaio integrado com o Contact Center foi aprovado integralmente em
`local-operational/20260905T173155804033Z/summary.json`: 4.800 mensagens aceitas sem
erro, 100 pares deduplicados, 5.161 eventos concluídos, indisponibilidade/recuperação,
fila esvaziada e ausência de reenvio nos três resultados incertos simulados. A
conferência inclui o registro de chamadas do provedor simulado. Isso valida o fluxo
funcional local; capacidade no hardware alvo e integração com provedores reais seguem os
gates de release.

Os resultados anteriores dos reviews são históricos e não foram reutilizados como prova
da árvore corrigida. Não houve publicação, deploy ou alteração de produção. A promoção
deve fixar os commits finais de ambos os repositórios, executar a CI correspondente e
usar nova janela de implantação; a janela de 2026-09-05 07:00–10:00 BRT expirou com gate
operacional incompleto. Rate limit permanece adiado por decisão do operador.
