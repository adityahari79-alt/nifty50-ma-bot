import streamlit as st
import pandas as pd
import asyncio
import websockets
import json, os
from datetime import datetime, timedelta
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
st.set_page_config(page_title="Nifty50 MA Bot (Stateful)", layout="wide")
st.title("📈 Nifty 50 MA Bot — WebSocket Feed with Persistent State")

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

# ===== WebSocket Constants =====
WS_URL = "wss://api-feed.dhan.co?version=2&token={}&clientId={}&authType=2"

# ===== Load Previous State =====
if "candles" not in st.session_state:
    st.session_state.candles = []
    st.session_state.position = None
    st.session_state.traded_candle = None
    load_state()  # try to restore from file

# ===== Candle Handling =====
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
        # close previous candle
        candles.append({"timestamp": start, "open": price, "high": price, "low": price, "close": price})
    else:
        c = candles[-1]
        c['high'] = max(c['high'], price)
        c['low'] = min(c['low'], price)
        c['close'] = price
    st.session_state.candles = candles

# ===== Async Bot =====
async def bot_loop(dhan):
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
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps(sub_msg))
                status_box.info("✅ Subscribed to Nifty WebSocket feed.")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except:
                        continue
                    if 'data' not in msg:
                        continue
                    for t in msg['data']:
                        try:
                            ts = datetime.fromtimestamp(t['time']/1000)
                            ltp = float(t['lastTradedPrice'])
                        except:
                            continue

                        # Update candles and save state
                        update_candles(ts, ltp)
                        save_state()

                        df = pd.DataFrame(st.session_state.candles)
                        if len(df) < 21:
                            continue
                        df['ma10'] = df['close'].rolling(10).mean()
                        df['ma21'] = df['close'].rolling(21).mean()
                        last = df.iloc[-2]  # completed candle

                        # Entry condition
                        if (last['ma10'] >= last['ma21'] 
                            and st.session_state.traded_candle != last['timestamp'] 
                            and not st.session_state.position):
                            strike = round_strike(last['close']) - 200
                            opt_id = find_deep_itm_ce(dhan, nifty_security_id, expiry_date, strike)
                            if not opt_id:
                                continue
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
                                except:
                                    continue
                            st.session_state.position = {
                                'option_id': opt_id, 'entry_price': entry,
                                'sl_price': entry * 0.95, 'max_price': entry
                            }
                            st.session_state.traded_candle = last['timestamp']
                            save_state()

                        # Manage trailing SL
                        if st.session_state.position:
                            try:
                                if paper_mode:
                                    ltp_opt = st.session_state.position['max_price'] + 1
                                else:
                                    q = dhan.security_quote(dhan.NSE_FNO, st.session_state.position['option_id'])
                                    ltp_opt = float(q['last_price'])
                            except:
                                continue

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
                                    except:
                                        continue
                                pnl = (exit_p - st.session_state.position['entry_price']) * lot_size
                                pnl_box.success(f"Trade exited. P&L={pnl}")
                                st.session_state.position = None
                                save_state()

        except Exception as e:
            status_box.warning(f"WebSocket disconnected: {e} — reconnect in {reconnect_delay}s")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)

# ===== Run Bot =====
if start_bot:
    if not (client_id and access_token and nifty_security_id and expiry_date):
        st.error("Fill all API details")
    else:
        dhan = dhanhq(client_id, access_token)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot_loop(dhan))
