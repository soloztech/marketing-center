# Spike do runtime Google Ads no Odoo 16

Data: 2026-09-01  
Alvo: imagem efetiva do laboratório SERVIDOR05 (`odoo16-local:latest`), Python
3.10.12 e Odoo 16/OCB. Nenhuma imagem, container persistente, banco ou fonte do
servidor foi alterado.

## Resultado

- o virtualenv real do Odoo contém `requests`, mas não contém `google-ads`,
  `google-auth`, `grpcio` nem `protobuf` do ecossistema Google;
- em um container efêmero da mesma imagem, `google-ads==31.4.0` foi instalado em
  `/tmp` e importou com sucesso pelo interpretador real `/opt/venv/bin/python`;
- no mesmo processo também importaram Odoo 16, `protobuf 7.36.1`, `grpcio 1.83.1`
  e `GoogleAdsClient`;
- a biblioteca oficial suporta Python 3.10 nesta versão, mas o próprio ecossistema
  Google avisa que o suporte seguirá o EOL do Python 3.10 em 2026-10-04;
- a API Google Ads vigente é v25, com release funcional v25.1 em 2026-08-19.

Portanto, o SDK é tecnicamente compatível, mas incluí-lo agora no processo inteiro
do Odoo adicionaria um conjunto grande de dependências e uma janela curta de suporte
do runtime. Isso não justifica um worker separado antes de existir carga real.

## Decisão inicial e ajuste comprovado

O primeiro reader Google Ads usa o REST oficial v25, com transporte fino em
`google_api_base`. O contrato permanece independente do transporte para permitir
trocar REST pelo SDK oficial ou por worker isolado sem alterar DTOs, ledgers ou o
addon `marketing_center_google`.

A credencial efetivamente disponível é uma service account autorizada na conta
Google Ads. Por isso, o runtime inclui apenas `google-auth==2.40.3`; não inclui o SDK,
gRPC nem protobuf. A versão foi pinada porque a linha 2.57 troca RSA por
`cryptography` recente, incompatível com o `pyOpenSSL 21.0.0` do Odoo 16. O build
valida simultaneamente `OpenSSL.crypto` e `google.oauth2.service_account`.

O transporte inicial deve:

- aceitar OAuth de usuário autorizado ou service account, sempre por referências
  externas de segredo, e resolver o `developer_token` da mesma forma;
- obter access token no endpoint OAuth oficial e nunca persistir access token;
- enviar `developer-token` e, quando aplicável, `login-customer-id` sem hífens;
- fixar e validar a versão suportada da API;
- preservar somente `request-id`, classe de erro allow-listed, cooldown e contexto
  técnico sanitizado;
- usar `customers:listAccessibleCustomers` para discovery e
  `customers/{id}/googleAds:search` com GAQL e `nextPageToken` opaco;
- limitar páginas, linhas, tamanho e tempo; toda chamada externa ocorre em job OCA;
- tratar `RESOURCE_EXHAUSTED` como cooldown e nunca repetir mutação ambígua. O
  primeiro corte é estritamente read-only.

Uma consulta `Search` custa uma operação; as páginas seguintes com token válido não
consomem operação adicional segundo a quota oficial. O perfil Explorer atual mantém
o teto de 2.880 operações por dia em contas de produção, portanto discovery e sync
devem ser bounded e agendados por conta.

## Gates para implantação

1. `google_api_base` instala sem Marketing Center; o runtime Google mínimo é pinado
   e validado contra o stack criptográfico do Odoo 16.
2. Testes cobrem OAuth, headers, masking, timeout, resposta truncada, paginação,
   request ID, versão e classificação de quota/permissão.
3. `marketing_center_google` depende do core e da base técnica, não contém segredo e
   projeta apenas DTOs provider-neutral.
4. Uma credencial reader real comprova developer token, customer/login customer,
   acesso e uma consulta GAQL mínima no laboratório.
5. O SDK só será empacotado após ADR novo, com versões/hashes pinados e smoke do
   processo Odoo; worker isolado só entra se dependência, EOL ou volume justificarem.

## Evidência posterior

- runtime mínimo implantado no SERVIDOR05 sem rebuild integral da imagem;
- service account e developer token montados read-only, modo `0600`, fora do banco;
- chamada real `customers:listAccessibleCustomers` em v25 retornou HTTP 200,
  `request-id` e uma raiz acessível, sem registrar IDs ou segredos;
- `google_api_base` `16.0.1.1.1` passou 39/39 testes, apply e replay no laboratório;
- o ensaio de falha por falta de disco levou a preflight de espaço, imagem derivada
  mínima e rollback de configuração por rename atômico.

## Referências oficiais

- https://developers.google.com/google-ads/api/docs/release-notes
- https://developers.google.com/google-ads/api/rest/auth
- https://developers.google.com/google-ads/api/rest/common/search
- https://developers.google.com/google-ads/api/docs/best-practices/quotas
- https://developers.google.com/google-ads/api/docs/client-libs/python
- https://pypi.org/project/google-ads/
