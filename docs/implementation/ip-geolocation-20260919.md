# IP e localização estimada no Website e CRM

## Escopo

Extensão de `marketing_center_website` e `marketing_center_website_crm` para registrar
uma observação de rede no visitante nativo e preservar o contexto do envio no lead
nativo. Ambos passam à versão `16.0.1.6.0`.

O Website nativo registra navegação e identidade de visitante, mas não mantém o IP bruto
nem a localização detalhada do envio. Essa lacuna exige Python nos dois addons
existentes, sem novo addon, ledger, JavaScript ou API externa.

## Ativação e confiança

Por padrão, a captura adicional está desligada. A ativação via ORM exige:

- `marketing_center_website.ip_enrichment_enabled`: `True`;
- `marketing_center_website.geo_proxy_networks`: CIDRs dos proxies diretamente
  conectados ao Odoo, separados por vírgula; produção utiliza `127.0.0.1/32`.

O proxy deve **sobrescrever**, inclusive com vazio quando não aplicável, todos os
cabeçalhos `X-MC-Visitor-IP`, `X-MC-Geo-Source`, `X-MC-Geo-Country`, `X-MC-Geo-Region`,
`X-MC-Geo-City` e `X-MC-Geo-Timezone`. O código de região é o código do estado, por
exemplo `SP`; somente uma origem Cloudflare validada pela infraestrutura recebe
`X-MC-Geo-Source: cloudflare`.

O addon verifica o peer TCP original preservado por `werkzeug.proxy_fix.orig`, antes de
aceitar os cabeçalhos normalizados. Não interpreta cabeçalhos brutos Cloudflare ou
`X-Forwarded-For`. Sem proxy confiável usa apenas o peer original. Com proxy confiável e
IP normalizado ausente/inválido, não registra uma falsa observação com o endereço do
proxy. A confiança depende também de impedir acesso direto indevido ao Odoo pela
infraestrutura.

A captura exige HTTPS, host e origem permitidos, binding nativo ativo, endpoint ativo e
política de captura elegível. Usuários internos não alimentam essa extensão; a política
nativa de visitantes continua funcionando. Modo legado permanece fora desta entrega.

## Dados e prioridades

Campos nos dois modelos: `marketing_ip_address`, `marketing_ip_observed_at`,
`marketing_geo_country_id`, `marketing_geo_state_id`, `marketing_geo_city`,
`marketing_geo_timezone`, `marketing_geo_source`.

- Visitante: uma observação mais recente; a próxima observação sem localização limpa a
  estimativa anterior para não associar uma cidade antiga a outro IP.
- País/fuso nativos do visitante são preenchidos somente quando vazios. Campos estimados
  separados preservam a coerência quando os nativos já estão preenchidos por outra
  origem. Nunca escreve endereço do parceiro.
- Lead: observação imutável no envio, criada somente com aquisição nativa elegível;
  retry com o mesmo UUID mantém o primeiro lead e a primeira observação. Campos
  estimados não alteram país, estado, cidade ou endereço informados.
- Vínculo visitante/lead usa a relação nativa `visitor_ids`; quando necessário, o
  próprio fluxo nativo cria o visitante antes de concluir o formulário.
- IPv4/IPv6 são validados; IPs privados ficam sem estimativa geográfica. Cloudflare tem
  prioridade como um conjunto coerente. Na ausência de localização válida, o resolvedor
  GeoIP local do Odoo consulta o IP atual, sem reutilizar o cache de localização da
  sessão. Falhas de localização preservam o IP e falhas da extensão não bloqueiam
  criação do lead ou aquisição existente.

Campos são de leitura para usuários internos sujeitos às permissões existentes dos
modelos. O formulário público descarta tentativas de preencher esses campos. Não há
cópia da observação na descrição do lead, evento ou JSON de aquisição. O hook existente
de apagamento de dados privados limpa os novos campos do lead e dos visitantes ligados.
País/fuso nativos previamente preenchidos permanecem como campos nativos, sem apagar
dados que podem vir de outras fontes. Nenhum cron de retenção novo nem limpeza
retroativa foi acrescentado.

## Validação e implantação

Testes cobrem confiança do peer antes do ProxyFix, cabeçalhos forjados, Unicode,
IPv4/IPv6, IP privado/inválido, GeoIP ausente/falhando, estado dentro do país,
política/opt-in desligados, prioridade dos dados existentes e formulário HTTP real com
vínculo nativo, snapshot imutável, retry, injeção e apagamento.

Execução e evidências no SERVIDOR05 precedem promoção do commit oficial `16.0` para os
dois providers de produção. Registrar resultados reais no incidente operacional; este
documento descreve o contrato, não atesta uma implantação. O operador autorizou produção
sem backup para esta entrega.

Rollback funcional: desligar `ip_enrichment_enabled` interrompe a captura adicional sem
apagar observações e mantém código, esquema e views compatíveis. Antes do upgrade, o
provider anterior pode ser retomado. Depois do upgrade, voltar ao código anterior exige
validar também a reversão de views e metadados que referenciam os novos campos; trocar
somente o provider não é um rollback comprovado. A origem pode retornar ao estado de
cabeçalhos anterior registrado pela operação de infraestrutura.
