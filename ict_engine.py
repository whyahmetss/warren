"""
ICT Engine v2 - Professional Inner Circle Trader Analysis Module
Multi-timeframe analysis with proper MSS, FVG, OB, OTE, Liquidity Sweep detection.
"""

import numpy as np
from datetime import datetime, timedelta




# ── SWING DETECTION ──────────────────────────────────────────

def find_swing_highs(h, l, c, lookback=5):
    """Swing high noktalarini tespit et."""
    swings = []
    for i in range(lookback, len(h) - lookback):
        if h[i] == max(h[i - lookback:i + lookback + 1]):
            swings.append((i, h[i]))
    return swings

def find_swing_lows(h, l, c, lookback=5):
    """Swing low noktalarini tespit et."""
    swings = []
    for i in range(lookback, len(l) - lookback):
        if l[i] == min(l[i - lookback:i + lookback + 1]):
            swings.append((i, l[i]))
    return swings


# ── LIQUIDITY SWEEP ──────────────────────────────────────────

def detect_liquidity_sweep(h, l, c, o, lookback=20):
    """
    Son X mumda swing high/low kirip geri donen (sweep) mumlar.
    Sweep = likit alma sonrasi donus. Fake breakout degil gercek sweep.
    """
    n = len(h)
    if n < lookback + 5:
        return {"bull_sweep": False, "bear_sweep": False, "sweep_level": 0.0}

    recent_h = h[-(lookback + 1):-1]
    recent_l = l[-(lookback + 1):-1]
    swing_hi = np.max(recent_h)
    swing_lo = np.min(recent_l)

    # Son 3 mumda sweep aranir
    bull_sweep = False
    bear_sweep = False
    sweep_level = 0.0

    for i in range(-3, 0):
        if l[i] < swing_lo and c[i] > swing_lo:
            bull_sweep = True
            sweep_level = swing_lo
        if h[i] > swing_hi and c[i] < swing_hi:
            bear_sweep = True
            sweep_level = swing_hi

    return {
        "bull_sweep": bull_sweep,
        "bear_sweep": bear_sweep,
        "sweep_level": sweep_level,
        "swing_high": float(swing_hi),
        "swing_low": float(swing_lo),
    }


# ── MARKET STRUCTURE SHIFT (MSS) ─────────────────────────────

def detect_mss(h, l, c, o, lookback=10):
    """
    Sweep sonrasi ters yone structure break (MSS).
    Bullish MSS: dusus sonrasi son swing high kiriliyor
    Bearish MSS: yukselis sonrasi son swing low kiriliyor
    """
    n = len(h)
    if n < lookback + 2:
        return {"bull_mss": False, "bear_mss": False}

    # Son lookback mumda internal swing bul
    seg_h = h[-lookback:]
    seg_l = l[-lookback:]
    seg_c = c[-lookback:]

    internal_highs = []
    internal_lows = []

    for i in range(2, len(seg_h) - 2):
        if seg_h[i] > seg_h[i - 1] and seg_h[i] > seg_h[i + 1]:
            internal_highs.append((i, seg_h[i]))
        if seg_l[i] < seg_l[i - 1] and seg_l[i] < seg_l[i + 1]:
            internal_lows.append((i, seg_l[i]))

    bull_mss = False
    bear_mss = False

    # Bullish MSS: son internal high body close ile kirildi
    if internal_highs:
        last_ih = internal_highs[-1][1]
        if seg_c[-1] > last_ih:
            bull_mss = True

    # Bearish MSS: son internal low body close ile kirildi
    if internal_lows:
        last_il = internal_lows[-1][1]
        if seg_c[-1] < last_il:
            bear_mss = True

    return {"bull_mss": bull_mss, "bear_mss": bear_mss}


# ── FAIR VALUE GAP (FVG) ─────────────────────────────────────

