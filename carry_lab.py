"""
Carry Lab: Inspector tests the two forex ideas with the most academic evidence behind them.

  1. CARRY: hold the high-interest currencies against the low-interest ones (you collect the
     interest difference every day you hold, as long as prices don't move against you)
  2. CURRENCY STRENGTH (momentum): hold the currencies that have been rising against the ones
     that have been falling, rebalanced every week or month
     2b. per-pair trend: each pair long if it rose over the lookback, short if it fell
  3. CARRY + STRENGTH together

Uses real OANDA daily prices with bid/ask spreads, and YOUR account's actual financing rates
(what OANDA really pays/charges you to hold each pair overnight).

Settings are picked on the earlier years, then judged on the most recent --months (default 12).
Run:  python carry_lab.py --months 12
"""

import argparse
import datetime as dt
import html
import json
import math
import os
import statistics
import sys
import time

import vault_fx as vf
from inspector import fetch

UTC = dt.timezone.utc
PAIRS = ["EUR_USD", "GBP_USD", "AUD_USD", "NZD_USD", "USD_JPY", "USD_CAD", "USD_CHF"]
CCY = {"EUR_USD": "EUR", "GBP_USD": "GBP", "AUD_USD": "AUD", "NZD_USD": "NZD", "USD_JPY": "JPY", "USD_CAD": "CAD", "USD_CHF": "CHF"}
TRAIN_YEARS = 3
LOOKBACKS = [21, 63, 126, 252]          # ~1, 3, 6, 12 months of trading days
KS = [1, 2, 3]                          # how many currencies long and short
REBAL = {"weekly": 5, "monthly": 21}


def load(token, acct, years, log):
    start = dt.datetime.now(UTC) - dt.timedelta(days=int(365 * years) + 400)
    px, spr = {}, {}
    for p in PAIRS:
        rows = fetch(token, p, "D", start)
        px[p] = {r["t"][:10]: r["c"] for r in rows}
        spr[p] = statistics.median([(r["ac"] - r["bc"]) / r["c"] for r in rows[-250:]]) if rows else 0.0002
        log.append(f"{p}: {len(rows)} daily candles, typical spread {spr[p] * 1e4:.1f} bps")
        print(log[-1], flush=True)
    fin = {}
    try:
        inst = vf.Oanda(token, acct).instruments()
        for p in PAIRS:
            f = (inst.get(p) or {}).get("financing") or {}
            fin[p] = (float(f.get("longRate", 0)), float(f.get("shortRate", 0)))
    except Exception as e:
        log.append(f"financing rates unavailable ({e}); carry uses 0")
        fin = {p: (0.0, 0.0) for p in PAIRS}
    days = sorted(set.intersection(*[set(v) for v in px.values()]))
    return days, px, spr, fin


def ccy_returns(days, px):
    """Daily return of each currency against USD (USD itself = 0)."""
    out = []
    for i in range(1, len(days)):
        r = {}
        for p, c in CCY.items():
            a, b = px[p][days[i - 1]], px[p][days[i]]
            r[c] = (b / a - 1) if p.endswith("_USD") else (a / b - 1)
        r["USD"] = 0.0
        out.append(r)
    return out


def carry_rate(fin, ccy, side):
    """Annual financing you get (+) or pay (−) for being long/short this currency vs USD, from OANDA's real rates."""
    if ccy == "USD":
        return 0.0
    p = next(k for k, v in CCY.items() if v == ccy)
    long_r, short_r = fin[p]
    if p.endswith("_USD"):
        return long_r if side == 1 else short_r      # long EUR = long EUR_USD
    return short_r if side == 1 else long_r          # long JPY = short USD_JPY


