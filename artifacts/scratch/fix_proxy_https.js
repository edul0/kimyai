const fs = require('fs');
let code = fs.readFileSync('app/main.py', 'utf8');

const httpsPatch = `
def get_secure_base_url(request: Request) -> str:
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    return f"{scheme}://{host}".rstrip("/")
`;

if (!code.includes('def get_secure_base_url')) {
    // Insert helper function after imports
    code = code.replace(
        'from .schemas import ComandoRequest, JobCreateResponse, JobState, NovoAgente',
        'from .schemas import ComandoRequest, JobCreateResponse, JobState, NovoAgente\n' + httpsPatch
    );

    // Replace base assignments
    code = code.replace(
        'base = str(request.base_url).rstrip("/")\n    redirect_uri = f"{base}/api/github/callback"',
        'base = get_secure_base_url(request)\n    redirect_uri = f"{base}/api/github/callback"'
    );
    
    code = code.replace(
        'base = str(request.base_url).rstrip("/")\n    payload = {',
        'base = get_secure_base_url(request)\n    payload = {'
    );

    fs.writeFileSync('app/main.py', code);
    console.log('Applied HTTPS proxy fix');
} else {
    console.log('Already applied');
}
