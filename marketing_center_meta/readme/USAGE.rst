Formulários da Meta
~~~~~~~~~~~~~~~~~~~~

Em ``Marketing Center / Configuration / Meta Lead Ads Routes``, o botão
``Descobrir formulários na Meta`` consulta os formulários da página escolhida.
Selecione a página, o perfil de leitura dos leads e a conta de marketing da mesma
empresa e aplicativo. A conta precisa ter uma conexão de leitura Meta ativa.
A descoberta usa a credencial protegida da página; o perfil de leads será usado
para consultar as respostas dos formulários.

A lista informa quais formulários já estão configurados e quais são novos.
Escolha os novos formulários que deseja receber e o período da primeira coleta:
somente novas entradas, últimos sete, trinta ou noventa dias. Clique em
``Configurar selecionados``. As novas configurações terão criação automática de
leads no CRM desligada. Configurações existentes, inclusive as arquivadas,
permanecem preservadas.

O total informado pela Meta é um contador do formulário; não representa a
quantidade disponível no período escolhido. Formulário ativo não comprova que
seus anúncios estejam em veiculação. A descoberta consulta a lista quando o
botão é acionado; não cadastra formulários futuros sem seleção do usuário.

Sincronizar histórico
~~~~~~~~~~~~~~~~~~~~~~

Abra um formulário configurado e clique em ``Sincronizar histórico``. Escolha
sete, trinta ou noventa dias, ou uma data inicial dentro dos últimos noventa dias.
A consulta termina no momento em que a solicitação é enviada e recupera os
registros que a Meta disponibilizar nesse intervalo.

Essa ação funciona mesmo quando o formulário já teve coletas anteriores.
Reutiliza a identificação única das submissões para evitar duplicações e
preserva o ponto de continuação da coleta automática. Uma coleta em andamento
precisa terminar antes de iniciar outra solicitação para o mesmo formulário.
O período solicitado, usuário, data e andamento ficam registrados na aba
``Acompanhamento`` da configuração.

``Buscar entradas recentes`` acompanha a coleta incremental. A escolha
``Na primeira sincronização, importar`` serve para a configuração inicial;
depois da primeira coleta, use o botão de histórico para buscar períodos
anteriores. Configurações antigas com durações diferentes das opções em dias
aparecem como ``Período anterior preservado``, mantendo seu valor original.

A sincronização mantém a política de CRM da rota. Ela não envia mensagens nem
inicia conversas no WhatsApp. A decisão de encaminhar um contato qualificado ao
CRM continua sendo um processo separado.
