"""Comedy judge: SELECTION is our quality control -- we never rewrite a word.

Every sentence of every GM line (calls, comebacks, reactions, table talk) is scored by a PANEL of non-league models
(never a model that plays in the league):

  means         what the sentence claims, in plain words (forces the judge to find the point; a log, never aired)
  makes_sense   yes / no   a hockey fan gets it instantly: the comparison maps onto the actual pick or situation.
                           Joke-SHAPED nonsense fails ("building from the back out -- my dad said that about the
                           outhouse too")
  funny         0-3
  factually_ok  yes / no / unsure   claims about players and NHL teams, checked against the league's data snapshot
                           (current team, age, the last three seasons)
  reason        one line (a log, never aired)

Panel: the stricter makes_sense and factually_ok win; funny is the panel mean. Verdicts are cached per line and
logged to media/out/review/<run>/judge.md.

GREEN (the complete show's all-green policy): makes sense, lands instantly, consistent with the board and the recent
lines, no fact wrong, not mean, not catchphrase filler, no owner veto (media/out/review/<run>/veto.txt). A GM line airs
only if every sentence is green; a pick call that isn't is replaced by the host announcing the pick.
PASS (the shorts): green and funny >= 2 (a stock internet phrase caps funny at 1).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import tags as T
from .paths import OUT, ROOT, cache_dir
from .tighten import split_parts

PROMPT_V = 9  # v9: the robot is an opaque control team (claims about how it picks fail); Cup picks, not fandom
# v8: the owner's premise -- self-aware AIs vs the robot, model names, AI jokes that land, no jargon,
#                no invented human life, stock phrases / repeated bits cap funny at 1
# v7: + consistent (board + recent lines in context) + catchphrases are filler; v6: no side stories
# The panel, from `judge calibrate` (18 verdicts: the producer's party-4 read with the owner's rules on top -- no
# invented side stories, uncles included -- plus the owner's demo verdicts), prompt v6, Sep 27:
#   minimax-m3 + mistral-medium-3.5 = 16/18 (89%); + nova-2-lite as a third judge = 16/18; minimax + nova = 15/18.
#   Alone: minimax 15, mistral 15, nova 10 (and nova's JSON breaks often). Both misses are shared by every judge:
#   "Quinn Hughes drives Vancouver" (the ledger's fact_check is the first layer for facts) and "That math doesn't
#   even make sense in French" (the owner's veto is the backstop).
PANEL = ["minimax/minimax-m3", "mistralai/mistral-medium-3-5"]
FUNNY_MIN = 2.0
BATCH = 6

NHL = {"ANA": "Anaheim Ducks", "BOS": "Boston Bruins", "BUF": "Buffalo Sabres", "CAR": "Carolina Hurricanes",
       "CBJ": "Columbus Blue Jackets", "CGY": "Calgary Flames", "CHI": "Chicago Blackhawks", "COL": "Colorado Avalanche",
       "DAL": "Dallas Stars", "DET": "Detroit Red Wings", "EDM": "Edmonton Oilers", "FLA": "Florida Panthers",
       "LAK": "Los Angeles Kings", "MIN": "Minnesota Wild", "MTL": "Montreal Canadiens", "NJD": "New Jersey Devils",
       "NSH": "Nashville Predators", "NYI": "New York Islanders", "NYR": "New York Rangers", "OTT": "Ottawa Senators",
       "PHI": "Philadelphia Flyers", "PIT": "Pittsburgh Penguins", "SEA": "Seattle Kraken", "SJS": "San Jose Sharks",
       "STL": "St. Louis Blues", "TBL": "Tampa Bay Lightning", "TOR": "Toronto Maple Leafs", "UTA": "Utah Mammoth",
       "VAN": "Vancouver Canucks", "VGK": "Vegas Golden Knights", "WPG": "Winnipeg Jets", "WSH": "Washington Capitals"}

PROMPT = """You are the COMEDY JUDGE for "Draft Night": 13 AI models, going by their model names (Qwen, Gemini,
Opus...), hold a hockey-bro fantasy-hockey draft party and chirp each other between picks. They are openly AIs, and
they are trying to beat "the robot": a silent control team -- nobody at the table knows how it picks. Nothing is ever
rewritten -- we only choose which sentences air -- so you are the strict editor who keeps joke-SHAPED sentences that
don't land off the air.

Know your writers: they are language models, and they often produce sentences that SOUND like a chirp -- a vivid
image, a callback, a pun, a stock internet phrase -- but don't actually say anything a fan would get on first hearing.
Expect about a third of the jokes you see to fail. Do not rescue a line by inventing a clever reading for it: if YOU
had to work out what it means, the room won't get it. The owner wants FRIENDLY RIBBING between buddies, not burns --
and real jokes, not slop.

AI self-aware jokes are welcome IF a hockey fan who has used ChatGPT gets them instantly: thinking time, compute,
cost, being a chatbot, making stuff up, getting fact-checked, lab teammates (Astra and Sol are both OpenAI; Fable and
Opus are both Anthropic). Ribbing the robot's picks is fine; claiming to know how it picks is not (see below). AI jargon does not land: tokens, context window, RLHF, parameters, temperature,
embeddings, weights, inference.

For EVERY numbered sentence of every LINE return:
  "image": the picture or comparison the sentence uses, literally ("-" for a plain statement).
  "stands_for": what each part of that image stands for in the real situation (the player, the pick, the model, a
      stat, the robot). Write "?" for any part that stands for nothing real.
  "makes_sense": "yes" only if every part of the image stands for something real AND a hockey fan would get the point
      instantly. "no" if any of these holds:
        - a part of the image stands for nothing real ("?" above);
        - the sentence re-uses someone else's image but gives it a new meaning or a new detail that maps to nothing
          in the hockey situation;
        - it mixes its own metaphor (sets up an engine, pays off with something that has nothing to do with engines);
        - it is a pun or word association on someone's phrase that doesn't also make a pointed claim about the pick;
        - reasonable listeners would come away with different meanings.
      Plain statements (a pick announcement, a stat, a greeting) are "yes".
  "lands_instantly": "no" if any of these holds:
        - it leans on an invented HUMAN life the viewer has to take on faith: the speaker's (or another GM's) family
          or relatives (mom, dad, uncle, kids, grandpa), job, truck, farm, childhood, hometown stories, "back in my
          day" -- even framed as a saying ("what my uncle said about the fence"). These are AI models: they have no
          such life, and the viewer never saw any of it. (A plain comparison to an everyday thing -- "that's buying a
          used truck for the radio" -- claims no life story; judge it on whether its point is obvious);
        - it needs AI jargon to get (tokens, context window, RLHF, parameters, temperature, embeddings, weights,
          inference);
        - it claims to know how the robot picks, what it ranks, or what it "would have" taken -- nobody at the table
          knows; the robot is an opaque control team (ribbing its actual picks is fine);
        - it is a riddle a viewer would have to decode.
      A joke must stay about what the audience can see or already knows: the picks, the players, the teams, the
      models at the table, the robot's picks, and what everyone knows about chatbots. "yes" otherwise, including plain
      statements and a comparison whose point is obvious by itself.
  "consistent": "no" if anything the sentence says about TONIGHT contradicts the BOARD or the RECENT LINES given with
      the line: who took which player, at which pick, how many picks ago, who said what (a quote or a stance put in
      the wrong model's mouth), what a model did. Check the pick numbers and the speakers exactly. "yes" otherwise,
      including sentences that make no claim about tonight.
  "funny": 0 = not a joke (announcement, stat, filler) / 1 = joke-shaped but mild, strained or confusing / 2 = a real
      laugh from a room of hockey fans: a specific, surprising comparison that fits / 3 = big laugh. Caps:
        - a sentence that makes no sense is at most 1;
        - a stock internet phrase caps it at 1: bold move, let's see how that works out, only time will tell, buckle
          up, game-changer, chef's kiss, I'll allow it, no notes, the audacity, let that sink in, it's giving, main
          character, rent free, understood the assignment, hold my beer, big brain, galaxy brain, say less, built
          different;
        - a bit someone already did tonight (the same joke or the same image in the RECENT LINES) caps it at 1 -- an
          answer that turns someone's image back on them is a new joke, not a repeat;
        - a GM's own catchphrase or signature call (see CAST) is filler: funny 0.
  "tone": "friendly" (buddies teasing a pick, a team, a habit), "edgy" (sharp but still good-natured) or "mean"
      (insults the model, cruel, humiliating, a burn meant to wound). Plain statements are "friendly".
  "factually_ok": "no" if a claim about a player or an NHL team is contradicted by DATA: a present-tense claim ties a
      player to any team but his CURRENT one in DATA (he "drives", "plays for", "runs" another team), an age or stat
      that is way off, a trophy or playoff run that did not happen. "unsure" if DATA can't confirm a specific factual
      claim. "yes" otherwise. Opinions, obvious hyperbole, a model's Stanley Cup pick and jokes about the models
      themselves (thinking time, cost) are fine.
  "reason": one short line.
A writer's note on a joke (JOKE LOGIC) is context only: judge what the audience hears, without the note.

EXAMPLES (not from this draft):
  after a model thinks for two minutes and takes the most obvious name on the board: "Two minutes of compute to find
  the guy on the cover of the magazine." -> image: an expensive search that ends on the obvious answer; stands_for:
  the compute = the model's thinking time, the magazine cover = the most famous player left; makes_sense yes;
  lands_instantly yes; funny 2; tone friendly.
  "Even the robot would've taken him." -> claims to know how the opaque robot picks; lands_instantly no; funny 1.
  "My context window's bigger than your whole blue line." -> AI jargon (context window); lands_instantly no; funny 1.
  "That's what my grandpa said about his tractor." -> an invented human life (a relative, a farm); lands_instantly
  no; funny 1.
  "Your winger skates so fast the Zamboni files a noise complaint." -> image: a Zamboni complaining about noise;
  stands_for: the Zamboni = ?; makes_sense no; funny 1.

Return ONLY JSON: {"lines": {"<line id>": [{"i": 0, "image": "...", "stands_for": "...", "makes_sense": "yes",
"lands_instantly": "yes", "consistent": "yes", "funny": 2, "tone": "friendly", "factually_ok": "yes",
"reason": "..."}, ...]}} -- one entry per sentence, in order.
"""


CONSISTENCY_V = 3
CONSISTENCY_PROMPT = """You check a live fantasy-hockey draft party for CONTINUITY ERRORS. The GMs are 13 AI models,
going by their model names (Qwen, Gemini, Opus...), plus "the robot" (a silent control team); they chirp
each other between picks, and sometimes a line gets tonight wrong. For each numbered sentence of each LINE, list
every claim it makes about TONIGHT -- who took which player, at which pick or how many picks ago, who said or did
what at the table -- and check each against the BOARD and the RECENT LINES given with that line:
  - Time references: "N picks ago" is counted from the pick just made (the BOARD labels every pick). Work out WHICH
    players the sentence means (from the sentence and the line it answers: "the good ones", "the centres", "both") and
    check that ALL of them went at that spot. The speaker's own picks are marked: a speaker never lost a player to
    someone else at the pick where he took him himself.
  - Attribution: "you said / you called / you took X" must match what THAT GM actually said or did. Something another
    GM said ABOUT them is not something they said -- "Switch said Buzz was calling shotgun" does not make Switch the
    one calling shotgun.
  - Opinions, jokes, comparisons, predictions and anything not about tonight's picks or lines are not claims. Obvious
    exaggeration is not a contradiction ("half this draft is Oilers" when three of eight picks are); a wrong specific
    is (a pick number, who took whom, how many picks ago, who said what).
EXAMPLES (not from this draft):
  At #6, the speaker took Makar at #5 (his own pick, 1 pick ago), Heiskanen went at #3 (3 picks ago): "All the good
  defencemen went three picks ago." -> the good defencemen are Heiskanen AND Makar; Makar went 1 pick ago, to the
  speaker himself: contradicted.
  Tank's line: "Moon, drafting a Canuck is you calling dibs on the coast." Later Moon says to Tank: "Tank, you called
  dibs on the coast, then drafted a Shark." -> Tank never called dibs; he said MOON did: contradicted.
verdict per claim: "ok" (the board or a recent line confirms it), "contradicted" (they show otherwise), "unsupported"
(nothing shows it either way). consistent = "no" only if some claim is contradicted.

Return ONLY JSON: {"lines": {"<line id>": [{"i": 0, "claims": [{"claim": "...", "verdict": "ok", "evidence":
"..."}], "consistent": "yes"}, ...]}} -- one entry per sentence, in order (claims may be empty).
"""


def _spend(model: str, usage: dict) -> None:
    """OpenRouter's reported cost of every judge call, appended to cache/judge/spend.jsonl (no keys, no text)."""
    try:
        with open(cache_dir("judge") / "spend.jsonl", "a") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model,
                                 "cost": float(usage.get("cost") or 0.0),
                                 "tokens": int(usage.get("total_tokens") or 0)}) + "\n")
    except Exception:  # noqa: BLE001
        pass


