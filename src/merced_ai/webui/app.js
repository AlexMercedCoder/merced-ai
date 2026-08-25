const state = {
  data: { workspace: "", profiles: [], bots: [], sessions: [], harnesses: [] },
  activeBot: "",
  activeSession: "",
  harnessOverride: "",
  activeRun: "",
  pendingPrompt: "",
  lastPrompt: "",
  view: "conversations",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const escapeHtml = (value = "") => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");
const titleCase = (value = "") => value.replaceAll("-", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
const selected = (left, right) => left === right ? " selected" : "";
const ready = (probe) => Boolean(probe?.path) && probe.status !== "probe_failed" && probe.status !== "incompatible";

function markdown(value = "") {
  const blocks = String(value).split("```");
  return blocks.map((block, index) => {
    const safe = escapeHtml(block);
    if (index % 2) {
      const firstBreak = safe.indexOf("\n");
      const code = firstBreak >= 0 ? safe.slice(firstBreak + 1) : safe;
      return `<div class="code-block"><button type="button" class="copy-code">Copy</button><pre><code>${code}</code></pre></div>`;
    }
    return safe.split(/\n{2,}/).filter(Boolean).map((paragraph) => `<p>${paragraph
      .replaceAll("\n", "<br>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")}</p>`).join("");
  }).join("");
}

async function api(path, options = {}) {
  const headers = { ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers };
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).detail || message; } catch { /* non-JSON error */ }
    throw new Error(message);
  }
  return response;
}

async function authenticate() {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const token = fragment.get("token");
  if (token) {
    await api("/api/auth", { method: "POST", body: JSON.stringify({ token }) });
    history.replaceState(null, "", `${location.pathname}${location.search}`);
  }
}

async function refresh({ quiet = false } = {}) {
  if (!quiet) setBusy(true, "Refreshing workspace…");
  state.data = await (await api("/api/bootstrap")).json();
  if (!state.activeBot || !state.data.bots.some((bot) => bot.name === state.activeBot)) {
    state.activeBot = state.data.bots[0]?.name || "";
  }
  if (!state.activeSession || !state.data.sessions.some((item) => item.id === state.activeSession)) {
    state.activeSession = state.data.sessions.find((item) => item.bot_name === state.activeBot)?.id || "";
  }
  render();
  setBusy(false);
}

function currentBot() { return state.data.bots.find((item) => item.name === state.activeBot); }
function currentSession() { return state.data.sessions.find((item) => item.id === state.activeSession); }
function currentProfile() {
  const bot = currentBot();
  return bot && state.data.profiles.find((item) => item.name === bot.profile || item.path === bot.profile);
}
function activeHarnessId() { return state.harnessOverride || currentSession()?.harness_id || currentBot()?.harness.preferred || ""; }
function activeProbe() { return state.data.harnesses.find((item) => item.harness_id === activeHarnessId()); }

function render() {
  const { profiles, bots, sessions, harnesses, workspace } = state.data;
  $("#session-count").textContent = sessions.length;
  $("#bot-count").textContent = bots.length;
  $("#profile-count").textContent = profiles.length;
  $("#harness-count").textContent = harnesses.filter(ready).length;
  $("#workspace-name").textContent = workspace.split(/[\\/]/).filter(Boolean).at(-1) || workspace;
  renderSelectors();
  renderRecents();
  renderConversation();
  renderInspector();
  if (state.view !== "conversations") renderManagement();
}

function renderSelectors() {
  const botSelect = $("#bot-select");
  botSelect.innerHTML = `<option value="">Select a bot</option>${state.data.bots.map((bot) => `<option value="${escapeHtml(bot.name)}"${selected(bot.name, state.activeBot)}>${escapeHtml(titleCase(bot.name))}</option>`).join("")}`;
  const harnessSelect = $("#harness-select");
  harnessSelect.innerHTML = `<option value="">Bot default</option>${state.data.harnesses.map((probe) => `<option value="${escapeHtml(probe.harness_id)}"${selected(probe.harness_id, state.harnessOverride)} ${ready(probe) ? "" : "disabled"}>${escapeHtml(titleCase(probe.harness_id))} · ${escapeHtml(probe.status)}</option>`).join("")}`;
  harnessSelect.disabled = !currentBot() || Boolean(state.activeRun);
}

