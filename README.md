# MercadoLivreBot — projeto Meli

Bot privado do Telegram para receber links do Mercado Livre, abrir o anúncio no Google Chrome com Playwright e organizar uma fila local de revisão em SQLite.

**A divulgação só é enviada aos destinos configurados depois que você toca em Aprovar no Telegram.** Rejeitar não envia nada. Telegram e WhatsApp são tentados separadamente; uma falha em um canal não cancela o outro.

## Requisitos

- Python 3.10 ou superior
- Token de bot criado com o [@BotFather](https://t.me/BotFather)
- Google Chrome instalado
- Acesso à internet para abrir anúncios e receber atualizações do Telegram

## Instalação no Windows (PowerShell)

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Preencha `TELEGRAM_BOT_TOKEN` e `TELEGRAM_GROUP_CHAT_ID` no `.env`. Adicione o bot ao grupo do Telegram com permissão para enviar mensagens e envie `/idgrupo` no grupo para descobrir o ID. O WhatsApp é opcional e usa a WAHA em Docker. Defina `WAHA_API_KEY`, `WAHA_DASHBOARD_USERNAME` e `WAHA_DASHBOARD_PASSWORD` com valores fortes no `.env`, então inicie o serviço:

```powershell
docker compose -f compose.waha.yml up -d
```

Abra [http://127.0.0.1:3000/dashboard](http://127.0.0.1:3000/dashboard), conecte à API usando `WAHA_API_KEY`, inicie a sessão `default` e leia o QR Code com o número dedicado. No painel/Swagger da WAHA, consulte `GET /api/default/groups` e copie o `id` do grupo para `WHATSAPP_GROUP_ID`. A WAHA também pode localizar pelo nome se `WHATSAPP_GROUP_ID` ficar vazio e `WHATSAPP_GROUP_NAME` estiver configurado. A porta fica vinculada apenas a `127.0.0.1`; não publique a chave nem exponha a API à internet. O estado da sessão fica salvo em `data/waha`. Na primeira consulta, o Chrome abre uma janela com um perfil local dedicado para o Mercado Livre; faça login nessa janela se necessário. O perfil fica salvo em `data/chrome-profile`.

Depois de salvar o `.env`, inicie o programa:

```powershell
python main.py
```

No Linux/macOS, crie o ambiente virtual com `python3 -m venv .venv`, instale os mesmos pacotes, preencha o `.env` e execute `python main.py`.

## Uso

1. Abra o bot no Telegram e envie `/start`.
2. Envie um link HTTPS de produto do Mercado Livre.
3. Revise no Telegram o título, valor atual, desconto e link.
4. Toque em **Aprovar** para enviar a mensagem aos canais configurados, ou em **Rejeitar** para descartá-la.

Comandos: `/ajuda` e `/pendentes`. O bot aceita mensagens apenas em conversa privada e links `mercadolivre.com.br`, `mercadolivre.com` ou `meli.la`, incluindo URLs de afiliados. O Chrome permanece visível: se aparecer login ou verificação, conclua manualmente; o bot aguarda até dois minutos para ler o anúncio. A aba fica aberta para conferência. A fila e as decisões ficam em `data/meli.sqlite3`.

O WhatsApp é enviado pela API local da WAHA, que mantém a sessão vinculada ao seu número e oferece endpoints HTTP para grupos e mensagens. Como é uma integração não oficial, ela pode ser desconectada ou afetada por mudanças no WhatsApp; use um número dedicado e mantenha a sessão e a chave da API protegidas. O Telegram é enviado primeiro. Se um dos canais falhar, o bot informa qual foi e mantém **Aprovar** disponível para tentar novamente sem repetir o canal já enviado. A divulgação continua condicionada à aprovação manual no Telegram. O painel local em [http://127.0.0.1:8765](http://127.0.0.1:8765) serve somente para consulta.

O painel escuta somente em `127.0.0.1`. Para encerrar bot e painel, pressione `Ctrl+C` no terminal que iniciou o programa.

## Estrutura

- `main.py`: bot Telegram, automação visível do Chrome com Playwright, banco SQLite e painel local.
- `compose.waha.yml`: serviço WAHA opcional para integração HTTP com WhatsApp.
- `.env.example`: configuração de ambiente de exemplo.
- `data/`: banco criado na primeira execução (ignorado pelo Git).
