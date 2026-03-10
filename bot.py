"""
Warren Bot V4 - Full Python ICT Trading & Grup Yonetim Botu
- Twelve Data API ile gercek zamanli fiyat verisi
- ICT sinyal tarama
- DeepSeek AI ile gunluk HTF analiz (sabah 09:00 TR saati)
- Telegram grup yonetimi
- 7/24 Render.com'da calisir
"""

import os
import io
import logging
import asyncio
from datetime import datetime, timedelta

import requests
import pandas as pd
import numpy as np
from ict_engine import analyze_ict_v2, get_active_session, is_in_kill_zone
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ── AYARLAR ─────────────────────────────────────────────────
TG_TOKEN      = os.environ.get("TG_TOKEN")
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID",    "-1003838635441")
TD_API_KEY    = os.environ.get("TD_API_KEY",    "YOUR_TWELVEDATA_KEY")
FMP_API_KEY   = os.environ.get("FMP_API_KEY",  "")  # financialmodelingprep.com - ucretsiz key
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions")
ADMIN_IDS     = [6663913960]
PORT          = int(os.environ.get("PORT", "8080"))

MODE_CONFIG = {
    "SCALP": {"interval": "1min", "htf": "15min"},
    "SWING": {"interval": "15min", "htf": "4h"},
}
TRADE_MODE = "SCALP"
SELECTED_SYMBOL = "XAU/USD"

SYMBOLS = {
    "XAU/USD":  {"name": "XAUUSD",  "pip_val": 100},
    "QQQ":      {"name": "US100",   "pip_val": 10},
    "XAG/USD":  {"name": "XAGUSD",  "pip_val": 100},
    "BTC/USD":  {"name": "BTCUSD", "pip_val": 1},
}
COOLDOWN_MIN     = 30
MIN_RR           = 2.5
MIN_CONFLUENCE   = 4       # Minimum confluence puani (0-6, 4+ = trade)
MAX_DAILY_TRADES = 10
RISK_PER_TRADE   = 0.01    # %1
MAX_DAILY_RISK   = 0.03    # %3
OB_LOOKBACK      = 20
SIGNAL_INTERVAL  = 60
daily_trade_count = 0
daily_trade_date  = None

stats            = {"total": 0, "win": 0, "loss": 0}
stats_per_symbol = {s: {"total": 0, "win": 0, "loss": 0} for s in SYMBOLS}  # Sembol bazli istatistik
results_history  = []  # ["W","L","W"...] kayip serisi uyarisi icin
kill_zone_only   = False  # True: sadece Kill Zone'da sinyal gonder
favori_semboller = set(SYMBOLS.keys())  # Taranacak semboller (varsayilan hepsi)
fiyat_alarmlari  = []  # [{"sembol","hedef","yon","chat_id"}]
signal_tracking  = {}  # {msg_id: {symbol, sig, time}} Al/Gec icin
last_daily_summary  = None
last_weekly_summary = None
warnings_db      = {}
message_counts   = {}
last_signal_time = {}
bot_active       = True
last_daily_analiz= None  # Son gunluk analiz tarihi

# PnL kayitlari: {kullanici_id: [{"sembol","yon","giris","cikis","lot","pnl","tarih"}]}
pnl_db = {}

# Ekonomik takvim uyari saatleri (UTC) - manuel liste
EKONOMIK_OLAYLAR = [
    {"saat": "13:30", "olay": "NFP (Non-Farm Payrolls)", "gun": 5, "etki": "🔴 YÜKSEK"},  # Cuma
    {"saat": "19:00", "olay": "FOMC Faiz Kararı",        "gun": -1, "etki": "🔴 YÜKSEK"}, # değişken
    {"saat": "13:30", "olay": "CPI Enflasyon Verisi",    "gun": -1, "etki": "🔴 YÜKSEK"},
    {"saat": "14:45", "olay": "PMI Verisi",               "gun": -1, "etki": "🟡 ORTA"},
    {"saat": "15:00", "olay": "ISM Verisi",               "gun": -1, "etki": "🟡 ORTA"},
]
# Aktif sinyal takibi: {symbol: {"direction","entry","sl","tp","time","chat_id"}}
aktif_sinyaller = {}
gonderilen_takvim_uyarilari = set()  # Tekrar gonderimi onlemek icin
son_kz_durum = None  # Son Kill Zone durumu (acilis/kapanis tekrarini onlemek icin)
_takvim_api_cache = {"date": None, "events": []}  # API cache (1 saat)
_htf_cache = {}  # {symbol: (timestamp, df)} - Twelve Data 8/dk limit icin HTF 15dk cache

if not TG_TOKEN:
    raise RuntimeError("TG_TOKEN ortam degiskeni zorunlu. Lutfen env'e ekleyin.")

trade_history = {s: [] for s in SYMBOLS}


def normalize_symbol(token):
    if not token:
        return None
    k = token.upper().replace("/", "").replace(" ", "")
    if k in ("XAUUSD", "GOLD"):
        return "XAU/USD"
    if k in ("QQQ", "US100", "NAS100"):
        return "QQQ"
    if k in ("XAGUSD", "SILVER"):
        return "XAG/USD"
    if k in ("BTCUSD", "BTCUSD", "BTC"):
        return "BTC/USD"
    if k in ("XAGUSD", "SİLVER"):
        return "XAG/USD"
    return token if token in SYMBOLS else None


def get_symbol_cfg(symbol):
    base = SYMBOLS.get(symbol)
    if not base:
        return None
    mode = MODE_CONFIG.get(TRADE_MODE, MODE_CONFIG["SCALP"])
    cfg = dict(base)
    cfg.update(mode)
    return cfg


def _set_selected_symbol(symbol):
    global SELECTED_SYMBOL
    if symbol in SYMBOLS:
        SELECTED_SYMBOL = symbol


def _record_trade_open(symbol, sig, trade_id):
    entry = {
        "id": trade_id,
        "status": "OPEN",
        "direction": sig["direction"],
        "entry": float(sig["price"]),
        "sl": float(sig["sl"]),
        "tp": float(sig["tp"]),
        "open_time": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        "close_time": None,
        "close_price": None,
        "result": None,
    }
    trade_history.setdefault(symbol, []).append(entry)
    trade_history[symbol] = trade_history[symbol][-50:]


def _record_trade_close(symbol, trade_id, hit, price):
    items = trade_history.setdefault(symbol, [])
    for item in reversed(items):
        if item.get("id") == trade_id:
            item["status"] = "CLOSED"
            item["close_time"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
            item["close_price"] = float(price)
            item["result"] = hit
            return
    items.append({
        "id": trade_id,
        "status": "CLOSED",
        "direction": "",
        "entry": None,
        "sl": None,
        "tp": None,
        "open_time": None,
        "close_time": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        "close_price": float(price),
        "result": hit,
    })
    trade_history[symbol] = trade_history[symbol][-50:]


def format_trade_history(symbol, limit=5):
    items = trade_history.get(symbol, [])
    if not items:
        return f"{symbol} icin islem yok."
    recent = items[-limit:]
    lines = [f"{symbol} | Son {len(recent)} islem"]
    for t in reversed(recent):
        status = "Açık" if t["status"] == "OPEN" else "Kapali"
        res = t.get("result") or "-"
        entry = f"{t['entry']:.4f}" if t.get("entry") is not None else "-"
        close = f"{t['close_price']:.4f}" if t.get("close_price") is not None else "-"
        lines.append(f"{status} {t.get('direction','')} | G:{entry} K:{close} | {res}")
    return "\n".join(lines)
# ── EKONOMİK TAKVİM API ──────────────────────────────────────
def get_economic_calendar_api():
    """FMP API'den bugunun ekonomik olaylarini al. Bos/None = fallback kullan."""
    if not FMP_API_KEY or FMP_API_KEY == "YOUR_FMP_KEY":
        return None
    now = datetime.utcnow()
    today = now.strftime("%Y-%m-%d")
    if _takvim_api_cache["date"] == today and _takvim_api_cache["events"]:
        return _takvim_api_cache["events"]
    try:
        r = requests.get(
            "https://financialmodelingprep.com/stable/economic-calendar",
            params={"from": today, "to": today, "apikey": FMP_API_KEY},
            timeout=10
        )
        data = r.json()
        if isinstance(data, dict) and "Error" in data:
            return None
        if not isinstance(data, list) or len(data) == 0:
            _takvim_api_cache["date"] = today
            _takvim_api_cache["events"] = []
            return []
        events = []
        for e in data:
            # FMP format: date, time veya date datetime, event, country, impact (High/Medium/Low)
            dt_str = e.get("date") or e.get("datetime") or ""
            event_name = e.get("event") or e.get("title") or e.get("name") or ""
            impact = (e.get("impact") or e.get("importance") or "Medium").upper()
            if "HIGH" in impact: etki = "🔴 YÜKSEK"
            elif "MEDIUM" in impact or "MED" in impact: etki = "🟡 ORTA"
            else: etki = "🟢 DÜŞÜK"
            # dt_str ornegi: "2025-03-10 13:30:00" veya "2025-03-10T13:30:00"
            hour, minute = "12", "00"
            dt_str = str(dt_str).replace("T", " ")
            if " " in dt_str and ":" in dt_str:
                tpart = dt_str.split()[1]
                parts = tpart.split(":")
                try:
                    h = int(parts[0])
                    m = int(parts[1]) if len(parts) > 1 else 0
                    hour, minute = f"{h:02d}", f"{m:02d}"
                except (ValueError, IndexError):
                    pass
            events.append({
                "saat": f"{hour}:{minute}",
                "olay": event_name,
                "etki": etki,
                "country": e.get("country", ""),
            })
        _takvim_api_cache["date"] = today
        _takvim_api_cache["events"] = events
        return events
    except Exception as ex:
        log.warning(f"Takvim API hatasi: {ex}")
        return None

# ── TWELVE DATA ──────────────────────────────────────────────
def get_candles(symbol, interval="1min", outputsize=50):
    try:
        r = requests.get("https://api.twelvedata.com/time_series", params={
            "symbol": symbol, "interval": interval,
            "outputsize": outputsize, "apikey": TD_API_KEY
        }, timeout=10)
        data = r.json()
        if "values" not in data:
            log.warning(f"API hatasi {symbol}: {data.get('message','?')}")
            return None
        df = pd.DataFrame(data["values"])
        df = df.rename(columns={"datetime":"time","open":"o","high":"h","low":"l","close":"c"})
        df = df.astype({"o": float, "h": float, "l": float, "c": float})
        return df.iloc[::-1].reset_index(drop=True)
    except Exception as e:
        log.error(f"API hatasi: {e}"); return None

def get_price(symbol):
    try:
        r = requests.get("https://api.twelvedata.com/price", params={
            "symbol": symbol, "apikey": TD_API_KEY}, timeout=5)
        return float(r.json().get("price", 0)) or None
    except: return None

def get_daily_candles(symbol, outputsize=30):
    """Gunluk mum verileri - HTF analiz icin"""
    try:
        r = requests.get("https://api.twelvedata.com/time_series", params={
            "symbol": symbol, "interval": "1day",
            "outputsize": outputsize, "apikey": TD_API_KEY
        }, timeout=10)
        data = r.json()
        if "values" not in data: return None
        df = pd.DataFrame(data["values"])
        df = df.rename(columns={"datetime":"time","open":"o","high":"h","low":"l","close":"c"})
        df = df.astype({"o": float, "h": float, "l": float, "c": float})
        return df.iloc[::-1].reset_index(drop=True)
    except: return None

def get_htf_cached(symbol, interval="15min", outputsize=30):
    """HTF mumlari 15 dakika cache - Twelve Data 8/dk limit icin"""
    global _htf_cache
    now = datetime.utcnow()
    key = f"{symbol}_{interval}"
    if key in _htf_cache:
        ts, df = _htf_cache[key]
        if (now - ts).total_seconds() < 14 * 60:  # 14 dk cache
            return df
    df = get_candles(symbol, interval, outputsize)
    if df is not None:
        _htf_cache[key] = (now, df)
    return df

async def aget_candles(symbol, interval="1min", outputsize=50):
    return await asyncio.to_thread(get_candles, symbol, interval, outputsize)

async def aget_price(symbol):
    return await asyncio.to_thread(get_price, symbol)

async def aget_daily_candles(symbol, outputsize=30):
    return await asyncio.to_thread(get_daily_candles, symbol, outputsize)

async def aget_htf_cached(symbol, interval="15min", outputsize=30):
    return await asyncio.to_thread(get_htf_cached, symbol, interval, outputsize)

async def aget_economic_calendar_api():
    return await asyncio.to_thread(get_economic_calendar_api)

# ── DEEPSEEK - GUNLUK ANALİZ ───────────────────────────────
async def get_market_context(symbols):
    """DeepSeek'e verilecek piyasa verilerini hazirla"""
    context = {}
    for symbol in symbols:
        price = await aget_price(symbol)
        daily = await aget_daily_candles(symbol, 10)
        if price and daily is not None:
            son5 = daily.tail(5)
            highs = son5["h"].values
            lows  = son5["l"].values
            closes= son5["c"].values
            trend = "Uptrend" if closes[-1] > closes[0] else "Downtrend"
            context[symbol] = {
                "price":  price,
                "trend":  trend,
                "high5":  round(float(max(highs)), 4),
                "low5":   round(float(min(lows)), 4),
                "close":  round(float(closes[-1]), 4),
            }
    return context


def _deepseek_chat(messages, max_tokens=800):
    if not DEEPSEEK_API_KEY:
        return None
    try:
        r = requests.post(
            DEEPSEEK_API_URL,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "deepseek-chat",
                "messages": messages,
                "max_tokens": max_tokens
            },
            timeout=30
        )
        data = r.json()
        if "choices" in data and data["choices"]:
            return data["choices"][0]["message"]["content"]
        log.error(f"DeepSeek API hatasi: {data}")
        return None
    except Exception as e:
        log.error(f"DeepSeek istek hatasi: {e}")
        return None


