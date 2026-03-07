"""
Warren Bot V4 - Full Python ICT Trading & Grup Yonetim Botu
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

TG_TOKEN   = os.environ.get("TG_TOKEN",   "8698295551:AAFLixj0p8t7REyHcIkXnSp0gChNf6bNk6w")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "-1003838635441")
TD_API_KEY = os.environ.get("TD_API_KEY", "YOUR_TWELVEDATA_KEY")
ADMIN_IDS  = [6663913960]

SYMBOLS = {
    "XAUUSD": {"name": "Gold",  "interval": "1min"},
    "US100":  {"name": "US100", "interval": "1min"},
}

COOLDOWN_MIN    = 30
MIN_RR          = 2.0
OB_LOOKBACK     = 20
SIGNAL_INTERVAL = 60

stats           = {"total": 0, "win": 0, "loss": 0}
warnings_db     = {}
message_counts  = {}
last_signal_time= {}
bot_active      = True

# ── Keep Alive ──────────────────────────────────────────────
class KeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Warren Bot V4 caliyor!")
    def log_message(self, *a): pass

def start_server():
    HTTPServer(("0.0.0.0", 8080), KeepAlive).serve_forever()

# ── Twelve Data ─────────────────────────────────────────────
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

# ── ICT Analiz ──────────────────────────────────────────────
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

# ── Sinyal Dongusu ───────────────────────────────────────────
async def scan_loop(app):
    log.info("Sinyal tarama dongusu basladi")
    while True:
        await asyncio.sleep(SIGNAL_INTERVAL)
        if not bot_active: continue
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
                    stats["total"] += 1
                    log.info(f"Sinyal: {symbol} {sig['direction']}")
            except Exception as e:
                log.error(f"Scan hatasi {symbol}: {e}")

# ── Komutlar ─────────────────────────────────────────────────
def is_admin(uid): return uid in ADMIN_IDS

async def cmd_start(update, ctx):
    await update.message.reply_text(
        "Warren Bot V4 Aktif!\n====================\n"
        "/durum /fiyat /analiz /sinyal /istatistik\n"
        "/ac /kapat\n====================\n"
        "GRUP: /kick /ban /unban /mute /unmute /uyar /uyarlar"
    )

async def cmd_yardim(update, ctx): await cmd_start(update, ctx)

async def cmd_durum(update, ctx):
    wr = stats["win"] / stats["total"] * 100 if stats["total"] else 0
    await update.message.reply_text(
        f"Durum : {'Aktif' if bot_active else 'Kapali'}\n"
        f"Seans : {get_session()}\nSaat  : {datetime.utcnow().strftime('%H:%M UTC')}\n"
        f"Sinyal: {stats['total']}  WR: %{wr:.1f}"
    )

async def cmd_fiyat(update, ctx):
    lines = ["=== FIYATLAR ==="]
    for symbol, cfg in SYMBOLS.items():
        p = get_price(symbol)
        lines.append(f"{cfg['name']:8}: {p:.4f}" if p else f"{cfg['name']:8}: Alinamadi")
    await update.message.reply_text("\n".join(lines))

async def cmd_analiz(update, ctx):
    symbol = (ctx.args[0].upper() if ctx.args else "XAUUSD")
    if symbol not in SYMBOLS:
        await update.message.reply_text(f"Gecersiz. Secenekler: {', '.join(SYMBOLS)}"); return
    await update.message.reply_text(f"{symbol} analiz ediliyor...")
    df = get_candles(symbol, SYMBOLS[symbol]["interval"], 50)
    if df is None:
        await update.message.reply_text("Veri alinamadi, API key kontrol et."); return
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
    await update.message.reply_text("Taranıyor...")
    last_signal_time = {}; found = False
    for symbol, cfg in SYMBOLS.items():
        df = get_candles(symbol, cfg["interval"], 50); sig = analyze_ict(df)
        if sig:
            await update.message.reply_text(format_signal(symbol, sig))
            stats["total"] += 1; last_signal_time[symbol] = datetime.utcnow(); found = True
    if not found: await update.message.reply_text("Setup yok, bekleniyor...")

async def cmd_ac(update, ctx):
    global bot_active
    if not is_admin(update.effective_user.id): return
    bot_active = True; await update.message.reply_text("Bot aktif!")

async def cmd_kapat(update, ctx):
    global bot_active
    if not is_admin(update.effective_user.id): return
    bot_active = False; await update.message.reply_text("Bot durduruldu. /ac ile baslatabilirsin.")

async def cmd_kick(update, ctx):
    if not is_admin(update.effective_user.id): return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply yap."); return
    t = update.message.reply_to_message.from_user
    try:
        await ctx.bot.ban_chat_member(update.effective_chat.id, t.id)
        await ctx.bot.unban_chat_member(update.effective_chat.id, t.id)
        await update.message.reply_text(f"{t.first_name} atildi.")
    except Exception as e: await update.message.reply_text(f"Hata: {e}")

async def cmd_ban(update, ctx):
    if not is_admin(update.effective_user.id): return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply yap."); return
    t = update.message.reply_to_message.from_user
    try:
        await ctx.bot.ban_chat_member(update.effective_chat.id, t.id)
        await update.message.reply_text(f"{t.first_name} banlandi.")
    except Exception as e: await update.message.reply_text(f"Hata: {e}")

async def cmd_unban(update, ctx):
    if not is_admin(update.effective_user.id): return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply yap."); return
    t = update.message.reply_to_message.from_user
    try:
        await ctx.bot.unban_chat_member(update.effective_chat.id, t.id)
        await update.message.reply_text(f"{t.first_name} bani kaldirildi.")
    except Exception as e: await update.message.reply_text(f"Hata: {e}")

async def cmd_mute(update, ctx):
    if not is_admin(update.effective_user.id): return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply yap."); return
    t = update.message.reply_to_message.from_user
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
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply yap."); return
    t = update.message.reply_to_message.from_user
    try:
        await ctx.bot.restrict_chat_ember(
            update.effective_chat.id, t.id,
            permissions=ChatPermissions(can_send_messages=True, can_send_media_messages=True,
                can_send_other_messages=True, can_add_web_page_previews=True)
        )
        await update.message.reply_text(f"{t.first_name} sesi acildi.")
    except Exception as e: await update.message.reply_text(f"Hata: {e}")

async def cmd_uyar(update, ctx):
    if not is_admin(update.effective_user.id): return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply yap."); return
    t = update.message.reply_to_message.from_user
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
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply yap."); return
    t = update.message.reply_to_message.from_user
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

async def welcome(update, ctx):
    for m in update.message.new_chat_members:
        if not m.is_bot:
            await update.message.reply_text(f"Hos geldin {m.first_name}! ICT sinyal grubuna katildin.")

# ── Main ─────────────────────────────────────────────────────
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
    app.add_handler(CommandHandler("ac",         cmd_ac))
    app.add_handler(CommandHandler("kapat",      cmd_kapat))
    app.add_handler(CommandHandler("kick",       cmd_kick))
    app.add_handler(CommandHandler("ban",        cmd_ban))
    app.add_handler(CommandHandler("unban",      cmd_unban))
    app.add_handler(CommandHandler("mute",       cmd_mute))
    app.add_handler(CommandHandler("unmute",     cmd_unmute))
    app.add_handler(CommandHandler("uyar",       cmd_uyar))
    app.add_handler(CommandHandler("uyarlar",    cmd_uyarlar))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, spam_check), group=1)

    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        log.info("Warren Bot V4 baslatildi!")
        await scan_loop(app)

if __name__ == "__main__":
    asyncio.run(main())
