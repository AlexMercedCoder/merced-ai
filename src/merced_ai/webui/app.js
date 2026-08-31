const state = {
  data: { workspace: "", profiles: [], bots: [], sessions: [], harnesses: [] },
  activeBot: "",
  activeSession: "",
  harnessOverride: "",
  activeRun: "",
  pendingPrompt: "",
  lastPrompt: "",
  pendingDispatch: "",
  liveParticipants: {},
  groupDraft: [],
  deriveFrom: "",
  harnessDetection: { refreshing: false, updated_at: null, cached: false, stale: true },
  harnessPoll: 0,
  selectedContext: [],
  contextDraft: [],
  contextFiles: [],
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
const ready = (probe) => Boolean(probe?.path) && !["detecting", "not_installed", "probe_failed", "incompatible"].includes(probe.status);
const botHue = (name = "assistant") => [...name].reduce((value, char) => (value * 31 + char.charCodeAt(0)) % 360, 47);
const initials = (name = "AI") => name.split(/[-_\s]+/).map((part) => part[0]).join("").slice(0, 2).toUpperCase();
const identityStyle = (name) => `style="--bot-hue:${botHue(name)}"`;

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
  state.harnessDetection = state.data.harness_detection || state.harnessDetection;
  if (!state.activeBot || !state.data.bots.some((bot) => bot.name === state.activeBot)) {
    state.activeBot = state.data.bots[0]?.name || "";
  }
  if (!state.activeSession || !state.data.sessions.some((item) => item.id === state.activeSession)) {
    state.activeSession = state.data.sessions.find((item) => item.participants?.some((participant) => participant.bot_name === state.activeBot))?.id || "";
  }
  render();
  setBusy(false);
}

const pause = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function applyHarnessPayload(payload) {
  state.data.harnesses = payload.harnesses;
  state.harnessDetection = {
    refreshing: payload.refreshing,
    updated_at: payload.updated_at,
    cached: payload.cached,
    stale: payload.stale,
  };
  $("#harness-count").textContent = state.data.harnesses.filter(ready).length;
  renderSelectors();
  renderHarnessHealth();
  if (state.view === "harnesses") renderManagement();
}

async function refreshHarnesses({ announce = false } = {}) {
  const poll = ++state.harnessPoll;
  let payload = await (await api("/api/harnesses/refresh", { method: "POST" })).json();
  applyHarnessPayload(payload);
  while (payload.refreshing && poll === state.harnessPoll) {
    await pause(250);
    payload = await (await api("/api/harnesses")).json();
    applyHarnessPayload(payload);
  }
  if (announce && poll === state.harnessPoll) toast("Harness detection refreshed");
}

function currentBot() { return state.data.bots.find((item) => item.name === state.activeBot); }
function currentSession() { return state.data.sessions.find((item) => item.id === state.activeSession); }
function currentParticipants() {
  const session = currentSession();
  if (session?.participants?.length) return session.participants;
  return currentBot() ? [{ bot_name: currentBot().name, harness_id: activeHarnessId() }] : [];
}
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
  renderSelectedContext();
  if (state.view !== "conversations") renderManagement();
}

function renderSelectors() {
  const botSelect = $("#bot-select");
  const groupSession = currentSession()?.kind === "group" ? currentSession() : null;
  const groupOption = groupSession ? `<option value="__group__" selected>Group · ${escapeHtml(groupSession.title || `${groupSession.participants.length} bots`)}</option>` : "";
  botSelect.innerHTML = `<option value="">Select a bot</option>${groupOption}${state.data.bots.map((bot) => `<option value="${escapeHtml(bot.name)}"${groupSession ? "" : selected(bot.name, state.activeBot)}>${escapeHtml(titleCase(bot.name))}</option>`).join("")}`;
  const harnessSelect = $("#harness-select");
  harnessSelect.innerHTML = `<option value="">Bot default</option>${state.data.harnesses.map((probe) => `<option value="${escapeHtml(probe.harness_id)}"${selected(probe.harness_id, state.harnessOverride)} ${ready(probe) ? "" : "disabled"}>${escapeHtml(titleCase(probe.harness_id))} · ${escapeHtml(probe.status)}</option>`).join("")}`;
  harnessSelect.disabled = !currentBot() || currentParticipants().length > 1 || Boolean(state.activeRun);
  const dispatch = $("#dispatch-select");
  const participants = currentParticipants();
  const isGroup = participants.length > 1;
  dispatch.innerHTML = isGroup
    ? `<option value="mentions">Mentioned / first bot</option><option value="all">Ask everyone</option><option value="round_robin">Round robin</option>${participants.map((item) => `<option value="${escapeHtml(item.bot_name)}">Only ${escapeHtml(titleCase(item.bot_name))}</option>`).join("")}`
    : `<option value="mentions">Active bot</option>`;
  dispatch.value = isGroup ? (currentSession()?.mode || "mentions") : "mentions";
  dispatch.disabled = !currentBot() || Boolean(state.activeRun);
}

function renderRecents() {
  const query = $("#session-search").value.trim().toLowerCase();
  const sessions = state.data.sessions.filter((session) => {
    const text = `${(session.participants || []).map((item) => item.bot_name).join(" ")} ${session.bot_name} ${session.turns.map((turn) => turn.content).join(" ")}`.toLowerCase();
    return !query || text.includes(query);
  });
  $("#recent-list").innerHTML = sessions.length ? sessions.slice(0, 30).map((item) => `
    <button class="recent-item${selected(item.id, state.activeSession)}" data-session="${escapeHtml(item.id)}">
      <strong>${escapeHtml(item.title || item.turns[0]?.content || `${titleCase(item.bot_name)} conversation`)}</strong>
      <small>${escapeHtml((item.participants || [{ bot_name: item.bot_name }]).map((participant) => titleCase(participant.bot_name)).join(", "))} · ${new Date(item.updated_at).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}</small>
    </button>`).join("") : `<div class="empty-mini">${query ? "No matching conversations" : "No conversations yet"}</div>`;
}

