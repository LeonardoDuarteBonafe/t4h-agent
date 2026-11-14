# Validação do time de Triagem N1 — 14/11/2026

Time funcionando localmente em http://localhost:8000. Triagista, especialista técnico e especialista administrativo utilizam exclusivamente `gpt-4o-mini` na API da organização. A aplicação faz chamadas reais, sem fallback para outro modelo.

## Resultados reais no mundo

| Cenário | Resultado confirmado |
| --- | --- |
| Dúvida de VPN | Infraestrutura, resposta da FAQ-01, sem chamado novo. |
| Notebook desligando | Associação ao chamado existente CH-0902 e envio à Infraestrutura. |
| Reembolso incompleto | Pergunta pelos dados faltantes; complemento preservado no mesmo caso. |
| Acesso ao CRM | CH-1001 aguardou a decisão de teste no canal humano; depois foi encaminhado à Segurança. Nenhum acesso foi concedido pela aplicação. |
| Férias | Bloqueio até decisão de teste no canal humano; Playwright preencheu a tela e confirmou FER-01. |
| Produto indisponível para todos | Incidente P0 INC-01, envio a Produto e pendência humana. |
| Remetente externo suspeito | Escalado à pessoa, sem resposta externa. |
| Recusa humana de reembolso | CH-1002 criado pelo caso foi cancelado; a recusa ficou no histórico. |
| Decisão de versão antiga | Bloqueada com HTTP 409. |

**Testes locais:** 62 testes e 10 subtestes passaram. Cobrem persistência, políticas, erro/timeout, reconciliação, aprovação versionada, concorrência de complemento, prevenção de repetição, associação por solicitante e recusa. O painel foi aberto com Playwright, mostrou `gpt-4o-mini` e não apresentou erro JavaScript.

## Como interpretar

Foram usados eventos, pessoas e decisões fictícios no mundo em modo manual/treino, sem injeção de erros. As primeiras tentativas revelaram falhas dos prompts; elas foram corrigidas e reprocessadas, preservando o histórico. A rodada oficial completa de 60 dias com falhas ainda não foi executada. Testes locais com dependências substituídas não representam aprovação dessa rodada.

O canal humano usa identificação declarada; não há autenticação corporativa. Encaminhar um pedido não prova resolução pelo setor. Os times do simulador recebem mensagens, mas não respondem como novos agentes. A implementação foi escrita no Codex; a restrição de autoria por Copilot da competição precisa ser considerada antes de uma entrega oficial.

Os detalhes das execuções ficam no histórico do painel e nas operações do mundo. Credenciais, banco e evidências locais não fazem parte deste repositório.
