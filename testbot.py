"""
GenRDP Proxy Test Shop Bot — admin/client, 4 languages
======================================================

Cliente:
  /start -> lingua -> connessione test IPv4/IPv6 -> carrier -> 24h/7 giorni
  -> pagamento -> HTTP/SOCKS + OpenVPN sì/no
  -> creazione proxy-access iProxy con expires_at.

Durante la validità:
  /myproxies -> il cliente può cambiare HTTP/SOCKS senza perdere porta/login.
  /myproxies -> il cliente può cambiare operatore/IP pagando solo eventuale differenza.
  /myproxies -> il cliente può richiedere trasferimento a @genrdprenewalbot.

Scadenza:
  il job cancella proxy-access e ovpn-access su iProxy e libera la connessione.

Admin:
  /admin, /inventory, /reload_inventory, /markpaid <order_id>, /active_tests
"""

from __future__ import annotations

import asyncio
import csv
import html
import json
import logging
import math
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import stripe
from dotenv import load_dotenv
from flask import Flask, jsonify, request as flask_request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("genrdp_proxy_test_shop")

# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
ADMIN_NOTIFY_CHAT_ID = os.environ.get("ADMIN_NOTIFY_CHAT_ID", "").strip()

BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
DB_PATH = os.environ.get("DB_PATH", "data/proxy_shop.db")
INVENTORY_FILE = os.environ.get("INVENTORY_FILE", "data/inventory.json")

IPROXY_BASE = os.environ.get("IPROXY_BASE", "https://iproxy.online/api/console/v1").rstrip("/")
IPROXY_API_KEY = os.environ.get("IPROXY_API_KEY", "")

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
stripe.api_key = STRIPE_SECRET_KEY or None

PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_MODE = os.environ.get("PAYPAL_MODE", "live").lower()
PAYPAL_BASE = "https://api-m.paypal.com" if PAYPAL_MODE == "live" else "https://api-m.sandbox.paypal.com"
PAYPAL_FEE_PCT = float(os.environ.get("PAYPAL_FEE_PCT", "0.03"))

COINGATE_API_KEY = os.environ.get("COINGATE_API_KEY", "")
COINGATE_MODE = os.environ.get("COINGATE_MODE", "live").lower()
COINGATE_BASE = "https://api.coingate.com" if COINGATE_MODE == "live" else "https://api-sandbox.coingate.com"

PAYMENT_CURRENCY = os.environ.get("PAYMENT_CURRENCY", "USD").upper()
RESERVATION_MINUTES = int(os.environ.get("RESERVATION_MINUTES", "45"))
EXPIRY_JOB_INTERVAL_SECONDS = int(os.environ.get("EXPIRY_JOB_INTERVAL_SECONDS", "3600"))
GENRDP_RENEWAL_BOT = os.environ.get("GENRDP_RENEWAL_BOT", "@genrdprenewalbot")

# Prezzi test richiesti. Di default vengono forzati anche se inventory contiene altri prezzi.
FORCE_TEST_PRICES = os.environ.get("FORCE_TEST_PRICES", "1").strip().lower() not in ("0", "false", "no")
TEST_PRICE_24H_IPV4 = float(os.environ.get("TEST_PRICE_24H_IPV4", "10"))
TEST_PRICE_7D_IPV4 = float(os.environ.get("TEST_PRICE_7D_IPV4", "30"))
TEST_PRICE_24H_IPV6 = float(os.environ.get("TEST_PRICE_24H_IPV6", "10"))
TEST_PRICE_7D_IPV6 = float(os.environ.get("TEST_PRICE_7D_IPV6", "35"))

# Limite anti-abuso: un solo test 24h per provider, per utente Telegram.
# Scope consigliato: "carrier" = stesso operatore bloccato anche tra IPv4/IPv6.
# Alternative: "carrier_ip" = blocca solo stessa coppia operatore + IPv4/IPv6.
LIMIT_24H_TEST_PER_PROVIDER = os.environ.get("LIMIT_24H_TEST_PER_PROVIDER", "1").strip().lower() not in ("0", "false", "no")
TEST_24H_LIMIT_SCOPE = os.environ.get("TEST_24H_LIMIT_SCOPE", "carrier").strip().lower()

# Se la forma esatta del body iProxy cambia, puoi mettere JSON template in env.
# Placeholders supportati: {login}, {password}, {protocol}, {expires_at}, {label}, {telegram_id}, {order_id}
IPROXY_PROXY_ACCESS_PAYLOAD_TEMPLATE = os.environ.get("IPROXY_PROXY_ACCESS_PAYLOAD_TEMPLATE", "").strip()
IPROXY_OVPN_ACCESS_PAYLOAD_TEMPLATE = os.environ.get("IPROXY_OVPN_ACCESS_PAYLOAD_TEMPLATE", "").strip()
IPROXY_PROTOCOL_UPDATE_TEMPLATE = os.environ.get("IPROXY_PROTOCOL_UPDATE_TEMPLATE", "").strip()

PROTOCOL_MAP = {"http": "http", "socks": "socks5"}
SUPPORTED_LANGS = ["en", "it", "zh", "ru"]

flask_app = Flask(__name__)
_app: Application | None = None


# ── i18n ───────────────────────────────────────────────────────────────────────
T: dict[str, dict[str, str]] = {
    "choose_lang": {
        "en": "🌐 Please choose your language:",
        "it": "🌐 Scegli la lingua:",
        "zh": "🌐 请选择语言：",
        "ru": "🌐 Выберите язык:",
    },
    "lang_set": {
        "en": "✅ Language set to English.",
        "it": "✅ Lingua impostata su Italiano.",
        "zh": "✅ 语言已设置为中文。",
        "ru": "✅ Язык установлен на русский.",
    },
    "home": {
        "en": (
            "👋 <b>GenRDP Test Proxy</b>\n\n"
            "Choose a test mobile proxy connection. After payment I will ask you:\n"
            "• HTTP or SOCKS\n"
            "• whether you need OpenVPN config\n\n"
            "Prices:\n"
            "• IPv4: {p24v4}/24h or {p7v4}/7 days\n"
            "• IPv6: {p24v6}/24h or {p7v6}/7 days\n\n"
            "Tip: start with the 24h test to find the operator/proxy type you prefer.\n\n"
            "⚠️ At expiry the connection access will be deleted automatically.\n"
            "If you like the proxy, upgrade via {renewal_bot} to keep the same port."
        ),
        "it": (
            "👋 <b>GenRDP Proxy Test</b>\n\n"
            "Scegli una connessione mobile test. Dopo il pagamento ti chiederò:\n"
            "• HTTP oppure SOCKS\n"
            "• se ti serve la configurazione OpenVPN\n\n"
            "Prezzi:\n"
            "• IPv4: {p24v4}/24h oppure {p7v4}/7 giorni\n"
            "• IPv6: {p24v6}/24h oppure {p7v6}/7 giorni\n\n"
            "Consiglio: parti dal test di 24h per trovare operatore e tipo di proxy che preferisci.\n\n"
            "⚠️ Alla scadenza l’accesso alla connessione verrà cancellato automaticamente.\n"
            "Se il proxy è di gradimento, puoi aggiornare l’abbonamento tramite {renewal_bot} per mantenere la stessa porta."
        ),
        "zh": (
            "👋 <b>GenRDP 测试代理</b>\n\n"
            "请选择一个测试移动代理连接。付款后我会询问：\n"
            "• HTTP 或 SOCKS\n"
            "• 是否需要 OpenVPN 配置\n\n"
            "价格：\n"
            "• IPv4：{p24v4}/24小时 或 {p7v4}/7天\n"
            "• IPv6：{p24v6}/24小时 或 {p7v6}/7天\n\n"
            "建议：先购买 24 小时测试，找到更适合你的运营商和代理类型。\n\n"
            "⚠️ 到期后连接访问将被自动删除。\n"
            "如果代理满意，可通过 {renewal_bot} 升级订阅以保留同一端口。"
        ),
        "ru": (
            "👋 <b>GenRDP тестовый прокси</b>\n\n"
            "Выберите тестовое мобильное прокси‑подключение. После оплаты бот спросит:\n"
            "• HTTP или SOCKS\n"
            "• нужна ли конфигурация OpenVPN\n\n"
            "Цены:\n"
            "• IPv4: {p24v4}/24ч или {p7v4}/7 дней\n"
            "• IPv6: {p24v6}/24ч или {p7v6}/7 дней\n\n"
            "Совет: начните с теста на 24 часа, чтобы выбрать подходящего оператора и тип прокси.\n\n"
            "⚠️ После истечения срока доступ к подключению будет удалён автоматически.\n"
            "Если прокси вам подходит, обновите подписку через {renewal_bot}, чтобы сохранить тот же порт."
        ),
    },
    "buy_btn": {"en": "🧪 Buy test proxy", "it": "🧪 Acquista proxy test", "zh": "🧪 购买测试代理", "ru": "🧪 Купить тестовый прокси"},
    "my_btn": {"en": "📡 My active proxies", "it": "📡 I miei proxy attivi", "zh": "📡 我的有效代理", "ru": "📡 Мои активные прокси"},
    "language_btn": {"en": "🌐 Language", "it": "🌐 Lingua", "zh": "🌐 语言", "ru": "🌐 Язык"},
    "admin_btn": {"en": "⚙️ Admin", "it": "⚙️ Admin", "zh": "⚙️ 管理", "ru": "⚙️ Админ"},
    "no_available": {
        "en": "No test connections are available right now. Please try later.",
        "it": "Al momento non ci sono connessioni test disponibili. Riprova più tardi.",
        "zh": "目前没有可用的测试连接。请稍后再试。",
        "ru": "Сейчас нет доступных тестовых подключений. Попробуйте позже.",
    },
    "choose_ip": {
        "en": "Choose the test connection type:",
        "it": "Scegli il tipo di connessione test:",
        "zh": "请选择测试连接类型：",
        "ru": "Выберите тип тестового подключения:",
    },
    "choose_carrier": {
        "en": "You selected <b>{ip}</b>. Choose the carrier:",
        "it": "Hai scelto <b>{ip}</b>. Seleziona il carrier:",
        "zh": "你选择了 <b>{ip}</b>。请选择运营商：",
        "ru": "Вы выбрали <b>{ip}</b>. Выберите оператора:",
    },
    "choose_duration": {
        "en": "<b>{ip} / {carrier}</b>\nChoose duration:\n\n💡 Recommended: start with 24h to test the operator/proxy type before taking 7 days.\n\n⚠️ At expiry this access will be deleted automatically.",
        "it": "<b>{ip} / {carrier}</b>\nScegli la durata:\n\n💡 Consigliato: parti da 24h per provare operatore e tipo proxy prima dei 7 giorni.\n\n⚠️ Alla scadenza questo accesso verrà cancellato automaticamente.",
        "zh": "<b>{ip} / {carrier}</b>\n请选择时长：\n\n💡 建议：先选择 24 小时测试运营商和代理类型，再购买 7 天。\n\n⚠️ 到期后此访问将被自动删除。",
        "ru": "<b>{ip} / {carrier}</b>\nВыберите срок:\n\n💡 Рекомендуем начать с 24 часов, чтобы проверить оператора и тип прокси перед покупкой на 7 дней.\n\n⚠️ После истечения срока доступ будет удалён автоматически.",
    },
    "duration_24h": {"en": "24 hours — {price}", "it": "24 ore — {price}", "zh": "24小时 — {price}", "ru": "24 часа — {price}"},
    "duration_7d": {"en": "7 days — {price}", "it": "7 giorni — {price}", "zh": "7天 — {price}", "ru": "7 дней — {price}"},
    "order_summary": {
        "en": "🧾 <b>Order #{order_id}</b>\nType: <b>{ip}</b>\nCarrier: <b>{carrier}</b>\nDuration: <b>{duration}</b>\nTotal: <b>{amount}</b>\n\n⚠️ At expiry the access will be deleted.\n\nChoose payment method:",
        "it": "🧾 <b>Ordine #{order_id}</b>\nTipo: <b>{ip}</b>\nCarrier: <b>{carrier}</b>\nDurata: <b>{duration}</b>\nTotale: <b>{amount}</b>\n\n⚠️ Alla scadenza l’accesso verrà cancellato.\n\nScegli metodo di pagamento:",
        "zh": "🧾 <b>订单 #{order_id}</b>\n类型：<b>{ip}</b>\n运营商：<b>{carrier}</b>\n时长：<b>{duration}</b>\n总额：<b>{amount}</b>\n\n⚠️ 到期后访问将被删除。\n\n请选择付款方式：",
        "ru": "🧾 <b>Заказ #{order_id}</b>\nТип: <b>{ip}</b>\nОператор: <b>{carrier}</b>\nСрок: <b>{duration}</b>\nИтого: <b>{amount}</b>\n\n⚠️ После истечения срока доступ будет удалён.\n\nВыберите способ оплаты:",
    },
    "duration_label_24": {"en": "24 hours", "it": "24 ore", "zh": "24小时", "ru": "24 часа"},
    "duration_label_168": {"en": "7 days", "it": "7 giorni", "zh": "7天", "ru": "7 дней"},
    "pay_card": {"en": "💳 Card / Alipay / Google Pay", "it": "💳 Carta / Alipay / Google Pay", "zh": "💳 银行卡 / 支付宝 / Google Pay", "ru": "💳 Карта / Alipay / Google Pay"},
    "pay_paypal": {"en": "🅿️ PayPal (+{fee}%)", "it": "🅿️ PayPal (+{fee}%)", "zh": "🅿️ PayPal (+{fee}%)", "ru": "🅿️ PayPal (+{fee}%)"},
    "pay_crypto": {"en": "₿ Crypto", "it": "₿ Crypto", "zh": "₿ 加密货币", "ru": "₿ Крипто"},
    "pay_now": {"en": "Pay now", "it": "Paga ora", "zh": "立即付款", "ru": "Оплатить"},
    "manual_test": {"en": "🧪 Mark paid (admin test)", "it": "🧪 Segna pagato (test admin)", "zh": "🧪 标记已付款（管理员测试）", "ru": "🧪 Отметить оплаченным (админ тест)"},
    "cancel": {"en": "❌ Cancel", "it": "❌ Annulla", "zh": "❌ 取消", "ru": "❌ Отмена"},
    "back": {"en": "↩️ Back", "it": "↩️ Indietro", "zh": "↩️ 返回", "ru": "↩️ Назад"},
    "new_buy": {"en": "🧪 New test purchase", "it": "🧪 Nuovo acquisto test", "zh": "🧪 新测试购买", "ru": "🧪 Новая тестовая покупка"},
    "no_providers": {
        "en": "⚠️ No payment provider configured. Admin can test with /markpaid <order_id>.",
        "it": "⚠️ Nessun provider di pagamento configurato. L’admin può testare con /markpaid <order_id>.",
        "zh": "⚠️ 未配置付款服务。管理员可用 /markpaid <order_id> 测试。",
        "ru": "⚠️ Платёжный провайдер не настроен. Админ может тестировать через /markpaid <order_id>.",
    },
    "generating_link": {"en": "⏳ Generating payment link...", "it": "⏳ Genero il link di pagamento...", "zh": "⏳ 正在生成付款链接...", "ru": "⏳ Создаю ссылку оплаты..."},
    "payment_ready": {
        "en": "✅ Payment link ready for order #{order_id}. Complete payment, then return to Telegram.",
        "it": "✅ Link pronto per l’ordine #{order_id}. Completa il pagamento, poi torna su Telegram.",
        "zh": "✅ 订单 #{order_id} 的付款链接已生成。完成付款后返回 Telegram。",
        "ru": "✅ Ссылка для заказа #{order_id} готова. Завершите оплату и вернитесь в Telegram.",
    },
    "payment_error": {"en": "❌ Payment link error: {error}", "it": "❌ Errore link pagamento: {error}", "zh": "❌ 付款链接错误：{error}", "ru": "❌ Ошибка ссылки оплаты: {error}"},
    "order_cancelled": {"en": "Order cancelled.", "it": "Ordine annullato.", "zh": "订单已取消。", "ru": "Заказ отменён."},
    "paid_choose_proto": {
        "en": "✅ Payment received for order #{order_id}.\n\nWhich proxy type do you want? You can change it later while the proxy is active.",
        "it": "✅ Pagamento ricevuto per l’ordine #{order_id}.\n\nChe tipo di proxy vuoi usare? Potrai cambiarlo finché il proxy è attivo.",
        "zh": "✅ 已收到订单 #{order_id} 的付款。\n\n你想使用哪种代理类型？代理有效期间可之后更改。",
        "ru": "✅ Оплата заказа #{order_id} получена.\n\nКакой тип прокси использовать? Его можно изменить, пока прокси активен.",
    },
    "choose_ovpn": {
        "en": "Proxy type selected: <b>{protocol}</b>\n\nDo you also need OpenVPN configuration?",
        "it": "Tipo proxy scelto: <b>{protocol}</b>\n\nTi serve anche la configurazione OpenVPN?",
        "zh": "已选择代理类型：<b>{protocol}</b>\n\n是否还需要 OpenVPN 配置？",
        "ru": "Выбран тип прокси: <b>{protocol}</b>\n\nНужна конфигурация OpenVPN?",
    },
    "ovpn_yes": {"en": "Yes, I need OpenVPN", "it": "Sì, mi serve OpenVPN", "zh": "是，需要 OpenVPN", "ru": "Да, нужен OpenVPN"},
    "ovpn_no": {"en": "No, proxy only", "it": "No, solo proxy", "zh": "不，只需要代理", "ru": "Нет, только прокси"},
    "creating": {"en": "⏳ Creating iProxy access and expiry...", "it": "⏳ Creo l’accesso iProxy e applico la scadenza...", "zh": "⏳ 正在创建 iProxy 访问并设置到期时间...", "ru": "⏳ Создаю доступ iProxy и срок действия..."},
    "delivery": {
        "en": (
            "✅ <b>Proxy ready — order #{order_id}</b>\n\n"
            "Type: <b>{protocol}</b>\nHost: <code>{host}</code>\nPort: <code>{port}</code>\n"
            "Login: <code>{login}</code>\nPassword: <code>{password}</code>\nExpiry: <b>{expiry}</b>\n\n"
            "⚠️ At expiry this access will be deleted automatically.\n"
            "You can change HTTP/SOCKS from /myproxies while it is active.\n\n"
            "If you like this proxy, upgrade via {renewal_bot} before expiry to keep the same port."
        ),
        "it": (
            "✅ <b>Proxy pronto — ordine #{order_id}</b>\n\n"
            "Tipo: <b>{protocol}</b>\nHost: <code>{host}</code>\nPorta: <code>{port}</code>\n"
            "Login: <code>{login}</code>\nPassword: <code>{password}</code>\nScadenza: <b>{expiry}</b>\n\n"
            "⚠️ Alla scadenza questo accesso verrà cancellato automaticamente.\n"
            "Puoi cambiare HTTP/SOCKS da /myproxies finché è attivo.\n\n"
            "Se il proxy è di gradimento, aggiorna l’abbonamento tramite {renewal_bot} prima della scadenza per mantenere la stessa porta."
        ),
        "zh": (
            "✅ <b>代理已就绪 — 订单 #{order_id}</b>\n\n"
            "类型：<b>{protocol}</b>\n主机：<code>{host}</code>\n端口：<code>{port}</code>\n"
            "登录：<code>{login}</code>\n密码：<code>{password}</code>\n到期：<b>{expiry}</b>\n\n"
            "⚠️ 到期后此访问将被自动删除。\n"
            "有效期间可通过 /myproxies 更改 HTTP/SOCKS。\n\n"
            "如果代理满意，请在到期前通过 {renewal_bot} 升级，以保留同一端口。"
        ),
        "ru": (
            "✅ <b>Прокси готов — заказ #{order_id}</b>\n\n"
            "Тип: <b>{protocol}</b>\nHost: <code>{host}</code>\nPort: <code>{port}</code>\n"
            "Login: <code>{login}</code>\nPassword: <code>{password}</code>\nИстекает: <b>{expiry}</b>\n\n"
            "⚠️ После истечения срока этот доступ будет удалён автоматически.\n"
            "Пока он активен, можно изменить HTTP/SOCKS через /myproxies.\n\n"
            "Если прокси подходит, обновите подписку через {renewal_bot} до истечения срока, чтобы сохранить тот же порт."
        ),
    },
    "ovpn_caption": {"en": "OpenVPN configuration file.", "it": "File di configurazione OpenVPN.", "zh": "OpenVPN 配置文件。", "ru": "Файл конфигурации OpenVPN."},
    "ovpn_manual": {
        "en": "OpenVPN was requested, but the config file was not valid or not returned. The team will handle it manually.",
        "it": "OpenVPN richiesto, ma il file config non è valido o non è stato restituito. Il team lo gestirà manualmente.",
        "zh": "已请求 OpenVPN，但配置文件无效或未返回。团队将手动处理。",
        "ru": "OpenVPN был запрошен, но конфиг недействителен или не был возвращён. Команда обработает вручную.",
    },
    "provision_fail": {
        "en": "⚠️ Payment received, but automatic setup failed. The team has been notified.",
        "it": "⚠️ Pagamento ricevuto, ma la creazione automatica non è riuscita. Il team è stato avvisato.",
        "zh": "⚠️ 已收到付款，但自动配置失败。团队已收到通知。",
        "ru": "⚠️ Оплата получена, но автоматическая настройка не удалась. Команда уведомлена.",
    },
    "active_title": {"en": "📡 <b>Your active test proxies</b>", "it": "📡 <b>I tuoi proxy test attivi</b>", "zh": "📡 <b>你的有效测试代理</b>", "ru": "📡 <b>Ваши активные тестовые прокси</b>"},
    "no_active": {
        "en": "You have no active test proxies.",
        "it": "Non hai proxy test attivi.",
        "zh": "你没有有效的测试代理。",
        "ru": "У вас нет активных тестовых прокси.",
    },
    "active_detail": {
        "en": "📡 <b>Order #{order_id}</b>\nType: <b>{protocol}</b>\nHost: <code>{host}</code>\nPort: <code>{port}</code>\nExpiry: <b>{expiry}</b>\n\nYou can change HTTP/SOCKS or switch operator while it is active. If the new option costs more, you only pay the difference.",
        "it": "📡 <b>Ordine #{order_id}</b>\nTipo: <b>{protocol}</b>\nHost: <code>{host}</code>\nPorta: <code>{port}</code>\nScadenza: <b>{expiry}</b>\n\nPuoi cambiare HTTP/SOCKS o operatore finché è attivo. Se la nuova opzione costa di più, paghi solo la differenza.",
        "zh": "📡 <b>订单 #{order_id}</b>\n类型：<b>{protocol}</b>\n主机：<code>{host}</code>\n端口：<code>{port}</code>\n到期：<b>{expiry}</b>\n\n有效期间可更改 HTTP/SOCKS 或切换运营商。如新选项价格更高，只需补差价。",
        "ru": "📡 <b>Заказ #{order_id}</b>\nТип: <b>{protocol}</b>\nHost: <code>{host}</code>\nPort: <code>{port}</code>\nИстекает: <b>{expiry}</b>\n\nПока прокси активен, можно изменить HTTP/SOCKS или сменить оператора. Если новый вариант дороже, оплачивается только разница.",
    },
    "change_protocol_btn": {"en": "🔁 Change HTTP/SOCKS", "it": "🔁 Cambia HTTP/SOCKS", "zh": "🔁 更改 HTTP/SOCKS", "ru": "🔁 Изменить HTTP/SOCKS"},
    "choose_new_protocol": {"en": "Choose the new proxy type:", "it": "Scegli il nuovo tipo di proxy:", "zh": "请选择新的代理类型：", "ru": "Выберите новый тип прокси:"},
    "change_success": {"en": "✅ Proxy type updated to {protocol}.", "it": "✅ Tipo proxy aggiornato a {protocol}.", "zh": "✅ 代理类型已更新为 {protocol}。", "ru": "✅ Тип прокси обновлён на {protocol}."},
    "change_failed": {"en": "❌ Could not update proxy type. The team has been notified.", "it": "❌ Non sono riuscito ad aggiornare il tipo proxy. Il team è stato avvisato.", "zh": "❌ 无法更新代理类型。团队已收到通知。", "ru": "❌ Не удалось обновить тип прокси. Команда уведомлена."},
    "expired_cannot_change": {"en": "This proxy is no longer active.", "it": "Questo proxy non è più attivo.", "zh": "此代理已不再有效。", "ru": "Этот прокси больше не активен."},
    "expired_deleted": {
        "en": "⏱️ Your test proxy order #{order_id} has expired and the connection access has been deleted.",
        "it": "⏱️ Il proxy test dell’ordine #{order_id} è scaduto e l’accesso alla connessione è stato cancellato.",
        "zh": "⏱️ 你的测试代理订单 #{order_id} 已到期，连接访问已删除。",
        "ru": "⏱️ Тестовый прокси заказа #{order_id} истёк, доступ к подключению удалён.",
    },
    "test_limit_reached": {
        "en": "⚠️ You have already used a 24h test for this provider. To continue, choose 7 days or test another provider.",
        "it": "⚠️ Hai già usato un test 24h per questo provider. Per continuare, scegli 7 giorni oppure prova un altro provider.",
        "zh": "⚠️ 你已经使用过该运营商的 24 小时测试。如需继续，请选择 7 天或测试其他运营商。",
        "ru": "⚠️ Вы уже использовали 24-часовой тест для этого оператора. Чтобы продолжить, выберите 7 дней или другого оператора.",
    },
    "test_limit_switch_reached": {
        "en": "⚠️ You have already used a 24h test for this provider, so this switch is not available. Choose another provider or upgrade to 7 days.",
        "it": "⚠️ Hai già usato un test 24h per questo provider, quindi questo cambio non è disponibile. Scegli un altro provider oppure passa a 7 giorni.",
        "zh": "⚠️ 你已经使用过该运营商的 24 小时测试，因此无法切换到此选项。请选择其他运营商或升级到 7 天。",
        "ru": "⚠️ Вы уже использовали 24-часовой тест для этого оператора, поэтому этот переход недоступен. Выберите другого оператора или 7 дней.",
    },
}