function messageTemplate(turn) {
  if (turn.role === "user") return `<article class="message user"><div class="message-body">${markdown(turn.content)}</div></article>`;
  const speaker = turn.bot_name ? titleCase(turn.bot_name) : "Assistant";
  const body = turn.pending ? '<p class="pending-response">Waiting for this collaborator…</p>' : markdown(turn.content);
  return `<article class="message assistant" ${identityStyle(turn.bot_name)}><div class="message-avatar bot-identity" aria-label="${escapeHtml(speaker)}">${escapeHtml(initials(turn.bot_name))}</div><div class="message-body"><strong class="message-speaker">${escapeHtml(speaker)}</strong>${body}<div class="message-meta"><span>${escapeHtml(turn.harness_id || "Harness response")}</span><span>${turn.pending ? "In progress" : "Profile-pinned"}</span></div></div></article>`;
}

function renderConversation() {
  const bot = currentBot();
  const profile = currentProfile();
  const session = currentSession();
  const participants = currentParticipants();
  const group = participants.length > 1;
  $("#conversation-title").textContent = session?.title || (group ? `Collaborating with ${participants.length} bots` : bot ? (session?.turns.length ? `Working with ${titleCase(bot.name)}` : `Meet ${titleCase(bot.name)}`) : "Your harness-native collaborator");
  $("#welcome-copy").textContent = group ? "Use @bot to target collaborators, or choose Ask everyone or Round robin below." : profile?.description || "Choose a bot to start a secure local conversation.";
  $("#participant-list").innerHTML = participants.map((item) => `<span class="participant-chip">${escapeHtml(titleCase(item.bot_name))} · ${escapeHtml(titleCase(item.harness_id))}</span>`).join("");
  $("#message-list").innerHTML = session?.turns.map(messageTemplate).join("") || "";
  $("#export-session").disabled = !session;
  $("#rename-session").disabled = !session;
  $("#delete-session").disabled = !session || Boolean(state.activeRun);
  $("#derive-group").disabled = !session || state.data.bots.length < 2;
  $("#message-input").disabled = !bot || Boolean(state.activeRun);
  $("#send-message").disabled = !bot || Boolean(state.activeRun);
  bindCopyButtons();
}

function renderHarnessHealth() {
  const probe = activeProbe();
  const harnessId = activeHarnessId();
  $("#active-harness").textContent = harnessId ? `${titleCase(harnessId)} · ${probe?.status || "detecting"}` : "No route";
  [$("#route-status-dot"), $("#inspector-route-dot")].forEach((dot) => {
    dot.classList.toggle("online", ready(probe));
    dot.classList.toggle("detecting", probe?.status === "detecting");
  });
  const installed = state.data.harnesses.filter(ready);
  const detecting = state.data.harnesses.filter((item) => item.status === "detecting").length;
  $("#healthy-count").textContent = `${installed.length} ready`;
  const detectionState = $("#harness-detection-state");
  if (state.harnessDetection.refreshing) {
    detectionState.textContent = `Detecting ${state.data.harnesses.length - detecting}/${state.data.harnesses.length}`;
  } else if (state.harnessDetection.updated_at) {
    detectionState.textContent = state.harnessDetection.cached
      ? state.harnessDetection.stale ? "Previous result · refreshing" : "Cached · refreshing"
      : "Detection complete";
  } else {
    detectionState.textContent = "Detection queued";
  }
  $("#refresh-harnesses").disabled = state.harnessDetection.refreshing;
  $("#harness-list").setAttribute("aria-busy", String(state.harnessDetection.refreshing));
  $("#harness-list").innerHTML = state.data.harnesses.map((item) => `
    <button class="harness-item" data-harness="${escapeHtml(item.harness_id)}" ${ready(item) ? "" : "disabled"}>
      <span class="status-dot ${ready(item) ? "online" : ""} ${item.status === "detecting" ? "detecting" : ""}"></span><strong>${escapeHtml(titleCase(item.harness_id))}</strong><small>${escapeHtml(item.status === "detecting" ? "detecting…" : item.status)}</small>
    </button>`).join("");
}

