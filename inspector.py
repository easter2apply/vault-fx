#!/usr/bin/env python3
"""
Inspector: replays months of real OANDA history through Vault FX's exact rules
and grades the strategy and every council officer.

    python inspector.py --months 6

It never trades. It reads candles (mid, bid and ask), finds every setup the live
bot would have seen, lets the council vote with the same code, and plays each
setup forward on 15-minute candles to see whether the stop or the target came first.

Output: inspector.html (the report), inspector.json (the numbers), and a Telegram summary.
Needs OANDA_API_TOKEN and OANDA_ACCOUNT_ID (same secrets as the bot). TELEGRAM_BOT_TOKEN is optional.
"""

import argparse
import datetime as dt
import html
import json
import os
import sys
import time

import vault_fx as vf

UTC = dt.timezone.utc
WINDOW = 300                 # the live bot looks at the last 300 candles of each timeframe
HORIZON_DAYS = 10            # a setup still open after 10 days is marked at the market
MIN_N = 30                   # don't call an officer helpful or harmful on fewer results than this
EDGE = 0.10                  # an officer "helps" if YES setups beat NO setups by at least 0.10R


# ───────────────────────────── data ──────────────────────────────────────────

def ts(s):
    return int(dt.datetime.fromisoformat(s[:19]).replace(tzinfo=UTC).timestamp())


def fetch(token, pair, gran, start):
    """Every complete candle from `start` until now, with mid, bid and ask."""
    out, frm, first, pages = [], start, True, 0
    while True:
        params = {"granularity": gran, "price": "MBA", "count": 5000,
                  "from": frm.strftime("%Y-%m-%dT%H:%M:%SZ")}
        if not first:
            params["includeFirst"] = "false"
        rows = vf.http("GET", f"{vf.OANDA}/instruments/{pair}/candles", token, params)["candles"]
        first = False
        if not rows:
            break
        for r in rows:
            if not r.get("complete"):
                continue
            out.append({"t": r["time"][:19], "o": float(r["mid"]["o"]), "h": float(r["mid"]["h"]),
                        "l": float(r["mid"]["l"]), "c": float(r["mid"]["c"]),
                        "bh": float(r["bid"]["h"]), "bl": float(r["bid"]["l"]), "bc": float(r["bid"]["c"]),
                        "ah": float(r["ask"]["h"]), "al": float(r["ask"]["l"]), "ac": float(r["ask"]["c"])})
        last = dt.datetime.fromisoformat(rows[-1]["time"][:19]).replace(tzinfo=UTC)
        pages += 1
        if last <= frm or pages > 40 or last >= dt.datetime.now(UTC) - dt.timedelta(hours=2):
            break                                    # keep paging until we reach the present (OANDA may send short pages)
        frm = last
        time.sleep(0.2)
    for c in out:
        c["ts"] = ts(c["t"])
    return out


# ───────────────────────────── playing a setup forward ───────────────────────

def play(m15, start_i, d, entry, sl, tp, breakeven):
    """Walk 15-min candles after start_i. Returns (R, exit index, how). Longs exit on the bid, shorts on the ask."""
    risk = abs(entry - sl)
    if risk <= 0:
        return 0.0, start_i, "bad"
    stop = sl
    end_ts = m15[start_i]["ts"] + HORIZON_DAYS * 86400
    j = start_i + 1
    while j < len(m15) and m15[j]["ts"] <= end_ts:
        c = m15[j]
        lo, hi = (c["bl"], c["bh"]) if d == 1 else (c["al"], c["ah"])
        hit_sl = lo <= stop if d == 1 else hi >= stop
        hit_tp = hi >= tp if d == 1 else lo <= tp
        if hit_sl:                                   # stop first (or both in one candle) counts against us
            return round((stop - entry) * d / risk, 3), j, "stop" if stop != entry else "breakeven"
        if hit_tp:
            return round((tp - entry) * d / risk, 3), j, "target"
        if breakeven and stop != entry and ((hi - entry) if d == 1 else (entry - lo)) >= vf.BREAKEVEN_AT_R * risk:
            stop = entry
        j += 1
    j = min(j, len(m15) - 1)
    px = m15[j]["bc"] if d == 1 else m15[j]["ac"]
    return round((px - entry) * d / risk, 3), j, "timeout"


# exit plans the lab tries: (stop size vs today's stop, target in R or None, special handling)
EXITS = {
    "today's exit (1.5R, breakeven at +1R)": (1.0, 1.5, "be"),
    "1R target": (1.0, 1.0, None),
    "2R target": (1.0, 2.0, None),
    "3R target, breakeven at +1R": (1.0, 3.0, "be"),
    "tighter stop, 2R target": (0.75, 2.0, None),
    "wider stop, 1R target": (1.5, 1.0, None),
    "trailing stop, no target": (1.0, None, "trail"),
    "1.5R target, out after 24h": (1.0, 1.5, "day"),
}


def play_v(m15, start_i, d, entry, risk, target_r, mode):
    """Like play(), for the lab's exit plans. Returns (R, exit timestamp)."""
    stop = entry - d * risk
    tp = entry + d * target_r * risk if target_r else None
    end_ts = m15[start_i]["ts"] + (86400 if mode == "day" else HORIZON_DAYS * 86400)
    best = entry
    j = start_i + 1
    while j < len(m15) and m15[j]["ts"] <= end_ts:
        c = m15[j]
        lo, hi = (c["bl"], c["bh"]) if d == 1 else (c["al"], c["ah"])
        if (lo <= stop) if d == 1 else (hi >= stop):
            return round((stop - entry) * d / risk, 3), c["ts"]
        if tp is not None and ((hi >= tp) if d == 1 else (lo <= tp)):
            return round(target_r, 3), c["ts"]
        fav = hi if d == 1 else lo
        best = max(best, fav) if d == 1 else min(best, fav)
        if mode == "be" and (best - entry) * d >= risk and (stop - entry) * d < 0:
            stop = entry
        elif mode == "trail" and (best - entry) * d >= risk:
            stop = max(stop, best - risk) if d == 1 else min(stop, best + risk)
        j += 1
    j = min(j, len(m15) - 1)
    px = m15[j]["bc"] if d == 1 else m15[j]["ac"]
    return round((px - entry) * d / risk, 3), m15[j]["ts"]


