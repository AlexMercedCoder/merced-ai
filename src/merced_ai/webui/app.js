const query = new URLSearchParams(window.location.search);
const token = query.get("token") || "";

const escapeHtml = (value = "") => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;");

const titleCase = (value = "") => value
  .replaceAll("-", " ")
  .replace(/\b\w/g, (letter) => letter.toUpperCase());

const shortTime = (value) => {
  if (!value) return "New";
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(new Date(value));
};

function renderMessage(turn) {
  if (turn.role === "user") {
    return `<article class="message user"><div class="message-body">${escapeHtml(turn.content)}</div></article>`;
  }
  return `<article class="message assistant"><div class="message-avatar">✦</div><div class="message-body"><p>${escapeHtml(turn.content).replaceAll("\n", "</p><p>")}</p><div class="message-meta"><span>Harness response</span><span>Profile-pinned</span></div></div></article>`;
}

async function boot() {
  const response = await fetch(`/api/bootstrap?token=${encodeURIComponent(token)}`);
  if (!response.ok) throw new Error("The local UI token was rejected.");
  const data = await response.json();
  const bot = data.bots[0];
  const profile = bot ? data.profiles.find((item) => item.name === bot.profile || item.path === bot.profile) : data.profiles[0];
  const session = data.sessions.find((item) => !bot || item.bot_name === bot.name) || data.sessions[0];

  document.querySelector("#session-count").textContent = data.sessions.length;
  document.querySelector("#bot-count").textContent = data.bots.length;
  document.querySelector("#profile-count").textContent = data.profiles.length;
  document.querySelector("#workspace-name").textContent = data.workspace.split(/[\\/]/).filter(Boolean).at(-1) || data.workspace;

  const recent = document.querySelector("#recent-list");
  recent.innerHTML = data.sessions.length ? data.sessions.slice(0, 6).map((item, index) => `
    <button class="recent-item ${index === 0 ? "selected" : ""}">
      <strong>${escapeHtml(item.turns[0]?.content || `${titleCase(item.bot_name)} session`)}</strong>
      <small>${escapeHtml(titleCase(item.bot_name))} · ${shortTime(item.updated_at)}</small>
    </button>`).join("") : `<button class="recent-item selected"><strong>Start your first collaboration</strong><small>No sessions yet</small></button>`;

  const botName = bot ? titleCase(bot.name) : "Portable collaborator";
  const harnessName = bot ? titleCase(bot.harness.preferred) : "No harness selected";
  document.querySelector("#active-bot-name").textContent = botName;
  document.querySelector("#active-harness").textContent = `${harnessName} · local`;
  document.querySelector("#inspector-name").textContent = botName;
  document.querySelector("#inspector-description").textContent = profile?.description || "Bind an OAP profile to an installed harness.";
  document.querySelector("#route-profile").textContent = profile?.name || bot?.profile || "—";
  document.querySelector("#route-harness").textContent = harnessName;
  document.querySelector("#welcome-title").textContent = session?.turns.length ? `Working with ${botName}` : `Meet ${botName}`;
  document.querySelector("#welcome-copy").textContent = profile?.description || "A durable identity that can move between your installed harnesses.";

  const thread = document.querySelector("#thread");
  if (session?.turns.length) {
    thread.insertAdjacentHTML("beforeend", session.turns.map(renderMessage).join(""));
  } else {
    thread.insertAdjacentHTML("beforeend", `<div class="message assistant"><div class="message-avatar">✦</div><div class="message-body"><p>I’m ready. My OAP identity is portable; execution stays inside your selected harness.</p><div class="message-meta"><span>Profile resolved</span><span>Harness policy active</span></div></div></div>`);
  }

  const installed = data.harnesses.filter((item) => item.path);
  document.querySelector("#healthy-count").textContent = `${installed.length} detected`;
  document.querySelector("#harness-list").innerHTML = installed.slice(0, 5).map((item) => `
    <div class="harness-item"><span class="status-dot online"></span><strong>${escapeHtml(item.harness_id)}</strong><small>${escapeHtml((item.version || "installed").split("\n")[0])}</small></div>`).join("");

  if (bot) {
    const projectionResponse = await fetch(`/api/projection/${encodeURIComponent(bot.name)}?token=${encodeURIComponent(token)}`);
    if (projectionResponse.ok) {
      const projection = await projectionResponse.json();
      document.querySelector("#projection-level").textContent = projection.support_level;
      document.querySelector("#adjustment-list").innerHTML = projection.adjustments.slice(0, 3).map((item) => `<div class="adjustment"><span>●</span><div><strong>${escapeHtml(item.action)}</strong> · ${escapeHtml(item.field)}<br>${escapeHtml(item.reason)}</div></div>`).join("");
    }
  }
}

boot().catch((error) => {
  document.querySelector("#welcome-title").textContent = "UI connection unavailable";
  document.querySelector("#welcome-copy").textContent = error.message;
});