def generate_daily_analysis(symbol, context_data):
    """DeepSeek ile gunluk HTF analiz olustur"""
    if not DEEPSEEK_API_KEY:
        return None

    if symbol not in context_data:
        return None

    now_tr = datetime.utcnow() + timedelta(hours=3)
    tarih  = now_tr.strftime("%d %B %Y - %A")
    symbol_name = SYMBOLS.get(symbol, {}).get("name", symbol)

    data = context_data[symbol]
    piyasa_str = f"{symbol}: Fiyat={data['price']}, Trend={data['trend']}, 5gun High={data['high5']}, 5gun Low={data['low5']}\n"

    prompt = f"""Sen bir ICT (Inner Circle Trader) piyasa analiz botusun. Asagidaki verilere gore {symbol_name} icin bugun ({tarih}) gunluk HTF analiz yaz.

MEVCUT PIYASA VERILERI:
{piyasa_str}

ONEMLI: Sinyal botu degilsin.
- \"XYZ fiyatindan LONG AL\" deme
- \"Kesin kazanc\" deme
- HTF bias + seviyeler + nedenlerini acikla
- Kullanici kendi kararini versin

Asagidaki FORMATTA Turkce analiz yaz (emojileri kullan):

📊 {symbol_name} - HTF ANALIZ
📅 {tarih}
━━━━━━━━━━━━━━━━━━━━━━
📈 YAPISAL ANALIZ
━━━━━━━━━━━━━━━━━━━━━━
🔹 DAILY STRUCTURE: [analiz]
🔹 WEEKLY STRUCTURE: [analiz]
🔹 MONTHLY: [analiz]
━━━━━━━━━━━━━━━━━━━━━━
🧭 KURUMSAL YONELIM
━━━━━━━━━━━━━━━━━━━━━━
📊 COT ANALIZI: [analiz]
📅 QUARTERLY: [analiz]
━━━━━━━━━━━━━━━━━━━━━━
🎯 BUGUNKU BIAS
━━━━━━━━━━━━━━━━━━━━━━
➡️ GENEL YONELIM: [BULLISH / BEARISH / NEUTRAL]
Neden? [3 sebep]
━━━━━━━━━━━━━━━━━━━━━━
🔍 DIKKAT EDILECEK SEVIYELER
━━━━━━━━━━━━━━━━━━━━━━
[Bias'a gore LONG veya SHORT zone'lari]
📍 ZONE 1: [aralik]
• Yapi: [OB/FVG/Breaker]
• Konum: [Premium/Discount]
• Nedeni: [aciklama]
⚠️ Invalid: [seviye] body close
📍 ZONE 2: [aralik]
[ayni format]
━━━━━━━━━━━━━━━━━━━━━━
🎯 LIKIDITE HEDEFLER
━━━━━━━━━━━━━━━━━━━━━━
1️⃣ [hedef 1]
2️⃣ [hedef 2]
3️⃣ [hedef 3]
━━━━━━━━━━━━━━━━━━━━━━
⏰ ZAMANLAMALAR
━━━━━━━━━━━━━━━━━━━━━━
🕐 KILL ZONES (TR Saati):
• 10:00-11:00 → London Silver Bullet
• 13:30-14:00 → NY Open
• 16:00-17:00 → NY Silver Bullet
📊 MACRO SAATLERI: 09:50-10:10 / 10:50-11:10 / 13:50-14:10 / 14:50-15:10
━━━━━━━━━━━━━━━━━━━━━━
📌 OZEL NOTLAR
━━━━━━━━━━━━━━━━━━━━━━
⚠️ DIKKAT: [onemli notlar]
💡 STRATEJI: [gunluk strateji tavsiyesi]
━━━━━━━━━━━━━━━━━━━━━━
🎓 HATIRLATMA
━━━━━━━━━━━━━━━━━━━━━━
✅ Bu bir analiz, sinyal degil
✅ Setup yoksa trade yok
✅ Invalid seviyeler gecerse bias iptal
✅ Risk max %1-2
📚 Konsept: Body close onemli | Premium'dan short, discount'tan long | HTF bias olmadan LTF entry yok
━━━━━━━━━━━━━━━━━━━━━━
📊 Bir sonraki analiz yarin 09:00'da
━━━━━━━━━━━━━━━━━━━━━━"""

    try:
        messages = [{"role": "user", "content": prompt}]
        return _deepseek_chat(messages, max_tokens=2000)
    except Exception as e:
        log.error(f"DeepSeek analiz hatasi: {e}")
        return None


def _fetch_haber_analysis(prompt):
    if not DEEPSEEK_API_KEY:
        return None
    try:
        messages = [{"role": "user", "content": prompt}]
        return _deepseek_chat(messages, max_tokens=800)
    except Exception as e:
        log.error(f"DeepSeek haber hatasi: {e}")
        return None


async def send_daily_analysis(app, symbols=None):
    """Sabah 09:00 TR saatinde gunluk analiz gonder"""
    global last_daily_analiz
    log.info("Gunluk analiz gonderiliyor...")
    symbols = symbols or list(favori_semboller)
    context = await get_market_context(symbols)
    if not context:
        await app.bot.send_message(chat_id=TG_CHAT_ID, text="Gunluk analiz icin veri alinamadi.")
        return

    # Her sembol icin ayri analiz
    for symbol in symbols:
        analysis = await asyncio.to_thread(generate_daily_analysis, symbol, context)
        if analysis:
            # Telegram 4096 karakter limiti - uzunsa bolu
            if len(analysis) > 4000:
                parts = [analysis[i:i+4000] for i in range(0, len(analysis), 4000)]
                for part in parts:
                    await app.bot.send_message(chat_id=TG_CHAT_ID, text=part)
                    await asyncio.sleep(1)
            else:
                await app.bot.send_message(chat_id=TG_CHAT_ID, text=analysis)
            await asyncio.sleep(2)
        else:
            await app.bot.send_message(
                chat_id=TG_CHAT_ID,
                text=f"{symbol} analizi olusturulamadi. DeepSeek ayarlarini kontrol et."
            )

    last_daily_analiz = datetime.utcnow().date()
    log.info("Gunluk analiz gonderildi!")
# ── ICT ANALİZ (v2 engine kullanir) ──────────────────────────
def analyze_ict(df, df_htf=None):
    """analyze_ict_v2 wrapper - mevcut kodu bozmamak icin"""
    return analyze_ict_v2(df, df_htf, min_rr=MIN_RR, min_confluence=MIN_CONFLUENCE)

