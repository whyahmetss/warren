"""
Warren Bot V4 - Full Python ICT Trading & Grup Yonetim Botu
- Twelve Data API ile gercek zamanli fiyat verisi
- ICT sinyal tarama (ict_engine v2)
- DeepSeek AI ile gunluk HTF analiz + haber sentiment (sabah 09:00 TR)
- Telegram grup yonetimi
- 7/24 Render.com'da calisir

DUZELTMELER (v4.1):
  - Claude → DeepSeek V3 (openai-compat API, cok daha ucuz)
  - Health server port cakismasi duzeltildi (tek port: 10000)
  - BTC panel buton sembol haritasi duzeltildi
  - API key kontrolu guclendirildi
  - Tum AI cagrilarinda hata logu iyilestirildi
"""

import os
import io
import json
import logging
import asyncio
import sqlite3
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
import pandas as pd
import numpy as np
from ict_engine import analyze_ict_v2, get_active_session, is_in_kill_zone, KILL_ZONES
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ── AYARLAR ─────────────────────────────────────────────────
TG_TOKEN       = os.environ.get("TG_TOKEN",       "")
TG_CHAT_ID     = os.environ.get("TG_CHAT_ID",     "")
TD_API_KEY     = os.environ.get("TD_API_KEY",     "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")   # platform.deepseek.com
FMP_API_KEY    = os.environ.get("FMP_API_KEY",    "")
ADMIN_IDS      = [6663913960]

# ── PERSIST DB ───────────────────────────────────────────────
DB_PATH = os.environ.get("DB_PATH", "/tmp/warren_state.db")

def _db_connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    con.commit()
    return con

_db_con = _db_connect()

def persist_save():
    """Kritik state'i SQLite'a yaz."""
    payload = json.dumps({
        "stats":          stats,
        "stats_per_symbol": stats_per_symbol,
        "results_history":  results_history[-50:],
        "aktif_sinyaller":  {
            k: {**v, "time": v["time"].isoformat()}
            for k, v in aktif_sinyaller.items()
        },
        "pnl_db": pnl_db,
    })
    _db_con.execute("INSERT OR REPLACE INTO state VALUES ('warren', ?)", (payload,))
    _db_con.commit()

def persist_load():
    """Başlangıçta SQLite'tan state'i geri yükle."""
    global stats, stats_per_symbol, results_history, aktif_sinyaller, pnl_db
    try:
        row = _db_con.execute("SELECT value FROM state WHERE key='warren'").fetchone()
        if not row:
            return
        d = json.loads(row[0])
        stats.update(d.get("stats", {}))
        stats_per_symbol.update(d.get("stats_per_symbol", {}))
        results_history[:] = d.get("results_history", [])
        pnl_db.update(d.get("pnl_db", {}))
        for sym, v in d.get("aktif_sinyaller", {}).items():
            v["time"] = datetime.fromisoformat(v["time"])
            aktif_sinyaller[sym] = v
        log.info(f"State yüklendi: {stats['total']} işlem, {len(aktif_sinyaller)} aktif sinyal")
    except Exception as e:
        log.warning(f"State yüklenemedi (ilk çalıştırma?): {e}")

# ── PERSIST DB ───────────────────────────────────────────────
# DeepSeek API endpoint (OpenAI-uyumlu)
DEEPSEEK_BASE  = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"   # DeepSeek-V3

SYMBOLS = {
    "XAU/USD": {"name": "XAUUSD",  "interval": "1min", "htf": "15min", "pip_val": 100},
    "QQQ":     {"name": "QQQ",     "interval": "1min", "htf": "15min", "pip_val": 10},
    "BTC/USD": {"name": "BTCUSD",  "interval": "5min", "htf": "1h",   "pip_val": 1},
    "XAG/USD": {"name": "XAGUSD",  "interval": "5min", "htf": "1h",   "pip_val": 1},
}

# ── SEMBOL HARİTASI (panel butonları için) ───────────────────
# Buton callback'den gelen string → SYMBOLS key
SYMBOL_MAP = {
    "XAUUSD": "XAU/USD",
    "QQQ":    "QQQ",
    "BTCUSD": "BTC/USD",
  "XAGUSD": "XAG/USD",
}

COOLDOWN_MIN     = 30
MIN_RR           = 2.5
MIN_CONFLUENCE   = 3
RISK_PER_TRADE   = 0.01
MAX_DAILY_RISK   = 0.03
OB_LOOKBACK      = 20
SIGNAL_INTERVAL  = 60

stats             = {"total": 0, "win": 0, "loss": 0}
stats_per_symbol  = {s: {"total": 0, "win": 0, "loss": 0} for s in SYMBOLS}
results_history   = []
kill_zone_only    = False
fiyat_alarmlari   = []
last_daily_summary  = None
last_weekly_summary = None
warnings_db       = {}
message_counts    = {}
last_signal_time  = {}
bot_active        = True
last_daily_analiz = None
pnl_db            = {}
aktif_sinyaller   = {}
gonderilen_takvim_uyarilari = set()
son_kz_durum      = None
_takvim_api_cache = {"date": None, "events": []}
_htf_cache        = {}



# ── DeepSeek AI YARDIMCI ─────────────────────────────────────

def _deepseek_chat(system_prompt: str, user_prompt: str, max_tokens: int = 2000) -> str | None:
    """
    DeepSeek V3 chat tamamlama. Hata durumunda None döner, detaylı log yazar.
    OpenAI-uyumlu /v1/chat/completions endpoint kullanır.
    """
    key = DEEPSEEK_API_KEY
    if not key:
        log.error("DEEPSEEK_API_KEY tanimli degil! Render → Environment'a ekle.")
        return None

    payload = {
        "model": DEEPSEEK_MODEL,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
    }

    try:
        r = requests.post(
            f"{DEEPSEEK_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        data = r.json()
        if r.status_code != 200:
            log.error(f"DeepSeek HTTP {r.status_code}: {data}")
            return None
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"DeepSeek API istegi hatasi: {e}")
        return None


# ── EKONOMİK TAKVİM API ──────────────────────────────────────

def _fmt_val(v):
    """Sayısal değeri okunabilir stringe çevir: 11500 → 11.5K, 0.003 → 0.3%"""
    if v is None:
        return None
    try:
        f = float(str(v).replace("%","").replace("K","000").strip())
        if abs(f) >= 1000:
            return f"{f/1000:.1f}K"
        elif abs(f) < 1 and f != 0:
            return f"{f*100:.2f}%"
        else:
            return f"{f:.2f}%"
    except:
        s = str(v).strip()
        return s if s else None


def get_economic_calendar_api():
    """
    ForexFactory JSON feed - API key gerektirmez, tamamen ucretsiz.
    Haftanin tum olaylarini cekar, bugunü filtreler.
    Fallback: FMP API (eger key varsa).
    """
    now   = datetime.utcnow()
    today = now.strftime("%Y-%m-%d")

    # Cache kontrolu (1 saat gecerli)
    if _takvim_api_cache["date"] == today and _takvim_api_cache["events"]:
        return _takvim_api_cache["events"]

    events = _fetch_forexfactory(today)

    # ForexFactory basarisizsa FMP dene
    if events is None and FMP_API_KEY:
        events = _fetch_fmp(today)

    if events is None:
        events = []

    _takvim_api_cache["date"]   = today
    _takvim_api_cache["events"] = events
    return events


def _fetch_forexfactory(today: str):
    """
    ForexFactory haftalik JSON: https://nfs.faireconomy.media/ff_calendar_thisweek.json
    Format: [{title, country, date, impact, forecast, previous}, ...]
    """
    try:
        r = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if r.status_code != 200:
            log.warning(f"ForexFactory HTTP {r.status_code}")
            return None
        data = r.json()
        if not isinstance(data, list):
            return None

        events = []
        for e in data:
            # Tarih filtresi: sadece bugun
            raw_date = str(e.get("date") or "")
            if today not in raw_date:
                continue

            country = (e.get("country") or "").upper()
            if country != "USD":
                continue

            impact = (e.get("impact") or "Low").strip()
            if impact == "High":
                etki = "🔴"; etki_text = "Yuksek"
            elif impact == "Medium":
                etki = "🟡"; etki_text = "Orta"
            else:
                continue  # Low etkileri gosterme

            event_name = e.get("title") or e.get("name") or ""

            # Saat parse: "2025-03-10T13:30:00-0500" formatinda gelebilir
            dt_raw = str(e.get("date") or "").replace("T", " ")
            hour, minute = "12", "00"
            if " " in dt_raw:
                tpart = dt_raw.split(" ")[1][:5]
                try:
                    parts  = tpart.split(":")
                    # ForexFactory EST (UTC-5), biz UTC istiyoruz: +5
                    h_est  = int(parts[0])
                    m_val  = int(parts[1]) if len(parts) > 1 else 0
                    h_utc  = (h_est + 5) % 24   # EST → UTC
                    hour   = f"{h_utc:02d}"
                    minute = f"{m_val:02d}"
                except (ValueError, IndexError):
                    pass

            try:
                tr_hour = f"{(int(hour)+3) % 24:02d}"
            except:
                tr_hour = hour

            events.append({
                "saat":      f"{hour}:{minute}",
                "tr_saat":   f"{tr_hour}:{minute}",
                "olay":      event_name,
                "etki":      etki,
                "etki_text": etki_text,
                "country":   country,
                "tahmin":    _fmt_val(e.get("forecast")),
                "onceki":    _fmt_val(e.get("previous")),
            })

        events.sort(key=lambda x: x["saat"])
        log.info(f"ForexFactory: {len(events)} olay bulundu ({today})")
        return events

    except Exception as ex:
        log.warning(f"ForexFactory hatasi: {ex}")
        return None


