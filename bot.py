"""
Warren Bot V4 - Full Python ICT Trading & Grup Yonetim Botu
- Twelve Data API ile gercek zamanli fiyat verisi
- ICT sinyal tarama
- Claude AI ile gunluk HTF analiz (sabah 09:00 TR saati)
- Telegram grup yonetimi
- 7/24 Render.com'da calisir
"""

import os
import logging
import asyncio
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
import pandas as pd
import numpy as np
from telegram import Update, ChatPermissions
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ── AYARLAR ─────────────────────────────────────────────────
TG_TOKEN      = os.environ.get("TG_TOKEN",      "8698295551:AAFLixj0p8t7REyHcIkXnSp0gChNf6bNk6w")
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID",    "-1003838635441")
TD_API_KEY    = os.environ.get("TD_API_KEY",    "YOUR_TWELVEDATA_KEY")
CLAUDE_API_KEY= os.environ.get("CLAUDE_API_KEY","YOUR_CLAUDE_KEY")
ADMIN_IDS     = [6663913960]

SYMBOLS = {
    "XAU/USD": {"name": "Gold",   "interval": "1min"},
    "QQQ":     {"name": "US100",  "interval": "1min"},
    "EUR/USD": {"name": "EURUSD", "interval": "1min"},
    "GBP/USD": {"name": "GBPUSD", "interval": "1min"},
}

COOLDOWN_MIN    = 30
MIN_RR          = 2.0
OB_LOOKBACK     = 20
SIGNAL_INTERVAL = 60

stats            = {"total": 0, "win": 0, "loss": 0}
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
son_takvim_uyari = None

# ── KEEP ALIVE ───────────────────────────────────────────────
class KeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Warren Bot V4 caliyor!")
    def log_message(self, *a): pass

def start_server():
    HTTPServer(("0.0.0.0", 8080), KeepAlive).serve_forever()

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

# ── CLAUDE AI - GUNLUK ANALİZ ───────────────────────────────
def get_market_context():
    """Claude'a verilecek piyasa verilerini hazirla"""
    context = {}
    for symbol in ["XAU/USD", "QQQ", "EUR/USD"]:
        price = get_price(symbol)
        daily = get_daily_candles(symbol, 10)
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

