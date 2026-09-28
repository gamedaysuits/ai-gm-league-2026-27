"""Studio stylesheet (1920x1080). `{A}` is the asset URL prefix ("../assets/" for standalone render
files in show/render/, "assets/" for templated sub-compositions, which resolve against the project root).
Brand: navy #0E1B4D, red-orange #E32402, royal #4770DB, off-white #EFF0F5; Archivo / Questrial (bundled).
No transforms in CSS on anything GSAP animates (lint: gsap_css_transform_conflict)."""

FONT_FACES = """
@font-face { font-family: "Archivo"; src: url("{A}fonts/archivo-latin-standard-normal.woff2") format("woff2");
  font-weight: 100 900; font-stretch: 62% 125%; font-style: normal;
  unicode-range: U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD; }
@font-face { font-family: "Archivo"; src: url("{A}fonts/archivo-latin-ext-standard-normal.woff2") format("woff2");
  font-weight: 100 900; font-stretch: 62% 125%; font-style: normal;
  unicode-range: U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF; }
@font-face { font-family: "Questrial"; src: url("{A}fonts/questrial-latin-400-normal.woff2") format("woff2");
  font-weight: 400; font-style: normal;
  unicode-range: U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD; }
@font-face { font-family: "Questrial"; src: url("{A}fonts/questrial-latin-ext-400-normal.woff2") format("woff2");
  font-weight: 400; font-style: normal;
  unicode-range: U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF; }
"""

