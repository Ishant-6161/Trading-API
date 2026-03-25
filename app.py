from flask import Flask, request, jsonify
from dotenv import load_dotenv
import requests
import time
import hmac
import hashlib
import os
import json
import threading

# ===== LOAD ENV =====
load_dotenv()

API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
BASE_URL   = "https://api.india.delta.exchange"

# ===== WEBHOOK SECRET (add WEBHOOK_SECRET=yourtoken to .env) =====
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")   # empty = disabled

app = Flask(__name__)

# ===== CONSTANTS =====
PRODUCT_ID     = 27     # BTCUSD Perpetual Futures
HARD_SL_PTS    = 500    # Emergency hard stop loss distance in pts ($)
MAX_SL_RETRIES = 3      # Retry attempts for SL placement

# ===== GLOBAL STATE =====
state_lock        = threading.Lock()
current_position  = None    # "BUY" | "SELL" | None
entry_price       = None    # Average fill price of entry order
hard_sl_order_id  = None    # Delta order ID of the 500pt safety SL
trail_sl_order_id = None    # Delta order ID of the trailing SL
trail_sl_price    = None    # Current trailing SL price level
monitor_thread    = None    # Background trailing SL thread


# ===== SIGNATURE =====
def generate_signature(secret, message):
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


# ===== AUTH HEADERS =====
def get_auth_headers(method, path, body_json=""):
    timestamp = str(int(time.time()))
    message   = method + timestamp + path + body_json
    signature = generate_signature(API_SECRET, message)
    return {
        "api-key":      API_KEY,
        "timestamp":    timestamp,
        "signature":    signature,
        "Content-Type": "application/json"
    }


# ===== GET MARK PRICE =====
def get_mark_price():
    try:
        url  = BASE_URL + "/v2/tickers?contract_types=perpetual_futures&symbol=BTCUSD"
        resp = requests.get(url, headers={"Accept-Encoding": "gzip, deflate"}, timeout=5)
        for ticker in resp.json().get("result", []):
            if ticker.get("symbol") == "BTCUSD":
                return float(ticker["mark_price"])
    except Exception as e:
        print(f"⚠️ get_mark_price error: {e}")
    return None


# ===== GET OPEN POSITION FROM DELTA =====
def get_open_position():
    """Returns the open BTCUSD position dict from Delta, or None."""
    try:
        path    = "/v2/positions/margined"
        headers = get_auth_headers("GET", path)
        resp    = requests.get(BASE_URL + path, headers=headers, timeout=5)
        for pos in resp.json().get("result", []):
            if pos.get("product_id") == PRODUCT_ID and int(pos.get("size", 0)) != 0:
                return pos
    except Exception as e:
        print(f"⚠️ get_open_position error: {e}")
    return None


# ===== GET OPEN STOP ORDERS FROM DELTA =====
def get_open_stop_orders():
    """Returns list of open stop loss orders for BTCUSD."""
    try:
        path    = f"/v2/orders?product_id={PRODUCT_ID}&state=open&stop_order_type=stop_loss_order"
        headers = get_auth_headers("GET", path)
        resp    = requests.get(BASE_URL + path, headers=headers, timeout=5)
        return resp.json().get("result", [])
    except Exception as e:
        print(f"⚠️ get_open_stop_orders error: {e}")
    return []


# ===== CANCEL ORDER =====
def cancel_order(order_id):
    """Cancel a single order by ID. Always call OUTSIDE state_lock."""
    if not order_id:
        return
    try:
        path      = f"/v2/orders/{order_id}"
        body_json = json.dumps({"product_id": PRODUCT_ID})
        headers   = get_auth_headers("DELETE", path, body_json)
        resp      = requests.delete(BASE_URL + path, headers=headers, data=body_json, timeout=5)
        data      = resp.json()
        if data.get("success"):
            print(f"🗑️  Cancelled order {order_id}")
        else:
            print(f"⚠️  Cancel failed {order_id}: {data}")
    except Exception as e:
        print(f"⚠️  cancel_order error: {e}")


