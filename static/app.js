const state = {
  sessionId: localStorage.getItem("kemy.sessionId"),
  poll: null,
  lastOutput: "",
  authMode: "login",
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

function setAuthMode(mode) {
  state.authMode = mode;
  const register = mode === "register";
  $("nameField").classList.toggle("hidden", !register);
  $("authTitle").textContent = register ? "Criar sua conta" : "Entrar na Kemy AI";
  $("authSubtitle").textContent = register
    ? "Crie um acesso para salvar sessoes, historico e entregas da Kemy."
    : "Entre no seu workspace privado para criar, revisar e preparar codigo para deploy.";
  $("authSubmit").textContent = register ? "Criar conta" : "Entrar";
  $("authModeBtn").textContent = register ? "Ja tenho conta" : "Criar conta";
}

async function checkAuth() {
  const data = await api("/api/auth/me");
  $("loginView").classList.toggle("hidden", data.authenticated);
  $("appView").classList.toggle("hidden", !data.authenticated);
  if (data.authenticated) {
    await loadStatus();
    if (!state.sessionId) await newSession();
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
      ? "Nao foi possivel criar a conta. Use senha com 8+ caracteres."
      : "Email ou senha invalidos.";
  }
}

async function logout() {
  await api("/api/auth/logout", { method: "POST", body: "{}" });
  state.sessionId = null;
  localStorage.removeItem("kemy.sessionId");
  await checkAuth();
}

async function loadStatus() {
  const data = await api("/api/status");
  $("apiPill").textContent = data.status === "online" ? "Online" : "Offline";
  $("statusList").innerHTML = `
    <div><dt>API</dt><dd>${data.status}</dd></div>
    <div><dt>Storage</dt><dd>${data.storage}</dd></div>
    <div><dt>Supabase</dt><dd>${data.supabase ? "ativo" : "off"}</dd></div>
    <div><dt>Modo</dt><dd>${data.free_only ? "free" : "fallback"}</dd></div>
  `;
  const tools = data.tools || {};
  $("toolStatus").innerHTML = Object.entries(tools)
    .map(([name, enabled]) => `<span>${name}: ${enabled ? "ativo" : "off"}</span>`)
    .join("");
}

async function newSession() {
  const data = await api("/api/sessao/nova", { method: "POST", body: "{}" });
  state.sessionId = data.session_id;
  localStorage.setItem("kemy.sessionId", state.sessionId);
  $("chatLog").innerHTML = `<div class="message assistant">Oi. Eu sou a Kemy. Posso conversar, revisar codigo, planejar features e preparar deploy.</div>`;
  $("timeline").innerHTML = `<li><span class="step-dot idle"></span><strong>Nova sessao iniciada</strong><small>${data.mensagem}</small></li>`;
  $("output").textContent = "Pronto. Escreva a tarefa e execute.";
  $("jobBadge").textContent = "sem tarefa";
  $("progressBar").style.width = "0%";
}

function renderJob(job) {
  $("jobBadge").textContent = `${job.status} · ${job.progresso}%`;
  $("progressBar").style.width = `${job.progresso}%`;
  $("timeline").innerHTML = job.eventos
    .map((event) => `<li><span class="step-dot"></span><strong>${event.agente}</strong><small>${event.msg}</small></li>`)
    .join("");

  if (job.resultado) {
    state.lastOutput = formatResult(job.resultado);
    $("output").textContent = state.lastOutput;
    appendMessage("assistant", state.lastOutput);
  }
  if (job.erro) $("output").textContent = job.erro;
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
  }
}

async function runAgents() {
  const prompt = $("prompt").value.trim();
  if (!prompt) return;
  $("runBtn").disabled = true;
  appendMessage("user", prompt);
  $("prompt").value = "";
  $("output").textContent = "Pensando...";
  const data = await api("/api/comando", {
    method: "POST",
    body: JSON.stringify({ mensagem: prompt, session_id: state.sessionId, modo: $("mode").value }),
  });
  state.sessionId = data.session_id;
  localStorage.setItem("kemy.sessionId", state.sessionId);
  await pollJob(data.job_id);
  clearInterval(state.poll);
  state.poll = setInterval(() => pollJob(data.job_id).catch(console.error), 900);
}

function appendMessage(role, text) {
  const node = document.createElement("div");
  node.className = `message ${role}`;
  node.textContent = text;
  $("chatLog").appendChild(node);
  $("chatLog").scrollTop = $("chatLog").scrollHeight;
}

$("authForm").addEventListener("submit", submitAuth);
$("authModeBtn").addEventListener("click", () => setAuthMode(state.authMode === "login" ? "register" : "login"));
$("themeToggle").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
$("logoutBtn").addEventListener("click", () => logout().catch(console.error));
$("runBtn").addEventListener("click", () => runAgents().catch((error) => {
  $("runBtn").disabled = false;
  $("output").textContent = error.message;
}));
$("newSessionBtn").addEventListener("click", () => newSession().catch(console.error));
$("copyBtn").addEventListener("click", () => navigator.clipboard.writeText(state.lastOutput || $("output").textContent));
$("systemToggle").addEventListener("click", () => $("systemDialog").showModal());
$("systemClose").addEventListener("click", () => $("systemDialog").close());
document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => {
    $("prompt").value = button.dataset.prompt;
    document.querySelectorAll("[data-prompt]").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
  });
});

setTheme(localStorage.getItem("kemy.theme") || "light");
setAuthMode("login");
checkAuth().catch(() => {
  $("loginView").classList.remove("hidden");
  $("appView").classList.add("hidden");
});
