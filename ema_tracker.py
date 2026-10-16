"""200 EMA Tracker — Easter's 200 EMA + supply/demand strategy, paper-traded on live OANDA prices.

Its own side project: a pretend $1,000 account, no real orders, no Telegram. Every 15-min candle it checks
28 currency pairs. Five "members" each check one rule and vote; a trade needs all five YES.
  The Trend   – the 200 EMA is sloping (moved at least 1 ATR over the last 50 candles), price on the trend side
  The Tap     – price taps the 200 EMA exactly (no tap, no trade)
  The Zone    – the 200 EMA sits in a supply (shorts) or demand (longs) zone
  The Candles – at least TWO very convincing candles in the trend direction after the tap; enter at the 2nd close
  The Target  – there's a previous swing high/low to aim for (take profit there)
Stop goes beyond the 1st confirmation candle. Risk 1% of the pretend account per trade.
Writes site/ema.html and ema_state.json.
"""
import argparse, datetime as dt, html, json, os, time
from zoneinfo import ZoneInfo

import vault_fx as vf

PAIRS = ["EUR_USD", "GBP_USD", "AUD_USD", "NZD_USD", "USD_JPY", "USD_CHF", "USD_CAD",
         "EUR_JPY", "GBP_JPY", "AUD_JPY", "NZD_JPY", "CAD_JPY", "CHF_JPY",
         "EUR_GBP", "EUR_AUD", "EUR_NZD", "EUR_CAD", "EUR_CHF",
         "GBP_AUD", "GBP_NZD", "GBP_CAD", "GBP_CHF",
         "AUD_NZD", "AUD_CAD", "AUD_CHF", "NZD_CAD", "NZD_CHF", "CAD_CHF"]
START = 1000.0
RISK_PCT = 0.01
SLOPE_MIN = 1.0          # ATRs the 200 EMA must move over 50 candles
SLOPE_N = 50
MAX_HOLD_H = 120         # close a trade after 5 days
CANDLES = 1200
STATE = "ema_state.json"
TZ = ZoneInfo("America/New_York")
FEED_MAX = 250
MEMBERS = ["The Trend", "The Tap", "The Zone", "The Candles", "The Target"]


# ───────────────────────── math ─────────────────────────
def ema(xs, n):
    k, out, e = 2 / (n + 1), [], None
    for x in xs:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def atr_list(b, n=14):
    out, a = [], None
    for i, x in enumerate(b):
        tr = x["h"] - x["l"] if i == 0 else max(x["h"] - x["l"], abs(x["h"] - b[i - 1]["c"]), abs(x["l"] - b[i - 1]["c"]))
        a = tr if a is None else (a * (n - 1) + tr) / n
        out.append(a)
    return out


def find_zones(bars, atr, imp=1.5, cls=1.0, base_max=1.0):
    """Base of 1-3 small candles, then a big move away. Confirmed when the move's last candle closes."""
    zones = []
    for i in range(20, len(bars) - 1):
        a = atr[i - 1]
        if not a:
            continue
        for blen in (1, 2, 3):
            base = bars[i - blen:i]
            if len(base) < blen or any(b["h"] - b["l"] > base_max * a for b in base):
                continue
            hi, lo = max(b["h"] for b in base), min(b["l"] for b in base)
            hit = None
            for k in (1, 2):
                mv = bars[i:i + k]
                if len(mv) < k:
                    continue
                if max(b["h"] for b in mv) - hi > imp * a and mv[-1]["c"] > hi + cls * a:
                    hit = {"kind": "demand", "hi": hi, "lo": lo, "known": i + k}; break
                if lo - min(b["l"] for b in mv) > imp * a and mv[-1]["c"] < lo - cls * a:
                    hit = {"kind": "supply", "hi": hi, "lo": lo, "known": i + k}; break
            if hit:
                zones.append(hit); break
    return zones


def live_zones(bars, zones, i):
    """Zones known by candle i that price hasn't closed through since."""
    out = []
    for z in zones:
        if z["known"] > i:
            continue
        dead = False
        for j in range(z["known"], i + 1):
            c = bars[j]["c"]
            if (z["kind"] == "demand" and c < z["lo"]) or (z["kind"] == "supply" and c > z["hi"]):
                dead = True; break
        if not dead:
            out.append(z)
    return out