# ===== PLACE STOP LOSS ORDER (with retries) =====
def place_stop_loss(stop_px, position_side, label="SL"):
    """
    Places a reduce-only stop loss order.
    Returns order_id on success, None on failure.
    Always call OUTSIDE state_lock.
    """
    sl_side   = "sell" if position_side == "BUY" else "buy"
    path      = "/v2/orders"
    method    = "POST"

    body = {
        "product_id":      PRODUCT_ID,
        "size":            1,
        "side":            sl_side,
        "order_type":      "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price":      str(round(float(stop_px), 1)),  # type: ignore[arg-type]
        "reduce_only":     True,
        "time_in_force":   "gtc"
    }
    body_json = json.dumps(body)

    for attempt in range(1, MAX_SL_RETRIES + 1):
        try:
            headers = get_auth_headers(method, path, body_json)
            resp    = requests.post(BASE_URL + path, headers=headers, data=body_json, timeout=5)
            data    = resp.json()
            if data.get("success"):
                oid = data["result"]["id"]
                print(f"🛡️  {label} placed at {stop_px} (id: {oid})")
                return oid
            else:
                print(f"❌ {label} attempt {attempt} failed: {data}")
        except Exception as e:
            print(f"⚠️  {label} attempt {attempt} exception: {e}")
        time.sleep(1)

    print(f"🚨 CRITICAL: Could not place {label} after {MAX_SL_RETRIES} attempts!")
    return None


# ===== CLOSE POSITION =====
def close_position(position_side):
    """Close the open position with a reduce-only market order. Call OUTSIDE state_lock."""
    path       = "/v2/orders"
    method     = "POST"
    close_side = "sell" if position_side == "BUY" else "buy"

    body = {
        "product_id":    PRODUCT_ID,
        "size":          1,
        "side":          close_side,
        "order_type":    "market_order",
        "reduce_only":   True,
        "time_in_force": "gtc"
    }
    body_json = json.dumps(body)
    headers   = get_auth_headers(method, path, body_json)
    resp      = requests.post(BASE_URL + path, headers=headers, data=body_json, timeout=5)
    data      = resp.json()
    print(f"📤 CLOSE ORDER: {body}")
    print(f"📥 RESPONSE:    {data}")
    return data


# ===== RESET STATE (call with state_lock held) =====
def reset_state():
    """Clear all position tracking state. MUST be called with state_lock held."""
    global current_position, entry_price, hard_sl_order_id, trail_sl_order_id, trail_sl_price
    current_position  = None
    entry_price       = None
    hard_sl_order_id  = None
    trail_sl_order_id = None
    trail_sl_price    = None


# ===== CANCEL ALL SL ORDERS (call OUTSIDE state_lock) =====
def cancel_all_sl():
    """
    Cancel both hard SL and trailing SL orders.
    Reads IDs under lock, makes HTTP calls outside lock, clears IDs under lock.
    """
    global hard_sl_order_id, trail_sl_order_id

    # FIX #8: Read IDs under lock, then cancel outside lock
    with state_lock:
        h_id = hard_sl_order_id
        t_id = trail_sl_order_id

    cancel_order(h_id)   # HTTP call outside lock
    cancel_order(t_id)   # HTTP call outside lock

    with state_lock:
        hard_sl_order_id  = None
        trail_sl_order_id = None


