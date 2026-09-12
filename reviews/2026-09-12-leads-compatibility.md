# Compatibilidade isolada: revisão + descoberta/histórico de leads

Não houve merge nem alteração no checkout canônico, no worktree de revisão ou na branch
de leads. O conjunto combinado existe somente como snapshot de teste.

- Snapshot:
  `/home/lucaszotelli/infra-ai-ops/scans/raw/20260912-marketing-review-fixes/integration-leads`.
- Base das alterações de leads: `0a62448503ad7d61cadfb7c609206692d9222e20`.
- SHA da branch de leads: `b3a892c7a54b45a23ffbcba0c18e0dfdc7eed50f`.
- Base Git do worktree de revisão: `3c082ad315e9d089dcdd749e673a7706bd336169`, com
  alterações locais capturadas e hashes no relatório JSON.
- Foram capturados 613 arquivos rastreados/novos não ignorados e aplicadas as diferenças
  dos 18 arquivos da branch de leads por três vias.
- Único conflito: versão do manifesto Meta. Resolvido no snapshot para `16.0.1.1.1`,
  reunindo feature `16.0.1.1.0` e correções `16.0.1.0.1`. Ambos os datapaths e os assets
  de leads foram preservados.
- Models/tests `__init__.py` combinaram sem conflito. Foi verificado que
  `credential_health` continua importado após `meta_profile`.
- O agente de CI confirmou estabilidade do transporte/webhook antes da cópia. Não houve
  edição de lógica adicional dessas dependências durante a preparação.

## Resultado

**215 testes, 0 falhas, 0 erros** em instalação limpa `mc_review_20260912_leads_compat`,
conclusão `2026-09-12 04:01:49 UTC`.

Suítes completas: `marketing_center_meta` e `meta_api_base`, incluindo descoberta de
formulários, histórico, lead collection, saúde de credenciais e Graph. As APIs foram
simuladas pelos testes; não houve consulta a leads ou credenciais reais. Log:
`/home/lucaszotelli/infra-ai-ops/scans/raw/20260912-marketing-review-fixes/leads_compat-test.log`.
Relatório completo e hashes:
`/home/lucaszotelli/infra-ai-ops/scans/raw/20260912-marketing-review-fixes/integration-leads-report.json`.

O checkout de leads permaneceu limpo e no mesmo SHA após o teste. Essa prova não
executou merge, publicação, CI remota nem smoke de navegador dos novos assets de leads.

## Hashes finais dos pontos de integração

- `marketing_center_meta/__manifest__.py`:
  `7ed58c3d7099f28553e4e7d60ee3654fc83c60a287204a800c6327f5d3c5050c`.
- `marketing_center_meta/models/__init__.py`:
  `9155750ea35689af764f78a357a6a096e747e05e36018a924495cfb366dbbcc7`.
- `marketing_center_meta/tests/__init__.py`:
  `f68a3cc4a26f8bccf29d0da3b1f1fa0d3dac24db7afa756531657f05b7741565`.

## Disposição dos arquivos

- `marketing_center_meta/__manifest__.py`: conflict; resolução de versão descrita acima.
- `marketing_center_meta/models/__init__.py`: three_way_clean; conteúdo preservado no
  snapshot.
- `marketing_center_meta/models/lead_ads.py`: incoming_only; conteúdo preservado no
  snapshot.
- `marketing_center_meta/models/lead_discovery.py`: incoming_only; conteúdo preservado
  no snapshot.
- `marketing_center_meta/models/lead_history.py`: incoming_only; conteúdo preservado no
  snapshot.
- `marketing_center_meta/readme/USAGE.rst`: incoming_only; conteúdo preservado no
  snapshot.
- `marketing_center_meta/security/ir.model.access.csv`: incoming_only; conteúdo
  preservado no snapshot.
- `marketing_center_meta/security/lead_tools_security.xml`: incoming_only; conteúdo
  preservado no snapshot.
- `marketing_center_meta/services/catalog.py`: incoming_only; conteúdo preservado no
  snapshot.
- `marketing_center_meta/services/lead_forms.py`: incoming_only; conteúdo preservado no
  snapshot.
- `marketing_center_meta/static/src/views/lead_route_list.esm.js`: incoming_only;
  conteúdo preservado no snapshot.
- `marketing_center_meta/static/src/views/lead_route_list.xml`: incoming_only; conteúdo
  preservado no snapshot.
- `marketing_center_meta/tests/__init__.py`: three_way_clean; conteúdo preservado no
  snapshot.
- `marketing_center_meta/tests/test_catalog_adapter.py`: incoming_only; conteúdo
  preservado no snapshot.
- `marketing_center_meta/tests/test_lead_discovery.py`: incoming_only; conteúdo
  preservado no snapshot.
- `marketing_center_meta/tests/test_lead_history.py`: incoming_only; conteúdo preservado
  no snapshot.
- `marketing_center_meta/views/lead_ads_views.xml`: incoming_only; conteúdo preservado
  no snapshot.
- `marketing_center_meta/views/lead_tools_views.xml`: incoming_only; conteúdo preservado
  no snapshot.

## Reprodução

Executar a partir do diretório do snapshot (o harness usa seu parent como diretório de
addons):

```bash
python3 tools/local_review_tests.py test \
  --runtime /home/lucaszotelli/infra-ai-ops/scans/raw/20260905-centers-greenfield-audit/runtime \
  --packages /home/lucaszotelli/infra-ai-ops/scans/raw/20260911-content-catalog-implementation/packages \
  --peer /home/lucaszotelli/infra-ai-ops/scans/raw/20260912-marketing-review-fixes/peer-5203d61 \
  --state /home/lucaszotelli/infra-ai-ops/scans/raw/20260912-marketing-review-fixes \
  --suite leads_compat --http-port 18183 \
  --modules meta_api_base,marketing_center_meta \
  --tags /meta_api_base,/marketing_center_meta
```