def _fetch_fmp(today: str):
    """FMP API yedek kaynagi (key gerekli)."""
    try:
        r = requests.get(
            "https://financialmodelingprep.com/stable/economic-calendar",
            params={"from": today, "to": today, "apikey": FMP_API_KEY},
            timeout=10,
        )
        data = r.json()
        if isinstance(data, dict) and "Error" in data:
            return None
        if not isinstance(data, list):
            return None

        events = []
        for e in data:
            dt_str     = str(e.get("date") or e.get("datetime") or "").replace("T", " ")
            event_name = e.get("event") or e.get("title") or ""
            impact     = (e.get("impact") or "Medium").upper()
            country    = (e.get("country") or "").upper()

            if "HIGH"  in impact: etki = "🔴"; etki_text = "Yuksek"
            elif "MED" in impact: etki = "🟡"; etki_text = "Orta"
            else: continue
            if country not in ("US", "USD", ""): continue

            hour, minute = "12", "00"
            if " " in dt_str and ":" in dt_str:
                tpart = dt_str.split()[1]
                parts = tpart.split(":")
                try:
                    hour   = f"{int(parts[0]):02d}"
                    minute = f"{int(parts[1]) if len(parts)>1 else 0:02d}"
                except: pass
            try:
                tr_hour = f"{(int(hour)+3)%24:02d}"
            except:
                tr_hour = hour

            events.append({
                "saat":      f"{hour}:{minute}",
                "tr_saat":   f"{tr_hour}:{minute}",
                "olay":      event_name,
                "etki":      etki,
                "etki_text": etki_text,
                "country":   country,
                "tahmin":    _fmt_val(e.get("estimate") or e.get("forecast")),
                "onceki":    _fmt_val(e.get("previous")),
            })

        events.sort(key=lambda x: x["saat"])
        return events
    except Exception as ex:
        log.warning(f"FMP hatasi: {ex}")
        return None


def format_takvim_mesaji(events: list, baslik_tarih: str) -> str:
    """
    Resimde görülen formatta takvim mesajı üret:
    📅 Önemli Haberler (🔴 Yüksek & 🟡 Orta Etki)
    🗓 Toplam: X haber
    ...
    """
    if not events:
        return (
            f"📅 Ekonomik Takvim ({baslik_tarih})\n\n"
            "ℹ️ Bugün önemli USD haberi yok.\n\n"
            "Detaylı: investing.com/economic-calendar"
        )

    lines = [
        f"📅 Önemli Haberler (🔴 Yüksek & 🟡 Orta Etki)",
        f"🗓 Toplam: {len(events)} haber",
    ]

    for e in events:
        lines.append("")  # boş satır ayrım için
        lines.append(f"{e['etki']} {e['olay']}")
        lines.append(f"🌐 USD | 🕐 {e['tr_saat']}")

        extras = []
        if e.get("tahmin"):
            extras.append(f"Tahmin: {e['tahmin']}")
        if e.get("onceki"):
            extras.append(f"Önceki: {e['onceki']}")
        if extras:
            lines.append("📊 " + " | ".join(extras))

    lines.append("")
    lines.append("⚠️ Yüksek etkili olaylardan 15dk önce işlem açma!")

    return "\n".join(lines)


# ── KEEP ALIVE (tek port: 10000) ─────────────────────────────
# DÜZELTME: Eskiden hem thread'de 8080 hem async'te 10000 açılıyordu,
#           Render'da port çakışması ve health check başarısızlığına yol açıyordu.
#           Artık sadece aiohttp 10000'de çalışıyor.

async def health_server():
    from aiohttp import web
    async def health(request):
        return web.Response(text="Warren Bot V4 caliyor!")
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 10000)
    await site.start()
    log.info("Health server 10000 portunda basladi")


# ── TWELVE DATA ──────────────────────────────────────────────

def get_candles(symbol, interval="1min", outputsize=50):
    try:
        r = requests.get("https://api.twelvedata.com/time_series", params={
            "symbol": symbol, "interval": interval,
            "outputsize": outputsize, "apikey": TD_API_KEY,
        }, timeout=10)
        data = r.json()
        if "values" not in data:
            log.warning(f"API hatasi {symbol}: {data.get('message', '?')}")
            return None
        df = pd.DataFrame(data["values"])
        df = df.rename(columns={"datetime": "time", "open": "o",
                                 "high": "h", "low": "l", "close": "c"})
        df = df.astype({"o": float, "h": float, "l": float, "c": float})
        return df.iloc[::-1].reset_index(drop=True)
    except Exception as e:
        log.error(f"get_candles hatasi: {e}")
        return None

def get_price(symbol):
    try:
        r = requests.get("https://api.twelvedata.com/price", params={
            "symbol": symbol, "apikey": TD_API_KEY}, timeout=5)
        return float(r.json().get("price", 0)) or None
    except:
        return None



def get_htf_cached(symbol, interval="15min", outputsize=30):
    global _htf_cache
    now = datetime.utcnow()
    key = f"{symbol}_{interval}"
    if key in _htf_cache:
        ts, df = _htf_cache[key]
        if (now - ts).total_seconds() < 14 * 60:
            return df
    df = get_candles(symbol, interval, outputsize)
    if df is not None:
        _htf_cache[key] = (now, df)
    return df


# ── DEEPSEEK - GÜNLÜK ANALİZ ────────────────────────────────

def get_market_context():
    context = {}
    for symbol in ["XAU/USD", "QQQ"]:
        price = get_price(symbol)
        daily = get_candles(symbol, "1day", 10)
        if price and daily is not None:
            son5   = daily.tail(5)
            closes = son5["c"].values
            trend  = "Uptrend" if closes[-1] > closes[0] else "Downtrend"
            context[symbol] = {
                "price": price,
                "trend": trend,
                "high5": round(float(son5["h"].max()), 4),
                "low5":  round(float(son5["l"].min()), 4),
                "close": round(float(closes[-1]), 4),
            }
    return context

def generate_daily_analysis(symbol_display: str, context_data: dict) -> str | None:
    """DeepSeek ile günlük HTF ICT analizi üret."""
    now_tr = datetime.utcnow() + timedelta(hours=3)
    tarih  = now_tr.strftime("%d %B %Y - %A")

    piyasa_str = ""
    for sym, d in context_data.items():
        piyasa_str += (f"{sym}: Fiyat={d['price']}, Trend={d['trend']}, "
                       f"5gun High={d['high5']}, 5gun Low={d['low5']}\n")

    system = (
        "Sen profesyonel bir ICT (Inner Circle Trader) piyasa analistisn. "
        "Türkçe, net ve yapılandırılmış analizler yazarsın. "
        "Kesin sinyal vermezsin; HTF bias, önemli seviyeler ve zamanlamalar konusunda "
        "kullanıcıyı bilgilendirirsin. Risk uyarılarını her zaman eklersin."
    )

    user = f"""Aşağıdaki verilere göre {symbol_display} için bugün ({tarih}) günlük HTF ICT analizi yaz.

MEVCUT PİYASA VERİLERİ:
{piyasa_str}

ÇIKTI FORMATI (tam olarak bu yapıyı kullan, Türkçe):

📊 {symbol_display} - HTF ANALİZ
📅 {tarih}
━━━━━━━━━━━━━━━━━━━━━━
📈 YAPISAL ANALİZ
━━━━━━━━━━━━━━━━━━━━━━
🔹 DAILY STRUCTURE: [analiz]
🔹 WEEKLY STRUCTURE: [analiz]
━━━━━━━━━━━━━━━━━━━━━━
🧭 KURUMSAL YÖNELİM
━━━━━━━━━━━━━━━━━━━━━━
📅 QUARTERLY THEORY: [analiz]
━━━━━━━━━━━━━━━━━━━━━━
🎯 BUGÜNKÜ BIAS
━━━━━━━━━━━━━━━━━━━━━━
➡️ GENEL YÖNELİM: [BULLISH / BEARISH / NEUTRAL]
Neden? [3 kısa sebep]
━━━━━━━━━━━━━━━━━━━━━━
🔍 DİKKAT EDİLECEK SEVİYELER
━━━━━━━━━━━━━━━━━━━━━━
📍 ZONE 1: [aralık]
• Yapı: [OB/FVG/Breaker]
• Konum: [Premium/Discount]
• Nedeni: [açıklama]
⚠️ Invalid: [seviye] body close
📍 ZONE 2: [aralık]
[aynı format]
━━━━━━━━━━━━━━━━━━━━━━
🎯 LİKİDİTE HEDEFLER
━━━━━━━━━━━━━━━━━━━━━━
1️⃣ [hedef 1]
2️⃣ [hedef 2]
3️⃣ [hedef 3]
━━━━━━━━━━━━━━━━━━━━━━
⏰ ZAMANLAMALAR
━━━━━━━━━━━━━━━━━━━━━━
Kill Zones (TR Saati):
• 10:00-13:00 → London Kill Zone
• 15:00-19:00 → New York Kill Zone
• 19:00-20:00 → NY Silver Bullet
━━━━━━━━━━━━━━━━━━━━━━
📌 ÖZEL NOTLAR
━━━━━━━━━━━━━━━━━━━━━━
⚠️ DİKKAT: [önemli notlar]
💡 STRATEJİ: [günlük strateji tavsiyesi]
━━━━━━━━━━━━━━━━━━━━━━
🎓 HATIRLATMA
━━━━━━━━━━━━━━━━━━━━━━
✅ Bu bir analiz, sinyal değil
✅ Setup yoksa trade yok
✅ Risk max %1-2
━━━━━━━━━━━━━━━━━━━━━━"""

    return _deepseek_chat(system, user, max_tokens=2000)


