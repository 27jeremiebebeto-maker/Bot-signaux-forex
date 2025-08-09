# main.py
import os
import time
import requests
import csv
import math
from datetime import datetime, timezone, timedelta
from threading import Thread
from flask import Flask

# ========== CONFIG (via env / Render secrets) ==========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")    # ex: from BotFather
CHAT_ID        = os.environ.get("CHAT_ID")           # ton chat id Telegram
TWELVEDATA_KEY = os.environ.get("TWELVEDATA_KEY")    # TwelveData API key

if not (TELEGRAM_TOKEN and CHAT_ID and TWELVEDATA_KEY):
    raise SystemExit("Missing env vars: TELEGRAM_TOKEN, CHAT_ID, TWELVEDATA_KEY")

# === Strategy params ===
SYMBOLS = ["EUR/USD","GBP/USD","USD/JPY","USD/CHF","AUD/USD","USD/CAD","NZD/USD"]
INTERVAL = "5min"            # timeframe M5 (recommandé)
EXPIRATION_MIN = 5
CHECK_INTERVAL = 60          # seconds between checks
COOLDOWN_MIN = 60            # minutes cooldown per pair (strict)
MAX_SIGNALS_PER_DAY = 5
LOOKBACK = 240
LOG_FILE = "trade_log.csv"
HEARTBEAT_SECONDS = 3600

# indicator params
EMA_SHORT = 20
EMA_LONG  = 50
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BB_PERIOD = 20
BB_STD = 2.0

BASE_TW = "https://api.twelvedata.com"

# ========== Flask app for Render (exposes /) ==========
app = Flask("bot_app")

@app.route("/")
def home():
    return "PocketOption Signals bot — running", 200

# ========== Helpers ==========
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=10)
        return r.ok
    except Exception as e:
        print("Telegram error:", e)
        return False

def td_get(endpoint, params):
    p = params.copy()
    p["apikey"] = TWELVEDATA_KEY
    try:
        r = requests.get(f"{BASE_TW}/{endpoint}", params=p, timeout=12)
        return r.json()
    except Exception as e:
        print("TwelveData request error:", e)
        return {}

def save_log(timestamp, symbol, signal, entry_price, exit_price, result, expiry_min):
    header = ["timestamp_utc","symbol","signal","entry_price","exit_price","result","expiry_min"]
    row = [timestamp.isoformat(), symbol, signal, entry_price, exit_price, result, expiry_min]
    exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        if not exists:
            f.write(",".join(header) + "\n")
        f.write(",".join(map(str,row)) + "\n")

# ========== Indicator helpers (no external libs) ==========
def ema_series(prices, period):
    n = len(prices)
    if n < period: return [None]*n
    k = 2/(period+1)
    emas = [None]*n
    sma = sum(prices[:period]) / period
    emas[period-1] = sma
    for i in range(period, n):
        emas[i] = (prices[i] - emas[i-1]) * k + emas[i-1]
    return emas