T.update({'change_operator_btn': {'en': '🔄 Change operator / IP', 'it': '🔄 Cambia operatore / IP', 'ru': '🔄 Сменить оператора / IP', 'zh': '🔄 更换运营商 / IP'},
 'choose_switch_target': {'en': 'Choose the new operator/proxy type for order #{order_id}.\n'
                                '\n'
                                'The expiry stays the same. Same or lower price: no extra charge. Higher price: you only pay the difference.',
                          'it': 'Scegli il nuovo operatore/tipo proxy per l’ordine #{order_id}.\n'
                                '\n'
                                'La scadenza resta la stessa. Prezzo uguale o inferiore: nessun costo extra. Prezzo superiore: paghi solo la differenza.',
                          'ru': 'Выберите нового оператора/тип прокси для заказа #{order_id}.\n'
                                '\n'
                                'Срок действия остаётся прежним. Та же или более низкая цена: без доплаты. Более высокая цена: оплачивается только разница.',
                          'zh': '为订单 #{order_id} 选择新的运营商/代理类型。\n\n到期时间保持不变。同价或更低无需额外付款；更高价格只需补差价。'},
 'switch_confirm_btn': {'en': '✅ Confirm switch', 'it': '✅ Conferma cambio', 'ru': '✅ Подтвердить смену', 'zh': '✅ 确认更换'},
 'switch_confirm_free': {'en': 'Confirm switch for order #{order_id}?\n'
                               '\n'
                               'From: <b>{old_ip} / {old_carrier}</b>\n'
                               'To: <b>{new_ip} / {new_carrier}</b>\n'
                               '\n'
                               'No extra payment is needed. The expiry stays the same: <b>{expiry}</b>.',
                         'it': 'Confermi il cambio per l’ordine #{order_id}?\n'
                               '\n'
                               'Da: <b>{old_ip} / {old_carrier}</b>\n'
                               'A: <b>{new_ip} / {new_carrier}</b>\n'
                               '\n'
                               'Non serve nessun pagamento extra. La scadenza resta la stessa: <b>{expiry}</b>.',
                         'ru': 'Подтвердить смену для заказа #{order_id}?\n'
                               '\n'
                               'С: <b>{old_ip} / {old_carrier}</b>\n'
                               'На: <b>{new_ip} / {new_carrier}</b>\n'
                               '\n'
                               'Доплата не требуется. Срок остаётся прежним: <b>{expiry}</b>.',
                         'zh': '确认更换订单 #{order_id}？\n\n从：<b>{old_ip} / {old_carrier}</b>\n到：<b>{new_ip} / {new_carrier}</b>\n\n无需额外付款。到期时间保持不变：<b>{expiry}</b>。'},
 'switch_confirm_paid': {'en': 'Confirm switch for order #{order_id}?\n'
                               '\n'
                               'From: <b>{old_ip} / {old_carrier}</b> ({old_price})\n'
                               'To: <b>{new_ip} / {new_carrier}</b> ({new_price})\n'
                               '\n'
                               'Difference to pay: <b>{difference}</b>. The expiry stays the same: <b>{expiry}</b>.',
                         'it': 'Confermi il cambio per l’ordine #{order_id}?\n'
                               '\n'
                               'Da: <b>{old_ip} / {old_carrier}</b> ({old_price})\n'
                               'A: <b>{new_ip} / {new_carrier}</b> ({new_price})\n'
                               '\n'
                               'Differenza da pagare: <b>{difference}</b>. La scadenza resta la stessa: <b>{expiry}</b>.',
                         'ru': 'Подтвердить смену для заказа #{order_id}?\n'
                               '\n'
                               'С: <b>{old_ip} / {old_carrier}</b> ({old_price})\n'
                               'На: <b>{new_ip} / {new_carrier}</b> ({new_price})\n'
                               '\n'
                               'Доплата: <b>{difference}</b>. Срок остаётся прежним: <b>{expiry}</b>.',
                         'zh': '确认更换订单 #{order_id}？\n'
                               '\n'
                               '从：<b>{old_ip} / {old_carrier}</b> ({old_price})\n'
                               '到：<b>{new_ip} / {new_carrier}</b> ({new_price})\n'
                               '\n'
                               '需补差价：<b>{difference}</b>。到期时间保持不变：<b>{expiry}</b>。'},
 'switch_extra': {'en': '+{amount} difference', 'it': '+{amount} differenza', 'ru': '+{amount} разница', 'zh': '+{amount} 差价'},
 'switch_failed': {'en': '❌ Could not switch the connection automatically. Your current proxy remains active and the team has been notified.',
                   'it': '❌ Non sono riuscito a cambiare connessione automaticamente. Il proxy attuale resta attivo e il team è stato avvisato.',
                   'ru': '❌ Не удалось автоматически сменить подключение. Текущий прокси остаётся активным, команда уведомлена.',
                   'zh': '❌ 无法自动更换连接。当前代理仍然有效，团队已收到通知。'},
 'switch_no_extra': {'en': 'no extra', 'it': 'nessun extra', 'ru': 'без доплаты', 'zh': '无需补差'},
 'switch_no_targets': {'en': 'No alternative test connections are available right now.',
                       'it': 'Al momento non ci sono connessioni test alternative disponibili.',
                       'ru': 'Сейчас нет доступных альтернативных тестовых подключений.',
                       'zh': '目前没有可替换的测试连接。'},
 'switch_paid': {'en': '✅ Difference payment received. I’m switching the connection now...',
                 'it': '✅ Pagamento differenza ricevuto. Cambio la connessione ora...',
                 'ru': '✅ Оплата разницы получена. Меняю подключение...',
                 'zh': '✅ 已收到差价付款。正在更换连接...'},
 'switch_pay_ready': {'en': '✅ Difference payment link ready for switch #{switch_id}. Complete payment, then return to Telegram.',
                      'it': '✅ Link per la differenza pronto per il cambio #{switch_id}. Completa il pagamento, poi torna su Telegram.',
                      'ru': '✅ Ссылка для оплаты разницы по смене #{switch_id} готова. Завершите оплату и вернитесь в Telegram.',
                      'zh': '✅ 更换 #{switch_id} 的差价付款链接已生成。完成付款后返回 Telegram。'},
 'switch_success': {'en': '✅ Operator/proxy type changed successfully. New connection details are below.',
                    'it': '✅ Operatore/tipo proxy cambiato correttamente. Qui sotto trovi i nuovi dati.',
                    'ru': '✅ Оператор/тип прокси успешно изменён. Новые данные ниже.',
                    'zh': '✅ 运营商/代理类型已成功更换。新的连接信息如下。'}})


def tt(key: str, lang: str | None = None, **kwargs: Any) -> str:
    lang = lang if lang in SUPPORTED_LANGS else "en"
    text = T[key].get(lang, T[key]["en"])
    return text.format(**kwargs) if kwargs else text


def money(amount: float, currency: str | None = None) -> str:
    cur = (currency or PAYMENT_CURRENCY).upper()
    if cur == "USD":
        return f"${amount:.2f}"
    if cur == "EUR":
        return f"€{amount:.2f}"
    return f"{amount:.2f} {cur}"


def test_prices_for(ip_version: str) -> tuple[float, float]:
    if ip_version == "ipv6":
        return TEST_PRICE_24H_IPV6, TEST_PRICE_7D_IPV6
    return TEST_PRICE_24H_IPV4, TEST_PRICE_7D_IPV4


def duration_label(hours: int, lang: str) -> str:
    return tt("duration_label_24" if hours == 24 else "duration_label_168", lang)


def price_for(ip_version: str, duration_hours: int) -> float:
    p24, p7 = test_prices_for(ip_version)
    return p24 if int(duration_hours) == 24 else p7


def normalize_carrier(carrier: str) -> str:
    return " ".join(str(carrier or "").strip().lower().split())


def test_24h_scope_key(ip_version: str, carrier: str) -> str:
    carrier_key = normalize_carrier(carrier)
    ip_key = str(ip_version or "").strip().lower()
    if TEST_24H_LIMIT_SCOPE in ("carrier_ip", "ip_carrier", "type_provider", "provider_type"):
        return f"{carrier_key}::{ip_key}"
    return carrier_key


def is_24h_test_limited(telegram_id: int, ip_version: str, carrier: str, ignore_order_id: int | None = None) -> bool:
    """Return True if this user already has/used a 24h test for the configured provider scope."""
    if not LIMIT_24H_TEST_PER_PROVIDER:
        return False
    scope_key = test_24h_scope_key(ip_version, carrier)
    statuses = ("pending", "paid", "awaiting_preferences", "provisioning", "provisioned", "expired")
    with db() as c:
        row = c.execute(
            "SELECT 1 FROM test_usages WHERE telegram_id=? AND scope_key=? LIMIT 1",
            (telegram_id, scope_key),
        ).fetchone()
        if row:
            return True
        # Compatibility fallback for older DBs before test_usages existed.
        if TEST_24H_LIMIT_SCOPE in ("carrier_ip", "ip_carrier", "type_provider", "provider_type"):
            sql = """
                SELECT 1 FROM orders
                WHERE telegram_id=? AND duration_hours=24 AND lower(ip_version)=lower(?)
                  AND lower(carrier)=lower(?) AND status IN ({})
            """.format(",".join("?" for _ in statuses))
            params = [telegram_id, ip_version, carrier, *statuses]
        else:
            sql = """
                SELECT 1 FROM orders
                WHERE telegram_id=? AND duration_hours=24
                  AND lower(carrier)=lower(?) AND status IN ({})
            """.format(",".join("?" for _ in statuses))
            params = [telegram_id, carrier, *statuses]
        if ignore_order_id is not None:
            sql += " AND id<>?"
            params.append(ignore_order_id)
        sql += " LIMIT 1"
        row = c.execute(sql, params).fetchone()
        return row is not None