async def send_daily_analysis(app, chat_id=None):
    global last_daily_analiz
    dest = chat_id or TG_CHAT_ID
    log.info("Gunluk analiz gonderiliyor...")

    if not DEEPSEEK_API_KEY:
        try:
            await app.bot.send_message(
                chat_id=dest,
                text="⚠️ DEEPSEEK_API_KEY tanımlı değil! Render → Environment'a DEEPSEEK_API_KEY ekle."
            )
        except:
            pass
        return

    context = get_market_context()
    if not context:
        try:
            await app.bot.send_message(chat_id=dest, text="❌ Piyasa verisi alınamadı (Twelve Data).")
        except:
            pass
        return

    for symbol_display in ["XAUUSD (Gold)", "NAS100 (QQQ)"]:
        analysis = generate_daily_analysis(symbol_display, context)
        if analysis:
            parts = [analysis[i:i + 4000] for i in range(0, len(analysis), 4000)]
            for part in parts:
                try:
                    await app.bot.send_message(chat_id=dest, text=part)
                except Exception as e:
                    log.error(f"HTF analiz gonderim hatasi: {e}")
                await asyncio.sleep(1)
        else:
            try:
                await app.bot.send_message(chat_id=dest, text=f"❌ {symbol_display} analizi oluşturulamadı.")
            except:
                pass
        await asyncio.sleep(2)

    last_daily_analiz = datetime.utcnow().date()
    log.info("Gunluk analiz gonderildi!")


# ── ICT WRAPPER ──────────────────────────────────────────────

def analyze_ict(df, df_htf=None):
    return analyze_ict_v2(df, df_htf, min_rr=MIN_RR, min_confluence=MIN_CONFLUENCE)

def is_market_open():
    return datetime.utcnow().weekday() < 5

def get_session():
    return get_active_session() or "Session Dışı"

def is_kill_zone():
    return is_in_kill_zone()

def format_signal(symbol, sig):
    name      = SYMBOLS.get(symbol, {}).get("name", symbol)
    direction = sig["direction"]
    conf      = sig["conf"]
    checks    = sig.get("checks", {})
    strength  = sig.get("strength", "LOW")
    session   = sig.get("session", get_session())
    rr        = sig["rr"]

    prec = 2 if "XAU" in symbol else (1 if "BTC" in symbol or "QQQ" in symbol else 5)
    p    = lambda v: f"{v:.{prec}f}"

    str_label = {"HIGH": "YUKSEK", "MEDIUM": "ORTA"}.get(strength, "DUSUK")
    dir_emoji = "LONG" if direction == "LONG" else "SHORT"

    check_lines = []
    for label, passed in checks.items():
        mark = "OK" if passed else "--"
        check_lines.append(f"  [{mark}] {label}")

    # Ekstra bilgiler
    extras = []
    if sig.get("macro_time"):
        extras.append("Macro Window aktif")
    if sig.get("fib_level"):
        extras.append(f"Fib: {sig['fib_level']:.2f}")
    htf_map = {1: "Bullish", -1: "Bearish", 0: "Notr"}
    extras.append(f"HTF Bias: {htf_map.get(sig.get('htf_bias',0), 'Notr')}")
    extra_line = " | ".join(extras)

    lines = [
        f"--- {name} {dir_emoji} SETUPi ---",
        "",
        "Confluence: " + str(conf) + "/6",
        chr(10).join(check_lines),
        "",
        f"Entry : {p(sig['price'])}",
        f"Stop  : {p(sig['sl'])}",
        f"TP    : {p(sig['tp'])}",
        f"RR    : 1:{rr:.1f}",
        "",
        f"Guc   : {str_label} ({conf}/6)",
        f"Seans : {session}",
        extra_line,
        "",
        "Giris karari sana ait. Risk max %1.",
    ]
    return chr(10).join(lines)

def _sinyal_butonlari(signal_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Al",  callback_data=f"sig_al_{signal_id}"),
        InlineKeyboardButton("⏭ Geç", callback_data=f"sig_gec_{signal_id}"),
    ]])


# ── ANA DÖNGÜ ────────────────────────────────────────────────

async def scan_loop(app):
    global last_daily_analiz, last_daily_summary, last_weekly_summary
    log.info("Ana dongu basladi")

    while True:
        await asyncio.sleep(90)

        now_tr = datetime.utcnow() + timedelta(hours=3)
        bugun  = now_tr.date()
        saat   = now_tr.hour
        dakika = now_tr.minute

        # 09:00 TR → günlük analiz
        if saat == 9 and dakika == 0 and now_tr.weekday() < 5 and last_daily_analiz != bugun:
            await send_daily_analysis(app)

        await check_kill_zone_status(app)
        await check_economic_calendar(app)

        # Fiyat alarmlari
        for al in list(fiyat_alarmlari):
            p = get_price(al["sembol"])
            if p is None:
                continue
            tetik = (al["yon"] == "ust" and p >= al["hedef"]) or \
                    (al["yon"] == "alt" and p <= al["hedef"])
            if tetik:
                try:
                    await app.bot.send_message(
                        chat_id=al["chat_id"],
                        text=f"🔔 Fiyat alarmi! {al['sembol']} {p:.4f} (hedef: {al['hedef']})",
                    )
                    fiyat_alarmlari.remove(al)
                except:
                    pass

        # Gunluk ozet 09:05 TR
        if saat == 9 and dakika == 5 and now_tr.weekday() < 5 and last_daily_summary != bugun:
            await send_daily_summary(app)
            last_daily_summary = bugun

        # Haftalik ozet Cuma 18:00 TR
        if saat == 18 and dakika == 0 and now_tr.weekday() == 4 and last_weekly_summary != bugun:
            await send_weekly_summary(app)
            last_weekly_summary = bugun

        if aktif_sinyaller:
            await check_tp_sl(app)

        if not is_market_open() or not bot_active or not is_kill_zone():
            continue

        for symbol, cfg in SYMBOLS.items():
            try:
                last = last_signal_time.get(symbol)
                if last and (datetime.utcnow() - last).seconds < COOLDOWN_MIN * 60:
                    continue
                df_ltf = get_candles(symbol, cfg["interval"], 50)
                await asyncio.sleep(2)
                df_htf = get_htf_cached(symbol, cfg.get("htf", "15min"), 30)
                await asyncio.sleep(2)
                sig = analyze_ict(df_ltf, df_htf)
                if sig:
                    txt = format_signal(symbol, sig)
                    if len(results_history) >= 3 and results_history[-3:] == ["L", "L", "L"]:
                        txt = f"⚠️ Ardışık 3 kayıp! Daha seçici ol.\n\n{txt}"
                    sig_id = f"{symbol.replace('/', '_')}_{int(datetime.utcnow().timestamp())}"
                    await app.bot.send_message(
                        chat_id=TG_CHAT_ID, text=txt,
                        reply_markup=_sinyal_butonlari(sig_id),
                    )
                    last_signal_time[symbol] = datetime.utcnow()
                    aktif_sinyaller[symbol]  = {
                        "direction": sig["direction"], "entry": sig["price"],
                        "sl": sig["sl"], "tp": sig["tp"], "time": datetime.utcnow(),
                    }
                    stats["total"] += 1
                    persist_save()
                    log.info(f"Sinyal: {symbol} {sig['direction']} conf={sig['conf']}/6 RR=1:{sig['rr']:.1f}")
            except Exception as e:
                log.error(f"Scan hatasi {symbol}: {e}")


# ── PANEL ────────────────────────────────────────────────────

