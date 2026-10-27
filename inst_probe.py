import json, os
import vault_fx as vf
api = vf.Oanda(os.environ["OANDA_API_TOKEN"].strip(), os.environ["OANDA_ACCOUNT_ID"].strip())
inst = api.instruments()
types = {}
for n, i in inst.items():
    types.setdefault(i.get("type"), []).append(n)
out = {t: sorted(v) for t, v in types.items() if t != "CURRENCY"}
out["count"] = {t: len(v) for t, v in types.items()}
print(json.dumps(out, indent=1))
json.dump(out, open("inst_probe.json", "w"), indent=1)