def pivots(bars, k=3):
    hi, lo = [], []
    for i in range(k, len(bars) - k):
        if all(bars[i]["h"] >= bars[j]["h"] for j in range(i - k, i + k + 1)):
            hi.append((i + k, bars[i]["h"]))
        if all(bars[i]["l"] <= bars[j]["l"] for j in range(i - k, i + k + 1)):
            lo.append((i + k, bars[i]["l"]))
    return hi, lo


def strong(b, d, a):
    body = (b["c"] - b["o"]) * d
    rng = b["h"] - b["l"]
    return body > 0 and rng > 0 and body >= 0.6 * rng and body >= 0.25 * a


def pfmt(pair, x):
    return f"{x:.3f}" if "JPY" in pair else f"{x:.5f}"


# ───────────────────────── strategy ─────────────────────────
def check_taps(pair, bars, after_t):
    """Every EMA tap (on a sloped EMA) whose confirmation window has closed and is newer than after_t.
    Returns a list of setup cards with each member's vote."""
    n = len(bars)
    atr = atr_list(bars)
    e200 = ema([b["c"] for b in bars], 200)
    zones = find_zones(bars, atr)
    sh, sl = pivots(bars)
    cards = []
    for i in range(max(260, n - 200), n - 2):          # need i+1 and i+2 closed to judge the candles
        if bars[i]["t"] <= after_t:
            continue
        a, e = atr[i], e200[i]
        if not a:
            continue
        slope = (e - e200[i - SLOPE_N]) / a
        d = 1 if slope > 0 else -1
        b = bars[i]
        prev_side = all((bars[j]["c"] - e200[j]) * d > 0 for j in range(i - 3, i))
        tap = (b["l"] <= e) if d == 1 else (b["h"] >= e)
        if not tap or not prev_side:
            continue                                     # not a pullback to the EMA: nobody needs to vote
        if abs(slope) < SLOPE_MIN:
            cards.append({"flat": True, "t": bars[i]["t"]})   # counted, but not shown: flat EMA = sideways market
            continue
        side = "BUY" if d == 1 else "SELL"
        v = {}
        v["The Trend"] = (abs(slope) >= SLOPE_MIN,
                          f"200 EMA is {'rising' if d == 1 else 'falling'} {abs(slope):.1f} ATR over 50 candles (needs {SLOPE_MIN:g})"
                          + ("" if abs(slope) >= SLOPE_MIN else ": too flat, market is drifting sideways"))
        v["The Tap"] = (True, f"candle {'low' if d == 1 else 'high'} touched the 200 EMA at {pfmt(pair, e)}")
        want = "demand" if d == 1 else "supply"
        lz = [z for z in live_zones(bars, zones, i) if z["kind"] == want and z["lo"] - 0.5 * a <= e <= z["hi"] + 0.5 * a]
        if lz:
            z = lz[-1]
            v["The Zone"] = (True, f"EMA is inside a {want} zone ({pfmt(pair, z['lo'])}–{pfmt(pair, z['hi'])})")
        else:
            v["The Zone"] = (False, f"no fresh {want} zone at the EMA: just a line, no confluence")
        c1 = None
        for s in (i, i + 1):
            if s + 1 < n and strong(bars[s], d, a) and strong(bars[s + 1], d, a) and (bars[s + 1]["c"] - e200[s + 1]) * d > 0:
                c1 = s; break
        if c1 is not None:
            v["The Candles"] = (True, f"two convincing {'bullish' if d == 1 else 'bearish'} candles closed ({bars[c1]['t'][11:16]} and {bars[c1 + 1]['t'][11:16]} UTC)")
        else:
            kinds = ["strong" if strong(bars[k], d, a) else "weak" for k in (i, i + 1, i + 2)]
            v["The Candles"] = (False, f"no two convincing candles in a row (got {', '.join(kinds)}): possible fake-out")
        entry = stop = tp = rr = None
        if c1 is not None:
            c2 = c1 + 1
            entry = bars[c2]["c"]
            stop = (bars[c1]["l"] if d == 1 else bars[c1]["h"]) - d * bars[c2]["sp"]
            risk = abs(entry - stop)
            cands = ([p for k, p in sh if k <= c2 and p > entry and k >= c2 - 300] if d == 1
                     else [p for k, p in sl if k <= c2 and p < entry and k >= c2 - 300])
            if cands and risk > 0:
                tp = cands[-1]; rr = abs(tp - entry) / risk
                v["The Target"] = (True, f"previous swing {'high' if d == 1 else 'low'} at {pfmt(pair, tp)} ({rr:.1f}R away)")
            else:
                v["The Target"] = (False, f"no previous swing {'high' if d == 1 else 'low'} to aim for")
        else:
            v["The Target"] = (None, "didn't get this far")
        ok = all(x[0] for x in v.values())
        cards.append({"t": bars[i]["t"], "pair": pair, "side": side, "ok": ok,
                      "votes": {k: [x[0], x[1]] for k, x in v.items()},
                      "entry": entry, "stop": stop, "tp": tp, "rr": round(rr, 2) if rr else None,
                      "entry_t": bars[c1 + 1]["t"] if c1 is not None else None})
    return cards


