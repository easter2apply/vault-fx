#!/usr/bin/env python3
"""
Vault FX — a trend-following forex bot for OANDA.

Every 15 minutes it:
  1. reads your phone commands from Telegram (/status, /pause, /resume, /closeall)
  2. manages open trades (moves the stop to breakeven at +1R, reports closed trades)
  3. on each new 1-hour candle, lets the 5-member council vote on a setup
  4. places the trade on OANDA with a stop loss and take profit attached
  5. publishes a dashboard and texts you what it did

Starts on OANDA's PRACTICE account (fake money). Switching to real money is a
deliberate change: set the OANDA_ENV secret/variable to "live".

Needs (as GitHub secrets): OANDA_API_TOKEN, OANDA_ACCOUNT_ID, TELEGRAM_BOT_TOKEN
Only uses the Python standard library.
"""

import argparse
import cases as cs
import office
import datetime as dt
import html
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

# ─────────────────────────── SETTINGS (edit these) ───────────────────────────
PAIRS = ["EUR_USD", "GBP_USD", "AUD_USD", "USD_JPY", "USD_CAD", "USD_CHF", "XAU_USD"]
MAX_SAME_DOLLAR_BETS = 2     # never more than 2 open trades betting the same way on the US dollar
START_BALANCE = 500.00       # the bot trades as if the account holds this much (set to None to use the real OANDA balance)
RISK_PCT = 0.01              # risk 1% of account value per trade (distance to stop × units)
MAX_OPEN_TRADES = 3          # never more than this many trades at once (one per pair)
MAX_LEVERAGE = 10            # cap a trade's size at 10× account value
DAILY_LOSS_LIMIT = 0.03      # stop opening trades for the day after a 3% drop
MAX_DRAWDOWN = 0.10          # pause the bot entirely after a 10% drop from the peak
STREAK_HALF = 5              # after 5 losses in a row, trade half size
STREAK_TIMEOUT = 7           # after 7 losses in a row, no new trades for 24 hours
STREAK_TIMEOUT_HOURS = 24
STREAK_RECOVER_WINS = 2      # back to full size after 2 wins
STOP_ATR = 2.0               # stop loss = 2 × ATR from entry
TARGET_ATR = 3.0             # take profit = 3 × ATR from entry (1.5 : 1 reward : risk)
BREAKEVEN_AT_R = 1.0         # move the stop to entry once the trade is +1R
COUNCIL_MIN_YES = 4          # a trade needs 4 of 5 YES votes, and Risk must vote YES
RELAXED = True               # practice-friendly rules: more trades so the council learns faster (set False for strict)
ADX_MIN = 18 if RELAXED else 22                  # The Quant wants a trend at least this strong
MAX_SPREAD_ATR = 0.25 if RELAXED else 0.15       # Risk votes NO if the spread is over this share of ATR (1-hour setups)
MAX_SPREAD_ATR_15M = 0.50 if RELAXED else 0.30   # 15-minute candles move less, so the allowance is wider
TRADING_HOURS_UTC = (6, 20)  # London + New York sessions (2 AM – 4 PM ET)
SESSION_HOURS_UTC = {        # pairs that also trade the Asian session; 20–23 UTC (around the 5 PM ET rollover) stays off
    "USD_JPY": [(6, 20), (23, 24), (0, 6)],
    "AUD_USD": [(6, 20), (23, 24), (0, 6)],
}
SHADOW_DAYS = 7              # rejected setups are scored for up to a week
COUNCIL_MIN_SHARE = 0.6 if RELAXED else 0.75   # weighted YES share (equal weights: 3 of 5 relaxed, 4 of 5 strict); Risk always vetoes

# News filter: no new trades from 60 min before to 30 min after a high-impact release for either currency
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_REFRESH_HOURS = 6
NEWS_BEFORE_MIN = 60
NEWS_AFTER_MIN = 30
NEWS_WARN_MIN = 30           # heads-up on Telegram when news is this close and you have an open trade on it

# Learning
LEARN_MIN_CALLS = 20         # a voter's weight starts changing after 20 scored calls
PROVEN_MIN = 6               # a pair + setup "theory" is proven after 6+ tests averaging +0.3R or better (party!)
PROVEN_AVG = 0.3
BENCH_MIN = 20               # a pair + setup combo is benched if its last 20 results average below 0R

# Reports (Eastern time; forex's trading day ends at 5 PM ET)
REPORT_HOUR_ET = 17
WEEKLY_DAY = 4               # weekly report on Friday (Mon=0)

LONDON_BREAKOUT = True       # trade the break of the overnight (Asian) range when London opens
LB_RANGE_UTC = (0, 7)        # overnight box: 8 PM – 3 AM ET
LB_WINDOW_UTC = (7, 10)      # breakouts count from 3 AM to 6 AM ET
LB_PAIRS = ["EUR_USD", "GBP_USD", "USD_CHF", "USD_CAD", "AUD_USD", "USD_JPY"]
FULL_SCAN_MIN = 5            # check all pairs every 5 minutes (votes happen on each 15-min and 1-hour candle close)
DEPLOY_EVERY_MIN = 14        # republish the dashboard at most ~4×/hour unless something happened (the live feed updates anyway)
STATE_FILE = "fx_state.json"
LOCAL_TZ = ZoneInfo("America/New_York")
OANDA_ENV = os.environ.get("OANDA_ENV", "practice").strip().lower() or "practice"
OANDA = ("https://api-fxtrade.oanda.com/v3" if OANDA_ENV == "live"
         else "https://api-fxpractice.oanda.com/v3")
NAMES = {"EUR_USD": "EUR/USD", "GBP_USD": "GBP/USD", "AUD_USD": "AUD/USD", "USD_JPY": "USD/JPY",
         "USD_CAD": "USD/CAD", "USD_CHF": "USD/CHF", "XAU_USD": "Gold"}
VOTERS = ["The Quant", "Risk", "Momentum", "Time", "Doubt"]
ROLES = {"The Quant": "trend strength (ADX)", "Risk": "spread, exposure and loss limits",
         "Momentum": "MACD agrees and is building", "Time": "not overextended, in session",
         "Doubt": "the 4-hour trend agrees"}
# ─────────────────────────────────────────────────────────────────────────────


# ───────────────────────────── HTTP helpers ──────────────────────────────────

class ApiError(Exception):
    pass


def http(method, url, token=None, params=None, body=None, tries=3):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": "vault-fx/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            if e.code in (429, 502, 503) and attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise ApiError(f"{e.code}: {msg}")
        except urllib.error.URLError as e:
            if attempt < tries - 1:
                time.sleep(1.5)
                continue
            raise ApiError(str(e))


class Oanda:
    def __init__(self, token, account):
        self.t, self.a = token, account

    def get(self, path, **params):
        return http("GET", f"{OANDA}{path}", self.t, params or None)

    def summary(self):
        return self.get(f"/accounts/{self.a}/summary")["account"]

    def instruments(self):
        out = self.get(f"/accounts/{self.a}/instruments")["instruments"]
        return {i["name"]: i for i in out}

    def pricing(self, pairs):
        out = self.get(f"/accounts/{self.a}/pricing", instruments=",".join(pairs))["prices"]
        return {p["instrument"]: p for p in out}

    def candles(self, pair, gran, count):
        rows = self.get(f"/instruments/{pair}/candles", granularity=gran, count=count, price="M")["candles"]
        return [{"t": c["time"][:19], "o": float(c["mid"]["o"]), "h": float(c["mid"]["h"]),
                 "l": float(c["mid"]["l"]), "c": float(c["mid"]["c"])} for c in rows if c.get("complete")]

    def open_trades(self):
        return self.get(f"/accounts/{self.a}/openTrades")["trades"]

    def trade(self, trade_id):
        return self.get(f"/accounts/{self.a}/trades/{trade_id}")["trade"]

    def market_order(self, pair, units, sl, tp):
        body = {"order": {"type": "MARKET", "instrument": pair, "units": str(units),
                          "timeInForce": "FOK", "positionFill": "DEFAULT",
                          "stopLossOnFill": {"price": sl, "timeInForce": "GTC"},
                          "takeProfitOnFill": {"price": tp, "timeInForce": "GTC"}}}
        return http("POST", f"{OANDA}/accounts/{self.a}/orders", self.t, body=body, tries=1)

    def case_order(self, pair, units, sl, tp=None, trail=None):
        """Market order for a promoted case: fixed stop, optional target, optional trailing stop distance."""
        o = {"type": "MARKET", "instrument": pair, "units": str(units), "timeInForce": "FOK", "positionFill": "DEFAULT",
             "stopLossOnFill": {"price": sl, "timeInForce": "GTC"}}
        if tp:
            o["takeProfitOnFill"] = {"price": tp, "timeInForce": "GTC"}
        if trail:
            o["trailingStopLossOnFill"] = {"distance": trail, "timeInForce": "GTC"}
        return http("POST", f"{OANDA}/accounts/{self.a}/orders", self.t, body={"order": o}, tries=1)

    def set_stop(self, trade_id, price):
        body = {"stopLoss": {"price": price, "timeInForce": "GTC"}}
        return http("PUT", f"{OANDA}/accounts/{self.a}/trades/{trade_id}/orders", self.t, body=body, tries=1)

    def close(self, trade_id):
        return http("PUT", f"{OANDA}/accounts/{self.a}/trades/{trade_id}/close", self.t, body={"units": "ALL"}, tries=1)


# ───────────────────────────── Indicators ────────────────────────────────────

def ema(xs, n):
    k, out, e = 2 / (n + 1), [], None
    for x in xs:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    gains, losses, out = [], [], [None] * len(closes)
    ag = al = None
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, l = max(d, 0), max(-d, 0)
        if i <= n:
            gains.append(g); losses.append(l)
            if i == n:
                ag, al = sum(gains) / n, sum(losses) / n
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + l) / n
        if ag is not None:
            out[i] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr_adx(c, n=14):
    """Wilder ATR and ADX lists for candle dicts."""
    trs, pdm, mdm = [], [], []
    for i in range(1, len(c)):
        h, l, pc = c[i]["h"], c[i]["l"], c[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, dn = h - c[i - 1]["h"], c[i - 1]["l"] - l
        pdm.append(up if up > dn and up > 0 else 0)
        mdm.append(dn if dn > up and dn > 0 else 0)

    def wilder(xs):
        out, s = [], None
        for i, x in enumerate(xs):
            if i < n - 1:
                out.append(None); continue
            s = sum(xs[:n]) if i == n - 1 else s - s / n + x
            out.append(s)
        return out

    tr_s, p_s, m_s = wilder(trs), wilder(pdm), wilder(mdm)
    atr = [None] + [(t / n if t is not None else None) for t in tr_s]
    dx = []
    for t, p, m in zip(tr_s, p_s, m_s):
        if t is None or t == 0:
            dx.append(None); continue
        pdi, mdi = 100 * p / t, 100 * m / t
        dx.append(0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi))
    adx, a, vals = [None], None, []
    for d in dx:
        if d is None:
            adx.append(None); continue
        vals.append(d)
        if len(vals) < n:
            adx.append(None)
        elif len(vals) == n:
            a = sum(vals) / n; adx.append(a)
        else:
            a = (a * (n - 1) + d) / n; adx.append(a)
    return atr, adx


def macd_hist(closes):
    fast, slow = ema(closes, 12), ema(closes, 26)
    line = [f - s for f, s in zip(fast, slow)]
    sig = ema(line, 9)
    return [l - s for l, s in zip(line, sig)]


# ───────────────────────────── Strategy + council ────────────────────────────

def analyze(h1, h4):
    """Indicator snapshot for the last completed 1-hour candle."""
    cl = [x["c"] for x in h1]
    e20, e50 = ema(cl, 20), ema(cl, 50)
    atr, adx = atr_adx(h1)
    r = rsi(cl)
    mh = macd_hist(cl)
    c4 = [x["c"] for x in h4]
    h4_50, h4_200 = ema(c4, 50), ema(c4, 200)
    i = len(h1) - 1
    trend = 1 if e20[i] > e50[i] else -1
    crossed = any((e20[j] > e50[j]) != (e20[j - 1] > e50[j - 1]) for j in range(i - 2, i + 1))
    # pullback: one of the last 2 candles touched EMA20 and the last one closed back on the trend side
    touched = any(h1[j]["l"] <= e20[j] <= h1[j]["h"] for j in (i - 1, i))
    closed_right = (h1[i]["c"] > e20[i]) if trend == 1 else (h1[i]["c"] < e20[i])
    return {
        "time": h1[i]["t"], "close": cl[i], "ema20": e20[i], "ema50": e50[i], "trend": trend,
        "setup": "crossover" if crossed else "pullback" if (touched and closed_right) else None,
        "atr": atr[i], "adx": adx[i], "rsi": r[i], "macd": mh[i], "macd_prev": mh[i - 1],
        "h4_trend": 1 if h4_50[-1] > h4_200[-1] else -1, "h4_close": c4[-1], "h4_ema200": h4_200[-1],
    }


def in_session(pair, now_utc):
    """True when this pair may open new trades right now."""
    wd, h = now_utc.weekday(), now_utc.hour
    if wd == 5 or (wd == 4 and h >= 19) or (wd == 6 and h < 22):
        return False                            # Friday late, Saturday, Sunday before the open
    return any(lo <= h < hi for lo, hi in SESSION_HOURS_UTC.get(pair, [TRADING_HOURS_UTC]))


