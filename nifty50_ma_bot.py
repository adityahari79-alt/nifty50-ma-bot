import streamlit as st
import pandas as pd
import asyncio
import websockets
import json, os
from datetime import datetime, timedelta
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from dhanhq import dhanhq

# ===== Persistent State File =====
STATE_FILE = "bot_state.json"

def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({
            "candles": st.session_state.candles,
            "position": st.session_state.position,
            "traded_candle": st.session_state.traded_candle
        }, f, default=str)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            # convert timestamps back
            candles = [
                {**c, "timestamp": pd.to_datetime(c["timestamp"])}
                for c in data.get("candles", [])
            ]
            st.session_state.candles = candles
            st.session_state.position = data.get("position", None)
            st.session_state.traded_candle = (
                pd.to_datetime(data["traded_candle"])
                if data.get("traded_candle") else None
            )

# ===== Streamlit Setup =====
st.set_page_config(page_title="Nifty50 MA Bot (State + Reconnect)", layout="wide")
st.title("📈 Nifty 50 MA Bot — WebSocket Feed + Persistent State + Auto Reconnect")

# ===== Inputs =====
st.sidebar.header("API & Config")
client_id = st.sidebar.text_input("Dhan Client ID", type="password")
access_token = st.sidebar.text_input("Access Token", type="password")
nifty_security_id = st.sidebar.text_input("Nifty 50 Security ID", help="e.g., 13 for Spot Index")
expiry_date = st.sidebar.text_input("Option Expiry Date (YYYY-MM-DD)")
lot_size = st.sidebar.number_input("Lot Size", value=50, min_value=1)
paper_mode = st.sidebar.checkbox("Paper Mode", True)
start_bot = st.sidebar.button("🚀 Start Bot")

# ===== UI Placeholders =====
status_box = st.empty()
trade_log = st.empty()
pnl_box = st.empty()

# ===== WebSocket Template =====
WS_URL = "wss://api-feed.dhan.co?version=2&token={}&clientId={}&authType=2"

# ===== Load Previous State =====
if "candles" not in st.session_state:
    st.session_state.candles = []
    st.session_state.position = None
    st.session_state.traded_candle = None
    load_state()