def manage(tr, bars):
    """Walk candles after the last one checked. Stop first if both hit in one candle."""
    d = 1 if tr["side"] == "BUY" else -1
    for b in bars:
        if b["t"] <= tr["checked"]:
            continue
        tr["checked"] = b["t"]
        tr["last"] = b["c"]
        hit = None
        if d == 1:
            if b["l"] <= tr["stop"]: hit = ("stop", tr["stop"])
            elif b["h"] >= tr["tp"]: hit = ("target", tr["tp"])
        else:
            if b["h"] >= tr["stop"]: hit = ("stop", tr["stop"])
            elif b["l"] <= tr["tp"]: hit = ("target", tr["tp"])
        age_h = (dt.datetime.fromisoformat(b["t"]) - dt.datetime.fromisoformat(tr["entry_t"])).total_seconds() / 3600
        if not hit and age_h >= MAX_HOLD_H:
            hit = ("5-day limit", b["c"])
        if hit:
            return hit
    return None


def r_of(tr, price):
    d = 1 if tr["side"] == "BUY" else -1
    return d * (price - tr["entry"]) / tr["risk_px"] - tr["cost_r"]


# ───────────────────────── run ─────────────────────────
def load():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {"balance": START, "open": [], "closed": [], "feed": [], "seen": {}, "equity": [], "started": time.time()}


