const fs = require('fs');
let code = fs.readFileSync('app/main.py', 'utf8');

if (code.includes('return JSONResponse({"url": url})')) {
    code = code.replace('return JSONResponse({"url": url})', 'return RedirectResponse(url)');
    fs.writeFileSync('app/main.py', code);
    console.log('Fixed main.py connect endpoint');
} else {
    console.log('Target not found');
}
