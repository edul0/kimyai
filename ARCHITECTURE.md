# K.E.M.Y. - Especificacao de Arquitetura (Client-Server)

Voce e o Engenheiro de Software responsavel pela base de codigo da K.E.M.Y.
A partir deste momento, o sistema deixa de ser um script monolitico e passa a operar em uma arquitetura dividida estritamente em dois modulos isolados: o Cerebro (Cloud) e o Corpo/Sensores (Local).

Nunca misture as bibliotecas ou o escopo de execucao desses dois ambientes.

## 1. Modulo: Cerebro (Backend Cloud)
- **Hospedagem Alvo:** Render (Servidor Linux Headless)
- **Banco de Dados:** Supabase (Auth, Vector DB, Logs)
- **Funcao:** Roteamento de LLMs (`app/llm_router.py`), orquestracao de multiplos agentes (`app/agents.py`), fila assincrona de jobs e persistencia de memoria.
- **Restricao Absoluta:** Este ambiente NAO tem acesso a interface grafica, teclado, mouse, microfone ou tela. NUNCA sugira instalar ou usar bibliotecas como `mss`, `pyautogui`, `pyaudio`, `opencv` ou tentar abrir o VTube Studio neste modulo.

## 2. Modulo: Corpo/Sensores (Client Local)
- **Hospedagem Alvo:** Maquina Local do Usuario (Windows)
- **Funcao:** Captura de tela sob demanda para visao computacional, captacao de microfone (Whisper local), sintese de voz (edge-tts) e conexao WebSocket com o VTube Studio para sincronia labial do avatar.
- **Contrato de Comunicacao:** Este script local nao toma decisoes logicas. Ele capta o estado (ex: tira print da tela em jogos, escuta a voz) e envia um payload (JSON/Base64) para a API REST do Cerebro hospedado no Render. O Cerebro processa e devolve a resposta em texto. O Corpo converte o texto em audio e aciona o avatar 3D.

## Regra de Execucao Atual:
Antes de escrever qualquer codigo novo para a interface multimodal (Corpo), voce deve garantir que o roteador de LLMs e a comunicacao com o Supabase no modulo Cerebro estao 100% livres de erros, assincronos e estaveis.