# ───────────────────────────── the "50 Pips a Day" strategy (from the article) ──
# Turned into exact rules so it can be tested:
#   trend:   4-hour trend (EMA 50/200) and 1-hour trend (EMA 20/50) point the same way
#   level:   the 15-min candle pulls back to a recent support (lowest low of the previous 20 candles, within 0.3 ATR)
#            in an uptrend, or a recent resistance (highest high) in a downtrend
#   trigger: that candle is a pin bar (long wick, small body, closes in the trend's third) or an engulfing candle
#   filter:  RSI not stretched (below 70 for buys, above 30 for sells); 7:00–17:00 GMT; first signal per pair per day
#   exits:   take profit 50 pips, stop 25–50 pips
FIFTY_EXITS = {
    "stop 25 pips, target 50 (1:2, the article's pick)": (25, 50, None),
    "stop 30 pips, target 50": (30, 50, None),
    "stop 50 pips, target 50 (1:1)": (50, 50, None),
    "stop 25, target 50, close after 24h": (25, 50, "day"),
}


def pip_size(pair, pip_loc):
    if pair == "XAU_USD":
        return 0.1                                              # gold: count $0.10 as a pip, so 50 pips = $5
    return 0.01 if pair.endswith("JPY") else 0.0001


def fifty_signal(m15, j, a, a15):
    """Returns +1 / -1 when the article's entry appears on 15-min candle j, else 0."""
    d = a["trend"]
    if a["h4_trend"] != d or not a15.get("atr") or j < 22:
        return 0
    c, p = m15[j], m15[j - 1]
    rng = c["h"] - c["l"]
    if rng <= 0:
        return 0
    body = abs(c["c"] - c["o"])
    prev = m15[j - 21:j - 1]
    tol = 0.3 * a15["atr"]
    r = a15.get("rsi") or 50
    if d == 1:
        level = min(x["l"] for x in prev)
        at_level = abs(c["l"] - level) <= tol
        pin = (min(c["o"], c["c"]) - c["l"]) >= 2 * body and (min(c["o"], c["c"]) - c["l"]) >= 0.6 * rng and c["c"] >= c["l"] + rng * 2 / 3
        engulf = p["c"] < p["o"] and c["c"] > c["o"] and c["c"] >= p["o"] and c["o"] <= p["c"]
        return 1 if at_level and (pin or engulf) and r < 70 else 0
    level = max(x["h"] for x in prev)
    at_level = abs(c["h"] - level) <= tol
    pin = (c["h"] - max(c["o"], c["c"])) >= 2 * body and (c["h"] - max(c["o"], c["c"])) >= 0.6 * rng and c["c"] <= c["h"] - rng * 2 / 3
    engulf = p["c"] > p["o"] and c["c"] < c["o"] and c["c"] <= p["o"] and c["o"] >= p["c"]
    return -1 if at_level and (pin or engulf) and r > 30 else 0


# ───────────────────────────── replay one pair ───────────────────────────────

def replay(pair, h1, h4, m15, test_from, pip_loc, log, fifty=None):
    setups = []
    fifty_days = set()
    pip = pip_size(pair, pip_loc)
    i1 = i4 = 0                                      # how many H1 / H4 candles are complete
    a_cache = {}
    busy_until = -1                                  # index in m15 when this pair's current trade closes
    lb_done = set()
    inf = {"pipLocation": pip_loc}
    t0 = time.time()

    def h1_analysis(n1, n4):
        key = (n1, n4)
        if key not in a_cache:
            a_cache.clear()
            a_cache[key] = vf.analyze(h1[max(0, n1 - WINDOW):n1], h4[max(0, n4 - WINDOW):n4])
        return a_cache[key]

    for j, c in enumerate(m15):
        close_ts = c["ts"] + 900
        while i1 < len(h1) and h1[i1]["ts"] + 3600 <= close_ts:
            i1 += 1
        while i4 < len(h4) and h4[i4]["ts"] + 14400 <= close_ts:
            i4 += 1
        if close_ts < test_from or i1 < 210 or i4 < 210 or j < 210:
            continue
        now = dt.datetime.fromtimestamp(close_ts, UTC)
        spread = c["ac"] - c["bc"]
        a = h1_analysis(i1, i4)
        decisions = []
        # 1-hour setups: decided when an H1 candle has just closed
        if h1[i1 - 1]["ts"] + 3600 == close_ts and a["setup"] and a["atr"]:
            hc = h1[i1 - 1]
            decisions.append(("H1", a, hc["ac"] - hc["bc"]))
        # 15-minute setups, filtered by the 1-hour and 4-hour trends
        mw = m15[max(0, j - WINDOW + 1):j + 1]
        a15 = vf.analyze(mw, h1[max(0, i1 - WINDOW):i1])
        a15.update(extra_trend=a["h4_trend"], htf_label="1-hour and 4-hour trends", spread_max=vf.MAX_SPREAD_ATR_15M)
        if fifty is not None and 7 <= now.hour - (1 if now.minute == 0 else 0) < 17 and now.strftime("%Y-%m-%d") not in fifty_days:
            fd = fifty_signal(m15, j, a, a15)
            if fd:
                fifty_days.add(now.strftime("%Y-%m-%d"))
                entry = c["ac"] if fd == 1 else c["bc"]
                fifty.append({"pair": pair, "t": c["t"], "ts": close_ts, "hour": c["ts"] // 3600 % 24, "adx": a["adx"] or 0,
                              "spread_pips": spread / pip,
                              "x": [play_v(m15, j, fd, entry, sl_p * pip, tp_p / sl_p, mode) for (sl_p, tp_p, mode) in FIFTY_EXITS.values()]})
        if a15["setup"] and a15["atr"]:
            a15["setup"] += " 15m"
            decisions.append(("M15", a15, spread))
        # London breakout, one decision per pair per day
        day = now.strftime("%Y-%m-%d")
        if day not in lb_done:
            lbk = vf.london_breakout(pair, mw, a, spread, now)
            if lbk:
                lb_done.add(day)
                decisions.append(("LB", lbk, spread))

        for tf, s, sp in decisions:
            free = j >= busy_until
            why = "" if free else "already in a trade on this pair"
            if tf == "LB":
                votes = vf.council_breakout(s, free, why, now, pair, inf)
            else:
                votes = vf.council(s, sp, free, why, now, pair)
            d = s["trend"]
            entry = c["ac"] if d == 1 else c["bc"]
            if s.get("sl_price") is not None:
                sl = s["sl_price"]
                tp = entry + d * (vf.TARGET_ATR / vf.STOP_ATR) * abs(entry - sl)
            else:
                sl = entry - d * vf.STOP_ATR * s["atr"]
                tp = entry + d * vf.TARGET_ATR * s["atr"]
            if (sl - entry) * d >= 0:
                continue
            ok = vf.approved(votes)
            r_plain, _, how_plain = play(m15, j, d, entry, sl, tp, breakeven=False)
            rec = {"pair": pair, "t": c["t"], "tf": tf, "setup": s["setup"], "dir": d, "votes": {n: o for n, (o, _) in votes.items()},
                   "approved": ok, "r": r_plain, "how": how_plain, "free": free}
            # lab data: how the council would vote with the pair free, plus every exit plan's result
            hv = vf.council_breakout(s, True, "", now, pair, inf) if tf == "LB" else vf.council(s, sp, True, "", now, pair)
            risk0 = abs(entry - sl)
            rec["lab"] = {"ts": close_ts, "hour": now.hour, "adx": (s.get("h1_adx") if tf == "LB" else s.get("adx")) or 0,
                          "ok": vf.approved(hv), "risk_ok": hv["Risk"][0] is True,
                          "x": [play_v(m15, j, d, entry, risk0 * k, tr, mode) for (k, tr, mode) in EXITS.values()]}
            if ok:
                r_live, k, how = play(m15, j, d, entry, sl, tp, breakeven=True)
                rec.update(r_trade=r_live, how_trade=how)
                busy_until = k + 1
            setups.append(rec)
    log.append(f"{pair}: {len(setups)} setups, {sum(1 for s in setups if s['approved'])} trades ({time.time() - t0:.0f}s)")
    return setups


