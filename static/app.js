const state = {
  sessionId: null,
  poll: null,
  lastOutput: "",
  lastResultMode: "coding",
  previewFullscreen: false,
  authMode: "login",
  sessions: [],
  renderedJobs: new Set(),
  attachments: [],
  currentUser: null,
};

const $ = (id) => document.getElementById(id);
const on = (id, event, handler) => {
  const element = $(id);
  if (!element) {
    console.warn(`[Kemy UI] Elemento #${id} não encontrado para ${event}.`);
    return;
  }
  element.addEventListener(event, handler);
};

function updateThemeIcon(theme) {
  const sunIcon = document.querySelector(".icon-sun");
  const moonIcon = document.querySelector(".icon-moon");
  if (!sunIcon || !moonIcon) return;
  sunIcon.classList.toggle("hidden", theme === "dark");
  moonIcon.classList.toggle("hidden", theme !== "dark");
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("kemy.theme", theme);
  const toggle = $("themeToggle");
  if (toggle) {
    toggle.setAttribute("aria-label", theme === "dark" ? "Ativar modo claro" : "Ativar modo escuro");
    toggle.setAttribute("title", theme === "dark" ? "Modo claro" : "Modo escuro");
  }
  updateThemeIcon(theme);
}

function sessionStorageKey(user = state.currentUser) {
  return user ? `kemy.sessionId:${user}` : null;
}

function restoreSessionIdForUser(user) {
  const key = sessionStorageKey(user);
  state.sessionId = key ? localStorage.getItem(key) : null;
}

function persistSessionId(sessionId) {
  state.sessionId = sessionId;
  const key = sessionStorageKey();
  if (key && sessionId) localStorage.setItem(key, sessionId);
}

function clearPersistedSessionId(user = state.currentUser) {
  const key = sessionStorageKey(user);
  if (key) localStorage.removeItem(key);
  state.sessionId = null;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let message = "Nao foi possivel concluir a acao.";
    try {
      const payload = await response.json();
      message = payload.detail || payload.message || message;
    } catch {
      message = await response.text();
    }
    throw new Error(message);
  }
  return response.json();
}

function showHome() {
  hidePreview();
  $("homeView").classList.remove("hidden");
  $("chatView").classList.add("hidden");
}

function showChat() {
  $("homeView").classList.add("hidden");
  $("chatView").classList.remove("hidden");
}

function showPreview(html) {
  if (!html) return;
  $("previewFrame").removeAttribute("src");
  $("previewFrame").srcdoc = html;
  $("previewPanel").classList.remove("hidden");
  $("chatView").classList.add("has-preview");
  updatePreviewFullscreenUI();
}

function showPreviewUrl(url) {
  if (!url) return;
  $("previewFrame").removeAttribute("srcdoc");
  $("previewFrame").src = url;
  $("previewPanel").classList.remove("hidden");
  $("chatView").classList.add("has-preview");
  updatePreviewFullscreenUI();
}

function hidePreview() {
  setPreviewFullscreen(false);
  $("previewFrame").srcdoc = "";
  $("previewFrame").removeAttribute("src");
  $("previewPanel").classList.add("hidden");
  $("chatView").classList.remove("has-preview");
  $("previewPrompt").value = "";
  updatePreviewFullscreenUI();
}

function updatePreviewFullscreenUI() {
  const active = document.body.classList.contains("preview-fullscreen");
  state.previewFullscreen = active;
  $("previewFullscreenBtn").textContent = active ? "Sair tela cheia" : "Tela cheia";
}

function setPreviewFullscreen(active) {
  if (active && $("previewPanel").classList.contains("hidden")) return;
  document.body.classList.toggle("preview-fullscreen", Boolean(active));
  updatePreviewFullscreenUI();
}

function togglePreviewFullscreen() {
  setPreviewFullscreen(!state.previewFullscreen);
}

function setAuthMode(mode) {
  state.authMode = mode;
  const register = mode === "register";
  $("nameField").classList.toggle("hidden", !register);
  $("authTitle").textContent = register ? "Criar conta na Kemy" : "Entrar na Kemy";
  $("authSubtitle").textContent = register
    ? "Crie um acesso por email e senha para manter conversas, tarefas e contexto."
    : "Use email ou usuário e senha para acessar suas conversas, tarefas e memória do workspace.";
  $("authSubmit").textContent = register ? "Criar conta" : "Entrar no workspace";
  $("authModeBtn").textContent = register ? "Já tenho conta" : "Criar conta";
}