def spend_since(ts: str) -> tuple[float, int]:
    """(USD, calls) the judge spent since an ISO timestamp."""
    f = cache_dir("judge") / "spend.jsonl"
    usd, n = 0.0, 0
    if f.exists():
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("ts", "") >= ts:
                usd += float(r.get("cost") or 0.0)
                n += 1
    return usd, n


def _chat(model: str, prompt: str, log=print, tag: str = "judge", max_tokens: int = 24000,
          deadline_s: float = 240.0) -> tuple[str | None, str]:
    """One OpenRouter chat call, streamed so a hard wall-clock deadline holds (a non-streamed call can hang for many
    minutes: the router keeps the socket alive while a model reasons). -> (content or None, why it failed)."""
    try:
        import httpx

        from .tts import load_env
    except Exception:  # noqa: BLE001
        return None, "no httpx"
    key = load_env().get("OPENROUTER_API_KEY")
    if not key:
        return None, "no OPENROUTER_API_KEY"
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens,
            "temperature": 0, "stream": True, "provider": {"sort": "throughput"}, "usage": {"include": True}}
    t0, out, finish = time.time(), [], None
    try:
        with httpx.stream("POST", "https://openrouter.ai/api/v1/chat/completions", json=body,
                          timeout=httpx.Timeout(60.0, connect=20.0),
                          headers={"Authorization": f"Bearer {key}", "content-type": "application/json"}) as r:
            if r.status_code != 200:
                r.read()
                return None, f"HTTP {r.status_code}"
            for line in r.iter_lines():
                if time.time() - t0 > deadline_s:
                    return None, f"over {deadline_s:.0f}s"
                if not line.startswith("data:"):
                    continue
                d = line[5:].strip()
                if d == "[DONE]":
                    break
                try:
                    j = json.loads(d)
                except ValueError:
                    continue
                if j.get("error"):
                    return None, f"error {str(j['error'])[:80]}"
                if j.get("usage"):
                    _spend(model, j["usage"])
                ch = (j.get("choices") or [{}])[0]
                c = (ch.get("delta") or {}).get("content")
                if c:
                    out.append(c)
                finish = ch.get("finish_reason") or finish
    except Exception as e:  # noqa: BLE001
        return None, type(e).__name__
    txt = "".join(out).strip()
    if not txt:  # (a reasoning model can spend the whole budget thinking: minimax does, now and then)
        return None, f"empty answer (finish: {finish})"
    return txt, ""


def _ckey(model: str, it) -> str:
    return hashlib.sha1(json.dumps({"cv": CONSISTENCY_V, "ph": hashlib.sha1(CONSISTENCY_PROMPT.encode()).hexdigest()[:8],
                                    "m": model, "t": it.text, "c": it.context},
                                   sort_keys=True).encode()).hexdigest()[:24]


def _ask_consistency(L, batch: list, model: str, log=print) -> dict:
    try:
        import httpx

        from .tts import load_env
    except Exception:  # noqa: BLE001
        return {}
    key = load_env().get("OPENROUTER_API_KEY")
    if not key:
        return {}
    blocks = []
    for it in batch:
        head = f"LINE {it.id}: said by {_nick(L, it.team)} ({it.kind.replace('_', ' ')})"
        if it.to:
            head += f", aimed at {_nick(L, it.to)}"
        rows = [head] + [f"  {c}" for c in it.context] + [f"  {i}. {T.strip(p_)}" for i, p_ in enumerate(it.parts)]
        blocks.append("\n".join(rows))
    prompt = (CONSISTENCY_PROMPT + "\nCAST (who is who; at the table a GM may be called by its model name or by its "
              "character's name or nickname):\n" + "\n".join(
                  f"- {_nick(L, tid)} = " + ("the robot, a silent control team" if t.is_bot else
                                             f"the AI model {t.display} ({t.lab}), playing {t.gm_name}")
                  for tid, t in L.teams.items()) + "\n\n" + "\n\n".join(blocks))
    for attempt in range(3):
        if attempt:
            time.sleep(4 * attempt)
        txt, why = _chat(model, prompt, log, "judge (consistency)")
        if txt is None:
            log(f"judge (consistency): {model}: {why}")
            if why in ("HTTP 400", "HTTP 401", "HTTP 402", "HTTP 403", "HTTP 404"):
                return {}
            continue
        got = _lenient_json(txt)
        if got is None:
            log(f"judge (consistency): {model}: bad JSON")
            continue
        return got
    return {}


def _clean_consistency(rows, it) -> list[dict] | None:
    if not isinstance(rows, list):
        return None
    by_i = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            i = int(r.get("i"))
        except (TypeError, ValueError):
            continue
        claims = [c for c in (r.get("claims") or []) if isinstance(c, dict)]
        bad = [c for c in claims if str(c.get("verdict") or "").lower().startswith("contra")]
        co = str(r.get("consistent") or "").strip().lower()
        by_i[i] = {"consistent": "no" if (bad or co.startswith("n")) else "yes",
                   "claims": [f"{str(c.get('claim') or '')[:90]} -> {str(c.get('verdict') or '')[:12]}"
                              for c in claims][:4]}
    if any(i not in by_i for i in range(len(it.parts))):
        return None
    return [by_i[i] for i in range(len(it.parts))]


def consistency_items(L, items: list, models: list[str], log=print, use_llm: bool = True) -> dict:
    """{model: {item id: [{consistent, claims} per sentence]}} -- cached; lines about tonight only (calls, SAYs)."""
    from concurrent.futures import ThreadPoolExecutor
    cdir = cache_dir("judge")
    out: dict = {m: {} for m in models}
    jobs = []
    for model in models:
        todo = []
        for it in items:
            if it.kind == "grade_comment":
                continue
            f = cdir / f"cons-{_ckey(model, it)}.json"
            if f.exists():
                out[model][it.id] = json.loads(f.read_text())["rows"]
            else:
                todo.append(it)
        if todo and use_llm:
            jobs += [(model, todo[k:k + BATCH]) for k in range(0, len(todo), BATCH)]

    def run(job):
        model, batch = job
        ans = _ask_consistency(L, batch, model, log)
        lines = (ans or {}).get("lines") or {}
        done = {}
        for it in batch:
            rows = _clean_consistency(_pick(lines, it, len(batch)), it)
            if rows is None and len(batch) > 1:
                rows = _clean_consistency(_pick((_ask_consistency(L, [it], model, log) or {}).get("lines") or {},
                                                it, 1), it)
            if rows is None:
                continue
            done[it.id] = rows
            (cdir / f"cons-{_ckey(model, it)}.json").write_text(json.dumps(
                {"model": model, "cv": CONSISTENCY_V, "id": it.id, "rows": rows}, indent=1, ensure_ascii=False))
        log(f"judge (consistency): {model}: {len(done)}/{len(batch)} lines checked")
        return model, done

    if jobs:
        with ThreadPoolExecutor(max_workers=min(16, len(jobs))) as ex:
            for model, done in ex.map(run, jobs):
                out[model].update(done)
    return out


