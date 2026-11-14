# Time de agentes — Triagem N1

Aplicação local para receber pedidos do mundo simulado do hackathon, classificar setor e impacto, consultar as bases oficiais, propor atendimento e executar somente as ações permitidas. A interface acompanha o pedido, as invocações dos agentes, as decisões humanas e os retornos das ferramentas.

Os três agentes usam exclusivamente **`gpt-4o-mini`**, pela API configurada da organização. Não há troca automática de modelo nem resposta simulada quando a API falha.

> Esta implementação foi produzida no Codex. O regulamento informado na conversa exige Copilot para escrever código; por isso, não presuma que esta implementação atende a essa condição da entrega oficial. Confirme a elegibilidade com a organização.

## 1. Entender o que está sendo executado

| Componente | Responsabilidade |
| --- | --- |
| `triagista` | Entende o pedido, identifica setor, impacto e destino. Depois revisa a proposta do especialista. |
| `especialista_tecnico` | Propõe atendimento de Infraestrutura, Produto e acessos rotineiros de Segurança, conforme FAQ e catálogo. |
| `especialista_administrativo` | Propõe atendimento de Pessoas, Financeiro, Operações, Marketing e Jurídico. |
| Pessoa responsável | Aprova ou recusa a proposta quando necessário e assume casos exclusivos de uma pessoa. |
| Orquestrador local | Mantém fila, histórico, validações, revisão, bloqueios de aprovação, correlação e execução das ferramentas. |

O caminho habitual é `triagista → especialista adequado → triagista revisor → execução permitida ou pessoa`. Um pedido usa o especialista pertinente; não precisa passar pelos dois especialistas. As funções são papéis distintos com instruções próprias, mesmo compartilhando o mesmo modelo.

A comunicação entre agentes ocorre por objetos estruturados com evento, histórico, fontes e proposta, registrados pela aplicação. Não é necessário copiar respostas de um chat para outro. O envio para os times da empresa ocorre pelas ferramentas do mundo e pelos destinatários cadastrados na base `times`. Os times simulados recebem os encaminhamentos, mas não respondem como outros agentes de IA.

```text
Pedido manual ou cenário do mundo
  → coleta e correlação de eventos
  → triagista: classificação
  → especialista técnico OU administrativo: proposta fundamentada
  → triagista: revisão
  → validações de código e eventual aprovação humana
  → FAQ / pedido de dados / chamado / incidente / encaminhamento / tela de férias
  → retorno confirmado, histórico local e registros do mundo
```

Suspeitas de golpe, vazamento, acesso indevido e outros casos exclusivos de uma pessoa interrompem o caminho automático. P0/P1 de Produto exigem escalonamento humano, além do registro previsto. A aprovação do revisor de IA confirma a adequação da proposta; **não substitui aprovação humana de negócio**.

## 2. Iniciar nesta máquina

Abra o PowerShell na pasta `solucao-triagem-n1` e execute:

```powershell
.\iniciar.ps1
```

Se a política local impedir executar o script, use diretamente o Python do ambiente, sem mudar a política do computador:

```powershell
.\.venv\Scripts\python.exe -m triagem_n1 --with-world
```

Deixe o terminal aberto e acesse:

- Aplicação: <http://localhost:8000>
- Painel oficial do mundo: <http://localhost:8765>
- Tela legada de férias: <http://localhost:8765/legado/ferias>
- Diagnóstico da aplicação: <http://localhost:8000/health>

O comando reutiliza o mundo que já estiver ouvindo na porta configurada. Quando não houver serviço nessa porta, inicia `mundo.py` na pasta irmã `Servidor MCP (Compartilhado)`. Portanto, mantenha essa pasta do projeto original junto da solução. Se outro programa ocupar a porta, confirme que é realmente o mundo correto.

O atendimento inicia automaticamente quando `AUTO_START=true`. A interface também permite iniciar ou pausar a coleta. Pausar mantém os registros disponíveis; não desfaz operações já efetuadas. Para encerrar o processo local, use `Ctrl+C` no terminal. Um mundo iniciado pelo próprio comando é encerrado junto dele; um mundo já existente continua sob responsabilidade de seu processo original.

## 3. Preparar um ambiente do zero

Pré-requisitos: Windows, Python 3.11 ou superior disponível no terminal, acesso à internet para instalar dependências e consultar a API, pasta do mundo original e credencial válida da organização. Se `python` abrir a Microsoft Store, use o executável de uma instalação real do Python.

Na pasta `solucao-triagem-n1`:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

O Chromium do Playwright é necessário para operar férias pela interface legada. As outras integrações usam o mundo por MCP.