def _panel_main_kbd():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Durum",    callback_data="panel_durum"),
         InlineKeyboardButton("🖥 Analiz",   callback_data="panel_analiz")],
        [InlineKeyboardButton("🔍 Sinyal",    callback_data="cmd_sinyal"),
         InlineKeyboardButton("📊 Dashboard", callback_data="cmd_dashboard")],
        [InlineKeyboardButton("📰 Haberler",  callback_data="panel_haber"),
         InlineKeyboardButton("📈 Equity",    callback_data="cmd_equity")],
        [InlineKeyboardButton("📉 İstatistik", callback_data="cmd_istatistik"),
         InlineKeyboardButton("📋 HTF Analiz", callback_data="cmd_htfanaliz")],
        [InlineKeyboardButton("▶ Aç",   callback_data="cmd_ac"),
         InlineKeyboardButton("⏹ Kapat", callback_data="cmd_kapat"),
         InlineKeyboardButton("🔄 Reset", callback_data="cmd_reset")],
        [InlineKeyboardButton("👥 Grup",      callback_data="panel_grup")],
    ])

def _panel_haber_kbd():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Ekonomik Takvim", callback_data="cmd_takvim_panel")],
        [InlineKeyboardButton("🧠 AI Piyasa Analizi", callback_data="cmd_haber")],
        [InlineKeyboardButton("◀ Geri", callback_data="panel")],
    ])

def _panel_durum_kbd():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Bot",    callback_data="cmd_durum_bot"),
         InlineKeyboardButton("📡 Piyasa", callback_data="cmd_durum_piyasa"),
         InlineKeyboardButton("📊 Sinyal", callback_data="cmd_durum_sinyal")],
        [InlineKeyboardButton("◀ Geri",   callback_data="panel")],
    ])

def _panel_analiz_kbd():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🥇 ALTIN",   callback_data="cmd_analiz_XAUUSD"),
         InlineKeyboardButton("📊 NASDAQ",  callback_data="cmd_analiz_QQQ")],
        [InlineKeyboardButton("₿ BİTCOİN", callback_data="cmd_analiz_BTCUSD")],
      [InlineKeyboardButton("🦾 GÜMÜŞ",   callback_data="cmd_analiz_XAGUSD")],
        [InlineKeyboardButton("◀ Geri",    callback_data="panel")],
    ])

def _panel_grup_kbd():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Kick",  callback_data="cmd_kick"),
         InlineKeyboardButton("Ban",   callback_data="cmd_ban"),
         InlineKeyboardButton("Unban", callback_data="cmd_unban")],
        [InlineKeyboardButton("Mute",   callback_data="cmd_mute"),
         InlineKeyboardButton("Unmute", callback_data="cmd_unmute")],
        [InlineKeyboardButton("Uyar",   callback_data="cmd_uyar"),
         InlineKeyboardButton("Uyarlar",callback_data="cmd_uyarlar")],
        [InlineKeyboardButton("◀ Geri", callback_data="panel")],
    ])


# ── KOMUTLAR ─────────────────────────────────────────────────

def is_admin(uid):
    return uid in ADMIN_IDS

async def cmd_start(update, ctx):
    await update.message.reply_text("Warren Panel", reply_markup=_panel_main_kbd())

async def cmd_komutlar(update, ctx):
    await cmd_start(update, ctx)

async def handle_button(update, ctx):
    global bot_active, last_signal_time
    q    = update.callback_query
    await q.answer()
    data = q.data
    target = q.message

    async def reply(txt):
        await ctx.bot.send_message(chat_id=target.chat_id, text=txt)

    async def edit_panel(msg, kbd):
        try:
            await q.edit_message_text(text=msg, reply_markup=kbd)
        except:
            await reply(msg)

    # Sinyal Al / Geç butonları
    if data and data.startswith("sig_"):
        parts = data.split("_", 2)
        if len(parts) >= 3:
            await reply("✅ Sinyal alındı." if parts[1] == "al" else "⏭ Geçildi.")
        return

    # Panel navigasyon
    if data == "panel":
        await edit_panel("Warren Panel", _panel_main_kbd()); return
    if data == "panel_durum":
        await edit_panel("Warren Panel › Durum", _panel_durum_kbd()); return
    if data == "panel_analiz":
        await edit_panel("Warren Panel › Analiz", _panel_analiz_kbd()); return
    if data == "panel_grup":
        await edit_panel("Warren Panel › Grup", _panel_grup_kbd()); return
    if data == "panel_haber":
        await edit_panel("Warren Panel › Haberler", _panel_haber_kbd()); return

    if not data or not data.startswith("cmd_"):
        return
    cmd = data[4:]

    # ── Durum
    if cmd in ("durum", "durum_bot"):
        wr = stats["win"] / stats["total"] * 100 if stats["total"] else 0
        await reply(
            f"Durum    : {'Aktif' if bot_active else 'Kapalı'}\n"
            f"Piyasa   : {'Açık' if is_market_open() else 'KAPALI'}\n"
            f"Seans    : {get_session()}\n"
            f"Saat     : {datetime.utcnow().strftime('%H:%M UTC')}\n"
            f"Sinyal   : {stats['total']}  WR: %{wr:.1f}\n"
            f"Son Analiz: {last_daily_analiz or 'Henüz yok'}"
        )
    elif cmd == "durum_piyasa":
        await reply(
            f"Piyasa: {'Açık' if is_market_open() else 'KAPALI'}\n"
            f"Seans : {get_session()}\n"
            f"Saat  : {datetime.utcnow().strftime('%H:%M UTC')}"
        )
    elif cmd == "durum_sinyal":
        wr    = stats["win"] / stats["total"] * 100 if stats["total"] else 0
        lines = [f"Toplam: {stats['total']}  W:{stats['win']}  L:{stats['loss']}  WR:%{wr:.1f}"]
        for sym, s in stats_per_symbol.items():
            if s["total"] > 0:
                swr = s["win"] / s["total"] * 100
                lines.append(f"  {SYMBOLS.get(sym,{}).get('name',sym)}: {s['total']} W{s['win']} L{s['loss']} %{swr:.1f}")
        await reply("\n".join(lines))

    # ── Analiz panel butonları
    elif cmd.startswith("analiz_"):
        sym_key = cmd[7:]
        symbol  = SYMBOL_MAP.get(sym_key)
        if not symbol or symbol not in SYMBOLS:
            await reply(f"Bilinmeyen sembol: {sym_key}"); return

        name = SYMBOLS[symbol]["name"]

        # Aktif (beklenen) sinyal
        aktif = aktif_sinyaller.get(symbol)
        if aktif:
            prec = 2 if "XAU" in symbol else 1
            p    = lambda v: f"{v:.{prec}f}"
            sure = int((datetime.utcnow() - aktif["time"]).total_seconds() / 60)
            aktif_txt = (
                f"BEKLEYEN\n"
                f"{aktif['direction']} @ {p(aktif['entry'])}\n"
                f"SL: {p(aktif['sl'])} | TP: {p(aktif['tp'])}\n"
                f"Süredir bekliyor: {sure} dk"
            )
        else:
            aktif_txt = "Aktif sinyal yok"

        # Sembol istatistikleri (kapalı işlemler)
        s = stats_per_symbol.get(symbol, {"total": 0, "win": 0, "loss": 0})
        total = s["total"]
        wins  = s["win"]
        losses= s["loss"]
        wr    = (wins / total * 100) if total > 0 else 0
        acik  = 1 if aktif else 0

        istat_txt = (
            f"Kapalı: {total} işlem\n"
            f"  TP: {wins}  SL: {losses}  WR: %{wr:.0f}"
        ) if total > 0 else "Henüz kapalı işlem yok"

        await reply(
            f"=== {name} ===\n\n"
            f"[AÇIK]\n{aktif_txt}\n\n"
            f"[KAPALI]\n{istat_txt}"
        )

    # ── Fiyat
    elif cmd == "fiyat":
        lines = ["=== FİYATLAR ==="]
        for symbol, cfg in SYMBOLS.items():
            p = get_price(symbol)
            lines.append(f"{cfg['name']:8}: {p:.4f}" if p else f"{cfg['name']:8}: Alınamadı")
        await reply("\n".join(lines))

    # ── Manuel sinyal tara
    elif cmd == "sinyal":
        await reply("Taranıyor...")
        last_signal_time.clear()
        found = False
        for symbol, cfg in SYMBOLS.items():
            df   = get_candles(symbol, cfg["interval"], 50)
            sig  = analyze_ict(df)
            if sig:
                await reply(format_signal(symbol, sig))
                stats["total"] += 1
                last_signal_time[symbol] = datetime.utcnow()
                found = True
        if not found:
            await reply("Setup yok, bekleniyor...")

    # ── İstatistik
    elif cmd == "istatistik":
        wr    = stats["win"] / stats["total"] * 100 if stats["total"] else 0
        lines = [f"Toplam: {stats['total']}  W:{stats['win']}  L:{stats['loss']}  WR:%{wr:.1f}"]
        for sym, s in stats_per_symbol.items():
            if s["total"] > 0:
                swr = s["win"] / s["total"] * 100
                lines.append(f"  {SYMBOLS.get(sym,{}).get('name',sym)}: {s['total']} W{s['win']} L{s['loss']} %{swr:.1f}")
        await reply("\n".join(lines))

    # ── HTF Analiz
    elif cmd == "htfanaliz":
        await reply("Günlük HTF analiz hazırlanıyor, ~30sn bekle...")
        await send_daily_analysis(ctx.application, chat_id=target.chat_id)

    # ── Bot ac/kapat/reset
    elif cmd == "ac":
        if not is_admin(q.from_user.id):
            await reply("Yetkin yok."); return
        bot_active = True
        await reply("✅ Bot aktif!")
    elif cmd == "kapat":
        if not is_admin(q.from_user.id):
            await reply("Yetkin yok."); return
        bot_active = False
        await reply("⛔ Bot durduruldu. /ac ile açabilirsin.")
    elif cmd == "reset":
        if not is_admin(q.from_user.id):
            await reply("Yetkin yok."); return
        stats.update({"total": 0, "win": 0, "loss": 0})
        for s in stats_per_symbol:
            stats_per_symbol[s] = {"total": 0, "win": 0, "loss": 0}
        results_history.clear()
        last_signal_time.clear()
        aktif_sinyaller.clear()
        persist_save()
        await reply("🔄 Reset tamamlandı.")

    # ── Dashboard
    elif cmd == "dashboard":
        wr    = stats["win"] / stats["total"] * 100 if stats["total"] else 0
        son10 = " ".join("W" if r == "W" else "L" for r in results_history[-10:]) or "-"
        aktif_lines = []
        for s, v in aktif_sinyaller.items():
            name = SYMBOLS.get(s, {}).get("name", s)
            aktif_lines.append(f"  {name} {v['direction']}")
        aktif = "\n".join(aktif_lines) or "  Yok"
        perf_lines = []
        for s, v in stats_per_symbol.items():
            if v["total"] > 0:
                swr = v["win"] / v["total"] * 100
                name = SYMBOLS.get(s, {}).get("name", s)
                perf_lines.append(f"  {name}: {v['total']}T W{v['win']} L{v['loss']} WR%{swr:.0f}")
        perf = "\n".join(perf_lines) or "  Henuz islem yok"
        msg = (
            "-- WARREN DASHBOARD --\n\n"
            f"Bot: {'Aktif' if bot_active else 'Kapali'} | {get_session()}\n"
                f"W:{stats['win']} L:{stats['loss']} WR%{wr:.0f}\n"
            f"Son 10: {son10}\n\n"
            f"{perf}\n\nAktif:\n{aktif}"
        )
        await reply(msg)

    # ── Equity
    elif cmd == "equity":
        uid      = q.from_user.id
        kayitlar = pnl_db.get(uid, [])
        if not kayitlar:
            await reply("Henüz işlem yok. /pnl ekle ile kaydet."); return
        try:
            import matplotlib; matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            cum = []; s = 0
            for k in kayitlar:
                s += k["pnl"]; cum.append(s)
            plt.figure(figsize=(8, 4))
            plt.plot(cum, color="#2ecc71", linewidth=2)
            plt.fill_between(range(len(cum)), cum, alpha=0.3)
            plt.axhline(0, color="gray", linestyle="--")
            plt.title("Equity Curve"); plt.ylabel("Cumulative PnL ($)")
            buf = io.BytesIO(); plt.savefig(buf, format="png", dpi=100); plt.close(); buf.seek(0)
            await ctx.bot.send_photo(chat_id=target.chat_id, photo=buf)
        except Exception as e:
            await reply(f"Grafik hatası: {e}")

    # ── Ekonomik Takvim (panel butonu)
    elif cmd == "takvim_panel":
        now_tr  = datetime.utcnow() + timedelta(hours=3)
        tarih   = now_tr.strftime("%d.%m.%Y")
        events  = get_economic_calendar_api()
        if events is None or len(events) == 0:
            await reply(
                f"📅 Ekonomik Takvim ({tarih})\n\n"
                "ℹ️ Bugun onemli USD haberi yok ya da veri alinamadi.\n"
                "Detayli: investing.com/economic-calendar"
            )
        else:
            await reply(format_takvim_mesaji(events, tarih))

    # ── Haberler (DeepSeek)
    elif cmd == "haber":
        await reply("📰 Haberler analiz ediliyor...")
        if not DEEPSEEK_API_KEY:
            await reply("⚠️ DEEPSEEK_API_KEY tanımlı değil!"); return
        now_tr = (datetime.utcnow() + timedelta(hours=3)).strftime("%d %B %Y %H:%M")
        result = _deepseek_chat(
            system_prompt="Sen ICT perspektifinden piyasa haberi yorumlayan analistsin. Türkçe, kısa ve net yaz.",
            user_prompt=(
                f"Tarih: {now_tr} TR\n\n"
                "Şu an piyasaları etkileyen makroekonomik ortamı ve güncel haberleri değerlendir. "
                "XAU/USD ve NAS100 için:\n"
                "1. Genel sentiment (Bullish/Bearish/Nötr)\n"
                "2. Risk faktörleri\n"
                "3. Kısa vadeli fırsat/tehdit\n\n"
                "ICT perspektifinden kısa yorum yap."
            ),
            max_tokens=600,
        )
        if result:
            await reply(f"📰 Haber Analizi\n\n{result}")
        else:
            await reply("❌ Haber analizi alınamadı. Log'u kontrol et.")

    elif cmd in ("kick", "ban", "unban", "mute", "unmute", "uyar", "uyarlar"):
        if not is_admin(q.from_user.id): return
        await reply(f"Grup komutları için ilgili kişinin mesajına reply yapıp /{cmd} yaz.")