def run(st, log):
    api = vf.Oanda(os.environ["OANDA_API_TOKEN"].strip(), os.environ["OANDA_ACCOUNT_ID"].strip())
    now = time.time()
    for pair in PAIRS:
        try:
            rows = api.get(f"/instruments/{pair}/candles", granularity="M15", count=CANDLES, price="MBA")["candles"]
        except Exception as e:
            log.append(f"{pair}: {e}"); continue
        bars = [{"t": c["time"][:19], "o": float(c["mid"]["o"]), "h": float(c["mid"]["h"]), "l": float(c["mid"]["l"]),
                 "c": float(c["mid"]["c"]), "sp": float(c["ask"]["c"]) - float(c["bid"]["c"])} for c in rows if c.get("complete")]
        if len(bars) < 400:
            continue
        # 1) look after open trades
        for tr in [t for t in st["open"] if t["pair"] == pair]:
            hit = manage(tr, bars)
            if hit:
                why, px = hit
                r = r_of(tr, px)
                pnl = round(r * tr["risk_usd"], 2)
                st["balance"] = round(st["balance"] + pnl, 2)
                tr.update({"exit": px, "exit_t": tr["checked"], "why": why, "r": round(r, 2), "pnl": pnl})
                st["open"].remove(tr); st["closed"].append(tr)
                st["equity"].append([tr["checked"], st["balance"]])
                log.append(f"closed {pair} {tr['side']} {why} {r:+.2f}R ${pnl:+.2f}")
        # 2) new setups
        after = st["seen"].get(pair, bars[-30]["t"])          # first run: only look at the last few hours
        cards = check_taps(pair, bars, after)
        if cards:
            st["seen"][pair] = cards[-1]["t"]
        else:
            st["seen"][pair] = max(after, bars[-3]["t"])
        for c in cards:
            if c.get("flat"):
                st["flat"] = st.get("flat", 0) + 1
                continue
            st["taps"] = st.get("taps", 0) + 1
            busy = any(t["pair"] == pair for t in st["open"])
            if c["ok"] and busy:
                c["ok"] = False
                c["note"] = "already in a trade on this pair"
            if c["ok"]:
                risk_px = abs(c["entry"] - c["stop"])
                sp = bars[-1]["sp"]
                tr = {"id": f"{pair}-{c['entry_t']}", "pair": pair, "side": c["side"], "entry": c["entry"], "stop": c["stop"],
                      "tp": c["tp"], "rr": c["rr"], "entry_t": c["entry_t"], "checked": c["entry_t"], "last": c["entry"],
                      "risk_px": risk_px, "cost_r": round(sp / risk_px, 3) if risk_px else 0,
                      "risk_usd": round(st["balance"] * RISK_PCT, 2), "votes": c["votes"]}
                st["open"].append(tr)
                hit = manage(tr, [b for b in bars if b["t"] > c["entry_t"]])   # catch up if it already played out
                if hit:
                    why, px = hit
                    r = r_of(tr, px); pnl = round(r * tr["risk_usd"], 2)
                    st["balance"] = round(st["balance"] + pnl, 2)
                    tr.update({"exit": px, "exit_t": tr["checked"], "why": why, "r": round(r, 2), "pnl": pnl})
                    st["open"].remove(tr); st["closed"].append(tr)
                    st["equity"].append([tr["checked"], st["balance"]])
                log.append(f"TRADE {pair} {c['side']} @ {c['entry']}")
            st["feed"].append(c)
    st["feed"] = st["feed"][-FEED_MAX:]
    st["last_run"] = now


# ───────────────────────── page ─────────────────────────
def et(t):
    try:
        x = dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc).astimezone(TZ)
        return x.strftime("%a %b %-d, %-I:%M %p")
    except Exception:
        return t


