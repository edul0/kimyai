"""Compatibility entrypoint for cloud hosts.

The production app now lives in app.main. Keeping this file preserves existing
Docker/Render/Railway commands that import agencia_kemy:app.
"""

from app.main import app


if __name__ == "__main__":
    import os

    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("agencia_kemy:app", host="0.0.0.0", port=port, reload=False)
