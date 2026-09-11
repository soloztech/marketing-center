# Marketing Center Catalog — implementação do MVP

**Versão 0.8 — 11/09/2026. Informações e perguntas frequentes na mesma aba.**

Aplicativo de catálogo de conteúdo para distribuição pública/comunitária em Odoo 16, com
nomes e funcionamento genéricos. O MVP mantém cadastro e edição diretos, sem aprovação
editorial, etapas de publicação ou bloqueio por revisão técnica.

## 1. Escopo entregue

Uma central para Marketing e Engenharia cadastrarem informações da empresa e de
produtos. Vendas e Atendimento encontram textos, especificações, fotos, vídeos,
catálogos, datasheets e FAQ no mesmo lugar.

Fluxo: **cadastrar → salvar → consultar/usar**. Para corrigir, editar e salvar
novamente. Para retirar da busca, arquivar. Não haverá aprovador, fila de validação ou
obrigação de criar nova versão.

## 2. Cadastro da ficha

Exemplos genéricos de fichas: **Empresa Exemplo** e **Linha Modular**. Cada organização
cadastra seus próprios nomes, produtos e materiais.

| Campo                   | Uso                                                                  |
| ----------------------- | -------------------------------------------------------------------- |
| Nome e tipo             | Identificar empresa ou solução/produto.                              |
| Imagem                  | Identificação visual da ficha.                                       |
| Produtos pai vinculados | Escolher um ou mais `product.template`.                              |
| Variantes vinculadas    | Escolher um ou mais `product.product`, inclusive de pais diferentes. |
| Conteúdos               | Textos, perguntas/respostas, arquivos e links da ficha.              |

A ficha pode vincular pais, variantes ou ambos. A ficha institucional não precisa de
produto. O vínculo com pai serve para encontrar a ficha a partir das suas variantes; uma
variante vinculada isoladamente não associa suas variantes irmãs.

As fichas e a biblioteca abrem em **cards/kanban**, com imagem e identificação. No
cadastro: **Informações · Materiais · Produtos vinculados**. Busca por nome da ficha,
produto/referência, título do material e texto/pergunta cadastrados.

A aba **Informações** reúne **Informações estruturadas** e, logo abaixo, **Perguntas
frequentes**. Cada seção permite alternar entre **Lista** e **Kanban** independentemente
e inicia em Lista. **Materiais** inicia em Kanban. A troca de visualização mantém o
trabalho na ficha, inclusive os conteúdos ainda não salvos.

O campo livre de apresentação foi removido do cadastro e do modelo. Os textos da ficha
são conteúdos estruturados com título e texto formatado; o projeto greenfield não mantém
campos de resumo nem compatibilidade com esse campo descartado.

## 3. Conteúdos da ficha

Cada conteúdo é um cadastro simples, com título e tipo:

| Tipo    | Cadastro                                                                                                                |
| ------- | ----------------------------------------------------------------------------------------------------------------------- |
| Texto   | Título e texto formatado: institucional, descrição técnica, aplicação, garantia ou orientação comercial.                |
| FAQ     | Pergunta e resposta; termos alternativos de busca opcionais.                                                            |
| Arquivo | Um arquivo por linha, com categoria como foto, vídeo, catálogo, datasheet ou manual. Vários arquivos são várias linhas. |
| Link    | Título e endereço de vídeo ou documento externo.                                                                        |

Fotos, PDFs e vídeos continuam no tipo **Arquivo**, com reconhecimento automático para
exibir a prévia correta. A categoria do material indica sua finalidade. Fotos aparecem
nos cards; PDFs renderizam a primeira página ao entrar na área visível e abrem no
visualizador completo; vídeos enviados como arquivo reproduzem sob demanda. Outros
formatos permitem download; links externos abrem em nova aba.

O cadastro pela aba **Materiais** oferece somente Arquivo ou Link. Na aba
**Informações**, a seção **Informações estruturadas** cria textos e **Perguntas
frequentes** cria perguntas e respostas, sem duplicar essa opção em Materiais.

