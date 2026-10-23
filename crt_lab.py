"""CRT Lab: Candle Range Theory on all 28 major pairs, last 12 months (OANDA candles, spread included, no peeking ahead).

CRT: candle R sets a range (high/low). The next candle N sweeps one side (trades past R's high or low)
and CLOSES BACK INSIDE R's range. Trade back the other way:
  swept the high -> SELL, swept the low -> BUY. Stop past N's wick. Target the other side of R's range.
Versions: range timeframe (4H / 1H / daily), entry (on N's close vs a 15-min break of structure after N),
key times (only 4H sweep candles starting 1/5/9 AM ET), daily-trend filter, minimum reward:risk.
Exits are walked on 15-min candles (stop first if both hit in one candle). Max hold = 3 range candles.
First 2/3 of the year = train, last 1/3 = unseen.
"""
import bisect
import json
import statistics as stt
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import zone_lab as zl

ET = ZoneInfo("America/New_York")
PAIRS = ["EUR_USD", "GBP_USD", "AUD_USD", "NZD_USD", "USD_JPY", "USD_CHF", "USD_CAD",
         "EUR_JPY", "GBP_JPY", "AUD_JPY", "NZD_JPY", "CAD_JPY", "CHF_JPY",
         "EUR_GBP", "EUR_AUD", "EUR_NZD", "EUR_CAD", "EUR_CHF",
         "GBP_AUD", "GBP_NZD", "GBP_CAD", "GBP_CHF",
         "AUD_NZD", "AUD_CAD", "AUD_CHF", "NZD_CAD", "NZD_CHF", "CAD_CHF"]
STEP = {"H4": 4 * 3600, "H1": 3600, "D": 86400}


def ts(t):
    return datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp()


def walk(m15, m15_ts, start_ts, d, entry, stop, tp, max_s):
    """From the first 15-min candle starting at/after start_ts. Returns (R before costs, exit ts)."""
    i = bisect.bisect_left(m15_ts, start_ts)
    risk = abs(entry - stop)
    end = start_ts + max_s
    last = None
    while i < len(m15) and m15_ts[i] < end:
        b = m15[i]
        if (b["l"] <= stop) if d == 1 else (b["h"] >= stop):
            return -1.0, m15_ts[i] + 900
        if (b["h"] >= tp) if d == 1 else (b["l"] <= tp):
            return abs(tp - entry) / risk, m15_ts[i] + 900
        last = b
        i += 1
    if last is None:
        return None, None
    return d * (last["c"] - entry) / risk, end


def signals(pair, R, m15, m15_ts, daily, v):
    """All CRT setups for one pair under version v."""
    step = STEP[v["tf"]]
    out = []
    dts = [ts(x["t"]) for x in daily]
    dcl = [x["c"] for x in daily]
    e50 = zl.ema(dcl, 50)
    for k in range(1, len(R) - 1):
        r, n = R[k - 1], R[k]
        n_start = ts(n["t"])
        if n_start - ts(r["t"]) > step * 3:          # weekend gap etc.: R isn't the candle right before N
            continue
        if v.get("keytimes"):
            h = datetime.fromtimestamp(n_start, ET).hour
            if h not in (1, 5, 9):
                continue
        up_sweep = n["h"] > r["h"] and r["l"] < n["c"] < r["h"]
        dn_sweep = n["l"] < r["l"] and r["l"] < n["c"] < r["h"]
        if up_sweep == dn_sweep:                     # neither, or both sides swept
            continue
        d = -1 if up_sweep else 1
        if v.get("trend"):
            j = bisect.bisect_right(dts, n_start - 86400) - 1   # last daily candle fully closed before N started
            if j < 50:
                continue
            tr = 1 if dcl[j] > e50[j] else -1
            if tr != d:
                continue
        n_end = n_start + step
        tp = r["l"] if d == -1 else r["h"]
        if v["entry"] == "close":
            entry, stop, t_in = n["c"], (n["h"] if d == -1 else n["l"]), n_end
        else:                                         # 15-min break of structure within the next range candle
            i = bisect.bisect_left(m15_ts, n_end)
            stop = n["h"] if d == -1 else n["l"]
            entry = t_in = None
            j0 = i
            while i < len(m15) and m15_ts[i] < n_end + step:
                b = m15[i]
                if (b["h"] >= stop) if d == -1 else (b["l"] <= stop):
                    break                              # swept further first: setup dead
                if i - j0 >= 3:
                    prior = m15[i - 3:i]
                    if d == -1 and b["c"] < min(x["l"] for x in prior):
                        entry, t_in = b["c"], m15_ts[i] + 900; break
                    if d == 1 and b["c"] > max(x["h"] for x in prior):
                        entry, t_in = b["c"], m15_ts[i] + 900; break
                i += 1
            if entry is None:
                continue
        i = bisect.bisect_left(m15_ts, t_in) - 1
        sp = m15[i]["sp"] if 0 <= i < len(m15) else 0
        stop = stop - d * sp                          # stop just past the wick, plus the spread
        risk = abs(entry - stop)
        if risk <= 0 or (tp - entry) * d <= 0:
            continue
        rr = abs(tp - entry) / risk
        if rr < v.get("min_rr", 0):
            continue
        r_mult, t_out = walk(m15, m15_ts, t_in, d, entry, stop, tp, step * 3)
        if r_mult is None:
            continue
        out.append({"pair": pair, "t": t_in, "t_out": t_out, "d": d, "r": r_mult - sp / risk, "rr": rr})
    return out