def backtest(days, rets, px, spr, fin, kind, look, k, reb):
    """Daily equity (return on total notional, both sides) for one rule."""
    ccys = ["EUR", "GBP", "AUD", "NZD", "JPY", "CAD", "CHF", "USD"]
    pos, daily = {}, []
    start = max(LOOKBACKS) + 1
    for i in range(start, len(rets)):
        if (i - start) % reb == 0:
            if kind in ("mom", "combo", "tsmom"):
                past = {c: sum(rets[j][c] for j in range(i - look, i)) for c in ccys}
            if kind in ("carry", "combo"):
                cr = {c: (carry_rate(fin, c, 1) - carry_rate(fin, c, -1)) / 2 for c in ccys}
            if kind == "carry":
                score = cr
            elif kind == "mom":
                score = past
            elif kind == "combo":
                rk = lambda d: {c: n for n, c in enumerate(sorted(d, key=d.get))}
                a, b = rk(cr), rk(past)
                score = {c: a[c] + b[c] for c in ccys}
            if kind == "tsmom":
                new = {c: (1 if past[c] > 0 else -1) / 7 for c in ccys if c != "USD"}
            else:
                order = sorted(ccys, key=lambda c: score[c])
                new = {c: 0.0 for c in ccys}
                for c in order[-k:]:
                    new[c] += 0.5 / k
                for c in order[:k]:
                    new[c] -= 0.5 / k
            # trading cost on the change in each currency's position (USD legs are free)
            cost = 0.0
            for c in ccys:
                if c == "USD":
                    continue
                p = next(q for q, v in CCY.items() if v == c)
                cost += abs(new.get(c, 0) - pos.get(c, 0)) * spr[p] / 2 * 2      # half-spread in and out, roughly
            pos = new
        else:
            cost = 0.0
        r = sum(w * rets[i][c] for c, w in pos.items())
        fin_d = sum(abs(w) * carry_rate(fin, c, 1 if w > 0 else -1) / 252 for c, w in pos.items() if w)
        daily.append((days[i + 1], r + fin_d - cost, fin_d))
    return daily


def perf(daily):
    if len(daily) < 40:
        return None
    rs = [d[1] for d in daily]
    eq, peak, dd = 1.0, 1.0, 0.0
    for r in rs:
        eq *= 1 + r
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak)
    yrs = len(rs) / 252
    ann = eq ** (1 / yrs) - 1 if eq > 0 else -1
    vol = statistics.pstdev(rs) * math.sqrt(252)
    weeks = [sum(rs[i:i + 5]) for i in range(0, len(rs), 5)]
    mon = {}
    for d, r, _ in daily:
        mon[d[:7]] = mon.get(d[:7], 0) + r
    return {"ann": ann, "vol": vol, "sharpe": (ann / vol) if vol else 0, "max_dd": dd, "total": eq - 1,
            "weeks_up": sum(1 for w in weeks if w > 0) / len(weeks), "months_up": sum(1 for v in mon.values() if v > 0),
            "months": len(mon), "carry_part": sum(d[2] for d in daily) / yrs, "days": len(rs)}


def run(days, px, spr, fin, test_months):
    rets = ccy_returns(days, px)
    cut = (dt.datetime.now(UTC) - dt.timedelta(days=int(test_months * 30.4))).strftime("%Y-%m-%d")
    rows = []
    rules = []
    for reb_l, reb in REBAL.items():
        for k in KS:
            rules.append(("carry", f"Carry · long top {k} / short bottom {k} by interest · {reb_l}", "carry", 0, k, reb))
            for L in LOOKBACKS:
                rules.append(("mom", f"Currency strength · {L // 21}-month · top/bottom {k} · {reb_l}", "mom", L, k, reb))
                rules.append(("combo", f"Carry + strength · {L // 21}-month · top/bottom {k} · {reb_l}", "combo", L, k, reb))
        for L in LOOKBACKS:
            rules.append(("tsmom", f"Per-pair trend · {L // 21}-month · {reb_l}", "tsmom", L, 0, reb))
    seen = set()
    for fam, label, kind, L, k, reb in rules:
        if label in seen:
            continue
        seen.add(label)
        d = backtest(days, rets, px, spr, fin, kind, L, k, reb)
        train = perf([x for x in d if x[0] < cut])
        test = perf([x for x in d if x[0] >= cut])
        if train and test:
            rows.append({"family": fam, "label": label, "train": train, "test": test})
    return rows, cut


FAM = {"carry": "Carry (interest rates)", "mom": "Currency strength", "combo": "Carry + strength", "tsmom": "Per-pair trend"}


def summary(rows, months, fin):
    lines = [f"🕵️ Carry Lab: forex carry & currency strength · picked on the earlier {TRAIN_YEARS} years, judged on the last {months:g} months", ""]
    for fam, name in FAM.items():
        rs = [r for r in rows if r["family"] == fam]
        if not rs:
            continue
        best = max(rs, key=lambda r: r["train"]["sharpe"])
        t = best["test"]
        pos = sum(1 for r in rs if r["test"]["ann"] > 0)
        lines += [f"{name}",
                  f"• Best on earlier years: {best['label']} (Sharpe {best['train']['sharpe']:.2f}, {best['train']['ann']:+.1%}/yr)",
                  f"• Last {months:g} months: {t['ann']:+.1%}/yr, worst drop {t['max_dd']:.1%}, {t['months_up']}/{t['months']} months up"
                  + (f", interest {t['carry_part']:+.1%}/yr of it" if fam in ("carry", "combo") else ""),
                  f"• {pos} of {len(rs)} versions were up over the last {months:g} months", ""]
    neg = [p for p, (lr, sr) in fin.items() if lr < 0 and sr < 0]
    if neg:
        lines.append(f"Note: on your OANDA account, holding EITHER side of {len(neg)} of {len(fin)} pairs costs interest (broker markup), which eats carry.")
    lines.append("Returns are on the total position size. At 1:1 (no leverage), +5%/yr on $500 = +$25/yr. Leverage multiplies gains AND drops.")
    return "\n".join(lines)


