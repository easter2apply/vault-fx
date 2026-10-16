"""Zone Lab: does a supply & demand zone strategy beat the current FX setups?

Her idea: mark 15-min and 4-hour supply/demand zones, use the EMA for trend, wait for price to come back
to a zone, reject it, enter, and ride to the next zone. Tested on ~12 months of OANDA 15-min candles,
spread included, no peeking ahead. First 8 months = "train", last 4 = "unseen".
"""
import json, os, statistics as stt
from datetime import datetime, timedelta, timezone
import vault_fx as vf

PAIRS = ["USD_JPY", "GBP_JPY", "EUR_JPY", "AUD_JPY", "GBP_USD", "AUD_USD", "USD_CHF", "EUR_USD"]
MONTHS = int(os.environ.get("LAB_MONTHS", "12"))
MAX_HOLD = 4 * 24 * 5          # 15-min bars (5 days)

api = vf.Oanda(os.environ["OANDA_API_TOKEN"].strip(), os.environ["OANDA_ACCOUNT_ID"].strip())


def fetch(pair):
    start = datetime.now(timezone.utc) - timedelta(days=30 * MONTHS)
    out, frm = [], start.strftime("%Y-%m-%dT%H:%M:%SZ")
    while True:
        rows = api.get(f"/instruments/{pair}/candles", granularity="M15", count=5000, price="MBA",
                       **{"from": frm})["candles"]
        rows = [c for c in rows if c.get("complete")]
        if not rows:
            break
        for c in rows:
            out.append({"t": c["time"][:19], "o": float(c["mid"]["o"]), "h": float(c["mid"]["h"]),
                        "l": float(c["mid"]["l"]), "c": float(c["mid"]["c"]),
                        "sp": float(c["ask"]["c"]) - float(c["bid"]["c"])})
        nxt = rows[-1]["time"]
        if len(rows) < 4000 or nxt[:19] == frm[:19]:
            break
        frm = nxt
    seen, uniq = set(), []
    for b in out:
        if b["t"] not in seen:
            seen.add(b["t"]); uniq.append(b)
    return uniq


def to_h4(m15):
    """Group 15-min bars into 4-hour bars. Each 4H bar records the index of its last 15-min bar (when it's known)."""
    out, cur = [], None
    for i, b in enumerate(m15):
        key = b["t"][:11] + f"{int(b['t'][11:13]) // 4 * 4:02d}"
        if cur is None or cur["k"] != key:
            if cur:
                out.append(cur)
            cur = {"k": key, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "end": i}
        else:
            cur["h"] = max(cur["h"], b["h"]); cur["l"] = min(cur["l"], b["l"]); cur["c"] = b["c"]; cur["end"] = i
    if cur:
        out.append(cur)
    return out


def atr_list(bars, n=14):
    out, a = [], None
    for i, b in enumerate(bars):
        tr = b["h"] - b["l"] if i == 0 else max(b["h"] - b["l"], abs(b["h"] - bars[i - 1]["c"]), abs(b["l"] - bars[i - 1]["c"]))
        a = tr if a is None else (a * (n - 1) + tr) / n
        out.append(a)
    return out


def find_zones(bars, atr, known_at):
    """Classic base-then-explosive-move zones.
    Base = 1-3 small candles (range < 0.8 ATR). Impulse = the next 1-2 candles move > 2 ATR away from the base.
    Demand = base low..base high when the move is up; supply when down. known_at(j) = 15-min index when it's confirmed."""
    zones = []
    for i in range(20, len(bars) - 2):
        a = atr[i - 1]
        if not a:
            continue
        for blen in (1, 2, 3):
            base = bars[i - blen:i]
            if len(base) < blen or any(b["h"] - b["l"] > 0.8 * a for b in base):
                continue
            hi, lo = max(b["h"] for b in base), min(b["l"] for b in base)
            for k in (1, 2):
                imp = bars[i:i + k]
                if len(imp) < k:
                    continue
                up = max(b["h"] for b in imp) - hi
                dn = lo - min(b["l"] for b in imp)
                if up > 2 * a and imp[-1]["c"] > hi + 1.5 * a:
                    zones.append({"kind": "demand", "hi": hi, "lo": lo, "known": known_at(i + k - 1), "used": 0, "dead": False})
                    break
                if dn > 2 * a and imp[-1]["c"] < lo - 1.5 * a:
                    zones.append({"kind": "supply", "hi": hi, "lo": lo, "known": known_at(i + k - 1), "used": 0, "dead": False})
                    break
            else:
                continue
            break
    zones.sort(key=lambda z: z["known"])
    return zones


