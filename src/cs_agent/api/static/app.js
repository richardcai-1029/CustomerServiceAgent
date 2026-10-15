// NovaTech Support web client: streaming chat over SSE (fetch + ReadableStream).
"use strict";

const $ = (id) => document.getElementById(id);
const els = {
  messages: $("messages"), input: $("input"), send: $("send"), composer: $("composer"),
  activity: $("activity"), activityText: $("activity-text"),
  phone: $("phone"), code: $("code"), codeRow: $("code-row"), authMsg: $("auth-msg"),
  loggedIn: $("logged-in"), loggedOut: $("logged-out"), who: $("who"), whoPhone: $("who-phone"),
};

// --- Persistence (best effort: storage may be unavailable) ---------------------------
const store = {
  get(key) { try { return JSON.parse(localStorage.getItem("cs." + key)); } catch { return null; } },
  set(key, value) {
    try {
      if (value === null) localStorage.removeItem("cs." + key);
      else localStorage.setItem("cs." + key, JSON.stringify(value));
    } catch { /* ignore */ }
  },
};

const state = {
  threadId: store.get("thread") || newThreadId(),
  token: store.get("token"),
  customer: store.get("customer"),
  busy: false,
  pendingConfirm: false,
};
store.set("thread", state.threadId);

function newThreadId() {
  return (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2))
    .replace(/[^A-Za-z0-9_-]/g, "");
}

function headers() {
  const h = { "Content-Type": "application/json" };
  if (state.token) h.Authorization = "Bearer " + state.token;
  return h;
}

// --- Rendering ----------------------------------------------------------------------
function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Minimal, safe Markdown: paragraphs, bullet/numbered lists, **bold**, [n] citations.
function renderMarkdown(text) {
  const inline = (s) => escapeHtml(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\[(\d{1,2})\]/g, '<sup class="cite">[$1]</sup>');
  const out = [];
  let list = null;
  for (const line of text.split("\n")) {
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    const kind = bullet ? "ul" : numbered ? "ol" : null;
    if (kind) {
      if (!list || list.kind !== kind) { if (list) out.push(`</${list.kind}>`); list = { kind }; out.push(`<${kind}>`); }
      out.push(`<li>${inline((bullet || numbered)[1])}</li>`);
      continue;
    }
    if (list) { out.push(`</${list.kind}>`); list = null; }
    if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push(`</${list.kind}>`);
  return out.join("");
}

const BADGES = { faq: "Instant answer", handoff: "Human handoff" };

function addMessage(role, text, opts = {}) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + role;
  if (opts.source && BADGES[opts.source]) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = BADGES[opts.source];
    wrap.appendChild(badge);
  }
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  wrap.appendChild(bubble);
  els.messages.appendChild(wrap);
  const msg = { wrap, bubble, raw: text, role };
  paint(msg);
  if (opts.sources && opts.sources.length) addSources(msg, opts.sources);
  scrollDown();
  return msg;
}

function paint(msg) {
  if (msg.role === "user") msg.bubble.textContent = msg.raw;
  else msg.bubble.innerHTML = renderMarkdown(msg.raw);
}

function addSources(msg, sources) {
  const box = document.createElement("div");
  box.className = "sources";
  for (const s of sources) {
    const chip = document.createElement("span");
    chip.className = "source";
    chip.textContent = `[${s.n}] ${s.citation}`;
    box.appendChild(chip);
  }
  msg.wrap.appendChild(box);
}

function scrollDown() { els.messages.scrollTop = els.messages.scrollHeight; }

function showActivity(label) { els.activityText.textContent = label + "…"; els.activity.hidden = false; }
function hideActivity() { els.activity.hidden = true; }

function setBusy(busy) {
  state.busy = busy;
  const locked = busy || state.pendingConfirm;
  els.send.disabled = locked;
  els.input.disabled = locked;
  if (!locked) els.input.focus();
}

function welcome() {
  const msg = addMessage("assistant",
    "Hi, I'm **Nova**, NovaTech's virtual assistant. I can answer product and policy questions, " +
    "check your orders and refunds, and help with cancellations, address changes, returns and repair bookings.");
  const box = document.createElement("div");
  box.className = "suggestions";
  for (const s of [
    "What's your return policy?",
    "How do I pair my Aurora X2 headphones?",
    "Where is my order?",
    "I want to return an item",
    "Cancel my order ORD-1002",
    "I'd like to talk to a person",
  ]) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = s;
    b.addEventListener("click", () => sendMessage(s));
    box.appendChild(b);
  }
  msg.wrap.appendChild(box);
}

// --- Confirmation cards ---------------------------------------------------------------
function showConfirm(data) {
  state.pendingConfirm = true;
  setBusy(state.busy);
  const node = $("confirm-template").content.firstElementChild.cloneNode(true);
  node.querySelector(".confirm-title").textContent = data.title || "Please confirm";
  const dl = node.querySelector(".confirm-details");
  for (const [k, v] of Object.entries(data.details || {})) {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    dl.append(dt, dd);
  }
  node.querySelectorAll("button[data-approve]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const approved = btn.dataset.approve === "true";
      node.classList.add("resolved");
      const result = document.createElement("div");
      result.className = "confirm-result";
      result.textContent = approved ? "✓ Confirmed" : "✕ Cancelled";
      node.appendChild(result);
      state.pendingConfirm = false;
      await runTurn("/api/resume", { thread_id: state.threadId, interrupt_id: data.interrupt_id, approved });
    });
  });
  els.messages.appendChild(node);
  scrollDown();
}

