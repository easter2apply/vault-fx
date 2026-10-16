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


def confirm(bars, i, d, a, e200, mode):
    """Returns (first confirmation candle, entry candle) or None."""
    side = lambda k: (bars[k]["c"] - e200[k]) * d > 0
    if mode == "strict2":
        for s in (i, i + 1):
            if strong(bars[s], d, a) and strong(bars[s + 1], d, a) and side(s + 1):
                return s, s + 1
    elif mode == "2of4":
        st = [k for k in range(i, i + 4) if strong(bars[k], d, a)]
        if len(st) >= 2 and side(st[1]):
            return st[0], st[1]
    elif mode == "engulf":
        for s in (i, i + 1, i + 2):
            b = bars[s]; body = (b["c"] - b["o"]) * d; rng = b["h"] - b["l"]
            if body > 0 and rng > 0 and body >= 0.6 * rng and body >= 0.6 * a and side(s):
                return s, s
    elif mode == "combined":
        for s in (i, i + 1):
            b1, b2 = bars[s], bars[s + 1]
            if (b1["c"] - b1["o"]) * d > 0 and (b2["c"] - b2["o"]) * d > 0 and (b2["c"] - b1["o"]) * d >= 0.6 * a and side(s + 1):
                return s, s + 1
    return None


def run(pair, bars, v):
    atr = zl.atr_list(bars)
    e200 = zl.ema([b["c"] for b in bars], 200)
    zones = zl.find_zones(bars, atr, lambda k: k + 1, imp=1.5, cls=1.0, base_max=1.0) if v["zone"] else []
    sh, sl_ = pivots(bars)
    trades, busy, zi, live = [], 0, 0, []
    hi_i = lo_i = 0
    SLOPE_N = 50
    hdir = v.get("hdir_by_pair", {}).get(pair)
    for i in range(260, len(bars) - 6):
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
        if hdir is not None and hdir[i] != d:
            continue
        cnt("1 EMA sloped")
        # price was on the trend side, and this candle taps the EMA exactly
        prev_side = all((bars[j]["c"] - e200[j]) * d > 0 for j in range(i - 3, i))
        nr = v.get("near", 0) * a
        tap = (b["l"] <= e + nr) if d == 1 else (b["h"] >= e - nr)
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
        cc = confirm(bars, i, d, a, e200, v.get("confirm", "strict2"))
        if cc is None:
            continue
        cnt("4 confirmation candles")
        c1, c2 = cc
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


BASE = {"zone": True, "slope": 1.0, "min_rr": 0.0}
MAJORS = ["EUR", "GBP", "USD", "JPY", "AUD", "NZD", "CAD", "CHF"]


def htf_dirs(m5):
    """For each 5-min candle: the slope direction of the last completed 15-min 200 EMA (0 if flat)."""
    m15, idx = [], []
    for i, b in enumerate(m5):
        key = b["t"][:14] + f"{int(b['t'][14:16]) // 15 * 15:02d}"
        if not m15 or m15[-1]["k"] != key:
            m15.append({"k": key, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
        else:
            x = m15[-1]; x["h"] = max(x["h"], b["h"]); x["l"] = min(x["l"], b["l"]); x["c"] = b["c"]
        idx.append(len(m15) - 1)
    atr = zl.atr_list(m15); e = zl.ema([x["c"] for x in m15], 200)
    dirs15 = [0] * len(m15)
    for k in range(250, len(m15)):
        sl = (e[k] - e[k - 50]) / atr[k] if atr[k] else 0
        dirs15[k] = 1 if sl >= 1 else -1 if sl <= -1 else 0
    return [dirs15[idx[i] - 1] if idx[i] > 0 else 0 for i in range(len(m5))]


def main():
    split = (datetime.now(timezone.utc) - timedelta(days=30 * MONTHS // 3)).strftime("%Y-%m-%dT%H:%M:%S")
    report = {"split": split, "results": {}}
    inst = zl.api.instruments()
    allp = sorted(n for n, i in inst.items() if i.get("type") == "CURRENCY"
                  and n.split("_")[0] in MAJORS and n.split("_")[1] in MAJORS)
    extra = [p for p in allp if p not in PAIRS]
    cache = {}

    def get(gran, pairs):
        out = {}
        for p in pairs:
            if (gran, p) not in cache:
                try:
                    cache[(gran, p)] = zl.fetch(p, gran)
                except Exception as ex:
                    print(gran, p, "skipped", ex); cache[(gran, p)] = None
            if cache[(gran, p)]:
                out[p] = cache[(gran, p)]
        return out

    def test(label, data, v):
        CNT.clear(); tr = []
        for p, m in data.items():
            tr += run(p, m, v)
        st = zl.stats(tr, split)
        st["funnel"] = dict(sorted(CNT.items()))
        st["by_pair"] = {p: zl.stats([x for x in tr if x["pair"] == p], split)["all"] for p in data}
        report["results"][label] = st
        a = st["all"]
        print(f"{label:70} n={a.get('n',0):4} win={a.get('win',0):.0%} avg={a.get('avg',0):+.3f}R tot={a.get('total',0):+.1f} "
              f"t={a.get('t',0)} | unseen n={st['unseen'].get('n',0)} avg={st['unseen'].get('avg',0):+.3f}", flush=True)
        json.dump(report, open("ema200_lab.json", "w"), indent=1)

    m15 = get("M15", PAIRS)
    test("0. Her rules as-is (15m, 8 pairs)", m15, BASE)
    test("1a. 2 strong candles anywhere in next 4", m15, {**BASE, "confirm": "2of4"})
    test("1b. 1 big engulfing-style candle is enough", m15, {**BASE, "confirm": "engulf"})
    test("1c. 2 candles that move strong together", m15, {**BASE, "confirm": "combined"})
    test("5. 'Almost' tap counts (within 0.1 ATR)", m15, {**BASE, "near": 0.1})
    test("5+1a. Almost tap + 2 of next 4", m15, {**BASE, "near": 0.1, "confirm": "2of4"})
    for g in ("M30", "H1"):
        d = get(g, PAIRS)
        test(f"3. {g} chart, her rules", d, BASE)
        test(f"3. {g} chart, 2 of next 4", d, {**BASE, "confirm": "2of4"})
    m5 = get("M5", PAIRS)
    hd = {p: htf_dirs(m) for p, m in m5.items()}
    test("4. 5m, her rules, 15m EMA must slope same way", m5, {**BASE, "hdir_by_pair": hd})
    test("4. 5m, 2 of next 4, 15m EMA agrees", m5, {**BASE, "confirm": "2of4", "hdir_by_pair": hd})
    mx = get("M15", extra)
    print("extra pairs:", list(mx))
    test("2. 20 MORE pairs (15m), her rules", mx, BASE)
    test("2. 20 MORE pairs (15m), 2 of next 4", mx, {**BASE, "confirm": "2of4"})
    test("2. 20 MORE pairs (15m), engulf", mx, {**BASE, "confirm": "engulf"})


if __name__ == "__main__":
    main()
