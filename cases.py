"""
Inspector's case files: alternative strategies, written once and used two ways.

  * inspector.py replays them on months of history (backtest).
  * vault_fx.py paper-trades them live every run (the proving ground), and can trade
    one for real (on your practice account) after you /approve it.

Every case looks at the same candle lists:
    m15[j]   the 15-minute candle that just closed (dicts with t, ts, o, h, l, c)
    h1[:n1]  completed 1-hour candles at that moment
    d1[:nd]  completed daily candles at that moment
and returns None or a trade plan:
    {"dir": +1/-1, "risk": stop distance (price), "target_r": R multiple or None,
     "trail": bool (trailing stop at the stop distance), "max_hours": hold limit, "why": text}
"""

import datetime as dt
import statistics

UTC = dt.timezone.utc


def _atr(c, end, n=14):
    """Simple average true range of the n candles ending at index end (inclusive)."""
    if end < n:
        return None
    trs = [max(c[k]["h"] - c[k]["l"], abs(c[k]["h"] - c[k - 1]["c"]), abs(c[k]["l"] - c[k - 1]["c"]))
           for k in range(end - n + 1, end + 1)]
    return sum(trs) / n


def _rsi(c, end, n=14):
    if end < n:
        return 50.0
    g = l_ = 0.0
    for k in range(end - n + 1, end + 1):
        d = c[k]["c"] - c[k - 1]["c"]
        g += max(d, 0); l_ += max(-d, 0)
    return 100.0 if l_ == 0 else 100 - 100 / (1 + g / l_)


def _ema_last(xs, n):
    k, e = 2 / (n + 1), None
    for x in xs:
        e = x if e is None else x * k + e * (1 - k)
    return e


