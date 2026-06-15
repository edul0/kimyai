# Kemy Desktop Local (.exe) — Assistente de Voz (VTuber)

Executavel Windows que transforma a Kemy num assistente de voz local: ela
**ouve**, **fala** com a **boca sincronizada em tempo real**, tem um
**personagem animado** (estilo VTuber) e **executa** o pedido usando os agentes
locais — salvando os arquivos gerados direto numa pasta do seu PC.

## O que o app faz
- Sobe o backend FastAPI local (`127.0.0.1:8000`) automaticamente.
- Carrega o seu `.env` e usa **suas chaves de IA** (respostas reais, sem mock).
- Faz login local e cria a sessao sozinho.
- Avatar reativo: **Pronta / Ouvindo / Pensando / Falando** (pisca, balanca,
  fones brilham, boca anima por palavra falada).
- **Falar com a Kemy** (push-to-talk) e **Modo conversa** (escuta continua).
- **Acao local**: arquivos gerados sao salvos em `KEMY_LOCAL_WORKSPACE_ROOT`
  (ou `~/KemyWorkspace`). Botao **Abrir pasta** abre o resultado.
- Botao **Atualizar** abre a pagina da ultima versao publicada.

## Ativar a IA real (chaves)
A Kemy so responde de verdade com um provedor de IA configurado. Duas formas:

1. **Coloque um arquivo `.env`** ao lado do `KemyDesktop.exe`, ou
2. Clique em **⚙ Configurar IA (.env)** dentro do app e selecione seu `.env`.

O app procura o `.env` nesta ordem: ao lado do exe → `%APPDATA%\Kemy\.env` →
raiz do projeto. Chaves aceitas: `GEMINI_API_KEY`, `GROQ_API_KEY`,
`CEREBRAS_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`. Defina tambem
`LLM_MODE=providers`. Para escolher onde os arquivos sao salvos, use
`KEMY_LOCAL_WORKSPACE_ROOT=C:\caminho\da\pasta`.

> O `.env` nunca vai para o Git (esta no `.gitignore`).

## Dependencias de voz (opcionais)
```bash
pip install -r local_app/requirements.txt
```
- `pyttsx3` — voz offline (usa a voz do Windows; pt-BR se houver). O lip-sync
  usa o evento `started-word` do pyttsx3.
- `SpeechRecognition` + `PyAudio` — microfone.

## Build do .exe
No PowerShell, na raiz do projeto:
```powershell
.\local_app\build_exe.ps1
```
Saida: `dist\KemyDesktop\KemyDesktop.exe`.

Ou baixe direto a ultima build publicada em **Releases** (tag `desktop-latest`).

## Uso direto sem build
```bash
python local_app/kemy_desktop.py
```

## Observacoes
- Credenciais locais sao injetadas no backend automaticamente (login sempre
  bate). Para mudar, defina `KEMY_AUTH_USER`/`KEMY_AUTH_PASSWORD` no ambiente.
- O `.exe` e o cliente de visao (`local_client`) sao modulos locais.