def next_vote(now_utc):
    """Next hourly candle close when at least one pair is in session (UTC datetime) and which pairs are."""
    t = now_utc.replace(minute=(now_utc.minute // 15) * 15, second=0, microsecond=0) + dt.timedelta(minutes=15)
    for _ in range(320):
        live = [p for p in PAIRS if in_session(p, t)]
        if live:
            return t, live
        t += dt.timedelta(minutes=15)
    return t, []


def council(a, spread, risk_ok, risk_why, now_utc, pair=None):
    d = a["trend"]
    v = {}
    adx = a["adx"] or 0
    v["The Quant"] = (adx >= ADX_MIN, f"ADX {adx:.0f} (needs {ADX_MIN})")
    rs = a["atr"] and spread / a["atr"]
    if not risk_ok:
        v["Risk"] = (False, risk_why)
    else:
        ok = rs is not None and rs <= a.get("spread_max", MAX_SPREAD_ATR)
        v["Risk"] = (ok, f"spread is {rs:.0%} of ATR" if rs is not None else "no ATR yet")
    m, mp = a["macd"], a["macd_prev"]
    if RELAXED:     # "not against us": on our side of zero, or turning our way
        mom_ok = (m > 0 or m > mp) if d == 1 else (m < 0 or m < mp)
        v["Momentum"] = (mom_ok, f"MACD {'with us' if mom_ok else 'against us'} ({'up' if d == 1 else 'down'})")
    else:
        mom_ok = (m > 0 and m >= mp) if d == 1 else (m < 0 and m <= mp)
        v["Momentum"] = (mom_ok, f"MACD {'building' if mom_ok else 'not confirming'} {'up' if d == 1 else 'down'}")
    r = a["rsi"] or 50
    rsi_ok = ((40 <= r <= 75) if d == 1 else (25 <= r <= 60)) if RELAXED else ((45 <= r <= 70) if d == 1 else (30 <= r <= 55))
    open_now = in_session(pair, now_utc)
    v["Time"] = (rsi_ok and open_now,
                 f"RSI {r:.0f}" + ("" if rsi_ok else " (stretched)") + ("" if open_now else ", outside trading hours"))
    big = a["h4_trend"] == d and ((a["h4_close"] > a["h4_ema200"]) if d == 1 else (a["h4_close"] < a["h4_ema200"]))
    big = big and a.get("extra_trend", d) == d
    lbl = a.get("htf_label", "4-hour trend")
    v["Doubt"] = (big, f"{lbl} {('agree' if big else 'disagree') if 'and' in lbl else ('agrees' if big else 'disagrees')}")
    return v


def london_breakout(pair, m15, h1a, spread, now_utc):
    """A breakout of today's Asian-session box on a completed 15-min candle, or None."""
    if not LONDON_BREAKOUT or pair not in LB_PAIRS or not m15:
        return None
    day = now_utc.strftime("%Y-%m-%d")
    lo_h, hi_h = LB_RANGE_UTC
    box = [x for x in m15 if x["t"][:10] == day and lo_h <= int(x["t"][11:13]) < hi_h]
    if len(box) < 16:
        return None
    last = m15[-1]
    lh = int(last["t"][11:13])
    if last["t"][:10] != day or not (LB_WINDOW_UTC[0] <= lh < LB_WINDOW_UTC[1]):
        return None
    top, bot = max(x["h"] for x in box), min(x["l"] for x in box)
    size = top - bot
    buf = size * 0.1
    if last["c"] > top + buf:
        d = 1
    elif last["c"] < bot - buf:
        d = -1
    else:
        return None
    mid = (top + bot) / 2
    body = abs(last["c"] - last["o"]) / max(last["h"] - last["l"], 1e-9)
    return {"time": last["t"], "setup": "London breakout", "trend": d, "close": last["c"], "atr": size,
            "sl_price": mid, "box": (bot, top), "size": size, "body": body, "spread": spread,
            "h1_trend": h1a["trend"], "h1_adx": h1a["adx"] or 0}


def council_breakout(b, risk_ok, risk_why, now_utc, pair, inf):
    d, v = b["trend"], {}
    pipsz = 10 ** int(inf.get("pipLocation", -4))
    size_p = b["size"] / pipsz
    ok_size = b["size"] >= 4 * max(b["spread"], 1e-9) and size_p <= 80
    v["The Quant"] = (ok_size, f"overnight box is {size_p:.0f} pips ({b['size'] / max(b['spread'], 1e-9):.0f}× the spread)")
    v["Risk"] = (False, risk_why) if not risk_ok else (True, "within limits")
    v["Momentum"] = (b["body"] >= 0.5, f"breakout candle {'closed strong' if b['body'] >= 0.5 else 'has a long wick'} ({b['body']:.0%} body)")
    v["Time"] = (in_session(pair, now_utc), "London is opening" if in_session(pair, now_utc) else "outside trading hours")
    against = b["h1_trend"] != d and b["h1_adx"] >= 25
    v["Doubt"] = (not against, "1-hour trend is strongly against it" if against else "1-hour trend isn't fighting it")
    return v


def tally(votes):
    return (sum(1 for ok, _ in votes.values() if ok is True), sum(1 for ok, _ in votes.values() if ok is False))


def voter_weights(st):
    """Voters who call it right more often get a louder vote (after LEARN_MIN_CALLS scored calls)."""
    w = {}
    for n in VOTERS:
        b = (st or {}).get("scoreboard", {}).get(n, {"right": 0, "total": 0})
        if b["total"] < LEARN_MIN_CALLS:
            w[n] = 1.0
            continue
        acc = (b["right"] + 2) / (b["total"] + 4)           # shrunk toward 50% so a lucky streak can't dominate
        w[n] = round(max(0.3, min(2.0, 1 + (acc - 0.5) * 4)), 2)
    return w


def approved(votes, weights=None):
    yes, _ = tally(votes)
    if yes < 3 or votes["Risk"][0] is not True:            # Risk is always a veto
        return False
    w = weights or {}
    ys = sum(w.get(n, 1.0) for n, (ok, _) in votes.items() if ok is True)
    ns = sum(w.get(n, 1.0) for n, (ok, _) in votes.items() if ok is False)
    return ys / (ys + ns) >= COUNCIL_MIN_SHARE - 1e-9


def add_party(st, kind, title, detail):
    """Something worked: the war room throws a little party the next time you open the dashboard."""
    ps = st.setdefault("parties", [])
    ps.append({"t": time.time(), "kind": kind, "title": title, "detail": detail})
    del ps[:-10]


def record_result(st, pair, setup, r_mult, approved_trade=False):
    """Track each pair + setup combo in R; bench combos that keep losing, un-bench them when they recover."""
    L = st.setdefault("learn", {})
    oc = L.setdefault("outcomes", [])
    oc.append({"t": time.time(), "approved": approved_trade, "r": round(r_mult, 3), "pair": pair, "setup": setup})
    del oc[:-400]
    combo = f"{pair}:{setup}"
    xs = L.setdefault("combos", {}).setdefault(combo, [])
    xs.append(round(r_mult, 3))
    del xs[:-30]
    proven = L.setdefault("proven", {})
    tests = xs[-2 * PROVEN_MIN:]
    if len(tests) >= PROVEN_MIN:
        pavg = sum(tests) / len(tests)
        if pavg >= PROVEN_AVG and combo not in proven:
            proven[combo] = round(pavg, 2)
            add_party(st, "proven", f"{setup} on {NAMES[pair]}",
                      f"{pavg:+.2f}R average over its last {len(tests)} tests")
            feed_add(st, "event", "Theory proven 🎉", who="The Quant",
                     text=f"{setup} setups on {NAMES[pair]} average {pavg:+.2f}R over {len(tests)} tests. That one works.")
        elif pavg < 0 and combo in proven:
            proven.pop(combo)
    recent = xs[-BENCH_MIN:]
    if len(recent) < BENCH_MIN:
        return None
    avg = sum(recent) / len(recent)
    benched = L.setdefault("benched", {})
    note = None
    if avg < 0 and combo not in benched:
        benched[combo] = avg
        note = (f"🧠 Learned: {NAMES[pair]} {setup} setups averaged {avg:+.2f}R over the last {BENCH_MIN}. "
                f"Benching them. I'll keep scoring them on paper and bring them back if they recover.")
    elif avg >= 0.1 and combo in benched:
        benched.pop(combo)
        add_party(st, "comeback", f"{setup} on {NAMES[pair]}", f"recovered to {avg:+.2f}R over the last {BENCH_MIN}")
        note = f"🧠 Learned: {NAMES[pair]} {setup} setups recovered ({avg:+.2f}R over the last {BENCH_MIN}). Back in the rotation."
    if note:
        L.setdefault("log", []).append({"t": time.time(), "text": note})
        L["log"] = L["log"][-40:]
        feed_add(st, "event", "Learning", who="Doubt", text=note.replace("🧠 Learned: ", ""))
    return note


def load_news(st, log):
    cal = st.get("news") or {}
    if time.time() - cal.get("fetched", 0) > NEWS_REFRESH_HOURS * 3600:
        try:
            evs = []
            for e in http("GET", NEWS_URL):
                if (e.get("impact") or "").lower() != "high":
                    continue
                try:
                    t = dt.datetime.fromisoformat(e["date"]).timestamp()
                except (KeyError, ValueError):
                    continue
                evs.append({"title": e.get("title", ""), "cur": e.get("country", ""), "t": t})
            st["news"] = {"fetched": time.time(), "events": evs}
        except Exception as ex:
            log.append(f"News calendar: unavailable this run ({ex})")
    return st.get("news", {}).get("events", [])


def news_label(e):
    return f"{e['cur']} {e['title']} ({dt.datetime.fromtimestamp(e['t'], LOCAL_TZ):%a %I:%M %p} ET)"


def usd_direction(pair, d):
    """+1 if the trade wins when the dollar rises, −1 if it wins when the dollar falls."""
    return d if pair.startswith("USD_") else -d


def pair_currencies(pair):
    return {c for c in pair.split("_") if c not in ("XAU", "XAG")} | ({"USD"} if pair.startswith("XA") else set())


def day_stats(st, nav=None):
    d = dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    days = st.setdefault("days", {})
    for k in sorted(days)[:-14]:
        days.pop(k, None)
    rec = days.setdefault(d, {"start_nav": nav, "opened": 0, "setups": 0, "approved": 0, "rejected": 0, "news": 0, "benched": 0})
    if rec.get("start_nav") is None and nav is not None:
        rec["start_nav"] = nav
    return rec


# ───────────────────────────── Sizing + prices ───────────────────────────────

def usd_per_unit_move(pair, price):
    """Dollars gained per unit for a 1.0 price move."""
    base, quote = pair.split("_")
    if quote == "USD":
        return 1.0
    if base == "USD":
        return 1.0 / price
    return None


def notional_usd(pair, units, price):
    base, quote = pair.split("_")
    return units * price if quote == "USD" else units if base == "USD" else None


def fmt_price(x, prec):
    return f"{x:.{prec}f}"


def pips(pair, dist, info):
    loc = int(info.get("pipLocation", -4))
    return dist / (10 ** loc)


# ───────────────────────────── State ─────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    for k, v in (("trades", {}), ("closed", []), ("shadow", []), ("last_bar", {}), ("scoreboard", {}),
                 ("council_record", {"right": 0, "total": 0}), ("record", {"wins": 0, "losses": 0, "pl": 0.0}),
                 ("pairs", {}), ("tg_offset", 0)):
        st.setdefault(k, v)
    return st


def save_state(st):
    st["closed"] = st["closed"][-60:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, sort_keys=True)


def score(st, votes, won, was_trade):
    for name, ok in votes.items():
        if ok is None:
            continue
        b = st["scoreboard"].setdefault(name, {"right": 0, "total": 0})
        b["total"] += 1
        if ok == won:
            b["right"] += 1
    cr = st["council_record"]
    cr["total"] += 1
    right = (was_trade and won) or (not was_trade and not won)
    if right:
        cr["right"] += 1
    st.setdefault("history", []).append({"t": time.time(), "trade": was_trade, "won": won, "right": right})
    st["history"] = st["history"][-400:]


# ───────────────────────────── Inspector: files, hindsight, proving ground ────

PROVING_DAYS = 28            # a cleared case paper-trades live for 4 weeks before it can be promoted
PROVING_MIN_TRADES = 8       # ...with at least this many closed paper trades
PROVING_MIN_AVG = 0.05       # ...averaging at least +0.05R
_FILES = {}


def inspector_files():
    """Inspector's latest backtest (inspector.json), or {} before his first run."""
    if "g" not in _FILES:
        try:
            with open("inspector.json", encoding="utf-8") as f:
                j = json.load(f)
            _FILES["g"], _FILES["when"] = j.get("grade", {}), j.get("when")
        except (OSError, ValueError):
            _FILES["g"], _FILES["when"] = {}, None
    return _FILES["g"]


def case_file(case_id):
    for c in (inspector_files().get("board") or {}).get("cases", []):
        if c["id"] == case_id:
            return c
    return None


def hindsight(st, text, tg=None, kind="note"):
    notes = st.setdefault("hindsight", [])
    notes.append({"t": time.time(), "text": text, "kind": kind})
    del notes[:-40]
    if tg and st.get("hindsight_tg"):
        tg.say("🕵️ " + text)


def file_line(setup):
    c = case_file("setup:" + setup) if setup else None
    if not c:
        return ""
    a = c.get("all") or {}
    return f"In my files, {c['name'].lower()} setups are {c['verdict']} ({(a.get('avg') or 0):+.2f}R over {a.get('n', 0):,})."


def paper_line(st, pair):
    bits = []
    for cid, P in (st.get("paper") or {}).items():
        pos = (P.get("open") or {}).get(pair)
        if pos:
            bits.append(f"{cs.CASES[cid]['name']} is holding a {'BUY' if pos['dir'] == 1 else 'SELL'} ({pos.get('now_r', 0):+.1f}R)")
    return ("Meanwhile on paper: " + "; ".join(bits) + ".") if bits else ""


def proving_ground(st, pair, m15, h1, d1, bid, ask, now, tg, log):
    """Paper-trade every case on this pair: close what hit its stop/target, open what just signalled.
    Returns the new plans {case_id: plan} so a promoted case can trade them for real."""
    paper = st.setdefault("paper", {})
    for cid in cs.CASES:
        paper.setdefault(cid, {"open": {}, "closed": [], "started": now.timestamp()})
    for c in m15:
        c.setdefault("ts", int(dt.datetime.fromisoformat(c["t"][:19]).replace(tzinfo=dt.timezone.utc).timestamp()))
    for c in h1 + d1:
        c.setdefault("ts", int(dt.datetime.fromisoformat(c["t"][:19]).replace(tzinfo=dt.timezone.utc).timestamp()))
    mid = (bid + ask) / 2 if bid and ask else m15[-1]["c"]
    for cid, P in paper.items():
        pos = P["open"].get(pair)
        if not pos:
            continue
        for c in m15:
            if c["ts"] + 900 <= pos["last_ts"]:
                continue
            pos["last_ts"] = c["ts"] + 900
            res = cs.step(pos, c)
            if res:
                r, how = res
                P["closed"].append({"pair": pair, "t": pos["t"], "closed": c["t"], "r": r, "how": how, "dir": pos["dir"], "why": pos.get("why", "")})
                del P["closed"][:-150]
                del P["open"][pair]
                hindsight(st, f"{cs.CASES[cid]['name']} closed its paper {'BUY' if pos['dir'] == 1 else 'SELL'} on {NAMES[pair]}: "
                              f"{r:+.2f}R ({'target' if how == 'target' else 'stop' if how == 'stop' else 'time limit'}).", tg, "paper")
                break
        else:
            pos["now_r"] = round((mid - pos["entry"]) * pos["dir"] / pos["risk"], 2)
    last = m15[-1]
    seen = st.setdefault("paper_bar", {})
    if seen.get(pair) == last["t"]:
        return {}
    seen[pair] = last["t"]
    plans = cs.signals(pair, m15, len(m15) - 1, h1, len(h1), d1, len(d1))
    for cid in list(plans):
        fx = fix_blocks(st, "case:" + cid, (last["ts"] // 3600) % 24, dt.datetime.fromtimestamp(last["ts"], dt.timezone.utc).weekday(), pair)
        if fx:
            plans.pop(cid)
    for cid, plan in plans.items():
        P = paper[cid]
        if pair in P["open"]:
            continue
        pos = cs.open_position(plan, last, ask=ask, bid=bid)
        pos["last_ts"] = last["ts"] + 900
        pos["now_r"] = 0.0
        P["open"][pair] = pos
        hindsight(st, f"{cs.CASES[cid]['name']} would {'BUY' if plan['dir'] == 1 else 'SELL'} {NAMES[pair]} now: {plan['why']}.", tg, "paper")
    return plans


def proving_status(st, cid, now_ts=None):
    """(state, details) for one case: untested · guilty · watching · proving · ready."""
    now_ts = now_ts or time.time()
    P = (st.get("paper") or {}).get(cid, {})
    f = case_file("case:" + cid)
    since = P.get("proving_since")
    closed = [x for x in P.get("closed", []) if since and dt.datetime.fromisoformat(x["closed"][:19]).replace(
        tzinfo=dt.timezone.utc).timestamp() >= since]
    n = len(closed)
    avg = sum(x["r"] for x in closed) / n if n else None
    days = (now_ts - since) / 86400 if since else 0
    if not f:
        state = "untested"
    elif f["verdict"] != "CLEARED":
        state = "guilty" if f["verdict"] == "GUILTY" else "watching"
    elif days >= PROVING_DAYS and n >= PROVING_MIN_TRADES and (avg or -9) >= PROVING_MIN_AVG:
        state = "ready"
    else:
        state = "proving"
    return state, {"days": days, "n": n, "avg": avg, "file": f}


def daily_candles(api, st, pair, now):
    """Daily candles, fetched once a day per pair (the 20-day breakout case needs them)."""
    cache = st.setdefault("d1", {})
    day = now.strftime("%Y-%m-%d %H")[:10]
    hit = cache.get(pair)
    if hit and hit.get("day") == day and now.hour != 21:
        return [dict(c) for c in hit["c"]]
    rows = api.candles(pair, "D", 30)
    cache[pair] = {"day": day, "c": [{k: c[k] for k in ("t", "o", "h", "l", "c")} for c in rows]}
    return [dict(c) for c in cache[pair]["c"]]


def active_fixes(st):
    return st.get("fixes_active", [])


def fix_blocks(st, case_id, hour_utc, weekday, pair):
    """The approved fix that rules this setup out, or None."""
    for f in active_fixes(st):
        applies_to = (case_id, "bot") if case_id.startswith("setup:") else (case_id,)   # a "bot" fix covers every council setup
        if f["case_id"] not in applies_to:
            continue
        if f["type"] == "hours" and f["value"][0] <= hour_utc < f["value"][1]:
            return f
        if f["type"] == "day" and weekday == f["value"]:
            return f
        if f["type"] == "pair" and pair == f["value"]:
            return f
    return None


def accuracy_change(st):
    """Win rate and average result before vs after Inspector's first correction (your first /approve)."""
    def summ(xs):
        if not xs:
            return None
        rs = [x.get("r", 0) or 0 for x in xs]
        return {"n": len(xs), "win": sum(1 for x in xs if (x.get("pl") or 0) > 0) / len(xs), "avg": sum(rs) / len(rs)}
    since = st.get("inspector_since")
    closed = [c for c in st.get("closed", []) if c.get("closed_ts")]
    bot = case_file("bot")
    backtest = {"n": bot["all"]["n"], "win": bot["all"]["win"], "avg": bot["all"]["avg"]} if bot and bot.get("all", {}).get("n") else None
    if not since:
        return {"state": "waiting", "now": summ(closed) or backtest, "source": "live" if closed else "backtest"}
    before = summ([c for c in closed if c["closed_ts"] < since]) or backtest
    after = summ([c for c in closed if c["closed_ts"] >= since])
    return {"state": "tracking", "since": since, "before": before, "after": after,
            "before_source": "live" if any(c["closed_ts"] < since for c in closed) else "backtest"}


def proving_checks(st, tg):
    for cid in cs.CASES:
        P = st.setdefault("paper", {}).setdefault(cid, {"open": {}, "closed": [], "started": time.time()})
        f = case_file("case:" + cid)
        if f and f["verdict"] == "CLEARED":
            P.setdefault("proving_since", time.time())
        else:
            P.pop("proving_since", None); P.pop("offered", None)
        state, d = proving_status(st, cid)
        if state == "ready" and not P.get("offered") and st.get("strategy") != cid:
            P["offered"] = True
            msg = (f"🕵️ Case closed: {cs.CASES[cid]['name']} passed the backtest AND {d['days']:.0f} days of live paper trading "
                   f"({d['avg']:+.2f}R a trade over {d['n']} trades).\n\nReply /approve {cid} to make it the bot's strategy "
                   "(on your practice account). /revert switches back to the council any time.")
            hindsight(st, f"{cs.CASES[cid]['name']} is ready for promotion. Waiting for your /approve.", None, "verdict")
            if tg:
                tg.say(msg)


def place_case_trade(api, st, pair, cid, plan, bid, ask, inf, nav, now, news, open_pairs, log, tg):
    """The promoted case trades for real (practice account), with the bot's usual safety limits."""
    d = plan["dir"]
    reasons = []
    if st.get("paused"):
        reasons.append("paused")
    if st["day"].get("halted"):
        reasons.append("daily loss limit hit")
    if streak_status(st, now.timestamp()) == "timeout":
        reasons.append("losing-streak timeout")
    if pair in open_pairs:
        reasons.append("already in a trade on this pair")
    if len(st["trades"]) >= MAX_OPEN_TRADES:
        reasons.append(f"{MAX_OPEN_TRADES} trades already open")
    ts = now.timestamp()
    if any(e["cur"] in pair_currencies(pair) and e["t"] - NEWS_BEFORE_MIN * 60 <= ts <= e["t"] + NEWS_AFTER_MIN * 60 for e in news):
        reasons.append("news")
    name = cs.CASES[cid]["name"]
    if reasons:
        log.append(f"{pair}: {name} signal skipped ({'; '.join(reasons)})")
        return
    entry = ask if d == 1 else bid
    risk = plan["risk"]
    sl = entry - d * risk
    tp = entry + d * plan["target_r"] * risk if plan.get("target_r") else None
    prec = int(inf.get("displayPrecision", 5))
    conv = usd_per_unit_move(pair, entry)
    if conv is None:
        return
    risk_usd = nav * RISK_PCT * (0.5 if streak_status(st, ts) != "normal" else 1)
    units = min(int(risk_usd / (risk * conv)), int(nav * MAX_LEVERAGE / max(notional_usd(pair, 1, entry), 1e-9)))
    if units < max(1, float(inf.get("minimumTradeSize", 1))):
        log.append(f"{pair}: {name} skipped, smallest trade would risk too much")
        return
    try:
        res = api.case_order(pair, d * units, fmt_price(sl, prec), fmt_price(tp, prec) if tp else None,
                             fmt_price(risk, prec) if plan.get("trail") else None)
    except ApiError as e:
        log.append(f"{pair}: {name} order rejected ({e})")
        return
    opened = (res.get("orderFillTransaction") or {}).get("tradeOpened") or {}
    if not opened:
        log.append(f"{pair}: {name} order not filled")
        return
    fill = float(opened.get("price", entry))
    tid = str(opened["tradeID"])
    st["trades"][tid] = {"pair": pair, "dir": d, "units": units, "entry": fill, "sl": sl, "tp": tp or sl, "opened": now.isoformat()[:19],
                         "risk": round(risk_usd, 2), "votes": {}, "setup": name, "tf": "case", "price": fill, "upl": 0.0,
                         "case": cid, "until": now.timestamp() + plan["max_hours"] * 3600}
    open_pairs.add(pair)
    st["_changed"] = True
    feed_add(st, "event", f"{name}: {NAMES[pair]} {'BUY' if d == 1 else 'SELL'}", who="Chair", ok=True,
             text=f"Inspector's promoted strategy opened {units:,} units: {plan['why']}.")
    hindsight(st, f"Promoted strategy {name} opened a real {'BUY' if d == 1 else 'SELL'} on {NAMES[pair]}: {plan['why']}.", None, "trade")
    if tg:
        tg.say(f"🟢 {name}: {'BUY' if d == 1 else 'SELL'} {NAMES[pair]} ({units:,} units)\nWhy: {plan['why']}\n"
               f"Stop {fmt_price(sl, prec)}" + (f" · target {fmt_price(tp, prec)}" if tp else " · trailing stop, no target"))


# ───────────────────────────── Telegram ──────────────────────────────────────

class Telegram:
    def __init__(self, token, st):
        self.token, self.st, self.out = token, st, []

    def api(self, method, **params):
        return http("POST", f"https://api.telegram.org/bot{self.token}/{method}", body=params, tries=2)

    def chat(self):
        return os.environ.get("TELEGRAM_CHAT_ID", "").strip() or self.st.get("chat_id")

    def commands(self):
        """Return command words sent from your chat since the last run."""
        cmds = []
        try:
            res = self.api("getUpdates", offset=self.st.get("tg_offset", 0), timeout=0).get("result", [])
        except ApiError:
            return cmds
        for u in res:
            self.st["tg_offset"] = u["update_id"] + 1
            msg = u.get("message") or {}
            cid = str(msg.get("chat", {}).get("id", ""))
            if not cid:
                continue
            if not self.chat():
                self.st["chat_id"] = cid
            if cid != str(self.chat()):
                continue                       # ignore anyone else who finds the bot
            text = (msg.get("text") or "").strip().lower().split("@")[0]
            if text.startswith("/"):
                cmds.append(text)
        return cmds

    def say(self, text):
        self.out.append(text)

    def flush(self, log):
        cid = self.chat()
        if not cid or not self.out:
            if self.out and not cid:
                log.append("Telegram: no chat yet. Send your FX bot any message.")
            return
        sent = 0
        for text in self.out:
            try:
                self.api("sendMessage", chat_id=cid, text=text, disable_web_page_preview=True)
                sent += 1
                time.sleep(0.4)
            except ApiError as e:
                log.append(f"Telegram: send failed ({e})")
                break
        log.append(f"Telegram: sent {sent} message(s)")


def money(x):
    return f"{'+' if x >= 0 else '−'}${abs(x):,.2f}"


# ───────────────────────────── One run ───────────────────────────────────────

def plan(st, now=None):
    """'full' = scan for new trades, 'manage' = only look after open trades, 'skip' = nothing to do."""
    now = now or time.time()
    if now - st.get("last_full_run", 0) >= FULL_SCAN_MIN * 60 - 90:   # 90s slack for trigger jitter
        return "full"
    return "manage" if st.get("trades") else "skip"


def run(log, full=True):
    token, acct = os.environ.get("OANDA_API_TOKEN", "").strip(), os.environ.get("OANDA_ACCOUNT_ID", "").strip()
    st = load_state()
    now = dt.datetime.now(dt.timezone.utc)
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg = Telegram(tg_token, st) if tg_token else None
    st["mode"] = OANDA_ENV
    st["last_run"] = now.timestamp()
    if full:
        st["last_full_run"] = now.timestamp()
    if not token or not acct:
        log.append("OANDA: add OANDA_API_TOKEN and OANDA_ACCOUNT_ID secrets to start trading")
        save_state(st)
        return st
    api = Oanda(token, acct)

    # 0) account + phone commands
    acc = api.summary()
    nav, bal = float(acc["NAV"]), float(acc["balance"])
    unreal = float(acc.get("unrealizedPL", 0))
    if START_BALANCE:
        # a virtual bankroll: starting amount + what this bot's own trades have made or lost
        if st.get("start_balance") != START_BALANCE:
            st.update(start_balance=START_BALANCE, peak=START_BALANCE, day={}, equity=[],
                      record={"wins": 0, "losses": 0, "pl": 0.0})     # new bankroll = fresh scorecard
        mine = [t for t in api.open_trades() if str(t["id"]) in st["trades"]]
        unreal = sum(float(t.get("unrealizedPL", 0)) for t in mine)
        bal = START_BALANCE + st["record"]["pl"]
        nav = bal + unreal
    st["account"] = {"nav": nav, "balance": bal, "unrealized": unreal, "oanda_nav": float(acc["NAV"]),
                     "currency": acc.get("currency", "USD"), "margin_avail": float(acc.get("marginAvailable", 0)),
                     "updated": now.timestamp()}
    today = now.strftime("%Y-%m-%d")
    if st.get("day", {}).get("date") != today:
        st["day"] = {"date": today, "start_nav": nav, "halted": False}
    st["peak"] = max(st.get("peak", nav), nav)

    if tg:
        for full_cmd in tg.commands():
            cmd, _, arg = full_cmd.partition(" ")
            if cmd == "/live":
                on = {"on": True, "off": False}.get(arg.strip(), not st.get("live_tg"))
                st["live_tg"] = on
                tg.say("🎙 Live council is ON. You'll get every debate, including rejected setups. /live off to stop."
                       if on else "🔇 Live council is OFF. You'll still get trades, closes and reports.")
            elif cmd == "/pause":
                feed_add(st, "event", "Paused", who="Chair", text="You paused new trades. Open trades keep their stops.")
                st["paused"] = "by you"; tg.say("⏸ Paused. I won't open new trades. Open trades keep their stops. /resume to restart.")
            elif cmd == "/resume":
                st["paused"] = None; st["peak"] = nav; tg.say("▶️ Resumed. Back to normal trading.")
                if st.get("streak", {}).get("until", 0) > time.time():
                    st["streak"]["until"] = 0
                    tg.say("🧊 Timeout ended early. Still trading at half size until 2 wins.")
                feed_add(st, "event", "Resumed", who="Chair", text="You resumed trading. Back to normal.")
            elif cmd == "/closeall":
                n = 0
                for t in api.open_trades():
                    try:
                        api.close(t["id"]); n += 1
                    except ApiError as e:
                        tg.say(f"Couldn't close {NAMES.get(t['instrument'], t['instrument'])}: {e}")
                st["paused"] = "after /closeall"
                tg.say(f"🧯 Closed {n} trade(s) and paused. /resume when you're ready.")
            elif cmd == "/testtrade":
                st["test_pending"] = True
                tg.say("🧪 Got it. On this run I'll place a 1-unit EUR/USD practice trade with a stop and target, close it right away, and report back.")
            elif cmd in ("/happy", "/unhappy") or (cmd == "/start" and arg.strip() in ("happy", "unhappy")):
                good = cmd == "/happy" or arg.strip() == "happy"
                mood = st.setdefault("mood", {})
                day_ = dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
                m = mood.setdefault(day_, {"happy": 0, "unhappy": 0})
                m["happy" if good else "unhappy"] += 1
                for k_ in sorted(mood)[:-14]:
                    mood.pop(k_, None)
                st["last_mood"] = {"kind": "cheers" if good else "sad", "t": time.time()}
                feed_add(st, "event", "Commander's feedback", who="Chair", ok=good,
                         text="The commander is happy. The council raises a glass! 🥂" if good else
                              "The commander isn't happy. Noted. We'll keep it disciplined and earn it back.")
                tg.say("🥂 Cheers from the council!" if good else "😔 Heard you, Commander. Noted for the daily report.")
            elif cmd == "/hindsight":
                on = {"on": True, "off": False}.get(arg.strip(), not st.get("hindsight_tg"))
                st["hindsight_tg"] = on
                tg.say("🕵️ Inspector's hindsight notes are ON. He'll comment on votes, closes and his paper trades. /hindsight off to stop."
                       if on else "🕵️ Hindsight notes OFF. They're still on his tab.")
            elif cmd == "/cases":
                rows = []
                for cid, case in cs.CASES.items():
                    state, d = proving_status(st, cid)
                    P = st.get("paper", {}).get(cid, {})
                    allc = P.get("closed", [])
                    avg = sum(x["r"] for x in allc) / len(allc) if allc else None
                    rows.append(f"• {case['name']} ({cid}): {state}" + (f", paper {avg:+.2f}R over {len(allc)}" if allc else ", no paper trades yet")
                                + (f", day {d['days']:.0f} of {PROVING_DAYS}" if state == "proving" else ""))
                cur = st.get("strategy")
                tg.say("🗂 Inspector's cases\n" + "\n".join(rows) + f"\n\nTrading now: {cs.CASES[cur]['name'] if cur in cs.CASES else 'the council'}")
            elif cmd == "/approve" and arg.strip().lower().startswith("f") and arg.strip().split(" ")[0][1:].isdigit():
                fid, _, extra = arg.strip().lower().partition(" ")
                f = next((x for x in (inspector_files().get("fixes") or []) if x["id"] == fid), None)
                if not f:
                    tg.say(f"I don't have a fix called {fid}. Send /fixes to see them.")
                elif f["status"] != "ready" and extra.strip() != "anyway":
                    why = (f"it's still proving itself on fresh data (day {f.get('days', 0):.0f} of 28)" if f["status"] == "proving" else
                           "it didn't hold up on fresh data, so I don't recommend it")
                    tg.say(f"⚠️ Fix {fid} ({f['case_name']}: {f['text']}) isn't ready: {why}. Send /approve {fid} anyway to apply it now.")
                elif any(x["id"] == fid for x in active_fixes(st)):
                    tg.say(f"Fix {fid} is already on.")
                else:
                    st.setdefault("fixes_active", []).append({k: f[k] for k in ("id", "case_id", "case_name", "type", "value", "text")})
                    st.setdefault("inspector_since", time.time())
                    st.setdefault("inspector_fixes", []).append({"t": time.time(), "fix": fid})
                    hindsight(st, f"You approved fix {fid}: {f['case_name']}, {f['text']}. It's live now.", None, "verdict")
                    feed_add(st, "event", "Inspector's fix applied", who="Chair", text=f"{f['case_name']}: {f['text']}.")
                    tg.say(f"🔧 Fix {fid} is on: {f['case_name']}, {f['text']}. The accuracy tracker on Inspector's tab counts from now. "
                           f"/unfix {fid} turns it off.")
            elif cmd == "/fixes":
                fx = inspector_files().get("fixes") or []
                on = {x["id"] for x in active_fixes(st)}
                word = {"proving": "proving", "ready": "READY, /approve " , "failed-fresh": "failed on fresh data", "orphaned": "retired"}
                rows = [f"• {f['id']}: {f['case_name']}, {f['text']} · {'ON' if f['id'] in on else word.get(f['status'], f['status']) + (f['id'] if f['status'] == 'ready' else '')}"
                        + (f" (day {f.get('days', 0):.0f}/28)" if f["status"] == "proving" else "") for f in fx]
                tg.say("🔧 Inspector's fixes\n" + ("\n".join(rows) if rows else "None yet. They appear after Inspector finds one that holds up on unseen months."))
            elif cmd == "/unfix":
                fid = arg.strip().lower()
                before = len(active_fixes(st))
                st["fixes_active"] = [x for x in active_fixes(st) if x["id"] != fid]
                tg.say(f"Fix {fid} is off." if len(st["fixes_active"]) < before else f"Fix {fid} wasn't on.")
            elif cmd == "/approve":
                cid, _, extra = arg.strip().partition(" ")
                if cid not in cs.CASES:
                    tg.say("Which one? " + ", ".join(cs.CASES) + "\nExample: /approve daily_breakout")
                else:
                    state, d = proving_status(st, cid)
                    if state != "ready" and extra.strip() != "anyway":
                        tg.say(f"⚠️ {cs.CASES[cid]['name']} hasn't passed the proving ground yet ({state}; "
                               f"{d['n']} paper trades{', ' + format(d['avg'], '+.2f') + 'R' if d['avg'] is not None else ''}). "
                               f"Send /approve {cid} anyway to switch now.")
                    else:
                        st["strategy"] = cid
                        st.setdefault("inspector_since", time.time())          # when Inspector first changed how the bot trades
                        st.setdefault("inspector_fixes", []).append({"t": time.time(), "case": cid})
                        feed_add(st, "event", "New strategy", who="Chair", text=f"Promoted {cs.CASES[cid]['name']}. The council steps aside.")
                        hindsight(st, f"You promoted {cs.CASES[cid]['name']}. It now trades your practice account.", None, "verdict")
                        tg.say(f"✅ {cs.CASES[cid]['name']} is now the bot's strategy (practice account). The council stops trading; "
                               "the same safety limits still apply. /revert to go back.")
            elif cmd == "/revert":
                st["strategy"] = None
                tg.say("↩️ Back to the council's strategy. Open trades keep their stops and targets.")
            elif cmd in ("/status", "/start"):
                st["_status_requested"] = True
        if not st.get("welcomed") and tg.chat():
            tg.say(f"✅ Vault FX is connected ({OANDA_ENV.upper()} account, trading a ${nav:,.2f} bankroll).\n\n"
                   f"I check {', '.join(NAMES[p] for p in PAIRS)} every 5 minutes. The council votes on every 15-minute and 1-hour candle, "
                   "and I trade when it approves.\n\n"
                   "Commands (I read them every 5 min):\n/status – account and open trades\n"
                   "/pause – stop opening new trades\n/resume – start again\n/closeall – close every open trade\n"
                   "/live – turn the live council debate on or off\n/testtrade – place and close a tiny practice trade to prove orders work\n"
                   "/cases – Inspector's strategies and their proving-ground status\n/approve <case> – promote one\n/revert – back to the council\n"
                   "/hindsight – Inspector's notes on or off\n/fixes – Inspector's strategy fixes\n/approve f1 – apply a fix · /unfix f1 – remove it")
            st["welcomed"] = True

    if tg and not st.get("bot_username"):
        try:
            st["bot_username"] = tg.api("getMe").get("result", {}).get("username")
        except ApiError:
            pass

    # 1) safety limits
    if nav <= st["day"]["start_nav"] * (1 - DAILY_LOSS_LIMIT) and not st["day"]["halted"]:
        st["day"]["halted"] = True
        if tg:
            tg.say(f"🛑 Daily loss limit hit ({DAILY_LOSS_LIMIT:.0%}). No new trades until tomorrow (UTC). Open trades keep their stops.")
    if nav <= st["peak"] * (1 - MAX_DRAWDOWN) and not st.get("paused"):
        st["paused"] = f"drawdown over {MAX_DRAWDOWN:.0%}"
        if tg:
            tg.say(f"🛑 Account is down {MAX_DRAWDOWN:.0%} from its peak. I've paused all new trading. Review things, then send /resume.")

    news = load_news(st, log)
    today = day_stats(st, nav)

    # 2) manage trades
    info = api.instruments()
    pairs = [p for p in PAIRS if p in info]
    for p in PAIRS:
        if p not in info:
            log.append(f"{p}: not offered on this OANDA account, skipping")
    prices = api.pricing(pairs) if pairs else {}
    open_now = {t["id"]: t for t in api.open_trades()}
    for tid, meta in list(st["trades"].items()):
        if tid in open_now:
            t = open_now[tid]
            meta["upl"] = float(t.get("unrealizedPL", 0))
            p = prices.get(meta["pair"], {})
            mid = (float(p["bids"][0]["price"]) + float(p["asks"][0]["price"])) / 2 if p.get("bids") and p.get("asks") else None
            meta["price"] = mid
            if meta.get("until") and now.timestamp() > meta["until"]:
                try:
                    api.close(tid); log.append(f"{meta['pair']}: {meta.get('setup')} time limit reached, closed")
                except ApiError as e:
                    log.append(f"{meta['pair']}: couldn't close at time limit ({e})")
            if mid and not meta.get("breakeven") and not meta.get("case"):
                r_dist = abs(meta["entry"] - meta["sl"])
                gain = (mid - meta["entry"]) * meta["dir"]
                if r_dist and gain >= BREAKEVEN_AT_R * r_dist:
                    prec = int(info.get(meta["pair"], {}).get("displayPrecision", 5))
                    try:
                        api.set_stop(tid, fmt_price(meta["entry"], prec))
                        meta["breakeven"] = True; meta["sl"] = meta["entry"]
                        feed_add(st, "event", f"{NAMES[meta['pair']]} stop to breakeven", who="Risk",
                                 text=f"Locked the {NAMES[meta['pair']]} stop at entry. This one can't lose now.")
                        if tg:
                            tg.say(f"🔒 {NAMES[meta['pair']]} {'BUY' if meta['dir'] == 1 else 'SELL'}: stop moved to entry. This trade can't lose now.")
                    except ApiError as e:
                        log.append(f"{meta['pair']}: couldn't move stop ({e})")
            continue
        try:
            t = api.trade(tid)
        except ApiError:
            continue
        if t.get("state") != "CLOSED":
            continue
        pl = float(t.get("realizedPL", 0))
        won = pl > 0
        meta.update(pl=pl, closed=t.get("closeTime", now.isoformat())[:19], exit=float(t.get("averageClosePrice", 0) or 0))
        st["closed"].append(meta)
        st["_changed"] = True
        rec = st["record"]; rec["wins" if won else "losses"] += 1; rec["pl"] = round(rec["pl"] + pl, 2)
        score(st, meta.get("votes", {}), won, True)
        meta["r"] = round(pl / meta["risk"], 2) if meta.get("risk") else 0
        if won:
            add_party(st, "win", f"{NAMES[meta['pair']]} {'BUY' if meta['dir'] == 1 else 'SELL'} won",
                      f"{money(pl)} · {meta['r']:+.2f}R · {meta.get('setup', 'pullback')} theory worked")
        for note_ in streak_update(st, meta["r"], now.timestamp()):
            if tg:
                tg.say(note_)
        feed_add(st, "event", f"{NAMES[meta['pair']]} closed", who="The Quant", ok=won,
                 text=f"{NAMES[meta['pair']]} {'BUY' if meta['dir'] == 1 else 'SELL'} closed {money(pl)} ({meta['r']:+.2f}R). "
                      f"Record {st['record']['wins']}W–{st['record']['losses']}L.")
        meta["closed_ts"] = now.timestamp()
        hindsight(st, " ".join(x for x in (f"{NAMES[meta['pair']]} {'BUY' if meta['dir'] == 1 else 'SELL'} ({meta.get('setup', '')}) closed {meta['r']:+.2f}R.",
                                             file_line(meta.get("setup")) if not meta.get("case") else "", paper_line(st, meta["pair"])) if x), tg, "close")
        note = record_result(st, meta["pair"], meta.get("setup", "pullback"), meta["r"], approved_trade=True)
        if note and tg:
            tg.say(note)
        del st["trades"][tid]
        if tg:
            how = "🎯 take profit" if won and not meta.get("breakeven") else "🔒 breakeven stop" if meta.get("breakeven") and abs(pl) < 1 else "🎯 profit" if won else "🛑 stop loss"
            tg.say(f"{'✅' if won else '❌'} CLOSED {NAMES[meta['pair']]} {'BUY' if meta['dir'] == 1 else 'SELL'} ({how})\n"
                   f"Result: {money(pl)}  ·  Record {rec['wins']}W–{rec['losses']}L, total {money(rec['pl'])}")

    # heads-up when big news is about to hit a currency you hold
    warned = st.setdefault("warned", [])
    ts = now.timestamp()
    for tid, meta in st["trades"].items():
        for e in news:
            key = f"{tid}:{e['t']}"
            if e["cur"] in pair_currencies(meta["pair"]) and 0 <= e["t"] - ts <= NEWS_WARN_MIN * 60 and key not in warned:
                warned.append(key)
                feed_add(st, "event", "News incoming", who="Time",
                         text=f"{news_label(e)} in {round((e['t'] - ts) / 60)} min. We're holding {NAMES[meta['pair']]}; stop is set.")
                if tg:
                    tg.say(f"📰 Heads up: {news_label(e)} in {round((e['t'] - ts) / 60)} min. "
                           f"Your {NAMES[meta['pair']]} {'BUY' if meta['dir'] == 1 else 'SELL'} is open "
                           f"({money(meta.get('upl', 0))}), stop at {meta['sl']:.{st.get('pairs', {}).get(meta['pair'], {}).get('prec', 5)}f}. "
                           "Prices can jump fast around news.")
    del warned[:-50]

    if START_BALANCE:                           # refresh the bankroll after closes/updates
        bal = START_BALANCE + st["record"]["pl"]
        unreal = sum(m.get("upl", 0) for m in st["trades"].values())
        nav = bal + unreal
        st["account"].update(nav=nav, balance=bal, unrealized=unreal)
        st["peak"] = max(st.get("peak", nav), nav)
    if full:
        eq = st.setdefault("equity", [])
        if not eq or now.timestamp() - eq[-1][0] >= 14 * 60:
            eq.append([round(now.timestamp()), round(nav, 2)])
        del eq[:-1500]                          # about two weeks at one point per 15 minutes

    # 3) council on each new 1-hour candle (full runs only)
    open_pairs = {m["pair"] for m in st["trades"].values()}
    for pair in (pairs if full else []):
        try:
            h1, h4, m15 = api.candles(pair, "H1", 300), api.candles(pair, "H4", 300), api.candles(pair, "M15", 300)
        except ApiError as e:
            log.append(f"{pair}: no candles ({e})"); continue
        if len(h1) < 100 or len(h4) < 210:
            log.append(f"{pair}: not enough history yet"); continue
        a = analyze(h1, h4)
        tfs = [("H1", a, h1)]
        if len(m15) >= 210:
            a15 = analyze(m15, h1)                 # 15-min setup, filtered by the 1-hour and the 4-hour trend
            a15.update(extra_trend=a["h4_trend"], htf_label="1-hour and 4-hour trends", spread_max=MAX_SPREAD_ATR_15M)
            if a15["setup"]:
                a15["setup"] += " 15m"
            tfs.append(("M15", a15, m15))
        p = prices.get(pair, {})
        bid = float(p["bids"][0]["price"]) if p.get("bids") else None
        ask = float(p["asks"][0]["price"]) if p.get("asks") else None
        spread = (ask - bid) if bid and ask else 0.0
        inf = info.get(pair, {})
        prec = int(inf.get("displayPrecision", 5))
        st["pairs"][pair] = {"trend": a["trend"], "h4_trend": a["h4_trend"], "adx": a["adx"], "rsi": a["rsi"],
                             "atr_pips": pips(pair, a["atr"] or 0, inf), "spread_pips": pips(pair, spread, inf),
                             "close": a["close"], "prec": prec, "setup": a["setup"], "bar": a["time"],
                             "tradeable": bool(p.get("tradeable", False)),
                             "chg": (a["close"] - h1[-25]["c"]) / h1[-25]["c"] * 100 if len(h1) > 25 else 0}
        # Inspector's proving ground: paper-trade every case; a promoted case trades for real
        try:
            d1 = daily_candles(api, st, pair, now)
            plans = proving_ground(st, pair, m15, h1, d1, bid, ask, now, tg, log) if bid and ask else {}
            strat = st.get("strategy")
            if strat in plans:
                place_case_trade(api, st, pair, strat, plans[strat], bid, ask, inf, nav, now, news, open_pairs, log, tg)
        except Exception as e:                       # never let the side project break trading
            log.append(f"{pair}: proving ground skipped ({e})")
        lbk = london_breakout(pair, m15, a, spread, now) if len(m15) >= 40 else None
        if lbk and st.setdefault("lb_done", {}).get(pair) != now.strftime("%Y-%m-%d"):
            tfs.append(("LB", lbk, m15))
        best = None
        for tf, at, _ in tfs:
            if tf == "LB" or not at["atr"]:
                continue
            hyp = council(at, spread, True, "", now, pair)         # "if a setup appeared now, how would they vote?"
            passed = sum(1 for o, _ in hyp.values() if o is True) + (1 if at["setup"] else 0)
            waiting = [] if at["setup"] else ["a pullback to the trend line or a fresh crossover"]
            waiting += [why for o, why in hyp.values() if o is False]
            cand = {"passed": passed, "of": 6, "dir": at["trend"], "waiting": waiting[:2], "tf": tf,
                    "in_trade": pair in open_pairs, "session": in_session(pair, now)}
            if best is None or passed > best["passed"]:
                best = cand
        if best:
            st["pairs"][pair]["ready"] = best

        # score rejected setups whose stop or target has been reached
        for sh in [s for s in st["shadow"] if s["pair"] == pair]:
            later = [x for x in (m15 if sh.get("tf") in ("M15", "LB") else h1) if x["t"] > sh["bar"]]
            hit = None
            for x in later:
                hit_sl = (x["l"] <= sh["sl"]) if sh["dir"] == 1 else (x["h"] >= sh["sl"])
                hit_tp = (x["h"] >= sh["tp"]) if sh["dir"] == 1 else (x["l"] <= sh["tp"])
                if hit_sl:
                    hit = False; break          # stop first (or both in one candle) counts as a loss
                if hit_tp:
                    hit = True; break
            age = (now - dt.datetime.fromisoformat(sh["bar"]).replace(tzinfo=dt.timezone.utc)).days
            if hit is not None:
                score(st, sh["votes"], hit, False); st["shadow"].remove(sh)
                note = record_result(st, pair, sh.get("setup", "pullback"), TARGET_ATR / STOP_ATR if hit else -1.0)
                if note and tg:
                    tg.say(note)
            elif age > SHADOW_DAYS:
                st["shadow"].remove(sh)

        for tf, a, _candles in tfs:
            if st.get("strategy") in cs.CASES:
                break                               # a promoted case is trading; the council steps aside
            bar_key = pair if tf == "H1" else f"{pair}:{tf}"
            if st["last_bar"].get(bar_key) == a["time"]:
                continue                            # already decided on this candle
            st["last_bar"][bar_key] = a["time"]
            if not a["setup"] or not a["atr"]:
                log.append(f"{pair}: no {'1-hour' if tf == 'H1' else '15-min'} setup on the {a['time'][11:16]} UTC candle")
                continue
            fx = fix_blocks(st, "setup:" + a["setup"], now.hour, now.weekday(), pair)
            if fx:
                log.append(f"{pair}: {a['setup']} skipped by Inspector's fix {fx['id']} ({fx['text']})")
                hindsight(st, f"Fix {fx['id']} kept the bot out of a {NAMES[pair]} {a['setup']} setup ({fx['text']}).", tg, "note")
                continue
            st["_changed"] = True

            # risk gate
            reasons = []
            if st.get("paused"):
                reasons.append(f"paused ({st['paused']})")
            if streak_status(st, now.timestamp()) == "timeout":
                reasons.append(f"cooling off after {st['streak']['losses']} losses in a row")
            if st["day"].get("halted"):
                reasons.append("daily loss limit hit")
            if pair in open_pairs:
                reasons.append("already in a trade on this pair")
            if len(st["trades"]) >= MAX_OPEN_TRADES:
                reasons.append(f"{MAX_OPEN_TRADES} trades already open")
            if not p.get("tradeable", False):
                reasons.append("market closed")
            same = sum(1 for m in st["trades"].values() if usd_direction(m["pair"], m["dir"]) == usd_direction(pair, a["trend"]))
            if same >= MAX_SAME_DOLLAR_BETS:
                reasons.append(f"already {same} trades betting the dollar goes {'up' if usd_direction(pair, a['trend']) == 1 else 'down'}")
            ts = now.timestamp()
            hit = next((e for e in news if e["cur"] in pair_currencies(pair)
                        and e["t"] - NEWS_BEFORE_MIN * 60 <= ts <= e["t"] + NEWS_AFTER_MIN * 60), None)
            if hit:
                reasons.append(f"news: {news_label(hit)}")
            bench = st.get("learn", {}).get("benched", {}).get(f"{pair}:{a['setup']}")
            if bench is not None:
                reasons.append(f"benched: {a['setup']} setups on {NAMES[pair]} have been losing ({bench:+.2f}R)")
            today["setups"] += 1
            if tf == "LB":
                st.setdefault("lb_done", {})[pair] = now.strftime("%Y-%m-%d")     # one breakout decision per pair per day
                votes = council_breakout(a, not reasons, "; ".join(reasons), now, pair, inf)
            else:
                votes = council(a, spread, not reasons, "; ".join(reasons), now, pair)
            d = a["trend"]
            entry = ask if d == 1 else bid
            entry = entry or a["close"]
            if a.get("sl_price") is not None:
                sl = a["sl_price"]
                tp = entry + d * (TARGET_ATR / STOP_ATR) * abs(entry - sl)
            else:
                sl = entry - d * STOP_ATR * a["atr"]
                tp = entry + d * TARGET_ATR * a["atr"]
            yes, no = tally(votes)
            ok = approved(votes, voter_weights(st))
            label = f"{NAMES[pair]} {'BUY' if d == 1 else 'SELL'} ({a['setup']})"
            st["last_vote"] = {"label": label, "side": "BUY" if d == 1 else "SELL", "approved": ok, "when": now.timestamp(),
                               "url": f"https://www.oanda.com/", "ask": 0, "edge": 0,
                               "detail": f"entry {fmt_price(entry, prec)} · stop {fmt_price(sl, prec)} · target {fmt_price(tp, prec)}",
                               "votes": {n: [o, why] for n, (o, why) in votes.items()},
                               "chart": {"closes": [round(x["c"], prec + 1) for x in _candles[-60:]], "entry": entry,
                                         "sl": sl, "tp": tp, "prec": prec, "tf": tf}}
            plain_votes = {n: o for n, (o, _) in votes.items()}
            hindsight(st, " ".join(x for x in (f"Council {'approved' if ok else 'rejected'} {label} {yes}–{no}.",
                                                 file_line(a["setup"]), paper_line(st, pair)) if x), tg, "vote")
            vote_entry = feed_add(st, "vote", label,
                             lines=[{"who": n, "vote": o, "say": say(n, o, why, a["time"] + pair)} for n, (o, why) in votes.items()],
                             verdict=(f"APPROVED {yes}–{no}. Placing the trade." if ok else
                                      f"REJECTED {yes}–{no}. Logged on paper so we learn whether we were right."), ok=ok)
            if tg and st.get("live_tg") and not ok:
                tg.say(transcript(vote_entry))      # approved trades already send their own message
            vote_text = "\n".join(f"{'✅' if o else '❌' if o is False else '➖'} {n}: {why}" for n, (o, why) in votes.items())

            if not ok:
                today["rejected"] += 1
                today["news"] += 1 if hit else 0
                today["benched"] += 1 if bench is not None else 0
                st["shadow"].append({"pair": pair, "dir": d, "bar": a["time"], "sl": sl, "tp": tp, "votes": plain_votes,
                                     "setup": a["setup"], "tf": tf})
                log.append(f"{pair}: council rejected {label} {yes}–{no}")
                continue

            # size the trade
            conv = usd_per_unit_move(pair, entry)
            if conv is None:
                log.append(f"{pair}: can't size (needs USD in the pair)"); continue
            risk_usd = nav * RISK_PCT * (0.5 if streak_status(st, now.timestamp()) != "normal" else 1)
            units = int(risk_usd / (abs(entry - sl) * conv))
            cap = int(nav * MAX_LEVERAGE / max(notional_usd(pair, 1, entry), 1e-9))
            units = min(units, cap)
            if units < max(1, float(inf.get("minimumTradeSize", 1))):
                log.append(f"{pair}: {label} skipped, the smallest allowed trade would risk more than {RISK_PCT:.0%} of a ${nav:,.0f} bankroll")
                vote_entry["verdict"] = f"APPROVED {yes}–{no}, but skipped: the smallest {NAMES[pair]} trade would risk more than {RISK_PCT:.0%} of ${nav:,.0f}."
                continue
            try:
                res = api.market_order(pair, d * units, fmt_price(sl, prec), fmt_price(tp, prec))
            except ApiError as e:
                log.append(f"{pair}: order rejected ({e})")
                if tg:
                    tg.say(f"⚠️ OANDA rejected the {label} order: {e}")
                continue
            fill = res.get("orderFillTransaction") or {}
            opened = fill.get("tradeOpened") or {}
            if not opened:
                reason = (res.get("orderCancelTransaction") or {}).get("reason", "unknown reason")
                log.append(f"{pair}: order not filled ({reason})")
                if tg:
                    tg.say(f"⚠️ {label} wasn't filled: {reason}")
                continue
            fill_px = float(opened.get("price", fill.get("price", entry)))
            vote_entry["verdict"] = (f"APPROVED {yes}–{no}. Filled {'BUY' if d == 1 else 'SELL'} {units:,} units at "
                                         f"{fmt_price(fill_px, prec)}, stop {fmt_price(sl, prec)}, target {fmt_price(tp, prec)}.")
            tid = str(opened["tradeID"])
            st["trades"][tid] = {"pair": pair, "dir": d, "units": units, "entry": fill_px, "sl": sl, "tp": tp,
                                 "opened": now.isoformat()[:19], "risk": round(risk_usd, 2), "votes": plain_votes,
                                 "setup": a["setup"], "tf": tf, "price": fill_px, "upl": 0.0}
            open_pairs.add(pair)
            today["approved"] += 1
            today["opened"] += 1
            log.append(f"{pair}: OPENED {label}, {units} units")
            if tg:
                tg.say(f"{'🟢 BUY' if d == 1 else '🔴 SELL'} {NAMES[pair]} — {a['setup']}\n\n"
                       f"Filled: {fmt_price(fill_px, prec)}  ·  {units:,} units\n"
                       f"🛑 Stop: {fmt_price(sl, prec)} ({pips(pair, abs(fill_px - sl), inf):.0f} pips)\n"
                       f"🎯 Target: {fmt_price(tp, prec)} ({pips(pair, abs(tp - fill_px), inf):.0f} pips)\n"
                       f"Risk: ${risk_usd:,.2f} ({RISK_PCT:.0%} of the bankroll)\n\n"
                       f"Council {yes}–{no}:\n{vote_text}")

    if full and st.get("pairs"):
        st["ticker"] = [{"s": NAMES[pr], "p": f"{st['pairs'][pr]['close']:.{st['pairs'][pr]['prec']}f}",
                         "c": round(st["pairs"][pr].get("chg", 0), 2)} for pr in pairs if pr in st["pairs"]]
        notes, setups = [], 0
        for pr in pairs:
            pp = st["pairs"].get(pr)
            if not pp:
                continue
            setups += 1 if pp.get("setup") else 0
            arrow = "▲" if pp["trend"] == 1 else "▼"
            if any(m["pair"] == pr for m in st["trades"].values()):
                what = "in a trade"
            elif pp.get("setup"):
                what = f"{pp['setup']} setup"
            elif (pp.get("adx") or 0) < ADX_MIN:
                what = "trend too weak, waiting"
            else:
                what = "trending, waiting for a pullback"
            notes.append({"who": NAMES[pr], "vote": None, "say": f"{arrow} ADX {(pp.get('adx') or 0):.0f}, RSI {(pp.get('rsi') or 0):.0f}: {what}"})
        feed_add(st, "scan", f"Scanned {len(notes)} market{'s' if len(notes) != 1 else ''}: {setups} setup{'s' if setups != 1 else ''} on the latest candle",
                 lines=notes)

    # 3b) test trade on request
    if st.get("test_pending"):
        tp_ = prices.get("EUR_USD", {})
        if not tp_.get("tradeable"):
            if not st.get("test_waiting_said") and tg:
                tg.say("🧪 Test trade waiting: EUR/USD is closed right now. I'll try again when it reopens.")
            st["test_waiting_said"] = True
        else:
            st.pop("test_waiting_said", None)
            ask_ = float(tp_["asks"][0]["price"])
            try:
                res = api.market_order("EUR_USD", 1, f"{ask_ - 0.0050:.5f}", f"{ask_ + 0.0050:.5f}")
                opened = (res.get("orderFillTransaction") or {}).get("tradeOpened") or {}
                if not opened:
                    raise ApiError((res.get("orderCancelTransaction") or {}).get("reason", "not filled"))
                tid = str(opened["tradeID"])
                closed = api.close(tid)
                cfill = closed.get("orderFillTransaction") or {}
                msg = (f"✅ Test trade worked.\nOpened BUY 1 unit EUR/USD at {float(opened.get('price', ask_)):.5f} "
                       f"(stop and target attached), closed at {float(cfill.get('price', 0) or 0):.5f}. "
                       f"Result: {money(float(cfill.get('pl', 0) or 0))}.\nOrders, stops and closing all work.")
                feed_add(st, "event", "Test trade", who="Chair", ok=True, text=msg.replace("\n", " "))
                log.append("Test trade: OK")
            except ApiError as e:
                msg = f"❌ Test trade failed: {e}\nThat tells us exactly what to fix. Send me this message."
                log.append(f"Test trade failed ({e})")
            if tg:
                tg.say(msg)
            st["test_pending"] = False

    if full:
        try:
            proving_checks(st, tg)
        except Exception as e:
            log.append(f"proving checks skipped ({e})")

    # 4) status reply
    if tg and st.pop("_status_requested", False):
        lines = [f"📊 {OANDA_ENV.upper()} bankroll: ${nav:,.2f}",
                 f"Today: {money(nav - st['day']['start_nav'])}  ·  Record {st['record']['wins']}W–{st['record']['losses']}L",
                 "Paused: " + (st["paused"] if st.get("paused") else "no")]
        for m in st["trades"].values():
            lines.append(f"• {NAMES[m['pair']]} {'BUY' if m['dir'] == 1 else 'SELL'} {m['units']:,}: {money(m.get('upl', 0))}")
        if not st["trades"]:
            lines.append("No open trades.")
        tg.say("\n".join(lines))

    now_live = [p for p in PAIRS if in_session(p, now)]
    if now_live:
        st.pop("recess", None)
    else:
        nt, _ = next_vote(now)
        st["recess"] = "resumes " + nt.astimezone(LOCAL_TZ).strftime("%a %I:%M %p ET").replace(" 0", " ")

    # 5) daily / weekly reports
    if tg:
        for text in due_reports(st, nav):
            tg.say(text)

    if tg:
        tg.flush(log)
    elif not tg_token:
        log.append("Telegram: off (add TELEGRAM_BOT_TOKEN)")
    save_state(st)
    return st


ERROR_MARKERS = ("failed", "rejected (", "unavailable", "skipped (", "not filled", "couldn't", "no chat", "OANDA: add")


def record_errors(log, st):
    """Keep the last 20 problems from the run log so the watchdog can report them."""
    errs = st.setdefault("errors", [])
    for line in log:
        if any(m in line for m in ERROR_MARKERS) and "council rejected" not in line and not line.startswith("Council:"):
            errs.append({"t": time.time(), "text": line[:300]})
    del errs[:-20]


# ───────────────────────────── Live council feed ─────────────────────────────

FEED_MAX = 80
PERSONA = {   # how each agent talks: (YES lines, NO lines); {why} is the actual reason
    "The Quant": (["Numbers check out: {why}.", "The data says go. {why}.", "Clean read: {why}."],
                  ["Data's too weak: {why}.", "I need more than this. {why}.", "Doesn't pass my screen: {why}."]),
    "Risk":      (["I can live with this. {why}.", "Exposure's fine. {why}.", "Within limits: {why}."],
                  ["Hard no. {why}.", "Not on my watch: {why}.", "Blocked. {why}."]),
    "Momentum":  (["It's moving our way. {why}.", "Feel that push? {why}.", "Engine's running: {why}."],
                  ["No juice here. {why}.", "Stalling out: {why}.", "Nothing pushing it. {why}."]),
    "Time":      (["Timing's right. {why}.", "Good window. {why}.", "Clock agrees: {why}."],
                  ["Not now. {why}.", "Bad timing: {why}.", "Wait for it. {why}."]),
    "Doubt":     (["...fine. {why}. I'll allow it.", "Hm. {why}. I can't argue.", "Begrudgingly yes: {why}."],
                  ["Something's off. {why}.", "I don't buy it: {why}.", "Too convenient. {why}."]),
}


def _pick(options, seed):
    return options[sum(map(ord, seed)) % len(options)]


def say(name, vote, why, seed=""):
    yes, no = PERSONA.get(name, (["{why}."], ["{why}."]))
    if vote is None:
        return f"Abstaining. {why}."
    return _pick(yes if vote else no, seed + name).format(why=why[0].upper() + why[1:] if why else "")


def feed_add(st, kind, title, lines=None, verdict=None, ok=None, who=None, text=None):
    """kind: vote | event | scan. A new scan entry replaces the previous scan entry if nothing happened in between."""
    feed = st.setdefault("feed", [])
    entry = {"t": time.time(), "kind": kind, "title": title, "lines": lines or [], "verdict": verdict, "ok": ok,
             "who": who, "text": text}
    if kind == "scan" and feed and feed[-1]["kind"] == "scan":
        feed[-1] = entry
    else:
        feed.append(entry)
    del feed[:-FEED_MAX]
    return entry


def transcript(entry):
    icon = {True: "✅", False: "❌", None: "➖"}
    out = [f"🏛 Council: {entry['title']}"]
    out += [f"{icon[l['vote']]} {l['who']}: {l['say']}" for l in entry["lines"]]
    if entry.get("verdict"):
        out += ["", f"⚖️ {entry['verdict']}"]
    return "\n".join(out)


# ───────────────────────────── Is it learning? ───────────────────────────────

def _avg(xs):
    return sum(xs) / len(xs) if xs else None


def learning_card(st):
    """The honest test: do the trades the council approves beat the ones it rejects, and is it improving?"""
    oc = st.get("learn", {}).get("outcomes", [])
    took = [o["r"] for o in oc if o["approved"]][-50:]
    skip = [o["r"] for o in oc if not o["approved"]][-50:]
    a_took, a_skip = _avg(took), _avg(skip)
    recent, before = [o["r"] for o in oc if o["approved"]][-20:], [o["r"] for o in oc if o["approved"]][-40:-20]
    if len(took) < 10 or len(skip) < 10:
        verdict, cls = (f"Not enough results yet ({len(took)} trades, {len(skip)} rejected setups scored; "
                        "the test needs 10 of each)."), "wait"
    elif a_took > a_skip + 0.1:
        verdict, cls = "The council is adding value: the trades it takes beat the ones it skips.", "good"
    elif a_took > a_skip - 0.1:
        verdict, cls = "No clear difference yet between what it takes and what it skips. Keep watching.", "wait"
    else:
        verdict, cls = "The council isn't helping right now: rejected setups are doing better than approved trades.", "bad"
    trend = None
    if len(recent) >= 10 and len(before) >= 10:
        delta = _avg(recent) - _avg(before)
        trend = (f"{'Improving' if delta > 0.05 else 'Slipping' if delta < -0.05 else 'Holding steady'}: "
                 f"last {len(recent)} trades average {_avg(recent):+.2f}R vs {_avg(before):+.2f}R before")
    return {"took": a_took, "skip": a_skip, "n_took": len(took), "n_skip": len(skip),
            "win": (sum(1 for r in took if r > 0) / len(took)) if took else None,
            "verdict": verdict, "cls": cls, "trend": trend}


def inspector_link():
    """A card linking Inspector's backtest report, once it has run."""
    try:
        g = json.load(open("inspector.json"))
    except (OSError, ValueError):
        return ""
    t = g["grade"]["trades"]
    when = dt.datetime.fromtimestamp(g["when"], LOCAL_TZ).strftime("%b %d")
    avg = t.get("avg")
    return f"""<section class="learnwrap"><h2>🕵️ Inspector's backtest</h2>
  <p>{g['grade']['months']:g} months replayed ({when}): {t['n']} trades, {('%+.2fR' % avg) if avg is not None else '—'} per trade.
  <a href="office.html">Visit Inspector's Office →</a> · <a href="inspector.html">full report</a></p></section>"""


def learning_html(st):
    c = learning_card(st)
    f = lambda x: "—" if x is None else f"{x:+.2f}R"
    return f"""<section class="learnwrap"><h2>Is it learning?</h2>
  <p class="lv {c['cls']}">{html.escape(c['verdict'])}</p>
  <div class="lgrid">
    <div><span>Trades it took</span><b class="{'pos' if (c['took'] or 0) > 0 else 'neg' if c['took'] is not None else ''}">{f(c['took'])}</b><small>avg of {c['n_took']}</small></div>
    <div><span>Setups it skipped</span><b>{f(c['skip'])}</b><small>avg of {c['n_skip']} (scored on paper)</small></div>
    <div><span>Win rate</span><b>{'—' if c['win'] is None else f"{c['win']:.0%}"}</b><small>needs ~40% at 1.5R targets to profit</small></div>
  </div>
  {f'<p class="muted">{html.escape(c["trend"])}</p>' if c['trend'] else ''}
  <p class="fine">R = profit measured in units of risk. +1.5R is a full win, −1R a full loss. The council earns its keep only if
  "took" beats "skipped". Vote weights adapt after {LEARN_MIN_CALLS} scored calls, and losing pair + setup combos get benched.</p>
</section>"""


# ───────────────────────────── Losing-streak breaker ─────────────────────────

def streak_update(st, r_mult, ts):
    """Count losses in a row. Half size at STREAK_HALF, a 24h timeout at STREAK_TIMEOUT, full size after wins."""
    sk = st.setdefault("streak", {"losses": 0, "half": False, "wins_since": 0, "until": 0})
    out = []
    if abs(r_mult) < 0.1:
        return out                                   # breakeven scratch: doesn't count either way
    if r_mult > 0:
        sk["losses"] = 0
        if sk["half"]:
            sk["wins_since"] += 1
            if sk["wins_since"] >= STREAK_RECOVER_WINS:
                sk.update(half=False, wins_since=0)
                out.append(f"💪 {STREAK_RECOVER_WINS} wins since the slump. Back to full size ({RISK_PCT:.0%} per trade).")
                feed_add(st, "event", "Back to full size", who="Risk", ok=True, text=out[-1][2:])
        return out
    sk["losses"] += 1
    sk["wins_since"] = 0
    n = sk["losses"]
    if n == STREAK_HALF:
        sk["half"] = True
        out.append(f"⚠️ {n} losses in a row. Cutting trade size in half ({RISK_PCT / 2:.1%} per trade) until "
                   f"{STREAK_RECOVER_WINS} wins. Streaks like this happen even when a strategy works; this keeps the damage small.")
        feed_add(st, "event", "Half size", who="Risk", ok=False, text=out[-1][2:])
    elif n >= STREAK_TIMEOUT and ts >= sk.get("until", 0):
        sk["until"] = ts + STREAK_TIMEOUT_HOURS * 3600
        sk["half"] = True
        back = dt.datetime.fromtimestamp(sk["until"], LOCAL_TZ).strftime("%a %I:%M %p ET").replace(" 0", " ")
        out.append(f"🧊 {n} losses in a row. Timeout: no new trades until {back}. Open trades keep their stops, "
                   "and the council keeps voting on paper so it keeps learning. Comes back at half size. /resume to end it early.")
        feed_add(st, "event", "Timeout", who="Risk", ok=False, text=out[-1][2:])
    return out


def streak_status(st, ts=None):
    sk = st.get("streak") or {}
    ts = ts or time.time()
    if sk.get("until", 0) > ts:
        return "timeout"
    return "half" if sk.get("half") else "normal"


# ───────────────────────────── Reports ───────────────────────────────────────

def _learning_lines(st):
    w = voter_weights(st)
    lowest = min((st.get("scoreboard", {}).get(n, {}).get("total", 0) for n in VOTERS), default=0)
    if all(x == 1.0 for x in w.values()):
        voters = f"Voter weights: all equal until each has {LEARN_MIN_CALLS} scored calls (lowest so far: {lowest})"
    else:
        voters = "Voter weights: " + " · ".join(f"{n} {w[n]:g}×" for n in sorted(VOTERS, key=lambda n: -w[n]))
    benched = st.get("learn", {}).get("benched", {})
    bench = ("Benched: " + ", ".join(f"{NAMES[c.split(':')[0]]} {c.split(':')[1]}" for c in benched)) if benched else "Benched: nothing"
    return voters, bench


def _closed_between(st, start, end):
    return [t for t in st.get("closed", []) if start <= t.get("closed_ts", 0) < end]


def _upcoming(st, hours):
    now = time.time()
    cur = set().union(*(pair_currencies(p) for p in PAIRS))
    evs = [e for e in st.get("news", {}).get("events", []) if e["cur"] in cur and now <= e["t"] <= now + hours * 3600]
    return "\n".join("   • " + news_label(e) for e in evs[:6]) or "   • nothing high-impact on the calendar"


def daily_report(st, day, nav):
    d = st.get("days", {}).get(day, {})
    start = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ).timestamp()
    closed = _closed_between(st, start, start + 86400)
    w = sum(1 for t in closed if t.get("pl", 0) > 0)
    pl = sum(t.get("pl", 0) for t in closed)
    change = nav - d["start_nav"] if d.get("start_nav") is not None else None
    voters, bench = _learning_lines(st)
    learned = [x["text"] for x in st.get("learn", {}).get("log", []) if x["t"] >= start]
    open_lines = [f"   • {NAMES[m['pair']]} {'BUY' if m['dir'] == 1 else 'SELL'}: {money(m.get('upl', 0))}"
                  + (" (🔒 breakeven)" if m.get("breakeven") else "") for m in st.get("trades", {}).values()]
    lines = [f"📅 Vault FX daily report: {dt.datetime.strptime(day, '%Y-%m-%d'):%a %b %d}", "",
             f"💰 Bankroll: ${nav:,.2f}" + (f" (today {money(change)})" if change is not None else ""),
             f"📈 Trades: {d.get('opened', 0)} opened · {len(closed)} closed"
             + (f" ({w}W–{len(closed) - w}L, {money(pl)})" if closed else ""),
             "📂 Open now:" if open_lines else "📂 Open now: none", *open_lines,
             f"🏛 Council: {d.get('setups', 0)} setups, {d.get('approved', 0)} approved, {d.get('rejected', 0)} rejected"
             + (f" ({d.get('news', 0)} for news, {d.get('benched', 0)} benched)" if d.get('rejected') else ""),
             "", "🧠 " + voters, "   " + bench]
    lines += ["   " + t.replace("🧠 ", "") for t in learned]
    lines += ["", "📰 Big news in the next 24h:", _upcoming(st, 24)]
    ss = streak_status(st)
    if ss != "normal":
        sk = st.get("streak", {})
        lines += ["", "🧊 Timeout after a losing streak, back " + dt.datetime.fromtimestamp(sk["until"], LOCAL_TZ).strftime("%a %I:%M %p ET")
                  if ss == "timeout" else f"⚠️ Half size until {STREAK_RECOVER_WINS - sk.get('wins_since', 0)} more win(s)"]
    md = st.get("mood", {}).get(day, {})
    if md.get("happy") or md.get("unhappy"):
        lines += ["", f"🎖 Your feedback today: 🥂 {md.get('happy', 0)} happy · 👎 {md.get('unhappy', 0)} not happy"]
    if st.get("paused"):
        lines += ["", f"⏸ Paused ({st['paused']}). Send /resume to restart."]
    return "\n".join(lines)


def weekly_report(st, day, nav):
    end = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ).timestamp() + 86400
    start = end - 7 * 86400
    closed = _closed_between(st, start, end)
    w = sum(1 for t in closed if t.get("pl", 0) > 0)
    pl = sum(t.get("pl", 0) for t in closed)
    avg_r = sum(t.get("r", 0) for t in closed) / len(closed) if closed else 0
    by_pair = {}
    for t in closed:
        by_pair[t["pair"]] = by_pair.get(t["pair"], 0) + t.get("pl", 0)
    hist = [h for h in st.get("history", []) if start <= h["t"] < end]
    days = [v for k, v in st.get("days", {}).items()
            if start <= dt.datetime.strptime(k, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ).timestamp() < end]
    tot = {k: sum(d_.get(k, 0) for d_ in days) for k in ("setups", "approved", "rejected", "news", "benched")}
    starts = [d_["start_nav"] for d_ in days if d_.get("start_nav") is not None]
    week_change = nav - starts[0] if starts else None
    board = st.get("scoreboard", {})
    ranked = sorted([(n, b["right"] / b["total"], b["total"]) for n, b in board.items() if b.get("total", 0) >= 5],
                    key=lambda x: -x[1])
    rec, cr = st.get("record", {}), st.get("council_record", {"right": 0, "total": 0})
    voters, bench = _learning_lines(st)
    learned = [x["text"] for x in st.get("learn", {}).get("log", []) if start <= x["t"] < end]
    lines = [f"📊 Vault FX weekly report (week ending {dt.datetime.strptime(day, '%Y-%m-%d'):%b %d})", "",
             f"💰 Bankroll: ${nav:,.2f}" + (f" (week {money(week_change)})" if week_change is not None else ""),
             (f"📈 Closed: {w}W–{len(closed) - w}L ({w / len(closed):.0%} win rate), {money(pl)}, avg {avg_r:+.2f}R"
              if closed else "📈 Closed: no trades this week")]
    for pr, v in sorted(by_pair.items(), key=lambda x: -x[1]):
        lines.append(f"   {NAMES[pr]}: {money(v)}")
    lines += [f"🏛 Setups: {tot['setups']} · approved {tot['approved']} · rejected {tot['rejected']} "
              f"({tot['news']} for news, {tot['benched']} benched)",
              f"   Council calls scored this week: {sum(1 for h in hist if h['right'])}/{len(hist)} right"
              if hist else "   Council calls scored this week: none yet",
              f"   Council all-time: {cr['right'] / cr['total']:.0%} ({cr['right']}/{cr['total']})" if cr["total"] else "   Council all-time: no scored calls yet",
              ""]
    if ranked:
        lines.append(f"⭐ Sharpest voter: {ranked[0][0]} ({ranked[0][1]:.0%} of {ranked[0][2]})")
        if len(ranked) > 1:
            lines.append(f"🤔 Least reliable: {ranked[-1][0]} ({ranked[-1][1]:.0%} of {ranked[-1][2]})")
    lc = learning_card(st)
    lines += ["🧠 " + voters, "   " + bench, "   Is it learning? " + lc["verdict"]]
    if lc["took"] is not None and lc["skip"] is not None:
        lines.append(f"   Took {lc['took']:+.2f}R avg ({lc['n_took']}) vs skipped {lc['skip']:+.2f}R avg ({lc['n_skip']})")
    if lc["trend"]:
        lines.append("   " + lc["trend"])
    lines += ["   " + t.replace("🧠 ", "") for t in learned]
    lines += ["", f"All-time: {rec.get('wins', 0)}W–{rec.get('losses', 0)}L, {money(rec.get('pl', 0))}"]
    return "\n".join(lines)


def due_reports(st, nav, now=None):
    now_et = dt.datetime.fromtimestamp(now or time.time(), LOCAL_TZ)
    day = now_et.strftime("%Y-%m-%d")
    out = []
    if now_et.hour < REPORT_HOUR_ET or now_et.weekday() == 5:
        return out
    if st.get("last_daily") != day:
        out.append(daily_report(st, day, nav)); st["last_daily"] = day
    if now_et.weekday() == WEEKLY_DAY and st.get("last_weekly") != day:
        out.append(weekly_report(st, day, nav)); st["last_weekly"] = day
    return out


# ───────────────────────────── Council room (3D) ─────────────────────────────

CHARACTERS = {
    # name: (color token, svg body)
    "The Quant": ("--c-quant", '<rect x="18" y="20" width="64" height="70" rx="6" fill="var(--c-quant)"/>'
                  '<rect x="28" y="38" width="18" height="13" rx="3" fill="none" stroke="var(--ink)" stroke-width="3"/>'
                  '<rect x="54" y="38" width="18" height="13" rx="3" fill="none" stroke="var(--ink)" stroke-width="3"/>'
                  '<line x1="46" y1="44" x2="54" y2="44" stroke="var(--ink)" stroke-width="3"/>'
                  '<circle cx="37" cy="45" r="2.5" fill="var(--ink)"/><circle cx="63" cy="45" r="2.5" fill="var(--ink)"/>'
                  '<path d="M38 68 h24" stroke="var(--ink)" stroke-width="3" stroke-linecap="round"/>'),
    "Risk": ("--c-risk", '<path d="M50 8 L58 22 L72 14 L72 30 L88 32 L78 46 L90 58 L76 64 L80 82 L64 78 L56 92 L50 80 '
             'L44 92 L36 78 L20 82 L24 64 L10 58 L22 46 L12 32 L28 30 L28 14 L42 22 Z" fill="var(--c-risk)"/>'
             '<circle cx="41" cy="46" r="7" fill="#fff"/><circle cx="59" cy="46" r="7" fill="#fff"/>'
             '<circle cx="43" cy="47" r="3" fill="var(--ink)"/><circle cx="57" cy="47" r="3" fill="var(--ink)"/>'
             '<path d="M33 37 l14 4 M67 37 l-14 4" stroke="var(--ink)" stroke-width="3" stroke-linecap="round"/>'
             '<path d="M42 66 q8 -6 16 0" fill="none" stroke="var(--ink)" stroke-width="3" stroke-linecap="round"/>'),
    "Momentum": ("--c-mom", '<path d="M6 40 h14 M2 52 h16 M6 64 h14" stroke="var(--c-mom)" stroke-width="4" stroke-linecap="round" opacity=".6"/>'
                 '<circle cx="56" cy="52" r="34" fill="var(--c-mom)"/>'
                 '<rect x="36" y="16" width="40" height="8" rx="2" fill="var(--ink)"/><rect x="44" y="2" width="24" height="18" rx="3" fill="var(--ink)"/>'
                 '<circle cx="47" cy="50" r="4" fill="var(--ink)"/><circle cx="67" cy="50" r="4" fill="var(--ink)"/>'
                 '<path d="M46 66 q10 8 20 0" fill="none" stroke="var(--ink)" stroke-width="3" stroke-linecap="round"/>'),
    "Time": ("--c-time", '<circle cx="50" cy="54" r="36" fill="var(--c-time)"/><circle cx="50" cy="54" r="27" fill="#fff" opacity=".9"/>'
             '<path class="hand" d="M50 54 V34" stroke="var(--ink)" stroke-width="4" stroke-linecap="round"/>'
             '<path d="M50 54 h12" stroke="var(--ink)" stroke-width="4" stroke-linecap="round"/>'
             '<circle cx="50" cy="54" r="3.5" fill="var(--ink)"/>'
             '<rect x="44" y="10" width="12" height="9" rx="2" fill="var(--c-time)"/>'),
    "Doubt": ("--c-doubt", '<ellipse cx="50" cy="56" rx="30" ry="38" fill="var(--c-doubt)"/>'
              '<circle cx="40" cy="48" r="4" fill="var(--ink)"/><circle cx="60" cy="48" r="4" fill="var(--ink)"/>'
              '<path d="M32 36 l14 -2 M54 32 l14 6" stroke="var(--ink)" stroke-width="3" stroke-linecap="round"/>'
              '<path d="M42 70 h16" stroke="var(--ink)" stroke-width="3" stroke-linecap="round"/>'
              '<path d="M68 64 q10 -2 10 8" fill="none" stroke="var(--ink)" stroke-width="2.5" stroke-linecap="round" opacity=".7"/>'),
}


SOLO = {   # personality lines, mixed in with the live commentary below
    "The Quant": ["Running the numbers again…", "Win rate times payoff. That's the whole game.", "I trust data, not feelings.",
                  "Every skipped setup gets scored too. Keeps us honest."],
    "Risk": ["Stops are set. Nobody panics.", "Protect the bankroll first, profits second.", "One bad trade won't sink us. Ten might.",
             "1% risk per trade. No exceptions."],
    "Momentum": ["Come on, somebody move!", "I can feel a breakout coming.", "The trend is my friend.", "Sitting still is killing me."],
    "Time": ["Patience. The next candle decides.", "London open is my favorite hour.", "Good trades wait for good timing.",
             "Tick, tock. Not yet."],
    "Doubt": ["Hmm. Too quiet.", "What if we're all wrong?", "I'll believe it when it pays.", "Somebody check the news calendar."],
}
OFF_DUTY = {
    "The Quant": ["Using the downtime to recheck last week's trades.", "Market's closed. Spreadsheets are open."],
    "Risk": ["Nothing can hurt us while the market's closed. I love it.", "Off duty, but I'm still watching."],
    "Momentum": ["Nothing moves on a closed market. I'm so bored.", "Anyone up for darts?"],
    "Time": ["Back in session soon. Don't be late.", "Clocks don't sleep, even when markets do."],
    "Doubt": ["Enjoy the quiet. Monday always surprises.", "Rest up. The open is never polite."],
}


def chatter(st):
    """What each officer mutters while walking around, plus little two-person conversations, from live state."""
    lines = {n: [] for n in VOTERS}
    dialogs = []
    acc = st.get("account", {})
    nav, rec = acc.get("nav"), st.get("record", {"wins": 0, "losses": 0, "pl": 0.0})
    recess = st.get("recess")
    trades = list(st.get("trades", {}).values())
    for t in trades[:3]:
        nm, side, upl = NAMES.get(t["pair"], t["pair"]), "BUY" if t["dir"] == 1 else "SELL", t.get("upl", 0) or 0
        lines["The Quant"].append(f"{nm} {side} is {money(upl)} so far.")
        if t.get("breakeven"):
            lines["Risk"].append(f"{nm} can't lose now. Stop's at entry.")
        else:
            lines["Risk"].append(f"Watching the {nm} stop like a hawk.")
        if upl >= 0:
            dialogs.append(["Momentum", f"{nm} is running our way!", "Risk", "Don't celebrate yet. Let it hit the target."])
        else:
            dialogs.append(["Doubt", f"{nm} is under water.", "The Quant", "One trade. The edge shows up over many."])
    if not trades and not recess:
        lines["Momentum"].append("No trades open. My feet are itching.")
    if nav is not None:
        lines["The Quant"].append(f"Bankroll's at {money(nav)}.")
    day = st.get("day", {})
    if nav is not None and day.get("start_nav") is not None:
        lines["The Quant"].append(f"Today we're {money(nav - day['start_nav'])}.")
    if rec["wins"] + rec["losses"]:
        lines["Doubt"].append(f"{rec['wins']}W–{rec['losses']}L so far. Let's see if it holds.")
        dialogs.append(["The Quant", f"We're {rec['wins']}W–{rec['losses']}L.", "Risk", "And the bankroll's still protected. That's what matters."])
    ss = streak_status(st)
    if ss == "half":
        lines["Risk"].append("Half size until we string two wins together.")
    elif ss == "timeout":
        lines["Risk"].append("Timeout. Nobody trades until I say so.")
        dialogs.append(["Momentum", "Can we trade yet?", "Risk", "No. Losing streak timeout. Cool off."])
    for p in PAIRS:
        r = (st.get("pairs", {}).get(p) or {}).get("ready")
        if not r or r.get("in_trade") or not r.get("session"):
            continue
        side = "BUY" if r["dir"] == 1 else "SELL"
        if not r.get("waiting"):
            lines["Time"].append(f"{NAMES[p]} could get a vote at the next candle.")
            dialogs.append(["Momentum", f"{NAMES[p]} is lining up for a {side}!", "Doubt", "Or it fakes us out again. We'll see."])
        elif r.get("passed", 0) >= r.get("of", 9) - 2:
            lines["Momentum"].append(f"{NAMES[p]} is close: {r['passed']}/{r['of']} checks for a {side}.")
    now = time.time()
    news = [e for e in st.get("news", {}).get("events", []) if now <= e["t"] <= now + 4 * 3600]
    if news:
        e = news[0]
        when = dt.datetime.fromtimestamp(e["t"], LOCAL_TZ).strftime("%I:%M %p").lstrip("0")
        lines["Risk"].append(f"Heads up: {e['cur']} {e['title']} at {when}. No new trades around it.")
        dialogs.append(["Time", f"{e['cur']} {e['title']} lands at {when}.", "Risk", "Then we sit on our hands until it passes."])
    lv = st.get("last_vote")
    if lv:
        for n, (ok, why) in (lv.get("votes") or {}).items():
            if n in lines and ok is not None:
                short = (lv.get("label") or "the last one").split(" (")[0]
                reason = why.split(";")[0].split(",")[0].strip()
                reason = reason if len(reason) <= 48 else reason[:46].rsplit(" ", 1)[0] + "…"
                lines[n].append(f"I voted {'YES' if ok else 'NO'} on {short}: {reason}.")
    board = st.get("scoreboard", {})
    for n in VOTERS:
        b = board.get(n, {})
        if b.get("total", 0) >= 5:
            lines[n].append(f"I've called {b['right'] / b['total']:.0%} of my votes right.")
    L = st.get("learn", {})
    for combo in list(L.get("benched", {}))[:2]:
        pr, stp = combo.split(":", 1)
        lines["Doubt"].append(f"Told you {stp} on {NAMES.get(pr, pr)} was trouble. It's benched.")
    for combo, avg in list(L.get("proven", {}).items())[:2]:
        pr, stp = combo.split(":", 1)
        lines["The Quant"].append(f"{stp} on {NAMES.get(pr, pr)} is a proven theory ({avg:+.2f}R).")
        dialogs.append(["Momentum", f"{stp} on {NAMES.get(pr, pr)} keeps paying!", "The Quant", f"{avg:+.2f}R average. The data agrees."])
    dialogs += [
        ["Doubt", "What if the market changed and our rules didn't?", "The Quant", "That's why we score every setup we skip."],
        ["Momentum", "Can we just trade something?", "Time", "Not until the setup's there. Patience pays."],
        ["Risk", "Remember: 1% per trade.", "Momentum", "Yeah, yeah. Boring, but alive."],
        ["Time", "Next candle's coming up.", "Doubt", "And probably nothing again. That's fine."],
        ["The Quant", "Our rejected setups get scored too.", "Doubt", "Good. Keeps us honest."],
    ]
    if recess:
        for n in VOTERS:
            lines[n] = OFF_DUTY[n] + lines[n][:1]
        dialogs = [["Momentum", "Anyone up for darts while the market's closed?", "Time", f"Sure. Court {recess}. Don't be late."],
                   ["The Quant", "Want to review last week?", "Doubt", "Only if we're honest about the losers."],
                   ["Risk", "Closed market, zero risk.", "Momentum", "Zero fun, too."]] + dialogs[:2]
    for n in VOTERS:
        lines[n] += SOLO[n]
    return lines, dialogs


def council_room(state):
    lv = state.get("last_vote")
    board = state.get("scoreboard", {})
    cr = state.get("council_record", {"right": 0, "total": 0})
    overall = f"{cr['right'] / cr['total']:.0%}" if cr["total"] else "—"
    overall_sub = (f"{cr['right']} of {cr['total']} settled calls right" if cr["total"]
                   else "fills in as markets settle")
    seats, voters = [], []
    for i, name in enumerate(VOTERS):
        tok, body = CHARACTERS[name]
        ok, why = (lv["votes"].get(name, [None, "—"]) if lv else [None, "waiting for a trade"])
        sign = {True: "YES", False: "NO", None: "—"}[ok]
        cls = {True: "yes", False: "no", None: "abs"}[ok]
        b = board.get(name, {"right": 0, "total": 0})
        acc = f"{b['right'] / b['total']:.0%} right" if b["total"] else "no record yet"
        voters.append({"name": name, "vote": ok, "why": why, "acc": acc})
        seats.append(f"""<button class="seat" data-i="{i}" style="--i:{i}" aria-label="{name}: {sign}. {html.escape(why)}">
          <span class="sign {cls}">{sign}</span>
          <svg class="char" viewBox="0 0 100 100" aria-hidden="true">{body}</svg>
          <span class="nm">{name}</span><span class="acc">{acc}</span>
          <span class="why">{html.escape(why)}</span></button>""")
    verdict = None
    if lv:
        yes = sum(1 for ok, _ in lv["votes"].values() if ok is True)
        no = sum(1 for ok, _ in lv["votes"].values() if ok is False)
        verdict = f"{'APPROVED' if lv['approved'] else 'REJECTED'} {yes}–{no}"
        when = dt.datetime.fromtimestamp(lv["when"], LOCAL_TZ).strftime("%a %I:%M %p")
        docket = (f'<a href="{html.escape(lv["url"])}" target="_blank" rel="noopener">{html.escape(lv["label"])}</a>'
                  + (f'<small>{html.escape(lv["detail"])} · voted {when} (<span class="ago" data-t="{int(lv["when"])}"></span>)</small>' if lv.get("detail") else
                  f'<small>BUY {lv["side"]} @ {round(lv["ask"] * 100)}¢ · edge {round(lv["edge"] * 100)}¢ · voted {when}</small>'))
        stamp = f'<div class="stamp {"ok" if lv["approved"] else "rej"}">{verdict}</div>'
    else:
        docket = "<span>No trade on the table yet.</span><small>The council votes when a market clears your minimum edge.</small>"
        stamp = ""
    talk, dialogs = chatter(state)
    try:
        nv = int(next_vote(dt.datetime.now(dt.timezone.utc))[0].timestamp())
    except Exception:
        nv = 0
    data = json.dumps({"voters": voters, "verdict": verdict, "approved": bool(lv and lv["approved"]),
                       "title": (lv or {}).get("label"), "chart": (lv or {}).get("chart"),
                       "ticker": state.get("ticker", []), "recess": state.get("recess"),
                       "chatter": talk, "dialogs": dialogs, "lastVoteT": int(lv["when"]) if lv else 0,
                       "nextVote": nv, "scanT": int(state.get("last_run") or time.time()),
                       "parties": [p_ for p_ in state.get("parties", []) if time.time() - p_["t"] < 172800][-3:]})
    script = ROOM_JS.replace("__DATA__", data.replace("</", "<\\/"))
    return f"""<section class="room" id="room3d" aria-label="Council room">
  <div class="roomhead">
    <div class="overall"><span class="lbl">Council accuracy</span><b>{overall}</b><small>{overall_sub}</small></div>
    <div class="docket"><span class="lbl">{'Last vote' if lv else 'On the table'}</span>{docket}
      {'<small class="stale" id="stalenote" hidden>No new setup since then. The council only meets when a pair has a setup, so this stays up until the next one.</small>' if lv else ''}</div>
  </div>
  <script>(function(){{function f(){{document.querySelectorAll('.docket .ago').forEach(function(e){{var m=Math.round((Date.now()/1000-(+e.dataset.t))/60);
    e.textContent=m<1?'just now':m<60?m+' min ago':Math.floor(m/60)+'h '+(m%60)+'m ago';
    var n=document.getElementById('stalenote');if(n)n.hidden=m<16;}});}}f();setInterval(f,30000);}})();</script>
  <div class="stage" id="stage3d"><div class="stamp3d">{stamp}</div><div class="tip3d" id="tip3d" hidden></div></div>
  <div class="floor">
    <div class="table"><div class="gavel" aria-hidden="true"></div>{stamp}</div>
    <div class="seats">{"".join(seats)}</div>
  </div>
  <div class="mood" role="group" aria-label="Tell the council how you feel">
    <button type="button" class="mood-btn good" data-k="cheers">🥂 Happy</button>
    <button type="button" class="mood-btn bad" data-k="sad">👎 Not happy</button>
    <span class="mood-msg" id="moodmsg" aria-live="polite"></span>
  </div>
  {f'<p class="mood-tg">Want it on the record? <a href="https://t.me/{state["bot_username"]}?start=happy" target="_blank" rel="noopener">Send 🥂 to the bot</a> · <a href="https://t.me/{state["bot_username"]}?start=unhappy" target="_blank" rel="noopener">send 👎</a></p>' if state.get("bot_username") else ''}
  <div class="roomctl"><button type="button" id="callOrder">📣 Call to order</button>
    <button type="button" id="partyBtn" hidden>🎉 Replay party</button></div>
  <p class="hint">Between votes the officers mingle and talk. When a vote is about a minute away they take their seats, and when a theory is proven they throw a party. Tap an officer to read their last vote. Drag to look around.</p>
  <script>(function(){{var msg=document.getElementById('moodmsg'),room=document.getElementById('room3d'),busy=0;
    var lines={{cheers:['🥂 Cheers, Commander! The council raises a glass.','🥂 To the next one!','🥂 Hooah! Drinks are on The Quant.'],
               sad:['😔 Understood, Commander. We’ll tighten up.','😔 Heard. Back to the drawing board.','😔 Noted. Discipline over hope.']}};
    room.querySelectorAll('.mood-btn').forEach(function(b){{b.addEventListener('click',function(){{
      var k=b.dataset.k,now=Date.now();if(now-busy<1500)return;busy=now;
      if(window.councilReact)window.councilReact(k);
      var l=lines[k];msg.textContent=l[Math.floor(Math.random()*l.length)];msg.className='mood-msg show '+k;
      if(!room.classList.contains('is3d')){{room.classList.remove('flat-cheers','flat-sad');void room.offsetWidth;room.classList.add(k==='cheers'?'flat-cheers':'flat-sad');}}
      clearTimeout(msg._t);msg._t=setTimeout(function(){{msg.className='mood-msg';}},5000);
    }});}});}})();</script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/objects/Reflector.js"></script>
  <script>{script}</script>
</section>"""


ROOM_JS = r"""
(function () {
  var D = __DATA__;
  if (D.recess) D.verdict = null;          // court adjourned: everyone stays seated
  var room = document.getElementById('room3d'), stage = document.getElementById('stage3d'), tip = document.getElementById('tip3d');
  var stampEl = stage.querySelector('.stamp3d');
  if (!window.THREE) return;
  var renderer;
  try { renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false }); } catch (e) { return; }
  if (!renderer.getContext()) return;
  room.classList.add('is3d');
  var still = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var small = (stage.clientWidth || 360) < 520;
  renderer.setPixelRatio(Math.min(small ? 1.75 : 2, window.devicePixelRatio || 1));
  renderer.setClearColor(0x05060d, 1);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  stage.insertBefore(renderer.domElement, stage.firstChild);

  var scene = new THREE.Scene();
  scene.fog = new THREE.Fog(0x05060d, 11, 24);
  var cam = new THREE.PerspectiveCamera(38, 1, 0.1, 100);
  var world = new THREE.Group(); scene.add(world);

  // ── lighting: dim room, hard key light with shadows, colored rims, glow from the holotable
  scene.add(new THREE.HemisphereLight(0x8aa2ff, 0x140c24, 0.6)); scene.add(new THREE.AmbientLight(0x404a70, 0.35));
  var key = new THREE.SpotLight(0xdcebff, 2.3, 28, 0.7, 0.5, 1.0);
  key.position.set(-2.5, 9.5, 5.5); key.target.position.set(0, 0.5, -1.6);
  key.castShadow = true; key.shadow.mapSize.set(small ? 1024 : 2048, small ? 1024 : 2048); key.shadow.bias = -0.0004;
  key.shadow.camera.near = 3; key.shadow.camera.far = 22;
  scene.add(key); scene.add(key.target);
  var rimC = new THREE.DirectionalLight(0x22d3ee, 0.8); rimC.position.set(-6, 3, -6); scene.add(rimC);
  var rimM = new THREE.DirectionalLight(0xc026d3, 0.65); rimM.position.set(6, 3, -5); scene.add(rimM);
  var holoLight = new THREE.PointLight(0x38e1ff, 1.0, 7, 2); holoLight.position.set(0, 1.5, 1.0); scene.add(holoLight);
  var verdictLight = new THREE.PointLight(0x53f0a6, 0, 12, 2); verdictLight.position.set(0, 3.5, 0.5); scene.add(verdictLight);

  var METAL = new THREE.MeshStandardMaterial({ color: 0x2a2f45, metalness: 0.9, roughness: 0.28 });
  var METAL2 = new THREE.MeshStandardMaterial({ color: 0x40465f, metalness: 0.85, roughness: 0.35 });
  var STEEL = new THREE.MeshStandardMaterial({ color: 0x1a1f30, metalness: 0.8, roughness: 0.35 });
  var GLASS = new THREE.MeshStandardMaterial({ color: 0x2a3b66, metalness: 0.1, roughness: 0.05, transparent: true, opacity: 0.28 });
  function glowMat(c) { return new THREE.MeshBasicMaterial({ color: c }); }
  function basic(c, o) { return new THREE.MeshBasicMaterial({ color: c, transparent: true, opacity: o === undefined ? 1 : o, depthWrite: false }); }
  function addv(c, o) { return new THREE.MeshBasicMaterial({ color: c, transparent: true, opacity: o, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide }); }
  function M(g, m) { return new THREE.Mesh(g, m); }
  function at(m, x, y, z) { m.position.set(x, y, z); world.add(m); return m; }
  function shadowy(o, cast, recv) { o.traverse(function (k) { if (k.isMesh) { k.castShadow = cast; k.receiveShadow = recv; } }); return o; }

  // soft glow sprite (fake bloom)
  var GLOW = (function () {
    var c = document.createElement('canvas'); c.width = c.height = 128; var x = c.getContext('2d');
    var g = x.createRadialGradient(64, 64, 0, 64, 64, 64);
    g.addColorStop(0, 'rgba(255,255,255,1)'); g.addColorStop(0.25, 'rgba(255,255,255,0.55)'); g.addColorStop(1, 'rgba(255,255,255,0)');
    x.fillStyle = g; x.fillRect(0, 0, 128, 128); return new THREE.CanvasTexture(c);
  })();
  function glow(color, size, opacity) {
    var s = new THREE.Sprite(new THREE.SpriteMaterial({ map: GLOW, color: color, transparent: true, opacity: opacity || 0.8,
      depthWrite: false, blending: THREE.AdditiveBlending }));
    s.scale.set(size, size, 1); return s;
  }

  // ── floor: mirror underneath, translucent tech tiles on top
  if (THREE.Reflector) {
    var mirror = new THREE.Reflector(new THREE.PlaneGeometry(30, 30), {
      textureWidth: Math.round((stage.clientWidth || 360) * (small ? 0.6 : 0.9)),
      textureHeight: Math.round((stage.clientWidth || 360) * (small ? 0.45 : 0.6)), color: 0x6a7090 });
    mirror.rotation.x = -Math.PI / 2; at(mirror, 0, -0.002, 0);
  }
  var tileTex = (function () {
    var c = document.createElement('canvas'); c.width = c.height = 256; var x = c.getContext('2d');
    x.fillStyle = 'rgba(10,12,24,0.86)'; x.fillRect(0, 0, 256, 256);
    x.strokeStyle = 'rgba(56,225,255,0.18)'; x.lineWidth = 2; x.strokeRect(1, 1, 254, 254);
    x.strokeStyle = 'rgba(56,225,255,0.07)'; x.lineWidth = 1; x.strokeRect(64, 64, 128, 128);
    var t = new THREE.CanvasTexture(c); t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(15, 15); return t;
  })();
  var tiles = M(new THREE.PlaneGeometry(30, 30), new THREE.MeshStandardMaterial({ map: tileTex, transparent: true, roughness: 0.4, metalness: 0.5,
    opacity: THREE.Reflector ? 0.9 : 1 }));
  tiles.rotation.x = -Math.PI / 2; tiles.receiveShadow = true; at(tiles, 0, 0, 0);

  // ── curved data wall with a scrolling ticker
  function dataWallTex() {
    var c = document.createElement('canvas'); c.width = 1024; c.height = 256; var x = c.getContext('2d');
    x.fillStyle = '#070a16'; x.fillRect(0, 0, 1024, 256);
    for (var p = 0; p < 16; p++) {                       // little chart panels
      var px = p * 64 + 4, py = 18 + (p % 2) * 112;
      x.strokeStyle = 'rgba(56,225,255,0.25)'; x.strokeRect(px, py, 56, 96);
      x.beginPath(); var v = 50 + (p * 13 % 30);
      for (var i = 0; i < 12; i++) { v += Math.sin(p * 7 + i * 1.7) * 7; x.lineTo(px + 4 + i * 4.4, py + 20 + v * 0.8 % 70); }
      x.strokeStyle = p % 3 ? 'rgba(56,225,255,0.7)' : 'rgba(192,38,211,0.7)'; x.lineWidth = 1.5; x.stroke();
      x.fillStyle = 'rgba(160,190,255,0.45)'; x.font = '9px monospace'; x.fillText((1 + (p * 0.037)).toFixed(4), px + 4, py + 92);
    }
    var t = new THREE.CanvasTexture(c); t.wrapS = THREE.RepeatWrapping; t.repeat.set(-2, 1); return t;
  }
  var wallTex = dataWallTex();
  var wall = M(new THREE.CylinderGeometry(9, 9, 5.2, 64, 1, true, Math.PI * 0.62, Math.PI * 0.76),
    new THREE.MeshBasicMaterial({ map: wallTex, side: THREE.BackSide }));
  at(wall, 0, 2.6, 1.5);
  var tickerText = (D.ticker && D.ticker.length) ? D.ticker.map(function (t) {
    return t.s + ' ' + t.p + ' ' + (t.c >= 0 ? '▲' : '▼') + Math.abs(t.c).toFixed(2) + '%'; }).join('     ') : (D.recess ? 'COURT ADJOURNED  ·  ' + D.recess.toUpperCase() : 'THE COUNCIL  ·  IN SESSION');
  var tickCanvas = document.createElement('canvas'); tickCanvas.width = 2048; tickCanvas.height = 64;
  (function () {
    var x = tickCanvas.getContext('2d'); x.fillStyle = '#0b1022'; x.fillRect(0, 0, 2048, 64);
    x.fillStyle = 'rgba(56,225,255,0.9)'; x.fillRect(0, 0, 2048, 2); x.fillRect(0, 62, 2048, 2);
    x.font = '600 30px ui-monospace, Menlo, monospace'; x.textBaseline = 'middle';
    var s = tickerText + '     ·     ', w = x.measureText(s).width, n = Math.max(1, Math.floor(2048 / w)), step = 2048 / n;
    for (var rep = 0; rep < n; rep++) {
      var pos = rep * step;
      s.split(/(▲[\d.]+%|▼[\d.]+%)/).forEach(function (pt) { if (!pt) return; x.fillStyle = pt[0] === '▲' ? '#53f0a6' : pt[0] === '▼' ? '#ff6b7c' : '#cfe3ff';
        x.fillText(pt, pos, 34); pos += x.measureText(pt).width; });
    }
  })();
  var tickTex = new THREE.CanvasTexture(tickCanvas); tickTex.wrapS = THREE.RepeatWrapping; tickTex.repeat.set(-2, 1);
  var ticker = M(new THREE.CylinderGeometry(8.9, 8.9, 0.42, 64, 1, true, Math.PI * 0.62, Math.PI * 0.76),
    new THREE.MeshBasicMaterial({ map: tickTex, side: THREE.BackSide }));
  at(ticker, 0, 4.45, 1.5);
  // ceiling ring + pillars
  var ring = M(new THREE.TorusGeometry(6.5, 0.05, 8, 96), addv(0x38e1ff, 0.6)); ring.rotation.x = Math.PI / 2; at(ring, 0, 6.2, -1.2);
  [-1, 1].forEach(function (sd) {
    for (var k = 0; k < 2; k++) {
      var px = sd * (5.2 + k * 1.6), pz = -2.8 + k * 1.8;
      at(shadowy(M(new THREE.BoxGeometry(0.35, 6.4, 0.35), STEEL), true, true), px, 3.2, pz);
      at(M(new THREE.BoxGeometry(0.04, 5.8, 0.04), glowMat(sd < 0 ? 0x22d3ee : 0xc026d3)), px + (sd < 0 ? 0.19 : -0.19), 3.2, pz + 0.19);
    }
  });

  // ── holotable at the center with the live chart hologram above it
  var table = new THREE.Group(); table.position.set(0, 0, 1.0); table.scale.setScalar(0.78); world.add(table);
  table.add(shadowy(M(new THREE.CylinderGeometry(1.15, 1.3, 0.12, 6), STEEL), true, true)).position.y = 0.86;
  table.add(shadowy(M(new THREE.CylinderGeometry(0.5, 0.75, 0.8, 6), METAL2), true, true)).position.y = 0.4;
  var rim = M(new THREE.TorusGeometry(1.17, 0.018, 6, 6), glowMat(0x38e1ff)); rim.rotation.x = Math.PI / 2; rim.rotation.z = Math.PI / 6; rim.position.y = 0.93; table.add(rim);
  var lens = M(new THREE.CircleGeometry(0.85, 6), addv(0x38e1ff, 0.35)); lens.rotation.x = -Math.PI / 2; lens.position.y = 0.935; table.add(lens);
  var beam = M(new THREE.CylinderGeometry(1.9, 0.7, 3.4, 6, 1, true), addv(0x38e1ff, 0.045)); beam.position.y = 2.6; table.add(beam);
  table.add(glow(0x38e1ff, 2.6, 0.55)).position.y = 1.0;

  function chartTexture() {
    var W = 1024, H = 560, c = document.createElement('canvas'); c.width = W; c.height = H; var x = c.getContext('2d');
    x.fillStyle = 'rgba(8,30,48,0.35)'; x.fillRect(0, 0, W, H);
    x.strokeStyle = 'rgba(56,225,255,0.9)'; x.lineWidth = 4; x.strokeRect(6, 6, W - 12, H - 12);
    for (var gy = 1; gy < 5; gy++) { x.strokeStyle = 'rgba(56,225,255,0.12)'; x.lineWidth = 1; x.beginPath(); x.moveTo(30, 90 + gy * 88); x.lineTo(W - 30, 90 + gy * 88); x.stroke(); }
    x.fillStyle = '#cff6ff'; x.font = '700 46px ui-monospace, Menlo, monospace'; x.textBaseline = 'middle';
    x.fillText(D.recess ? 'THE COUNCIL' : D.title ? String(D.title).slice(0, 34) : 'COURT IN RECESS', 34, 50);
    var ch = D.chart;
    if (D.recess) {
      x.fillStyle = 'rgba(8,30,48,0.6)'; x.fillRect(12, 80, W - 24, H - 92);
      x.fillStyle = '#f4c84a'; x.font = '800 64px ui-monospace, Menlo, monospace'; x.textAlign = 'center';
      x.fillText('COURT ADJOURNED', W / 2, H / 2 - 10);
      x.fillStyle = '#cff6ff'; x.font = '500 38px ui-monospace, Menlo, monospace'; x.fillText(String(D.recess), W / 2, H / 2 + 60);
      x.textAlign = 'left';
    } else if (ch && ch.closes && ch.closes.length > 2) {
      var vals = ch.closes.slice(), lv = [ch.entry, ch.sl, ch.tp].filter(function (v) { return v != null; });
      var lo = Math.min.apply(null, vals.concat(lv)), hi = Math.max.apply(null, vals.concat(lv)), pad = (hi - lo) * 0.08 || 1e-4;
      lo -= pad; hi += pad;
      var X = function (i) { return 40 + i / (vals.length - 1) * (W - 260); }, Y = function (v) { return 110 + (hi - v) / (hi - lo) * (H - 150); };
      x.beginPath(); vals.forEach(function (v, i) { if (i) x.lineTo(X(i), Y(v)); else x.moveTo(X(i), Y(v)); });
      x.lineTo(X(vals.length - 1), H - 30); x.lineTo(X(0), H - 30); x.closePath();
      var gr = x.createLinearGradient(0, 110, 0, H); gr.addColorStop(0, 'rgba(56,225,255,0.35)'); gr.addColorStop(1, 'rgba(56,225,255,0)');
      x.fillStyle = gr; x.fill();
      x.beginPath(); vals.forEach(function (v, i) { if (i) x.lineTo(X(i), Y(v)); else x.moveTo(X(i), Y(v)); });
      x.strokeStyle = '#7ff3ff'; x.lineWidth = 5; x.lineJoin = 'round'; x.stroke();
      function level(v, col, name) {
        if (v == null) return; x.setLineDash([16, 10]); x.strokeStyle = col; x.lineWidth = 3;
        x.beginPath(); x.moveTo(40, Y(v)); x.lineTo(W - 220, Y(v)); x.stroke(); x.setLineDash([]);
        x.fillStyle = col; x.font = '600 30px ui-monospace, Menlo, monospace'; x.fillText(name + ' ' + v.toFixed(ch.prec || 5), W - 212, Y(v));
      }
      level(ch.tp, '#53f0a6', 'TP'); level(ch.entry, '#ffffff', 'IN'); level(ch.sl, '#ff6b7c', 'SL');
    } else {
      x.fillStyle = 'rgba(207,246,255,0.7)'; x.font = '500 34px ui-monospace, Menlo, monospace';
      x.fillText(D.title ? 'live chart appears with the next vote' : 'waiting for a setup…', 34, H / 2 + 20);
    }
    return new THREE.CanvasTexture(c);
  }
  var holo = new THREE.Group(); holo.position.set(0, 3.7, 0.6); world.add(holo);
  var holoMat = new THREE.MeshBasicMaterial({ map: chartTexture(), transparent: true, opacity: 0.95, depthWrite: false,
    blending: THREE.AdditiveBlending, side: THREE.DoubleSide });
  var holoPlane = M(new THREE.PlaneGeometry(3.0, 1.64), holoMat); holo.add(holoPlane);
  var scan = M(new THREE.PlaneGeometry(3.0, 0.03), addv(0x9ff8ff, 0.5)); scan.position.z = 0.01; holo.add(scan);

  // ── The Chair's raised dais at the back
  var dais = at(shadowy(M(new THREE.CylinderGeometry(1.6, 1.9, 0.8, 8), STEEL), true, true), 0, 0.4, -5.9);
  var daisRim = M(new THREE.TorusGeometry(1.62, 0.03, 6, 8), glowMat(0xf4c84a)); daisRim.rotation.x = Math.PI / 2; at(daisRim, 0, 0.81, -5.9);
  var bench = at(shadowy(M(new THREE.BoxGeometry(2.6, 1.0, 0.5), STEEL), true, true), 0, 1.3, -5.05);
  at(M(new THREE.BoxGeometry(2.62, 0.05, 0.05), glowMat(0xf4c84a)), 0, 1.78, -4.79);
  var plateC = document.createElement('canvas'); plateC.width = 512; plateC.height = 64;
  (function () { var x = plateC.getContext('2d'); x.fillStyle = '#0b0e1c'; x.fillRect(0, 0, 512, 64);
    x.fillStyle = '#f4c84a'; x.font = '700 30px ui-monospace, Menlo, monospace'; x.textAlign = 'center'; x.textBaseline = 'middle'; x.fillText('THE CHAIR', 256, 33); })();
  at(M(new THREE.PlaneGeometry(1.3, 0.16), new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(plateC) })), 0, 1.45, -4.79);

  var judge = new THREE.Group(); judge.position.set(0, 0.8, -5.75); world.add(judge);
  var ROBE = new THREE.MeshStandardMaterial({ color: 0x1b2433, roughness: 0.7, metalness: 0.1 });
  judge.add(shadowy(M(new THREE.ConeGeometry(0.46, 1.6, 24), ROBE), true, false)).position.y = 1.0;
  var trim = M(new THREE.TorusGeometry(0.2, 0.025, 8, 24), glowMat(0xf4c84a)); trim.rotation.x = Math.PI / 2; trim.position.y = 1.72; judge.add(trim);
  var jhead = new THREE.Group(); jhead.position.y = 1.96; judge.add(jhead);
  var JSKIN = new THREE.MeshStandardMaterial({ color: 0xa86b45, roughness: 0.65 });
  jhead.add(shadowy(M(new THREE.SphereGeometry(0.24, 24, 16), JSKIN), true, false));
  [-1, 1].forEach(function (sd) { var e = M(new THREE.SphereGeometry(0.03, 8, 6), new THREE.MeshBasicMaterial({ color: 0x111111 })); e.position.set(sd * 0.085, 0.02, 0.215); jhead.add(e);
    var br = M(new THREE.BoxGeometry(0.1, 0.025, 0.02), new THREE.MeshStandardMaterial({ color: 0xdddddd })); br.position.set(sd * 0.085, 0.09, 0.22); jhead.add(br); });
  var jcap = M(new THREE.CylinderGeometry(0.28, 0.23, 0.13, 28), new THREE.MeshStandardMaterial({ color: 0x1b2433, roughness: 0.6 })); jcap.position.y = 0.21; jhead.add(jcap);
  var braid = M(new THREE.CylinderGeometry(0.235, 0.235, 0.05, 28), new THREE.MeshStandardMaterial({ color: 0xd9b44a, metalness: 0.85, roughness: 0.3 })); braid.position.y = 0.16; jhead.add(braid);
  var jpeak = M(new THREE.CylinderGeometry(0.18, 0.18, 0.015, 24, 1, false, -Math.PI / 2, Math.PI), new THREE.MeshStandardMaterial({ color: 0x0b0b0e })); jpeak.position.set(0, 0.14, 0.13); jpeak.rotation.x = 0.25; jhead.add(jpeak);
  var halo = new THREE.Group(); jhead.add(halo);
  [-1, 1].forEach(function (sd) { for (var k = 0; k < 4; k++) { var star = M(new THREE.OctahedronGeometry(0.025, 0), new THREE.MeshBasicMaterial({ color: 0xf4c84a }));
    star.position.set(sd * (0.36 + k * 0.0) , 1.62 + 0, 0.05 - k * 0.06); judge.add(star); } });
  var arm = new THREE.Group(); arm.position.set(0.32, 1.55, 0.12); judge.add(arm);
  var fore = M(new THREE.CylinderGeometry(0.05, 0.045, 0.5, 10), ROBE); fore.rotation.x = Math.PI / 2; fore.position.z = 0.25; arm.add(fore);
  var gavel = new THREE.Group(); gavel.position.set(0, 0, 0.5); arm.add(gavel);
  var gh = M(new THREE.CylinderGeometry(0.02, 0.02, 0.42, 8), METAL2); gh.rotation.x = Math.PI / 2; gh.position.z = 0.15; gavel.add(gh);
  var ghead = M(new THREE.CylinderGeometry(0.075, 0.075, 0.24, 16), glowMat(0xf4c84a)); ghead.rotation.z = Math.PI / 2; ghead.position.z = 0.36; gavel.add(ghead);
  var gglow = glow(0xf4c84a, 0.5, 0.0); gglow.position.z = 0.36; gavel.add(gglow);

  // ── atmosphere: light shafts and drifting dust
  [[-3.0, -2.3], [0, -3.4], [3.0, -2.3], [0, 1.0]].forEach(function (p, i) {
    var shaft = M(new THREE.CylinderGeometry(0.15, i === 3 ? 1.5 : 0.95, 6.2, 24, 1, true), addv(i === 3 ? 0x38e1ff : 0xbfd4ff, i === 3 ? 0.035 : 0.045));
    at(shaft, p[0], 3.1, p[1]);
  });
  var dustN = small ? 220 : 380, dustG = new THREE.BufferGeometry(), dp = new Float32Array(dustN * 3);
  for (var di = 0; di < dustN; di++) { dp[di * 3] = (Math.random() - 0.5) * 9; dp[di * 3 + 1] = Math.random() * 5.5; dp[di * 3 + 2] = (Math.random() - 0.7) * 7; }
  dustG.setAttribute('position', new THREE.BufferAttribute(dp, 3));
  var dust = new THREE.Points(dustG, new THREE.PointsMaterial({ color: 0xbfe9ff, size: 0.035, transparent: true, opacity: 0.55,
    depthWrite: false, blending: THREE.AdditiveBlending }));
  world.add(dust);

  // ── the council as little military officers: big heads, uniforms, unique headgear and insignia
  var COLORS = { 'The Quant': 0x3ee6a1, 'Risk': 0xff5468, 'Momentum': 0xffb03b, 'Time': 0x4cb7ff, 'Doubt': 0xc9a7ff };
  function std(c, r, m) { return new THREE.MeshStandardMaterial({ color: c, roughness: r === undefined ? 0.75 : r, metalness: m || 0 }); }
  var BOOT = std(0x15130f, 0.45, 0.2), GOLD = std(0xd9b44a, 0.3, 0.85), BLACK = std(0x0b0b0e, 0.6), WHITE = std(0xf2f2ee, 0.5);
  var EYE = new THREE.MeshBasicMaterial({ color: 0x111111 });
  function bar(parent, w, h, x, y, z, rz, mat) { var b = M(new THREE.BoxGeometry(w, h, 0.03), mat); b.position.set(x, y, z); b.rotation.z = rz || 0; parent.add(b); return b; }
  // two-part limb: shoulder/hip → elbow/knee → hand/boot
  function limb(g, x, y, len, r, mat, endMat) {
    var piv = new THREE.Group(); piv.position.set(x, y, 0);
    var half = len / 2, leg = y < 1.0;
    var up = M(new THREE.CylinderGeometry(r * 1.08, r * 0.95, half, 12), mat); up.position.y = -half / 2; piv.add(up);
    var joint = new THREE.Group(); joint.position.y = -half; piv.add(joint);
    joint.add(M(new THREE.SphereGeometry(r * 1.0, 10, 8), mat));
    var lo = M(new THREE.CylinderGeometry(r * 0.95, r * 0.85, half, 12), leg ? BOOT : mat); lo.position.y = -half / 2; joint.add(lo);
    if (leg) { var boot = M(new THREE.BoxGeometry(r * 2.3, r * 1.3, r * 3.8), BOOT); boot.position.set(0, -half - r * 0.25, r * 0.9); joint.add(boot); }
    else { var hand = M(new THREE.SphereGeometry(r * 1.15, 10, 8), endMat); hand.position.y = -half - r * 0.3; joint.add(hand); }
    piv.userData.joint = joint;
    g.add(piv); return piv;
  }
  // shared body: boots, trousers, tunic, belt, collar, big head with eyes/brows/mouth
  function soldier(g, u, o) {
    var TUNIC = std(o.tunic, 0.8), PANTS = std(o.pants || o.tunic, 0.8), SKIN = std(o.skin, 0.65);
    var w = o.wide || 1;
    u.legL = limb(g, -0.1 * w, 0.62, 0.6, 0.065 * w, PANTS); u.legR = limb(g, 0.1 * w, 0.62, 0.6, 0.065 * w, PANTS);
    var torso = M(new THREE.BoxGeometry(0.44 * w, 0.58, 0.28 * Math.max(1, w * 0.9)), TUNIC); torso.position.y = 0.93; g.add(torso);
    var belly = M(new THREE.BoxGeometry(0.46 * w, 0.08, 0.3 * Math.max(1, w * 0.9)), BLACK); belly.position.y = 0.72; g.add(belly);
    var buckle = M(new THREE.BoxGeometry(0.07, 0.06, 0.02), GOLD); buckle.position.set(0, 0.72, 0.16 * Math.max(1, w * 0.9)); g.add(buckle);
    for (var b = 0; b < 3; b++) { var btn = M(new THREE.SphereGeometry(0.018, 8, 6), GOLD); btn.position.set(0, 0.84 + b * 0.1, 0.145 * Math.max(1, w * 0.9)); g.add(btn); }
    [-1, 1].forEach(function (sd) {                                   // epaulettes
      var ep = M(new THREE.BoxGeometry(0.13, 0.03, 0.2), o.epaulette ? GOLD : TUNIC); ep.position.set(sd * 0.2 * w, 1.23, 0); g.add(ep);
    });
    var badge = M(new THREE.BoxGeometry(0.08, 0.06, 0.02), new THREE.MeshBasicMaterial({ color: u.col }));   // unit patch in their color
    badge.position.set(-0.12 * w, 1.06, 0.145 * Math.max(1, w * 0.9)); g.add(badge); u.patch = badge;
    if (o.medals) for (var m = 0; m < o.medals; m++) {
      var md = M(new THREE.BoxGeometry(0.035, 0.05, 0.02), [GOLD, std(0xb03a2e), std(0x2e5cb0)][m % 3]);
      md.position.set(0.07 * w + m * 0.045, 1.07, 0.145 * Math.max(1, w * 0.9)); g.add(md);
    }
    u.armL = limb(g, -0.27 * w, 1.2, 0.56, 0.055 * w, TUNIC, SKIN); u.armR = limb(g, 0.27 * w, 1.2, 0.56, 0.055 * w, TUNIC, SKIN);
    var neck = M(new THREE.CylinderGeometry(0.07, 0.08, 0.08, 12), SKIN); neck.position.y = 1.27; g.add(neck);
    var head = new THREE.Group(); head.position.y = 1.5; g.add(head); u.head = head;
    var skull = M(new THREE.SphereGeometry(0.235, 28, 20), SKIN); skull.scale.set(1, 1.05, 0.98); head.add(skull);
    [-1, 1].forEach(function (sd) { var ear = M(new THREE.SphereGeometry(0.05, 10, 8), SKIN); ear.position.set(sd * 0.235, 0, 0); ear.scale.set(0.5, 1, 0.8); head.add(ear); });
    u.glows = [];
    [-1, 1].forEach(function (sd) {
      var e = M(new THREE.SphereGeometry(0.03, 10, 8), EYE); e.position.set(sd * 0.085, 0.02, 0.215); head.add(e); u.glows.push(e);
      var br = bar(head, 0.09, 0.022, sd * 0.085, 0.09 + (o.browUp && sd > 0 ? 0.03 : 0), 0.22, sd * (o.browTilt || 0), BLACK);
    });
    var mouth = M(new THREE.BoxGeometry(o.mouthW || 0.08, 0.018, 0.02), std(0x5a2a22)); mouth.position.set(o.mouthX || 0, -0.1, 0.22); mouth.rotation.z = o.mouthTilt || 0; head.add(mouth); u.mouth = mouth;
    var nose = M(new THREE.SphereGeometry(0.028, 8, 6), SKIN); nose.position.set(0, -0.03, 0.235); head.add(nose);
    return { head: head, TUNIC: TUNIC, SKIN: SKIN };
  }
  var BUILD = {
    // intelligence officer: olive uniform, beret, round glasses, tablet
    'The Quant': function (g, u, col) {
      var s = soldier(g, u, { tunic: 0x4b5a3a, skin: 0xe0ac69, medals: 1 }), h = s.head;
      var beret = M(new THREE.SphereGeometry(0.25, 20, 12, 0, Math.PI * 2, 0, Math.PI / 2), std(0x1f5e3a)); beret.scale.set(1.05, 0.45, 1.05);
      beret.position.set(0.03, 0.12, -0.01); beret.rotation.z = -0.25; h.add(beret);
      var flash = M(new THREE.BoxGeometry(0.05, 0.05, 0.02), GOLD); flash.position.set(-0.11, 0.18, 0.18); h.add(flash);
      [-1, 1].forEach(function (sd) { var lens = M(new THREE.TorusGeometry(0.05, 0.008, 6, 20), BLACK); lens.position.set(sd * 0.085, 0.02, 0.23); h.add(lens); });
      var tab = M(new THREE.BoxGeometry(0.2, 0.26, 0.02), BLACK); tab.position.set(0, -0.06, 0.08); tab.rotation.x = -0.8;
      var scr = M(new THREE.PlaneGeometry(0.17, 0.22), new THREE.MeshBasicMaterial({ color: col })); scr.position.z = 0.012; tab.add(scr);
      u.armL.userData.joint.add(tab);
    },
    // military police / drill sergeant: broad, campaign hat, red armband, scowl
    'Risk': function (g, u, col) {
      var s = soldier(g, u, { tunic: 0x6b5a3e, pants: 0x4a3f2c, skin: 0x8d5524, wide: 1.22, browTilt: 0.35, mouthW: 0.1, medals: 2 }), h = s.head;
      var brim = M(new THREE.CylinderGeometry(0.4, 0.4, 0.02, 32), std(0x8a6a3a)); brim.position.y = 0.14; h.add(brim);
      var crown = M(new THREE.ConeGeometry(0.2, 0.22, 4), std(0x8a6a3a)); crown.position.y = 0.26; crown.rotation.y = Math.PI / 4; h.add(crown);
      var band = M(new THREE.CylinderGeometry(0.21, 0.21, 0.04, 20), BLACK); band.position.y = 0.17; h.add(band);
      var arm = M(new THREE.CylinderGeometry(0.075, 0.075, 0.1, 12), new THREE.MeshStandardMaterial({ color: 0xc0262e, roughness: 0.7 }));
      arm.position.y = -0.1; u.armL.add(arm);
      var mp = M(new THREE.BoxGeometry(0.05, 0.04, 0.005), WHITE); mp.position.set(0, -0.1, 0.078); u.armL.add(mp);
      var whistle = M(new THREE.CylinderGeometry(0.012, 0.012, 0.05, 8), GOLD); whistle.position.set(0.1, 1.12, 0.17); whistle.rotation.z = 1.2; g.add(whistle);
    },
    // paratrooper: lean, helmet with goggles, forward-leaning, speed lines when walking
    'Momentum': function (g, u, col) {
      var s = soldier(g, u, { tunic: 0x5a4a32, pants: 0x4d4030, skin: 0xc68642, mouthW: 0.1, mouthTilt: -0.15, browUp: true }), h = s.head;
      var helm = M(new THREE.SphereGeometry(0.27, 24, 14, 0, Math.PI * 2, 0, Math.PI / 2), std(0x4f5a34, 0.85)); helm.position.y = 0.04; h.add(helm);
      var rim = M(new THREE.TorusGeometry(0.27, 0.02, 6, 32), std(0x4f5a34, 0.85)); rim.rotation.x = Math.PI / 2; rim.position.y = 0.04; h.add(rim);
      var strap = M(new THREE.BoxGeometry(0.5, 0.035, 0.02), BLACK); strap.position.set(0, 0.17, 0.2); h.add(strap);
      [-1, 1].forEach(function (sd) { var gg = M(new THREE.CylinderGeometry(0.055, 0.055, 0.04, 16), new THREE.MeshBasicMaterial({ color: col })); gg.rotation.x = Math.PI / 2; gg.position.set(sd * 0.08, 0.17, 0.22); h.add(gg); });
      var pack = M(new THREE.BoxGeometry(0.34, 0.4, 0.14), std(0x3d4529)); pack.position.set(0, 0.95, -0.21); g.add(pack);
      u.trail = [];
      for (var i = 0; i < 4; i++) { var tp = M(new THREE.SphereGeometry(0.045 - i * 0.008, 8, 6), basic(col, 0.7 - i * 0.15)); world.add(tp); u.trail.push(tp); }
    },
    // navy officer / timekeeper: white peaked cap, navy tunic, glowing wristwatch, stout
    'Time': function (g, u, col) {
      var s = soldier(g, u, { tunic: 0x1c2a4a, skin: 0xf1c27d, wide: 1.12, epaulette: true, medals: 2 }), h = s.head;
      var cap = M(new THREE.CylinderGeometry(0.26, 0.22, 0.12, 28), WHITE); cap.position.y = 0.2; h.add(cap);
      var band = M(new THREE.CylinderGeometry(0.225, 0.225, 0.05, 28), BLACK); band.position.y = 0.15; h.add(band);
      var peak = M(new THREE.CylinderGeometry(0.17, 0.17, 0.015, 24, 1, false, -Math.PI / 2, Math.PI), BLACK); peak.position.set(0, 0.13, 0.12); peak.rotation.x = 0.25; h.add(peak);
      var crest = M(new THREE.SphereGeometry(0.035, 10, 8), GOLD); crest.position.set(0, 0.17, 0.225); h.add(crest);
      var watch = M(new THREE.CylinderGeometry(0.06, 0.06, 0.03, 16), new THREE.MeshBasicMaterial({ color: col })); watch.rotation.z = Math.PI / 2;
      watch.position.set(0.06, -0.2, 0); u.armL.userData.joint.add(watch);
      var hands = new THREE.Group(); hands.position.set(0.078, -0.2, 0); hands.rotation.y = Math.PI / 2; u.armL.userData.joint.add(hands); u.hands = hands;
      bar(hands, 0.008, 0.045, 0, 0.02, 0, 0, BLACK);
    },
    // grizzled colonel: garrison cap tilted, grey mustache, monocle, lots of medals
    'Doubt': function (g, u, col) {
      var s = soldier(g, u, { tunic: 0x3a3f4a, skin: 0x5c3a21, medals: 4, browUp: true, mouthX: 0.03, mouthTilt: -0.2, epaulette: true }), h = s.head;
      var cap = M(new THREE.BoxGeometry(0.3, 0.1, 0.4), std(0x2c313b)); cap.position.set(0.04, 0.24, 0); cap.rotation.z = -0.3; h.add(cap);
      var pip = M(new THREE.BoxGeometry(0.04, 0.04, 0.02), GOLD); pip.position.set(-0.08, 0.27, 0.19); h.add(pip);
      var stache = M(new THREE.BoxGeometry(0.16, 0.035, 0.04), std(0x9a9a9a)); stache.position.set(0, -0.065, 0.225); h.add(stache);
      var mono = M(new THREE.TorusGeometry(0.05, 0.01, 6, 20), GOLD); mono.position.set(0.085, 0.02, 0.235); h.add(mono);
      var chain = M(new THREE.CylinderGeometry(0.004, 0.004, 0.25, 4), GOLD); chain.position.set(0.12, -0.1, 0.22); chain.rotation.z = 0.3; h.add(chain);
      head_tilt = h; h.rotation.z = 0.12;
    }
  };
  var head_tilt;
  function agent(name) {
    var col = COLORS[name] || 0xffffff, g = new THREE.Group(), u = g.userData;
    u.col = col;
    (BUILD[name] || BUILD['Doubt'])(g, u, col);
    var halo = M(new THREE.RingGeometry(0.34, 0.42, 40), basic(col, 0.45)); halo.rotation.x = -Math.PI / 2; halo.position.y = 0.02; g.add(halo); u.halo = halo;
    return g;
  }

  // ── member stations in an arc behind the holotable
  var people = [], signs = [], rings = [], N = D.voters.length;
  var SPEAK = new THREE.Vector3(1.75, 0, 2.55);
  function seatPos(i) {
    var a = (200 + i * (140 / Math.max(1, N - 1))) * Math.PI / 180;
    return new THREE.Vector3(Math.cos(a) * 3.5, 0, Math.sin(a) * 2.0 - 1.45);
  }
  function screenTex(name, col) {
    var c = document.createElement('canvas'); c.width = 256; c.height = 96; var x = c.getContext('2d');
    var hex = '#' + col.toString(16).padStart(6, '0');
    x.fillStyle = '#081020'; x.fillRect(0, 0, 256, 96); x.strokeStyle = hex; x.lineWidth = 4; x.strokeRect(2, 2, 252, 92);
    x.fillStyle = hex; x.font = '700 26px ui-monospace, Menlo, monospace'; x.textAlign = 'center'; x.textBaseline = 'middle';
    x.fillText(name.toUpperCase(), 128, 36);
    x.fillStyle = 'rgba(207,230,255,0.6)'; x.font = '500 16px ui-monospace, Menlo, monospace'; x.fillText('COUNCIL MEMBER', 128, 68);
    return new THREE.CanvasTexture(c);
  }
  function signTex(text, color) {
    var c = document.createElement('canvas'); c.width = 256; c.height = 112; var x = c.getContext('2d');
    x.fillStyle = 'rgba(6,10,24,0.85)'; x.fillRect(0, 0, 256, 112);
    x.strokeStyle = color; x.lineWidth = 4; x.strokeRect(4, 4, 248, 104);
    x.fillStyle = color; x.fillRect(4, 4, 248, 6);
    x.font = '700 50px ui-monospace, SFMono-Regular, Menlo, monospace'; x.textAlign = 'center'; x.textBaseline = 'middle';
    x.fillText(text, 128, 62);
    return new THREE.CanvasTexture(c);
  }
  D.voters.forEach(function (v, i) {
    var seat = seatPos(i);
    var face = Math.atan2(-seat.x, -seat.z);
    var g = agent(v.name), u = g.userData;
    u.i = i; u.seat = seat; u.faceSeat = face; u.yaw = face; u.phase = i * 1.37;
    u.hip = u.legL ? u.legL.position.y : 0.6; u.prev = seat.clone(); u.blinkAt = 1 + i * 0.9;
    // glow parts: the colored visor/face meshes, plus a halo sprite in front of the face
    u.glows = u.glows || [];
    var fg = glow(u.col, 0.35, 0.4); if (u.patch) { fg.position.copy(u.patch.position); fg.position.z += 0.03; g.add(fg); } u.faceGlow = fg;
    shadowy(g, true, false);
    g.position.copy(seat); world.add(g); people.push(g);
    // station: sleek chair + console with a screen and a floor ring that lights with the vote
    var st = new THREE.Group(); st.position.copy(seat); st.rotation.y = face; world.add(st);
    var seatTop = 0.5;
    st.add(shadowy(M(new THREE.BoxGeometry(0.66, 0.08, 0.58), STEEL), true, true)).position.set(0, seatTop - 0.04, -0.05);
    var back = M(new THREE.BoxGeometry(0.62, 1.45, 0.06), GLASS); back.position.set(0, seatTop + 0.66, -0.36); st.add(back);
    st.add(M(new THREE.BoxGeometry(0.03, 1.45, 0.07), glowMat(u.col))).position.set(-0.31, seatTop + 0.66, -0.36);
    st.add(M(new THREE.BoxGeometry(0.03, 1.45, 0.07), glowMat(u.col))).position.set(0.31, seatTop + 0.66, -0.36);
    st.add(shadowy(M(new THREE.CylinderGeometry(0.06, 0.12, seatTop - 0.08, 10), METAL2), true, true)).position.set(0, (seatTop - 0.08) / 2, -0.05);
    var con = new THREE.Group(); con.position.set(0.62, 0, -0.2); con.scale.setScalar(0.8); st.add(con);
    con.add(shadowy(M(new THREE.BoxGeometry(0.16, 0.9, 0.16), STEEL), true, true)).position.y = 0.45;
    var scr = M(new THREE.PlaneGeometry(0.64, 0.24), new THREE.MeshBasicMaterial({ map: screenTex(v.name, u.col) }));
    scr.position.set(0, 1.02, 0.06); scr.rotation.x = -0.35; con.add(scr);
    var ringMat = addv(0x334466, 0.5);
    var fr = M(new THREE.RingGeometry(0.46, 0.56, 48), ringMat); fr.rotation.x = -Math.PI / 2; fr.position.y = 0.012; st.add(fr);
    var rg = glow(0xffffff, 1.6, 0); rg.position.y = 0.2; st.add(rg);
    rings.push({ mat: ringMat, glow: rg, vote: v.vote });
    var word = v.vote === true ? 'YES' : v.vote === false ? 'NO' : 'PASS';
    var color = v.vote === true ? '#53f0a6' : v.vote === false ? '#ff6b7c' : '#a99fc9';
    var sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: signTex(word, color), transparent: true, depthWrite: false }));
    sp.scale.set(0.001, 0.001, 1); world.add(sp); signs.push(sp);
  });
  // ── commander's reactions: 🥂 cheers / 👎 disappointed
  var GLASS = new THREE.MeshStandardMaterial({ color: 0xe8f4ff, transparent: true, opacity: 0.55, roughness: 0.05, metalness: 0.1 });
  var BUBBLY = new THREE.MeshBasicMaterial({ color: 0xf6d36b });
  people.forEach(function (g) {
    var u = g.userData; if (!u.armR) return;
    var gl = new THREE.Group(); gl.visible = false;
    var bowl = M(new THREE.CylinderGeometry(0.06, 0.035, 0.12, 14, 1, true), GLASS); bowl.position.y = 0.1; gl.add(bowl);
    var fizz = M(new THREE.CylinderGeometry(0.052, 0.034, 0.08, 14), BUBBLY); fizz.position.y = 0.08; gl.add(fizz);
    var stem = M(new THREE.CylinderGeometry(0.008, 0.008, 0.08, 6), GLASS); stem.position.y = 0.0; gl.add(stem);
    var foot = M(new THREE.CylinderGeometry(0.035, 0.035, 0.008, 12), GLASS); foot.position.y = -0.04; gl.add(foot);
    gl.position.set(0, -0.38, 0.04); gl.rotation.x = Math.PI; gl.scale.setScalar(1.7);      // hand points down the arm; flip so the glass stands up when raised
    u.armR.userData.joint.add(gl); u.glass = gl;
  });
  var confN = 260, confG = new THREE.BufferGeometry(), cp = new Float32Array(confN * 3), cc = new Float32Array(confN * 3), cv = [];
  var palette = [[0.33, 0.94, 0.65], [0.96, 0.78, 0.29], [0.22, 0.88, 1.0], [1.0, 0.42, 0.49], [0.79, 0.65, 1.0]];
  for (var ci = 0; ci < confN; ci++) { var pc = palette[ci % 5]; cc[ci * 3] = pc[0]; cc[ci * 3 + 1] = pc[1]; cc[ci * 3 + 2] = pc[2]; cv.push([0, 0, 0]); cp[ci * 3 + 1] = -50; }
  confG.setAttribute('position', new THREE.BufferAttribute(cp, 3)); confG.setAttribute('color', new THREE.BufferAttribute(cc, 3));
  var confetti = new THREE.Points(confG, new THREE.PointsMaterial({ size: 0.07, vertexColors: true, transparent: true, depthWrite: false }));
  world.add(confetti);
  var react = null, REACT_LEN = 5.0;
  window.councilReact = function (kind) { react = { kind: kind, t: performance.now() / 1000, burst: false }; };
  var cheerLight = new THREE.PointLight(0xf6d36b, 0, 10, 2); cheerLight.position.set(0, 2.6, -0.6); scene.add(cheerLight);

  // speaking spot marker
  var spot = M(new THREE.RingGeometry(0.42, 0.5, 48), addv(0xf4c84a, 0.5)); spot.rotation.x = -Math.PI / 2; at(spot, SPEAK.x, 0.013, SPEAK.z);

  // paths around the holotable (out on one lane, back on another)
  var B = new THREE.Vector3(0, 0, -0.95), R = new THREE.Vector3(2.35, 0, 1.0);
  function pathFor(u, back) {
    var off = back ? -0.28 : 0.28, pts = [u.seat.clone()];
    if (u.seat.x < 0.9) pts.push(B.clone().add(new THREE.Vector3(0, 0, off)));
    pts.push(R.clone().add(new THREE.Vector3(off, 0, 0)));
    pts.push(SPEAK.clone().add(new THREE.Vector3(0, 0, -0.1)));
    return back ? pts.reverse() : pts;
  }
  function along(pts, k) {
    var L = 0, seg = []; for (var i = 1; i < pts.length; i++) { var d = pts[i].distanceTo(pts[i - 1]); seg.push(d); L += d; }
    var want = k * L;
    for (var j = 0; j < seg.length; j++) { if (want <= seg[j] || j === seg.length - 1) return pts[j].clone().lerp(pts[j + 1], Math.min(1, want / seg[j])); want -= seg[j]; }
    return pts[pts.length - 1].clone();
  }
  people.forEach(function (g) { g.userData.out = pathFor(g.userData, false); g.userData.back = pathFor(g.userData, true); });

  // ── life between votes: mingling, chit-chat bubbles, assembling, and parties
  var t0 = performance.now(), lastT = 0;
  var nowS = function () { return Date.now() / 1000; };
  function sget(k, ses) { try { return (ses ? sessionStorage : localStorage).getItem(k); } catch (e) { return null; } }
  function sset(k, v, ses) { try { (ses ? sessionStorage : localStorage).setItem(k, v); } catch (e) {} }
  function sdel(k, ses) { try { (ses ? sessionStorage : localStorage).removeItem(k); } catch (e) {} }
  var SPEED = 1.1, TC = new THREE.Vector3(0, 0, 1.0), PARTY_C = new THREE.Vector3(0, 0, 3.05);
  var SPOTS = [[-3.0, 2.6], [-1.6, 3.1], [0, 3.3], [1.6, 3.1], [3.0, 2.6], [-3.4, 0.9], [3.4, 0.9], [-1.7, -0.9], [1.7, -0.9],
               [-3.7, -0.3], [3.7, -0.3], [2.4, 2.1], [-2.4, 2.1], [0, -0.85]].map(function (a) { return new THREE.Vector3(a[0], 0, a[1]); });
  function rnd(a) { return a[Math.floor(Math.random() * a.length)]; }
  function route(a, b) {
    var pts = [], ab = b.clone().sub(a), L2 = ab.lengthSq();
    var k = L2 ? Math.max(0, Math.min(1, TC.clone().sub(a).dot(ab) / L2)) : 0;
    var cl = a.clone().add(ab.clone().multiplyScalar(k)), off = cl.clone().sub(TC); off.y = 0;
    if (off.length() < 1.4 && k > 0.02 && k < 0.98) {
      if (off.length() < 1e-3) off.set(a.x >= 0 ? 1 : -1, 0, 0);
      pts.push(TC.clone().add(off.normalize().multiplyScalar(1.8)));
    }
    pts.push(b.clone()); return pts;
  }
  people.forEach(function (g) { var u = g.userData; u.pos = u.seat.clone(); u.sit = 1; u.route = []; u.wantSit = true; u.face = u.faceSeat; u.task = null; u.talkUntil = 0; u.used = {}; });
  function goTo(u, dest, face) { u.route = route(u.pos, dest); u.face = face; u.wantSit = false; }
  function goSeat(u) { u.route = route(u.pos, u.seat); u.face = u.faceSeat; u.wantSit = true; }
  function seated(g) { var u = g.userData; return !u.route.length && u.wantSit && u.sit > 0.95; }
  function lineFor(name, pool) {
    var xs = (pool || D.chatter || {})[name] || [], u = people[D.voters.findIndex(function (v) { return v.name === name; })].userData;
    if (!xs.length) return null;
    var fresh = xs.filter(function (l) { return !u.used[l]; });
    if (!fresh.length) { u.used = {}; fresh = xs; }
    var l = rnd(fresh); u.used[l] = 1; return l;
  }
  function idx(name) { for (var i = 0; i < N; i++) if (D.voters[i].name === name) return i; return -1; }

  // speech bubbles (HTML over the canvas so they stay crisp on a phone)
  var bubWrap = document.createElement('div'); bubWrap.className = 'bubbles'; stage.appendChild(bubWrap);
  var pill = document.createElement('div'); pill.className = 'phasepill'; stage.appendChild(pill);
  var banner = document.createElement('div'); banner.className = 'partybanner'; banner.hidden = true; stage.appendChild(banner);
  var bubs = [], V = new THREE.Vector3(), HEX = { 'The Quant': '#3ee6a1', 'Risk': '#ff5468', 'Momentum': '#ffb03b', 'Time': '#4cb7ff', 'Doubt': '#c9a7ff' };
  function speak(who, text, dur) {
    if (!text || (who !== 'chair' && who < 0)) return;
    var name = who === 'chair' ? 'The Chair' : D.voters[who].name;
    bubs = bubs.filter(function (b) { if (b.who === who) { b.el.remove(); return false; } return true; });
    while (bubs.length >= 2) { bubs.shift().el.remove(); }
    if (text.length > 90) text = text.slice(0, 88).replace(/\s+\S*$/, '') + '…';
    var el = document.createElement('div'); el.className = 'bub'; el.style.setProperty('--bc', who === 'chair' ? '#f4c84a' : HEX[name] || '#fff');
    var b = document.createElement('b'); b.textContent = name; el.appendChild(b); el.appendChild(document.createTextNode(text));
    bubWrap.appendChild(el); requestAnimationFrame(function () { el.classList.add('on'); });
    var len = dur || Math.min(5.5, 2.2 + text.length * 0.045);
    bubs.push({ who: who, el: el, until: performance.now() / 1000 + len });
    if (who !== 'chair') people[who].userData.talkUntil = lastT + Math.min(len, 2.6);
  }
  function placeBubbles(nowP) {
    var W = stage.clientWidth, H = renderer.domElement.clientHeight, placed = [];
    bubs = bubs.filter(function (b) {
      if (nowP > b.until) { b.el.classList.remove('on'); setTimeout(function () { b.el.remove(); }, 300); return false; }
      if (b.who === 'chair') jhead.getWorldPosition(V); else people[b.who].userData.head.getWorldPosition(V);
      V.y += 0.42; V.project(cam);
      var x = (V.x + 1) / 2 * W, y = (1 - V.y) / 2 * H, bw = b.el.offsetWidth || 120, bh = b.el.offsetHeight || 40;
      var left = Math.max(4, Math.min(W - bw - 4, x - bw / 2)), top = Math.max(bh + 4, Math.min(H - 4, y));
      placed.forEach(function (r) {                                   // stack above an earlier bubble instead of covering it
        if (left < r.l + r.w + 4 && left + bw + 4 > r.l && top - bh < r.t + 4 && top + 4 > r.t - r.h) {
          var up = r.t - r.h - 8;
          if (up - bh >= 4) top = up; else top = Math.min(H - 4, r.t + bh + 8);
        }
      });
      placed.push({ l: left, t: top, w: bw, h: bh });
      b.el.style.left = left + 'px'; b.el.style.top = top + 'px';
      b.el.style.setProperty('--tx', Math.max(12, Math.min(bw - 12, x - left)) + 'px');
      b.el.style.visibility = V.z > 1 ? 'hidden' : 'visible';
      return true;
    });
  }

  var PARTY_LINES = { 'The Quant': 'The numbers never lie! 📈', 'Risk': 'And we stayed inside our risk. Cheers!', 'Momentum': 'Told you it would run! 🚀',
                      'Time': 'Right on schedule. ⏱', 'Doubt': 'Fine… I’m impressed. 🥂' };
  var WAIT_LINES = { 'The Quant': 'Numbers are loaded.', 'Risk': 'Stops ready if we go.', 'Momentum': 'Come on, come on…', 'Time': '', 'Doubt': 'Here we go again.' };
  var phase = { name: 'mingle', t: 0 }, assembledFor = 0, chats = [], partyQ = null, nextSay = 0;
  var disco = [0xff4fd8, 0x38e1ff, 0xffd84a].map(function (c) { var l = new THREE.PointLight(c, 0, 9, 2); scene.add(l); return l; });
  var confettiUntil = 0;
  function fmtClock(ts) { try { return new Date(ts * 1000).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York' }); } catch (e) { return 'the next candle'; } }

  function startMingle(t) {
    phase = { name: 'mingle', t: t }; chats = []; banner.hidden = true;
    people.forEach(function (g, i) { var u = g.userData; u.wantSit = false; u.route = []; u.dance = false; u.task = { type: 'rest', until: t + 0.6 + i * 0.8 }; });
    if (partyQ) { var P = partyQ; partyQ = null; startParty(t + 0.01, P); }
  }
  function callToOrder(t, then, line, hold) {
    phase = { name: 'assemble', t: t, then: then, hold: hold || 0 }; chats = []; banner.hidden = true;
    people.forEach(function (g) { var u = g.userData; u.task = null; u.dance = false; goSeat(u); });
    if (line) speak('chair', line, 3.4);
  }
  function startHearing(t) {
    phase = { name: 'hearing', t: t };
    people.forEach(function (g) { var u = g.userData; u.pos.copy(u.seat); u.sit = 1; u.route = []; u.wantSit = true; u.task = null; u.spoke = -1; });
  }
  function startParty(t, P) {
    phase = { name: 'party', t: t, p: P, danceT: 0, said: 0 }; chats = [];
    sset('vfxParty', String(P.t));
    banner.textContent = '';
    var h = document.createElement('b'); h.textContent = (P.kind === 'win' ? '🎉 THE THEORY WORKED' : P.kind === 'comeback' ? '🎉 COMEBACK' : '🎉 THEORY PROVEN'); banner.appendChild(h);
    var a = document.createElement('span'); a.textContent = P.title; banner.appendChild(a);
    var c = document.createElement('small'); c.textContent = P.detail || ''; banner.appendChild(c);
    banner.hidden = false;
    people.forEach(function (g, i) {
      var u = g.userData, ang = -Math.PI / 2 + (i - (N - 1) / 2) * (Math.PI * 2 / N);
      var dest = PARTY_C.clone().add(new THREE.Vector3(Math.cos(ang) * 1.05, 0, Math.sin(ang) * 1.05));
      u.task = null; goTo(u, dest, Math.atan2(PARTY_C.x - dest.x, PARTY_C.z - dest.z));
    });
    speak('chair', 'Council! ' + P.title + (P.kind === 'win' ? '!' : P.kind === 'comeback' ? ' is back!' : ' is proven!') + ' Party time!', 3.2);
  }
  function burst(cx, cz) {
    for (var b = 0; b < confN; b++) {
      cp[b * 3] = (cx || 0) + (Math.random() - 0.5) * 1.2; cp[b * 3 + 1] = 2.6 + Math.random() * 0.4; cp[b * 3 + 2] = (cz === undefined ? -0.6 : cz) + (Math.random() - 0.5) * 1.2;
      var a = Math.random() * Math.PI * 2, sp_ = 1.2 + Math.random() * 2.4;
      cv[b] = [Math.cos(a) * sp_, 2 + Math.random() * 3, Math.sin(a) * sp_];
    }
    confG.attributes.position.needsUpdate = true; confettiUntil = performance.now() / 1000 + 6;
  }

  function mingleTick(t) {
    // finish conversations
    chats = chats.filter(function (ch) {
      var A = people[ch.a].userData, B = people[ch.b].userData;
      if (A.route.length || B.route.length) return true;
      if (!ch.step) { ch.step = 1; ch.next = t + 0.3; }
      if (t < ch.next) return true;
      if (ch.step === 1) { speak(ch.first, ch.l1); ch.step = 2; ch.next = t + Math.min(5, 2.4 + ch.l1.length * 0.045); return true; }
      if (ch.step === 2) { speak(ch.second, ch.l2); ch.step = 3; ch.next = t + Math.min(5, 2.4 + ch.l2.length * 0.045); return true; }
      A.task = { type: 'rest', until: t + 1 + Math.random() * 2 }; B.task = { type: 'rest', until: t + 1 + Math.random() * 2 };
      return false;
    });
    var free = [];
    people.forEach(function (g, i) {
      var u = g.userData;
      if (u.task && u.task.type === 'rest' && t > u.task.until) u.task = null;
      if (u.task && u.task.type === 'solo' && !u.route.length) {
        if (!u.task.said) { u.task.said = true; u.task.until = t + 3.5 + Math.random() * 2.5; speak(i, lineFor(D.voters[i].name)); }
        else if (t > u.task.until) u.task = null;
      }
      if (!u.task && !u.route.length && u.sit < 0.1) free.push(i);
    });
    if (!free.length) return;
    var talking = chats.length;
    if (free.length >= 2 && talking < 2 && Math.random() < 0.6) {
      var ds = (D.dialogs || []).slice().sort(function () { return Math.random() - 0.5; });
      for (var k = 0; k < ds.length; k++) {
        var a = idx(ds[k][0]), b = idx(ds[k][2]);
        if (a < 0 || b < 0 || free.indexOf(a) < 0 || free.indexOf(b) < 0) continue;
        var key = ds[k][1]; if ((chats.used || {})[key] && Math.random() < 0.8) continue;
        var S = rnd(SPOTS), ang = Math.random() * Math.PI, off = new THREE.Vector3(Math.cos(ang) * 0.48, 0, Math.sin(ang) * 0.48);
        var pa = S.clone().add(off), pb = S.clone().sub(off), A = people[a].userData, B = people[b].userData;
        goTo(A, pa, Math.atan2(pb.x - pa.x, pb.z - pa.z)); goTo(B, pb, Math.atan2(pa.x - pb.x, pa.z - pb.z));
        A.task = { type: 'chat' }; B.task = { type: 'chat' };
        chats.push({ a: a, b: b, first: a, second: b, l1: ds[k][1], l2: ds[k][3], step: 0 });
        return;
      }
    }
    var i = rnd(free), u = people[i].userData;
    if (Math.random() < 0.25) { goTo(u, u.seat.clone().add(new THREE.Vector3(Math.sin(u.faceSeat) * 0.55, 0, Math.cos(u.faceSeat) * 0.55)), u.faceSeat + Math.PI); }
    else { var S2 = rnd(SPOTS), j = new THREE.Vector3((Math.random() - 0.5) * 0.6, 0, (Math.random() - 0.5) * 0.6);
      goTo(u, S2.clone().add(j), Math.atan2(-S2.x, (S2.z < 1.5 ? 1.0 : 5) - S2.z)); }
    u.task = { type: 'solo' };
  }
  function separate() {
    for (var a = 0; a < N; a++) for (var b = a + 1; b < N; b++) {
      var A = people[a].userData, B = people[b].userData;
      if (A.sit > 0.2 || B.sit > 0.2) continue;
      var dx = B.pos.x - A.pos.x, dz = B.pos.z - A.pos.z, d = Math.hypot(dx, dz);
      if (d > 0 && d < 0.62) { var push = (0.62 - d) / 2 / d; A.pos.x -= dx * push; A.pos.z -= dz * push; B.pos.x += dx * push; B.pos.z += dz * push; }
    }
  }
  function steer(g, u, t, dt, frozen) {
    if (u.route.length && !frozen) {
      if (u.sit > 0.02) u.sit = Math.max(0, u.sit - dt * 2.2);
      else {
        var tg = u.route[0], dx = tg.x - u.pos.x, dz = tg.z - u.pos.z, dist = Math.hypot(dx, dz), st_ = SPEED * dt;
        if (dist <= st_) { u.pos.x = tg.x; u.pos.z = tg.z; u.route.shift(); } else { u.pos.x += dx / dist * st_; u.pos.z += dz / dist * st_; }
      }
    } else if (!u.route.length) {
      u.sit += ((u.wantSit ? 1 : 0) - u.sit) * Math.min(1, dt * 3);
    }
    return { pos: u.pos, sit: u.sit, face: u.face, dance: u.dance };
  }
  function nextVoteTs(ns) { var nv = D.nextVote || 0; while (nv && ns - nv > 60) nv += 900; return nv; }

  function director(t, dt) {
    var ns = nowS();
    if (phase.name === 'hearing') {
      if (t - phase.t >= CYCLE) { sset('vfxHeard', String(D.lastVoteT)); startMingle(t); }
      return;
    }
    if (phase.name === 'assemble') {
      if (people.every(seated)) {
        if (phase.then === 'hearing') startHearing(t);
        else { phase = { name: 'wait', t: t, until: phase.hold ? t + phase.hold : 0, closed: !!phase.hold }; nextSay = t + 2; }
      }
      return;
    }
    if (phase.name === 'wait') {
      var left = Math.round(assembledFor - ns);
      if (left > 0 && t > nextSay) {
        nextSay = t + 6;
        var who = Math.floor(Math.random() * N), nm = D.voters[who].name;
        speak(who, nm === 'Time' ? 'Candle closes in ' + left + ' seconds.' : WAIT_LINES[nm], 2.6);
      }
      if (left <= 0 && !phase.closed) { phase.closed = true; speak('chair', 'The candle has closed. Counting votes… results post in about a minute.', 4.5); }
      if (!phase.until && ns > assembledFor + 110 && !phase.reloading) { phase.reloading = true; location.replace(location.pathname + '?r=' + Math.floor(ns)); }
      if (phase.until && t > phase.until) startMingle(t);
      return;
    }
    if (phase.name === 'party') {
      var allIn = people.every(function (g) { return !g.userData.route.length; });
      if (allIn && !phase.danceT) { phase.danceT = t; burst(PARTY_C.x, PARTY_C.z); people.forEach(function (g) { g.userData.dance = true; }); }
      if (phase.danceT) {
        var dtp = t - phase.danceT;
        while (phase.said < N && dtp > 0.8 + phase.said * 1.7) { speak(phase.said, PARTY_LINES[D.voters[phase.said].name] || '🎉', 2.6); phase.said++; }
        if (Math.floor(dtp / 2.8) !== phase.lastBurst) { phase.lastBurst = Math.floor(dtp / 2.8); if (dtp > 1) burst(PARTY_C.x + (Math.random() - 0.5), PARTY_C.z); }
        if (dtp > 10 && !phase.bye) { phase.bye = true; speak('chair', 'Well done, council. Back to work!', 2.8); }
        if (dtp > 12.5) { people.forEach(function (g) { g.userData.dance = false; }); startMingle(t); }
      }
      return;
    }
    // mingle
    if (!D.recess) {
      var nv = nextVoteTs(ns), lft = nv - ns;
      if (nv && lft > 0 && lft <= 75 && assembledFor !== nv) {
        assembledFor = nv; sset('vfxWait', String(nv), true);
        callToOrder(t, 'wait', 'Council, to your seats! Vote at ' + fmtClock(nv) + '.');
        return;
      }
    }
    mingleTick(t);
  }
  function phaseLabel(ns) {
    if (phase.name === 'hearing') return '⚖️ In session';
    if (phase.name === 'party') return '🎉 Party!';
    if (phase.name === 'assemble') return '📣 Taking seats';
    if (phase.name === 'wait') { var l = Math.round(assembledFor - ns); return l > 0 ? '⏳ Vote in ' + l + 's' : '🗳 Counting votes…'; }
    if (D.recess) return '☕ Off duty · market closed';
    var nv = nextVoteTs(ns), s = Math.max(0, Math.round(nv - ns));
    return nv ? '☕ Break · next vote in ' + Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0') : '☕ Break';
  }

  // what to do when the page opens
  (function boot() {
    var ns = nowS(), wait = +(sget('vfxWait', true) || 0);
    var P = (D.parties || []).filter(function (p) { return p.t > +(sget('vfxParty') || 0) && ns - p.t < 172800; }).pop();
    if (P) partyQ = P;
    if (wait && !D.recess) {
      if (D.scanT < wait && ns < wait + 420) {                        // the new results haven't posted yet
        assembledFor = wait; phase = { name: 'wait', t: 0, closed: true, reloading: true }; nextSay = 2;
        setTimeout(function () { speak('chair', 'Still counting… hang tight.', 3); }, 600);
        var tries = +(sget('vfxTries', true) || 0); sset('vfxTries', String(tries + 1), true);
        if (tries < 5) setTimeout(function () { location.replace(location.pathname + '?r=' + Math.floor(nowS())); }, 45000);
        else { sdel('vfxWait', true); phase.until = 4; }
        return;
      }
      sdel('vfxWait', true); sdel('vfxTries', true);
      if (D.verdict && D.lastVoteT >= wait - 120) { startHearing(0); return; }
      phase = { name: 'wait', t: 0, closed: true, until: 5 }; assembledFor = ns - 999;
      setTimeout(function () { speak('chair', 'No setups this candle. Back to work, team!', 3.4); }, 500);
      return;
    }
    if (D.verdict && D.lastVoteT && ns - D.lastVoteT < 600 && sget('vfxHeard') !== String(D.lastVoteT) && !D.recess) { startHearing(0); return; }
    startMingle(0);
    if (!partyQ) setTimeout(function () { var i = Math.floor(Math.random() * N); speak(i, lineFor(D.voters[i].name)); }, 1500);
  })();
  var callBtn = document.getElementById('callOrder'), partyBtn = document.getElementById('partyBtn');
  if (callBtn) {
    if (!D.verdict) callBtn.textContent = '📣 Call to order (no vote yet)';
    callBtn.addEventListener('click', function () {
      if (phase.name === 'hearing' || phase.name === 'assemble') return;
      if (D.verdict) callToOrder(lastT, 'hearing', 'Order! Let’s go over the last vote.');
      else { assembledFor = nowS() - 999; callToOrder(lastT, 'wait', 'Order! Nothing to vote on yet. As you were.', 5); }
    });
  }
  var lastParty = (D.parties || [])[(D.parties || []).length - 1];
  if (partyBtn && lastParty) {
    partyBtn.hidden = false;
    partyBtn.addEventListener('click', function () { if (phase.name !== 'party' && phase.name !== 'hearing') startParty(lastT, lastParty); });
  }

  // ── layout
  function size() {
    var w = stage.clientWidth || 360, h = Math.round(Math.min(520, Math.max(320, w * 0.82)));
    renderer.setSize(w, h); cam.aspect = w / h;
    var narrow = w < 520;
    cam.position.set(0, narrow ? 3.9 : 3.6, narrow ? 10.8 : 9.6); cam.lookAt(0, 1.9, -1.3);
    cam.updateProjectionMatrix();
  }
  size(); window.addEventListener('resize', size);

  // ── interaction
  var ray = new THREE.Raycaster(), ptr = new THREE.Vector2(), drag = null, turn = 0, picked = -1, canvas = renderer.domElement;
  canvas.addEventListener('pointerdown', function (e) { drag = { x: e.clientX, t: turn, moved: 0 }; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener('pointermove', function (e) {
    if (!drag) return; var dx = e.clientX - drag.x; drag.moved = Math.max(drag.moved, Math.abs(dx));
    turn = Math.max(-0.7, Math.min(0.7, drag.t + dx * 0.005));
  });
  canvas.addEventListener('pointerup', function (e) {
    var tap = drag && drag.moved < 6; drag = null; if (!tap) return;
    var r = canvas.getBoundingClientRect();
    ptr.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ptr, cam);
    var hit = ray.intersectObjects(people, true)[0], o = hit && hit.object;
    while (o && o.userData.i === undefined) o = o.parent;
    show(o ? o.userData.i : -1);
  });
  function show(i) {
    picked = (picked === i) ? -1 : i;
    if (picked < 0) { tip.hidden = true; return; }
    var v = D.voters[picked], word = v.vote === true ? 'YES' : v.vote === false ? 'NO' : 'abstained';
    tip.textContent = '';
    var b = document.createElement('b'); b.textContent = v.name + ' · ' + word; tip.appendChild(b);
    var a = document.createElement('span'); a.textContent = v.why; tip.appendChild(a);
    var c = document.createElement('small'); c.textContent = v.acc; tip.appendChild(c);
    tip.hidden = false;
  }
  Array.prototype.forEach.call(room.querySelectorAll('.seat'), function (btn) {
    btn.addEventListener('click', function () { show(+btn.getAttribute('data-i')); });
  });

  // ── choreography: stand → walk around the table to the speaking spot → vote → walk back → sit → gavel
  var WALK = 2.2, STAGGER = 4.2, START = 1.0, TURN = 0.5 + WALK + 1.4 + WALK + 0.5;
  var VERDICT_AT = START + (N - 1) * STAGGER + TURN + 0.4, CYCLE = VERDICT_AT + 6.5;
  var VCOL = new THREE.Color(D.approved ? 0x53f0a6 : 0xff6b7c), CYAN = new THREE.Color(0x38e1ff);
  function clamp(x) { return Math.max(0, Math.min(1, x)); }
  function smooth(x) { x = clamp(x); return x * x * (3 - 2 * x); }
  function pop(x) { x = clamp(x); var c = 1.6; return 1 + (c + 1) * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2); }
  function plan(i, c) {
    var u = people[i].userData, tau = c - (START + i * STAGGER), t1 = 0.5, t2 = t1 + WALK, t3 = t2 + 1.4, t4 = t3 + WALK;
    if (!D.verdict || tau < 0) return { pos: u.seat, sit: 1 };
    if (tau < t1) return { pos: u.seat, sit: 1 - smooth(tau / t1) };
    if (tau < t2) return { pos: along(u.out, smooth((tau - t1) / WALK)), sit: 0 };
    if (tau < t3) return { pos: u.out[u.out.length - 1], sit: 0, at: true, k: tau - t2, spoke: tau > t2 + 0.25 };
    if (tau < t4) return { pos: along(u.back, smooth((tau - t3) / WALK)), sit: 0, spoke: true };
    return { pos: u.seat, sit: smooth((tau - t4) / 0.5), spoke: true };
  }
  t0 = performance.now(); lastT = 0;
  function frame(now) {
    if (!stage.offsetParent) { requestAnimationFrame(frame); return; }   // hidden (collapsed section): skip work
    var t = (now - t0) / 1000, dt = Math.min(0.05, t - lastT); lastT = t;
    if (!still) director(t, dt);
    var hearing = phase.name === 'hearing' && !!D.verdict, formal = hearing || still;
    var c = hearing ? t - phase.t : (still && D.verdict ? CYCLE - 3 : 0);
    var deliberating = D.verdict && formal && c < CYCLE - 0.6;
    var verdictOn = D.verdict && formal && c >= VERDICT_AT + 0.45 && c < CYCLE - 0.6;
    var reacting = react && now / 1000 - react.t < REACT_LEN;
    if (!formal) separate();
    if (still && t > nextSay) { nextSay = t + 6; var si = Math.floor(Math.random() * N); speak(si, lineFor(D.voters[si].name)); }
    if (stampEl) stampEl.classList.toggle('show', !!verdictOn);
    world.rotation.y += (turn + (still ? 0 : Math.sin(t * 0.12) * 0.05) - world.rotation.y) * 0.06;

    var speaker = -1;
    people.forEach(function (g, i) {
      var u = g.userData, p = formal ? plan(i, c) : steer(g, u, t, dt, reacting);
      if (reacting && formal) p = { pos: u.seat, sit: 1 };   // reactions pause the hearing
      if (hearing && p.at && p.k > 0.3 && u.spoke !== phase.t) {
        u.spoke = phase.t; var vv = D.voters[i];
        speak(i, (vv.vote === true ? 'YES. ' : vv.vote === false ? 'NO. ' : 'I pass. ') + (vv.why || ''), 2.8);
      }
      if (p.at) speaker = i;
      u.prev.copy(g.position);
      var drop = (u.hip - 0.52) * p.sit;
      g.position.set(p.pos.x, -drop, p.pos.z);
      var vx = (g.position.x - u.prev.x) / Math.max(dt, 1e-3), vz = (g.position.z - u.prev.z) / Math.max(dt, 1e-3);
      var speed = Math.hypot(vx, vz);
      var target = p.at ? Math.atan2(cam.position.x - g.position.x, cam.position.z - g.position.z) - world.rotation.y
                 : speed > 0.15 ? Math.atan2(vx, vz) : (p.face !== undefined ? p.face : u.faceSeat);
      var d = Math.atan2(Math.sin(target - u.yaw), Math.cos(target - u.yaw)); u.yaw += d * 0.12; g.rotation.y = u.yaw;
      var stride = Math.min(1, speed / 0.9), ph = t * 8 + u.phase, sw = Math.sin(ph) * 0.6 * stride;
      if (u.legL) {
        u.legL.rotation.x = sw - 1.45 * p.sit; u.legR.rotation.x = -sw - 1.45 * p.sit;
        u.legL.userData.joint.rotation.x = Math.max(0, -Math.sin(ph)) * 0.9 * stride + 1.45 * p.sit;
        u.legR.userData.joint.rotation.x = Math.max(0, Math.sin(ph)) * 0.9 * stride + 1.45 * p.sit;
      }
      var raise = p.at ? pop((p.k - 0.15) / 0.45) * (1 - smooth((p.k - 1.1) / 0.3)) : 0;
      if (u.armL) { u.armL.rotation.x = -sw * 0.7 - 0.45 * p.sit; u.armL.userData.joint.rotation.x = -0.25 * stride - 0.9 * p.sit; }
      if (u.armR) { u.armR.rotation.x = sw * 0.7 * (1 - raise) - raise * 2.8 - 0.45 * p.sit;
        u.armR.userData.joint.rotation.x = (-0.25 * stride - 0.9 * p.sit) * (1 - raise) - 0.15 * raise; }
      g.position.y += Math.abs(Math.sin(ph)) * 0.03 * stride;
      if (u.head) u.head.rotation.y = p.at ? 0 : (still ? 0 : 0.4 * Math.sin(t * 0.5 + u.phase * 3)) * (p.sit > 0.5 ? 1 : 0.3);
      if (u.spin) u.spin.rotation.z = t * 1.8;
      if (u.gyro) { u.gyro.rotation.y = t * 1.4; u.gyro.rotation.x = t * 0.7; }
      if (u.hands) u.hands.rotation.z = -t * 1.2;
      if (u.trail) u.trail.forEach(function (tp, k) {
        var bk = 0.25 + k * 0.16;
        tp.position.set(g.position.x - Math.sin(u.yaw) * bk * (0.3 + stride), 1.2 - k * 0.05, g.position.z - Math.cos(u.yaw) * bk * (0.3 + stride));
        tp.visible = stride > 0.2;
      });
      // blink + speaking pulse
      if (!still && t > u.blinkAt) { u.blinkAt = t + 2.5 + ((i * 1.7 + t) % 3); u.blinkEnd = t + 0.12; }
      var blink = (!still && u.blinkEnd && t < u.blinkEnd) ? 0.15 : 1;
      var talk = (p.at && p.k > 0.25 && p.k < 1.3) || u.talkUntil > t ? 0.5 + 0.5 * Math.sin(t * 22) : 0;
      if (u.talkUntil > t && speed < 0.15 && !p.at && p.sit < 0.3 && u.armR) {            // talk with your hands
        var gst = 0.5 + 0.5 * Math.sin(t * 2.6 + i); u.armR.rotation.x -= 0.55 * gst; u.armR.userData.joint.rotation.x -= 0.7 * gst;
      }
      u.glows.forEach(function (m) { m.scale.y = blink; });
      u.faceGlow.material.opacity = 0.25 + talk * 0.6; u.faceGlow.scale.setScalar(0.3 + talk * 0.3);
      if (u.mouth) u.mouth.scale.y = 1 + talk * 3.5;
      var rk = react ? now / 1000 - react.t : 99;
      if (rk < REACT_LEN) {
        var fade = 1 - smooth((rk - (REACT_LEN - 0.8)) / 0.8), lift = smooth(rk / 0.6) * fade;
        if (react.kind === 'cheers') {
          var standUp = smooth(rk / 0.5) * fade;
          g.position.y = -drop * (1 - standUp) + Math.abs(Math.sin(rk * 7 + i)) * 0.09 * lift;
          if (u.legL) { u.legL.rotation.x *= 1 - standUp; u.legR.rotation.x *= 1 - standUp;
            u.legL.userData.joint.rotation.x *= 1 - standUp; u.legR.userData.joint.rotation.x *= 1 - standUp; }
          var toast = rk > 1.3 && rk < 1.7 ? 0.35 : 0;            // everyone leans in to clink
          if (u.armR) { u.armR.rotation.x = -2.5 * lift + 0.15 * Math.sin(rk * 5 + i) * lift + toast * lift;
            u.armR.userData.joint.rotation.x = -0.25 * lift; }
          if (u.armL) { u.armL.rotation.x = -0.6 * lift * (0.5 + 0.5 * Math.sin(rk * 6 + i)); }
          if (u.head) { u.head.rotation.x = -0.15 * lift; }
          if (u.mouth) u.mouth.scale.set(1 + lift * 0.6, 1 + lift * 3, 1);
          if (u.glass) u.glass.visible = lift > 0.05;
          var face = Math.atan2(-g.position.x, -0.6 - g.position.z); var dd = Math.atan2(Math.sin(face - u.yaw), Math.cos(face - u.yaw));
          u.yaw += dd * 0.1 * lift; g.rotation.y = u.yaw;
        } else {
          if (u.head) { u.head.rotation.x = 0.4 * lift; u.head.rotation.y = Math.sin(rk * 9 + i) * 0.35 * lift * (rk < 2.2 ? 1 : 0.3); }
          if (u.armR) u.armR.rotation.x *= 1 - lift; if (u.armL) u.armL.rotation.x *= 1 - lift;
          if (u.mouth) u.mouth.scale.set(1 - 0.3 * lift, 1, 1);
        }
      } else {
        if (u.head) u.head.rotation.x = 0;
        if (u.glass) u.glass.visible = false;
      }
      if (p.dance) {
        var bt = t * 6 + i * 1.3;
        g.position.y = Math.abs(Math.sin(bt)) * 0.13;
        if (u.armR) { u.armR.rotation.x = -2.7 + Math.sin(bt) * 0.35; u.armR.userData.joint.rotation.x = -0.2; }
        if (u.armL) { u.armL.rotation.x = -2.6 + Math.cos(bt) * 0.4; u.armL.userData.joint.rotation.x = -0.2; }
        if (u.legL) { u.legL.rotation.x = Math.sin(bt) * 0.3; u.legR.rotation.x = -Math.sin(bt) * 0.3; }
        if (u.glass) u.glass.visible = true;
        if (u.mouth) u.mouth.scale.set(1.4, 3, 1);
        if (i === Math.floor(t / 2.4) % N) g.rotation.y = u.yaw + ((t % 2.4) / 2.4) * Math.PI * 2;      // take turns spinning
      }
      u.halo.visible = p.sit < 0.5;
      var sel = picked === i ? 1.08 : 1; g.scale.setScalar(g.scale.x + (sel - g.scale.x) * 0.2);
      u.halo.material.opacity = picked === i ? 0.95 : 0.4;
      // vote sign + station ring
      var sp = signs[i], showSign = deliberating && (p.spoke || (still && D.verdict)) && !(react && now / 1000 - react.t < REACT_LEN);
      var k = showSign ? (p.at ? pop((p.k - 0.25) / 0.4) : 0.72) : 0;
      sp.position.set(g.position.x, (p.at ? 2.35 : 2.1) + g.position.y + (p.sit ? -0.1 : 0), g.position.z);
      sp.scale.set(0.8 * k + 0.001, 0.35 * k + 0.001, 1);
      var rr = rings[i], lit = showSign && rr.vote !== null && rr.vote !== undefined;
      var lc = rr.vote === true ? 0x53f0a6 : 0xff6b7c;
      rr.mat.color.setHex(lit ? lc : 0x334466); rr.mat.opacity = lit ? 0.85 : 0.35;
      rr.glow.material.color.setHex(lc); rr.glow.material.opacity = lit ? 0.35 + 0.1 * Math.sin(t * 4) : 0;
    });

    var rkk = react ? now / 1000 - react.t : 99;
    if (react && react.kind === 'cheers' && rkk > 1.45 && !react.burst) { react.burst = true; burst(); }
    var nowP = now / 1000, confLeft = Math.max(confettiUntil - nowP, 0);
    if (rkk < REACT_LEN + 3 || confLeft > 0) {
      var cpos = confG.attributes.position;
      for (var b = 0; b < confN; b++) {
        if (cp[b * 3 + 1] < -10) continue;
        cv[b][1] -= 6 * dt; cv[b][0] *= 0.985; cv[b][2] *= 0.985;
        cp[b * 3] += cv[b][0] * dt; cp[b * 3 + 1] += Math.max(cv[b][1], -1.2) * dt; cp[b * 3 + 2] += cv[b][2] * dt;
        if (cp[b * 3 + 1] < 0.02) { cp[b * 3 + 1] = 0.02; cv[b] = [0, 0, 0]; }
      }
      cpos.needsUpdate = true;
      confetti.material.opacity = Math.max(rkk < REACT_LEN ? 1 : Math.max(0, 1 - (rkk - REACT_LEN) / 3), Math.min(1, confLeft / 2));
    } else if (confetti.material.opacity > 0) {
      confetti.material.opacity = 0; for (var b2 = 0; b2 < confN; b2++) cp[b2 * 3 + 1] = -50; confG.attributes.position.needsUpdate = true;
    }
    var clink = react && react.kind === 'cheers' ? Math.max(0, 1 - Math.abs(rkk - 1.5) / 0.6) : 0;
    cheerLight.intensity = clink * 4;
    if (react && react.kind === 'sad' && rkk < REACT_LEN) { holoLight.color.lerp(new THREE.Color(0xff6b7c), 0.08); }

    // The Chair follows the speaker, then swings the gavel
    var look = speaker >= 0 ? Math.atan2(people[speaker].position.x - judge.position.x, people[speaker].position.z - judge.position.z) * 0.6
             : (still ? 0 : 0.25 * Math.sin(t * 0.35));
    if (react && rkk < REACT_LEN) look = react.kind === 'cheers' ? 0 : Math.sin(rkk * 8) * 0.4 * (1 - rkk / REACT_LEN);
    jhead.rotation.y += (look - jhead.rotation.y) * 0.08;
    jhead.rotation.x = react && react.kind === 'cheers' && rkk < REACT_LEN ? Math.sin(rkk * 6) * 0.15 : 0;
    halo.rotation.z = t * 0.6;
    var gt = (D.verdict && formal) ? (c - VERDICT_AT) : -1;
    if (hearing && gt > 0.5 && !phase.ruled) { phase.ruled = true; speak('chair', (D.approved ? 'Approved, ' : 'Rejected, ') + String(D.verdict).replace(/^\S+ /, '') + '. So ordered!', 3.2); }
    var partyOn = phase.name === 'party' && phase.danceT;
    disco.forEach(function (l, k) { l.intensity += ((partyOn ? 3 : 0) - l.intensity) * 0.08;
      l.position.set(PARTY_C.x + Math.cos(t * 1.6 + k * 2.1) * 2.2, 3.2, PARTY_C.z + Math.sin(t * 1.6 + k * 2.1) * 1.6); });
    if (partyOn) jhead.rotation.x = Math.sin(t * 6) * 0.18;
    placeBubbles(nowP);
    if (!frame._pl || t - frame._pl > 0.5) { frame._pl = t; pill.textContent = phaseLabel(nowS()); pill.className = 'phasepill ' + phase.name; }
    var swing = gt < 0 ? 0 : gt < 0.35 ? -smooth(gt / 0.35) * 1.1 : gt < 0.5 ? -1.1 + smooth((gt - 0.35) / 0.15) * 1.5 : 0.4 - smooth((gt - 0.5) / 0.6) * 0.4;
    arm.rotation.x = still ? 0 : swing;
    var flash = verdictOn ? Math.max(0, 1 - (c - VERDICT_AT - 0.45) / 1.6) : 0;
    gglow.material.opacity = flash * 0.9;
    verdictLight.color.copy(VCOL); verdictLight.intensity = verdictOn ? 0.6 + flash * 3.5 : 0;
    var hc = verdictOn ? VCOL : CYAN;
    holoLight.color.lerp(hc, 0.1); rim.material.color.lerp(hc, 0.1); lens.material.color.lerp(hc, 0.1);
    beam.material.color.lerp(hc, 0.1); beam.material.opacity = 0.07 + flash * 0.12;

    // hologram: faces the viewer, gentle hover, scanline + flicker
    holo.rotation.y = -world.rotation.y * 0.85;
    holo.position.y = 3.7 + (still ? 0 : Math.sin(t * 1.1) * 0.04);
    holoMat.opacity = still ? 0.95 : 0.88 + 0.07 * Math.sin(t * 13) * Math.sin(t * 3.1);
    scan.position.y = still ? -0.3 : ((t * 0.5) % 1) * 1.64 - 0.82;
    wallTex.offset.x = still ? 0 : t * 0.004; tickTex.offset.x = still ? 0 : t * 0.02;
    var pos = dust.geometry.attributes.position;
    if (!still) { for (var q = 0; q < dustN; q++) { var yy = pos.getY(q) + 0.12 * dt; pos.setY(q, yy > 5.6 ? 0.1 : yy); pos.setX(q, pos.getX(q) + Math.sin(t + q) * 0.0015); } pos.needsUpdate = true; }

    renderer.render(scene, cam);
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
})();
"""



# ───────────────────────────── Dashboard ─────────────────────────────────────

FEED_COLORS = {"The Quant": "#3ee6a1", "Risk": "#ff5468", "Momentum": "#ffb03b", "Time": "#4cb7ff", "Doubt": "#c9a7ff",
               "Chair": "#f4c84a"}


def feed_panel(st):
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    raw = f"https://raw.githubusercontent.com/{repo}/main/{STATE_FILE}" if repo else ""
    data = json.dumps({"feed": st.get("feed", [])[-40:], "last_run": st.get("last_run"), "raw": raw,
                       "colors": FEED_COLORS}).replace("</", "<\\/")
    return f"""<section class="feedwrap" aria-label="Live council feed">
  <div class="feedhead"><h2>Live council</h2><span class="beat" id="beat">…</span></div>
  <div id="feed" class="feed" aria-live="polite"></div>
  <script type="application/json" id="feeddata">{data}</script>
  <script>{FEED_JS}</script>
</section>"""


FEED_JS = r"""
(function () {
  var D = JSON.parse(document.getElementById('feeddata').textContent);
  var box = document.getElementById('feed'), beat = document.getElementById('beat'), seen = {};
  var still = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  function ago(t) {
    var s = Math.max(0, Date.now() / 1000 - t);
    return s < 60 ? 'just now' : s < 3600 ? Math.round(s / 60) + ' min ago' : s < 86400 ? Math.round(s / 3600) + 'h ago' : Math.round(s / 86400) + 'd ago';
  }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function bubble(who, vote, text) {
    var row = el('div', 'msg');
    var dot = el('span', 'dot'); dot.style.background = D.colors[who] || '#9a92bf'; row.appendChild(dot);
    var body = el('div', 'body');
    var top = el('div', 'who', who); top.style.color = D.colors[who] || '#c9bdf0';
    if (vote === true || vote === false) top.appendChild(el('span', 'chip ' + (vote ? 'yes' : 'no'), vote ? 'YES' : 'NO'));
    body.appendChild(top); body.appendChild(el('div', 'say', text)); row.appendChild(body);
    return row;
  }
  function card(e, fresh) {
    var c = el('article', 'card ' + e.kind + (fresh && !still ? ' fresh' : ''));
    var h = el('header'); h.appendChild(el('b', null, e.title)); h.appendChild(el('time', null, ago(e.t)));
    c.appendChild(h);
    if (e.kind === 'event') c.appendChild(bubble(e.who || 'Chair', null, e.text || ''));
    (e.lines || []).forEach(function (l, i) {
      var b = e.kind === 'scan' ? (function () { var r = el('div', 'watch'); r.appendChild(el('b', null, l.who)); r.appendChild(el('span', null, l.say)); return r; })()
                                : bubble(l.who, l.vote, l.say);
      if (fresh && !still) b.style.animationDelay = (0.15 + i * 0.35) + 's';
      c.appendChild(b);
    });
    if (e.verdict) { var v = el('div', 'verdict ' + (e.ok ? 'ok' : 'rej'), '⚖️ ' + e.verdict);
      if (fresh && !still) v.style.animationDelay = (0.3 + (e.lines || []).length * 0.35) + 's'; c.appendChild(v); }
    return c;
  }
  function render(feed, animate) {
    var items = feed.slice().reverse();
    box.textContent = '';
    if (!items.length) { box.appendChild(el('p', 'empty', 'The council hasn’t met yet. Debates show up here as soon as a setup appears.')); }
    items.forEach(function (e) { var k = e.t + e.title; var fresh = animate && !seen[k]; seen[k] = 1; box.appendChild(card(e, fresh)); });
  }
  function heartbeat(t) {
    if (!t) { beat.textContent = 'waiting for first run'; beat.className = 'beat'; return; }
    var s = Date.now() / 1000 - t;
    beat.textContent = (s < 1200 ? '● live · ' : '○ ') + 'last check ' + ago(t);
    beat.className = 'beat ' + (s < 1200 ? 'on' : 'stale');
  }
  render(D.feed || [], false); heartbeat(D.last_run);
  function poll() {
    if (!D.raw) return;
    fetch(D.raw + '?t=' + Date.now(), { cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : null; }).then(function (st) {
      if (!st) return;
      D.last_run = st.last_run; heartbeat(st.last_run);
      render((st.feed || []).slice(-40), true);
    }).catch(function () {});
  }
  setInterval(function () { heartbeat(D.last_run); }, 30000);
  setInterval(poll, 60000);
})();
"""


def recess_panel(st):
    now = dt.datetime.now(dt.timezone.utc)
    live = [p for p in PAIRS if in_session(p, now)]
    if live and len(live) == len(PAIRS):
        return ""
    t, nxt = next_vote(now)
    if live:
        names = ", ".join(NAMES[p] for p in live)
        return f"""<section class="recess night"><div><span class="rb">NIGHT SESSION</span>
          <b>Only {html.escape(names)} {'is' if len(live) == 1 else 'are'} in session</b>
          <p>The other pairs wait for London. Full court resumes at 2:00 AM ET.</p></div></section>"""
    resume = t.astimezone(LOCAL_TZ)
    sess = "Tokyo opens" if t.hour in (22, 23, 0) else "London opens" if t.hour in (6, 7) else "session opens"
    return f"""<section class="recess"><div><span class="rb">⚖️ COURT IS OUT OF SESSION</span>
      <b>Markets are quiet or closed. The council resumes when {sess}.</b>
      <p>Back at {resume.strftime('%a %I:%M %p').replace(' 0', ' ')} ET. Open trades keep their stops and still get checked.</p></div>
      <div class="rc" id="rc" data-t="{int(t.timestamp())}">--:--:--</div>
      <script>(function(){{var e=document.getElementById('rc'),t=+e.dataset.t;function f(){{var s=Math.max(0,Math.round(t-Date.now()/1000));
      var d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60),x=s%60;
      e.textContent=s?((d?d+'d ':'')+h+'h '+String(m).padStart(2,'0')+'m '+String(x).padStart(2,'0')+'s'):'back in session';}}f();setInterval(f,1000);}})();</script>
    </section>"""


def next_panel(st):
    now = dt.datetime.now(dt.timezone.utc)
    t, live = next_vote(now)
    when = t.astimezone(LOCAL_TZ).strftime("%I:%M %p").lstrip("0")
    day = "" if t.astimezone(LOCAL_TZ).date() == now.astimezone(LOCAL_TZ).date() else t.astimezone(LOCAL_TZ).strftime(" %a")
    rows = []
    pairs = st.get("pairs", {})
    def order(p):
        r = pairs.get(p, {}).get("ready") or {}
        return (not r.get("in_trade"), not r.get("session"), -(r.get("passed") or 0))
    for p in sorted(PAIRS, key=order):
        r = (pairs.get(p) or {}).get("ready")
        if not r:
            continue
        side = "BUY" if r["dir"] == 1 else "SELL"
        if r["in_trade"]:
            note, cls = "In a trade. The council is managing it.", "trade"
        elif not r["session"]:
            note, cls = "Off-hours for this pair. It'll be judged when its session opens.", "off"
        elif not r["waiting"]:
            note, cls = "Everything lines up. Expect a vote at the next candle.", "hot"
        else:
            note = "Holding back: " + "; ".join(w if w.startswith(("a pullback", "a fresh")) else w for w in r["waiting"])
            note = note.replace("Holding back: a pullback", "Needs a pullback")
            cls = "warm" if r["passed"] >= 4 else ""
        pct = round(100 * r["passed"] / r["of"])
        rows.append(f"""<li class="{cls}"><div class="rl"><b>{NAMES[p]}</b><span class="bias {'buy' if r['dir'] == 1 else 'sell'}">{side} bias · {'15m' if r.get('tf') == 'M15' else '1h'}</span>
          <span class="cnt">{r['passed']}/{r['of']} checks</span></div>
          <div class="rbar" role="img" aria-label="{r['passed']} of {r['of']} checks pass"><i style="width:{pct}%"></i></div>
          <p>{html.escape(note)}</p></li>""")
    body = "".join(rows) or "<li><p>Readiness shows up after the first full scan.</p></li>"
    return f"""<section class="nextwrap" aria-label="Next hearing">
  <div class="nh"><div><h2>Next hearing</h2><p class="muted"><span id="cdl">{when}{day}</span> ET candle · {len(live)} market{'s' if len(live) != 1 else ''} in session</p></div>
  <div class="cd" id="cd" data-t="{int(t.timestamp())}">--:--</div></div>
  <ol class="ready">{body}</ol>
  <script>(function(){{var e=document.getElementById('cd'),t=+e.dataset.t;function f(){{var s=Math.max(0,Math.round(t-Date.now()/1000));
  if(Date.now()/1000-t>60){{t+=900*Math.ceil((Date.now()/1000-t-60)/900);try{{document.getElementById('cdl').textContent=
    new Date(t*1000).toLocaleTimeString('en-US',{{hour:'numeric',minute:'2-digit',timeZone:'America/New_York'}});}}catch(_){{}}}}
  s=Math.max(0,Math.round(t-Date.now()/1000));
  var h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;e.textContent=s?((h?h+':'+String(m).padStart(2,'0'):m)+':'+String(x).padStart(2,'0')):'voting now';}}f();setInterval(f,1000);}})();</script>
</section>"""


def bankroll_chart(st):
    eq = st.get("equity", [])
    start = START_BALANCE or (eq[0][1] if eq else 0)
    if len(eq) < 2:
        return f"""<section class="chartwrap"><h2>Bankroll</h2><p class="muted">The chart starts after the first couple of scans.
          It tracks your ${start:,.0f} from here, with every closed trade marked.</p></section>"""
    W, H, L, R, T, B = 640, 230, 52, 14, 14, 26
    xs, ys = [e[0] for e in eq], [e[1] for e in eq]
    x0, x1 = xs[0], max(xs[-1], xs[0] + 3600)
    lo, hi = min(ys + [start]), max(ys + [start])
    pad = max((hi - lo) * 0.15, start * 0.005)
    lo, hi = lo - pad, hi + pad
    X = lambda t: L + (t - x0) / (x1 - x0) * (W - L - R)
    Y = lambda v: T + (hi - v) / (hi - lo) * (H - T - B)
    pts = " ".join(f"{X(t):.1f},{Y(v):.1f}" for t, v in eq)
    area = f"M{X(xs[0]):.1f},{Y(lo):.1f} L" + pts.replace(" ", " L") + f" L{X(xs[-1]):.1f},{Y(lo):.1f} Z"
    grid = ""
    for i in range(4):
        v = lo + (hi - lo) * i / 3
        grid += (f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" class="gl"/>'
                 f'<text x="{L - 8}" y="{Y(v) + 4:.1f}" class="yl">${v:,.2f}</text>')
    fmt = lambda t: dt.datetime.fromtimestamp(t, LOCAL_TZ).strftime("%b %d %I:%M %p").replace(" 0", " ")
    xl = (f'<text x="{L}" y="{H - 6}" class="xl">{fmt(x0)}</text>'
          f'<text x="{W - R}" y="{H - 6}" class="xr">{fmt(xs[-1])}</text>')
    base = (f'<line x1="{L}" x2="{W - R}" y1="{Y(start):.1f}" y2="{Y(start):.1f}" class="base"/>'
            f'<text x="{W - R}" y="{Y(start) - 6:.1f}" class="bl">start ${start:,.0f}</text>')

    def nav_at(t):
        prev = eq[0]
        for e in eq:
            if e[0] >= t:
                return e[1]
            prev = e
        return prev[1]
    marks, mdata = "", []
    for tr in st.get("closed", []):
        t = tr.get("closed_ts")
        if not t or t < x0:
            continue
        v, won = nav_at(t), tr.get("pl", 0) > 0
        cx, cy = X(min(t, xs[-1])), Y(v)
        shape = (f'<path d="M{cx:.1f},{cy - 6:.1f} L{cx + 6:.1f},{cy + 4:.1f} L{cx - 6:.1f},{cy + 4:.1f} Z" class="mk win"/>' if won else
                 f'<path d="M{cx:.1f},{cy + 6:.1f} L{cx + 6:.1f},{cy - 4:.1f} L{cx - 6:.1f},{cy - 4:.1f} Z" class="mk loss"/>')
        marks += shape
        mdata.append([round(cx, 1), f"{NAMES.get(tr['pair'], tr['pair'])} {'BUY' if tr['dir'] == 1 else 'SELL'} {money(tr.get('pl', 0))}"])
    last = ys[-1]
    chg = last - start
    data = json.dumps({"pts": [[round(X(t), 1), round(Y(v), 1), fmt(t), f"${v:,.2f}"] for t, v in eq], "marks": mdata})
    return f"""<section class="chartwrap"><div class="chd"><h2>Bankroll</h2>
  <span class="{'pos' if chg >= 0 else 'neg'}">${last:,.2f} ({money(chg)} since start)</span></div>
  <div class="chart" id="bkchart">
  <svg viewBox="0 0 {W} {H}" role="img" aria-label="Bankroll over time, from ${ys[0]:,.2f} to ${last:,.2f}">
    <defs><linearGradient id="bkg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#4cb7ff" stop-opacity=".28"/>
      <stop offset="1" stop-color="#4cb7ff" stop-opacity="0"/></linearGradient></defs>
    {grid}{base}<path d="{area}" fill="url(#bkg)"/><polyline points="{pts}" class="ln"/>
    <circle cx="{X(xs[-1]):.1f}" cy="{Y(last):.1f}" r="4.5" class="end"/>{marks}{xl}
    <line id="bkx" class="cross" y1="{T}" y2="{H - B}" x1="-10" x2="-10"/><circle id="bkd" r="5" class="end" cx="-10" cy="-10"/>
    <rect x="{L}" y="0" width="{W - L - R}" height="{H}" fill="#000" fill-opacity="0" pointer-events="all" id="bkhit"/>
  </svg><div class="ctip" id="bktip" hidden></div></div>
  <p class="muted legend"><span class="win">▲</span> winning trade closed · <span class="loss">▼</span> losing trade closed · dashed line = starting bankroll</p>
  <script>(function(){{var D={data},svg=document.querySelector('#bkchart svg'),hit=document.getElementById('bkhit'),
  x=document.getElementById('bkx'),d=document.getElementById('bkd'),tip=document.getElementById('bktip');
  function mv(e){{var r=svg.getBoundingClientRect(),sx={W}/r.width,px=(e.clientX-r.left)*sx,b=D.pts[0];
  D.pts.forEach(function(p){{if(Math.abs(p[0]-px)<Math.abs(b[0]-px))b=p;}});
  x.setAttribute('x1',b[0]);x.setAttribute('x2',b[0]);d.setAttribute('cx',b[0]);d.setAttribute('cy',b[1]);
  var m=D.marks.filter(function(k){{return Math.abs(k[0]-b[0])<10;}}).map(function(k){{return k[1];}});
  tip.textContent='';var a=document.createElement('b');a.textContent=b[3];tip.appendChild(a);
  var s=document.createElement('span');s.textContent=b[2]+' ET';tip.appendChild(s);
  m.forEach(function(t){{var q=document.createElement('span');q.textContent=t;tip.appendChild(q);}});
  tip.hidden=false;var left=b[0]/sx;tip.style.left=Math.min(Math.max(left-70,0),r.width-150)+'px';}}
  function out(){{tip.hidden=true;x.setAttribute('x1',-10);x.setAttribute('x2',-10);d.setAttribute('cx',-10);}}
  hit.addEventListener('pointermove',mv);hit.addEventListener('pointerdown',mv);hit.addEventListener('pointerleave',out);}})();</script>
</section>"""


def build_html(st, log):
    acc = st.get("account", {})
    nav = acc.get("nav")
    day = st.get("day", {})
    today = (nav - day.get("start_nav", nav)) if nav is not None else None
    rec = st.get("record", {"wins": 0, "losses": 0, "pl": 0.0})
    mode = st.get("mode", OANDA_ENV).upper()
    paused = st.get("paused")
    updated = dt.datetime.now(LOCAL_TZ).strftime("%a %b %d, %I:%M %p ET")

    def m(x):
        return "—" if x is None else money(x)

    def tr_row(tid, t):
        prec = st.get("pairs", {}).get(t["pair"], {}).get("prec", 5)
        upl = t.get("upl", 0)
        return f"""<tr><td class="mkt">{NAMES[t['pair']]}<small>{t.get('setup', '')} · opened {t['opened'][5:16].replace('T', ' ')} UTC{' · 🔒 breakeven' if t.get('breakeven') else ''}</small></td>
          <td data-l="Side"><span class="side {'buy' if t['dir'] == 1 else 'sell'}">{'BUY' if t['dir'] == 1 else 'SELL'}</span></td>
          <td class="n" data-l="Units">{t['units']:,}</td><td class="n" data-l="Entry">{t['entry']:.{prec}f}</td>
          <td class="n" data-l="Now">{(t.get('price') or 0):.{prec}f}</td><td class="n" data-l="Stop">{t['sl']:.{prec}f}</td>
          <td class="n" data-l="Target">{t['tp']:.{prec}f}</td><td class="n {'pos' if upl >= 0 else 'neg'}" data-l="P&amp;L">{m(upl)}</td></tr>"""

    def cl_row(t):
        pl = t.get("pl", 0)
        return f"""<tr><td class="mkt">{NAMES[t['pair']]}<small>closed {t.get('closed', '')[5:16].replace('T', ' ')} UTC</small></td>
          <td data-l="Side"><span class="side {'buy' if t['dir'] == 1 else 'sell'}">{'BUY' if t['dir'] == 1 else 'SELL'}</span></td>
          <td class="n" data-l="Units">{t['units']:,}</td><td class="n {'pos' if pl >= 0 else 'neg'}" data-l="Result">{m(pl)}</td></tr>"""

    def pair_card(pair):
        p = st.get("pairs", {}).get(pair)
        if not p:
            return f'<div class="pc"><b>{NAMES[pair]}</b><span class="muted">waiting for first scan</span></div>'
        arrow = "▲ up" if p["trend"] == 1 else "▼ down"
        agree = "agrees" if p["trend"] == p["h4_trend"] else "disagrees"
        return f"""<div class="pc"><div class="pch"><b>{NAMES[pair]}</b><span class="tr {'up' if p['trend'] == 1 else 'dn'}">{arrow}</span></div>
          <dl><div><dt>Price</dt><dd>{p['close']:.{p['prec']}f}</dd></div><div><dt>ADX</dt><dd>{(p['adx'] or 0):.0f}</dd></div>
          <div><dt>RSI</dt><dd>{(p['rsi'] or 0):.0f}</dd></div><div><dt>ATR</dt><dd>{p['atr_pips']:.0f} pips</dd></div></dl>
          <small>4-hour trend {agree} · {('setup: ' + p['setup']) if p['setup'] else 'no setup'} · {'open' if p['tradeable'] else 'market closed'}</small></div>"""

    open_rows = "".join(tr_row(k, v) for k, v in st.get("trades", {}).items()) or \
        '<tr><td colspan="8" class="empty">No open trades. The bot waits for the council to approve a setup.</td></tr>'
    closed = list(reversed(st.get("closed", [])))[:15]
    closed_rows = "".join(cl_row(t) for t in closed) or '<tr><td colspan="4" class="empty">No closed trades yet.</td></tr>'
    board = st.get("scoreboard", {})
    weights = voter_weights(st)
    board_rows = "".join(
        f"""<tr><td class="mkt">{n}<small>{ROLES[n]}</small></td>
        <td class="n" data-l="Called it">{(f"{board[n]['right'] / board[n]['total']:.0%}") if board.get(n, {}).get('total') else '—'}</td>
        <td class="n" data-l="Vote weight">{weights[n]:g}×</td>
        <td class="n" data-l="Right / scored">{board.get(n, {}).get('right', 0)} / {board.get(n, {}).get('total', 0)}</td></tr>"""
        for n in VOTERS)
    benched = st.get("learn", {}).get("benched", {})
    learn_line = ("Benched (losing lately, still scored on paper): " + ", ".join(
        f"{NAMES[c.split(':')[0]]} {c.split(':')[1]} ({v:+.2f}R)" for c, v in benched.items())) if benched else \
        f"Nothing benched. Vote weights start changing after {LEARN_MIN_CALLS} scored calls."
    soon = [e for e in st.get("news", {}).get("events", [])
            if e["cur"] in set().union(*(pair_currencies(p) for p in PAIRS)) and time.time() - NEWS_AFTER_MIN * 60 <= e["t"] <= time.time() + 36 * 3600][:5]
    news_rows = "".join(f"<li>{html.escape(news_label(e))}</li>" for e in soon) or "<li>Nothing high-impact in the next 36 hours.</li>"

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="300">
<title>Vault FX</title>
<style>{office.TAB_CSS}
:root {{ --bg:#0f0c1d; --panel:#17132b; --fg:#ece8fb; --muted:#9a92bf; --line:#2c2550; --accent:#f4c84a;
  --pos:#53f0a6; --neg:#ff6b7c; --buy:#53f0a6; --sell:#ff6b7c; color-scheme:dark }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; padding:20px 16px }}
.wrap {{ max-width:1100px; margin:0 auto; display:grid; gap:18px }}
header {{ display:flex; flex-wrap:wrap; align-items:center; gap:10px; justify-content:space-between }}
header h1 {{ margin:0; font-size:22px; letter-spacing:-.01em }} header p {{ margin:2px 0 0; color:var(--muted) }}
.badge {{ font:700 11px/1 ui-monospace,Menlo,monospace; letter-spacing:.14em; padding:6px 10px; border-radius:3px; border:1px solid }}
.badge.practice {{ color:#4cb7ff }} .badge.live {{ color:var(--neg) }} .badge.paused {{ color:var(--accent) }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px }}
.stat {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px 14px }}
.stat span {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em }}
.stat b {{ display:block; font-size:22px; font-variant-numeric:tabular-nums }}
.pos {{ color:var(--pos) }} .neg {{ color:var(--neg) }} .muted {{ color:var(--muted) }}
h2 {{ font-size:12px; text-transform:uppercase; letter-spacing:.1em; color:var(--muted); margin:6px 0 6px }}
.pairs {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:10px }}
.pc {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px 14px; display:grid; gap:8px }}
.pch {{ display:flex; justify-content:space-between }} .tr.up {{ color:var(--pos) }} .tr.dn {{ color:var(--neg) }}
.pc dl {{ margin:0; display:grid; grid-template-columns:repeat(4,1fr); gap:6px }} .pc dt {{ font-size:10px; color:var(--muted); text-transform:uppercase }}
.pc dd {{ margin:0; font-variant-numeric:tabular-nums }} .pc small {{ color:var(--muted) }}
.tbl {{ overflow-x:auto; background:var(--panel); border:1px solid var(--line); border-radius:8px }}
table {{ width:100%; border-collapse:collapse }}
th,td {{ padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top }}
th {{ font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); font-weight:600 }}
td.n {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap }}
td.mkt small {{ display:block; color:var(--muted); font-size:12px }}
.side {{ font:700 11px/1 ui-monospace,Menlo,monospace; padding:3px 7px; border-radius:3px; border:1px solid currentColor }}
.side.buy {{ color:var(--buy) }} .side.sell {{ color:var(--sell) }}
.empty {{ color:var(--muted); text-align:center; padding:22px }}
.news {{ margin:0; padding-left:18px; display:grid; gap:4px }}
.learnwrap {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px; display:grid; gap:10px }}
.learnwrap h2 {{ margin:0 }} .learnwrap .fine {{ margin:0 }}
.lv {{ margin:0; padding:8px 10px; border-radius:8px; border:1px solid var(--line) }}
.lv.good {{ border-color:var(--pos); color:var(--pos) }} .lv.bad {{ border-color:var(--neg); color:var(--neg) }} .lv.wait {{ color:var(--accent) }}
.lgrid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:8px }}
.lgrid div {{ border:1px solid var(--line); border-radius:8px; padding:8px 10px; display:grid; gap:2px }}
.lgrid span {{ font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted) }}
.lgrid b {{ font-size:20px; font-variant-numeric:tabular-nums }} .lgrid small {{ color:var(--muted); font-size:11px }}
.recess {{ display:flex; justify-content:space-between; align-items:center; gap:14px; flex-wrap:wrap; padding:16px;
  border-radius:12px; border:1px solid #f4c84a66; background:linear-gradient(135deg,#2a1f08,#15102a) }}
.recess.night {{ border-color:#4cb7ff66; background:linear-gradient(135deg,#0b1a33,#15102a) }}
.recess div {{ display:grid; gap:4px; min-width:0 }} .recess b {{ font-size:16px }} .recess p {{ margin:0; color:var(--muted); font-size:13px }}
.rb {{ font:800 12px/1 ui-monospace,Menlo,monospace; letter-spacing:.12em; color:var(--accent) }} .recess.night .rb {{ color:#4cb7ff }}
.rc {{ font:700 28px/1 ui-monospace,Menlo,monospace; color:var(--accent); font-variant-numeric:tabular-nums }}
.nextwrap, .chartwrap {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px; display:grid; gap:10px }}
.nextwrap h2, .chartwrap h2 {{ margin:0 }}
.nh {{ display:flex; justify-content:space-between; align-items:center; gap:12px }} .nh p {{ margin:4px 0 0; font-size:12px }}
.cd {{ font:700 26px/1 ui-monospace,Menlo,monospace; color:var(--accent); font-variant-numeric:tabular-nums }}
.ready {{ list-style:none; margin:0; padding:0; display:grid; gap:8px }}
.ready li {{ border:1px solid var(--line); border-radius:8px; padding:9px 11px; display:grid; gap:6px }}
.ready li.hot {{ border-color:var(--pos); background:#53f0a610 }} .ready li.warm {{ border-color:#f4c84a66 }}
.ready li.trade {{ border-color:#4cb7ff88 }} .ready li.off {{ opacity:.55 }}
.rl {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap }} .rl .cnt {{ margin-left:auto; color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums }}
.bias {{ font:700 10px/1 ui-monospace,Menlo,monospace; padding:3px 6px; border-radius:3px; border:1px solid currentColor }}
.bias.buy {{ color:var(--buy) }} .bias.sell {{ color:var(--sell) }}
.rbar {{ height:6px; border-radius:99px; background:var(--line); overflow:hidden }}
.rbar i {{ display:block; height:100%; border-radius:99px; background:var(--accent) }}
.ready li.hot .rbar i {{ background:var(--pos) }}
.ready p {{ margin:0; font-size:12.5px; color:var(--muted) }}
.chd {{ display:flex; justify-content:space-between; align-items:baseline; gap:10px; flex-wrap:wrap }} .chd span {{ font-variant-numeric:tabular-nums; font-weight:600 }}
.chart {{ position:relative }} .chart svg {{ width:100%; height:auto; display:block; touch-action:pan-y }}
.chart .gl {{ stroke:var(--line); stroke-width:1 }} .chart .yl, .chart .xl {{ fill:var(--muted); font-size:11px; text-anchor:end; font-variant-numeric:tabular-nums }}
.chart .xl {{ text-anchor:start }} .chart .xr {{ fill:var(--muted); font-size:11px; text-anchor:end }} .chart .base {{ stroke:var(--muted); stroke-dasharray:4 4; stroke-width:1 }}
.chart .bl {{ fill:var(--muted); font-size:11px; text-anchor:end }}
.chart .ln {{ fill:none; stroke:#4cb7ff; stroke-width:2; stroke-linejoin:round; stroke-linecap:round }}
.chart .end {{ fill:#4cb7ff; stroke:var(--panel); stroke-width:2 }}
.chart .mk {{ stroke:var(--panel); stroke-width:2 }} .chart .mk.win {{ fill:var(--pos) }} .chart .mk.loss {{ fill:var(--neg) }}
.chart .cross {{ stroke:var(--muted); stroke-width:1 }}
.ctip {{ position:absolute; top:6px; width:150px; background:#0f0c1dee; border:1px solid var(--line); border-radius:8px; padding:6px 9px;
  display:grid; gap:2px; font-size:12px; pointer-events:none }} .ctip[hidden] {{ display:none }} .ctip b {{ font-variant-numeric:tabular-nums }} .ctip span {{ color:var(--muted) }}
.legend {{ margin:0; font-size:12px }} .legend .win {{ color:var(--pos) }} .legend .loss {{ color:var(--neg) }}
.feedwrap {{ background:linear-gradient(180deg,#1a1436,#120e24); border:1px solid var(--line); border-radius:12px; padding:14px; display:grid; gap:10px }}
.feedhead {{ display:flex; justify-content:space-between; align-items:center; gap:10px }}
.feedhead h2 {{ margin:0 }}
.beat {{ font:600 11px/1 ui-monospace,Menlo,monospace; color:var(--muted) }} .beat.on {{ color:var(--pos) }} .beat.stale {{ color:var(--accent) }}
.feed {{ display:grid; gap:10px; max-height:560px; overflow-y:auto; padding-right:4px }}
.card {{ background:#0f0c1dcc; border:1px solid #2c2550; border-radius:10px; padding:10px 12px; display:grid; gap:8px }}
.card header {{ display:flex; justify-content:space-between; gap:8px; align-items:baseline }}
.card header b {{ font-size:14px }} .card header time {{ color:var(--muted); font-size:11px; white-space:nowrap }}
.card.scan {{ background:transparent; border-style:dashed }}
.msg {{ display:grid; grid-template-columns:10px 1fr; gap:8px; align-items:start }}
.dot {{ width:10px; height:10px; border-radius:50%; margin-top:5px; box-shadow:0 0 8px currentColor }}
.who {{ font-weight:700; font-size:12px; display:flex; gap:6px; align-items:center }}
.chip {{ font:700 10px/1 ui-monospace,Menlo,monospace; padding:2px 5px; border:1px solid currentColor; border-radius:3px }}
.chip.yes {{ color:var(--pos) }} .chip.no {{ color:var(--neg) }}
.say {{ font-size:13px; color:#e4def9 }}
.watch {{ display:grid; grid-template-columns:80px 1fr; gap:8px; font-size:12px; color:var(--muted) }} .watch b {{ color:var(--fg); font-weight:600 }}
.verdict {{ font:600 12px/1.4 ui-monospace,Menlo,monospace; padding:7px 9px; border-radius:6px; border:1px solid }}
.verdict.ok {{ color:var(--pos); background:#53f0a612 }} .verdict.rej {{ color:var(--neg); background:#ff6b7c12 }}
.feed .empty {{ color:var(--muted); margin:6px 0 }}
.fresh {{ animation:fin .4s ease-out both }} .fresh .msg, .fresh .verdict, .fresh .watch {{ animation:fin .4s ease-out both }}
@keyframes fin {{ from {{ opacity:0; transform:translateY(6px) }} to {{ opacity:1; transform:none }} }}
@media (prefers-reduced-motion: reduce) {{ .fresh, .fresh * {{ animation:none !important }} }}
details {{ color:var(--muted) }} .fine {{ color:var(--muted); font-size:12px; max-width:78ch }}
@media (max-width:720px) {{
  .tbl {{ background:none; border:0 }} .tbl table tr:first-child {{ display:none }}
  .tbl tr {{ display:grid; grid-template-columns:repeat(3,1fr); gap:6px 8px; background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px; margin-bottom:10px }}
  .tbl td {{ border:0; padding:0; text-align:left !important; min-width:0 }}
  .tbl td.mkt, .tbl td.empty {{ grid-column:1/-1 }}
  .tbl td[data-l]::before {{ content:attr(data-l); display:block; font-size:10px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted) }}
}}
.room {{ --ink:#1b1430; --c-quant:#7ccf8a; --c-risk:#e8565b; --c-mom:#f2a23c; --c-time:#5aa7e0; --c-doubt:#d9c3a5;
  background:radial-gradient(120% 90% at 50% 0%, #3a2a6b 0%, #1d1538 60%, #140f28 100%); color:#efe9ff;
  border-radius:14px; padding:16px; overflow:hidden; display:grid; gap:12px }}
.roomhead {{ display:grid; grid-template-columns:auto 1fr; gap:16px; align-items:end }}
.overall {{ display:grid; gap:2px; padding-right:16px; border-right:1px solid #4a3a85 }}
.overall b {{ font:800 40px/1 ui-sans-serif,-apple-system,sans-serif; color:#f4c84a; font-variant-numeric:tabular-nums; letter-spacing:-.02em }}
.overall small {{ color:#c9bdf0; font-size:11px }}
.overall .lbl {{ font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:#b6a8e6 }}
.stage {{ display:none; position:relative; border-radius:10px; overflow:hidden; touch-action:pan-y }}
.bubbles {{ position:absolute; inset:0; pointer-events:none; overflow:hidden }}
.bub {{ position:absolute; max-width:min(150px, 46%); padding:6px 9px 7px; border-radius:12px; background:#0d1322f0; color:#eaf2ff;
  font:600 11.5px/1.3 ui-sans-serif,-apple-system,"Segoe UI",sans-serif; border:1.5px solid var(--bc); box-shadow:0 6px 16px #0009;
  transform:translateY(-100%) scale(.6); transform-origin:var(--tx, 50%) 100%; opacity:0; transition:opacity .22s, transform .28s cubic-bezier(.2,1.5,.4,1) }}
.bub.on {{ opacity:1; transform:translateY(-100%) scale(1) }}
.bub::after {{ content:""; position:absolute; bottom:-6px; left:var(--tx, 50%); width:10px; height:10px; background:#0d1322;
  border-right:1.5px solid var(--bc); border-bottom:1.5px solid var(--bc); transform:translateX(-50%) rotate(45deg) }}
.bub b {{ display:block; font-size:9.5px; letter-spacing:.07em; text-transform:uppercase; color:var(--bc); margin-bottom:1px }}
.phasepill {{ position:absolute; left:10px; bottom:10px; pointer-events:none; font:700 11px/1 ui-sans-serif,-apple-system,sans-serif;
  color:#eaf2ff; background:#0d1322cc; border:1px solid #38e1ff66; padding:6px 9px; border-radius:99px; letter-spacing:.02em }}
.phasepill.hearing {{ border-color:#f4c84a; color:#f4c84a }} .phasepill.party {{ border-color:#ff4fd8; color:#ffd84a }}
.phasepill.wait, .phasepill.assemble {{ border-color:#ffb03b; color:#ffd59a }}
.partybanner {{ position:absolute; top:10px; left:50%; transform:translateX(-50%); width:max-content; max-width:calc(100% - 20px); text-align:center;
  pointer-events:none; background:linear-gradient(135deg,#3a1450ee,#14304aee); border:1.5px solid #ffd84a; border-radius:12px; padding:8px 14px;
  color:#fff; box-shadow:0 0 24px #ff4fd855; animation:bannerIn .5s cubic-bezier(.2,1.5,.4,1) both }}
.partybanner b {{ display:block; color:#ffd84a; font:800 12px/1.2 ui-sans-serif,-apple-system,sans-serif; letter-spacing:.12em }}
.partybanner span {{ display:block; font-weight:700; font-size:14px; margin-top:2px }} .partybanner small {{ color:#cfe3ff; font-size:11.5px }}
@keyframes bannerIn {{ from {{ opacity:0; transform:translate(-50%,-12px) scale(.8) }} }}
.roomctl {{ display:flex; gap:8px; flex-wrap:wrap }}
.roomctl button {{ font:700 13px/1 ui-sans-serif,-apple-system,sans-serif; color:#efe9ff; background:#2b2150; border:1px solid #6a58b8;
  border-radius:99px; padding:9px 13px; cursor:pointer }}
.roomctl button:focus-visible {{ outline:2px solid #f4c84a; outline-offset:2px }}
@media (prefers-reduced-motion: reduce) {{ .bub, .partybanner {{ transition:none; animation:none }} }}
.stage canvas {{ display:block; width:100% !important; cursor:grab }}
.is3d .stage {{ display:block }}
.is3d .table, .is3d .char, .is3d .seat .sign {{ display:none }}
.is3d .seats {{ margin-top:-4px }}
.is3d .seat:hover .why, .is3d .seat:focus .why {{ display:none }}
.mood {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; justify-content:center }}
.mood-btn {{ font:700 15px/1 ui-sans-serif,-apple-system,sans-serif; padding:12px 18px; border-radius:999px; border:2px solid; cursor:pointer;
  background:transparent; min-height:44px; transition:transform .12s, background .2s }}
.mood-btn:active {{ transform:scale(.95) }} .mood-btn:focus-visible {{ outline:2px solid #f4c84a; outline-offset:3px }}
.mood-btn.good {{ color:#53f0a6; border-color:#53f0a6 }} .mood-btn.good:hover {{ background:#53f0a61f }}
.mood-btn.bad {{ color:#ff6b7c; border-color:#ff6b7c }} .mood-btn.bad:hover {{ background:#ff6b7c1f }}
.mood-msg {{ flex-basis:100%; text-align:center; font-weight:600; min-height:1.4em; opacity:0; transition:opacity .3s }}
.mood-msg.show {{ opacity:1 }} .mood-msg.cheers {{ color:#f6d36b }} .mood-msg.sad {{ color:#ff9aa5 }}
.mood-tg {{ margin:0; text-align:center; font-size:12px; color:#9c90c8 }} .mood-tg a {{ color:#c9bdf0 }}
.room.flat-cheers .char {{ animation:hop .5s ease-in-out 4 }} .room.flat-sad .char {{ animation:droop 2.5s ease-in-out 1 }}
@keyframes hop {{ 50% {{ transform:translateY(-12px) rotate(-4deg) }} }}
@keyframes droop {{ 30%,70% {{ transform:translateY(6px) rotate(6deg); filter:grayscale(.6) }} }}
.stamp3d {{ position:absolute; left:50%; bottom:14px; transform:translateX(-50%); pointer-events:none; opacity:0; transition:opacity .4s }}
.stamp3d.show {{ opacity:1 }}
.stamp3d .stamp {{ animation:none; transform:none; font:600 12px/1 ui-monospace,SFMono-Regular,Menlo,monospace; letter-spacing:.22em;
  padding:8px 14px; border-width:1px; border-radius:2px; background:#0e0a1ecc; box-shadow:0 0 18px currentColor }}
.tip3d {{ position:absolute; left:10px; top:10px; max-width:min(260px, 70%); display:grid; gap:3px; background:#2b2150ee;
  border:1px solid #6a58b8; border-radius:10px; padding:8px 10px; font-size:12px; color:#efe9ff }}
.tip3d[hidden] {{ display:none }}
.tip3d b {{ font-size:13px }} .tip3d small {{ color:#b6a8e6 }}
.docket {{ display:grid; gap:2px; min-width:0 }}
.docket .stale {{ color:#f4c84a; margin-top:4px }} .docket .stale[hidden] {{ display:none }}
.docket .lbl {{ font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:#b6a8e6 }}
.docket a, .docket span {{ color:#fff; font-weight:650; font-size:16px; text-decoration:none }}
.docket small {{ color:#c9bdf0 }}
.floor {{ position:relative; display:grid; gap:10px }}
.table {{ position:relative; height:64px; margin:0 6%; border-radius:50%; background:radial-gradient(closest-side, #f4c84a55, #f4c84a10 70%, transparent);
  border:2px solid #f4c84a66; box-shadow:0 0 30px #f4c84a33 inset; display:grid; place-items:center }}
.gavel {{ position:absolute; left:14%; top:8px; width:34px; height:14px; background:#8a5a2b; border-radius:4px; transform-origin:100% 50%;
  animation:slam 1.2s 1.6s ease-in both }}
.gavel::after {{ content:""; position:absolute; right:-20px; top:4px; width:22px; height:5px; background:#5e3b1a; border-radius:3px }}
.stamp {{ font:800 15px/1 ui-sans-serif,-apple-system,sans-serif; letter-spacing:.08em; padding:7px 12px; border-radius:6px;
  border:3px solid currentColor; transform:rotate(-6deg) scale(0); animation:stamp .35s 2.5s cubic-bezier(.2,1.6,.4,1) both }}
.stamp.ok {{ color:#6ff0a8 }} .stamp.rej {{ color:#ff8a8e }}
.seats {{ display:grid; grid-template-columns:repeat(5,1fr); gap:6px }}
.seat {{ all:unset; cursor:pointer; display:grid; justify-items:center; gap:3px; text-align:center; position:relative; min-width:0 }}
.seat:focus-visible {{ outline:2px solid #f4c84a; outline-offset:4px; border-radius:8px }}
.char {{ width:min(76px,100%); height:auto; animation:bob 2.6s calc(var(--i) * .3s) ease-in-out infinite; filter:drop-shadow(0 6px 6px #0006) }}
.char .hand {{ transform-origin:50px 54px; animation:tick 4s linear infinite }}
.sign {{ font:800 11px/1 ui-sans-serif,-apple-system,sans-serif; padding:5px 8px; border-radius:99px; background:#2b2150; border:2px solid;
  opacity:0; transform:translateY(10px); animation:raise .4s calc(.3s + var(--i) * .22s) ease-out both }}
.sign.yes {{ color:#6ff0a8 }} .sign.no {{ color:#ff8a8e }} .sign.abs {{ color:#a99fc9 }}
.nm {{ font-weight:700; font-size:12px }} .acc {{ font-size:11px; color:#b6a8e6 }}
.why {{ display:none; font-size:11px; color:#efe9ff; background:#2b2150; border:1px solid #4a3a85; border-radius:8px; padding:6px 8px; max-width:140px }}
.seat:focus .why, .seat:hover .why {{ display:block; position:absolute; top:100%; z-index:2; margin-top:4px }}
.hint {{ margin:0; font-size:11px; color:#9c90c8; text-align:center }}
@keyframes bob {{ 0%,100% {{ transform:translateY(0) }} 50% {{ transform:translateY(-5px) }} }}
@keyframes raise {{ to {{ opacity:1; transform:translateY(0) }} }}
@keyframes slam {{ 0% {{ transform:rotate(-40deg) }} 60% {{ transform:rotate(12deg) }} 75% {{ transform:rotate(4deg) }} 100% {{ transform:rotate(8deg) }} }}
@keyframes stamp {{ to {{ transform:rotate(-6deg) scale(1) }} }}
@keyframes tick {{ to {{ transform:rotate(360deg) }} }}
@media (prefers-reduced-motion: reduce) {{ .room * {{ animation-duration:0s !important; animation-delay:0s !important; animation-iteration-count:1 !important }} }}
@media (max-width:720px) {{ .nm {{ font-size:11px }} .acc {{ font-size:10px }}
  .roomhead {{ grid-template-columns:1fr; gap:10px }} .overall {{ border-right:0; padding-right:0; border-bottom:1px solid #4a3a85; padding-bottom:10px }} }}
</style></head><body><div class="wrap">
{office.tabbar("war")}
<header><div><h1>Vault FX</h1><p>Updated {updated} · trend following on 15-minute and 1-hour candles{f" · started with ${START_BALANCE:,.0f}" if START_BALANCE else ""}</p></div>
  <div><span class="badge {mode.lower()}">{mode}</span> {f'<span class="badge paused">PAUSED · {html.escape(paused)}</span>' if paused else ''}</div></header>
{council_room(st)}
{recess_panel(st)}
{next_panel(st)}
{feed_panel(st)}
<div class="stats">
  <div class="stat"><span>{'Bankroll' if START_BALANCE else 'Account value'}</span><b>{'—' if nav is None else f'${nav:,.2f}'}</b></div>
  <div class="stat"><span>Today</span><b class="{'pos' if (today or 0) >= 0 else 'neg'}">{m(today)}</b></div>
  <div class="stat"><span>Open P&amp;L</span><b class="{'pos' if acc.get('unrealized', 0) >= 0 else 'neg'}">{m(acc.get('unrealized'))}</b></div>
  <div class="stat"><span>Record</span><b>{rec['wins']}W–{rec['losses']}L</b></div>
  <div class="stat"><span>Trade size</span><b class="{'neg' if streak_status(st) != 'normal' else ''}">{ {'normal': f'{RISK_PCT:.0%} risk', 'half': f'{RISK_PCT / 2:.1%} (half)', 'timeout': 'Timeout'}[streak_status(st)] }</b></div>
  <div class="stat"><span>Closed P&amp;L</span><b class="{'pos' if rec['pl'] >= 0 else 'neg'}">{m(rec['pl'])}</b></div>
</div>
{bankroll_chart(st)}
<section><h2>Markets</h2><div class="pairs">{''.join(pair_card(p) for p in PAIRS)}</div></section>
<section><h2>Open trades</h2><div class="tbl"><table><tr><th>Pair</th><th>Side</th><th>Units</th><th>Entry</th><th>Now</th><th>Stop</th><th>Target</th><th>P&amp;L</th></tr>{open_rows}</table></div></section>
<section><h2>Big news ahead</h2><ul class="news">{news_rows}</ul><p class="fine">No new trades from {NEWS_BEFORE_MIN} min before to {NEWS_AFTER_MIN} min after these.</p></section>
{learning_html(st)}
{inspector_link()}
<section><h2>Who called it?</h2><p class="fine">{html.escape(learn_line)}</p><div class="tbl"><table><tr><th>Voter</th><th>Called it</th><th>Vote weight</th><th>Right / scored</th></tr>{board_rows}</table></div></section>
<section><h2>Recent closed trades</h2><div class="tbl"><table><tr><th>Pair</th><th>Side</th><th>Units</th><th>Result</th></tr>{closed_rows}</table></div></section>
<details><summary>Scan log</summary><ul>{''.join(f'<li>{html.escape(l)}</li>' for l in log)}</ul></details>
<p class="fine">Strategy: the EMA20/EMA50 trend on 1-hour or 15-minute candles sets the direction (15-minute setups must also agree with the 1-hour and 4-hour trends); a fresh crossover or a pullback to the EMA20 creates a setup.
The council (Quant = ADX strength, Risk = spread/exposure/limits, Momentum = MACD, Time = RSI + trading hours, Doubt = 4-hour trend)
must approve with Risk in favor (4 of 5 at equal weights; voters with better records count more once they have {LEARN_MIN_CALLS} scored calls).
Pair + setup combos that average below 0R over their last {BENCH_MIN} results are benched until they recover. No new trades around high-impact news. Each trade risks {RISK_PCT:.0%} of the account with a stop at {STOP_ATR:g}× ATR and a target at {TARGET_ATR:g}× ATR;
the stop moves to entry at +{BREAKEVEN_AT_R:g}R. Limits: {MAX_OPEN_TRADES} open trades, {DAILY_LOSS_LIMIT:.0%} daily loss stop, {MAX_DRAWDOWN:.0%} drawdown pause.
Rejected setups are scored against later prices so "Who called it?" learns from both sides. Forex is leveraged and most retail accounts lose money.
This is software, not financial advice.</p>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Vault FX: trend-following forex bot for OANDA.")
    ap.add_argument("--out", default="site/index.html")
    ap.add_argument("--mode", default="auto", choices=["auto", "full"],
                    help="auto: full scan every 15 min, trade checks in between; full: always scan")
    args = ap.parse_args()
    mode = "full" if args.mode == "full" else plan(load_state())
    print(f"Mode: {mode}")

    def output(deploy):
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
                f.write(f"deploy={'true' if deploy else 'false'}\n")

    if mode == "skip":
        print("No open trades and the last full scan was under 15 minutes ago. Nothing to do.")
        output(False)
        return
    log = []
    try:
        st = run(log, full=(mode == "full"))
    except Exception as e:                      # never leave the dashboard blank
        log.append(f"Run failed: {e}")
        st = load_state()
    try:
        record_errors(log, st); save_state(st)
    except Exception:
        pass
    for line in log:
        print(line)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(build_html(st, log))
    try:                                         # Inspector's Office tab
        ctx = {"files": inspector_files(), "when": _FILES.get("when"), "cases": cs.CASES, "status": proving_status,
               "names": NAMES, "tz": LOCAL_TZ, "proving_days": PROVING_DAYS, "proving_min": PROVING_MIN_TRADES,
               "proving_avg": PROVING_MIN_AVG, "accuracy": accuracy_change(st)}
        with open(os.path.join(os.path.dirname(os.path.abspath(args.out)), "office.html"), "w", encoding="utf-8") as f:
            f.write(office.build_office(st, ctx))
    except Exception as e:
        print(f"Office page skipped: {e}")
    if os.path.exists("inspector.html"):        # Inspector's latest backtest report rides along with the dashboard
        try:
            import shutil
            shutil.copy("inspector.html", os.path.join(os.path.dirname(os.path.abspath(args.out)), "inspector.html"))
        except OSError:
            pass
    deploy = mode == "full" and (time.time() - st.get("last_deploy", 0) >= DEPLOY_EVERY_MIN * 60 or st.pop("_changed", False))
    st.pop("_changed", None)
    if deploy:
        st["last_deploy"] = time.time()
    try:
        save_state(st)
    except Exception:
        pass
    output(deploy)                              # GitHub Pages has hourly limits; the live feed refreshes on its own


if __name__ == "__main__":
    main()