async function checkAuth() {
  const data = await api("/api/auth/me");
  state.currentUser = data.user || null;
  $("loginView").classList.toggle("hidden", data.authenticated);
  $("appView").classList.toggle("hidden", !data.authenticated);
  if (data.authenticated) {
    restoreSessionIdForUser(state.currentUser);
    await loadStatus();
    await loadSessions();
    const latestSessionId = state.sessionId || state.sessions[0]?.session_id || null;
    if (!state.sessionId && latestSessionId) {
      persistSessionId(latestSessionId);
    }
    if (state.sessionId) {
      const opened = await openSession(state.sessionId, { stayHome: false }).then(() => true).catch(() => {
        clearPersistedSessionId();
        return false;
      });
      if (opened) return;
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
    $("loginError").textContent = error.message || (state.authMode === "register"
      ? "Nao foi possivel criar a conta."
      : "Email ou senha invalidos.");
  }
}

async function logout() {
  await api("/api/auth/logout", { method: "POST", body: "{}" });
  clearPersistedSessionId();
  $("chatLog").innerHTML = "";
  showHome();
  await checkAuth();
}

async function loadStatus() {
  const data = await api("/api/status");
  $("apiPill").textContent = data.status === "online" ? "Online" : "Offline";
  const cache = data.cache || {};
  $("statusList").innerHTML = `
    <div style="background: var(--surface-2); padding: 12px; border-radius: var(--radius-sm); border: 1px solid var(--line);"><dt style="font-size: 12px; color: var(--muted); text-transform: uppercase;">API</dt><dd style="font-weight: 600; margin-top: 4px;">${data.status}</dd></div>
    <div style="background: var(--surface-2); padding: 12px; border-radius: var(--radius-sm); border: 1px solid var(--line);"><dt style="font-size: 12px; color: var(--muted); text-transform: uppercase;">Storage</dt><dd style="font-weight: 600; margin-top: 4px;">${data.storage}</dd></div>
    <div style="background: var(--surface-2); padding: 12px; border-radius: var(--radius-sm); border: 1px solid var(--line);"><dt style="font-size: 12px; color: var(--muted); text-transform: uppercase;">Cache</dt><dd style="font-weight: 600; margin-top: 4px;">${cache.keys ?? 0} keys</dd></div>
    <div style="background: var(--surface-2); padding: 12px; border-radius: var(--radius-sm); border: 1px solid var(--line);"><dt style="font-size: 12px; color: var(--muted); text-transform: uppercase;">LLM Mode</dt><dd style="font-weight: 600; margin-top: 4px;">${data.llm_mode}</dd></div>
  `;
}

async function loadSessionInsights() {
  const target = $("sessionInsights");
  if (!state.sessionId) {
    target.innerHTML = "";
    return;
  }
  try {
    const data = await api(`/api/analytics/${state.sessionId}`);
    const context = await api(`/api/sessao/${state.sessionId}/contexto`).catch(() => null);
    const summary = context?.contexto_compacto?.summary || "";
    target.innerHTML = `
      <div style="margin-top: 24px;">
        <h3 style="font-size: 14px; margin-bottom: 8px;">Dados da Sessão</h3>
        <p style="color: var(--muted); font-size: 13px;">${data.total_jobs || 0} jobs executados | ${data.message_count || 0} mensagens | ${data.generated_files || 0} arquivos</p>
      </div>
      ${summary ? `<div style="margin-top: 16px;"><h3 style="font-size: 14px; margin-bottom: 8px;">Contexto Aprendido</h3><p style="color: var(--muted); font-size: 13px; line-height: 1.5;">${escapeHtml(summary)}</p></div>` : ""}
    `;
  } catch {
    target.innerHTML = "";
  }
}

async function loadSessions() {
  const data = await api("/api/sessao/listar");
  state.sessions = data.sessions || [];
  renderSessions();
}

function renderSessions() {
  if (!state.sessions.length) {
    $("sessionList").innerHTML = `
      <div class="session-empty-state" style="padding: 16px; text-align: center; color: var(--muted); font-size: 13px; background: var(--surface-2); border-radius: var(--radius-md);">
        <strong>Workspace vazio</strong>
        <p style="margin-top: 8px;">Crie a primeira tarefa para começar.</p>
      </div>
    `;
    return;
  }
  $("sessionList").innerHTML = state.sessions
    .map((session) => `
      <div class="saved-session ${session.session_id === state.sessionId ? "active" : ""}">
        <div class="saved-session-main" data-session-id="${session.session_id}">
          <strong style="display: block; font-size: 13px; white-space: nowrap; text-overflow: ellipsis; overflow: hidden;">${escapeHtml(session.title || "Nova conversa")}</strong>
        </div>
        <button type="button" class="saved-session-delete" data-delete-session="${session.session_id}" aria-label="Excluir tarefa" title="Excluir tarefa"><span aria-hidden="true">&times;</span></button>
      </div>
    `)
    .join("");
  document.querySelectorAll("[data-session-id]").forEach((button) => {
    button.addEventListener("click", () => openSession(button.dataset.sessionId));
  });
  document.querySelectorAll("[data-delete-session]").forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      const sessionId = button.dataset.deleteSession;
      const confirmed = window.confirm("Excluir esta conversa? Esta acao remove o historico salvo.");
      if (!confirmed) return;
      await deleteSession(sessionId);
    });
  });
}

async function newSession({ openChat = false } = {}) {
  const data = await api("/api/sessao/nova", { method: "POST", body: "{}" });
  persistSessionId(data.session_id);
  state.lastResultMode = "coding";
  $("sessionTitle").textContent = "Nova conversa";
  $("chatLog").innerHTML = "";
  hidePreview();
  resetProgress(data.mensagem || "Sistema inicializado. Aguardando instrução.");
  await loadSessions();
  if (openChat) showChat();
}

async function openSession(sessionId, options = {}) {
  const data = await api(`/api/sessao/${sessionId}/historico`);
  persistSessionId(sessionId);
  $("sessionTitle").textContent = data.title || "Nova conversa";
  $("chatLog").innerHTML = "";
  const history = data.historico || [];
  const inferredHistoryMode = inferModeFromHistory(history);
  state.lastResultMode = inferredHistoryMode || "coding";
  if (!history.length) {
    appendMessage("assistant", "Estou pronta. Pergunte qualquer coisa ou descreva uma tarefa.");
  } else {
    history.forEach((item) => {
      if (item.role && item.content) {
        if (item.role === "assistant" && ((item.files || item.result?.files || []).length || item.result?.document_title)) {
          appendResult(
            {
              files: item.files || item.result?.files || [],
              document_title: item.result?.document_title,
              summary: item.result?.summary || item.content,
              provider: item.provider || item.result?.provider,
              model: item.model || item.result?.model,
            },
            item.content,
          );
        } else if (item.role === "assistant" && (item.image_url || item.result?.image_url || extractImageUrl(item))) {
          appendResult(
            {
              image_url: item.image_url || item.result?.image_url,
              image_data_url: item.image_data_url || item.result?.image_data_url,
              summary: item.result?.summary || item.content,
              provider: item.provider || item.result?.provider,
              model: item.model || item.result?.model,
            },
            item.content,
          );
        } else {
          appendMessage(item.role, item.content);
        }
        return;
      }
      if (item.usuario) appendMessage("user", item.usuario);
      if (item.resumo) appendMessage("assistant", item.resumo);
    });
  }
  const lastAssistantWithPreview = [...history].reverse().find((item) => {
    if (item.role !== "assistant") return false;
    const target = item.result || item;
    return Boolean(extractPreviewHtml(target, item.content || "") || extractPreviewUrl(target));
  });
  const previewHtml = lastAssistantWithPreview ? extractPreviewHtml(lastAssistantWithPreview.result || lastAssistantWithPreview, lastAssistantWithPreview.content || "") : "";
  const previewUrl = lastAssistantWithPreview ? extractPreviewUrl(lastAssistantWithPreview.result || lastAssistantWithPreview) : "";
  if (previewHtml) showPreview(previewHtml);
  else if (previewUrl) showPreviewUrl(previewUrl);
  else hidePreview();
  resetProgress("Conversa carregada. Pronta para continuar.");
  renderSessions();
  if (!options.stayHome) showChat();
}

async function deleteSession(sessionId) {
  await api(`/api/sessao/${sessionId}`, { method: "DELETE" });
  if (state.sessionId === sessionId) {
    clearPersistedSessionId();
    $("chatLog").innerHTML = "";
    $("sessionTitle").textContent = "Nova conversa";
    resetProgress("Conversa excluida.");
    showHome();
  }
  await loadSessions();
}

