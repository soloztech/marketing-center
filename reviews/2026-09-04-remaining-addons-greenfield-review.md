# Marketing Center — revisão greenfield dos seis addons restantes

Data: 2026-09-04  
Escopo: bridges de Contact Center/CRM/Website e Web Ingress  
Natureza: revisão estática consolidada, transição validada e primeiro baseline
greenfield sem migrations aplicado no laboratório

## Escopo

Esta rodada completa o inventário módulo a módulo que não havia sido aplicado com a
mesma profundidade aos seguintes addons:

- `marketing_center_contact_center`;
- `marketing_center_crm`;
- `marketing_center_contact_center_crm`;
- `marketing_center_web_ingress`;
- `marketing_center_website`;
- `marketing_center_website_crm`.

Versões e testes observados na árvore atual:

| Addon | Versão | Métodos `test_*` por AST |
| --- | --- | ---: |
| `marketing_center_contact_center` | `16.0.3.3.0` | 36 |
| `marketing_center_crm` | `16.0.1.3.1` | 15 |
| `marketing_center_contact_center_crm` | `16.0.1.2.1` | 24 |
| `marketing_center_web_ingress` | `16.0.2.1.1` | 47 |
| `marketing_center_website` | `16.0.2.2.1` | 39 |
| `marketing_center_website_crm` | `16.0.1.5.2` | 37 |

As contagens são do código consolidado. Tanto a transição quanto a árvore final sem
migrations possuem execuções independentes no Odoo, documentadas na seção de
release.

## Veredito executivo

Os seis addons preservam separações justificadas por dependências opcionais. Não há
motivo técnico para fundi-los: isso tornaria `crm`, `website`, `website_crm` e Contact
Center dependências obrigatórias do mesmo pacote.

A revisão confirmou uma omissão real: as regras de segurança do
`marketing_center_crm` estavam congeladas por `noupdate=1`. A transição do laboratório
foi corrigida com:

- remoção do `noupdate` do arquivo de regras;
- um pre-migration transitório `16.0.1.3.1` que tornou atualizáveis exatamente os
  **15** XML IDs de `ir.rule` pertencentes ao addon;
- inclusão dessas regras no contrato de regressão da facade;
- bump para `16.0.1.3.1`.

Depois de executar e comprovar essa transição no SERVIDOR05, o hook histórico foi
retirado junto com as demais migrations pré-baseline. A correção efetiva permanece
no XML corrente; não se mantém infraestrutura de upgrade para versões que nunca
serão baseline produtivo.

O número correto é 15, não 14: quatro famílias de links/revogações/eventos, seus
escopos de vendedor/all-leads/system, mais as três regras de equivalência de merge.

Não foi encontrada outra violação atual de segurança, empresa, idempotência ou
concorrência que justifique patch imediato.

## Resultado por addon

### `marketing_center_contact_center`

- links de atribuição e ledgers de resposta são imutáveis;
- cursores são internos, únicos por conversa e validam que todos os ponteiros
  permaneçam na mesma empresa/conversa;
- materialização histórica possui cutoff persistente e páginas seek; sinais live não
  ultrapassam a fronteira histórica;
- confirmação tardia de um outbound antigo não fecha um episódio iniciado depois;
- bootstrap é paginado e rejeita cursor que não avance.

O código transitório continha os estados `legacy_pending`, `legacy_materializing`,
`legacy_draining` e o campo `legacy_consumed_signal_id` para drenar cursores antigos.
Antes da remoção foram verificados no banco: ausência de cursores em estado legacy,
ausência de floors legados, cobertura completa de cursores/sinais, igualdade das
fronteiras materializadas e convergência dos jobs relacionados. Só então a versão
`16.0.3.3.0` removeu o runtime e o campo legado. O fluxo corrente possui uma única
semântica de materialização, sem ramo de compatibilidade morto.

O scheduler do bootstrap/live usa rotação persistente, em vez de selecionar sempre
os primeiros 25/50 registros. Cada job também reancora o contexto à empresa exata do
binding. Portanto, as críticas externas sobre starvation e vazamento de empresa
eram procedentes para uma revisão histórica, mas estão corrigidas na árvore atual.

### `marketing_center_crm`

- mudanças relevantes de `crm.lead` são serializadas sob `FOR UPDATE`;
- `marketing_event_company_id` ancora a empresa na primeira evidência e impede mover
  depois um lead com ledger para outra empresa;
- assertions, revogações, equivalências de merge e business events são imutáveis e
  idempotentes por identidade natural;
- o backfill manual é paginado e possui teto síncrono explícito; volumes maiores
  devem usar fila;
- ACLs permitem somente leitura dos ledgers e reaproveitam a visibilidade nativa de
  leads próprios/todos os leads, além do escopo por empresa.

