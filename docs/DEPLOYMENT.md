# Deployment

## Local run

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp data/inventory.sample.json data/inventory.json
python testbot.py
```

## Docker Compose

```bash
cp .env.example .env
cp data/inventory.sample.json data/inventory.json
docker compose up -d --build
```

## Public HTTPS

The bot needs a public HTTPS URL for payment callbacks.

Recommended options:

- Cloudflare Tunnel
- Nginx reverse proxy on a VPS
- Caddy reverse proxy
- ngrok for temporary development testing

Set:

```env
BASE_URL=https://bot.yourdomain.com
PORT=8080
```

Configure payment callback URLs:

```text
https://bot.yourdomain.com/stripe-webhook
https://bot.yourdomain.com/paypal-success
https://bot.yourdomain.com/coingate-webhook
```

## Windows startup

Use `scripts/windows_start.ps1` as a starting point for Task Scheduler.
Run PowerShell as Administrator and adjust paths first.
