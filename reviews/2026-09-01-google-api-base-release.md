# Release — Google API Base

Data: 2026-09-01 Ambiente: `odoo16-teste.soloz.com.br` / servidor05 / base neutralizada

## Resultado

`google_api_base` `16.0.1.0.0` foi instalado como terceiro addon do `integration-core`,
depois de `meta_api_base` e `meta_webhook_base`.

- `meta_api_base`: 40/40 testes;
- `meta_webhook_base`: 39/39 testes;
- `google_api_base`: 33/33 testes;
- instalação e replay offline concluídos;
- nenhum módulo removido ou instalado além de `google_api_base`;
- bases/containers efêmeros removidos;
- rota Traefik restaurada byte a byte;
- HTTP privado e público 200;
- produção intocada.

Evidência canônica:
`scans/raw/20260901-odoo16-integration-core/release/20260901T124740581851Z`.

O próximo consumer é `marketing_center_google`, estritamente read-only. Credencial,
health, fila, catálogo e métricas permanecem no domínio consumidor; OAuth/transporte,
quota e referências externas de segredo permanecem nesta base técnica.