A lista de **Perguntas frequentes** mostra diretamente **Pergunta** e **Resposta**, com
inclusão e edição na própria linha e resposta formatada. Não exige digitar um título
separado: quando ausente, o título da nova FAQ é preenchido com a pergunta. Títulos
personalizados existentes são preservados. As configurações complementares continuam no
formulário, acessível ao abrir o card no Kanban.

Especificações começam como texto/tabela no editor ou documento anexado. Não haverá
dicionário de propriedades, cálculo de valores ou motor de comparação técnica.

Por padrão, o conteúdo acompanha a ficha. Uma opção **“Somente para estes
produtos/variantes”** permite restringir um datasheet ou informação específica, sem
criar outro cadastro de solução. Essa restrição limita os resultados por produto; não
calcula compatibilidade técnica. Sem produto selecionado, a biblioteca mostra a
indicação de aplicabilidade.

Uma marcação simples distingue **Uso interno** de **Pode compartilhar com cliente**. O
próprio editor escolhe; não há aprovação. Todo conteúdo salvo fica disponível para
consulta interna conforme o acesso. O botão de encaminhar fica disponível para os
conteúdos marcados para compartilhamento.

## 4. As duas pontes

### Contact Center

Botão **Conteúdo** na conversa abre um popup com busca, ficha, materiais e FAQ.

1. Buscar e abrir um card de ficha, como a solução/produto ou a empresa.
2. Navegar por Informações, Materiais e FAQ e abrir os detalhes com prévia. A seleção de
   textos, FAQs, links e arquivos permanece ao mudar de aba ou ficha.
3. Clicar em **Adicionar à mensagem**: inserir no rascunho sem apagar o que o atendente
   já escreveu.
4. Enviar pelo composer normal da conversa.

Reutilizar o caminho existente de anexos do Contact Center, com acesso autenticado e
**cópia do arquivo**. O original fica na Central; os arquivos já enviados permanecem na
mensagem mesmo que o cadastro seja alterado depois. A fila, os limites dos arquivos, as
falhas e retentativas continuam no fluxo normal de atendimento.

Nesta versão, não haverá recibo editorial separado nem bloqueio de fila por
alteração/arquivamento do conteúdo. O rascunho já preparado mantém o texto e as cópias
selecionadas; ele não é sincronizado automaticamente com edições posteriores da Central.
O histórico da conversa registra o que foi efetivamente enviado.

### Vendas

Botão **Conteúdo** na cotação/pedido abre o mesmo navegador visual em popup, filtrado
pelos produtos das linhas, com opção de consultar outros produtos e a empresa. O
cadastro do produto pai e da variante também terá acesso à ficha.

O vendedor consulta especificações, abre/baixa materiais e copia textos durante a
elaboração da cotação. Criar a cotação e escolher itens continuam no fluxo atual de
Vendas. Anexação automática ao e-mail e inclusão de produtos pela ficha ficam para
depois.

## 5. Estrutura técnica enxuta

| Addon                                     | Responsabilidade                                                                                                         | Dependências diretas                            |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------- |
| `marketing_center_catalog`                | Aplicativo principal: fichas, produtos/variantes, textos, FAQ, arquivos, links, busca e acesso pelo cadastro de produto. | `product`, `mail`, `web`, `web_editor`          |
| `marketing_center_catalog_contact_center` | Botão na conversa, popup e inserção de textos/arquivos no composer existente.                                            | `marketing_center_catalog`, `contact_center_ui` |
| `marketing_center_catalog_sale`           | Botão na cotação/pedido e consulta filtrada pelos produtos das linhas.                                                   | `marketing_center_catalog`, `sale`              |

O núcleo é instalável sozinho, com `application=True` e menu próprio **Catálogo de
Conteúdo**. As pontes são opcionais, com `application=False` e `auto_install=False`.
Nenhuma ponte depende da outra. O prefixo `marketing_center_` identifica a família de
addons; não exige instalar `marketing_center_base`, providers ou atribuição.