def ema(xs, n):
    k, out, e = 2 / (n + 1), [], None
    for x in xs:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def walk(m15, i, d, entry, sl, tp):
    """Simulate from bar i (entry at its open). Stop checked first if both hit in one bar. Returns R (before spread)."""
    risk = abs(entry - sl)
    for j in range(i, min(len(m15), i + MAX_HOLD)):
        b = m15[j]
        if d == 1:
            if b["l"] <= sl: return -1.0, j
            if b["h"] >= tp: return (tp - entry) / risk, j
        else:
            if b["h"] >= sl: return -1.0, j
            if b["l"] <= tp: return (entry - tp) / risk, j
    j = min(len(m15), i + MAX_HOLD) - 1
    return d * (m15[j]["c"] - entry) / risk, j


def run(pair, m15, variant):
    h4 = to_h4(m15)
    a15, a4 = atr_list(m15), atr_list(h4)
    cl15 = [b["c"] for b in m15]
    e50_15 = ema(cl15, 50)
    e200_15 = ema(cl15, 200)
    h4c = [b["c"] for b in h4]
    e50_4 = ema(h4c, 50)
    # which completed 4H bar is known at each 15-min index
    h4_at, j = [], -1
    for i in range(len(m15)):
        while j + 1 < len(h4) and h4[j + 1]["end"] < i:
            j += 1
        h4_at.append(j)
    z15 = find_zones(m15, a15, lambda k: k + 1)
    z4 = find_zones(h4, a4, lambda k: h4[k]["end"] + 1)
    for z in z15: z["tf"] = "15m"
    for z in z4: z["tf"] = "4h"
    allz = sorted(z15 + z4, key=lambda z: z["known"])
    entry_tf = "4h" if variant["zones"] == "4h" else "15m"
    target_tfs = {"15m", "4h"} if variant["zones"] in ("both", "4h") else {"15m"}
    trades, busy_until, zi, live = [], 0, 0, []
    for i in range(300, len(m15) - 1):
        while zi < len(allz) and allz[zi]["known"] <= i:
            live.append(allz[zi]); zi += 1
        b = m15[i]
        # zones die when price closes through them; keep the most recent 120 per timeframe
        for z in live:
            if (z["kind"] == "demand" and b["c"] < z["lo"]) or (z["kind"] == "supply" and b["c"] > z["hi"]):
                z["dead"] = True
        live = [z for z in live if not z["dead"]]
        if len(live) > 240:
            l15 = [z for z in live if z["tf"] == "15m"][-120:]
            l4 = [z for z in live if z["tf"] == "4h"][-120:]
            live = sorted(l15 + l4, key=lambda z: z["known"])
        if i < busy_until:
            continue
        k4 = h4_at[i]
        if k4 < 60:
            continue
        trend4 = 1 if h4c[k4] > e50_4[k4] else -1
        trend15 = 1 if e50_15[i] > e200_15[i] else -1
        if variant["trend"] == "4h":
            trend = trend4
        elif variant["trend"] == "both":
            if trend4 != trend15:
                continue
            trend = trend4
        else:
            trend = trend15
        a = a15[i]
        for z in reversed(live):
            if z["tf"] != entry_tf or z["used"] >= variant["touches"]:
                continue
            want = "demand" if trend == 1 else "supply"
            if z["kind"] != want:
                continue
            if variant["zones"] == "both":   # 15-min zone must sit inside/overlap a live 4H zone of the same kind
                if not any(y["tf"] == "4h" and y["kind"] == want and y["lo"] <= z["hi"] and y["hi"] >= z["lo"] for y in live):
                    continue
            # rejection: candle wicks into the zone and closes back out, in the trend direction
            if want == "demand":
                tapped = b["l"] <= z["hi"] and b["l"] >= z["lo"] - 0.25 * a
                rej = tapped and b["c"] > z["hi"] and b["c"] > b["o"] and (b["c"] - b["l"]) >= 0.6 * (b["h"] - b["l"])
            else:
                tapped = b["h"] >= z["lo"] and b["h"] <= z["hi"] + 0.25 * a
                rej = tapped and b["c"] < z["lo"] and b["c"] < b["o"] and (b["h"] - b["c"]) >= 0.6 * (b["h"] - b["l"])
            if tapped:
                z["used"] += 1
            if not rej:
                continue
            d = trend
            entry = m15[i + 1]["o"]
            sl = (z["lo"] - 0.2 * a) if d == 1 else (z["hi"] + 0.2 * a)
            risk = abs(entry - sl)
            if risk <= 0 or risk > 4 * a:
                break
            if variant["target"] == "zone":
                opp = "supply" if d == 1 else "demand"
                cands = [(y["lo"] if d == 1 else y["hi"]) for y in live if y["tf"] in target_tfs and y["kind"] == opp
                         and ((y["lo"] > entry) if d == 1 else (y["hi"] < entry))]
                if not cands:
                    break
                tp = min(cands) if d == 1 else max(cands)
                if abs(tp - entry) < variant["min_rr"] * risk:
                    break
            else:
                tp = entry + d * variant["rr"] * risk
            r, jx = walk(m15, i + 1, d, entry, sl, tp)
            cost = m15[i + 1]["sp"] / risk if m15[i + 1]["sp"] else 0
            trades.append({"pair": pair, "t": m15[i + 1]["t"], "d": d, "r": round(r - cost, 3),
                           "rr": round(abs(tp - entry) / risk, 2)})
            busy_until = jx + 1
            break
    return trades