def is_market_open():
    now = datetime.utcnow()
    if now.weekday() >= 5: return False
    return True

def get_session():
    s = get_active_session()
    return s or "Session Disi"

def is_kill_zone():
    return is_in_kill_zone()

def format_signal(symbol, sig):
    """Profesyonel ICT sinyal formati"""
    name = SYMBOLS.get(symbol, {}).get("name", symbol)
    direction = sig["direction"]
    conf = sig["conf"]
    checks = sig.get("checks", {})
    strength = sig.get("strength", "LOW")
    session = sig.get("session", get_session())
    rr = sig["rr"]

    # Precision: XAUUSD 2 decimal, forex 5, BTC 1
    prec = 2 if "XAU" in symbol else (1 if "BTC" in symbol else (1 if "QQQ" in symbol else 5))
    p = lambda v: f"{v:.{prec}f}"

    # Strength emoji
    if strength == "HIGH":
        str_emoji = "🔴 HIGH"
    elif strength == "MEDIUM":
        str_emoji = "🟡 MEDIUM"
    else:
        str_emoji = "🟢 LOW"

    # Confluence detay
    check_lines = []
    for label, passed in checks.items():
        mark = "✔" if passed else "✘"
        check_lines.append(f"  {mark} {label}")
    check_text = "\n".join(check_lines)

    dir_emoji = "📈" if direction == "LONG" else "📉"

    return (
        f"📊 PAIR: {name}\n"
        f"{dir_emoji} Direction: {direction}\n\n"
        f"Confluence: {conf}/6\n"
        f"{check_text}\n\n"
        f"Entry: {p(sig['price'])}\n"
        f"Stop: {p(sig['sl'])}\n"
        f"TP: {p(sig['tp'])}\n\n"
        f"RR: 1:{rr:.1f}\n\n"
        f"Session: {session}\n"
        f"Signal Strength: {str_emoji}\n\n"
        f"⚠️ Giris karari sana ait!"
    )

def _sinyal_butonlari(signal_id):
    """Sinyal mesajina Al / Gec butonlari"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Al", callback_data=f"sig_al_{signal_id}"),
            InlineKeyboardButton("⏭ Geç", callback_data=f"sig_gec_{signal_id}"),
        ],
    ])

# ── ANA DONGÜ ────────────────────────────────────────────────
async def scan_loop(app):
    global last_daily_analiz, last_daily_summary, last_weekly_summary, daily_trade_count, daily_trade_date
    log.info("Ana dongü basladi")

    while True:
        await asyncio.sleep(90)  # 90 sn - Twelve Data 8/dk limit

        now_tr   = datetime.utcnow() + timedelta(hours=3)
        bugun    = now_tr.date()
        saat     = now_tr.hour
        dakika   = now_tr.minute

        # Sabah 09:00 TR saatinde gunluk analiz gonder (haftaici)
        if (saat == 9 and 0 <= dakika < 10 and
            now_tr.weekday() < 5 and
            last_daily_analiz != bugun):
            await send_daily_analysis(app)

        # Kill Zone acilis/kapanis bildirimi
        await check_kill_zone_status(app)

        # Ekonomik takvim kontrolu
        await check_economic_calendar(app)

        # Fiyat alarmlari
        for al in list(fiyat_alarmlari):
            p = await aget_price(al["sembol"])
            if p is None: continue
            tetik = False
            if al["yon"] == "ust" and p >= al["hedef"]: tetik = True
            elif al["yon"] == "alt" and p <= al["hedef"]: tetik = True
            if tetik:
                try:
                    await app.bot.send_message(
                        chat_id=al["chat_id"],
                        text=f"🔔 *Fiyat alarmi!* {al['sembol']} {p:.4f} seviyesine ulasti (hedef: {al['hedef']})",
                        parse_mode="Markdown"
                    )
                    fiyat_alarmlari.remove(al)
                except: pass

        # Gunluk ozet (09:05 TR)
        if (saat == 9 and 5 <= dakika < 15 and now_tr.weekday() < 5 and last_daily_summary != bugun):
            await send_daily_summary(app)
            last_daily_summary = bugun

        # Haftalik ozet (Cuma 18:00 TR)
        if (saat == 18 and 0 <= dakika < 10 and now_tr.weekday() == 4 and last_weekly_summary != bugun):
            await send_weekly_summary(app)
            last_weekly_summary = bugun

        # TP/SL takip
        if aktif_sinyaller:
            await check_tp_sl(app)

        if not bot_active:
            continue
        if not is_market_open():
            continue

        # Sadece Kill Zone'da trade
        if kill_zone_only and not is_kill_zone():
            continue

        # Gunluk trade limiti reset
        today = datetime.utcnow().date()
        if daily_trade_date != today:
            daily_trade_count = 0
            daily_trade_date = today

        if daily_trade_count >= MAX_DAILY_TRADES:
            continue

        for symbol in SYMBOLS:
            if symbol not in favori_semboller:
                continue
            if daily_trade_count >= MAX_DAILY_TRADES:
                break
            cfg = get_symbol_cfg(symbol)
            if not cfg:
                continue
            try:
                last = last_signal_time.get(symbol)
                if last and (datetime.utcnow() - last).seconds < COOLDOWN_MIN * 60:
                    continue
                df_ltf = await aget_candles(symbol, cfg["interval"], 50)
                await asyncio.sleep(2)  # API limit: 8/dk - istekleri yay
                df_htf = await aget_htf_cached(symbol, cfg.get("htf", "15min"), 30)
                await asyncio.sleep(2)
                sig = analyze_ict(df_ltf, df_htf)
                if sig:
                    txt = format_signal(symbol, sig)
                    if len(results_history) >= 3 and results_history[-3:] == ["L", "L", "L"]:
                        txt = f"⚠️ Ardışık 3 kayıp! Daha seçici ol.\n\n{txt}"
                    sig_id = f"{symbol.replace('/', '_')}_{int(datetime.utcnow().timestamp())}"
                    signal_tracking[sig_id] = {"symbol": symbol, "sig": sig, "time": datetime.utcnow()}
                    await app.bot.send_message(
                        chat_id=TG_CHAT_ID, text=txt,
                        reply_markup=_sinyal_butonlari(sig_id)
                    )
                    last_signal_time[symbol] = datetime.utcnow()
                    aktif_sinyaller[symbol] = {
                        "id": sig_id,
                        "direction": sig["direction"],
                        "entry": sig["price"],
                        "sl": sig["sl"],
                        "tp": sig["tp"],
                        "time": datetime.utcnow()
                    }
                    _record_trade_open(symbol, sig, sig_id)
                    stats["total"] += 1
                    daily_trade_count += 1
                    log.info(f"Sinyal [{sig.get('strength','?')}]: {symbol} {sig['direction']} conf={sig['conf']}/6 RR=1:{sig['rr']:.1f}")
            except Exception as e:
                log.error(f"Scan hatasi {symbol}: {e}")

# ── KOMUTLAR ────────────────────────────────────────────────
def is_admin(uid): return uid in ADMIN_IDS

def _panel_main_msg():
    return "Warren Panel"

def _panel_main_kbd():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Durum", callback_data="panel_durum"),
            InlineKeyboardButton("🖥 Analiz", callback_data="panel_analiz"),
        ],
        [
            InlineKeyboardButton("🔍 Sinyal", callback_data="cmd_sinyal"),
            InlineKeyboardButton("📊 Dashboard", callback_data="cmd_dashboard"),
        ],
        [
            InlineKeyboardButton("📰 Haberler", callback_data="cmd_haber"),
            InlineKeyboardButton("📉 İstatistik", callback_data="cmd_istatistik"),
        ],
        [
            InlineKeyboardButton("📋 HTF Analiz", callback_data="cmd_htfanaliz"),
            InlineKeyboardButton("🧭 Mod", callback_data="cmd_mod"),
        ],
        [
            InlineKeyboardButton("🔁 Reset", callback_data="cmd_reset"),
            InlineKeyboardButton("▶ Aç", callback_data="cmd_ac"),
        ],
        [
            InlineKeyboardButton("⏹ Kapat", callback_data="cmd_kapat"),
            InlineKeyboardButton("👥 Grup", callback_data="panel_grup"),
        ],
    ])

def _panel_durum_msg():
    return "Warren Panel › Durum"

def _panel_durum_kbd():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🤖 Bot", callback_data="cmd_durum_bot"),
            InlineKeyboardButton("📡 Piyasa", callback_data="cmd_durum_piyasa"),
            InlineKeyboardButton("📊 Sinyal", callback_data="cmd_durum_sinyal"),
        ],
        [InlineKeyboardButton("◀ Geri", callback_data="panel")],
    ])

def _panel_analiz_msg():
    return "Warren Panel › Analiz"

_SYMBOL_MAP = {"XAUUSD": "XAU/USD", "QQQ": "QQQ", "XAGUSD": "XAG/USD", "GBPUSD": "GBP/USD"}

def _panel_analiz_kbd():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🥇 XAUUSD", callback_data="cmd_analiz_XAUUSD"),
            InlineKeyboardButton("📊 US100", callback_data="cmd_analiz_QQQ"),
        ],
        [
            InlineKeyboardButton(" XAGUSD", callback_data="cmd_analiz_XAGUSD"),
            InlineKeyboardButton(" BTCUSD", callback_data="cmd_analiz_BTCUSD"),
        ],
        [InlineKeyboardButton("◀ Geri", callback_data="panel")],
    ])

def _panel_grup_msg():
    return "Warren Panel › Grup"

def _panel_grup_kbd():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Kick", callback_data="cmd_kick"),
            InlineKeyboardButton("Ban", callback_data="cmd_ban"),
            InlineKeyboardButton("Unban", callback_data="cmd_unban"),
        ],
        [
            InlineKeyboardButton("Mute", callback_data="cmd_mute"),
            InlineKeyboardButton("Unmute", callback_data="cmd_unmute"),
        ],
        [
            InlineKeyboardButton("Uyar", callback_data="cmd_uyar"),
            InlineKeyboardButton("Uyarlar", callback_data="cmd_uyarlar"),
        ],
        [InlineKeyboardButton("◀ Geri", callback_data="panel")],
    ])

async def cmd_start(update, ctx):
    await update.message.reply_text(
        _panel_main_msg(),
        reply_markup=_panel_main_kbd()
    )

async def cmd_komutlar(update, ctx):
    """Komut listesi (butonlu) - yardim ile karismasin diye"""
    await cmd_start(update, ctx)

async def _run_cmd_via_callback(update, ctx, cmd_fn):
    """Callback icin: mesaj kaynagini cmd'ye uygun hale getir."""
    q = update.callback_query
    class _FakeUpdate:
        message = q.message
        effective_user = q.from_user
        effective_chat = q.message.chat
    await cmd_fn(_FakeUpdate(), ctx)