def money(x):
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def page(st):
    closed, opened = st["closed"], st["open"]
    bal = st["balance"]
    unreal = sum(r_of(t, t["last"]) * t["risk_usd"] for t in opened)
    wins = sum(1 for t in closed if t["pnl"] > 0)
    n = len(closed)
    tot_r = sum(t["r"] for t in closed)
    pct = (bal - START) / START * 100
    e = h = html.escape
    # equity sparkline
    pts = [[None, START]] + st["equity"]
    vals = [p[1] for p in pts]
    lo, hi = min(vals + [START * 0.98]), max(vals + [START * 1.02])
    W, H = 600, 120
    path = " ".join(f"{'M' if k == 0 else 'L'}{k * W / max(1, len(vals) - 1):.1f},{H - (v - lo) / (hi - lo) * H:.1f}" for k, v in enumerate(vals))
    base_y = H - (START - lo) / (hi - lo) * H
    col = "#45e39b" if bal >= START else "#ff6378"

    def vote_rows(votes):
        out = []
        for m in MEMBERS:
            ok, why = votes.get(m, [None, ""])
            tag = '<b class="y">YES</b>' if ok else ('<b class="n">NO</b>' if ok is False else '<b class="s">—</b>')
            out.append(f'<div class="vote">{tag}<span class="m">{m}</span><span class="w">{e(why)}</span></div>')
        return "".join(out)

    open_html = "".join(
        f'<div class="card"><div class="row"><b>{vf.NAMES.get(t["pair"], t["pair"].replace("_", "/"))} {t["side"]}</b>'
        f'<span class="{"pos" if r_of(t, t["last"]) >= 0 else "neg"}">{r_of(t, t["last"]):+.2f}R · {money(r_of(t, t["last"]) * t["risk_usd"])}</span></div>'
        f'<div class="sub">in {pfmt(t["pair"], t["entry"])} · stop {pfmt(t["pair"], t["stop"])} · target {pfmt(t["pair"], t["tp"])} ({t["rr"]}R) · now {pfmt(t["pair"], t["last"])} · since {et(t["entry_t"])}</div>'
        f'<details><summary>Why each member voted YES</summary>{vote_rows(t["votes"])}</details></div>'
        for t in opened) or '<p class="muted">No open trades. Waiting for a clean setup.</p>'
    closed_html = "".join(
        f'<tr><td>{et(t["exit_t"])}</td><td>{t["pair"].replace("_", "/")}</td><td>{t["side"]}</td><td>{e(t["why"])}</td>'
        f'<td class="{"pos" if t["pnl"] >= 0 else "neg"}">{t["r"]:+.2f}R</td><td class="{"pos" if t["pnl"] >= 0 else "neg"}">{money(t["pnl"])}</td></tr>'
        for t in reversed(closed[-60:])) or '<tr><td colspan="6" class="muted">No closed trades yet.</td></tr>'
    feed = sorted(st["feed"], key=lambda c: c["t"], reverse=True)[:40]

    def card(c):
        head = (f'<div class="row"><b>{c["pair"].replace("_", "/")} {c["side"]}</b>'
                f'<span class="{"pos" if c["ok"] else "muted"}">{"✅ TRADE" if c["ok"] else "⏭ SKIP"}</span></div>')
        if c["ok"]:
            return (f'<div class="card go">{head}<div class="sub">EMA tap at {et(c["t"])} · entry {pfmt(c["pair"], c["entry"])}, '
                    f'target {c["rr"]}R away</div>{vote_rows(c["votes"])}</div>')
        nos = [m for m in MEMBERS if c["votes"].get(m, [None])[0] is False]
        why = e(c["note"]) if c.get("note") else ("NO from " + ", ".join(nos) if nos else "")
        return (f'<div class="card">{head}<div class="sub">EMA tap at {et(c["t"])} · {why}</div>'
                f'<details><summary>See all 5 votes</summary>{vote_rows(c["votes"])}</details></div>')
    feed_html = "".join(card(c) for c in feed) or '<p class="muted">No EMA taps checked yet. The first ones show up after the next few candles.</p>'
    taps = st.get("taps", 0)
    last = dt.datetime.fromtimestamp(st.get("last_run", time.time()), TZ).strftime("%a %-I:%M %p ET")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>200 EMA Tracker</title><style>
