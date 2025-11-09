# --- Bybit Analyzer PRO (versión estable 2025 con API v5, corrección total) ---
import time
import json
import os
from datetime import datetime
import requests

# ---- CONFIG ----
BYBIT_BASE = "https://api.bybit.com"
TELEGRAM_CHAT_ID = "1077062543"
TELEGRAM_TOKEN = "8213474884:AAHZ3bUIDybSZLX6nlAYzf9xq4iVyqVSlcI"
SENT_SIGNALS_FILE = "sent_signals.txt"

MA_WINDOWS = [20, 50]
ATR_WINDOW = 14
VOL_WINDOW = 20

# ==== Control de señales ====
def load_sent_signals():
    if os.path.exists(SENT_SIGNALS_FILE):
        with open(SENT_SIGNALS_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_sent_signal(signal_id):
    with open(SENT_SIGNALS_FILE, "a", encoding="utf-8") as f:
        f.write(signal_id + "\n")

sent_signals = load_sent_signals()

# ==== API Bybit ====
def get_with_retry(url, params=None, retries=3, timeout=10):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"[get_with_retry] intento {i+1} fallo: {e}")
            time.sleep(1 + i)
    return None

def fetch_klines(symbol, interval="5", limit=200):
    url = f"{BYBIT_BASE}/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
    data = get_with_retry(url, params)
    if not data or "result" not in data:
        return []
    res = data["result"]
    return res.get("list", []) if isinstance(res, dict) else []

def fetch_funding_rate(symbol):
    url = f"{BYBIT_BASE}/v5/market/funding/history"
    params = {"category": "linear", "symbol": symbol, "limit": 1}
    data = get_with_retry(url, params)
    if not data or "result" not in data:
        return None
    try:
        return float(data["result"]["list"][0]["fundingRate"])
    except:
        return None

def fetch_orderbook(symbol, limit=50):
    url = f"{BYBIT_BASE}/v5/market/orderbook"
    params = {"category": "linear", "symbol": symbol, "limit": limit}
    return get_with_retry(url, params)

def fetch_trades(symbol, limit=200):
    url = f"{BYBIT_BASE}/v5/market/recent-trade"
    params = {"category": "linear", "symbol": symbol, "limit": limit}
    return get_with_retry(url, params)

# ==== Indicadores ====
def sma(values, window):
    return sum(values[-window:]) / window if len(values) >= window else None

def compute_atr(klines, window=14):
    tr = []
    for i in range(1, len(klines)):
        high, low, prev_close = float(klines[i][2]), float(klines[i][3]), float(klines[i-1][4])
        tr.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(tr[-window:]) / window if len(tr) >= window else None