# ───────────────────────────── grading ───────────────────────────────────────

def stats(rs):
    n = len(rs)
    if not n:
        return {"n": 0, "win": None, "avg": None, "total": 0.0}
    return {"n": n, "win": sum(1 for r in rs if r > 0) / n, "avg": sum(rs) / n, "total": sum(rs)}


def max_dd(rs):
    peak = cur = dd = 0.0
    for r in rs:
        cur += r
        peak = max(peak, cur)
        dd = max(dd, peak - cur)
    return dd


def approved_without(votes, drop):
    v = {n: o for n, o in votes.items() if n != drop}
    if drop != "Risk" and v.get("Risk") is not True:
        return False
    yes = sum(1 for o in v.values() if o is True)
    no = sum(1 for o in v.values() if o is False)
    return yes >= 3 and yes / max(1, yes + no) >= vf.COUNCIL_MIN_SHARE - 1e-9


def grade(setups, months):
    trades = [s for s in setups if s["approved"]]
    trades.sort(key=lambda s: s["t"])
    rejected = [s for s in setups if not s["approved"] and s["free"]]       # rejections that weren't just "pair busy"
    out = {"months": months, "all": stats([s["r"] for s in setups]), "trades": stats([s["r_trade"] for s in trades]),
           "rejected": stats([s["r"] for s in rejected]), "max_dd": max_dd([s["r_trade"] for s in trades])}
    out["dollars"] = out["trades"]["total"] * vf.START_BALANCE * vf.RISK_PCT
    out["per_month"] = out["trades"]["n"] / max(months, 1e-9)
    # officers: how did setups do when this officer said YES vs NO?
    off = {}
    for n in vf.VOTERS:
        yes = [s["r"] for s in setups if s["votes"].get(n) is True and (n != "Risk" or s["free"])]
        no = [s["r"] for s in setups if s["votes"].get(n) is False and (n != "Risk" or s["free"])]
        right = sum(1 for s in setups if s["votes"].get(n) is not None and (s["votes"][n] is True) == (s["r"] > 0))
        called = sum(1 for s in setups if s["votes"].get(n) is not None)
        sy, sn = stats(yes), stats(no)
        diff = (sy["avg"] - sn["avg"]) if sy["n"] and sn["n"] else None
        if diff is None or min(sy["n"], sn["n"]) < MIN_N:
            verdict = "not enough data"
        elif diff >= EDGE:
            verdict = "helps"
        elif diff <= -EDGE:
            verdict = "hurts"
        else:
            verdict = "no real effect"
        without = [s["r"] for s in setups if s["free"] and approved_without(s["votes"], n)]
        off[n] = {"yes": sy, "no": sn, "diff": diff, "verdict": verdict, "acc": right / called if called else None,
                  "without": stats(without)}
    out["officers"] = off
    out["council_now"] = stats([s["r"] for s in setups if s["free"] and s["approved"]])     # same basis as the what-ifs
    out["take_all"] = stats([s["r"] for s in setups if s["free"]])
    groups = {}
    for key in ("setup", "pair"):
        g = {}
        for s in trades:
            g.setdefault(s[key], []).append(s["r_trade"])
        groups[key] = {k: stats(v) for k, v in g.items()}
        g2 = {}
        for s in setups:
            g2.setdefault(s[key], []).append(s["r"])
        groups[key + "_all"] = {k: stats(v) for k, v in g2.items()}
    out["groups"] = groups
    # monthly R for approved trades
    mon = {}
    for s in trades:
        mon.setdefault(s["t"][:7], []).append(s["r_trade"])
    out["monthly"] = {k: stats(v) for k, v in sorted(mon.items())}
    out["advice"] = advice(out)
    return out