# ── KOMUT HANDLER'LARI (slash komutlar) ─────────────────────

async def cmd_durum(update, ctx):
    wr = stats["win"] / stats["total"] * 100 if stats["total"] else 0
    await update.message.reply_text(
        f"Durum    : {'Aktif' if bot_active else 'Kapalı'}\n"
        f"Piyasa   : {'Açık' if is_market_open() else 'KAPALI'}\n"
        f"Seans    : {get_session()}\n"
        f"Saat     : {datetime.utcnow().strftime('%H:%M UTC')}\n"
        f"Sinyal   : {stats['total']}  WR: %{wr:.1f}\n"
        f"Son Analiz: {last_daily_analiz or 'Henüz yok'}"
    )

async def cmd_analiz(update, ctx):
    symbol = (ctx.args[0].upper() if ctx.args else "XAU/USD")
    if symbol not in SYMBOLS:
        await update.message.reply_text(f"Geçersiz. Seçenekler: {', '.join(SYMBOLS)}"); return
    cfg = SYMBOLS[symbol]
    await update.message.reply_text(f"{symbol} analiz ediliyor...")
    df_ltf = get_candles(symbol, cfg["interval"], 50)
    df_htf = get_candles(symbol, cfg.get("htf", "15min"), 30)
    if df_ltf is None:
        await update.message.reply_text("Veri alınamadı."); return
    sig = analyze_ict(df_ltf, df_htf)
    if sig: await update.message.reply_text(format_signal(symbol, sig))
    else:   await update.message.reply_text(f"{symbol}: Setup yok ({get_session()})")

async def cmd_istatistik(update, ctx):
    wr    = stats["win"] / stats["total"] * 100 if stats["total"] else 0
    lines = [f"Toplam: {stats['total']}  W:{stats['win']}  L:{stats['loss']}  WR:%{wr:.1f}"]
    for sym, s in stats_per_symbol.items():
        if s["total"] > 0:
            swr = s["win"] / s["total"] * 100
            lines.append(f"  {SYMBOLS.get(sym,{}).get('name',sym)}: {s['total']} W{s['win']} L{s['loss']} %{swr:.1f}")
    await update.message.reply_text("\n".join(lines))

async def cmd_sinyal(update, ctx):
    global last_signal_time
    req = " ".join(ctx.args).upper().replace(" ", "/") if ctx.args else None
    if req and req not in SYMBOLS:
        await update.message.reply_text("Bilinmeyen sembol: " + req); return
    scan = {req: SYMBOLS[req]} if req else SYMBOLS
    await update.message.reply_text(f"Taranıyor: {', '.join(scan.keys())}...")
    last_signal_time = {}; found = False
    for symbol, cfg in scan.items():
        df_ltf = get_candles(symbol, cfg["interval"], 50)
        df_htf = get_candles(symbol, cfg.get("htf", "15min"), 30)
        sig    = analyze_ict(df_ltf, df_htf)
        if sig:
            txt = format_signal(symbol, sig)
            if len(results_history) >= 3 and results_history[-3:] == ["L","L","L"]:
                txt = f"⚠️ Ardışık 3 kayıp!\n\n{txt}"
            sig_id = f"{symbol.replace('/','_')}_{int(datetime.utcnow().timestamp())}"
            await update.message.reply_text(txt, reply_markup=_sinyal_butonlari(sig_id))
            stats["total"] += 1; last_signal_time[symbol] = datetime.utcnow(); found = True
    if not found:
        await update.message.reply_text("Setup yok, bekleniyor...")

async def cmd_htfanaliz(update, ctx):
    await update.message.reply_text("Günlük HTF analiz hazırlanıyor (~30sn)...")
    await send_daily_analysis(ctx.application, chat_id=update.effective_chat.id)

