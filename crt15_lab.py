"""CRT 15m Lab: Easter's own CRT plan, 28 pairs, last 12 months (OANDA 15m + 5m candles, spread included).

SHORT (long is the mirror):
 1. Range candle R = a GREEN 15m candle.
 2. Next 15m candle N sweeps R's high, then pushes down to ~50% of R (R's midpoint).
 3. Entry A (confirmation): after the 50% touch, inside N, a bearish 5m confirmation:
      - 5m CRT: a 5m candle takes the prior 5m candle's high and closes back below it, red; or
      - 5m break of structure (IFVG-style close-through): a 5m close below the lowest low of the prior 3 5m candles.
    Enter at that 5m close.
 4. Entry B (no confirmation): N closes red, back inside R and below R's 50%. Enter on a pullback to R's 50%
    within the next hour (4 x 15m), if the stop isn't hit first.
 5. Stop: above the sweep high (N's high) + spread.
 6. TP1: R's body (its open). TP2: the next demand zone below (15m supply/demand zones); if none, R's low.
    Default management: half off at TP1, stop to entry, rest to TP2. Max hold 24h.
"""
import bisect
import json
import statistics as stt
from datetime import datetime, timedelta, timezone

import zone_lab as zl
import vault_fx as vf

PAIRS = vf.PAIRS
COST_FREE = False


def ts(t):
    return datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp()


def walk(m5, m5_ts, t0, d, entry, stop, tp1, tp2, mode, max_s=86400):
    """Manage on 5m candles from t0. mode: 'half' (half at TP1, stop to entry, rest TP2), 'tp1', 'tp2'.
    Returns (R before costs, exit ts)."""
    risk = abs(entry - stop)
    i = bisect.bisect_left(m5_ts, t0)
    half_done, banked, sl = False, 0.0, stop
    tgt = tp2 if mode == "tp2" else tp1
    end = t0 + max_s
    last = None
    while i < len(m5) and m5_ts[i] < end:
        b = m5[i]
        hit_sl = (b["h"] >= sl) if d == -1 else (b["l"] <= sl)
        hit_tg = (b["l"] <= tgt) if d == -1 else (b["h"] >= tgt)
        if hit_sl:
            rest = d * (sl - entry) / risk
            return (banked + (0.5 if half_done else 1.0) * rest), m5_ts[i] + 300
        if hit_tg:
            g = abs(tgt - entry) / risk
            if mode == "half" and not half_done:
                half_done, banked, sl, tgt = True, 0.5 * g, entry, tp2
                hit2 = (b["l"] <= tgt) if d == -1 else (b["h"] >= tgt)
                if not hit2:
                    i += 1; last = b; continue
                g = abs(tgt - entry) / risk
            return (banked + (0.5 if half_done else 1.0) * g), m5_ts[i] + 300
        last = b
        i += 1
    if last is None:
        return None, None
    return banked + (0.5 if half_done else 1.0) * d * (last["c"] - entry) / risk, end


def setups(pair, m15, m5, m5_ts, daily, v):
    out = []
    atr = vf._atr_list(m15)
    zones = vf._find_zones(m15, atr)
    zi, live = 0, []
    dts = [ts(x["t"]) for x in daily]; dcl = [x["c"] for x in daily]; e50 = zl.ema(dcl, 50)
    busy_until = 0
    for k in range(30, len(m15) - 6):
        while zi < len(zones) and zones[zi]["known"] <= k:
            live.append(zones[zi]); zi += 1
        c = m15[k - 1]["c"]
        live = [z for z in live if not ((z["kind"] == "demand" and c < z["lo"]) or (z["kind"] == "supply" and c > z["hi"]))][-150:]
        R, N = m15[k - 1], m15[k]
        r_ts, n_ts = ts(R["t"]), ts(N["t"])
        if n_ts - r_ts != 900 or n_ts < busy_until:
            continue
        if v.get("hours") and not (7 <= datetime.fromtimestamp(n_ts, timezone.utc).hour < 17):
            continue
        green, red = R["c"] > R["o"], R["c"] < R["o"]
        if green and N["h"] > R["h"]:
            d = -1
        elif red and N["l"] < R["l"]:
            d = 1
        else:
            continue
        if v.get("trend"):
            j = bisect.bisect_right(dts, n_ts - 86400) - 1
            if j < 50 or (1 if dcl[j] > e50[j] else -1) != d:
                continue
        mid = (R["h"] + R["l"]) / 2
        # 5m sequence inside N: sweep first, then the 50% push
        a = bisect.bisect_left(m5_ts, n_ts); b_end = bisect.bisect_left(m5_ts, n_ts + 900)
        five = list(range(a, b_end))
        if len(five) < 2:
            continue
        swept = touched = None
        for i in five:
            x = m5[i]
            if swept is None and ((x["h"] > R["h"]) if d == -1 else (x["l"] < R["l"])):
                swept = i
            if swept is not None and ((x["l"] <= mid) if d == -1 else (x["h"] >= mid)):
                touched = i; break
        if touched is None:
            continue
        sweep_ext = max(m5[i]["h"] for i in five) if d == -1 else min(m5[i]["l"] for i in five)
        entry = t_in = None
        how = None
        if v["entry"] in ("both", "confirm"):
            for i in range(max(touched, a + 1), b_end):
                x, p = m5[i], m5[i - 1]
                crt5 = ((x["h"] > p["h"] and x["c"] < p["h"] and x["c"] < x["o"]) if d == -1 else
                        (x["l"] < p["l"] and x["c"] > p["l"] and x["c"] > x["o"]))
                bos = i - 3 >= 0 and ((x["c"] < min(m5[j]["l"] for j in range(i - 3, i))) if d == -1 else
                                      (x["c"] > max(m5[j]["h"] for j in range(i - 3, i))))
                if (crt5 or bos) and ((x["c"] < R["h"]) if d == -1 else (x["c"] > R["l"])):
                    entry, t_in, how = x["c"], m5_ts[i] + 300, "confirm"
                    sweep_ext = max(m5[j]["h"] for j in range(a, i + 1)) if d == -1 else min(m5[j]["l"] for j in range(a, i + 1))
                    break
        if entry is None and v["entry"] in ("both", "pullback"):
            closed_ok = ((N["c"] < N["o"] and N["c"] < mid) if d == -1 else (N["c"] > N["o"] and N["c"] > mid))
            if closed_ok:
                p0 = bisect.bisect_left(m5_ts, n_ts + 900); p1 = bisect.bisect_left(m5_ts, n_ts + 900 + 3600)
                for i in range(p0, p1):
                    x = m5[i]
                    if (x["h"] >= sweep_ext) if d == -1 else (x["l"] <= sweep_ext):
                        break                               # stop area hit before the pullback filled
                    if (x["h"] >= mid) if d == -1 else (x["l"] <= mid):
                        entry, t_in, how = mid, m5_ts[i] + 300, "pullback"
                        break
        if entry is None:
            continue
        sp = m15[k]["sp"]
        stop = sweep_ext - d * sp
        risk = abs(entry - stop)
        tp1 = R["o"]
        if risk <= 0 or (tp1 - entry) * d <= 0:
            continue
        below = [z["hi"] for z in live if z["kind"] == "demand" and z["hi"] < tp1] if d == -1 else \
                [z["lo"] for z in live if z["kind"] == "supply" and z["lo"] > tp1]
        tp2 = (max(below) if d == -1 else min(below)) if below else (R["l"] if d == -1 else R["h"])
        if (tp2 - tp1) * d < 0:
            tp2 = tp1
        r, t_out = walk(m5, m5_ts, t_in, d, entry, stop, tp1, tp2, v.get("mode", "half"))
        if r is None:
            continue
        cost = 0 if COST_FREE else sp / risk
        out.append({"pair": pair, "t": t_in, "t_out": t_out, "d": d, "r": r - cost, "how": how,
                    "rr1": abs(tp1 - entry) / risk, "rr2": abs(tp2 - entry) / risk, "risk_pips": risk})
        busy_until = t_out
    return out


