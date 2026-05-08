const fs = require('fs');
let code = fs.readFileSync('app/main.py', 'utf8');

// 1. Add github_repo to jobs.create
code = code.replace(
    'attachments=[item.model_dump() for item in cmd.anexos],',
    'attachments=[item.model_dump() for item in cmd.anexos],\n        github_repo=cmd.github_repo,'
);

// 2. Add /api/github/repos route at the end
code += '\n\n@app.get("/api/github/repos")\nasync def github_repos(request: Request):\n    cookie = request.cookies.get(COOKIE_NAME)\n    user = verify_token(cookie, settings) if cookie else None\n    if not user:\n        return JSONResponse({"repos": []})\n    \n    config = storage.get_json(f"github_config:{user}") or {}\n    token = config.get("token")\n    if not token:\n        return JSONResponse({"repos": []})\n    \n    async with httpx.AsyncClient() as client:\n        res = await client.get("https://api.github.com/user/repos?sort=updated&per_page=100", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})\n        if res.status_code != 200:\n            return JSONResponse({"repos": []})\n        data = res.json()\n        repos = [{"name": r["full_name"], "url": r["clone_url"], "default_branch": r["default_branch"]} for r in data]\n        return JSONResponse({"repos": repos})\n';

fs.writeFileSync('app/main.py', code);
console.log('main.py updated successfully');
