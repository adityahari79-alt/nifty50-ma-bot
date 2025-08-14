# nifty50_ma_bot.py
# Streamlit + DhanHQ API Bot for Nifty 50 CALL options based on MA crossover
# Please TEST in paper trading mode before using real money.

import streamlit as st
import pandas as pd
import time
from dhanhq import DhanContext, dhanhq  # ✅ Correct import

# ===== Streamlit Page Config =====
st.set_page_config(page_title="Nifty50 MA Options Bot", layout="wide")
st.title("📈 Nifty 50 Moving Average Call Option Bot (Dhan API)")

# ===== Sidebar Inputs =====
st.sidebar.header("🔑 Dhan API & Config")
client_id = st.sidebar.text_input("Dhan Client ID", type="password")
access_token = st.sidebar.text_input("Access Token", type="password")
nifty_security_id = st.sidebar.text_input("Nifty 50 Security ID (Spot)", help="e.g., 13 for Nifty spot index")
expiry_date = st.sidebar.text_input("Option Expiry Date (YYYY-MM-DD)")
lot_size = st.sidebar.number_input("Lot Size", value=50, min_value=1)

run_bot = st.sidebar.button("🚀 Start Bot")

# ===== Placeholders =====
status_box = st.empty()
trade_log = st.container()
pnl_box = st.empty()

# ===== Helper Functions =====
def calculate_ma(series, period):
    return series.rolling(window=period).mean()

def fetch_5min_candles(dhan, sec_id):
    """ Fetch latest intraday data for Nifty 5-min candles """
    data = dhan.intraday_minute_data(
        security_id=sec_id,
        exchange_segment=dhan.NSE,
        instrument_type="IDX_I"
    )
    df = pd.DataFrame(data)
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    return df

def find_deep_itm_ce(dhan, underlying_id, expiry, target_strike):
    """ Find option security_id for Deep ITM CE """
    chain = dhan.option_chain(
        under_security_id=underlying_id,
        under_exchange_segment="IDX_I",
        expiry=expiry
    )
    for opt in chain:
        if (opt['strike_price'] == target_strike) and (opt['option_type'].upper() == "CE"):
            return opt['security_id']
    return None

# ===== BOT LOOP =====
if run_bot:
    if not (client_id and access_token and nifty_security_id and expiry_date):
        st.error("Please fill all API & config fields before starting.")
    else:
        try:
            dhan_context = DhanContext(client_id, access_token)  # ✅ Correct connection
            dhan = dhanhq(dhan_context)
            status_box.info("✅ Connected to Dhan API. Starting strategy loop...")
        except Exception as e:
            st.error(f"❌ Failed to connect to Dhan API: {e}")
            st.stop()

        traded_candle = None

        while True:
            try:
                df = fetch_5min_candles(dhan, nifty_security_id)
            except Exception as e:
                status_box.error(f"Failed to fetch candles: {e}")
                time.sleep(10)
                continue

            if len(df) < 22:
                status_box.warning("Not enough data for MAs yet. Waiting...")
                time.sleep(60)
                continue

            df['ma10'] = calculate_ma(df['close'], 10)
            df['ma21'] = calculate_ma(df['close'], 21)

            last_candle = df.iloc[-1]
            current_time = last_candle['timestamp']
            spot = last_candle['close']

            if traded_candle == current_time:
                status_box.info("Already traded this candle. Waiting for next...")
                time.sleep(60)
                continue

            # ===== MA Condition =====
            if last_candle['ma10'] >=