def detect_fvg(h, l, c, o, lookback=15):
    """
    3 mumluk imbalance: Mum1 high < Mum3 low (bullish) veya Mum1 low > Mum3 high (bearish).
    En yakin ve fiyata en uygun FVG'yi dondurur.
    """
    n = len(h)
    if n < lookback:
        return {"bull_fvg": False, "bear_fvg": False, "fvg_high": 0, "fvg_low": 0}

    price = c[-1]
    best_bull = None
    best_bear = None

    start = max(0, n - lookback)
    for i in range(start, n - 2):
        # Bullish FVG: Mum3_low > Mum1_high (gap yukari)
        if l[i + 2] > h[i]:
            gap_h = float(l[i + 2])
            gap_l = float(h[i])
            if gap_l <= price <= gap_h * 1.005:
                if best_bull is None or abs(price - (gap_h + gap_l) / 2) < abs(price - (best_bull[0] + best_bull[1]) / 2):
                    best_bull = (gap_h, gap_l)

        # Bearish FVG: Mum3_high < Mum1_low (gap asagi)
        if h[i + 2] < l[i]:
            gap_h = float(l[i])
            gap_l = float(h[i + 2])
            if gap_l * 0.995 <= price <= gap_h:
                if best_bear is None or abs(price - (gap_h + gap_l) / 2) < abs(price - (best_bear[0] + best_bear[1]) / 2):
                    best_bear = (gap_h, gap_l)

    result = {"bull_fvg": False, "bear_fvg": False, "fvg_high": 0.0, "fvg_low": 0.0}
    if best_bull:
        result["bull_fvg"] = True
        result["fvg_high"] = best_bull[0]
        result["fvg_low"] = best_bull[1]
    if best_bear:
        result["bear_fvg"] = True
        result["fvg_high"] = best_bear[0]
        result["fvg_low"] = best_bear[1]
    return result


# ── ORDER BLOCK ──────────────────────────────────────────────

def detect_order_block(h, l, c, o, lookback=20):
    """
    Kurumsal mum bolgesi:
    Bullish OB: Dusus sonrasi son bearish mum (pivot oncesi) - fiyat bu bolgede
    Bearish OB: Yukselis sonrasi son bullish mum (pivot oncesi) - fiyat bu bolgede
    """
    n = len(h)
    if n < lookback:
        return {"bull_ob": False, "bear_ob": False,
                "ob_high": 0.0, "ob_low": 0.0}

    price = c[-1]
    bull_ob = False
    bear_ob = False
    ob_high = 0.0
    ob_low = 0.0

    for i in range(n - 3, max(n - lookback, 2), -1):
        # Bullish OB: bearish mum sonrasi impulsive yukselis
        if c[i] < o[i] and not bull_ob:
            future_high = max(h[i + 1:min(i + 6, n)])
            if future_high > h[i]:
                b_h = max(o[i], c[i])
                b_l = min(o[i], c[i])
                tolerance = (b_h - b_l) * 0.5
                if b_l - tolerance <= price <= b_h + tolerance:
                    bull_ob = True
                    ob_high = float(b_h)
                    ob_low = float(b_l)

        # Bearish OB: bullish mum sonrasi impulsive dusus
        if c[i] > o[i] and not bear_ob:
            future_low = min(l[i + 1:min(i + 6, n)])
            if future_low < l[i]:
                b_h = max(o[i], c[i])
                b_l = min(o[i], c[i])
                tolerance = (b_h - b_l) * 0.5
                if b_l - tolerance <= price <= b_h + tolerance:
                    bear_ob = True
                    ob_high = float(b_h)
                    ob_low = float(b_l)

        if bull_ob and bear_ob:
            break

    return {"bull_ob": bull_ob, "bear_ob": bear_ob,
            "ob_high": ob_high, "ob_low": ob_low}


# ── OTE ZONE ─────────────────────────────────────────────────