:root{{--bg:#0d0f1a;--card:#161a2b;--fg:#eef0fa;--muted:#9aa2c2;--line:#262c45;--ok:#45e39b;--bad:#ff6378;--acc:#ffb547}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:16px}}h1{{font-size:22px;margin:4px 0}}h2{{font-size:16px;margin:22px 0 8px;color:var(--acc)}}
.muted{{color:var(--muted)}}.pos{{color:var(--ok);font-weight:700}}.neg{{color:var(--bad);font-weight:700}}
.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}.k,.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px}}
.k small{{display:block;color:var(--muted);font-size:12px}}.k b{{font-size:19px}}
.card{{margin-bottom:8px}}.card.go{{border-color:#2f7d5c}}.row{{display:flex;justify-content:space-between;gap:8px}}.sub{{color:var(--muted);font-size:13px;margin:2px 0 6px}}
.vote{{display:grid;grid-template-columns:42px 92px 1fr;gap:6px;font-size:13px;padding:3px 0;border-top:1px solid var(--line)}}
.vote .m{{font-weight:600}}.vote .w{{color:var(--muted)}}.y{{color:var(--ok)}}.n{{color:var(--bad)}}.s{{color:var(--muted)}}
table{{width:100%;border-collapse:collapse;font-size:13px}}td{{padding:6px 4px;border-top:1px solid var(--line)}}
details summary{{cursor:pointer;color:var(--muted);font-size:13px}}svg{{width:100%;height:auto;display:block}}
a{{color:var(--acc)}}.k li b{{font-size:inherit}}ul{{padding-left:18px;margin:6px 0}}li{{margin:3px 0}}
@media(max-width:420px){{.stats{{grid-template-columns:repeat(2,1fr)}}.vote{{grid-template-columns:38px 84px 1fr}}}}
</style></head><body><main>
<p class="muted"><a href="index.html">← Vault FX</a> · side test, pretend money, no real orders</p>
<h1>📈 200 EMA Tracker</h1>
<p class="muted">Easter's 200 EMA + supply/demand strategy on 28 currency pairs, 15-min chart. Updated {last}.</p>
<div class="stats">
<div class="k"><small>Account</small><b>{money(bal)}</b></div>
<div class="k"><small>Profit</small><b class="{"pos" if bal >= START else "neg"}">{money(bal - START)}</b></div>
<div class="k"><small>Return</small><b class="{"pos" if pct >= 0 else "neg"}">{pct:+.1f}%</b></div>
<div class="k"><small>Trades</small><b>{n}</b> <span class="muted">{wins}W–{n - wins}L</span></div>
<div class="k"><small>Win rate</small><b>{(wins / n * 100 if n else 0):.0f}%</b></div>
<div class="k"><small>Total R</small><b class="{"pos" if tot_r >= 0 else "neg"}">{tot_r:+.1f}R</b></div>
<div class="k"><small>Open</small><b>{len(opened)}</b> <span class="{"pos" if unreal >= 0 else "neg"}">{money(unreal)}</span></div>
<div class="k"><small>Setups checked</small><b>{taps}</b></div>
<div class="k"><small>Started with</small><b>{money(START)}</b></div>
</div>
<h2>Account over time</h2>
<div class="k"><svg viewBox="0 0 {W} {H}" preserveAspectRatio="none"><line x1="0" x2="{W}" y1="{base_y:.1f}" y2="{base_y:.1f}" stroke="#394065" stroke-dasharray="4 4"/>
<path d="{path}" fill="none" stroke="{col}" stroke-width="2.5"/></svg>
<p class="muted" style="margin:4px 0 0;font-size:12px">Dashed line = the starting $1,000. Moves only when a trade closes.</p></div>
<h2>Open trades</h2>{open_html}
<h2>Closed trades</h2><div class="k"><table>{closed_html}</table></div>
<h2>The members' votes (last 40 EMA taps)</h2>
<p class="muted">Every time price taps a sloping 200 EMA, five members each check one rule. All five YES = trade. ({st.get("flat", 0)} more taps were on a flat EMA, so The Trend said NO right away and they're not listed.)</p>
{feed_html}
<h2>The rules</h2><div class="k"><ul>
<li><b>The Trend:</b> 200 EMA sloping (moved at least {SLOPE_MIN:g} ATR over 50 candles), price on the trend side.</li>
<li><b>The Tap:</b> price must touch the 200 EMA exactly.</li>
<li><b>The Zone:</b> the EMA sits in a supply zone (sells) or demand zone (buys).</li>
<li><b>The Candles:</b> two very convincing candles after the tap; enter at the close of the 2nd.</li>
<li><b>The Target:</b> take profit at the previous swing high/low. Stop goes beyond the 1st confirmation candle.</li>
<li>Risk 1% of the account per trade · one trade per pair at a time · closes after 5 days if neither hits.</li>
</ul><p class="muted" style="font-size:13px">Backtest (12 months, all 28 pairs): about 44 trades a year, ~+7R. Few trades means results swing a lot, so give it a few months.</p></div>
</main></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/ema.html")
    args = ap.parse_args()
    st = load()
    log = []
    bucket = int(time.time() // 900)
    if st.get("bucket") != bucket:            # only once per new 15-min candle
        try:
            run(st, log)
            st["bucket"] = bucket
        except Exception as e:
            log.append(f"run failed: {e}")
        json.dump(st, open(STATE, "w"), indent=0)
    else:
        log.append("no new 15-min candle since last check")
    for line in log:
        print(line)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    open(args.out, "w", encoding="utf-8").write(page(st))


if __name__ == "__main__":
    main()
