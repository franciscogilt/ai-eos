// AI-EOS dashboard. Plain ES module, no dependencies. All data is rendered with textContent.
// Auth: OIDC authorization code + PKCE (public client). The access token is kept in sessionStorage.

const TOKEN_KEY = "aieos.token";
const ORG_KEY = "aieos.org";
const PKCE_KEY = "aieos.pkce";
const main = document.getElementById("main");
let config = null;

// ---------------------------------------------------------------- helpers
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function render(...nodes) {
  main.replaceChildren(...nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false));
}

function status(value) {
  return el("span", { class: `status ${String(value).replace(/[^a-z_]/g, "")}` }, value);
}

function money(value) {
  const n = Number(value || 0);
  return `$${n.toFixed(n < 1 ? 4 : 2)}`;
}

function when(value) {
  return value ? new Date(value).toLocaleString() : "—";
}

function bar(used, total, { budget = true } = {}) {
  const pct = total > 0 ? Math.min(100, (Number(used) / Number(total)) * 100) : 0;
  const span = el("span");
  span.style.width = `${pct}%`;  // CSSOM, allowed by the style-src policy
  return el("div", { class: `bar${budget && pct >= 100 ? " over" : ""}`, title: `${pct.toFixed(0)}%` }, span);
}

function table(headers, rows) {
  if (!rows.length) return el("p", { class: "muted" }, "Nada por aquí todavía.");
  return el(
    "table",
    {},
    el("thead", {}, el("tr", {}, headers.map((h) => el("th", { class: h.num ? "num" : null }, h.label ?? h)))),
    el("tbody", {}, rows.map((r) => el("tr", {}, r.map((cell, i) => el("td", { class: headers[i].num ? "num" : null }, cell))))),
  );
}

function b64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString(n = 32) {
  return b64url(crypto.getRandomValues(new Uint8Array(n)));
}

// ---------------------------------------------------------------- API
class AuthError extends Error {}

async function api(path, options = {}) {
  const token = sessionStorage.getItem(TOKEN_KEY);
  if (!token) throw new AuthError("no token");
  const response = await fetch(path, {
    ...options,
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", ...(options.headers || {}) },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (response.status === 401) {
    sessionStorage.removeItem(TOKEN_KEY);
    throw new AuthError("expired");
  }
  if (response.status === 204) return null;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data?.error?.message || `HTTP ${response.status}`);
  return data;
}

const org = () => sessionStorage.getItem(ORG_KEY);
const orgPath = (path) => `/v1/orgs/${encodeURIComponent(org())}${path}`;

// ---------------------------------------------------------------- sign-in (OIDC code + PKCE)
async function discovery() {
  const r = await fetch(`${config.issuer.replace(/\/$/, "")}/.well-known/openid-configuration`);
  if (!r.ok) throw new Error("no se pudo leer la configuración del proveedor de identidad");
  return r.json();
}

