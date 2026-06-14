# Kemy Desktop Local (.exe) — Assistente de Voz

Este modulo cria um executavel Windows que transforma a Kemy num assistente de
voz local: ela **ouve** voce, **fala** de volta, mostra um **avatar animado** que
reage ao que esta acontecendo e **executa** o pedido usando os agentes locais —
tudo rodando no seu PC, sem Render.

## O que o app faz
- Sobe o backend FastAPI local (`127.0.0.1:8000`) automaticamente ao abrir.
- Faz login local e cria uma sessao sozinho.
- Avatar reativo com 4 estados: **Pronta**, **Ouvindo**, **Pensando**, **Falando**.
- Botao **Falar com a Kemy** (push-to-talk) e **Modo conversa** (escuta continua).
- Campo de texto como alternativa/complemento da voz.
- Le a resposta em voz alta e mostra a transcricao em tempo real.

## Dependencias de voz (opcionais)
A voz e opcional. Sem ela, o app funciona por texto e avisa o que instalar.

```bash
pip install -r local_app/requirements.txt
```

- `pyttsx3` — sintese de voz offline (usa a voz do Windows; escolhe pt-BR se houver).
- `SpeechRecognition` + `PyAudio` — captura e reconhecimento do microfone.

## Credenciais
O login local usa as variaveis de ambiente (ou os padroes):

- `KEMY_AUTH_USER` (padrao: `admin`)
- `KEMY_AUTH_PASSWORD` (padrao: `troque-esta-senha`)

Configure-as no `.env` da maquina para casar com o backend.

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
- O `.exe` e o cliente de visao local (`local_client`) sao modulos locais
  independentes do backend cloud.
- Para usar LLMs reais no local, configure suas chaves no `.env` da maquina
  (caso contrario o backend roda em modo mock).
