# Meta Insights daily performance — release 2026-08-31

## Resultado

O corte foi instalado e validado no laboratório `odoo16-teste.soloz.com.br`.

- `marketing_center_base`: `16.0.1.3.0`
- `marketing_center_meta`: `16.0.1.2.0`
- testes isolados base: `53/53`
- testes integrados: `116/116`
- upgrade offline: aprovado
- replay do upgrade: aprovado
- HTTP privado e público: `200`
- produção: não tocada

Evidência:
`scans/raw/20260831-odoo16-marketing-center-meta-insights/release/20260831T045958728100Z`.

## Contrato entregue

- fato diário provider-neutral nos grains `account` e `campaign`;
- impressões, cliques e custo em micros, todos exatos em PostgreSQL `BIGINT`;
- ausência de métrica diferente de zero explícito;
- projeção atual e revisões imutáveis, incluindo sequência A→B→A;
- janela máxima de 31 dias no timezone IANA da source;
- Meta Graph `v26.0`, `time_increment=1`, `time_range` explícito e paginação somente
  pelo cursor `after`;
- oito tentativas por página, teto de 512 páginas, fencing e aplicação atômica de
  página/cursor/job sucessor;
- botão administrativo para os sete dias fechados anteriores.

Não fazem parte deste contrato: reach, actions, action values, conversões,
breakdowns, parâmetros de atribuição, jobs async Meta ou tombstone por varredura
vazia.

## Correção encontrada pelo gate real

O Odoo 16 serializa um objeto JSON vazio como SQL `NULL`. O primeiro teste isolado
detectou que `dimensions_json` havia sido declarado `NOT NULL`. O campo passou a
aceitar `NULL` exclusivamente como representação física de `{}`; `dimension_hash`
continua sendo a identidade canônica. O rollback automático restaurou fonte, rota e
serviço antes da correção e da repetição completa do release.

## Bloqueio externo

Não há perfil Marketing Meta com uma credencial comprovada contendo `ads_read` no
laboratório. O token do Contact Center é de mensageria e não será reaproveitado.
Portanto, o código e a instalação estão validados, mas o sync real aguarda um System
User reader dedicado e escopado somente às ad accounts autorizadas.
