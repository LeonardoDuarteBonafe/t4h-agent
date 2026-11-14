"use strict";

const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => Array.from(parent.querySelectorAll(selector));
const state = { snapshot: null, cases: [], selectedId: null, filter: "all", search: "", view: "overview", polling: false, detailPolling: false, detailSignature: "", decisionMode: "", pendingEvent: null };
const treated = new Set(["respondido", "encaminhado", "resolvido", "concluido", "concluído"]);
const humanStates = new Set(["aguardando_aprovacao", "aguardando aprovação", "escalado", "aguardando_humano"]);
const statuses = { novo: "Novo", lido: "Recebido", recebido: "Recebido", analisando: "Em análise", processando: "Em análise", aguardando_dados: "Aguardando informação", aguardando_aprovacao: "Aguardando aprovação", "aguardando aprovação": "Aguardando aprovação", aguardando_humano: "Aguardando pessoa", escalado: "Escalado à pessoa", com_humano: "Com a pessoa responsável", aprovado: "Proposta aprovada", encaminhado: "Encaminhado", respondido: "Respondido", resolvido: "Resolvido", concluido: "Concluído", "concluído": "Concluído", erro: "Precisa de atenção", ambiguo: "Resultado incerto", "ambíguo": "Resultado incerto", recusado: "Proposta recusada", cancelado: "Cancelado", ferias_lancadas: "Férias lançadas" };
const agents = { triagista: "Triagista", especialista_tecnico: "Especialista técnico", especialista_administrativo: "Especialista administrativo", coordenador: "Coordenação" };
const stages = { triagem: "Classificação do pedido", analise: "Análise do especialista", análise: "Análise do especialista", proposta: "Proposta de atendimento", revisao: "Revisão da proposta", revisão: "Revisão da proposta", decisao: "Decisão", decisão: "Decisão", consulta: "Consulta às fontes", erro: "Detalhe da execução", execution: "Execução", classificacao: "Classificação do pedido" };
const actions = { responder_faq: "Responder com a FAQ", pedir_dados: "Solicitar informações", registrar_chamado: "Registrar chamado", registrar_incidente: "Registrar incidente", vincular: "Vincular ao registro existente", encaminhar: "Encaminhar ao time responsável", ferias: "Lançar férias pela tela", escalar: "Encaminhar a uma pessoa" };