def generate_daily_analysis(symbol_display, context_data):
    """Claude API ile gunluk HTF analiz olustur"""
    if not CLAUDE_API_KEY or CLAUDE_API_KEY == "YOUR_CLAUDE_KEY":
        return None

    now_tr = datetime.utcnow() + timedelta(hours=3)
    tarih  = now_tr.strftime("%d %B %Y - %A")

    # Piyasa verisini stringe cevir
    piyasa_str = ""
    for sym, data in context_data.items():
        piyasa_str += f"{sym}: Fiyat={data['price']}, Trend={data['trend']}, 5gun High={data['high5']}, 5gun Low={data['low5']}\n"

    prompt = f"""Sen bir ICT (Inner Circle Trader) piyasa analiz botusun. Asagidaki verilere gore {symbol_display} icin bugun ({tarih}) gunluk HTF analiz yaz.

MEVCUT PIYASA VERILERI:
{piyasa_str}

ONEMLI: Sinyal botu degilsin.
- "XYZ fiyatindan LONG AL" deme
- "Kesin kazanc" deme
- HTF bias + seviyeler + nedenlerini acikla
- Kullanici kendi kararini versin

Asagidaki FORMATTA Turkce analiz yaz (emojileri kullan):

📊 {symbol_display} - HTF ANALIZ
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
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        data = r.json()
        if "content" in data and data["content"]:
            return data["content"][0]["text"]
        log.error(f"Claude API hatasi: {data}")
        return None
    except Exception as e:
        log.error(f"Claude API istegi hatasi: {e}")
        return None

async def send_daily_analysis(app):
    """Sabah 09:00 TR saatinde gunluk analiz gonder"""
    global last_daily_analiz
    log.info("Gunluk analiz gonderiliyor...")

    context = get_market_context()
    if not context:
        await app.bot.send_message(chat_id=TG_CHAT_ID, text="Gunluk analiz icin veri alinamadi.")
        return

    # Her sembol icin ayri analiz
    for symbol_display in ["XAUUSD (Gold)", "NAS100 (QQQ)"]:
        analysis = generate_daily_analysis(symbol_display, context)
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
                text=f"{symbol_display} analizi olusturulamadi. CLAUDE_API_KEY kontrol et."
            )

    last_daily_analiz = datetime.utcnow().date()
    log.info("Gunluk analiz gonderildi!")

# ── ICT ANALİZ ───────────────────────────────────────────────
def analyze_ict(df):
    if df is None or len(df) < OB_LOOKBACK + 5:
        return None

    h = df["h"].values; l = df["l"].values
    o = df["o"].values; c = df["c"].values
    price = c[-1]; n = len(df)

    swing_high = max(h[-OB_LOOKBACK:-1])
    swing_low  = min(l[-OB_LOOKBACK:-1])

    buy_sweep  = l[-2] < swing_low  and c[-2] > swing_low
    sell_sweep = h[-2] > swing_high and c[-2] < swing_high

    has_bull_ob = has_bear_ob = False
    bull_ob_h = bull_ob_l = bear_ob_h = bear_ob_l = 0.0

    for i in range(2, OB_LOOKBACK - 1):
        idx = n - i - 1
        if idx < 1: break
        if c[idx] < o[idx] and not has_bull_ob:
            if any(h[idx-j] > h[idx+1] for j in range(1, min(5, idx))):
                has_bull_ob = True
                bull_ob_h = max(o[idx], c[idx]); bull_ob_l = min(o[idx], c[idx])
        if c[idx] > o[idx] and not has_bear_ob:
            if any(l[idx-j] < l[idx+1] for j in range(1, min(5, idx))):
                has_bear_ob = True
                bear_ob_h = max(o[idx], c[idx]); bear_ob_l = min(o[idx], c[idx])

    bull_fvg = bear_fvg = False
    fvg_h = fvg_l = 0.0
    for i in range(1, n - 2):
        if l[i+1] > h[i-1]: bull_fvg = True; fvg_h = l[i+1]; fvg_l = h[i-1]; break
        if h[i+1] < l[i-1]: bear_fvg = True; fvg_h = h[i+1]; fvg_l = l[i-1]; break

    bull_bos = c[-1] > max(h[-8:-1])
    bear_bos = c[-1] < min(l[-8:-1])

    move_high = max(h[-OB_LOOKBACK:]); move_low = min(l[-OB_LOOKBACK:])
    ote_high  = move_high - (move_high - move_low) * 0.62
    ote_low   = move_high - (move_high - move_low) * 0.79
    in_ote    = ote_low <= price <= ote_high

    htf_bias  = 1 if c[-1] > c[n//2] else -1

    bull_conf = []; bear_conf = []
    if buy_sweep: bull_conf.append("Likidite Sweep")
    if has_bull_ob and bull_ob_l <= price <= bull_ob_h * 1.002: bull_conf.append("Bullish OB")
    if bull_fvg and fvg_l <= price <= fvg_h: bull_conf.append("Bullish FVG")
    if bull_bos: bull_conf.append("BOS Yukari")
    if in_ote:   bull_conf.append("OTE Zone")
    if htf_bias == 1: bull_conf.append("HTF Bullish")

    if sell_sweep: bear_conf.append("Likidite Sweep")
    if has_bear_ob and bear_ob_l * 0.998 <= price <= bear_ob_h: bear_conf.append("Bearish OB")
    if bear_fvg and fvg_l <= price <= fvg_h: bear_conf.append("Bearish FVG")
    if bear_bos: bear_conf.append("BOS Asagi")
    if htf_bias == -1: bear_conf.append("HTF Bearish")

    direction = None; reasons = []
    if len(bull_conf) >= 2 and htf_bias >= 0: direction = "LONG";  reasons = bull_conf
    elif len(bear_conf) >= 2 and htf_bias <= 0: direction = "SHORT"; reasons = bear_conf
    if not direction: return None

    if direction == "LONG":
        sl = swing_low  - (swing_high - swing_low) * 0.01
        tp = price + (price - sl) * MIN_RR
    else:
        sl = swing_high + (swing_high - swing_low) * 0.01
        tp = price - (sl - price) * MIN_RR

    sl_pips = abs(price - sl); tp_pips = abs(tp - price)
    rr = tp_pips / sl_pips if sl_pips > 0 else 0
    if rr < MIN_RR: return None

    return {"direction": direction, "price": price, "sl": sl, "tp": tp,
            "sl_pips": sl_pips, "tp_pips": tp_pips, "rr": rr,
            "conf": len(reasons), "reasons": reasons}

def is_market_open():
    now = datetime.utcnow()
    if now.weekday() >= 5: return False
    return True

def get_session():
    h = datetime.utcnow().hour
    if 8  <= h < 12: return "London Kill Zone"
    if 13 <= h < 17: return "New York Kill Zone"
    if 0  <= h < 7:  return "Asya Seansi"
    return "Normal Seans"

def format_signal(symbol, sig):
    name = SYMBOLS.get(symbol, {}).get("name", symbol)
    ico  = "LONG ^" if sig["direction"] == "LONG" else "SHORT v"
    bar  = "+" * sig["conf"] + "-" * (6 - sig["conf"])
    reasons_text = "\n".join(f"  + {r}" for r in sig["reasons"])
    return (
        f"{'='*22}\n[{ico}] {name} ({symbol})\n{'='*22}\n"
        f"Confluance [{bar}] {sig['conf']}/6\n{reasons_text}\n{'='*22}\n"
        f"Giris  : {sig['price']:.4f}\n"
        f"SL     : {sig['sl']:.4f}  (-{sig['sl_pips']:.1f})\n"
        f"TP     : {sig['tp']:.4f}  (+{sig['tp_pips']:.1f})\n"
        f"RR     : 1:{sig['rr']:.1f}\n{'='*22}\n"
        f"Seans  : {get_session()}\nGiris karari SANA ait!"
    )

# ── ANA DONGÜ ────────────────────────────────────────────────
async def scan_loop(app):
    global last_daily_analiz
    log.info("Ana dongü basladi")

    while True:
        await asyncio.sleep(60)

        now_tr   = datetime.utcnow() + timedelta(hours=3)
        bugun    = now_tr.date()
        saat     = now_tr.hour
        dakika   = now_tr.minute

        # Sabah 09:00 TR saatinde gunluk analiz gonder (haftaici)
        if (saat == 9 and dakika == 0 and
            now_tr.weekday() < 5 and
            last_daily_analiz != bugun):
            await send_daily_analysis(app)

        # Ekonomik takvim kontrolu
        await check_economic_calendar(app)

        # TP/SL takip
        if aktif_sinyaller:
            await check_tp_sl(app)
        if not is_market_open():
            continue

        for symbol, cfg in SYMBOLS.items():
            try:
                last = last_signal_time.get(symbol)
                if last and (datetime.utcnow() - last).seconds < COOLDOWN_MIN * 60:
                    continue
                df  = get_candles(symbol, cfg["interval"], 50)
                sig = analyze_ict(df)
                if sig:
                    await app.bot.send_message(chat_id=TG_CHAT_ID, text=format_signal(symbol, sig))
                    last_signal_time[symbol] = datetime.utcnow()
                    aktif_sinyaller[symbol] = {
                        "direction": sig["direction"],
                        "entry": sig["price"],
                        "sl": sig["sl"],
                        "tp": sig["tp"],
                        "time": datetime.utcnow()
                    }
                    stats["total"] += 1
                    log.info(f"Sinyal: {symbol} {sig['direction']}")
            except Exception as e:
                log.error(f"Scan hatasi {symbol}: {e}")

# ── KOMUTLAR ────────────────────────────────────────────────
def is_admin(uid): return uid in ADMIN_IDS

async def cmd_start(update, ctx):
    await update.message.reply_text(
        "Warren Bot V4 Aktif!\n====================\n"
        "/durum /fiyat /analiz /sinyal /istatistik\n"
        "/htfanaliz - Simdi gunluk analiz gonder\n"
        "/ac /kapat\n====================\n"
        "GRUP: /kick /ban /unban /mute /unmute /uyar /uyarlar"
    )

async def cmd_yardim(update, ctx): await cmd_start(update, ctx)

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
        p = get_price(symbol)
        lines.append(f"{cfg['name']:8}: {p:.4f}" if p else f"{cfg['name']:8}: Alinamadi")
    await update.message.reply_text("\n".join(lines))

async def cmd_analiz(update, ctx):
    symbol = (ctx.args[0].upper() if ctx.args else "XAU/USD")
    if symbol not in SYMBOLS:
        await update.message.reply_text(f"Gecersiz. Secenekler: {', '.join(SYMBOLS)}"); return
    await update.message.reply_text(f"{symbol} analiz ediliyor...")
    df = get_candles(symbol, SYMBOLS[symbol]["interval"], 50)
    if df is None:
        await update.message.reply_text("Veri alinamadi."); return
    sig = analyze_ict(df)
    if sig: await update.message.reply_text(format_signal(symbol, sig))
    else:   await update.message.reply_text(f"{symbol}: Setup yok, bekleniyor... ({get_session()})")

async def cmd_istatistik(update, ctx):
    wr = stats["win"] / stats["total"] * 100 if stats["total"] else 0
    await update.message.reply_text(
        f"Toplam: {stats['total']}  Kazan: {stats['win']}  Kaybet: {stats['loss']}\nWR: %{wr:.1f}"
    )

async def cmd_sinyal(update, ctx):
    global last_signal_time
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Yetkin yok."); return
    req = " ".join(ctx.args).upper().replace(" ", "/") if ctx.args else None
    if req and req not in SYMBOLS:
        mevcut = ", ".join(SYMBOLS.keys())
        await update.message.reply_text("Bilinmeyen sembol: " + req + "\nMevcut: " + mevcut); return
    scan_symbols = {req: SYMBOLS[req]} if req else SYMBOLS
    await update.message.reply_text(f"Taranıyor: {', '.join(scan_symbols.keys())}...")
    last_signal_time = {}; found = False
    for symbol, cfg in scan_symbols.items():
        df = get_candles(symbol, cfg["interval"], 50); sig = analyze_ict(df)
        if sig:
            await update.message.reply_text(format_signal(symbol, sig))
            stats["total"] += 1; last_signal_time[symbol] = datetime.utcnow(); found = True
    if not found: await update.message.reply_text("Setup yok, bekleniyor...")

async def cmd_htfanaliz(update, ctx):
    """Manuel gunluk analiz tetikle"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Yetkin yok."); return
    await update.message.reply_text("Gunluk HTF analiz hazirlaniyor, 30 saniye bekle...")
    await send_daily_analysis(ctx.application)

