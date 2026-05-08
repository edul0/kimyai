const fs = require('fs');
let code = fs.readFileSync('app/main.py', 'utf8');

// Replace redirect_uri generation in github_connect
code = code.replace(
    'redirect_uri = f"{settings.app_public_url}/api/github/callback"',
    'base = str(request.base_url).rstrip("/")\n    redirect_uri = f"{base}/api/github/callback"'
);

// Replace redirect_uri in github_callback
code = code.replace(
    '"redirect_uri": f"{settings.app_public_url}/api/github/callback"',
    'base = str(request.base_url).rstrip("/")\n        "redirect_uri": f"{base}/api/github/callback"'
);

// Wait, the second one might break indentation. Let's do it safely.
code = code.replace(
    '"redirect_uri": f"{settings.app_public_url}/api/github/callback"',
    '"redirect_uri": f"{str(request.base_url).rstrip(\'/\')}/api/github/callback"'
);

fs.writeFileSync('app/main.py', code);
console.log('Fixed base_url in main.py');