function resetProgress(message) {
  $("jobBadge").textContent = "Status: Aguardando";
  $("timeline").innerHTML = `<li><p>${escapeHtml(message)}</p></li>`;
}

function renderJob(job) {
  $("jobBadge").textContent = `Status: ${job.status} (${job.progresso}%)`;
  $("timeline").innerHTML = (job.eventos || [])
    .map((event) => `<li><p><strong style="color: var(--ink-strong);">${escapeHtml(event.agente)}</strong>: ${escapeHtml(event.msg)}</p></li>`)
    .join("");

  if (job.resultado && job.status === "done" && !state.renderedJobs.has(job.job_id)) {
    state.lastOutput = formatResult(job.resultado);
    const inferredMode = job.modo || inferModeFromResult(job.resultado, state.lastOutput);
    if (inferredMode) state.lastResultMode = inferredMode;
    appendResult(job.resultado, state.lastOutput);
    state.renderedJobs.add(job.job_id);
    if (job.resultado.git_output) {
      const chatLog = $(\"chatLog\");
      if (chatLog) {
        chatLog.appendChild(renderGitTerminal(
          job.resultado.git_operation || \"operation\",
          job.resultado.git_output,
          job.resultado.git_success !== false
        ));
        chatLog.lastChild.scrollIntoView({ behavior: \"smooth\", block: \"end\" });
      }
    }
    const previewHtml = extractPreviewHtml(job.resultado, state.lastOutput);
    const previewUrl = extractPreviewUrl(job.resultado);
    if (previewHtml) showPreview(previewHtml);
    else if (previewUrl) showPreviewUrl(previewUrl);
  }
  if (job.erro) {
    appendMessage("assistant", `**Erro na execução:** ${job.erro}`);
  }
}

function formatResult(result) {
  if (extractImageSource(result)) {
    return result.raw || result.summary || "Imagem gerada.";
  }
  if ((result.files || []).some((file) => file.download_url)) {
    return result.summary || result.raw || "Arquivos gerados com sucesso.";
  }
  if (extractArtifactFiles(result.raw || result.summary || "").length) {
    return result.summary || "Projeto gerado. O live preview foi aberto ao lado.";
  }
  if (result.raw) return result.raw;
  if (result.summary && result.files?.length) {
    return `${result.summary}\n\n${result.files.map((file) => `### ${file.path}\n\n\`\`\`\n${file.content}\n\`\`\``).join("\n\n")}`;
  }
  if (result.files?.length) {
    return result.files.map((file) => `### ${file.path}\n\n\`\`\`\n${file.content}\n\`\`\``).join("\n\n---\n\n");
  }
  return result.summary || "Concluido.";
}

let pollErrorCount = 0;
async function pollJob(jobId) {
  try {
    const job = await api(`/api/jobs/${jobId}`);
    pollErrorCount = 0;
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
  } catch (err) {
    pollErrorCount++;
    console.error("Erro na sondagem do job:", err);
    if (pollErrorCount >= 5) {
      clearInterval(state.poll);
      state.poll = null;
      $("runBtn").disabled = false;
      $("homeRunBtn").disabled = false;
      appendMessage("assistant", "**Erro de Conexão:** Falha persistente ao se comunicar com o servidor. Por favor, recarregue a página e tente novamente.");
      return true;
    }
    return false;
  }
}

async function ensureSession(options = {}) {
  if (options.forceNewSession) {
    await newSession({ openChat: true });
    return;
  }
  if (!state.sessionId) {
    await newSession();
    return;
  }
  try {
    await api(`/api/sessao/${state.sessionId}/historico`);
  } catch {
    clearPersistedSessionId();
    await newSession();
  }
}

function resolveRequestedMode(text) {
  const selectedMode = $("mode").value;
  if (selectedMode !== "coding") return selectedMode;
  const lowered = String(text || "").toLowerCase();
  const imageMarkers = ["gere uma imagem", "gera uma imagem", "crie uma imagem", "desenhe", "ilustre", "imagem de", "foto de", "logo de", "banner de"];
  const slideMarkers = ["slide", "slides", "deck", "ppt", "pptx", "powerpoint", "apresentacao", "apresentação"];
  const documentMarkers = [".docx", "docx", "docxs", ".pdf", "pdf", ".md", "markdown", "documento", "abnt", "relatorio", "relatório", "proposta", "contrato"];
  const siteMarkers = ["site", "landing page", "dashboard", "frontend", "pagina", "página", "app web", "web app", "html", "tailwind", "saas", "crud"];
  const siteFollowupMarkers = ["mude", "altere", "ajuste", "refaca", "refaça", "melhore", "evolua", "troque", "adicione", "implemente", "deixe", "aplique"];
  if (imageMarkers.some((marker) => lowered.includes(marker))) return "imagem";
  if (slideMarkers.some((marker) => lowered.includes(marker))) return "documento";
  if (documentMarkers.some((marker) => lowered.includes(marker))) return "documento";
  if (siteMarkers.some((marker) => lowered.includes(marker))) return "site";
  const hasSiteFollowupSignal = siteFollowupMarkers.some((marker) => lowered.includes(marker)) || lowered.split(/\s+/).length <= 18;
  if (state.lastResultMode === "site" && hasSiteFollowupSignal) return "site";
  return selectedMode;
}

function resolveDailyMode(text) {
  const lowered = String(text || "").toLowerCase();
  const dailyMarkers = ["resuma", "resumo", "traduza", "traduzir", "organize", "checklist", "roteiro", "agenda", "planejamento", "plano", "email", "mensagem", "texto", "explique", "ideias", "brainstorm"];
  if (dailyMarkers.some((marker) => lowered.includes(marker)) && lowered.split(/\s+/).length > 2) return "planejamento";
  return "";
}

function inferModeFromResult(result, fallbackText = "") {
  if (!result) return "";
  if (extractImageSource(result)) return "imagem";
  const files = result.files || [];
  if (result.document_title || files.some((file) => String(file.name || file.path || "").toLowerCase().endsWith(".docx"))) {
    return "documento";
  }
  if (
    extractPreviewUrl(result) ||
    extractPreviewHtml(result, fallbackText) ||
    files.some((file) => String(file.name || file.path || "").toLowerCase().endsWith(".html"))
  ) {
    return "site";
  }
  return "";
}

function inferModeFromHistory(history) {
  const assistantItems = [...(history || [])].reverse().filter((item) => item.role === "assistant");
  for (const item of assistantItems) {
    const inferred = inferModeFromResult(item.result || item, item.content || "");
    if (inferred) return inferred;
  }
  return "";
}

async function runAgents(prompt, source = "chat", options = {}) {
  const text = (prompt || "").trim();
  if (!text) return;
  const displayText = (options.displayText || text).trim();
  const forcedMode = options.forcedMode || "";
  const keepAttachments = Boolean(options.keepAttachments);
  await ensureSession({ forceNewSession: source === "home" });
  showChat();
  $("runBtn").disabled = true;
  $("homeRunBtn").disabled = true;
  appendMessage("user", displayText);
  const attachments = [...state.attachments];
  if (!options.keepPromptInput) {
    $("prompt").value = "";
    $("homePrompt").value = "";
  }
  if (!keepAttachments) clearAttachments();
  $("sessionTitle").textContent = titleFromPrompt(text);
  $("jobBadge").textContent = "Processando...";
  $("timeline").innerHTML = `<li><p>Processando arquitetura da resposta e acionando agentes...</p></li>`;

  let data;
  const intentMode = resolveRequestedMode(text);
  const requestedMode = forcedMode || (intentMode === "coding" ? (resolveDailyMode(text) || "coding") : intentMode);
  try {
    const repoUrl = $("gitRepo")?.value || undefined;
    data = await api("/api/comando", {
      method: "POST",
      body: JSON.stringify({ mensagem: text, session_id: state.sessionId, modo: requestedMode, anexos: attachments, github_repo: repoUrl }),
    });
  } catch (error) {
    clearPersistedSessionId();
    await ensureSession();
    const repoUrl = $("gitRepo")?.value || undefined;
    data = await api("/api/comando", {
      method: "POST",
      body: JSON.stringify({ mensagem: text, session_id: state.sessionId, modo: requestedMode, anexos: attachments, github_repo: repoUrl }),
    });
  }
  persistSessionId(data.session_id);
  const done = await pollJob(data.job_id);
  clearInterval(state.poll);
  if (!done) state.poll = setInterval(() => pollJob(data.job_id).catch(console.error), 900);
  if (source === "home") await loadSessions();
}

async function applyPreviewEdit() {
  const raw = ($("previewPrompt").value || "").trim();
  if (!raw) return;
  if ($("previewPanel").classList.contains("hidden")) {
    appendMessage("assistant", "Abra um live preview antes de pedir ajustes contextuais.");
    return;
  }
  const systemized = `No projeto aberto no live preview, aplique esta alteracao mantendo o que ja funciona: ${raw}`;
  $("previewApplyBtn").disabled = true;
  try {
    await runAgents(systemized, "chat", {
      forcedMode: "site",
      displayText: raw,
      keepPromptInput: true,
      keepAttachments: false,
    });
    $("previewPrompt").value = "";
  } finally {
    $("previewApplyBtn").disabled = false;
  }
}

// ==========================================
// KEMY MARKDOWN PARSER
// Transforma o texto cru do LLM em HTML bonito
// ==========================================
function parseMarkdown(text) {
  if (!text) return "";
  
  // 1. Escapar HTML para segurança
  let html = escapeHtml(text);
  
  // 2. Proteger Blocos de Código (Ignorar formatação dentro deles)
  const codeBlocks = [];
  html = html.replace(/```[\s\S]*?```/g, (match) => {
    codeBlocks.push(match);
    return `%%%CODEBLOCK_${codeBlocks.length - 1}%%%`;
  });

  // 3. Formatar Títulos (Headers)
  html = html.replace(/^### (.*$)/gim, '<h3 style="margin-top: 24px; margin-bottom: 12px; font-size: 18px; color: var(--ink-strong);">$1</h3>');
  html = html.replace(/^## (.*$)/gim, '<h2 style="margin-top: 32px; margin-bottom: 16px; font-size: 22px; color: var(--ink-strong);">$1</h2>');
  html = html.replace(/^# (.*$)/gim, '<h1 style="margin-top: 32px; margin-bottom: 16px; font-size: 26px; color: var(--ink-strong);">$1</h1>');
  
  // 4. Formatar Negrito e Itálico
  html = html.replace(/\*\*(.*?)\*\*/g, '<strong style="color: var(--ink-strong);">$1</strong>');
  html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');
  
  // 5. Formatar Código Inline
  html = html.replace(/`(.*?)`/g, '<code style="background: var(--surface-2); padding: 2px 6px; border-radius: 6px; font-family: \'JetBrains Mono\', monospace; font-size: 13px; color: var(--accent);">$1</code>');

  // 6. Criar Parágrafos e Listas
  html = html.split('\n\n').map(p => {
    if (p.startsWith('<h') || p.startsWith('%%%CODEBLOCK')) return p;
    
    // Identifica se o bloco é uma lista
    if (p.match(/^\s*[-*]\s/m) || p.match(/^\s*\d+\.\s/m)) {
      const items = p.split('\n').filter(l => l.trim()).map(l => {
        const content = l.replace(/^\s*[-*]\s/, '').replace(/^\s*\d+\.\s/, '');
        return `<li style="margin-bottom: 8px; margin-left: 24px;">${content}</li>`;
      }).join('');
      return `<ul style="margin-bottom: 16px; padding: 0;">${items}</ul>`;
    }
    
    // Converte quebras de linha simples em <br>
    return `<p style="margin-bottom: 16px;">${p.replace(/\n/g, '<br>')}</p>`;
  }).join('\n');

  // 7. Restaurar Blocos de Código renderizados
  html = html.replace(/%%%CODEBLOCK_(\d+)%%%/g, (match, i) => {
    let block = codeBlocks[i];
    // Remove as marcações de escape seguras apenas dentro do pre/code
    block = block.replace(/```(\w*)\n([\s\S]*?)```/, '<div class="code-block-wrapper" style="position: relative; margin: 16px 0;"><button class="copy-code-btn secondary-btn compact-btn" type="button" aria-label="Copiar código" title="Copiar código" style="position: absolute; top: 8px; right: 8px; z-index: 10;">Copiar</button><pre style="background: var(--surface-2); padding: 40px 16px 16px; border-radius: var(--radius-md); overflow-x: auto; border: 1px solid var(--line-strong); margin: 0;"><code style="font-family: \'JetBrains Mono\', monospace; font-size: 14px;">$2</code></pre></div>');
    // Fallback caso o LLM não envie o tipo de linguagem
    block = block.replace(/```([\s\S]*?)```/, '<div class="code-block-wrapper" style="position: relative; margin: 16px 0;"><button class="copy-code-btn secondary-btn compact-btn" type="button" aria-label="Copiar código" title="Copiar código" style="position: absolute; top: 8px; right: 8px; z-index: 10;">Copiar</button><pre style="background: var(--surface-2); padding: 40px 16px 16px; border-radius: var(--radius-md); overflow-x: auto; border: 1px solid var(--line-strong); margin: 0;"><code style="font-family: \'JetBrains Mono\', monospace; font-size: 14px;">$1</code></pre></div>');
    return block;
  });

  return html;
}

// Atualizamos a função para injetar o HTML processado
function appendMessage(role, text) {
  const node = document.createElement("div");
  node.className = `message ${role === "user" ? "user" : "assistant"}`;
  
  if (role === "user") {
    node.textContent = text; // Mensagem do usuário continua como texto puro
  } else {
    node.innerHTML = parseMarkdown(text); // Kemy usa o parser
  }
  
  $("chatLog").appendChild(node);
  node.scrollIntoView({ block: "end", behavior: "smooth" });
}

function appendResult(result, fallbackText) {
  const downloadableFiles = prioritizeFiles((result.files || []).filter((file) => file.download_url));
  const inlinePreview = extractPreviewHtml(result, fallbackText);
  const previewUrl = extractPreviewUrl(result);
  const archiveUrl = result.project_archive_url || downloadableFiles.find((file) => String(file.name || "").toLowerCase().endsWith(".zip"))?.download_url || "";
  if (downloadableFiles.length || previewUrl || archiveUrl || inlinePreview) {
    const node = document.createElement("div");
    node.className = "message assistant";
    node.innerHTML = `
      <div class="result-card">
        <div class="result-card-head">
          <strong>${escapeHtml(result.document_title || result.artifact_title || result.summary || "Projeto gerado")}</strong>
          <span class="result-meta">${escapeHtml((result.provider || "kemy") + " - " + (result.model || "preview"))}</span>
        </div>
        <div class="project-actions">
          ${previewUrl ? `<a href="${escapeHtml(previewUrl)}" target="_blank" rel="noreferrer" class="primary-link">Abrir preview do projeto</a>` : ""}
          ${inlinePreview ? `<button type="button" class="primary-link" data-open-inline-preview>Abrir live preview</button>` : ""}
          ${archiveUrl ? `<a href="${escapeHtml(archiveUrl)}" target="_blank" rel="noreferrer" class="primary-link">Baixar projeto ZIP</a>` : ""}
        </div>
        ${downloadableFiles.length ? `
          <div class="file-actions">
            ${downloadableFiles.map((file) => `<a href="${escapeHtml(file.download_url)}" target="_blank" rel="noreferrer" class="secondary-btn">Download ${escapeHtml(file.name)}</a>`).join("")}
          </div>
        ` : ""}
        ${inlinePreview ? `<p class="result-note">Live preview pronto no painel lateral.</p>` : ""}
        ${renderCodeFileTable(downloadableFiles)}
      </div>
    `;
    $("chatLog").appendChild(node);
    node.querySelector("[data-open-inline-preview]")?.addEventListener("click", () => showPreview(inlinePreview));
    if (inlinePreview) showPreview(inlinePreview);
    else if (previewUrl) showPreviewUrl(previewUrl);
    node.scrollIntoView({ block: "end", behavior: "smooth" });
    return;
  }
  const imageSource = extractImageSource(result) || extractImageSource({ raw: fallbackText, summary: fallbackText });
  if (!imageSource) {
    const downloadableFiles = prioritizeFiles((result.files || []).filter((file) => file.download_url));
    const inlinePreview = extractPreviewHtml(result, fallbackText);
    if (downloadableFiles.length) {
      const previewUrl = extractPreviewUrl(result);
      const archiveUrl = result.project_archive_url || downloadableFiles.find((file) => String(file.name || "").toLowerCase().endsWith(".zip"))?.download_url || "";
      const node = document.createElement("div");
      node.className = "message assistant";
      node.innerHTML = `
        <div class="result-card">
          <div class="result-card-head">
            <strong>${escapeHtml(result.document_title || result.summary || "Arquivos gerados")}</strong>
            <span class="result-meta">${escapeHtml((result.provider || "kemy") + " - " + (result.model || ""))}</span>
          </div>
          ${(previewUrl || archiveUrl) ? `
            <div class="project-actions">
              ${previewUrl ? `<a href="${escapeHtml(previewUrl)}" target="_blank" rel="noreferrer" class="primary-link">Abrir preview do projeto</a>` : ""}
              ${archiveUrl ? `<a href="${escapeHtml(archiveUrl)}" target="_blank" rel="noreferrer" class="primary-link">Baixar projeto ZIP</a>` : ""}
            </div>
          ` : ""}
          <div class="file-actions">
            ${downloadableFiles.map((file) => `<a href="${escapeHtml(file.download_url)}" target="_blank" rel="noreferrer" class="secondary-btn">Download ${escapeHtml(file.name)}</a>`).join("")}
          </div>
          ${renderCodeFileTable(downloadableFiles)}
          ${fallbackText ? parseMarkdown(fallbackText) : ""}
        </div>
      `;
      $("chatLog").appendChild(node);
      node.scrollIntoView({ block: "end", behavior: "smooth" });
      return;
    }
    if (inlinePreview) {
      const node = document.createElement("div");
      node.className = "message assistant";
      node.innerHTML = `
        <div class="result-card">
          <div class="result-card-head">
            <strong>${escapeHtml(result.artifact_title || result.summary || "Preview do site pronto")}</strong>
            <span class="result-meta">kemy - live preview</span>
          </div>
          <div class="project-actions">
            <button type="button" class="primary-link" data-open-inline-preview>Abrir live preview</button>
          </div>
          <p class="result-note">Extraí o site do artifact gerado e abri no painel de preview.</p>
        </div>
      `;
      $("chatLog").appendChild(node);
      node.querySelector("[data-open-inline-preview]")?.addEventListener("click", () => showPreview(inlinePreview));
      showPreview(inlinePreview);
      node.scrollIntoView({ block: "end", behavior: "smooth" });
      return;
    }
    appendMessage("assistant", fallbackText);
    return;
  }
  const node = document.createElement("div");
  node.className = "message assistant";
  node.innerHTML = `
    <div class="result-card">
      <div class="result-card-head">
        <strong>${escapeHtml(result.summary || "Imagem gerada")}</strong>
      </div>
      <img src="${escapeHtml(imageSource)}" alt="${escapeHtml(result.prompt || result.summary || "Imagem")}" />
      <div class="result-actions">
        <a href="${escapeHtml(imageSource)}" target="_blank" rel="noreferrer" class="secondary-btn">Abrir original</a>
      </div>
      ${fallbackText ? parseMarkdown(fallbackText) : ""}
    </div>
  `;
  $("chatLog").appendChild(node);
  node.scrollIntoView({ block: "end", behavior: "smooth" });
}

function extractImageSource(result) {
  return result?.image_data_url || result?.result?.image_data_url || extractImageUrl(result);
}

function extractImageUrl(result) {
  const direct = result?.image_url || result?.result?.image_url;
  if (direct) return direct;
  const text = [result?.raw, result?.summary, result?.content].filter(Boolean).join("\n");
  const match = text.match(/https?:\/\/\S+/i);
  return match ? match[0].replace(/[)\],.]+$/, "") : "";
}

function prioritizeFiles(files) {
  return [...files].sort((a, b) => filePriority(a) - filePriority(b));
}

function filePriority(file) {
  const mime = String(file?.mime_type || "").toLowerCase();
  const name = String(file?.name || "").toLowerCase();
  if (
    mime === "application/vnd.openxmlformats-officedocument.presentationml.presentation" ||
    name.endsWith(".pptx")
  ) return 0;
  if (mime === "application/pdf" || name.endsWith(".pdf")) return 1;
  if (mime.includes("word") || name.endsWith(".docx")) return 2;
  if (mime === "application/zip" || name.endsWith(".zip")) return 3;
  if (mime === "text/html" || name.endsWith(".html")) return 4;
  if (mime === "text/markdown" || name.endsWith(".md")) return 5;
  return 10;
}

function renderCodeFileTable(files) {
  const codeFiles = (files || []).filter((file) => file.content);
  if (!codeFiles.length) return "";
  return `
    <div class="code-file-table" aria-label="Arquivos de codigo gerados">
      <div class="code-file-table-head">
        <span>Arquivos do projeto</span>
        <small>${codeFiles.length} arquivo(s) com codigo visivel</small>
      </div>
      ${codeFiles.map((file, index) => `
        <details class="code-file-row" ${index === 0 ? "open" : ""}>
          <summary>
            <span class="code-file-path">${escapeHtml(file.relative_path || file.name)}</span>
            <span class="code-file-lang">${escapeHtml(file.language || inferLanguage(file.name))}</span>
          </summary>
          <pre><code>${escapeHtml(file.content)}</code></pre>
        </details>
      `).join("")}
    </div>
  `;
}

function inferLanguage(name) {
  const suffix = String(name || "").split(".").pop()?.toLowerCase() || "text";
  const map = { js: "javascript", ts: "typescript", jsx: "jsx", tsx: "tsx", md: "markdown" };
  return map[suffix] || suffix;
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
  appendMessage("assistant", `**Falha do Sistema:** ${error.message}`);
}

function extractPreviewHtml(result, fallbackText = "") {
  let fromPreview = "";
  let fromFile = "";
  let fromFence = "";
  if (result?.preview_html) {
    fromPreview = normalizePreviewHtml(result.preview_html);
    if (fromPreview && !looksLikeViteShellHtml(fromPreview)) return fromPreview;
  }
  const files = result?.files || [];
  const htmlFile = files.find((file) => {
    const name = String(file.relative_path || file.name || file.path || "").toLowerCase();
    return name.endsWith("preview.html") || name.endsWith("index.html") || name.endsWith(".html");
  });
  if (htmlFile?.content) {
    fromFile = normalizePreviewHtml(htmlFile.content);
    if (fromFile && !looksLikeViteShellHtml(fromFile)) return fromFile;
  }
  const match = fallbackText.match(/```html\s*([\s\S]*?)```/i);
  if (match) {
    fromFence = normalizePreviewHtml(match[1]);
    if (fromFence && !looksLikeViteShellHtml(fromFence)) return fromFence;
  }
  const artifactFiles = extractArtifactFiles([result?.raw, result?.summary, fallbackText].filter(Boolean).join("\n"));
  const artifactHtml = artifactFiles.find((file) => file.path.toLowerCase().endsWith("preview.html"))
    || artifactFiles.find((file) => file.path.toLowerCase().endsWith("index.html"))
    || artifactFiles.find((file) => file.path.toLowerCase().endsWith(".html"));
  const fromArtifact = normalizePreviewHtml(artifactHtml?.content || "");
  if (fromArtifact && !looksLikeViteShellHtml(fromArtifact)) return fromArtifact;
  if (fromPreview || fromFile || fromFence || fromArtifact) return buildEmergencyPreviewHtml(result, fallbackText);
  return "";
}

function extractPreviewUrl(result) {
  if (result?.preview_url) return result.preview_url;
  const files = result?.files || [];
  const htmlFile = files.find((file) => (String(file.mime_type || "").toLowerCase() === "text/html" || String(file.name || "").toLowerCase().endsWith(".html")) && file.download_url);
  return htmlFile?.download_url || "";
}

function extractArtifactFiles(text = "") {
  const source = decodeHtmlEntities(String(text || ""));
  if (!source.includes("<kemy_artifact")) return [];
  const files = [];
  const pattern = /<file\b[^>]*\bpath\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)<\/file\s*>/gi;
  let match;
  while ((match = pattern.exec(source))) {
    files.push({ path: match[1].trim(), content: decodeHtmlEntities(match[2].trim()) });
  }
  return files;
}

function decodeHtmlEntities(value = "") {
  const textarea = document.createElement("textarea");
  textarea.innerHTML = value;
  return textarea.value;
}

function normalizePreviewHtml(value = "") {
  let html = decodeHtmlEntities(String(value || "").trim());
  if (html.startsWith("```")) {
    html = html.replace(/^```[a-zA-Z]*\s*/, "").replace(/\s*```$/, "").trim();
  }
  if (html.toLowerCase().startsWith("html")) {
    html = html.slice(4).trim();
  }
  const lower = html.toLowerCase();
  if (!lower.includes("<html") && !lower.includes("<!doctype html")) return "";
  return html;
}

function looksLikeViteShellHtml(html = "") {
  const lower = String(html || "").toLowerCase();
  const hasEmptyRoot = /<div[^>]+id=["'](?:root|app)["'][^>]*>\s*<\/div>/.test(lower);
  const hasModuleSrc = /<script[^>]+type=["']module["'][^>]+src=["'][^"']*(?:\/src\/|main\.(?:tsx|jsx|ts|js))/.test(lower);
  return hasEmptyRoot && hasModuleSrc;
}

function buildEmergencyPreviewHtml(result, fallbackText = "") {
  const title = escapeHtml(result?.artifact_title || result?.document_title || "Preview do projeto");
  const summary = escapeHtml(result?.summary || "A Kemy detectou um HTML de bootstrap e criou este preview funcional.");
  const request = escapeHtml(String(fallbackText || result?.raw || "").slice(0, 220).replace(/\s+/g, " "));
  return `<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>${title}</title>
  <style>
    body{margin:0;font-family:Segoe UI,system-ui,sans-serif;background:linear-gradient(135deg,#eef8f4,#dff0ea);color:#0f2b22;display:grid;place-items:center;min-height:100vh;padding:28px}
    .card{width:min(960px,100%);background:#ffffffd9;border:1px solid #0f2b2220;border-radius:20px;padding:28px;box-shadow:0 20px 40px #0f2b2220}
    h1{margin:0 0 10px;font-size:34px;letter-spacing:-.03em}
    p{margin:8px 0 0;font-size:18px;line-height:1.5;color:#345247}
    code{display:block;margin-top:14px;background:#0f2b220d;padding:10px 12px;border-radius:10px;color:#1d3d32;font-size:13px;white-space:pre-wrap}
  </style>
</head>
<body>
  <article class="card">
    <h1>${title}</h1>
    <p>${summary}</p>
    <p>Esse preview de contingência evita tela em branco quando o arquivo recebido é só bootstrap de framework.</p>
    ${request ? `<code>${request}</code>` : ""}
  </article>
</body>
</html>`;
}

async function handleFileSelection(fileList) {
  const files = Array.from(fileList || []).slice(0, 6);
  const parsed = [];
  for (const file of files) {
    const attachment = await readAttachment(file);
    if (attachment) parsed.push(attachment);
  }
  state.attachments = parsed;
  renderAttachments();
}

function readAttachment(file) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => {
      const raw = typeof reader.result === "string" ? reader.result : "";
      const mimeType = file.type || inferMimeType(file.name);
      const isImage = mimeType.startsWith("image/");
      const isPdf = mimeType === "application/pdf";
      const isPptx = mimeType === "application/vnd.openxmlformats-officedocument.presentationml.presentation";
      const isDocx = mimeType === "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        || mimeType === "application/msword";
      const isTextLike = mimeType.startsWith("text/")
        || /(\.md|\.txt|\.py|\.js|\.ts|\.tsx|\.jsx|\.json|\.html|\.css|\.csv|\.log|\.yml|\.yaml)$/i.test(file.name);
      const content = (isImage || isPdf || isDocx || isPptx)
        ? raw
        : raw.slice(0, 120000);
      resolve({
        name: file.name,
        mime_type: mimeType,
        content,
        kind: isImage ? "image" : (isPdf || isDocx || isPptx ? "document" : (isTextLike ? "text" : "binary")),
      });
    };
    reader.onerror = () => resolve(null);
    const mimeType = file.type || inferMimeType(file.name);
    if (
      mimeType.startsWith("image/") ||
      mimeType === "application/pdf" ||
      mimeType === "application/vnd.openxmlformats-officedocument.presentationml.presentation" ||
      mimeType === "application/vnd.openxmlformats-officedocument.wordprocessingml.document" ||
      mimeType === "application/msword"
    ) {
      reader.readAsDataURL(file);
      return;
    }
    reader.readAsText(file);
  });
}

function inferMimeType(filename) {
  const lower = String(filename || "").toLowerCase();
  if (lower.endsWith(".pdf")) return "application/pdf";
  if (lower.endsWith(".pptx")) return "application/vnd.openxmlformats-officedocument.presentationml.presentation";
  if (lower.endsWith(".docx")) return "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
  if (lower.endsWith(".doc")) return "application/msword";
  return "text/plain";
}

function renderAttachments() {
  const targets = [$("homeAttachments"), $("chatAttachments")];
  for (const target of targets) {
    if (!state.attachments.length) {
      target.innerHTML = "";
      target.classList.add("hidden");
      continue;
    }
    target.classList.remove("hidden");
    target.innerHTML = state.attachments
      .map((item, index) => `
        <span class="attachment-chip">
          <span>${escapeHtml(item.name)}</span>
          <button type="button" data-remove-attachment="${index}" aria-label="Remover anexo">&times;</button>
        </span>
      `)
      .join("");
  }
  document.querySelectorAll("[data-remove-attachment]").forEach((button) => {
    button.addEventListener("click", () => {
      state.attachments.splice(Number(button.dataset.removeAttachment), 1);
      renderAttachments();
    });
  });
}

function clearAttachments() {
  state.attachments = [];
  $("homeFileInput").value = "";
  $("chatFileInput").value = "";
  renderAttachments();
}

on("authForm", "submit", submitAuth);
on("authModeBtn", "click", () => setAuthMode(state.authMode === "login" ? "register" : "login"));
on("themeToggle", "click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
on("logoutBtn", "click", () => logout().catch(console.error));
on("newSessionBtn", "click", () => newSession({ openChat: true }).catch(console.error));
on("backHomeBtn", "click", showHome);
on("refreshSessionsBtn", "click", () => loadSessions().catch(console.error));
on("copyBtn", "click", () => navigator.clipboard.writeText(state.lastOutput || ""));
on("systemToggle", "click", async () => {
  await loadStatus().catch(console.error);
  await loadSessionInsights();
  const dialog = $("systemDialog");
  if (dialog && !dialog.open) dialog.showModal();
});
on("systemClose", "click", () => $("systemDialog")?.close());
on("chatForm", "submit", (event) => {
  event.preventDefault();
  runAgents($("prompt").value, "chat").catch(showRunError);
});
on("homeRunBtn", "click", () => runAgents($("homePrompt").value, "home").catch(showRunError));
on("openSessionsBtn", "click", async () => {
  await loadSessions();
  if (state.sessions[0]?.session_id) {
    await openSession(state.sessions[0].session_id).catch(showRunError);
    showChat();
    return;
  }
  appendMessage("assistant", "Nenhuma conversa encontrada no Storage. Digite algo para inicializar a memória.");
  showChat();
});
on("plusBtn", "click", () => $("chatFileInput")?.click());
on("homeAttachBtn", "click", () => $("homeFileInput")?.click());
on("voiceBtn", "click", () => appendMessage("assistant", "Módulo de voz será ativado no próximo update. A infraestrutura de STT/TTS precisa ser conectada ao WebSocket primeiro."));
on("closePreviewBtn", "click", hidePreview);
on("previewFullscreenBtn", "click", togglePreviewFullscreen);
on("previewEditForm", "submit", (event) => {
  event.preventDefault();
  applyPreviewEdit().catch(showRunError);
});
on("homeFileInput", "change", (event) => handleFileSelection(event.target.files));
on("chatFileInput", "change", (event) => handleFileSelection(event.target.files));

document.querySelectorAll("[data-home-prompt]").forEach((button) => {
  button.addEventListener("click", () => setPromptAndMaybeRun(button.dataset.homePrompt, false));
});

on("homePrompt", "keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    runAgents($("homePrompt").value, "home").catch(showRunError);
  }
});

on("prompt", "keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    runAgents($("prompt").value, "chat").catch(showRunError);
  }
});