async def handle_button(update, ctx):
    global bot_active, TRADE_MODE, daily_trade_count, daily_trade_date, SELECTED_SYMBOL
    q = update.callback_query
    await q.answer()
    data = q.data
    target = q.message

    async def reply(txt):
        await ctx.bot.send_message(chat_id=target.chat_id, text=txt)

    async def edit_panel(msg, kbd):
        try:
            await q.edit_message_text(text=msg, reply_markup=kbd)
        except Exception:
            await reply(msg)

    # Sinyal Al / Gec
    if data and data.startswith("sig_"):
        parts = data.split("_", 2)
        if len(parts) >= 3:
            action = parts[1]
            if action == "al":
                await reply("✅ Sinyal alindi. Islem kapaninca /pnl ekle ile kaydet.")
            else:
                await reply("⏭ Gectin. Sonraki sinyalde gorusuruz.")
        return

    # Panel navigasyonu
    if data == "panel":
        await edit_panel(_panel_main_msg(), _panel_main_kbd())
        return
    if data == "panel_durum":
        await edit_panel(_panel_durum_msg(), _panel_durum_kbd())
        return
    if data == "panel_analiz":
        await edit_panel(_panel_analiz_msg(), _panel_analiz_kbd())
        return
    if data == "panel_grup":
        await edit_panel(_panel_grup_msg(), _panel_grup_kbd())
        return

    if not data or not data.startswith("cmd_"):
        return
    cmd = data[4:]

    if cmd == "durum" or cmd == "durum_bot":
        wr = stats["win"] / stats["total"] * 100 if stats["total"] else 0
        son_analiz = str(last_daily_analiz) if last_daily_analiz else "Henuz yok"
        await reply(
            f"Durum   : {'Aktif' if bot_active else 'Kapali'}\n"
            f"Piyasa  : {'Acik' if is_market_open() else 'KAPALI (Hafta Sonu)'}\n"
            f"Seans   : {get_session()}\n"
            f"Saat    : {datetime.utcnow().strftime('%H:%M UTC')}\n"
            f"Sinyal  : {stats['total']}  WR: %{wr:.1f}\n"
            f"Son Analiz: {son_analiz}"
        )
    elif cmd == "durum_piyasa":
        await reply(
            f"Piyasa  : {'Acik' if is_market_open() else 'KAPALI (Hafta Sonu)'}\n"
            f"Seans   : {get_session()}\n"
            f"Saat    : {datetime.utcnow().strftime('%H:%M UTC')}"
        )
    elif cmd == "durum_sinyal":
        wr = stats["win"] / stats["total"] * 100 if stats["total"] else 0
        lines = [f"Toplam: {stats['total']}  Kazan: {stats['win']}  Kaybet: {stats['loss']}\nWR: %{wr:.1f}", "\n*Sembol bazli:*"]
        for sym, s in stats_per_symbol.items():
            if s["total"] > 0:
                swr = s["win"] / s["total"] * 100
                lines.append(f"  {SYMBOLS.get(sym,{}).get(\"name\",sym)}: {s[\"total\"]} | W{s[\"win\"]} L{s[\"loss\"]} WR %{swr:.1f}")
        await reply("\\n".join(lines))
    elif cmd.startswith("analiz_"):
        sym = normalize_symbol(cmd[7:])
        if sym not in SYMBOLS:
            await reply("Gecersiz sembol.")
            return
        _set_selected_symbol(sym)
        cfg = get_symbol_cfg(sym)
        await reply(f"{sym} analiz ediliyor...")
        df_ltf = await aget_candles(sym, cfg["interval"], 50)
        df_htf = await aget_candles(sym, cfg.get("htf", "15min"), 30)
        if df_ltf is None:
            await reply("Veri alinamadi.")
            return
        sig = analyze_ict(df_ltf, df_htf)
        if sig:
            await reply(format_signal(sym, sig))
        else:
            await reply(f"{sym}: Setup yok, bekleniyor...")
    elif cmd == "sinyal":
        symbol = SELECTED_SYMBOL if SELECTED_SYMBOL in SYMBOLS else next(iter(SYMBOLS))
        await reply(format_trade_history(symbol, limit=5))
    elif cmd == "istatistik":
        wr = stats["win"] / stats["total"] * 100 if stats["total"] else 0
        lines = [f"Toplam: {stats['total']}  Kazan: {stats['win']}  Kaybet: {stats['loss']}\nWR: %{wr:.1f}", "\n*Sembol bazli:*"]
        for sym, s in stats_per_symbol.items():
            if s["total"] > 0:
                swr = s["win"] / s["total"] * 100
                lines.append(f"  {SYMBOLS.get(sym,{}).get('name',sym)}: {s['total']} | W{s['win']} L{s['loss']} WR %{swr:.1f}")
        await reply("\n".join(lines))
    elif cmd == "htfanaliz":
        await reply("Gunluk HTF analiz hazirlaniyor, 30 saniye bekle...")
        symbol = SELECTED_SYMBOL if SELECTED_SYMBOL in SYMBOLS else next(iter(SYMBOLS))
        await send_daily_analysis(ctx.application, [symbol])
    elif cmd == "haber":
        symbol = SELECTED_SYMBOL if SELECTED_SYMBOL in SYMBOLS else next(iter(SYMBOLS))
        await send_haber(ctx.bot, target.chat_id, symbol)
    elif cmd == "mod":
        if not is_admin(q.from_user.id):
            return
        TRADE_MODE = "SWING" if TRADE_MODE == "SCALP" else "SCALP"
        cfg = MODE_CONFIG.get(TRADE_MODE, MODE_CONFIG["SCALP"])
        await reply(f"Mod: {TRADE_MODE} | LTF: {cfg['interval']} HTF: {cfg['htf']}")
    elif cmd == "reset":
        if not is_admin(q.from_user.id):
            return
        daily_trade_count = 0
        daily_trade_date = datetime.utcnow().date()
        await reply("Gunluk trade limiti sifirlandi.")
    elif cmd == "ac":
        if not is_admin(q.from_user.id):
            return
        bot_active = True
        await reply("Bot aktif!")
    elif cmd == "kapat":
        if not is_admin(q.from_user.id):
            return
        bot_active = False
        await reply("Bot durduruldu. /ac ile baslatabilirsin.")
    elif cmd == "dashboard":
        session = get_session()
        wr = stats["win"] / stats["total"] * 100 if stats["total"] else 0
        son10 = " ".join(("✅" if r == "W" else "❌") for r in results_history[-10:]) if results_history else "—"
        aktif_list = []
        for sym, s in aktif_sinyaller.items():
            name = SYMBOLS.get(sym, {}).get("name", sym)
            aktif_list.append(f"  {name} {s['direction']}")
        aktif_text = "\n".join(aktif_list) if aktif_list else "  Yok"
        perf_lines = []
        for sym, s in stats_per_symbol.items():
            if s["total"] > 0:
                swr = s["win"] / s["total"] * 100
                perf_lines.append(f"  {SYMBOLS.get(sym,{}).get('name',sym)}: {s['total']}T WR%{swr:.0f}")
        perf_text = "\n".join(perf_lines) if perf_lines else "  —"
        await reply(
            f"━━ DASHBOARD ━━\n"
            f"📡 {'Aktif' if bot_active else 'Kapali'} | {session}\n"
            f"📊 Trade: {daily_trade_count}/{MAX_DAILY_TRADES}\n\n"
            f"Toplam: {stats['total']} | ✅{stats['win']} ❌{stats['loss']} WR%{wr:.0f}\n"
            f"Son 10: {son10}\n\n"
            f"{perf_text}\n\n"
            f"Aktif:\n{aktif_text}"
        )
    elif cmd == "equity":
        uid = q.from_user.id
        kayitlar = pnl_db.get(uid, [])
        if not kayitlar:
            await reply("Henuz islem yok. /pnl ekle ile kaydet.")
            return
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            cum = []
            s = 0
            for k in kayitlar:
                s += k["pnl"]
                cum.append(s)
            plt.figure(figsize=(8, 4))
            plt.plot(cum, color="#2ecc71", linewidth=2)
            plt.fill_between(range(len(cum)), cum, alpha=0.3)
            plt.axhline(0, color="gray", linestyle="--")
            plt.title("Equity Curve")
            plt.ylabel("Cumulative PnL ($)")
            buf = io.BytesIO()
            plt.savefig(buf, format="png", dpi=100)
            plt.close()
            buf.seek(0)
            await ctx.bot.send_photo(chat_id=target.chat_id, photo=buf)
        except Exception as e:
            await reply(f"Grafik hatasi: {e}")
    elif cmd == "haber":
        await reply("📰 Haberler analiz ediliyor...")
        prompt = (
            f"Tarih: {(datetime.utcnow() + timedelta(hours=3)).strftime('%d %B %Y %H:%M')} TR\n\n"
            "Piyasalari etkileyen guncel haberleri degerlendir. XAU/USD ve NAS100 icin:\n"
            "1. Genel sentiment (Bullish/Bearish/Notr)\n2. Risk faktorleri\n3. Kisa vadeli firsat/tehdit\nKisa yaz."
        )
        try:
            analiz = await asyncio.to_thread(_fetch_haber_analysis, prompt)
            if analiz:
                await reply(f"📰 Haber Analizi\n\n{analiz}")
            else:
                await reply("Haber analizi alinamadi.")
        except Exception as e:
            await reply(f"Hata: {e}")
    elif cmd in ("kick", "ban", "unban", "mute", "unmute", "uyar", "uyarlar"):
        if not is_admin(q.from_user.id):
            return
        await reply("Grup komutlari icin ilgili kisinin mesajina yanit verip /" + cmd + " yazin.")

