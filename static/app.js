const state = {
  sessionId: localStorage.getItem("kemy.sessionId"),
  poll: null,
  lastOutput: "",
  authMode: "login",
  sessions: [],
  renderedJobs: new Set(),
};

const $ = (id) => document.getElementById(id);

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("kemy.theme", theme);
  $("themeToggle").textContent = theme === "dark" ? "Claro" : "Escuro";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function showHome() {
  $("homeView").classList.remove("hidden");
  $("chatView").classList.add("hidden");
}

function showChat() {
  $("homeView").classList.add("hidden");
  $("chatView").classList.remove("hidden");
}

function setAuthMode(mode) {
  state.authMode = mode;
  const register = mode === "register";
  $("nameField").classList.toggle("hidden", !register);
  $("authTitle").textContent = register ? "Criar conta na Kemy" : "Entrar na Kemy";
  $("authSubtitle").textContent = register
    ? "Crie um acesso por email e senha para manter conversas, tarefas e contexto."
    : "Use email e senha para acessar suas conversas, tarefas e memoria do workspace.";
  $("authSubmit").textContent = register ? "Criar conta" : "Entrar";
  $("authModeBtn").textContent = register ? "Ja tenho conta" : "Criar conta";
}

async function checkAuth() {
  const data = await api("/api/auth/me");
  $("loginView").classList.toggle("hidden", data.authenticated);
  $("appView").classList.toggle("hidden", !data.authenticated);
  if (data.authenticated) {
    await loadStatus();
    await loadSessions();
    if (state.sessionId) {
      await openSession(state.sessionId, { stayHome: true }).catch(() => {
        state.sessionId = null;
        localStorage.removeItem("kemy.sessionId");
      });
    }
    showHome();
  }
}

async function submitAuth(event) {
  event.preventDefault();
  $("loginError").textContent = "";
  const path = state.authMode === "register" ? "/api/auth/register" : "/api/auth/login";
  try {
    await api(path, {
      method: "POST",
      body: JSON.stringify({
        name: $("loginName").value,
        email: $("loginEmail").value,
        password: $("loginPass").value,
      }),
    });
    await checkAuth();
  } catch (error) {
    $("loginError").textContent = state.authMode === "register"
      ? "Nao foi possivel criar a conta. Confira email e senha com 8+ caracteres."
      : "Email ou senha invalidos.";
  }
}

async function logout() {
  await api("/api/auth/logout", { method: "POST", body: "{}" });
  state.sessionId = null;
  localStorage.removeItem("kemy.sessionId");
  $("chatLog").innerHTML = "";
  showHome();
  await checkAuth();
}

async function loadStatus() {
  const data = await api("/api/status");
  $("apiPill").textContent = data.status === "online" ? "Online" : "Offline";
  $("statusList").innerHTML = `
    <div><dt>API</dt><dd>${data.status}</dd></div>
    <div><dt>Storage</dt><dd>${data.storage}</dd></div>
    <div><dt>Supabase</dt><dd>${data.supabase ? "ativo" : "off"}</dd></div>
    <div><dt>LLM</dt><dd>${data.llm_mode}</dd></div>
  `;
  const providers = Object.entries(data.providers || {})
    .map(([name, enabled]) => `<span>${name}: ${enabled ? "ativo" : "off"}</span>`)
    .join("");
  const tools = Object.entries(data.tools || {})
    .map(([name, enabled]) => `<span>${name}: ${enabled ? "ativo" : "off"}</span>`)
    .join("");
  $("toolStatus").innerHTML = providers + tools;
}

async function loadSessions() {
  const data = await api("/api/sessao/listar");
  state.sessions = data.sessions || [];
  renderSessions();
}

function renderSessions() {
  if (!state.sessions.length) {
    $("sessionList").innerHTML = `<div class="saved-session"><strong>Nenhuma tarefa ainda</strong><small>Comece pela tela inicial.</small></div>`;
    return;
  }
  $("sessionList").innerHTML = state.sessions
    .map((session) => `
      <button class="saved-session ${session.session_id === state.sessionId ? "active" : ""}" data-session-id="${session.session_id}">
        <strong>${escapeHtml(session.title || "Nova conversa")}</strong>
        <small>${escapeHtml(session.preview || "Sem mensagens")}</small>
      </button>
    `)
    .join("");
  document.querySelectorAll("[data-session-id]").forEach((button) => {
    button.addEventListener("click", () => openSession(button.dataset.sessionId));
  });
}