A correção `noupdate` acima é necessária porque segurança precisa ser atualizável em
upgrades. Crons podem permanecer em `noupdate`; regras de autorização, não.

### `marketing_center_contact_center_crm`

- não cria um segundo modelo de equipe nem uma identidade paralela de CRM;
- o antigo fan-out M×N síncrono foi substituído por jobs paginados e resumíveis;
- cada job fixa um lado da relação e percorre o outro com cursor monotônico;
- jobs reancoram `allowed_company_ids` e `with_company` na empresa dona;
- advisory lock por empresa/conversa serializa a projeção;
- revogação ocorre antes do desaparecimento do case link; reconciliação posterior é
  idempotente.

Não há modelos persistentes próprios que exijam outra ACL: o addon estende serviços,
empresa e hooks dos dois bridges.

### `marketing_center_web_ingress`

- valida tamanho, origem, host, replay window e proveniência antes de persistir;
- dedupe usa advisory lock por Endpoint/event hash, constraint natural e savepoint;
- evento, click IDs e touchpoint são gravados na mesma transação;
- o `sudo` está reancorado à empresa do Endpoint;
- o ledger de admission é payload-free e apagado em lotes após cinco minutos.

O rate limiter interno deliberadamente faz count seguido de insert sem serializar
todos os visitantes. Um burst concorrente pode exceder o teto pelo número de
transações Odoo simultâneas. Essa escolha só é aceitável com rate limit autoritativo
no proxy/edge; sua presença e configuração são gate de implantação.

`protected_value` ainda é um `Char` recuperável, restrito ao administrador de
Marketing. Click IDs não são senha, mas são identificadores correlacionáveis e hoje
não possuem retenção. Isso integra o gate LGPD descrito abaixo.

### `marketing_center_website`

- apenas elementos explicitamente marcados/configurados são interceptados;
- a falha de telemetria continua no `href` original validado e same-origin, não em
  `/` nem em URL externa arbitrária;
- grants de redirect são capabilities opacas, de uso único, com expiração e purge em
  lote;
- alteração da empresa do Website é impedida quando quebraria bindings existentes;
- o formulário nativo permanece a autoridade: erro de atribuição não impede a
  criação nativa.

Assim, o claim do parecer sobre perda do destino original era válido historicamente,
mas está corrigido no estado atual.

### `marketing_center_website_crm`

- o manifest depende explicitamente de `website_crm`;
- o controller usa MRO cooperativo entre o wrapper de recibo e o controller nativo
  Website CRM, preservando telefone, geolocalização, visitor e hooks de lead;
- correlação e intent validam Website, Endpoint, ação, lead e empresa;
- intents têm propriedade exata de job, retry limitado, estado terminal e recuperação
  administrativa;
- os dois crons usam scheduler rotativo transacional em lanes separadas e reancoram
  a empresa antes de reconciliar.

O claim de que o bridge herdava somente o controller genérico e não dependia de
`website_crm` também era histórico; não procede na árvore atual.

## Concorrência, cursores e `sudo`

Foram revisados os caminhos que cruzam transação/fila:

- cursores de backfill são monotônicos e falham se não avançarem;
- jobs com efeito persistido validam identidade e propriedade antes do efeito;
- locks de lead, conversa, Endpoint e intent seguem escopos determinísticos;
- os serviços públicos elevam privilégio apenas após resolver uma capability ou
  configuração opaca e reancoram a empresa explicitamente;
- as regras dos modelos persistentes são associadas a grupos e limitadas por
  `company_ids`;
- não foi encontrado `sudo()` que transformasse input público em busca irrestrita de
  registros por ID fornecido livremente.

## Retenção e privacidade

A ausência de retenção completa é um gate verdadeiro, mas não deve ser resolvida com
`unlink` genérico dos ledgers. O desenho precisa classificar:

1. payload/identificador pessoal que pode ser apagado;
2. hash ou prova mínima necessária a dedupe e auditoria;
3. prazo e base legal por classe de dado;
4. legal hold;
5. purge paginado, idempotente e observável;
6. efeito da remoção sobre views e projeções efetivas.

Prioridade especial deve ser dada aos click IDs protegidos no Web Ingress e a
qualquer snapshot que permita reidentificação indireta.

Os **3** registros `ingress_provenance='unclassified'` existentes no laboratório
foram preservados por serem evidência histórica imutável, não configuração runtime.
O código atual não usa `unclassified` como default e rejeita a criação de novos
eventos nessa condição. Apagá-los apenas para obter uma contagem zero destruiria
evidência sem melhorar o comportamento futuro; sua anonimização/expiração deve
seguir a política de retenção acima.

## Squash do primeiro baseline greenfield

