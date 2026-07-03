# GenRDP Proxy Test Shop Bot

A Telegram bot for selling and managing short-term mobile proxy tests for GenRDP.

This is a **public portfolio-safe version** of a production-style project. Real credentials, production databases, customer data, iProxy connection IDs, payment secrets and deployment files are intentionally excluded.

---

## Features

### Client side
- `/start` — language selection and main purchase flow
- Choose test connection type: IPv4 or IPv6
- Choose carrier/operator from the available inventory
- Choose duration: 24 hours or 7 days
- Pay via Stripe, PayPal or CoinGate
- Choose HTTP or SOCKS after payment
- Optional OpenVPN access/configuration
- `/myproxies` — view active test proxies
- Change HTTP/SOCKS while the proxy is active
- Change operator/IP while active, paying only the price difference when needed
- Request transfer/upgrade to the renewal bot before expiry

### Admin side
- `/admin` — inline admin panel
- `/inventory` — show available inventory
- `/reload_inventory` — reload inventory from JSON or CSV
- `/markpaid <order_id>` — manually mark an order as paid for testing
- `/markswitchpaid <switch_id>` — manually mark a paid switch request
- `/marktransferred <transfer_id>` — mark a transfer request as completed
- `/active_tests` — show active test proxies

### Automation
- Reserves inventory during checkout
- Releases expired pending reservations
- Deletes expired proxy-access / ovpn-access from iProxy
- Keeps transferred proxies from being deleted after upgrade

---

## Stack

| Component | Technology |
|---|---|
| Bot framework | python-telegram-bot |
| Payments | Stripe Checkout, PayPal Orders API, CoinGate |
| Proxy management | iProxy Console API |
| Database | SQLite |
| Webhooks | Flask |
| Language | Python 3.12 |

---

## Repository structure

```text
genrdp-proxy-test-bot/
├── testbot.py
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── data/
│   ├── .gitkeep
│   ├── inventory.sample.json
│   └── inventory.sample.csv
├── docs/
│   ├── DEPLOYMENT.md
│   ├── INVENTORY.md
│   └── SECURITY.md
├── scripts/
│   ├── run_dev.sh
│   └── windows_start.ps1
└── .github/
    └── workflows/
        └── python-check.yml
```

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/genrdp-proxy-test-bot.git
cd genrdp-proxy-test-bot
```

### 2. Create a virtual environment

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

On Windows PowerShell:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your real credentials.

Important: `.env` must never be committed to GitHub.

### 4. Add inventory

For local demo testing, copy the sample inventory:

```bash
cp data/inventory.sample.json data/inventory.json
```

Then replace the fake `conn_id` values with real iProxy connection IDs.

### 5. Run the bot

```bash
python testbot.py
```

The bot uses Telegram polling and starts a Flask web server for payment callbacks.

---

## Webhook endpoints

Configure these public HTTPS endpoints in your payment providers:

| Provider | Endpoint |
|---|---|
| Stripe | `/stripe-webhook` |
| PayPal | `/paypal-success` |
| CoinGate | `/coingate-webhook` |

Basic success/cancel pages:

| Route | Purpose |
|---|---|
| `/payment-success` | Payment success landing page |
| `/payment-cancel` | Payment cancel landing page |
| `/health` | Health check |

---

## Environment variables

See `.env.example` for the full list.

The most important variables are:

```env
TELEGRAM_TOKEN=
ADMIN_IDS=
BASE_URL=
DB_PATH=data/proxy_shop.db
INVENTORY_FILE=data/inventory.json
IPROXY_API_KEY=
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
PAYPAL_CLIENT_ID=
PAYPAL_CLIENT_SECRET=
COINGATE_API_KEY=
```

---

## Security note

This repository is safe for public portfolio use only when it contains sample data.

Never commit:

```text
.env
data/*.db
real inventory with production connection IDs
customer data
payment secrets
iProxy API keys
cloudflared credentials
logs
```

---

## License

Portfolio/demo repository. All rights reserved unless a separate license is added.