Crie a configuração local apenas se ela ainda não existir:

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

Abra `.env` em seu editor e preencha a chave localmente. Não publique esse arquivo, nem inclua a chave em capturas, mensagens ou commits. Preserve a configuração existente ao preparar novamente o ambiente.

| Variável | Uso e valor esperado |
| --- | --- |
| `LLM_API_KEY` | Chave privada da API; obrigatória para iniciar os agentes. |
| `LLM_BASE_URL` | `https://litellm.tech4humans.com.br/v1`. A configuração também aceita o endereço completo terminado em `/chat/completions` e normaliza esse sufixo. |
| `LLM_MODEL` | Se definida, deve ser exatamente `gpt-4o-mini`. Outro valor bloqueia a aplicação. |
| `MCP_URL` | Endereço do mundo; padrão `http://localhost:8765/mcp`. |
| `RUN_ID` | Identificador da rodada, por exemplo `demo-triagem-001`. Veja a regra de reinício abaixo. |
| `APP_HOST` / `APP_PORT` | Padrões `127.0.0.1` e `8000`. Mantenha a aplicação local neste protótipo. |
| `AUTO_START` | `true` inicia a coleta ao abrir a aplicação; `false` aguarda o botão no painel. |
| `BROWSER_HEADLESS` | `true` opera o Chromium sem janela; `false` permite acompanhar a janela de férias em uma demonstração. |
| `WORLD_MANUAL` | `true`, padrão, inicia um novo mundo sem cenário automático, para demonstração por pedidos manuais. `false` permite o cenário. |
| `WORLD_ERRORS` | Parâmetro de erros do mundo iniciado pelo comando; padrão `0`. |
| `WORLD_DAY_SECONDS` | Duração de um dia simulado no novo mundo; padrão `1800`. |

As três últimas variáveis só afetam um mundo **novo** iniciado pelo comando. Elas não alteram um servidor que já estava em execução. O `RUN_ID` padrão de desenvolvimento não é uma forma automática de reconhecer que o mundo foi reiniciado.

## 4. Cuidar da rodada e da memória

Use o mesmo `RUN_ID` ao reiniciar somente a aplicação, mantendo exatamente o mesmo mundo e seus dados. Isso permite recuperar o histórico local da rodada.

Use um **novo `RUN_ID` toda vez que reiniciar ou resetar o mundo**. Os IDs de eventos do mundo podem se repetir em uma nova simulação. Reutilizar a rodada pode misturar decisões anteriores com eventos novos.

Exemplo no terminal, antes de iniciar:

```powershell
$env:RUN_ID = 'demo-triagem-002'
.\.venv\Scripts\python.exe -m triagem_n1 --with-world
```

Também é possível ajustar `RUN_ID` no `.env` antes de iniciar. Uma variável já definida no ambiente do terminal tem precedência sobre o arquivo. Não troque o identificador enquanto o processo estiver aberto. Não apague o banco local como procedimento habitual de reinício.

Os dados ficam em `data/triagem.sqlite3`, separados pelo identificador da rodada; as capturas de férias, em `data/evidencias/`. O log de um mundo iniciado pelo comando fica em `data/mundo-processo.log`. Esses arquivos não substituem as bases oficiais do mundo e não devem ser versionados.

## 5. Enviar e acompanhar um pedido

1. Abra a aplicação e confira a conexão com o mundo, o identificador da rodada e `gpt-4o-mini` na barra de estado.
2. Se necessário, clique em **Iniciar atendimento**.
3. Clique em **Novo pedido**. Os botões VPN, Notebook, Reembolso, Acesso e Suspeita oferecem exemplos fictícios editáveis.
4. Informe nome, remetente interno fictício `@t4h.exemplo`, assunto e descrição. Use dados fictícios e descreva o impacto observado.
5. Clique em **Enviar pedido** uma vez. A aplicação cria um evento manual no mundo; não é uma mensagem desconectada do simulador.
6. Selecione o caso na caixa de atendimento. Confira setor, criticidade quando aplicável, dados faltantes, fonte e ação proposta.
7. Abra **Agentes** para conferir classificação, proposta e revisão efetivamente registradas. Nem todo caso tem três invocações: uma regra exclusiva de pessoa pode interromper o fluxo antes disso.
8. Abra **Operações** para verificar o que foi solicitado e o retorno confirmado das ferramentas. Confira também os IDs no painel do mundo.

Exemplo inicial: “Meu notebook desliga durante o uso, sou a única pessoa afetada, patrimônio NB-123”. O setor esperado é Infraestrutura, e não um incidente de Produto apenas por haver a palavra “desliga”. O atendimento deve usar FAQ aplicável ou catálogo e dados necessários; não deve inventar diagnóstico de bateria ou afirmar que consertou o equipamento.