Como ainda não existe baseline produtivo do Marketing Center, migrations acumuladas
durante o desenvolvimento seriam legado artificial no primeiro release. A remoção,
porém, só ocorreu **depois** da transição e dos gates de convergência no SERVIDOR05:

- foram removidos **33 hooks de migration em 12 addons**;
- o instalador/release agora rejeita a presença de qualquer diretório `migrations/`;
- instalação limpa passa a nascer diretamente no schema e nos dados declarativos
  correntes;
- versões anteriores não são aceitas como origem de upgrade pelo primeiro baseline.

Isso não equivale a suportar upgrades antigos sem migration. É uma decisão explícita
de baseline: quando a primeira versão entrar em produção, migrations futuras voltam
a ser obrigatórias e não poderão ser descartadas.

## Dashboard: disposição do claim de performance

O dashboard está fora destes seis addons, mas o claim foi confrontado porque apareceu
no mesmo parecer. A view SQL é grande e merece teste de escala; tamanho de arquivo não
é, por si, defeito.

No laboratório, o plano completo foi medido em 18,414 ms com 310 métricas, 229
touchpoints efetivos, 40 resolutions e 3.945 eventos. Isso refuta um problema atual,
mas não certifica cardinalidade produtiva. Antes de produção deve ser repetido
`EXPLAIN (ANALYZE, BUFFERS)` com volume representativo e orçamento explícito. Não se
recomenda split cosmético sem evidência do plano de execução.

## Complexidade e manutenção

Existem métodos extensos nos serviços de resposta, CRM, ingress e Website CRM, mas a
árvore conhecida permanece dentro do limite de complexidade configurado. A ação
adequada é extrair etapas quando uma mudança funcional tocar esses fluxos, mantendo
transação e invariantes visíveis; quebrar arquivos apenas por contagem de linhas
criaria indireção sem reduzir risco.

## Runtime/release

### Transição de limpeza — validada

O release transitório que drenou o legado antes do squash concluiu com
`status=applied_and_validated` no SERVIDOR05:

- `marketing_center_base`: **123/123**;
- suíte integrada: **660/660**;
- Website/HTTP: **123/123**;
- facade `marketing_center_suite`: **5/5**;
- `marketing_center_contact_center`: **36** testes dentro da suíte integrada;
- upgrade e replay offline com status zero;
- zero job ativo, falho, inesperado ou projeção falha;
- QUnit Website minificado e `debug=assets`: **12/12 testes** e **49/49
  assertions** em cada modo;
- HTTP privado e público 200, rota restaurada e produção não tocada;
- 379 arquivos, árvore
  `6fecb754613568ddd3cdbad38e46bcb0adad46eb7aa5e43270c91b40d69d6552`.

Evidência:
`scans/raw/20260903-odoo16-marketing-center-remaining-addons-closeout/release/20260904-stage-b-direct-r2/summary.json`.

### Primeiro baseline sem migrations — validado

Depois dessa prova foram retirados os 33 hooks históricos. Uma segunda execução
canônica validou a árvore final, já sem diretórios `migrations/`, com
`status=applied_and_validated`:

- **123/123** testes base, **660/660** integrados, **123/123** Website/HTTP e
  **5/5** da facade;
- upgrade e replay offline com status zero;
- zero job ativo, falho, inesperado ou projeção falha;
- QUnit minificado e `debug=assets`: **12/12 testes** e **49/49 assertions** em
  cada modo;
- HTTP privado e público 200, rota restaurada e produção não tocada;
- 346 arquivos, árvore
  `ecc5e80744ba17a2133cc3fe4d4ff7f50ea3e180a1ec5cfda59ee50b00a5543e`.

Essa primeira execução sem migrations fica preservada como evidência histórica do
squash:
`scans/raw/20260903-odoo16-marketing-center-remaining-addons-closeout/release/20260904-final-no-migrations/summary.json`.

Depois que Contact Center e Integration Core também fixaram seus baselines finais,
o mesmo Marketing Center foi liberado novamente contra essas dependências. A árvore,
as versões e as contagens permaneceram idênticas; apply/replay, filas, QUnit e HTTP
passaram outra vez. Essa execução posterior é a evidência canônica integrada:
`scans/raw/20260903-odoo16-marketing-center-remaining-addons-closeout/release/20260904-final-after-contact-integration/summary.json`.

A configuração autoritativa do rate limiter no edge e a política LGPD/retenção são
gates de produção separados; não se tornam verdadeiros apenas porque o release de
software passa.

Com a evidência integrada final, a revisão, a transição e o primeiro baseline
greenfield do Marketing Center estão concluídos no SERVIDOR05 contra os baselines
definitivos dos três repositórios. Isso não autoriza produção nem fecha os gates
operacionais e de privacidade listados acima.
