import streamlit as st
import pandas as pd
import numpy as np
import asyncio
import websockets
import json
from datetime import datetime, timedelta
from dhanhq import dhanhq

# Streamlit Setup
st.set_page_config(page_title="Nifty50 MA Options Bot (WebSocket)", layout="wide")
st.title("📈 Nifty 50 Moving Average Call Option Bot with WebSocket Feed (Dhan API)")

# Sidebar Inputs
st.sidebar.header("API & Config")
client_id = st.sidebar.text_input("Dhan Client ID", type="password")
access_token = st.sidebar.text_input("Access Token", type="password")
nifty_security_id = st.sidebar.text_input("Nifty 50 Security ID (Spot)", help="e.g., 13 for Nifty spot index")
expiry_date = st.sidebar.text_input("Option Expiry Date (YYYY-MM-DD)")
lot_size = st.sidebar.number_input("Lot Size", value=50, min_value=1)
paper_mode = st.sidebar.checkbox("Paper Trading Mode", value=True, help="If ON, no real orders will be placed.")
run_bot = st.sidebar.button("🚀 Start Bot")

# Placeholders
status_box = st.empty()
trade_log = st.empty()
pnl_box = st.empty()

# Constants for WebSocket
WS_URL_TEMPLATE = "wss://api-feed.dhan.co?version=2&token={token}&clientId={client_id}&authType=2"

# Utility Functions

def calculate_ma(series, period):
    return series.rolling(window=period).mean()

def find_deep_itm_ce(dhan, underlying_id, expiry, target_strike):
    chain = dhan.option_chain(
        under_security_id=underlying_id,
        under_exchange_segment="IDX_I",
        expiry=expiry
    )
    for opt in chain:
        if (opt['strike_price'] == target_strike) and (opt['option_type'].upper() == "CE"):
            return opt['security_id']
    return None

