# Release — Lead Ads, financeiro e visão gerencial

Data: 2026-09-01 Ambiente: `odoo16-teste.soloz.com.br` / servidor05 / base neutralizada

## Resultado

O corte foi aplicado e validado com sucesso. Produção não foi tocada.

- `marketing_center_base` `16.0.1.5.0`;
- `marketing_center_contact_center` `16.0.2.1.0`;
- `marketing_center_crm` e `marketing_center_contact_center_crm` `16.0.1.0.0`;
- `marketing_center_meta` `16.0.2.0.0`;
- `marketing_center_sale`, `marketing_center_account`, `marketing_center_sale_account` e
  `marketing_center_dashboard` `16.0.1.0.0`.

## Gates

- base isolado: 92/92 testes;
- conjunto integrado: 320/320 testes;
- instalação/upgrade offline: concluído;
- replay de upgrade: concluído;
- bases e containers efêmeros removidos;
- rota Traefik restaurada byte a byte;
- HTTP privado e público: 200;
- console do navegador no dashboard e nas rotas Lead Ads: sem erro.

Dois primeiros candidatos foram revertidos automaticamente pelo gate: o primeiro
detectou alias SQL reservado no dashboard; o segundo detectou o tratamento incorreto dos
defaults neutros da herança delegada `account.payment` → `account.move` e um
`assertRaises` incompatível com o runner do Odoo. Ambos foram corrigidos antes do
release aceito.

## Evidência canônica

`scans/raw/20260901-odoo16-marketing-center-leads-finance-dashboard/release/20260901T122900890762Z`

`summary.json` registra `status=applied_and_validated`, hashes dos logs, versões,
contagens, replay, saúde HTTP, locks exclusivos e `production_touched=false`.

## Próximo corte

1. instalar/testar `google_api_base` isoladamente;
2. implementar `marketing_center_google` estritamente read-only;
3. validar credenciais Meta Ads/Lead reais;
4. iniciar captura first-party antes dos exporters de conversão.
