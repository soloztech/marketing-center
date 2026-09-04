# Consolidação das fundações de integração

Data: 2026-09-04  
Estado: em validação

## Decisão

Os addons técnicos `google_api_base`, `meta_api_base` e `meta_webhook_base` passam
a ser versionados fisicamente no repositório `soloztech/marketing-center`.

Essa é uma consolidação de repositório, não de responsabilidade funcional. Os três
addons permanecem independentes de `marketing_center_base`, conservam os mesmos
nomes técnicos, modelos, tabelas, XML IDs e contratos públicos. Portanto, não há
migração de banco nem renomeação de módulos Odoo.

## Histórico e recuperabilidade

- O histórico do antigo Integration Core foi incorporado ao Marketing Center por
  merge de históricos não relacionados; os commits originais continuam ancestrais
  da branch `16.0`.
- O checkout antigo foi retirado do workspace e preservado temporariamente em
  `/home/lucaszotelli/.codex/backups/20260904-integration-core-pre-consolidation`
  até o fechamento dos testes e do release remoto.
- Referências históricas a `integration-core` em auditorias e evidências datadas não
  foram reescritas, pois descrevem corretamente a topologia existente à época.

## Invariantes do cutover

1. Cada nome técnico deve resolver para exatamente um caminho no `addons_path`.
2. No SERVIDOR05, os 18 addons devem resolver sob
   `/mnt/outros/marketing-center`.
3. O caminho remoto antigo `/home/administrador/odoo16/src/outros/integration-core`
   não pode coexistir com a árvore consolidada ativa.
4. A ativação deve trocar a árvore do Marketing Center e retirar a árvore antiga no
   mesmo intervalo com Odoo e dbmanager parados.
5. O rollback deve restaurar as duas árvores de forma simétrica.
6. Contact Center continua consumindo apenas os contratos técnicos Meta; não passa
   a depender do domínio funcional do Marketing Center.

## Gates de release

- [ ] testes estáticos dos dois repositórios;
- [ ] 160 testes isolados das três fundações técnicas;
- [ ] suítes Odoo integradas de Marketing Center e Contact Center;
- [ ] cutover consolidado no SERVIDOR05 com resolução única dos 18 addons;
- [ ] smoke autenticado sem regressão funcional;
- [ ] repositórios privados `soloztech/contact-center` e
      `soloztech/marketing-center` publicados com CI reproduzível;
- [ ] tag/release candidato aponta para os commits validados.

## Segurança do CI privado

Os repositórios usam acesso cruzado somente leitura para instalar dependências de
teste. Nenhum PAT, chave privada ou valor de credencial integra a árvore Git. As
chaves de deploy e secrets são configuradas diretamente no GitHub após a criação
dos repositórios.