VERSIONS = {
    "Her plan (confirmation or pullback) · half at TP1, rest TP2": {"entry": "both"},
    "Confirmation only": {"entry": "confirm"},
    "Pullback only": {"entry": "pullback"},
    "Her plan · London/NY hours only (3 AM–1 PM ET)": {"entry": "both", "hours": True},
    "Her plan · with the daily trend": {"entry": "both", "trend": True},
    "Her plan · London/NY + daily trend": {"entry": "both", "hours": True, "trend": True},
    "Her plan · all out at TP1": {"entry": "both", "mode": "tp1"},
    "Her plan · all out at TP2": {"entry": "both", "mode": "tp2"},
}


def summ(tr, split):
    def s(xs):
        if not xs:
            return {"n": 0}
        rs = [x["r"] for x in xs]
        sd = stt.pstdev(rs) if len(rs) > 1 else 0
        streak = worst = 0
        for r in rs:
            streak = streak + 1 if r < 0 else 0; worst = max(worst, streak)
        return {"n": len(rs), "per_week": round(len(rs) / 52, 1), "win": round(sum(r > 0 for r in rs) / len(rs), 3),
                "avg_r": round(stt.mean(rs), 3), "total_r": round(sum(rs), 1),
                "t": round(stt.mean(rs) / (sd / len(rs) ** 0.5), 2) if sd else 0, "worst_streak": worst,
                "conf_share": round(sum(1 for x in xs if x["how"] == "confirm") / len(xs), 2),
                "avg_rr1": round(stt.mean(x["rr1"] for x in xs), 2), "avg_rr2": round(stt.mean(x["rr2"] for x in xs), 2)}
    return {"all": s(tr), "train": s([x for x in tr if x["t"] < split]), "unseen": s([x for x in tr if x["t"] >= split]),
            "confirm": s([x for x in tr if x["how"] == "confirm"]), "pullback": s([x for x in tr if x["how"] == "pullback"])}


def main():
    split = (datetime.now(timezone.utc) - timedelta(days=120)).timestamp()
    data = {}
    for p in PAIRS:
        try:
            m15, m5, d1 = zl.fetch(p, "M15"), zl.fetch(p, "M5"), zl.fetch(p, "D")
            data[p] = (m15, m5, [ts(x["t"]) for x in m5], d1)
            print(p, len(m15), len(m5), flush=True)
        except Exception as e:
            print(p, "skipped", e, flush=True)
    rep = {"versions": {}, "by_pair": {}}
    for name, v in VERSIONS.items():
        tr = []
        for p, (m15, m5, m5_ts, d1) in data.items():
            tr += setups(p, m15, m5, m5_ts, d1, v)
        st = summ(tr, split)
        rep["versions"][name] = st
        rep["by_pair"][name] = {p: summ([x for x in tr if x["pair"] == p], split)["all"] for p in data}
        a, u = st["all"], st["unseen"]
        print(f"{name:58} n={a.get('n', 0):5} /wk={a.get('per_week', 0):5} win={a.get('win', 0):.0%} avg={a.get('avg_r', 0):+.3f} "
              f"t={a.get('t', 0)} streak={a.get('worst_streak', 0)} conf={a.get('conf_share')} | unseen {u.get('avg_r', 0):+.3f}", flush=True)
        json.dump(rep, open("crt15_lab.json", "w"), indent=1)


if __name__ == "__main__":
    main()