function renderRecents() {
  const query = $("#session-search").value.trim().toLowerCase();
  const sessions = state.data.sessions.filter((session) => {
    const text = `${session.bot_name} ${session.turns.map((turn) => turn.content).join(" ")}`.toLowerCase();
    return !query || text.includes(query);
  });
  $("#recent-list").innerHTML = sessions.length ? sessions.slice(0, 30).map((item) => `
    <button class="recent-item${selected(item.id, state.activeSession)}" data-session="${escapeHtml(item.id)}">
      <strong>${escapeHtml(item.turns[0]?.content || `${titleCase(item.bot_name)} conversation`)}</strong>
      <small>${escapeHtml(titleCase(item.bot_name))} · ${new Date(item.updated_at).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}</small>
    </button>`).join("") : `<div class="empty-mini">${query ? "No matching conversations" : "No conversations yet"}</div>`;
}

function messageTemplate(turn) {
  if (turn.role === "user") return `<article class="message user"><div class="message-body">${markdown(turn.content)}</div></article>`;
  return `<article class="message assistant"><div class="message-avatar">✦</div><div class="message-body">${markdown(turn.content)}<div class="message-meta"><span>Harness response</span><span>Profile-pinned</span></div></div></article>`;
}

function renderConversation() {
  const bot = currentBot();
  const profile = currentProfile();
  const session = currentSession();
  $("#conversation-title").textContent = bot ? (session?.turns.length ? `Working with ${titleCase(bot.name)}` : `Meet ${titleCase(bot.name)}`) : "Your harness-native collaborator";
  $("#welcome-copy").textContent = profile?.description || "Choose a bot to start a secure local conversation.";
  $("#message-list").innerHTML = session?.turns.map(messageTemplate).join("") || "";
  $("#export-session").disabled = !session;
  $("#message-input").disabled = !bot || Boolean(state.activeRun);
  $("#send-message").disabled = !bot || Boolean(state.activeRun);
  bindCopyButtons();
}

async function renderInspector() {
  const bot = currentBot();
  const profile = currentProfile();
  const harnessId = activeHarnessId();
  const probe = activeProbe();
  $("#inspector-name").textContent = bot ? titleCase(bot.name) : "No bot selected";
  $("#inspector-description").textContent = profile?.description || "Create an OAP profile and bind it to a harness.";
  $("#route-profile").textContent = profile?.name || bot?.profile || "—";
  $("#route-harness").textContent = harnessId ? titleCase(harnessId) : "—";
  $("#active-harness").textContent = harnessId ? `${titleCase(harnessId)} · ${probe?.status || "unknown"}` : "No route";
  [$("#route-status-dot"), $("#inspector-route-dot")].forEach((dot) => dot.classList.toggle("online", ready(probe)));
  const installed = state.data.harnesses.filter(ready);
  $("#healthy-count").textContent = `${installed.length} ready`;
  $("#harness-list").innerHTML = state.data.harnesses.map((item) => `
    <button class="harness-item" data-harness="${escapeHtml(item.harness_id)}" ${ready(item) ? "" : "disabled"}>
      <span class="status-dot ${ready(item) ? "online" : ""}"></span><strong>${escapeHtml(titleCase(item.harness_id))}</strong><small>${escapeHtml(item.status)}</small>
    </button>`).join("");
  $("#projection-level").textContent = "—";
  $("#approval-level").textContent = "—";
  $("#adjustment-list").innerHTML = "";
  if (!bot) return;
  try {
    const suffix = state.harnessOverride ? `?harness=${encodeURIComponent(state.harnessOverride)}` : "";
    const projection = await (await api(`/api/projection/${encodeURIComponent(bot.name)}${suffix}`)).json();
    if (bot.name !== state.activeBot) return;
    $("#projection-level").textContent = projection.support_level;
    $("#approval-level").textContent = projection.approval_required ? "Ask before run" : "Profile-denied";
    $("#adjustment-list").innerHTML = projection.adjustments.slice(0, 5).map((item) => `<div class="adjustment"><span>●</span><div><strong>${escapeHtml(item.action)}</strong> · ${escapeHtml(item.field)}<br>${escapeHtml(item.reason)}</div></div>`).join("");
  } catch (error) { $("#adjustment-list").textContent = error.message; }
}