# ===== TRAILING SL MONITOR THREAD =====
def monitor_trailing_sl():
    """
    Background thread:
    1. Detects if position was closed externally (hard SL hit) → cancels trailing SL + resets
    2. Step trailing SL:
       - profit >= 100 pts → SL to breakeven
       - each +100 pts after → SL +50 pts further
    All HTTP calls happen OUTSIDE state_lock.
    """
    global trail_sl_order_id, trail_sl_price

    print("🔁 Trailing SL monitor started")

    while True:
        # FIX #1,11: Read state under lock, then do HTTP calls outside
        with state_lock:
            pos           = current_position
            ep            = entry_price
            current_trail = trail_sl_price
            t_id          = trail_sl_order_id

        if pos is None:
            break

        try:
            # ── Check if position still exists on Delta (HTTP outside lock) ──
            live_pos = get_open_position()
            if live_pos is None:
                print("⚠️  Position closed externally (hard SL likely triggered)")
                cancel_order(t_id)   # FIX #1: HTTP outside lock
                with state_lock:
                    reset_state()
                break

            # ── Get price (HTTP outside lock) ─────────────────────────────
            mark = get_mark_price()
            if mark is None or ep is None:
                time.sleep(5)
                continue

            # Assert for type narrowing (both checked above)
            assert mark is not None
            assert ep is not None

            profit_pts = (mark - ep) if pos == "BUY" else (ep - mark)
            print(f"📊 Mark: {mark:.1f} | Entry: {ep:.1f} | P&L: {profit_pts:+.1f} pts | Trail SL: {current_trail}")

            # ── Step trailing SL logic ────────────────────────────────────
            if profit_pts >= 100:
                steps     = int((profit_pts - 100) / 100)
                sl_offset = steps * 50
                target_sl = (ep + sl_offset) if pos == "BUY" else (ep - sl_offset)

                should_update = (
                    current_trail is None or
                    (pos == "BUY"  and target_sl > current_trail) or
                    (pos == "SELL" and target_sl < current_trail)
                )

                if should_update:
                    print(f"📈 Moving trail SL to {target_sl:.1f}")
                    # FIX #11: All HTTP calls outside lock
                    cancel_order(t_id)
                    new_oid = place_stop_loss(target_sl, pos, label="Trail SL")
                    with state_lock:
                        trail_sl_order_id = new_oid
                        trail_sl_price    = target_sl

        except Exception as e:
            print(f"⚠️  Monitor error: {e}")

        time.sleep(5)

    print("🛑 Trailing SL monitor stopped")


# ===== PLACE ENTRY ORDER =====
def place_order(side):
    """
    Places market entry, stores entry price, places hard SL, starts monitor.
    Call OUTSIDE state_lock.
    """
    global entry_price, hard_sl_order_id, trail_sl_order_id, trail_sl_price, monitor_thread

    path      = "/v2/orders"
    method    = "POST"
    body      = {
        "product_id":    PRODUCT_ID,
        "size":          1,
        "side":          side.lower(),
        "order_type":    "market_order",
        "time_in_force": "gtc"
    }
    body_json = json.dumps(body)
    headers   = get_auth_headers(method, path, body_json)

    resp = requests.post(BASE_URL + path, headers=headers, data=body_json, timeout=5)
    data = resp.json()

    print(f"\n📤 ORDER SENT: {body}")
    print(f"📥 RESPONSE:   {data}")

    if data.get("success"):
        avg_fill = data["result"].get("average_fill_price")
        if avg_fill:
            ep       = float(avg_fill)
            pos_side = "BUY" if side.lower() == "buy" else "SELL"

            # Place 500pt hard SL (HTTP outside lock)
            hard_sl_px = (ep - HARD_SL_PTS) if side.lower() == "buy" else (ep + HARD_SL_PTS)
            h_id       = place_stop_loss(hard_sl_px, pos_side, label="Hard SL")

            with state_lock:
                entry_price       = ep
                trail_sl_price    = None
                trail_sl_order_id = None
                hard_sl_order_id  = h_id

            # FIX #7: Only start monitor if not already running
            if monitor_thread is None or not monitor_thread.is_alive():
                monitor_thread = threading.Thread(target=monitor_trailing_sl, daemon=True)
                monitor_thread.start()
            else:
                print("ℹ️  Monitor thread already running")
        else:
            print("⚠️  No fill price returned — SL & monitor NOT started")
    else:
        print(f"❌ Order failed: {data}")

    return data