async def cmd_ac(update, ctx):
    global bot_active
    if not is_admin(update.effective_user.id): return
    bot_active = True; await update.message.reply_text("Bot aktif!")

async def cmd_kapat(update, ctx):
    global bot_active
    if not is_admin(update.effective_user.id): return
    bot_active = False; await update.message.reply_text("Bot durduruldu. /ac ile baslatabilirsin.")

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
            await update.message.reply_text(f"Kullanici bulunamadi: @{username}")
            return None
    await update.message.reply_text("Kullanici belirt: reply yap veya @username yaz.")
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
    t = await get_target(update, ctx)
    if not t: return
    dk = int(ctx.args[0]) if ctx.args else 10
    try:
        await ctx.bot.restrict_chat_member(
            update.effective_chat.id, t.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.utcnow() + timedelta(minutes=dk)
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
            "`/pnl ekle SEMBOL YON GIRIS CIKIS LOT`\n"
            "`/pnl liste` - İşlemlerini gör\n"
            "`/pnl sifirla` - Kayıtları temizle\n\n"
            "Örnek: `/pnl ekle XAUUSD LONG 1950 1970 0.1`",
            parse_mode="Markdown"
        )
        return
    alt = ctx.args[0].lower()
    ctx.args = ctx.args[1:]
    if alt == "ekle":
        await cmd_pnl_ekle(update, ctx)
    elif alt == "liste":
        await cmd_pnl_liste(update, ctx)
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
    """Kullanim: /pnl ekle XAUUSD LONG 1950.00 1970.00 0.1"""
    uid = update.effective_user.id
    args = ctx.args
    if len(args) < 5:
        await update.message.reply_text(
            "Kullanim: `/pnl ekle SEMBOL YON GIRIS CIKIS LOT`\n"
            "Ornek: `/pnl ekle XAUUSD LONG 1950.00 1970.00 0.1`",
            parse_mode="Markdown"
        )
        return
    try:
        sembol = args[0].upper()
        yon    = args[1].upper()
        giris  = float(args[2])
        cikis  = float(args[3])
        lot    = float(args[4])

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
            "tarih": datetime.utcnow().strftime("%Y-%m-%d %H:%M")
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

