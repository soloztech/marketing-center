# Integration Core — fechamento greenfield

Data: 2026-09-04  
Escopo: `meta_api_base`, `meta_webhook_base`, `google_api_base`  
Natureza: revisão estática, baseline greenfield e evidência de laboratório  
Estado: `applied_and_validated` na árvore final sem migrations

## Veredito

Os três addons continuam com um limite arquitetural adequado: oferecem identidade,
credenciais referenciadas, transporte, webhook e mecanismos técnicos de fila, sem
absorver conversa, campanha, CRM ou Website. A revisão complementar não encontrou
dependência circular, ACL global, regra global ou quebra atual de isolamento por
empresa.

Parte do parecer independente descrevia problemas reais de versões anteriores, mas
não o estado atual da árvore. Em especial, o lock exclusivo da reconciliação Meta,
os jobs sem propriedade exata e os limites insuficientes de payload/paginação já
foram corrigidos. Esses apontamentos são úteis como histórico e como regressões a
impedir; não devem ser contabilizados novamente como defeitos abertos.

Versões observadas nesta revisão:

| Addon | Versão | Métodos `test_*` por AST |
| --- | --- | ---: |
| `meta_api_base` | `16.0.1.1.1` | 45 |
| `meta_webhook_base` | `16.0.1.4.2` | 73 |
| `google_api_base` | `16.0.1.1.2` | 42 |

As contagens são inventário estático. A execução canônica que cobriu esses 160 testes
é discriminada ao final, sem converter o laboratório em evidência de produção.

## Confronto dos principais claims

### Lock Meta durante chamadas externas

**Válido historicamente; corrigido no estado atual.**

O worker de subscriptions agora:

1. serializa apenas outros workers do mesmo Endpoint com advisory lock
   transacional;
2. comprova a propriedade exata do `job_uuid` sob `FOR SHARE`;
3. mantém um fence compartilhado contra alteração da configuração durante a mutação
   remota;
4. revalida revisões antes de projetar o resultado.

O challenge e o webhook público também usam leituras compartilhadas. Portanto o I/O
Meta não bloqueia o ingresso público, enquanto uma rotação/pausa concorrente continua
corretamente impedida de atravessar uma mutação remota iniciada sob outra revisão. A
recomendação genérica de eliminar qualquer lock durante rede não se aplica a essa
mutação externa; fazê-lo reintroduziria TOCTOU.

### Crons e filas

Delivery e dispatch órfãos são selecionados em lote, por ordem determinística, com
`FOR UPDATE SKIP LOCKED`. A reconciliação de subscriptions limita o lote, exclui jobs
ativos e atualiza a data de observação tanto em sucesso quanto em erro, fazendo os
Endpoints rotacionarem pela ordenação temporal. Workers de subscription, delivery e
dispatch exigem UUID persistido e idêntico ao UUID da execução.

Não foi encontrada starvation reproduzível nesse core. O scheduler rotativo citado
no parecer foi aplicado aos crons dos conectores do Marketing Center que antes
selecionavam repetidamente os primeiros IDs; não é requisito substituir consultas
ordenadas que já avançam sua própria fronteira operacional.

### Runtime Google

O runtime Google é resolvido como snapshot sem manter lock durante OAuth/API. A
revision esperada é obrigatória e os consumidores revalidam antes de projetar o
resultado. Esse desenho é intencional: segurar até `FOR SHARE` pela latência externa
bloquearia rotação de credenciais sem aumentar a segurança do efeito.

## Segurança e ciclo de vida

- os três modelos de configuração são acessíveis apenas a `base.group_system`;
- todas as regras persistentes são associadas explicitamente a grupo e limitadas por
  `company_ids`;
- o ingresso público resolve uma referência opaca sob `sudo`, autentica o request e
  só então persiste o envelope sanitizado;
- capabilities internas são process-local e recusam serialização;
- Endpoint, Page, Asset e Subscription são archive-only porque são identidades
  referenciadas; Delivery, Item e Dispatch são evidência imutável.

## Primeiro baseline produtivo e migrations

A orientação anterior de preservar migrations pré-produtivas ficou obsoleta após a
decisão explícita de fixar o primeiro baseline produtivo no estado atual. As
migrations `meta_webhook_base/16.0.1.3.0` e `16.0.1.4.0` foram removidas junto com o
diretório `migrations/`: elas serviam apenas para atravessar estados que nunca foram
colocados em produção.

O contrato canônico deste baseline agora exige:

- ausência de diretório `migrations/` nos três addons;
- manifests exatamente nas versões listadas acima;
- recusa de uma linhagem pré-produtiva como origem de upgrade;
- instalação limpa para uma base nova e upgrade/replay idempotente apenas sobre o
  baseline atual do laboratório.

Esse contrato proíbe `migrations/` na árvore greenfield atual; não autoriza apagar
histórico depois do primeiro go-live. Uma eventual política pós-baseline deverá ser
decidida e versionada explicitamente antes da primeira mudança persistente de schema
ou dados. O squash atual é uma operação única de fundação, não um mecanismo
recorrente de release.

## Gates ainda abertos

### Retenção e privacidade

O core ainda não possui uma política completa de retenção. Isso não autoriza apagar
ledgers imutáveis de forma genérica: o desenho produtivo deve distinguir payload ou
identificador apagável da prova mínima necessária para dedupe, replay e auditoria,
incluindo `retain_until`, legal hold, purge paginado e observabilidade.

### Escala e provedores reais

Os testes conhecidos usam doubles para transporte. Antes de produção permanecem
necessários ensaios controlados de rate limit, timeout, cursor repetido e resultado
incerto contra ambientes reais dos provedores, sem registrar segredo ou payload
privado na evidência.

Delivery, dispatch e subscription compartilham hoje o canal `root.meta_webhook`.
Isso não recria o antigo bloqueio do ingresso HTTP — o callback pode persistir —,
mas reconciliações lentas podem disputar workers e conexões com o fan-out. O gate de
escala deve, por isso, medir também latência e backlog por tipo de job e decidir se o
canal necessita reserva ou separação.

## Evidência runtime no SERVIDOR05

O release canônico da árvore final, já sem qualquer diretório `migrations/`, concluiu
no laboratório:

- 45/45 testes de `meta_api_base`;
- 73/73 testes de `meta_webhook_base`;
- 42/42 testes de `google_api_base`;
- 160/160 no total, sem falhas ou erros;
- 79 arquivos verificados e hash de árvore
  `cec6ebd0d95a8797430163be30e1c25f834cb4de7532cc2e65eca463d766b3af`;
- apply e replay offline dos três addons com status zero;
- versões instaladas iguais às versões deste documento;
- restauração da rota de teste com HTTP 200;
- `production_touched: false` e nenhuma rota de produção no artefato.

Artefato:
`scans/raw/20260903-odoo16-integration-core-greenfield-closeout/release/20260904-final-no-migrations/summary.json`.

O artefato registra `status: applied_and_validated`, fecha a proveniência do hash
exato do baseline sem migrations e substitui, para fins de release, o artefato
intermediário de hash `5f06506e...`. A evidência valida laboratório e repetibilidade
do apply; não transforma doubles de transporte em certificação de provedor real nem
em prova de capacidade produtiva.
