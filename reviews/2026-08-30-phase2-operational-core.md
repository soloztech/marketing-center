# Marketing Center — núcleo operacional da Fase 2

Data local: 2026-08-30 Ambiente: `odoo16-teste.soloz.com.br` / servidor05 Versões:
`marketing_center_base 16.0.1.2.0`, bridge `16.0.1.0.0`

## Resultado

O segundo corte da Fase 2 foi implantado e validado. O base agora possui configuração de
fontes e conexões, roster próprio, catálogo provider-neutral, revisões imutáveis e
coordenação transacional de sincronização. Nenhuma API externa ou mutação de campanha
foi habilitada neste corte.

## Correções incorporadas na revisão adversarial

- record rules deixaram de combinar folhas One2many independentes; o acesso é projetado
  por usuário ativo correlacionado ao mesmo time e vínculo;
- touchpoints sem associação resolvida a source ficaram admin-only;
- `company_id` de team/source, `source_id` de connection e identidades dos vínculos
  passaram a ser imutáveis após criação;
- revisões de source/connection são incrementadas atomicamente sob row lock;
- `state` e mudanças de capabilities participam do fencing;
- sync captura `source_revision`, `binding_revision`, `profile_revision` e `window_key`,
  exige connection `reader/ready` e source ativa/read-enabled;
- existe somente um run ativo por source/kind/scope, com advisory lock e índice parcial
  como última defesa;
- a mesma idempotency key com fingerprint diferente gera conflito explícito;
- `expected_cursor_sequence` é obrigatório; polling assíncrono também incrementa a
  sequência de ownership;
- páginas com `reporting_context_hash` diferente são rejeitadas;
- erros de página anteriores preservam estado terminal `partial` e o result hash é
  encadeado por página;
- limites de `provider_job_ref`/`watermark` do DTO e do banco foram alinhados;
- constraints e índices com nomes acima do limite PostgreSQL foram corrigidos, com
  migração do estado instalado em `1.1.0`.

## Evidência

Release aplicado:

`/home/lucaszotelli/infra-ai-ops/scans/raw/20260829-odoo16-marketing-center-first-slice/release/20260831T022305825981Z`

- base limpo: 39/39 testes;
- base + bridge: 41/41 testes;
- upgrade offline: sucesso;
- segundo upgrade idempotente: sucesso;
- versão instalada: `16.0.1.2.0`;
- HTTP privado e público: 200;
- rota Traefik restaurada com o mesmo SHA-256;
- produção: não tocada;
- backup da base de teste: dispensado conforme orientação do proprietário.

Estado pós-upgrade:

```text
cc_touchpoints=130
marketing_touchpoints=130
bridge_links=130
bridge_jobs_failed=0
sources=0
entities=0
sync_runs=0
sync_cursors=0
active_scope_index=1
legacy_constraints=0
new_constraints=2
run_required_columns=2
cursor_required_columns=1
```

## Próximo corte

Extrair o runtime técnico `meta_api_base` em compatibilidade com `contact_center_meta`,
e então criar `marketing_center_meta` read-only. Os dois cores continuam independentes;
somente os consumidores dependem da base técnica Meta.