def render(rows, months, cut, fin, log):
    body = ""
    for fam, name in FAM.items():
        rs = sorted([r for r in rows if r["family"] == fam], key=lambda r: -r["train"]["sharpe"])
        trs = "".join(
            f"<tr><td>{html.escape(r['label'])}</td><td class=n>{r['train']['ann']:+.1%}</td><td class=n>{r['train']['sharpe']:.2f}</td>"
            f"<td class='n {'pos' if r['test']['ann'] > 0 else 'neg'}'>{r['test']['ann']:+.1%}</td><td class=n>{r['test']['max_dd']:.1%}</td>"
            f"<td class=n>{r['test']['months_up']}/{r['test']['months']}</td></tr>" for r in rs)
        body += (f"<section><h2>{name}</h2><div class=tbl><table><tr><th>Version (best first, by the earlier years)</th><th>Earlier yrs /yr</th>"
                 f"<th>Sharpe</th><th>Last {months:g} mo /yr</th><th>Worst drop</th><th>Months up</th></tr>{trs}</table></div></section>")
    finrows = "".join(f"<tr><td>{p}</td><td class=n>{lr:+.2%}</td><td class=n>{sr:+.2%}</td></tr>" for p, (lr, sr) in fin.items())
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Carry Lab</title><style>
:root{{--bg:#0f1220;--panel:#171b2e;--fg:#e8ecf8;--muted:#9aa3c0;--line:#2a3150;--good:#53f0a6;--bad:#ff6b7c}}
@media (prefers-color-scheme: light){{:root:not([data-theme="dark"]){{--bg:#f4f5fa;--panel:#fff;--fg:#151a2c;--muted:#5b6380;--line:#dde1ee;--good:#138a55;--bad:#c23345}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;padding:18px 16px 40px}}
.wrap{{max-width:980px;margin:0 auto;display:grid;gap:16px}} section{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px}}
h2{{margin:0 0 8px;font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}} p{{color:var(--muted)}}
.tbl{{overflow-x:auto}} table{{width:100%;border-collapse:collapse}} td,th{{padding:6px;border-bottom:1px solid var(--line);text-align:left}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}} .pos{{color:var(--good)}} .neg{{color:var(--bad)}}
</style></head><body><div class=wrap><h1>🕵️ Carry Lab</h1>
<p>OANDA daily prices with real spreads and your account's real financing rates. Earlier years pick the settings; the last {months:g} months
(since {cut}) are the honest test. Returns are per year on the total position size (no leverage).</p>{body}
<section><h2>Your OANDA financing rates (per year)</h2><table><tr><th>Pair</th><th>Hold long</th><th>Hold short</th></tr>{finrows}</table>
<p>Positive = OANDA pays you; negative = you pay. These are today's rates, applied to the whole test (rates in the past were somewhat different).</p></section>
<details><summary>Log</summary><ul>{''.join(f'<li>{html.escape(l)}</li>' for l in log)}</ul></details></div></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=float, default=12)
    a = ap.parse_args()
    token, acct = os.environ.get("OANDA_API_TOKEN", "").strip(), os.environ.get("OANDA_ACCOUNT_ID", "").strip()
    if not token or not acct:
        sys.exit("Add OANDA_API_TOKEN and OANDA_ACCOUNT_ID.")
    log, t0 = [], time.time()
    days, px, spr, fin = load(token, acct, TRAIN_YEARS + a.months / 12, log)
    rows, cut = run(days, px, spr, fin, a.months)
    log.append(f"Tested {len(rows)} versions in {time.time() - t0:.0f}s")
    msg = summary(rows, a.months, fin)
    print(msg)
    json.dump({"when": t0, "months": a.months, "cut": cut, "fin": fin, "rows": rows}, open("carry_lab.json", "w"), indent=1)
    open("carry_lab.html", "w").write(render(rows, a.months, cut, fin, log))
    tg = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    try:
        chat = json.load(open(vf.STATE_FILE)).get("chat_id")
    except Exception:
        chat = None
    if tg and chat:
        try:
            vf.http("POST", f"https://api.telegram.org/bot{tg}/sendMessage", body={"chat_id": chat, "text": msg}, tries=2)
        except Exception as e:
            print(f"Telegram failed: {e}")


if __name__ == "__main__":
    main()