// --- Streaming ----------------------------------------------------------------------
async function postStream(url, body, onEvent) {
  const res = await fetch(url, { method: "POST", headers: headers(), body: JSON.stringify(body) });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const error = new Error(err.detail || `Request failed (${res.status})`);
    error.status = res.status;
    throw error;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const raw = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = "message", data = "";
      for (const line of raw.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      onEvent(event, data ? JSON.parse(data) : {});
    }
  }
}

async function runTurn(url, body) {
  setBusy(true);
  let current = null;        // assistant bubble being streamed
  let pendingSources = null; // citations waiting for the answer bubble
  const newBubble = (text, opts = {}) => {
    const msg = addMessage("assistant", text, { ...opts, sources: pendingSources });
    pendingSources = null;
    return msg;
  };
  try {
    await postStream(url, body, (type, data) => {
      switch (type) {
        case "token":
          hideActivity();
          if (!current) current = newBubble("");
          current.raw += data.text;
          paint(current);
          scrollDown();
          break;
        case "message_end": current = null; break;
        case "message": hideActivity(); current = null; newBubble(data.text, { source: data.source }); break;
        case "tool": showActivity(data.label); break;
        case "sources": pendingSources = data.items; break;
        case "confirm": hideActivity(); showConfirm(data); break;
        case "auth": saveAuth(data.token, data.customer); break;
        case "error": addMessage("system", data.message); break;
        case "done": hideActivity(); break;
      }
    });
  } catch (err) {
    hideActivity();
    if (err.status === 401) { clearAuth(); addMessage("system", "Your session expired. Please sign in again."); }
    else if (err.status === 409 && /another account/.test(err.message)) { resetConversation(); addMessage("system", err.message); }
    else addMessage("system", err.message);
  } finally {
    setBusy(false);
  }
}

function sendMessage(text) {
  text = text.trim();
  if (!text || state.busy || state.pendingConfirm) return;
  addMessage("user", text);
  els.input.value = "";
  autoGrow();
  runTurn("/api/chat", { thread_id: state.threadId, message: text });
}

// --- Account --------------------------------------------------------------------------
function saveAuth(token, customer) {
  state.token = token; state.customer = customer;
  store.set("token", token); store.set("customer", customer);
  renderAccount();
}

function clearAuth() {
  state.token = null; state.customer = null;
  store.set("token", null); store.set("customer", null);
  renderAccount();
}

function renderAccount() {
  const on = Boolean(state.token && state.customer);
  els.loggedIn.hidden = !on;
  els.loggedOut.hidden = on;
  if (on) {
    els.who.textContent = state.customer.name;
    els.whoPhone.textContent = state.customer.phone ? "Phone " + state.customer.phone : "";
  }
}

function authMessage(text, isError = false) {
  els.authMsg.textContent = text;
  els.authMsg.classList.toggle("error", isError);
}

async function postJson(url, body) {
  const res = await fetch(url, { method: "POST", headers: headers(), body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "Request failed");
  return data;
}

$("send-code").addEventListener("click", async () => {
  try {
    const data = await postJson("/api/auth/send-code", { phone: els.phone.value });
    els.codeRow.hidden = false;
    els.code.focus();
    authMessage(`Code sent to ${data.sent_to}. ${data.dev_hint || ""}`);
  } catch (err) { authMessage(err.message, true); }
});

$("login").addEventListener("click", async () => {
  try {
    const data = await postJson("/api/auth/login", { phone: els.phone.value, code: els.code.value });
    saveAuth(data.token, data.customer);
    els.code.value = ""; els.codeRow.hidden = true; authMessage("");
  } catch (err) { authMessage(err.message, true); }
});

$("logout").addEventListener("click", () => { clearAuth(); resetConversation(); });

function resetConversation() {
  state.threadId = newThreadId();
  store.set("thread", state.threadId);
  state.pendingConfirm = false;
  els.messages.innerHTML = "";
  welcome();
  setBusy(false);
}

$("new-chat").addEventListener("click", resetConversation);

// --- Composer ---------------------------------------------------------------------------
function autoGrow() {
  els.input.style.height = "auto";
  els.input.style.height = Math.min(els.input.scrollHeight, 140) + "px";
}
els.input.addEventListener("input", autoGrow);
els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMessage(els.input.value); }
});
els.composer.addEventListener("submit", (e) => { e.preventDefault(); sendMessage(els.input.value); });

// --- Boot -------------------------------------------------------------------------------
async function loadConfig() {
  const cfg = await fetch("/api/config").then((r) => r.json()).catch(() => ({}));
  if (!cfg.dev_mode) return;
  $("demo-card").hidden = false;
  $("dev-code").textContent = cfg.dev_code;
  for (const c of cfg.demo_customers || []) {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = `${c.name} · ${c.phone}`;
    b.addEventListener("click", () => { els.phone.value = c.phone; els.phone.focus(); });
    li.appendChild(b);
    $("demo-list").appendChild(li);
  }
}

async function restoreHistory() {
  try {
    const res = await fetch("/api/history?thread_id=" + encodeURIComponent(state.threadId), { headers: headers() });
    if (res.status === 401) clearAuth();
    if (!res.ok) { resetConversation(); return; }
    const data = await res.json();
    welcome();
    for (const m of data.messages) addMessage(m.role, m.text, { source: m.source, sources: m.sources });
    for (const p of data.pending) showConfirm(p);
  } catch { welcome(); }
}

renderAccount();
loadConfig();
restoreHistory().then(() => setBusy(false));