async def cmd_pnl_sifirla(update, ctx):
    uid = update.effective_user.id
    pnl_db[uid] = []
    await update.message.reply_text("🗑️ PnL kayitlari silindi.")

# ── HABER SENTİMENT ANALİZİ ─────────────────────────────────
async def cmd_haber(update, ctx):
    """Deepseek ile gold/nas haber sentiment analizi"""
    await update.message.reply_text("📰 Haberler analiz ediliyor...")

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

        now_tr = datetime.utcnow() + timedelta(hours=3)
        tarih = now_tr.strftime("%d %B %Y %H:%M")

        mesaj = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            messages=[{
                "role": "user",
                "content": (
                    f"Tarih: {tarih} TR saati\n\n"
                    "Şu an piyasaları etkileyen güncel haberleri ve makroekonomik ortamı değerlendir. "
                    "XAU/USD (Gold) ve NAS100 için:\n"
                    "1. Genel piyasa sentiment'i (Bullish/Bearish/Nötr)\n"
                    "2. Risk faktörleri\n"
                    "3. Kısa vadeli fırsat/tehdit\n\n"
                    "Kısa ve ICT perspektifinden yorum yap."
                )
            }]
        )
        analiz = mesaj.content[0].text
        await update.message.reply_text(f"📰 *Haber Sentiment Analizi*\n\n{analiz}", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Hata: {e}")

# ── EKONOMİK TAKVİM ─────────────────────────────────────────
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
    global aktif_sinyaller
    kapatilacak = []

    for symbol, sig in aktif_sinyaller.items():
        price = get_price(symbol)
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
            pnl_pips = abs(tp - entry) if hit == "TP" else abs(sl - entry)
            emoji = "✅" if hit == "TP" else "❌"
            sonuc = "KAZANÇ" if hit == "TP" else "KAYIP"

            if hit == "TP":
                stats["win"] += 1
            else:
                stats["loss"] += 1

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

    if symbol not in SYMBOLS and symbol == "XAUUSD":
        symbol = "XAU/USD"
    elif symbol == "US100" or symbol == "NAS100":
        symbol = "QQQ"

    await update.message.reply_text(f"⏳ {symbol} için backtest çalışıyor... (100 mum)")

    try:
        df = get_candles(symbol, "1min", 100)
        if df is None or len(df) < 30:
            await update.message.reply_text("❌ Veri alınamadı.")
            return

        wins = losses = 0
        toplam_rr = 0.0
        islemler = []

        for i in range(20, len(df) - 5):
            parca = df.iloc[:i+1].reset_index(drop=True)
            sig = analyze_ict(parca)
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

async def check_economic_calendar(app):
    """Her saat basinda ekonomik takvim kontrolu"""
    global son_takvim_uyari
    now_utc = datetime.utcnow()
    now_tr  = now_utc + timedelta(hours=3)
    saat_str = now_utc.strftime("%H:%M")

    for olay in EKONOMIK_OLAYLAR:
        # Saat 30dk oncesi uyari
        olay_saati = datetime.strptime(olay["saat"], "%H:%M").replace(
            year=now_utc.year, month=now_utc.month, day=now_utc.day
        )
        fark = (olay_saati - now_utc).total_seconds() / 60

        if 25 <= fark <= 35:  # 30dk oncesi pencere
            anahtar = f"{olay['olay']}_{now_utc.date()}"
            if son_takvim_uyari == anahtar:
                continue
            son_takvim_uyari = anahtar

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
    threading.Thread(target=start_server, daemon=True).start()

    app = Application.builder().token(TG_TOKEN).build()

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("yardim",     cmd_yardim))
    app.add_handler(CommandHandler("durum",      cmd_durum))
    app.add_handler(CommandHandler("fiyat",      cmd_fiyat))
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
    app.add_handler(CommandHandler("takvim",     cmd_takvim))
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
    site = web.TCPSite(runner, "0.0.0.0", 10000)
    await site.start()
    log.info("Health server started on port 8080")

async def run_all():
    await asyncio.gather(health_server(), main())

if __name__ == "__main__":
    asyncio.run(run_all())