async def cmd_durum(update, ctx):
    wr = stats["win"] / stats["total"] * 100 if stats["total"] else 0
    son_analiz = str(last_daily_analiz) if last_daily_analiz else "Henuz yok"
    await update.message.reply_text(
        f"Durum   : {'Aktif' if bot_active else 'Kapali'}\n"
        f"Piyasa  : {'Acik' if is_market_open() else 'KAPALI (Hafta Sonu)'}\n"
        f"Seans   : {get_session()}\n"
        f"Saat    : {datetime.utcnow().strftime('%H:%M UTC')}\n"
        f"Sinyal  : {stats['total']}  WR: %{wr:.1f}\n"
        f"Son Analiz: {son_analiz}"
    )

async def cmd_fiyat(update, ctx):
    lines = ["=== FIYATLAR ==="]
    for symbol, cfg in SYMBOLS.items():
        p = await aget_price(symbol)
        lines.append(f"{cfg['name']:8}: {p:.4f}" if p else f"{cfg['name']:8}: Alinamadi")
    await update.message.reply_text("\n".join(lines))

async def cmd_analiz(update, ctx):
    symbol = normalize_symbol(ctx.args[0]) if ctx.args else SELECTED_SYMBOL
    if symbol not in SYMBOLS:
        await update.message.reply_text(f"Gecersiz. Secenekler: {', '.join(SYMBOLS)}")
        return
    _set_selected_symbol(symbol)
    cfg = get_symbol_cfg(symbol)
    await update.message.reply_text(
        f"{symbol} analiz ediliyor (Mod: {TRADE_MODE} | LTF: {cfg['interval']} + HTF: {cfg.get('htf','15min')})..."
    )
    df_ltf = await aget_candles(symbol, cfg["interval"], 50)
    df_htf = await aget_candles(symbol, cfg.get("htf", "15min"), 30)
    if df_ltf is None:
        await update.message.reply_text("Veri alinamadi.")
        return
    sig = analyze_ict(df_ltf, df_htf)
    if sig:
        await update.message.reply_text(format_signal(symbol, sig))
    else:
        await update.message.reply_text(f"{symbol}: Setup yok, bekleniyor... ({get_session()})")
async def cmd_istatistik(update, ctx):
    wr = stats["win"] / stats["total"] * 100 if stats["total"] else 0
    lines = [f"Toplam: {stats['total']}  Kazan: {stats['win']}  Kaybet: {stats['loss']}\nWR: %{wr:.1f}", "\n*Sembol bazli:*"]
    for sym, s in stats_per_symbol.items():
        if s["total"] > 0:
            swr = s["win"] / s["total"] * 100
            lines.append(f"  {SYMBOLS.get(sym,{}).get('name',sym)}: {s['total']} | W{s['win']} L{s['loss']} WR %{swr:.1f}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_sinyal(update, ctx):
    global last_signal_time

    if ctx.args and ctx.args[0].lower() in ("tara", "scan"):
        symbol_arg = normalize_symbol(ctx.args[1]) if len(ctx.args) > 1 else None
        scan_symbols = [symbol_arg] if symbol_arg else list(favori_semboller)
        await update.message.reply_text(f"Taranıyor: {', '.join(scan_symbols)}...")
        last_signal_time = {}
        found = False
        for symbol in scan_symbols:
            cfg = get_symbol_cfg(symbol)
            if not cfg:
                continue
            df_ltf = await aget_candles(symbol, cfg["interval"], 50)
            df_htf = await aget_candles(symbol, cfg.get("htf", "15min"), 30)
            sig = analyze_ict(df_ltf, df_htf)
            if sig:
                txt = format_signal(symbol, sig)
                if len(results_history) >= 3 and results_history[-3:] == ["L", "L", "L"]:
                    txt = f"⚠️ *UYARI:* Ardışık 3 kayıp! Daha seçici ol.\n\n{txt}"
                sig_id = f"{symbol.replace('/', '_')}_{int(datetime.utcnow().timestamp())}"
                await update.message.reply_text(txt, reply_markup=_sinyal_butonlari(sig_id), parse_mode="Markdown")
                stats["total"] += 1
                last_signal_time[symbol] = datetime.utcnow()
                found = True
        if not found:
            await update.message.reply_text("Setup yok, bekleniyor...")
        return

    symbol = normalize_symbol(ctx.args[0]) if ctx.args else SELECTED_SYMBOL
    if symbol not in SYMBOLS:
        await update.message.reply_text(f"Gecersiz. Secenekler: {', '.join(SYMBOLS)}")
        return
    _set_selected_symbol(symbol)
    await update.message.reply_text(format_trade_history(symbol, limit=5))

async def cmd_htfanaliz(update, ctx):
    """Manuel gunluk analiz tetikle"""
    symbol = SELECTED_SYMBOL if SELECTED_SYMBOL in SYMBOLS else next(iter(SYMBOLS))
    await update.message.reply_text("Gunluk HTF analiz hazirlaniyor, 30 saniye bekle...")
    await send_daily_analysis(ctx.application, [symbol])

async def cmd_ac(update, ctx):
    global bot_active, TRADE_MODE, daily_trade_count, daily_trade_date, SELECTED_SYMBOL
    if not is_admin(update.effective_user.id):
        return
    bot_active = True
    await update.message.reply_text("Bot aktif!")

async def cmd_kapat(update, ctx):
    global bot_active, TRADE_MODE, daily_trade_count, daily_trade_date, SELECTED_SYMBOL
    if not is_admin(update.effective_user.id):
        return
    bot_active = False
    await update.message.reply_text("Bot durduruldu. /ac ile baslatabilirsin.")

# ── GRUP YÖNETİMİ ────────────────────────────────────────────
async def get_target(update, ctx, user_token=None):
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user

    token = user_token
    if token is None and ctx.args:
        token = ctx.args[0]
    if token:
        token = token.lstrip("@")
        if token.isdigit():
            uid = int(token)
            try:
                member = await ctx.bot.get_chat_member(update.effective_chat.id, uid)
                return member.user
            except:
                await update.message.reply_text(f"Kullanici bulunamadi: {uid}")
                return None
        try:
            chat = await ctx.bot.get_chat(f"@{token}")
            try:
                member = await ctx.bot.get_chat_member(update.effective_chat.id, chat.id)
                return member.user
            except:
                return chat
        except:
            await update.message.reply_text(f"Kullanici bulunamadi: @{token}")
            return None

    await update.message.reply_text("Kullanici belirt: reply yap veya @username ya da user id yaz.")
    return None

async def cmd_kick(update, ctx):
    if not is_admin(update.effective_user.id): return
    t = await get_target(update, ctx)
    if not t: return
    try:
        await ctx.bot.ban_chat_member(update.effective_chat.id, t.id)
        await ctx.bot.unban_chat_member(update.effective_chat.id, t.id)
        await update.message.reply_text(f"{t.first_name} atildi.")
    except Exception as e: await update.message.reply_text(f"Hata: {e}")

async def cmd_ban(update, ctx):
    if not is_admin(update.effective_user.id): return
    t = await get_target(update, ctx)
    if not t: return
    try:
        await ctx.bot.ban_chat_member(update.effective_chat.id, t.id)
        await update.message.reply_text(f"{t.first_name} banlandi.")
    except Exception as e: await update.message.reply_text(f"Hata: {e}")

async def cmd_unban(update, ctx):
    if not is_admin(update.effective_user.id): return
    t = await get_target(update, ctx)
    if not t: return
    try:
        await ctx.bot.unban_chat_member(update.effective_chat.id, t.id)
        await update.message.reply_text(f"{t.first_name} bani kaldirildi.")
    except Exception as e: await update.message.reply_text(f"Hata: {e}")

async def cmd_mute(update, ctx):
    if not is_admin(update.effective_user.id): return
    dakika = 10
    token = None

    if update.message.reply_to_message:
        if ctx.args and ctx.args[0].isdigit():
            dakika = int(ctx.args[0])
    else:
        if ctx.args:
            if ctx.args[-1].isdigit():
                dakika = int(ctx.args[-1])
                if len(ctx.args) > 1:
                    token = ctx.args[0]
            else:
                token = ctx.args[0]

    t = await get_target(update, ctx, token)
    if not t: return
    try:
        await ctx.bot.restrict_chat_member(
            update.effective_chat.id, t.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.utcnow() + timedelta(minutes=dakika)
        )
        await update.message.reply_text(f"{t.first_name} {dakika}dk susturuldu.")
    except Exception as e: await update.message.reply_text(f"Hata: {e}")

async def cmd_unmute(update, ctx):
    if not is_admin(update.effective_user.id): return
    t = await get_target(update, ctx)
    if not t: return
    try:
        await ctx.bot.restrict_chat_member(
            update.effective_chat.id, t.id,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_other_messages=True,
                can_add_web_page_previews=True)
        )
        await update.message.reply_text(f"{t.first_name} sesi acildi.")
    except Exception as e: await update.message.reply_text(f"Hata: {e}")

async def cmd_uyar(update, ctx):
    if not is_admin(update.effective_user.id): return
    t = await get_target(update, ctx)
    if not t: return
    sebep = " ".join(ctx.args) if ctx.args else "Kural ihlali"
    warnings_db[t.id] = warnings_db.get(t.id, 0) + 1
    count = warnings_db[t.id]
    msg = f"{t.first_name} uyarildi! ({count}/3)\nSebep: {sebep}"
    if count >= 3:
        try:
            await ctx.bot.ban_chat_member(update.effective_chat.id, t.id)
            msg += "\n3 uyariya ulasti - BANLANDI!"; warnings_db[t.id] = 0
        except Exception as e: msg += f"\nBan hatasi: {e}"
    await update.message.reply_text(msg)

async def cmd_uyarlar(update, ctx):
    t = await get_target(update, ctx)
    if not t: return
    await update.message.reply_text(f"{t.first_name}: {warnings_db.get(t.id, 0)}/3 uyari")

