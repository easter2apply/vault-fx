"""
Inspector's Office: his own tab on the dashboard (site/office.html), built on every dashboard deploy.

A 3D noir detective office (rain on the window, lamp light, evidence board with red string),
his case board, the proving ground (live paper trading of his cases), the hindsight desk, and odds boosters.
"""

import datetime as dt
import html
import json
import re
import time

TAB_CSS = """
.tabs { position:sticky; top:0; z-index:20; display:flex; gap:6px; padding:8px; margin:-20px -16px 0; background:#0b0916ee;
  backdrop-filter:blur(8px); -webkit-backdrop-filter:blur(8px); border-bottom:1px solid #2c2550 }
.tabs a { flex:1; text-align:center; text-decoration:none; color:#9a92bf; font:700 14px/1 ui-sans-serif,-apple-system,sans-serif;
  padding:11px 8px; border-radius:10px; border:1px solid transparent }
.tabs a.on { color:#fff; background:#221c40; border-color:#4a3a85 }
.tabs a:focus-visible { outline:2px solid #f4c84a; outline-offset:2px }
"""


def tabbar(active):
    return (f'<nav class="tabs" aria-label="Pages"><a href="index.html" class="{"on" if active == "war" else ""}"'
            f'{" aria-current=page" if active == "war" else ""}>⚔️ War Room</a>'
            f'<a href="office.html" class="{"on" if active == "office" else ""}"{" aria-current=page" if active == "office" else ""}>🕵️ Inspector\'s Office</a></nav>')


VERDICT_CLS = {"CLEARED": "good", "GUILTY": "bad", "UNDER INVESTIGATION": "wait"}
KIND = {"current": "Your bot", "article": "From the article", "new": "New case", "lab": "Lab"}
STATE_TXT = {"untested": "Waiting for Inspector's first report", "guilty": "Not eligible: guilty in the backtest",
             "watching": "Not eligible yet: mixed backtest", "proving": "In the proving ground", "ready": "Ready for promotion"}


def _r(x):
    return "—" if x is None else f"{x:+.2f}R"


def _pc(x):
    return "—" if x is None else f"{x:.0%}"


def _ago(ts):
    s = max(0, time.time() - ts)
    return "just now" if s < 90 else f"{s / 60:.0f} min ago" if s < 5400 else f"{s / 3600:.0f}h ago" if s < 172800 else f"{s / 86400:.0f}d ago"


def accuracy_texts(acc, tz):
    """(bubble text for Inspector, HTML for the pinned bubble) about accuracy since his first correction."""
    def pct(x):
        return "—" if x is None else f"{x:.0%}"
    if not acc:
        return None, "📊 Accuracy tracking starts after Inspector's first report."
    if acc.get("state") == "waiting":
        now = acc.get("now")
        base = (f"{pct(now['win'])} wins · {now['avg']:+.2f}R a trade" + (" (backtest)" if acc.get("source") == "backtest" else "")) if now else "no trades yet"
        return (f"Your bot right now: {base}. The day you /approve one of my cases, I'll track how much it improves.",
                f"📊 <b>Your bot now:</b> {base}<br><small>Tracking starts when you /approve one of Inspector's cases</small>")
    b, a = acc.get("before"), acc.get("after")
    since = dt.datetime.fromtimestamp(acc["since"], tz).strftime("%b %-d")
    if not a:
        return (f"My fix went in on {since}. Waiting for the first trades to close.",
                f"📈 <b>Inspector's fix since {since}</b><br><small>Waiting for the first trades to close</small>")
    dw = (a["win"] - b["win"]) * 100 if b else None
    head = (f"{pct(b['win'])} → {pct(a['win'])} wins ({dw:+.0f} pts)" if b else f"{pct(a['win'])} wins")
    avg = (f"{b['avg']:+.2f}R → {a['avg']:+.2f}R a trade" if b else f"{a['avg']:+.2f}R a trade")
    up = dw is None or dw >= 0
    return (f"Since my fix on {since}: {head}, {avg}, over {a['n']} trades.",
            f"{'📈' if up else '📉'} <b>Since Inspector's fix ({since})</b><br>{head}<br><small>{avg} · {a['n']} trades"
            + (" · before = backtest" if acc.get("before_source") == "backtest" else "") + "</small>")


