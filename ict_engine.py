"""
ICT Engine v3 - Sikilestirilmis Inner Circle Trader Analiz Modulu

v2'ye gore onemli degisiklikler:
- Sweep + MSS ZORUNLU (core ICT kurali)
- OTE: sweep swing'den gercek fib retracement
- OB: body tolerance %10 (v2'de %50 cok gevskti)
- FVG: min ATR*0.2 genisligi gerekli
- MSS: sweep sonrasi body close teyidi
- Ranging/spike/doji filtresi eklendi
- Macro time tespiti eklendi
"""

import numpy as np
from datetime import datetime, timedelta

# ── SESSION / KILL ZONE ──────────────────────────────────────

KILL_ZONES = {
    "london":    {"start": 7,  "end": 10, "name": "London Kill Zone"},
    "ny_open":   {"start": 12, "end": 16, "name": "New York Kill Zone"},
    "ny_silver": {"start": 16, "end": 17, "name": "NY Silver Bullet"},
}

def get_active_session():
    h = datetime.utcnow().hour
    for key, kz in KILL_ZONES.items():
        if kz["start"] <= h < kz["end"]:
            return kz["name"]
    return None

def is_in_kill_zone():
    return get_active_session() is not None


# ── ATR ──────────────────────────────────────────────────────

def calc_atr(h, l, lookback=14):
    if len(h) < lookback + 1:
        return float(np.mean(h[-5:] - l[-5:])) if len(h) >= 5 else 0.0
    trs = []
    for i in range(1, lookback + 1):
        tr = max(h[-i] - l[-i],
                 abs(h[-i] - l[-i-1]),
                 abs(l[-i] - h[-i-1]))
        trs.append(tr)
    return float(np.mean(trs))


# ── SWING DETECTION ──────────────────────────────────────────

def find_swings(h, l, lookback=5):
    highs, lows = [], []
    n = len(h)
    for i in range(lookback, n - lookback):
        if h[i] == max(h[i-lookback:i+lookback+1]):
            highs.append((i, float(h[i])))
        if l[i] == min(l[i-lookback:i+lookback+1]):
            lows.append((i, float(l[i])))
    return highs, lows


# ── LIQUIDITY SWEEP ──────────────────────────────────────────

def detect_liquidity_sweep(h, l, c, o, lookback=30, atr=None):
    """
    Gercek ICT sweep: wick gecer, body geri kalir.
    Min sweep genisligi: ATR * 0.3 (noise filtresi).
    """
    n = len(h)
    if n < lookback + 5:
        sh = float(np.max(h[-min(lookback,n):]))
        sl = float(np.min(l[-min(lookback,n):]))
        return {"bull_sweep": False, "bear_sweep": False,
                "sweep_level": 0.0, "swing_high": sh, "swing_low": sl}

    seg_h    = h[-(lookback+1):-1]
    seg_l    = l[-(lookback+1):-1]
    swing_hi = float(np.max(seg_h))
    swing_lo = float(np.min(seg_l))
    min_sw   = (atr * 0.3) if atr else 0.0

    bull_sweep = bear_sweep = False
    sweep_level = 0.0

    for i in range(-2, 0):
        # Bullish sweep: wick swing_lo altina indi, body yukarda kaldi
        if (l[i] < swing_lo and
                min(c[i], o[i]) >= swing_lo and
                (swing_lo - l[i]) >= min_sw):
            bull_sweep  = True
            sweep_level = swing_lo

        # Bearish sweep: wick swing_hi ustune cikti, body asagida kaldi
        if (h[i] > swing_hi and
                max(c[i], o[i]) <= swing_hi and
                (h[i] - swing_hi) >= min_sw):
            bear_sweep  = True
            sweep_level = swing_hi

    return {
        "bull_sweep":  bull_sweep,
        "bear_sweep":  bear_sweep,
        "sweep_level": sweep_level,
        "swing_high":  swing_hi,
        "swing_low":   swing_lo,
    }


# ── MARKET STRUCTURE SHIFT (MSS) ─────────────────────────────