def record_24h_test_usage(telegram_id: int, ip_version: str, carrier: str, source_order_id: int | None = None, source_switch_id: int | None = None) -> None:
    if not LIMIT_24H_TEST_PER_PROVIDER:
        return
    scope_key = test_24h_scope_key(ip_version, carrier)
    with db() as c:
        c.execute(
            """
            INSERT OR IGNORE INTO test_usages
            (telegram_id, scope_key, ip_version, carrier, source_order_id, source_switch_id)
            VALUES (?,?,?,?,?,?)
            """,
            (telegram_id, scope_key, str(ip_version).lower(), carrier, source_order_id, source_switch_id),
        )




T.update({
    "transfer_btn": {
        "en": "🚀 Upgrade / transfer to renewal bot",
        "it": "🚀 Aggiorna / trasferisci al renewal bot",
        "zh": "🚀 升级 / 转移到续费机器人",
        "ru": "🚀 Обновить / перенести в renewal bot",
    },
    "transfer_id_request": {
        "en": (
            "🚀 <b>Transfer to renewal bot</b>\n\n"
            "To create the customer in {renewal_bot} and assign this proxy, I need the <b>Telegram ID</b> that will receive the proxy.\n\n"
            "Detected Telegram ID for this account: <code>{detected_id}</code>\n\n"
            "Is this the same Telegram account that must be created in the renewal bot?"
        ),
        "it": (
            "🚀 <b>Trasferimento al renewal bot</b>\n\n"
            "Per creare il cliente su {renewal_bot} e assegnargli questo proxy, mi serve il <b>Telegram ID</b> che riceverà il proxy.\n\n"
            "Telegram ID rilevato per questo account: <code>{detected_id}</code>\n\n"
            "È lo stesso account Telegram da creare nel renewal bot?"
        ),
        "zh": (
            "🚀 <b>转移到续费机器人</b>\n\n"
            "为了在 {renewal_bot} 中创建客户并分配此代理，我需要接收代理的 <b>Telegram ID</b>。\n\n"
            "当前账号检测到的 Telegram ID：<code>{detected_id}</code>\n\n"
            "这是否就是要在续费机器人中创建的同一个 Telegram 账号？"
        ),
        "ru": (
            "🚀 <b>Перенос в renewal bot</b>\n\n"
            "Чтобы создать клиента в {renewal_bot} и назначить ему этот прокси, нужен <b>Telegram ID</b> аккаунта, который получит прокси.\n\n"
            "Обнаруженный Telegram ID этого аккаунта: <code>{detected_id}</code>\n\n"
            "Это тот же Telegram аккаунт, который нужно создать в renewal bot?"
        ),
    },
    "transfer_other_id_request": {
        "en": (
            "✏️ <b>Send the customer Telegram ID</b>\n\n"
            "Send only the numeric Telegram ID of the account that must receive this proxy in {renewal_bot}.\n\n"
            "How to find it:\n"
            "1. Open <b>@userinfobot</b> with the Telegram account that will receive the proxy.\n"
            "2. Press <b>Start</b>.\n"
            "3. Copy the numeric <b>ID</b>.\n"
            "4. Send that number here.\n\n"
            "Example: <code>123456789</code>"
        ),
        "it": (
            "✏️ <b>Invia il Telegram ID del cliente</b>\n\n"
            "Invia solo l’ID numerico dell’account Telegram che dovrà ricevere questo proxy su {renewal_bot}.\n\n"
            "Come trovarlo:\n"
            "1. Apri <b>@userinfobot</b> con l’account Telegram che riceverà il proxy.\n"
            "2. Premi <b>Start</b>.\n"
            "3. Copia l’<b>ID</b> numerico.\n"
            "4. Invia qui quel numero.\n\n"
            "Esempio: <code>123456789</code>"
        ),
        "zh": (
            "✏️ <b>发送客户 Telegram ID</b>\n\n"
            "请只发送将在 {renewal_bot} 中接收此代理的 Telegram 账号数字 ID。\n\n"
            "查找方法：\n"
            "1. 使用将接收代理的 Telegram 账号打开 <b>@userinfobot</b>。\n"
            "2. 点击 <b>Start</b>。\n"
            "3. 复制数字 <b>ID</b>。\n"
            "4. 将该数字发送到这里。\n\n"
            "示例：<code>123456789</code>"
        ),
        "ru": (
            "✏️ <b>Отправьте Telegram ID клиента</b>\n\n"
            "Отправьте только числовой Telegram ID аккаунта, который должен получить этот прокси в {renewal_bot}.\n\n"
            "Как найти ID:\n"
            "1. Откройте <b>@userinfobot</b> с Telegram аккаунта, который получит прокси.\n"
            "2. Нажмите <b>Start</b>.\n"
            "3. Скопируйте числовой <b>ID</b>.\n"
            "4. Отправьте это число сюда.\n\n"
            "Пример: <code>123456789</code>"
        ),
    },
    "transfer_use_detected_btn": {
        "en": "✅ Yes, use this Telegram ID",
        "it": "✅ Sì, usa questo Telegram ID",
        "zh": "✅ 是，使用此 Telegram ID",
        "ru": "✅ Да, использовать этот Telegram ID",
    },
    "transfer_use_other_btn": {
        "en": "✏️ No, I need to use another Telegram ID",
        "it": "✏️ No, devo usare un altro Telegram ID",
        "zh": "✏️ 否，我需要使用另一个 Telegram ID",
        "ru": "✏️ Нет, нужно использовать другой Telegram ID",
    },
    "transfer_id_invalid": {
        "en": "❌ Invalid Telegram ID. Send only the numeric ID, for example: 123456789.",
        "it": "❌ Telegram ID non valido. Invia solo l’ID numerico, per esempio: 123456789.",
        "zh": "❌ Telegram ID 无效。请只发送数字 ID，例如：123456789。",
        "ru": "❌ Неверный Telegram ID. Отправьте только числовой ID, например: 123456789.",
    },
    "transfer_confirm": {
        "en": (
            "🚀 <b>Transfer request for order #{order_id}</b>\n\n"
            "I will collect the information needed to move this proxy to {renewal_bot}:\n"
            "• Customer Telegram ID for renewal bot: <code>{customer_telegram_id}</code>\n"
            "• Requesting Telegram ID: <code>{telegram_id}</code>\n"
            "• Username: <code>{username}</code>\n"
            "• Connection ID: <code>{conn_id}</code>\n"
            "• Proxy access ID: <code>{proxy_access_id}</code>\n"
            "• Port: <code>{port}</code>\n\n"
            "After the team completes the transfer, this test bot will stop deleting this access at expiry."
        ),
        "it": (
            "🚀 <b>Richiesta trasferimento ordine #{order_id}</b>\n\n"
            "Raccoglierò le informazioni necessarie per spostare questo proxy su {renewal_bot}:\n"
            "• Telegram ID cliente da creare nel renewal bot: <code>{customer_telegram_id}</code>\n"
            "• Telegram ID richiedente: <code>{telegram_id}</code>\n"
            "• Username: <code>{username}</code>\n"
            "• ID connessione: <code>{conn_id}</code>\n"
            "• Proxy access ID: <code>{proxy_access_id}</code>\n"
            "• Porta: <code>{port}</code>\n\n"
            "Quando il team completa il trasferimento, questo bot test smetterà di cancellare l’accesso alla scadenza."
        ),
        "zh": (
            "🚀 <b>订单 #{order_id} 转移请求</b>\n\n"
            "我会收集将此代理转移到 {renewal_bot} 所需的信息：\n"
            "• 续费机器人中的客户 Telegram ID：<code>{customer_telegram_id}</code>\n"
            "• 请求者 Telegram ID：<code>{telegram_id}</code>\n"
            "• 用户名：<code>{username}</code>\n"
            "• 连接 ID：<code>{conn_id}</code>\n"
            "• Proxy access ID：<code>{proxy_access_id}</code>\n"
            "• 端口：<code>{port}</code>\n\n"
            "团队完成转移后，此测试机器人将不会在到期时删除该访问。"
        ),
        "ru": (
            "🚀 <b>Запрос переноса заказа #{order_id}</b>\n\n"
            "Я соберу данные для переноса этого прокси в {renewal_bot}:\n"
            "• Telegram ID клиента для renewal bot: <code>{customer_telegram_id}</code>\n"
            "• Telegram ID отправителя: <code>{telegram_id}</code>\n"
            "• Username: <code>{username}</code>\n"
            "• Connection ID: <code>{conn_id}</code>\n"
            "• Proxy access ID: <code>{proxy_access_id}</code>\n"
            "• Port: <code>{port}</code>\n\n"
            "После завершения переноса тестовый бот не будет удалять этот доступ по истечении срока."
        ),
    },
    "transfer_confirm_btn": {
        "en": "✅ Send transfer request",
        "it": "✅ Invia richiesta trasferimento",
        "zh": "✅ 发送转移请求",
        "ru": "✅ Отправить запрос переноса",
    },
    "transfer_requested": {
        "en": "✅ Transfer request #{transfer_id} sent. The team has received the customer Telegram ID, connection ID and port. Continue in {renewal_bot} only after the team confirms the upgrade.",
        "it": "✅ Richiesta trasferimento #{transfer_id} inviata. Il team ha ricevuto Telegram ID cliente, ID connessione e porta. Continua su {renewal_bot} solo dopo la conferma dell’upgrade.",
        "zh": "✅ 转移请求 #{transfer_id} 已发送。团队已收到客户 Telegram ID、连接 ID 和端口。请在团队确认升级后再使用 {renewal_bot}。",
        "ru": "✅ Запрос переноса #{transfer_id} отправлен. Команда получила Telegram ID клиента, connection ID и порт. Продолжайте в {renewal_bot} только после подтверждения апгрейда.",
    },
    "transfer_already_requested": {
        "en": "ℹ️ A transfer request is already open for this order: #{transfer_id}.",
        "it": "ℹ️ Esiste già una richiesta di trasferimento aperta per questo ordine: #{transfer_id}.",
        "zh": "ℹ️ 此订单已有一个转移请求：#{transfer_id}。",
        "ru": "ℹ️ Для этого заказа уже есть открытый запрос переноса: #{transfer_id}.",
    },
    "transfer_marked_completed": {
        "en": "✅ Transfer completed. This test bot will no longer delete that proxy access at expiry. Please continue with {renewal_bot}.",
        "it": "✅ Trasferimento completato. Questo bot test non cancellerà più quell’accesso alla scadenza. Continua con {renewal_bot}.",
        "zh": "✅ 转移完成。此测试机器人将不会在到期时删除该代理访问。请继续使用 {renewal_bot}。",
        "ru": "✅ Перенос завершён. Тестовый бот больше не удалит этот доступ по истечении срока. Продолжайте с {renewal_bot}.",
    },
})

