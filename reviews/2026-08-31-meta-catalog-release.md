# Meta catalog — implementação e release

Data: 2026-08-31

## Veredito

`marketing_center_meta` `16.0.1.1.0` e `marketing_center_base` `16.0.1.2.1` foram
implantados e validados no servidor05. Produção não foi tocada.

## Entregue

- um único run por ad account, na ordem campaign → adset → ad → creative;
- cursor local versionado com somente o `after` opaco do provider;
- CAS do core, fencing de job/source/connection/profile e cap de 512 páginas;
- allow-list fixa de campos, sem targeting, specs de criativo ou URLs assinadas;
- creative reutilizável sem parent e referência `meta.creative_ref` no ad;
- retry limitado, fechamento terminal, pausa segura de autorização e rollback atômico da
  página quando o enqueue sucessor falha;
- nenhuma inferência de exclusão por ausência na varredura.

## Gate

- base: 39/39 testes;
- integrado: 84/84 testes, incluindo 20 testes novos de catálogo;
- upgrade offline e segundo replay: aprovados;
- versões instaladas: base `16.0.1.2.1`, bridge `16.0.1.0.0`, Meta `16.0.1.1.0`;
- HTTP privado e público: 200;
- rota de teste restaurada com o mesmo SHA-256;
- evidência:
  `scans/raw/20260831-odoo16-marketing-center-meta-catalog/release/20260831T042545536542Z`.

O primeiro gate foi abortado e recuperado antes da alteração da base principal porque o
Python 3.10 não aceitava diretamente o offset Meta `+0000`. A normalização estrita para
`+00:00` foi adicionada e a execução seguinte passou integralmente. Evidência da falha
recuperada:
`scans/raw/20260831-odoo16-marketing-center-meta-catalog/release/20260831T042328248688Z`.

## Bloqueio externo

O laboratório ainda não possui reader Ads. O token atual de Messenger/Instagram é um
Page token e não tem `ads_read`; ele não será reutilizado. Para o teste live, criar um
System User reader, conceder somente as ad accounts necessárias e montar App secret e
token em arquivos privados referenciados pelo perfil de Marketing.