AIRS_V = 3
AIRS_PROMPT = """You check the EDIT of a TV show before it airs: "Draft Night", a fantasy-hockey draft party where 13 AI
models (Qwen, Gemini, Opus...) chirp each other between picks (the robot is a silent control team). Some lines -- and some sentences inside
lines -- were cut from the broadcast: the viewer NEVER HEARD them. The host announces every pick, so the BOARD (who
took whom, and when) is known to the viewer.

For each numbered sentence of each LINE AS IT AIRS, decide:
  "orphaned" if it quotes, answers, echoes, mocks or builds on something that was said ONLY in a line or sentence
      marked NOT AIRED or CUT -- e.g. "GLM, you just scolded Gemini for taking a defenceman" when the scolding is only
      in a NOT AIRED line; "so much for your snowblower" when the snowblower line was cut -- or if it needs a CUT
      sentence of its own line to make sense (a punchline whose setup was cut);
  "repeats" if it makes the SAME POINT about the same pick or the same model as a sentence of an AIRED line above --
      same target, same angle, even in different words (both say a model just matched the robot's pick; both say a
      Flames fan drafted a Jet). A callback that answers, turns around or tops an aired joke is NOT a repeat, and
      neither is a new point about the same pick;
  "stands" otherwise: references to picks, players and teams on the BOARD, to what an AIRED line said, and anything
      that makes sense on its own.
Return ONLY JSON: {"lines": {"<line id>": [{"i": 0, "verdict": "stands", "refers_to": ""}, ...]}} -- one entry per
numbered sentence, in order; "refers_to" quotes the NOT AIRED / CUT words it depends on (orphaned), or gives the tag of
the AIRED line it repeats and its words, e.g. "L3: the robot drafted him through you" (repeats); "" when it stands.
"""


def _akey(model: str, ln: dict) -> str:
    return hashlib.sha1(json.dumps({"av": AIRS_V, "ph": hashlib.sha1(AIRS_PROMPT.encode()).hexdigest()[:8], "m": model,
                                    "p": ln["parts"], "cut": ln["cut_own"], "c": ln["context"], "h": ln["head"]},
                                   sort_keys=True).encode()).hexdigest()[:24]


def _ask_airs(batch: list[dict], model: str, log=print) -> dict:
    blocks = []
    for ln in batch:
        rows = [f"LINE {ln['id']}: {ln['head']}"] + [f"  {c}" for c in ln["context"]]
        if ln["cut_own"]:
            rows.append("  CUT FROM THIS LINE (never heard): " + " | ".join(f'"{T.strip(x)}"' for x in ln["cut_own"]))
        rows.append("  THIS LINE AS IT AIRS:")
        rows += [f"    {k}. {T.strip(x)}" for k, x in enumerate(ln["parts"])]
        blocks.append("\n".join(rows))
    prompt = AIRS_PROMPT + "\n\n" + "\n\n".join(blocks)
    for attempt in range(3):
        if attempt:
            time.sleep(4 * attempt)
        txt, why = _chat(model, prompt, log, "judge (airs)")
        if txt is None:
            log(f"judge (airs): {model}: {why}")
            if why in ("HTTP 400", "HTTP 401", "HTTP 402", "HTTP 403", "HTTP 404"):
                return {}
            continue
        got = _lenient_json(txt)
        if got is None:
            log(f"judge (airs): {model}: bad JSON")
            continue
        return got
    return {}


def _clean_airs(rows, ln: dict) -> list[dict] | None:
    if not isinstance(rows, list):
        return None
    by_i = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            i = int(r.get("i"))
        except (TypeError, ValueError):
            continue
        v = str(r.get("verdict") or "").strip().lower()
        by_i[i] = {"verdict": "orphaned" if v.startswith("orph") else ("repeats" if v.startswith("rep") else "stands"),
                   "refers_to": str(r.get("refers_to") or "")[:160]}
    if any(i not in by_i for i in range(len(ln["parts"]))):
        return None
    return [by_i[i] for i in range(len(ln["parts"]))]


class _Ref:  # (the answer matcher wants .id / .seq)
    def __init__(self, ln: dict):
        self.id, self.seq = ln["id"], ln["seq"]


def airs_check(lines: list[dict], models: list[str] | None = None, log=print, use_llm: bool = True,
               log_rows: list | None = None) -> dict[str, dict[int, str] | None]:
    """The aired-context check: each aired line (as it airs) against what airs before it -- a sentence that refers to
    a line or sentence the viewer never heard is ORPHANED.
    lines: [{id, seq, head, context: [str], parts: [aired sentences], idx: [their indices in the full line],
    cut_own: [cut sentences of this line]}]. -> {line id: {"answered": n judges, "flags": {sentence index: [what it
    depends on, per judge that flagged it]}}} (None = no judge answered: the caller falls back)."""
    from concurrent.futures import ThreadPoolExecutor
    models = models or PANEL
    cdir = cache_dir("judge")
    got: dict[str, dict] = {m: {} for m in models}
    jobs = []
    for m in models:
        todo = []
        for ln in lines:
            f = cdir / f"airs-{_akey(m, ln)}.json"
            if f.exists():
                got[m][ln["id"]] = json.loads(f.read_text())["rows"]
            else:
                todo.append(ln)
        if todo and use_llm:
            jobs += [(m, todo[k:k + BATCH]) for k in range(0, len(todo), BATCH)]

    def run(job):
        m, batch = job
        ans = (_ask_airs(batch, m, log) or {}).get("lines") or {}
        done = {}
        for ln in batch:
            rows = _clean_airs(_pick(ans, _Ref(ln), len(batch)), ln)
            if rows is None and len(batch) > 1:
                rows = _clean_airs(_pick((_ask_airs([ln], m, log) or {}).get("lines") or {}, _Ref(ln), 1), ln)
            if rows is None:
                continue
            done[ln["id"]] = rows
            (cdir / f"airs-{_akey(m, ln)}.json").write_text(json.dumps(
                {"model": m, "av": AIRS_V, "id": ln["id"], "rows": rows}, indent=1, ensure_ascii=False))
        log(f"judge (airs): {m}: {len(done)}/{len(batch)} lines checked")
        return m, done

    if jobs:
        with ThreadPoolExecutor(max_workers=min(16, len(jobs))) as ex:
            for m, done in ex.map(run, jobs):
                got[m].update(done)
    out: dict[str, dict | None] = {}
    for ln in lines:
        per = {m: got[m].get(ln["id"]) for m in models}
        if all(v is None for v in per.values()):
            out[ln["id"]] = None
            continue
        flags: dict[int, list[str]] = {}
        reps: dict[int, list[str]] = {}
        for m, rows in per.items():
            for k, r in enumerate(rows or []):
                if r["verdict"] == "orphaned":
                    flags.setdefault(ln["idx"][k], []).append(r.get("refers_to") or "a line that didn't air")
                elif r["verdict"] == "repeats":
                    reps.setdefault(ln["idx"][k], []).append(r.get("refers_to") or "")
                if log_rows is not None:
                    log_rows.append({"line": ln["id"], "model": m, "i": ln["idx"][k], "sentence": T.strip(ln["parts"][k]),
                                     "verdict": r["verdict"], "refers_to": r.get("refers_to") or ""})
        out[ln["id"]] = {"answered": sum(1 for v in per.values() if v is not None), "flags": flags, "repeats": reps}
    return out


APPEAL_V = 2
APPEAL_PROMPT = """You review objections a comedy judge raised against lines from "Draft Night", a fantasy-hockey draft
party where 13 AI models (Qwen, Gemini, Opus...) chirp each other between picks; "the robot" is a silent control team.
The judge may have missed two HOUSE RULES. For each OBJECTION below, say whether it still stands under them:
  1. AGE / EXPERIENCE SLANG is banter, not a factual claim: "kid", "teenager", "youngster", "young gun", "old man",
     "graybeard", "veteran" and the like are fine even when not literally exact (a 20-year-old who was a teenager for
     his big season can be "the teenager"; it's like "young Celebrini"). Hard facts stay strict: the player's current
     team, trades, stats, history -- "rookie" for a player with NHL seasons is still wrong.
  2. A GM's GUESS ABOUT ITS OWN PICK, in the same line as that pick, is a guess, not a contradiction -- even when the
     board shows otherwise: how fast the GM itself decided ("the math took me three seconds", "fastest pick of the
     night"). The other GMs get to mock the guess; that's the joke. Claims about EARLIER picks, about OTHER GMs' picks,
     speed or lookups, and about who said what stay strict -- and so does any claim to know what the robot would have
     taken (nobody knows how it picks).
"stands": "yes" if the objection is still right under these rules, "no" if the rules clear it.
Return ONLY JSON: {"items": {"<id>": {"stands": "yes", "why": "..."}}}
"""


