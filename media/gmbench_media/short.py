"""9:16 shorts — the viral product. Two kinds, one renderer:
  draft shorts:  cold hook (the best line first) -> clock -> the call -> the GM's own call -> the chirps -> end card
  roast shorts:  "The AIs roast each other's drafts": the best roast first -> the Commissioner's one-line frame ->
                 the next best roasts back to back, each followed by a cutaway to the roasted GM -> end card
The current speaker is always BIG (the Commissioner included); a reaction-cam PiP shows the GM a line is about.
Same audio pipeline as the show (TTS cache, SFX, music, -16 LUFS) and the same deterministic motion system.
The end card carries the sponsor CTA: "Build <GM>'s suit — gamedaysuits.ca" with the persona's suit fabric.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import audio
from . import avatars as av
from .captions import fit_size
from .compose import (OFF, RED, RUNTIME_JS, Assets, ShowModel, _state_at, _vis, burst_html, esc, fit, grade_cls,
                      motion_data, slam_html, team_vars, word_events)
from .nhl import pos_display
from .paths import SHOW, rel
from .rigview import RIG_CSS, RIG_JS
from .studio_css import FONT_FACES, pose_rules

W, H = 1080, 1920
LEAD_WORDS = {"is", "are", "was", "were", "at", "to", "of", "in", "on", "and", "but", "that's", "it's", "like", "so",
              "just", "for", "with", "from", "by", "as"}
HOST_VARS = "--c1:#0E1B4D;--c2:#4770DB;--ct:#EFF0F5"

SHORT_CSS = FONT_FACES + """
html, body { margin: 0; width: 1080px; height: 1920px; overflow: hidden; background: #0E1B4D; }
#root { position: relative; width: 1080px; height: 1920px; overflow: hidden; color: #EFF0F5; font-family: "Questrial"; }
.studio { position: absolute; inset: 0; }
.bg { position: absolute; inset: 0; background:
    radial-gradient(900px 900px at 50% 32%, rgba(71,112,219,0.30), transparent 70%),
    repeating-linear-gradient(115deg, rgba(239,240,245,0.03) 0 2px, transparent 2px 26px),
    linear-gradient(180deg, #111f57 0%, #0E1B4D 60%, #0a1440 100%); }
.aibug { position: absolute; z-index: 40; left: 40px; top: 44px; height: 58px; display: flex; align-items: center; gap: 12px;
  padding: 0 20px 0 16px; background: #E32402; border-radius: 14px; }
.aibug .dot { width: 16px; height: 16px; border-radius: 8px; background: #EFF0F5; }
.aibug .t1 { font-family: "Archivo"; font-weight: 800; font-stretch: 112%; font-size: 25px; letter-spacing: 0.06em; color: #fff; }
.sponsor { position: absolute; z-index: 40; right: 40px; top: 40px; text-align: right; white-space: nowrap; }
.sponsor .t1 { font-size: 16px; letter-spacing: 0.3em; color: #c9cfe6; }
.sponsor .t2 { font-family: "Archivo"; font-weight: 900; font-stretch: 118%; font-size: 32px; letter-spacing: 0.04em; }
.sponsor .t2 b { color: #E32402; }
.cam { position: absolute; left: 0; top: 0; width: 1080px; height: 1920px; }
.strap { position: absolute; left: 40px; top: 150px; display: flex; gap: 16px; align-items: center; }
.strap .chip { height: 56px; padding: 0 20px; display: flex; align-items: center; background: #E32402; border-radius: 12px;
  font-family: "Archivo"; font-weight: 900; font-stretch: 118%; font-size: 30px; letter-spacing: 0.05em; color: #fff; white-space: nowrap; }
.strap .sub { font-size: 28px; letter-spacing: 0.14em; color: #c9cfe6; text-transform: uppercase; white-space: nowrap; }
.pt { position: absolute; left: 156px; top: 236px; width: 768px; height: 900px; }
.pt .frame { position: absolute; left: 0; top: 0; width: 768px; height: 768px; border-radius: 30px; overflow: hidden;
  box-shadow: 0 0 0 8px var(--c2), 0 26px 70px rgba(0,0,0,0.55); background: var(--c1); }
.pt .bob { position: absolute; left: 0; top: 0; width: 100%; height: 100%; transform-origin: 50% 92%; }
.pt .cu { position: absolute; left: 0; top: 0; width: 768px; height: 768px; overflow: hidden; }
.pt .av { position: absolute; left: 0; top: 0; width: 768px; height: 768px; }
.pt .gm { position: absolute; left: -106px; top: 790px; width: 980px; text-align: center; font-family: "Archivo"; font-weight: 800;
  font-stretch: 108%; white-space: nowrap; text-shadow: 0 4px 0 rgba(0,0,0,0.4); }
.pt .fr { position: absolute; left: -106px; top: 858px; width: 980px; text-align: center; font-size: 32px; color: #d9def0; white-space: nowrap; }
.pt .cutlab { position: absolute; right: 30px; top: 682px; height: 56px; padding: 0 20px; display: flex; align-items: center;
  border-radius: 12px; background: #E32402; font-family: "Archivo"; font-weight: 900; font-stretch: 115%; font-size: 28px;
  letter-spacing: 0.1em; color: #fff; box-shadow: 0 10px 26px rgba(0,0,0,0.45); }
.av .f, .av .p { position: absolute; left: 0; top: 0; width: 100%; height: 100%; opacity: 0; }
.av[data-m="closed"] .f-closed, .av[data-m="mid"] .f-mid, .av[data-m="open"] .f-open, .av[data-m="blink"] .f-blink,
.av[data-m="mid_blink"] .f-mid_blink, .av[data-m="open_blink"] .f-open_blink { opacity: 1; }
__POSE_RULES__
.pip { position: absolute; z-index: 6; left: 40px; top: 700px; width: 300px; height: 300px; }
.pip .ph { position: absolute; left: 0; top: 0; width: 300px; height: 300px; border-radius: 26px; overflow: hidden;
  box-shadow: 0 0 0 6px #E32402, 0 14px 34px rgba(0,0,0,0.55); background: var(--c1); }
.pip .av { position: absolute; left: 0; top: 0; width: 100%; height: 100%; }
.pip .n { position: absolute; left: 0; top: 252px; width: 300px; height: 48px; line-height: 48px; text-align: center;
  border-radius: 0 0 26px 26px; background: rgba(7,14,44,0.86); font-family: "Archivo"; font-weight: 900; font-size: 24px;
  letter-spacing: 0.08em; color: #fff; white-space: nowrap; }
.card { position: absolute; left: 60px; top: 1150px; width: 960px; height: 250px; box-sizing: border-box; padding: 22px 34px;
  background: rgba(7,14,44,0.86); border: 1px solid rgba(71,112,219,0.45); border-radius: 26px; }
.card .lab { font-size: 22px; letter-spacing: 0.24em; color: #b9c3e8; text-transform: uppercase; white-space: nowrap; }
.card .big { font-family: "Archivo"; font-weight: 800; font-stretch: 108%; line-height: 1.02; margin-top: 10px; white-space: nowrap; }
.card .row { display: flex; gap: 14px; margin-top: 14px; align-items: center; }
.pill { display: inline-flex; align-items: center; height: 46px; padding: 0 18px; border-radius: 11px; font-family: "Archivo";
  font-weight: 800; font-stretch: 110%; font-size: 26px; letter-spacing: 0.05em; white-space: nowrap; }
.pill.red { background: #E32402; color: #fff; }
.pill.gold { background: #FFC83D; color: #0E1B4D; }
.pill.royal { background: #4770DB; color: #fff; }
.pill.ghost { background: rgba(239,240,245,0.10); color: #EFF0F5; border: 1px solid rgba(239,240,245,0.25); }
.by { font-size: 26px; color: #d9def0; white-space: nowrap; }
.by b { font-family: "Archivo"; font-weight: 800; color: #EFF0F5; }
.pre, .post, .slip { position: absolute; left: 34px; top: 62px; width: 892px; }
.slip { top: 20px; height: 210px; }
.slip .lab { font-size: 22px; }
.slip .stamp { position: absolute; right: 0; top: 4px; width: 200px; height: 170px; border-radius: 18px; box-sizing: border-box;
  border: 6px solid #EFF0F5; text-align: center; box-shadow: 0 12px 30px rgba(0,0,0,0.45); }
.slip .stamp .l { margin-top: 14px; font-family: "Archivo"; font-weight: 900; font-stretch: 112%; font-size: 100px; line-height: 1; color: #fff; }
.slip .stamp .g { margin-top: 4px; font-family: "Archivo"; font-weight: 800; font-size: 18px; letter-spacing: 0.1em; color: #fff; }
.ga { background: #4770DB; } .gb { background: #4a557d; } .gc { background: #E32402; }
.hookchip { position: absolute; z-index: 7; left: 620px; top: 262px; height: 58px; padding: 0 22px; display: flex; align-items: center;
  border-radius: 14px; background: #E32402; font-family: "Archivo"; font-weight: 900; font-stretch: 118%; font-size: 30px;
  letter-spacing: 0.06em; color: #fff; box-shadow: 0 10px 28px rgba(0,0,0,0.45); white-space: nowrap; }
.caps { position: absolute; z-index: 35; left: 50px; top: 1430px; width: 980px; height: 270px; }
.mc { position: absolute; left: 0; top: 0; width: 980px; }
.mc .who { display: flex; align-items: center; gap: 10px; height: 34px; }
.mc .who .sw { width: 14px; height: 30px; border-radius: 3px; background: var(--c2); box-shadow: inset 0 0 0 2px var(--c1); }
.mc .who .t { font-family: "Archivo"; font-weight: 800; letter-spacing: 0.08em; color: #c9cfe6; white-space: nowrap; }
.mc .rows { margin-top: 12px; }
.mc .r { font-family: "Archivo"; font-weight: 800; font-size: 62px; line-height: 82px; height: 82px; white-space: nowrap;
  color: #EFF0F5; -webkit-text-stroke: 3px #0a1238; paint-order: stroke fill; text-shadow: 0 5px 0 rgba(0,0,0,0.5); }
.mc .w { display: inline-block; transform-origin: 50% 80%; }
.ctx { position: absolute; z-index: 41; left: 40px; top: 150px; width: 1000px; height: 58px; display: flex; align-items: center;
  justify-content: center; border-radius: 12px; background: #EFF0F5; color: #0E1B4D; font-family: "Archivo"; font-weight: 900;
  font-stretch: 110%; letter-spacing: 0.04em; white-space: nowrap; box-shadow: 0 10px 26px rgba(0,0,0,0.45); }
.prevc, .robotx { position: absolute; z-index: 8; left: 186px; width: 708px; box-sizing: border-box; padding: 12px 20px;
  border-radius: 14px; background: rgba(7,14,44,0.93); box-shadow: 0 10px 26px rgba(0,0,0,0.45); }
.prevc { top: 262px; border: 3px solid #FFC83D; }
.robotx { top: 380px; border: 3px solid #EFF0F5; }
.prevc .k, .robotx .k { font-family: "Archivo"; font-weight: 900; font-size: 21px; letter-spacing: 0.2em; }
.prevc .k { color: #FFC83D; } .robotx .k { color: #EFF0F5; }
.prevc .t, .robotx .t { margin-top: 3px; font-family: "Archivo"; font-weight: 800; color: #fff; white-space: nowrap; }
.pt .model { position: absolute; left: -106px; top: 856px; width: 980px; text-align: center; }
.pt .model .chip { display: inline-block; height: 60px; line-height: 60px; padding: 0 26px; border-radius: 14px;
  background: var(--c2); color: var(--ct); font-family: "Archivo"; font-weight: 900; font-stretch: 112%; letter-spacing: 0.04em;
  white-space: nowrap; box-shadow: 0 0 0 3px #0a1238, 0 8px 20px rgba(0,0,0,0.45); }
.card.tight { top: 1172px; height: 204px; padding: 18px 34px; }
.dboard { position: absolute; left: 20px; top: 14px; width: 920px; height: 220px; }
.dboard .dr { position: absolute; left: 0; width: 920px; height: 40px; display: flex; align-items: center; gap: 16px;
  font-family: "Archivo"; font-weight: 800; font-size: 31px; color: #EFF0F5; }
.dboard .dr > span { position: relative; z-index: 1; }
.dboard .dh { position: absolute; z-index: 0; left: -12px; top: -2px; width: 944px; height: 44px; border-radius: 10px;
  background: #E32402; }
.dboard .nm { width: 330px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.dboard .me { width: 190px; color: #FFD23F; }
.dboard .lg { width: 220px; color: #d9def0; }
.dboard .gp { width: 110px; text-align: right; font-weight: 900; }
.dboard .gp.up { color: #FFD23F; } .dboard .gp.dn { color: #8FA7E8; }
.card.tight .pre, .card.tight .post { top: 52px; }
.tk { position: absolute; z-index: 36; left: 30px; top: 1404px; width: 1020px; height: 240px; display: flex; align-items: center;
  justify-content: center; text-align: center; font-family: "Archivo"; font-weight: 900; font-stretch: 106%; line-height: 1.04;
  color: #fff; -webkit-text-stroke: 8px #0a1238; paint-order: stroke fill; text-shadow: 0 8px 0 rgba(0,0,0,0.55); }
.tk .p { color: #FFD23F; } .tk .n { color: #FF6A4A; }
.kick { position: absolute; z-index: 34; left: 40px; width: 1000px; top: 292px; text-align: center; font-family: "Archivo";
  font-weight: 900; font-stretch: 118%; line-height: 1; color: #fff; white-space: nowrap; -webkit-text-stroke: 9px #E32402;
  paint-order: stroke fill; text-shadow: 0 10px 0 #0a1238, 0 0 44px rgba(227,36,2,0.55); }
.slam { position: absolute; z-index: 32; left: 0; height: 330px; text-align: center; }
.slam .st { font-family: "Archivo"; font-weight: 900; font-stretch: 125%; line-height: 1; white-space: nowrap;
  -webkit-text-stroke: 5px #0E1B4D; paint-order: stroke fill; text-shadow: 0 10px 0 var(--c1, #0E1B4D), 0 0 60px rgba(227,36,2,0.55); }
.slam .ss { margin-top: 18px; display: inline-block; padding: 8px 22px; border-radius: 12px; background: #E32402; color: #fff;
  font-family: "Archivo"; font-weight: 800; font-stretch: 112%; font-size: 30px; letter-spacing: 0.12em; white-space: nowrap; }
.flash { position: absolute; z-index: 33; left: 0; top: 0; width: 1080px; height: 1920px; background: #fff; opacity: 0; visibility: hidden; }
.burst { position: absolute; z-index: 31; width: 0; height: 0; }
.burst i { position: absolute; left: 0; top: 0; display: block; }
.end { position: absolute; z-index: 38; inset: 0; background:
    radial-gradient(900px 900px at 50% 42%, rgba(71,112,219,0.34), transparent 70%),
    repeating-linear-gradient(115deg, rgba(239,240,245,0.035) 0 2px, transparent 2px 24px), #0E1B4D; }
.end .k { position: absolute; top: 470px; width: 1080px; text-align: center; font-size: 30px; letter-spacing: 0.4em; color: #c9cfe6; }
.end .t { position: absolute; top: 530px; width: 1080px; text-align: center; font-family: "Archivo"; font-weight: 900;
  font-stretch: 125%; font-size: 150px; line-height: 0.95; }
.end .bar { position: absolute; top: 860px; left: 440px; width: 200px; height: 12px; border-radius: 6px; background: #E32402; }
.end .l2 { position: absolute; top: 916px; width: 1080px; text-align: center; font-family: "Archivo"; font-weight: 800;
  font-stretch: 112%; font-size: 38px; letter-spacing: 0.08em; }
.end .l3 { position: absolute; top: 986px; width: 1080px; text-align: center; font-size: 28px; letter-spacing: 0.2em; color: #c9cfe6; }
.end .cta { position: absolute; left: 70px; top: 1100px; width: 940px; height: 300px; box-sizing: border-box; padding: 30px 34px 0 290px;
  border-radius: 28px; background: rgba(7,14,44,0.9); border: 3px solid #E32402; text-align: left; }
.end .cta .sw { position: absolute; left: 30px; top: 30px; width: 236px; height: 236px; border-radius: 18px; background-size: cover;
  box-shadow: 0 0 0 4px #EFF0F5, 0 10px 26px rgba(0,0,0,0.5); }
.end .cta .c1 { font-family: "Archivo"; font-weight: 900; font-stretch: 108%; line-height: 1.1; color: #fff; width: 610px; }
.end .cta .c2 { margin-top: 14px; color: #d9def0; line-height: 1.3; width: 610px; }
.end .cta .c3 { position: absolute; left: 290px; bottom: 26px; font-family: "Archivo"; font-weight: 900; font-stretch: 112%; font-size: 36px;
  letter-spacing: 0.02em; color: #E32402; white-space: nowrap; }
.end .l4 { position: absolute; left: 60px; top: 1740px; width: 960px; text-align: center; font-size: 26px; line-height: 1.3;
  letter-spacing: 0.03em; color: #aab2d4; }
.suitc { position: absolute; z-index: 7; left: 172px; top: 252px; width: 188px; height: 262px; box-sizing: border-box;
  padding: 176px 12px 0; border-radius: 16px; background: rgba(7,14,44,0.92); border: 2px solid #E32402;
  box-shadow: 0 10px 26px rgba(0,0,0,0.45); }
.suitc .sw { position: absolute; left: 12px; top: 12px; width: 160px; height: 156px; border-radius: 10px; background-size: cover;
  box-shadow: 0 0 0 2px #EFF0F5; }
.suitc .k { font-family: "Archivo"; font-weight: 900; letter-spacing: 0.06em; color: #FF5A3C; white-space: nowrap; }
.suitc .f { margin-top: 4px; font-family: "Archivo"; font-weight: 800; color: #fff; line-height: 1.15; }
.suitc .d { margin-top: 3px; font-family: "Questrial"; color: #c9cfe6; line-height: 1.2; }
"""


def cta_for(league, tid: str | None) -> dict | None:
    """'Build <GM>'s suit — gamedaysuits.ca' + the persona's suit fabric (text only)."""
    t = league.teams.get(tid) if tid else None
    if not t or not t.has_persona:
        return None
    suit = t.p("suit") or {}
    fab = league.fabrics.get(suit.get("fabric_id") or "", {})
    who = t.short_gm or t.gm_name
    return {"team": tid, "line": f"Build {who}'s suit", "fabric": fab.get("name") or "",
            "summary": fab.get("summary") or fab.get("pattern_desc") or ""}


class VerticalShort:
    def __init__(self, model: ShowModel, assets: Assets, cfg: dict, rd: dict, rigview=None):
        self.m, self.A, self.cfg, self.rd = model, assets, cfg, rd
        self.RV = rigview
        self.L = model.L
        self.kind = (rd.get("short") or {}).get("kind", "draft")
        self.rc = {t["team"]: t for t in (model.report or {}).get("teams", [])}

    def label(self, sp: str, addressed: str | None) -> str:
        if sp == "host":
            s = "THE COMMISSIONER"
        else:
            t = self.L.teams[sp]
            s = f"{t.short_gm} · {t.nickname}" if t.has_persona else t.display
        if addressed and addressed in self.L.teams and addressed != sp:
            tt = self.L.teams[addressed]
            s += "  →  " + (tt.nickname if tt.has_persona else tt.display)
        return s.upper()

    def vars(self, sp: str) -> str:
        return HOST_VARS if sp == "host" else team_vars(self.L.teams[sp])

    def who(self, sp: str) -> tuple[str, str]:
        if sp == "host":
            return "The Commissioner", "Draft Night host · reads the ledger"
        t = self.L.teams[sp]
        if t.has_persona:
            return t.gm_name, t.franchise_name
        return ("Autodraft", "Control bot · house projection") if t.is_bot else (t.display, f"{t.display} · {t.lab}")

    def nick(self, tid: str) -> str:
        t = self.L.teams[tid]
        return t.nickname if t.has_persona else ("Autodraft" if t.is_bot else t.display)

    def featured(self) -> str | None:
        spec = self.rd.get("short") or {}
        if spec.get("cta"):
            return spec["cta"]
        if self.kind == "roast":
            first = next((ln for ln in self.m.lines if ln["speaker"] != "host"), None)
            return first["speaker"] if first else None
        if self.kind == "delusion":
            rows = spec.get("rows") or []
            return rows[0][0] if rows and self.L.teams[rows[0][0]].has_persona else None
        for ln in self.m.lines:
            p = self.m.picks.get(ln.get("pick_no"))
            if p and self.L.teams[p.team].has_persona:
                return p.team
        return None

    def chip_colors(self, sp: str) -> str:
        """The model chip in the team's main colour with whichever of white / navy reads on it."""
        from .compose import contrast
        t = self.L.teams.get(sp)
        bg = (t.colors[0] if t and t.colors else "#4770DB")
        if contrast(bg, "#0E1B4D") < 1.6:  # a navy-ish team colour disappears on the page: use its second colour
            bg = t.colors[1] if t and len(t.colors) > 1 else "#4770DB"
        fg = max(("#FFFFFF", "#0E1B4D"), key=lambda c: contrast(bg, c))
        return f"background:{bg};color:{fg}"

    def model_name(self, sp: str) -> str:
        t = self.L.teams.get(sp)
        if not t:
            return "THE COMMISSIONER"
        return "CONTROL BOT" if t.is_bot else t.display.upper()

    def tight_layer(self, cid: str, vis, data: dict, T: float) -> str:
        """Tight shorts: TikTok word cards (2-4 words, punch words coloured), the kicker pop on every punch, the
        context strip, the 'Previously' chip and the robot explainer."""
        from .captions import word_times
        m, L = self.m, self.L
        sh = self.rd.get("short") or {}
        out = []
        # --- word cards: per sentence, balanced 2-4 word cards; the kicker is its own (final) card
        player_words = {w.lower().strip(".,!?'\"") for p in L.picks for w in p.player_name.split()}
        groups = []
        for ln in m.lines:
            ws = word_times(ln)
            marks = (ln.get("beats") or {}).get("marks") or []
            kick_spans = [(float(ln["start"]) + mk["kicker_start"] - 0.02, float(ln["start"]) + mk["kicker_end"] + 0.02)
                          for mk in marks]
            sents: list[list[dict]] = [[]]
            for w in ws:
                if w["w"] in ("—", "–", "-"):
                    if sents[-1]:
                        sents[-1][-1] = {**sents[-1][-1], "w": sents[-1][-1]["w"].rstrip(",") + ","}
                    continue
                sents[-1].append(w)
                if re.search(r"[.!?]$", w["w"]):
                    sents.append([])
            for sent in [x for x in sents if x]:
                kick = [i for i, w in enumerate(sent) if any(a_ <= w["start"] <= b_ for a_, b_ in kick_spans)]
                cut = kick[0] if (kick and len(sent) - kick[0] <= 4 and kick[0] >= 2) else len(sent)
                head, tail = sent[:cut], sent[cut:]
                phrases: list[list[dict]] = [[]]  # commas and dashes first: cards follow the speech's phrasing
                for w in head:
                    phrases[-1].append(w)
                    if re.search(r"[,;:—–]$", w["w"]) or w["w"] in ("—", "–"):
                        phrases.append([])
                phrases = [x for x in phrases if x]
                merged: list[list[dict]] = []
                for ph in phrases:  # a one-word phrase leans on its neighbour
                    if merged and (len(ph) == 1 or len(merged[-1]) == 1) and len(merged[-1]) + len(ph) <= 4:
                        merged[-1] = merged[-1] + ph
                    else:
                        merged.append(ph)
                for i in range(len(merged) - 2, -1, -1):  # ...forward when the one behind is full
                    if len(merged[i]) == 1 and i + 1 < len(merged):
                        merged[i + 1] = merged[i] + merged[i + 1]
                        merged.pop(i)
                chunks = []
                for ph in merged:
                    n = len(ph)
                    k = max(1, round(n / 3.2))
                    while n / k > 4:
                        k += 1
                    size, rem = divmod(n, k)
                    i = 0
                    for c in range(k):
                        sz = size + (1 if c < rem else 0)
                        chunks.append(ph[i:i + sz])
                        i += sz
                if tail:
                    if len(tail) == 1 and chunks and len(chunks[-1]) <= 3:
                        chunks[-1] = chunks[-1] + tail
                    else:
                        chunks.append(tail)
                if len(chunks) > 1 and len(chunks[-1]) == 1 and len(chunks[-2]) <= 3:
                    chunks[-2] = chunks[-2] + chunks.pop()
                for ch in chunks:
                    groups.append((ln, ch, kick_spans))
        for gi, (ln, grp, kick_spans) in enumerate(groups):
            t_on = 0.0 if gi == 0 else max(0.0, grp[0]["start"] - 0.03)  # muted autoplay: words from frame one
            nxt_on = groups[gi + 1][1][0]["start"] - 0.03 if gi + 1 < len(groups) else None
            same_line = gi + 1 < len(groups) and groups[gi + 1][0]["id"] == ln["id"]
            t_off = nxt_on if (nxt_on is not None and (same_line or nxt_on - grp[-1]["said"] < 0.6)) else grp[-1]["said"] + 0.3
            t_off = min(t_off, T - 0.05)
            spans = []
            for w in grp:
                core = w["w"].lower().strip(".,!?'\"")
                cls = "p" if any(a_ <= w["start"] <= b_ for a_, b_ in kick_spans) else ("n" if core in player_words else "")
                spans.append(f'<span class="{cls}">{esc(w["w"].upper())}</span>' if cls else esc(w["w"].upper()))
            text = " ".join(x["w"] for x in grp).upper()
            size = fit(text, 980, "archivo", 96, 58, 2, 900, 106)
            key = f"tk{gi}"
            if t_on > 1e-6:
                vis(key, t_on, 1, 0.0)
            vis(key, t_off, 0, 0.0)
            out.append(f'<div class="tk" id="{cid}-{key}" style="font-size:{size}px;{_vis(t_on <= 1e-6)}">'
                       f'<div>{" ".join(spans)}</div></div>')
        # --- kicker pops (verbatim: the kicker's own words), on the punch word
        data.setdefault("kp", [])
        j = 0
        for ln in m.lines:
            ws = word_times(ln)
            for mk in (ln.get("beats") or {}).get("marks") or []:
                a = float(ln["start"]) + mk["kicker_start"]
                b = float(ln["start"]) + mk["kicker_end"]
                kw = [w for w in ws if a - 0.03 <= w["start"] <= b]
                while len(kw) > 1 and kw[0]["w"].lower().strip(".,!?'\"") in LEAD_WORDS:
                    kw = kw[1:]
                if not kw:
                    continue
                text = " ".join(w["w"] for w in kw).upper()
                size = fit(text, 960, "archivo", 124, 60, 1, 900, 118)
                nxt_start = min([float(x["start"]) for x in m.lines if float(x["start"]) > float(ln["start"]) + 0.01
                                 and x["speaker"] != ln["speaker"]] + [T])
                off = min(b + float(mk.get("beat") or 0.4) + 0.35, nxt_start - 0.05, T - 0.05)
                key = f"kk{j}"
                data["kp"].append([round(a, 4), key, round(off, 4)])
                out.append(f'<div class="kick" id="{cid}-{key}" data-layout-allow-overlap style="font-size:{size}px;'
                           f'opacity:0;visibility:hidden">{esc(text)}</div>')
                j += 1
        # --- context strip (by 1 s, gone by ~6 s) over the round strap
        ctx = sh.get("context")
        if ctx:
            vis("ctx", 0.8, 1, 0.25, 8)
            vis("ctx", 6.2, 0, 0.25)
            vis("strap", 0.8, 0, 0.0)
            vis("strap", 6.2, 1, 0.25)
            out.append(f'<div class="ctx" id="{cid}-ctx" style="font-size:{fit(ctx, 960, "archivo", 30, 20, 1, 900, 110)}px;'
                       f'opacity:0;visibility:hidden">{esc(ctx)}</div>')
        # --- 'Previously' chip at the first call after the cold open
        body = [ln for ln in m.lines if ln["kind"] != "hook"]
        prev = sh.get("previously")
        if prev and body:
            t0 = float(body[0]["start"]) - 0.1
            vis("prevc", t0, 1, 0.2, 10)
            vis("prevc", t0 + 3.6, 0, 0.25)
            txt = prev["text"]
            out.append(f'<div class="prevc" id="{cid}-prevc" style="opacity:0;visibility:hidden"><div class="k">PREVIOUSLY</div>'
                       f'<div class="t" style="font-size:{fit(txt, 660, "archivo", 34, 20, 1, 800, 100)}px">{esc(txt)}</div></div>')
        # --- the robot, explained once: the first time it is on screen or named
        if sh.get("robot"):
            bots = {tid for tid, t in L.teams.items() if t.is_bot}
            times = [x["t0"] for x in m.shots if x["sp"] in bots]
            for ln in m.lines:
                for w in word_times(ln):
                    if w["w"].lower().strip(".,!?'\"") in ("robot", "robot's", "bot"):
                        times.append(w["start"])
                        break
            if times:
                t0 = max(0.05, min(times) - 0.05)
                vis("robotx", t0, 1, 0.2, 10)
                vis("robotx", t0 + 3.4, 0, 0.25)
                out.append('<div class="robotx" id="' + cid + '-robotx" style="opacity:0;visibility:hidden">'
                           '<div class="k">THE ROBOT</div><div class="t" style="font-size:30px">'
                           'the dumb control bot they all have to beat</div></div>')
        return "".join(out)

    def html(self, cid: str) -> tuple[str, float]:  # noqa: C901
        m = self.m
        L = self.L
        a = "../assets/"
        T = round(m.T, 4)
        lines = m.lines
        EPS = 1e-6
        roast = self.kind in ("roast", "delusion")
        delusion = self.kind == "delusion"
        sh = (self.rd.get("short") or {}) if isinstance(self.rd, dict) else {}
        tight = bool(sh.get("tight"))
        picks = sorted({ln["pick_no"] for ln in lines if ln.get("pick_no")})
        first_pick = m.picks[picks[0]] if picks else None
        data = {"m": [], "a": [], "v": [], "c": [], "x": [], "e": [], "p": [], "ps": []}
        vis_ev: dict[str, list] = {}

        def vis(el: str, t: float, on: int, d: float, dy: float = 0.0) -> None:
            vis_ev.setdefault(el, []).append((round(t, 4), on, d, dy))

        # stage: the current speaker BIG (the Commissioner too); cutaways hold on the roasted GM
        focus_ev = [(t, f) for t, f in m.focus_ev if f]
        if not focus_ev:
            focus_ev = [(0.0, lines[0]["speaker"])]
        focus_ev[0] = (0.0, focus_ev[0][1])
        cuts = [(round(e["t"], 4), round(e["until"], 4), e["team"]) for e in m.cutaways]
        stage = sorted({f for _, f in focus_ev})
        for (t, f), (_, prev) in zip(focus_ev[1:], focus_ev):
            if f != prev:
                vis(f"pt-{prev}", t, 0, 0.0)
                vis(f"pt-{f}", t, 1, 0.0 if round(t, 4) in m.hard_cuts else 0.12)
        # close-ups: cuts inside the frame (the medium instance keeps playing under it)
        if self.RV is not None:
            for sp in stage:
                for a_, b_ in self.RV.cu.get(sp, []):
                    if a_ > EPS:
                        vis(f"cu-{sp}", a_, 1, 0.0)
                    vis(f"cu-{sp}", b_, 0, 0.0)
        # the reaction cut: REACTION CAM label on the listener's close-up, their PiP off meanwhile
        lis_cuts = [(x["t0"], x["t1"], x["sp"]) for x in m.shots if x["kind"] == "listener"]
        for t, u, f in lis_cuts:
            vis(f"cut-{f}", t, 1, 0.0)
            vis(f"cut-{f}", u, 0, 0.0)
        for t, u, f in cuts:
            vis(f"cut-{f}", t, 1, 0.12, -8)
            vis(f"cut-{f}", u, 0, 0.0)
        # reaction-cam PiP: the team on the clock while the host calls it; the target of a chirp / roast
        pip_ev: list[tuple[float, str | None]] = []
        for ln in lines:
            if ln["speaker"] == "host":
                who = ln.get("focus") if ln.get("focus") in L.teams else None
            else:
                tgt = ln.get("addressed_to")
                who = tgt if tgt in L.teams and tgt != ln["speaker"] else None
            pip_ev.append((ln["sw"], who))
            if who and ln["id"] in m.beat2:  # two-beat call: the reaction cam only while the last pick is the topic
                pip_ev.append((round(m.beat2[ln["id"]] - 0.1, 4), None))
        for t, u, f in cuts:
            pip_ev.append((t, None))
        pip_ev.sort(key=lambda x: x[0])
        if lis_cuts:
            def _pip_at(tt, ev):
                v = None
                for x, w in ev:
                    if x <= tt + 1e-6:
                        v = w
                return v
            base = list(pip_ev)
            for t, u, f in lis_cuts:
                if _pip_at(t, base) == f:
                    pip_ev = [(x, w) for x, w in pip_ev if not (t - 1e-6 <= x < u)]
                    pip_ev += [(t, None), (u, _pip_at(u, base))]
            pip_ev.sort(key=lambda x: x[0])
        pips = sorted({w for _, w in pip_ev if w})
        cur = None
        pip0 = None
        for t, w in pip_ev:
            if t <= EPS:
                pip0 = w
                cur = w
                continue
            if w != cur:
                if cur:
                    vis(f"pip-{cur}", t, 0, 0.0)
                if w:
                    vis(f"pip-{w}", t, 1, 0.2, 14)
                cur = w
        # pick cards (draft shorts): show on the first call; reveal beat at the name
        cards_by_pick = sorted({ln["pick_no"] for ln in lines if ln.get("pick_no") and ln["kind"] != "hook"})
        card_on0 = False
        if not roast:
            for ln in lines:
                if ln.get("pick_no") and ln["kind"] in ("host_clock", "host_pick", "on_air_call", "pick_statement"):
                    if ln["sw"] <= EPS:
                        card_on0 = True
                    else:
                        vis("card", ln["sw"], 1, 0.3, 20)
                    break
            for idx, pn in enumerate(cards_by_pick):
                start_ln = next((ln for ln in lines if ln.get("pick_no") == pn and ln["kind"] in
                                 ("host_clock", "host_pick", "on_air_call", "pick_statement")), None)
                if idx > 0 and start_ln:
                    vis(f"pk{cards_by_pick[idx - 1]}-post", start_ln["sw"], 0, 0.0)
                    vis(f"pk{pn}-pre", start_ln["sw"], 1, 0.2, 10)
                rt = m.pick_reveal.get(pn)
                if rt is not None:  # a cut, not a crossfade: 'On the clock.' never ghosts under the player's name
                    vis(f"pk{pn}-pre", rt, 0, 0.0)
                    vis(f"pk{pn}-post", rt, 1, 0.18, 14)
        # grade slips (roast shorts): one per roast line, the grade stamp punches in as the roast starts
        slips = [ln for ln in lines if (ln.get("card") or {}).get("type") in ("roast", "report") and ln["speaker"] != "host"]
        if delusion:  # the board is up from frame one; each call-out lights its team's row
            slips = []
            card_on0 = True
            lit = [(ln["sw"], ((ln.get("card") or {}).get("hl") or [None])[0]) for ln in lines]
            for k, (t_, team) in enumerate(lit):
                if team:
                    if t_ > EPS:
                        vis(f"dh-{team}", t_, 1, 0.15, 0)
                    nxt_t = next((u for u, tm in lit[k + 1:] if tm != team), None)
                    if nxt_t is not None:
                        vis(f"dh-{team}", nxt_t, 0, 0.0)
        if roast and slips:
            card_on0 = slips[0]["sw"] <= EPS
            if not card_on0:
                vis("card", slips[0]["sw"], 1, 0.3, 20)
            for i, ln in enumerate(slips):
                if ln["sw"] > EPS:
                    vis(f"sl-{ln['id']}", ln["sw"], 1, 0.2, 10)
                if i + 1 < len(slips):
                    vis(f"sl-{ln['id']}", slips[i + 1]["sw"], 0, 0.0)
                data["p"].append([round(ln["start"] + 0.15, 4), f"st-{ln['id']}"])
            for x in (x for x in lines if x["speaker"] == "host"):
                if x["sw"] > EPS:
                    vis("card", x["sw"], 0, 0.0)
                nxt = next((sl for sl in slips if sl["sw"] > x["sw"]), None)
                if nxt:
                    vis("card", nxt["sw"], 1, 0.25, 16)
        # the hook chip during the cold hook
        hook = next((ln for ln in lines if ln["kind"] == "hook"), None) if not tight else None
        if hook:
            vis("hookchip", hook["end"] + 0.1, 0, 0.0)
        # captions + words
        pages = list(enumerate(m.pages)) if not tight else []
        first_pg = min((pg["show"] for _, pg in pages), default=0.0)
        for i, pg in pages:
            if pg["show"] == first_pg:  # muted autoplay: the hook's words are on screen from frame one
                pg["show"] = 0.0
            if pg["show"] > EPS:
                vis(f"cp{i}", pg["show"], 1, 0.0)
            vis(f"cp{i}", min(pg["hide"], T - 0.05), 0, 0.0)
        # word highlights on the short's own clock: word_events returns times relative to t0, so t0 must be ~0
        # (it was -1.0, which lit every word a full second after the voice said it)
        data["w"] = word_events(pages, -1e-6, T + 1)
        # mouths
        speakers = sorted(set(stage) | set(pips) | {"host"})
        if self.RV is None:
            for sp in speakers:
                for t, st in m.mouth.get(sp, []):
                    if EPS < t < T:
                        data["m"].append([round(t, 4), sp, st])
            data["m"].sort(key=lambda r: r[0])
        else:  # rigs: text-driven lip sync, expressions and gestures (animate.py), no loudness flapping
            keys = set(speakers) | {f"{sp}-cu" for sp in stage if f"{sp}-cu" in self.RV.state}
            data.update(self.RV.events(keys, 0.0, T))
            data["rs"] = sorted(k for k in keys if k in self.RV.state)
            data["rigmode"] = True
        mouth0 = {sp: _state_at(m.mouth.get(sp, []), 0.0) for sp in speakers}
        pose0 = {sp: m.motion.pose_at(sp, 0.0) for sp in speakers}
        # end card after the last word (and its cutaway)
        last_end = max(ln["end"] for ln in lines)
        # ...and after the last reaction shot (the roasted GM's face on the final punchline)
        tail = max([u for _, u, _ in cuts] + [last_end] + [u - 0.7 for t, u, _ in lis_cuts if t >= last_end - 1.0])
        end_t = round(min(T - 1.8, tail + 0.25), 4)
        self.end_t = end_t  # (the montage cuts each short here)
        vis("end", end_t, 1, 0.45, 0)
        vis("card", round(end_t + 0.5, 4), 0, 0.0)  # nothing live under the opaque end card
        for el, dt_, dy in (("e1", 0.25, 30), ("e2", 0.4, 50), ("e3", 0.6, 0), ("e4", 0.75, 20), ("e5", 0.9, 20),
                            ("e6", 1.15, 30)):
            data["e"].append([round(end_t + dt_, 4), el, 0.5, dy])
        for el, evs in vis_ev.items():
            evs.sort(key=lambda e: e[0])
            for n, (t, on, d, dy) in enumerate(evs):
                nxt = evs[n + 1][0] if n + 1 < len(evs) else t + 99
                d2 = min(d, max(0.0, nxt - t - 0.02))
                data["v"].append([t, el, on, round(d2 if d2 >= 0.08 else 0.0, 3), dy])
        data["v"].sort(key=lambda r: r[0])
        data["p"].sort(key=lambda r: r[0])
        present = {"portraits": set(stage), "tiles": set(), "host": False}
        data.update(motion_data(m, 0.0, T, present, self.cfg, rig=self.RV is not None))
        data["origin"] = "540px 620px"

        # ---- DOM
        portraits = []
        for sp in stage:
            name, fr = self.who(sp)
            gs = fit(name, 960, "archivo", 64, 34, 1)
            mcard = ""
            fset = m.fs.get(sp)
            if sp != "host" and fset and fset.style == "placeholder":  # no production art: the model's name, big
                t = L.teams[sp]
                main = (t.gm_name if t.has_persona else t.display).upper()
                sub = (t.franchise_name if t.has_persona else (t.lab or "")).upper()
                mcard = (f'<div class="mcard"><div class="m1" style="font-size:{fit(main, 700, "archivo", 100, 36, 1, 900, 112)}px">'
                         f'{esc(main)}</div><div class="m2">{esc(sub)}</div></div>')
            cu = ""
            if self.RV is not None and self.RV.cu.get(sp):
                on0 = any(a_ <= EPS < b_ for a_, b_ in self.RV.cu[sp])
                cu = (f'<div class="cu" id="{cid}-cu-{sp}" data-layout-allow-overflow style="{_vis(on0)}">'
                      f'{self.RV.html(sp, a, 768, 0.0, crop="close", key=f"{sp}-cu")}</div>')
            portraits.append(
                f'<div class="pt" id="{cid}-pt-{sp}" style="{self.vars(sp)};{_vis(sp == focus_ev[0][1])}">'
                f'<div class="frame"><div class="bob" id="{cid}-bob-{sp}">'
                f'{self.RV.html(sp, a, 768, 0.0) if self.RV else self.A.av(sp, "X", a, initial=mouth0.get(sp, "closed"), pose=pose0.get(sp, "none"))}'
                f'</div>{cu}{mcard}</div>'
                f'<div class="cutlab" id="{cid}-cut-{sp}" style="{_vis(False)}">'
                f'{esc(self.model_name(sp) + " REACTS") if tight and sp != "host" else "REACTION CAM"}</div>'
                + (f'<div class="gm" style="font-size:{min(gs, 58)}px">{esc(name)}</div>'
                   f'<div class="model"><span class="chip" style="{self.chip_colors(sp)};font-size:{fit(self.model_name(sp), 860, "archivo", 40, 24, 1, 900, 112)}px">'
                   f'{esc(self.model_name(sp))}</span></div></div>' if tight else
                   f'<div class="gm" style="font-size:{gs}px">{esc(name)}</div><div class="fr">{esc(fr)}</div></div>'))
        pip_html = []
        for tid in pips:
            nm = self.model_name(tid) if tight else self.nick(tid).upper()
            ns = fit(nm, 280, "archivo", 24, 14, 1, 900, 100)
            pip_html.append(f'<div class="pip" id="{cid}-pip-{tid}" style="{self.vars(tid)};{_vis(tid == pip0)}">'
                            f'<div class="ph">{self.RV.html(tid, a, 300, 0.0, crop="bust") if self.RV else self.A.av(tid, "X", a, initial=mouth0.get(tid, "closed"), pose=pose0.get(tid, "none"))}'
                            f'</div><div class="n" style="font-size:{ns}px">{esc(nm)}</div></div>')
        cards = []
        lab = ""
        if not roast:
            for pn in cards_by_pick:
                p = m.picks[pn]
                t = L.teams[p.team]
                size, _ = fit_size(p.player_name, 880, "archivo", 90, 44, 1, 800, 108)
                who = t.franchise_name if t.has_persona else ("Autodraft" if t.is_bot else t.display)
                flag = L.value_flags().get(pn)
                fl = {"reach": '<span class="pill gold">REACH?!</span>', "steal": '<span class="pill royal">STEAL!</span>'}.get(flag or "", "")
                bsz = fit(f"by {who}", 520 if not fl else 380, "archivo", 26, 16, 1)
                first = pn == cards_by_pick[0]
                pre_txt = "On the clock." if self.cfg["script"].get("format", "party") == "party" else "The pick is in."
                cards.append(
                    f'<div class="pre" id="{cid}-pk{pn}-pre" style="{_vis(first)}"><div class="big" style="font-size:80px">'
                    f'{pre_txt}</div></div>'
                    f'<div class="post" id="{cid}-pk{pn}-post" style="{team_vars(t)};{_vis(False)}"><div class="big" '
                    f'style="font-size:{size}px">{esc(p.player_name)}</div><div class="row"><span class="pill red">'
                    f'{esc(pos_display(p.position))}</span><span class="pill ghost">{esc(p.nhl_team)}</span>{fl}'
                    f'<span class="by" style="font-size:{bsz}px">by <b>{esc(who)}</b></span></div></div>')
            lab = (f"Round {first_pick.round} · pick {picks[0]}" + (f"–{picks[-1]}" if len(picks) > 1 else "")
                   if first_pick else "")
        elif delusion:
            rows = (sh.get("rows") or [])
            show = rows[:4] + [r for r in rows[4:] if r[3] <= -2.0][:1]
            first_lit = next((((ln.get("card") or {}).get("hl") or [None])[0] for ln in lines
                              if ((ln.get("card") or {}).get("hl") or [None])[0]), None)
            rws = []
            for k, (team, own, avg, gap) in enumerate(show):
                t = L.teams[team]
                nm = t.short_gm if t.has_persona else t.display
                rws.append(f'<div class="dr" style="top:{12 + k * 42}px">'
                           f'<div class="dh" id="{cid}-dh-{team}" style="{_vis(team == first_lit and lines[0]["sw"] <= EPS)}"></div>'
                           f'<span class="nm">{esc(nm.upper())}</span><span class="me">THINKS {own}</span>'
                           f'<span class="lg">LEAGUE {avg:.1f}</span>'
                           f'<span class="gp {"up" if gap > 0 else "dn"}">{"+" if gap > 0 else ""}{gap:.1f}</span></div>')
            cards.append(f'<div class="dboard">{"".join(rws)}</div>')
            lab = "The Delusion Index"
        else:
            for i, ln in enumerate(slips):
                c = ln["card"]
                tgt, grd = c["team"], c.get("grade", "?")
                r = self.rc.get(tgt) or {}
                head = f"{self.nick(ln['speaker'])} grades the {self.nick(tgt)}".upper()
                tname = L.teams[tgt].franchise_name if L.teams[tgt].has_persona else self.nick(tgt)
                tsz = fit(tname, 640, "archivo", 58, 30, 1)
                cons = f"Class GPA {float(r['gpa']):.2f} · consensus {r['letter']}" if r.get("gpa") is not None else ""
                cards.append(f'<div class="slip" id="{cid}-sl-{ln["id"]}" style="{_vis(i == 0 and ln["sw"] <= EPS)}">'
                             f'<div class="lab" style="font-size:{fit(head, 640, "questrial", 22, 14, 1, 400, 100)}px">'
                             f'{esc(head)}</div><div class="big" style="font-size:{tsz}px;margin-top:22px">{esc(tname)}</div>'
                             f'<div class="by" style="margin-top:18px">{esc(cons)}</div>'
                             f'<div class="stamp {grade_cls(grd)}" id="{cid}-st-{ln["id"]}" style="opacity:0;visibility:hidden">'
                             f'<div class="l">{esc(grd)}</div><div class="g">THEIR GRADE</div></div></div>')
            lab = "The AIs roast each other's drafts"
        card = (f'<div class="card{" tight" if tight else ""}" id="{cid}-card" style="{_vis(card_on0)}">'
                + (f'<div class="lab">{esc(lab)}</div>' if not roast else "") + f'{"".join(cards)}</div>')
        caps = []
        for i, pg in pages:
            ln = next(x for x in lines if x["id"] == pg["line"])
            b2 = m.beat2.get(ln["id"])
            who = self.label(pg["speaker"], ln.get("addressed_to") if (b2 is None or pg["start"] < b2 - 0.05) else None)
            ws = fit(who, 960, "archivo", 24, 14, 1, 800, 105)
            rows, k = [], 0
            for row in pg["rows"]:
                spans = []
                for w in row:
                    act = w["start"] <= 0.0 < w["end"]
                    spans.append(f'<span class="w" id="{cid}-w{i}_{k}" style="color:{RED if act else OFF}">{esc(w["w"])}</span>')
                    k += 1
                rows.append('<div class="r">' + " ".join(spans) + "</div>")
            sp = pg["speaker"]
            caps.append(f'<div class="mc" id="{cid}-cp{i}" style="{self.vars(sp)};{_vis(pg["show"] <= EPS)}"><div class="who">'
                        f'<div class="sw"></div><div class="t" style="font-size:{ws}px">{esc(who)}</div></div>'
                        f'<div class="rows">{"".join(rows)}</div></div>')
        extra_html = ""
        if tight:
            done_keys = set(vis_ev)
            extra_html = self.tight_layer(cid, vis, data, T)
            for key in [k for k in vis_ev if k not in done_keys]:
                evs = sorted(vis_ev[key], key=lambda e: e[0])
                for n, (t, on, d, dy) in enumerate(evs):
                    nxt = evs[n + 1][0] if n + 1 < len(evs) else t + 99
                    d2 = min(d, max(0.0, nxt - t - 0.02))
                    data["v"].append([t, key, on, round(d2 if d2 >= 0.08 else 0.0, 3), dy])
            data["v"].sort(key=lambda r: r[0])
        hook_txt = ("THEY THINK THEY'LL WIN" if delusion else "THE AIs GRADED EACH OTHER") if roast else "WAIT FOR IT"
        hookchip = f'<div class="hookchip" id="{cid}-hookchip">{hook_txt}</div>' if hook else ""
        strap_chip = ("DELUSION INDEX" if delusion else "REPORT CARD") if roast else "DRAFT NIGHT"
        strap_sub = ("Where they think they'll finish" if delusion else "The AIs roast each other") if roast else lab.upper()
        slams = "".join(slam_html(x, L, W, 560) for x in m.motion.slams).replace("{cid}", cid)
        bursts = "".join(burst_html(b, L, 540, 620) for b in m.motion.bursts).replace("{cid}", cid)
        from .suits import cta_lines, suit_info, swatch_url
        info = suit_info(L, self.featured())
        cta_html = ""
        if info:
            h1, h2, h3 = cta_lines(info)
            sw = swatch_url(info)
            swd = (f'<div class="sw" style="background-image:url({a}{sw})"></div>' if sw else
                   f'<div class="sw" style="background:{info["color"]}"></div>')
            h2sz = fit(h2, 610, "questrial", 28, 16, 2, 400, 100)
            cta_html = (f'<div class="cta" id="{cid}-e6" style="opacity:0;visibility:hidden">{swd}'
                        f'<div class="c1" style="font-size:{fit(h1, 600, "archivo", 34, 16, 2, 900, 108)}px">{esc(h1)}</div>'
                        f'<div class="c2" style="font-size:{h2sz}px">{esc(h2)}</div>'
                        f'<div class="c3" style="font-size:{fit(h3, 600, "archivo", 36, 20, 1, 900, 112)}px">{esc(h3)}</div></div>')
        # the suit card: the first time each GM talks, what they are wearing (a real fabric, a real cut)
        suit_cards = []
        seen_suit = set()
        for ln in lines:
            sp = ln["speaker"]
            if sp == "host" or sp in seen_suit or sp not in stage:
                continue
            seen_suit.add(sp)
            si = suit_info(L, sp)
            if not si:
                continue
            sw = swatch_url(si)
            key = f"sc-{sp}"
            win = m.suit_window(sp, 4.2)  # the first medium stretch: never over a close-up or a reaction cut
            if not win:
                continue
            vis(key, win[0], 1, 0.3, 12)
            vis(key, win[1], 0, 0.25)
            swd = (f'<div class="sw" style="background-image:url({a}{sw})"></div>' if sw else
                   f'<div class="sw" style="background:{si["color"]}"></div>')
            l2 = " · ".join(x for x in (si["summary"], si["cut"]) if x)
            suit_cards.append(f'<div class="suitc" id="{cid}-{key}" style="opacity:0;visibility:hidden">{swd}'
                              f'<div class="k" style="font-size:{fit(si["possessive"].upper() + " SUIT", 164, "archivo", 16, 10, 1, 900, 100)}px">'
                             f'{esc(si["possessive"].upper())} SUIT</div>'
                              f'<div class="f" style="font-size:{fit(si["fabric"], 164, "archivo", 15, 10, 2, 800, 100)}px">'
                              f'{esc(si["fabric"])}</div><div class="d" style="font-size:{fit(l2, 164, "questrial", 13, 9, 3, 400, 100)}px">'
                              f'{esc(l2)}</div></div>')
        for key in [k for k in vis_ev if k.startswith("sc-")]:
            evs = sorted(vis_ev[key], key=lambda e: e[0])
            for n, (t, on, d, dy) in enumerate(evs):
                nxt = evs[n + 1][0] if n + 1 < len(evs) else t + 99
                d2 = min(d, max(0.0, nxt - t - 0.02))
                data["v"].append([t, key, on, round(d2 if d2 >= 0.08 else 0.0, 3), dy])
        data["v"].sort(key=lambda r: r[0])
        fine = ("AI-generated voices &amp; avatars · every GM line is the model's own words, edited for length"
                if tight else "AI-generated voices &amp; avatars · every GM line is the model's own words")
        t1, t2 = (("DELUSION", "INDEX") if delusion else ("REPORT", "CARD")) if roast else ("DRAFT", "NIGHT")
        end = (f'<div class="end" id="{cid}-end" style="{_vis(False)}"><div class="k" id="{cid}-e1" style="opacity:0;'
               f'visibility:hidden">GM-BENCH PRESENTS</div><div class="t" id="{cid}-e2" style="opacity:0;visibility:hidden">'
               f'<div>{t1}</div><div>{t2}</div></div><div class="bar" id="{cid}-e3" style="opacity:0;visibility:hidden"></div>'
               f'<div class="l2" id="{cid}-e4" style="opacity:0;visibility:hidden">THE SUITS 2026–27</div>'
               f'<div class="l3" id="{cid}-e5" style="opacity:0;visibility:hidden">Presented by Game Day Suits</div>'
               f'{cta_html}'
               f'<div class="l4">{fine}</div></div>')
        studio = (f'<div class="studio" id="{cid}-s"><div class="bg"></div>'
                  f'<div class="aibug"><div class="dot"></div><div class="t1">AI-GENERATED</div></div>'
                  f'<div class="sponsor"><div class="t1">PRESENTED BY</div><div class="t2">GAME DAY <b>SUITS</b></div></div>'
                  f'<div class="cam" id="{cid}-cam"><div class="strap" id="{cid}-strap"><div class="chip">{strap_chip}</div>'
                  f'<div class="sub">{esc(strap_sub)}</div></div>{"".join(portraits)}{"".join(suit_cards)}{"".join(pip_html)}{card}{hookchip}{extra_html}</div>'
                  f'{bursts}{slams}<div class="flash" id="{cid}-flash"></div><div class="caps">{"".join(caps)}</div>'
                  f'{end}<div id="{cid}-strip" style="display:none"></div></div>')
        root = (f'<div id="root" data-composition-id="{cid}" data-start="0" data-width="{W}" data-height="{H}" '
                f'data-duration="{(round(T * m.fps) - 0.01) / m.fps:.6f}" data-fps="{m.fps}">{studio}</div>')
        js = RUNTIME_JS.replace("__CID__", cid).replace("__RIG_JS__", RIG_JS if self.RV else "").replace(
            "__DATA__", json.dumps({**data, "speakers": speakers, "x0": 0}, separators=(",", ":")))
        css = SHORT_CSS.replace("{A}", a).replace("__POSE_RULES__", pose_rules()) + (RIG_CSS if self.RV else "")
        doc = (f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="UTF-8" />\n'
               f'<meta name="viewport" content="width={W}, height={H}" />\n<title>{cid}</title>\n'
               f'<script src="{a}vendor/gsap-3.14.2.min.js"></script>\n<style>\n{css}</style>\n</head>\n<body>\n'
               f'{root}\n<script>\n{js}\n</script>\n</body>\n</html>\n')
        return doc, T


def build_short_page(name: str, league, rd: dict, cues: dict, tracks: dict, env: dict, cfg: dict):
    """The short's page, in memory: (doc, duration, model, rigview). Deterministic: the render and the QA both use it."""
    fs = av.load_framesets(league, av.all_speakers(league))
    model = ShowModel(league, rd, cues, tracks, fs, cfg, env=env, layout="short")
    speakers = ({"host"} | {ln["speaker"] for ln in model.lines} | {ln.get("focus") for ln in model.lines if ln.get("focus")}
                | {ln.get("addressed_to") for ln in model.lines if ln.get("addressed_to")})
    assets = Assets({sp: fs[sp] for sp in speakers if sp in fs}, sizes={"X": 768})
    rv = None
    if (cfg.get("animation") or {}).get("rigs", True):
        from .animate import Plan
        from .rig import build_sheets, load_rig
        from .rigview import RigView
        rigs = {sp: r for sp in speakers if sp in fs for r in [load_rig(sp, fs[sp])] if r}
        rv = RigView(rigs, {sp: build_sheets(r) for sp, r in rigs.items()}, model.plan or Plan(cues, league),
                     addressee_side="left")
    vs = VerticalShort(model, assets, cfg, rd, rv)
    doc, dur = vs.html(f"short-{name}")
    model.end_card_s = getattr(vs, "end_t", None)
    return doc, dur, model, rv


def render_short(odir: Path, name: str, league, rd: dict, cues: dict, tracks: dict, env: dict, cfg: dict,
                 log=print, render: bool = True) -> dict:
    cid = f"short-{name}"
    doc, dur, model, rv = build_short_page(name, league, rd, cues, tracks, env, cfg)
    rdir = SHOW / "render"
    rdir.mkdir(exist_ok=True)
    (rdir / f"{cid}.html").write_text(doc)
    if not render:
        return {"name": name, "html": rel(rdir / f"{cid}.html"), "duration_s": dur}
    r = cfg["render"]
    silent = odir / f"{cid}-video.mp4"
    cmd = ["npx", "--yes", r["hyperframes"], "render", str(SHOW), "-c", f"render/{cid}.html", "-o", str(silent),
           "--quality", str(r["quality"]), "--crf", str(r["crf"]), "--fps", str(model.fps), "--quiet"]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=SHOW)
    if proc.returncode != 0:
        (odir / f"{cid}.log").write_text(proc.stdout[-20000:] + proc.stderr[-20000:])
        raise RuntimeError(f"short render failed; see {rel(odir / (cid + '.log'))}")
    out = odir / f"{cid}.mp4"
    audio.run([audio.FFMPEG, "-v", "error", "-y", "-i", str(silent), "-i", str(odir / "mix.wav"), "-map", "0:v:0",
               "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", r["audio_bitrate"], "-ar", "48000",
               "-t", f"{dur:.4f}", "-movflags", "+faststart", str(out)])
    silent.unlink(missing_ok=True)
    loud = audio.ebur128(out, true_peak=True)
    info = {"name": name, "mp4": rel(out), "duration_s": dur, "size_mb": round(out.stat().st_size / 1e6, 2),
            "ebur128": loud, "html": rel(rdir / f"{cid}.html"), "lines": [ln["id"] for ln in model.lines],
            "end_card_s": getattr(model, "end_card_s", None)}
    (odir / f"{cid}.json").write_text(json.dumps(info, indent=1))
    log(f"short: {name} ({dur:.1f}s, I={loud['I']} LUFS) -> {rel(out)}")
    if (rd.get("short") or {}).get("tight"):
        try:
            from .packaging import write_beatmap, write_package
            bm = write_beatmap(odir, rd, cues, model.plan, league)
            pkg = write_package(odir, name, rd, cues, model.plan, league, out)
            info.update({"beatmap": rel(bm), "package": rel(odir / f"{cid}.package.json"), "titles": pkg["titles"]})
            (odir / f"{cid}.json").write_text(json.dumps(info, indent=1))
            log(f"short: beat map + package -> {rel(bm)}, {rel(odir / (cid + '.package.txt'))}")
        except Exception as e:  # noqa: BLE001  packaging never sinks a render
            log(f"short: packaging failed: {type(e).__name__}: {e}")
    return info
