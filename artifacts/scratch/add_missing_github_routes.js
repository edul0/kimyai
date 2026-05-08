const fs = require('fs');
let code = fs.readFileSync('app/main.py', 'utf8');

const missingRoutes = `
@app.get("/api/github/me")
async def github_me(request: Request):
    cookie = request.cookies.get(COOKIE_NAME)
    user = verify_token(cookie, settings) if cookie else None
    if not user:
        return JSONResponse({"connected": False})
    
    config = storage.get_json(f"github_config:{user}") or {}
    token = config.get("token")
    if not token:
        return JSONResponse({"connected": False})
        
    async with httpx.AsyncClient() as client:
        res = await client.get("https://api.github.com/user", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        if res.status_code != 200:
            return JSONResponse({"connected": False})
        data = res.json()
        return JSONResponse({
            "connected": True,
            "github_login": data.get("login"),
            "github_avatar": data.get("avatar_url")
        })

@app.post("/api/github/disconnect")
async def github_disconnect(request: Request):
    cookie = request.cookies.get(COOKIE_NAME)
    user = verify_token(cookie, settings) if cookie else None
    if user:
        storage.delete(f"github_config:{user}")
    return JSONResponse({"status": "disconnected"})
`;

// Insert the missing routes right before @app.get("/api/github/repos")
if (!code.includes('/api/github/me')) {
    code = code.replace(
        '@app.get("/api/github/repos")',
        missingRoutes + '\n\n@app.get("/api/github/repos")'
    );
    fs.writeFileSync('app/main.py', code);
    console.log('Added missing endpoints');
} else {
    console.log('Endpoints already exist');
}