# ── DB helpers ─────────────────────────────────────────────────────────────────
def db() -> sqlite3.Connection:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def db_init() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                lang TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT UNIQUE NOT NULL,
                label TEXT NOT NULL,
                ip_version TEXT NOT NULL CHECK(ip_version IN ('ipv4','ipv6')),
                carrier TEXT NOT NULL,
                conn_id TEXT NOT NULL,
                price_24h REAL NOT NULL,
                price_7d REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'available'
                    CHECK(status IN ('available','reserved','sold','disabled')),
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                username TEXT,
                inventory_id INTEGER NOT NULL,
                ip_version TEXT NOT NULL,
                carrier TEXT NOT NULL,
                duration_hours INTEGER NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'USD',
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','paid','awaiting_preferences','provisioning','provisioned','failed','expired','cancelled')),
                provider TEXT,
                payment_id TEXT,
                protocol TEXT,
                needs_ovpn INTEGER,
                proxy_access_id TEXT,
                ovpn_access_id TEXT,
                proxy_host TEXT,
                proxy_port TEXT,
                proxy_login TEXT,
                proxy_password TEXT,
                ovpn_config_path TEXT,
                ovpn_config_valid INTEGER,
                error TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                paid_at TEXT,
                expires_at TEXT,
                managed_by_renewal INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS pending_payments (
                payment_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                order_id INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS switch_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                target_inventory_id INTEGER NOT NULL,
                target_ip_version TEXT NOT NULL,
                target_carrier TEXT NOT NULL,
                old_price REAL NOT NULL,
                new_price REAL NOT NULL,
                difference_amount REAL NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'USD',
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','paid','completed','failed','cancelled')),
                provider TEXT,
                payment_id TEXT,
                error TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                paid_at TEXT,
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS test_usages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                scope_key TEXT NOT NULL,
                ip_version TEXT NOT NULL,
                carrier TEXT NOT NULL,
                source_order_id INTEGER,
                source_switch_id INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(telegram_id, scope_key)
            );

            CREATE TABLE IF NOT EXISTS transfer_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                customer_telegram_id TEXT,
                username TEXT,
                ip_version TEXT,
                carrier TEXT,
                conn_id TEXT NOT NULL,
                proxy_access_id TEXT,
                proxy_host TEXT,
                proxy_port TEXT,
                proxy_login TEXT,
                proxy_password TEXT,
                protocol TEXT,
                expires_at TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','completed','cancelled')),
                created_at TEXT DEFAULT (datetime('now')),
                completed_at TEXT
            );
            """
        )
        # migrations for older DBs
        for table, col, definition in [
            ("users", "lang", "TEXT"),
            ("orders", "ovpn_config_valid", "INTEGER"),
            ("orders", "managed_by_renewal", "INTEGER DEFAULT 0"),
            ("transfer_requests", "customer_telegram_id", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
            except Exception:
                pass


def upsert_user(update: Update) -> None:
    u = update.effective_user
    if not u:
        return
    with db() as c:
        c.execute(
            """
            INSERT INTO users (telegram_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
            """,
            (u.id, u.username, u.first_name),
        )


def get_user_lang(telegram_id: int | None) -> str:
    if not telegram_id:
        return "en"
    with db() as c:
        row = c.execute("SELECT lang FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    if row and row["lang"] in SUPPORTED_LANGS:
        return row["lang"]
    return "en"


def has_user_lang(telegram_id: int | None) -> bool:
    if not telegram_id:
        return False
    with db() as c:
        row = c.execute("SELECT lang FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    return bool(row and row["lang"] in SUPPORTED_LANGS)


def set_user_lang(telegram_id: int, lang: str) -> None:
    if lang not in SUPPORTED_LANGS:
        lang = "en"
    with db() as c:
        c.execute("UPDATE users SET lang=? WHERE telegram_id=?", (lang, telegram_id))


def get_inventory(inv_id: int) -> sqlite3.Row | None:
    with db() as c:
        return c.execute("SELECT * FROM inventory WHERE id=?", (inv_id,)).fetchone()


def get_order(order_id: int) -> sqlite3.Row | None:
    with db() as c:
        return c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()


def get_switch_request(switch_id: int) -> sqlite3.Row | None:
    with db() as c:
        return c.execute("SELECT * FROM switch_requests WHERE id=?", (switch_id,)).fetchone()


def get_transfer_request(transfer_id: int) -> sqlite3.Row | None:
    with db() as c:
        return c.execute("SELECT * FROM transfer_requests WHERE id=?", (transfer_id,)).fetchone()


def get_open_transfer_for_order(order_id: int) -> sqlite3.Row | None:
    with db() as c:
        return c.execute(
            "SELECT * FROM transfer_requests WHERE order_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (order_id,),
        ).fetchone()


def is_admin(uid: int | None) -> bool:
    return bool(uid and uid in ADMIN_IDS)


def h(value: Any) -> str:
    return html.escape(str(value))


def admin_user_label(username: Any, telegram_id: Any) -> str:
    """Return a compact admin-facing Telegram user label with @username when available."""
    raw = str(username or "").strip()
    if raw and raw.lower() not in ("none", "null"):
        shown = raw if raw.startswith("@") else f"@{raw}"
    else:
        shown = "senza @username"
    return f"{h(shown)} — <code>{h(telegram_id)}</code>"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def format_dt(value: str | None) -> str:
    dt = parse_iso(value)
    if not dt:
        return value or "N/D"
    return dt.strftime("%d/%m/%Y %H:%M UTC")


def paypal_price(base: float) -> float:
    return math.ceil(base * (1 + PAYPAL_FEE_PCT) * 100) / 100


# ── Inventory import ───────────────────────────────────────────────────────────
def load_inventory_file(path: str = INVENTORY_FILE) -> tuple[int, list[str]]:
    """Upsert inventory from JSON or CSV. Does not delete missing rows."""
    p = Path(path)
    if not p.exists():
        return 0, [f"File non trovato: {path}"]

    try:
        if p.suffix.lower() == ".json":
            items = json.loads(p.read_text(encoding="utf-8"))
        elif p.suffix.lower() == ".csv":
            with p.open("r", encoding="utf-8", newline="") as f:
                items = list(csv.DictReader(f))
        else:
            return 0, ["Formato non supportato: usa .json oppure .csv"]
    except Exception as e:
        return 0, [f"Errore lettura inventario: {e}"]

    count = 0
    errors: list[str] = []
    required = {"sku", "label", "ip_version", "carrier", "conn_id"}
    with db() as c:
        for raw in items:
            try:
                missing = required - set(raw)
                if missing:
                    raise ValueError(f"campi mancanti {sorted(missing)}")
                sku = str(raw["sku"]).strip()
                label = str(raw["label"]).strip()
                ip_version = str(raw["ip_version"]).strip().lower()
                carrier = str(raw["carrier"]).strip() or "Test"
                conn_id = str(raw["conn_id"]).strip()
                status = str(raw.get("status") or "available").strip().lower()
                notes = str(raw.get("notes") or "").strip()

                if ip_version not in ("ipv4", "ipv6"):
                    raise ValueError("ip_version deve essere ipv4 o ipv6")
                if status not in ("available", "reserved", "sold", "disabled"):
                    raise ValueError("status non valido")
                if not sku or not label or not conn_id:
                    raise ValueError("sku/label/conn_id vuoti")

                if FORCE_TEST_PRICES:
                    price_24h, price_7d = test_prices_for(ip_version)
                else:
                    price_24h = float(raw.get("price_24h") or test_prices_for(ip_version)[0])
                    price_7d = float(raw.get("price_7d") or test_prices_for(ip_version)[1])

                c.execute(
                    """
                    INSERT INTO inventory
                    (sku,label,ip_version,carrier,conn_id,price_24h,price_7d,status,notes)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(sku) DO UPDATE SET
                        label=excluded.label,
                        ip_version=excluded.ip_version,
                        carrier=excluded.carrier,
                        conn_id=excluded.conn_id,
                        price_24h=excluded.price_24h,
                        price_7d=excluded.price_7d,
                        status=CASE
                            WHEN inventory.status='sold' THEN inventory.status
                            ELSE excluded.status
                        END,
                        notes=excluded.notes,
                        updated_at=datetime('now')
                    """,
                    (sku, label, ip_version, carrier, conn_id, price_24h, price_7d, status, notes),
                )
                count += 1
            except Exception as e:
                errors.append(f"{raw.get('sku', '?')}: {e}")
    return count, errors


# ── iProxy helpers ─────────────────────────────────────────────────────────────
def iproxy_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {IPROXY_API_KEY}", "Content-Type": "application/json"}


async def iproxy_request(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any] | str | None]:
    if not IPROXY_API_KEY:
        return False, "IPROXY_API_KEY missing"
    url = f"{IPROXY_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.request(method, url, headers=iproxy_headers(), json=body)
        if r.status_code in (200, 201, 202, 204):
            if not r.content:
                return True, {}
            try:
                return True, r.json()
            except Exception:
                return True, r.text
        return False, f"{r.status_code}: {r.text[:500]}"
    except Exception as e:
        return False, str(e)


async def iproxy_get(path: str) -> dict[str, Any] | None:
    ok, data = await iproxy_request("GET", path)
    return data if ok and isinstance(data, dict) else None


async def iproxy_post(path: str, body: dict[str, Any]) -> dict[str, Any] | None:
    ok, data = await iproxy_request("POST", path, body)
    if ok and isinstance(data, dict):
        return data
    logger.warning("iProxy POST failed path=%s body=%s error=%s", path, body, data)
    return None


async def iproxy_delete(path: str) -> bool:
    ok, data = await iproxy_request("DELETE", path)
    if not ok:
        logger.warning("iProxy DELETE failed path=%s error=%s", path, data)
    return ok


def render_template_json(template: str, values: dict[str, Any]) -> dict[str, Any]:
    rendered = template.format(**{k: str(v).replace('"', '\\"') for k, v in values.items()})
    return json.loads(rendered)


def extract_access(data: dict[str, Any]) -> dict[str, Any]:
    for key in ("proxy_access", "access", "data", "ovpn_access"):
        if isinstance(data.get(key), dict):
            return data[key]
    return data


def pick_field(d: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in d and d[name] not in (None, ""):
            return d[name]
    return None


async def get_proxy_access_by_login(conn_id: str, login: str, access_id: str | None = None) -> dict[str, Any] | None:
    data = await iproxy_get(f"/connections/{conn_id}/proxy-access")
    if not data:
        return None
    accesses = data.get("proxy_accesses") or data.get("items") or data.get("data") or []
    if isinstance(accesses, dict):
        accesses = [accesses]
    for a in accesses:
        auth = a.get("auth") or {}
        if access_id and str(a.get("id")) == str(access_id):
            return a
        if a.get("login") == login or auth.get("login") == login or a.get("username") == login:
            return a
    return None


async def create_proxy_access(
    conn_id: str,
    order_id: int,
    telegram_id: int,
    protocol: str,
    expires_at: str,
    label: str,
) -> tuple[bool, dict[str, Any] | str]:
    login = f"genrdp_{telegram_id}_{order_id}"
    password = secrets.token_urlsafe(10)
    iproxy_protocol = PROTOCOL_MAP.get(protocol, protocol)
    values = {
        "login": login,
        "password": password,
        "protocol": iproxy_protocol,
        "expires_at": expires_at,
        "label": label,
        "telegram_id": telegram_id,
        "order_id": order_id,
    }

    bodies: list[dict[str, Any]] = []
    if IPROXY_PROXY_ACCESS_PAYLOAD_TEMPLATE:
        bodies.append(render_template_json(IPROXY_PROXY_ACCESS_PAYLOAD_TEMPLATE, values))

    bodies.extend(
        [
            {
                "name": label,
                "protocol": iproxy_protocol,
                "auth_type": "userpass",
                "auth": {"login": login, "password": password},
                "expires_at": expires_at,
            },
            {
                "name": label,
                "type": iproxy_protocol,
                "auth_type": "userpass",
                "auth": {"login": login, "password": password},
                "expires_at": expires_at,
            },
            {
                "login": login,
                "password": password,
                "protocol": iproxy_protocol,
                "expires_at": expires_at,
            },
            {
                "auth_type": "userpass",
                "auth": {"login": login, "password": password},
                "expires_at": expires_at,
            },
        ]
    )

    for body in bodies:
        data = await iproxy_post(f"/connections/{conn_id}/proxy-access", body)
        if data is not None:
            access = extract_access(data)
            access.setdefault("login", login)
            access.setdefault("password", password)
            return True, access

    return False, "iProxy rejected proxy-access payload"


async def create_ovpn_access(
    conn_id: str,
    order_id: int,
    telegram_id: int,
    expires_at: str,
    label: str,
    proxy_access_id: str | None,
) -> tuple[bool, dict[str, Any] | str]:
    login = f"ovpn_{telegram_id}_{order_id}"
    password = secrets.token_urlsafe(10)
    values = {
        "login": login,
        "password": password,
        "expires_at": expires_at,
        "label": label,
        "telegram_id": telegram_id,
        "order_id": order_id,
        "proxy_access_id": proxy_access_id or "",
    }

    bodies: list[dict[str, Any]] = []
    if IPROXY_OVPN_ACCESS_PAYLOAD_TEMPLATE:
        bodies.append(render_template_json(IPROXY_OVPN_ACCESS_PAYLOAD_TEMPLATE, values))
    if proxy_access_id:
        bodies.append(
            {
                "name": label,
                "proxy_access_id": proxy_access_id,
                "auth_type": "userpass",
                "auth": {"login": login, "password": password},
                "expires_at": expires_at,
            }
        )
    bodies.extend(
        [
            {
                "name": label,
                "auth_type": "userpass",
                "auth": {"login": login, "password": password},
                "expires_at": expires_at,
            },
            {
                "name": label,
                "login": login,
                "password": password,
                "expires_at": expires_at,
            },
        ]
    )

    for body in bodies:
        data = await iproxy_post(f"/connections/{conn_id}/ovpn-access", body)
        if data is not None:
            access = extract_access(data)
            access.setdefault("login", login)
            access.setdefault("password", password)
            return True, access

    return False, "iProxy rejected ovpn-access payload"


def extract_config_text(data: dict[str, Any] | str) -> str:
    if isinstance(data, str):
        return data
    for key in ("config", "ovpn_config", "openvpn_config", "content", "file", "data"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return json.dumps(data, indent=2, ensure_ascii=False)


def looks_like_ovpn_config(content: str) -> bool:
    low = content.lower()
    return (
        "client" in low
        and "remote " in low
        and ("dev tun" in low or "dev " in low)
        and ("proto " in low or "<ca>" in low or "auth-user-pass" in low)
    )


async def fetch_ovpn_config(conn_id: str, ovpn_id: str, order_id: int) -> tuple[str | None, bool]:
    ok, data = await iproxy_request("GET", f"/connections/{conn_id}/ovpn-access/{ovpn_id}/config")
    if not ok or data is None:
        logger.warning("Could not fetch OVPN config: %s", data)
        return None, False
    content = extract_config_text(data)
    valid = looks_like_ovpn_config(content)
    out_dir = Path("data/ovpn_configs")
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".ovpn" if valid else ".txt"
    out_path = out_dir / f"order_{order_id}{suffix}"
    out_path.write_text(content, encoding="utf-8")
    return str(out_path), valid


async def update_proxy_access_protocol(conn_id: str, proxy_access_id: str, protocol: str) -> bool:
    iproxy_protocol = PROTOCOL_MAP.get(protocol, protocol)
    values = {"protocol": iproxy_protocol}
    bodies: list[dict[str, Any]] = []
    if IPROXY_PROTOCOL_UPDATE_TEMPLATE:
        bodies.append(render_template_json(IPROXY_PROTOCOL_UPDATE_TEMPLATE, values))
    bodies.extend(
        [
            {"protocol": iproxy_protocol},
            {"type": iproxy_protocol},
            {"proxy_type": iproxy_protocol},
        ]
    )
    for body in bodies:
        data = await iproxy_post(f"/connections/{conn_id}/proxy-access/{proxy_access_id}/update", body)
        if data is not None:
            return True
    return False


# ── Payments ───────────────────────────────────────────────────────────────────
def provider_enabled(provider: str) -> bool:
    if provider == "stripe":
        return bool(STRIPE_SECRET_KEY and BASE_URL)
    if provider == "paypal":
        return bool(PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET and BASE_URL)
    if provider == "coingate":
        return bool(COINGATE_API_KEY and BASE_URL)
    return False


async def paypal_token() -> str:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{PAYPAL_BASE}/v1/oauth2/token",
            auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
        )
    r.raise_for_status()
    return r.json()["access_token"]


async def paypal_create_order(amount: float, description: str, metadata: dict[str, Any]) -> tuple[str, str]:
    token = await paypal_token()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{PAYPAL_BASE}/v2/checkout/orders",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "intent": "CAPTURE",
                "purchase_units": [
                    {
                        "amount": {"currency_code": PAYMENT_CURRENCY, "value": f"{amount:.2f}"},
                        "description": description,
                        "custom_id": json.dumps(metadata),
                    }
                ],
                "application_context": {
                    "return_url": f"{BASE_URL}/paypal-success",
                    "cancel_url": f"{BASE_URL}/payment-cancel",
                    "brand_name": "GenRDP",
                    "user_action": "PAY_NOW",
                },
            },
        )
    r.raise_for_status()
    data = r.json()
    approve = next(link["href"] for link in data["links"] if link["rel"] == "approve")
    return data["id"], approve


async def paypal_capture(payment_id: str) -> dict[str, Any] | None:
    token = await paypal_token()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{PAYPAL_BASE}/v2/checkout/orders/{payment_id}/capture",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
    if r.status_code not in (200, 201):
        logger.error("PayPal capture failed %s: %s", r.status_code, r.text)
        return None
    return r.json()


async def coingate_create_order(amount: float, description: str, metadata: dict[str, Any]) -> tuple[str, str]:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{COINGATE_BASE}/v2/orders",
            headers={"Authorization": f"Token {COINGATE_API_KEY}", "Content-Type": "application/json"},
            json={
                "order_id": f"switch_{metadata['switch_id']}" if metadata.get("kind") == "switch" else str(metadata["order_id"]),
                "price_amount": f"{amount:.2f}",
                "price_currency": PAYMENT_CURRENCY,
                "receive_currency": "USDT",
                "title": "GenRDP Test Proxy",
                "description": description,
                "callback_url": f"{BASE_URL}/coingate-webhook",
                "success_url": f"{BASE_URL}/payment-success",
                "cancel_url": f"{BASE_URL}/payment-cancel",
                "token": json.dumps(metadata),
            },
        )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"CoinGate error {r.status_code}: {r.text}")
    data = r.json()
    return str(data["id"]), data["payment_url"]


def save_pending(payment_id: str, provider: str, order_id: int) -> None:
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO pending_payments (payment_id, provider, order_id) VALUES (?,?,?)",
            (payment_id, provider, order_id),
        )
        c.execute(
            "UPDATE orders SET provider=?, payment_id=? WHERE id=?",
            (provider, payment_id, order_id),
        )


async def build_payment(order_id: int, provider: str) -> tuple[str, str]:
    order = get_order(order_id)
    if not order:
        raise RuntimeError("Order not found")
    amount = float(order["amount"])
    desc = f"GenRDP Test {order['ip_version'].upper()} {order['carrier']} - {order['duration_hours']}h"
    meta = {"kind": "order", "order_id": order_id, "telegram_id": order["telegram_id"]}

    if provider == "stripe":
        session = stripe.checkout.Session.create(
            line_items=[
                {
                    "price_data": {
                        "currency": PAYMENT_CURRENCY.lower(),
                        "product_data": {"name": desc},
                        "unit_amount": int(round(amount * 100)),
                    },
                    "quantity": 1,
                }
            ],
            mode="payment",
            payment_method_types=["card", "alipay"],
            success_url=f"{BASE_URL}/payment-success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{BASE_URL}/payment-cancel",
            metadata={k: str(v) for k, v in meta.items()},
        )
        save_pending(session["id"], "stripe", order_id)
        return session["id"], session["url"]

    if provider == "paypal":
        total = paypal_price(amount)
        payment_id, url = await paypal_create_order(total, desc, meta)
        save_pending(payment_id, "paypal", order_id)
        return payment_id, url

    if provider == "coingate":
        payment_id, url = await coingate_create_order(amount, desc, meta)
        save_pending(payment_id, "coingate", order_id)
        return payment_id, url


    raise RuntimeError("Provider not supported")


def save_switch_payment(switch_id: int, provider: str, payment_id: str) -> None:
    with db() as c:
        c.execute(
            "UPDATE switch_requests SET provider=?, payment_id=? WHERE id=?",
            (provider, payment_id, switch_id),
        )


async def build_switch_payment(switch_id: int, provider: str) -> tuple[str, str]:
    sw = get_switch_request(switch_id)
    if not sw:
        raise RuntimeError("Switch request not found")
    amount = float(sw["difference_amount"])
    if amount <= 0:
        raise RuntimeError("No difference to pay")
    desc = f"GenRDP switch order #{sw['order_id']} to {sw['target_ip_version'].upper()} {sw['target_carrier']}"
    meta = {
        "kind": "switch",
        "switch_id": switch_id,
        "order_id": sw["order_id"],
        "telegram_id": sw["telegram_id"],
    }

    if provider == "stripe":
        session = stripe.checkout.Session.create(
            line_items=[
                {
                    "price_data": {
                        "currency": PAYMENT_CURRENCY.lower(),
                        "product_data": {"name": desc},
                        "unit_amount": int(round(amount * 100)),
                    },
                    "quantity": 1,
                }
            ],
            mode="payment",
            payment_method_types=["card", "alipay"],
            success_url=f"{BASE_URL}/payment-success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{BASE_URL}/payment-cancel",
            metadata={k: str(v) for k, v in meta.items()},
        )
        save_switch_payment(switch_id, "stripe", session["id"])
        return session["id"], session["url"]

    if provider == "paypal":
        total = paypal_price(amount)
        payment_id, url = await paypal_create_order(total, desc, meta)
        save_switch_payment(switch_id, "paypal", payment_id)
        return payment_id, url

    if provider == "coingate":
        payment_id, url = await coingate_create_order(amount, desc, meta)
        save_switch_payment(switch_id, "coingate", payment_id)
        return payment_id, url

    raise RuntimeError("Provider not supported")


# ── Client UI ──────────────────────────────────────────────────────────────────
def lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🇬🇧 English", callback_data="lang:en"), InlineKeyboardButton("🇮🇹 Italiano", callback_data="lang:it")],
            [InlineKeyboardButton("🇨🇳 中文", callback_data="lang:zh"), InlineKeyboardButton("🇷🇺 Русский", callback_data="lang:ru")],
        ]
    )


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    upsert_user(update)
    ctx.user_data.pop("buy", None)
    if not has_user_lang(update.effective_user.id):
        await update.message.reply_text(tt("choose_lang", "en"), reply_markup=lang_keyboard())
        return
    await send_home(update.message, update.effective_user.id)


async def cmd_language(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    upsert_user(update)
    lang = get_user_lang(update.effective_user.id)
    await update.message.reply_text(tt("choose_lang", lang), reply_markup=lang_keyboard())


async def send_home(msg_or_q, user_id: int) -> None:
    lang = get_user_lang(user_id)
    p24v4, p7v4 = test_prices_for("ipv4")
    p24v6, p7v6 = test_prices_for("ipv6")
    text = tt(
        "home",
        lang,
        p24v4=money(p24v4),
        p7v4=money(p7v4),
        p24v6=money(p24v6),
        p7v6=money(p7v6),
        renewal_bot=GENRDP_RENEWAL_BOT,
    )
    kb = [
        [InlineKeyboardButton(tt("buy_btn", lang), callback_data="buy:start")],
        [InlineKeyboardButton(tt("my_btn", lang), callback_data="my:list")],
        [InlineKeyboardButton(tt("language_btn", lang), callback_data="langmenu")],
    ]
    if is_admin(user_id):
        kb.append([InlineKeyboardButton(tt("admin_btn", lang), callback_data="adm:menu")])
    markup = InlineKeyboardMarkup(kb)
    if hasattr(msg_or_q, "edit_message_text"):
        await msg_or_q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await msg_or_q.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def buy_start(q, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_user_lang(q.from_user.id)
    with db() as c:
        rows = c.execute(
            """
            SELECT ip_version, COUNT(*) n, MIN(price_24h) p24, MIN(price_7d) p7
            FROM inventory
            WHERE status='available'
            GROUP BY ip_version
            ORDER BY ip_version
            """
        ).fetchall()
    if not rows:
        await q.edit_message_text(tt("no_available", lang), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("back", lang), callback_data="buy:home")]]))
        return
    kb = []
    for r in rows:
        ip = r["ip_version"]
        label = f"{ip.upper()} — {money(float(r['p24']))}/24h | {money(float(r['p7']))}/7d ({r['n']})"
        kb.append([InlineKeyboardButton(label, callback_data=f"buy:ip:{ip}")])
    kb.append([InlineKeyboardButton(tt("back", lang), callback_data="buy:home")])
    await q.edit_message_text(tt("choose_ip", lang), reply_markup=InlineKeyboardMarkup(kb))


async def buy_ip(q, ctx: ContextTypes.DEFAULT_TYPE, ip_version: str) -> None:
    lang = get_user_lang(q.from_user.id)
    ctx.user_data["buy"] = {"ip_version": ip_version}
    with db() as c:
        rows = c.execute(
            """
            SELECT carrier, COUNT(*) n
            FROM inventory
            WHERE status='available' AND ip_version=?
            GROUP BY carrier
            ORDER BY carrier COLLATE NOCASE
            """,
            (ip_version,),
        ).fetchall()
    if not rows:
        await q.edit_message_text(tt("no_available", lang), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("back", lang), callback_data="buy:start")]]))
        return
    kb = [[InlineKeyboardButton(f"{r['carrier']} ({r['n']})", callback_data=f"buy:carrier:{r['carrier']}")] for r in rows]
    kb.append([InlineKeyboardButton(tt("back", lang), callback_data="buy:start")])
    await q.edit_message_text(
        tt("choose_carrier", lang, ip=ip_version.upper()),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def buy_carrier(q, ctx: ContextTypes.DEFAULT_TYPE, carrier: str) -> None:
    lang = get_user_lang(q.from_user.id)
    buy = ctx.user_data.get("buy") or {}
    ip_version = buy.get("ip_version")
    if not ip_version:
        await buy_start(q, ctx)
        return
    ctx.user_data["buy"] = {"ip_version": ip_version, "carrier": carrier}
    p24, p7 = test_prices_for(ip_version)
    with db() as c:
        r = c.execute(
            "SELECT COUNT(*) n FROM inventory WHERE status='available' AND ip_version=? AND carrier=?",
            (ip_version, carrier),
        ).fetchone()
    if not r or r["n"] == 0:
        await q.edit_message_text(tt("no_available", lang), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("back", lang), callback_data=f"buy:ip:{ip_version}")]]))
        return
    kb = [
        [InlineKeyboardButton(tt("duration_24h", lang, price=money(p24)), callback_data="buy:duration:24")],
        [InlineKeyboardButton(tt("duration_7d", lang, price=money(p7)), callback_data="buy:duration:168")],
        [InlineKeyboardButton(tt("back", lang), callback_data=f"buy:ip:{ip_version}")],
    ]
    await q.edit_message_text(
        tt("choose_duration", lang, ip=ip_version.upper(), carrier=h(carrier)),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb),
    )


def reserve_inventory(ip_version: str, carrier: str, duration_hours: int, telegram_id: int, username: str | None) -> int | None:
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        inv = c.execute(
            """
            SELECT * FROM inventory
            WHERE status='available' AND ip_version=? AND carrier=?
            ORDER BY id
            LIMIT 1
            """,
            (ip_version, carrier),
        ).fetchone()
        if not inv:
            c.execute("ROLLBACK")
            return None
        amount = price_for(ip_version, duration_hours)
        cur = c.execute(
            """
            INSERT INTO orders
            (telegram_id, username, inventory_id, ip_version, carrier, duration_hours, amount, currency)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (telegram_id, username, inv["id"], ip_version, carrier, duration_hours, amount, PAYMENT_CURRENCY),
        )
        order_id = cur.lastrowid
        c.execute("UPDATE inventory SET status='reserved', updated_at=datetime('now') WHERE id=?", (inv["id"],))
        c.execute("COMMIT")
        return int(order_id)


