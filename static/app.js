const state = {
  sessionId: localStorage.getItem("kemy.sessionId"),
  poll: null,
  lastOutput: "",
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
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

async function login(event) {
  event.preventDefault();
  $("loginError").textContent = "";
  try {
    await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username: $("loginUser").value, password: $("loginPass").value }),
    });
    await checkAuth();
  } catch {
    $("loginError").textContent = "Usuario ou senha invalidos.";
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
    <div><dt>Modo</dt><dd>${data.free_only ? "free" : "fallback"}</dd></div>
    <div><dt>LLM</dt><dd>${data.llm_mode}</dd></div>
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
  }
  if (job.erro) $("output").textContent = job.erro;
}

function formatResult(result) {
  if (result.raw) return result.raw;
  if (result.files?.length) {
    return result.files.map((file) => `# ${file.path}\n\n${file.content}`).join("\n\n---\n\n");
  }
  return JSON.stringify(result, null, 2);
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

$("loginForm").addEventListener("submit", login);
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

checkAuth().catch(() => {
  $("loginView").classList.remove("hidden");
  $("appView").classList.add("hidden");
});