def one_per_pair(trades):
    """Like a live bot: only one trade per pair at a time."""
    taken, busy = [], {}
    for t in sorted(trades, key=lambda x: x["t"]):
        if t["t"] < busy.get(t["pair"], 0):
            continue
        taken.append(t)
        busy[t["pair"]] = t["t_out"]
    return taken


def stats(tr, split, weeks):
    def s(xs):
        if not xs:
            return {"n": 0}
        rs = [x["r"] for x in xs]
        sd = stt.pstdev(rs) if len(rs) > 1 else 0
        streak = worst = 0
        for r in rs:
            streak = streak + 1 if r < 0 else 0
            worst = max(worst, streak)
        return {"n": len(rs), "win": round(sum(r > 0 for r in rs) / len(rs), 3), "avg_r": round(stt.mean(rs), 3),
                "total_r": round(sum(rs), 1), "t": round(stt.mean(rs) / (sd / len(rs) ** 0.5), 2) if sd else 0,
                "avg_rr": round(stt.mean(x["rr"] for x in xs), 2), "worst_streak": worst}
    a = s(tr)
    a["per_week"] = round(len(tr) / weeks, 1)
    return {"all": a, "train": s([x for x in tr if x["t"] < split]), "unseen": s([x for x in tr if x["t"] >= split])}


VERSIONS = {
    "4H · enter on close": {"tf": "H4", "entry": "close"},
    "4H · enter on close · RR 1+": {"tf": "H4", "entry": "close", "min_rr": 1.0},
    "4H · 15m confirmation": {"tf": "H4", "entry": "bos"},
    "4H · 15m confirmation · RR 1+": {"tf": "H4", "entry": "bos", "min_rr": 1.0},
    "4H · key times 1/5/9 AM · close": {"tf": "H4", "entry": "close", "keytimes": True},
    "4H · key times · 15m confirmation": {"tf": "H4", "entry": "bos", "keytimes": True},
    "4H · with daily trend · close": {"tf": "H4", "entry": "close", "trend": True},
    "4H · with daily trend · 15m conf · RR 1+": {"tf": "H4", "entry": "bos", "trend": True, "min_rr": 1.0},
    "1H · enter on close": {"tf": "H1", "entry": "close"},
    "1H · enter on close · RR 1+": {"tf": "H1", "entry": "close", "min_rr": 1.0},
    "1H · 15m confirmation": {"tf": "H1", "entry": "bos"},
    "1H · with daily trend · close": {"tf": "H1", "entry": "close", "trend": True},
    "Daily · enter on close": {"tf": "D", "entry": "close"},
    "Daily · 15m confirmation": {"tf": "D", "entry": "bos"},
    "Daily · with daily trend · close": {"tf": "D", "entry": "close", "trend": True},
}


def main():
    now = datetime.now(timezone.utc)
    split = (now - timedelta(days=120)).timestamp()
    weeks = 52
    data = {}
    for p in PAIRS:
        try:
            m15 = zl.fetch(p, "M15")
            data[p] = {"M15": m15, "M15_ts": [ts(x["t"]) for x in m15],
                       "H4": zl.fetch(p, "H4"), "H1": zl.fetch(p, "H1"), "D": zl.fetch(p, "D")}
            print(p, len(m15), len(data[p]["H4"]), len(data[p]["H1"]), len(data[p]["D"]), flush=True)
        except Exception as e:
            print(p, "skipped", e, flush=True)
    report = {"versions": {}, "by_pair": {}}
    for name, v in VERSIONS.items():
        tr = []
        for p, D in data.items():
            tr += signals(p, D[v["tf"]], D["M15"], D["M15_ts"], D["D"], v)
        tr = one_per_pair(tr)
        st = stats(tr, split, weeks)
        report["versions"][name] = st
        report["by_pair"][name] = {p: stats([x for x in tr if x["pair"] == p], split, weeks)["all"] for p in data}
        a, u = st["all"], st["unseen"]
        print(f"{name:42} n={a.get('n', 0):5} /wk={a.get('per_week', 0):5} win={a.get('win', 0):.0%} avg={a.get('avg_r', 0):+.3f}R "
              f"tot={a.get('total_r', 0):+7.1f} t={a.get('t', 0)} rr={a.get('avg_rr', 0)} streak={a.get('worst_streak', 0)} "
              f"| unseen avg={u.get('avg_r', 0):+.3f} n={u.get('n', 0)}", flush=True)
        json.dump(report, open("crt_lab.json", "w"), indent=1)


if __name__ == "__main__":
    main()