async function renderInspector() {
  const bot = currentBot();
  const profile = currentProfile();
  const harnessId = activeHarnessId();
  const participants = currentParticipants();
  $("#inspector-name").textContent = participants.length > 1 ? (currentSession()?.title || `${participants.length} collaborators`) : bot ? titleCase(bot.name) : "No bot selected";
  $("#inspector-description").textContent = participants.length > 1 ? "Each collaborator keeps an independent profile, route, and approval boundary." : profile?.description || "Create an OAP profile and bind it to a harness.";
  $("#participant-count").textContent = participants.length;
  $("#inspector-participants").innerHTML = participants.map((item) => `<div class="inspector-participant" ${identityStyle(item.bot_name)}><div class="message-avatar bot-identity">${escapeHtml(initials(item.bot_name))}</div><div><strong>${escapeHtml(titleCase(item.bot_name))}</strong><small>${escapeHtml(titleCase(item.harness_id))} · ${escapeHtml(item.profile_name || "profile")}</small></div></div>`).join("");
  $("#route-profile").textContent = profile?.name || bot?.profile || "—";
  $("#route-harness").textContent = harnessId ? titleCase(harnessId) : "—";
  renderHarnessHealth();
  $("#projection-level").textContent = "—";
  $("#approval-level").textContent = "—";
  $("#adjustment-list").innerHTML = "";
  const runs = (state.data.recent_runs || []).filter((item) => !state.activeSession || item.session_id === state.activeSession);
  $("#run-count").textContent = runs.length;
  $("#run-history").innerHTML = runs.slice(0, 8).map((item) => `<article class="run-row"><span class="status-dot ${item.status === "completed" ? "online" : ""}"></span><div><strong>${escapeHtml(titleCase(item.status))}</strong><small>${escapeHtml(item.participants.map((participant) => titleCase(participant.bot_name)).join(", "))} · ${(item.duration_ms / 1000).toFixed(1)}s${item.context.length ? ` · ${item.context.length} context` : ""}</small></div></article>`).join("") || '<div class="empty-mini">No recorded runs</div>';
  $("#handoff-harness").disabled = !harnessId;
  $("#notification-toggle").textContent = localStorage.getItem("merced-ai-notifications") === "on" ? "Notifications on" : "Notify on completion";
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
  $("#management-action").hidden = false;
  $("#generate-profile").hidden = view !== "profiles";
  $("#management-action").disabled = view === "harnesses" && state.harnessDetection.refreshing;
  $("#management-action").textContent = view === "profiles" ? "Create profile" : view === "bots" ? "Create bot" : state.harnessDetection.refreshing ? "Detecting…" : "Refresh detection";
  if (view === "profiles") {
    $("#management-list").innerHTML = state.data.profiles.map((item) => `<article class="management-card"><div><span class="card-kicker">REVISION ${item.revision} · ${escapeHtml(item.source)}</span><h2>${escapeHtml(titleCase(item.name))}</h2><p>${escapeHtml(item.description)}</p><div class="tag-row"><span>${escapeHtml(item.model?.provider || "harness model")}</span><span>${escapeHtml(item.model?.id || "default")}</span><span>edit: ${escapeHtml(item.permissions?.edit || "inherit")}</span><span>shell: ${escapeHtml(item.permissions?.shell || "inherit")}</span></div>${item.warnings?.length ? `<div class="profile-warnings" role="status"><strong>Profile adjustments</strong>${item.warnings.map((warning) => `<span>${escapeHtml(warning)}</span>`).join("")}</div>` : ""}</div><div class="card-actions"><button class="secondary-button edit-profile" data-profile="${escapeHtml(item.name)}" ${item.editable ? "" : "disabled"}>${item.editable ? "Edit" : "Read only"}</button>${item.editable ? `<button class="secondary-button danger-button delete-profile" data-profile="${escapeHtml(item.name)}">Delete</button>` : ""}</div></article>`).join("") || emptyState("No profiles", "Create an OAP profile before making a bot.");
  } else if (view === "bots") {
    $("#management-list").innerHTML = state.data.bots.map((item) => `<article class="management-card"><div><span class="card-kicker">${escapeHtml(item.source)} BINDING</span><h2>${escapeHtml(titleCase(item.name))}</h2><p>${escapeHtml(item.profile)} → ${escapeHtml(titleCase(item.harness.preferred))}</p><div class="tag-row">${item.harness.fallbacks.map((value) => `<span>fallback: ${escapeHtml(value)}</span>`).join("") || "<span>No fallbacks</span>"}</div></div><div class="card-actions"><button class="secondary-button use-bot" data-bot="${escapeHtml(item.name)}">Open</button>${item.source === "project" ? `<button class="secondary-button edit-bot" data-bot="${escapeHtml(item.name)}">Edit</button><button class="secondary-button danger-button delete-bot" data-bot="${escapeHtml(item.name)}">Delete</button>` : ""}</div></article>`).join("") || emptyState("No bots", "Create a profile, then bind it to an installed harness.");
  } else {
    $("#management-list").innerHTML = state.data.harnesses.map((item) => `<article class="management-card"><div><span class="card-kicker">${escapeHtml(item.status === "detecting" ? "DETECTING…" : item.status)}</span><h2><span class="status-dot ${ready(item) ? "online" : ""} ${item.status === "detecting" ? "detecting" : ""}"></span> ${escapeHtml(titleCase(item.harness_id))}</h2><p>${escapeHtml(item.status === "detecting" ? "Checking executable and bounded version metadata…" : item.path || "Executable not found")}</p><div class="tag-row"><span>${escapeHtml(item.transport || "no transport")}</span><span>${item.capabilities.streaming ? "streaming" : "atomic"}</span><span>${item.capabilities.approvals ? "approvals" : "no approval bridge"}</span></div></div><small>${escapeHtml((item.version || (item.status === "detecting" ? "Previous result retained while checking" : "No version reported")).split("\n")[0])}</small></article>`).join("");
  }
}

function emptyState(title, copy) { return `<div class="empty-state"><div class="bot-orb large">✦</div><h2>${title}</h2><p>${copy}</p></div>`; }
function setBusy(busy, message = "") {
  $("#thread").setAttribute("aria-busy", String(busy));
  if (message || !busy) $("#composer-status").textContent = message;
}
function toast(message) { const node = $("#toast"); node.textContent = message; node.hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => { node.hidden = true; }, 3500); }

function renderSelectedContext() {
  $("#context-chips").innerHTML = state.selectedContext.map((item) => `<span>${escapeHtml(item.name || item.path)}<button type="button" data-remove-context="${escapeHtml(item.path)}" aria-label="Remove ${escapeHtml(item.name || item.path)}">×</button></span>`).join("");
}

function renderContextPicker() {
  const query = $("#context-search").value.trim().toLowerCase();
  const files = state.contextFiles.filter((item) => !query || item.path.toLowerCase().includes(query));
  $("#context-options").innerHTML = files.slice(0, 250).map((item) => `<label class="context-option"><input type="checkbox" value="${escapeHtml(item.path)}" ${state.contextDraft.some((selectedItem) => selectedItem.path === item.path) ? "checked" : ""} /><span><strong>${escapeHtml(item.path)}</strong><small>${Math.ceil(item.size / 1024)} KB · ${escapeHtml(item.media_type)}${item.readable ? "" : " · path reference"}</small></span></label>`).join("") || '<div class="empty-mini">No matching workspace files</div>';
}