async def cmd_haber(update, ctx):
    """DeepSeek ile haber sentiment analizi"""
    await update.message.reply_text("📰 Haberler analiz ediliyor...")
    if not DEEPSEEK_API_KEY:
        await update.message.reply_text("⚠️ DEEPSEEK_API_KEY tanımlı değil!"); return
    now_tr = (datetime.utcnow() + timedelta(hours=3)).strftime("%d %B %Y %H:%M")
    result = _deepseek_chat(
        system_prompt="Sen ICT perspektifinden piyasa haberi yorumlayan analistsin. Türkçe, kısa ve net yaz.",
        user_prompt=(
            f"Tarih: {now_tr} TR\n\n"
            "Güncel makroekonomik ortamı değerlendir. XAU/USD ve NAS100 için:\n"
            "1. Genel sentiment\n2. Risk faktörleri\n3. Fırsat/tehdit\n\n"
            "ICT perspektifinden kısa yorum yap."
        ),
        max_tokens=600,
    )
    if result:
        await update.message.reply_text(f"📰 Haber Analizi\n\n{result}")
    else:
        await update.message.reply_text("❌ Haber analizi alınamadı.")

async def cmd_ac(update, ctx):
    global bot_active
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Yetkin yok."); return
    bot_active = True
    await update.message.reply_text("✅ Bot aktif!")

async def cmd_kapat(update, ctx):
    global bot_active
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Yetkin yok."); return
    bot_active = False
    await update.message.reply_text("⛔ Bot durduruldu.")

async def cmd_reset(update, ctx):
    global stats, stats_per_symbol, results_history, last_signal_time
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Yetkin yok."); return
    stats = {"total": 0, "win": 0, "loss": 0}
    stats_per_symbol = {s: {"total": 0, "win": 0, "loss": 0} for s in SYMBOLS}
    results_history = []; last_signal_time = {}; aktif_sinyaller.clear()
    persist_save()
    await update.message.reply_text("🔄 Reset tamamlandı.")

async def cmd_dashboard(update, ctx):
    wr    = stats["win"] / stats["total"] * 100 if stats["total"] else 0
    son10 = " ".join("W" if r == "W" else "L" for r in results_history[-10:]) or "-"
    perf_lines = []
    for s, v in stats_per_symbol.items():
        if v["total"] > 0:
            swr  = v["win"] / v["total"] * 100
            name = SYMBOLS.get(s, {}).get("name", s)
            perf_lines.append(f"  {name}: {v['total']}T W{v['win']} L{v['loss']} WR%{swr:.0f}")
    perf  = "\n".join(perf_lines) or "  Henuz islem yok"
    aktif_lines = []
    for s, v in aktif_sinyaller.items():
        aktif_lines.append(f"  {SYMBOLS.get(s,{}).get('name',s)} {v['direction']}")
    aktif = "\n".join(aktif_lines) or "  Yok"
    await update.message.reply_text(
        "-- WARREN DASHBOARD --\n\n"
        f"Bot: {'Aktif' if bot_active else 'Kapali'} | {get_session()}\n"
        f"Toplam: {stats['total']} W:{stats['win']} L:{stats['loss']} WR%{wr:.1f}\n"
        f"Son 10: {son10}\n\n"
        f"{perf}\n\nAktif:\n{aktif}\n\n"
        f"Min RR: 1:{MIN_RR} | Min Conf: {MIN_CONFLUENCE}/6\n"
        f"Risk: %{RISK_PER_TRADE*100:.0f}/trade"
    )

async def cmd_takvim(update, ctx):
    """Bugünün ekonomik takvimini resimde görülen formatta göster."""
    now_tr  = datetime.utcnow() + timedelta(hours=3)
    tarih   = now_tr.strftime("%d.%m.%Y")
    events  = get_economic_calendar_api()

    if events is None:
        await update.message.reply_text(
            f"📅 Ekonomik Takvim ({tarih})\n\n"
            "⚠️ FMP_API_KEY tanımlı değil ya da veri alınamadı.\n\n"
            "🔴 FOMC, NFP, CPI → yüksek volatilite\n"
            "🟡 PMI, ISM → orta etki\n\n"
            "Detaylı: investing.com/economic-calendar"
        )
        return

    mesaj = format_takvim_mesaji(events, tarih)
    await update.message.reply_text(mesaj)



async def cmd_alarm(update, ctx):
    if not ctx.args or len(ctx.args) < 3:
        await update.message.reply_text("Kullanım: /alarm SEMBOL FİYAT ust|alt"); return
    sym = ctx.args[0].upper().replace("/","")
    sym_map = {"XAUUSD":"XAU/USD","QQQ":"QQQ","BTCUSD":"BTC/USD","XAGUSD":"XAG/USD"}
    sym = sym_map.get(sym, sym)
    if sym not in SYMBOLS:
        await update.message.reply_text("Geçersiz sembol."); return
    try:
        hedef = float(ctx.args[1])
        yon   = ctx.args[2].lower()
        assert yon in ("ust","alt")
    except:
        await update.message.reply_text("Fiyat sayı, yön: ust veya alt"); return
    fiyat_alarmlari.append({"sembol":sym,"hedef":hedef,"yon":yon,"chat_id":update.effective_chat.id})
    await update.message.reply_text(f"🔔 Alarm eklendi: {sym} {hedef} {yon}")

async def cmd_seans(update, ctx):
    global kill_zone_only
    if not is_admin(update.effective_user.id): return
    kill_zone_only = not kill_zone_only
    await update.message.reply_text(f"Kill Zone filtresi: {'açık' if kill_zone_only else 'kapalı'}")

async def cmd_equity(update, ctx):
    uid      = update.effective_user.id
    kayitlar = pnl_db.get(uid, [])
    if not kayitlar:
        await update.message.reply_text("Henüz işlem yok."); return
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        cum=[]; s=0
        for k in kayitlar:
            s+=k["pnl"]; cum.append(s)
        plt.figure(figsize=(8,4))
        plt.plot(cum,color="#2ecc71",linewidth=2)
        plt.fill_between(range(len(cum)),cum,alpha=0.3)
        plt.axhline(0,color="gray",linestyle="--")
        plt.title("Equity Curve"); plt.ylabel("Cumulative PnL ($)")
        buf=io.BytesIO(); plt.savefig(buf,format="png",dpi=100); plt.close(); buf.seek(0)
        await update.message.reply_photo(photo=buf)
    except Exception as e:
        await update.message.reply_text(f"Grafik hatası: {e}")

async def cmd_backtest(update, ctx):
    args   = ctx.args
    symbol = args[0].upper() if args else "XAU/USD"
    sym_map= {"XAUUSD":"XAU/USD","XAGUSD":"XAGUSD","NAS100":"QQQ","BTCUSD":"BTC/USD"}
    symbol = sym_map.get(symbol, symbol)
    if symbol not in SYMBOLS:
        await update.message.reply_text(f"Geçersiz. Seçenekler: {', '.join(SYMBOLS)}"); return
    cfg = SYMBOLS[symbol]
    await update.message.reply_text(f"⏳ {symbol} backtest çalışıyor...")
    try:
        df     = get_candles(symbol, "1min", 100)
        df_htf = get_candles(symbol, cfg.get("htf","15min"), 30)
        if df is None or len(df) < 30:
            await update.message.reply_text("❌ Veri alınamadı."); return
        wins=losses=0; toplam_rr=0.0; islemler=[]
        for i in range(20, len(df)-5):
            parca = df.iloc[:i+1].reset_index(drop=True)
            sig   = analyze_ict(parca, df_htf)
            if not sig: continue
            entry=sig["price"]; tp=sig["tp"]; sl=sig["sl"]; direction=sig["direction"]
            gelecek=df.iloc[i+1:i+6]; sonuc=None
            for _, mum in gelecek.iterrows():
                if direction=="LONG":
                    if mum["h"]>=tp: sonuc="WIN"; break
                    elif mum["l"]<=sl: sonuc="LOSS"; break
                else:
                    if mum["l"]<=tp: sonuc="WIN"; break
                    elif mum["h"]>=sl: sonuc="LOSS"; break
            if sonuc=="WIN":   wins+=1;   toplam_rr+=sig["rr"]; islemler.append(("✅",sig["rr"]))
            elif sonuc=="LOSS":losses+=1; toplam_rr-=1.0;       islemler.append(("❌",-1.0))
        toplam=wins+losses
        wr=wins/toplam*100 if toplam else 0
        son10=" ".join(e for e,_ in islemler[-10:]) if islemler else "Sinyal yok"
        await update.message.reply_text(
            f"📊 Backtest - {SYMBOLS[symbol]['name']}\n"
            f"Toplam: {toplam} | ✅{wins} ❌{losses}\n"
            f"WR: %{wr:.1f} | Net R: {toplam_rr:+.1f}R\n"
            f"Son 10: {son10}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Backtest hatası: {e}")


# ── GRUP YÖNETİMİ ────────────────────────────────────────────