function el(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined && text !== null) node.textContent = String(text); return node; }
function valueText(value) { if (value === null || value === undefined || value === "") return "Não informado"; return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value); }
function proposalForView(raw) {
  const kindActions = { faq: "responder_faq", request: "registrar_chamado", incident: "registrar_incidente", followup: "vincular", vacation: "ferias", handoff: "encaminhar", human: "escalar", need_info: "pedir_dados" };
  const kindTypes = { faq: "Dúvida atendida pela FAQ", incident: "Incidente de produto", followup: "Acompanhamento de registro", vacation: "Pedido de férias", handoff: "Encaminhamento", human: "Escalação para pessoa", need_info: "Pedido em complementação" };
  const sources = raw.fonte_ids || raw.fontes || raw.sources || [];
  const sourceList = Array.isArray(sources) ? [...sources] : sources ? [sources] : [];
  if (raw.faq_id && !sourceList.some(item => item === raw.faq_id)) sourceList.unshift(raw.faq_id);
  return { ...raw, acao: raw.acao || kindActions[raw.kind], tipo_catalogo: raw.tipo_catalogo || raw.catalog_type || kindTypes[raw.kind], campos: raw.campos || raw.fields || {}, campos_faltantes: raw.campos_faltantes || raw.missing || [], aprovacao_necessaria: raw.aprovacao_necessaria ?? raw.requires_approval, registro_existente: raw.registro_existente || raw.existing, resposta_sugerida: raw.resposta_sugerida || raw.response, resumo: raw.resumo || raw.summary, fonte_ids: sourceList };
}
function normalize(value) { return String(value ?? "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase(); }
function isHuman(c) { return humanStates.has(c.status) || Boolean(c.needs_human && !treated.has(c.status) && c.status !== "recusado"); }
function statusClass(status) { if (treated.has(status) || status === "ferias_lancadas") return "success"; if (humanStates.has(status)) return "human"; if (["erro", "ambiguo", "ambíguo", "recusado"].includes(status)) return "danger"; if (["analisando", "processando"].includes(status)) return "working"; return "neutral"; }
function badge(c) { return el("span", `badge ${statusClass(c.status)}`, statuses[c.status] || String(c.status || "Recebido").replaceAll("_", " ")); }
function when(value) { if (!value) return ""; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" }); }

async function api(path, options = {}) {
  const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 90000);
  try {
    const response = await fetch(path, { ...options, signal: controller.signal, headers: { "Content-Type": "application/json", ...options.headers } });
    let payload; try { payload = await response.json(); } catch { payload = {}; }
    if (!response.ok) { const detail = payload.detail ?? payload.error ?? payload.message; throw new Error(typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : `A solicitação retornou ${response.status}.`); }
    return payload;
  } catch (error) { if (error.name === "AbortError") throw new Error("A solicitação está demorando. Confira o estado do atendimento antes de repetir uma ação."); throw error; }
  finally { clearTimeout(timer); }
}

let toastTimer;
function notify(message, error = false) { const node = $("#toast"); node.textContent = message; node.classList.toggle("is-error", error); node.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { node.hidden = true; }, error ? 9000 : 5000); }
function setLoading(button, loading, label) { if (loading) { button.dataset.previousLabel = button.textContent; button.textContent = label || "Aguarde…"; } else if (button.dataset.previousLabel) { button.textContent = button.dataset.previousLabel; delete button.dataset.previousLabel; } button.disabled = loading; }

async function refresh() {
  if (state.polling) return; state.polling = true;
  try {
    const snapshot = await api("/api/state"); state.snapshot = snapshot; state.cases = Array.isArray(snapshot.cases) ? snapshot.cases : [];
    $("#connection-error").hidden = !snapshot.error;
    if (snapshot.error) $("#connection-error").textContent = `O atendimento precisa de atenção. ${valueText(snapshot.error)}`;
    const world = $("#world-status"); world.classList.toggle("connected", Boolean(snapshot.world_connected)); world.replaceChildren(el("span", "status-dot"), document.createTextNode(snapshot.world_connected ? "Mundo conectado" : "Mundo sem conexão"));
    $("#run-id").textContent = snapshot.run_id || "Não identificada"; $("#model-name").textContent = snapshot.model || "Não configurado";
    $("#last-sync").textContent = `Atualizado às ${new Date().toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
    const toggle = $("#runtime-toggle"); if (!toggle.disabled) toggle.textContent = snapshot.running ? "Pausar atendimento" : "Iniciar atendimento";
    toggle.classList.toggle("is-running", Boolean(snapshot.running)); $("#sidebar-dot").classList.toggle("running", Boolean(snapshot.running));
    const runtime = $("#sidebar-runtime"); runtime.replaceChildren(document.createTextNode(snapshot.running ? "Atendimento em andamento" : "Atendimento pausado"), el("small", "", snapshot.running ? "O time está acompanhando os pedidos" : "Os registros continuam disponíveis"));
    $("#metric-total").textContent = state.cases.length; $("#metric-human").textContent = state.cases.filter(isHuman).length;
    $("#metric-treated").textContent = state.cases.filter(c => treated.has(c.status) || c.status === "ferias_lancadas").length;
    $("#metric-data").textContent = state.cases.filter(c => c.status === "aguardando_dados").length;
    $("#nav-case-count").textContent = state.cases.length; $("#nav-human-count").textContent = state.cases.filter(isHuman).length; $("#filter-human-count").textContent = state.cases.filter(isHuman).length;
    if (state.pendingEvent) { const incoming = state.cases.find(c => Array.isArray(c.event_ids) && c.event_ids.includes(state.pendingEvent)); if (incoming) { state.pendingEvent = null; selectCase(incoming.id); } }
    renderCases(); if (state.selectedId) await refreshDetail();
  } catch (error) { $("#connection-error").textContent = `Não foi possível atualizar o painel. ${error.message}`; $("#connection-error").hidden = false; }
  finally { state.polling = false; }
}

function setFilter(filter) { state.filter = filter; $$(".filter").forEach(b => b.classList.toggle("active", b.dataset.filter === filter)); $("#nav-human").classList.toggle("active", filter === "human"); $("#nav-all").classList.toggle("active", filter !== "human"); renderCases(); }

function renderCases() {
  const list = $("#case-list"); const scroll = list.scrollTop;
  const query = normalize(state.search);
  const cases = state.cases.filter(c => {
    if (state.filter === "human" && !isHuman(c)) return false;
    if (state.filter === "treated" && !treated.has(c.status) && c.status !== "ferias_lancadas") return false;
    if (state.filter === "active" && (treated.has(c.status) || c.status === "ferias_lancadas" || c.status === "recusado")) return false;
    return !query || normalize([c.id, c.subject, c.sender, c.sector, c.summary].join(" ")).includes(query);
  });
  const fragment = document.createDocumentFragment();
  for (const c of cases) {
    const button = el("button", `case-item${c.id === state.selectedId ? " selected" : ""}`); button.type = "button"; button.dataset.caseId = c.id; button.setAttribute("aria-pressed", String(c.id === state.selectedId));
    const top = el("div", "case-topline"); top.append(el("span", "case-reference", c.id || "Caso"), el("span", "case-time", when(c.updated_at))); button.append(top);
    button.append(el("h3", "", c.subject || "Pedido sem assunto")); button.append(el("p", "case-person", c.sender || "Remetente não informado"));
    const bottom = el("div", "case-bottomline"); bottom.append(badge(c));
    if (c.criticality) bottom.append(el("span", `criticality criticality-${String(c.criticality).toLowerCase()}`, c.criticality));
    if (c.sector) bottom.append(el("span", "sector-label", c.sector)); button.append(bottom);
    button.addEventListener("click", () => selectCase(c.id)); fragment.append(button);
  }
  list.replaceChildren(fragment); list.scrollTop = scroll; $("#empty-cases").hidden = cases.length > 0;
  if (!cases.length && state.cases.length) { $("#empty-cases h3").textContent = "Nenhum caso encontrado"; $("#empty-cases p").textContent = "Altere a busca ou o filtro para encontrar outros atendimentos."; $("#empty-intake").hidden = true; }
  else { $("#empty-cases h3").textContent = "Sua caixa está pronta"; $("#empty-cases p").textContent = "Inicie o atendimento e crie um pedido para acompanhar os agentes trabalhando juntos."; $("#empty-intake").hidden = false; }
  $("#list-count").textContent = `${cases.length} ${cases.length === 1 ? "caso nesta visão" : "casos nesta visão"}`;
}

function selectCase(id) { if (state.selectedId !== id) { state.selectedId = id; state.detailSignature = ""; state.decisionMode = ""; $("#decision-note").value = ""; $("#decision-error").hidden = true; } renderCases(); refreshDetail(); }
function setView(view) { state.view = view; $$(".detail-tab").forEach(b => { const selected = b.dataset.view === view; b.classList.toggle("active", selected); b.setAttribute("aria-selected", String(selected)); }); $$(".detail-view").forEach(v => { v.hidden = v.id !== `view-${view}`; }); }
function section(title) { const node = el("section", "overview-section"); node.append(el("h3", "section-title", title)); return node; }
function keyValue(parent, key, value) { const row = el("div", "key-value"); row.append(el("dt", "", key), el("dd", "", valueText(value))); parent.append(row); }

async function refreshDetail() {
  if (!state.selectedId || state.detailPolling) return; state.detailPolling = true; const requestedId = state.selectedId;
  try {
    const payload = await api(`/api/cases/${encodeURIComponent(requestedId)}`); if (requestedId !== state.selectedId) return;
    const signature = JSON.stringify(payload); if (signature === state.detailSignature) return; state.detailSignature = signature;
    const c = payload.case || payload; const proposal = proposalForView(c.proposal || {});
    $("#inspector-placeholder").hidden = true; $("#case-detail").hidden = false;
    $("#detail-id").textContent = c.id || requestedId; $("#detail-subject").textContent = c.subject || "Pedido sem assunto"; $("#detail-sender").textContent = c.sender || "Remetente não informado"; $("#detail-status").replaceChildren(badge(c));
    const labels = $("#detail-labels"); labels.replaceChildren(); if (c.sector) labels.append(el("span", "sector-pill", c.sector)); if (c.criticality) labels.append(el("span", `criticality criticality-${String(c.criticality).toLowerCase()}`, c.criticality));
    const overview = $("#overview-content"); overview.replaceChildren();
    if (c.summary || proposal.resumo) { const node = section("Entendimento do pedido"); node.append(el("p", "", c.summary || proposal.resumo)); overview.append(node); }
    const plan = section("Próximo passo"); const fields = el("dl", "key-values");
    keyValue(fields, "Ação proposta", actions[proposal.acao] || proposal.acao || (c.status === "analisando" ? "Análise em andamento" : "Ainda não definida"));
    keyValue(fields, "Tipo de atendimento", proposal.tipo_catalogo || proposal.tipo || "Em classificação");
    if (proposal.aprovacao_necessaria !== undefined) keyValue(fields, "Aprovação humana", proposal.aprovacao_necessaria ? "Necessária" : "Sem exigência nesta proposta");
    if (proposal.registro_existente) keyValue(fields, "Registro relacionado", proposal.registro_existente);
    plan.append(fields);
    if (Array.isArray(proposal.campos_faltantes) && proposal.campos_faltantes.length) { plan.append(el("p", "missing-fields", `Informações pendentes: ${proposal.campos_faltantes.join(", ")}`)); }
    if (proposal.resposta_sugerida) { plan.append(el("h4", "small-heading", "Resposta proposta"), el("p", "response-preview", proposal.resposta_sugerida)); }
    overview.append(plan);
    if (proposal.campos && Object.keys(proposal.campos).length) { const node = section("Informações reunidas"); const fields = el("dl", "key-values"); Object.entries(proposal.campos).forEach(([k, v]) => keyValue(fields, k.replaceAll("_", " "), v)); node.append(fields); overview.append(node); }
    const sources = proposal.fonte_ids || proposal.fontes || []; if (sources.length) { const node = section("Fontes consultadas"); const wrap = el("div", "chip-list"); (Array.isArray(sources) ? sources : [sources]).forEach(s => wrap.append(el("span", "source-chip", valueText(s)))); node.append(wrap); overview.append(node); }
    if (c.world_records && (Array.isArray(c.world_records) ? c.world_records.length : Object.keys(c.world_records).length)) { const node = section("Registros confirmados"); node.append(el("pre", "compact-json", JSON.stringify(c.world_records, null, 2))); overview.append(node); }
    if (Array.isArray(c.event_ids) && c.event_ids.length) { const node = section("Eventos deste caso"); const wrap = el("div", "chip-list"); c.event_ids.forEach(id => wrap.append(el("span", "source-chip", id))); node.append(wrap); overview.append(node); }
    if (c.error) { const node = section("Detalhe que precisa de atenção"); node.append(el("p", "case-error", valueText(c.error))); overview.append(node); }
    updateDecision(c);
    renderDecisions(payload.decisions || []);
    renderTraces(payload.traces || []); renderOperations(payload.operations || []); setView(state.view);
  } catch (error) { notify(`Não foi possível carregar o caso. ${error.message}`, true); }
  finally { state.detailPolling = false; }
}

function updateDecision(c) {
  const mode = ["aguardando_aprovacao", "aguardando aprovação"].includes(c.status) ? "approval" : ["escalado", "aguardando_humano"].includes(c.status) ? "escalation" : "";
  const form = $("#decision-form"); form.hidden = !mode;
  const nextVersion = String(c.version ?? "");
  const sameCaseAndMode = form.dataset.caseId === String(c.id) && mode === state.decisionMode;
  if (mode && sameCaseAndMode && form.dataset.version !== nextVersion) {
    form.dataset.stale = "true"; form.dataset.pendingVersion = nextVersion;
    $("#decision-error").textContent = `A proposta mudou para a versão ${nextVersion || "atual"}. Confira o atendimento novamente antes de decidir.`;
    $("#decision-error").hidden = false;
    $$("button[type=submit]", form).forEach(b => { b.disabled = true; });
    if (!$("#review-new-version")) {
      const review = el("button", "button button-secondary", "Conferir nova versão"); review.id = "review-new-version"; review.type = "button";
      review.addEventListener("click", () => { form.dataset.version = form.dataset.pendingVersion; delete form.dataset.stale; delete form.dataset.pendingVersion; $("#decision-version").textContent = form.dataset.version || "atual"; $("#decision-error").hidden = true; review.remove(); $$("button[type=submit]", form).forEach(b => { b.disabled = false; }); });
      $("#decision-actions").append(review);
    }
    return;
  }
  form.dataset.caseId = c.id; form.dataset.version = nextVersion;
  $("#decision-version").textContent = c.version ?? "atual";
  if (mode === state.decisionMode) return; state.decisionMode = mode;
  delete form.dataset.stale; delete form.dataset.pendingVersion;
  $("#decision-error").hidden = true;
  const actionsNode = $("#decision-actions"); actionsNode.replaceChildren();
  if (mode === "approval") {
    $("#decision-kicker").textContent = "APROVAÇÃO HUMANA"; $("#decision-title").textContent = "Esta proposta precisa de você"; $("#decision-description").textContent = "Confira a proposta e seus limites. A aprovação autoriza somente a ação e a versão apresentadas neste caso.";
    const reject = el("button", "button button-secondary reject-button", "Recusar"); reject.type = "submit"; reject.value = "reject"; reject.name = "decision";
    const approve = el("button", "button button-primary", "Aprovar proposta"); approve.type = "submit"; approve.value = "approve"; approve.name = "decision"; actionsNode.append(reject, approve);
  } else if (mode === "escalation") {
    $("#decision-kicker").textContent = "CASO ESCALADO À PESSOA"; $("#decision-title").textContent = "Registre o recebimento"; $("#decision-description").textContent = "Este caso precisa de decisão humana. Registrar o recebimento preserva a responsabilidade da pessoa e não libera automaticamente ações de negócio.";
    const ack = el("button", "button button-primary", "Registrar recebimento"); ack.type = "submit"; ack.value = "acknowledge"; ack.name = "decision"; actionsNode.append(ack);
  }
}

function renderDecisions(decisions) {
  const node = $("#decision-history"); node.replaceChildren(); if (!decisions.length) return;
  node.append(el("h3", "section-title", "Decisões registradas"));
  for (const item of decisions) { const card = el("div", "decision-record"); const label = { approve: "Proposta aprovada", reject: "Proposta recusada", acknowledge: "Recebimento registrado" }[item.decision] || item.decision || "Decisão"; card.append(el("strong", "", label), el("span", "record-meta", `${item.actor || "Pessoa responsável"} · ${when(item.timestamp || item.created_at)} · versão ${item.version ?? "—"}`)); if (item.note) card.append(el("p", "", item.note)); node.append(card); }
}

function renderTraces(traces) {
  $("#trace-count").textContent = traces.length; const node = $("#trace-list"); const opened = new Set($$("details[open]", node).map(d => d.dataset.key)); node.replaceChildren();
  if (!traces.length) { node.append(el("p", "muted", "As invocações aparecerão aqui quando o atendimento começar.")); return; }
  for (const [i, trace] of traces.entries()) {
    const agent = trace.agent || "Aplicação"; const item = el("article", "trace-item"); const icon = el("span", `trace-avatar ${agent === "triagista" ? "triage" : agent === "especialista_tecnico" ? "technical" : "admin"}`, agent === "triagista" ? "T" : agent === "especialista_tecnico" ? "TI" : agent === "especialista_administrativo" ? "AD" : "·");
    const content = el("div", "trace-content"); const top = el("div", "trace-top"); top.append(el("strong", "", agents[agent] || agent), el("time", "", when(trace.timestamp || trace.created_at))); content.append(top, el("p", "trace-stage", stages[trace.stage] || String(trace.stage || "Registro").replaceAll("_", " ")));
    const details = el("details", "trace-details"); details.dataset.key = String(trace.id ?? `${i}-${agent}-${trace.stage}`); details.open = opened.has(details.dataset.key); details.append(el("summary", "", "Ver resultado registrado"), el("pre", "compact-json", JSON.stringify(trace.data ?? trace.result ?? {}, null, 2))); content.append(details); item.append(icon, content); node.append(item);
  }
}

function renderOperations(operations) {
  const node = $("#operation-list"); node.replaceChildren();
  if (!operations.length) { node.append(el("p", "muted", "Nenhuma operação registrada para este caso.")); return; }
  for (const item of operations) { const card = el("article", "operation-record"); const header = el("div", "operation-heading"); header.append(el("strong", "", item.tool || "Operação"), el("span", "source-chip", item.status || "Registrada")); card.append(header); const details = el("details", "trace-details"); details.append(el("summary", "", "Conferir chamada e retorno"), el("pre", "compact-json", JSON.stringify({ argumentos: item.args || item.arguments || {}, resultado: item.result ?? null }, null, 2))); card.append(details); node.append(card); }
}

$("#decision-form").addEventListener("submit", async event => {
  event.preventDefault(); const form = event.currentTarget; if (!form.reportValidity()) return;
  if (form.dataset.stale === "true") { $("#decision-error").textContent = "Confira a nova versão da proposta antes de registrar uma decisão."; $("#decision-error").hidden = false; return; }
  const actor = $("#decision-actor").value.trim(); const note = $("#decision-note").value.trim(); if (!actor || !note) return;
  const decision = event.submitter?.value; if (!decision) return;
  const version = String(form.dataset.version ?? "");
  const buttons = $$("button", $("#decision-actions")); buttons.forEach(b => { b.disabled = true; }); $("#decision-error").hidden = true;
  try { await api(`/api/cases/${encodeURIComponent(form.dataset.caseId)}/decision`, { method: "POST", body: JSON.stringify({ actor, decision, version, note }) }); notify("Decisão registrada. Acompanhe o próximo passo do caso."); state.detailSignature = ""; await refresh(); }
  catch (error) { $("#decision-error").textContent = error.message; $("#decision-error").hidden = false; state.detailSignature = ""; }
  finally { buttons.forEach(b => { b.disabled = false; }); }
});

const examples = {
  vpn: { nome: "Marina Teste", de: "marina.teste@t4h.exemplo", assunto: "Configurar a VPN", corpo: "Estou em casa com um notebook novo e não sei como instalar ou configurar a VPN. Qual procedimento devo seguir?" },
  notebook: { nome: "Colaborador Teste", de: "colaborador.teste@t4h.exemplo", assunto: "Notebook desligando sozinho", corpo: "Meu notebook está desligando sozinho durante o trabalho, sem mostrar mensagem de erro. Preciso de ajuda." },
  reembolso: { nome: "Diego Teste", de: "diego.teste@t4h.exemplo", assunto: "Reembolso de almoço", corpo: "Paguei um almoço de trabalho com um cliente e quero solicitar reembolso. Quais informações vocês precisam?" },
  acesso: { nome: "Júlia Teste", de: "julia.teste@t4h.exemplo", assunto: "Acesso de leitura ao CRM", corpo: "Preciso de acesso ao CRM, somente leitura, para acompanhar a carteira de clientes da minha equipe. Meu gestor já está sabendo." },
  suspeita: { nome: "Suporte T4H de teste", de: "suporte@t4h-exemplo.com", assunto: "Validação de senha solicitada", corpo: "Solicitamos que informe sua senha atual para validar o acesso e evitar o bloqueio. Esta é uma mensagem fictícia para testar o fluxo de suspeita de golpe." }
};
function openIntake() { $("#intake-error").hidden = true; $("#intake-dialog").showModal(); }
function closeIntake() { $("#intake-dialog").close(); }
$("#open-intake").addEventListener("click", openIntake); $("#empty-intake").addEventListener("click", openIntake); $("#close-intake").addEventListener("click", closeIntake); $("#cancel-intake").addEventListener("click", closeIntake);
$$("[data-example]").forEach(button => button.addEventListener("click", () => { const example = examples[button.dataset.example]; $("#intake-name").value = example.nome; $("#intake-sender").value = example.de; $("#intake-subject").value = example.assunto; $("#intake-body").value = example.corpo; }));
$("#intake-form").addEventListener("submit", async event => {
  event.preventDefault(); const form = event.currentTarget; if (!form.reportValidity()) return;
  const payload = Object.fromEntries(new FormData(form)); Object.keys(payload).forEach(key => { payload[key] = payload[key].trim(); });
  const button = $("#submit-intake"); setLoading(button, true, "Enviando pedido…"); $("#intake-error").hidden = true;
  try { const result = await api("/api/intake", { method: "POST", body: JSON.stringify(payload) }); const id = result.id || result.evento_id || result.event?.id; state.pendingEvent = id || null; notify(id ? `Pedido ${id} recebido pelo mundo.` : "Pedido recebido pelo mundo."); closeIntake(); $("#intake-subject").value = ""; $("#intake-body").value = ""; setFilter("all"); state.search = ""; $("#case-search").value = ""; await refresh(); }
  catch (error) { $("#intake-error").textContent = error.message; $("#intake-error").hidden = false; }
  finally { setLoading(button, false); }
});
$("#runtime-toggle").addEventListener("click", async () => { const button = $("#runtime-toggle"); const action = state.snapshot?.running ? "stop" : "start"; setLoading(button, true, action === "start" ? "Iniciando…" : "Pausando…"); try { await api(`/api/runtime/${action}`, { method: "POST", body: "{}" }); notify(action === "start" ? "Atendimento iniciado." : "Coleta pausada. Os registros continuam disponíveis."); } catch (error) { notify(error.message, true); } finally { setLoading(button, false); await refresh(); } });
$$(".filter").forEach(b => b.addEventListener("click", () => setFilter(b.dataset.filter)));
$("#nav-all").addEventListener("click", () => setFilter("all")); $("#nav-human").addEventListener("click", () => setFilter("human"));
$("#case-search").addEventListener("input", event => { state.search = event.target.value; renderCases(); });
$$(".detail-tab").forEach(b => b.addEventListener("click", () => setView(b.dataset.view)));
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
refresh(); setInterval(() => { if (!document.hidden) refresh(); }, 2000);
