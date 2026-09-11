# Marketing Center — ícone de megafone

Opção 01 aprovada pelo usuário, incorporada ao código e aplicada no SERVIDOR05 (`odoo16-teste.soloz.com.br`).

- `marketing_center_base/static/description/icon.svg` e `icon.png`: megafone branco sobre fundo malva, no estilo Odoo 16.
- Menu raiz configurado com `web_icon="marketing_center_base,static/description/icon.png"`.
- Mesmos arquivos em `marketing_center_suite/static/description/` para o catálogo de aplicativos.
- SVG 70 × 70; PNG RGBA 140 × 140, idêntico à proposta aprovada.
- Validação: XML bem formado, caminho do menu existente, dimensões/formato e igualdade dos arquivos conferidos; `git diff --check` sem erros.

Os cinco arquivos foram publicados e o menu 1105 atualizado via ORM, recalculando `web_icon_data`. Os registros de catálogo de `marketing_center_base` e `marketing_center_suite` também receberam seus caminhos de ícone. Não foi necessário reiniciar serviços nem executar upgrade completo dos módulos.

Os bytes do menu e das duas imagens do catálogo, lidos pelo ORM e pelas rotas autenticadas `/web/image`, correspondem ao PNG aprovado: 4.753 bytes, SHA-256 `4d5cac01050468d1d6d646f461304e82f55da91f6b3d6cdbc96de5608f068cb8`. As três imagens responderam HTTP 200.

Evidência no workspace operacional: `scans/raw/20260910-marketing-center-megafone/` (`before.json`, `plan.json`, `after.json`, `summary.json`). `backup_required: false`: arquivos reproduzíveis e metadados isolados e reversíveis no laboratório. Produção não foi alterada.