def advice(g):
    tips = []
    t = g["trades"]
    if t["n"] < 30:
        tips.append(f"Only {t['n']} trades in the test. That's too few to trust; rerun with more months.")
    elif t["avg"] > 0.1:
        tips.append(f"The strategy made money in the replay: {t['avg']:+.2f}R per trade over {t['n']} trades. Keep running it on practice.")
    elif t["avg"] > -0.05:
        tips.append(f"The strategy roughly broke even ({t['avg']:+.2f}R per trade). No real edge yet; don't add real money.")
    else:
        tips.append(f"The strategy lost money in the replay ({t['avg']:+.2f}R per trade over {t['n']} trades). It needs changes before real money.")
    if g["rejected"]["n"] >= MIN_N and t["n"] >= MIN_N:
        d = (t["avg"] or 0) - (g["rejected"]["avg"] or 0)
        tips.append("The council is helping: trades it took beat setups it rejected by {:+.2f}R.".format(d) if d > 0.05 else
                    "The council isn't adding much: rejected setups did about as well as approved ones." if d > -0.05 else
                    "The council is hurting: the setups it rejected did better than the ones it took.")
    base = g["council_now"]
    for n, o in g["officers"].items():
        if o["verdict"] == "hurts":
            w = o["without"]
            better = w["n"] and base["n"] and w["avg"] > base["avg"] + 0.03
            tips.append(f"{n} hurts: setups it approves do {o['diff']:+.2f}R worse than ones it blocks."
                        + (f" Without {n}, the council would have averaged {w['avg']:+.2f}R (vs {base['avg']:+.2f}R)." if better else ""))
        elif o["verdict"] == "helps":
            tips.append(f"{n} earns its seat: its YES setups beat its NO setups by {o['diff']:+.2f}R.")
    for kind in ("setup", "pair"):
        rows = [(k, s) for k, s in g["groups"][kind + "_all"].items() if s["n"] >= MIN_N]
        bad = sorted([r for r in rows if r[1]["avg"] <= -0.15], key=lambda r: r[1]["avg"])[:2]
        good = sorted([r for r in rows if r[1]["avg"] >= 0.15], key=lambda r: -r[1]["avg"])[:1]
        for k, s in bad:
            what = f"{k} setups" if kind == "setup" else vf.NAMES.get(k, k)
            tips.append(f"{what} lose on their own ({s['avg']:+.2f}R over {s['n']}). Consider turning {'them' if kind == 'setup' else 'it'} off.")
        for k, s in good:
            what = f"{k} setups are" if kind == "setup" else f"{vf.NAMES.get(k, k)} is"
            tips.append(f"Best {kind}: {what} the bright spot ({s['avg']:+.2f}R over {s['n']}).")
    tips.append("Careful: any change picked from this same history can look better here than it will be live. "
                "Make one change at a time and let the next few weeks confirm it.")
    return tips


# ───────────────────────────── the lab ───────────────────────────────────────

SETUP_SETS = {
    "every setup type": lambda s: True,
    "no 15-minute setups": lambda s: s["tf"] != "M15",
    "1-hour setups only": lambda s: s["tf"] == "H1",
    "1-hour pullbacks + London breakouts": lambda s: s["setup"] in ("pullback", "London breakout"),
    "London breakouts only": lambda s: s["tf"] == "LB",
}
HOURS = {
    "any hour": None,
    "3 AM–noon ET": (7, 16),
    "8 AM–noon ET (London + New York)": (12, 16),
    "3–7 AM ET (London morning)": (7, 11),
}
COUNCILS = {"council approves": "ok", "Risk approves (spread check only)": "risk_ok", "no council": None}
TRENDS = {"any trend": 0, "strong trend only (ADX 25+)": 25}


def simulate(cands, xi, split_ts, pairs=None):
    """One trade per pair at a time. Returns (train R list, test R list, per-pair train R)."""
    busy, train, test, per_pair = {}, [], [], {}
    for s in cands:
        if pairs is not None and s["pair"] not in pairs:
            continue
        L = s["lab"]
        if L["ts"] < busy.get(s["pair"], 0):
            continue
        r, exit_ts = L["x"][xi]
        busy[s["pair"]] = exit_ts + 1
        if L["ts"] < split_ts:
            train.append(r); per_pair.setdefault(s["pair"], []).append(r)
        else:
            test.append(r)
    return train, test, per_pair


def lab(setups, log):
    rows = sorted([s for s in setups if "lab" in s], key=lambda s: s["lab"]["ts"])
    if len(rows) < 200:
        return None
    t0, t1 = rows[0]["lab"]["ts"], rows[-1]["lab"]["ts"]
    split_ts = t0 + (t1 - t0) * 2 / 3
    exits = list(EXITS)
    results = []
    started = time.time()
    for sn, sf in SETUP_SETS.items():
        for hn, hr in HOURS.items():
            for cn, ck in COUNCILS.items():
                for tn, tmin in TRENDS.items():
                    cands = [s for s in rows if sf(s) and (hr is None or hr[0] <= s["lab"]["hour"] < hr[1])
                             and (ck is None or s["lab"][ck]) and s["lab"]["adx"] >= tmin]
                    if len(cands) < 40:
                        continue
                    for xi, xn in enumerate(exits):
                        tr, te, pp = simulate(cands, xi, split_ts)
                        rule = {"setups": sn, "hours": hn, "council": cn, "trend": tn, "exit": xn, "pairs": "all pairs"}
                        results.append((rule, stats(tr), stats(te)))
                        winners = sorted(p for p, rs in pp.items() if len(rs) >= 10 and sum(rs) / len(rs) > 0)
                        if winners and len(winners) < len(pp):
                            tr2, te2, _ = simulate(cands, xi, split_ts, set(winners))
                            rule2 = dict(rule, pairs="only " + ", ".join(vf.NAMES.get(p, p) for p in winners))
                            results.append((rule2, stats(tr2), stats(te2)))
    log.append(f"Lab: tried {len(results)} rule sets in {time.time() - started:.0f}s")
    train_months = (split_ts - t0) / (30.4 * 86400)
    test_months = (t1 - split_ts) / (30.4 * 86400)
    min_train = max(40, int(8 * train_months))           # at least ~2 trades a week, so it's not a fluke
    ok = [r for r in results if r[1]["n"] >= min_train and r[2]["n"] >= 15]
    ok.sort(key=lambda r: -(r[1]["avg"] or -9))
    top = ok[:10]
    base = next((r for r in results if r[0] == {"setups": "every setup type", "hours": "any hour", "council": "council approves",
                                                 "trend": "any trend", "exit": exits[0], "pairs": "all pairs"}), None)
    champ = top[0] if top else None
    held = sum(1 for r in top if (r[2]["avg"] or -9) > 0.05)
    passed = bool(champ and (champ[2]["avg"] or -9) >= 0.05 and champ[2]["total"] > 0)
    fmt = lambda ts_: dt.datetime.fromtimestamp(ts_, UTC).strftime("%b %d")
    return {"top": [{"rule": r[0], "train": r[1], "test": r[2]} for r in top],
            "base": {"rule": base[0], "train": base[1], "test": base[2]} if base else None,
            "champ": {"rule": champ[0], "train": champ[1], "test": champ[2]} if champ else None,
            "passed": passed, "held": held, "tried": len(results),
            "train_span": f"{fmt(t0)} – {fmt(split_ts)}", "test_span": f"{fmt(split_ts)} – {fmt(t1)}",
            "train_months": train_months, "test_months": test_months}