Dois modelos principais no núcleo: **`marketing.center.catalog.subject`** para a ficha e
**`marketing.center.catalog.item`** para texto/FAQ/arquivo/link. Produtos e restrições
usam relações diretas com os modelos nativos. Arquivos usam `ir.attachment`. Menus,
grupos, XML IDs e assets são definidos no namespace dos novos addons.

Dois perfis simples: **Editor**, para Marketing/Engenharia manterem o cadastro, e
**Leitor**, para Vendas/Atendimento consultarem. Os registros pertencem a uma empresa.
Permanecem os controles normais de acesso do Odoo, acesso autenticado aos arquivos e
conferência do destino da conversa; isso não cria um fluxo de aprovação de conteúdo.

O catálogo editorial tem responsabilidade distinta do catálogo externo de anúncios
existente em `marketing_center_base`. O serviço existente
`marketing.center.catalog.service` continua pertencendo à sincronização publicitária. O
novo cadastro usa os seus próprios modelos; um serviço editorial futuro, se necessário,
deve ter nome distinto.

`marketing_center_catalog_sale` consulta conteúdos; `marketing_center_sale` registra
eventos/valores de vendas para atribuição. Da mesma forma,
`marketing_center_catalog_contact_center` insere materiais na conversa;
`marketing_center_contact_center` integra eventos de atendimento à atribuição. As pontes
de catálogo não dependem das pontes de atribuição.

Não dependemos de módulos de validação, DMS, Knowledge ou IA para começar. Também não
vamos criar novas APIs de publicação, hashes de aprovação, recibos editoriais ou
alterações especiais no despacho do Contact Center nesta primeira entrega.

### Organização e distribuição comunitária

Os três addons são filhos diretos da raiz do repositório `marketing-center`, permitindo
instalação independente pelo `addons_path`:

```text
marketing-center/
├── marketing_center_catalog/
├── marketing_center_catalog_contact_center/
└── marketing_center_catalog_sale/
```

Essa árvore corresponde aos diretórios implementados. Este arquivo é o plano canônico do
catálogo; `plan.md` continua documentando a suíte analítica existente. O perfil
`marketing_center_suite` não passa a instalar o catálogo automaticamente.

Adotar nomes técnicos, textos, configurações e exemplos genéricos, sem dados
empresariais embutidos, IDs de banco fixos ou dependências de addons de uma empresa
específica. Código/XML IDs em inglês e traduções da interface, inicialmente pt-BR. Os
manifests seguem a licença `AGPL-3` adotada pelos addons atuais do repositório,
preservando autoria e créditos reais.

A distribuição pública se refere ao código do módulo; os cadastros e arquivos continuam
sujeitos aos acessos normais de cada instalação. Não há portal público de conteúdo no
MVP. A ponte Contact Center utiliza a dependência `contact_center_ui` do repositório
próprio desse aplicativo; o núcleo e a ponte Vendas funcionam sem ela.

## 6. Preparação suficiente para IA depois

Manter texto/FAQ em campos consultáveis, tipo de conteúdo, vínculos com produtos,
indicação de uso interno/compartilhamento e os identificadores/datas de atualização do
Odoo. Isso permitirá acrescentar uma consulta de IA sobre a base existente.

Sem IA executando agora, sem banco vetorial e sem corpus cacheado obrigatório. Se no
futuro houver necessidade de fontes históricas, aprovação, indexação de PDFs ou
publicação no site, essas funções entram como evolução. O cadastro inicial representa o
**conteúdo atual**, sem promessa de histórico imutável.

## 7. Percurso implementado

1. Construir o cadastro de fichas e conteúdos com um exemplo institucional e uma solução
   vinculada a pai e variante.
2. Entregar o popup do Contact Center e o acesso na cotação/produto.
3. Conferir o percurso completo: cadastrar e editar diretamente, localizar o material
   correto, adicionar vários arquivos/FAQ à mensagem, preservar originais e consultar em
   Vendas.