on("previewPrompt", "keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    applyPreviewEdit().catch(showRunError);
  }
});

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.previewFullscreen) {
    setPreviewFullscreen(false);
  }
});

setTheme(localStorage.getItem("kemy.theme") || "light");
setAuthMode("login");
updatePreviewFullscreenUI();
checkAuth().catch(() => {
  $("loginView").classList.remove("hidden");
  $("appView").classList.add("hidden");
});

document.addEventListener("paste", (event) => {
  if (state.authMode !== "login" && !state.currentUser) return;
  const items = event.clipboardData?.items;
  if (!items) return;
  const files = [];
  for (let i = 0; i < items.length; i++) {
    if (items[i].kind === "file" || items[i].type.startsWith("image/")) {
      const file = items[i].getAsFile();
      if (file) files.push(file);
    }
  }
  if (files.length) {
    handleFileSelection(files);
  }
});

document.addEventListener("dragover", (event) => {
  event.preventDefault();
  document.body.classList.add("drag-active");
});

document.addEventListener("dragleave", (event) => {
  if (event.relatedTarget === null) {
    document.body.classList.remove("drag-active");
  }
});

document.addEventListener("drop", (event) => {
  event.preventDefault();
  document.body.classList.remove("drag-active");
  if (state.authMode !== "login" && !state.currentUser) return;
  const files = event.dataTransfer?.files;
  if (files && files.length) {
    handleFileSelection(files);
  }
});