async function startLogin() {
  const meta = await discovery();
  const verifier = randomString(48);
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const state = randomString();
  sessionStorage.setItem(PKCE_KEY, JSON.stringify({ verifier, state, token_endpoint: meta.token_endpoint }));
  const params = new URLSearchParams({
    response_type: "code",
    client_id: config.client_id,
    redirect_uri: `${location.origin}/ui/`,
    scope: config.scopes,
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  if (config.audience) params.set("audience", config.audience);
  location.assign(`${meta.authorization_endpoint}?${params}`);
}

async function finishLogin() {
  const query = new URLSearchParams(location.search);
  if (!query.has("code")) return false;
  const saved = JSON.parse(sessionStorage.getItem(PKCE_KEY) || "null");
  sessionStorage.removeItem(PKCE_KEY);
  history.replaceState(null, "", "/ui/");  // drop the code from the address bar
  if (!saved || saved.state !== query.get("state")) throw new Error("respuesta de inicio de sesión inválida (state)");
  const r = await fetch(saved.token_endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code: query.get("code"),
      redirect_uri: `${location.origin}/ui/`,
      client_id: config.client_id,
      code_verifier: saved.verifier,
    }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok || !data.access_token) throw new Error("el proveedor de identidad no entregó un token");
  sessionStorage.setItem(TOKEN_KEY, data.access_token);
  return true;
}

function loginView(message) {
  const children = [el("h1", {}, "Iniciar sesión"), message ? el("p", { class: "error" }, message) : null];
  if (config.client_id) {
    children.push(el("p", {}, el("button", { class: "primary", onclick: () => startLogin().catch(fail) }, "Entrar con tu cuenta")));
  }
  if (config.token_paste) {
    const input = el("textarea", { placeholder: "Pega aquí un token de acceso (desarrollo: aieos-admin dev-token)" });
    children.push(
      el("p", { class: "muted small" }, "Modo desarrollo: usar un token existente."),
      input,
      el("p", {}, el("button", {
        onclick: () => {
          const t = input.value.trim();
          if (t) { sessionStorage.setItem(TOKEN_KEY, t); route(); }
        },
      }, "Usar token")),
    );
  }
  document.getElementById("nav").hidden = true;
  document.getElementById("org").hidden = true;
  document.getElementById("logout").hidden = true;
  render(el("div", { class: "panel login" }, children));
}

function fail(error) {
  if (error instanceof AuthError) return loginView(error.message === "expired" ? "La sesión expiró." : null);
  render(el("div", { class: "panel" }, el("p", { class: "error" }, `Error: ${error.message}`)));
}

// ---------------------------------------------------------------- chrome
async function setupChrome() {
  const orgs = await api("/v1/me/orgs");
  if (!orgs.length) {
    render(el("div", { class: "panel" }, el("p", {}, "Tu usuario no pertenece a ninguna organización.")));
    return false;
  }
  if (!orgs.some((o) => o.id === org())) sessionStorage.setItem(ORG_KEY, orgs[0].id);
  const select = document.getElementById("org");
  select.replaceChildren(...orgs.map((o) => el("option", { value: o.id, selected: o.id === org() }, `${o.name} (${o.role})`)));
  select.onchange = () => { sessionStorage.setItem(ORG_KEY, select.value); route(); };
  select.hidden = orgs.length < 2;
  document.getElementById("nav").hidden = false;
  const logout = document.getElementById("logout");
  logout.hidden = false;
  logout.onclick = () => { sessionStorage.clear(); loginView("Sesión cerrada."); };
  return true;
}

async function refreshPendingCount() {
  try {
    const pending = await api(orgPath("/approvals?status=pending"));
    const badge = document.getElementById("pending-count");
    badge.textContent = String(pending.length);
    badge.hidden = pending.length === 0;
  } catch { /* the badge is optional */ }
}

// ---------------------------------------------------------------- views
async function workflowsView() {
  const workflows = await api(orgPath("/workflows?limit=100"));
  render(
    el("h1", {}, "Flujos"),
    table(
      ["Título", "Estado", { label: "Gasto", num: true }, "Presupuesto", "Creado"],
      workflows.map((w) => [
        el("a", { href: `#/w/${w.id}` }, w.title),
        status(w.status),
        money(w.spent_usd),
        el("div", {}, bar(w.spent_usd, w.budget_usd), el("span", { class: "muted small" }, `de ${money(w.budget_usd)}`)),
        when(w.created_at),
      ]),
    ),
  );
}

function decisionForm(approval, after) {
  const comment = el("textarea", { placeholder: "Comentario (opcional)" });
  const message = el("p", { class: "error" });
  const decide = (decision) => async (event) => {
    event.target.disabled = true;
    try {
      await api(orgPath(`/approvals/${approval.id}/decision`), {
        method: "POST",
        body: { decision, version: approval.version, comment: comment.value.trim() || null },
      });
      await after();
    } catch (e) {
      message.textContent = e.message;
      event.target.disabled = false;
    }
  };
  return el(
    "div",
    { class: "panel" },
    el("div", { class: "row" }, status(approval.status), el("b", {}, approval.tool_name || approval.kind),
      el("span", { class: "muted small" }, `nivel ${approval.permission_level} · vence ${when(approval.expires_at)}`)),
    el("p", {}, approval.summary),
    el("p", { class: "muted small" }, "Flujo: ", el("a", { href: `#/w/${approval.workflow_id}` }, approval.workflow_id)),
    comment,
    el("div", { class: "row" },
      el("button", { class: "primary", onclick: decide("approve") }, "Aprobar"),
      el("button", { class: "danger", onclick: decide("reject") }, "Rechazar")),
    message,
  );
}

async function approvalsView() {
  const pending = await api(orgPath("/approvals?status=pending"));
  render(
    el("h1", {}, "Aprobaciones pendientes"),
    pending.length ? pending.map((a) => decisionForm(a, approvalsView)) : el("p", { class: "muted" }, "No hay aprobaciones pendientes."),
  );
  refreshPendingCount();
}

async function workflowView(id) {
  const [t, pending] = await Promise.all([api(orgPath(`/workflows/${id}/timeline`)), api(orgPath("/approvals?status=pending"))]);
  const w = t.workflow;
  const mine = pending.filter((a) => a.workflow_id === id);
  const active = !["succeeded", "failed", "cancelled"].includes(w.status);
  const cancel = active
    ? el("button", {
        class: "danger",
        onclick: async (e) => {
          if (!window.confirm("¿Cancelar este flujo?")) return;
          e.target.disabled = true;
          try { await api(orgPath(`/workflows/${id}/cancel`), { method: "POST", body: { reason: "cancelado desde el panel" } }); }
          catch (err) { window.alert(err.message); }
          workflowView(id);
        },
      }, "Cancelar flujo")
    : null;
  render(
    el("p", { class: "small" }, el("a", { href: "#/" }, "← Flujos")),
    el("h1", {}, w.title),
    el("div", { class: "grid" },
      el("div", { class: "stat" }, el("span", { class: "muted small" }, "Estado"), el("div", {}, status(w.status))),
      el("div", { class: "stat" }, el("span", { class: "muted small" }, "Gasto"), el("b", {}, money(w.spent_usd)), bar(w.spent_usd, w.budget_usd)),
      el("div", { class: "stat" }, el("span", { class: "muted small" }, "Tokens"), el("b", {}, Number(w.spent_tokens).toLocaleString()), bar(w.spent_tokens, w.budget_tokens)),
      el("div", { class: "stat" }, el("span", { class: "muted small" }, "Creado"), el("b", { class: "small" }, when(w.created_at)))),
    cancel ? el("div", { class: "row" }, cancel) : null,
    mine.length ? [el("h2", {}, "Esperando tu decisión"), mine.map((a) => decisionForm(a, () => workflowView(id)))] : null,
    el("h2", {}, "Pasos"),
    table(["Paso", "Intento", "Estado", "Inicio", "Fin"], t.steps.map((s) => [s.step_key, s.attempt, status(s.status), when(s.started_at), when(s.finished_at)])),
    el("h2", {}, "Tareas"),
    table(["Tarea", "Título", "Agente", "Intento", "Estado"], t.tasks.map((k) => [k.key, k.title, k.assigned_agent_key, k.attempt, status(k.status)])),
    el("h2", {}, "Controles de calidad"),
    table(["Control", "Tarea", "Resultado", "Detalle", "Cuándo"], t.gates.map((g) => [
      g.gate_key, g.task_key || "—", status(g.result), el("pre", {}, g.details?.summary || JSON.stringify(g.details)), when(g.evaluated_at),
    ])),
    el("h2", {}, "Ejecuciones de agentes"),
    table(
      ["Agente", "Paso", "Modelo", "Estado", { label: "Tokens entrada", num: true }, { label: "Tokens salida", num: true }, { label: "Costo", num: true }, "Inicio"],
      t.runs.map((r) => [
        r.agent_key, r.step_key ? `${r.step_key} #${r.step_attempt}` : "—", `${r.model_provider}/${r.model_name}`, status(r.status),
        Number(r.input_tokens).toLocaleString(), Number(r.output_tokens).toLocaleString(), money(r.cost_usd), when(r.started_at),
      ]),
    ),
  );
}

async function costsView(days = 30) {
  const c = await api(orgPath(`/costs?days=${days}`));
  const max = (rows) => Math.max(0, ...rows.map((r) => Number(r.cost_usd)));
  const section = (title, rows) => [
    el("h2", {}, title),
    table(
      ["", { label: "Llamadas", num: true }, { label: "Tokens entrada", num: true }, { label: "Tokens salida", num: true }, { label: "Costo", num: true }, ""],
      rows.map((r) => [r.key, r.calls, Number(r.input_tokens).toLocaleString(), Number(r.output_tokens).toLocaleString(), money(r.cost_usd), bar(r.cost_usd, max(rows), { budget: false })]),
    ),
  ];
  const picker = el("select", { onchange: (e) => costsView(Number(e.target.value)).catch(fail) },
    [7, 30, 90, 365].map((d) => el("option", { value: d, selected: d === days }, `Últimos ${d} días`)));
  render(
    el("div", { class: "row" }, el("h1", {}, "Costos"), el("div", { class: "spacer" }), picker),
    el("div", { class: "grid" }, el("div", { class: "stat" }, el("span", { class: "muted small" }, `Total ${days} días`), el("b", {}, money(c.total_usd)))),
    section("Por agente", c.by_agent),
    section("Por modelo", c.by_model),
    section("Por día", c.by_day),
  );
}

// ---------------------------------------------------------------- router
async function route() {
  try {
    if (!sessionStorage.getItem(TOKEN_KEY)) return loginView();
    if (!(await setupChrome())) return;
    refreshPendingCount();
    const hash = location.hash || "#/";
    for (const a of document.querySelectorAll("nav a")) a.classList.toggle("active", a.getAttribute("href") === hash);
    const match = hash.match(/^#\/w\/([0-9a-f-]{36})$/);
    if (match) return await workflowView(match[1]);
    if (hash === "#/approvals") return await approvalsView();
    if (hash === "#/costs") return await costsView();
    return await workflowsView();
  } catch (e) {
    fail(e);
  }
}

async function start() {
  config = await (await fetch("/ui/config.json")).json();
  try {
    await finishLogin();
  } catch (e) {
    return loginView(e.message);
  }
  window.addEventListener("hashchange", route);
  route();
}

start();