def rsi_series(prices, period=RSI_PERIOD):
    n = len(prices)
    if n < period+1: return [None]*n
    deltas = [prices[i]-prices[i-1] for i in range(1,n)]
    gains = [d if d>0 else 0 for d in deltas]
    losses = [-d if d<0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi = [None]*n
    if avg_loss == 0:
        rsi[period] = 100.0
    else:
        rs = avg_gain/avg_loss
        rsi[period] = 100 - (100/(1+rs))
    for i in range(period+1, n):
        g = gains[i-1]; l = losses[i-1]
        avg_gain = (avg_gain*(period-1) + g) / period
        avg_loss = (avg_loss*(period-1) + l) / period
        if avg_loss == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gain/avg_loss
            rsi[i] = 100 - (100/(1+rs))
    return rsi

def macd_series(prices, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    ema_f = ema_series(prices, fast)
    ema_s = ema_series(prices, slow)
    macd = [None]*len(prices)
    for i in range(len(prices)):
        if ema_f[i] is not None and ema_s[i] is not None:
            macd[i] = ema_f[i] - ema_s[i]
    vals = [v for v in macd if v is not None]
    if len(vals) < signal:
        return macd, [None]*len(prices), [None]*len(prices)
    sig_vals = ema_series(vals, signal)
    sig_line = [None]*len(prices)
    hist = [None]*len(prices)
    idxs = [i for i,v in enumerate(macd) if v is not None]
    for k, idx in enumerate(idxs):
        if k < len(sig_vals) and sig_vals[k] is not None:
            sig_line[idx] = sig_vals[k]
            hist[idx] = macd[idx] - sig_vals[k]
    return macd, sig_line, hist

def bbands(prices, period=BB_PERIOD, std=BB_STD):
    n = len(prices)
    mids, upp, low = [None]*n, [None]*n, [None]*n
    if n < period: return mids, upp, low
    for i in range(period-1, n):
        window = prices[i-period+1:i+1]
        sma = sum(window)/period
        variance = sum((p-sma)**2 for p in window)/period
        sd = math.sqrt(variance)
        mids[i] = sma; upp[i] = sma + std*sd; low[i] = sma - std*sd
    return mids, upp, low

# ========== Strategy logic ==========
last_sent = {}    # symbol -> datetime
sent_count_today = 0
today_date = datetime.now(timezone.utc).date()

def can_send(symbol):
    global sent_count_today, today_date
    now = datetime.now(timezone.utc)
    if now.date() != today_date:
        sent_count_today = 0; today_date = now.date()
    if sent_count_today >= MAX_SIGNALS_PER_DAY:
        return False
    last = last_sent.get(symbol)
    if last and (now - last).total_seconds() < COOLDOWN_MIN*60:
        return False
    return True

def fetch_indicator_values(symbol):
    # call a few endpoints and return parsed latest arrays
    try:
        ema = td_get("ema", {"symbol": symbol, "interval": INTERVAL, "time_period": 10})
        rsi = td_get("rsi", {"symbol": symbol, "interval": INTERVAL, "time_period": RSI_PERIOD})
        macd = td_get("macd", {"symbol": symbol, "interval": INTERVAL, "fast_period": MACD_FAST, "slow_period": MACD_SLOW, "signal_period": MACD_SIGNAL})
        stoch = td_get("stoch", {"symbol": symbol, "interval": INTERVAL, "fast_k_period":14, "slow_k_period":3, "slow_d_period":3})
        bb = td_get("bbands", {"symbol": symbol, "interval": INTERVAL, "time_period": BB_PERIOD, "nbdevup":2, "nbdevdn":2})
        if not all(isinstance(x, dict) and "values" in x for x in (ema, rsi, macd, stoch, bb)):
            return None
        return {"ema": ema["values"], "rsi": rsi["values"], "macd": macd["values"], "stoch": stoch["values"], "bb": bb["values"]}
    except Exception as e:
        print("fetch_indicator_values error:", e)
        return None

def analyze_symbol(symbol):
    data = fetch_indicator_values(symbol)
    if not data: return None
    try:
        ema_now = float(data["ema"][0]["ema"]); ema_prev = float(data["ema"][1]["ema"])
        rsi_now = float(data["rsi"][0]["rsi"])
        macd_now = float(data["macd"][0]["macd"]); macd_sig = float(data["macd"][0]["macd_signal"])
        stoch_k = float(data["stoch"][0]["slow_k"]); stoch_d = float(data["stoch"][0]["slow_d"])
        close = float(data["bb"][0]["close"]); upper = float(data["bb"][0]["upper_band"]); lower = float(data["bb"][0]["lower_band"])
    except Exception as e:
        print("parse indicators error:", e)
        return None

    volatil_high = close > upper
    volatil_low = close < lower

    # Ultra-strict BUY
    if ema_now > ema_prev and rsi_now > 60 and macd_now > macd_sig and stoch_k > stoch_d and not volatil_high:
        return {"signal":"CALL","type":"ULTRA","price":close,"conf":99}
    # Ultra-strict SELL
    if ema_now < ema_prev and rsi_now < 40 and macd_now < macd_sig and stoch_k < stoch_d and not volatil_low:
        return {"signal":"PUT","type":"ULTRA","price":close,"conf":99}
    # Strong conditions
    if ema_now > ema_prev and rsi_now > 55 and macd_now > macd_sig and stoch_k > stoch_d:
        return {"signal":"CALL","type":"STRONG","price":close,"conf":90}
    if ema_now < ema_prev and rsi_now < 45 and macd_now < macd_sig and stoch_k < stoch_d:
        return {"signal":"PUT","type":"STRONG","price":close,"conf":90}
    return None

def evaluate_quick(symbol, signal, entry_price):
    resp = td_get("time_series", {"symbol": symbol, "interval": INTERVAL, "outputsize": 3})
    if not (isinstance(resp, dict) and "values" in resp): return None, None
    try:
        exit_price = float(resp["values"][0]["close"])
    except:
        return None, None
    if signal == "CALL":
        return exit_price, ("WIN" if exit_price > entry_price else "LOSS")
    else:
        return exit_price, ("WIN" if exit_price < entry_price else "LOSS")

# ========== Bot loop (runs in background thread) ==========
def bot_loop():
    global sent_count_today
    send_telegram("✅ PocketOption signals bot started — M5 strict")
    last_heartbeat = time.time()
    eval_queue = []

    while True:
        try:
            now = datetime.now(timezone.utc)
            hour = now.hour
            # recommended active sessions: London & NewYork overlaps
            in_session = (8 <= hour < 11) or (13 <= hour < 17) or (18 <= hour < 21)
            if in_session:
                for symbol in SYMBOLS:
                    if not can_send(symbol):
                        continue
                    res = analyze_symbol(symbol)
                    if res:
                        # send message
                        msg = (f"📊 Signal {res['type']} | {symbol}\n"
                               f"➡️ {res['signal']} | Prix: {res['price']}\n"
                               f"🔎 Confiance: {res['conf']}%\n"
                               f"🕒 {now.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                               f"⏳ Durée recommandée: {EXPIRATION_MIN} min\n"
                               f"⚠️ Gestion: risk 1-3% du capital")
                        ok = send_telegram(msg)
                        if ok:
                            last_sent[symbol] = datetime.now(timezone.utc)
                            sent_count_today += 1
                            eval_queue.append({"symbol":symbol,"signal":res["signal"],"entry":res["price"],"time":datetime.now(timezone.utc)})
                            save_log(datetime.now(timezone.utc), symbol, res["signal"], res["price"], "", "PENDING", EXPIRATION_MIN)
            # evaluate queued trades after expiry
            remaining = []
            for job in eval_queue:
                elapsed = (datetime.now(timezone.utc) - job["time"]).total_seconds()
                if elapsed >= EXPIRATION_MIN*60:
                    exit_price, result = evaluate_quick(job["symbol"], job["signal"], job["entry"])
                    if exit_price is None:
                        send_telegram(f"⚠️ Unable to evaluate {job['symbol']} {job['signal']}")
                        save_log(job["time"], job["symbol"], job["signal"], job["entry"], "", "UNKNOWN", EXPIRATION_MIN)
                    else:
                        send_telegram(f"📣 Result {job['symbol']} | {job['signal']} -> {result} (entry {job['entry']} -> exit {exit_price})")
                        save_log(job["time"], job["symbol"], job["signal"], job["entry"], exit_price, result, EXPIRATION_MIN)
                else:
                    remaining.append(job)
            eval_queue = remaining

            # heartbeat
            if time.time() - last_heartbeat >= HEARTBEAT_SECONDS:
                send_telegram(f"✅ Bot active — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
                last_heartbeat = time.time()

        except Exception as e:
            print("Bot loop error:", e)
        time.sleep(CHECK_INTERVAL)

# Start background thread when the module is loaded by gunicorn
def start_bot_background():
    t = Thread(target=bot_loop, daemon=True)
    t.start()

# If run locally, also start thread and run flask
if __name__ == "__main__":
    start_bot_background()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
# When deployed on Render, run with: gunicorn main:app
else:
    # when gunicorn imports main, start the thread
    start_bot_background()￼Enter