Quando faltarem dados, o caso fica aguardando complemento. Para um complemento manual, use o mesmo remetente e inclua explicitamente o ID do evento original no assunto ou corpo. Isso ajuda a correlação; não use mensagens semelhantes de pessoas diferentes como se fossem o mesmo atendimento. O simulador também pode produzir respostas conforme suas próprias regras.

## 6. Participar como pessoa responsável

Abra **Pendências humanas** ou o filtro **Pessoa**, selecione o caso e leia a proposta, os campos e o histórico antes de decidir.

- **Aguardando aprovação:** preencha “Seu nome e papel” e a justificativa. **Aprovar proposta** autoriza somente a proposta e a versão exibidas. **Recusar** registra a decisão humana e cancela apenas o chamado criado por este caso que ainda esteja aguardando aprovação; um chamado anterior apenas associado não é cancelado. Se o registro mudou ou o mundo não confirmar a atualização, a recusa fica salva e o caso pede conferência humana. Um chamado pode já existir no mundo com estado “aguardando aprovação”; isso não significa que a execução foi autorizada.
- **Escalado:** registre o recebimento e o encaminhamento humano. Esse registro muda a responsabilidade para uma pessoa; não libera automaticamente ações sensíveis nem equivale a resolver o caso.
- **Proposta atualizada:** se aparecer “Conferir nova versão”, releia o novo conteúdo antes de confirmar. Uma decisão para versão antiga é recusada pelo servidor.

O nome e o papel são **identidade declarada no protótipo local**. Não existe autenticação corporativa que comprove que a pessoa seja realmente gestora ou tenha autorização de negócio. Para produção, essa decisão precisa estar vinculada a identidade autenticada e autorização verificável.

## 7. Férias: operação pela tela e evidência

1. Crie um pedido fictício de férias com remetente interno e datas de início e fim em `DD/MM/AAAA`, posteriores ou iguais à data simulada do mundo.
2. Confira a proposta administrativa e a revisão.
3. Verifique que o caso aguarda aprovação humana e que não houve lançamento antes da aprovação.
4. Aprove somente se período, pessoa e proposta estiverem corretos.
5. A aplicação abre o Chromium e opera exclusivamente a tela `/legado/ferias`. Não utiliza uma chamada direta ao endpoint de gravação do legado.
6. Confira protocolo `FER-...`, período e pessoa no retorno da operação e na tela do mundo.
7. Confira a captura em `data/evidencias/`, quando produzida. A API serve um arquivo pelo endereço `/api/evidence/NOME-DO-ARQUIVO.png`; use somente o nome encontrado na operação, sem o caminho de pasta.

Para ver o navegador durante uma demonstração, defina `BROWSER_HEADLESS=false` antes de iniciar a aplicação. O navegador só abre quando o fluxo chegar à operação de férias.

A ferramenta procura um lançamento igual na tabela visível antes de enviar. Essa conferência é limitada pelo conteúdo exibido na tela; não oferece unicidade global. Se houver resultado incerto, confira o legado e os registros antes de qualquer nova tentativa.

## 8. Validar de forma verificável

Em 14/11/2026, a suíte local passou com **62 testes e 10 subtestes**. Também foram executados, com API real `gpt-4o-mini` e mundo real em modo manual: FAQ VPN em Infraestrutura; associação do notebook ao CH-0902; coleta de dados de reembolso e complemento no mesmo caso; acesso pendente seguido de decisão no canal humano e encaminhamento do CH-1001; férias pendentes seguidas de decisão no canal humano e lançamento pela tela com FER-01; incidente P0 INC-01 e escalonamento humano; remetente externo bloqueado. A recusa humana cancelou o CH-1002 criado pelo caso, e decisão com versão antiga retornou HTTP 409.

Os dados e as decisões usados nessa validação são fictícios e pertencem ao teste local. As primeiras tentativas revelaram falhas de classificação e revisão; os ajustes foram reprocessados mantendo as trilhas. Não foi executada a rodada oficial completa de 60 dias com injeção de falhas. O roteiro abaixo permite repetir a aceitação em uma nova rodada.

