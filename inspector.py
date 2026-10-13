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
    out, frm, first = [], start, True
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
        if len(rows) < 5000 or last >= dt.datetime.now(UTC) - dt.timedelta(minutes=30):
            break
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


# ───────────────────────────── replay one pair ───────────────────────────────

def replay(pair, h1, h4, m15, test_from, pip_loc, log):
    setups = []
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
.fine{{color:var(--muted);font-size:12.5px}} details summary{{cursor:pointer;color:var(--muted)}}
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
    lines += ["", "What to do:"] + [f"• {x}" for x in g["advice"][:4]]
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
    setups = []
    for pair in [p.strip() for p in args.pairs.split(",") if p.strip()]:
        try:
            m15 = fetch(token, pair, "M15", test_from - dt.timedelta(days=5))
            h1 = fetch(token, pair, "H1", test_from - dt.timedelta(days=20))
            h4 = fetch(token, pair, "H4", test_from - dt.timedelta(days=75))
        except Exception as e:
            log.append(f"{pair}: skipped ({e})")
            continue
        log.append(f"{pair}: {len(m15)} 15-min, {len(h1)} 1-hour, {len(h4)} 4-hour candles")
        setups += replay(pair, h1, h4, m15, test_from.timestamp(), int(info.get(pair, {}).get("pipLocation", -4)), log)
        print(log[-1], flush=True)
    g = grade(setups, args.months)
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