async def buy_duration(q, ctx: ContextTypes.DEFAULT_TYPE, duration_hours: int) -> None:
    lang = get_user_lang(q.from_user.id)
    buy = ctx.user_data.get("buy") or {}
    ip_version = buy.get("ip_version")
    carrier = buy.get("carrier")
    if not ip_version or not carrier:
        await buy_start(q, ctx)
        return
    if int(duration_hours) == 24 and is_24h_test_limited(q.from_user.id, ip_version, carrier):
        await q.edit_message_text(
            tt("test_limit_reached", lang),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(tt("duration_7d", lang, price=money(price_for(ip_version, 168))), callback_data="buy:duration:168")],
                [InlineKeyboardButton(tt("back", lang), callback_data=f"buy:carrier:{carrier}")],
            ]),
        )
        return
    order_id = reserve_inventory(ip_version, carrier, duration_hours, q.from_user.id, q.from_user.username)
    if not order_id:
        await q.edit_message_text(tt("no_available", lang), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("new_buy", lang), callback_data="buy:start")]]))
        return
    ctx.user_data["buy"]["order_id"] = order_id
    await show_payment_methods(q, order_id)


async def show_payment_methods(q, order_id: int) -> None:
    lang = get_user_lang(q.from_user.id)
    order = get_order(order_id)
    if not order:
        await q.edit_message_text("Order not found.")
        return
    text = tt(
        "order_summary",
        lang,
        order_id=order_id,
        ip=h(order["ip_version"]).upper(),
        carrier=h(order["carrier"]),
        duration=duration_label(int(order["duration_hours"]), lang),
        amount=money(float(order["amount"]), order["currency"]),
    )
    kb = []
    if provider_enabled("stripe"):
        kb.append([InlineKeyboardButton(tt("pay_card", lang), callback_data=f"pay:stripe:{order_id}")])
    if provider_enabled("paypal"):
        kb.append([InlineKeyboardButton(tt("pay_paypal", lang, fee=int(PAYPAL_FEE_PCT * 100)), callback_data=f"pay:paypal:{order_id}")])
    if provider_enabled("coingate"):
        kb.append([InlineKeyboardButton(tt("pay_crypto", lang), callback_data=f"pay:coingate:{order_id}")])
    if is_admin(q.from_user.id):
        kb.append([InlineKeyboardButton(tt("manual_test", lang), callback_data=f"pay:manual:{order_id}")])
    kb.append([InlineKeyboardButton(tt("cancel", lang), callback_data=f"buy:cancel:{order_id}")])
    if len(kb) == 1:
        text += "\n\n" + tt("no_providers", lang)
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def handle_pay_button(q, provider: str, order_id: int) -> None:
    lang = get_user_lang(q.from_user.id)
    if provider == "manual":
        if not is_admin(q.from_user.id):
            await q.answer("Admin only", show_alert=True)
            return
        await mark_order_paid(order_id, "manual")
        await q.edit_message_text(f"✅ Ordine #{order_id} segnato pagato. Ho chiesto le preferenze al cliente.")
        return

    if not provider_enabled(provider):
        await q.answer("Provider not configured", show_alert=True)
        return
    await q.edit_message_text(tt("generating_link", lang))
    try:
        _, url = await build_payment(order_id, provider)
        await q.edit_message_text(
            tt("payment_ready", lang, order_id=order_id),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("pay_now", lang), url=url)]]),
        )
    except Exception as e:
        logger.exception("Payment creation failed")
        await q.edit_message_text(tt("payment_error", lang, error=h(e)))


async def cancel_order(q, order_id: int) -> None:
    lang = get_user_lang(q.from_user.id)
    order = get_order(order_id)
    if order and order["status"] == "pending" and order["telegram_id"] == q.from_user.id:
        with db() as c:
            c.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
            c.execute("UPDATE inventory SET status='available', updated_at=datetime('now') WHERE id=?", (order["inventory_id"],))
    await q.edit_message_text(tt("order_cancelled", lang), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("new_buy", lang), callback_data="buy:start")]]))


async def mark_order_paid(order_id: int, provider: str) -> None:
    order = get_order(order_id)
    if not order:
        return
    if order["status"] not in ("pending", "paid", "awaiting_preferences"):
        return
    with db() as c:
        c.execute(
            """
            UPDATE orders
            SET status='awaiting_preferences', provider=?, paid_at=datetime('now')
            WHERE id=?
            """,
            (provider, order_id),
        )
    order_after = get_order(order_id)
    if order_after and int(order_after["duration_hours"] or 0) == 24:
        record_24h_test_usage(
            int(order_after["telegram_id"]),
            str(order_after["ip_version"]),
            str(order_after["carrier"]),
            source_order_id=order_id,
        )
    await ask_post_purchase_preferences(order_id)
    await notify_admin(f"💰 Pagamento ricevuto\nOrdine #{order_id}\nProvider: {provider}")


async def ask_post_purchase_preferences(order_id: int) -> None:
    order = get_order(order_id)
    if not order or not _app:
        return
    lang = get_user_lang(order["telegram_id"])
    kb = [
        [InlineKeyboardButton("HTTP", callback_data=f"pref:proto:{order_id}:http")],
        [InlineKeyboardButton("SOCKS", callback_data=f"pref:proto:{order_id}:socks")],
    ]
    await _app.bot.send_message(
        chat_id=order["telegram_id"],
        text=tt("paid_choose_proto", lang, order_id=order_id),
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def handle_pref_proto(q, order_id: int, protocol: str) -> None:
    lang = get_user_lang(q.from_user.id)
    order = get_order(order_id)
    if not order or order["telegram_id"] != q.from_user.id:
        await q.answer("Order not found", show_alert=True)
        return
    if protocol not in ("http", "socks"):
        await q.answer("Invalid protocol", show_alert=True)
        return
    with db() as c:
        c.execute("UPDATE orders SET protocol=? WHERE id=?", (protocol, order_id))
    kb = [
        [InlineKeyboardButton(tt("ovpn_yes", lang), callback_data=f"pref:ovpn:{order_id}:1")],
        [InlineKeyboardButton(tt("ovpn_no", lang), callback_data=f"pref:ovpn:{order_id}:0")],
    ]
    await q.edit_message_text(
        tt("choose_ovpn", lang, protocol=protocol.upper()),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def handle_pref_ovpn(q, order_id: int, needs_ovpn: int) -> None:
    lang = get_user_lang(q.from_user.id)
    order = get_order(order_id)
    if not order or order["telegram_id"] != q.from_user.id:
        await q.answer("Order not found", show_alert=True)
        return
    if not order["protocol"]:
        await q.answer("Choose HTTP/SOCKS first", show_alert=True)
        return
    with db() as c:
        c.execute("UPDATE orders SET needs_ovpn=? WHERE id=?", (needs_ovpn, order_id))
    await q.edit_message_text(tt("creating", lang))
    await provision_order(order_id)


async def provision_order(order_id: int) -> None:
    order = get_order(order_id)
    if not order:
        return
    lang = get_user_lang(order["telegram_id"])
    inv = get_inventory(order["inventory_id"])
    if not inv:
        await fail_order(order_id, "Inventory item not found")
        return
    if not order["protocol"] or order["needs_ovpn"] is None:
        return

    with db() as c:
        c.execute("UPDATE orders SET status='provisioning' WHERE id=?", (order_id,))

    expires = iso(now_utc() + timedelta(hours=int(order["duration_hours"])))
    label = f"GenRDP test TG {order['telegram_id']} order {order_id}"

    ok, access_or_err = await create_proxy_access(
        conn_id=inv["conn_id"],
        order_id=order_id,
        telegram_id=order["telegram_id"],
        protocol=order["protocol"],
        expires_at=expires,
        label=label,
    )
    if not ok:
        await fail_order(order_id, f"proxy-access: {access_or_err}")
        return

    access = access_or_err if isinstance(access_or_err, dict) else {}
    proxy_access_id = str(pick_field(access, "id", "proxy_id", "proxy_access_id") or "") or None
    login = pick_field(access, "login", "username") or (access.get("auth") or {}).get("login") or f"genrdp_{order['telegram_id']}_{order_id}"
    password = pick_field(access, "password") or (access.get("auth") or {}).get("password") or ""

    list_access = await get_proxy_access_by_login(inv["conn_id"], str(login), proxy_access_id)
    if list_access:
        access = {**access, **list_access}
        proxy_access_id = str(pick_field(access, "id", "proxy_id", "proxy_access_id") or proxy_access_id or "") or None

    proxy_host = pick_field(access, "hostname", "host", "server", "domain", "ip") or "see iProxy dashboard"
    proxy_port = pick_field(access, "port", "http_port", "socks_port") or "see iProxy dashboard"

    ovpn_id = None
    ovpn_config_path = None
    ovpn_config_valid = 0
    if int(order["needs_ovpn"] or 0) == 1:
        ok2, ovpn_or_err = await create_ovpn_access(
            conn_id=inv["conn_id"],
            order_id=order_id,
            telegram_id=order["telegram_id"],
            expires_at=expires,
            label=label,
            proxy_access_id=proxy_access_id,
        )
        if ok2 and isinstance(ovpn_or_err, dict):
            ovpn_id = str(pick_field(ovpn_or_err, "id", "ovpn_id", "ovpn_access_id") or "") or None
            if ovpn_id:
                ovpn_config_path, valid = await fetch_ovpn_config(inv["conn_id"], ovpn_id, order_id)
                ovpn_config_valid = 1 if valid else 0
                if not valid:
                    await notify_admin(f"⚠️ OpenVPN config non valida o non .ovpn\nOrdine #{order_id}\nFile: {ovpn_config_path or 'N/D'}")
        else:
            await notify_admin(f"⚠️ OVPN manuale richiesto\nOrdine #{order_id}\nErrore: {ovpn_or_err}")

    with db() as c:
        c.execute(
            """
            UPDATE orders SET
                status='provisioned', expires_at=?, proxy_access_id=?, ovpn_access_id=?,
                proxy_host=?, proxy_port=?, proxy_login=?, proxy_password=?, ovpn_config_path=?, ovpn_config_valid=?
            WHERE id=?
            """,
            (expires, proxy_access_id, ovpn_id, str(proxy_host), str(proxy_port), str(login), str(password), ovpn_config_path, ovpn_config_valid, order_id),
        )
        c.execute("UPDATE inventory SET status='sold', updated_at=datetime('now') WHERE id=?", (inv["id"],))

    await deliver_order(order_id)
    await notify_admin(f"✅ Proxy test creato\nOrdine #{order_id}\nScadenza: {format_dt(expires)}")


async def fail_order(order_id: int, error: str) -> None:
    logger.error("Order %s failed: %s", order_id, error)
    order = get_order(order_id)
    if order:
        with db() as c:
            c.execute("UPDATE orders SET status='failed', error=? WHERE id=?", (error, order_id))
    if _app and order:
        lang = get_user_lang(order["telegram_id"])
        await _app.bot.send_message(chat_id=order["telegram_id"], text=tt("provision_fail", lang))
    await notify_admin(f"🚨 Provisioning fallito\nOrdine #{order_id}\nErrore: {error}")


async def deliver_order(order_id: int) -> None:
    order = get_order(order_id)
    if not order or not _app:
        return
    lang = get_user_lang(order["telegram_id"])
    text = tt(
        "delivery",
        lang,
        order_id=order_id,
        protocol=h((order["protocol"] or "").upper()),
        host=h(order["proxy_host"]),
        port=h(order["proxy_port"]),
        login=h(order["proxy_login"]),
        password=h(order["proxy_password"]),
        expiry=h(format_dt(order["expires_at"])),
        renewal_bot=GENRDP_RENEWAL_BOT,
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(tt("my_btn", lang), callback_data="my:list")]])
    await _app.bot.send_message(chat_id=order["telegram_id"], text=text, parse_mode=ParseMode.HTML, reply_markup=kb)
    if order["needs_ovpn"]:
        if order["ovpn_config_path"] and int(order["ovpn_config_valid"] or 0) == 1:
            path = Path(order["ovpn_config_path"])
            if path.exists():
                await _app.bot.send_document(
                    chat_id=order["telegram_id"],
                    document=InputFile(path.open("rb"), filename=f"genrdp_order_{order_id}.ovpn"),
                    caption=tt("ovpn_caption", lang),
                )
                return
        await _app.bot.send_message(chat_id=order["telegram_id"], text=tt("ovpn_manual", lang))


# ── Active proxies and protocol change ─────────────────────────────────────────
def active_orders_for_user(telegram_id: int) -> list[sqlite3.Row]:
    now_str = iso(now_utc())
    with db() as c:
        return c.execute(
            """
            SELECT * FROM orders
            WHERE telegram_id=? AND status='provisioned' AND COALESCE(managed_by_renewal,0)=0
              AND expires_at IS NOT NULL AND expires_at > ?
            ORDER BY expires_at ASC
            """,
            (telegram_id, now_str),
        ).fetchall()


