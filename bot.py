"""
Warren Bot V4 - Full Python ICT Trading & Grup Yonetim Botu
- Twelve Data API ile gercek zamanli fiyat verisi
- ICT: Order Block, FVG, Liquidity Sweep, BOS, OTE
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

# ─── LOGGING ────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─── AYARLAR ────────────────────────────────────────────────
TG_TOKEN     = os.environ.get("TG_TOKEN",    "8698295551:AAFLixj0p8t7REyHcIkXnSp0gChNf6bNk6w")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID",  "-1003838635441")
TD_API_KEY   = os.environ.get("TD_API_KEY",  "YOUR_TWELVEDATA_KEY")  # twelvedata.com'dan al
ADMIN_IDS    = [6663913960]

SYMBOLS = {
    "XAUUSD": {"name": "Gold",   "interval": "1min"},
    "US100":  {"name": "US100",  "interval": "1min"},
}

COOLDOWN_MIN   = 30    # Ayni sembol icin min sinyal arasi (dakika)
MIN_RR         = 2.0   # Min risk/reward
OB_LOOKBACK    = 20    # Order block arama penceresi
SIGNAL_INTERVAL= 60    # Kac saniyede bir sinyal tara

# ─── STATE ──────────────────────────────────────────────────
stats = {"total": 0, "win": 0, "loss": 0}
warnings_db = {}
message_counts = {}
last_signal_time = {}   # symbol: datetime
bot_active = True
app_ref = None          # Telegram app referansi

# ─── KEEP ALIVE (Render icin) ───────────────────────────────
class KeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Warren Bot V4 caliyor!")
    def log_message(self, *args):
        pass

def start_server():
    HTTPServer(("0.0.0.0", 8080), KeepAlive).serve_forever()

# ─── TWELVE DATA API ────────────────────────────────────────
def get_candles(symbol: str, interval: str = "1min", outputsize: int = 50) -> pd.DataFrame | None:
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol":     symbol,
        "interval":   interval,
        "outputsize": outputsize,
        "apikey":     TD_API_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if "values" not in data:
            log.warning(f"API hatasi {symbol}: {data.get('message','?')}")
            return None
        df = pd.DataFrame(data["values"])
        df = df.rename(columns={"datetime":"time","open":"o","high":"h","low":"l","close":"c","volume":"v"})
        df = df.astype({"o": float, "h": float, "l": float, "c": float})
        df = df.iloc[::-1].reset_index(drop=True)  # En eskiden en yeniye sirala
        return df
    except Exception as e:
        log.error(f"API istegi hatasi: {e}")
        return None

def get_price(symbol: str) -> float | None:
    url = "https://api.twelvedata.com/price"
    try:
        r = requests.get(url, params={"symbol": symbol, "apikey": TD_API_KEY}, timeout=5)
        data = r.json()
        return float(data.get("price", 0)) or None
    except:
        return None

# ─── ICT ANALİZ ─────────────────────────────────────────────
def analyze_ict(df: pd.DataFrame) -> dict | None:
    """
    ICT: Liquidity Sweep + Order Block + FVG + BOS + OTE
    En az 2 confluance varsa sinyal uret
    """
    if df is None or len(df) < OB_LOOKBACK + 5:
        return None

    h = df["h"].values
    l = df["l"].values
    o = df["o"].values
    c = df["c"].values

    price = c[-1]
    n     = len(df)

    # ── Swing High / Low ──
    swing_high = max(h[-OB_LOOKBACK:-1])
    swing_low  = min(l[-OB_LOOKBACK:-1])

    # ── Liquidity Sweep ──
    last_h = h[-2]; last_l = l[-2]; last_c = c[-2]
    buy_sweep  = last_l < swing_low  and last_c > swing_low
    sell_sweep = last_h > swing_high and last_c < swing_high

    # ── Order Block ──
    bull_ob_h = bull_ob_l = 0.0
    bear_ob_h = bear_ob_l = 0.0
    has_bull_ob = has_bear_ob = False

    for i in range(2, OB_LOOKBACK - 1):
        idx = n - i - 1
        if idx < 1: break
        # Bullish OB: bearish mum + sonra yukselen BOS
        if c[idx] < o[idx] and not has_bull_ob:
            if any(h[idx-j] > h[idx+1] for j in range(1, min(5, idx))):
                has_bull_ob = True
                bull_ob_h = max(o[idx], c[idx])
                bull_ob_l = min(o[idx], c[idx])
        # Bearish OB: bullish mum + sonra dusen BOS
        if c[idx] > o[idx] and not has_bear_ob:
            if any(l[idx-j] < l[idx+1] for j in range(1, min(5, idx))):
                has_bear_ob = True
                bear_ob_h = max(o[idx], c[idx])
                bear_ob_l = min(o[idx], c[idx])

    # ── FVG (Fair Value Gap) ──
    bull_fvg = bear_fvg = False
    fvg_h = fvg_l = 0.0
    for i in range(1, n - 2):
        if l[i+1] > h[i-1]:  # Bullish FVG
            bull_fvg = True; fvg_h = l[i+1]; fvg_l = h[i-1]; break
        if h[i+1] < l[i-1]:  # Bearish FVG
            bear_fvg = True; fvg_h = h[i+1]; fvg_l = l[i-1]; break

    # ── BOS (Break of Structure) ──
    recent_high = max(h[-8:-1])
    recent_low  = min(l[-8:-1])
    bull_bos = c[-1] > recent_high
    bear_bos = c[-1] < recent_low

    # ── OTE (Fibonacci 0.62-0.79) ──
    move_high = max(h[-OB_LOOKBACK:])
    move_low  = min(l[-OB_LOOKBACK:])
    ote_high  = move_high - (move_high - move_low) * 0.62
    ote_low   = move_high - (move_high - move_low) * 0.79
    in_ote    = ote_low <= price <= ote_high

    # ── HTF Bias (son 20 mumdaki yon) ──
    htf_mid = len(df) // 2
    htf_bullish = c[-1] > c[htf_mid]
    htf_bias = 1 if htf_bullish else -1

    # ── Confluance sayaci ──
    bull_conf = []; bear_conf = []

    if buy_sweep:  bull_conf.append("Likidite Sweep")
    if has_bull_ob and bull_ob_l <= price <= bull_ob_h * 1.002:
        bull_conf.append("Bullish OB")
    if bull_fvg and fvg_l <= price <= fvg_h:
        bull_conf.append("Bullish FVG")
    if bull_bos:   bull_conf.append("BOS Yukari")
    if in_ote:     bull_conf.append("OTE Zone (Fib)")
    if htf_bias == 1: bull_conf.append("HTF Bullish")

    if sell_sweep: bear_conf.append("Likidite Sweep")
    if has_bear_ob and bear_ob_l * 0.998 <= price <= bear_ob_h:
        bear_conf.append("Bearish OB")
    if bear_fvg and fvg_l <= price <= fvg_h:
        bear_conf.append("Bearish FVG")
    if bear_bos:   bear_conf.append("BOS Asagi")
    if htf_bias == -1: bear_conf.append("HTF Bearish")

    # ── Sinyal uret ──
    direction = None
    reasons   = []

    if len(bull_conf) >= 2 and htf_bias >= 0:
        direction = "LONG"; reasons = bull_conf
    elif len(bear_conf) >= 2 and htf_bias <= 0:
        direction = "SHORT"; reasons = bear_conf

    if not direction:
        return None

    # ── SL / TP ──
    if direction == "LONG":
        sl = swing_low - (swing_high - swing_low) * 0.01
        tp = price + (price - sl) * MIN_RR
    else:
        sl = swing_high + (swing_high - swing_low) * 0.01
        tp = price - (sl - price) * MIN_RR

    sl_pips = abs(price - sl)
    tp_pips = abs(tp - price)
    rr      = tp_pips / sl_pips if sl_pips > 0 else 0

    if rr < MIN_RR:
        return None

    return {
        "direction": direction,
        "price":     price,
        "sl":        sl,
        "tp":        tp,
        "sl_pips":   sl_pips,
        "tp_pips":   tp_pips,
        "rr":        rr,
        "conf":      len(reasons),
        "reasons":   reasons,
    }

def get_session() -> str:
    h = datetime.utcnow().hour
    if 0  <= h < 7:  return "Asya Seansı"
    if 7  <= h < 8:  return "Pre-London"
    if 8  <= h < 12: return "London Kill Zone"
    if 12 <= h < 13: return "London-NY Overlap"
    if 13 <= h < 17: return "New York Kill Zone"
    if 17 <= h < 21: return "NY Kapanis"
    return "Sessiz Seans"

def conf_bar(n: int) -> str:
    return "+" * n + "-" * (6 - n)

def format_signal(symbol: str, sig: dict) -> str:
    name = SYMBOLS.get(symbol, {}).get("name", symbol)
    ico  = "LONG ▲" if sig["direction"] == "LONG" else "SHORT ▼"
    bar  = conf_bar(sig["conf"])

    reasons_text = "\n".join(f"  + {r}" for r in sig["reasons"])

    msg = (
        f"{'='*22}\n"
        f"[{ico}] {name} ({symbol})\n"
        f"{'='*22}\n"
        f"Confluance [{bar}] {sig['conf']}/6\n"
        f"{reasons_text}\n"
        f"{'='*22}\n"
        f"Giris  : {sig['price']:.4f}\n"
        f"SL     : {sig['sl']:.4f}  (-{sig['sl_pips']:.1f})\n"
        f"TP     : {sig['tp']:.4f}  (+{sig['tp_pips']:.1f})\n"
        f"RR     : 1:{sig['rr']:.1f}\n"
        f"{'='*22}\n"
        f"Seans  : {get_session()}\n"
        f"{'='*22}\n"
        f"Giris karari SANA ait!"
    )
    return msg

# ─── SİNYAL TARAMA DÖNGÜSÜ ──────────────────────────────────
async def scan_loop():
    global bot_active, app_ref
    log.info("Sinyal tarama dongusu basladi")

    while True:
        await asyncio.sleep(SIGNAL_INTERVAL)

        if not bot_active or app_ref is None:
            continue

        for symbol, cfg in SYMBOLS.items():
            try:
                # Cooldown kontrolu
                last = last_signal_time.get(symbol)
                if last and (datetime.utcnow() - last).seconds < COOLDOWN_MIN * 60:
                    continue

                df = get_candles(symbol, cfg["interval"], outputsize=50)
                sig = analyze_ict(df)

                if sig:
                    msg = format_signal(symbol, sig)
                    await app_ref.bot.send_message(chat_id=TG_CHAT_ID, text=msg)
                    last_signal_time[symbol] = datetime.utcnow()
                    stats["total"] += 1
                    log.info(f"Sinyal gonderildi: {symbol} {sig['direction']}")

            except Exception as e:
                log.error(f"Scan hatasi {symbol}: {e}")

# ─── TELEGRAM KOMUTLARI ─────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Warren Bot V4 Aktif!\n"
        "====================\n"
        "TRADING:\n"
        "/durum       - Bot durumu\n"
        "/fiyat       - Anlik fiyatlar\n"
        "/analiz      - ICT analizi\n"
        "/istatistik  - Sinyal istatistigi\n"
        "/sinyal      - Manuel sinyal tara\n"
        "/ac          - Botu ac\n"
        "/kapat       - Botu kapat\n"
        "====================\n"
        "GRUP YONETIMI:\n"
        "/kick   - At (reply yap)\n"
        "/ban    - Banla (reply yap)\n"
        "/unban  - Bani kaldir (reply yap)\n"
        "/mute [dk] - Sustur (reply yap)\n"
        "/unmute - Sesi ac (reply yap)\n"
        "/uyar [sebep] - Uyar (reply yap)\n"
        "/uyarlar - Uyari sayisi (reply yap)\n"
        "====================\n"
        "ICT: OB | FVG | Sweep | BOS | OTE"
    )

async def cmd_yardim(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)

async def cmd_durum(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    wr = stats["win"] / stats["total"] * 100 if stats["total"] > 0 else 0
    await update.message.reply_text(
        f"=== BOT DURUMU ===\n"
        f"Durum    : {'Aktif' if bot_active else 'Kapali'}\n"
        f"Seans    : {get_session()}\n"
        f"Saat     : {datetime.utcnow().strftime('%H:%M UTC')}\n"
        f"Toplam   : {stats['total']} sinyal\n"
        f"Win Rate : %{wr:.1f}\n"
        f"=================="
    )

async def cmd_fiyat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = ["=== ANLIK FİYATLAR ==="]
    for symbol, cfg in SYMBOLS.items():
        p = get_price(symbol)
        if p:
            lines.append(f"{cfg['name']:8}: {p:.4f}")
        else:
            lines.append(f"{cfg['name']:8}: Alinamadi")
    lines.append(f"Seans: {get_session()}")
    await update.message.reply_text("\n".join(lines))

async def cmd_analiz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    symbol = args[0].upper() if args else "XAUUSD"
    if symbol not in SYMBOLS:
        await update.message.reply_text(f"Gecersiz sembol. Secenekler: {', '.join(SYMBOLS.keys())}")
        return

    await update.message.reply_text(f"{symbol} analiz ediliyor...")
    df = get_candles(symbol, SYMBOLS[symbol]["interval"], 50)
    if df is None:
        await update.message.reply_text("Veri alinamadi, API key kontrol et.")
        return

    sig = analyze_ict(df)
    if sig:
        await update.message.reply_text(format_signal(symbol, sig))
    else:
        price = df["c"].iloc[-1]
        await update.message.reply_text(
            f"=== {symbol} ANALIZ ===\n"
            f"Fiyat  : {price:.4f}\n"
            f"Seans  : {get_session()}\n"
            f"Durum  : Setup yok, bekleniyor...\n"
            f"Min confluance: 2/6 gerekli"
        )

async def cmd_istatistik(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    wr = stats["win"] / stats["total"] * 100 if stats["total"] > 0 else 0
    await update.message.reply_text(
        f"=== ISTATISTIKLER ===\n"
        f"Toplam   : {stats['total']}\n"
        f"Kazanan  : {stats['win']}\n"
        f"Kaybeden : {stats['loss']}\n"
        f"Beklemede: {stats['total'] - stats['win'] - stats['loss']}\n"
        f"Win Rate : %{wr:.1f}\n"
        f"===================="
    )

async def cmd_sinyal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global last_signal_time
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Yetkin yok.")
        return
    await update.message.reply_text("Tum semboller taranıyor...")
    last_signal_time = {}  # Cooldown sifirla

    found = False
    for symbol, cfg in SYMBOLS.items():
        df  = get_candles(symbol, cfg["interval"], 50)
        sig = analyze_ict(df)
        if sig:
            msg = format_signal(symbol, sig)
            await update.message.reply_text(msg)
            stats["total"] += 1
            last_signal_time[symbol] = datetime.utcnow()
            found = True

    if not found:
        await update.message.reply_text("Simdilik setup yok, bekleniyor...")

async def cmd_ac(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global bot_active
    if not is_admin(update.effective_user.id):
        return
    bot_active = True
    await update.message.reply_text("Bot aktif! Sinyal tarama basliyor...")

async def cmd_kapat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global bot_active
    if not is_admin(update.effective_user.id):
        return
    bot_active = False
    await update.message.reply_text("Bot durduruldu. /ac ile tekrar baslatabilirsin.")

# ─── GRUP YÖNETİMİ ──────────────────────────────────────────
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

async def cmd_kick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Yetkin yok.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Kick icin birisinin mesajina reply yap.")
        return
    t = update.message.reply_to_message.from_user
    try:
        await ctx.bot.ban_chat_member(update.effective_chat.id, t.id)
        await ctx.bot.unban_chat_member(update.effective_chat.id, t.id)
        await update.message.reply_text(f"{t.first_name} atildi.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Yetkin yok.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Ban icin birisinin mesajina reply yap.")
        return
    t = update.message.reply_to_message.from_user
    try:
        await ctx.bot.ban_chat_member(update.effective_chat.id, t.id)
        await update.message.reply_text(f"{t.first_name} banlandi.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Yetkin yok.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Unban icin reply yap.")
        return
    t = update.message.reply_to_message.from_user
    try:
        await ctx.bot.unban_chat_member(update.effective_chat.id, t.id)
        await update.message.reply_text(f"{t.first_name} bani kaldirildi.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Yetkin yok.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Mute icin reply yap.")
        return
    t   = update.message.reply_to_message.from_user
    dk  = int(ctx.args[0]) if ctx.args else 10
    until = datetime.utcnow() + timedelta(minutes=dk)
    try:
        await ctx.bot.restrict_chat_member(
            update.effective_chat.id, t.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        await update.message.reply_text(f"{t.first_name} {dk} dakika susturuldu.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def cmd_unmute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Yetkin yok.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Unmute icin reply yap.")
        return
    t = update.message.reply_to_message.from_user
    try:
        await ctx.bot.restrict_chat_member(
            update.effective_chat.id, t.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        await update.message.reply_text(f"{t.first_name} sesi acildi.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def cmd_uyar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Yetkin yok.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Uyari icin reply yap.")
        return
    t      = update.message.reply_to_message.from_user
    sebep  = " ".join(ctx.args) if ctx.args else "Kural ihlali"
    warnings_db[t.id] = warnings_db.get(t.id, 0) + 1
    count  = warnings_db[t.id]
    msg    = f"{t.first_name} uyarildi! ({count}/3)\nSebep: {sebep}"
    if count >= 3:
        try:
            await ctx.bot.ban_chat_member(update.effective_chat.id, t.id)
            msg += "\n3 uyariya ulasti - BANLANDI!"
            warnings_db[t.id] = 0
        except Exception as e:
            msg += f"\nBan hatasi: {e}"
    await update.message.reply_text(msg)

async def cmd_uyarlar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply yap.")
        return
    t = update.message.reply_to_message.from_user
    await update.message.reply_text(f"{t.first_name}: {warnings_db.get(t.id, 0)}/3 uyari")

# ─── SPAM KORUMASI ──────────────────────────────────────────
async def spam_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or is_admin(update.effective_user.id):
        return
    uid = update.effective_user.id
    now = datetime.utcnow().timestamp()
    message_counts.setdefault(uid, [])
    message_counts[uid] = [t for t in message_counts[uid] if now - t < 10]
    message_counts[uid].append(now)
    if len(message_counts[uid]) > 8:
        try:
            until = datetime.utcnow() + timedelta(minutes=5)
            await ctx.bot.restrict_chat_member(
                update.effective_chat.id, uid,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until
            )
            await update.message.reply_text(
                f"{update.effective_user.first_name} spam nedeniyle 5 dk susturuldu."
            )
            message_counts[uid] = []
        except:
            pass

# ─── YENİ ÜYE KARŞILAMA ─────────────────────────────────────
async def welcome(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if not member.is_bot:
            await update.message.reply_text(
                f"Hos geldin {member.first_name}!\n"
                f"Warren Bot ICT sinyal grubuna katildin.\n"
                f"Kurallar icin adminlere danisabilirsin."
            )

# ─── MAIN ───────────────────────────────────────────────────
async def post_init(app):
    global app_ref
    app_ref = app
    asyncio.create_task(scan_loop())
    log.info("Sinyal dongusu baslatildi")

def main():
    # Keep alive server
    threading.Thread(target=start_server, daemon=True).start()

    app = Application.builder().token(TG_TOKEN).post_init(post_init).build()

    # Trading komutlari
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("yardim",     cmd_yardim))
    app.add_handler(CommandHandler("durum",      cmd_durum))
    app.add_handler(CommandHandler("fiyat",      cmd_fiyat))
    app.add_handler(CommandHandler("analiz",     cmd_analiz))
    app.add_handler(CommandHandler("istatistik", cmd_istatistik))
    app.add_handler(CommandHandler("sinyal",     cmd_sinyal))
    app.add_handler(CommandHandler("ac",         cmd_ac))
    app.add_handler(CommandHandler("kapat",      cmd_kapat))

    # Grup yonetimi
    app.add_handler(CommandHandler("kick",    cmd_kick))
    app.add_handler(CommandHandler("ban",     cmd_ban))
    app.add_handler(CommandHandler("unban",   cmd_unban))
    app.add_handler(CommandHandler("mute",    cmd_mute))
    app.add_handler(CommandHandler("unmute",  cmd_unmute))
    app.add_handler(CommandHandler("uyar",    cmd_uyar))
    app.add_handler(CommandHandler("uyarlar", cmd_uyarlar))

    # Mesaj handlerlari
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, spam_check), group=1)

    log.info("Warren Bot V4 baslatildi!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