**Primeiro resultado esperado:** um editor salva um datasheet, uma foto e uma FAQ na
ficha; atendente e vendedor já conseguem encontrá-los e utilizá-los. Homologar núcleo
sozinho, núcleo com cada ponte separadamente e combinação das duas pontes.

## 8. Instalação e uso

1. Colocar a raiz deste repositório no `addons_path` e atualizar a lista de aplicativos.
2. Instalar `marketing_center_catalog`. Instalar cada ponte conforme os aplicativos
   usados; a ponte de atendimento exige também o repositório Contact Center.
3. Em Configurações → Usuários, conceder **Catálogo de Conteúdo / Editor** a quem mantém
   o cadastro e **Leitor** a quem consulta. As permissões normais de Vendas/Atendimento
   continuam necessárias.
4. Abrir **Catálogo de Conteúdo**, cadastrar uma ficha e adicionar conteúdos. A marcação
   inicial é **Uso interno**; o editor pode escolher **Pode compartilhar com cliente**
   ao cadastrar material destinado ao atendimento.
5. Na cotação, usar **Catálogo de Conteúdo**; na conversa, usar **Conteúdo**. O popup da
   conversa adiciona conteúdo ao rascunho para o atendente revisar e enviar.

Os três addons foram instalados em homologação em 11/09/2026, versão atual `16.0.1.1.0`.
O administrador tem acesso de Editor. Publicação do código e implantação em produção não
foram realizadas.

## 9. Verificações técnicas da implementação

A versão visual passou instalação limpa e atualização dos três addons: **32 testes
Python/HTTP**, sem falhas. Uma regressão adicional de campos dos kanbans foi validada na
suíte do núcleo (**17 testes**). O navegador e a ponte passaram **17 testes QUnit / 89
assertivas**, em modo minificado e `debug=assets`. No navegador foram conferidos cards
com imagens, miniaturas e leitor de PDF, filtros de Vendas, navegação do popup e
cadastro de Materiais restrito a Arquivo/Link.

Os resultados históricos abaixo se referem ao MVP 1.0. As verificações usam bancos
locais e dados fictícios, sem envio externo. Não acrescentam etapas de aprovação ao
cadastro de conteúdo.

| Instalação                       | Testes Python/HTTP aprovados                        |
| -------------------------------- | --------------------------------------------------- |
| Núcleo independente              | 10                                                  |
| Núcleo + Vendas                  | 14                                                  |
| Núcleo + Contact Center          | 17                                                  |
| Três addons + Contact Center CRM | 21, tanto na instalação limpa quanto na atualização |

A ponte passou 9 testes QUnit e 29 assertivas em cada modo, minificado e `debug=assets`.
Foram exercitados o popup real, a preservação do texto, múltiplos arquivos, troca de
conversa, fechamento durante inclusão e retry nativo. A regressão da interface existente
do Contact Center e CRM passou 306 testes / 2.579 assertivas no modo minificado com a
nova ponte carregada. Os hooks pre-commit do repositório verificam Python, JavaScript,
XML, traduções e manifests.

No navegador, uma ficha nova recebeu conteúdo antes de seu primeiro salvamento; a
cotação abriu a biblioteca com filtro removível; uma FAQ e dois PDFs foram adicionados
ao rascunho sem substituir a saudação existente. O banco confirmou anexos novos, bytes
iguais aos originais e originais privados preservados. O transporte dessa prova foi
simulado e bloqueava envios externos.

Compatibilidade exercitada com Odoo/OCB 16.0 e `contact_center_base` /
`contact_center_ui` 16.0.1.3.0, revisão Contact Center
`1ef3743a70a4e94c37604c2d0f33aa27217df0f8`. Não houve execução da CI remota nem
publicação de código. A instalação posterior em homologação preservou os módulos
preexistentes e os registros de negócio conferidos; a atualização visual preservou os
cadastros, arquivos e demais registros de negócio.