async def cmd_myproxies(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    upsert_user(update)
    await show_myproxies_message(update.message, update.effective_user.id)


async def show_myproxies_message(msg_or_q, user_id: int) -> None:
    lang = get_user_lang(user_id)
    rows = active_orders_for_user(user_id)
    if not rows:
        text = tt("no_active", lang)
        kb = [[InlineKeyboardButton(tt("new_buy", lang), callback_data="buy:start")], [InlineKeyboardButton(tt("back", lang), callback_data="buy:home")]]
    else:
        text = tt("active_title", lang)
        kb = []
        for r in rows:
            label = f"#{r['id']} {str(r['ip_version']).upper()} {r['carrier']} | {str(r['protocol']).upper()} | {format_dt(r['expires_at'])}"
            kb.append([InlineKeyboardButton(label, callback_data=f"my:order:{r['id']}")])
        kb.append([InlineKeyboardButton(tt("back", lang), callback_data="buy:home")])
    markup = InlineKeyboardMarkup(kb)
    if hasattr(msg_or_q, "edit_message_text"):
        await msg_or_q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await msg_or_q.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def show_active_order(q, order_id: int) -> None:
    lang = get_user_lang(q.from_user.id)
    order = get_order(order_id)
    if (
        not order
        or order["telegram_id"] != q.from_user.id
        or order["status"] != "provisioned"
        or int(order["managed_by_renewal"] or 0) == 1
        or not order["expires_at"]
    ):
        await q.edit_message_text(tt("expired_cannot_change", lang), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("back", lang), callback_data="my:list")]]))
        return
    exp_dt = parse_iso(order["expires_at"])
    if not exp_dt or exp_dt <= now_utc():
        await q.edit_message_text(tt("expired_cannot_change", lang), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("back", lang), callback_data="my:list")]]))
        return
    text = tt(
        "active_detail",
        lang,
        order_id=order_id,
        protocol=h(str(order["protocol"]).upper()),
        host=h(order["proxy_host"]),
        port=h(order["proxy_port"]),
        expiry=h(format_dt(order["expires_at"])),
    )
    kb = [
        [InlineKeyboardButton(tt("change_protocol_btn", lang), callback_data=f"chproto:{order_id}")],
        [InlineKeyboardButton(tt("change_operator_btn", lang), callback_data=f"switch:list:{order_id}")],
        [InlineKeyboardButton(tt("transfer_btn", lang), callback_data=f"transfer:req:{order_id}")],
        [InlineKeyboardButton(tt("back", lang), callback_data="my:list")],
    ]
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def show_change_protocol(q, order_id: int) -> None:
    lang = get_user_lang(q.from_user.id)
    order = get_order(order_id)
    if not order or order["telegram_id"] != q.from_user.id or order["status"] != "provisioned" or int(order["managed_by_renewal"] or 0) == 1:
        await q.answer(tt("expired_cannot_change", lang), show_alert=True)
        return
    exp_dt = parse_iso(order["expires_at"])
    if not exp_dt or exp_dt <= now_utc():
        await q.answer(tt("expired_cannot_change", lang), show_alert=True)
        return
    kb = [
        [InlineKeyboardButton("HTTP", callback_data=f"chproto_set:{order_id}:http")],
        [InlineKeyboardButton("SOCKS", callback_data=f"chproto_set:{order_id}:socks")],
        [InlineKeyboardButton(tt("back", lang), callback_data=f"my:order:{order_id}")],
    ]
    await q.edit_message_text(tt("choose_new_protocol", lang), reply_markup=InlineKeyboardMarkup(kb))


async def handle_change_protocol(q, order_id: int, protocol: str) -> None:
    lang = get_user_lang(q.from_user.id)
    order = get_order(order_id)
    if not order or order["telegram_id"] != q.from_user.id or order["status"] != "provisioned" or int(order["managed_by_renewal"] or 0) == 1:
        await q.answer(tt("expired_cannot_change", lang), show_alert=True)
        return
    exp_dt = parse_iso(order["expires_at"])
    if not exp_dt or exp_dt <= now_utc() or not order["proxy_access_id"]:
        await q.answer(tt("expired_cannot_change", lang), show_alert=True)
        return
    inv = get_inventory(order["inventory_id"])
    if not inv:
        await q.edit_message_text(tt("change_failed", lang))
        return
    await q.edit_message_text(tt("creating", lang))
    ok = await update_proxy_access_protocol(inv["conn_id"], order["proxy_access_id"], protocol)
    if ok:
        with db() as c:
            c.execute("UPDATE orders SET protocol=? WHERE id=?", (protocol, order_id))
        await q.edit_message_text(
            tt("change_success", lang, protocol=protocol.upper()),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("back", lang), callback_data=f"my:order:{order_id}")]]),
        )
        await notify_admin(f"🔁 Cambio protocollo\nOrdine #{order_id}\nNuovo tipo: {protocol.upper()}")
    else:
        await q.edit_message_text(
            tt("change_failed", lang),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("back", lang), callback_data=f"my:order:{order_id}")]]),
        )
        await notify_admin(f"🚨 Cambio protocollo fallito\nOrdine #{order_id}\nProtocollo richiesto: {protocol.upper()}")



# ── Transfer to renewal bot ────────────────────────────────────────────────────
def transfer_admin_summary(tr: sqlite3.Row) -> str:
    username = (tr["username"] or "").strip()
    username_display = f"@{username}" if username and not username.startswith("@") else (username or "N/D")
    return (
        f"🚀 Richiesta trasferimento proxy #{tr['id']}\n"
        f"Ordine test: #{tr['order_id']}\n"
        f"Telegram ID richiedente: {tr['telegram_id']}\n"
        f"Telegram ID cliente da creare nel renewal bot: {tr['customer_telegram_id'] or tr['telegram_id']}\n"
        f"Username: {username_display}\n\n"
        f"DATI PER {GENRDP_RENEWAL_BOT}\n"
        f"conn_id: {tr['conn_id']}\n"
        f"proxy_access_id / proxy_id: {tr['proxy_access_id'] or 'N/D'}\n"
        f"porta: {tr['proxy_port'] or 'N/D'}\n"
        f"host: {tr['proxy_host'] or 'N/D'}\n"
        f"login: {tr['proxy_login'] or 'N/D'}\n"
        f"password: {tr['proxy_password'] or 'N/D'}\n"
        f"protocollo attuale: {str(tr['protocol'] or '').upper() or 'N/D'}\n"
        f"tipo/provider: {str(tr['ip_version'] or '').upper()} {tr['carrier'] or ''}\n"
        f"scadenza test: {format_dt(tr['expires_at'])}\n\n"
        f"Nel renewal bot l'utente va salvato con telegram_id={tr['customer_telegram_id'] or tr['telegram_id']} e gli va assegnata questa porta/proxy.\n"
        f"Quando hai completato il trasferimento, usa: /marktransferred {tr['id']}"
    )


async def show_transfer_id_request(q, ctx: ContextTypes.DEFAULT_TYPE, order_id: int) -> None:
    """Ask the customer which Telegram ID must be created in the renewal bot."""
    lang = get_user_lang(q.from_user.id)
    order = get_order(order_id)
    if not order or order["telegram_id"] != q.from_user.id or order["status"] != "provisioned":
        await q.answer(tt("expired_cannot_change", lang), show_alert=True)
        return
    if int(order["managed_by_renewal"] or 0) == 1:
        await q.answer(tt("transfer_marked_completed", lang, renewal_bot=GENRDP_RENEWAL_BOT), show_alert=True)
        return
    exp_dt = parse_iso(order["expires_at"])
    if not exp_dt or exp_dt <= now_utc():
        await q.answer(tt("expired_cannot_change", lang), show_alert=True)
        return
    inv = get_inventory(order["inventory_id"])
    if not inv:
        await q.answer(tt("expired_cannot_change", lang), show_alert=True)
        return
    existing = get_open_transfer_for_order(order_id)
    if existing:
        await q.edit_message_text(
            tt("transfer_already_requested", lang, transfer_id=existing["id"]),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("back", lang), callback_data=f"my:order:{order_id}")]]),
        )
        return

    ctx.user_data.pop("awaiting_transfer_telegram_id", None)
    text = tt("transfer_id_request", lang, renewal_bot=GENRDP_RENEWAL_BOT, detected_id=q.from_user.id)
    kb = [
        [InlineKeyboardButton(tt("transfer_use_detected_btn", lang), callback_data=f"transfer:use_tid:{order_id}:{q.from_user.id}")],
        [InlineKeyboardButton(tt("transfer_use_other_btn", lang), callback_data=f"transfer:other_tid:{order_id}")],
        [InlineKeyboardButton(tt("back", lang), callback_data=f"my:order:{order_id}")],
    ]
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def show_transfer_other_id_request(q, ctx: ContextTypes.DEFAULT_TYPE, order_id: int) -> None:
    """Ask the customer to manually send another Telegram ID for the renewal bot."""
    lang = get_user_lang(q.from_user.id)
    order = get_order(order_id)
    if not order or order["telegram_id"] != q.from_user.id or order["status"] != "provisioned":
        await q.answer(tt("expired_cannot_change", lang), show_alert=True)
        return
    if int(order["managed_by_renewal"] or 0) == 1:
        await q.answer(tt("transfer_marked_completed", lang, renewal_bot=GENRDP_RENEWAL_BOT), show_alert=True)
        return
    exp_dt = parse_iso(order["expires_at"])
    if not exp_dt or exp_dt <= now_utc():
        await q.answer(tt("expired_cannot_change", lang), show_alert=True)
        return
    existing = get_open_transfer_for_order(order_id)
    if existing:
        await q.edit_message_text(
            tt("transfer_already_requested", lang, transfer_id=existing["id"]),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("back", lang), callback_data=f"my:order:{order_id}")]]),
        )
        return

    ctx.user_data["awaiting_transfer_telegram_id"] = {"order_id": order_id}
    text = tt("transfer_other_id_request", lang, renewal_bot=GENRDP_RENEWAL_BOT)
    kb = [[InlineKeyboardButton(tt("back", lang), callback_data=f"transfer:req:{order_id}")]]
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


def clean_telegram_id(text: str) -> str | None:
    tid = str(text or "").strip().replace(" ", "")
    if tid.startswith("+"):
        tid = tid[1:]
    if tid.isdigit() and 5 <= len(tid) <= 20:
        return tid
    return None


async def send_transfer_confirm_message(msg, order_id: int, customer_telegram_id: str) -> None:
    lang = get_user_lang(msg.from_user.id)
    text, kb = build_transfer_confirm(order_id, msg.from_user, lang, customer_telegram_id)
    if not text:
        await msg.reply_text(tt("expired_cannot_change", lang))
        return
    await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


def build_transfer_confirm(order_id: int, user, lang: str, customer_telegram_id: str) -> tuple[str | None, list[list[InlineKeyboardButton]]]:
    order = get_order(order_id)
    if not order or order["telegram_id"] != user.id or order["status"] != "provisioned":
        return None, []
    if int(order["managed_by_renewal"] or 0) == 1:
        return None, []
    exp_dt = parse_iso(order["expires_at"])
    if not exp_dt or exp_dt <= now_utc():
        return None, []
    inv = get_inventory(order["inventory_id"])
    if not inv:
        return None, []
    existing = get_open_transfer_for_order(order_id)
    if existing:
        text = tt("transfer_already_requested", lang, transfer_id=existing["id"])
        kb = [[InlineKeyboardButton(tt("back", lang), callback_data=f"my:order:{order_id}")]]
        return text, kb
    username = order["username"] or user.username or "N/D"
    text = tt(
        "transfer_confirm",
        lang,
        order_id=order_id,
        renewal_bot=GENRDP_RENEWAL_BOT,
        customer_telegram_id=h(customer_telegram_id),
        telegram_id=order["telegram_id"],
        username=h(username),
        conn_id=h(inv["conn_id"]),
        proxy_access_id=h(order["proxy_access_id"] or "N/D"),
        port=h(order["proxy_port"] or "N/D"),
    )
    kb = [
        [InlineKeyboardButton(tt("transfer_confirm_btn", lang), callback_data=f"transfer:confirm:{order_id}:{customer_telegram_id}")],
        [InlineKeyboardButton(tt("back", lang), callback_data=f"my:order:{order_id}")],
    ]
    return text, kb


async def show_transfer_confirm(q, order_id: int, customer_telegram_id: str) -> None:
    lang = get_user_lang(q.from_user.id)
    customer_telegram_id = clean_telegram_id(customer_telegram_id) or str(q.from_user.id)
    text, kb = build_transfer_confirm(order_id, q.from_user, lang, customer_telegram_id)
    if not text:
        await q.answer(tt("expired_cannot_change", lang), show_alert=True)
        return
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def confirm_transfer(q, order_id: int, customer_telegram_id: str | None = None) -> None:
    lang = get_user_lang(q.from_user.id)
    order = get_order(order_id)
    if not order or order["telegram_id"] != q.from_user.id or order["status"] != "provisioned":
        await q.answer(tt("expired_cannot_change", lang), show_alert=True)
        return
    if int(order["managed_by_renewal"] or 0) == 1:
        await q.answer(tt("transfer_marked_completed", lang, renewal_bot=GENRDP_RENEWAL_BOT), show_alert=True)
        return
    exp_dt = parse_iso(order["expires_at"])
    if not exp_dt or exp_dt <= now_utc():
        await q.answer(tt("expired_cannot_change", lang), show_alert=True)
        return
    inv = get_inventory(order["inventory_id"])
    if not inv:
        await q.answer(tt("expired_cannot_change", lang), show_alert=True)
        return
    existing = get_open_transfer_for_order(order_id)
    if existing:
        await q.edit_message_text(
            tt("transfer_already_requested", lang, transfer_id=existing["id"]),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("back", lang), callback_data=f"my:order:{order_id}")]]),
        )
        return
    customer_telegram_id = clean_telegram_id(customer_telegram_id or "") or str(q.from_user.id)
    username = order["username"] or q.from_user.username or ""
    with db() as c:
        cur = c.execute(
            """
            INSERT INTO transfer_requests
            (order_id, telegram_id, customer_telegram_id, username, ip_version, carrier, conn_id, proxy_access_id,
             proxy_host, proxy_port, proxy_login, proxy_password, protocol, expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                order["id"], order["telegram_id"], customer_telegram_id, username, order["ip_version"], order["carrier"],
                inv["conn_id"], order["proxy_access_id"], order["proxy_host"], order["proxy_port"],
                order["proxy_login"], order["proxy_password"], order["protocol"], order["expires_at"],
            ),
        )
        transfer_id = int(cur.lastrowid)
    tr = get_transfer_request(transfer_id)
    if tr:
        await notify_admin(transfer_admin_summary(tr))
    await q.edit_message_text(
        tt("transfer_requested", lang, transfer_id=transfer_id, renewal_bot=GENRDP_RENEWAL_BOT),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("back", lang), callback_data=f"my:order:{order_id}")]]),
    )

async def mark_transfer_completed(transfer_id: int) -> bool:
    tr = get_transfer_request(transfer_id)
    if not tr or tr["status"] != "pending":
        return False
    with db() as c:
        c.execute("UPDATE transfer_requests SET status='completed', completed_at=datetime('now') WHERE id=?", (transfer_id,))
        c.execute("UPDATE orders SET managed_by_renewal=1 WHERE id=?", (tr["order_id"],))
    if _app:
        lang = get_user_lang(tr["telegram_id"])
        try:
            await _app.bot.send_message(
                chat_id=tr["telegram_id"],
                text=tt("transfer_marked_completed", lang, renewal_bot=GENRDP_RENEWAL_BOT),
            )
        except Exception:
            pass
    return True


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    pending = ctx.user_data.get("awaiting_transfer_telegram_id")
    if pending and update.message and update.effective_user:
        lang = get_user_lang(update.effective_user.id)
        customer_tid = clean_telegram_id(update.message.text or "")
        if not customer_tid:
            await update.message.reply_text(tt("transfer_id_invalid", lang))
            return
        order_id = int(pending["order_id"])
        ctx.user_data.pop("awaiting_transfer_telegram_id", None)
        await send_transfer_confirm_message(update.message, order_id, customer_tid)
        return
    await cmd_start(update, ctx)


async def cmd_marktransferred(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Uso: /marktransferred <transfer_id>")
        return
    try:
        transfer_id = int(ctx.args[0])
    except Exception:
        await update.message.reply_text("transfer_id non valido")
        return
    ok = await mark_transfer_completed(transfer_id)
    await update.message.reply_text("✅ Transfer segnato completato." if ok else "❌ Transfer non trovato o già chiuso.")


async def adm_transfers(q) -> None:
    with db() as c:
        rows = c.execute("SELECT * FROM transfer_requests ORDER BY id DESC LIMIT 15").fetchall()
    if not rows:
        text = "🚀 Nessuna richiesta trasferimento."
        kb = [[InlineKeyboardButton("⬅️ Menu", callback_data="adm:menu")]]
    else:
        lines = ["🚀 <b>Ultime richieste trasferimento</b>"]
        kb = []
        for r in rows:
            lines.append(
                f"#{r['id']} | ordine #{r['order_id']} | TG cliente {r['customer_telegram_id'] or r['telegram_id']} | "
                f"porta {h(r['proxy_port'] or 'N/D')} | {h(r['status'])}"
            )
            if r["status"] == "pending":
                kb.append([InlineKeyboardButton(f"✅ Completa transfer #{r['id']}", callback_data=f"adm:transferdone:{r['id']}")])
        kb.append([InlineKeyboardButton("⬅️ Menu", callback_data="adm:menu")])
        text = "\n".join(lines)
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


# ── Operator / IP switch during validity ────────────────────────────────────────
def validate_active_order_for_user(order_id: int, user_id: int) -> sqlite3.Row | None:
    order = get_order(order_id)
    if (
        not order
        or order["telegram_id"] != user_id
        or order["status"] != "provisioned"
        or int(order["managed_by_renewal"] or 0) == 1
        or not order["expires_at"]
    ):
        return None
    exp_dt = parse_iso(order["expires_at"])
    if not exp_dt or exp_dt <= now_utc():
        return None
    return order


def switch_difference(order: sqlite3.Row, target_ip_version: str) -> tuple[float, float, float]:
    old_price = float(order["amount"] or 0)
    new_price = price_for(target_ip_version, int(order["duration_hours"]))
    difference = max(0.0, round(new_price - old_price, 2))
    return old_price, new_price, difference


async def show_switch_targets(q, order_id: int) -> None:
    lang = get_user_lang(q.from_user.id)
    order = validate_active_order_for_user(order_id, q.from_user.id)
    if not order:
        await q.answer(tt("expired_cannot_change", lang), show_alert=True)
        return
    with db() as c:
        rows = c.execute(
            """
            SELECT MIN(id) id, ip_version, carrier, COUNT(*) n
            FROM inventory
            WHERE status='available' AND id<>?
            GROUP BY ip_version, carrier
            ORDER BY ip_version, carrier COLLATE NOCASE
            """,
            (order["inventory_id"],),
        ).fetchall()
    if not rows:
        await q.edit_message_text(
            tt("switch_no_targets", lang),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("back", lang), callback_data=f"my:order:{order_id}")]]),
        )
        return
    kb = []
    for r in rows:
        if int(order["duration_hours"] or 0) == 24 and is_24h_test_limited(
            q.from_user.id, str(r["ip_version"]), str(r["carrier"]), ignore_order_id=order_id
        ):
            continue
        _, new_price, diff = switch_difference(order, r["ip_version"])
        suffix = tt("switch_no_extra", lang) if diff <= 0 else tt("switch_extra", lang, amount=money(diff, order["currency"]))
        label = f"{str(r['ip_version']).upper()} / {r['carrier']} — {money(new_price, order['currency'])} | {suffix}"
        if r["n"] > 1:
            label += f" ({r['n']})"
        kb.append([InlineKeyboardButton(label, callback_data=f"switch:target:{order_id}:{r['id']}")])
    if not kb:
        await q.edit_message_text(
            tt("switch_no_targets", lang),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("back", lang), callback_data=f"my:order:{order_id}")]]),
        )
        return
    kb.append([InlineKeyboardButton(tt("back", lang), callback_data=f"my:order:{order_id}")])
    await q.edit_message_text(
        tt("choose_switch_target", lang, order_id=order_id),
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def show_switch_confirm(q, order_id: int, target_inventory_id: int) -> None:
    lang = get_user_lang(q.from_user.id)
    order = validate_active_order_for_user(order_id, q.from_user.id)
    target = get_inventory(target_inventory_id)
    if not order or not target or target["status"] != "available":
        await q.edit_message_text(
            tt("switch_no_targets", lang),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("back", lang), callback_data=f"switch:list:{order_id}")]]),
        )
        return
    if int(order["duration_hours"] or 0) == 24 and is_24h_test_limited(
        q.from_user.id, str(target["ip_version"]), str(target["carrier"]), ignore_order_id=order_id
    ):
        await q.edit_message_text(
            tt("test_limit_switch_reached", lang),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("back", lang), callback_data=f"switch:list:{order_id}")]]),
        )
        return
    old_price, new_price, diff = switch_difference(order, target["ip_version"])
    common = dict(
        order_id=order_id,
        old_ip=h(str(order["ip_version"]).upper()),
        old_carrier=h(order["carrier"]),
        new_ip=h(str(target["ip_version"]).upper()),
        new_carrier=h(target["carrier"]),
        old_price=money(old_price, order["currency"]),
        new_price=money(new_price, order["currency"]),
        difference=money(diff, order["currency"]),
        expiry=h(format_dt(order["expires_at"])),
    )
    key = "switch_confirm_paid" if diff > 0 else "switch_confirm_free"
    kb = [
        [InlineKeyboardButton(tt("switch_confirm_btn", lang), callback_data=f"switch:confirm:{order_id}:{target_inventory_id}")],
        [InlineKeyboardButton(tt("back", lang), callback_data=f"switch:list:{order_id}")],
    ]
    await q.edit_message_text(tt(key, lang, **common), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


def create_switch_request(order_id: int, target_inventory_id: int) -> int | None:
    order = get_order(order_id)
    target = get_inventory(target_inventory_id)
    if not order or not target or target["status"] != "available":
        return None
    if int(order["duration_hours"] or 0) == 24 and is_24h_test_limited(
        int(order["telegram_id"]), str(target["ip_version"]), str(target["carrier"]), ignore_order_id=order_id
    ):
        return None
    old_price, new_price, diff = switch_difference(order, target["ip_version"])
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        target2 = c.execute("SELECT * FROM inventory WHERE id=? AND status='available'", (target_inventory_id,)).fetchone()
        if not target2:
            c.execute("ROLLBACK")
            return None
        cur = c.execute(
            """
            INSERT INTO switch_requests
            (order_id, telegram_id, target_inventory_id, target_ip_version, target_carrier,
             old_price, new_price, difference_amount, currency)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                order_id, order["telegram_id"], target_inventory_id, target2["ip_version"], target2["carrier"],
                old_price, new_price, diff, order["currency"],
            ),
        )
        switch_id = int(cur.lastrowid)
        c.execute("UPDATE inventory SET status='reserved', updated_at=datetime('now') WHERE id=?", (target_inventory_id,))
        c.execute("COMMIT")
        return switch_id