CSS = FONT_FACES + """
#root { position: relative; width: 1920px; height: 1080px; overflow: hidden; background: #0E1B4D;
  color: #EFF0F5; font-family: "Questrial", sans-serif; }
.studio { position: absolute; inset: 0; --navy: #0E1B4D; --red: #E32402; --royal: #4770DB; --off: #EFF0F5;
  --panel: rgba(7, 14, 44, 0.78); --line: rgba(71, 112, 219, 0.38); }
.bg { position: absolute; inset: 0; z-index: 0;
  background:
    radial-gradient(1200px 700px at 30% 38%, rgba(71, 112, 219, 0.22), transparent 70%),
    radial-gradient(900px 600px at 85% 20%, rgba(227, 36, 2, 0.07), transparent 70%),
    repeating-linear-gradient(115deg, rgba(239, 240, 245, 0.028) 0 2px, transparent 2px 26px),
    linear-gradient(180deg, #111f57 0%, #0E1B4D 55%, #0a1440 100%); }
.hx { font-family: "Archivo", sans-serif; font-weight: 800; font-stretch: 112%; letter-spacing: 0.01em; }
.kick { font-family: "Questrial", sans-serif; letter-spacing: 0.22em; text-transform: uppercase; }
.panel { position: absolute; background: var(--panel); border: 1px solid var(--line); border-radius: 18px; }

/* bugs: always on top */
.bug { position: absolute; z-index: 40; }
.aibug { left: 44px; top: 30px; height: 50px; display: flex; align-items: center; gap: 12px; padding: 0 18px 0 14px;
  background: var(--red); border-radius: 12px; box-shadow: 0 6px 18px rgba(0,0,0,0.35); }
.aibug .dot { width: 14px; height: 14px; border-radius: 7px; background: var(--off); }
.aibug .t1 { font-family: "Archivo"; font-weight: 800; font-stretch: 112%; font-size: 21px; letter-spacing: 0.06em; color: #fff; }
.aibug .t2 { font-family: "Questrial"; font-size: 15px; color: #fff; letter-spacing: 0.04em; }
.sponsor { right: 44px; top: 26px; width: 460px; height: 58px; text-align: right; white-space: nowrap; }
.sponsor .t1 { font-family: "Questrial"; font-size: 14px; letter-spacing: 0.3em; color: #c9cfe6; }
.sponsor .t2 { font-family: "Archivo"; font-weight: 900; font-stretch: 118%; font-size: 30px; letter-spacing: 0.04em; color: var(--off); line-height: 1.1; }
.sponsor .t2 b { color: var(--red); font-weight: 900; }
.sponsor .t2 { margin-top: 2px; display: flex; justify-content: flex-end; }
.logo.wordmark { font-family: "Archivo"; font-weight: 900; font-stretch: 118%; letter-spacing: 0.04em; color: var(--off); white-space: nowrap; }
.logo.wordmark b { color: var(--red); }
.ovl .brand { margin-top: 42px; display: flex; flex-direction: column; align-items: center; gap: 12px; }
.ovl .brand .pb { font-family: "Questrial"; font-size: 18px; letter-spacing: 0.42em; color: #c9cfe6; }
.ovl .brand.end { margin-top: 36px; gap: 18px; }
.ovl .brand .cta { font-family: "Archivo"; font-weight: 800; font-stretch: 110%; font-size: 30px; letter-spacing: 0.06em; color: var(--off); }
.card .cardlogo { position: absolute; right: 34px; top: 20px; opacity: 0.95; }
.card .rtbrand { position: absolute; left: 34px; bottom: 30px; display: flex; align-items: center; gap: 16px; }
.card .rtbrand .pb { font-family: "Questrial"; font-size: 15px; letter-spacing: 0.34em; color: #c9cfe6; }
.card .swstrip { position: absolute; left: 0; right: 0; top: 0; height: 14px; border-radius: 18px 18px 0 0;
  background-size: 140px auto; background-repeat: repeat; box-shadow: inset 0 -2px 0 rgba(0,0,0,0.35); }
.card .swlab { position: absolute; right: 34px; top: 26px; font-family: "Questrial"; font-size: 14px; letter-spacing: 0.2em;
  color: #b9c3e8; text-transform: uppercase; white-space: nowrap; max-width: 420px; overflow: hidden; text-overflow: ellipsis; }
.pill.cup { background: rgba(71,112,219,0.22); color: var(--off); border: 2px solid var(--royal); }

.showtitle { position: absolute; z-index: 5; left: 468px; top: 30px; height: 56px; display: flex; align-items: baseline; gap: 18px; }
.showtitle .t1 { font-family: "Archivo"; font-weight: 900; font-stretch: 125%; font-size: 38px; letter-spacing: 0.02em; }
.showtitle .t2 { font-family: "Questrial"; font-size: 17px; letter-spacing: 0.26em; color: #b9c3e8; text-transform: uppercase; }

/* two layouts: speaker beats (.spk: the current speaker BIG + the grid collapsed to a bench strip) and board beats
   (.brd: the full grid, the host box, wide cards). Cards and captions are positioned by class. */
.brd, .spk { position: absolute; inset: 0; z-index: 4; }

/* segment strap */
.strap { position: absolute; z-index: 6; height: 46px; display: flex; align-items: center; gap: 14px; }
.strap.b { left: 48px; top: 110px; }
.strap.s { left: 952px; top: 200px; }
.strap .chip { height: 46px; padding: 0 18px; display: flex; align-items: center; background: var(--red); border-radius: 10px;
  font-family: "Archivo"; font-weight: 800; font-stretch: 115%; font-size: 23px; letter-spacing: 0.06em; color: #fff; white-space: nowrap; }
.strap .sub { font-family: "Questrial"; font-size: 21px; letter-spacing: 0.16em; color: #c9cfe6; text-transform: uppercase; white-space: nowrap; }

/* the big speaker (~45% of the frame width) */
.bigpt { position: absolute; left: 92px; top: 108px; width: 768px; height: 900px; }
.bigpt .frame { position: absolute; left: 0; top: 0; width: 768px; height: 768px; border-radius: 22px; overflow: hidden;
  box-shadow: 0 0 0 6px var(--c2), 0 26px 64px rgba(0,0,0,0.5); background: var(--c1); }
.bigpt .bob { position: absolute; left: 0; top: 0; width: 100%; height: 100%; transform-origin: 50% 92%; }
.bigpt .cu { position: absolute; left: 0; top: 0; width: 768px; height: 768px; overflow: hidden; }
.bigpt .av { position: absolute; left: 0; top: 0; width: 768px; height: 768px; }
.bigpt .plate { position: absolute; left: 0; top: 782px; width: 768px; height: 118px; padding: 0 4px; box-sizing: border-box; }
.bigpt .gm { font-family: "Archivo"; font-weight: 800; font-stretch: 108%; line-height: 1.04; color: var(--off); white-space: nowrap;
  text-shadow: 0 3px 0 rgba(0,0,0,0.5); }
.bigpt .row2 { margin-top: 8px; display: flex; align-items: center; gap: 16px; }
.bigpt .fr { font-family: "Questrial"; color: #d9def0; white-space: nowrap; }
.bigpt .mdl { display: inline-flex; align-items: center; height: 30px; padding: 0 12px; border-radius: 8px; white-space: nowrap;
  background: rgba(71,112,219,0.26); border: 1px solid rgba(71,112,219,0.6); font-family: "Questrial"; font-size: 18px; color: var(--off); }
.bigpt .cutlab { position: absolute; right: 26px; top: 700px; height: 44px; padding: 0 16px; display: flex; align-items: center; gap: 10px;
  border-radius: 10px; background: #E32402; font-family: "Archivo"; font-weight: 900; font-stretch: 115%; font-size: 22px;
  letter-spacing: 0.1em; color: #fff; box-shadow: 0 8px 22px rgba(0,0,0,0.45); }

.bigpt .mcard { position: absolute; left: 0; top: 220px; width: 768px; height: 250px; box-sizing: border-box; padding-top: 46px;
  text-align: center; background: rgba(7,14,44,0.78); border-top: 4px solid #E32402; border-bottom: 4px solid #E32402; }
.bigpt .mcard .m1 { font-family: "Archivo"; font-weight: 900; font-stretch: 112%; line-height: 1.02; color: #EFF0F5; white-space: nowrap; }
.bigpt .mcard .m2 { margin-top: 14px; font-family: "Archivo"; font-weight: 800; font-size: 34px; letter-spacing: 0.24em; color: #c9cfe6; }

/* suit tag: the first time a GM talks (top-left of the frame: never over the face or the suit) */
.suitc { position: absolute; z-index: 6; left: 108px; top: 124px; width: 188px; height: 262px; box-sizing: border-box;
  padding: 176px 12px 0; border-radius: 16px; background: rgba(7,14,44,0.92); border: 2px solid #E32402;
  box-shadow: 0 10px 26px rgba(0,0,0,0.45); }
.suitc .sw { position: absolute; left: 12px; top: 12px; width: 160px; height: 156px; border-radius: 10px; background-size: cover;
  box-shadow: 0 0 0 2px #EFF0F5; }
.suitc .k { font-family: "Archivo"; font-weight: 900; letter-spacing: 0.06em; color: #FF5A3C; white-space: nowrap; }
.suitc .f { margin-top: 4px; font-family: "Archivo"; font-weight: 800; color: #fff; line-height: 1.15; }
.suitc .d { margin-top: 3px; font-family: "Questrial"; color: #c9cfe6; line-height: 1.2; }

/* the collapsed grid: a slim bench strip of faces */
.bench { position: absolute; left: 952px; top: 104px; width: 920px; height: 84px; }
.bn { position: absolute; top: 0; width: 58px; height: 84px; }
.bn .ph { position: absolute; left: 0; top: 0; width: 58px; height: 58px; border-radius: 12px; overflow: hidden;
  box-shadow: 0 0 0 2px var(--c2); background: var(--c1); }
.bn .av { position: absolute; left: 0; top: 0; width: 100%; height: 100%; }
.bn .ab { position: absolute; left: -8px; top: 63px; width: 74px; height: 19px; border-radius: 5px; text-align: center; font-family: "Archivo";
  font-weight: 800; font-stretch: 105%; font-size: 13px; line-height: 19px; letter-spacing: 0.05em; color: #aeb8de; }
.bn[data-on="1"] .ph { box-shadow: 0 0 0 3px #E32402, 0 0 18px rgba(227,36,2,0.6); }
.bn[data-on="1"] .ab { color: #fff; }
.bn[data-clock="1"] .ab { color: #fff; background: #E32402; }

/* cards: side (speaker beats, right column) and full (board beats) */
.card { position: absolute; z-index: 5; box-sizing: border-box; }
.card.side { left: 952px; top: 262px; width: 920px; height: 420px; padding: 26px 34px; }
.card.full { left: 48px; top: 176px; width: 1040px; height: 588px; padding: 30px 34px; }
.card .lab { font-family: "Questrial"; font-size: 17px; letter-spacing: 0.24em; color: #b9c3e8; text-transform: uppercase; white-space: nowrap; }
.card .big { font-family: "Archivo"; font-weight: 800; font-stretch: 108%; line-height: 1.05; color: var(--off); }
.card .rule { width: 72px; height: 6px; border-radius: 3px; background: var(--red); margin: 16px 0 14px; }
.card .txt { font-family: "Questrial"; font-size: 24px; line-height: 1.35; color: #e4e7f3; }
.card .row { display: flex; align-items: center; gap: 12px; margin-top: 12px; }
.card .by { font-family: "Questrial"; font-size: 20px; color: #c9cfe6; white-space: nowrap; }
.card .by .k { letter-spacing: 0.18em; text-transform: uppercase; font-size: 15px; margin-right: 8px; }
.card .by b { font-family: "Archivo"; font-weight: 800; color: var(--off); }
.pk { position: relative; height: 160px; margin-top: 12px; }
.pk > div { position: absolute; left: 0; top: 0; width: 852px; }
.pill { display: inline-flex; align-items: center; height: 36px; padding: 0 14px; border-radius: 9px; font-family: "Archivo";
  font-weight: 800; font-stretch: 110%; font-size: 20px; letter-spacing: 0.05em; white-space: nowrap; }
.pill.red { background: var(--red); color: #fff; }
.pill.royal { background: var(--royal); color: #fff; }
.pill.gold { background: #FFC83D; color: #0E1B4D; }
.pill.ghost { background: rgba(239,240,245,0.10); color: var(--off); border: 1px solid rgba(239,240,245,0.25); }
.pill.agree { background: #EFF0F5; color: #0E1B4D; }
.pill.disagree { background: rgba(255,200,61,0.14); color: #FFC83D; border: 2px solid #FFC83D; }
.pill.think { background: rgba(7,14,44,0.55); color: #EFF0F5; border: 2px solid rgba(239,240,245,0.55); }
.stat { font-family: "Questrial"; font-size: 21px; color: #d9def0; white-space: nowrap; }
.stat b { font-family: "Archivo"; font-weight: 800; color: var(--off); font-size: 24px; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.chips span { font-family: "Questrial"; font-size: 17px; padding: 6px 12px; border-radius: 8px; background: rgba(239,240,245,0.08);
  border: 1px solid rgba(239,240,245,0.18); color: var(--off); }
.suit { margin-top: 12px; font-family: "Questrial"; font-size: 18px; line-height: 1.35; color: #c9cfe6; }
.suit b { color: var(--off); font-weight: 400; }
.tbl { margin-top: 10px; }
.tbl .tr { display: flex; align-items: center; height: 44px; border-bottom: 1px solid rgba(239,240,245,0.08); gap: 14px; }
.tbl .n { width: 40px; text-align: right; font-family: "Archivo"; font-weight: 800; font-size: 22px; color: #b9c3e8; }
.tbl .ab { width: 64px; height: 30px; border-radius: 7px; display: flex; align-items: center; justify-content: center;
  font-family: "Archivo"; font-weight: 800; font-stretch: 110%; font-size: 16px; letter-spacing: 0.04em; background: var(--c1); color: var(--ct);
  box-shadow: inset 0 0 0 2px var(--c2); }
.tbl .nm { font-family: "Archivo"; font-weight: 700; font-size: 21px; color: var(--off); white-space: nowrap; }
.tbl .sm { font-family: "Questrial"; font-size: 17px; color: #b9c3e8; white-space: nowrap; }
.tbl .lt { position: absolute; right: 8px; top: 6px; width: 58px; height: 32px; border-radius: 8px; display: flex; align-items: center;
  justify-content: center; font-family: "Archivo"; font-weight: 900; font-size: 20px; }
.tbl .lt.q { background: rgba(239,240,245,0.08); color: #7f8bb8; border: 1px dashed rgba(239,240,245,0.3); box-sizing: border-box; }
.tbl .tr { position: relative; }
.cols { display: flex; gap: 30px; }
.cols > div { flex: 1; }
.foot { position: absolute; left: 34px; right: 34px; bottom: 24px; font-family: "Questrial"; font-size: 16px; color: #9fa9cf; letter-spacing: 0.06em; }
.card .rec { position: absolute; left: 34px; right: 34px; bottom: 22px; font-family: "Questrial"; line-height: 1.35; color: #aeb8de; }
.card .rec b { font-family: "Archivo"; font-weight: 800; font-size: 13px; letter-spacing: 0.16em; color: #EFF0F5; margin-right: 8px; }

/* reaction card (beat 1: the previous pick + its GM's face) */
.rcam { position: absolute; left: 34px; top: 64px; width: 290px; height: 290px; }
.rcam .ph { position: absolute; left: 0; top: 0; width: 290px; height: 290px; border-radius: 22px; overflow: hidden;
  box-shadow: 0 0 0 5px var(--c2), 0 14px 34px rgba(0,0,0,0.5); background: var(--c1); }
.rcam .av { position: absolute; left: 0; top: 0; width: 100%; height: 100%; }
.rcam .tg { position: absolute; left: 14px; top: 14px; height: 34px; padding: 0 12px; display: flex; align-items: center;
  border-radius: 8px; background: #E32402; font-family: "Archivo"; font-weight: 900; font-size: 16px; letter-spacing: 0.1em; color: #fff; }
.rtx { position: absolute; left: 356px; top: 64px; width: 496px; }

/* report card */
.ga { background: #4770DB; color: #fff; }
.gb { background: rgba(239,240,245,0.14); color: #EFF0F5; }
.gc { background: #E32402; color: #fff; }
.stamp { position: absolute; right: 40px; top: 26px; width: 200px; height: 148px; border-radius: 16px; box-sizing: border-box;
  border: 6px solid #EFF0F5; text-align: center; box-shadow: 0 12px 30px rgba(0,0,0,0.45); }
.stamp .l { margin-top: 6px; font-family: "Archivo"; font-weight: 900; font-stretch: 112%; font-size: 86px; line-height: 1; color: #fff; }
.stamp .g { margin-top: 6px; font-family: "Archivo"; font-weight: 800; font-size: 20px; letter-spacing: 0.08em; color: #fff; }
.gchips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; width: 852px; }
.gch { display: flex; align-items: center; height: 38px; border-radius: 9px; overflow: hidden; background: rgba(7,14,44,0.9);
  border: 1px solid rgba(239,240,245,0.18); box-sizing: border-box; }
.gch .ab { height: 38px; padding: 0 9px; display: flex; align-items: center; background: var(--c1); color: var(--ct);
  font-family: "Archivo"; font-weight: 800; font-stretch: 108%; font-size: 14px; letter-spacing: 0.05em; }
.gch .gl { height: 38px; min-width: 46px; padding: 0 8px; box-sizing: border-box; display: flex; align-items: center; justify-content: center;
  font-family: "Archivo"; font-weight: 900; font-size: 19px; }
.gch[data-on="1"] { border: 3px solid #FFC83D; box-shadow: 0 0 20px rgba(255,200,61,0.55); }

/* the delusion index */
.legend { margin-top: 10px; display: flex; align-items: center; gap: 10px; font-family: "Questrial"; font-size: 18px; color: #c9cfe6; }
.legend .ks, .legend .ka { display: inline-block; width: 16px; height: 16px; border-radius: 8px; margin-left: 14px; }
.legend .ks { background: #E32402; margin-left: 0; }
.legend .ka { background: #EFF0F5; }
.dchart { position: relative; margin-top: 8px; width: 972px; }
.dchart .ticks { position: relative; height: 22px; }
.dchart .tk { position: absolute; top: 0; width: 40px; text-align: center; font-family: "Archivo"; font-weight: 800; font-size: 14px; color: #7f8bb8; }
.dr { position: relative; height: 30px; margin-top: 2px; border-radius: 8px; }
.dr[data-on="1"] { background: rgba(227,36,2,0.22); box-shadow: inset 0 0 0 2px #E32402; }
.dr .ab { position: absolute; left: 6px; top: 3px; width: 60px; height: 24px; border-radius: 6px; display: flex; align-items: center;
  justify-content: center; background: var(--c1); color: var(--ct); box-shadow: inset 0 0 0 1.5px var(--c2); font-family: "Archivo";
  font-weight: 800; font-size: 13px; letter-spacing: 0.04em; }
.dr .nm { position: absolute; left: 78px; top: 0; width: 270px; height: 30px; line-height: 30px; font-family: "Archivo"; font-weight: 700;
  font-size: 18px; color: var(--off); white-space: nowrap; overflow: hidden; }
.dr .bar { position: absolute; top: 13px; height: 4px; border-radius: 2px; background: rgba(239,240,245,0.45); }
.dr .ds, .dr .da { position: absolute; top: 6px; width: 18px; height: 18px; border-radius: 9px; }
.dr .ds { background: #E32402; box-shadow: 0 0 0 2px #0E1B4D; }
.dr .da { background: #EFF0F5; box-shadow: 0 0 0 2px #0E1B4D; }
.dr .gp { position: absolute; left: 870px; top: 0; width: 90px; height: 30px; line-height: 30px; text-align: right; font-family: "Archivo";
  font-weight: 900; font-size: 20px; color: var(--off); }

/* grid (board beats) */
.grid { position: absolute; z-index: 4; left: 1120px; top: 110px; width: 752px; height: 654px; }
.tile { position: absolute; width: 370px; height: 86px; border-radius: 14px; background: var(--panel); border: 1px solid var(--line);
  box-sizing: border-box; overflow: hidden; }
.tile .tav { position: absolute; left: 8px; top: 7px; width: 70px; height: 70px; border-radius: 12px; overflow: hidden;
  box-shadow: 0 0 0 2px var(--c2); background: var(--c1); }
.tile .tbob, .host .hbob { position: absolute; left: 0; top: 0; width: 100%; height: 100%; transform-origin: 50% 90%; }
.tile .av, .host .av { position: absolute; left: 0; top: 0; width: 100%; height: 100%; }
.tile .ab { position: absolute; left: 92px; top: 12px; height: 24px; padding: 0 8px; border-radius: 6px; display: flex; align-items: center;
  font-family: "Archivo"; font-weight: 800; font-stretch: 110%; font-size: 14px; letter-spacing: 0.06em; background: var(--c1); color: var(--ct);
  box-shadow: inset 0 0 0 1.5px var(--c2); }
.tile .nm { position: absolute; left: 92px; top: 41px; width: 262px; font-family: "Archivo"; font-weight: 700; color: var(--off); white-space: nowrap; overflow: hidden; }
.tile .md { position: absolute; left: 150px; top: 14px; width: 206px; font-family: "Questrial"; font-size: 15px; color: #aeb8de; white-space: nowrap; overflow: hidden; text-align: right; }
.tile .clk { position: absolute; right: 8px; top: 11px; height: 22px; padding: 0 7px; border-radius: 5px; background: var(--red);
  font-family: "Archivo"; font-weight: 800; font-size: 12px; letter-spacing: 0.08em; color: #fff; display: flex; align-items: center; opacity: 0; }
.tile[data-clock="1"] .clk { opacity: 1; }
.tile[data-clock="1"] .md { opacity: 0; }
.tile[data-on="1"] { border: 3px solid var(--red); background: rgba(227, 36, 2, 0.16); box-shadow: 0 0 26px rgba(227, 36, 2, 0.45); }

/* avatar frames: exactly one state visible; states are swapped by GSAP attr sets on .av[data-m] */
.av .f { position: absolute; left: 0; top: 0; width: 100%; height: 100%; opacity: 0; }
.av[data-m="closed"] .f-closed, .av[data-m="mid"] .f-mid, .av[data-m="open"] .f-open, .av[data-m="blink"] .f-blink,
.av[data-m="mid_blink"] .f-mid_blink, .av[data-m="open_blink"] .f-open_blink { opacity: 1; }
.av .p { position: absolute; left: 0; top: 0; width: 100%; height: 100%; opacity: 0; }
__POSE_RULES__

/* host box (board beats; the host is BIG on speaker beats) */
.host { position: absolute; z-index: 4; left: 48px; top: 788px; width: 356px; height: 212px; }
.host .hav { position: absolute; left: 12px; top: 12px; width: 186px; height: 186px; border-radius: 16px; overflow: hidden;
  box-shadow: 0 0 0 3px var(--royal); background: #0E1B4D; }
.host .who { position: absolute; left: 212px; top: 40px; width: 136px; }
.host .who .k { font-family: "Questrial"; font-size: 14px; letter-spacing: 0.24em; color: #b9c3e8; }
.host .who .n { margin-top: 6px; font-family: "Archivo"; font-weight: 800; font-stretch: 100%; font-size: 19px; line-height: 1.15; color: var(--off); }
.host .who .n2 { font-family: "Questrial"; font-size: 15px; color: #aeb8de; margin-top: 10px; line-height: 1.3; }
.host[data-on="1"] { border: 3px solid var(--red); box-shadow: 0 0 26px rgba(227, 36, 2, 0.45); }
.host[data-on="1"] .hav { box-shadow: 0 0 0 3px var(--red); }

/* studio layer (hidden while a full-screen card is opaque) */
.set { position: absolute; inset: 0; z-index: 3; }
/* captions: the panel boxes live in each layout; the text layer floats above everything */
.capbox { position: absolute; z-index: 4; }
.capbox.b { left: 428px; top: 788px; width: 1444px; height: 212px; }
.capbox.s { left: 952px; top: 700px; width: 920px; height: 276px; }
.caps { position: absolute; z-index: 35; left: 0; top: 0; width: 1920px; height: 1080px; pointer-events: none; }
.mc { position: absolute; box-sizing: border-box; }
.mc.b { left: 428px; top: 788px; width: 1444px; height: 212px; padding: 16px 32px 0; }
.mc.s { left: 952px; top: 700px; width: 920px; height: 276px; padding: 20px 34px 0; }
.mc .who { height: 30px; display: flex; align-items: center; gap: 10px; }
.mc .who .sw { width: 12px; height: 26px; border-radius: 3px; background: var(--c2); box-shadow: inset 0 0 0 2px var(--c1); }
.mc .who .t { font-family: "Archivo"; font-weight: 800; font-stretch: 108%; letter-spacing: 0.08em; color: #c9cfe6; white-space: nowrap; }
.mc .rows { margin-top: 14px; }
.mc .r { font-family: "Archivo"; font-weight: 800; white-space: nowrap; color: #EFF0F5; text-shadow: 0 3px 0 rgba(0,0,0,0.45); }
.mc .w { display: inline-block; transform-origin: 50% 80%; }

/* camera (zoom punches + shake), name slams, impact flash, confetti */
.cam { position: absolute; left: 0; top: 0; width: 1920px; height: 1080px; }
.slam { position: absolute; z-index: 32; left: 0; height: 330px; text-align: center; pointer-events: none; }
.slam .st { font-family: "Archivo"; font-weight: 900; font-stretch: 125%; line-height: 1; letter-spacing: 0.01em; white-space: nowrap;
  -webkit-text-stroke: 5px #0E1B4D; paint-order: stroke fill;
  text-shadow: 0 10px 0 var(--c1, #0E1B4D), 0 0 60px rgba(227, 36, 2, 0.55); }
.slam .ss { margin-top: 18px; display: inline-block; padding: 8px 22px; border-radius: 12px; background: #E32402; color: #fff;
  font-family: "Archivo"; font-weight: 800; font-stretch: 112%; font-size: 30px; letter-spacing: 0.12em; }
.flash { position: absolute; z-index: 33; left: 0; top: 0; width: 1920px; height: 1080px; background: #fff; opacity: 0; visibility: hidden; }
.burst { position: absolute; z-index: 31; width: 0; height: 0; }
.burst i { position: absolute; left: 0; top: 0; display: block; }
/* ticker */
.ticker { position: absolute; z-index: 6; left: 0; top: 1016px; width: 1920px; height: 64px; background: rgba(5, 10, 34, 0.92);
  border-top: 2px solid var(--red); }
.ticker .lab { position: absolute; left: 0; top: 0; width: 236px; height: 62px; background: var(--red); display: flex; align-items: center;
  justify-content: center; font-family: "Archivo"; font-weight: 900; font-stretch: 118%; font-size: 21px; letter-spacing: 0.08em; color: #fff; }
.ticker .view { position: absolute; left: 236px; top: 0; width: 1684px; height: 62px; overflow: hidden; }
.ticker .strip { position: absolute; left: 0; top: 0; height: 62px; }
.te { position: absolute; top: 0; width: 384px; height: 62px; display: flex; align-items: center; gap: 10px; padding: 0 14px;
  box-sizing: border-box; border-right: 1px solid rgba(239,240,245,0.10); background: #060c26; }
.te > div { flex: none; }
.te .no { font-family: "Archivo"; font-weight: 800; font-size: 15px; color: #9fa9cf; width: 56px; }
.ticker .hint { position: absolute; left: 28px; top: 0; height: 62px; display: flex; align-items: center; font-family: "Questrial";
  font-size: 18px; letter-spacing: 0.16em; color: #7f8bb8; text-transform: uppercase; }
.te .ab { height: 26px; padding: 0 7px; border-radius: 6px; display: flex; align-items: center; font-family: "Archivo"; font-weight: 800;
  font-stretch: 110%; font-size: 14px; letter-spacing: 0.05em; background: var(--c1); color: var(--ct); box-shadow: inset 0 0 0 1.5px var(--c2); }
.te .pl { font-family: "Archivo"; font-weight: 700; font-size: 17px; color: var(--off); white-space: nowrap; }
.te .ps { font-family: "Questrial"; font-size: 15px; color: #aeb8de; white-space: nowrap; }

/* full-frame overlays */
.ovl { position: absolute; z-index: 30; inset: 0; overflow: hidden; }
.ovl .bg2 { position: absolute; inset: 0; background:
    radial-gradient(900px 520px at 50% 45%, rgba(71,112,219,0.30), transparent 70%),
    repeating-linear-gradient(115deg, rgba(239,240,245,0.035) 0 2px, transparent 2px 24px),
    #0E1B4D; }
.ovl .blk { position: absolute; left: 0; top: 150px; width: 1920px; text-align: center; }
.ovl .k1 { font-family: "Questrial"; font-size: 28px; letter-spacing: 0.42em; color: #c9cfe6; }
.ovl .ttl { margin-top: 18px; font-family: "Archivo"; font-weight: 900; font-stretch: 125%; font-size: 172px; line-height: 1; letter-spacing: 0.01em; color: var(--off); }
.ovl .bar { margin: 34px auto 30px; width: 180px; height: 10px; border-radius: 5px; background: var(--red); }
.ovl .l2 { font-family: "Archivo"; font-weight: 800; font-stretch: 112%; font-size: 34px; letter-spacing: 0.08em; color: var(--off); }
.ovl .l3 { margin-top: 18px; font-family: "Questrial"; font-size: 26px; letter-spacing: 0.2em; color: #c9cfe6; text-transform: uppercase; }
.ovl .l4 { position: absolute; left: 0; width: 1920px; top: 1000px; text-align: center; font-family: "Questrial"; font-size: 20px;
  letter-spacing: 0.08em; color: #aeb8de; }
"""


def pose_rules() -> str:
    talk = ("open", "mid", "open_blink", "mid_blink")
    shut = ("closed", "blink")
    out = []
    for pz in ("hype", "point", "shock", "celebrate"):
        out.append(f'.av.has-{pz}[data-p="{pz}"] .f {{ visibility: hidden; }}')
        out.append(", ".join(f'.av.has-{pz}[data-p="{pz}"][data-m="{m}"] .p-{pz}-open' for m in talk) + " { opacity: 1; }")
        out.append(", ".join(f'.av.has-{pz}[data-p="{pz}"][data-m="{m}"] .p-{pz}-closed' for m in shut) + " { opacity: 1; }")
    return "\n".join(out)


def css(asset_prefix: str) -> str:
    return CSS.replace("{A}", asset_prefix).replace("__POSE_RULES__", pose_rules())
