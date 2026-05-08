const fs = require('fs');
let code = fs.readFileSync('app/main.py', 'utf8');

// The syntax error is:
//         "code": code,
//         base = str(request.base_url).rstrip("/")
//         "redirect_uri": f"{base}/api/github/callback"

const searchStr = `"code": code,
        base = str(request.base_url).rstrip("/")
        "redirect_uri": f"{base}/api/github/callback"`;

const replaceStr = `"code": code,
        "redirect_uri": f"{str(request.base_url).rstrip('/')}/api/github/callback"`;

if (code.includes(searchStr)) {
    code = code.replace(searchStr, replaceStr);
    fs.writeFileSync('app/main.py', code);
    console.log('Fixed syntax error!');
} else {
    console.log('Target string not found');
}