def build_office(st, ctx):
    files, when, CASES = ctx["files"], ctx["when"], ctx["cases"]
    status, names, tz = ctx["status"], ctx["names"], ctx["tz"]
    board = (files.get("board") or {}).get("cases", [])
    strat = st.get("strategy")
    trading = CASES[strat]["name"] if strat in CASES else "the council"
    insp_when = dt.datetime.fromtimestamp(when, tz).strftime("%a %b %-d, %-I:%M %p ET") if when else None

    # ── case board
    cards = []
    for c in board:
        a = c.get("all") or {}
        extra = ""
        if c["kind"] == "new":
            cid = c["id"].split(":", 1)[1]
            state, d = status(st, cid)
            chip = STATE_TXT.get(state, state)
            if state == "proving":
                chip += f" · day {d['days']:.0f} of {ctx['proving_days']} · {d['n']} paper trades" + (f" · {d['avg']:+.2f}R" if d['avg'] is not None else "")
            elif state == "ready":
                chip += f" · send /approve {cid}"
            if strat == cid:
                chip = "⭐ Trading now (promoted)"
            extra = f'<p class="chip {state}">{html.escape(chip)}</p>'
        cards.append(f"""<article class="case {VERDICT_CLS[c['verdict']]}">
          <div class="ch"><span class="kind">{KIND.get(c['kind'], '')}</span><span class="stamp {VERDICT_CLS[c['verdict']]}">{c['verdict']}</span></div>
          <h3>{html.escape(c['name'])}</h3><p class="what">{html.escape(c['what'])}</p>
          <p class="why">{html.escape(c['why'])}</p>
          <p class="nums">{a.get('n', 0):,} trades · {_pc(a.get('win'))} wins · <b class="{'pos' if (a.get('avg') or 0) > 0 else 'neg'}">{_r(a.get('avg'))}</b> a trade</p>
          {('<p class="why"><b>🔧 What would help</b></p><ul class="help">' + "".join(f"<li>{html.escape(t)}</li>" for t in c.get("improve", [])) + "</ul>") if c.get("improve") else ""}{extra}</article>""")
    board_html = ("".join(cards) if cards else
                  '<p class="empty">No case files yet. Run Inspector once: GitHub → Actions → Inspector → Run workflow.</p>')

    # ── proving ground
    rows = []
    on_board = {c["id"] for c in board}
    for cid, case in CASES.items():
        if board and "case:" + cid not in on_board:
            continue                                   # not testable on this account (e.g. gold on a US account)
        P = (st.get("paper") or {}).get(cid, {})
        closed = P.get("closed", [])
        avg = sum(x["r"] for x in closed) / len(closed) if closed else None
        wins = sum(1 for x in closed if x["r"] > 0)
        opens = "".join(f'<span class="pos-chip {"buy" if p["dir"] == 1 else "sell"}">{names.get(pr, pr)} {"BUY" if p["dir"] == 1 else "SELL"} '
                        f'<b>{p.get("now_r", 0):+.1f}R</b></span>' for pr, p in (P.get("open") or {}).items()) or '<span class="muted">no open paper trades</span>'
        last = closed[-1] if closed else None
        state, d = status(st, cid)
        rows.append(f"""<div class="pg"><div class="pgh"><b>{html.escape(case['name'])}</b><span class="chip {state}">{html.escape(STATE_TXT.get(state, state))}</span></div>
          <div class="pgs"><span>{len(closed)} closed · {wins} won</span><span class="{'pos' if (avg or 0) > 0 else 'neg' if avg is not None else ''}">{_r(avg)} avg</span>
          {f'<span>last: {names.get(last["pair"], last["pair"])} {last["r"]:+.2f}R</span>' if last else ''}</div>
          <div class="opens">{opens}</div></div>""")

    # ── hindsight desk
    icon = {"vote": "⚖️", "close": "📕", "paper": "🧪", "verdict": "🔨", "trade": "💼", "note": "🕵️"}
    notes = list(reversed(st.get("hindsight", [])))[:18]
    notes_html = "".join(f'<li><span class="ic">{icon.get(n.get("kind"), "🕵️")}</span><div><p>{html.escape(n["text"])}</p>'
                         f'<small>{_ago(n["t"])}</small></div></li>' for n in notes) or \
        '<li><div><p class="muted">Inspector comments here when the council votes, a trade closes, or one of his cases opens or closes a paper trade.</p></div></li>'

    # ── odds boosters
    bo = files.get("boosters") or {}
    tips = "".join(f"<li>{html.escape(t)}</li>" for t in bo.get("tips", []))
    hours = bo.get("hours", [])
    mx = max([abs(h["avg"] or 0) for h in hours] + [0.01])
    hbars = "".join(f'<div class="hb"><span>{html.escape(h["label"].replace(" ET", ""))}</span><i class="{"pos" if (h["avg"] or 0) >= 0 else "neg"}" '
                    f'style="--w:{abs(h["avg"] or 0) / mx * 100:.0f}%"></i><b>{h["avg"]:+.2f}R</b></div>' for h in hours)

    # ── lineup data: who stands in the line (cleared first, then pending, the bot, then the worst offenders)
    order = {"CLEARED": 0, "UNDER INVESTIGATION": 1, "GUILTY": 2}
    picks = sorted(board, key=lambda c: (order[c["verdict"]], 0 if c["id"] == "bot" else 1, -((c.get("all") or {}).get("n") or 0)))[:8]
    suspects = []
    for i, c in enumerate(picks):
        a = c.get("all") or {}
        tip = next((t for t in c.get("improve", []) if not t.startswith("These ideas")), None)
        short = c["name"].replace(" (council trades)", "").replace(" (as written)", "").replace("Your bot today", "Your bot")
        suspects.append({"num": i + 1, "name": short, "verdict": c["verdict"], "avg": a.get("avg"), "n": a.get("n", 0),
                         "kind": c["kind"], "tip": tip})
    stamp = None
    if board:
        promo = [c for c in board if c["kind"] == "new" and c["verdict"] == "CLEARED"]
        pick = promo[0] if promo else next((c for c in board if c["id"] == "bot"), board[0])
        short = pick["name"].replace(" (council trades)", "").replace(" (as written)", "").replace("Your bot today", "Your bot")
        stamp = {"verdict": pick["verdict"], "name": short, "when": int(when or 0)}
    acc = ctx.get("accuracy") or {}
    acc_line, acc_html = accuracy_texts(acc, tz)
    data = json.dumps({"suspects": suspects, "stamp": stamp, "accLine": acc_line, "strategy": trading})
    scene = OFFICE_JS.replace("__DATA__", data.replace("</", "<\\/"))

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="300">
<title>Inspector's Office</title>
<style>
:root {{ --bg:#0b0a10; --panel:#15131c; --fg:#ece6da; --muted:#a39a8a; --line:#2c2733; --amber:#f4c84a;
  --good:#53f0a6; --bad:#ff6b7c; --wait:#f4c84a; color-scheme:dark }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; padding:20px 16px 40px }}
.wrap {{ max-width:1000px; margin:0 auto; display:grid; gap:18px }}
{TAB_CSS}
header h1 {{ margin:6px 0 0; font:800 24px/1.2 Georgia,"Times New Roman",serif; letter-spacing:.01em }}
header p {{ margin:4px 0 0; color:var(--muted) }}
.trading {{ display:inline-block; margin-top:8px; font:700 12px/1 ui-monospace,Menlo,monospace; letter-spacing:.06em; color:var(--amber);
  border:1px solid #6b5a2a; border-radius:99px; padding:6px 10px }}
section {{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:14px }}
h2 {{ margin:0 0 10px; font:700 12px/1.2 ui-monospace,Menlo,monospace; letter-spacing:.14em; text-transform:uppercase; color:var(--muted) }}
.office {{ padding:0; overflow:hidden; background:#07060a }}
.ostage {{ position:relative; touch-action:pan-y }} .ostage canvas {{ display:block; width:100% !important; cursor:grab }}
.ofall {{ padding:20px; color:var(--muted) }}
.stampover {{ position:absolute; left:50%; top:46%; transform:translate(-50%,-50%) rotate(-12deg) scale(2.4); opacity:0; pointer-events:none;
  font:900 34px/1 Georgia,serif; letter-spacing:.12em; padding:10px 18px; border:5px solid currentColor; border-radius:6px; background:#0008;
  text-shadow:0 0 18px currentColor; white-space:nowrap }}
.stampover.go {{ animation:stampin 2.6s cubic-bezier(.2,1.4,.4,1) forwards }}
.stampover.good {{ color:var(--good) }} .stampover.bad {{ color:var(--bad) }} .stampover.wait {{ color:var(--wait) }}
@keyframes stampin {{ 0% {{ opacity:0; transform:translate(-50%,-50%) rotate(-12deg) scale(2.4) }} 18% {{ opacity:1; transform:translate(-50%,-50%) rotate(-12deg) scale(1) }}
  80% {{ opacity:1 }} 100% {{ opacity:0; transform:translate(-50%,-50%) rotate(-12deg) scale(1) }} }}
.obubs {{ position:absolute; inset:0; pointer-events:none; overflow:hidden }}
.obub {{ position:absolute; max-width:min(240px,66%); padding:7px 10px; border-radius:12px; background:#f3ecdcf2; color:#1d1a14;
  font:600 12px/1.35 Georgia,serif; box-shadow:0 6px 18px #000a; transform:translateY(-100%); opacity:0; transition:opacity .25s }}
.obub.on {{ opacity:1 }} .obub b {{ display:block; font:800 9.5px/1 ui-monospace,Menlo,monospace; letter-spacing:.12em; color:#8a2b1f; margin-bottom:3px }}
.obub::after {{ content:""; position:absolute; bottom:-6px; left:var(--tx,50%); width:10px; height:10px; background:#f3ecdc; transform:translateX(-50%) rotate(45deg) }}
.accbub {{ position:absolute; left:10px; bottom:10px; max-width:min(270px,78%); background:#f3ecdcf2; color:#1d1a14; border-radius:12px;
  padding:8px 11px; font:600 12.5px/1.35 Georgia,serif; box-shadow:0 6px 18px #000a; pointer-events:none }}
.accbub small {{ color:#5b5245; font-family:ui-sans-serif,-apple-system,sans-serif; font-size:11px }}

.obs {{ position:absolute; right:10px; top:12px; font:700 10px/1 ui-monospace,Menlo,monospace; letter-spacing:.18em; color:#ffffff88; pointer-events:none }}
.ostage::after {{ content:""; position:absolute; inset:0; pointer-events:none; box-shadow:inset 0 0 60px #000c;
  background:linear-gradient(115deg, transparent 40%, #ffffff0a 48%, transparent 56%) }}
.legend {{ display:flex; flex-wrap:wrap; gap:6px; padding:10px 12px 0 }}
.lchip {{ font:600 12px/1.2 ui-sans-serif,-apple-system,sans-serif; color:var(--fg); background:#ffffff0d; border:1px solid var(--line);
  border-left:4px solid var(--wait); border-radius:8px; padding:6px 9px; cursor:pointer }}
.lchip.good {{ border-left-color:var(--good) }} .lchip.bad {{ border-left-color:var(--bad) }}
.lchip b {{ font-family:ui-monospace,Menlo,monospace }} .lchip:focus-visible {{ outline:2px solid var(--amber); outline-offset:2px }}
.case .help {{ margin:6px 0 0; padding-left:18px; font-size:13px }} .case .help li {{ margin:3px 0 }}
.ohint {{ margin:0; padding:8px 12px 12px; font-size:12px; color:var(--muted); text-align:center }}
.cases {{ display:grid; gap:10px; grid-template-columns:repeat(auto-fill,minmax(270px,1fr)) }}
.case {{ position:relative; background:#efe6d2; color:#26211a; border-radius:4px; padding:12px 12px 10px; box-shadow:0 2px 0 #0006, 0 10px 20px #0005;
  font-family:Georgia,"Times New Roman",serif }}
.case::before {{ content:""; position:absolute; top:-6px; left:50%; width:12px; height:12px; margin-left:-6px; border-radius:50%; background:#c0392b; box-shadow:0 2px 3px #0008 }}
.case h3 {{ margin:6px 0 4px; font-size:17px }} .case p {{ margin:4px 0 }}
.case .what {{ font-size:12.5px; color:#5b5245 }} .case .why {{ font-size:14px }} .case .nums {{ font:600 12px/1.4 ui-monospace,Menlo,monospace; color:#4a4237 }}
.case .pos {{ color:#127a49 }} .case .neg {{ color:#b2283a }}
.ch {{ display:flex; justify-content:space-between; align-items:center; gap:8px }}
.kind {{ font:700 10px/1 ui-monospace,Menlo,monospace; letter-spacing:.12em; text-transform:uppercase; color:#7a6f5f }}
.stamp {{ font:900 11px/1 Georgia,serif; letter-spacing:.12em; padding:4px 7px; border:2px solid currentColor; border-radius:3px; transform:rotate(-4deg) }}
.stamp.good {{ color:#127a49 }} .stamp.bad {{ color:#b2283a }} .stamp.wait {{ color:#9a6b00 }}
.chip {{ display:inline-block; font:700 11.5px/1.3 ui-sans-serif,-apple-system,sans-serif; padding:4px 8px; border-radius:99px; background:#0000000f }}
.chip.proving {{ background:#1f6f5c22; color:#127a49 }} .chip.ready {{ background:#f4c84a44; color:#6b4b00 }}
.pgwrap {{ display:grid; gap:10px }}
.pg {{ border:1px solid var(--line); border-radius:10px; padding:10px 12px }}
.pg .chip {{ background:#ffffff10; color:var(--muted) }} .pg .chip.proving {{ color:var(--good) }} .pg .chip.ready {{ color:var(--amber) }}
.pgh {{ display:flex; justify-content:space-between; gap:8px; align-items:center; flex-wrap:wrap }}
.pgs {{ display:flex; gap:14px; flex-wrap:wrap; margin:6px 0; color:var(--muted); font-variant-numeric:tabular-nums }}
.pos {{ color:var(--good) }} .neg {{ color:var(--bad) }} .muted {{ color:var(--muted) }}
.opens {{ display:flex; gap:6px; flex-wrap:wrap }}
.pos-chip {{ font:600 12px/1 ui-monospace,Menlo,monospace; padding:6px 8px; border-radius:6px; border:1px solid var(--line) }}
.pos-chip.buy {{ border-color:#53f0a655 }} .pos-chip.sell {{ border-color:#ff6b7c55 }}
ul.notes {{ list-style:none; margin:0; padding:0; display:grid; gap:10px }}
ul.notes li {{ display:grid; grid-template-columns:28px 1fr; gap:8px }} ul.notes p {{ margin:0 }} ul.notes small {{ color:var(--muted) }}
.ic {{ font-size:18px }}
ul.tips {{ margin:0 0 10px; padding-left:20px; display:grid; gap:6px }}
.hb {{ display:grid; grid-template-columns:96px 1fr 60px; gap:8px; align-items:center; font-size:12.5px; margin:3px 0 }}
.hb i {{ display:block; height:10px; border-radius:3px; width:var(--w); min-width:2px }} .hb i.pos {{ background:var(--good) }} .hb i.neg {{ background:var(--bad) }}
.hb b {{ text-align:right; font-variant-numeric:tabular-nums }} .hb span {{ color:var(--muted) }}
.cmds {{ font:12.5px/1.6 ui-monospace,Menlo,monospace; color:var(--muted) }} .cmds b {{ color:var(--fg) }}
.empty {{ color:var(--muted) }}
a {{ color:var(--amber) }}
</style></head><body><div class="wrap">
{tabbar("office")}
<header><h1>🕵️ Inspector's Office</h1>
<p>{f"Last inspection {insp_when}" if insp_when else "No inspection yet"} · he re-inspects every Sunday morning</p>
<span class="trading">TRADING NOW: {html.escape(trading.upper())}</span></header>

<section class="office" aria-label="The lineup"><div class="ostage" id="ostage"><div class="obubs" id="obubs"></div>
<div class="accbub">{acc_html}</div><div class="obs">OBSERVATION · ROOM 2</div>
<div class="stampover" id="stampover"></div></div>
<div class="legend" id="lineup-legend"></div>
<p class="ohint">The lineup: every strategy holds its average result per trade. Cleared ones step forward, guilty ones are cuffed.
Inspector walks the line and says what would help each one. Tap a name to send him over. Drag to look around.</p></section>

<section><h2>🗂 Case board</h2><div class="cases">{board_html}</div></section>

<section><h2>🧪 Proving ground (live paper trading)</h2>
<p class="muted">Every case paper-trades live, every 5 minutes, on today's prices. Only CLEARED cases count toward promotion:
{ctx['proving_days']} days and at least {ctx['proving_min']} closed paper trades averaging {ctx['proving_avg']:+.2f}R or better. Then Inspector texts you.</p>
<div class="pgwrap">{''.join(rows)}</div></section>

<section><h2>📝 Hindsight desk</h2><ul class="notes">{notes_html}</ul></section>

<section><h2>🎯 Odds boosters</h2>{f'<ul class="tips">{tips}</ul>' if tips else '<p class="empty">Shows up after Inspector\'s next run.</p>'}
{f'<h2 style="margin-top:12px">Average result by entry time</h2>{hbars}' if hbars else ''}</section>

<section><h2>📟 Telegram commands</h2><p class="cmds"><b>/cases</b> each case's status · <b>/approve case_id</b> promote a case ·
<b>/revert</b> back to the council · <b>/hindsight</b> Inspector's notes on or off</p>
<p><a href="inspector.html">Read Inspector's full report →</a></p></section>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>{scene}</script>
</body></html>"""


OFFICE_JS = r"""
(function () {
  var D = __DATA__;
  var stage = document.getElementById('ostage'), bubWrap = document.getElementById('obubs'), over = document.getElementById('stampover');
  var legend = document.getElementById('lineup-legend');
  if (!window.THREE) { stage.insertAdjacentHTML('beforeend', '<p class="ofall">The 3D lineup needs a newer browser.</p>'); return; }
  var renderer;
  try { renderer = new THREE.WebGLRenderer({ antialias: true }); } catch (e) { return; }
  if (!renderer.getContext()) return;
  var still = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var small = (stage.clientWidth || 360) < 520;
  renderer.setPixelRatio(Math.min(small ? 1.75 : 2, window.devicePixelRatio || 1));
  renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.setClearColor(0x0a0b0e, 1);
  stage.insertBefore(renderer.domElement, stage.firstChild);
  var scene = new THREE.Scene();
  var cam = new THREE.PerspectiveCamera(40, 1, 0.1, 60);
  var world = new THREE.Group(); scene.add(world);
  function M(g, m) { return new THREE.Mesh(g, m); }
  function put(p, o, x, y, z) { o.position.set(x || 0, y || 0, z || 0); p.add(o); return o; }
  function std(c, r, m) { return new THREE.MeshStandardMaterial({ color: c, roughness: r === undefined ? 0.8 : r, metalness: m || 0 }); }
  function shadow(o, c, r) { o.traverse(function (k) { if (k.isMesh) { k.castShadow = c; k.receiveShadow = r; } }); return o; }
  function canvasTex(w, h, draw) { var c = document.createElement('canvas'); c.width = w; c.height = h; draw(c.getContext('2d'), w, h); var t = new THREE.CanvasTexture(c); t.anisotropy = 4; return t; }
  var VC = { 'CLEARED': 0x53f0a6, 'GUILTY': 0xff5468, 'UNDER INVESTIGATION': 0xf4c84a };
  var VCSS = { 'CLEARED': '#2fbf7f', 'GUILTY': '#e2394f', 'UNDER INVESTIGATION': '#d9a520' };

  // ── room: height-chart wall, concrete floor, harsh overhead lights
  var S = (D.suspects || []).slice(0, small ? 6 : 8);
  if (!S.length) S = [{ num: 1, name: 'No suspects yet', verdict: 'UNDER INVESTIGATION', avg: null, n: 0, kind: 'new', tip: 'Run Inspector to fill the lineup.' }];
  var N = S.length, GAP = small ? 0.86 : 1.05, WIDTH = Math.max(4, (N - 1) * GAP + 2.2);
  scene.add(new THREE.AmbientLight(0x9aa6c0, 0.55));
  scene.add(new THREE.HemisphereLight(0xdfe8ff, 0x1a1a20, 0.45));
  var top = new THREE.SpotLight(0xf2f6ff, 1.5, 20, 0.9, 0.5, 1.0); top.position.set(0, 6, 3); top.target.position.set(0, 0.8, -1.3);
  top.castShadow = true; top.shadow.mapSize.set(small ? 1024 : 2048, small ? 1024 : 2048); top.shadow.bias = -0.0005; scene.add(top); scene.add(top.target);
  var wallTex = canvasTex(1024, 512, function (x, w, h) {
    x.fillStyle = '#c9cfd8'; x.fillRect(0, 0, w, h);
    for (var i = 0; i <= 10; i++) {
      var y = h - 40 - i * 42; x.fillStyle = i % 2 ? '#40454f' : '#20242c'; x.fillRect(0, y, w, i % 2 ? 2 : 4);
      if (i % 2 === 0) { x.font = 'bold 22px ui-monospace, Menlo, monospace'; x.fillText((4 + i / 2) + "'", 10, y - 6); x.fillText((4 + i / 2) + "'", w - 50, y - 6); }
    }
  });
  var wall = M(new THREE.PlaneGeometry(WIDTH + 2, 4.2), new THREE.MeshStandardMaterial({ map: wallTex, roughness: 0.95 }));
  wall.receiveShadow = true; put(world, wall, 0, 2.1, -1.9);
  var floor = M(new THREE.PlaneGeometry(WIDTH + 4, 7), std(0x3a3d44, 0.9)); floor.rotation.x = -Math.PI / 2; floor.receiveShadow = true; put(world, floor, 0, 0, 0.6);
  put(world, M(new THREE.BoxGeometry(WIDTH + 2, 0.12, 0.06), std(0x22252b)), 0, 0.06, -1.87);
  var stripe = M(new THREE.PlaneGeometry(WIDTH + 1, 0.06), new THREE.MeshBasicMaterial({ color: 0xf4c84a })); stripe.rotation.x = -Math.PI / 2; put(world, stripe, 0, 0.005, -0.25);

  // ── suspects
  var SKINS = [0xf1c27d, 0xc68642, 0x8d5524, 0xe0ac69, 0x5c3a21, 0xffdbac, 0xa86b45, 0x7a4a2a];
  var KINDC = { current: 0x2d4a7a, article: 0x7a5530, new: 0x4f5560, lab: 0x5b3f8a };
  var suspects = [];
  function placardTex(s) {
    return canvasTex(256, 168, function (x, w, h) {
      x.fillStyle = '#111317'; x.fillRect(0, 0, w, h); x.fillStyle = VCSS[s.verdict] || '#888'; x.fillRect(0, 0, w, 14);
      x.fillStyle = '#f2f2f2'; x.font = 'bold 40px ui-monospace, Menlo, monospace'; x.textAlign = 'center'; x.textBaseline = 'middle';
      x.fillText('#' + s.num, w / 2, 52);
      x.font = 'bold 44px ui-monospace, Menlo, monospace'; x.fillStyle = s.avg == null ? '#bbb' : s.avg > 0 ? '#53f0a6' : '#ff6b7c';
      x.fillText(s.avg == null ? '—' : (s.avg > 0 ? '+' : '−') + Math.abs(s.avg).toFixed(2) + 'R', w / 2, 116);
    });
  }
  function limb(g, x, y, len, r, mat, endMat, leg) {
    var piv = new THREE.Group(); piv.position.set(x, y, 0); g.add(piv);
    put(piv, M(new THREE.CylinderGeometry(r * 1.05, r * 0.95, len / 2, 10), mat), 0, -len / 4, 0);
    var j = new THREE.Group(); j.position.y = -len / 2; piv.add(j);
    j.add(M(new THREE.SphereGeometry(r, 10, 8), mat));
    put(j, M(new THREE.CylinderGeometry(r * 0.95, r * 0.85, len / 2, 10), mat), 0, -len / 4, 0);
    if (leg) put(j, M(new THREE.BoxGeometry(r * 2.4, r * 1.2, r * 3.8), std(0x15110d, 0.4)), 0, -len / 2 - r * 0.3, r * 0.9);
    else put(j, M(new THREE.SphereGeometry(r * 1.1, 10, 8), endMat), 0, -len / 2 - r * 0.25, 0);
    piv.userData.j = j; return piv;
  }
  S.forEach(function (s, i) {
    var g = new THREE.Group(), u = g.userData, x = (i - (N - 1) / 2) * GAP;
    var tall = 0.92 + ((i * 37) % 5) * 0.035;
    var BODY = std(KINDC[s.kind] || 0x4f5560, 0.75), SKIN = std(SKINS[i % SKINS.length], 0.65), PANTS = std(0x23262c, 0.8);
    u.legL = limb(g, -0.09, 0.6, 0.58, 0.06, PANTS, null, true); u.legR = limb(g, 0.09, 0.6, 0.58, 0.06, PANTS, null, true);
    put(g, M(new THREE.BoxGeometry(0.4, 0.56, 0.24), BODY), 0, 0.9, 0);
    u.armL = limb(g, -0.25, 1.15, 0.52, 0.05, BODY, SKIN); u.armR = limb(g, 0.25, 1.15, 0.52, 0.05, BODY, SKIN);
    var head = new THREE.Group(); put(g, head, 0, 1.42, 0); u.head = head;
    head.add(M(new THREE.SphereGeometry(0.2, 20, 14), SKIN));
    [-1, 1].forEach(function (sd) { put(head, M(new THREE.SphereGeometry(0.024, 8, 6), new THREE.MeshBasicMaterial({ color: 0x111111 })), sd * 0.07, 0.02, 0.185); });
    var mouth = put(head, M(new THREE.BoxGeometry(0.08, 0.016, 0.02), std(0x5a2a22)), 0, -0.08, 0.19); u.mouth = mouth;
    var hair = [function () { put(head, M(new THREE.SphereGeometry(0.205, 18, 10, 0, Math.PI * 2, 0, Math.PI / 2.2), std(0x2a1d14)), 0, 0.03, -0.01); },
                function () { put(head, M(new THREE.CylinderGeometry(0.21, 0.21, 0.12, 18), std(0x1d2a40)), 0, 0.17, 0); put(head, M(new THREE.BoxGeometry(0.24, 0.02, 0.14), std(0x1d2a40)), 0, 0.12, 0.17); },
                function () { put(head, M(new THREE.TorusGeometry(0.13, 0.05, 8, 18), std(0x6b6b6b)), 0, 0.12, 0).rotation.x = Math.PI / 2; },
                function () { put(head, M(new THREE.SphereGeometry(0.21, 18, 10, 0, Math.PI * 2, 0, Math.PI / 3), std(0x7a1d14)), 0, 0.05, 0); }][i % 4];
    hair();
    var plac = M(new THREE.PlaneGeometry(0.42, 0.28), new THREE.MeshBasicMaterial({ map: placardTex(s) }));
    put(g, plac, 0, 0.98, 0.15);
    var cuffs = new THREE.Group(); cuffs.visible = s.verdict === 'GUILTY';
    [-1, 1].forEach(function (sd) { var c = M(new THREE.TorusGeometry(0.045, 0.012, 6, 16), std(0xc0c4cc, 0.25, 0.9)); c.position.set(sd * 0.07, 0, 0); cuffs.add(c); });
    put(cuffs, M(new THREE.CylinderGeometry(0.006, 0.006, 0.08, 6), std(0xc0c4cc, 0.25, 0.9)), 0, 0, 0).rotation.z = Math.PI / 2;
    put(g, cuffs, 0, 0.74, 0.2);
    shadow(g, true, false);
    g.scale.setScalar(tall); g.position.set(x, 0, -1.3); world.add(g);
    var light = new THREE.PointLight(VC[s.verdict] || 0xffffff, 0, 2.6, 2); light.position.set(x, 2.3, -0.9); scene.add(light);
    var ring = M(new THREE.RingGeometry(0.26, 0.32, 32), new THREE.MeshBasicMaterial({ color: VC[s.verdict] || 0xffffff, transparent: true, opacity: 0.0 }));
    ring.rotation.x = -Math.PI / 2; put(world, ring, x, 0.01, -1.3);
    suspects.push({ g: g, u: u, s: s, x: x, light: light, ring: ring, homeZ: -1.3, forward: s.verdict === 'CLEARED' ? -0.45 : -1.3, phase: i * 1.7 });
  });

  // ── Inspector (trench coat, fedora, magnifying glass)
  var COAT = std(0x8a7350, 0.85), HAT = std(0x2b2722, 0.7), SKIN = std(0xc68b5e, 0.65), PANTS = std(0x2e2c30, 0.8);
  var insp = new THREE.Group(); world.add(insp); var iu = {};
  iu.legL = limb(insp, -0.1, 0.62, 0.6, 0.065, PANTS, null, true); iu.legR = limb(insp, 0.1, 0.62, 0.6, 0.065, PANTS, null, true);
  put(insp, M(new THREE.CylinderGeometry(0.21, 0.33, 0.95, 18), COAT), 0, 0.92, 0);
  var coll = put(insp, M(new THREE.ConeGeometry(0.24, 0.16, 18, 1, true), COAT), 0, 1.37, 0); coll.rotation.x = Math.PI;
  put(insp, M(new THREE.CylinderGeometry(0.255, 0.255, 0.06, 18), std(0x5a4630, 0.7)), 0, 1.0, 0);
  iu.armL = limb(insp, -0.28, 1.3, 0.58, 0.06, COAT, SKIN); iu.armR = limb(insp, 0.28, 1.3, 0.58, 0.06, COAT, SKIN);
  var ihead = new THREE.Group(); put(insp, ihead, 0, 1.62, 0);
  ihead.add(M(new THREE.SphereGeometry(0.21, 24, 18), SKIN));
  [-1, 1].forEach(function (sd) { put(ihead, M(new THREE.SphereGeometry(0.026, 10, 8), new THREE.MeshBasicMaterial({ color: 0x111111 })), sd * 0.075, 0.02, 0.19); });
  put(ihead, M(new THREE.BoxGeometry(0.15, 0.035, 0.035), std(0x3a2a1c)), 0, -0.055, 0.2);
  var brim = put(ihead, M(new THREE.CylinderGeometry(0.34, 0.34, 0.02, 28), HAT), 0, 0.13, 0); brim.rotation.x = 0.08;
  put(ihead, M(new THREE.CylinderGeometry(0.17, 0.2, 0.17, 24), HAT), 0, 0.22, 0);
  put(ihead, M(new THREE.CylinderGeometry(0.205, 0.205, 0.04, 24), std(0x6b1d14, 0.6)), 0, 0.16, 0);
  var mag = new THREE.Group(); put(iu.armR.userData.j, mag, 0, -0.36, 0.02);
  put(mag, M(new THREE.CylinderGeometry(0.014, 0.014, 0.14, 8), std(0x3b2414, 0.5)), 0, -0.04, 0);
  var mring = put(mag, M(new THREE.TorusGeometry(0.075, 0.012, 8, 24), std(0xc9a44a, 0.25, 0.9)), 0, -0.17, 0); mring.rotation.y = Math.PI / 2;
  shadow(insp, true, false);
  var FRONT_Z = 0.55;
  insp.position.set(suspects[0].x - 0.9, 0, FRONT_Z); var yaw = Math.PI / 2;

  // ── speech bubble
  var V = new THREE.Vector3(), bub = null;
  function say(text, who, dur) {
    if (bub) bub.el.remove();
    var el = document.createElement('div'); el.className = 'obub';
    var b = document.createElement('b'); b.textContent = who || 'INSPECTOR'; el.appendChild(b); el.appendChild(document.createTextNode(text));
    bubWrap.appendChild(el); requestAnimationFrame(function () { el.classList.add('on'); });
    bub = { el: el, until: performance.now() / 1000 + (dur || Math.min(8, 3 + text.length * 0.045)) };
  }
  function placeBubble(nowP) {
    if (!bub) return;
    if (nowP > bub.until) { var el = bub.el; el.classList.remove('on'); setTimeout(function () { el.remove(); }, 300); bub = null; return; }
    ihead.getWorldPosition(V); V.y += 0.5; V.project(cam);
    var W = stage.clientWidth, H = renderer.domElement.clientHeight, x = (V.x + 1) / 2 * W, y = (1 - V.y) / 2 * H;
    var bw = bub.el.offsetWidth || 160, bh = bub.el.offsetHeight || 40, left = Math.max(6, Math.min(W - bw - 6, x - bw / 2));
    bub.el.style.left = left + 'px'; bub.el.style.top = Math.max(bh + 6, y) + 'px';
    bub.el.style.setProperty('--tx', Math.max(12, Math.min(bw - 12, x - left)) + 'px');
  }
  function verdictWord(v) { return v === 'CLEARED' ? 'cleared' : v === 'GUILTY' ? 'guilty' : 'still under investigation'; }
  function lineFor(s) {
    var head = '#' + s.num + ' ' + s.name + ': ' + verdictWord(s.verdict) + (s.avg == null ? '.' : ' (' + (s.avg > 0 ? '+' : '') + s.avg.toFixed(2) + 'R a trade).');
    return s.tip ? head + ' What would help: ' + s.tip : head;
  }

  // ── legend chips under the stage (readable names), tap to send Inspector over
  var target = -1;
  if (legend) {
    legend.textContent = '';
    suspects.forEach(function (sp, i) {
      var bt = document.createElement('button'); bt.type = 'button'; bt.className = 'lchip ' + ({ 'CLEARED': 'good', 'GUILTY': 'bad' }[sp.s.verdict] || 'wait');
      var n = document.createElement('b'); n.textContent = '#' + sp.s.num; bt.appendChild(n);
      bt.appendChild(document.createTextNode(' ' + sp.s.name));
      bt.addEventListener('click', function () { target = i; });
      legend.appendChild(bt);
    });
  }

  // ── layout + drag
  function size() {
    var w = stage.clientWidth || 360, h = Math.round(Math.min(500, Math.max(300, w * 0.78)));
    renderer.setSize(w, h); cam.aspect = w / h;
    var halfW = ((N - 1) * GAP + 1.3) / 2, dist = halfW / (Math.tan(THREE.MathUtils.degToRad(20)) * cam.aspect);
    cam.position.set(0, 1.55, Math.max(4.6, Math.min(11, -1.1 + dist))); cam.lookAt(0, 1.0, -1.0); cam.updateProjectionMatrix();
  }
  size(); window.addEventListener('resize', size);
  var turn = 0, drag = null, canvas = renderer.domElement;
  canvas.addEventListener('pointerdown', function (e) { drag = { x: e.clientX, t: turn }; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener('pointermove', function (e) { if (drag) turn = Math.max(-0.45, Math.min(0.45, drag.t + (e.clientX - drag.x) * 0.004)); });
  canvas.addEventListener('pointerup', function () { drag = null; });

  // ── routine: lights up, cleared step forward, Inspector walks the line suspect by suspect
  function clamp(x) { return Math.max(0, Math.min(1, x)); }
  function smooth(x) { x = clamp(x); return x * x * (3 - 2 * x); }
  var seenKey = 'lineupSeen', seen = 0; try { seen = +(localStorage.getItem(seenKey) || 0); } catch (e) {}
  var feature = D.stamp && D.stamp.when > seen ? D.stamp : null;
  var cur = -1, mode = 'walk', modeT = 0, from = insp.position.clone(), to = from.clone(), WALK_SPEED = 1.1, stamped = false;
  function goTo(i, t) {
    cur = i; mode = 'walk'; modeT = t; from = insp.position.clone();
    to = new THREE.Vector3(suspects[i].x, 0, FRONT_Z);
  }
  var t0 = performance.now(), lastT = 0, accShown = false;
  goTo(0, 0);
  function frame(now) {
    var t = (now - t0) / 1000, dt = Math.min(0.05, t - lastT); lastT = t;
    // intro: verdict lights come up one by one, cleared suspects step forward
    suspects.forEach(function (sp, i) {
      var on = still ? 1 : smooth((t - 0.4 - i * 0.25) / 0.6);
      sp.light.intensity = 1.6 * on + (i === cur && mode === 'look' ? 1.2 : 0);
      sp.ring.material.opacity = 0.75 * on;
      var stepK = still ? 1 : smooth((t - 1.6 - i * 0.2) / 0.9);
      var z = sp.homeZ + (sp.forward - sp.homeZ) * stepK; sp.g.position.z = z; sp.ring.position.z = z;
      var walking = Math.abs(sp.forward - sp.homeZ) > 0.01 && stepK > 0 && stepK < 1;
      var ph = t * 7 + sp.phase, sw = walking ? Math.sin(ph) * 0.5 : 0;
      sp.u.legL.rotation.x = sw; sp.u.legR.rotation.x = -sw;
      // poses: hold the placard; guilty: cuffed, head down; cleared: relaxed bob; pending: shrug now and then
      var holdA = -0.55, holdJ = -1.1;
      if (sp.s.verdict === 'GUILTY') { holdA = -0.35; holdJ = -0.95; sp.u.head.rotation.x = 0.28 + Math.sin(t * 0.8 + sp.phase) * 0.04; sp.g.rotation.z = Math.sin(t * 0.9 + sp.phase) * 0.015; }
      else if (sp.s.verdict === 'CLEARED') { sp.u.head.rotation.x = -0.05; sp.g.position.y = still ? 0 : Math.abs(Math.sin(t * 1.6 + sp.phase)) * 0.02; if (sp.u.mouth) sp.u.mouth.scale.set(1.3, 1, 1); }
      else { var shrug = still ? 0 : Math.max(0, Math.sin(t * 0.7 + sp.phase) - 0.85) * 6.6; sp.u.head.rotation.z = shrug * 0.12; sp.g.position.y = shrug * 0.03; }
      sp.u.armL.rotation.x = holdA; sp.u.armR.rotation.x = holdA; sp.u.armL.rotation.z = 0.35; sp.u.armR.rotation.z = -0.35;
      sp.u.armL.userData.j.rotation.x = holdJ; sp.u.armR.userData.j.rotation.x = holdJ;
      if (i === cur && mode === 'look') sp.u.head.rotation.y = Math.sin(t * 2) * 0.1;
    });
    // Inspector
    if (!still) {
      if (target >= 0 && target !== cur) { goTo(target, t); target = -1; }
      if (mode === 'walk') {
        var d = from.distanceTo(to), k = d < 0.01 ? 1 : (t - modeT) * WALK_SPEED / d;
        insp.position.lerpVectors(from, to, smooth(k));
        if (k >= 1) {
          mode = 'look'; modeT = t; var s = suspects[cur].s;
          if (feature && !stamped && s.name === feature.name) {
            stamped = true; say(lineFor(s), 'INSPECTOR', 7);
            setTimeout(function () {
              var cls = feature.verdict === 'CLEARED' ? 'good' : feature.verdict === 'GUILTY' ? 'bad' : 'wait';
              over.textContent = feature.verdict === 'UNDER INVESTIGATION' ? 'PENDING' : feature.verdict;
              over.className = 'stampover ' + cls; void over.offsetWidth; over.className = 'stampover go ' + cls;
              try { localStorage.setItem(seenKey, String(feature.when)); } catch (e) {}
            }, 1800);
          } else say(lineFor(s));
        }
      } else if (mode === 'look' && t - modeT > Math.min(9, 3.5 + (bub ? 3 : 0))) {
        if (!accShown && D.accLine && cur === N - 1) { accShown = true; say(D.accLine, 'INSPECTOR', 7); modeT = t; }
        else goTo((cur + 1) % N, t);
      }
    }
    var moving = mode === 'walk' && !still ? 1 : 0, face = moving ? Math.atan2(to.x - from.x, to.z - from.z) : Math.PI;
    var dd = Math.atan2(Math.sin(face - yaw), Math.cos(face - yaw)); yaw += dd * 0.12; insp.rotation.y = yaw;
    var ph2 = t * 7.5, sw2 = Math.sin(ph2) * 0.55 * moving;
    iu.legL.rotation.x = sw2; iu.legR.rotation.x = -sw2; insp.position.y = Math.abs(Math.sin(ph2)) * 0.025 * moving;
    var lift = mode === 'look' ? smooth((t - modeT) / 0.5) : 0;
    iu.armR.rotation.x += ((-sw2 * 0.6 - 1.7 * lift) - iu.armR.rotation.x) * 0.15; iu.armR.userData.j.rotation.x += ((-0.2 - 0.5 * lift) - iu.armR.userData.j.rotation.x) * 0.15;
    iu.armL.rotation.x += ((sw2 * 0.6 - 0.3 * lift) - iu.armL.rotation.x) * 0.15;
    ihead.rotation.x = -0.15 * lift;
    world.rotation.y += (turn + (still ? 0 : Math.sin(t * 0.15) * 0.03) - world.rotation.y) * 0.06;
    placeBubble(now / 1000);
    renderer.render(scene, cam);
    requestAnimationFrame(frame);
  }
  if (still) { cur = 0; say(lineFor(suspects[0].s), 'INSPECTOR', 999); }
  requestAnimationFrame(frame);
})();
"""