function showView(view) {
  state.view = view;
  $$(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  const conversations = view === "conversations";
  $("#conversation-view").hidden = !conversations;
  $("#management-view").hidden = conversations;
  $("#conversation-view").classList.toggle("active-view", conversations);
  if (!conversations) renderManagement();
  closeNavigation();
}

function renderManagement() {
  const view = state.view;
  const copy = {
    bots: ["BOT BINDINGS", "Bots", "Bind portable OAP profiles to preferred and fallback harnesses."],
    profiles: ["OPEN AGENT PROFILE", "Profiles", "Create and validate portable identities, models, and permission requests."],
    harnesses: ["LOCAL RUNTIMES", "Harnesses", "Inspect detection, versions, capabilities, and readiness."],
  }[view];
  if (!copy) return;
  [$("#management-eyebrow").textContent, $("#management-title").textContent, $("#management-copy").textContent] = copy;
  $("#management-action").hidden = view === "harnesses";
  $("#management-action").textContent = view === "profiles" ? "Create profile" : "Create bot";
  if (view === "profiles") {
    $("#management-list").innerHTML = state.data.profiles.map((item) => `<article class="management-card"><div><span class="card-kicker">REVISION ${item.revision} · ${escapeHtml(item.source)}</span><h2>${escapeHtml(titleCase(item.name))}</h2><p>${escapeHtml(item.description)}</p><div class="tag-row"><span>${escapeHtml(item.model?.provider || "harness model")}</span><span>${escapeHtml(item.model?.id || "default")}</span><span>edit: ${escapeHtml(item.permissions?.edit || "inherit")}</span><span>shell: ${escapeHtml(item.permissions?.shell || "inherit")}</span></div></div><button class="secondary-button edit-profile" data-profile="${escapeHtml(item.name)}" ${item.editable ? "" : "disabled"}>${item.editable ? "Edit" : "Read only"}</button></article>`).join("") || emptyState("No profiles", "Create an OAP profile before making a bot.");
  } else if (view === "bots") {
    $("#management-list").innerHTML = state.data.bots.map((item) => `<article class="management-card"><div><span class="card-kicker">${escapeHtml(item.source)} BINDING</span><h2>${escapeHtml(titleCase(item.name))}</h2><p>${escapeHtml(item.profile)} → ${escapeHtml(titleCase(item.harness.preferred))}</p><div class="tag-row">${item.harness.fallbacks.map((value) => `<span>fallback: ${escapeHtml(value)}</span>`).join("") || "<span>No fallbacks</span>"}</div></div><button class="secondary-button use-bot" data-bot="${escapeHtml(item.name)}">Open</button></article>`).join("") || emptyState("No bots", "Create a profile, then bind it to an installed harness.");
  } else {
    $("#management-list").innerHTML = state.data.harnesses.map((item) => `<article class="management-card"><div><span class="card-kicker">${escapeHtml(item.status)}</span><h2><span class="status-dot ${ready(item) ? "online" : ""}"></span> ${escapeHtml(titleCase(item.harness_id))}</h2><p>${escapeHtml(item.path || "Executable not found")}</p><div class="tag-row"><span>${escapeHtml(item.transport || "no transport")}</span><span>${item.capabilities.streaming ? "streaming" : "atomic"}</span><span>${item.capabilities.approvals ? "approvals" : "no approval bridge"}</span></div></div><small>${escapeHtml((item.version || "No version reported").split("\n")[0])}</small></article>`).join("");
  }
}

function emptyState(title, copy) { return `<div class="empty-state"><div class="bot-orb large">✦</div><h2>${title}</h2><p>${copy}</p></div>`; }
function setBusy(busy, message = "") { $("#thread").setAttribute("aria-busy", String(busy)); if (message) $("#composer-status").textContent = message; }
function toast(message) { const node = $("#toast"); node.textContent = message; node.hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => { node.hidden = true; }, 3500); }

async function selectBot(name) {
  state.activeBot = name;
  state.harnessOverride = "";
  state.activeSession = state.data.sessions.find((item) => item.bot_name === name)?.id || "";
  showView("conversations");
  render();
}