async def spam_check(update, ctx):
    if not update.effective_user or is_admin(update.effective_user.id): return
    uid = update.effective_user.id; now = datetime.utcnow().timestamp()
    message_counts.setdefault(uid, [])
    message_counts[uid] = [t for t in message_counts[uid] if now - t < 10]
    message_counts[uid].append(now)
    if len(message_counts[uid]) > 8:
        try:
            await ctx.bot.restrict_chat_member(
                update.effective_chat.id, uid,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.utcnow() + timedelta(minutes=5)
            )
            await update.message.reply_text(f"{update.effective_user.first_name} spam - 5dk mute.")
            message_counts[uid] = []
        except: pass

async def cmd_pnl_dispatcher(update, ctx):
    """/pnl ekle | liste | sifirla"""
    if not ctx.args:
        await update.message.reply_text(
            "📊 *PnL Komutları*\n\n"
            "`/pnl ekle SEMBOL YON GIRIS CIKIS LOT [sebep]`\n"
            "`/pnl liste` - Özet gör\n"
            "`/pnl journal` - Detaylı işlem listesi\n"
            "`/pnl sifirla` - Kayıtları temizle\n\n"
            "Örnek: `/pnl ekle XAUUSD LONG 1950 1970 0.1 ICT Long`",
            parse_mode="Markdown"
        )
        return
    alt = ctx.args[0].lower()
    ctx.args = ctx.args[1:]
    if alt == "ekle":
        await cmd_pnl_ekle(update, ctx)
    elif alt == "liste":
        await cmd_pnl_liste(update, ctx)
    elif alt == "journal":
        await cmd_pnl_journal(update, ctx)
    elif alt == "sifirla":
        await cmd_pnl_sifirla(update, ctx)
    else:
        await update.message.reply_text("Geçersiz komut. `/pnl` yaz.", parse_mode="Markdown")

async def welcome(update, ctx):
    for m in update.message.new_chat_members:
        if not m.is_bot:
            await update.message.reply_text(f"Hos geldin {m.first_name}! ICT sinyal grubuna katildin.")

# ── PNL KOMUTLARI ────────────────────────────────────────────
async def cmd_pnl_ekle(update, ctx):
    """Kullanim: /pnl ekle XAUUSD LONG 1950.00 1970.00 0.1 [sebep]"""
    uid = update.effective_user.id
    args = ctx.args
    if len(args) < 5:
        await update.message.reply_text(
            "Kullanim: `/pnl ekle SEMBOL YON GIRIS CIKIS LOT [sebep]`\n"
            "Ornek: `/pnl ekle XAUUSD LONG 1950.00 1970.00 0.1 ICT Long`",
            parse_mode="Markdown"
        )
        return
    try:
        sembol = args[0].upper()
        yon    = args[1].upper()
        giris  = float(args[2])
        cikis  = float(args[3])
        lot    = float(args[4])
        sebep  = " ".join(args[5:]) if len(args) > 5 else ""

        # PnL hesapla (Gold icin pip degeri ~10 USD/lot)
        fark = (cikis - giris) if yon == "LONG" else (giris - cikis)
        if "XAU" in sembol or "GOLD" in sembol:
            pnl = fark * lot * 100
        elif "US100" in sembol or "QQQ" in sembol or "NAS" in sembol:
            pnl = fark * lot * 10
        else:
            pnl = fark * lot * 100000 * 0.0001  # Forex pip

        kayit = {
            "sembol": sembol, "yon": yon, "giris": giris,
            "cikis": cikis, "lot": lot, "pnl": round(pnl, 2),
            "tarih": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            "sebep": sebep
        }
        if uid not in pnl_db:
            pnl_db[uid] = []
        pnl_db[uid].append(kayit)

        emoji = "✅" if pnl > 0 else "❌"
        await update.message.reply_text(
            f"{emoji} *İşlem Kaydedildi*\n\n"
            f"Sembol: `{sembol}` | Yön: `{yon}`\n"
            f"Giriş: `{giris}` → Çıkış: `{cikis}`\n"
            f"Lot: `{lot}` | P/L: `${pnl:+.2f}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def cmd_pnl_liste(update, ctx):
    uid = update.effective_user.id
    kayitlar = pnl_db.get(uid, [])
    if not kayitlar:
        await update.message.reply_text("Henuz islem kaydedilmemis. `/pnl ekle` ile ekle.", parse_mode="Markdown")
        return

    toplam = sum(k["pnl"] for k in kayitlar)
    kazanan = sum(1 for k in kayitlar if k["pnl"] > 0)
    kaybeden = len(kayitlar) - kazanan
    wr = (kazanan / len(kayitlar) * 100) if kayitlar else 0

    satirlar = [f"*📊 PnL Raporu* ({len(kayitlar)} islem)\n"]
    for k in kayitlar[-10:]:  # Son 10
        emoji = "✅" if k["pnl"] > 0 else "❌"
        satirlar.append(f"{emoji} {k['sembol']} {k['yon']} `${k['pnl']:+.2f}`")

    satirlar.append(f"\n💰 Toplam: `${toplam:+.2f}`")
    satirlar.append(f"🎯 Win Rate: `%{wr:.1f}` ({kazanan}W / {kaybeden}L)")

    await update.message.reply_text("\n".join(satirlar), parse_mode="Markdown")

async def cmd_pnl_journal(update, ctx):
    """Trade journal - detayli islem listesi"""
    uid = update.effective_user.id
    kayitlar = pnl_db.get(uid, [])
    if not kayitlar:
        await update.message.reply_text("Henuz islem yok.")
        return
    satirlar = ["*📓 Trade Journal*\n"]
    for k in kayitlar[-15:]:
        emoji = "✅" if k["pnl"] > 0 else "❌"
        sebep = f" | {k['sebep']}" if k.get("sebep") else ""
        satirlar.append(f"{emoji} {k['tarih']} {k['sembol']} {k['yon']} `${k['pnl']:+.2f}`{sebep}")
    await update.message.reply_text("\n".join(satirlar), parse_mode="Markdown")

async def cmd_pnl_sifirla(update, ctx):
    uid = update.effective_user.id
    pnl_db[uid] = []
    await update.message.reply_text("🗑️ PnL kayitlari silindi.")

# ── FAVORİ, ALARM, SEANS, EQUITY ───────────────────────────
async def cmd_favori(update, ctx):
    """/favori XAUUSD QQQ - Taranacak sembolleri sec"""
    global favori_semboller
    if not is_admin(update.effective_user.id):
        return
    args = ctx.args
    if not args:
        mevcut = ", ".join(SYMBOLS.get(s, {}).get("name", s) for s in favori_semboller)
        await update.message.reply_text(f"Favori semboller: {mevcut}\n\nKullanim: /favori XAUUSD QQQ (bos = hepsi)")
        return
    yeni = set()
    for a in args:
        sym = normalize_symbol(a)
        if sym:
            yeni.add(sym)
    if yeni:
        favori_semboller = yeni
        if SELECTED_SYMBOL not in favori_semboller:
            _set_selected_symbol(next(iter(favori_semboller)))
        await update.message.reply_text(f"Favori: {', '.join(SYMBOLS.get(s,{}).get('name',s) for s in favori_semboller)}")
    else:
        favori_semboller = set(SYMBOLS.keys())
        await update.message.reply_text("Favori: Tum semboller")

async def cmd_alarm(update, ctx):(update, ctx):
    """//alarm XAUUSD 2650 ust - Fiyat 2650'ye ulasinca uyari"""
    if not ctx.args or len(ctx.args) < 3:
        await update.message.reply_text("Kullanim: /alarm SEMBOL FIYAT ust|alt\nOrnek: /alarm XAUUSD 2650 ust")
        return
    sym = ctx.args[0].upper().replace(" ", "/")
    if sym == "XAUUSD": sym = "XAU/USD"
    elif sym in ("QQQ", "US100"): sym = "QQQ"
    elif sym == "XAGUSD": sym = "XAG/USD"
    elif sym == "GBPUSD": sym = "GBP/USD"
    if sym not in SYMBOLS:
        await update.message.reply_text("Gecersiz sembol. XAUUSD, QQQ, XAGUSD, GBPUSD")
        return
    try:
        hedef = float(ctx.args[1])
        yon = ctx.args[2].lower()
        if yon not in ("ust", "alt"):
            raise ValueError()
    except:
        await update.message.reply_text("Fiyat sayi olmali, yon: ust veya alt")
        return
    fiyat_alarmlari.append({"sembol": sym, "hedef": hedef, "yon": yon, "chat_id": update.effective_chat.id})
    await update.message.reply_text(f"Alarm eklendi: {sym} {hedef} {yon}")

async def cmd_seans(update, ctx):
    """Kill Zone filtresini ac/kapat"""
    global kill_zone_only
    if not is_admin(update.effective_user.id):
        return
    kill_zone_only = not kill_zone_only
    durum = "acik" if kill_zone_only else "kapali"
    await update.message.reply_text(f"Kill Zone filtresi: {durum}")

async def cmd_mod(update, ctx):
    """Trade modu degistir: /mod scalp|swing"""
    global TRADE_MODE
    if not is_admin(update.effective_user.id):
        return
    if ctx.args:
        arg = ctx.args[0].lower()
        if arg in ("scalp", "s"):
            TRADE_MODE = "SCALP"
        elif arg in ("swing", "w"):
            TRADE_MODE = "SWING"
    else:
        TRADE_MODE = "SWING" if TRADE_MODE == "SCALP" else "SCALP"
    cfg = MODE_CONFIG.get(TRADE_MODE, MODE_CONFIG["SCALP"])
    await update.message.reply_text(f"Mod: {TRADE_MODE} | LTF: {cfg[\"interval\"]} HTF: {cfg[\"htf\"]}")


async def cmd_reset(update, ctx):
    """Gunluk trade limitini sifirla"""
    global daily_trade_count, daily_trade_date
    if not is_admin(update.effective_user.id):
        return
    daily_trade_count = 0
    daily_trade_date = datetime.utcnow().date()
    await update.message.reply_text("Gunluk trade limiti sifirlandi.")


async def cmd_equity(update, ctx):
    """Equity curve grafigi - PnL verisinden"""
    uid = update.effective_user.id
    kayitlar = pnl_db.get(uid, [])
    if not kayitlar:
        await update.message.reply_text("Henuz islem yok. /pnl ekle ile kaydet.")
        return
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        cum = []
        s = 0
        for k in kayitlar:
            s += k["pnl"]
            cum.append(s)
        plt.figure(figsize=(8, 4))
        plt.plot(cum, color="#2ecc71", linewidth=2)
        plt.fill_between(range(len(cum)), cum, alpha=0.3)
        plt.axhline(0, color="gray", linestyle="--")
        plt.title("Equity Curve")
        plt.ylabel("Cumulative PnL ($)")
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100)
        plt.close()
        buf.seek(0)
        await update.message.reply_photo(photo=buf)
    except Exception as e:
        await update.message.reply_text(f"Grafik hatasi: {e}")

