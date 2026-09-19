Automações de comunicação
=========================

Integra jornadas do ``automation_oca`` com leads nativos do CRM e com a fila de
mensagens do Contact Center. Não cria outro motor de jornadas.

Dependência validada: OCA/automation, branch ``16.0``, commit
``b8ebefd3f6f8084feadc25880c8224d0486c3aa5``.

Instalação
----------

Disponibilizar ``automation_oca`` no addons_path e instalar este addon.
A instalação não cria jornada nem habilita mensagens. A ação nativa de criação
de lead apenas procura jornadas explicitamente habilitadas; sem elas, não cria
jobs, conversas ou envios.

Configuração do piloto (posterior à publicação)
-----------------------------------------------

* Abrir Marketing Center > Automações. Neste primeiro piloto, configurar as
  jornadas de comunicação exige administrador do sistema e gerente OCA de
  automação. O usuário de execução pode ser um atendente com os acessos corretos.
* Criar uma jornada para CRM, escolher empresa, usuário de execução e domínio
  de origem/formulário. O usuário deve ter acesso ao lead e à caixa no Contact
  Center; não há envio com privilégios de administrador herdados do cron.
* Adicionar etapa ``Mensagem no Contact Center``, caixa e texto. Variáveis
  simples disponíveis: ``{{nome}}``, ``{{lead}}`` e ``{{empresa}}``.
* O responsável opcional preenche somente leads ainda sem vendedor. A
  atribuição de atendimento da caixa continua seguindo o Contact Center.
* Manter ``Permitir mensagens automáticas`` desmarcado até revisar o piloto.
  O teste da etapa simula sem abrir conversa, consultar endereço ou enviar.
* Ao habilitar os envios, o corte de data e ID é fixado automaticamente. Somente
  novos leads entram, inclusive no cron periódico OCA. Desligar e religar fixa
  novo corte e impede liberar etapas pendentes dos leads antigos.

Funcionamento
-------------

``base_automation`` identifica criação de leads. ``queue_job`` processa a entrada
após o commit, reaproveitando filtros, inscrições, passos e deduplicação OCA.
Cada etapa de Contact Center é novamente executada na fila; nunca chama o
provider na transação de criação do lead.

O UUID persistido na própria etapa identifica a admissão na outbox canônica do
Contact Center. O estado ``Enfileirada no Contact Center`` significa que o
pedido de envio foi aceito, e não que a mensagem foi entregue. Entrega e erros
do provider continuam visíveis na conversa/outbox. Erros de admissão ficam no
registro de etapa OCA; a retomada manual reaproveita o mesmo UUID.

Por padrão, uma resposta do cliente ou mensagem humana desde a criação do lead
interrompe a etapa e seus próximos passos. As mensagens com origem
``automation`` não contam como atendimento humano. O vendedor também pode
marcar ``Pausar mensagens automáticas`` no lead. Os filtros de cada etapa OCA
continuam disponíveis para condições de estágio e qualificação.
Essas condições são verificadas antes da admissão na outbox; pausar a jornada
não cancela uma mensagem já aceita pelo Contact Center.

Os gatilhos OCA de abertura, clique e resposta de e-mail não representam eventos
WhatsApp. A etapa Contact Center implementa seus próprios controles de parada.
Uma etapa genérica de código Python não é uma simulação garantida pelo OCA;
usar a etapa dedicada para mensagens e testar ações de servidor separadamente.

Validação
---------

Testes Odoo em ``tests/test_communication.py`` exercitam gates, corte de entrada,
repetição de inscrição, simulação, UUID, falha/retomada e interrupção por
atendimento. Transporte e consulta de endereço são simulados nos testes.