async function createConversation() {
  if (!state.activeBot) {
    if (!state.data.bots.length) { showView("bots"); openBotEditor(); return; }
    state.activeBot = state.data.bots[0].name;
  }
  try {
    const session = await (await api("/api/sessions", { method: "POST", body: JSON.stringify({ bot_name: state.activeBot, harness: state.harnessOverride || null }) })).json();
    state.data.sessions.unshift(session);
    state.activeSession = session.id;
    render();
    $("#message-input").focus();
    toast("New conversation ready");
  } catch (error) { toast(error.message); }
}

async function ensureSession() {
  if (currentSession()) return currentSession();
  await createConversation();
  return currentSession();
}

function parseSseBlock(block) {
  let event = "message";
  const data = [];
  block.split("\n").forEach((line) => {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trim());
  });
  return { event, payload: data.length ? JSON.parse(data.join("\n")) : {} };
}

async function approval() {
  const dialog = $("#approval-dialog");
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "approve"), { once: true });
    dialog.showModal();
  });
}

async function sendPrompt(approved = false) {
  const content = state.pendingPrompt || $("#message-input").value.trim();
  if (!content || state.activeRun) return;
  const session = await ensureSession();
  if (!session) return;
  state.pendingPrompt = content;
  state.lastPrompt = content;
  setBusy(true, approved ? "Starting approved harness run…" : "Checking effective authority…");
  $("#message-input").disabled = true;
  $("#send-message").disabled = true;
  try {
    const response = await api(`/api/sessions/${encodeURIComponent(session.id)}/messages`, { method: "POST", body: JSON.stringify({ content, approved }) });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done }).replaceAll("\r\n", "\n");
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() || "";
      for (const block of blocks.filter(Boolean)) {
        const item = parseSseBlock(block);
        if (item.event === "approval_required") {
          setBusy(false, item.payload.authority);
          if (await approval()) return sendPrompt(true);
          state.pendingPrompt = "";
          return;
        }
        if (item.event === "run_started") {
          state.activeRun = item.payload.run_id;
          session.turns.push({ role: "user", content });
          $("#message-input").value = "";
          $("#cancel-run").hidden = false;
          $("#composer-status").textContent = `Running through ${titleCase(item.payload.harness_id)}…`;
          renderConversation();
          scrollThread();
        } else if (item.event === "tool_event") {
          addActivity("Harness activity", summarizeEvent(item.payload.event));
        } else if (item.event === "assistant_message") {
          session.turns.push({ role: "assistant", content: item.payload.content });
          renderConversation();
          $("#composer-status").textContent = `${titleCase(item.payload.harness_id)} completed in ${(item.payload.duration_ms / 1000).toFixed(1)}s`;
          scrollThread();
        } else if (item.event === "run_error" || item.event === "run_cancelled") {
          addActivity(item.event === "run_cancelled" ? "Run cancelled" : "Harness error", item.payload.message, true, item.event === "run_error");
          $("#composer-status").textContent = item.payload.message;
        }
      }
      if (done) break;
    }
  } catch (error) {
    addActivity("Connection error", error.message, true, true);
    $("#composer-status").textContent = error.message;
  } finally {
    state.activeRun = "";
    state.pendingPrompt = "";
    $("#cancel-run").hidden = true;
    $("#message-input").disabled = !currentBot();
    $("#send-message").disabled = !currentBot();
    setBusy(false);
    await refresh({ quiet: true });
  }
}

function summarizeEvent(event) {
  if (typeof event?.type === "string") return titleCase(event.type);
  if (typeof event?.name === "string") return event.name;
  return "Structured event received";
}
function addActivity(title, copy, error = false, retry = false) { $("#activity-list").insertAdjacentHTML("beforeend", `<div class="activity ${error ? "error" : ""}"><span>${error ? "!" : "↗"}</span><div><strong>${escapeHtml(title)}</strong><small>${escapeHtml(copy)}</small></div>${retry ? '<button type="button" class="retry-run">Retry</button>' : ""}</div>`); scrollThread(); }
function scrollThread() { requestAnimationFrame(() => $("#thread").scrollTo({ top: $("#thread").scrollHeight, behavior: "smooth" })); }

async function cancelRun() {
  if (!state.activeRun) return;
  try { await api(`/api/runs/${encodeURIComponent(state.activeRun)}/cancel`, { method: "POST" }); $("#composer-status").textContent = "Cancellation requested…"; } catch (error) { toast(error.message); }
}