async function openContextPicker() {
  const session = await ensureSession();
  if (!session) return;
  try {
    state.contextFiles = (await (await api("/api/context")).json()).files;
    state.contextDraft = [...state.selectedContext];
    $("#context-search").value = "";
    $("#context-error").textContent = "";
    renderContextPicker();
    $("#context-dialog").showModal();
  } catch (error) { toast(error.message); }
}

function fileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`Could not read ${file.name}`));
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.readAsDataURL(file);
  });
}

async function uploadContextFiles(files) {
  const session = currentSession();
  if (!session) return;
  $("#context-error").textContent = "";
  try {
    for (const file of files) {
      const response = await api(`/api/sessions/${encodeURIComponent(session.id)}/attachments`, { method: "POST", body: JSON.stringify({ name: file.name, media_type: file.type || "application/octet-stream", content_base64: await fileBase64(file) }) });
      const uploaded = await response.json();
      state.contextFiles.unshift(uploaded);
      state.contextDraft.push(uploaded);
    }
    renderContextPicker();
  } catch (error) { $("#context-error").textContent = error.message; }
}

async function copyHandoff() {
  const harnessId = activeHarnessId();
  if (!harnessId) return;
  try {
    const payload = await (await api(`/api/handoff/${encodeURIComponent(harnessId)}`)).json();
    const command = `cd ${JSON.stringify(payload.workspace)} && ${payload.argv.map((part) => JSON.stringify(part)).join(" ")}`;
    await navigator.clipboard.writeText(command);
    toast(`${titleCase(harnessId)} handoff copied`);
  } catch (error) { toast(error.message); }
}

async function toggleNotifications() {
  if (!("Notification" in window)) { toast("Desktop notifications are not supported by this browser"); return; }
  if (localStorage.getItem("merced-ai-notifications") === "on") {
    localStorage.removeItem("merced-ai-notifications");
    renderInspector();
    return;
  }
  const permission = await Notification.requestPermission();
  if (permission === "granted") localStorage.setItem("merced-ai-notifications", "on");
  else toast("Notification permission was not granted");
  renderInspector();
}

function notifyRunFinished(payload) {
  if (localStorage.getItem("merced-ai-notifications") !== "on" || Notification.permission !== "granted") return;
  new Notification("Merced AI run finished", { body: `${payload.completed} completed${payload.failed ? `, ${payload.failed} failed` : ""} in ${(payload.duration_ms / 1000).toFixed(1)}s` });
}

async function selectBot(name) {
  state.activeBot = name;
  state.harnessOverride = "";
  state.activeSession = state.data.sessions.find((item) => item.kind !== "group" && item.bot_name === name)?.id || "";
  showView("conversations");
  render();
}

function renderGroupPicker() {
  const query = $("#group-search").value.trim().toLowerCase();
  $("#group-options").innerHTML = state.data.bots.filter((bot) => !query || bot.name.toLowerCase().includes(query)).map((bot) => `<label class="group-option" ${identityStyle(bot.name)}><input type="checkbox" value="${escapeHtml(bot.name)}" ${state.groupDraft.includes(bot.name) ? "checked" : ""}/><span class="message-avatar bot-identity">${escapeHtml(initials(bot.name))}</span><span>${escapeHtml(titleCase(bot.name))}<small>${escapeHtml(titleCase(bot.harness.preferred))}</small></span></label>`).join("");
  $("#group-selected").innerHTML = state.groupDraft.map((name, index) => `<div class="group-selection" data-name="${escapeHtml(name)}"><span>${index + 1}. ${escapeHtml(titleCase(name))}</span><button type="button" data-move="up" ${index ? "" : "disabled"} aria-label="Move ${escapeHtml(name)} up">↑</button><button type="button" data-move="down" ${index < state.groupDraft.length - 1 ? "" : "disabled"} aria-label="Move ${escapeHtml(name)} down">↓</button><button type="button" data-remove aria-label="Remove ${escapeHtml(name)}">×</button></div>`).join("") || '<div class="empty-mini">Select at least two bots</div>';
}