# ===== Helper Functions =====
def round_strike(price, interval=50):
    return int(price // interval * interval)

def find_deep_itm_ce(dhan, underlying_id, expiry, strike):
    oc = dhan.option_chain(under_security_id=underlying_id, under_exchange_segment="IDX_I", expiry=expiry)
    for opt in oc:
        if opt['strike_price'] == strike and opt['option_type'].upper() == "CE":
            return opt['security_id']
    return None

def update_candles(ts, price, minutes=5):
    candles = st.session_state.candles
    start = ts - timedelta(
        minutes=ts.minute % minutes,
        seconds=ts.second,
        microseconds=ts.microsecond
    )
    if not candles or candles[-1]['timestamp'] != start:
        candles.append({"timestamp": start, "open": price, "high": price, "low": price, "close": price})
    else:
        c = candles[-1]
        c['high'] = max(c['high'], price)
        c['low'] = min(c['low'], price)
        c['close'] = price
    st.session_state.candles = candles

# ===== Tick Processor =====
async def on_tick(t, dhan):
    try:
        ts = datetime.fromtimestamp(t['time']/1000)
        ltp = float(t['lastTradedPrice'])
    except:
        return

    # Update candle and save
    update_candles(ts, ltp)
    save_state()

    df = pd.DataFrame(st.session_state.candles)
    if len(df) < 21:
        return
    df['ma10'] = df['close'].rolling(10).mean()
    df['ma21'] = df['close'].rolling(21).mean()
    last = df.iloc[-2]  # last complete

    # Entry condition
    if (last['ma10'] >= last['ma21'] and 
        st.session_state.traded_candle != last['timestamp'] and 
        not st.session_state.position):

        strike = round_strike(last['close']) - 200
        opt_id = find_deep_itm_ce(dhan, nifty_security_id, expiry_date, strike)
        if not opt_id:
            return

        if paper_mode:
            entry = last['close']
            trade_log.write(f"[PAPER] Bought {strike} CE @ {entry}")
        else:
            try:
                o = dhan.place_order(
                    security_id=opt_id,
                    exchange_segment=dhan.NSE_FNO,
                    transaction_type=dhan.BUY,
                    quantity=lot_size,
                    order_type=dhan.MARKET,
                    product_type=dhan.INTRA,
                    price=0
                )
                entry = float(o['order_legs'][0]['traded_price'])
                trade_log.write(f"Bought {strike} CE @ {entry}")
            except Exception as e:
                status_box.error(f"Buy failed: {e}")
                return

        st.session_state.position = {
            'option_id': opt_id, 'entry_price': entry,
            'sl_price': entry * 0.95, 'max_price': entry
        }
        st.session_state.traded_candle = last['timestamp']
        save_state()

    # Manage position
    if st.session_state.position:
        try:
            if paper_mode:
                ltp_opt = st.session_state.position['max_price'] + 1
            else:
                q = dhan.security_quote(dhan.NSE_FNO, st.session_state.position['option_id'])
                ltp_opt = float(q['last_price'])
        except:
            return

        if ltp_opt > st.session_state.position['max_price']:
            st.session_state.position['max_price'] = ltp_opt
            st.session_state.position['sl_price'] = max(st.session_state.position['sl_price'], ltp_opt * 0.95)
            save_state()

        if ltp_opt <= st.session_state.position['sl_price']:
            if paper_mode:
                exit_p = ltp_opt
            else:
                try:
                    sell_o = dhan.place_order(
                        security_id=st.session_state.position['option_id'],
                        exchange_segment=dhan.NSE_FNO,
                        transaction_type=dhan.SELL,
                        quantity=lot_size,
                        order_type=dhan.MARKET,
                        product_type=dhan.INTRA,
                        price=0
                    )
                    exit_p = float(sell_o['order_legs'][0]['traded_price'])
                except Exception as e:
                    status_box.error(f"Exit failed: {e}")
                    return
            pnl = (exit_p - st.session_state.position['entry_price']) * lot_size
            pnl_box.success(f"Trade exited. P&L={pnl}")
            st.session_state.position = None
            save_state()

# ===== Robust WebSocket Loop with Auto-Reconnect =====
async def run_ws_loop(dhan):
    url = WS_URL.format(access_token, client_id)
    sub_msg = {
        "action": "subscribe",
        "requestId": "nifty_sub",
        "instruments": [
            {"exchangeSegment": "NSE", "securityID": nifty_security_id, "dataType": "full"}
        ]
    }
    reconnect_delay = 5

    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=10,
                ping_timeout=5,
                close_timeout=5,
                max_size=2**20
            ) as ws:
                await ws.send(json.dumps(sub_msg))
                status_box.info("✅ Connected & Subscribed to WebSocket feed.")
                reconnect_delay = 5  # reset after success

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if isinstance(msg, dict) and msg.get("message", "").lower() == "ping":
                        await ws.send(json.dumps({"message": "pong"}))
                        continue

                    if "data" in msg:
                        for tick in msg["data"]:
                            await on_tick(tick, dhan)

        except (ConnectionClosedError, ConnectionClosedOK) as e:
            status_box.warning(f"⚠️ WS closed: {e} — reconnect in {reconnect_delay}s")
        except (OSError, asyncio.TimeoutError) as e:
            status_box.warning(f"⚠️ Network/timeout: {e} — reconnect in {reconnect_delay}s")
        except Exception as e:
            status_box.error(f"⚠️ Unexpected error: {e} — reconnect in {reconnect_delay}s")

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)

# ===== Start Bot =====
if start_bot:
    if not (client_id and access_token and nifty_security_id and expiry_date):
        st.error("Fill all API & config fields")
    else:
        dhan = dhanhq(client_id, access_token)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_ws_loop(dhan))
