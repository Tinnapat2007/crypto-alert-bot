"""
🤖 AI Crypto Signal Bot — GitHub Actions Version
=================================================
รันอัตโนมัติทุก 15 นาที ไม่ต้องเปิดแอป
Logic เดียวกับ Streamlit v4.2 ทุกอย่าง
"""

import os
import sys
import json
import time
import logging
import datetime
import requests
import pandas as pd
import yfinance as yf
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD, EMAIndicator
from ta.volatility import BollingerBands, AverageTrueRange

# =========================================================================
# 0. Logging
# =========================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# =========================================================================
# 1. Config — อ่านจาก Environment Variables (GitHub Secrets)
# =========================================================================
WEBHOOKS = {
    "STRONG BUY":  os.environ.get("DISCORD_BUY_STRONG",  ""),
    "BUY":         os.environ.get("DISCORD_BUY_NORMAL",  ""),
    "STRONG SELL": os.environ.get("DISCORD_SELL_STRONG", ""),
    "SELL":        os.environ.get("DISCORD_SELL_NORMAL", ""),
    "HOLD":        os.environ.get("DISCORD_HOLD",        ""),
}

# เหรียญที่ต้องการสแกน — แก้ตรงนี้ได้เลย
COINS = ["BTC", "ETH", "SOL", "DOGE", "PEPE", "BNB", "XRP"]

# Timeframe: 1h = เดย์เทรด / 4h = สวิง / 1d = สปอต
YF_INTERVAL = "1h"
YF_PERIOD   = "60d"

# ส่ง HOLD ด้วยไหม (True = ส่ง / False = ส่งแค่ BUY/SELL)
SEND_HOLD = True

# =========================================================================
# 2. State File — เก็บ signal ล่าสุด ป้องกันส่งซ้ำ
# =========================================================================
STATE_FILE = "signal_state.json"

