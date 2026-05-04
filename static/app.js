const state = {
  sessionId: localStorage.getItem("kemy.sessionId"),
  poll: null,
  lastOutput: "",
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

async function loadStatus() {
  const data = await api("/api/status");
  $("statusList").innerHTML = `
    <div><dt>API</dt><dd>${data.status}</dd></div>
    <div><dt>Storage</dt><dd>${data.storage}</dd></div>
    <div><dt>Modo</dt><dd>${data.free_only ? "free" : "fallback"}</dd></div>
    <div><dt>LLM</dt><dd>${data.llm_mode}</dd></div>
  `;
  $("apiPill").textContent = data.status === "online" ? "Online" : "Offline";
  if (data.fallback_routes) {
    const renderRoute = (name, label) => {
      const route = data.fallback_routes[name] || [];
      return `<div><strong>${label}</strong><span>${route.filter((item) => item !== "openrouter").join(" -> ")}</span></div>`;
    };
    $("routeList").innerHTML = [
      renderRoute("coding", "Coding"),
      renderRoute("site", "Site"),
      renderRoute("auditoria", "Auditoria"),
      renderRoute("planejamento", "Planejamento"),
    ].join("");
  }
}

async function loadAgents() {
  return Promise.resolve();
}

async function newSession() {
  const data = await api("/api/sessao/nova", { method: "POST", body: "{}" });
  state.sessionId = data.session_id;
  localStorage.setItem("kemy.sessionId", state.sessionId);
  $("timeline").innerHTML = "";
  $("output").textContent = data.mensagem;
  $("jobBadge").textContent = "nova sessao";
  $("progressBar").style.width = "0%";
}

function renderJob(job) {
  $("jobBadge").textContent = `${job.status} · ${job.progresso}%`;
  $("progressBar").style.width = `${job.progresso}%`;
  $("timeline").innerHTML = job.eventos
    .map((event) => `<li><strong>${event.agente}</strong><br>${event.msg}</li>`)
    .join("");

  if (job.resultado) {
    state.lastOutput = JSON.stringify(job.resultado, null, 2);
    $("output").textContent = state.lastOutput;
  }
  if (job.erro) {
    $("output").textContent = job.erro;
  }
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
  $("output").textContent = "Enviando para a fila...";
  const data = await api("/api/comando", {
    method: "POST",
    body: JSON.stringify({
      mensagem: prompt,
      session_id: state.sessionId,
      modo: $("mode").value,
    }),
  });
  state.sessionId = data.session_id;
  localStorage.setItem("kemy.sessionId", state.sessionId);
  await pollJob(data.job_id);
  clearInterval(state.poll);
  state.poll = setInterval(() => pollJob(data.job_id).catch(console.error), 900);
}

$("runBtn").addEventListener("click", () => runAgents().catch((error) => {
  $("runBtn").disabled = false;
  $("output").textContent = error.message;
}));

$("newSessionBtn").addEventListener("click", () => newSession().catch(console.error));
$("copyBtn").addEventListener("click", () => navigator.clipboard.writeText(state.lastOutput || $("output").textContent));

loadStatus().catch(console.error);
loadAgents().catch(console.error);
if (!state.sessionId) newSession().catch(console.error);