document.body.addEventListener("click", (event) => {
  if (event.target.classList.contains("copy-code-btn")) {
    const codeNode = event.target.nextElementSibling;
    const codeText = codeNode?.textContent;
    if (codeText) {
      navigator.clipboard.writeText(codeText);
      const originalText = event.target.textContent;
      event.target.textContent = "Copiado!";
      setTimeout(() => {
        if (event.target) event.target.textContent = originalText;
      }, 2000);
    }
  }
});


// ── GitHub Integration ────────────────────────────────────────────────────────
async function loadGithubStatus() {
  try {
    const resp = await fetch("/api/github/me", { credentials: "include" });
    if (!resp.ok) return;
    const data = await resp.json();
    const btn = document.getElementById("githubConnectBtn");
    const label = document.getElementById("githubBtnLabel");
    if (!btn || !label) return;

    if (data.connected) {
      btn.classList.add("connected");
      btn.href = "#";
      btn.title = `Conectado como @${data.github_login}. Clique para desconectar.`;
      if (data.github_avatar) {
        label.innerHTML = `<img src="${data.github_avatar}" class="gh-avatar" alt="@${data.github_login}"> @${data.github_login}`;
      } else {
        label.textContent = `@${data.github_login}`;
      }
      btn.addEventListener("click", async (e) => {
        e.preventDefault();
        if (!confirm(`Desconectar GitHub (@${data.github_login})?`)) return;
        await fetch("/api/github/disconnect", { method: "DELETE", credentials: "include" });
        location.reload();
      }, { once: true });
    } else if (!data.oauth_available) {
      btn.style.display = "none"; // Hide if OAuth not configured on server
    }
  } catch (e) {
    // Silently ignore
  }
}