| Cenário | O que verificar |
| --- | --- |
| Dúvida de VPN | Infraestrutura, FAQ existente citada quando aplicável, resposta confirmada e ausência de chamado desnecessário. |
| Notebook desligando | Impacto individual identificado, sem diagnóstico inventado; campos do catálogo solicitados ou reutilizados conforme o histórico. |
| Reembolso | Especialista administrativo, Financeiro, requisitos do catálogo e aprovação quando exigida. |
| Acesso rotineiro | Catálogo de Segurança, dados completos e aprovação humana; não confundir com conta invadida. |
| Suspeita de conta invadida | Escalonamento exclusivo para pessoa, sem resposta técnica automática ou acesso concedido. |
| Produto indisponível para todos | P0 fundamentado no impacto, incidente e escalonamento humano; sem declarar resolução pelo simples encaminhamento. |
| Férias | Aprovação exata antes do lançamento, operação pela tela, protocolo e evidência conferidos. |
| Nova proposta após complemento | Versão anterior não pode autorizar a nova proposta. |
| Reinício da aplicação | Mesma rodada e mesmo mundo preservam histórico; conferir se não houve repetição de efeito. |
| Erro ou confirmação incerta | Erro visível, sem resposta inventada; operação ambígua não é repetida às cegas. |

Para registrar um resultado, anote cenário, `RUN_ID`, ID do evento, ID do caso, fonte, proposta/versão, decisão humana se houver, ID do registro no mundo e retorno da operação. Uma captura de tela isolada não prova o fluxo completo.

Exporte o estado e as operações da rodada em <http://localhost:8000/api/export>. O arquivo inclui conteúdo dos pedidos e decisões; revise antes de compartilhar. O painel do mundo contém os indicadores oficiais, enquanto os cartões da aplicação são indicadores locais de acompanhamento.

Testes automatizados locais:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Os arquivos em `tests/` verificam regras de política, persistência, orquestração e fronteira com o mundo, incluindo cenários com dependências substituídas. Eles não substituem o teste de aceitação com a API real, o mundo e a tela legada. Registre o resultado do comando que você efetivamente executar, sem presumir aprovação a partir deste documento.

## 9. Diagnosticar problemas

| Sintoma | Próximo passo |
| --- | --- |
| Painel não abre | Confira o terminal e se a porta 8000 está disponível. Não inicie várias cópias da aplicação. |
| Mundo desconectado | Abra o painel 8765; confira `MCP_URL`, a pasta irmã do mundo e `data/mundo-processo.log`, quando existir. |
| API não configurada ou HTTP 401/403 | Confira a configuração local e a validade/permissão da credencial, sem colá-la no chat. |
| HTTP 429 ou falha da API | Aguarde a disponibilidade/cota do serviço e confira o erro do caso. A aplicação mantém `gpt-4o-mini`. |
| Chromium não encontrado | Execute a instalação do Chromium com o Python da `.venv`, conforme o passo 3. |
| Mundo encerrado | Encerre a rodada, inicie outro mundo e use outro `RUN_ID`. |
| Resultado incerto | Confira primeiro o registro oficial e as operações. Não crie outro pedido só para repetir a ação. |
| Setor ou proposta incorreta | Não aprove; registre evidência e ajuste instruções/regras com validação de regressão. Saída do modelo não é garantia de correção. |

## 10. Estrutura e limites para evoluir

- `triagem_n1/agents.py`: instruções e contratos dos três papéis; chamadas reais ao mesmo modelo.
- `triagem_n1/runtime.py`: coleta, correlação, fluxo, revisão, decisões e execução.
- `triagem_n1/policy.py`: limites e verificações independentes do texto do modelo.
- `triagem_n1/world.py`: cliente MCP e tratamento conservador de falhas.
- `triagem_n1/browser.py`: operação restrita da tela de férias por Playwright.
- `triagem_n1/store.py`: memória local e registro de casos, decisões e operações.
- `triagem_n1/app.py`: API e entrega do painel local.
- `triagem_n1/web/`: interface sem dependência de CDN.
- `tests/`: verificações automatizadas locais.

Para incluir outro agente, primeiro defina um setor ou responsabilidade que o time atual ainda não cubra, suas fontes autorizadas, contrato de entrada/saída e ações permitidas. Depois implemente o destino no contrato de classificação, as instruções do novo papel, o roteamento no orquestrador, as validações e a identificação no painel. Só adicionar um novo prompt não conecta um agente à aplicação. Mantenha a revisão e os bloqueios humanos; confirme cenários do novo papel e regressão dos existentes antes de demonstrar.

Este é um protótipo local, de processo único, para o simulador. Não há SSO, autorização corporativa verificada, isolamento multiempresa, fila distribuída ou garantia de execução exatamente uma vez entre aplicação e mundo. O histórico local e as tentativas de reconciliação reduzem repetições, mas falhas de transporte podem deixar efeitos incertos e exigir conferência. O mundo e o modelo podem apresentar respostas e classificações incorretas; os estados “encaminhado” e “respondido” não provam resolução técnica pelo time destinatário.
