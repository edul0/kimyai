# Kemy Local Client (Corpo/Sensores)

Este modulo roda **somente no PC local** do usuario e conversa com o Cerebro da Kemy via API.

## Objetivo
- Capturar tela sob demanda (sem stream continuo).
- Enviar screenshot para o endpoint `/api/vision/analyze`.
- Receber resposta textual da Kemy.

## Instalar
```bash
pip install -r local_client/requirements.txt
```

## Execucao unica (captura uma vez)
```bash
python local_client/kemy_vision_hotkey.py \
  --base-url https://kimyai.onrender.com \
  --user SEU_USUARIO \
  --password SUA_SENHA \
  --prompt "Olhe essa tela e me diga o proximo passo"
```

## Modo hotkey (F12 dispara captura)
```bash
python local_client/kemy_vision_hotkey.py \
  --base-url https://kimyai.onrender.com \
  --user SEU_USUARIO \
  --password SUA_SENHA \
  --loop \
  --hotkey f12 \
  --context "Estou jogando e quero ajuda tatica objetiva"
```

Pressione `ESC` para sair do modo loop.

## Notas
- Esse cliente local nao toma decisao de negocio; ele so captura e envia.
- O backend cloud nao usa `mss` nem acessa hardware local.
