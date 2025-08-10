# bot.py
import os
import threading
import time
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------- Config (via env vars) ----------
TELEGRAM_BOT_TOKEN = os.getenv("T8306980335:AAGYgB3TbCymLLRZCxQtpntxEf0TQeuUWDQ")   # set on Render
CHAT_ID = os.getenv("8447335131")                         # 8447335131
HTTP_API_KEY = os.getenv("f891551ef7494b59aff29d5f1ab37555")           # secret for HTTP endpoint (optional)

if not (TELEGRAM_BOT_TOKEN and CHAT_ID):
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN or CHAT_ID environment variables.")

CHAT_ID = str(CHAT_ID)

# ---------- Flask app (HTTP endpoint to submit signals) ----------
app = Flask(__name__)

def forward_to_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.ok, r.text
    except Exception as e:
        return False, str(e)

@app.route("/")
def home():
    return "Signal bot running", 200

@app.route("/signal", methods=["POST"])
def http_signal():
    """
    JSON expected:
    {
      "api_key": "SECRET_IF_USED",
      "pair": "EUR/USD",
      "side": "BUY" or "SELL",
      "expiry_min": 5,
      "confidence": "95%",
      "note": "optional note"
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    # simple auth
    if HTTP_API_KEY:
        if data.get("api_key") != HTTP_API_KEY:
            return jsonify({"ok": False, "error": "bad api_key"}), 401

    pair = data.get("pair") or data.get("symbol")
    side = (data.get("side") or "").upper()
    expiry = data.get("expiry_min") or data.get("expiry") or ""
    conf = data.get("confidence") or ""
    note = data.get("note") or ""

    if not pair or side not in ("BUY","SELL","CALL","PUT"):
        return jsonify({"ok": False, "error": "missing/invalid pair or side"}), 400

    side_label = "BUY" if side in ("BUY","CALL") else "SELL"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    text = f"📡 *SIGNAL* — *{side_label}*  \nPair: `{pair}`\nExpiry: {expiry} min\nConfidence: {conf}\nNote: {note}\nTime: {now}"
    ok, resp = forward_to_telegram(text)
    return jsonify({"ok": ok, "resp": resp})

# ---------- Telegram bot handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot actif. Utilise /signal <PAIR> <BUY/SELL> <expiry_min> [note]")

async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /signal EUR/USD BUY 5 optional note...
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Usage: /signal <PAIR> <BUY/SELL> <expiry_min> [note]")
        return
    pair = args[0]
    side = args[1].upper()
    expiry = args[2]
    note = " ".join(args[3:]) if len(args) > 3 else ""
    if side not in ("BUY","SELL","CALL","PUT"):
        await update.message.reply_text("Side must be BUY or SELL (or CALL/PUT).")
        return
    side_label = "BUY" if side in ("BUY","CALL") else "SELL"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    text = f"📡 *SIGNAL* — *{side_label}*  \nPair: `{pair}`\nExpiry: {expiry} min\nNote: {note}\nTime: {now}"
    ok, resp = forward_to_telegram(text)
    if ok:
        await update.message.reply_text("Signal forwarded ✅")
    else:
        await update.message.reply_text(f"Failed to forward: {resp}")

# ---------- Run Telegram bot in background thread ----------
def run_telegram_bot():
    app_builder = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app_builder.add_handler(CommandHandler("start", start_cmd))
    app_builder.add_handler(CommandHandler("signal", signal_cmd))
    # run polling (blocking) — we start it in a thread
    app_builder.run_polling()

# ---------- Start background thread when module imported by gunicorn ----------
def start_background():
    t = threading.Thread(target=run_telegram_bot, daemon=True)
    t.start()

# start background when imported by gunicorn / executed by python
start_background()

# if run directly (for local testing), run Flask
if __name__ == "__main__":
    # Note: when deploying on Render use gunicorn: gunicorn bot:app
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))￼Enter
