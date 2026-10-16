"""EMA 200 Lab: Easter's exact strategy — the 200 EMA as confluence in a supply/demand zone.

Rules (from her write-up):
 1. A supply or demand zone forms first (big move).
 2. The 200 EMA is sloping (diagonal), not flat. Price is trading on the trend side of it.
 3. Price taps the 200 EMA exactly (skip if it doesn't touch).
 4. Then at least TWO very convincing candles in the trend direction. Enter at the close of the 2nd.
 5. Stop beyond the high/low of the 1st confirmation candle.
 6. Take profit at the previous swing high/low.
Tested on ~12 months of OANDA candles, spread included, no peeking ahead.
"""
import json, os, statistics as stt
from datetime import datetime, timedelta, timezone
import zone_lab as zl

PAIRS = zl.PAIRS
MONTHS = zl.MONTHS


def pivots(bars, k=3):
    """Swing highs/lows confirmed k bars later: list of (confirmed_at_index, price)."""
    hi, lo = [], []
    for i in range(k, len(bars) - k):
        h, l = bars[i]["h"], bars[i]["l"]
        if all(h >= bars[j]["h"] for j in range(i - k, i + k + 1)):
            hi.append((i + k, h))
        if all(l <= bars[j]["l"] for j in range(i - k, i + k + 1)):
            lo.append((i + k, l))
    return hi, lo


def strong(b, d, atr):
    body = (b["c"] - b["o"]) * d
    rng = b["h"] - b["l"]
    return body > 0 and rng > 0 and body >= 0.6 * rng and body >= 0.25 * atr


CNT = {}


def cnt(k):
    CNT[k] = CNT.get(k, 0) + 1


def run(pair, bars, v):
    atr = zl.atr_list(bars)
    e200 = zl.ema([b["c"] for b in bars], 200)
    zones = zl.find_zones(bars, atr, lambda k: k + 1, imp=1.5, cls=1.0, base_max=1.0) if v["zone"] else []
    sh, sl_ = pivots(bars)
    trades, busy, zi, live = [], 0, 0, []
    hi_i = lo_i = 0
    SLOPE_N = 50
    for i in range(260, len(bars) - 4):
        while zi < len(zones) and zones[zi]["known"] <= i:
            live.append(zones[zi]); zi += 1
        b = bars[i]
        if live:
            for z in live:
                if (z["kind"] == "demand" and b["c"] < z["lo"]) or (z["kind"] == "supply" and b["c"] > z["hi"]):
                    z["dead"] = True
            live = [z for z in live if not z["dead"]][-150:]
        if i < busy:
            continue
        a, e = atr[i], e200[i]
        slope = (e - e200[i - SLOPE_N]) / a if a else 0
        if abs(slope) < v["slope"]:
            continue
        d = 1 if slope > 0 else -1
        cnt("1 EMA sloped")
        # price was on the trend side, and this candle taps the EMA exactly
        prev_side = all((bars[j]["c"] - e200[j]) * d > 0 for j in range(i - 3, i))
        tap = (b["l"] <= e) if d == 1 else (b["h"] >= e)
        if not (prev_side and tap):
            continue
        cnt("2 tapped EMA from trend side")
        if v["zone"]:
            want = "demand" if d == 1 else "supply"
            near = 0.5 * a
            if not any(z["kind"] == want and z["lo"] - near <= e <= z["hi"] + near for z in live):
                continue
        cnt("3 EMA inside a zone")
        # two convincing candles: the tap candle itself can be the 1st, or the next one
        c1 = None
        for s in (i, i + 1):
            if strong(bars[s], d, a) and strong(bars[s + 1], d, a) and (bars[s + 1]["c"] - e200[s + 1]) * d > 0:
                c1 = s; break
        if c1 is None:
            continue
        cnt("4 two strong candles")
        c2 = c1 + 1
        entry = bars[c2]["c"]
        stop = bars[c1]["l"] if d == 1 else bars[c1]["h"]
        stop -= d * bars[c2]["sp"]
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        # previous swing high (longs) / low (shorts) beyond entry, known by now
        if d == 1:
            cands = [p for k, p in sh if k <= c2 and p > entry and k >= c2 - 300]
        else:
            cands = [p for k, p in sl_ if k <= c2 and p < entry and k >= c2 - 300]
        if not cands:
            continue
        cnt("5 swing target found")
        tp = cands[-1]                         # the most recent swing
        rr = abs(tp - entry) / risk
        if rr < v["min_rr"]:
            continue
        r, jx = zl.walk(bars, c2 + 1, d, entry, stop, tp)
        r -= bars[c2]["sp"] / risk
        trades.append({"pair": pair, "t": bars[c2]["t"], "d": d, "r": round(r, 3), "rr": round(rr, 2),
                       "hour_et": (int(bars[c2]["t"][11:13]) - 4) % 24})
        busy = jx + 1
    return trades


VARIANTS = {
    "Her exact rules, gentle slope (EMA moved 0.5 ATR / 50 candles)": {"zone": True, "slope": 0.5, "min_rr": 0.0},
    "Her exact rules, clear slope (1 ATR / 50 candles)": {"zone": True, "slope": 1.0, "min_rr": 0.0},
    "Her exact rules, steep slope (2 ATR / 50 candles)": {"zone": True, "slope": 2.0, "min_rr": 0.0},
    "Clear slope + only trades with TP at least 1R away": {"zone": True, "slope": 1.0, "min_rr": 1.0},
    "No zone needed, clear slope": {"zone": False, "slope": 1.0, "min_rr": 0.0},
    "No zone needed, clear slope, TP 1R+": {"zone": False, "slope": 1.0, "min_rr": 1.0},
}


def main():
    split = (datetime.now(timezone.utc) - timedelta(days=30 * MONTHS // 3)).strftime("%Y-%m-%dT%H:%M:%S")
    report = {"split": split, "results": {}}
    for gran in ("M5", "M15"):
        data = {}
        for p in PAIRS:
            try:
                data[p] = zl.fetch(p, gran)
                print(gran, p, len(data[p]), "bars")
            except Exception as ex:
                print(gran, p, "skipped", ex)
        for name, v in VARIANTS.items():
            tr = []
            CNT.clear()
            for p, m in data.items():
                tr += run(p, m, v)
            print("   funnel:", dict(sorted(CNT.items())))
            st = zl.stats(tr, split)
            st["funnel"] = dict(sorted(CNT.items()))
            st["by_pair"] = {p: zl.stats([x for x in tr if x["pair"] == p], split)["all"] for p in data}
            hrs = {}
            for x in tr:
                hrs.setdefault(x["hour_et"] // 4 * 4, []).append(x["r"])
            st["by_time_et"] = {f"{h:02d}-{h+4:02d}": {"n": len(r), "avg": round(stt.mean(r), 3)} for h, r in sorted(hrs.items())}
            report["results"][f"{gran} | {name}"] = st
            a = st["all"]
            print(f"{gran} {name:62} n={a.get('n',0):4} win={a.get('win',0):.0%} avg={a.get('avg',0):+.3f}R tot={a.get('total',0):+.1f} "
                  f"t={a.get('t',0)} | unseen n={st['unseen'].get('n',0)} avg={st['unseen'].get('avg',0):+.3f}")
    json.dump(report, open("ema200_lab.json", "w"), indent=1)


if __name__ == "__main__":
    main()
