# Release — Website→CRM, dashboard e Google observability

Data: 2026-09-01 Ambiente: `odoo16-teste.soloz.com.br` / servidor05 / base neutralizada
Produção: não tocada

## Resultado

O corte consolidado foi aplicado e validado com sucesso. Os principais componentes
promovidos foram:

- `marketing_center_base` `16.0.1.7.0`;
- `marketing_center_contact_center` `16.0.3.0.0`;
- `marketing_center_crm` e `marketing_center_contact_center_crm` `16.0.1.1.0`;
- `marketing_center_dashboard` `16.0.1.1.0`;
- `marketing_center_google` `16.0.1.1.0`;
- `marketing_center_web_ingress` e `marketing_center_website` `16.0.2.0.0`;
- `marketing_center_website_crm` `16.0.1.2.2`.

O Website continua usando o controller e a criação nativa de `crm.lead`. O bridge não lê
valores do formulário: ele valida um comprovante opaco assinado, cria um intent durável
e correlaciona o lead ao touchpoint first-party. A correlação é evidência M:N e não
crédito causal.

O Google Ads continua estritamente read-only. Change History e Delivery Diagnostics usam
o transporte oficial REST v25, snapshot de revisões, paginação limitada, cooldown, retry
e ledger de observações imutável. Nenhum mutate foi implementado ou executado.

## Gates finais

- base isolado: 109/109 testes;
- conjunto integrado: 496/496 testes;
- Website/Web Ingress/CRM: 89/89 testes;
- instalação offline: concluída;
- replay de todos os upgrades: concluído e idempotente;
- módulos nas versões esperadas: confirmado;
- Odoo e dbmanager: ativos;
- HTTP privado e público: 200;
- rota Traefik: restaurada byte a byte;
- smoke autenticado em Chromium: dashboard, Change History e Delivery Diagnostics
  abriram sem erro de console;
- `production_touched=false`.

Evidência canônica:
`scans/raw/20260901-odoo16-marketing-center-website-crm-google-observability/release/20260901T184433945695Z`.

Hash do conjunto promovido:
`e69e58d689962afb374da343b1a04cde13466190897c7cb72ac6dd2b04e97d07`.

## Falhas encontradas e corrigidas antes do release

As duas tentativas anteriores falharam com segurança antes de alterar a base principal.
Em ambas, o orquestrador restaurou os sources anteriores, religou Odoo/dbmanager e
confirmou HTTP 200.

Quatro sintomas tinham três causas:

1. um teste de recibo HMAC usava formato inválido e esperava a classe de erro errada; o
   teste agora adultera uma assinatura estruturalmente válida;
2. um HttpCase exercitava a ausência de `crm.lead.name`, que no Odoo 16 causa uma falha
   nativa de savepoint; o contrato passou a testar uma conversão inválida de `team_id`,
   preservando o objetivo de comprovar que nenhum lead/evidência é criado;
3. controllers decorados do Odoo convertem o retorno textual do pai em um objeto
   `Response` entre as camadas. O código aceitava somente `str`, descartava o recibo e
   deixava o lead sem intent/correlação. O boundary agora extrai o JSON de `str` ou
   `Response`, altera somente o body pelo `set_data()` e devolve o mesmo objeto,
   preservando status, headers e cookies.

Dois testes específicos para `Response` foram adicionados e o gate Website foi elevado
de 87 para 89 para evitar regressão silenciosa.

## Validação Google real

Após o release, a fonte Google existente recebeu sincronização manual read-only:

- Change History: dez runs concluídos, zero falhas e zero alterações observadas no
  período consultado;
- Delivery Diagnostics: dois runs concluídos, 124 itens recebidos/aplicados e zero
  falhas;
- nenhum run permaneceu `planned`, `queued` ou `running`;
- a UI exibiu os 124 snapshots com status configurado, status primário e severidade.

As 124 linhas representam dois snapshots imutáveis de 62 ativos em instantes distintos.
Elas não são duplicação de uma mesma observação dentro do mesmo run.

## Limites e próximo corte

- custo, impressões e cliques reportados já existem, mas conversões e valor reportados
  pela plataforma ainda não fazem parte do DTO de performance;
- a resolução/correlação disponível ainda não executa automaticamente um modelo de
  atribuição causal;
- `website.visitor`, `link.tracker`, `utm.*`, `fbc/fbp` e migração de identificadores
  legados permanecem incrementos do addon Website;
- forms e datasets/pixels Meta e validação externa com readers dedicados permanecem
  pendentes;
- conversões outbound e mutações continuam deliberadamente desligadas.

Próximo corte recomendado: Performance DTO v2 com conversões e valor em micros, Google
GAQL correspondente, migração/replay e apresentação separada de conversões
reportadas/CPA no dashboard.