def round_to_nearest_strike(price, strike_interval=50):
    return int(price // strike_interval * strike_interval)

# Candle Aggregator Class

class CandleAggregator:
    """
    Aggregates tick data (price, timestamp) into 5-minute candles with OHLC.
    Assumes timestamps are datetime objects.
    """
    def __init__(self, interval_minutes=5):
        self.interval_minutes = interval_minutes
        self.current_candle_start = None
        self.candle_data = []

        # Store completed candles
        self.completed_candles = []

    def update(self, tick_time, price):
        candle_start = tick_time - timedelta(
            minutes=tick_time.minute % self.interval_minutes,
            seconds=tick_time.second,
            microseconds=tick_time.microsecond,
        )
        
        # If starting first candle or new candle period
        if self.current_candle_start != candle_start:
            # Close previous candle
            if self.candle_data:
                o = self.candle_data[0][1]
                h = max(p for _, p in self.candle_data)
                l = min(p for _, p in self.candle_data)
                c = self.candle_data[-1][1]
                candle = {
                    "timestamp": self.current_candle_start,
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c
                }
                self.completed_candles.append(candle)
            # Start new candle
            self.current_candle_start = candle_start
            self.candle_data = []
        # Append current tick to candle data
        self.candle_data.append((tick_time, price))

    def get_candles_df(self):
        # Include current incomplete candle as well
        candles = self.completed_candles.copy()
        if self.candle_data:
            o = self.candle_data[0][1]
            h = max(p for _, p in self.candle_data)
            l = min(p for _, p in self.candle_data)
            c = self.candle_data[-1][1]
            candle = {
                "timestamp": self.current_candle_start,
                "open": o,
                "high": h,
                "low": l,
                "close": c
            }
            candles.append(candle)
        df = pd.DataFrame(candles)
        if not df.empty:
            df['timestamp'] = pd.to_datetime(df['timestamp'])
        return df

# Main Async Trading Logic

async def trading_bot(client_id, access_token, nifty_security_id, expiry_date, lot_size, paper_mode):
    # Initialize dhanhq client
    dhan = dhanhq(client_id, access_token)

    status_box.info(f"✅ Connected to Dhan API. Running in {'PAPER' if paper_mode else 'LIVE'} mode.")

    # Prepare WebSocket URL
    ws_url = WS_URL_TEMPLATE.format(token=access_token, client_id=client_id)

    # Prepare subscription request JSON for Nifty spot live feed
    # Format: [exchangeSegment, securityID, mode], mode 'MINI' or 'FULL' possible (use FULL for detailed data)
    subscribe_message = {
        "action": "subscribe",
        "requestId": "nifty_subscription",
        "instruments": [
            {
                "exchangeSegment": "NSE",
                "securityID": nifty_security_id,
                "dataType": "full"  # full real-time ticks/candles
            }
        ]
    }

    traded_candle = None
    bought_option_id = None
    entry_price = None
    sl_price = None
    max_price = None

    candle_aggregator = CandleAggregator(interval_minutes=5)

    async with websockets.connect(ws_url) as websocket:
        # Authenticate & subscribe
        await websocket.send(json.dumps(subscribe_message))
        status_box.info("Subscribed to Nifty 50 spot live feed.")

        async for message in websocket:
            # Parse incoming message
            try:
                msg = json.loads(message)
            except Exception:
                continue  # ignore malformed

            # Response can contain 'message' or 'data' with list of ticks/candles
            if 'message' in msg:
                # Handle pings/pongs or errors if any
                if msg['message'].lower() == "pong":
                    # Reply to keep connection alive if needed
                    await websocket.send(json.dumps({"action":"pong"}))
                    continue
                # Handle others (subscribe confirmation etc)
                continue

            if 'data' not in msg:
                continue

            # Process each tick
            for tick in msg['data']:
                try:
                    # Get tick time and last traded price
                    tick_time = datetime.fromtimestamp(tick['time']/1000)  # assuming ms timestamp
                    ltp = float(tick['lastTradedPrice'])
                except Exception:
                    continue

                # Update candle aggregator
                candle_aggregator.update(tick_time, ltp)
                candles_df = candle_aggregator.get_candles_df()

                # Only proceed if we have enough candles for MA21
                if len(candles_df) < 21:
                    status_box.info("Waiting for at least 21 candles to calculate MAs...")
                    continue

                # Calculate moving averages on the completed candles only (exclude current incomplete candle)
                ma_df = candles_df.iloc[:-1].copy()
                ma_df['ma10'] = ma_df['close'].rolling(window=10).mean()
                ma_df['ma21'] = ma_df['close'].rolling(window=21).mean()

                # Latest completed candle index
                last_candle = ma_df.iloc[-1]

                # Check if we already traded on this candle
                if traded_candle == last_candle['timestamp']:
                    continue

                # MA Crossover condition
                if last_candle['ma10'] >= last_candle['ma21']:
                    # Determine deep ITM strike
                    spot_price = last_candle['close']
                    atm_strike = round_to_nearest_strike(spot_price)
                    deep_itm_strike = atm_strike - 200  # 200 points deep ITM

                    status_box.success(f"MA condition met for candle at {last_candle['timestamp']}. Spot={spot_price}, Strike={deep_itm_strike}")

                    # Get option security ID for deep ITM CE
                    option_id = find_deep_itm_ce(dhan, nifty_security_id, expiry_date, deep_itm_strike)
                    if not option_id:
                        status_box.error("Deep ITM Call Option not found in option chain.")
                        continue

                    if paper_mode:
                        entry_price = spot_price  # simulate entry price
                        status_box.info(f"[PAPER] Buying deep ITM CE {deep_itm_strike} @ approx {entry_price}")
                        trade_log.text(f"[PAPER TRADE] Bought {deep_itm_strike} CE at ₹{entry_price}")
                    else:
                        # Place real buy order
                        try:
                            order_resp = dhan.place_order(
                                security_id=option_id,
                                exchange_segment=dhan.NSE_FNO,
                                transaction_type=dhan.BUY,
                                quantity=lot_size,
                                order_type=dhan.MARKET,
                                product_type=dhan.INTRA,
                                price=0
                            )
                            entry_price = float(order_resp['order_legs'][0]['traded_price'])
                            trade_log.text(f"Bought {deep_itm_strike} CE @ ₹{entry_price}")
                            status_box.info(f"Bought deep ITM CE {deep_itm_strike} @ ₹{entry_price}")
                        except Exception as e:
                            status_box.error(f"Order placement failed: {e}")
                            continue

                    traded_candle = last_candle['timestamp']
                    bought_option_id = option_id
                    sl_price = entry_price * 0.95
                    max_price = entry_price

                # Trailing stop loss management if in position
                if bought_option_id and entry_price and sl_price:
                    # Try to get live LTP for option; fallback to REST or use last tick
                    try:
                        if paper_mode:
                            # Simulate LTP moving up then down around entry_price for demo
                            ltp = max_price + 1 if max_price < entry_price * 1.02 else max_price - 1
                        else:
                            quote = dhan.security_quote(dhan.NSE_FNO, bought_option_id)
                            ltp = float(quote['last_price'])
                    except Exception as e:
                        status_box.error(f"Failed fetching LTP for trailing stop: {e}")
                        await asyncio.sleep(10)
                        continue

                    if ltp > max_price:
                        max_price = ltp
                        sl_price = max(sl_price, max_price * 0.95)

                    status_box.info(f"Trailing Stop Update - LTP: ₹{ltp}, SL: ₹{sl_price}")

                    # Check if SL hit
                    if ltp <= sl_price:
                        if paper_mode:
                            exit_price = ltp
                            pnl = (exit_price - entry_price) * lot_size
                            trade_log.text(f"[PAPER TRADE] Exited trade at ₹{exit_price}, P&L: ₹{pnl}")
                            pnl_box.success(f"[PAPER] Trade Closed! Exit: ₹{exit_price}, P&L: ₹{pnl}")
                        else:
                            try:
                                sell_order = dhan.place_order(
                                    security_id=bought_option_id,
                                    exchange_segment=dhan.NSE_FNO,
                                    transaction_type=dhan.SELL,
                                    quantity=lot_size,
                                    order_type=dhan.MARKET,
                                    product_type=dhan.INTRA,
                                    price=0
                                )
                                exit_price = float(sell_order['order_legs'][0]['traded_price'])
                                pnl = (exit_price - entry_price) * lot_size
                                trade_log.text(f"Exited trade at ₹{exit_price}, P&L: ₹{pnl}")
                                pnl_box.success(f"Trade Closed! Exit: ₹{exit_price}, P&L: ₹{pnl}")
                            except Exception as e:
                                status_box.error(f"Failed to place exit order: {e}")
                        # Reset position data
                        bought_option_id = None
                        entry_price = None
                        sl_price = None
                        max_price = None

            # Sleep briefly to avoid flooding UI
            await asyncio.sleep(0.1)


def main():
    if run_bot:
        if not (client_id and access_token and nifty_security_id and expiry_date):
            st.error("Please fill all API & configuration fields before starting.")
            return

        # Run asynchronous event loop for bot
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(trading_bot(
                client_id, 
                access_token, 
                nifty_security_id,
                expiry_date,
                lot_size,
                paper_mode
            ))
        except Exception as e:
            st.error(f"Error running bot: {e}")

if __name__ == "__main__":
    main()
