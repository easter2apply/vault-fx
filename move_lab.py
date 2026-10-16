"""Which currency pairs move the most for what they cost to trade? (OANDA practice account, last ~6 months)"""
import json, os, statistics as stt
import vault_fx as vf

api = vf.Oanda(os.environ["OANDA_API_TOKEN"].strip(), os.environ["OANDA_ACCOUNT_ID"].strip())
inst = api.instruments()
pairs = sorted(n for n, i in inst.items() if i.get("type") == "CURRENCY")
rows = []
for p in pairs:
    try:
        d = api.get(f"/instruments/{p}/candles", granularity="D", count=130, price="M")["candles"]
        h = api.get(f"/instruments/{p}/candles", granularity="H1", count=720, price="BA")["candles"]
    except Exception as e:
        print(p, "skipped", e); continue
    d = [c for c in d if c.get("complete")]
    h = [c for c in h if c.get("complete")]
    if len(d) < 60 or len(h) < 200:
        continue
    pip = 10 ** int(inst[p].get("pipLocation", -4))
    rng = [(float(c["mid"]["h"]) - float(c["mid"]["l"])) for c in d]
    pct = [(float(c["mid"]["h"]) - float(c["mid"]["l"])) / float(c["mid"]["o"]) * 100 for c in d]
    spr = [float(c["ask"]["c"]) - float(c["bid"]["c"]) for c in h]
    sp = stt.median(spr)
    rows.append({"pair": p, "day_range_pips": round(stt.mean(rng) / pip, 1), "day_range_pct": round(stt.mean(pct), 3),
                 "spread_pips": round(sp / pip, 2), "moves_per_spread": round(stt.mean(rng) / sp, 1)})
rows.sort(key=lambda r: -r["moves_per_spread"])
for r in rows:
    print(f"{r['pair']:8} range {r['day_range_pct']:.2f}%/day {r['day_range_pips']:7.1f} pips  spread {r['spread_pips']:5.2f}  x{r['moves_per_spread']}")
json.dump(rows, open("move_lab.json", "w"), indent=1)