def detect_ote(h, l, c, lookback=20):
    """Fibonacci 0.62 – 0.79 arasi optimal entry bolgesi."""
    n = len(h)
    if n < lookback:
        return {"in_ote": False, "ote_high": 0.0, "ote_low": 0.0}

    price = c[-1]
    move_high = float(np.max(h[-lookback:]))
    move_low = float(np.min(l[-lookback:]))

    if move_high == move_low:
        return {"in_ote": False, "ote_high": 0.0, "ote_low": 0.0}

    ote_high = move_high - (move_high - move_low) * 0.62
    ote_low = move_high - (move_high - move_low) * 0.79
    in_ote = ote_low <= price <= ote_high

    return {"in_ote": in_ote, "ote_high": float(ote_high), "ote_low": float(ote_low)}


# ── HTF BIAS ─────────────────────────────────────────────────

def detect_htf_bias(df_htf):
    """
    HTF (15M/1H) trend yonu.
    Higher highs & higher lows = Bullish
    Lower highs & lower lows = Bearish
    """
    if df_htf is None or len(df_htf) < 10:
        return 0  # Nötr

    h = df_htf["h"].values
    l = df_htf["l"].values
    c = df_htf["c"].values

    # Son 10 mumda trend
    mid = len(c) // 2
    first_half_avg = np.mean(c[:mid])
    second_half_avg = np.mean(c[mid:])

    # Swing yapisi kontrolu
    recent_highs = []
    recent_lows = []
    for i in range(2, len(h) - 2):
        if h[i] > h[i - 1] and h[i] > h[i + 1]:
            recent_highs.append(h[i])
        if l[i] < l[i - 1] and l[i] < l[i + 1]:
            recent_lows.append(l[i])

    hh = False  # Higher highs
    hl = False  # Higher lows
    lh = False  # Lower highs
    ll = False  # Lower lows

    if len(recent_highs) >= 2:
        hh = recent_highs[-1] > recent_highs[-2]
        lh = recent_highs[-1] < recent_highs[-2]
    if len(recent_lows) >= 2:
        hl = recent_lows[-1] > recent_lows[-2]
        ll = recent_lows[-1] < recent_lows[-2]

    if hh and hl:
        return 1   # Bullish
    elif lh and ll:
        return -1  # Bearish
    elif second_half_avg > first_half_avg * 1.001:
        return 1
    elif second_half_avg < first_half_avg * 0.999:
        return -1
    return 0


# ── FAKE BREAKOUT FILTER ─────────────────────────────────────

def is_fake_breakout(h, l, c, o, lookback=10):
    """
    Son mumlarda breakout sonrasi hizli geri donus = fake breakout.
    True ise sinyal filtrelenmeli.
    """
    n = len(h)
    if n < lookback + 3:
        return False

    recent_range = np.max(h[-lookback:]) - np.min(l[-lookback:])
    if recent_range == 0:
        return False

    last3_range = np.max(h[-3:]) - np.min(l[-3:])

    # Son 3 mumda genis range + kotu kapanis = fake breakout
    for i in range(-3, 0):
        body = abs(c[i] - o[i])
        wick_upper = h[i] - max(c[i], o[i])
        wick_lower = min(c[i], o[i]) - l[i]
        total_wick = wick_upper + wick_lower

        if body > 0 and total_wick > body * 3:
            return True

    return False


# ── VOLATILITY FILTER ────────────────────────────────────────

def check_volatility(h, l, lookback=20, min_atr_mult=0.5, max_atr_mult=4.0):
    """
    ATR bazli volatilite filtresi.
    Cok dusuk veya cok yuksek volatilitede trade yapma.
    """
    if len(h) < lookback + 1:
        return {"ok": True, "atr": 0, "avg_atr": 0}

    trs = []
    for i in range(-lookback, 0):
        tr = max(h[i] - l[i], abs(h[i] - (h[i - 1] + l[i - 1]) / 2))
        trs.append(tr)

    atr = np.mean(trs)
    last_tr = h[-1] - l[-1]

    ok = min_atr_mult * atr <= last_tr <= max_atr_mult * atr
    return {"ok": ok, "atr": float(atr), "last_tr": float(last_tr)}


