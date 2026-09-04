# Runtime Google service account — validação no SERVIDOR05

Data: 2026-09-01 Escopo: laboratório descartável `odoo16-teste.soloz.com.br`; produção
intocada.

## Resultado

- `google-auth==2.40.3` instalado em uma camada derivada mínima da imagem conhecida;
- compatibilidade provada no mesmo processo com `pyOpenSSL 21.0.0` e
  `cryptography 3.4.8` do Odoo 16;
- JSON da service account e developer token montados read-only em
  `/run/secrets/odoo-google-api`, arquivos `0600`;
- nenhuma credencial copiada para addon, banco, log ou evidência;
- autenticação e chamada oficial Google Ads REST v25 executadas dentro do container:
  HTTP 200, `request-id` presente e uma raiz acessível;
- Odoo, DB Manager e PostgreSQL saudáveis; rota pública HTTP 200.

## Incidente e correção estrutural

O primeiro ensaio usou o Dockerfile integral e esgotou o disco com cache de build. O
banco e os volumes não foram afetados. Foram removidos somente cache de build e imagem
intermediária reconstruíveis; os quatro arquivos de configuração foram restaurados por
hash a partir do rollback.

O deploy agora:

1. verifica espaço livre antes de mutar configuração;
2. constrói uma camada mínima sem enviar o repositório ou segredos ao build context;
3. testa `OpenSSL.crypto` e Google Auth antes de promover a imagem;
4. restaura configuração por rename atômico, inclusive com disco cheio;
5. aceita o estado somente após health, mount, permissões e imports.

Evidência canônica do runtime:
`scans/raw/20260901-odoo16-google-service-account-runtime/20260901T135450026109Z`.

Evidência canônica da fundação lock-free:
`scans/raw/20260901-odoo16-integration-core/release/20260901T140852431397Z`.