async def confirm_switch(q, order_id: int, target_inventory_id: int) -> None:
    lang = get_user_lang(q.from_user.id)
    order = validate_active_order_for_user(order_id, q.from_user.id)
    if not order:
        await q.answer(tt("expired_cannot_change", lang), show_alert=True)
        return
    switch_id = create_switch_request(order_id, target_inventory_id)
    if not switch_id:
        await q.edit_message_text(tt("switch_no_targets", lang), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("back", lang), callback_data=f"switch:list:{order_id}")]]))
        return
    sw = get_switch_request(switch_id)
    if not sw:
        await q.edit_message_text(tt("switch_failed", lang))
        return
    if float(sw["difference_amount"]) <= 0:
        await q.edit_message_text(tt("creating", lang))
        await perform_switch(switch_id, "free")
        return
    await show_switch_payment_methods(q, switch_id)


async def show_switch_payment_methods(q, switch_id: int) -> None:
    lang = get_user_lang(q.from_user.id)
    sw = get_switch_request(switch_id)
    order = get_order(sw["order_id"]) if sw else None
    if not sw or not order:
        await q.edit_message_text(tt("switch_failed", lang))
        return
    amount = float(sw["difference_amount"])
    text = tt(
        "switch_confirm_paid",
        lang,
        order_id=sw["order_id"],
        old_ip=h(str(order["ip_version"]).upper()),
        old_carrier=h(order["carrier"]),
        new_ip=h(str(sw["target_ip_version"]).upper()),
        new_carrier=h(sw["target_carrier"]),
        old_price=money(float(sw["old_price"]), sw["currency"]),
        new_price=money(float(sw["new_price"]), sw["currency"]),
        difference=money(amount, sw["currency"]),
        expiry=h(format_dt(order["expires_at"])),
    )
    kb = []
    if provider_enabled("stripe"):
        kb.append([InlineKeyboardButton(tt("pay_card", lang), callback_data=f"switchpay:stripe:{switch_id}")])
    if provider_enabled("paypal"):
        kb.append([InlineKeyboardButton(tt("pay_paypal", lang, fee=int(PAYPAL_FEE_PCT * 100)), callback_data=f"switchpay:paypal:{switch_id}")])
    if provider_enabled("coingate"):
        kb.append([InlineKeyboardButton(tt("pay_crypto", lang), callback_data=f"switchpay:coingate:{switch_id}")])
    if is_admin(q.from_user.id):
        kb.append([InlineKeyboardButton(tt("manual_test", lang), callback_data=f"switchpay:manual:{switch_id}")])
    kb.append([InlineKeyboardButton(tt("cancel", lang), callback_data=f"switch:cancel:{switch_id}")])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def handle_switch_pay_button(q, provider: str, switch_id: int) -> None:
    lang = get_user_lang(q.from_user.id)
    sw = get_switch_request(switch_id)
    if not sw or sw["telegram_id"] != q.from_user.id or sw["status"] != "pending":
        await q.answer(tt("expired_cannot_change", lang), show_alert=True)
        return
    if provider == "manual":
        if not is_admin(q.from_user.id):
            await q.answer("Admin only", show_alert=True)
            return
        await mark_switch_paid(switch_id, "manual")
        await q.edit_message_text(f"✅ Switch #{switch_id} segnato pagato.")
        return
    if not provider_enabled(provider):
        await q.answer("Provider not configured", show_alert=True)
        return
    await q.edit_message_text(tt("generating_link", lang))
    try:
        _, url = await build_switch_payment(switch_id, provider)
        await q.edit_message_text(
            tt("switch_pay_ready", lang, switch_id=switch_id),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("pay_now", lang), url=url)]]),
        )
    except Exception as e:
        logger.exception("Switch payment creation failed")
        await q.edit_message_text(tt("payment_error", lang, error=h(e)))


async def cancel_switch(q, switch_id: int) -> None:
    lang = get_user_lang(q.from_user.id)
    sw = get_switch_request(switch_id)
    if sw and sw["telegram_id"] == q.from_user.id and sw["status"] == "pending":
        with db() as c:
            c.execute("UPDATE switch_requests SET status='cancelled' WHERE id=?", (switch_id,))
            c.execute("UPDATE inventory SET status='available', updated_at=datetime('now') WHERE id=?", (sw["target_inventory_id"],))
        await q.edit_message_text(tt("order_cancelled", lang), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tt("back", lang), callback_data=f"my:order:{sw['order_id']}")]]))
    else:
        await q.edit_message_text(tt("order_cancelled", lang))


async def mark_switch_paid(switch_id: int, provider: str) -> None:
    sw = get_switch_request(switch_id)
    if not sw or sw["status"] not in ("pending", "paid"):
        return
    with db() as c:
        c.execute("UPDATE switch_requests SET status='paid', provider=?, paid_at=datetime('now') WHERE id=?", (provider, switch_id))
    if _app:
        lang = get_user_lang(sw["telegram_id"])
        await _app.bot.send_message(chat_id=sw["telegram_id"], text=tt("switch_paid", lang))
    await perform_switch(switch_id, provider)


async def perform_switch(switch_id: int, provider: str) -> None:
    sw = get_switch_request(switch_id)
    if not sw:
        return
    order = get_order(sw["order_id"])
    target_inv = get_inventory(sw["target_inventory_id"])
    if not order or not target_inv or not _app:
        return
    lang = get_user_lang(order["telegram_id"])
    if target_inv["status"] != "reserved":
        with db() as c:
            c.execute("UPDATE switch_requests SET status='failed', error=? WHERE id=?", ("Target inventory is no longer reserved", switch_id))
        await _app.bot.send_message(chat_id=order["telegram_id"], text=tt("switch_failed", lang))
        await notify_admin(f"🚨 Cambio operatore fallito\nSwitch #{switch_id}\nOrdine #{order['id']}\nErrore: target inventory non più riservato")
        return
    current_inv = get_inventory(order["inventory_id"])
    if not validate_active_order_for_user(order["id"], order["telegram_id"]):
        with db() as c:
            c.execute("UPDATE switch_requests SET status='failed', error=? WHERE id=?", ("Original order expired", switch_id))
            c.execute("UPDATE inventory SET status='available', updated_at=datetime('now') WHERE id=?", (sw["target_inventory_id"],))
        await _app.bot.send_message(chat_id=order["telegram_id"], text=tt("expired_cannot_change", lang))
        return

    expires = order["expires_at"]
    label = f"GenRDP test TG {order['telegram_id']} order {order['id']} switch {switch_id}"
    ok, access_or_err = await create_proxy_access(
        conn_id=target_inv["conn_id"],
        order_id=order["id"],
        telegram_id=order["telegram_id"],
        protocol=order["protocol"] or "http",
        expires_at=expires,
        label=label,
    )
    if not ok:
        with db() as c:
            c.execute("UPDATE switch_requests SET status='failed', error=? WHERE id=?", (f"proxy-access: {access_or_err}", switch_id))
            c.execute("UPDATE inventory SET status='available', updated_at=datetime('now') WHERE id=?", (sw["target_inventory_id"],))
        await _app.bot.send_message(chat_id=order["telegram_id"], text=tt("switch_failed", lang))
        await notify_admin(f"🚨 Cambio operatore fallito\nSwitch #{switch_id}\nOrdine #{order['id']}\nErrore: {access_or_err}")
        return

    access = access_or_err if isinstance(access_or_err, dict) else {}
    new_proxy_access_id = str(pick_field(access, "id", "proxy_id", "proxy_access_id") or "") or None
    login = pick_field(access, "login", "username") or (access.get("auth") or {}).get("login") or f"genrdp_{order['telegram_id']}_{order['id']}"
    password = pick_field(access, "password") or (access.get("auth") or {}).get("password") or ""
    list_access = await get_proxy_access_by_login(target_inv["conn_id"], str(login), new_proxy_access_id)
    if list_access:
        access = {**access, **list_access}
        new_proxy_access_id = str(pick_field(access, "id", "proxy_id", "proxy_access_id") or new_proxy_access_id or "") or None
    proxy_host = pick_field(access, "hostname", "host", "server", "domain", "ip") or "see iProxy dashboard"
    proxy_port = pick_field(access, "port", "http_port", "socks_port") or "see iProxy dashboard"

    new_ovpn_id = None
    ovpn_config_path = None
    ovpn_config_valid = 0
    if int(order["needs_ovpn"] or 0) == 1:
        ok2, ovpn_or_err = await create_ovpn_access(
            conn_id=target_inv["conn_id"],
            order_id=order["id"],
            telegram_id=order["telegram_id"],
            expires_at=expires,
            label=label,
            proxy_access_id=new_proxy_access_id,
        )
        if ok2 and isinstance(ovpn_or_err, dict):
            new_ovpn_id = str(pick_field(ovpn_or_err, "id", "ovpn_id", "ovpn_access_id") or "") or None
            if new_ovpn_id:
                ovpn_config_path, valid = await fetch_ovpn_config(target_inv["conn_id"], new_ovpn_id, order["id"])
                ovpn_config_valid = 1 if valid else 0
        else:
            await notify_admin(f"⚠️ OVPN manuale dopo cambio operatore\nSwitch #{switch_id}\nOrdine #{order['id']}\nErrore: {ovpn_or_err}")

    if current_inv:
        try:
            if order["proxy_access_id"]:
                await iproxy_delete(f"/connections/{current_inv['conn_id']}/proxy-access/{order['proxy_access_id']}")
            if order["ovpn_access_id"]:
                await iproxy_delete(f"/connections/{current_inv['conn_id']}/ovpn-access/{order['ovpn_access_id']}")
        except Exception as e:
            await notify_admin(f"⚠️ Cleanup vecchia connessione fallito\nSwitch #{switch_id}\nOrdine #{order['id']}\nErrore: {e}")

    with db() as c:
        c.execute(
            """
            UPDATE orders SET
                inventory_id=?, ip_version=?, carrier=?, amount=?,
                proxy_access_id=?, ovpn_access_id=?, proxy_host=?, proxy_port=?,
                proxy_login=?, proxy_password=?, ovpn_config_path=?, ovpn_config_valid=?
            WHERE id=?
            """,
            (
                target_inv["id"], target_inv["ip_version"], target_inv["carrier"], float(sw["new_price"]),
                new_proxy_access_id, new_ovpn_id, str(proxy_host), str(proxy_port),
                str(login), str(password), ovpn_config_path, ovpn_config_valid, order["id"],
            ),
        )
        c.execute("UPDATE inventory SET status='sold', updated_at=datetime('now') WHERE id=?", (target_inv["id"],))
        if current_inv:
            c.execute("UPDATE inventory SET status='available', updated_at=datetime('now') WHERE id=?", (current_inv["id"],))
        c.execute("UPDATE switch_requests SET status='completed', provider=?, completed_at=datetime('now') WHERE id=?", (provider, switch_id))

    if int(order["duration_hours"] or 0) == 24:
        record_24h_test_usage(
            int(order["telegram_id"]),
            str(target_inv["ip_version"]),
            str(target_inv["carrier"]),
            source_order_id=int(order["id"]),
            source_switch_id=switch_id,
        )

    await _app.bot.send_message(chat_id=order["telegram_id"], text=tt("switch_success", lang))
    await deliver_order(order["id"])
    await notify_admin(f"✅ Cambio operatore completato\nSwitch #{switch_id}\nOrdine #{order['id']}\nNuovo: {target_inv['ip_version'].upper()} {target_inv['carrier']}\nDiff: {money(float(sw['difference_amount']), sw['currency'])}")


# ── Admin ──────────────────────────────────────────────────────────────────────
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    upsert_user(update)
    if not is_admin(update.effective_user.id):
        return
    await show_admin(update.message)


async def show_admin(msg_or_q) -> None:
    kb = [
        [InlineKeyboardButton("📦 Inventario", callback_data="adm:inventory")],
        [InlineKeyboardButton("👥 Test assegnati per cliente", callback_data="adm:active_tests")],
        [InlineKeyboardButton("🧾 Ordini", callback_data="adm:orders")],
        [InlineKeyboardButton("🚀 Transfer renewal", callback_data="adm:transfers")],
        [InlineKeyboardButton("📊 Stats", callback_data="adm:stats")],
        [InlineKeyboardButton("🔄 Reload inventory.json", callback_data="adm:reload")],
    ]
    text = "⚙️ <b>Admin GenRDP Proxy Test Shop</b>"
    if hasattr(msg_or_q, "edit_message_text"):
        await msg_or_q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await msg_or_q.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def adm_inventory(q) -> None:
    with db() as c:
        rows = c.execute(
            """
            SELECT status, ip_version, carrier, COUNT(*) n
            FROM inventory
            GROUP BY status, ip_version, carrier
            ORDER BY status, ip_version, carrier
            """
        ).fetchall()
    if not rows:
        text = "📦 Inventario vuoto. Carica data/inventory.json e usa /reload_inventory."
    else:
        lines = ["📦 <b>Inventario</b>"]
        for r in rows:
            lines.append(f"{h(r['status'])} | {h(r['ip_version']).upper()} | {h(r['carrier'])}: <b>{r['n']}</b>")
        text = "\n".join(lines)
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="adm:menu")]]))


