"""Check the new Vault FX 200 EMA engine on real candles (no orders): every pair, the last ~10 days."""
import json, os
import vault_fx as vf
import ema_tracker as et
api = vf.Oanda(os.environ["OANDA_API_TOKEN"].strip(), os.environ["OANDA_ACCOUNT_ID"].strip())
out, save = {}, {}
for pair in vf.PAIRS:
    rows = api.get(f"/instruments/{pair}/candles", granularity="M15", count=vf.EMA_CANDLES, price="MBA")["candles"]
    bars = [{"t": c["time"][:19], "o": float(c["mid"]["o"]), "h": float(c["mid"]["h"]), "l": float(c["mid"]["l"]),
             "c": float(c["mid"]["c"]), "sp": float(c["ask"]["c"]) - float(c["bid"]["c"])} for c in rows if c.get("complete")]
    after = bars[-200]["t"]
    cards, flat = vf.ema200_taps(pair, bars, after, 5)
    tr = [c for c in et.check_taps(pair, bars, after) if not c.get("flat")]
    ok = [c for c in cards if c["ok"]]
    out[pair] = {"bars": len(bars), "taps": len(cards), "flat": flat, "ok": [{k: c[k] for k in ("t", "dir", "entry", "stop", "tp", "rr", "entry_t")} for c in ok],
                 "tracker_taps": len(tr), "tracker_ok": sum(1 for c in tr if c["ok"]),
                 "no_votes": {}}
    for c in cards:
        for n, (o, _) in c["votes"].items():
            if o is False:
                out[pair]["no_votes"][n] = out[pair]["no_votes"].get(n, 0) + 1
    if ok:
        save[pair] = rows
    print(pair, len(bars), "taps", len(cards), "ok", len(ok), "| tracker", len(tr), sum(1 for c in tr if c["ok"]), out[pair]["no_votes"], flush=True)
json.dump(out, open("ema_check.json", "w"), indent=1)
json.dump(save, open("ema_candles.json", "w"))