function renderGitTerminal(operation, output, success) {
  const wrapper = document.createElement("div");
  wrapper.className = "git-terminal";
  const color = success ? "#3fb950" : "#ff7b72";
  const icon = success ? "✅" : "❌";
  wrapper.innerHTML = `
    <div class="git-terminal-header">
      <div class="terminal-dots">
        <span class="dot dot-red"></span>
        <span class="dot dot-yellow"></span>
        <span class="dot dot-green"></span>
      </div>
      <span class="terminal-title">git ${operation} — ${icon} ${success ? "sucesso" : "erro"}</span>
    </div>
    <div class="git-terminal-body" style="border-top: 1px solid rgba(255,255,255,0.05);">${
      output.replace(/</g, "&lt;").replace(/>/g, "&gt;")
             .replace(/✅[^\n]*/g, m => `<span class="git-success">${m}</span>`)
             .replace(/❌[^\n]*/g, m => `<span class="git-error">${m}</span>`)
             .replace(/⚠️[^\n]*/g, m => `<span class="git-info">${m}</span>`)
    }</div>
  `;
  return wrapper;
}

// Git terminal rendered directly in renderJob (above) — no monkey-patch needed

// Check GitHub status on load
loadGithubStatus();

// Check if redirected back from GitHub OAuth
const urlParams = new URLSearchParams(window.location.search);
if (urlParams.get("github") === "connected") {
  const user = urlParams.get("user");
  history.replaceState({}, "", window.location.pathname);
  loadGithubStatus();
  setTimeout(() => {
    const msg = document.createElement("div");
    msg.style.cssText = "position:fixed;top:22px;right:80px;z-index:9999;padding:12px 20px;background:rgba(63,185,80,0.15);border:1px solid rgba(63,185,80,0.4);border-radius:12px;color:#3fb950;font-weight:700;font-size:14px;backdrop-filter:blur(12px);";
    msg.textContent = `✅ GitHub @${user} conectado com sucesso!`;
    document.body.appendChild(msg);
    setTimeout(() => msg.remove(), 4000);
  }, 500);
}

// --- GitHub Workspace Integration ---
async function loadGitHubRepos() {
  const select = $("gitRepo");
  if (!select) return;
  try {
    const res = await api("/api/github/repos");
    if (res && res.repos && res.repos.length > 0) {
      select.classList.remove("hidden");
      let options = '<option value="">Selecione um Workspace</option>';
      res.repos.forEach(repo => {
        options += `<option value="${repo.url}">${repo.name}</option>`;
      });
      select.innerHTML = options;
      
      // Auto-select first repo if available
      if(res.repos.length === 1) select.selectedIndex = 1;
    } else {
      select.classList.add("hidden");
    }
  } catch (err) {
    console.error("Erro ao carregar repos:", err);
  }
}

// Load repos when github connected
window.addEventListener("message", (e) => {
  if (e.data === "github_connected") {
    $("githubBtnLabel").textContent = "GitHub Conectado";
    loadGitHubRepos();
    loadGithubStatus();
  }
});

// Also try loading on init just in case they are already connected
setTimeout(loadGitHubRepos, 2000);