async def get_target(update, ctx):
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user
    if ctx.args:
        username = ctx.args[0].lstrip("@")
        try:
            member = await ctx.bot.get_chat_member(update.effective_chat.id, username)
            return member.user
        except:
            await update.message.reply_text(f"Kullanıcı bulunamadı: @{username}")
    else:
        await update.message.reply_text("Kullanıcı belirt: reply yap veya @username yaz.")
    return None

async def cmd_kick(update, ctx):
    if not is_admin(update.effective_user.id): return
    t = await get_target(update, ctx)
    if not t: return
    try:
        await ctx.bot.ban_chat_member(update.effective_chat.id, t.id)
        await ctx.bot.unban_chat_member(update.effective_chat.id, t.id)
        await update.message.reply_text(f"{t.first_name} atıldı.")
    except Exception as e: await update.message.reply_text(f"Hata: {e}")

async def cmd_ban(update, ctx):
    if not is_admin(update.effective_user.id): return
    t = await get_target(update, ctx)
    if not t: return
    try:
        await ctx.bot.ban_chat_member(update.effective_chat.id, t.id)
        await update.message.reply_text(f"{t.first_name} banlandı.")
    except Exception as e: await update.message.reply_text(f"Hata: {e}")

async def cmd_unban(update, ctx):
    if not is_admin(update.effective_user.id): return
    t = await get_target(update, ctx)
    if not t: return
    try:
        await ctx.bot.unban_chat_member(update.effective_chat.id, t.id)
        await update.message.reply_text(f"{t.first_name} ban kaldırıldı.")
    except Exception as e: await update.message.reply_text(f"Hata: {e}")

async def cmd_mute(update, ctx):
    if not is_admin(update.effective_user.id): return
    t = await get_target(update, ctx)
    if not t: return
    dk = int(ctx.args[0]) if ctx.args else 10
    try:
        await ctx.bot.restrict_chat_member(
            update.effective_chat.id, t.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.utcnow() + timedelta(minutes=dk),
        )
        await update.message.reply_text(f"{t.first_name} {dk}dk susturuldu.")
    except Exception as e: await update.message.reply_text(f"Hata: {e}")

async def cmd_unmute(update, ctx):
    if not is_admin(update.effective_user.id): return
    t = await get_target(update, ctx)
    if not t: return
    try:
        await ctx.bot.restrict_chat_member(
            update.effective_chat.id, t.id,
            permissions=ChatPermissions(can_send_messages=True, can_send_other_messages=True,
                                        can_add_web_page_previews=True),
        )
        await update.message.reply_text(f"{t.first_name} sesi açıldı.")
    except Exception as e: await update.message.reply_text(f"Hata: {e}")

async def cmd_uyar(update, ctx):
    if not is_admin(update.effective_user.id): return
    t = await get_target(update, ctx)
    if not t: return
    sebep = " ".join(ctx.args) if ctx.args else "Kural ihlali"
    warnings_db[t.id] = warnings_db.get(t.id, 0) + 1
    count = warnings_db[t.id]
    msg   = f"{t.first_name} uyarıldı! ({count}/3)\nSebep: {sebep}"
    if count >= 3:
        try:
            await ctx.bot.ban_chat_member(update.effective_chat.id, t.id)
            msg += "\n3 uyarı → BANLANDI!"; warnings_db[t.id] = 0
        except Exception as e: msg += f"\nBan hatası: {e}"
    await update.message.reply_text(msg)

async def cmd_uyarlar(update, ctx):
    t = await get_target(update, ctx)
    if not t: return
    await update.message.reply_text(f"{t.first_name}: {warnings_db.get(t.id, 0)}/3 uyarı")

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
                until_date=datetime.utcnow() + timedelta(minutes=5),
            )
            await update.message.reply_text(f"{update.effective_user.first_name} spam → 5dk mute.")
            message_counts[uid] = []
        except: pass

async def welcome(update, ctx):
    for m in update.message.new_chat_members:
        if not m.is_bot:
            await update.message.reply_text(f"Hoş geldin {m.first_name}! ICT sinyal grubuna katıldın. 📊")


# ── PNL ──────────────────────────────────────────────────────

async def cmd_pnl_dispatcher(update, ctx):
    if not ctx.args:
        await update.message.reply_text(
            "📊 PnL Komutları\n\n"
            "/pnl ekle SEMBOL YÖN GİRİŞ ÇIKIŞ LOT [sebep]\n"
            "/pnl liste\n/pnl journal\n/pnl sifirla\n\n"
            "Örnek: /pnl ekle XAUUSD LONG 1950 1970 0.1 ICT Long"
        ); return
    alt = ctx.args[0].lower(); ctx.args = ctx.args[1:]
    if alt == "ekle":    await cmd_pnl_ekle(update, ctx)
    elif alt == "liste": await cmd_pnl_liste(update, ctx)
    elif alt == "journal": await cmd_pnl_journal(update, ctx)
    elif alt == "sifirla": await cmd_pnl_sifirla(update, ctx)
    else: await update.message.reply_text("Geçersiz. /pnl yaz.")

async def cmd_pnl_ekle(update, ctx):
    uid  = update.effective_user.id
    args = ctx.args
    if len(args) < 5:
        await update.message.reply_text("/pnl ekle SEMBOL YÖN GİRİŞ ÇIKIŞ LOT [sebep]"); return
    try:
        sembol = args[0].upper(); yon = args[1].upper()
        giris  = float(args[2]);  cikis = float(args[3]); lot = float(args[4])
        sebep  = " ".join(args[5:]) if len(args) > 5 else ""
        fark   = (cikis - giris) if yon == "LONG" else (giris - cikis)
        if "XAU" in sembol or "GOLD" in sembol:   pnl = fark * lot * 100
        elif "XAG" in sembol or "SILVER" in sembol: pnl = fark * lot * 50
        elif "US100" in sembol or "QQQ" in sembol:  pnl = fark * lot * 10
        elif "BTC"  in sembol:                       pnl = fark * lot
        else:                                         pnl = fark * lot * 100000 * 0.0001
        kayit = {"sembol":sembol,"yon":yon,"giris":giris,"cikis":cikis,
                 "lot":lot,"pnl":round(pnl,2),"tarih":datetime.utcnow().strftime("%Y-%m-%d %H:%M"),"sebep":sebep}
        pnl_db.setdefault(uid, []).append(kayit)
        persist_save()
        await update.message.reply_text(
            f"{'✅' if pnl>0 else '❌'} Kaydedildi\n"
            f"{sembol} {yon} | {giris}→{cikis} | Lot:{lot} | P/L: ${pnl:+.2f}"
        )
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def cmd_pnl_liste(update, ctx):
    uid      = update.effective_user.id
    kayitlar = pnl_db.get(uid, [])
    if not kayitlar:
        await update.message.reply_text("Henüz işlem yok."); return
    toplam   = sum(k["pnl"] for k in kayitlar)
    kazanan  = sum(1 for k in kayitlar if k["pnl"] > 0)
    wr       = kazanan / len(kayitlar) * 100
    lines    = [f"📊 PnL ({len(kayitlar)} işlem)\n"]
    for k in kayitlar[-10:]:
        lines.append(f"{'✅' if k['pnl']>0 else '❌'} {k['sembol']} {k['yon']} ${k['pnl']:+.2f}")
    lines.append(f"\n💰 Toplam: ${toplam:+.2f}  WR:%{wr:.1f} ({kazanan}W/{len(kayitlar)-kazanan}L)")
    await update.message.reply_text("\n".join(lines))

async def cmd_pnl_journal(update, ctx):
    uid      = update.effective_user.id
    kayitlar = pnl_db.get(uid, [])
    if not kayitlar:
        await update.message.reply_text("Henüz işlem yok."); return
    lines = ["📓 Trade Journal\n"]
    for k in kayitlar[-15:]:
        sebep = f" | {k['sebep']}" if k.get("sebep") else ""
        lines.append(f"{'✅' if k['pnl']>0 else '❌'} {k['tarih']} {k['sembol']} {k['yon']} ${k['pnl']:+.2f}{sebep}")
    await update.message.reply_text("\n".join(lines))

async def cmd_pnl_sifirla(update, ctx):
    pnl_db[update.effective_user.id] = []
    await update.message.reply_text("🗑️ PnL kayıtları silindi.")


# ── TP/SL TAKİPÇİSİ ─────────────────────────────────────────