def detect_mss(h, l, c, o, sweep, lookback=15):
    """
    Sweep SONRASI yapısal kirilma.
    Body close (hem open hem close) ile teyit edilmeli.
    """
    n = len(h)
    if n < lookback + 3:
        return {"bull_mss": False, "bear_mss": False, "mss_level": 0.0}

    seg_h = h[-lookback:]
    seg_l = l[-lookback:]
    seg_c = c[-lookback:]

    int_highs, int_lows = [], []
    for i in range(2, len(seg_h) - 2):
        if seg_h[i] > seg_h[i-1] and seg_h[i] > seg_h[i+1]:
            int_highs.append(float(seg_h[i]))
        if seg_l[i] < seg_l[i-1] and seg_l[i] < seg_l[i+1]:
            int_lows.append(float(seg_l[i]))

    bull_mss = bear_mss = False
    mss_level = 0.0

    if sweep.get("bull_sweep") and int_highs:
        last_ih = int_highs[-1]
        if min(c[-1], o[-1]) > last_ih:   # body close teyidi
            bull_mss  = True
            mss_level = last_ih

    if sweep.get("bear_sweep") and int_lows:
        last_il = int_lows[-1]
        if max(c[-1], o[-1]) < last_il:   # body close teyidi
            bear_mss  = True
            mss_level = last_il

    return {"bull_mss": bull_mss, "bear_mss": bear_mss, "mss_level": mss_level}


# ── FAIR VALUE GAP ───────────────────────────────────────────

def detect_fvg(h, l, c, o, atr=None, lookback=20):
    """
    3 mumluk imbalance. Min genislik: ATR*0.2. En taze FVG oncelikli.
    """
    n = len(h)
    if n < 5:
        return {"bull_fvg": False, "bear_fvg": False,
                "fvg_high": 0.0, "fvg_low": 0.0, "fvg_age": 99}

    price    = float(c[-1])
    min_size = (atr * 0.2) if atr else 0.0
    tol      = price * 0.003

    best_bull = best_bear = None
    start = max(0, n - lookback)

    for i in range(start, n - 2):
        age = (n - 1) - i

        # Bullish FVG
        gl = float(h[i]); gh = float(l[i+2])
        if gh > gl and (gh - gl) >= min_size:
            if (gl - tol) <= price <= (gh + tol):
                if best_bull is None or age < best_bull[0]:
                    best_bull = (age, gh, gl)

        # Bearish FVG
        gh2 = float(l[i]); gl2 = float(h[i+2])
        if gh2 > gl2 and (gh2 - gl2) >= min_size:
            if (gl2 - tol) <= price <= (gh2 + tol):
                if best_bear is None or age < best_bear[0]:
                    best_bear = (age, gh2, gl2)

    result = {"bull_fvg": False, "bear_fvg": False,
              "fvg_high": 0.0, "fvg_low": 0.0, "fvg_age": 99}

    if best_bull:
        result.update({"bull_fvg": True, "fvg_high": best_bull[1],
                        "fvg_low": best_bull[2], "fvg_age": best_bull[0]})
    elif best_bear:
        result.update({"bear_fvg": True, "fvg_high": best_bear[1],
                        "fvg_low": best_bear[2], "fvg_age": best_bear[0]})

    return result


# ── ORDER BLOCK ──────────────────────────────────────────────

def detect_order_block(h, l, c, o, atr=None, lookback=25):
    """
    Fresh OB: impulse sonrasi ilk kez geri donen fiyat.
    Body tolerance: %10 (v2 %50'den dusuruldu).
    Min impulse: 2x ATR.
    """
    n = len(h)
    if n < lookback:
        return {"bull_ob": False, "bear_ob": False,
                "ob_high": 0.0, "ob_low": 0.0, "ob_age": 99}

    price       = float(c[-1])
    min_impulse = (atr * 2.0) if atr else 0.0
    tol_mult    = 0.10

    bull_ob = bear_ob = False
    ob_high = ob_low = 0.0
    ob_age  = 99

    for i in range(n-3, max(n-lookback, 2), -1):
        # Bullish OB
        if c[i] < o[i] and not bull_ob:
            bh = float(max(o[i], c[i]))
            bl = float(min(o[i], c[i]))
            bd = bh - bl
            if bd == 0: continue
            tol = bd * tol_mult
            fs  = h[i+1:min(i+6, n)]
            if len(fs) == 0: continue
            impulse = float(np.max(fs)) - bh
            if impulse >= min_impulse and (bl - tol) <= price <= (bh + tol):
                bull_ob = True; ob_high = bh; ob_low = bl
                ob_age  = (n-1) - i

        # Bearish OB
        if c[i] > o[i] and not bear_ob:
            bh = float(max(o[i], c[i]))
            bl = float(min(o[i], c[i]))
            bd = bh - bl
            if bd == 0: continue
            tol = bd * tol_mult
            fs  = l[i+1:min(i+6, n)]
            if len(fs) == 0: continue
            impulse = bl - float(np.min(fs))
            if impulse >= min_impulse and (bl - tol) <= price <= (bh + tol):
                bear_ob = True; ob_high = bh; ob_low = bl
                ob_age  = (n-1) - i

        if bull_ob and bear_ob: break

    return {"bull_ob": bull_ob, "bear_ob": bear_ob,
            "ob_high": ob_high, "ob_low": ob_low, "ob_age": ob_age}


