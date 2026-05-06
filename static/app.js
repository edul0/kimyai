const state = {
  sessionId: null,
  poll: null,
  lastOutput: "",
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
}

function showPreviewUrl(url) {
  if (!url) return;
  $("previewFrame").removeAttribute("srcdoc");
  $("previewFrame").src = url;
  $("previewPanel").classList.remove("hidden");
  $("chatView").classList.add("has-preview");
}

function hidePreview() {
  $("previewFrame").srcdoc = "";
  $("previewPanel").classList.add("hidden");
  $("chatView").classList.remove("has-preview");
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
      <div class="saved-session ${session.session_id === state.sessionId ? "active" : ""}" style="display: flex; justify-content: space-between; align-items: center;">
        <div class="saved-session-main" data-session-id="${session.session_id}" style="overflow: hidden; flex: 1;">
          <strong style="display: block; font-size: 13px; white-space: nowrap; text-overflow: ellipsis; overflow: hidden;">${escapeHtml(session.title || "Nova conversa")}</strong>
        </div>
        <button class="saved-session-delete" data-delete-session="${session.session_id}" aria-label="Excluir" style="background: none; border: none; color: var(--danger); cursor: pointer; padding: 4px; opacity: 0.7;">×</button>
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
  const lastAssistant = [...history].reverse().find((item) => item.role === "assistant" && item.content);
  const previewHtml = lastAssistant ? extractPreviewHtml(lastAssistant.result || lastAssistant, lastAssistant.content) : "";
  const previewUrl = lastAssistant ? extractPreviewUrl(lastAssistant.result || lastAssistant) : "";
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
    appendResult(job.resultado, state.lastOutput);
    state.renderedJobs.add(job.job_id);
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
  if (result.raw) return result.raw;
  if (result.summary && result.files?.length) {
    return `${result.summary}\n\n${result.files.map((file) => `### ${file.path}\n\n\`\`\`\n${file.content}\n\`\`\``).join("\n\n")}`;
  }
  if (result.files?.length) {
    return result.files.map((file) => `### ${file.path}\n\n\`\`\`\n${file.content}\n\`\`\``).join("\n\n---\n\n");
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
  const documentMarkers = [".docx", ".pdf", ".md", "markdown", "documento", "gerar pdf"];
  if (documentMarkers.some((marker) => lowered.includes(marker))) return "documento";
  return selectedMode;
}

async function runAgents(prompt, source = "chat") {
  const text = (prompt || "").trim();
  if (!text) return;
  await ensureSession({ forceNewSession: source === "home" });
  showChat();
  $("runBtn").disabled = true;
  $("homeRunBtn").disabled = true;
  appendMessage("user", text);
  const attachments = [...state.attachments];
  $("prompt").value = "";
  $("homePrompt").value = "";
  clearAttachments();
  $("sessionTitle").textContent = titleFromPrompt(text);
  $("jobBadge").textContent = "Processando...";
  $("timeline").innerHTML = `<li><p>Processando arquitetura da resposta e acionando agentes...</p></li>`;

  let data;
  const requestedMode = resolveRequestedMode(text);
  try {
    data = await api("/api/comando", {
      method: "POST",
      body: JSON.stringify({ mensagem: text, session_id: state.sessionId, modo: requestedMode, anexos: attachments }),
    });
  } catch (error) {
    clearPersistedSessionId();
    await ensureSession();
    data = await api("/api/comando", {
      method: "POST",
      body: JSON.stringify({ mensagem: text, session_id: state.sessionId, modo: requestedMode, anexos: attachments }),
    });
  }
  persistSessionId(data.session_id);
  const done = await pollJob(data.job_id);
  clearInterval(state.poll);
  if (!done) state.poll = setInterval(() => pollJob(data.job_id).catch(console.error), 900);
  if (source === "home") await loadSessions();
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
    block = block.replace(/```(\w*)\n([\s\S]*?)```/, '<pre style="background: var(--surface-2); padding: 16px; border-radius: var(--radius-md); overflow-x: auto; border: 1px solid var(--line-strong); margin: 16px 0;"><code style="font-family: \'JetBrains Mono\', monospace; font-size: 14px;">$2</code></pre>');
    // Fallback caso o LLM não envie o tipo de linguagem
    block = block.replace(/```([\s\S]*?)```/, '<pre style="background: var(--surface-2); padding: 16px; border-radius: var(--radius-md); overflow-x: auto; border: 1px solid var(--line-strong); margin: 16px 0;"><code style="font-family: \'JetBrains Mono\', monospace; font-size: 14px;">$1</code></pre>');
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
  const imageSource = extractImageSource(result) || extractImageSource({ raw: fallbackText, summary: fallbackText });
  if (!imageSource) {
    const downloadableFiles = prioritizeFiles((result.files || []).filter((file) => file.download_url));
    if (downloadableFiles.length) {
      const node = document.createElement("div");
      node.className = "message assistant";
      node.innerHTML = `
        <div class="result-card">
          <div class="result-card-head">
            <strong>${escapeHtml(result.document_title || result.summary || "Arquivos gerados")}</strong>
            <span class="result-meta">${escapeHtml((result.provider || "kemy") + " - " + (result.model || ""))}</span>
          </div>
          <div class="file-actions">
            ${downloadableFiles.map((file) => `<a href="${escapeHtml(file.download_url)}" target="_blank" rel="noreferrer" class="secondary-btn">Download ${escapeHtml(file.name)}</a>`).join("")}
          </div>
          ${fallbackText ? parseMarkdown(fallbackText) : ""}
        </div>
      `;
      $("chatLog").appendChild(node);
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
  if (mime === "text/markdown" || name.endsWith(".md")) return 3;
  if (mime === "text/html" || name.endsWith(".html")) return 4;
  return 10;
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
  if (result?.preview_html) return result.preview_html;
  const files = result?.files || [];
  const htmlFile = files.find((file) => String(file.path || "").toLowerCase().endsWith(".html"));
  if (htmlFile?.content) return htmlFile.content;
  const match = fallbackText.match(/```html\s*([\s\S]*?)```/i);
  return match ? match[1].trim() : "";
}

function extractPreviewUrl(result) {
  if (result?.preview_url) return result.preview_url;
  const files = result?.files || [];
  const htmlFile = files.find((file) => (String(file.mime_type || "").toLowerCase() === "text/html" || String(file.name || "").toLowerCase().endsWith(".html")) && file.download_url);
  return htmlFile?.download_url || "";
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
      const isDocx = mimeType === "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        || mimeType === "application/msword";
      const isTextLike = mimeType.startsWith("text/")
        || /(\.md|\.txt|\.py|\.js|\.ts|\.tsx|\.jsx|\.json|\.html|\.css|\.csv|\.log|\.yml|\.yaml)$/i.test(file.name);
      const content = (isImage || isPdf || isDocx)
        ? raw
        : raw.slice(0, 120000);
      resolve({
        name: file.name,
        mime_type: mimeType,
        content,
        kind: isImage ? "image" : (isPdf || isDocx ? "document" : (isTextLike ? "text" : "binary")),
      });
    };
    reader.onerror = () => resolve(null);
    const mimeType = file.type || inferMimeType(file.name);
    if (mimeType.startsWith("image/") || mimeType === "application/pdf" || mimeType === "application/vnd.openxmlformats-officedocument.wordprocessingml.document" || mimeType === "application/msword") {
      reader.readAsDataURL(file);
      return;
    }
    reader.readAsText(file);
  });
}

function inferMimeType(filename) {
  const lower = String(filename || "").toLowerCase();
  if (lower.endsWith(".pdf")) return "application/pdf";
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
    return;
  }
  appendMessage("assistant", "Nenhuma conversa encontrada no Storage. Digite algo para inicializar a memória.");
  showChat();
});
on("plusBtn", "click", () => $("chatFileInput")?.click());
on("homeAttachBtn", "click", () => $("homeFileInput")?.click());
on("voiceBtn", "click", () => appendMessage("assistant", "Módulo de voz será ativado no próximo update. A infraestrutura de STT/TTS precisa ser conectada ao WebSocket primeiro."));
on("closePreviewBtn", "click", hidePreview);
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

setTheme(localStorage.getItem("kemy.theme") || "light");
setAuthMode("login");
checkAuth().catch(() => {
  $("loginView").classList.remove("hidden");
  $("appView").classList.add("hidden");
});
