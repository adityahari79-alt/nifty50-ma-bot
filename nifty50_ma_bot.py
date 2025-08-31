import streamlit as st
import pandas as pd
import asyncio
from datetime import datetime, timedelta
from dhanhq import dhanhq
import json
import os

STATE_FILE = "bot_state.json"

def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({
            "candles": st.session_state.candles,
            "position": st.session_state.position,
            "traded_candle": str(st.session_state.traded_candle) if st.session_state.traded_candle else None
        }, f)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            st.session_state.candles = [
                {**c, "timestamp": pd.to_datetime(c["timestamp"])} for c in data.get("candles", [])
            ]
            st.session_state.position = data.get("position", None)
            traded = data.get("traded_candle")
            st.session_state.traded_candle = pd.to_datetime(traded) if traded else None
    else:
        st.session_state.candles = []
        st.session_state.position = None
        st.session_state.traded_candle = None

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
    start = ts - timedelta(minutes=ts.minute % minutes, seconds=ts.second, microseconds=ts.microsecond)
    if not candles or candles[-1]['timestamp'] != start:
        candles.append({"timestamp": start, "open": price, "high": price, "low": price, "close": price})
    else:
        c = candles[-1]
        c['high'] = max(c['high'], price)
        c['low'] = min(c['low'], price)
        c['close'] = price
    st.session_state.candles = candles

async def on_tick(tick, dhan, lot_size, expiry_date, nifty_security_id, paper_mode, trade_log, status_box, pnl_box):
    try:
        ts = datetime.fromtimestamp(tick['time'] / 1000)
        ltp = float(tick['lastTradedPrice'])
    except:
        return

    update_candles(ts, ltp)
    save_state()

    df = pd.DataFrame(st.session_state.candles)
    if len(df) < 21:
        return

    df['ma10'] = df['close'].rolling(10).mean()
    df['ma21'] = df['close'].rolling(21).mean()
    last = df.iloc[-2]

    if last['ma10'] >= last['ma21'] and st.session_state.traded_candle != last['timestamp'] and not st.session_state.position:
        strike = round_strike(last['close']) - 200
        opt_id = find_deep_itm_ce(dhan, nifty_security_id, expiry_date, strike)
        if not opt_id:
            return

        if paper_mode:
            entry_price = last['close']
            trade_log.write(f"[PAPER] Bought {strike} CE @ {entry_price}")
        else:
            try:
                order = dhan.place_order(
                    security_id=opt_id,
                    exchange_segment=dhan.NSE_FNO,
                    transaction_type=dhan.BUY,
                    quantity=lot_size,
                    order_type=dhan.MARKET,
                    product_type=dhan.INTRA,
                    price=0
                )
                entry_price = float(order['order_legs'][0]['traded_price'])
                trade_log.write(f"Bought {strike} CE @ {entry_price}")
            except Exception as e:
                status_box.error(f"Buy failed: {e}")
                return

        st.session_state.position = {
            'option_id': opt_id,
            'entry_price': entry_price,
            'sl_price': entry_price * 0.95,
            'max_price': entry_price
        }
        st.session_state.traded_candle = last['timestamp']
        save_state()

    if st.session_state.position:
        try:
            if paper_mode:
                ltp_opt = st.session_state.position['max_price'] + 1
            else:
                quote = dhan.security_quote(dhan.NSE_FNO, st.session_state.position['option_id'])
                ltp_opt = float(quote['last_price'])
        except:
            return

        if ltp_opt > st.session_state.position['max_price']:
            st.session_state.position['max_price'] = ltp_opt
            st.session_state.position['sl_price'] = max(st.session_state.position['sl_price'], ltp_opt * 0.95)
            save_state()

        if ltp_opt <= st.session_state.position['sl_price']:
            if paper_mode:
                exit_price = ltp_opt
            else:
                try:
                    sell_order = dhan.place_order(
                        security_id=st.session_state.position['option_id'],
                        exchange_segment=dhan.NSE_FNO,
                        transaction_type=dhan.SELL,
                        quantity=lot_size,
                        order_type=dhan.MARKET,
                        product_type=dhan.INTRA,
                        price=0
                    )
                    exit_price = float(sell_order['order_legs'][0]['traded_price'])
                except Exception as e:
                    status_box.error(f"Exit failed: {e}")
                    return

            pnl = (exit_price - st.session_state.position['entry_price']) * lot_size
            pnl_box.success(f"Trade exited. P&L = {pnl}")
            st.session_state.position = None
            save_state()

async def run_bot(dhan, nifty_security_id, expiry_date, lot_size, paper_mode, trade_log, status_box, pnl_box):
    reconnect_delay = 5
    while True:
        try:
            async for tick in dhan.market_feed(nifty_security_id):
                await on_tick(tick, dhan, lot_size, expiry_date, nifty_security_id, paper_mode, trade_log, status_box, pnl_box)
            reconnect_delay = 5
        except Exception as e:
            status_box.warning(f"Market feed error: {e}, reconnecting in {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)

def main():
    st.set_page_config(page_title="Nifty50 MA Bot", layout="wide")
    st.title("📈 Nifty 50 MA Bot with dhanhq Market Feed")

    client_id = st.sidebar.text_input("Dhan Client ID", type="password")
    access_token = st.sidebar.text_input("Access Token", type="password")
    nifty_security_id = st.sidebar.text_input("Nifty 50 Security ID (e.g., 13)")
    expiry_date = st.sidebar.text_input("Option Expiry Date (YYYY-MM-DD)")
    lot_size = st.sidebar.number_input("Lot Size", value=50, min_value=1)
    paper_mode = st.sidebar.checkbox("Paper Mode (No real orders)", True)
    start_bot = st.sidebar.button("🚀 Start Bot")

    status_box = st.empty()
    trade_log = st.empty()
    pnl_box = st.empty()

    if "candles" not in st.session_state:
        load_state()

    if start_bot:
        if not all([client_id, access_token, nifty_security_id, expiry_date]):
            st.error("Please fill all API & config fields.")
            return
        dhan = dhanhq(client_id, access_token)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            run_bot(dhan, nifty_security_id, expiry_date, lot_size, paper_mode, trade_log, status_box, pnl_box)
        )

if __name__ == "__main__":
    main()