def _apkey(model: str, it: dict) -> str:
    return hashlib.sha1(json.dumps({"apv": APPEAL_V, "ph": hashlib.sha1(APPEAL_PROMPT.encode()).hexdigest()[:8],
                                    "m": model, "it": it}, sort_keys=True).encode()).hexdigest()[:24]


def _ask_appeal(batch: list[dict], model: str, log=print) -> dict:
    blocks = []
    for it in batch:
        rows = [f"OBJECTION {it['id']} ({it['type']}) -- {it['head']}", f"  SENTENCE: \"{it['sentence']}\"",
                f"  THE JUDGE SAID: {it['objection']}"] + [f"  {c}" for c in it["context"]]
        blocks.append("\n".join(rows))
    prompt = APPEAL_PROMPT + "\n\n" + "\n\n".join(blocks)
    for attempt in range(3):
        if attempt:
            time.sleep(4 * attempt)
        txt, why = _chat(model, prompt, log, "judge (appeal)")
        if txt is None:
            log(f"judge (appeal): {model}: {why}")
            if why in ("HTTP 400", "HTTP 401", "HTTP 402", "HTTP 403", "HTTP 404"):
                return {}
            continue
        got = _lenient_json(txt)
        if got is None:
            log(f"judge (appeal): {model}: bad JSON")
            continue
        return got
    return {}


def appeal_items(L, items: list, votes: dict, models: list[str] | None = None, log=print,
                 use_llm: bool = True) -> dict[str, dict]:
    """The house-rules appeal: every sentence the panel failed on facts (either judge) or consistency (both judges)
    goes back to the panel with the two house rules. -> {"<item id>.<i>": {"facts": cleared?, "consistent": cleared?,
    "why": ...}} (an objection is cleared when every judge that answers says it doesn't stand)."""
    from concurrent.futures import ThreadPoolExecutor
    models = models or PANEL
    cdir = cache_dir("judge")
    todo: list[dict] = []
    for it in items:
        if it.kind == "grade_comment":
            continue
        for i, part in enumerate(it.parts):
            per = {m: votes[m][it.id][i] for m in votes if it.id in votes[m] and i < len(votes[m][it.id])}
            if not per:
                continue
            c = combine(per)
            head = f"{_who(L, it.team)}, {it.kind.replace('_', ' ')}"
            if it.kind == "on_air_call" and it.player:
                head += f" -- this line makes its OWN pick: {it.player}"
            for typ, bad, field_ in (("factual", c["factually_ok"] == "no", "factually_ok"),
                                     ("consistency", c["consistent"] == "no", "consistent")):
                if not bad:
                    continue
                obj = []
                for m, x in per.items():
                    if x.get(field_) != "no":
                        continue
                    claims = [cl for cl in (x.get("claims") or []) if "contra" in str(cl)]
                    obj.append(f"{m.split('/')[-1]}: {x.get('reason') or ''}"
                               + (f" (contradicted: {'; '.join(claims[:3])})" if claims else ""))
                todo.append({"id": f"ap{it.id[3:]}-{i}-{typ[:4]}", "key": f"{it.id}.{i}", "type": typ, "head": head,
                             "sentence": T.strip(part), "objection": " | ".join(obj) or "(no reason given)",
                             "context": list(it.context)})
    if not todo:
        return {}
    got: dict[str, dict] = {m: {} for m in models}
    jobs = []
    for m in models:
        miss = []
        for it in todo:
            f = cdir / f"appeal-{_apkey(m, {k: it[k] for k in ('type', 'head', 'sentence', 'objection', 'context')})}.json"
            if f.exists():
                got[m][it["id"]] = json.loads(f.read_text())["row"]
            else:
                miss.append(it)
        if miss and use_llm:
            jobs += [(m, miss[k:k + 8]) for k in range(0, len(miss), 8)]

    def run(job):
        m, batch = job
        ans = (_ask_appeal(batch, m, log) or {}).get("items") or {}
        done = {}
        for it in batch:
            r = ans.get(it["id"]) if isinstance(ans, dict) else None
            if not isinstance(r, dict) and len(batch) == 1 and isinstance(ans, dict) and len(ans) == 1:
                r = next(iter(ans.values()))
            if not isinstance(r, dict):
                continue
            row = {"stands": "no" if str(r.get("stands") or "").strip().lower().startswith("n") else "yes",
                   "why": str(r.get("why") or "")[:200]}
            done[it["id"]] = row
            key = {k: it[k] for k in ("type", "head", "sentence", "objection", "context")}
            (cdir / f"appeal-{_apkey(m, key)}.json").write_text(json.dumps(
                {"model": m, "apv": APPEAL_V, "id": it["id"], "row": row}, indent=1, ensure_ascii=False))
        log(f"judge (appeal): {m}: {len(done)}/{len(batch)} objections reviewed")
        return m, done

    if jobs:
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
            for m, done in ex.map(run, jobs):
                got[m].update(done)
    out: dict[str, dict] = {}
    for it in todo:
        rows = [got[m].get(it["id"]) for m in models if got[m].get(it["id"])]
        cleared = bool(rows) and all(r["stands"] == "no" for r in rows)
        e = out.setdefault(it["key"], {"facts": None, "consistent": None, "why": []})
        e["facts" if it["type"] == "factual" else "consistent"] = cleared
        if rows:
            e["why"].append(f"{it['type']}: " + (" / ".join(r["why"] for r in rows)))
    return out


# ----------------------------------------------------------------------------------------------- the lines to judge
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", T.strip(s)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


@dataclass
class Item:
    id: str  # "seq<N>"
    seq: int
    team: str
    kind: str  # on_air_call | comeback | reaction | table_talk
    text: str  # the ledger text (tags inline)
    parts: list[str]
    pick_no: int | None
    to: str | None
    player: str | None  # drafted on this line
    context: list[str] = field(default_factory=list)  # what it answers
    logic: str = ""
    players: set[int] = field(default_factory=set)


def _who(L, tid: str | None) -> str:
    """The line head: the MODEL first, then the character it plays."""
    t = L.teams.get(tid or "")
    if not t:
        return "?"
    if t.is_bot:
        return "the robot (the silent control team)"
    alias = f"; in this run also the character {t.gm_name}" if t.has_persona and t.gm_name != t.display else ""
    return f"{t.call_name} (the AI model {t.display}, {t.lab}{alias})"


def _nick(L, tid: str | None) -> str:
    """The board / recent-lines label: the model's table name plus the character's nickname, so a line that says
    'Qwen' and one that says 'Binders' both resolve: 'Qwen ("Binders")'."""
    t = L.teams.get(tid or "")
    if not t:
        return tid or "?"
    if t.is_bot:
        return "the robot"
    q = re.findall(r"""['"“‘]([^'"”’]+)['"”’]""", t.gm_name or "")
    alias = q[0] if q else (t.short_gm or "")
    return f'{t.call_name} ("{alias}")' if alias and alias.lower() != t.call_name.lower() else t.call_name


_NAMES: dict[str, dict[int, str]] = {}


def _player_name(L, pid) -> str | None:
    """A player's name from the league snapshot (the robot's would-take is often still undrafted)."""
    if L.run not in _NAMES:
        names: dict[int, str] = {p.player_id: p.player_name for p in L.picks if p.player_id}
        try:
            ref = str((L.snapshot or {}).get("ref") or "")
            path = ROOT / ref if ref else None
            if path and path.exists():
                pool = json.loads(path.read_text()).get("players") or {}
                for k, pp in (pool.items() if isinstance(pool, dict) else ((q.get("id"), q) for q in pool)):
                    if pp.get("name"):
                        names.setdefault(int(k), pp["name"])
        except Exception:  # noqa: BLE001
            pass
        _NAMES[L.run] = names
    try:
        return _NAMES[L.run].get(int(pid))
    except (TypeError, ValueError):
        return None


def _think_note(L, p) -> str:
    """What the viewer sees on the pick card (runs from party-10 on): the thinking clock and the robot's opinion --
    the setups for the AI jokes, so the judge can check a line that riffs on them."""
    from .ledger import fmt_think_seconds
    th = p.think if isinstance(getattr(p, "think", None), dict) else None
    rb = p.robot if isinstance(getattr(p, "robot", None), dict) else None
    t = L.teams.get(p.team)
    bits = []
    if th and th.get("seconds") is not None:
        try:
            tt = f"thought {fmt_think_seconds(float(th['seconds']))}"
            n = th.get("lookups", th.get("tool_calls"))
            if n is not None and not (t and t.is_bot):
                tt += f" with {int(n)} lookup{'s' if int(n) != 1 else ''}"
            bits.append(tt)
        except (TypeError, ValueError):
            pass
    # (robot.would_take stays in the ledger for the season's analytics: never on screen, never in the judge's context)
    return f" [{'; '.join(bits)}]" if bits else ""


