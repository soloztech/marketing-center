# Fundação provider-neutral de cálculo de atribuição

Data: 2026-09-01 Escopo: candidato local de `marketing_center_base`; sem implantação.

## Decisão

O corte adiciona modelos versionados e um ledger imutável de cálculo:

- `marketing.attribution.model` congela estratégia, janela, policy e bases de evidência
  por versão;
- `marketing.attribution.calculation.run` congela produtor, input e versão do modelo;
- `marketing.attribution.result` representa um único `marketing.business.event` e
  registra inclusive o caso `unattributed`;
- `marketing.attribution.candidate` preserva toda evidência candidata, inclusive a que
  foi excluída;
- `marketing.attribution.contribution` contém somente touchpoints efetivos que receberam
  peso.

Uma nova execução cria novo `calculation_ref` e não substitui a anterior. Replay com o
mesmo input devolve o mesmo resultado; reutilizar a referência com input diferente
falha. Pesos usam micros inteiros e sempre somam exatamente `1_000_000`; a alocação
monetária também fecha exatamente no valor canônico em micros do fato Odoo.

## Limite entre correlação e crédito

`correlation_only` é persistido no ledger de candidatos para explicar a cobertura, mas
nunca pode ser configurado como base creditável. Relações M:N existentes entre conversa,
pessoa, lead e touchpoint, por si sós, portanto não geram contribuição.

O termo `journey_credit` significa apenas alocação reproduzível sob uma política
declarada (`first_touch`, `last_touch` ou `linear`). Não prova incremento, causal lift
ou contrafactual. `platform_reported` vive em estratégia e interpretação separadas e não
pode ser misturado ao crédito da jornada.

## Trust boundary

O serviço exige uma capability Python interna e um `producer_key` estável. Não é uma API
RPC/UI. Cada `evidence_ref` deve ser uma referência imutável e namespaced emitida pelo
produtor, por exemplo `website.session_event:<uuid>`; texto digitado na UI não é
evidência aceitável.

Neste corte, apenas `deterministic_first_party` pode alimentar modelos de jornada.
`manual_reviewed` permanece reconhecido como tipo de evidência, mas fail-closed: não
pode receber crédito até existir um ledger próprio de decisão, com autoridade, autor,
ocorrência e revisão imutáveis. Esse produtor é um gate futuro, não uma causalidade
presumida agora.

Para `platform_reported`, cada candidato traz peso reportado. Peso zero continua no
ledger, com estado `eligible_not_selected`, e não cria contribuição. Os pesos positivos
precisam fechar exatamente `1_000_000`.

## Garantias do corte

- somente a revisão presente em `marketing.attribution.effective.touchpoint` pode
  receber crédito;
- touchpoint posterior ao fato ou fora da janela fica explicitamente excluído;
- empresa do modelo, fato, touchpoint, execução e resultado deve ser a mesma;
- create/write/unlink direto em qualquer ledger novo é negado, inclusive com `sudo()`
  sem o token interno;
- ACL e record rules mantêm o detalhe de cálculo restrito ao administrador e às empresas
  ativas;
- A→B→A é preservado como três execuções, não como update destrutivo.

## Próximo produtor, deliberadamente fora deste corte

Ainda não existe inferência automática de candidatos. Um addon de bridge futuro deve
emitir referências determinísticas a partir de sessão/submissão first-party ou de um
vínculo de negócio explicitamente auditável e só então chamar o serviço. A projeção
segura para dashboard também fica separada do ledger administrativo detalhado.