# ── DASHBOARD ────────────────────────────────────────────────
async def cmd_dashboard(update, ctx):
    """Profesyonel trading dashboard"""
    now = datetime.utcnow()
    session = get_session()
    wr = stats["win"] / stats["total"] * 100 if stats["total"] else 0
    toplam_rr = sum(1 for r in results_history if r == "W") * MIN_RR - sum(1 for r in results_history if r == "L")

    # Aktif sinyaller
    aktif_lines = []
    for sym, s in aktif_sinyaller.items():
        name = SYMBOLS.get(sym, {}).get("name", sym)
        p = await aget_price(sym)
        if p:
            pnl_pips = abs(p - s["entry"])
            emoji = "🟢" if (s["direction"] == "LONG" and p > s["entry"]) or (s["direction"] == "SHORT" and p < s["entry"]) else "🔴"
            aktif_lines.append(f"  {emoji} {name} {s['direction']} | {pnl_pips:.1f} pip")
    aktif_text = "\n".join(aktif_lines) if aktif_lines else "  Yok"

    # Sembol bazli performans
    perf_lines = []
    for sym, s in stats_per_symbol.items():
        if s["total"] > 0:
            swr = s["win"] / s["total"] * 100
            perf_lines.append(f"  {SYMBOLS.get(sym,{}).get('name',sym):8} {s['total']}T  W{s['win']} L{s['loss']}  WR%{swr:.0f}")
    perf_text = "\n".join(perf_lines) if perf_lines else "  Henuz islem yok"

    # Son 10 islem
    son10 = " ".join(("✅" if r == "W" else "❌") for r in results_history[-10:]) if results_history else "—"

    txt = (
        f"━━━ WARREN DASHBOARD ━━━\n\n"
        f"📡 Durum: {'Aktif' if bot_active else 'Kapali'}\n"
        f"🕐 {now.strftime('%H:%M UTC')} | {session}\n"
        f"📊 Gunluk Trade: {daily_trade_count}/{MAX_DAILY_TRADES}\n\n"
        f"━━━ PERFORMANS ━━━\n"
        f"Toplam: {stats['total']} | ✅{stats['win']} ❌{stats['loss']}\n"
        f"Win Rate: %{wr:.1f}\n"
        f"Net R: {toplam_rr:+.1f}R\n"
        f"Son 10: {son10}\n\n"
        f"━━━ SEMBOL BAZLI ━━━\n"
        f"{perf_text}\n\n"
        f"━━━ AKTİF SİNYALLER ━━━\n"
        f"{aktif_text}\n\n"
        f"━━━ AYARLAR ━━━\n"
        f"Min RR: 1:{MIN_RR} | Min Conf: {MIN_CONFLUENCE}/6\n"
        f"Risk: %{RISK_PER_TRADE*100:.0f}/trade | Max: %{MAX_DAILY_RISK*100:.0f}/gun\n"
        f"Kill Zone Only: {'Evet' if kill_zone_only else 'Hayir'}"
    )
    await update.message.reply_text(txt)

# ── HABER SENTİMENT ANALİZİ ─────────────────────────────────
async def send_haber(bot, chat_id, symbol):
    """Parite bazli haber sentiment analizi"""
    await asyncio.sleep(0)
    now_tr = datetime.utcnow() + timedelta(hours=3)
    symbol_name = SYMBOLS.get(symbol, {}).get("name", symbol)
    prompt = (
        f"Tarih: {now_tr.strftime('%d %B %Y %H:%M')} TR saati\n\n"
        f"Sembol: {symbol_name}\n\n"
        "Şu an piyasaları etkileyen güncel haberleri ve makroekonomik ortamı değerlendir. "
        "Aşağıdakileri kısa ve ICT perspektifinden yaz:\n"
        "1. Genel piyasa sentiment'i (Bullish/Bearish/Nötr)\n"
        "2. Risk faktörleri\n"
        "3. Kısa vadeli fırsat/tehdit"
    )

    try:
        analiz = await asyncio.to_thread(_fetch_haber_analysis, prompt)
        if analiz:
            await bot.send_message(chat_id=chat_id, text=f"📰 *Haber Sentiment Analizi* ({symbol_name})\n\n{analiz}", parse_mode="Markdown")
        else:
            await bot.send_message(chat_id=chat_id, text="Haber analizi alinamadi.")
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"❌ Hata: {e}")


async def cmd_haber(update, ctx):
    """Parite bazli haber sentiment analizi"""
    symbol = normalize_symbol(ctx.args[0]) if ctx.args else SELECTED_SYMBOL
    if symbol not in SYMBOLS:
        await update.message.reply_text(f"Gecersiz. Secenekler: {', '.join(SYMBOLS)}")
        return
    _set_selected_symbol(symbol)
    await update.message.reply_text("📰 Haberler analiz ediliyor...")
    await send_haber(ctx.bot, update.effective_chat.id, symbol)

async def cmd_takvim(update, ctx):
    """Bugunun onemli ekonomik olaylarini goster"""
    now_utc = datetime.utcnow()
    now_tr  = now_utc + timedelta(hours=3)

    metin = f"📅 *Ekonomik Takvim* ({now_tr.strftime('%d.%m.%Y')})\n\n"
    metin += "⚠️ Yüksek etkili olaylardan 15dk önce işlem açma!\n\n"
    metin += "🔴 FOMC, NFP, CPI → Gold/NAS volatilite yüksek\n"
    metin += "🟡 PMI, ISM, Retail Sales → Orta etki\n\n"
    metin += "📌 Detaylı takvim: investing.com/economic-calendar\n"
    metin += "\n*Otomatik uyarı:* Piyasa açılışında aktif 🟢"

    await update.message.reply_text(metin, parse_mode="Markdown")

# ── TP/SL TAKİPÇİSİ ─────────────────────────────────────────
async def check_tp_sl(app):
    """Aktif sinyallerin TP/SL'e ulaşıp ulaşmadığını kontrol et"""
    global aktif_sinyaller, stats_per_symbol, results_history
    kapatilacak = []

    for symbol, sig in aktif_sinyaller.items():
        price = await aget_price(symbol)
        if not price:
            continue

        direction = sig["direction"]
        tp = sig["tp"]
        sl = sig["sl"]
        entry = sig["entry"]
        name = SYMBOLS.get(symbol, {}).get("name", symbol)
        hit = None
        if direction == "LONG":
            if price >= tp:
                hit = "TP"
            elif price <= sl:
                hit = "SL"
        else:
            if price <= tp:
                hit = "TP"
            elif price >= sl:
                hit = "SL"

        if hit:
            trade_id = sig.get("id")
            if trade_id:
                _record_trade_close(symbol, trade_id, hit, price)
            pnl_pips = abs(tp - entry) if hit == "TP" else abs(sl - entry)
            emoji = "✅" if hit == "TP" else "❌"
            sonuc = "KAZANÇ" if hit == "TP" else "KAYIP"

            if hit == "TP":
                stats["win"] += 1
                results_history.append("W")
            else:
                stats["loss"] += 1
                results_history.append("L")
            if len(results_history) > 50:
                results_history[:] = results_history[-50:]
            # Sembol bazli istatistik
            if symbol not in stats_per_symbol:
                stats_per_symbol[symbol] = {"total": 0, "win": 0, "loss": 0}
            stats_per_symbol[symbol]["total"] += 1
            if hit == "TP":
                stats_per_symbol[symbol]["win"] += 1
            else:
                stats_per_symbol[symbol]["loss"] += 1

            mesaj = (
                f"{emoji} *{hit} HIT - {sonuc}*\n"
                f"{'='*20}\n"
                f"Sembol  : {name} ({symbol})\n"
                f"Yön     : {direction}\n"
                f"Giriş   : {entry:.4f}\n"
                f"Kapanış : {price:.4f}\n"
                f"Fark    : {pnl_pips:.1f} pip\n"
                f"{'='*20}\n"
                f"📊 Toplam: {stats['total']} | ✅{stats['win']} ❌{stats['loss']}"
            )
            try:
                await app.bot.send_message(chat_id=TG_CHAT_ID, text=mesaj, parse_mode="Markdown")
                log.info(f"{symbol} {hit} hit @ {price}")
            except Exception as e:
                log.error(f"TP/SL mesaj hatasi: {e}")
            kapatilacak.append(symbol)

    for s in kapatilacak:
        aktif_sinyaller.pop(s, None)