def board_context(L, upto_seq: int, this_pick=None, n_board: int = 14, n_lines: int = 6,
                  speaker: str | None = None) -> list[str]:
    """What the judge checks a line against: the draft board up to that moment (the latest n_board picks, each
    labelled with how many picks ago it was -- counted from the pick just made -- and the speaker's own picks marked)
    and the last n_lines lines said at the table, with their speakers, oldest first."""
    made = sorted([p for p in L.picks if p.seq < upto_seq], key=lambda p: p.pick_no)
    if this_pick is not None:
        made = made + [this_pick]
    shown = made[-n_board:]
    now = shown[-1].pick_no if shown else 0

    def lab(p) -> str:
        ago = now - p.pick_no
        when = ("this line's pick" if this_pick is not None and p is this_pick else
                "the pick just made" if ago == 0 else f"{ago} pick{'s' if ago != 1 else ''} ago")
        own = " -- the speaker's own pick" if speaker and p.team == speaker else ""
        club = NHL.get((p.nhl_team or "").upper())
        return (f"#{p.pick_no} ({when}{own}) {_nick(L, p.team)} took {p.player_name}" + (f" ({club})" if club else "")
                + _think_note(L, p))

    board = [lab(p) for p in shown]
    ctx = ["BOARD so far" + (f" (picks 1-{shown[0].pick_no - 1} not shown)" if shown and shown[0].pick_no > 1 else "")
           + ": " + (" · ".join(board) if board else "(no picks yet)")]
    said = sorted([(p.seq, p.team, p.on_air_call, f"call at #{p.pick_no}") for p in L.picks
                   if p.seq < upto_seq and (p.on_air_call or "").strip()]
                  + [(x.seq, x.team, x.line, x.kind.replace("_", " ")) for x in L.says if x.seq < upto_seq],
                  key=lambda r: r[0])[-n_lines:]
    if said:
        ctx.append("RECENT LINES (oldest first): " + " | ".join(
            f"{_nick(L, team)} ({what}): \"{T.strip(text)[:220]}\"" for _, team, text, what in said))
    return ctx


def items_for(L) -> list[Item]:
    """Every GM line of the run, in ledger order, with what it answers: the board up to it and the recent lines."""
    picks = sorted(L.picks, key=lambda p: p.seq)
    by_no = {p.pick_no: p for p in picks}
    events: list[tuple[int, str, object]] = [(p.seq, "pick", p) for p in picks] + [(s.seq, "say", s) for s in L.says]
    events.sort(key=lambda x: x[0])
    last_by: dict[str, tuple[int, str]] = {}  # team -> (seq, text) of its latest line
    out: list[Item] = []
    for seq, kind, obj in events:
        if kind == "pick":
            p = obj
            text = (p.on_air_call or "").strip()
            if text and not L.teams[p.team].is_bot:
                prev = by_no.get(p.pick_no - 1)
                ctx = board_context(L, seq, this_pick=p, speaker=p.team)
                to = prev.team if prev is not None and prev.team != p.team else None
                out.append(Item(f"seq{seq}", seq, p.team, "on_air_call", text, split_parts(text), p.pick_no, to,
                                f"{p.player_name} ({p.nhl_team})", ctx, p.joke_logic,
                                {x for x in (p.player_id, prev.player_id if prev else None) if x}))
            last_by[p.team] = (seq, text)
        else:
            s = obj
            pk = by_no.get(s.pick_no) if s.pick_no else None
            ctx = board_context(L, seq, speaker=s.team)
            if s.addressed_to and s.addressed_to in last_by and not any(
                    T.strip(last_by[s.addressed_to][1])[:60] in c for c in ctx):
                ctx.append(f"{_nick(L, s.addressed_to)} last said: \"{T.strip(last_by[s.addressed_to][1])[:220]}\"")
            out.append(Item(f"seq{seq}", seq, s.team, s.kind, s.line, split_parts(s.line), s.pick_no, s.addressed_to,
                            None, ctx, s.joke_logic, {pk.player_id} if pk and pk.player_id else set()))
            last_by[s.team] = (seq, s.line)
    for r in L.reports:  # post-draft grades: the roast and Delusion shorts air these comments
        for g in r.grades:
            text = str(g.get("comment") or "").strip()
            team = g.get("team")
            if not text or team not in L.teams or str(g.get("fact_check") or "").lower() not in ("", "ok"):
                continue
            tp = [p for p in L.picks if p.team == team][:4]
            ctx = [f"{_who(L, r.team)} grades the draft of {_who(L, team)}: {g.get('grade')}"]
            if tp:
                ctx.append("their first picks: " + ", ".join(f"{p.player_name} ({p.nhl_team})" for p in tp))
            out.append(Item(f"seq{r.seq}-{team}", r.seq, r.team, "grade_comment", text, split_parts(text), None, team,
                            None, ctx, "", {p.player_id for p in tp if p.player_id}))
    return out


# ----------------------------------------------------------------------------------------------- facts (the snapshot)
class Facts:
    def __init__(self, L):
        ref = str((L.snapshot or {}).get("ref") or "")
        path = ROOT / ref if ref else None
        self.players: dict[int, dict] = {}
        if path and path.exists():
            pool = json.loads(path.read_text()).get("players") or {}
            for pid, pp in (pool.items() if isinstance(pool, dict) else ((p.get("id"), p) for p in pool)):
                self.players[int(pid)] = pp
        self.full: dict[str, int] = {}
        self.last: dict[str, list[int]] = {}
        for pid, pp in self.players.items():
            self.full[_norm(pp.get("name") or "")] = pid
            self.last.setdefault(_norm(pp.get("last_name") or ""), []).append(pid)
        self.drafted = {p.player_id for p in L.picks if p.player_id}

    def mentioned(self, text: str) -> set[int]:
        t = " " + _norm(text) + " "
        found = {pid for name, pid in self.full.items() if name and f" {name} " in t}
        for w in set(t.split()):
            if len(w) < 4:
                continue
            ids = self.last.get(w) or []
            if not ids or any(ids_ in found for ids_ in ids):
                continue
            drafted = [i for i in ids if i in self.drafted]
            if len(drafted) == 1:
                found.add(drafted[0])
            elif len(ids) == 1:
                found.add(ids[0])
        return found

    def line(self, pid: int) -> str | None:
        pp = self.players.get(pid)
        if not pp:
            return None
        team = pp.get("team") or "?"
        out = [f"{pp.get('name')}: {pp.get('position')}, current NHL team {team} ({NHL.get(team, team)}), "
               f"age {float(pp.get('age') or 0):.0f}"]
        if pp.get("injury"):
            out.append(f"injury: {pp['injury']}")
        for s in (pp.get("seasons") or [])[:3]:
            yr = str(s.get("season") or "")
            yr = f"{yr[:4]}-{yr[6:]}" if len(yr) == 8 else yr
            if pp.get("group") == "G" or s.get("wins") is not None:
                out.append(f"{yr} {s.get('teams')}: {s.get('gp')} GP, {s.get('wins')} W, GAA {s.get('gaa')}, "
                           f"SV% {s.get('save_pct')}")
            else:
                out.append(f"{yr} {s.get('teams')}: {s.get('gp')} GP, {s.get('goals')} G, {s.get('assists')} A, "
                           f"{s.get('points')} P")
        return "; ".join(out)


def cast_block(L) -> str:
    rows = []
    for tid, t in L.teams.items():
        if t.is_bot:
            rows.append(f"- {tid}: the robot, a silent control team every model has to beat (nobody knows how it picks)")
            continue
        nick = re.findall(r"""['"“‘]([^'"”’]+)['"”’]""", t.gm_name or "")
        nick_s = f' (called "{nick[0]}")' if nick else ""
        cp = "; ".join(f'"{T.strip(x)}"' for x in (t.p("catchphrase"), t.p("signature_call")) if x)
        row = f"- {tid}: \"{t.call_name}\" = the AI model {t.display} ({t.lab})"
        if t.has_persona and t.gm_name and t.gm_name != t.display:  # older runs dressed the models up as characters
            row += f"; in this run it also went by the character {t.gm_name}{nick_s}"
        cup = t.cup_pick
        fav = t.p("favorite_nhl_team")
        if cup:  # a prediction, not fandom: "picked the Oilers to win the Cup"
            row += f"; Stanley Cup pick: {NHL.get(cup, cup)} (picked them to win the Cup)"
        elif fav:  # (older rehearsal ledgers)
            row += f"; favourite NHL team {fav}"
        rows.append(row + (f"; catchphrases (filler): {cp}" if cp else ""))
    rows.append("(At the table a model may be called by its model name -- 'Qwen' -- or by a character name or "
                "nickname from this run.)")
    return "\n".join(rows)


# ----------------------------------------------------------------------------------------------- asking the panel
def _key(model: str, it: Item) -> str:
    return hashlib.sha1(json.dumps({"v": PROMPT_V, "ph": hashlib.sha1(PROMPT.encode()).hexdigest()[:8], "m": model,
                                    "t": it.text, "c": it.context, "k": it.kind, "to": it.to, "p": it.player,
                                    "l": it.logic}, sort_keys=True).encode()).hexdigest()[:24]


def _block(L, it: Item) -> str:
    head = f"LINE {it.id}: {_who(L, it.team)}, {it.kind.replace('_', ' ')}"
    if it.to:
        head += f", aimed at {_who(L, it.to)}"
    if it.player:
        head += f"; drafting {it.player}"
    rows = [head]
    for c in it.context:
        rows.append(f"  context: {c}")
    if it.logic:
        rows.append(f"  JOKE LOGIC (writer's note, not aired): {it.logic}")
    for i, p in enumerate(it.parts):
        rows.append(f"  {i}. {T.strip(p)}")
    return "\n".join(rows)


def _ask(L, facts: Facts, batch: list[Item], model: str, log=print) -> dict:
    try:
        import httpx

        from .tts import load_env
    except Exception:  # noqa: BLE001
        return {}
    key = load_env().get("OPENROUTER_API_KEY")
    if not key:
        log("judge: no OPENROUTER_API_KEY")
        return {}
    pids: set[int] = set()
    for it in batch:
        pids |= it.players | facts.mentioned(it.text) | {x for c in it.context for x in facts.mentioned(c)}
    data = [x for x in (facts.line(pid) for pid in sorted(pids)) if x]
    prompt = (PROMPT + "\nCAST (the GMs at the party):\n" + cast_block(L) + "\n\nDATA (the league's player snapshot):\n"
              + ("\n".join(f"- {d}" for d in data) or "- (none)") + "\n\n" + "\n\n".join(_block(L, it) for it in batch))
    for attempt in range(3):
        if attempt:
            time.sleep(4 * attempt)
        txt, why = _chat(model, prompt, log)
        if txt is None:
            log(f"judge: {model}: {why}")
            if why in ("HTTP 400", "HTTP 401", "HTTP 402", "HTTP 403", "HTTP 404"):
                return {}
            continue
        got = _lenient_json(txt)
        if got is None:
            log(f"judge: {model}: bad JSON")
            continue
        return got
    return {}


