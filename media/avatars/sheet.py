"""Self-contained contact sheet for every generated avatar (media/assets/avatars/index.html)."""
from __future__ import annotations

import base64
import html
import io
import json
from pathlib import Path

from PIL import Image

FRAMES = ("base", "mid", "wide", "blink", "mid_blink", "wide_blink")
KEY = {"base": "closed", "mid": "mid", "wide": "wide", "blink": "blink", "mid_blink": "mid_blink", "wide_blink": "wide_blink"}


def _uri(path: Path, style: str) -> str:
    if style == "pixel" or path.suffix != ".png":
        return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
    buf = io.BytesIO()  # painted: embed as WebP to keep the page light
    Image.open(path).convert("RGB").save(buf, "WEBP", quality=85, method=6)
    return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()


def _ink(hexs: str | None) -> str:
    try:
        r, g, b = (int(hexs[i : i + 2], 16) for i in (1, 3, 5))
    except (TypeError, ValueError):
        return "#EFF0F5"
    return "#0E1B4D" if 0.2126 * r + 0.7152 * g + 0.0722 * b > 150 else "#EFF0F5"


def _order(team: str) -> tuple:
    return (0 if team == "host" else 2 if team == "autodraft" else 1, team)


def _card(m: dict, root: Path) -> str:
    team, style = m["team"], m["style"]
    folder = root / team
    stage = []
    for f in FRAMES:
        p = folder / m["files"].get(f, f"{f}.png")
        if p.exists():
            on = ' class="on"' if f == "base" else ""
            stage.append(f'<img data-frame="{KEY[f]}"{on} src="{_uri(p, style)}" alt="" width="768" height="768" decoding="sync">')
    pose_thumbs = []
    for pose, pm in (m.get("poses") or {}).items():
        for kind_, rel in (pm.get("files") or {}).items():
            p = folder / rel
            if p.exists():
                key = f"pose_{pose}" + ("_closed" if kind_ == "closed" else "")
                stage.append(f'<img data-frame="{key}" src="{_uri(p, style)}" alt="" width="768" height="768" decoding="sync">')
                if kind_ == "open":
                    pose_thumbs.append(f'<figure><img data-copy="{team}:{key}" alt="{pose}"><figcaption>{pose.capitalize()}</figcaption></figure>')
    thumbs = []
    for f, label in (("base", "Closed"), ("mid", "Slightly open"), ("wide", "Wide open"), ("blink", "Blink")):
        thumbs.append(f'<figure><img data-copy="{team}:{KEY[f]}" alt="{label}"><figcaption>{label}</figcaption></figure>')
    masks = folder / "raw" / f"{style}_masks.png"
    if masks.exists():
        thumbs.append(f'<figure><img src="{_uri(masks, style)}" alt="Lock regions"><figcaption>Lock regions</figcaption></figure>')
    rows = []
    for v, label in (("mid", "Slightly open"), ("wide", "Wide open"), ("blink", "Blink")):
        x = (m.get("metrics") or {}).get(v, {})
        attempts = len((m.get("attempts") or {}).get(v, []))
        rows.append(
            f"<tr><td>{label}</td><td class=n>{x.get('outside_pct', 0):.2f}%</td><td class=n>{x.get('dropped_pct', 0):.2f}%</td>"
            f"<td class=n>{x.get('mask_pct', 0):.2f}%</td><td class=n>{attempts}</td><td>{'check' if x.get('bad') else 'ok'}</td></tr>"
        )
    flags = "".join(f"<li>{html.escape(f)}</li>" for f in (m.get("flags") or []) + (m.get("poses_flags") or []))
    celebration = m.get("poses_celebration")
    primary, secondary = m.get("primary_color") or "#0E1B4D", m.get("secondary_color") or "#E32402"
    notes = len(m.get("flags") or []) + len(m.get("poses_flags") or [])
    qa = "QA ok" if not notes else f"{notes} QA note{'s' if notes > 1 else ''}"
    px = " px" if style == "pixel" else ""
    return f"""
<article class="gm" style="--p:{primary};--s:{secondary};--ink:{_ink(secondary)}" data-team="{team}">
  <div class="tile {style}">
    <div class="stage{px}" data-avatar="{team}" role="img" aria-label="{html.escape(m.get('gm_name', team))} avatar">
      {''.join(stage)}
    </div>
    <div class="plate"><span class="tab"></span><span class="nm">{html.escape(m.get('gm_name', team))}</span><span class="live"><i></i></span></div>
  </div>
  <div class="who">
    <div class="fr">{html.escape(m.get('franchise_name') or '')}</div>
    <div class="meta">{html.escape(team)} · {html.escape(m.get('face_type', ''))} · {html.escape(m.get('personas_run') or '')} · ${m.get('cost_usd', 0) + (m.get('poses_cost') or 0):.2f} · {qa}</div>
  </div>
  <div class="row"><button class="talk" type="button" data-talk="{team}" aria-pressed="false"><span class="ico" aria-hidden="true"></span><span class="lbl">Talk</span></button>{'<button class="talk hype" type="button" data-hype="' + team + '" aria-pressed="false"><span class="lbl">Hype mode</span></button>' if pose_thumbs else ''}</div>
  <details>
    <summary>Frames and QA</summary>
    <div class="thumbs{px}">{''.join(thumbs)}</div>
    {f'<div class="thumbs four{px}">{"".join(pose_thumbs)}</div>' if pose_thumbs else ''}
    {f'<p class="cel">Celebration: {html.escape(celebration)}</p>' if celebration else ''}
    <table><thead><tr><th>Edit</th><th class=n>Outside</th><th class=n>Dropped</th><th class=n>Region</th><th class=n>Tries</th><th>QA</th></tr></thead>
    <tbody>{''.join(rows)}</tbody></table>
    {f'<ul class="flags">{flags}</ul>' if flags else ''}
  </details>
</article>"""