def fifty_lab(rows, log):
    rows = sorted(rows, key=lambda s: s["ts"])
    if len(rows) < 60:
        return {"n": len(rows)}
    t0, t1 = rows[0]["ts"], rows[-1]["ts"]
    split_ts = t0 + (t1 - t0) * 2 / 3
    exits = list(FIFTY_EXITS)

    def sim(cands, xi, pairs=None):
        tr, te, pp = [], [], {}
        for s in cands:
            if pairs is not None and s["pair"] not in pairs:
                continue
            r = s["x"][xi][0]
            if s["ts"] < split_ts:
                tr.append(r); pp.setdefault(s["pair"], []).append(r)
            else:
                te.append(r)
        return tr, te, pp
    hours = {"7:00–17:00 GMT (London + New York)": (7, 17), "12:00–16:00 GMT only (the overlap)": (12, 16)}
    spreads = {"any spread": 99, "spread under 2 pips": 2}
    trends = {"any trend": 0, "strong trend (ADX 25+)": 25}
    res = []
    for hn, (h0, h1_) in hours.items():
        for sn, smax in spreads.items():
            for tn, tmin in trends.items():
                cands = [s for s in rows if h0 <= s["hour"] < h1_ and s["spread_pips"] <= smax and s["adx"] >= tmin]
                for xi, xn in enumerate(exits):
                    tr, te, pp = sim(cands, xi)
                    rule = {"hours": hn, "spread": sn, "trend": tn, "exit": xn, "pairs": "all pairs"}
                    res.append((rule, stats(tr), stats(te)))
                    win = sorted(p for p, rs in pp.items() if len(rs) >= 8 and sum(rs) / len(rs) > 0)
                    if win and len(win) < len(pp):
                        tr2, te2, _ = sim(cands, xi, set(win))
                        res.append((dict(rule, pairs="only " + ", ".join(vf.NAMES.get(p, p) for p in win)), stats(tr2), stats(te2)))
    as_written = next(r for r in res if r[0] == {"hours": list(hours)[0], "spread": "any spread", "trend": "any trend",
                                                  "exit": exits[0], "pairs": "all pairs"})
    ok = sorted([r for r in res if r[1]["n"] >= 30 and r[2]["n"] >= 20], key=lambda r: -(r[1]["avg"] or -9))
    best = ok[0] if ok else None
    passed = bool(best and (best[2]["avg"] or -9) >= 0.05 and best[2]["total"] > 0)
    fmt = lambda x: dt.datetime.fromtimestamp(x, UTC).strftime("%b %d")
    by_pair = {}
    for s in rows:
        by_pair.setdefault(s["pair"], []).append(s["x"][0][0])
    log.append(f"50 pips: {len(rows)} signals, tried {len(res)} versions")
    return {"n": len(rows), "per_month": len(rows) / max(1e-9, (t1 - t0) / (30.4 * 86400)),
            "written": {"train": as_written[1], "test": as_written[2], "all": stats([s["x"][0][0] for s in rows])},
            "best": {"rule": best[0], "train": best[1], "test": best[2]} if best else None, "passed": passed,
            "held": sum(1 for r in ok[:10] if (r[2]["avg"] or -9) > 0.05), "top": [{"rule": r[0], "train": r[1], "test": r[2]} for r in ok[:6]],
            "pairs": {k: stats(v) for k, v in by_pair.items()},
            "train_span": f"{fmt(t0)} – {fmt(split_ts)}", "test_span": f"{fmt(split_ts)} – {fmt(t1)}"}


def fifty_verdict(F):
    if not F or F.get("n", 0) < 60:
        return f"Too few signals ({(F or {}).get('n', 0)}) to judge the 50 Pips strategy."
    w = F["written"]["all"]
    head = (f"As written, the 50 Pips strategy {'made' if w['avg'] > 0 else 'lost'} {w['avg']:+.2f}R per trade "
            f"over {w['n']} trades ({pc(w['win'])} wins; it needs 34% at 1:2 just to break even).")
    if F["passed"]:
        b = F["best"]
        return head + f" Its best version held up on unseen months: {b['test']['avg']:+.2f}R over {b['test']['n']} trades."
    return head + " None of its versions held up on the unseen months."


def rule_text(r):
    parts = [r["setups"], r["hours"], r["council"], r["trend"], r["exit"], r["pairs"]]
    return " · ".join(p for p in parts if p not in ("any hour", "any trend", "all pairs"))


def lab_verdict(L):
    if not L or not L["champ"]:
        return "The lab didn't have enough setups to test."
    c = L["champ"]
    if L["passed"]:
        return (f"The lab found a version that held up on months it never saw: {c['test']['avg']:+.2f}R per trade "
                f"over {c['test']['n']} test trades. Worth trying on practice.")
    return (f"No version held up. The best rule on the training months ({c['train']['avg']:+.2f}R) made "
            f"{c['test']['avg']:+.2f}R on the unseen months. That points to no real edge in this strategy family.")


# ───────────────────────────── report ────────────────────────────────────────

def fr(x, digits=2):
    return "—" if x is None else f"{x:+.{digits}f}R"


def pc(x):
    return "—" if x is None else f"{x:.0%}"