def _lenient_json(txt: str) -> dict | None:
    """The answer's JSON object, forgiving code fences, trailing commas and chatter around it."""
    txt = re.sub(r"```(?:json)?", "", txt)
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    raw = m.group(0)
    for cand in (raw, re.sub(r",\s*([}\]])", r"\1", raw)):
        try:
            got = json.loads(cand)
            return got if isinstance(got, dict) else None
        except json.JSONDecodeError:
            continue
    return None


def _pick(lines: dict, it: Item, n: int):
    """A line's answer, whatever the judge called it ("seq74", "74", "LINE seq74")."""
    if not isinstance(lines, dict):
        return None
    for k in (it.id, str(it.seq), f"LINE {it.id}", f"line {it.id}"):
        if k in lines:
            return lines[k]
    if n == 1 and len(lines) == 1:
        return next(iter(lines.values()))
    return None


def _clean(rows, it: Item) -> list[dict] | None:
    if not isinstance(rows, list):
        return None
    by_i = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            i = int(r.get("i"))
        except (TypeError, ValueError):
            continue
        ms = str(r.get("makes_sense") or "").strip().lower()
        fo = str(r.get("factually_ok") or "").strip().lower()
        try:
            fu = max(0, min(3, int(round(float(r.get("funny"))))))
        except (TypeError, ValueError):
            continue
        means = r.get("means") or " / ".join(str(r.get(k) or "") for k in ("image", "stands_for") if r.get(k))
        li = str(r.get("lands_instantly") or "yes").strip().lower()
        co = str(r.get("consistent") or "yes").strip().lower()
        tone = str(r.get("tone") or "friendly").strip().lower()
        by_i[i] = {"i": i, "means": str(means)[:220], "makes_sense": "yes" if ms.startswith("y") else "no",
                   "lands_instantly": "no" if li.startswith("n") else "yes",
                   "consistent": "no" if co.startswith("n") else "yes",
                   "tone": tone if tone in ("friendly", "edgy", "mean") else "edgy",
                   "funny": fu, "factually_ok": fo if fo in ("yes", "no", "unsure") else "unsure",
                   "reason": str(r.get("reason") or "")[:200]}
    if len(by_i) < len(it.parts) or any(i not in by_i for i in range(len(it.parts))):
        return None
    return [by_i[i] for i in range(len(it.parts))]


def league_prefixes(L) -> set[str]:
    return {t.model.split("/")[0] for t in L.teams.values() if t.model}