async function newSession({ openChat = false } = {}) {
  const data = await api("/api/sessao/nova", { method: "POST", body: "{}" });
  state.sessionId = data.session_id;
  localStorage.setItem("kemy.sessionId", state.sessionId);
  $("sessionTitle").textContent = "Nova conversa";
  $("chatLog").innerHTML = "";
  resetProgress(data.mensagem || "Nova conversa iniciada.");
  await loadSessions();
  if (openChat) showChat();
}

async function openSession(sessionId, options = {}) {
  const data = await api(`/api/sessao/${sessionId}/historico`);
  state.sessionId = sessionId;
  localStorage.setItem("kemy.sessionId", sessionId);
  $("sessionTitle").textContent = data.title || "Nova conversa";
  $("chatLog").innerHTML = "";
  const history = data.historico || [];
  if (!history.length) {
    appendMessage("assistant", "Estou pronta. Pergunte qualquer coisa ou descreva uma tarefa.");
  } else {
    history.forEach((item) => {
      if (item.role && item.content) {
        appendMessage(item.role, item.content);
        return;
      }
      if (item.usuario) appendMessage("user", item.usuario);
      if (item.resumo) appendMessage("assistant", item.resumo);
    });
  }
  resetProgress("Conversa carregada.");
  renderSessions();
  if (!options.stayHome) showChat();
}

function resetProgress(message) {
  $("jobBadge").textContent = "pronta";
  $("progressBar").style.width = "0%";
  $("timeline").innerHTML = `<li><span></span><p>${escapeHtml(message)}</p></li>`;
}

function renderJob(job) {
  $("jobBadge").textContent = `${job.status} - ${job.progresso}%`;
  $("progressBar").style.width = `${job.progresso}%`;
  $("timeline").innerHTML = (job.eventos || [])
    .map((event) => `<li><span></span><p><strong>${escapeHtml(event.agente)}</strong> ${escapeHtml(event.msg)}</p></li>`)
    .join("");

  if (job.resultado && job.status === "done" && !state.renderedJobs.has(job.job_id)) {
    state.lastOutput = formatResult(job.resultado);
    appendMessage("assistant", state.lastOutput);
    state.renderedJobs.add(job.job_id);
  }
  if (job.erro) {
    appendMessage("assistant", `Erro: ${job.erro}`);
  }
}

function formatResult(result) {
  if (result.raw) return result.raw;
  if (result.summary && result.files?.length) {
    return `${result.summary}\n\n${result.files.map((file) => `### ${file.path}\n\n${file.content}`).join("\n\n")}`;
  }
  if (result.files?.length) {
    return result.files.map((file) => `# ${file.path}\n\n${file.content}`).join("\n\n---\n\n");
  }
  return result.summary || "Concluido.";
}

async function pollJob(jobId) {
  const job = await api(`/api/jobs/${jobId}`);
  renderJob(job);
  if (["done", "error"].includes(job.status)) {
    clearInterval(state.poll);
    state.poll = null;
    $("runBtn").disabled = false;
    $("homeRunBtn").disabled = false;
    await loadSessions();
    return true;
  }
  return false;
}

async function ensureSession() {
  if (!state.sessionId) await newSession();
}

