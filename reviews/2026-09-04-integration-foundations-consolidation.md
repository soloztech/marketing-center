# Consolidação das fundações de integração

Data: 2026-09-04  
Estado: validado no laboratório; publicação GitHub em andamento

## Decisão

Os addons técnicos `google_api_base`, `meta_api_base` e `meta_webhook_base` passam a ser
versionados fisicamente no repositório `soloztech/marketing-center`.

Essa é uma consolidação de repositório, não de responsabilidade funcional. Os três
addons permanecem independentes de `marketing_center_base`, conservam os mesmos nomes
técnicos, modelos, tabelas, XML IDs e contratos públicos. Portanto, não há migração de
banco nem renomeação de módulos Odoo.

## Histórico e recuperabilidade

- O histórico do antigo Integration Core foi incorporado ao Marketing Center por merge
  de históricos não relacionados; os commits originais continuam ancestrais da branch
  `16.0`.
- O checkout antigo foi retirado do workspace e preservado temporariamente em
  `/home/lucaszotelli/.codex/backups/20260904-integration-core-pre-consolidation` até o
  fechamento dos testes e do release remoto.
- Referências históricas a `integration-core` em auditorias e evidências datadas não
  foram reescritas, pois descrevem corretamente a topologia existente à época.

## Invariantes do cutover

1. Cada nome técnico deve resolver para exatamente um caminho no `addons_path`.
2. No SERVIDOR05, os 18 addons devem resolver sob `/mnt/outros/marketing-center`.
3. O caminho remoto antigo `/home/administrador/odoo16/src/outros/integration-core` não
   pode coexistir com a árvore consolidada ativa.
4. A ativação deve trocar a árvore do Marketing Center e retirar a árvore antiga no
   mesmo intervalo com Odoo e dbmanager parados.
5. O rollback deve restaurar as duas árvores de forma simétrica.
6. Contact Center continua consumindo apenas os contratos técnicos Meta; não passa a
   depender do domínio funcional do Marketing Center.

## Gates de release

- [x] testes estáticos dos dois repositórios;
- [x] 160 testes isolados das três fundações técnicas;
- [x] suítes Odoo integradas de Marketing Center e Contact Center;
- [x] cutover consolidado no SERVIDOR05 com resolução única dos 18 addons;
- [x] smoke autenticado sem regressão funcional;
- [ ] repositórios privados `soloztech/contact-center` e `soloztech/marketing-center`
      publicados com CI reproduzível;
- [ ] tag/release candidato aponta para os commits validados.

## Segurança do CI privado

Os repositórios usam um secret de Actions chamado `CROSS_REPO_READ_TOKEN` para o
checkout cruzado das dependências privadas de teste. Nenhum token, chave privada ou
valor de credencial integra a árvore Git. A organização não permite deploy keys; por
isso, o bootstrap usa temporariamente o token autenticado do mantenedor, armazenado
somente como secret cifrado no GitHub. Depois do primeiro release, ele deve ser
substituído por uma credencial fina de GitHub App com acesso `Contents: read`
exclusivamente aos dois repositórios.

O GitHub Actions permanece desabilitado enquanto os repositórios estão vazios. A ordem
segura de publicação é: publicar ambas as branches `16.0`, selecionar a branch padrão,
publicar em ambos a tag coordenada `16.0.20260904.1-rc1`, habilitar Actions e só então
disparar manualmente as duas suítes. Cada workflow consome a tag imutável do repositório
irmão. Isso evita tanto o bootstrap circular quanto a redefinição posterior de uma CI já
verde pela movimentação da outra branch.

## Evidência do laboratório

- Marketing Center e fundações: `applied_and_validated`, 425 arquivos, árvore
  `d62f85de79bef0c23b19fd3315bd114d98fe18a8b1bec9e10f2e1d02d8234ceb`, 1.071 testes Odoo
  e QUnit 12/12 em ambos os modos. Evidência:
  `scans/raw/20260903-odoo16-marketing-center-remaining-addons-greenfield-closeout/release/20260904T123542490166Z/summary.json`.
- Contact Center consumidor: `applied_and_validated`, árvore
  `83b88b22773067c34e370d3784ec05f1bd0b2b09f08a478230475bbe16bb1f9e`, 505 Base, 199
  WuzAPI e 895 testes integrados, além de QUnit Base/UI nos dois modos. Evidência:
  `scans/raw/20260903-odoo16-contact-center-base-crm-greenfield-closeout/release/20260904T124751394613Z/summary.json`.
- A rota foi restaurada com SHA-256
  `2fd9e569856478dfa336391bd226f3c8af05454db56033be6352ef80aad56403`, o endpoint público
  de teste respondeu HTTP 200 e `production_touched` permaneceu `false` nos dois
  releases.
- O fechamento documental posterior não alterou fontes Python/XML/JS. Os dry-runs das
  árvores exatas de publicação retornaram `dry_run_ready`: Marketing Center com 425
  arquivos e SHA-256 `478d26f9a63b640c3fedbc7f6afd01d7af4b491a6cd2c43b87e79358a80c0b36`;
  Contact Center com 261 arquivos e SHA-256
  `8dfdaabd5926687f09d755798032910946d4353ff406753ae1a007aee5382c77`.