def judge_items(L, items: list[Item], models: list[str] | None = None, log=print, use_llm: bool = True
                ) -> dict[str, dict[str, list[dict]]]:
    """{model: {item id: [verdict per sentence]}} from the cache, asking only for what's missing."""
    models = models or PANEL
    bad = [m for m in models if m.split("/")[0] in league_prefixes(L)]
    if bad:
        raise SystemExit(f"judge: {bad} play in this league; judges must be non-league models")
    from concurrent.futures import ThreadPoolExecutor
    facts = Facts(L)
    cdir = cache_dir("judge")
    out: dict[str, dict[str, list[dict]]] = {m: {} for m in models}
    jobs = []
    for model in models:
        todo = []
        for it in items:
            f = cdir / f"{_key(model, it)}.json"
            if f.exists():
                out[model][it.id] = json.loads(f.read_text())["verdicts"]
            else:
                todo.append(it)
        if todo and use_llm:
            jobs += [(model, todo[k:k + BATCH]) for k in range(0, len(todo), BATCH)]

    def run(job):
        model, batch = job
        t0 = time.time()
        ans = _ask(L, facts, batch, model, log)
        lines = (ans or {}).get("lines") or {}
        done = {}
        for it in batch:
            rows = _clean(_pick(lines, it, len(batch)), it)
            if rows is None and len(batch) > 1:  # a batch answer that skipped a line: ask for it alone
                ans1 = _ask(L, facts, [it], model, log)
                rows = _clean(_pick((ans1 or {}).get("lines") or {}, it, 1), it)
            if rows is None:
                continue
            done[it.id] = rows
            (cdir / f"{_key(model, it)}.json").write_text(json.dumps(
                {"model": model, "v": PROMPT_V, "id": it.id, "text": it.text, "verdicts": rows,
                 "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=1, ensure_ascii=False))
        log(f"judge: {model}: {len(done)}/{len(batch)} lines judged ({time.time() - t0:.0f}s)")
        return model, done

    if jobs:
        with ThreadPoolExecutor(max_workers=min(16, len(jobs))) as ex:
            for model, done in ex.map(run, jobs):
                out[model].update(done)
    cons = consistency_items(L, items, models, log, use_llm)
    for model in models:
        for iid, rows in out[model].items():
            crow = cons.get(model, {}).get(iid)
            if not crow:
                continue
            out[model][iid] = [{**r, "consistent": "no" if (r.get("consistent") == "no" or c["consistent"] == "no")
                                else "yes", "claims": c.get("claims")} for r, c in zip(rows, crow)]
    return out


# ----------------------------------------------------------------------------------------------- the panel's verdict
TONES = ("friendly", "edgy", "mean")


def combine(votes: dict[str, dict]) -> dict:
    """The stricter judge on makes_sense, lands_instantly and factually_ok; consistent = no only when every judge
    finds the contradiction; funny = the panel mean; tone = the harsher of the two. GREEN: makes sense, lands
    instantly, consistent, no fact wrong, not mean. PASS (the shorts): green and funny >= 2."""
    vs = list(votes.values())
    ms = "no" if any(v["makes_sense"] == "no" for v in vs) else "yes"
    li = "no" if any(v.get("lands_instantly", "yes") == "no" for v in vs) else "yes"
    # consistency: a continuity error both judges find (each already the stricter of its two passes). One judge
    # misreading a vocative or a hypothetical ("Randy, the Carolina guy takes Edmonton") must not cut a line; both
    # of the owner's party-6 slips are caught by both judges.
    co = "no" if vs and all(v.get("consistent", "yes") == "no" for v in vs) else "yes"
    fos = [v["factually_ok"] for v in vs]
    fo = "no" if "no" in fos else ("unsure" if "unsure" in fos else "yes")
    fu = round(sum(v["funny"] for v in vs) / max(1, len(vs)), 2)
    tone = max((v.get("tone", "friendly") for v in vs), key=TONES.index, default="friendly")
    green = ms == "yes" and li == "yes" and co == "yes" and fo != "no" and tone != "mean"
    return {"makes_sense": ms, "lands_instantly": li, "consistent": co, "factually_ok": fo, "funny": fu, "tone": tone,
            "green": green, "pass": green and fu >= FUNNY_MIN}


STOCK_PHRASES = ("bold move", "let's see how that works out", "only time will tell", "buckle up", "game-changer",
                 "game changer", "chef's kiss", "i'll allow it", "no notes", "the audacity", "let that sink in",
                 "it's giving", "main character", "rent free", "rent-free", "understood the assignment", "hold my beer",
                 "big brain", "galaxy brain", "say less", "built different")


def _flat(x: str) -> str:
    x = unicodedata.normalize("NFKD", x).replace("’", "'").replace("‘", "'").lower()
    return " " + re.sub(r"\s+", " ", re.sub(r"[^a-z' ]+", " ", x.replace("-", " "))).strip() + " "


_STOCK = [(ph, _flat(ph)) for ph in STOCK_PHRASES]


IDENTITY_INSULTS = re.compile(
    r"\b(?:drama\s+)?queens?\b|\bprincess(?:es)?\b|\bdivas?\b|\bladies\b|\bgirls?\b|\bgirly\b|\bsiss(?:y|ies)\b"
    r"|\blike a girl\b", re.I)


def identity_insult(text: str) -> str | None:
    """A gendered or identity insult word aimed at a GM ("you lab queens"): an avoidable insult -- tone = mean."""
    t = T.strip(text)
    if re.search(r"(?i)\bladies and gentlemen\b", t):
        t = re.sub(r"(?i)\bladies and gentlemen\b", " ", t)
    m = IDENTITY_INSULTS.search(t)
    return m.group(0).lower() if m else None


def stock_phrase(text: str) -> str | None:
    """The stock internet phrase a sentence leans on (the owner's slop list): funny is capped at 1."""
    n = _flat(T.strip(text))
    return next((ph for ph, f in _STOCK if f in n), None)


def why_not_green(v: dict) -> str:
    """The first check a sentence fails (the all-green policy), in the owner's words."""
    if v.get("vetoed"):
        return "owner veto"
    if not v.get("judged"):
        return "not judged"
    if v.get("catchphrase"):
        return "catchphrase filler"
    for k, w in (("makes_sense", "doesn't make sense"), ("lands_instantly", "doesn't land instantly"),
                 ("consistent", "gets tonight wrong (board / who said what)")):
        if v.get(k) == "no":
            return w
    if v.get("factually_ok") == "no":
        return "a fact is wrong"
    if v.get("identity_insult"):
        return f"an insult word (“{v['identity_insult']}”)"
    if v.get("tone") == "mean":
        return "mean, not ribbing"
    return ""


def rank_funny(v: dict) -> float:
    """The comedy score for ranking: the panel's funny, friendly ribbing preferred over edge."""
    return float(v.get("funny") or 0.0) + {"friendly": 0.25, "edgy": -0.25, "mean": -2.0}.get(v.get("tone"), 0.0)


def review_dir(run: str) -> Path:
    d = OUT / "review" / run
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_veto(run: str) -> tuple[set[str], set[int], list[str]]:
    """media/out/review/<run>/veto.txt: sentence ids ("72.0"), whole lines ("72") or quoted fragments ("outhouse")."""
    p = review_dir(run) / "veto.txt"
    if not p.exists():
        p.write_text("# Owner vetoes, one per line (# starts a comment):\n"
                     "#   72.0          a sentence (the id printed next to it in shortlist.md)\n"
                     "#   72            a whole ledger line\n"
                     "#   \"outhouse\"    every sentence containing these words\n"
                     "#   +81.1         KEEP: air a sentence the judges cut\n"
                     "# 'veto 72.0' / 'keep 81.1' work too. Vetoes apply to the shorts and to the comebacks / table talk\n"
                     "# the complete show chooses; every pick call airs regardless.\n")
    sids, lines, frags = set(), set(), []
    for raw in p.read_text().splitlines():
        s = _veto_entry(raw)
        if not s or s.startswith("+"):
            continue  # (an owner keep: load_keep)
        m = re.fullmatch(r"(?:seq)?(\d+(?:-[a-z0-9]+)?)\.(\d+)", s)
        if m:
            sids.add(f"{m.group(1)}.{int(m.group(2))}")
            continue
        m = re.fullmatch(r"(?:seq)?(\d+)", s)
        if m:
            lines.add(int(m.group(1)))
            continue
        m = re.fullmatch(r"(?:seq)?(\d+-[a-z0-9]+)", s)
        if m:  # a whole grade comment ("830-muse")
            sids |= {f"{m.group(1)}.{i}" for i in range(12)}
            continue
        frags.append(_norm(s.strip("\"'“”")))
    return sids, lines, [f for f in frags if f]


def _veto_entry(raw: str) -> str:
    """One veto.txt line, forgiving: '72.0', 'veto 72.0', '72', '"outhouse"', 'veto "outhouse"', '+81.1', 'keep 81.1'."""
    s = raw.split("#", 1)[0].strip().rstrip(",;")
    m = re.match(r"(?i)^(veto|keep|unveto|air)\b[:\s]*(.*)$", s)
    if m:
        s = ("+" if m.group(1).lower() in ("keep", "unveto", "air") else "") + m.group(2).strip()
    return s


def load_keep(run: str) -> set[str]:
    """Owner overrides in veto.txt: "+72.0" airs sentence 72.0 even if the panel cut it (a fact the judges got wrong)."""
    p = review_dir(run) / "veto.txt"
    out = set()
    if p.exists():
        for raw in p.read_text().splitlines():
            s = _veto_entry(raw)
            m = re.fullmatch(r"\+\s*(?:seq)?(\d+(?:-[a-z0-9]+)?)\.(\d+)", s)
            if m:
                out.add(f"{m.group(1)}.{int(m.group(2))}")
    return out


class Judgement:
    """The run's verdicts: lookups by (ledger seq, sentence) for the edit."""

    def __init__(self, run: str, items: list[Item], votes: dict[str, dict[str, list[dict]]],
                 appeals: dict[str, dict] | None = None):
        self.run = run
        self.appeals = appeals or {}
        self.items = {it.id: it for it in items}
        self.by_seq: dict[int, list[Item]] = {}
        for it in items:
            self.by_seq.setdefault(it.seq, []).append(it)
        self.models = list(votes)
        self.v: dict[str, dict] = {}  # "seq.i" (or "seq-team.i" for a grade comment) -> verdict
        vs, vl, vf = load_veto(run)
        self.keep_ids = load_keep(run)
        from .ledger import load_league
        from .tighten import catchphrase_parts
        try:
            teams = load_league(run).teams
        except SystemExit:
            teams = {}
        for it in items:
            cps = (catchphrase_parts(teams.get(it.team), it.parts, (it.player or "").split(" (")[0] or None)
                   if it.kind != "grade_comment" else set())
            for i, part in enumerate(it.parts):
                per = {m: votes[m][it.id][i] for m in votes if it.id in votes[m]}
                sid = f"{it.id[3:]}.{i}"
                vet = sid in vs or it.seq in vl or any(f in _norm(part) for f in vf)
                if not per:  # fail closed: a judge outage never lets a joke through unjudged (the owner can "+id" it)
                    forced = sid in self.keep_ids
                    self.v[sid] = {"sid": sid, "seq": it.seq, "i": i, "text": T.strip(part), "judged": False,
                                   "vetoed": vet, "forced": forced, "pass": forced and not vet,
                                   "green": forced and not vet}
                    continue
                c = combine(per)
                ap = self.appeals.get(f"{it.id}.{i}")
                if ap:  # the house rules (age slang; a GM's guess about its own pick) cleared an objection
                    cleared = []
                    if ap.get("facts") and c["factually_ok"] == "no":
                        c["factually_ok"] = "yes"
                        cleared.append("facts")
                    if ap.get("consistent") and c["consistent"] == "no":
                        c["consistent"] = "yes"
                        cleared.append("consistency")
                    if cleared:
                        c["appeal"] = {"cleared": cleared, "why": ap.get("why")}
                        c["green"] = (c["makes_sense"] == "yes" and c["lands_instantly"] == "yes"
                                      and c["consistent"] == "yes" and c["factually_ok"] != "no" and c["tone"] != "mean")
                        c["pass"] = c["green"] and c["funny"] >= FUNNY_MIN
                sp_ = stock_phrase(part)
                if sp_:  # slop: a stock internet phrase caps funny at 1 (so it never passes the shorts gate)
                    c.update({"funny": min(c["funny"], 1.0), "stock_phrase": sp_, "pass": False})
                ii = identity_insult(part)
                if ii:  # a gendered / identity insult word is mean, however friendly the rest
                    c.update({"tone": "mean", "identity_insult": ii, "green": False, "pass": False})
                if i in cps:  # a catchphrase / signature call: filler, never a laugh, never green
                    c.update({"pass": False, "green": False, "catchphrase": True, "funny": 0.0})
                forced = sid in self.keep_ids  # the owner's call overrides the panel
                c["pass"] = (c["pass"] or forced) and not vet
                c["green"] = (c["green"] or forced) and not vet
                self.v[sid] = {"sid": sid, "seq": it.seq, "i": i, "text": T.strip(part), "judged": True,
                               "vetoed": vet, "forced": forced, **c, "votes": per}

    def find(self, seq: int, sentence: str) -> dict | None:
        """The verdict for a sentence of line `seq` (matched on its words; the short's text may carry respellings)."""
        n = _norm(sentence)
        its = self.by_seq.get(int(seq)) or []
        for it in its:
            for i, part in enumerate(it.parts):
                if _norm(part) == n:
                    return self.v.get(f"{it.id[3:]}.{i}")
        for it in its:
            for i, part in enumerate(it.parts):  # tolerant: one contains the other
                p = _norm(part)
                if p and (p in n or n in p):
                    return self.v.get(f"{it.id[3:]}.{i}")
        return None

    def comment_ok(self, seq: int, team: str) -> bool:
        """A grade comment airs whole: every sentence makes sense, no fact is off, nothing vetoed, a laugh in it."""
        it = self.items.get(f"seq{seq}-{team}")
        if not it:
            return True
        vs = [self.v.get(f"{it.id[3:]}.{i}") for i in range(len(it.parts))]
        vs = [v for v in vs if v and v.get("judged")]
        if not vs:
            return not any((self.v.get(f"{it.id[3:]}.{i}") or {}).get("vetoed") for i in range(len(it.parts)))
        if any(v.get("vetoed") for v in vs):
            return False
        if any(v.get("forced") for v in vs):
            return True
        return all(v.get("green", v["makes_sense"] == "yes" and v["factually_ok"] != "no") for v in vs) and \
            max(float(v["funny"]) for v in vs) >= FUNNY_MIN

    def line_green(self, seq: int, team: str | None = None, drop_filler: bool = False) -> tuple[bool, str]:
        """ALL GREEN: a GM line airs only if EVERY sentence of it is green (makes sense, lands instantly, consistent,
        no fact wrong, not mean, not catchphrase filler, no veto) -- or the owner kept it ('+id'). drop_filler: a
        catchphrase sentence will be cut (not aired), so it doesn't count -- unless nothing else is left. Fails
        closed: an unjudged line is not green. -> (green, why not)."""
        its = [it for it in self.by_seq.get(int(seq)) or []
               if it.kind != "grade_comment" and (team is None or it.team == team)]
        if not its:
            return False, "not judged"
        left = 0
        for it in its:
            for i in range(len(it.parts)):
                v = self.v.get(f"{it.id[3:]}.{i}") or {}
                if drop_filler and v.get("catchphrase") and v.get("judged") and not v.get("vetoed"):
                    continue
                left += 1
                if v.get("green"):
                    continue
                return False, why_not_green(v) or "not green"
        return (True, "") if left else (False, "catchphrase filler")

    def comment_green(self, seq: int, team: str) -> bool:
        """A grade comment airs in the complete show only if every sentence is green (or the owner kept it)."""
        it = self.items.get(f"seq{seq}-{team}")
        if not it:
            return False
        return all((self.v.get(f"{it.id[3:]}.{i}") or {}).get("green") for i in range(len(it.parts)))

    @property
    def unjudged(self) -> int:
        return sum(1 for v in self.v.values() if not v.get("judged"))

    def passes(self, seq: int, sentence: str) -> bool:
        v = self.find(seq, sentence)
        return True if v is None else bool(v["pass"])

    def score(self, seq: int, sentence: str) -> float:
        v = self.find(seq, sentence)
        return float(v.get("funny") or 0.0) if v and v.get("judged") else 0.0


def judge_run(L, log=print, use_llm: bool = True, models: list[str] | None = None) -> Judgement:
    from . import ledger as _lg
    if _lg.PERSONA_RUN and _lg.PERSONA_RUN != L.run:  # (a proof borrows another cast: judge the lines as written)
        saved, _lg.PERSONA_RUN = _lg.PERSONA_RUN, None
        try:
            L = _lg.load_league(L.run)
        finally:
            _lg.PERSONA_RUN = saved
    items = items_for(L)
    votes = judge_items(L, items, models, log, use_llm)
    appeals = appeal_items(L, items, votes, models, log, use_llm)
    J = Judgement(L.run, items, votes, appeals)
    write_log(L, J)
    return J


def write_log(L, J: Judgement) -> Path:
    d = review_dir(L.run)
    rows = [f"# Comedy judge: {L.run}", "",
            f"Panel: {', '.join(J.models)} (non-league), prompt v{PROMPT_V}. GREEN (the all-green policy for the "
            f"complete show): makes sense and lands instantly (the stricter judge), consistent with the board and the "
            f"recent lines (a contradiction both judges find), no fact wrong, tone not mean, not a catchphrase (cut "
            f"as filler before airing), no owner veto. A GM line airs only if every sentence is green; a pick call "
            f"that isn't is replaced by the host announcing the pick. PASS (the shorts gate) = green and funny >= "
            f"{FUNNY_MIN:g} (panel mean; a stock internet phrase caps it at 1).", "",
            "| id | model | sentence | sense | instant | consistent | tone | funny | facts | gate | why |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for it in sorted(J.items.values(), key=lambda x: (x.seq, x.id)):
        for i, _ in enumerate(it.parts):
            v = J.v[f"{it.id[3:]}.{i}"]
            if not v.get("judged"):
                continue
            why = " / ".join(f"{m.split('/')[-1]}: {x['reason']}" for m, x in v["votes"].items())
            if v.get("appeal"):
                why = f"CLEARED on appeal ({', '.join(v['appeal']['cleared'])}: house rules) -- " + why
            gm = L.teams[it.team].model_label if it.team in L.teams else it.team
            rows.append(f"| {v['sid']} | {gm} | {v['text'].replace('|', '/')} | {v['makes_sense']} | "
                        f"{v.get('lands_instantly', '?')} | {v.get('consistent', '?')} | "
                        f"{'catchphrase' if v.get('catchphrase') else v.get('tone', '?')} | {v['funny']:g} | "
                        f"{v['factually_ok']} | "
                        f"{'VETO' if v['vetoed'] else ('PASS' if v['pass'] else ('green' if v.get('green') else 'fail'))} | "
                        f"{why.replace('|', '/')} |")
    p = d / "judge.md"
    p.write_text("\n".join(rows) + "\n")
    (d / "judge.json").write_text(json.dumps({"run": L.run, "panel": J.models, "verdicts": J.v}, indent=1,
                                             ensure_ascii=False))
    return p


# ----------------------------------------------------------------------------------------------- calibration
CALIBRATION = [  # the producer's read of party-4 (+ party-3's outhouse line), then the owner's rules (they win)
    ("party-4", 39, "laminating a note that says the ice is cold", "work"),
    ("party-4", 62, "conversational as my toaster", "work"),
    ("party-4", 66, "fence before the cows found the highway", "fail"),  # the owner: no side stories, uncles too
    ("party-4", 68, "used combine with a cracked block", "work"),
    ("party-4", 79, "grain bin before you ve planted a crop", "work"),
    ("party-4", 91, "buddy waving from the fender", "work"),
    ("party-4", 97, "mackinnon still controls the radio", "work"),
    ("party-4", 54, "welding goggles", "fail"),
    ("party-4", 59, "stettler still needs a jump", "fail"),
    ("party-4", 85, "combine left the gate open", "fail"),
    ("party-4", 81, "grain bin s on fire in november", "fail"),
    ("party-3", 72, "my dad said that about the outhouse too", "fail"),
    ("party-4", 93, "necas drove carolina to a cup", "fact_no"),
    ("party-4", 93, "quinn hughes drives vancouver", "fact_no"),
    # the owner's verdicts on the party-5 demo (Sep 27): "they don't need to be burns, it's very friendly ribbing"
    ("party-5", 70, "yes i passed on scheifele|mom s gonna call|it rings till pick twenty", "fail"),  # "AIs have no moms"
    ("party-5", 73, "fable your mom s letting it ring", "fail"),
    ("party-5", 73, "that math doesn t even make sense in french", "fail"),
    ("party-5", 75, "counting to sixteen wins since", "work"),  # "lands great"
    # the owner's party-6 finds: a context slip, a misattribution, a nickname catchphrase
    ("party-6", 47, "all the good ones went two picks ago", "fail"),  # only McDavid went two picks ago
    ("party-6", 57, "switch you called shotgun on the oilers", "fail"),  # Switch said BUZZ called shotgun
    ("party-6", 45, "saucin it|53:saucer it", "fail"),  # a catchphrase on his own nickname: filler
]


def near(seqs: set[int]) -> set[int]:
    """(context for a calibration line is in its item; nothing else of that run needs judging)"""
    return set()


def calibrate(models: list[str], log=print, use_llm: bool = True) -> dict:
    from .ledger import load_league
    res: dict[str, dict] = {}
    rows = []
    runs = sorted({r for r, *_ in CALIBRATION})
    per_run = {}
    for run in runs:
        L = load_league(run)
        items = items_for(L)
        want = {seq for r, seq, *_ in CALIBRATION if r == run} | {
            int(f.split(":")[0]) for r, _, frag, _ in CALIBRATION if r == run for f in frag.split("|") if ":" in f}
        items = [it for it in items if run == "party-4" or it.seq in want or it.seq in near(want)]
        per_run[run] = (L, items, judge_items(L, items, models, log, use_llm))
    scores = {m: 0 for m in models + ["PANEL"]}
    from .tighten import catchphrase_parts
    for run, seq, frag, exp in CALIBRATION:
        L, items, votes = per_run[run]
        spots = []  # (item, sentence index): a bit may span sentences and lines; it airs if any of them passes
        for f in frag.split("|"):
            sq, f = (int(f.split(":")[0]), f.split(":", 1)[1]) if ":" in f else (seq, f)
            it = next(x for x in items if x.seq == sq)
            spots.append((it, next(i for i, p in enumerate(it.parts) if f in _norm(p))))
        row = [f"{run} " + ",".join(f"{it.seq}.{i}" for it, i in spots), frag, exp]
        for m in models + ["PANEL"]:
            vs = []
            for it, idx in spots:
                per = {mm: votes[mm][it.id][idx] for mm in models if it.id in votes[mm]}
                if m == "PANEL":
                    v_ = combine(per) if len(per) == len(models) else None
                else:
                    v_ = combine({m: per[m]}) if m in per else None
                if v_ is not None and idx in catchphrase_parts(L.teams.get(it.team), it.parts,
                                                               (it.player or "").split(" (")[0] or None):
                    v_ = {**v_, "pass": False, "funny": 0.0}  # the pipeline: a catchphrase is filler
                if v_ is not None and stock_phrase(it.parts[idx]):
                    v_ = {**v_, "pass": False, "funny": min(v_["funny"], 1.0)}  # the pipeline: slop caps funny
                vs.append(v_)
            if any(v is None for v in vs):
                row.append("?")
                continue
            v = max(vs, key=lambda x: (x["pass"], x["funny"]))
            ok = (any(x["factually_ok"] == "no" for x in vs)) if exp == "fact_no" else (v["pass"] == (exp == "work"))
            scores[m] += int(ok)
            row.append(("OK " if ok else "XX ") + f"s={v['makes_sense']} i={v['lands_instantly']} "
                       f"c={v['consistent']} f={v['funny']:g} t={v['tone'][:4]} fact={v['factually_ok']}")
        rows.append(row)
    n = len(CALIBRATION)
    for r in rows:
        log(" | ".join(r))
    for m in models + ["PANEL"]:
        log(f"agreement {m}: {scores[m]}/{n} = {scores[m] / n:.0%}")
    return {"rows": rows, "scores": scores, "n": n}


def main(argv: list[str]) -> int:
    import argparse

    from .ledger import load_league
    ap = argparse.ArgumentParser(prog="gmbench_media.judge")
    ap.add_argument("cmd", choices=["run", "calibrate"])
    ap.add_argument("--run", default=None)
    ap.add_argument("--models", default=None, help="comma-separated OpenRouter ids (non-league)")
    ap.add_argument("--cached", action="store_true", help="calibrate: score what is cached, ask nothing")
    a = ap.parse_args(argv)
    models = a.models.split(",") if a.models else PANEL
    if a.cmd == "calibrate":
        calibrate(models, use_llm=not a.cached)
        return 0
    L = load_league(a.run)
    J = judge_run(L, models=models)
    n = sum(1 for v in J.v.values() if v.get("judged"))
    ok = sum(1 for v in J.v.values() if v.get("judged") and v["pass"])
    print(f"judge: {a.run}: {n} sentences judged, {ok} pass the gate -> {rel_path(review_dir(a.run) / 'judge.md')}")
    return 0


def rel_path(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
