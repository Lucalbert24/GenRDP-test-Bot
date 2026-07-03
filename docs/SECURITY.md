# Security

This repository is designed to be safe for public GitHub portfolio use.

## Never commit

```text
.env
data/*.db
real inventory with production connection IDs
Telegram bot tokens
iProxy API keys
Stripe / PayPal / CoinGate credentials
customer data
payment logs
cloudflared credentials
```

## Public demo inventory

The included inventory files contain only dummy values. They are safe to publish, but they will not work until replaced with real iProxy connection IDs.

## If a secret is accidentally committed

1. Revoke the secret immediately.
2. Remove the file from the latest commit.
3. Rewrite Git history if the repository is public.
4. Force push the cleaned history.
5. Rotate any related credentials.
