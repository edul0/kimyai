const fs = require('fs');
let code = fs.readFileSync('static/app.js', 'utf8');

const additionalCode = `
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
        options += \`<option value="\${repo.url}">\${repo.name}</option>\`;
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
  }
});

// Also try loading on init just in case they are already connected
setTimeout(loadGitHubRepos, 2000);
`;

code += additionalCode;
fs.writeFileSync('static/app.js', code);
console.log('app.js updated successfully');