def build(root: Path) -> Path:
    metas = []
    for p in root.glob("*/meta.json"):
        m = json.loads(p.read_text())
        if m.get("files"):
            metas.append(m)
    metas.sort(key=lambda m: _order(m["team"]))
    total = sum(m.get("cost_usd", 0) + (m.get("poses_cost") or 0) for m in metas)
    styles = sorted({m["style"] for m in metas}) or ["-"]
    cards = "\n".join(_card(m, root) for m in metas)
    page = TEMPLATE.replace("%%CARDS%%", cards).replace("%%COUNT%%", str(len(metas))).replace(
        "%%STYLES%%", " + ".join(styles)).replace("%%TOTAL%%", f"${total:.2f}")
    out = root / "index.html"
    out.write_text(page)
    return out


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GM avatar sheet</title>
<meta name="description" content="AI GM League: every GM avatar with lip-flap and blink previews.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;700;800&family=Questrial&display=swap" rel="stylesheet">
<style>
:root{--navy:#0E1B4D;--red:#E32402;--royal:#4770DB;--off:#EFF0F5;--bg:#EFF0F5;--surface:#fff;--text:#0E1B4D;--text-2:#4B557E;--line:#D6DAE8;--chip:#E4E7F2;--ring:rgba(14,27,77,0);color-scheme:light}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#080F2E;--surface:#0F1A45;--text:#EFF0F5;--text-2:#AEB5D3;--line:#24306A;--chip:#18255C;--ring:rgba(239,240,245,.16);color-scheme:dark}}
:root[data-theme="dark"]{--bg:#080F2E;--surface:#0F1A45;--text:#EFF0F5;--text-2:#AEB5D3;--line:#24306A;--chip:#18255C;--ring:rgba(239,240,245,.16);color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 Questrial,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:36px 16px 64px}
.over{font:700 13px Archivo,system-ui,sans-serif;color:var(--red);margin:0 0 8px}
h1{font:800 clamp(28px,4vw,38px)/1.1 Archivo,system-ui,sans-serif;margin:0}
.lede{color:var(--text-2);margin:10px 0 0;max-width:760px}
.bar{display:flex;gap:10px;flex-wrap:wrap;margin:22px 0 22px}
.btn{border:0;border-radius:999px;cursor:pointer;font:700 14px Archivo,system-ui,sans-serif;padding:10px 16px;min-height:42px;background:var(--red);color:#fff}
.btn.ghost{background:transparent;color:var(--text);box-shadow:inset 0 0 0 2px var(--line)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,270px),1fr));gap:20px}
.gm{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:14px;display:flex;flex-direction:column;gap:10px;min-width:0}
.tile{background:var(--p);padding:10px;border-radius:10px;box-shadow:0 10px 24px rgba(14,27,77,.28),0 0 0 1px var(--ring),inset 0 0 0 1px rgba(255,255,255,.08)}
.stage{position:relative;aspect-ratio:1/1;width:100%;overflow:hidden;background:#0b1438;border:2px solid var(--s);border-radius:3px}
.stage img{position:absolute;inset:0;width:100%;height:100%;display:block;opacity:0}
.stage img.on{opacity:1}
.px img{image-rendering:pixelated}
.plate{display:flex;align-items:stretch;margin-top:8px;height:36px}
.plate .tab{width:9px;background:var(--s)}
.plate .nm{flex:1;display:flex;align-items:center;padding:0 10px;background:rgba(0,0,0,.35);color:#EFF0F5;font:800 16px/1.1 Archivo,system-ui,sans-serif;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.plate .live{display:flex;align-items:center;padding:0 10px;background:rgba(0,0,0,.45)}
.plate .live i{width:8px;height:8px;border-radius:50%;background:rgba(255,255,255,.2)}
.gm.talking .plate .live i{background:var(--red);box-shadow:0 0 0 3px rgba(227,36,2,.28)}
.who .fr{font:700 14px Archivo,system-ui,sans-serif}
.who .meta{font-size:12.5px;color:var(--text-2)}
.row{display:flex;gap:8px}
.talk{display:inline-flex;align-items:center;gap:8px;border:0;border-radius:999px;cursor:pointer;font:700 14px Archivo,system-ui,sans-serif;padding:8px 14px;min-height:40px;background:transparent;color:var(--text);box-shadow:inset 0 0 0 2px var(--line)}
.talk[aria-pressed="true"]{background:var(--text);color:var(--surface);box-shadow:none}
.talk .ico{width:0;height:0;border-left:9px solid currentColor;border-top:5px solid transparent;border-bottom:5px solid transparent}
.talk[aria-pressed="true"] .ico{width:9px;height:11px;border:0;border-left:3px solid currentColor;border-right:3px solid currentColor}
.talk:focus-visible,.btn:focus-visible,summary:focus-visible{outline:3px solid var(--royal);outline-offset:2px}
details{border-top:1px solid var(--line);padding-top:8px}
summary{cursor:pointer;font:700 13px Archivo,system-ui,sans-serif;color:var(--text-2)}
.thumbs{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:6px;margin-top:8px}
.thumbs figure{margin:0;min-width:0}
.thumbs img{width:100%;aspect-ratio:1;display:block;border-radius:5px;border:1px solid var(--line);background:#0b1438}
.thumbs.px img{image-rendering:pixelated}
.thumbs.four{grid-template-columns:repeat(4,minmax(0,1fr))}
.cel{font-size:12.5px;color:var(--text-2);margin:8px 0 0}
.talk.hype{background:var(--red);color:#fff;box-shadow:none}
.talk.hype[aria-pressed="true"]{background:var(--text);color:var(--surface)}
.thumbs figcaption{font-size:11px;color:var(--text-2);margin-top:2px;line-height:1.2}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
th,td{text-align:left;padding:4px 5px;border-bottom:1px solid var(--line)}
th{font:700 11.5px Archivo,system-ui,sans-serif;color:var(--text-2)}
.n{text-align:right;font-variant-numeric:tabular-nums}
.flags{margin:8px 0 0;padding-left:18px;font-size:12.5px;color:var(--text-2)}
.note{margin-top:28px;font-size:13px;color:var(--text-2)}
</style>
</head>
<body>
<main class="wrap">
  <p class="over">AI GM League · avatars</p>
  <h1>Contact sheet</h1>
  <p class="lede">%%COUNT%% characters · style: %%STYLES%% · image spend %%TOTAL%%. Every frame is an AI edit of that character's base portrait. Talk runs a speech-like 90–140&nbsp;ms mouth rhythm on face-locked frames (only the mouth or eyes change); blinks can land mid-word. Hype mode cuts between gesture poses mid-sentence (hype, point, shock, signature celebration), with lip-flap inside the talking poses.</p>
  <div class="bar"><button class="btn" type="button" id="hypeall">Hype all</button><button class="btn ghost" type="button" id="all">Talk all</button><button class="btn ghost" type="button" id="none">Stop all</button></div>
  <section class="grid">
%%CARDS%%
  </section>
  <p class="note">QA columns: “Outside” = pixels that changed outside the lock region (thrown away by the lock), “Dropped” = real change blobs outside it (e.g. brows), “Region” = size of the lock region. All are percentages of the frame.</p>
</main>
<script>
(() => {
  const rand = (a, b) => a + Math.random() * (b - a);
  class Avatar {
    constructor(stage) {
      this.team = stage.dataset.avatar;
      this.card = stage.closest('.gm');
      this.imgs = {};
      stage.querySelectorAll('img[data-frame]').forEach(i => { this.imgs[i.dataset.frame] = i; });
      this.mouth = 'closed'; this.eyes = false; this.talking = false; this.timer = 0; this.left = 0;
      this.btn = this.card.querySelector('[data-talk]');
      this.btn.addEventListener('click', () => { this.setHype(false); this.set(!this.talking); });
      this.stage = stage;
      this.poses = Object.keys(this.imgs).filter(k => k.startsWith('pose_') && !k.endsWith('_closed')).map(k => k.slice(5));
      this.pose = null; this.hype = false; this.poseTimer = 0;
      this.hbtn = this.card.querySelector('[data-hype]');
      if (this.hbtn) this.hbtn.addEventListener('click', () => this.setHype(!this.hype));
      setTimeout(() => this.blink(), rand(400, 4000));
      this.render();
    }
    key() {
      if (this.pose) {
        const closed = `pose_${this.pose}_closed`;
        return (this.imgs[closed] && this.mouth === 'closed') ? closed : `pose_${this.pose}`;
      }
      if (this.eyes) return this.mouth === 'closed' ? 'blink' : `${this.mouth}_blink`;
      return this.mouth;
    }
    setHype(on) {
      if (!this.hbtn) return;
      this.hype = on;
      clearTimeout(this.poseTimer);
      this.hbtn.setAttribute('aria-pressed', String(on));
      this.hbtn.querySelector('.lbl').textContent = on ? 'Stop hype' : 'Hype mode';
      if (on) { if (!this.talking) this.set(true); this.nextPose(); }
      else { this.pose = null; this.render(); }
    }
    nextPose() {
      if (!this.hype) return;
      const bag = [null, null, ...this.poses, ...this.poses.filter(p => p === 'point' || p === 'hype')];
      let p = this.pose;
      for (let i = 0; i < 6 && p === this.pose; i++) p = bag[Math.floor(Math.random() * bag.length)];
      this.pose = p;
      this.render();
      this.stage.animate([{ transform: 'scale(1.07) rotate(-1.2deg)' }, { transform: 'scale(1)' }], { duration: 200, easing: 'cubic-bezier(.2,.9,.2,1)' });
      this.poseTimer = setTimeout(() => this.nextPose(), p ? rand(900, 1700) : rand(1200, 2200));
    }
    render() {
      let k = this.key();
      if (!this.imgs[k]) k = this.eyes ? 'blink' : this.mouth;
      for (const [n, i] of Object.entries(this.imgs)) i.classList.toggle('on', n === k);
    }
    set(on) {
      if (!on && this.hype) this.setHype(false);
      this.talking = on;
      clearTimeout(this.timer);
      this.btn.setAttribute('aria-pressed', String(on));
      this.btn.querySelector('.lbl').textContent = on ? 'Stop' : 'Talk';
      this.card.classList.toggle('talking', on);
      if (on) { this.left = Math.round(rand(6, 16)); this.tick(); } else { this.mouth = 'closed'; this.render(); }
    }
    next(p) {
      const r = Math.random();
      if (p === 'closed') return r < 0.6 ? 'mid' : r < 0.93 ? 'wide' : 'closed';
      if (p === 'mid') return r < 0.36 ? 'wide' : r < 0.8 ? 'closed' : 'mid';
      return r < 0.55 ? 'mid' : r < 0.9 ? 'closed' : 'wide';
    }
    tick() {
      if (!this.talking) return;
      if (--this.left <= 0) {
        this.mouth = 'closed'; this.left = Math.round(rand(6, 16)); this.render();
        this.timer = setTimeout(() => this.tick(), rand(260, 560)); return;
      }
      this.mouth = this.next(this.mouth); this.render();
      this.timer = setTimeout(() => this.tick(), rand(90, 140));
    }
    blink() {
      this.eyes = true; this.render();
      setTimeout(() => { this.eyes = false; this.render(); setTimeout(() => this.blink(), rand(3000, 4000)); }, rand(120, 160));
    }
  }
  const avatars = [...document.querySelectorAll('.stage[data-avatar]')].map(s => new Avatar(s));
  document.getElementById('all').addEventListener('click', () => avatars.forEach(a => a.talking || a.set(true)));
  document.getElementById('none').addEventListener('click', () => avatars.forEach(a => { a.setHype(false); if (a.talking) a.set(false); }));
  document.getElementById('hypeall').addEventListener('click', () => avatars.forEach(a => a.hbtn ? a.setHype(true) : (a.talking || a.set(true))));
  document.querySelectorAll('img[data-copy]').forEach(img => {
    const [team, key] = img.dataset.copy.split(':');
    const src = document.querySelector(`.stage[data-avatar="${team}"] img[data-frame="${key}"]`);
    if (src) img.src = src.src;
  });
})();
</script>
</body>
</html>
"""