function field(label, name, value = "", options = {}) {
  const id = `field-${name}`;
  if (options.select) return `<label for="${id}">${label}<select id="${id}" name="${name}" ${options.required ? "required" : ""}>${options.placeholder ? `<option value="">${escapeHtml(options.placeholder)}</option>` : ""}${options.select.map((item) => `<option value="${escapeHtml(item.value ?? item)}"${selected(item.value ?? item, value)}>${escapeHtml(item.label ?? titleCase(item))}</option>`).join("")}</select></label>`;
  if (options.textarea) return `<label for="${id}" class="full-field">${label}<textarea id="${id}" name="${name}" rows="${options.rows || 5}" ${options.required ? "required" : ""}>${escapeHtml(value)}</textarea></label>`;
  return `<label for="${id}">${label}<input id="${id}" name="${name}" value="${escapeHtml(value)}" ${options.required ? "required" : ""} ${options.readonly ? "readonly" : ""} /></label>`;
}

function openEditor({ title, eyebrow, fields, submit }) {
  $("#editor-title").textContent = title;
  $("#editor-eyebrow").textContent = eyebrow;
  $("#editor-fields").innerHTML = fields;
  $("#editor-error").textContent = "";
  $("#editor-form").onsubmit = async (event) => {
    event.preventDefault();
    const button = $("#editor-submit");
    button.disabled = true;
    try {
      await submit(Object.fromEntries(new FormData(event.currentTarget)));
      $("#editor-dialog").close();
      await refresh({ quiet: true });
      renderManagement();
      toast(`${title} saved`);
    } catch (error) { $("#editor-error").textContent = error.message; }
    finally { button.disabled = false; }
  };
  $("#editor-dialog").showModal();
}

function profileFields(profile = {}) {
  const permissionOptions = [{ value: "", label: "Inherit harness policy" }, "ask", "allow", "deny"];
  return field("Profile name", "name", profile.name, { required: true, readonly: Boolean(profile.name) })
    + field("Description", "description", profile.description, { required: true })
    + field("Instructions", "instructions", profile.instructions, { textarea: true, rows: 8, required: true })
    + field("Model provider", "model_provider", profile.model?.provider || "")
    + field("Model ID", "model_id", profile.model?.id || "")
    + field("Edit permission", "edit_permission", profile.permissions?.edit || "", { select: permissionOptions })
    + field("Shell permission", "shell_permission", profile.permissions?.shell || "", { select: permissionOptions });
}

function profilePayload(values) { return { ...values, model_provider: values.model_provider || null, model_id: values.model_id || null, edit_permission: values.edit_permission || null, shell_permission: values.shell_permission || null }; }
function openProfileEditor(profile = null) {
  openEditor({
    title: profile ? "Edit profile" : "Create profile",
    eyebrow: "OPEN AGENT PROFILE",
    fields: profileFields(profile || {}),
    submit: async (values) => api(profile ? `/api/profiles/${encodeURIComponent(profile.name)}` : "/api/profiles", { method: profile ? "PUT" : "POST", body: JSON.stringify(profilePayload(values)) }),
  });
}

function openBotEditor() {
  if (!state.data.profiles.length) { showView("profiles"); openProfileEditor(); return; }
  const harnesses = state.data.harnesses.filter(ready).map((item) => item.harness_id);
  openEditor({
    title: "Create bot",
    eyebrow: "BOT BINDING",
    fields: field("Bot name", "name", "", { required: true })
      + field("OAP profile", "profile", "", { required: true, placeholder: "Choose a profile", select: state.data.profiles.map((item) => item.name) })
      + field("Preferred harness", "harness", "", { required: true, placeholder: "Choose a harness", select: harnesses })
      + field("Fallback harnesses", "fallbacks", "", { placeholder: "Comma-separated, in priority order" }),
    submit: async (values) => api("/api/bots", { method: "POST", body: JSON.stringify({ ...values, fallbacks: values.fallbacks.split(",").map((item) => item.trim()).filter(Boolean) }) }),
  });
}