# ===== STARTUP POSITION RECOVERY =====
def recover_state_on_startup():
    """On startup, check Delta for open positions and restore state."""
    global current_position, entry_price, hard_sl_order_id, trail_sl_order_id, monitor_thread

    print("🔍 Checking for open positions on Delta...")
    pos = get_open_position()   # HTTP outside lock — startup is single-threaded

    if pos is None:
        print("✅ No open position found. Starting fresh.")
        return

    size          = int(pos.get("size", 0))
    entry_val     = float(pos.get("entry_price", 0))
    recovered_side = "BUY" if size > 0 else "SELL"   # FIX #6: removed unused pos_side_raw

    with state_lock:
        current_position = recovered_side
        entry_price      = entry_val

    print(f"⚠️  RECOVERED: {recovered_side} @ {entry_val}")

    # Recover existing SL order IDs
    stop_orders = get_open_stop_orders()   # HTTP outside lock
    for o in stop_orders:
        sp   = float(o.get("stop_price", 0))
        diff = abs(sp - entry_val)
        with state_lock:
            if hard_sl_order_id is None and diff >= 400:
                hard_sl_order_id = o["id"]
                print(f"🛡️  Recovered Hard SL: id={o['id']} at {sp}")
            elif trail_sl_order_id is None:
                trail_sl_order_id = o["id"]
                print(f"📈 Recovered Trail SL: id={o['id']} at {sp}")

    # FIX #7: Check before starting thread
    if monitor_thread is None or not monitor_thread.is_alive():
        monitor_thread = threading.Thread(target=monitor_trailing_sl, daemon=True)
        monitor_thread.start()


# ===== WEBHOOK =====
@app.route('/webhook', methods=['POST'])
def webhook():
    # FIX #10: removed trail_sl_price from global (never written here)
    global current_position

    data = request.json
    if data is None:
        return jsonify({"error": "no JSON"}), 400

    # ── Webhook secret check ────────────────────────────────────────────
    if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
        print("🔒 Unauthorized webhook attempt blocked")
        return jsonify({"error": "unauthorized"}), 403

    print(f"\n📩 Received Signal: {data}")
    signal = data.get("signal")

    # FIX #9: Read state under lock FIRST, then verify against Delta outside lock
    with state_lock:
        pos = current_position

    # Verify live state — HTTP call outside lock
    live_pos = get_open_position()
    with state_lock:
        if live_pos is None and current_position is not None:
            print("⚠️  State mismatch: bot has position but Delta shows none. Resetting.")
            reset_state()
        pos = current_position   # re-read after potential reset

    # ── EXIT ─────────────────────────────────────────────────────────────
    if signal == "EXIT":
        if pos is None:
            print("⚠️  EXIT received but no open position")
            return jsonify({"status": "no position to exit"})

        print(f"🚪 Exiting {pos} position")
        # FIX #3: All HTTP calls outside lock
        cancel_all_sl()
        close_position(pos)
        with state_lock:
            reset_state()

        return jsonify({"status": "EXIT executed"})

    # ── BUY ──────────────────────────────────────────────────────────────
    if signal == "BUY":
        if pos == "SELL":
            print("🔄 Reversing SELL → BUY")
            # FIX #4: HTTP calls outside lock
            cancel_all_sl()
            place_order("buy")
            with state_lock:
                current_position = "BUY"
        elif pos is None:
            print("🟢 Opening BUY")
            place_order("buy")
            with state_lock:
                current_position = "BUY"
        else:
            print("⚠️  Already in BUY, ignoring")
            return jsonify({"status": "already in BUY"})
        return jsonify({"status": "BUY executed"})

    # ── SELL ─────────────────────────────────────────────────────────────
    if signal == "SELL":
        if pos == "BUY":
            print("🔄 Reversing BUY → SELL")
            # FIX #5: HTTP calls outside lock
            cancel_all_sl()
            place_order("sell")
            with state_lock:
                current_position = "SELL"
        elif pos is None:
            print("🔴 Opening SELL")
            place_order("sell")
            with state_lock:
                current_position = "SELL"
        else:
            print("⚠️  Already in SELL, ignoring")
            return jsonify({"status": "already in SELL"})
        return jsonify({"status": "SELL executed"})

    return jsonify({"status": "signal ignored"})


# ===== MAIN =====
if __name__ == '__main__':
    print("\n🚀 STARTING BOT...\n")
    recover_state_on_startup()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)