def baseline(pair, m15):
    """Today's bot, simplified: 1-hour EMA20 pullback in the EMA20/50 trend, 4H EMA50/200 agrees, ADX >= 22.
    Stop 2 ATR, target 3 ATR."""
    h1, cur = [], None
    for i, b in enumerate(m15):
        key = b["t"][:13]
        if cur is None or cur["k"] != key:
            if cur: h1.append(cur)
            cur = {"k": key, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "end": i}
        else:
            cur["h"] = max(cur["h"], b["h"]); cur["l"] = min(cur["l"], b["l"]); cur["c"] = b["c"]; cur["end"] = i
    if cur: h1.append(cur)
    h4 = to_h4(m15)
    cl = [x["c"] for x in h1]
    e20, e50 = ema(cl, 20), ema(cl, 50)
    atr, adx = vf.atr_adx([{"o": x["o"], "h": x["h"], "l": x["l"], "c": x["c"]} for x in h1])
    c4 = [x["c"] for x in h4]
    h50, h200 = ema(c4, 50), ema(c4, 200)
    trades, busy, k4 = [], 0, -1
    for i in range(250, len(h1) - 1):
        while k4 + 1 < len(h4) and h4[k4 + 1]["end"] <= h1[i]["end"]:
            k4 += 1
        mi = h1[i]["end"] + 1
        if mi < busy or mi >= len(m15) or k4 < 200 or not adx[i] or adx[i] < 22:
            continue
        tr = 1 if e20[i] > e50[i] else -1
        if (1 if h50[k4] > h200[k4] else -1) != tr:
            continue
        touched = any(h1[j]["l"] <= e20[j] <= h1[j]["h"] for j in (i - 1, i))
        right = (h1[i]["c"] > e20[i]) if tr == 1 else (h1[i]["c"] < e20[i])
        if not (touched and right):
            continue
        entry = m15[mi]["o"]
        sl, tp = entry - tr * 2 * atr[i], entry + tr * 3 * atr[i]
        r, jx = walk(m15, mi, tr, entry, sl, tp)
        cost = m15[mi]["sp"] / abs(entry - sl)
        trades.append({"pair": pair, "t": m15[mi]["t"], "d": tr, "r": round(r - cost, 3), "rr": 1.5})
        busy = jx + 1
    return trades