function bindCopyButtons() { $$(".copy-code").forEach((button) => button.addEventListener("click", async () => { await navigator.clipboard.writeText(button.nextElementSibling.textContent); button.textContent = "Copied"; setTimeout(() => { button.textContent = "Copy"; }, 1200); })); }
function openNavigation() { $("#sidebar").classList.add("open"); $("#sidebar-backdrop").hidden = false; $("#close-navigation").focus(); }
function closeNavigation() { $("#sidebar").classList.remove("open"); $("#sidebar-backdrop").hidden = true; }

function bindEvents() {
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
  $("#bot-select").addEventListener("change", (event) => selectBot(event.target.value));
  $("#harness-select").addEventListener("change", async (event) => { state.harnessOverride = event.target.value; if (currentSession()) { state.activeSession = ""; await createConversation(); } else { render(); } });
  $("#recent-list").addEventListener("click", (event) => { const button = event.target.closest("[data-session]"); if (!button) return; const session = state.data.sessions.find((item) => item.id === button.dataset.session); state.activeSession = session.id; state.activeBot = session.bot_name; state.harnessOverride = ""; showView("conversations"); render(); closeNavigation(); });
  $("#session-search").addEventListener("input", renderRecents);
  $("#refresh-data").addEventListener("click", () => refresh().catch(showFatal));
  $("#new-thread").addEventListener("click", createConversation);
  $("#composer").addEventListener("submit", (event) => { event.preventDefault(); sendPrompt(); });
  $("#message-input").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#composer").requestSubmit(); } });
  $("#message-input").addEventListener("input", (event) => { event.target.style.height = "auto"; event.target.style.height = `${Math.min(event.target.scrollHeight, 180)}px`; });
  $("#cancel-run").addEventListener("click", cancelRun);
  $("#activity-list").addEventListener("click", (event) => { if (event.target.closest(".retry-run") && state.lastPrompt) { state.pendingPrompt = state.lastPrompt; sendPrompt(); } });
  $("#management-action").addEventListener("click", () => state.view === "profiles" ? openProfileEditor() : openBotEditor());
  $("#management-list").addEventListener("click", (event) => { const edit = event.target.closest(".edit-profile"); const use = event.target.closest(".use-bot"); if (edit) openProfileEditor(state.data.profiles.find((item) => item.name === edit.dataset.profile)); if (use) selectBot(use.dataset.bot); });
  $("#harness-list").addEventListener("click", (event) => { const button = event.target.closest("[data-harness]"); if (button && !currentSession()?.turns.length) { state.harnessOverride = button.dataset.harness; render(); } });
  $("#export-session").addEventListener("click", () => { const session = currentSession(); if (session) location.href = `/api/sessions/${encodeURIComponent(session.id)}/export`; });
  $("#focus-route").addEventListener("click", () => $("#harness-select").focus());
  $("#toggle-inspector").addEventListener("click", () => { const hidden = $("#inspector").classList.toggle("closed"); $("#toggle-inspector").setAttribute("aria-expanded", String(!hidden)); });
  $("#close-inspector").addEventListener("click", () => { $("#inspector").classList.add("closed"); $("#toggle-inspector").setAttribute("aria-expanded", "false"); });
  $("#open-navigation").addEventListener("click", openNavigation);
  $("#close-navigation").addEventListener("click", closeNavigation);
  $("#sidebar-backdrop").addEventListener("click", closeNavigation);
  $("#logout").addEventListener("click", async () => { await api("/api/logout", { method: "POST" }); location.reload(); });
  $("#theme-toggle").addEventListener("click", () => { const theme = document.body.dataset.theme === "light" ? "dark" : "light"; document.body.dataset.theme = theme; localStorage.setItem("merced-ai-theme", theme); });
  document.addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); createConversation(); } if (event.key === "Escape") closeNavigation(); });
}

function showFatal(error) {
  $("#conversation-title").textContent = "UI connection unavailable";
  $("#welcome-copy").textContent = error.message;
  $("#composer-status").textContent = "Restart merced-ai ui to create a fresh local session.";
  setBusy(false);
}

async function boot() {
  const platform = navigator.userAgentData?.platform || navigator.platform || "";
  $("#shortcut-label").textContent = /mac/i.test(platform) ? "⌘ K" : "Ctrl K";
  document.body.dataset.theme = localStorage.getItem("merced-ai-theme") || "dark";
  bindEvents();
  await authenticate();
  await refresh();
}

boot().catch(showFatal);