async def check_tp_sl(app):
    """
    Mum high/low ile TP/SL kontrolü — anlık fiyattan daha güvenilir.
    Son kapanan mumu alır, direction'a göre h veya l'yi kontrol eder.
    """
    global aktif_sinyaller, stats_per_symbol, results_history
    kapatilacak = []
    for symbol, sig in list(aktif_sinyaller.items()):
        cfg = SYMBOLS.get(symbol, {})
        df  = get_candles(symbol, cfg.get("interval", "1min"), 3)
        if df is None or len(df) < 1:
            continue
        # Son kapanan mum (en son indeks = en yeni, tersine çevrilmiş)
        last = df.iloc[-1]
        high, low = float(last["h"]), float(last["l"])
        direction = sig["direction"]; tp = sig["tp"]; sl = sig["sl"]
        hit = None
        if direction == "LONG":
            if high >= tp:  hit = "TP"
            elif low <= sl: hit = "SL"
        else:
            if low <= tp:    hit = "TP"
            elif high >= sl: hit = "SL"
        if hit:
            if hit == "TP": stats["win"] += 1;  results_history.append("W")
            else:           stats["loss"] += 1; results_history.append("L")
            if len(results_history) > 50: results_history[:] = results_history[-50:]
            stats_per_symbol.setdefault(symbol, {"total":0,"win":0,"loss":0})
            stats_per_symbol[symbol]["total"] += 1
            if hit == "TP": stats_per_symbol[symbol]["win"] += 1
            else:           stats_per_symbol[symbol]["loss"] += 1
            price_ref = tp if hit == "TP" else sl
            try:
                await app.bot.send_message(
                    chat_id=TG_CHAT_ID,
                    text=(
                        f"{'✅' if hit=='TP' else '❌'} {hit} HIT\n"
                        f"{SYMBOLS.get(symbol,{}).get('name',symbol)} {direction}\n"
                        f"Giriş:{sig['entry']:.2f} → {'TP' if hit=='TP' else 'SL'}:{price_ref:.2f}\n"
                        f"Toplam: {stats['total']} W:{stats['win']} L:{stats['loss']}"
                    )
                )
            except Exception as e:
                log.error(f"TP/SL mesaj hatası: {e}")
            kapatilacak.append(symbol)
    for s in kapatilacak:
        aktif_sinyaller.pop(s, None)
    if kapatilacak:
        persist_save()


# ── KILL ZONE BİLDİRİMİ ──────────────────────────────────────

async def check_kill_zone_status(app):
    global son_kz_durum
    if not is_market_open(): return
    now_utc = datetime.utcnow()
    h = now_utc.hour; m = now_utc.minute
    for key, kz in KILL_ZONES.items():
        start_h = kz["start"]; end_h = kz["end"]; kz_name = kz["name"]
        if h == start_h and m < 5:
            anahtar = f"acilis_{key}_{now_utc.date()}"
            if son_kz_durum == anahtar: continue
            son_kz_durum = anahtar
            try:
                await app.bot.send_message(
                    chat_id=TG_CHAT_ID,
                    text=(
                        f"🟢 {kz_name} AÇILDI\n"
                        f"🕐 {start_h+3:02d}:00 - {end_h+3:02d}:00 TR\n"
                        f"📡 Sinyal tarama aktif..."
                    )
                )
            except Exception as e:
                log.error(f"KZ açılış mesaj hatası: {e}")
        if h == end_h and m < 5:
            anahtar = f"kapanis_{key}_{now_utc.date()}"
            if son_kz_durum == anahtar: continue
            son_kz_durum = anahtar
            try:
                await app.bot.send_message(
                    chat_id=TG_CHAT_ID,
                    text=f"🔴 {kz_name} KAPANDI\n📊 Sinyal tarama durduruldu."
                )
            except Exception as e:
                log.error(f"KZ kapanış mesaj hatası: {e}")


# ── GÜNLÜK / HAFTALIK ÖZET ───────────────────────────────────

async def send_daily_summary(app):
    if stats["total"] == 0: return
    wr = stats["win"] / stats["total"] * 100
    lines = [f"📊 Günlük Özet\nToplam:{stats['total']} W:{stats['win']} L:{stats['loss']} WR:%{wr:.1f}"]
    for sym, s in stats_per_symbol.items():
        if s["total"] > 0:
            swr = s["win"] / s["total"] * 100
            lines.append(f"  {SYMBOLS.get(sym,{}).get('name',sym)}: {s['total']} işlem WR%{swr:.1f}")
    try:
        await app.bot.send_message(chat_id=TG_CHAT_ID, text="\n".join(lines))
    except: pass

async def send_weekly_summary(app):
    if stats["total"] == 0: return
    wr = stats["win"] / stats["total"] * 100
    lines = [f"📈 Haftalık Özet\nToplam:{stats['total']} W:{stats['win']} L:{stats['loss']} WR:%{wr:.1f}"]
    for sym, s in stats_per_symbol.items():
        if s["total"] > 0:
            swr = s["win"] / s["total"] * 100
            lines.append(f"  {SYMBOLS.get(sym,{}).get('name',sym)}: {s['total']} işlem WR%{swr:.1f}")
    try:
        await app.bot.send_message(chat_id=TG_CHAT_ID, text="\n".join(lines))
    except: pass


# ── EKONOMİK TAKVİM KONTROLÜ ─────────────────────────────────

async def check_economic_calendar(app):
    """
    Her döngüde 30dk sonraki yüksek/orta etkili USD haberlerini kontrol et.
    Sabah 09:00 TR'de (06:00 UTC) aynı zamanda günün tüm takvimini özetle gönder.
    """
    global gonderilen_takvim_uyarilari
    now_utc = datetime.utcnow()
    if now_utc.weekday() >= 5: return

    bugun = str(now_utc.date())
    gonderilen_takvim_uyarilari = {k for k in gonderilen_takvim_uyarilari if k.endswith(bugun)}

    olaylar = get_economic_calendar_api()
    if not olaylar:
        return  # Veri yoksa uyarı göndermek yerine sessiz geç

    # ── Sabah özeti: 06:00 UTC (09:00 TR) - bir kez gönder
    sabah_key = f"sabah_ozet_{bugun}"
    if now_utc.hour == 6 and now_utc.minute < 5 and sabah_key not in gonderilen_takvim_uyarilari:
        gonderilen_takvim_uyarilari.add(sabah_key)
        now_tr = now_utc + timedelta(hours=3)
        tarih  = now_tr.strftime("%d.%m.%Y")
        mesaj  = format_takvim_mesaji(olaylar, tarih)
        try:
            await app.bot.send_message(chat_id=TG_CHAT_ID, text=mesaj)
            log.info("Sabah takvim ozeti gonderildi.")
        except Exception as e:
            log.error(f"Sabah takvim ozeti hatasi: {e}")

    # ── 30 dakika öncesi bireysel uyarılar
    for olay in olaylar:
        try:
            olay_saati = datetime.strptime(olay["saat"], "%H:%M").replace(
                year=now_utc.year, month=now_utc.month, day=now_utc.day)
        except (ValueError, TypeError):
            continue
        fark = (olay_saati - now_utc).total_seconds() / 60
        if 25 <= fark <= 35:
            anahtar = f"{olay['olay']}_{bugun}"
            if anahtar in gonderilen_takvim_uyarilari: continue
            gonderilen_takvim_uyarilari.add(anahtar)

            tr_saat = olay.get("tr_saat") or f"{int(olay['saat'][:2])+3:02d}:{olay['saat'][3:]}"
            etki    = olay.get("etki", "⚠️")

            # Tahmin / önceki değer satırı
            extras = []
            if olay.get("tahmin"): extras.append(f"Tahmin: {olay['tahmin']}")
            if olay.get("onceki"): extras.append(f"Önceki: {olay['onceki']}")
            extra_line = ("\n📊 " + " | ".join(extras)) if extras else ""

            try:
                await app.bot.send_message(
                    chat_id=TG_CHAT_ID,
                    text=(
                        f"⚠️ EKONOMİK TAKVİM - 30 DAKİKA KALDI!\n\n"
                        f"{etki} {olay['olay']}\n"
                        f"🌐 USD | 🕐 {tr_saat} TR"
                        f"{extra_line}\n\n"
                        f"⚡ Açık pozisyonlarını kontrol et!"
                    )
                )
            except Exception as e:
                log.error(f"Takvim uyarı hatası: {e}")


# ── MAIN ────────────────────────────────────────────────────

async def main():
    persist_load()  # Render restart sonrası state geri yükle
    app = Application.builder().token(TG_TOKEN).build()

    handlers = [
        ("start",       cmd_start),
        ("komutlar",    cmd_komutlar),
        ("durum",       cmd_durum),
        ("analiz",      cmd_analiz),
        ("istatistik",  cmd_istatistik),
        ("sinyal",      cmd_sinyal),
        ("htfanaliz",   cmd_htfanaliz),
        ("haber",       cmd_haber),
        ("ac",          cmd_ac),
        ("kapat",       cmd_kapat),
        ("reset",       cmd_reset),
        ("dashboard",   cmd_dashboard),
        ("takvim",      cmd_takvim),
        ("alarm",       cmd_alarm),
        ("seans",       cmd_seans),
        ("equity",      cmd_equity),
        ("pnl",         cmd_pnl_dispatcher),
        ("backtest",    cmd_backtest),
        ("kick",        cmd_kick),
        ("ban",         cmd_ban),
        ("unban",       cmd_unban),
        ("mute",        cmd_mute),
        ("unmute",      cmd_unmute),
        ("uyar",        cmd_uyar),
        ("uyarlar",     cmd_uyarlar),
    ]
    for name, fn in handlers:
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, spam_check), group=1)

    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        log.info("Warren Bot V4.1 başlatıldı! (DeepSeek AI)")
        await scan_loop(app)


async def run_all():
    await asyncio.gather(health_server(), main())


if __name__ == "__main__":
    asyncio.run(run_all())