# ── BACKTEST ─────────────────────────────────────────────────
async def cmd_backtest(update, ctx):
    """Son 100 mumda ICT stratejisi backtesti"""
    args = ctx.args
    symbol = args[0].upper() if args else "XAU/USD"

    sym_map = {"XAUUSD": "XAU/USD", "US100": "QQQ", "NAS100": "QQQ", "BTCUSD": "BTC/USD", "XAGUSD": "XAG/USD"}
    symbol = sym_map.get(symbol, symbol)
    if symbol not in SYMBOLS:
        await update.message.reply_text(f"Gecersiz. Secenekler: {', '.join(SYMBOLS)}"); return

    cfg = get_symbol_cfg(symbol)
    if not cfg:
        await update.message.reply_text("Konfig bulunamadi.")
        return
    await update.message.reply_text(f"⏳ {symbol} icin backtest calisiyor... (100 mum)")

    try:
        df = await aget_candles(symbol, cfg["interval"], 100)
        df_htf = await aget_candles(symbol, cfg.get("htf", "15min"), 30)
        if df is None or len(df) < 30:
            await update.message.reply_text("❌ Veri alinamadi.")
            return

        wins = losses = 0
        toplam_rr = 0.0
        islemler = []

        for i in range(20, len(df) - 5):
            parca = df.iloc[:i+1].reset_index(drop=True)
            sig = analyze_ict(parca, df_htf)
            if not sig:
                continue

            entry = sig["price"]
            tp = sig["tp"]
            sl = sig["sl"]
            direction = sig["direction"]

            # Sonraki 5 mumda sonucu simüle et
            gelecek = df.iloc[i+1:i+6]
            sonuc = None
            for _, mum in gelecek.iterrows():
                if direction == "LONG":
                    if mum["h"] >= tp:
                        sonuc = "WIN"; break
                    elif mum["l"] <= sl:
                        sonuc = "LOSS"; break
                else:
                    if mum["l"] <= tp:
                        sonuc = "WIN"; break
                    elif mum["h"] >= sl:
                        sonuc = "LOSS"; break

            if sonuc == "WIN":
                wins += 1
                toplam_rr += sig["rr"]
                islemler.append(("✅", sig["rr"]))
            elif sonuc == "LOSS":
                losses += 1
                toplam_rr -= 1.0
                islemler.append(("❌", -1.0))

        toplam = wins + losses
        wr = (wins / toplam * 100) if toplam else 0
        ort_rr = (toplam_rr / toplam) if toplam else 0
        name = SYMBOLS.get(symbol, {}).get("name", symbol)

        son10 = " ".join(f"{e}" for e, _ in islemler[-10:]) if islemler else "Sinyal yok"

        rapor = (
            f"📊 *Backtest Raporu - {name}*\n"
            f"{'='*24}\n"
            f"Toplam İşlem : {toplam}\n"
            f"Kazanan      : {wins} ✅\n"
            f"Kaybeden     : {losses} ❌\n"
            f"Win Rate     : %{wr:.1f}\n"
            f"Ort. R:R     : {ort_rr:.2f}\n"
            f"Net R        : {toplam_rr:+.1f}R\n"
            f"{'='*24}\n"
            f"Son 10: {son10}"
        )
        await update.message.reply_text(rapor, parse_mode="Markdown")

    except Exception as e:
        log.error(f"Backtest hatasi: {e}")
        await update.message.reply_text(f"❌ Backtest hatası: {e}")

async def check_kill_zone_status(app):
    """Kill Zone acilis/kapanis bildirimi"""
    global son_kz_durum

    if not is_market_open():
        return

    from ict_engine import KILL_ZONES
    now_utc = datetime.utcnow()
    h = now_utc.hour
    m = now_utc.minute
    tr_saat = h + 3

    for key, kz in KILL_ZONES.items():
        kz_name = kz["name"]
        start_h = kz["start"]
        end_h = kz["end"]

        # Acilis: tam saat basinda (dakika 0)
        if h == start_h and m == 0:
            anahtar = f"acilis_{key}_{now_utc.date()}"
            if son_kz_durum == anahtar:
                continue
            son_kz_durum = anahtar
            tr_start = start_h + 3
            tr_end = end_h + 3
            try:
                await app.bot.send_message(
                    chat_id=TG_CHAT_ID,
                    text=(
                        f"🟢 *{kz_name} AÇILDI*\n\n"
                        f"🕐 {tr_start:02d}:00 - {tr_end:02d}:00 TR\n"
                        f"📡 Sinyal tarama aktif\n"
                        f"⚡ Yüksek kaliteli setup'ları bekliyorum..."
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                log.error(f"KZ acilis mesaj hatasi: {e}")
            return

        # Kapanis: tam saat basinda (dakika 0)
        if h == end_h and m == 0:
            anahtar = f"kapanis_{key}_{now_utc.date()}"
            if son_kz_durum == anahtar:
                continue
            son_kz_durum = anahtar
            try:
                await app.bot.send_message(
                    chat_id=TG_CHAT_ID,
                    text=(
                        f"🔴 *{kz_name} KAPANDI*\n\n"
                        f"📊 Sinyal tarama durduruldu\n"
                        f"📋 Açık pozisyonlarını kontrol et"
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                log.error(f"KZ kapanis mesaj hatasi: {e}")
            return

async def send_daily_summary(app):
    """Dunun performans ozeti"""
    if stats["total"] == 0:
        return
    wr = stats["win"] / stats["total"] * 100
    satirlar = [
        "📊 *Gunluk Ozet*",
        f"Toplam: {stats['total']} | ✅{stats['win']} ❌{stats['loss']} | WR: %{wr:.1f}",
    ]
    for sym, s in stats_per_symbol.items():
        if s["total"] > 0:
            swr = s["win"] / s["total"] * 100
            satirlar.append(f"  {SYMBOLS.get(sym,{}).get('name',sym)}: {s['total']} islem WR %{swr:.1f}")
    try:
        await app.bot.send_message(chat_id=TG_CHAT_ID, text="\n".join(satirlar), parse_mode="Markdown")
    except: pass

async def send_weekly_summary(app):
    """Haftalik performans ozeti"""
    if stats["total"] == 0:
        return
    wr = stats["win"] / stats["total"] * 100
    satirlar = [
        "📈 *Haftalik Ozet*",
        f"Toplam: {stats['total']} | ✅{stats['win']} ❌{stats['loss']} | WR: %{wr:.1f}",
    ]
    for sym, s in stats_per_symbol.items():
        if s["total"] > 0:
            swr = s["win"] / s["total"] * 100
            satirlar.append(f"  {SYMBOLS.get(sym,{}).get('name',sym)}: {s['total']} islem WR %{swr:.1f}")
    try:
        await app.bot.send_message(chat_id=TG_CHAT_ID, text="\n".join(satirlar), parse_mode="Markdown")
    except: pass

async def check_economic_calendar(app):
    """Her saat basinda ekonomik takvim kontrolu - sadece hafta ici"""
    global gonderilen_takvim_uyarilari
    now_utc = datetime.utcnow()
    if now_utc.weekday() >= 5:  # Cumartesi=5, Pazar=6 - piyasa kapali, veri yok
        return
    now_tr  = now_utc + timedelta(hours=3)
    bugun   = str(now_utc.date())

    # Eski gunlerin kayitlarini temizle
    gonderilen_takvim_uyarilari = {k for k in gonderilen_takvim_uyarilari if k.endswith(bugun)}
    olaylar = await aget_economic_calendar_api()
    if olaylar is None:
        # API yok/hatali - static fallback (sadece gun kontrolu)
        olaylar = [o for o in EKONOMIK_OLAYLAR if o.get("gun", -1) == -1 or o.get("gun") == now_utc.weekday()]

    for olay in olaylar:
        try:
            olay_saati = datetime.strptime(olay["saat"], "%H:%M").replace(
                year=now_utc.year, month=now_utc.month, day=now_utc.day
            )
        except (ValueError, TypeError):
            continue
        fark = (olay_saati - now_utc).total_seconds() / 60

        if 25 <= fark <= 35:  # 30dk oncesi pencere
            anahtar = f"{olay['olay']}_{now_utc.date()}"
            if anahtar in gonderilen_takvim_uyarilari:
                continue
            gonderilen_takvim_uyarilari.add(anahtar)

            uyari = (
                f"⚠️ *EKONOMİK TAKVİM UYARISI*\n\n"
                f"{olay['etki']} - 30 dakika sonra!\n\n"
                f"📌 **{olay['olay']}**\n"
                f"🕐 Saat: {olay['saat']} UTC ({int(int(olay['saat'][:2])+3):02d}:{olay['saat'][3:]} TR)\n\n"
                f"⚡ Yüksek volatilite bekleniyor!\n"
                f"🛑 Açık pozisyonlarını kontrol et!"
            )
            try:
                await app.bot.send_message(chat_id=TG_CHAT_ID, text=uyari, parse_mode="Markdown")
                log.info(f"Takvim uyarisi gonderildi: {olay['olay']}")
            except Exception as e:
                log.error(f"Takvim uyari hatasi: {e}")

# ── MAIN ────────────────────────────────────────────────────
async def main():

    app = Application.builder().token(TG_TOKEN).build()

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("komutlar",   cmd_komutlar))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(CommandHandler("durum",      cmd_durum))
    app.add_handler(CommandHandler("analiz",     cmd_analiz))
    app.add_handler(CommandHandler("istatistik", cmd_istatistik))
    app.add_handler(CommandHandler("sinyal",     cmd_sinyal))
    app.add_handler(CommandHandler("htfanaliz",  cmd_htfanaliz))
    app.add_handler(CommandHandler("ac",         cmd_ac))
    app.add_handler(CommandHandler("kapat",      cmd_kapat))
    app.add_handler(CommandHandler("kick",       cmd_kick))
    app.add_handler(CommandHandler("ban",        cmd_ban))
    app.add_handler(CommandHandler("unban",      cmd_unban))
    app.add_handler(CommandHandler("mute",       cmd_mute))
    app.add_handler(CommandHandler("unmute",     cmd_unmute))
    app.add_handler(CommandHandler("uyar",       cmd_uyar))
    app.add_handler(CommandHandler("uyarlar",    cmd_uyarlar))
    app.add_handler(CommandHandler("haber",      cmd_haber))
    app.add_handler(CommandHandler("dashboard",  cmd_dashboard))
    app.add_handler(CommandHandler("takvim",     cmd_takvim))
    app.add_handler(CommandHandler("favori",     cmd_favori))
    app.add_handler(CommandHandler("alarm",      cmd_alarm))
    app.add_handler(CommandHandler("seans",      cmd_seans))
    app.add_handler(CommandHandler("mod",        cmd_mod))
    app.add_handler(CommandHandler("reset",      cmd_reset))
    app.add_handler(CommandHandler("equity",     cmd_equity))
    app.add_handler(CommandHandler("pnl",        cmd_pnl_dispatcher))
    app.add_handler(CommandHandler("backtest",   cmd_backtest))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, spam_check), group=1)

    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        log.info("Warren Bot V4 baslatildi!")
        await scan_loop(app)

async def health_server():
    from aiohttp import web
    async def health(request):
        return web.Response(text="OK")
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Health server started on port {PORT}")

async def run_all():
    await asyncio.gather(health_server(), main())

if __name__ == "__main__":
    asyncio.run(run_all())