# ── OTE ZONE ─────────────────────────────────────────────────

def detect_ote(h, l, c, swing_high, swing_low):
    """
    Fib 0.62-0.79 OTE bolgesi. Sweep swing'lerini kullanir (daha gercekci).
    """
    price = float(c[-1])
    rng   = swing_high - swing_low

    if rng <= 0:
        return {"in_ote": False, "in_bull_ote": False, "in_bear_ote": False,
                "ote_high": 0.0, "ote_low": 0.0,
                "bear_ote_high": 0.0, "bear_ote_low": 0.0, "fib_level": 0.0}

    b_ote_h = swing_high - rng * 0.62
    b_ote_l = swing_high - rng * 0.79
    s_ote_l = swing_low  + rng * 0.62
    s_ote_h = swing_low  + rng * 0.79

    in_bull = b_ote_l <= price <= b_ote_h
    in_bear = s_ote_l <= price <= s_ote_h
    fib     = round((swing_high - price) / rng, 2) if rng > 0 else 0.0

    return {
        "in_ote":        in_bull or in_bear,
        "in_bull_ote":   in_bull,
        "in_bear_ote":   in_bear,
        "ote_high":      float(b_ote_h),
        "ote_low":       float(b_ote_l),
        "bear_ote_high": float(s_ote_h),
        "bear_ote_low":  float(s_ote_l),
        "fib_level":     fib,
    }


# ── HTF BIAS ─────────────────────────────────────────────────

def detect_htf_bias(df_htf):
    if df_htf is None or len(df_htf) < 15:
        return 0
    h = df_htf["h"].values.astype(float)
    l = df_htf["l"].values.astype(float)
    c = df_htf["c"].values.astype(float)

    highs, lows = find_swings(h, l, lookback=3)

    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1]  > lows[-2][1]
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1]  < lows[-2][1]
        if hh and hl: return  1
        if lh and ll: return -1
        return 0

    if len(c) >= 10:
        if np.mean(c[-5:]) > np.mean(c[:5]) * 1.002: return  1
        if np.mean(c[-5:]) < np.mean(c[:5]) * 0.998: return -1
    return 0


# ── MACRO TIME ───────────────────────────────────────────────

def is_macro_time():
    """ICT macro windows (UTC) plusminus 5 dakika."""
    now    = datetime.utcnow()
    mins   = now.hour * 60 + now.minute
    macros = [2*60+33, 4*60+3, 8*60+50, 9*60+50, 10*60+50, 11*60+50]
    return any(abs(mins - m) <= 5 for m in macros)


# ── PIYASA KOSUlLARI ─────────────────────────────────────────

def check_market_conditions(h, l, c, o, lookback=20):
    """
    ATR hesapla + 3 filtre:
    1. Doji/pin bar: body/range < 0.10
    2. Spike/news mumu: range > 4x ATR
    3. Ranging: son 10 mum range < 0.8x ATR
    """
    atr = calc_atr(h, l, lookback)
    if atr == 0:
        return {"ok": True, "atr": 0.0, "reason": ""}

    last_range = h[-1] - l[-1]
    last_body  = abs(c[-1] - o[-1])

    if last_range > 0 and (last_body / last_range) < 0.10:
        return {"ok": False, "atr": atr, "reason": "doji/pin bar"}

    if last_range > atr * 4.0:
        return {"ok": False, "atr": atr, "reason": "spike/news mumu"}

    if float(np.max(h[-10:]) - np.min(l[-10:])) < atr * 0.8:
        return {"ok": False, "atr": atr, "reason": "ranging/flat piyasa"}

    return {"ok": True, "atr": atr, "reason": ""}


# ── ANA ANALİZ ───────────────────────────────────────────────

