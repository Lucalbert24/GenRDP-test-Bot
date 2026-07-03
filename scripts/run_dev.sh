#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d "venv" ]; then
  python -m venv venv
fi

source venv/bin/activate
pip install -r requirements.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Edit it before running in production."
fi

if [ ! -f "data/inventory.json" ]; then
  cp data/inventory.sample.json data/inventory.json
  echo "Created data/inventory.json from sample data."
fi

python testbot.py