def orderbook_imbalance(book_json):
    """Corrige cualquier formato del orderbook devuelto por Bybit v5"""
    if not book_json or "result" not in book_json:
        return 0
    bids, asks = 0.0, 0.0
    try:
        data = book_json["result"]
        if isinstance(data, dict) and "list" in data:
            data = data["list"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except:
                print("[orderbook_imbalance] formato texto inválido")
                return 0
        for entry in data:
            if isinstance(entry, list) and len(entry) >= 3:
                side = str(entry[0]).lower()
                size = float(entry[2]) if entry[2] else 0
            elif isinstance(entry, dict):
                side = entry.get("side", "").lower()
                size = float(entry.get("size", entry.get("qty", 0)) or 0)
            else:
                continue
            if "buy" in side:
                bids += size
            elif "sell" in side:
                asks += size
    except Exception as e:
        print(f"[orderbook_imbalance] parse error final: {e}")
        return 0
    if bids + asks == 0:
        return 0
    return (bids - asks) / (bids + asks)

def compute_cvd(trades_json):
    if not trades_json or "result" not in trades_json:
        return 0
    res = trades_json["result"]
    if isinstance(res, dict) and "list" in res:
        res = res["list"]
    delta = 0.0
    for t in res:
        side = str(t.get("side", "")).lower()
        size = float(t.get("size", t.get("qty", 0)) or 0)
        delta += size if "buy" in side else -size
    return delta

def detect_liquidity_sweep(klines):
    if len(klines) < 6:
        return False, None
    last = klines[-1]
    o, h, l, c, v = float(last[1]), float(last[2]), float(last[3]), float(last[4]), float(last[5])
    vol_avg = sum(float(k[5]) for k in klines[-6:-1]) / 5
    body, upper, lower = abs(c - o), h - max(c, o), min(c, o) - l
    if upper > body * 2 and v > 2 * vol_avg:
        return True, "upper"
    if lower > body * 2 and v > 2 * vol_avg:
        return True, "lower"
    return False, None

# ==== Evaluación de filtros ====
def evaluate_filters(symbol):
    kl1 = fetch_klines(symbol, "1", 200)
    kl5 = fetch_klines(symbol, "5", 200)
    trades = fetch_trades(symbol)
    book = fetch_orderbook(symbol)
    funding = fetch_funding_rate(symbol)

    closes5 = [float(k[4]) for k in kl5] if kl5 else []
    ma_s = sma(closes5, MA_WINDOWS[0])
    ma_l = sma(closes5, MA_WINDOWS[1])
    atr = compute_atr(kl5, ATR_WINDOW) if kl5 else None
    vol_now = float(kl1[-1][5]) if kl1 else 0
    vol_avg = sum(float(k[5]) for k in kl1[-VOL_WINDOW:]) / VOL_WINDOW if len(kl1) >= VOL_WINDOW else 1
    vol_ratio = vol_now / vol_avg if vol_avg else 1
    ob_imb = orderbook_imbalance(book)
    cvd = compute_cvd(trades)
    sweep, sweep_dir = detect_liquidity_sweep(kl1)

    score, reasons = 0, []
    if ma_s and ma_l:
        score += 1
        reasons.append("Tendencia alcista" if ma_s > ma_l else "Tendencia bajista")
    if abs(ob_imb) > 0.1:
        score += 1
        reasons.append(f"Orderbook imbalance {ob_imb:.2f}")
    if vol_ratio > 1.5:
        score += 1
        reasons.append(f"Volumen alto ({vol_ratio:.2f}x)")
    if cvd != 0:
        score += 1
        reasons.append(f"Delta CVD {'positivo' if cvd > 0 else 'negativo'}")
    if (ob_imb > 0.05 and cvd > 0) or (ob_imb < -0.05 and cvd < 0):
        score += 1
        reasons.append("OI + CVD alineados")
    if abs(ob_imb) > 0.2:
        score += 1
        reasons.append("Desequilibrio fuerte en el libro")
    if sweep:
        score += 1
        reasons.append(f"Barrida de liquidez ({sweep_dir})")
    if funding is not None:
        score += 0.5
        reasons.append(f"Funding {'positivo' if funding > 0 else 'negativo'} ({funding:.6f})")
    if atr and closes5:
        last_price = closes5[-1]
        if atr / last_price > 0.005:
            score += 1
            reasons.append("Alta volatilidad (ATR alto)")

    direction = "NEUTRAL"
    if ma_s and ma_l:
        if ma_s > ma_l and (cvd > 0 or ob_imb > 0):
            direction = "LONG"
        elif ma_s < ma_l and (cvd < 0 or ob_imb < 0):
            direction = "SHORT"

    probability = min(100, int((score / 9.5) * 100))
    return {"symbol": symbol, "score": score, "probability": probability, "direction": direction, "reasons": reasons}

# ==== Telegram ====
def send_signal_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.get(url, params={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    except Exception as e:
        print(f"[send_signal_telegram] fallo: {e}")

def process_and_send(symbol, metrics):
    sid = f"{symbol}_{metrics['direction']}_{metrics['probability']}"
    if metrics["direction"] == "NEUTRAL" or metrics["probability"] < 40:
        print(f"Señal ignorada ({symbol}) - NEUTRAL o prob < 40")
        return
    if sid in sent_signals:
        print(f"⏩ Señal repetida: {sid}")
        return
    msg = (
        f"⚡ Señal detectada en {symbol}\n"
        f"📈 Dirección: {metrics['direction']}\n"
        f"🎯 Probabilidad: {metrics['probability']}%\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        + "\n".join(f"• {r}" for r in metrics["reasons"])
    )
    send_signal_telegram(msg)
    sent_signals.add(sid)
    save_sent_signal(sid)

# ==== LOOP PRINCIPAL ====
SYMBOLS = ["BTCUSDT", "ETHUSDT", "HYPEUSDT", "ETCUSDT"]

if __name__ == "__main__":
    send_signal_telegram("🚀 Iniciando Bybit Analyzer PRO (v5 corregido y estable)...")
    while True:
        for sym in SYMBOLS:
            metrics = evaluate_filters(sym)
            print(f"[CHECK] {sym} -> dir:{metrics['direction']} prob:{metrics['probability']} score:{metrics['score']}")
            process_and_send(sym, metrics)
            time.sleep(2)
        print("Esperando 10 minutos para el siguiente análisis...")
        time.sleep(600)
        fix line endings for Linux