def analyze_ict_v2(df_ltf, df_htf=None, min_rr=2.5, min_confluence=4, require_core=False, debug=False):
    """
    ZORUNLU:
      1. Kill Zone aktif

    Opsiyonel katı filtre (require_core=True):
      2. Liquidity Sweep
      3. MSS (sweep sonrasi yapısal kirilma)

    CONFLUENCE skorlama (min 4/6):
      Sweep / MSS / FVG / OB / OTE / HTF Bias

    debug=True ise (signal, reason) döner.
    """
    def _ret(signal, reason):
        if debug:
            return signal, reason
        return signal

    if df_ltf is None or len(df_ltf) < 35:
        return _ret(None, "ltf_data_short")

    h = df_ltf["h"].values.astype(float)
    l = df_ltf["l"].values.astype(float)
    o = df_ltf["o"].values.astype(float)
    c = df_ltf["c"].values.astype(float)
    price = float(c[-1])

    session = get_active_session()
    if session is None:
        return _ret(None, "outside_killzone")

    mkt = check_market_conditions(h, l, c, o)
    if not mkt["ok"]:
        reason = mkt.get("reason", "market_filter") or "market_filter"
        return _ret(None, f"market_{reason.replace('/', '_').replace(' ', '_')}")
    atr = mkt["atr"]

    htf_bias = detect_htf_bias(df_htf)

    sweep = detect_liquidity_sweep(h, l, c, o, atr=atr)
    mss = detect_mss(h, l, c, o, sweep)
    if require_core and not sweep["bull_sweep"] and not sweep["bear_sweep"]:
        return _ret(None, "core_sweep_missing")
    if require_core and not mss["bull_mss"] and not mss["bear_mss"]:
        return _ret(None, "core_mss_missing")

    fvg = detect_fvg(h, l, c, o, atr=atr)
    ob  = detect_order_block(h, l, c, o, atr=atr)
    ote = detect_ote(h, l, c,
                     swing_high=sweep["swing_high"],
                     swing_low=sweep["swing_low"])

    bull_checks = {
        "Liquidity Sweep": sweep["bull_sweep"],
        "MSS / BOS":       mss["bull_mss"],
        "FVG":             fvg["bull_fvg"],
        "Order Block":     ob["bull_ob"],
        "OTE (0.62-0.79)": ote["in_bull_ote"],
        "HTF Bias":        htf_bias >= 1,
    }

    bear_checks = {
        "Liquidity Sweep": sweep["bear_sweep"],
        "MSS / BOS":       mss["bear_mss"],
        "FVG":             fvg["bear_fvg"],
        "Order Block":     ob["bear_ob"],
        "OTE (0.62-0.79)": ote["in_bear_ote"],
        "HTF Bias":        htf_bias <= -1,
    }

    bull_conf = sum(1 for v in bull_checks.values() if v)
    bear_conf = sum(1 for v in bear_checks.values() if v)

    direction = None
    checks    = {}
    conf      = 0

    if (bull_conf >= min_confluence
            and bull_conf >= bear_conf and htf_bias >= 0):
        direction = "LONG";  checks = bull_checks; conf = bull_conf

    elif (bear_conf >= min_confluence
            and bear_conf > bull_conf and htf_bias <= 0):
        direction = "SHORT"; checks = bear_checks; conf = bear_conf

    if direction is None:
        if bull_conf < min_confluence and bear_conf < min_confluence:
            return _ret(None, "low_confluence")
        if htf_bias == 0:
            return _ret(None, "neutral_htf_bias")
        return _ret(None, "direction_not_selected")

    # SL/TP — sweep seviyesi bazli
    sl_buffer = atr * 0.5
    if direction == "LONG":
        sl  = sweep["swing_low"]  - sl_buffer
        tp  = price + (price - sl) * min_rr
    else:
        sl  = sweep["swing_high"] + sl_buffer
        tp  = price - (sl - price) * min_rr

    sl_dist = abs(price - sl)
    tp_dist = abs(tp - price)

    if sl_dist == 0:
        return _ret(None, "sl_distance_zero")
    if (tp_dist / sl_dist) < min_rr:
        return _ret(None, "rr_below_threshold")

    rr = tp_dist / sl_dist

    if   conf == 6: strength = "HIGH"
    elif conf >= 5: strength = "HIGH"
    elif conf == 4: strength = "MEDIUM"
    else:           strength = "LOW"

    return _ret({
        "direction":   direction,
        "price":       price,
        "sl":          float(sl),
        "tp":          float(tp),
        "sl_pips":     float(sl_dist),
        "tp_pips":     float(tp_dist),
        "rr":          float(rr),
        "conf":        conf,
        "checks":      checks,
        "strength":    strength,
        "session":     session,
        "atr":         float(atr),
        "htf_bias":    htf_bias,
        "macro_time":  is_macro_time(),
        "fib_level":   ote["fib_level"],
        "sweep_level": sweep["sweep_level"],
        "ob_age":      ob["ob_age"],
        "fvg_age":     fvg["fvg_age"],
    }, "ok")

