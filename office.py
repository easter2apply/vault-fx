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
          <p class="nums">{a.get('n', 0):,} trades · {_pc(a.get('win'))} wins · <b class="{'pos' if (a.get('avg') or 0) > 0 else 'neg'}">{_r(a.get('avg'))}</b> a trade</p>{extra}</article>""")
    board_html = ("".join(cards) if cards else
                  '<p class="empty">No case files yet. Run Inspector once: GitHub → Actions → Inspector → Run workflow.</p>')

    # ── proving ground
    rows = []
    for cid, case in CASES.items():
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

    # ── 3D scene data
    stamp = None
    if board:
        promo = [c for c in board if c["kind"] == "new" and c["verdict"] == "CLEARED"]
        pick = promo[0] if promo else next((c for c in board if c["id"] == "bot"), board[0])
        stamp = {"verdict": pick["verdict"], "name": pick["name"], "when": int(when or 0)}
    lines = []
    for c in board[:6]:
        a = c.get("all") or {}
        first = re.split(r"(?<=[.!?])\s", c["why"])[0].rstrip(".")
        lines.append(f"{c['name']}: {c['verdict'].lower()}. {first}.")
    lines += [n["text"] for n in list(reversed(st.get("hindsight", [])))[:6]]
    lines += ["Every strategy is guilty until proven profitable.", "Two stretches of history. It has to win in both.",
              "The spread is the quiet thief in every case.", f"Right now the bot trades with {trading}."]
    data = json.dumps({"cards": [{"name": c["name"], "verdict": c["verdict"]} for c in board[:9]], "stamp": stamp,
                       "lines": [l[:150] for l in lines], "strategy": trading})
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
.obub {{ position:absolute; max-width:min(210px,62%); padding:7px 10px; border-radius:12px; background:#f3ecdcf2; color:#1d1a14;
  font:600 12px/1.35 Georgia,serif; box-shadow:0 6px 18px #000a; transform:translateY(-100%); opacity:0; transition:opacity .25s }}
.obub.on {{ opacity:1 }} .obub b {{ display:block; font:800 9.5px/1 ui-monospace,Menlo,monospace; letter-spacing:.12em; color:#8a2b1f; margin-bottom:3px }}
.obub::after {{ content:""; position:absolute; bottom:-6px; left:var(--tx,50%); width:10px; height:10px; background:#f3ecdc; transform:translateX(-50%) rotate(45deg) }}
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

<section class="office" aria-label="Inspector's office"><div class="ostage" id="ostage"><div class="obubs" id="obubs"></div>
<div class="stampover" id="stampover"></div></div>
<p class="ohint">Inspector works his cases: reading files at the desk, checking the evidence board, and stamping verdicts. Drag to look around.</p></section>

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
  if (!window.THREE) { stage.insertAdjacentHTML('beforeend', '<p class="ofall">The 3D office needs a newer browser.</p>'); return; }
  var renderer;
  try { renderer = new THREE.WebGLRenderer({ antialias: true }); } catch (e) { return; }
  if (!renderer.getContext()) return;
  var still = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var small = (stage.clientWidth || 360) < 520;
  renderer.setPixelRatio(Math.min(small ? 1.75 : 2, window.devicePixelRatio || 1));
  renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.setClearColor(0x050407, 1);
  stage.insertBefore(renderer.domElement, stage.firstChild);
  var scene = new THREE.Scene(); scene.fog = new THREE.Fog(0x050407, 9, 20);
  var cam = new THREE.PerspectiveCamera(44, 1, 0.1, 60);
  var world = new THREE.Group(); scene.add(world);
  function M(g, m) { return new THREE.Mesh(g, m); }
  function put(p, o, x, y, z) { o.position.set(x || 0, y || 0, z || 0); p.add(o); return o; }
  function std(c, r, m) { return new THREE.MeshStandardMaterial({ color: c, roughness: r === undefined ? 0.8 : r, metalness: m || 0 }); }
  function at(o, x, y, z, p) { o.position.set(x, y, z); (p || world).add(o); return o; }
  function shadow(o, c, r) { o.traverse(function (k) { if (k.isMesh) { k.castShadow = c; k.receiveShadow = r; } }); return o; }
  function canvasTex(w, h, draw) { var c = document.createElement('canvas'); c.width = w; c.height = h; draw(c.getContext('2d'), w, h); var t = new THREE.CanvasTexture(c); t.anisotropy = 4; return t; }

  // ── lights: dim room, warm desk lamp, cold moonlight through the blinds, lightning
  var amb = new THREE.AmbientLight(0x3a3f58, 0.9); scene.add(amb);
  var key = new THREE.SpotLight(0xffd9a8, 1.25, 16, 0.5, 0.8, 1.2); key.position.set(2.2, 4.6, 4.5); key.target.position.set(0, 1.1, -0.8); scene.add(key); scene.add(key.target);
  scene.add(new THREE.HemisphereLight(0x3a4a70, 0x120d08, 0.35));
  var moon = new THREE.SpotLight(0x8fb4ff, 1.6, 18, 0.55, 0.6, 1.2);
  moon.position.set(-3.2, 4.4, -8.5); moon.target.position.set(0.3, 0.4, 0.4);
  moon.castShadow = true; moon.shadow.mapSize.set(small ? 1024 : 2048, small ? 1024 : 2048); moon.shadow.bias = -0.0005;
  scene.add(moon); scene.add(moon.target);
  var lamp = new THREE.PointLight(0xffc477, 2.2, 7, 2); lamp.position.set(-0.85, 1.55, 0.05); lamp.castShadow = true;
  lamp.shadow.mapSize.set(512, 512); scene.add(lamp);
  var flash = new THREE.AmbientLight(0xcfe0ff, 0); scene.add(flash);

  // ── room
  var floorTex = canvasTex(256, 256, function (x, w, h) {
    x.fillStyle = '#2a1d14'; x.fillRect(0, 0, w, h);
    for (var i = 0; i < 8; i++) { x.fillStyle = i % 2 ? '#2f2117' : '#251911'; x.fillRect(0, i * 32, w, 31); x.fillStyle = '#140d08'; x.fillRect((i * 77) % w, i * 32, 2, 32); }
  }); floorTex.wrapS = floorTex.wrapT = THREE.RepeatWrapping; floorTex.repeat.set(4, 4);
  var floor = M(new THREE.PlaneGeometry(12, 10), new THREE.MeshStandardMaterial({ map: floorTex, roughness: 0.75 }));
  floor.rotation.x = -Math.PI / 2; floor.receiveShadow = true; at(floor, 0, 0, 0);
  var wallTex = canvasTex(256, 256, function (x, w, h) {
    x.fillStyle = '#2b2a2e'; x.fillRect(0, 0, w, h);
    for (var i = 0; i < 16; i++) { x.fillStyle = i % 2 ? '#2f2e33' : '#29282c'; x.fillRect(i * 16, 0, 8, h); }
  }); wallTex.wrapS = wallTex.wrapT = THREE.RepeatWrapping; wallTex.repeat.set(3, 1);
  var WALL = new THREE.MeshStandardMaterial({ map: wallTex, roughness: 0.95 });
  var back = M(new THREE.PlaneGeometry(12, 5), WALL); back.receiveShadow = true; at(back, 0, 2.5, -4);
  var left = M(new THREE.PlaneGeometry(10, 5), WALL); left.rotation.y = Math.PI / 2; left.receiveShadow = true; at(left, -5, 2.5, 0);
  var right = M(new THREE.PlaneGeometry(10, 5), WALL); right.rotation.y = -Math.PI / 2; right.receiveShadow = true; at(right, 5, 2.5, 0);
  at(M(new THREE.BoxGeometry(12, 0.18, 0.06), std(0x1a120c)), 0, 0.09, -3.97);

  // ── window with rain, blinds and lightning (left wall)
  var rainC = document.createElement('canvas'); rainC.width = 256; rainC.height = 512;
  var rx = rainC.getContext('2d'), drops = [];
  for (var di = 0; di < 140; di++) drops.push([Math.random() * 256, Math.random() * 512, 6 + Math.random() * 16, 2 + Math.random() * 4]);
  var rainTex = new THREE.CanvasTexture(rainC);
  function drawRain(dt) {
    rx.fillStyle = 'rgba(12,20,38,1)'; rx.fillRect(0, 0, 256, 512);
    var g = rx.createLinearGradient(0, 0, 0, 512); g.addColorStop(0, 'rgba(60,80,130,0.55)'); g.addColorStop(1, 'rgba(15,20,40,0.2)');
    rx.fillStyle = g; rx.fillRect(0, 0, 256, 512);
    rx.fillStyle = 'rgba(255,214,140,0.5)'; for (var b = 0; b < 9; b++) rx.fillRect(20 + b * 27, 380 - (b * 37) % 90, 10, 14);  // far windows
    rx.strokeStyle = 'rgba(190,210,255,0.45)'; rx.lineWidth = 1.2; rx.beginPath();
    drops.forEach(function (d) { d[1] += d[3] * dt * 60; if (d[1] > 520) { d[1] = -20; d[0] = Math.random() * 256; } rx.moveTo(d[0], d[1]); rx.lineTo(d[0] - 1, d[1] + d[2]); });
    rx.stroke(); rainTex.needsUpdate = true;
  }
  drawRain(0);
  var WX = -2.35, WY = 2.45, WW = 1.9, WH = 1.9;            // window on the back wall, left of the evidence board
  var glass = M(new THREE.PlaneGeometry(WW, WH), new THREE.MeshBasicMaterial({ map: rainTex })); at(glass, WX, WY, -3.97);
  var FRAME = std(0x1b130d, 0.6);
  [-1, 1].forEach(function (sd) { at(M(new THREE.BoxGeometry(WW + 0.2, 0.08, 0.1), FRAME), WX, WY + sd * (WH / 2 + 0.02), -3.93); });
  [-1, 0, 1].forEach(function (sd) { at(M(new THREE.BoxGeometry(0.07, WH + 0.1, 0.1), FRAME), WX + sd * (WW / 2), WY, -3.93); });
  at(M(new THREE.BoxGeometry(WW + 0.35, 0.06, 0.25), FRAME), WX, WY - WH / 2 - 0.06, -3.85);
  var blinds = new THREE.Group(); at(blinds, WX, 0, -3.84);
  for (var bl = 0; bl < 13; bl++) { var sl = shadow(M(new THREE.BoxGeometry(WW, 0.05, 0.02), std(0xcab89a, 0.7)), true, false); sl.position.y = WY - WH / 2 + 0.12 + bl * 0.14; sl.rotation.x = 0.55; blinds.add(sl); }

  // ── desk, lamp, files, stamp, mug, phone
  var WOOD = std(0x4a2c18, 0.55), DARKWOOD = std(0x2c190d, 0.6);
  var desk = new THREE.Group(); at(desk, 0, 0, 0);
  put(desk, shadow(M(new THREE.BoxGeometry(2.6, 0.09, 1.25), WOOD), true, true), 0, 0.78, 0);
  [[-1.18, -0.52], [1.18, -0.52], [-1.18, 0.52], [1.18, 0.52]].forEach(function (p) { put(desk, shadow(M(new THREE.BoxGeometry(0.09, 0.76, 0.09), DARKWOOD), true, true), p[0], 0.38, p[1]); });
  put(desk, shadow(M(new THREE.BoxGeometry(0.75, 0.6, 1.1), DARKWOOD), true, true), 0.88, 0.42, 0);
  var lampG = new THREE.Group(); at(lampG, -0.85, 0.83, 0.05);
  lampG.add(shadow(M(new THREE.CylinderGeometry(0.12, 0.14, 0.04, 20), std(0xb08d3c, 0.3, 0.8)), true, false));
  put(lampG, M(new THREE.CylinderGeometry(0.015, 0.015, 0.55, 8), std(0xb08d3c, 0.3, 0.8)), 0, 0.28, 0);
  var shade = M(new THREE.CylinderGeometry(0.09, 0.26, 0.2, 20, 1, true), new THREE.MeshStandardMaterial({ color: 0x1f5e3a, roughness: 0.4, side: THREE.DoubleSide }));
  shade.position.set(0.05, 0.58, 0); shade.rotation.z = -0.25; lampG.add(shade);
  var bulb = M(new THREE.SphereGeometry(0.05, 12, 8), new THREE.MeshBasicMaterial({ color: 0xffe1a8 })); bulb.position.set(0.06, 0.52, 0); lampG.add(bulb);
  var FILE = std(0xc9a46a, 0.85), PAPER = std(0xefe6d2, 0.9);
  for (var fi = 0; fi < 5; fi++) { var f = shadow(M(new THREE.BoxGeometry(0.46, 0.03, 0.34), FILE), true, true); f.position.set(0.72, 0.84 + fi * 0.032, -0.15); f.rotation.y = (fi % 2 ? 0.08 : -0.05); world.add(f); }
  var openFile = new THREE.Group(); at(openFile, 0.05, 0.835, 0.15);
  openFile.add(shadow(M(new THREE.BoxGeometry(0.7, 0.012, 0.46), FILE), false, true));
  var page = M(new THREE.PlaneGeometry(0.3, 0.4), new THREE.MeshStandardMaterial({ map: canvasTex(128, 170, function (x, w, h) {
    x.fillStyle = '#f2ead8'; x.fillRect(0, 0, w, h); x.fillStyle = '#5b5245';
    for (var l = 0; l < 14; l++) x.fillRect(10, 16 + l * 10, 60 + (l * 37) % 48, 3);
  }), roughness: 0.9 }));
  page.rotation.x = -Math.PI / 2; page.position.set(0.17, 0.008, 0); openFile.add(page);
  var markMat = new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0 });
  var mark = M(new THREE.PlaneGeometry(0.26, 0.11), markMat); mark.rotation.x = -Math.PI / 2; mark.rotation.z = 0.2; mark.position.set(0.17, 0.011, 0.05); openFile.add(mark);
  var stampObj = new THREE.Group(); at(stampObj, 0.45, 0.83, 0.32);
  put(stampObj, shadow(M(new THREE.BoxGeometry(0.12, 0.04, 0.08), std(0x7a1d14, 0.5)), true, false), 0, 0.02, 0);
  put(stampObj, shadow(M(new THREE.CylinderGeometry(0.02, 0.025, 0.1, 10), DARKWOOD), true, false), 0, 0.09, 0);
  put(stampObj, shadow(M(new THREE.SphereGeometry(0.04, 12, 8), DARKWOOD), true, false), 0, 0.15, 0);
  var stampHome = stampObj.position.clone();
  var mug = shadow(M(new THREE.CylinderGeometry(0.06, 0.055, 0.13, 16), std(0xdedad0, 0.4)), true, false); at(mug, -0.45, 0.89, 0.38);
  var phone = shadow(M(new THREE.BoxGeometry(0.26, 0.08, 0.18), std(0x101010, 0.3)), true, false); at(phone, -0.4, 0.86, -0.32);
  var chair = new THREE.Group(); at(chair, 0, 0, -1.05);
  put(chair, shadow(M(new THREE.BoxGeometry(0.55, 0.07, 0.5), std(0x3b1f12, 0.5)), true, true), 0, 0.5, 0);
  put(chair, shadow(M(new THREE.BoxGeometry(0.55, 0.8, 0.07), std(0x3b1f12, 0.5)), true, true), 0, 0.92, -0.24);
  put(chair, M(new THREE.CylinderGeometry(0.04, 0.04, 0.48, 8), std(0x222222, 0.4, 0.6)), 0, 0.24, 0);

  // ── filing cabinet + coat rack
  var cab = new THREE.Group(); at(cab, -4.3, 0, -2.6); cab.rotation.y = Math.PI / 2;
  put(cab, shadow(M(new THREE.BoxGeometry(0.8, 1.5, 0.7), std(0x4d5a52, 0.5, 0.4)), true, true), 0, 0.75, 0);
  for (var dr = 0; dr < 3; dr++) { put(cab, M(new THREE.BoxGeometry(0.7, 0.42, 0.02), std(0x56645b, 0.5, 0.4)), 0, 0.28 + dr * 0.48, 0.36);
    put(cab, M(new THREE.BoxGeometry(0.16, 0.03, 0.03), std(0xb8b8b0, 0.3, 0.9)), 0, 0.36 + dr * 0.48, 0.38); }
  var rack = new THREE.Group(); at(rack, 3.9, 0, -3.3);
  put(rack, M(new THREE.CylinderGeometry(0.03, 0.03, 1.9, 8), DARKWOOD), 0, 0.95, 0);
  put(rack, M(new THREE.CylinderGeometry(0.25, 0.25, 0.04, 16), DARKWOOD), 0, 0.02, 0);
  var spareHat = new THREE.Group(); spareHat.position.set(0.08, 1.85, 0); rack.add(spareHat);

  // ── evidence board with case cards and red string
  var board = new THREE.Group(); at(board, 1.4, 2.35, -3.93);
  var corkTex = canvasTex(256, 160, function (x, w, h) { x.fillStyle = '#9b6d3f'; x.fillRect(0, 0, w, h);
    for (var k = 0; k < 900; k++) { x.fillStyle = Math.random() < 0.5 ? 'rgba(70,40,15,0.35)' : 'rgba(200,160,110,0.25)'; x.fillRect(Math.random() * w, Math.random() * h, 2, 2); } });
  board.add(shadow(M(new THREE.BoxGeometry(3.4, 2.0, 0.05), new THREE.MeshStandardMaterial({ map: corkTex, roughness: 1 })), false, true));
  put(board, M(new THREE.BoxGeometry(3.55, 2.15, 0.03), std(0x2c190d, 0.6)), 0, 0, -0.02);
  var colors = { 'CLEARED': '#127a49', 'GUILTY': '#b2283a', 'UNDER INVESTIGATION': '#9a6b00' };
  function cardTex(name, verdict) {
    return canvasTex(256, 176, function (x, w, h) {
      x.fillStyle = '#f2ead8'; x.fillRect(0, 0, w, h); x.fillStyle = '#c9b892'; x.fillRect(0, 0, w, 6);
      x.fillStyle = '#2a241c'; x.font = 'bold 22px Georgia'; x.textBaseline = 'top';
      var words = String(name).split(' '), line = '', y = 18;
      words.forEach(function (wd) { var t = line ? line + ' ' + wd : wd; if (x.measureText(t).width > 228 && line) { x.fillText(line, 14, y); y += 26; line = wd; } else line = t; });
      x.fillText(line, 14, y);
      x.save(); x.translate(w / 2, 128); x.rotate(-0.18); x.strokeStyle = colors[verdict] || '#555'; x.fillStyle = colors[verdict] || '#555';
      x.lineWidth = 4; x.font = 'bold 26px Georgia'; x.textAlign = 'center'; x.textBaseline = 'middle';
      var word = verdict === 'UNDER INVESTIGATION' ? 'PENDING' : verdict; var tw = x.measureText(word).width + 22;
      x.strokeRect(-tw / 2, -20, tw, 40); x.fillText(word, 0, 1); x.restore();
    });
  }
  var cards = [], cardsData = (D.cards && D.cards.length) ? D.cards : [{ name: 'Awaiting first case', verdict: 'UNDER INVESTIGATION' }];
  cardsData.slice(0, 9).forEach(function (c, i) {
    var col = i % 3, row = Math.floor(i / 3);
    var m = M(new THREE.PlaneGeometry(0.88, 0.6), new THREE.MeshStandardMaterial({ map: cardTex(c.name, c.verdict), roughness: 0.9 }));
    m.position.set(-1.08 + col * 1.08, 0.62 - row * 0.64, 0.04); m.rotation.z = ((i * 37) % 7 - 3) * 0.015; board.add(m);
    var pin = M(new THREE.SphereGeometry(0.035, 10, 8), std(0xc0392b, 0.3)); pin.position.set(m.position.x, m.position.y + 0.26, 0.07); board.add(pin);
    cards.push({ mesh: m, pin: pin, data: c });
  });
  var stringMat = new THREE.LineBasicMaterial({ color: 0xd2342a });
  for (var si = 0; si + 1 < cards.length; si++) {
    var a = cards[si].pin.position, b = cards[(si * 4 + 1) % cards.length].pin.position;
    if (a === b) continue;
    board.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([a.clone().setZ(0.075), b.clone().setZ(0.075)]), stringMat));
  }
  var boardLight = new THREE.SpotLight(0xffe6b8, 0.0, 6, 0.5, 0.7, 1.5); boardLight.position.set(1.4, 4.4, -2.2);
  boardLight.target = board; scene.add(boardLight);

  // ── Inspector: trench coat, fedora, magnifying glass
  var COAT = std(0x8a7350, 0.85), HAT = std(0x2b2722, 0.7), SKIN = std(0xc68b5e, 0.65), SHOE = std(0x15110d, 0.4), PANTS = std(0x2e2c30, 0.8);
  var insp = new THREE.Group(); world.add(insp); var u = {};
  function limb(x, y, len, r, mat, endMat, leg) {
    var piv = new THREE.Group(); piv.position.set(x, y, 0); insp.add(piv);
    var up = M(new THREE.CylinderGeometry(r * 1.05, r * 0.95, len / 2, 10), mat); up.position.y = -len / 4; piv.add(up);
    var j = new THREE.Group(); j.position.y = -len / 2; piv.add(j);
    j.add(M(new THREE.SphereGeometry(r, 10, 8), mat));
    var lo = M(new THREE.CylinderGeometry(r * 0.95, r * 0.85, len / 2, 10), mat); lo.position.y = -len / 4; j.add(lo);
    if (leg) { var sh = M(new THREE.BoxGeometry(r * 2.4, r * 1.2, r * 3.8), SHOE); sh.position.set(0, -len / 2 - r * 0.3, r * 0.9); j.add(sh); }
    else { var hd = M(new THREE.SphereGeometry(r * 1.1, 10, 8), endMat); hd.position.y = -len / 2 - r * 0.25; j.add(hd); }
    piv.userData.j = j; return piv;
  }
  u.legL = limb(-0.1, 0.62, 0.6, 0.065, PANTS, null, true); u.legR = limb(0.1, 0.62, 0.6, 0.065, PANTS, null, true);
  var coat = M(new THREE.CylinderGeometry(0.21, 0.33, 0.95, 18), COAT); coat.position.y = 0.92; insp.add(coat);
  var collar = M(new THREE.ConeGeometry(0.24, 0.16, 18, 1, true), COAT); collar.position.y = 1.37; collar.rotation.x = Math.PI; insp.add(collar);
  var belt = M(new THREE.CylinderGeometry(0.255, 0.255, 0.06, 18), std(0x5a4630, 0.7)); belt.position.y = 1.0; insp.add(belt);
  [0.88, 1.05, 1.2].forEach(function (y) { [-1, 1].forEach(function (sd) { var bt = M(new THREE.SphereGeometry(0.018, 8, 6), std(0x2a1f14)); bt.position.set(sd * 0.07, y, 0.25 - (y - 0.88) * 0.12); insp.add(bt); }); });
  u.armL = limb(-0.28, 1.3, 0.58, 0.06, COAT, SKIN); u.armR = limb(0.28, 1.3, 0.58, 0.06, COAT, SKIN);
  var head = new THREE.Group(); head.position.y = 1.62; insp.add(head); u.head = head;
  head.add(M(new THREE.SphereGeometry(0.21, 24, 18), SKIN));
  [-1, 1].forEach(function (sd) { var e = M(new THREE.SphereGeometry(0.026, 10, 8), new THREE.MeshBasicMaterial({ color: 0x111111 })); e.position.set(sd * 0.075, 0.02, 0.19); head.add(e);
    var br = M(new THREE.BoxGeometry(0.08, 0.02, 0.02), std(0x2a1f14)); br.position.set(sd * 0.075, 0.08, 0.195); br.rotation.z = sd * -0.15; head.add(br); });
  var stache = M(new THREE.BoxGeometry(0.15, 0.035, 0.035), std(0x3a2a1c)); stache.position.set(0, -0.055, 0.2); head.add(stache);
  var nose = M(new THREE.SphereGeometry(0.03, 8, 6), SKIN); nose.position.set(0, -0.01, 0.215); head.add(nose);
  var brim = M(new THREE.CylinderGeometry(0.34, 0.34, 0.02, 28), HAT); brim.position.y = 0.13; brim.rotation.x = 0.08; head.add(brim);
  var crown = M(new THREE.CylinderGeometry(0.17, 0.2, 0.17, 24), HAT); crown.position.y = 0.22; head.add(crown);
  var band = M(new THREE.CylinderGeometry(0.205, 0.205, 0.04, 24), std(0x6b1d14, 0.6)); band.position.y = 0.16; head.add(band);
  var dent = M(new THREE.BoxGeometry(0.05, 0.03, 0.3), HAT); dent.position.y = 0.305; head.add(dent);
  var mag = new THREE.Group(); u.armR.userData.j.add(mag); mag.position.set(0, -0.36, 0.02);
  put(mag, M(new THREE.CylinderGeometry(0.014, 0.014, 0.14, 8), std(0x3b2414, 0.5)), 0, -0.04, 0);
  var ring = M(new THREE.TorusGeometry(0.075, 0.012, 8, 24), std(0xc9a44a, 0.25, 0.9)); ring.position.y = -0.17; ring.rotation.y = Math.PI / 2; mag.add(ring);
  var lens = M(new THREE.CircleGeometry(0.07, 20), new THREE.MeshStandardMaterial({ color: 0xbfe3ff, transparent: true, opacity: 0.35, roughness: 0.05, metalness: 0.2, side: THREE.DoubleSide }));
  lens.position.y = -0.17; lens.rotation.y = Math.PI / 2; mag.add(lens);
  shadow(insp, true, false); insp.scale.setScalar(1.12);
  var DESK_SPOT = new THREE.Vector3(0, 0, -0.68), BOARD_SPOT = new THREE.Vector3(1.4, 0, -3.15), WINDOW_SPOT = new THREE.Vector3(-2.35, 0, -3.05);
  insp.position.copy(DESK_SPOT); var yaw = 0;

  // ── bubbles
  var V = new THREE.Vector3(), bub = null, lineIdx = 0;
  function say(text, dur) {
    if (bub) bub.el.remove();
    var el = document.createElement('div'); el.className = 'obub';
    var b = document.createElement('b'); b.textContent = 'INSPECTOR'; el.appendChild(b); el.appendChild(document.createTextNode(text));
    bubWrap.appendChild(el); requestAnimationFrame(function () { el.classList.add('on'); });
    bub = { el: el, until: performance.now() / 1000 + (dur || Math.min(6.5, 2.6 + text.length * 0.04)) };
  }
  function nextLine() { var L = D.lines || []; if (!L.length) return null; var l = L[lineIdx % L.length]; lineIdx++; return l; }
  function placeBubble(nowP) {
    if (!bub) return;
    if (nowP > bub.until) { var el = bub.el; el.classList.remove('on'); setTimeout(function () { el.remove(); }, 300); bub = null; return; }
    head.getWorldPosition(V); V.y += 0.45; V.project(cam);
    var W = stage.clientWidth, H = renderer.domElement.clientHeight, x = (V.x + 1) / 2 * W, y = (1 - V.y) / 2 * H;
    var bw = bub.el.offsetWidth || 160, bh = bub.el.offsetHeight || 40, left = Math.max(6, Math.min(W - bw - 6, x - bw / 2));
    bub.el.style.left = left + 'px'; bub.el.style.top = Math.max(bh + 6, y) + 'px';
    bub.el.style.setProperty('--tx', Math.max(12, Math.min(bw - 12, x - left)) + 'px');
  }

  // ── layout + drag
  function size() {
    var w = stage.clientWidth || 360, h = Math.round(Math.min(560, Math.max(330, w * 0.86)));
    renderer.setSize(w, h); cam.aspect = w / h;
    var narrow = w < 520; cam.position.set(narrow ? 0.1 : 0.2, narrow ? 2.6 : 2.5, narrow ? 6.7 : 6.0); cam.lookAt(-0.2, 1.45, -1.4);
    cam.updateProjectionMatrix();
  }
  size(); window.addEventListener('resize', size);
  var turn = 0, drag = null, canvas = renderer.domElement;
  canvas.addEventListener('pointerdown', function (e) { drag = { x: e.clientX, t: turn }; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener('pointermove', function (e) { if (drag) turn = Math.max(-0.5, Math.min(0.5, drag.t + (e.clientX - drag.x) * 0.004)); });
  canvas.addEventListener('pointerup', function () { drag = null; });

  // ── the routine: read files → walk to the board → study a card → walk back → (stamp a verdict) → window → repeat
  function clamp(x) { return Math.max(0, Math.min(1, x)); }
  function smooth(x) { x = clamp(x); return x * x * (3 - 2 * x); }
  var plan = [], step = 0, stepT = 0, studyCard = 0, shake = 0;
  var seenKey = 'inspSeen', seen = 0; try { seen = +(localStorage.getItem(seenKey) || 0); } catch (e) {}
  var stampFirst = D.stamp && D.stamp.when && D.stamp.when > seen;
  function buildPlan() {
    plan = [{ k: 'read', d: 5.5 }, { k: 'walk', to: BOARD_SPOT, d: 0 }, { k: 'study', d: 5.0 }, { k: 'walk', to: DESK_SPOT, d: 0 }];
    if (D.stamp) plan.push({ k: 'stamp', d: 4.2 });
    plan.push({ k: 'read', d: 4.5 }, { k: 'walk', to: WINDOW_SPOT, d: 0 }, { k: 'window', d: 4.5 }, { k: 'walk', to: DESK_SPOT, d: 0 });
    if (stampFirst) { plan.unshift({ k: 'stamp', d: 4.2 }); stampFirst = false; }
  }
  buildPlan();
  function begin(s, t) {
    s.t0 = t; s.from = insp.position.clone();
    if (s.k === 'walk') { s.d = Math.max(0.6, s.from.distanceTo(s.to) / 1.0); }
    if (s.k === 'study') { studyCard = (studyCard + 1) % cards.length; var c = cards[studyCard]; say(lineForCard(c.data)); }
    if (s.k === 'read' || s.k === 'window') { var l = nextLine(); if (l && Math.random() < 0.85) setTimeout(function () { say(l); }, 900); }
    if (s.k === 'stamp') { markMat.opacity = 0; s.slammed = false; }
  }
  function lineForCard(c) {
    var v = c.verdict === 'CLEARED' ? 'cleared. Made money in both stretches.' : c.verdict === 'GUILTY' ? 'guilty. Lost money over the test.' : 'still under investigation.';
    return c.name + ': ' + v;
  }
  begin(plan[0], 0);
  var t0 = performance.now(), lastT = 0, nextFlash = 6 + Math.random() * 10;
  function frame(now) {
    var t = (now - t0) / 1000, dt = Math.min(0.05, t - lastT); lastT = t;
    if (!still) drawRain(dt);
    var s = plan[step], k = still ? 0 : (t - s.t0) / s.d;
    if (!still && k >= 1) {
      if (s.k === 'walk') insp.position.copy(s.to);
      step = (step + 1) % plan.length; if (step === 0) buildPlan();
      s = plan[step]; begin(s, t); k = 0;
    }
    var pos = insp.position, moving = 0, faceTo = 0;
    if (s.k === 'walk' && !still) {
      var kk = smooth(k); pos.lerpVectors(s.from, s.to, kk); moving = Math.sin(Math.PI * clamp(k)) > 0.05 ? 1 : 0;
      var dx = s.to.x - s.from.x, dz = s.to.z - s.from.z; faceTo = Math.atan2(dx, dz);
    } else if (s.k === 'study') faceTo = Math.PI;
    else if (s.k === 'window') faceTo = Math.PI;
    else faceTo = 0;
    var dd = Math.atan2(Math.sin(faceTo - yaw), Math.cos(faceTo - yaw)); yaw += dd * 0.1; insp.rotation.y = yaw;
    var ph = t * 7.5, sw = Math.sin(ph) * 0.55 * moving;
    u.legL.rotation.x = sw; u.legR.rotation.x = -sw;
    u.legL.userData.j.rotation.x = Math.max(0, -Math.sin(ph)) * 0.8 * moving; u.legR.userData.j.rotation.x = Math.max(0, Math.sin(ph)) * 0.8 * moving;
    insp.position.y = Math.abs(Math.sin(ph)) * 0.025 * moving;
    var aR = -sw * 0.6, aRj = -0.2, aL = sw * 0.6, aLj = -0.2, hx = 0, hy = still ? 0 : Math.sin(t * 0.6) * 0.15;
    if (s.k === 'read') { aR = -0.9 + Math.sin(t * 2.2) * 0.12; aRj = -0.9; aL = -0.7; aLj = -0.8; hx = 0.35; hy = Math.sin(t * 0.9) * 0.12;
      page.rotation.z = Math.sin(t * 1.3) * 0.05; }
    if (s.k === 'study') { var lift = smooth(k / 0.15) * (1 - smooth((k - 0.85) / 0.15)); aR = -1.9 * lift; aRj = -0.6 * lift; hx = -0.1; hy = Math.sin(t * 1.2) * 0.1;
      cards.forEach(function (c, i) { c.mesh.scale.setScalar(i === studyCard ? 1 + 0.06 * lift : 1); });
      boardLight.intensity = 1.6 * lift; }
    else { boardLight.intensity *= 0.92; cards.forEach(function (c) { c.mesh.scale.setScalar(1); }); }
    if (s.k === 'window') { aL = -0.4; aLj = -1.6; hy = 0.2 * Math.sin(t * 0.5); }
    if (s.k === 'stamp') {
      var up = k < 0.35 ? smooth(k / 0.35) : k < 0.45 ? 1 - smooth((k - 0.35) / 0.1) : 0;
      aR = -1.0 - up * 1.4; aRj = -0.5; hx = 0.3;
      stampObj.position.set(stampHome.x - 0.3 * clamp(k / 0.2), stampHome.y + up * 0.55 + (k < 0.2 ? 0.1 * k / 0.2 : 0), stampHome.z - 0.12 * clamp(k / 0.2));
      if (k >= 0.45 && !s.slammed) {
        s.slammed = true; shake = 0.35;
        var cls = D.stamp.verdict === 'CLEARED' ? 'good' : D.stamp.verdict === 'GUILTY' ? 'bad' : 'wait';
        over.textContent = D.stamp.verdict === 'UNDER INVESTIGATION' ? 'PENDING' : D.stamp.verdict;
        over.className = 'stampover ' + cls; void over.offsetWidth; over.className = 'stampover go ' + cls;
        markMat.color.set(cls === 'good' ? 0x127a49 : cls === 'bad' ? 0xb2283a : 0x9a6b00); markMat.opacity = 0.85;
        say(D.stamp.name + ': ' + (D.stamp.verdict === 'CLEARED' ? 'cleared. Off to the proving ground.' : D.stamp.verdict === 'GUILTY' ? 'guilty as charged.' : 'case stays open.'), 3.6);
        try { localStorage.setItem(seenKey, String(D.stamp.when)); } catch (e) {}
      }
      if (k > 0.9) stampObj.position.lerp(stampHome, 0.2);
    }
    u.armR.rotation.x += (aR - u.armR.rotation.x) * 0.15; u.armR.userData.j.rotation.x += (aRj - u.armR.userData.j.rotation.x) * 0.15;
    u.armL.rotation.x += (aL - u.armL.rotation.x) * 0.15; u.armL.userData.j.rotation.x += (aLj - u.armL.userData.j.rotation.x) * 0.15;
    head.rotation.x += (hx - head.rotation.x) * 0.08; head.rotation.y += (hy - head.rotation.y) * 0.08;
    // lamp flicker, lightning
    lamp.intensity = still ? 2.2 : 2.1 + Math.sin(t * 23) * 0.04 + (Math.random() < 0.004 ? -0.9 : 0);
    if (!still && t > nextFlash) { nextFlash = t + 8 + Math.random() * 14; flash.userData.t = t; }
    var ft = flash.userData.t ? t - flash.userData.t : 99;
    flash.intensity = ft < 0.5 ? (ft < 0.08 || (ft > 0.18 && ft < 0.26) ? 1.6 : 0.2) : 0;
    // camera: gentle sway + drag + stamp shake
    world.rotation.y += (turn + (still ? 0 : Math.sin(t * 0.15) * 0.04) - world.rotation.y) * 0.06;
    if (shake > 0) { shake = Math.max(0, shake - dt); world.position.set((Math.random() - 0.5) * shake * 0.12, (Math.random() - 0.5) * shake * 0.08, 0); } else world.position.set(0, 0, 0);
    placeBubble(now / 1000);
    renderer.render(scene, cam);
    requestAnimationFrame(frame);
  }
  if (still) { say(nextLine() || 'Every strategy is guilty until proven profitable.', 999); }
  requestAnimationFrame(frame);
})();
"""