async function runAgents(prompt, source = "chat") {
  const text = (prompt || "").trim();
  if (!text) return;
  await ensureSession();
  showChat();
  $("runBtn").disabled = true;
  $("homeRunBtn").disabled = true;
  appendMessage("user", text);
  $("prompt").value = "";
  $("homePrompt").value = "";
  $("sessionTitle").textContent = titleFromPrompt(text);
  $("jobBadge").textContent = "enviando";
  $("progressBar").style.width = "4%";
  $("timeline").innerHTML = `<li><span></span><p>Mensagem recebida. A Kemy vai decidir se responde, pesquisa ou codifica.</p></li>`;

  const data = await api("/api/comando", {
    method: "POST",
    body: JSON.stringify({ mensagem: text, session_id: state.sessionId, modo: $("mode").value }),
  });
  state.sessionId = data.session_id;
  localStorage.setItem("kemy.sessionId", state.sessionId);
  const done = await pollJob(data.job_id);
  clearInterval(state.poll);
  if (!done) state.poll = setInterval(() => pollJob(data.job_id).catch(console.error), 900);
  if (source === "home") await loadSessions();
}

function appendMessage(role, text) {
  const node = document.createElement("div");
  node.className = `message ${role === "user" ? "user" : "assistant"}`;
  node.textContent = text;
  $("chatLog").appendChild(node);
  node.scrollIntoView({ block: "end", behavior: "smooth" });
}

function titleFromPrompt(text) {
  return text.trim().replace(/\s+/g, " ").slice(0, 58) || "Nova conversa";
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function setPromptAndMaybeRun(text, run = false) {
  $("homePrompt").value = text;
  $("prompt").value = text;
  if (run) runAgents(text, "home").catch(showRunError);
}

function showRunError(error) {
  $("runBtn").disabled = false;
  $("homeRunBtn").disabled = false;
  appendMessage("assistant", `Erro: ${error.message}`);
}

$("authForm").addEventListener("submit", submitAuth);
$("authModeBtn").addEventListener("click", () => setAuthMode(state.authMode === "login" ? "register" : "login"));
$("themeToggle").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
$("logoutBtn").addEventListener("click", () => logout().catch(console.error));
$("newSessionBtn").addEventListener("click", () => newSession({ openChat: false }).then(showHome).catch(console.error));
$("backHomeBtn").addEventListener("click", showHome);
$("refreshSessionsBtn").addEventListener("click", () => loadSessions().catch(console.error));
$("copyBtn").addEventListener("click", () => navigator.clipboard.writeText(state.lastOutput || ""));
$("systemToggle").addEventListener("click", () => $("systemDialog").showModal());
$("systemClose").addEventListener("click", () => $("systemDialog").close());
$("chatForm").addEventListener("submit", (event) => {
  event.preventDefault();
  runAgents($("prompt").value, "chat").catch(showRunError);
});
$("homeRunBtn").addEventListener("click", () => runAgents($("homePrompt").value, "home").catch(showRunError));
$("searchBtn").addEventListener("click", () => setPromptAndMaybeRun("Pesquise contexto atualizado para minha tarefa e traga fontes e proximos passos."));
$("memoryBtn").addEventListener("click", () => setPromptAndMaybeRun("Resuma o que voce lembra desta conversa e quais decisoes ja tomamos."));
$("plusBtn").addEventListener("click", () => appendMessage("assistant", "Anexos e acesso ao PC entram na proxima etapa com Browserless/E2B. Por enquanto cole o contexto aqui."));
$("homeAttachBtn").addEventListener("click", () => setPromptAndMaybeRun("Quero anexar contexto ao projeto. Me diga o melhor formato para colar arquivos, imagens ou logs aqui."));
$("voiceBtn").addEventListener("click", () => appendMessage("assistant", "Voz sera ligada em uma etapa propria: entrada por microfone, resposta em audio e historico salvo."));

document.querySelectorAll("[data-home-prompt]").forEach((button) => {
  button.addEventListener("click", () => setPromptAndMaybeRun(button.dataset.homePrompt, false));
});

document.querySelectorAll("[data-nav-prompt]").forEach((button) => {
  button.addEventListener("click", () => setPromptAndMaybeRun(button.dataset.navPrompt, true));
});

$("homePrompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    runAgents($("homePrompt").value, "home").catch(showRunError);
  }
});

$("prompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    runAgents($("prompt").value, "chat").catch(showRunError);
  }
});

setTheme(localStorage.getItem("kemy.theme") || "light");
setAuthMode("login");
checkAuth().catch(() => {
  $("loginView").classList.remove("hidden");
  $("appView").classList.add("hidden");
});
