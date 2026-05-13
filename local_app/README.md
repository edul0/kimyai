# Kemy Desktop Local (.exe)

Este modulo cria um executavel Windows para rodar a Kemy localmente, sem Render.

## O que o launcher faz
- Sobe o backend FastAPI local (`127.0.0.1:8000`).
- Abre automaticamente a interface web da Kemy.
- Permite iniciar/parar o servidor por botao.

## Build do .exe
No PowerShell, na raiz do projeto:

```powershell
.\local_app\build_exe.ps1
```

Saida esperada:

```text
dist\KemyDesktop\KemyDesktop.exe
```

## Uso direto sem build
```bash
python local_app/kemy_desktop.py
```

## Observacoes
- O `.exe` e o cliente de visao local (`local_client`) sao modulos locais independentes do backend cloud.
- Para usar LLMs no local, configure suas chaves no `.env` da maquina.