# ── ANA ANALİZ FONKSİYONU ────────────────────────────────────

def analyze_ict_v2(df_ltf, df_htf=None, min_rr=2.5, min_confluence=4):
    """
    Profesyonel ICT analiz. Multi-timeframe.

    Args:
        df_ltf: LTF dataframe (1M veya 5M) - entry icin
        df_htf: HTF dataframe (1H veya 4H) - bias icin
        min_rr: Minimum Risk:Reward orani
        min_confluence: Minimum confluence puani (0-6)

    Returns:
        dict veya None
    """
    if df_ltf is None or len(df_ltf) < 30:
        return None

    h = df_ltf["h"].values.astype(float)
    l = df_ltf["l"].values.astype(float)
    o = df_ltf["o"].values.astype(float)
    c = df_ltf["c"].values.astype(float)
    price = float(c[-1])


    # 2. Volatilite kontrolu
    vol = check_volatility(h, l)
    if not vol["ok"]:
        return None

    # 3. Fake breakout filtresi
    if is_fake_breakout(h, l, c, o):
        return None

    # 4. HTF Bias
    htf_bias = detect_htf_bias(df_htf)  # 1, -1, 0

    # 5. ICT konseptleri
    sweep = detect_liquidity_sweep(h, l, c, o)
    mss = detect_mss(h, l, c, o)
    fvg = detect_fvg(h, l, c, o)
    ob = detect_order_block(h, l, c, o)
    ote = detect_ote(h, l, c)

    # 6. Confluence hesapla - her yon icin ayri
    bull_checks = {
        "Liquidity Sweep": sweep["bull_sweep"],
        "MSS": mss["bull_mss"],
        "FVG": fvg["bull_fvg"],
        "Order Block": ob["bull_ob"],
        "OTE": ote["in_ote"],
        "HTF Bias": htf_bias >= 1,
    }

    bear_checks = {
        "Liquidity Sweep": sweep["bear_sweep"],
        "MSS": mss["bear_mss"],
        "FVG": fvg["bear_fvg"],
        "Order Block": ob["bear_ob"],
        "OTE": ote["in_ote"],
        "HTF Bias": htf_bias <= -1,
    }

    bull_conf = sum(1 for v in bull_checks.values() if v)
    bear_conf = sum(1 for v in bear_checks.values() if v)

    # 7. Yon secimi - sadece HTF yonunde ve minimum confluence
    direction = None
    checks = {}
    conf = 0

    if bull_conf >= bear_conf and bull_conf >= min_confluence and htf_bias >= 0:
        direction = "LONG"
        checks = bull_checks
        conf = bull_conf
    elif bear_conf > bull_conf and bear_conf >= min_confluence and htf_bias <= 0:
        direction = "SHORT"
        checks = bear_checks
        conf = bear_conf
    else:
        return None

    # 8. SL & TP hesapla
    swing_high = sweep["swing_high"]
    swing_low = sweep["swing_low"]
    atr = vol["atr"]

    if direction == "LONG":
        sl = swing_low - atr * 0.3
        tp_distance = (price - sl) * min_rr
        tp = price + tp_distance
    else:
        sl = swing_high + atr * 0.3
        tp_distance = (sl - price) * min_rr
        tp = price - tp_distance

    sl_pips = abs(price - sl)
    tp_pips = abs(tp - price)

    if sl_pips == 0:
        return None

    rr = tp_pips / sl_pips

    if rr < min_rr:
        return None

    # 9. Kalite puani
    if conf >= 5:
        strength = "HIGH"
    elif conf >= 4:
        strength = "MEDIUM"
    else:
        strength = "LOW"

    return {
        "direction": direction,
        "price": price,
        "sl": float(sl),
        "tp": float(tp),
        "sl_pips": float(sl_pips),
        "tp_pips": float(tp_pips),
        "rr": float(rr),
        "conf": conf,
        "checks": checks,
        "strength": strength,
        "session": session,
        "atr": float(atr),
        "htf_bias": htf_bias,
    }
