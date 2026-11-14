# t4h-agent — Time de agentes de Triagem N1

Aplicação local para classificar e atender pedidos do mundo simulado do Hackathon UNIFEI 2026 da Tech for Humans. O time usa exclusivamente **`gpt-4o-mini`** na API configurada pela organização.

## Time e funcionamento

| Papel | Responsabilidade |
| --- | --- |
| Triagista | Classifica setor e impacto; revisa a proposta do especialista. |
| Especialista técnico | Infraestrutura, Produto e acessos rotineiros de Segurança. |
| Especialista administrativo | Pessoas, Financeiro, Operações, Marketing e Jurídico. |
| Pessoa responsável | Aprova ou recusa pedidos e assume casos exclusivos de atendimento humano. |

Fluxo: **evento → triagista → especialista pertinente → revisão do triagista → validações → execução ou decisão humana**.

A integração usa MCP real: `proximo_evento`, `listar_eventos`, `ler_evento`, `consultar`, `registrar`, `atualizar`, `responder_evento`, `enviar_mensagem` e `registrar_conversa`. Férias são registradas pela tela legada com Playwright, após decisão humana. O modelo propõe o plano; a aplicação executa as ferramentas respeitando os bloqueios.

## Executar no Windows

Requer Python 3.12, Git, acesso à API e uma chave válida. No PowerShell:

```powershell
git clone https://github.com/LeonardoDuarteBonafe/t4h-agent.git
Set-Location t4h-agent/solucao-triagem-n1
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
notepad .env
```

Preencha `LLM_API_KEY` no arquivo local. O endereço padrão é `https://litellm.tech4humans.com.br/v1`; mantenha `LLM_MODEL=gpt-4o-mini`. A chave não faz parte deste repositório.

Depois de salvar a configuração:

```powershell
.\.venv\Scripts\python.exe -m triagem_n1 --with-world
```

Também pode usar `.\iniciar.ps1`. O comando inicia o mundo incluído quando a porta 8765 estiver livre e abre o serviço da aplicação na porta 8000.

- Aplicação: http://localhost:8000
- Mundo MCP: http://localhost:8765
- Tela legada de férias: http://localhost:8765/legado/ferias

O modo padrão é manual, sem injeção de falhas, para criar pedidos pelo botão **Novo pedido**. Use somente dados fictícios. Para uma nova simulação do mundo, altere `RUN_ID` no `.env`; mantenha-o quando reiniciar apenas a aplicação na mesma rodada.

## Acompanhar os agentes

Clique em um ticket. A aba **Atendimento** mostra a proposta e as decisões humanas; **Agentes** apresenta classificação, proposta, revisão e correções; **Operações** mostra chamadas e retornos confirmados. Em `responder_evento`, o argumento `mensagem` contém a resposta enviada. O painel atualiza a cada dois segundos enquanto está visível.

## Testar

Na pasta `solucao-triagem-n1`, com as dependências instaladas:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Validação registrada em 14/11/2026: **62 testes e 10 subtestes passaram**, além dos fluxos com API real no simulador. Veja [resultados e limites da validação](docs/VALIDACAO.md).

## Estrutura e documentação

```text
t4h-agent/
  solucao-triagem-n1/          # aplicação, painel e testes
  Servidor MCP (Compartilhado)/ # simulador oficial preservado
  docs/
```

- [Guia detalhado de configuração e operação](solucao-triagem-n1/README.md)
- [Origem do simulador oficial](docs/ORIGEM-MUNDO.md)
- [Resultados da validação](docs/VALIDACAO.md)
- [Descrição do desafio](https://hackathon.techforhumans.com.br/desafio#o-desafio)

Este é um protótipo local com identificação humana declarada, sem SSO. Um encaminhamento não representa resolução técnica pelo setor. A rodada oficial completa de 60 dias com falhas ainda não foi executada. Esta implementação foi escrita no Codex; a exigência de autoria por Copilot deve ser considerada antes de uma entrega oficial.