async def adm_orders(q) -> None:
    with db() as c:
        rows = c.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 15").fetchall()
    if not rows:
        text = "Nessun ordine."
    else:
        lines = ["🧾 <b>Ultimi ordini</b>"]
        for r in rows:
            user = admin_user_label(r["username"], r["telegram_id"])
            lines.append(
                f"#{r['id']} | {h(r['status'])} | {user} | {h(r['ip_version']).upper()} {h(r['carrier'])} | {money(float(r['amount']), r['currency'])}"
            )
        text = "\n".join(lines)
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="adm:menu")]]))


async def adm_active_tests(q) -> None:
    now_str = iso(now_utc())
    with db() as c:
        rows = c.execute(
            """
            SELECT
                o.*,
                i.sku AS inventory_sku,
                i.label AS inventory_label,
                i.conn_id AS inventory_conn_id,
                u.username AS current_username
            FROM orders o
            JOIN inventory i ON i.id=o.inventory_id
            LEFT JOIN users u ON u.telegram_id=o.telegram_id
            WHERE o.status='provisioned'
              AND COALESCE(o.managed_by_renewal,0)=0
              AND o.expires_at IS NOT NULL
              AND o.expires_at > ?
            ORDER BY o.telegram_id ASC, o.expires_at ASC
            LIMIT 80
            """,
            (now_str,),
        ).fetchall()

    if not rows:
        text = "👥 <b>Test assegnati per cliente</b>\nNessun test attivo assegnato."
    else:
        lines = ["👥 <b>Test assegnati per cliente</b>"]
        last_tid = None
        for r in rows:
            tid = r["telegram_id"]
            username = r["current_username"] or r["username"]
            if tid != last_tid:
                lines.append("")
                lines.append(f"👤 {admin_user_label(username, tid)}")
                last_tid = tid

            proto = (r["protocol"] or "N/D").upper()
            host = r["proxy_host"] or "N/D"
            port = r["proxy_port"] or "N/D"
            login = r["proxy_login"] or "N/D"
            label = r["inventory_label"] or r["inventory_sku"] or f"{r['ip_version']} {r['carrier']}"
            lines.append(
                f"  • #{r['id']} | {h(str(r['ip_version']).upper())} {h(r['carrier'])} | {h(proto)} | exp {h(format_dt(r['expires_at']))}"
            )
            lines.append(
                f"    {h(label)} | <code>{h(host)}:{h(port)}</code> | login <code>{h(login)}</code>"
            )

        text = "\n".join(lines)
        if len(text) > 3900:
            text = text[:3850] + "\n\n… lista tagliata. Usa /active_tests per aggiornarla o filtra dal DB."

    await q.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="adm:menu")]]),
    )


async def cmd_active_tests(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    upsert_user(update)
    if not is_admin(update.effective_user.id):
        return

    class ReplyAdapter:
        async def edit_message_text(self, text, parse_mode=None, reply_markup=None):
            await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)

    await adm_active_tests(ReplyAdapter())


async def adm_stats(q) -> None:
    with db() as c:
        revenue = c.execute("SELECT COALESCE(SUM(amount),0) v FROM orders WHERE status IN ('provisioned','expired')").fetchone()["v"]
        pending = c.execute("SELECT COUNT(*) n FROM orders WHERE status='pending'").fetchone()["n"]
        provisioned = c.execute("SELECT COUNT(*) n FROM orders WHERE status='provisioned'").fetchone()["n"]
        available = c.execute("SELECT COUNT(*) n FROM inventory WHERE status='available'").fetchone()["n"]
    text = (
        "📊 <b>Stats</b>\n"
        f"Revenue provisioned/expired: <b>{money(float(revenue))}</b>\n"
        f"Ordini pending: <b>{pending}</b>\n"
        f"Ordini attivi: <b>{provisioned}</b>\n"
        f"Proxy disponibili: <b>{available}</b>"
    )
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="adm:menu")]]))


async def adm_reload(q) -> None:
    count, errors = load_inventory_file()
    text = f"🔄 Import completato: <b>{count}</b> righe."
    if FORCE_TEST_PRICES:
        text += "\nPrezzi test forzati: IPv4 $10/$30, IPv6 $10/$35."
    if errors:
        text += "\n\nErrori:\n" + "\n".join(h(e) for e in errors[:10])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="adm:menu")]]))


async def cmd_reload_inventory(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    count, errors = load_inventory_file()
    text = f"✅ Inventario caricato: {count} righe."
    if FORCE_TEST_PRICES:
        text += "\nPrezzi test forzati: IPv4 $10/$30, IPv6 $10/$35."
    if errors:
        text += "\n" + "\n".join(errors[:10])
    await update.message.reply_text(text)


async def cmd_inventory(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    with db() as c:
        rows = c.execute("SELECT * FROM inventory ORDER BY id DESC LIMIT 50").fetchall()
    if not rows:
        await update.message.reply_text("Inventario vuoto.")
        return
    lines = ["📦 Inventory"]
    for r in rows:
        lines.append(f"#{r['id']} {r['status']} {r['ip_version'].upper()} {r['carrier']} {r['sku']} {money(r['price_24h'])}/{money(r['price_7d'])}")
    await update.message.reply_text("\n".join(lines[:60]))


async def cmd_markpaid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Uso: /markpaid <order_id>")
        return
    try:
        order_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("order_id non valido")
        return
    await mark_order_paid(order_id, "manual")
    await update.message.reply_text(f"✅ Ordine #{order_id} segnato pagato.")


async def cmd_markswitchpaid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Uso: /markswitchpaid <switch_id>")
        return
    try:
        switch_id = int(ctx.args[0])
    except Exception:
        await update.message.reply_text("switch_id non valido")
        return
    await mark_switch_paid(switch_id, "manual")
    await update.message.reply_text(f"✅ Switch #{switch_id} segnato pagato.")


async def notify_admin(text: str) -> None:
    if not _app:
        return
    targets: list[int | str] = []
    if ADMIN_NOTIFY_CHAT_ID:
        targets.append(ADMIN_NOTIFY_CHAT_ID)
    targets.extend(ADMIN_IDS)
    for target in dict.fromkeys(targets):
        try:
            await _app.bot.send_message(chat_id=target, text=text)
        except Exception as e:
            logger.warning("Admin notify failed target=%s: %s", target, e)


# ── Callback dispatcher ────────────────────────────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    upsert_user(update)

    try:
        if data == "langmenu":
            await q.edit_message_text(tt("choose_lang", get_user_lang(q.from_user.id)), reply_markup=lang_keyboard())
            return
        if data.startswith("lang:"):
            lang = data.split(":", 1)[1]
            set_user_lang(q.from_user.id, lang)
            await q.edit_message_text(tt("lang_set", lang))
            await send_home(q.message, q.from_user.id)
            return
        if data == "buy:home":
            await send_home(q, q.from_user.id)
            return
        if data == "buy:start":
            await buy_start(q, ctx)
            return
        if data.startswith("buy:ip:"):
            await buy_ip(q, ctx, data.split(":", 2)[2])
            return
        if data.startswith("buy:carrier:"):
            await buy_carrier(q, ctx, data.split(":", 2)[2])
            return
        if data.startswith("buy:duration:"):
            await buy_duration(q, ctx, int(data.rsplit(":", 1)[1]))
            return
        if data.startswith("buy:cancel:"):
            await cancel_order(q, int(data.rsplit(":", 1)[1]))
            return
        if data.startswith("pay:"):
            _, provider, oid = data.split(":", 2)
            await handle_pay_button(q, provider, int(oid))
            return
        if data.startswith("pref:proto:"):
            _, _, oid, proto = data.split(":", 3)
            await handle_pref_proto(q, int(oid), proto)
            return
        if data.startswith("pref:ovpn:"):
            _, _, oid, val = data.split(":", 3)
            await handle_pref_ovpn(q, int(oid), int(val))
            return
        if data == "my:list":
            await show_myproxies_message(q, q.from_user.id)
            return
        if data.startswith("my:order:"):
            await show_active_order(q, int(data.rsplit(":", 1)[1]))
            return
        if data.startswith("chproto:"):
            await show_change_protocol(q, int(data.rsplit(":", 1)[1]))
            return
        if data.startswith("chproto_set:"):
            _, oid, proto = data.split(":", 2)
            await handle_change_protocol(q, int(oid), proto)
            return
        if data.startswith("transfer:req:"):
            await show_transfer_id_request(q, ctx, int(data.rsplit(":", 1)[1]))
            return
        if data.startswith("transfer:use_tid:"):
            _, _, order_id, customer_tid = data.split(":", 3)
            ctx.user_data.pop("awaiting_transfer_telegram_id", None)
            await show_transfer_confirm(q, int(order_id), customer_tid)
            return
        if data.startswith("transfer:other_tid:"):
            await show_transfer_other_id_request(q, ctx, int(data.rsplit(":", 1)[1]))
            return
        if data.startswith("transfer:confirm:"):
            parts = data.split(":")
            order_id = int(parts[2])
            customer_tid = parts[3] if len(parts) > 3 else None
            ctx.user_data.pop("awaiting_transfer_telegram_id", None)
            await confirm_transfer(q, order_id, customer_tid)
            return
        if data.startswith("switch:list:"):
            await show_switch_targets(q, int(data.rsplit(":", 1)[1]))
            return
        if data.startswith("switch:target:"):
            _, _, oid, inv_id = data.split(":", 3)
            await show_switch_confirm(q, int(oid), int(inv_id))
            return
        if data.startswith("switch:confirm:"):
            _, _, oid, inv_id = data.split(":", 3)
            await confirm_switch(q, int(oid), int(inv_id))
            return
        if data.startswith("switchpay:"):
            _, provider, sid = data.split(":", 2)
            await handle_switch_pay_button(q, provider, int(sid))
            return
        if data.startswith("switch:cancel:"):
            await cancel_switch(q, int(data.rsplit(":", 1)[1]))
            return
        if data == "adm:menu" and is_admin(q.from_user.id):
            await show_admin(q)
            return
        if data == "adm:inventory" and is_admin(q.from_user.id):
            await adm_inventory(q)
            return
        if data == "adm:orders" and is_admin(q.from_user.id):
            await adm_orders(q)
            return
        if data == "adm:active_tests" and is_admin(q.from_user.id):
            await adm_active_tests(q)
            return
        if data == "adm:transfers" and is_admin(q.from_user.id):
            await adm_transfers(q)
            return
        if data.startswith("adm:transferdone:") and is_admin(q.from_user.id):
            transfer_id = int(data.rsplit(":", 1)[1])
            ok = await mark_transfer_completed(transfer_id)
            await q.answer("Transfer completato" if ok else "Transfer non trovato", show_alert=True)
            await adm_transfers(q)
            return
        if data == "adm:stats" and is_admin(q.from_user.id):
            await adm_stats(q)
            return
        if data == "adm:reload" and is_admin(q.from_user.id):
            await adm_reload(q)
            return
    except Exception as e:
        logger.exception("Callback error")
        await q.message.reply_text(f"Errore: {e}")


# ── Webhooks ───────────────────────────────────────────────────────────────────
def fire_paid(order_id: int, provider: str) -> None:
    if not _app:
        logger.error("Application not ready for fire_paid")
        return
    threading.Thread(target=lambda: asyncio.run(mark_order_paid(order_id, provider)), daemon=True).start()


def fire_switch_paid(switch_id: int, provider: str) -> None:
    if not _app:
        logger.error("Application not ready for fire_switch_paid")
        return
    threading.Thread(target=lambda: asyncio.run(mark_switch_paid(switch_id, provider)), daemon=True).start()


@flask_app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "STRIPE_WEBHOOK_SECRET missing"}), 400
    try:
        event = stripe.Webhook.construct_event(
            flask_request.data,
            flask_request.headers.get("Stripe-Signature", ""),
            STRIPE_WEBHOOK_SECRET,
        )
    except stripe.error.SignatureVerificationError:
        return jsonify({"error": "bad signature"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        meta = session.get("metadata", {}) or {}
        if meta.get("kind") == "switch":
            fire_switch_paid(int(meta["switch_id"]), "stripe")
        else:
            order_id = int(meta["order_id"])
            fire_paid(order_id, "stripe")
    return jsonify({"status": "ok"})


@flask_app.route("/paypal-success")
def paypal_success():
    payment_id = flask_request.args.get("token")
    if payment_id:
        threading.Thread(target=lambda: asyncio.run(_capture_paypal(payment_id)), daemon=True).start()
    return "<h2>✅ Payment received. Return to Telegram.</h2>"


async def _capture_paypal(payment_id: str) -> None:
    cap = await paypal_capture(payment_id)
    if not cap or cap.get("status") != "COMPLETED":
        logger.error("PayPal not completed: %s", payment_id)
        return
    try:
        meta = json.loads(cap["purchase_units"][0].get("custom_id", "{}"))
    except Exception as e:
        logger.error("PayPal metadata error: %s", e)
        return
    if meta.get("kind") == "switch":
        await mark_switch_paid(int(meta["switch_id"]), "paypal")
    else:
        await mark_order_paid(int(meta["order_id"]), "paypal")


@flask_app.route("/coingate-webhook", methods=["POST"])
def coingate_webhook():
    data = flask_request.form or flask_request.json or {}
    status = data.get("status")
    if status in ("paid", "confirmed"):
        try:
            meta = json.loads(data.get("token", "{}"))
        except Exception as e:
            return jsonify({"error": f"bad token: {e}"}), 400
        if meta.get("kind") == "switch":
            fire_switch_paid(int(meta["switch_id"]), "coingate")
        else:
            fire_paid(int(meta["order_id"]), "coingate")
    return jsonify({"status": "ok"})


@flask_app.route("/payment-success")
def payment_success():
    return "<h2>✅ Payment received. Return to Telegram.</h2>"


@flask_app.route("/payment-cancel")
def payment_cancel():
    return "<h2>❌ Payment cancelled. Return to Telegram.</h2>"


@flask_app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ── Jobs ───────────────────────────────────────────────────────────────────────
async def job_release_pending(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cutoff = (now_utc() - timedelta(minutes=RESERVATION_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as c:
        rows = c.execute(
            "SELECT * FROM orders WHERE status='pending' AND created_at < ?",
            (cutoff,),
        ).fetchall()
        for r in rows:
            c.execute("UPDATE orders SET status='cancelled' WHERE id=?", (r["id"],))
            c.execute("UPDATE inventory SET status='available', updated_at=datetime('now') WHERE id=?", (r["inventory_id"],))
    with db() as c:
        sw_rows = c.execute(
            "SELECT * FROM switch_requests WHERE status='pending' AND created_at < ?",
            (cutoff,),
        ).fetchall()
        for sw in sw_rows:
            c.execute("UPDATE switch_requests SET status='cancelled' WHERE id=?", (sw["id"],))
            c.execute("UPDATE inventory SET status='available', updated_at=datetime('now') WHERE id=?", (sw["target_inventory_id"],))
    if rows:
        logger.info("Released %d expired reservations", len(rows))
    if sw_rows:
        logger.info("Released %d expired switch reservations", len(sw_rows))


async def job_expire_orders(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    now_str = iso(now_utc())
    with db() as c:
        rows = c.execute(
            """
            SELECT o.*, i.conn_id
            FROM orders o JOIN inventory i ON i.id=o.inventory_id
            WHERE o.status='provisioned' AND COALESCE(o.managed_by_renewal,0)=0
              AND o.expires_at IS NOT NULL AND o.expires_at <= ?
            """,
            (now_str,),
        ).fetchall()
    for r in rows:
        try:
            if r["proxy_access_id"]:
                await iproxy_delete(f"/connections/{r['conn_id']}/proxy-access/{r['proxy_access_id']}")
            if r["ovpn_access_id"]:
                await iproxy_delete(f"/connections/{r['conn_id']}/ovpn-access/{r['ovpn_access_id']}")
        except Exception as e:
            logger.warning("Expiry cleanup failed order=%s: %s", r["id"], e)
        with db() as c:
            c.execute("UPDATE orders SET status='expired' WHERE id=?", (r["id"],))
            c.execute("UPDATE inventory SET status='available', updated_at=datetime('now') WHERE id=?", (r["inventory_id"],))
        try:
            lang = get_user_lang(r["telegram_id"])
            await ctx.bot.send_message(chat_id=r["telegram_id"], text=tt("expired_deleted", lang, order_id=r["id"]))
        except Exception:
            pass


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    global _app
    db_init()
    count, errors = load_inventory_file()
    if count:
        logger.info("Inventory loaded: %s items", count)
    if errors:
        logger.warning("Inventory load errors: %s", errors[:5])

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    _app = app

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("language", cmd_language))
    app.add_handler(CommandHandler("myproxies", cmd_myproxies))
    app.add_handler(CommandHandler("myproxy", cmd_myproxies))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("reload_inventory", cmd_reload_inventory))
    app.add_handler(CommandHandler("inventory", cmd_inventory))
    app.add_handler(CommandHandler("markpaid", cmd_markpaid))
    app.add_handler(CommandHandler("markswitchpaid", cmd_markswitchpaid))
    app.add_handler(CommandHandler("marktransferred", cmd_marktransferred))
    app.add_handler(CommandHandler("active_tests", cmd_active_tests))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.job_queue.run_repeating(job_release_pending, interval=300, first=300)
    app.job_queue.run_repeating(job_expire_orders, interval=EXPIRY_JOB_INTERVAL_SECONDS, first=120)

    server_port = int(os.environ.get("PORT", "8080"))
    threading.Thread(target=lambda: flask_app.run(host="0.0.0.0", port=server_port), daemon=True).start()
    logger.info("GenRDP Proxy Test Shop Bot avviato")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