def render(g, log, started):
    t, a, rj = g["trades"], g["all"], g["rejected"]
    verdict_cls = "good" if (t["avg"] or 0) > 0.1 and t["n"] >= 30 else "bad" if (t["avg"] or 0) < -0.05 else "wait"
    badge = {"helps": ("good", "Helps"), "hurts": ("bad", "Hurts"), "no real effect": ("wait", "No effect"),
             "not enough data": ("wait", "Not enough data")}
    off_rows = ""
    for n, o in g["officers"].items():
        cls, lab = badge[o["verdict"]]
        off_rows += f"""<tr><td class="nm" style="--c:{FEED.get(n, '#fff')}">{n}<small>{html.escape(vf.ROLES.get(n, ''))}</small></td>
          <td><span class="pill {cls}">{lab}</span></td><td class="n">{fr(o['yes']['avg'])}<small>{o['yes']['n']} setups</small></td>
          <td class="n">{fr(o['no']['avg'])}<small>{o['no']['n']} setups</small></td><td class="n"><b>{fr(o['diff'])}</b></td>
          <td class="n">{pc(o['acc'])}</td></tr>"""

    def grp(name, rows):
        body = "".join(f"<tr><td>{html.escape(vf.NAMES.get(k, k))}</td><td class='n'>{s['n']}</td><td class='n'>{pc(s['win'])}</td>"
                       f"<td class='n {'pos' if (s['avg'] or 0) > 0 else 'neg'}'>{fr(s['avg'])}</td></tr>"
                       for k, s in sorted(rows.items(), key=lambda kv: -(kv[1]['avg'] or -9)))
        return f"<h3>{name}</h3><div class='tbl'><table><tr><th></th><th>Setups</th><th>Win</th><th>Avg</th></tr>{body}</table></div>"

    mon = g["monthly"]
    mx = max([abs(s["total"]) for s in mon.values()] + [1])
    bars = "".join(f"<div class='mb'><span>{k[2:]}</span><i class='{'pos' if s['total'] >= 0 else 'neg'}' "
                   f"style='--w:{abs(s['total']) / mx * 100:.0f}%'></i><b>{s['total']:+.1f}R</b></div>" for k, s in mon.items())
    tips = "".join(f"<li>{html.escape(x)}</li>" for x in g["advice"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inspector</title><style>
:root{{--bg:#0f1220;--panel:#171b2e;--fg:#e8ecf8;--muted:#9aa3c0;--line:#2a3150;--good:#53f0a6;--bad:#ff6b7c;--wait:#f4c84a;--acc:#38e1ff}}
@media (prefers-color-scheme: light){{:root:not([data-theme="dark"]){{--bg:#f4f5fa;--panel:#fff;--fg:#151a2c;--muted:#5b6380;--line:#dde1ee;--good:#138a55;--bad:#c23345;--wait:#9a7100;--acc:#0b7fa0}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;padding:20px 16px 40px}}
.wrap{{max-width:900px;margin:0 auto;display:grid;gap:18px}}
header{{display:flex;gap:14px;align-items:center}} .badge{{width:52px;height:52px;border-radius:14px;background:linear-gradient(135deg,#1f3a5f,#38e1ff);display:grid;place-items:center;font-size:28px;flex:none}}
h1{{margin:0;font-size:24px}} header p{{margin:2px 0 0;color:var(--muted);font-size:13px}}
section{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px}}
h2{{margin:0 0 10px;font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}} h3{{margin:14px 0 6px;font-size:14px}}
.verdict{{font-size:18px;font-weight:700;margin:0}} .verdict.good{{color:var(--good)}} .verdict.bad{{color:var(--bad)}} .verdict.wait{{color:var(--wait)}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-top:12px}}
.kpi{{border:1px solid var(--line);border-radius:10px;padding:10px 12px}} .kpi span{{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}}
.kpi b{{font-size:22px;font-variant-numeric:tabular-nums}} .kpi small{{display:block;color:var(--muted);font-size:12px}}
.tbl{{overflow-x:auto}} table{{width:100%;border-collapse:collapse;font-size:14px}} th,td{{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}} td.n,th:not(:first-child){{text-align:right;font-variant-numeric:tabular-nums}}
td small{{display:block;color:var(--muted);font-size:11.5px}} td.nm{{font-weight:700;border-left:3px solid var(--c);padding-left:8px}}
.pill{{font-size:11.5px;font-weight:700;padding:3px 8px;border-radius:99px;border:1px solid currentColor;white-space:nowrap}}
.pill.good{{color:var(--good)}} .pill.bad{{color:var(--bad)}} .pill.wait{{color:var(--wait)}}
.pos{{color:var(--good)}} .neg{{color:var(--bad)}}
ul.tips{{margin:0;padding-left:20px;display:grid;gap:8px}}
.mb{{display:grid;grid-template-columns:44px 1fr 64px;gap:8px;align-items:center;font-size:13px;margin:4px 0}}
.mb i{{display:block;height:12px;border-radius:4px;width:var(--w);min-width:2px}} .mb i.pos{{background:var(--good)}} .mb i.neg{{background:var(--bad)}}
.mb b{{text-align:right;font-variant-numeric:tabular-nums}} .mb span{{color:var(--muted)}}
.fine{{color:var(--muted);font-size:12.5px}}
table.lab td{{font-size:13px}} td.rk{{color:var(--muted);width:28px}} tr.base td{{background:color-mix(in srgb,var(--acc) 8%,transparent)}}
.champ{{border:1px solid var(--line);border-left:4px solid var(--wait);border-radius:10px;padding:10px 12px;margin:10px 0}}
.champ.good{{border-left-color:var(--good)}} .champ ul{{margin:6px 0 0;padding-left:18px;font-size:14px}} details summary{{cursor:pointer;color:var(--muted)}}
@media (max-width:560px){{th,td{{padding:7px 4px;font-size:13px}}}}
</style></head><body><div class="wrap">
<header><div class="badge" aria-hidden="true">🕵️</div><div><h1>Inspector's report</h1>
<p>Replayed the last {g['months']:g} months of OANDA history through Vault FX's exact rules · {dt.datetime.fromtimestamp(started, vf.LOCAL_TZ):%a %b %d, %I:%M %p} ET</p></div></header>

<section><h2>Verdict</h2><p class="verdict {verdict_cls}">{html.escape(g['advice'][0])}</p>
<div class="kpis">
 <div class="kpi"><span>Trades taken</span><b>{t['n']}</b><small>about {g['per_month']:.0f} a month</small></div>
 <div class="kpi"><span>Win rate</span><b>{pc(t['win'])}</b><small>needs ~40% at 1.5R targets</small></div>
 <div class="kpi"><span>Avg per trade</span><b class="{'pos' if (t['avg'] or 0) > 0 else 'neg'}">{fr(t['avg'])}</b><small>total {t['total']:+.1f}R</small></div>
 <div class="kpi"><span>On a ${vf.START_BALANCE:,.0f} account</span><b class="{'pos' if g['dollars'] >= 0 else 'neg'}">{'+' if g['dollars'] >= 0 else '−'}${abs(g['dollars']):,.0f}</b><small>at {vf.RISK_PCT:.0%} risk, no compounding</small></div>
 <div class="kpi"><span>Worst drawdown</span><b>{g['max_dd']:.1f}R</b><small>≈ ${g['max_dd'] * vf.START_BALANCE * vf.RISK_PCT:,.0f} from a high</small></div>
</div></section>

<section><h2>What to do</h2><ul class="tips">{tips}</ul></section>

{lab_html(g.get('lab'))}

{fifty_html(g.get('fifty'))}

<section><h2>Does the council help?</h2>
<div class="kpis">
 <div class="kpi"><span>Setups it took</span><b class="{'pos' if (t['avg'] or 0) > 0 else 'neg'}">{fr(t['avg'])}</b><small>{t['n']} trades</small></div>
 <div class="kpi"><span>Setups it rejected</span><b>{fr(rj['avg'])}</b><small>{rj['n']} scored on paper</small></div>
 <div class="kpi"><span>Every setup, no council</span><b>{fr(a['avg'])}</b><small>{a['n']} setups</small></div>
</div></section>

<section><h2>Officer report cards</h2>
<p class="fine">For each officer: how setups turned out when they voted YES vs NO. A useful officer's YES setups clearly beat their NO setups.</p>
<div class="tbl"><table><tr><th>Officer</th><th>Grade</th><th>When YES</th><th>When NO</th><th>Difference</th><th>"Right"</th></tr>{off_rows}</table></div>
<p class="fine">"Right" is the same accuracy the dashboard shows. It's a weak measure: an officer who says NO to everything looks right whenever most setups lose. Difference is the number that matters.</p></section>

<section><h2>Month by month (trades taken)</h2>{bars or '<p class="fine">No trades.</p>'}</section>

<section><h2>Where it works and where it doesn't</h2>
{grp('By setup (every setup, council or not)', g['groups']['setup_all'])}
{grp('By pair (every setup, council or not)', g['groups']['pair_all'])}</section>

<section><h2>How this test works</h2><p class="fine">Inspector downloaded {g['months']:g} months of 15-minute, 1-hour and 4-hour candles with real bid and ask prices,
found every pullback, crossover, 15-minute and London-breakout setup the bot would have seen, and ran the bot's own council code on each one.
Each setup was played forward on 15-minute candles: buys exit on the bid, sells on the ask, so the spread is paid. If the stop and target were both
touched in the same candle it counts as a loss. Approved trades move their stop to entry at +{vf.BREAKEVEN_AT_R:g}R, like the live bot; one trade per pair at a time.
Not simulated: the news blackout, the daily loss limit, the losing-streak breaker, the 3-open-trades and same-dollar limits, and learned vote weights
(all officers count equally here). Results are in R: +1.5R is a full win, −1R a full loss.</p>
<details><summary>Run log</summary><ul class="fine">{''.join(f'<li>{html.escape(x)}</li>' for x in log)}</ul></details></section>
</div></body></html>"""


def fifty_html(F):
    if not F:
        return ""
    def cell(s):
        return ("<td class='n'>—</td>" if not s["n"] else
                f"<td class='n {'pos' if s['avg'] > 0 else 'neg'}'>{s['avg']:+.2f}R<small>{s['n']} trades · {pc(s['win'])} win</small></td>")
    body = ""
    if F.get("written"):
        w = F["written"]
        body += f"<tr class='base'><td>As written: 7–17 GMT, stop 25 / target 50, every pair</td>{cell(w['train'])}{cell(w['test'])}</tr>"
    for r in F.get("top", []):
        rr = r["rule"]
        txt = " · ".join(x for x in (rr["hours"], rr["spread"], rr["trend"], rr["exit"], rr["pairs"]) if x not in ("any spread", "any trend", "all pairs"))
        body += f"<tr><td>{html.escape(txt)}</td>{cell(r['train'])}{cell(r['test'])}</tr>"
    pairs = "".join(f"<tr><td>{vf.NAMES.get(k, k)}</td><td class='n'>{s['n']}</td><td class='n'>{pc(s['win'])}</td>"
                    f"<td class='n {'pos' if s['avg'] > 0 else 'neg'}'>{fr(s['avg'])}</td></tr>"
                    for k, s in sorted(F.get("pairs", {}).items(), key=lambda kv: -kv[1]['avg']))
    return f"""<section><h2>📄 The "50 Pips a Day" strategy</h2>
<p class="verdict {'good' if F.get('passed') else 'bad'}">{html.escape(fifty_verdict(F))}</p>
<p class="fine">Rules tested: the 4-hour and 1-hour trends agree; a 15-minute candle pulls back to the lowest low (uptrend) or highest high (downtrend)
of the previous 20 candles and forms a pin bar or engulfing candle; RSI isn't stretched; 7:00–17:00 GMT; the first signal per pair per day.
Take profit 50 pips, stop 25–50 pips (gold: $5 target). About {F.get('per_month', 0):.0f} signals a month across all pairs.
Training {F.get('train_span', '')}, unseen test {F.get('test_span', '')}.</p>
<div class="tbl"><table class="lab"><tr><th>Version</th><th>Training</th><th>Test (unseen)</th></tr>{body}</table></div>
<h3>By pair (as written)</h3><div class="tbl"><table><tr><th></th><th>Trades</th><th>Win</th><th>Avg</th></tr>{pairs}</table></div></section>"""


def lab_html(L):
    if not L:
        return ""
    def cell(s):
        if not s["n"]:
            return "<td class='n'>—</td>"
        return f"<td class='n {'pos' if s['avg'] > 0 else 'neg'}'>{s['avg']:+.2f}R<small>{s['n']} trades · {pc(s['win'])} win</small></td>"
    rows = ""
    for k, r in enumerate(L["top"]):
        held = (r["test"]["avg"] or -9) > 0.05
        rows += (f"<tr><td class='rk'>{k + 1}</td><td>{html.escape(rule_text(r['rule']))}</td>{cell(r['train'])}{cell(r['test'])}"
                 f"<td>{'<span class=\'pill good\'>held up</span>' if held else '<span class=\'pill bad\'>fell apart</span>'}</td></tr>")
    base = L["base"]
    base_row = (f"<tr class='base'><td class='rk'>now</td><td>Your bot today</td>{cell(base['train'])}{cell(base['test'])}<td></td></tr>"
                if base else "")
    c = L["champ"]
    champ = ""
    if c:
        r = c["rule"]
        champ = f"""<div class="champ {'good' if L['passed'] else 'bad'}"><b>{'🏆 Best rule, and it held up' if L['passed'] else 'Best rule on the training months'}</b>
          <ul><li>Setups: {html.escape(r['setups'])}</li><li>Hours: {html.escape(r['hours'])}</li><li>Votes: {html.escape(r['council'])}</li>
          <li>Trend: {html.escape(r['trend'])}</li><li>Exit: {html.escape(r['exit'])}</li><li>Pairs: {html.escape(r['pairs'])}</li></ul></div>"""
    return f"""<section><h2>🧪 The lab</h2><p class="verdict {'good' if L['passed'] else 'bad'}">{html.escape(lab_verdict(L))}</p>
<p class="fine">Inspector tried {L['tried']:,} versions of the strategy: different setups, hours, voting, trend filters, exits and pairs.
He picked the best on the <b>training months</b> ({L['train_span']}), then checked them on the <b>test months</b> ({L['test_span']}), which he never used
to choose. A rule that only works on the months it was picked from is luck. A rule that keeps working on the unseen months might be real.
{L['held']} of the top {len(L['top'])} held up.</p>
{champ}
<div class="tbl"><table class="lab"><tr><th>#</th><th>Rule</th><th>Training</th><th>Test (unseen)</th><th></th></tr>{base_row}{rows}</table></div></section>"""


FEED = {"The Quant": "#3ee6a1", "Risk": "#ff5468", "Momentum": "#ffb03b", "Time": "#4cb7ff", "Doubt": "#c9a7ff"}


def telegram_summary(g):
    t = g["trades"]
    lines = [f"🕵️ Inspector's report: {g['months']:g} months replayed", "",
             f"Trades: {t['n']} · win rate {pc(t['win'])} · {fr(t['avg'])} per trade",
             f"On ${vf.START_BALANCE:,.0f}: {'+' if g['dollars'] >= 0 else '−'}${abs(g['dollars']):,.0f} · worst drawdown {g['max_dd']:.1f}R",
             f"Council took {fr(t['avg'])} vs rejected {fr(g['rejected']['avg'])}", "", "Officers:"]
    icon = {"helps": "✅", "hurts": "❌", "no real effect": "➖", "not enough data": "❔"}
    for n, o in g["officers"].items():
        lines.append(f"{icon[o['verdict']]} {n}: {o['verdict']} ({fr(o['diff'])})")
    L = g.get("lab")
    if L and L["champ"]:
        c = L["champ"]
        lines += ["", "🧪 Lab: " + lab_verdict(L),
                  f"Best rule: {rule_text(c['rule'])}",
                  f"Training {c['train']['avg']:+.2f}R ({c['train']['n']} trades) → unseen months {c['test']['avg']:+.2f}R ({c['test']['n']} trades)",
                  f"{L['held']} of the top {len(L['top'])} rules held up on the unseen months."]
    F = g.get("fifty")
    if F:
        lines += ["", "📄 50 Pips a Day: " + fifty_verdict(F)]
        if F.get("best"):
            b = F["best"]
            lines.append(f"Its best version: {b['rule']['hours']}, {b['rule']['exit']}, {b['rule']['pairs']} → "
                         f"training {b['train']['avg']:+.2f}R, unseen {b['test']['avg']:+.2f}R ({b['test']['n']} trades)")
    lines += ["", "What to do:"] + [f"• {x}" for x in g["advice"][:3]]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        o_, r_ = repo.split("/", 1)
        lines += ["", f"Full report (after the next dashboard update): https://{o_}.github.io/{r_}/inspector.html"]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Backtest Vault FX's rules on OANDA history.")
    ap.add_argument("--months", type=float, default=6)
    ap.add_argument("--pairs", default=",".join(vf.PAIRS))
    args = ap.parse_args()
    token, acct = os.environ.get("OANDA_API_TOKEN", "").strip(), os.environ.get("OANDA_ACCOUNT_ID", "").strip()
    if not token or not acct:
        sys.exit("Add OANDA_API_TOKEN and OANDA_ACCOUNT_ID.")
    started = time.time()
    now = dt.datetime.now(UTC)
    test_from = now - dt.timedelta(days=args.months * 30.4)
    log = []
    try:
        info = vf.Oanda(token, acct).instruments()
    except Exception as e:
        info = {}
        log.append(f"instrument info unavailable ({e})")
    setups, fifty = [], []
    for pair in [p.strip() for p in args.pairs.split(",") if p.strip()]:
        try:
            m15 = fetch(token, pair, "M15", test_from - dt.timedelta(days=5))
            h1 = fetch(token, pair, "H1", test_from - dt.timedelta(days=20))
            h4 = fetch(token, pair, "H4", test_from - dt.timedelta(days=75))
        except Exception as e:
            log.append(f"{pair}: skipped ({e})")
            continue
        log.append(f"{pair}: {len(m15)} 15-min ({m15[0]['t'][:10] if m15 else '?'} → {m15[-1]['t'][:10] if m15 else '?'}), {len(h1)} 1-hour, {len(h4)} 4-hour candles")
        setups += replay(pair, h1, h4, m15, test_from.timestamp(), int(info.get(pair, {}).get("pipLocation", -4)), log, fifty)
        print(log[-1], flush=True)
    g = grade(setups, args.months)
    g["lab"] = lab(setups, log)
    g["fifty"] = fifty_lab(fifty, log)
    log.append(f"Finished in {(time.time() - started) / 60:.1f} min")
    with open("inspector.html", "w", encoding="utf-8") as f:
        f.write(render(g, log, started))
    with open("inspector.json", "w", encoding="utf-8") as f:
        json.dump({"when": started, "grade": g}, f, indent=1)
    msg = telegram_summary(g)
    print(msg)
    tg = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat:
        try:
            chat = json.load(open(vf.STATE_FILE)).get("chat_id")
        except Exception:
            chat = None
    if tg and chat:
        try:
            vf.http("POST", f"https://api.telegram.org/bot{tg}/sendMessage", body={"chat_id": chat, "text": msg,
                                                                                 "disable_web_page_preview": True}, tries=2)
        except Exception as e:
            print(f"Telegram failed: {e}")


if __name__ == "__main__":
    main()