def load_state() -> dict:
    """โหลด state จากไฟล์ (GitHub Actions เก็บระหว่าง step ได้)"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# =========================================================================
# 3. HTTP Session
# =========================================================================
def build_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.6,
                  status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    })
    return s

http = build_session()

# =========================================================================
# 4. Fear & Greed Index
# =========================================================================
def get_fear_greed() -> dict:
    try:
        r = http.get("https://api.alternative.me/fng/?limit=1", timeout=8).json()
        val = int(r["data"][0]["value"])
        if   val >= 80: return {"value": val, "label": f"Extreme Greed 🔥 ({val})", "score": -20.0}
        elif val >= 60: return {"value": val, "label": f"Greed 😀 ({val})",          "score": +10.0}
        elif val >= 40: return {"value": val, "label": f"Neutral 😐 ({val})",        "score":   0.0}
        elif val >= 20: return {"value": val, "label": f"Fear 😨 ({val})",           "score": -10.0}
        else:           return {"value": val, "label": f"Extreme Fear 💀 ({val})",   "score": +20.0}
    except Exception as e:
        logger.warning(f"Fear&Greed failed: {e}")
        return {"value": 50, "label": "Neutral 😐 (50)", "score": 0.0}

# =========================================================================
# 5. CoinGecko — Market Data
# =========================================================================
COINGECKO_ID_MAP = {
    "BTC":"bitcoin","ETH":"ethereum","SOL":"solana","BNB":"binancecoin",
    "XRP":"ripple","ADA":"cardano","DOGE":"dogecoin","AVAX":"avalanche-2",
    "DOT":"polkadot","MATIC":"matic-network","LINK":"chainlink","UNI":"uniswap",
    "LTC":"litecoin","ATOM":"cosmos","XLM":"stellar","PEPE":"pepe",
    "SHIB":"shiba-inu","TRX":"tron","OP":"optimism","ARB":"arbitrum",
    "SUI":"sui","APT":"aptos","INJ":"injective-protocol","FIL":"filecoin",
    "NEAR":"near","ICP":"internet-computer","WIF":"dogwifcoin","BONK":"bonk",
    "TON":"the-open-network","WLD":"worldcoin-wld",
}

def get_coingecko_batch(tickers: list) -> dict:
    empty   = {"market_cap":0,"volume_24h":0,"price_change_24h":0.0,
               "rank":"-","cg_price":0.0}
    results = {t: dict(empty) for t in tickers}
    id_map  = {}

    for t in tickers:
        cg_id = COINGECKO_ID_MAP.get(t.upper())
        if cg_id:
            id_map[cg_id] = t
        else:
            # Auto-search
            try:
                r = http.get("https://api.coingecko.com/api/v3/search",
                             params={"query": t}, timeout=5).json()
                coins = r.get("coins", [])
                if coins:
                    id_map[coins[0]["id"]] = t
            except Exception as e:
                logger.warning(f"CoinGecko search [{t}]: {e}")

    if not id_map:
        return results

    try:
        resp = http.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency":"usd","ids":",".join(id_map),
                    "per_page":len(id_map),"page":1},
            timeout=10,
        ).json()
        for d in resp:
            t = id_map.get(d.get("id",""))
            if t:
                results[t] = {
                    "market_cap":       d.get("market_cap")                  or 0,
                    "volume_24h":       d.get("total_volume")                or 0,
                    "price_change_24h": d.get("price_change_percentage_24h") or 0.0,
                    "rank":             d.get("market_cap_rank")             or "-",
                    "cg_price":         d.get("current_price")               or 0.0,
                }
    except Exception as e:
        logger.warning(f"CoinGecko batch failed: {e}")

    return results

# =========================================================================
# 6. Yahoo Finance — OHLCV Batch
# =========================================================================
def fetch_ohlcv(tickers: list, period: str, interval: str) -> pd.DataFrame:
    yf_syms = [f"{t}-USD" for t in tickers]
    try:
        df = yf.download(
            tickers=yf_syms, period=period, interval=interval,
            group_by="ticker", progress=False,
            auto_adjust=True, threads=True,
        )
        return df
    except Exception as e:
        logger.error(f"yfinance failed: {e}")
        return pd.DataFrame()


def extract_df(all_data: pd.DataFrame, yf_sym: str) -> pd.DataFrame:
    if all_data.empty:
        return pd.DataFrame()
    try:
        if isinstance(all_data.columns, pd.MultiIndex):
            lvl0 = all_data.columns.get_level_values(0).unique()
            if yf_sym in lvl0:   return all_data[yf_sym].copy().dropna(how="all")
            if "Close" in lvl0:  return all_data.copy().dropna(how="all")
        elif "Close" in all_data.columns:
            return all_data.copy().dropna(how="all")
    except Exception as e:
        logger.warning(f"extract [{yf_sym}]: {e}")
    return pd.DataFrame()

# =========================================================================
# 7. Signal Engine — 4 แกน (Logic เดียวกับ Streamlit v4.2)
# =========================================================================
MIN_BARS = 52

def compute_signal(df: pd.DataFrame, fg: dict) -> dict:
    trend_s = mom_s = vol_s = 0.0
    rsi_v = macd_v = stoch_v = bb_v = vol_r = atr_v = 0.0
    price = 0.0
    data_ok = False

    if not df.empty and "Close" in df.columns:
        df = df.dropna(subset=["Close"]).copy()
        if len(df) >= MIN_BARS:
            data_ok = True
            close  = df["Close"].astype(float)
            high   = df["High"].astype(float)
            low    = df["Low"].astype(float)
            volume = (df["Volume"].astype(float) if "Volume" in df.columns
                      else pd.Series([1.0]*len(df), index=df.index))
            price  = float(close.iloc[-1])

            df["EMA20"]    = EMAIndicator(close=close, window=20).ema_indicator()
            df["EMA50"]    = EMAIndicator(close=close, window=50).ema_indicator()
            df["RSI"]      = RSIIndicator(close=close, window=14).rsi()
            stoch          = StochasticOscillator(high=high, low=low, close=close,
                                                  window=14, smooth_window=3)
            df["Stoch_K"]  = stoch.stoch()
            df["Stoch_D"]  = stoch.stoch_signal()
            macd_obj       = MACD(close=close)
            df["MACD_diff"]= macd_obj.macd_diff()
            bb             = BollingerBands(close=close, window=20, window_dev=2)
            df["BB_pct"]   = bb.bollinger_pband()
            atr_obj        = AverageTrueRange(high=high, low=low, close=close, window=14)
            df["ATR"]      = atr_obj.average_true_range()
            vol_ma         = volume.rolling(20).mean()
            df["VolRatio"] = volume / vol_ma.replace(0, 1)
            df.ffill(inplace=True)

            last = df.iloc[-1]
            prev = df.iloc[-2]

            def g(col, d=0.0):
                v = last.get(col, d)
                return d if pd.isna(v) else float(v)
            def gp(col, d=0.0):
                v = prev.get(col, d)
                return d if pd.isna(v) else float(v)

            rsi_v   = g("RSI", 50.0);   macd_v  = g("MACD_diff")
            stoch_v = g("Stoch_K",50.0);stoch_d = g("Stoch_D",50.0)
            bb_v    = g("BB_pct",0.5);  atr_v   = g("ATR")
            vol_r   = g("VolRatio",1.0)
            p_macd  = gp("MACD_diff");  p_stk   = gp("Stoch_K",50.0)
            p_std   = gp("Stoch_D",50.0)

            # Trend 35%
            p, e20, e50 = float(last["Close"]), g("EMA20"), g("EMA50")
            if   p > e20 and e20 > e50: trend_s =  35.0
            elif p < e20 and e20 < e50: trend_s = -35.0
            elif p > e20:               trend_s =  12.0
            else:                       trend_s = -12.0

            # Momentum 30%
            if   rsi_v > 55: mom_s += 10.0
            elif rsi_v < 45: mom_s -= 10.0
            if   rsi_v >= 70: mom_s -= 5.0
            elif rsi_v <= 30: mom_s += 5.0
            if   macd_v > 0 and p_macd <= 0: mom_s += 10.0
            elif macd_v > 0:                  mom_s +=  7.0
            elif macd_v < 0 and p_macd >= 0: mom_s -= 10.0
            else:                             mom_s -=  7.0
            if   stoch_v > stoch_d and p_stk <= p_std: mom_s += 10.0
            elif stoch_v > stoch_d:                     mom_s +=  7.0
            elif stoch_v < stoch_d and p_stk >= p_std: mom_s -= 10.0
            else:                                       mom_s -=  7.0

            # Volatility 15%
            if   bb_v <= 0.20: vol_s += 10.0
            elif bb_v <= 0.35: vol_s +=  5.0
            elif bb_v >= 0.80: vol_s -= 10.0
            elif bb_v >= 0.65: vol_s -=  5.0
            if   vol_r >= 2.5: vol_s += 5.0
            elif vol_r >= 1.5: vol_s += 2.5

    # Sentiment 20%
    sent_s    = fg.get("score", 0.0)
    total_raw = trend_s + mom_s + vol_s + sent_s

    if total_raw >= 18:
        side   = "BUY"
        prob   = min(abs(total_raw), 100.0)
        signal = "🟢 STRONG BUY" if prob >= 65 else "🟢 BUY"
    elif total_raw <= -18:
        side   = "SELL"
        prob   = min(abs(total_raw), 100.0)
        signal = "🔴 STRONG SELL" if prob >= 65 else "🔴 SELL"
    else:
        side   = "HOLD"
        prob   = min(abs(total_raw), 100.0)
        signal = "🟡 HOLD"

    return {
        "side":side,"signal":signal,"prob":prob,"total_raw":total_raw,
        "trend_s":trend_s,"mom_s":mom_s,"vol_s":vol_s,"sent_s":sent_s,
        "rsi":rsi_v,"macd":macd_v,"stoch":stoch_v,
        "bb_pct":bb_v,"vol_ratio":vol_r,"atr":atr_v,
        "price":price,"data_ok":data_ok,
    }

# =========================================================================
# 8. Discord Sender
# =========================================================================
COLOR_MAP = {
    "STRONG BUY": 0x00FF88, "BUY": 0x00CC66,
    "STRONG SELL":0xFF2244, "SELL":0xFF6644,
    "HOLD":       0xFFCC00,
}

def signal_key(signal_str: str) -> str:
    return (signal_str
            .replace("🟢","").replace("🔴","").replace("🟡","")
            .replace("(ไซด์เวย์)","").strip())

def bkk_now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=7)).strftime("%d/%m/%Y %H:%M")

def fmt_price(p: float) -> str:
    if p <= 0:      return "N/A"
    if p < 0.0001:  return f"${p:.8f}"
    if p < 0.01:    return f"${p:.6f}"
    if p < 1:       return f"${p:.4f}"
    if p < 1000:    return f"${p:,.2f}"
    return                 f"${p:,.0f}"

def send_to_discord(sym: str, sig: dict, cg: dict, fg: dict) -> bool:
    sig_k   = signal_key(sig["signal"])
    url     = WEBHOOKS.get(sig_k, "")
    if not url:
        logger.warning(f"No webhook for: {sig_k}")
        return False

    chg     = cg.get("price_change_24h", 0.0)
    chg_str = f"{'▲' if chg>=0 else '▼'}{abs(chg):.2f}%"
    price   = sig["price"] or cg.get("cg_price", 0.0)
    mcap    = cg.get("market_cap", 0)
    mcap_s  = (f"${mcap/1e9:.2f}B" if mcap>=1e9
               else f"${mcap/1e6:.0f}M" if mcap>=1e6 else "N/A")

    embed = {
        "title":       f"{sig['signal']} — {sym}",
        "description": f"🤖 GitHub Actions Bot | `{YF_INTERVAL}` Timeframe",
        "color":       COLOR_MAP.get(sig_k, 0x888888),
        "fields": [
            {"name":"💰 ราคา",         "value":f"`{fmt_price(price)}`",      "inline":True},
            {"name":"📊 Score",         "value":f"`{sig['total_raw']:+.1f}`", "inline":True},
            {"name":"🎯 ความมั่นใจ",   "value":f"`{sig['prob']:.0f}%`",      "inline":True},
            {"name":"📈 เปลี่ยน 24H",  "value":f"`{chg_str}`",              "inline":True},
            {"name":"💎 Market Cap",    "value":mcap_s,                       "inline":True},
            {"name":"😱 Sentiment",     "value":fg["label"],                  "inline":True},
            {"name":"🧮 T/M/V/S",      "value":(
                f"`{sig['trend_s']:+.0f}`/`{sig['mom_s']:+.0f}`/"
                f"`{sig['vol_s']:+.0f}`/`{sig['sent_s']:+.0f}`"
            ), "inline":True},
            {"name":"📉 RSI",           "value":f"`{sig['rsi']:.1f}`",        "inline":True},
            {"name":"📊 BB%B",          "value":f"`{sig['bb_pct']:.2f}`",     "inline":True},
        ],
        "footer": {"text": f"AI Crypto Sniper · Bangkok {bkk_now()}"},
    }

    try:
        resp = requests.post(url, json={"embeds": [embed]}, timeout=8)
        if resp.status_code in (200, 204):
            logger.info(f"✅ Sent: {sym} {sig_k}")
            return True
        else:
            logger.warning(f"Discord {sym}: HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"Discord send failed [{sym}]: {e}")
    return False

# =========================================================================
# 9. Main
# =========================================================================
def main():
    logger.info(f"🚀 Bot started — {bkk_now()} (Bangkok)")
    logger.info(f"📋 Coins: {COINS}")
    logger.info(f"⏱️  Interval: {YF_INTERVAL} | Period: {YF_PERIOD}")

    # ── โหลด state เดิม ───────────────────────────────────────────────────
    state = load_state()
    # state = { "BTC": "🟢 STRONG BUY", "ETH": "🟡 HOLD", ... }

    # ── ดึงข้อมูลทั้งหมด ─────────────────────────────────────────────────
    logger.info("😱 Fetching Fear & Greed...")
    fg = get_fear_greed()
    logger.info(f"   → {fg['label']}")

    logger.info("📥 Fetching OHLCV from Yahoo Finance...")
    yf_syms    = [f"{c}-USD" for c in COINS]
    all_ohlcv  = fetch_ohlcv(COINS, YF_PERIOD, YF_INTERVAL)

    logger.info("🦎 Fetching Market Data from CoinGecko...")
    cg_all = get_coingecko_batch(COINS)

    # ── คำนวณ + ส่ง Discord ───────────────────────────────────────────────
    new_state    = dict(state)
    sent_count   = 0
    skip_count   = 0

    for coin in COINS:
        try:
            yf_sym = f"{coin}-USD"
            df     = extract_df(all_ohlcv, yf_sym)
            cg     = cg_all.get(coin, {})
            sig    = compute_signal(df, fg)

            logger.info(f"{coin}: {sig['signal']} (Score {sig['total_raw']:+.1f})")

            # ข้าม HOLD ถ้าไม่ต้องการ
            if sig["side"] == "HOLD" and not SEND_HOLD:
                skip_count += 1
                continue

            # เช็คว่าสัญญาณเปลี่ยนจากรอบก่อนไหม
            last_sig = state.get(coin, "")
            if last_sig == sig["signal"]:
                logger.info(f"   → Same signal as before, skip")
                skip_count += 1
                continue

            # ส่ง Discord
            sent = send_to_discord(coin, sig, cg, fg)
            if sent:
                new_state[coin] = sig["signal"]
                sent_count += 1
            else:
                skip_count += 1

            time.sleep(0.5)   # ป้องกัน rate limit

        except Exception as e:
            logger.error(f"Error [{coin}]: {e}")

    # ── บันทึก state ใหม่ ─────────────────────────────────────────────────
    save_state(new_state)

    logger.info(f"✅ Done — Sent: {sent_count} | Skipped: {skip_count}")
    return sent_count

if __name__ == "__main__":
    count = main()
    sys.exit(0)