VARIANTS = {
    "A. 15m zones, 15m EMA trend, ride to next zone": {"zones": "15m", "trend": "15m", "target": "zone", "min_rr": 1.5, "touches": 1},
    "B. 4H zones, 4H EMA trend, ride to next zone": {"zones": "4h", "trend": "4h", "target": "zone", "min_rr": 1.5, "touches": 1},
    "C. Her full plan: 15m zone inside a 4H zone, both EMAs agree, next zone": {"zones": "both", "trend": "both", "target": "zone", "min_rr": 1.5, "touches": 1},
    "D. Same as C but fixed 2R target": {"zones": "both", "trend": "both", "target": "fixed", "rr": 2.0, "touches": 1},
    "E. 15m zones, both EMAs agree, next zone": {"zones": "15m", "trend": "both", "target": "zone", "min_rr": 1.5, "touches": 1},
    "F. 15m zones, both EMAs agree, fixed 2R": {"zones": "15m", "trend": "both", "target": "fixed", "rr": 2.0, "touches": 1},
}


def stats(tr, split):
    def s(xs):
        if not xs:
            return {"n": 0}
        rs = [x["r"] for x in xs]
        sd = stt.pstdev(rs) if len(rs) > 1 else 0
        return {"n": len(rs), "win": round(sum(r > 0 for r in rs) / len(rs), 3), "avg": round(stt.mean(rs), 3),
                "total": round(sum(rs), 1), "t": round(stt.mean(rs) / (sd / len(rs) ** 0.5), 2) if sd else 0,
                "avg_rr": round(stt.mean(x["rr"] for x in xs), 2)}
    return {"all": s(tr), "train": s([x for x in tr if x["t"] < split]), "unseen": s([x for x in tr if x["t"] >= split])}


def main():
    split = (datetime.now(timezone.utc) - timedelta(days=30 * MONTHS // 3)).strftime("%Y-%m-%dT%H:%M:%S")
    data = {}
    for p in PAIRS:
        try:
            data[p] = fetch(p)
            print(p, len(data[p]), "bars", data[p][0]["t"], "→", data[p][-1]["t"])
        except Exception as e:
            print(p, "skipped", e)
    report = {"split": split, "variants": {}, "by_pair": {}}
    allv = {"Today's bot (1H EMA pullback)": None, **VARIANTS}
    for name, v in allv.items():
        tr = []
        for p, m in data.items():
            tr += baseline(p, m) if v is None else run(p, m, v)
        st = stats(tr, split)
        report["variants"][name] = st
        report["by_pair"][name] = {p: stats([x for x in tr if x["pair"] == p], split)["all"] for p in data}
        a = st["all"]
        print(f"{name:70} n={a.get('n',0):4} win={a.get('win',0):.0%} avg={a.get('avg',0):+.3f}R total={a.get('total',0):+.1f}R t={a.get('t',0)} "
              f"| unseen n={st['unseen'].get('n',0)} avg={st['unseen'].get('avg',0):+.3f}R")
    json.dump(report, open("zone_lab.json", "w"), indent=1)


if __name__ == "__main__":
    main()