def _hour(c):
    return (c["ts"] // 3600) % 24


def _closes_at(c):
    return c["ts"] + 900               # a 15-min candle's close time


# ───────────────────────────── the cases ─────────────────────────────────────

def mean_reversion(pair, m15, j, h1, n1, d1, nd):
    """Quiet-hours snap-back: price stretches outside its 2-standard-deviation band and RSI is extreme, so bet it returns to the average."""
    c = m15[j]
    if not (0 <= _hour(c) < 6) or j < 25 or pair == "XAU_USD":
        return None
    closes = [x["c"] for x in m15[j - 19:j + 1]]
    mid, sd = statistics.fmean(closes), statistics.pstdev(closes)
    atr = _atr(m15, j)
    if not sd or not atr:
        return None
    r = _rsi(m15, j)
    if c["c"] < mid - 2 * sd and r < 30:
        d = 1
    elif c["c"] > mid + 2 * sd and r > 70:
        d = -1
    else:
        return None
    risk = 1.5 * atr
    reward = abs(mid - c["c"])
    if reward < 0.6 * risk:
        return None
    return {"dir": d, "risk": risk, "target_r": round(reward / risk, 2), "trail": False, "max_hours": 8,
            "why": f"stretched {'below' if d == 1 else 'above'} its band, RSI {r:.0f}"}


def daily_breakout(pair, m15, j, h1, n1, d1, nd):
    """Slow trend-following: buy a close above the 20-day high, sell a close below the 20-day low, ride it with a trailing stop."""
    if nd < 22 or d1[nd - 1]["ts"] + 86400 != _closes_at(m15[j]):
        return None                    # only right when a daily candle has just closed
    last = d1[nd - 1]
    prev = d1[nd - 21:nd - 1]
    hi, lo = max(x["h"] for x in prev), min(x["l"] for x in prev)
    atr = _atr(d1, nd - 1, 20)
    if not atr:
        return None
    if last["c"] > hi:
        d = 1
    elif last["c"] < lo:
        d = -1
    else:
        return None
    return {"dir": d, "risk": 2 * atr, "target_r": None, "trail": True, "max_hours": 24 * 40,
            "why": f"daily close {'above the 20-day high' if d == 1 else 'below the 20-day low'}"}


def gold_breakout(pair, m15, j, h1, n1, d1, nd):
    """Gold only: trade the break of the overnight (Asian) range in the London morning, target 2R."""
    if pair != "XAU_USD":
        return None
    c = m15[j]
    h = _hour(c)
    if not (7 <= h < 12) or j < 60:
        return None
    day = c["t"][:10]
    box = [x for x in m15[j - 60:j] if x["t"][:10] == day and _hour(x) < 7]
    if len(box) < 20:
        return None
    if any(x["t"][:10] == day and 7 <= _hour(x) and (x["c"] > max(b["h"] for b in box) or x["c"] < min(b["l"] for b in box))
           for x in m15[j - 20:j]):
        return None                    # already broke out earlier today: one shot per day
    top, bot = max(x["h"] for x in box), min(x["l"] for x in box)
    size = top - bot
    if c["c"] > top + 0.1 * size:
        d = 1
    elif c["c"] < bot - 0.1 * size:
        d = -1
    else:
        return None
    risk = abs(c["c"] - (top + bot) / 2)
    return {"dir": d, "risk": risk, "target_r": 2.0, "trail": False, "max_hours": 10,
            "why": f"gold broke {'above' if d == 1 else 'below'} its overnight range"}


def ny_opening_range(pair, m15, j, h1, n1, d1, nd):
    """New York open: mark the 13:00–14:00 GMT range, trade a close outside it before 17:00 GMT, target 1.5R."""
    c = m15[j]
    h = _hour(c)
    if not (14 <= h < 17) or j < 20:
        return None
    day = c["t"][:10]
    rng = [x for x in m15[j - 16:j] if x["t"][:10] == day and _hour(x) == 13]
    if len(rng) < 4:
        return None
    top, bot = max(x["h"] for x in rng), min(x["l"] for x in rng)
    if any(x["t"][:10] == day and _hour(x) >= 14 and (x["c"] > top or x["c"] < bot) for x in m15[j - 12:j]):
        return None                    # first break only
    if c["c"] > top:
        d = 1
    elif c["c"] < bot:
        d = -1
    else:
        return None
    risk = abs(c["c"] - (top + bot) / 2)
    atr = _atr(m15, j)
    if not atr or risk < 0.5 * atr:
        return None
    return {"dir": d, "risk": risk, "target_r": 1.5, "trail": False, "max_hours": 6,
            "why": f"broke {'above' if d == 1 else 'below'} the New York opening range"}


def vol_expansion(pair, m15, j, h1, n1, d1, nd):
    """Trend + rising volatility: on a 1-hour close, the trend agrees, volatility is 30% above yesterday's, and price makes a 24-hour high/low."""
    if n1 < 80 or h1[n1 - 1]["ts"] + 3600 != _closes_at(m15[j]):
        return None
    closes = [x["c"] for x in h1[n1 - 80:n1]]
    e20, e50 = _ema_last(closes, 20), _ema_last(closes, 50)
    d = 1 if e20 > e50 else -1
    atr_now, atr_then = _atr(h1, n1 - 1), _atr(h1, n1 - 25)
    if not atr_now or not atr_then or atr_now < 1.3 * atr_then:
        return None
    last = h1[n1 - 1]
    prev = h1[n1 - 25:n1 - 1]
    if d == 1 and last["c"] <= max(x["h"] for x in prev):
        return None
    if d == -1 and last["c"] >= min(x["l"] for x in prev):
        return None
    return {"dir": d, "risk": 1.5 * atr_now, "target_r": None, "trail": True, "max_hours": 72,
            "why": f"volatility up {atr_now / atr_then - 1:.0%} and a fresh 24-hour {'high' if d == 1 else 'low'}"}


CASES = {
    "mean_reversion": {"name": "Quiet-hours snap-back", "fn": mean_reversion,
                       "what": "Between 8 PM and 2 AM ET, when price stretches 2 standard deviations from its 15-min average with RSI extreme, bet it snaps back to the average. Stop 1.5 ATR, out within 8 hours."},
    "daily_breakout": {"name": "20-day breakout", "fn": daily_breakout,
                       "what": "Slow trend-following on daily candles: buy a close above the 20-day high, sell a close below the 20-day low, and ride it with a trailing stop. Few trades, small spread costs."},
    "gold_breakout": {"name": "Gold London breakout", "fn": gold_breakout,
                      "what": "Gold only (the closest to break-even in the first report): trade the first break of the overnight range between 3 AM and 8 AM ET, stop at the middle of the range, target 2R."},
    "ny_opening_range": {"name": "New York opening range", "fn": ny_opening_range,
                         "what": "Mark the 9–10 AM ET range, trade the first close outside it before 1 PM ET. Stop at the middle of the range, target 1.5R, out within 6 hours."},
    "vol_expansion": {"name": "Trend + rising volatility", "fn": vol_expansion,
                      "what": "On a 1-hour close: the trend agrees, volatility is at least 30% above a day ago, and price makes a fresh 24-hour high (or low). Trailing stop, out within 3 days."},
}


def signals(pair, m15, j, h1, n1, d1, nd):
    """Every case's plan for the candle that just closed: {case_id: plan}."""
    out = {}
    for cid, case in CASES.items():
        try:
            p = case["fn"](pair, m15, j, h1, n1, d1, nd)
        except (ValueError, ZeroDivisionError, statistics.StatisticsError, KeyError, IndexError):
            p = None
        if p and p["risk"] > 0:
            out[cid] = p
    return out


def step(pos, c):
    """Advance an open paper position by one 15-min candle. Returns (R, reason) when it closes, else None.
    Uses the candle's bid/ask lows and highs when present, otherwise mid ± half the entry spread."""
    d, hs = pos["dir"], pos.get("hs", 0.0)
    lo = c.get("bl" if d == 1 else "al", c["l"] + (-hs if d == 1 else hs))
    hi = c.get("bh" if d == 1 else "ah", c["h"] + (-hs if d == 1 else hs))
    if (lo <= pos["stop"]) if d == 1 else (hi >= pos["stop"]):
        return round((pos["stop"] - pos["entry"]) * d / pos["risk"], 3), "stop"
    if pos.get("tp") is not None and ((hi >= pos["tp"]) if d == 1 else (lo <= pos["tp"])):
        return round((pos["tp"] - pos["entry"]) * d / pos["risk"], 3), "target"
    if pos.get("trail"):
        best = hi if d == 1 else lo
        pos["stop"] = max(pos["stop"], best - pos["risk"]) if d == 1 else min(pos["stop"], best + pos["risk"])
    if c["ts"] + 900 >= pos["until"]:
        px = c.get("bc" if d == 1 else "ac", c["c"] + (-hs if d == 1 else hs))
        return round((px - pos["entry"]) * d / pos["risk"], 3), "time"
    return None


def open_position(plan, c, ask=None, bid=None):
    """A paper position from a plan, entered at the candle's close (ask for buys, bid for sells)."""
    d = plan["dir"]
    ask = ask if ask is not None else c.get("ac", c["c"])
    bid = bid if bid is not None else c.get("bc", c["c"])
    entry = ask if d == 1 else bid
    risk = plan["risk"]
    return {"dir": d, "entry": entry, "risk": risk, "stop": entry - d * risk,
            "tp": entry + d * plan["target_r"] * risk if plan.get("target_r") else None,
            "trail": plan.get("trail", False), "until": c["ts"] + 900 + plan["max_hours"] * 3600,
            "hs": abs(ask - bid) / 2, "cost_r": abs(ask - bid) / risk, "t": c["t"], "ts": c["ts"] + 900, "why": plan.get("why", "")}