function openGroupEditor(derive = false) {
  if (state.data.bots.length < 2) { showView("bots"); toast("Create at least two bots first"); return; }
  const session = currentSession();
  state.deriveFrom = derive ? session?.id || "" : "";
  state.groupDraft = derive ? currentParticipants().map((item) => item.bot_name) : state.data.bots.slice(0, 2).map((item) => item.name);
  $("#group-dialog-title").textContent = derive ? "Start with different participants" : "Create group conversation";
  $("#group-submit").textContent = derive ? "Start derived conversation" : "Create conversation";
  $("#group-title").value = derive && session?.title ? `${session.title} — follow-up` : "";
  $("#group-mode").value = derive ? session?.mode || "mentions" : "mentions";
  $("#group-search").value = "";
  $("#group-error").textContent = "";
  renderGroupPicker();
  $("#group-dialog").showModal();
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

let visibleAAIS = "";
async function pollAAIS() {
  try {
    const snapshot = await (await api("/api/approvals/snapshot")).json();
    const pending = snapshot.snapshot?.pending || [];
    const request = pending[0];
    const dialog = $("#aais-dialog");
    if (!request) {
      visibleAAIS = "";
      if (dialog.open) dialog.close();
      return;
    }
    if (visibleAAIS === request.id && dialog.open) return;
    visibleAAIS = request.id;
    $("#aais-title").textContent = request.action.summary;
    $("#aais-risk").textContent = `${request.risk.level.toUpperCase()} · ${request.risk.reasons.join(" · ")}`;
    $("#aais-action").textContent = JSON.stringify({ name: request.action.name, resource: request.action.resource, working_directory: request.action.working_directory, arguments: request.action.arguments, digest: request.action_digest }, null, 2);
    $("#aais-origin").textContent = `${request.origin.harness} · ${request.origin.project || "local project"}`;
    $("#aais-choices").innerHTML = request.choices.map((choice) => `<button class="${choice.decision === "approve" ? "primary-button" : "secondary-button"}" data-decision="${escapeHtml(choice.decision)}" data-scope="${escapeHtml(choice.scope)}">${escapeHtml(choice.label)}</button>`).join("");
    if (!dialog.open) dialog.showModal();
  } catch { /* the active run stream remains usable during transient reconnects */ }
}

async function decideAAIS(requestId, decision, scope) {
  await api("/api/approvals/decisions", { method: "POST", body: JSON.stringify({ request_id: requestId, decision, scope, decision_id: `dec_web_${crypto.randomUUID().replaceAll("-", "")}` }) });
  visibleAAIS = "";
  if ($("#aais-dialog").open) $("#aais-dialog").close();
  await pollAAIS();
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
    const dispatch = state.pendingDispatch || (currentParticipants().length > 1 ? $("#dispatch-select").value : null);
    const context = state.selectedContext.map((item) => ({ path: item.path }));
    const response = await api(`/api/sessions/${encodeURIComponent(session.id)}/messages`, { method: "POST", body: JSON.stringify({ content, approved, dispatch, context }) });
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
          const approvalNames = (item.payload.participants || []).map((participant) => `${titleCase(participant.bot_name)} via ${titleCase(participant.harness_id)}`);
          $("#approval-copy").textContent = approvalNames.length
            ? `Approval is required for: ${approvalNames.join(", ")}.`
            : item.payload.message;
          $("#approval-participants").innerHTML = (item.payload.participants || []).map((participant) => `<div ${identityStyle(participant.bot_name)}><strong>${escapeHtml(titleCase(participant.bot_name))}</strong> via ${escapeHtml(titleCase(participant.harness_id))}</div>`).join("");
          if (await approval()) return sendPrompt(true);
          state.pendingPrompt = "";
          return;
        }
        if (item.event === "run_started") {
          state.activeRun = item.payload.run_id;
          session.turns.push({ role: "user", content });
          $("#message-input").value = "";
          state.selectedContext = [];
          renderSelectedContext();
          $("#cancel-run").hidden = false;
          const routes = item.payload.participants || [{ harness_id: item.payload.harness_id }];
          routes.forEach((route) => session.turns.push({ role: "assistant", content: "", bot_name: route.bot_name || state.activeBot, harness_id: route.harness_id, pending: true }));
          state.liveParticipants = Object.fromEntries(routes.map((route) => [route.bot_name || state.activeBot, "queued"]));
          $("#activity-list").innerHTML = routes.map((route) => `<div class="activity running" id="run-participant-${escapeHtml(route.bot_name || state.activeBot)}" ${identityStyle(route.bot_name || state.activeBot)}><span>●</span><div><strong>${escapeHtml(titleCase(route.bot_name || state.activeBot))}</strong><small>Queued · ${escapeHtml(titleCase(route.harness_id))}</small></div></div>`).join("");
          $("#composer-status").textContent = `Running ${routes.length} collaborator${routes.length === 1 ? "" : "s"}…`;
          renderConversation();
          scrollThread();
        } else if (item.event === "participant_started") {
          updateParticipantStatus(item.payload.bot_name, "Running", item.payload.harness_id);
        } else if (item.event === "tool_event") {
          addActivity("Harness activity", summarizeEvent(item.payload.event));
        } else if (item.event === "assistant_message") {
          const pendingTurn = session.turns.find((turn) => turn.pending && turn.bot_name === item.payload.bot_name);
          if (pendingTurn) { pendingTurn.content = item.payload.content; pendingTurn.pending = false; }
          else session.turns.push({ role: "assistant", content: item.payload.content, bot_name: item.payload.bot_name, harness_id: item.payload.harness_id });
          updateParticipantStatus(item.payload.bot_name, `Completed in ${(item.payload.duration_ms / 1000).toFixed(1)}s`, item.payload.harness_id);
          renderConversation();
          $("#composer-status").textContent = `${titleCase(item.payload.harness_id)} completed in ${(item.payload.duration_ms / 1000).toFixed(1)}s`;
          scrollThread();
        } else if (["run_error", "run_cancelled", "participant_error", "participant_cancelled"].includes(item.event)) {
          const cancelled = item.event.includes("cancelled");
          const speaker = item.payload.bot_name ? `${titleCase(item.payload.bot_name)} · ` : "";
          updateParticipantStatus(item.payload.bot_name, cancelled ? "Cancelled" : item.payload.message, item.payload.harness_id, true, !cancelled);
          session.turns = session.turns.filter((turn) => !(turn.pending && turn.bot_name === item.payload.bot_name));
          renderConversation();
          $("#composer-status").textContent = item.payload.message;
        } else if (item.event === "run_finished") {
          $("#composer-status").textContent = `${item.payload.completed} completed${item.payload.failed ? ` · ${item.payload.failed} failed` : ""} · ${(item.payload.duration_ms / 1000).toFixed(1)}s`;
          notifyRunFinished(item.payload);
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
    state.pendingDispatch = "";
    state.liveParticipants = {};
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
function updateParticipantStatus(botName, copy, harnessId, error = false, retry = false) {
  const node = document.getElementById(`run-participant-${botName}`);
  if (!node) { addActivity(botName || "Harness", copy, error, retry, botName); return; }
  node.classList.toggle("running", copy === "Running");
  node.classList.toggle("error", error);
  node.querySelector("small").textContent = `${copy}${harnessId ? ` · ${titleCase(harnessId)}` : ""}`;
  if (retry && !node.querySelector(".retry-run")) node.insertAdjacentHTML("beforeend", `<button type="button" class="retry-run" data-bot="${escapeHtml(botName)}">Retry only this bot</button>`);
}
function addActivity(title, copy, error = false, retry = false, botName = "") { $("#activity-list").insertAdjacentHTML("beforeend", `<div class="activity ${error ? "error" : ""}"><span>${error ? "!" : "↗"}</span><div><strong>${escapeHtml(title)}</strong><small>${escapeHtml(copy)}</small></div>${retry ? `<button type="button" class="retry-run" data-bot="${escapeHtml(botName)}">Retry${botName ? " only this bot" : ""}</button>` : ""}</div>`); scrollThread(); }
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

function openEditor({ title, eyebrow, fields, submit, busyLabel = "Saving", progressCopy = "Applying the validated change." }) {
  $("#editor-title").textContent = title;
  $("#editor-eyebrow").textContent = eyebrow;
  $("#editor-fields").innerHTML = fields;
  $("#editor-error").textContent = "";
  $("#editor-progress").hidden = true;
  $("#editor-form").onsubmit = async (event) => {
    event.preventDefault();
    const button = $("#editor-submit");
    const originalLabel = button.textContent;
    const started = Date.now();
    button.disabled = true;
    event.currentTarget.setAttribute("aria-busy", "true");
    $("#editor-progress").hidden = false;
    $("#editor-progress-copy").textContent = progressCopy;
    const updateProgress = () => { button.textContent = `${busyLabel}… ${Math.floor((Date.now() - started) / 1000)}s`; };
    updateProgress();
    const progressTimer = window.setInterval(updateProgress, 1000);
    try {
      await submit(Object.fromEntries(new FormData(event.currentTarget)));
      $("#editor-dialog").close();
      await refresh({ quiet: true });
      renderManagement();
      toast(`${title} saved`);
    } catch (error) { $("#editor-error").textContent = error.message; }
    finally { window.clearInterval(progressTimer); button.disabled = false; button.textContent = originalLabel; event.currentTarget.setAttribute("aria-busy", "false"); $("#editor-progress").hidden = true; }
  };
  $("#editor-dialog").showModal();
}

function profileFields(profile = {}) {
  const permissionOptions = [{ value: "", label: "Inherit harness policy" }, "ask", "allow", "deny"];
  const providerOptions = [
    { value: "", label: "Inherit harness provider" },
    "openai", "anthropic", "google", "gemini", "ollama", "openrouter", "moonshot", "kimi",
  ];
  const knownModels = Array.from(new Set([
    ...state.data.profiles.map((item) => item.model?.id).filter(Boolean),
    "gpt-5.4", "claude-sonnet-4-6", "gemini-3.5-flash",
  ]));
  return field("Profile name", "name", profile.name, { required: true, readonly: Boolean(profile.name) })
    + field("Description", "description", profile.description, { required: true })
    + field("Instructions", "instructions", profile.instructions, { textarea: true, rows: 8, required: true })
    + field("Model provider", "model_provider", profile.model?.provider || "", { select: providerOptions })
    + field("Model ID", "model_id", profile.model?.id || "", { placeholder: "Inherit harness model", select: knownModels })
    + field("Edit permission", "edit_permission", profile.permissions?.edit || "", { select: permissionOptions })
    + field("Shell permission", "shell_permission", profile.permissions?.shell || "", { select: permissionOptions })
    + (!profile.name ? field("Save location", "scope", "project", { select: [{ value: "project", label: "Portable with this project · .agents" }, { value: "universal", label: "Universal across compatible harnesses · ~/.agentprofiles" }, { value: "user", label: "This merced-ai user" }] }) : "");
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

function openProfileGenerator() {
  const harnesses = state.data.harnesses.filter(ready).map((item) => item.harness_id);
  openEditor({
    title: "Generate profile",
    eyebrow: "OAP PROFILE AUTHOR",
    busyLabel: "Generating and validating profile",
    progressCopy: "The selected harness is authoring a bounded OAP draft and merced-ai will validate it before review. This reports lifecycle progress, not private model reasoning.",
    fields: field("What should this specialist do?", "prompt", "", { textarea: true, rows: 8, required: true })
      + field("Preferred name", "name", "")
      + field("Generation harness", "harness", "", { placeholder: "Choose automatically", select: harnesses })
      + field("Save location", "scope", "project", { select: [{ value: "project", label: "Portable with this project · .agents" }, { value: "universal", label: "Universal across compatible harnesses · ~/.agentprofiles" }, { value: "user", label: "This merced-ai user" }] }),
    submit: async (values) => {
      const response = await api("/api/profiles/generate", { method: "POST", body: JSON.stringify({ prompt: values.prompt, name: values.name || null, harness: values.harness || null }) });
      const proposal = await response.json();
      const document = proposal.document;
      const approved = window.confirm(`Create @${document.metadata.name}?\n\n${document.metadata.description}\n\nInstructions:\n${document.spec.role.instructions}\nPermissions: ${JSON.stringify(document.spec.permissions || {})}`);
      if (!approved) throw new Error("The validated draft was not saved. Adjust the prompt or generate it again when ready.");
      return api("/api/profiles/document", { method: "POST", body: JSON.stringify({ document, scope: values.scope }) });
    },
  });
}

function openBotEditor(bot = null) {
  if (!state.data.profiles.length) { showView("profiles"); openProfileEditor(); return; }
  const harnesses = state.data.harnesses.filter(ready).map((item) => item.harness_id);
  openEditor({
    title: bot ? "Edit bot" : "Create bot",
    eyebrow: "BOT BINDING",
    fields: field("Bot name", "name", bot?.name || "", { required: true, readonly: Boolean(bot) })
      + field("OAP profile", "profile", bot?.profile || "", { required: true, placeholder: "Choose a profile", select: state.data.profiles.map((item) => item.name) })
      + field("Preferred harness", "harness", bot?.harness?.preferred || "", { required: true, placeholder: "Choose a harness", select: harnesses })
      + field("Fallback harnesses", "fallbacks", (bot?.harness?.fallbacks || []).join(", "), { placeholder: "Comma-separated, in priority order" }),
    submit: async (values) => api(bot ? `/api/bots/${encodeURIComponent(bot.name)}` : "/api/bots", { method: bot ? "PUT" : "POST", body: JSON.stringify({ ...values, fallbacks: values.fallbacks.split(",").map((item) => item.trim()).filter(Boolean) }) }),
  });
}

function bindCopyButtons() { $$(".copy-code").forEach((button) => button.addEventListener("click", async () => { await navigator.clipboard.writeText(button.nextElementSibling.textContent); button.textContent = "Copied"; setTimeout(() => { button.textContent = "Copy"; }, 1200); })); }
function renderMentionMenu() {
  const input = $("#message-input");
  const match = input.value.slice(0, input.selectionStart).match(/(?:^|\s)@([\w-]*)$/);
  const participants = currentParticipants().filter((item) => !match || item.bot_name.toLowerCase().startsWith(match[1].toLowerCase()));
  const menu = $("#mention-menu");
  if (!match || currentParticipants().length < 2 || !participants.length) { menu.hidden = true; return; }
  menu.innerHTML = participants.map((item) => `<button type="button" role="option" data-mention="${escapeHtml(item.bot_name)}">@${escapeHtml(item.bot_name)} · ${escapeHtml(titleCase(item.harness_id))}</button>`).join("");
  menu.hidden = false;
}
function insertMention(name) {
  const input = $("#message-input");
  const before = input.value.slice(0, input.selectionStart).replace(/@([\w-]*)$/, `@${name} `);
  input.value = before + input.value.slice(input.selectionStart);
  input.selectionStart = input.selectionEnd = before.length;
  $("#mention-menu").hidden = true;
  input.focus();
}
async function renameConversation() {
  const session = currentSession();
  if (!session) return;
  const title = window.prompt("Conversation name", session.title || "");
  if (!title?.trim()) return;
  try { await api(`/api/sessions/${encodeURIComponent(session.id)}`, { method: "PUT", body: JSON.stringify({ title }) }); await refresh({ quiet: true }); toast("Conversation renamed"); } catch (error) { toast(error.message); }
}
async function deleteConversation() {
  const session = currentSession();
  if (!session || !window.confirm(`Delete “${session.title || "this conversation"}” and its transcript?`)) return;
  try { await api(`/api/sessions/${encodeURIComponent(session.id)}`, { method: "DELETE" }); state.activeSession = ""; await refresh({ quiet: true }); toast("Conversation deleted"); }
  catch (error) { toast(error.message); }
}
function openNavigation() { $("#sidebar").classList.add("open"); $("#sidebar-backdrop").hidden = false; $("#close-navigation").focus(); }
function closeNavigation() { $("#sidebar").classList.remove("open"); $("#sidebar-backdrop").hidden = true; }

function bindEvents() {
  $("#aais-choices").addEventListener("click", (event) => {
    const button = event.target.closest("[data-decision]");
    if (button && visibleAAIS) decideAAIS(visibleAAIS, button.dataset.decision, button.dataset.scope).catch((error) => toast(error.message));
  });
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
  $("#bot-select").addEventListener("change", (event) => { if (event.target.value !== "__group__") selectBot(event.target.value); });
  $("#harness-select").addEventListener("change", async (event) => { state.harnessOverride = event.target.value; if (currentSession()) { state.activeSession = ""; await createConversation(); } else { render(); } });
  $("#recent-list").addEventListener("click", (event) => { const button = event.target.closest("[data-session]"); if (!button) return; const session = state.data.sessions.find((item) => item.id === button.dataset.session); state.activeSession = session.id; state.activeBot = session.bot_name; state.harnessOverride = ""; showView("conversations"); render(); closeNavigation(); });
  $("#session-search").addEventListener("input", renderRecents);
  $("#refresh-data").addEventListener("click", () => refresh().catch(showFatal));
  $("#refresh-harnesses").addEventListener("click", () => refreshHarnesses({ announce: true }).catch((error) => toast(error.message)));
  $("#new-thread").addEventListener("click", createConversation);
  $("#new-group").addEventListener("click", () => openGroupEditor(false));
  $("#derive-group").addEventListener("click", () => openGroupEditor(true));
  $("#rename-session").addEventListener("click", renameConversation);
  $("#delete-session").addEventListener("click", deleteConversation);
  $("#composer").addEventListener("submit", (event) => { event.preventDefault(); sendPrompt(); });
  $("#add-context").addEventListener("click", openContextPicker);
  $("#context-chips").addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-context]");
    if (!button) return;
    state.selectedContext = state.selectedContext.filter((item) => item.path !== button.dataset.removeContext);
    renderSelectedContext();
  });
  $("#context-search").addEventListener("input", renderContextPicker);
  $("#context-options").addEventListener("change", (event) => {
    const checkbox = event.target.closest('input[type="checkbox"]');
    if (!checkbox) return;
    const file = state.contextFiles.find((item) => item.path === checkbox.value);
    state.contextDraft = checkbox.checked
      ? [...state.contextDraft.filter((item) => item.path !== checkbox.value), file]
      : state.contextDraft.filter((item) => item.path !== checkbox.value);
  });
  $("#context-form").addEventListener("submit", (event) => {
    event.preventDefault();
    state.selectedContext = [...state.contextDraft];
    renderSelectedContext();
    $("#context-dialog").close();
  });
  $$(".context-cancel").forEach((button) => button.addEventListener("click", () => $("#context-dialog").close()));
  $("#upload-context").addEventListener("click", () => $("#context-upload-input").click());
  $("#context-upload-input").addEventListener("change", (event) => {
    uploadContextFiles([...event.target.files]);
    event.target.value = "";
  });
  $("#message-input").addEventListener("keydown", (event) => { if (event.key === "ArrowDown" && !$("#mention-menu").hidden) { event.preventDefault(); $("#mention-menu button")?.focus(); } else if (event.key === "Escape") { $("#mention-menu").hidden = true; } else if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#composer").requestSubmit(); } });
  $("#message-input").addEventListener("input", (event) => { event.target.style.height = "auto"; event.target.style.height = `${Math.min(event.target.scrollHeight, 180)}px`; renderMentionMenu(); });
  $("#mention-menu").addEventListener("click", (event) => { const button = event.target.closest("[data-mention]"); if (button) insertMention(button.dataset.mention); });
  $("#cancel-run").addEventListener("click", cancelRun);
  $("#activity-list").addEventListener("click", (event) => { const retry = event.target.closest(".retry-run"); if (retry && state.lastPrompt) { state.pendingPrompt = state.lastPrompt; state.pendingDispatch = retry.dataset.bot || ""; sendPrompt(); } });
  $("#group-search").addEventListener("input", renderGroupPicker);
  $$(".group-cancel").forEach((button) => button.addEventListener("click", () => $("#group-dialog").close()));
  $("#group-options").addEventListener("change", (event) => { const checkbox = event.target.closest('input[type="checkbox"]'); if (!checkbox) return; state.groupDraft = checkbox.checked ? [...state.groupDraft, checkbox.value] : state.groupDraft.filter((name) => name !== checkbox.value); renderGroupPicker(); });
  $("#group-selected").addEventListener("click", (event) => { const row = event.target.closest("[data-name]"); if (!row) return; const index = state.groupDraft.indexOf(row.dataset.name); if (event.target.closest("[data-remove]")) state.groupDraft.splice(index, 1); else if (event.target.closest('[data-move="up"]') && index > 0) [state.groupDraft[index - 1], state.groupDraft[index]] = [state.groupDraft[index], state.groupDraft[index - 1]]; else if (event.target.closest('[data-move="down"]') && index < state.groupDraft.length - 1) [state.groupDraft[index + 1], state.groupDraft[index]] = [state.groupDraft[index], state.groupDraft[index + 1]]; renderGroupPicker(); });
  $("#group-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.groupDraft.length < 2) { $("#group-error").textContent = "Select at least two bots."; return; }
    const payload = { bot_names: state.groupDraft, mode: $("#group-mode").value, title: $("#group-title").value.trim() || null };
    const path = state.deriveFrom ? `/api/sessions/${encodeURIComponent(state.deriveFrom)}/derive` : "/api/sessions";
    try {
      const session = await (await api(path, { method: "POST", body: JSON.stringify(payload) })).json();
      $("#group-dialog").close();
      state.data.sessions = [session, ...state.data.sessions.filter((item) => item.id !== session.id)];
      state.activeSession = session.id;
      state.activeBot = session.bot_name;
      showView("conversations");
      render();
      toast(state.deriveFrom ? "Derived conversation ready" : "Group conversation ready");
    } catch (error) { $("#group-error").textContent = error.message; }
  });
  $("#management-action").addEventListener("click", () => {
    if (state.view === "profiles") openProfileEditor();
    else if (state.view === "bots") openBotEditor();
    else refreshHarnesses({ announce: true }).catch((error) => toast(error.message));
  });
  $("#generate-profile").addEventListener("click", openProfileGenerator);
  $("#management-list").addEventListener("click", async (event) => {
    const editProfile = event.target.closest(".edit-profile"); const deleteProfile = event.target.closest(".delete-profile");
    const use = event.target.closest(".use-bot"); const editBot = event.target.closest(".edit-bot"); const deleteBot = event.target.closest(".delete-bot");
    if (editProfile) openProfileEditor(state.data.profiles.find((item) => item.name === editProfile.dataset.profile));
    if (deleteProfile && window.confirm(`Delete profile “${deleteProfile.dataset.profile}”?`)) { try { await api(`/api/profiles/${encodeURIComponent(deleteProfile.dataset.profile)}`, { method: "DELETE" }); await refresh({ quiet: true }); renderManagement(); toast("Profile deleted"); } catch (error) { toast(error.message); } }
    if (use) selectBot(use.dataset.bot);
    if (editBot) openBotEditor(state.data.bots.find((item) => item.name === editBot.dataset.bot));
    if (deleteBot && window.confirm(`Delete bot “${deleteBot.dataset.bot}”? Existing transcripts remain but cannot run with it.`)) { try { await api(`/api/bots/${encodeURIComponent(deleteBot.dataset.bot)}`, { method: "DELETE" }); await refresh({ quiet: true }); renderManagement(); toast("Bot deleted"); } catch (error) { toast(error.message); } }
  });
  $("#harness-list").addEventListener("click", (event) => { const button = event.target.closest("[data-harness]"); if (button && !currentSession()?.turns.length) { state.harnessOverride = button.dataset.harness; render(); } });
  $("#export-session").addEventListener("click", () => { const session = currentSession(); if (session) location.href = `/api/sessions/${encodeURIComponent(session.id)}/export`; });
  $("#focus-route").addEventListener("click", () => $("#harness-select").focus());
  $("#handoff-harness").addEventListener("click", copyHandoff);
  $("#notification-toggle").addEventListener("click", toggleNotifications);
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
  window.setInterval(pollAAIS, 800);
  await pollAAIS();
  refreshHarnesses().catch((error) => toast(`Harness detection: ${error.message}`));
}

boot().catch(showFatal);
