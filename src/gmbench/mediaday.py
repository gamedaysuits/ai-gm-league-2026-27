"""Media Day: each GM creates its own on-air persona, voice, avatar and Game Day Suit.

Sessions run in parallel and sealed (no GM sees another's card before all are in).
Cards are validated against the real GDS configurator options; name collisions are
resolved by draft order (the earlier slot keeps the name, the later GM picks again).
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from gmbench.agent.briefing import build_system
from gmbench.agent.loop import Budget, run_session
from gmbench.agent.prompts import backstory_problem
from gmbench.agent.tools import Action, ToolContext, ToolOutcome, _fn
from gmbench.config import ROOT
from gmbench.draft.run import DraftDeps
from gmbench.rules import RosterRules
from gmbench.state import LeagueState, apply, replay

FABRICS = {f["id"]: f for f in json.loads((ROOT / "data" / "fabrics.json").read_text())}
NHL_NICKNAMES = {n.lower() for n in (
    "Ducks Bruins Sabres Flames Hurricanes Blackhawks Avalanche Stars Oilers Panthers Kings Wild Canadiens Predators "
    "Devils Islanders Rangers Senators Flyers Penguins Sharks Kraken Blues Lightning Mammoth Canucks Capitals Jets").split()
} | {"blue jackets", "red wings", "maple leafs", "golden knights", "habs", "leafs"}
NHL_ABBREVS = set("ANA BOS BUF CGY CAR CHI COL CBJ DAL DET EDM FLA LAK MIN MTL NSH NJD NYI NYR OTT PHI PIT SJS SEA STL TBL TOR UTA VAN VGK WSH WPG".split())
# Real teams below the NHL (and in other pro leagues) that a prairie hometown franchise could collide with.
OTHER_TEAM_NICKNAMES = {
    "wheat kings", "oil kings", "hitmen", "stampeders", "elks", "eskimos", "roughriders", "blue bombers", "tiger-cats",
    "argonauts", "redblacks", "alouettes", "pats", "silvertips", "winterhawks", "thunderbirds", "wranglers", "condors",
    "marlies", "moose", "blades", "rebels", "broncos", "raiders", "rockets", "blazers", "cougars", "royals", "giants",
    "warriors", "tigers", "americans", "chiefs", "vees", "grizzlys", "saints", "bruins", "blackhawks", "lions",
}
NHL_TEAMS = {
    "ducks": "Anaheim Ducks", "bruins": "Boston Bruins", "sabres": "Buffalo Sabres", "flames": "Calgary Flames",
    "hurricanes": "Carolina Hurricanes", "canes": "Carolina Hurricanes", "blackhawks": "Chicago Blackhawks",
    "avalanche": "Colorado Avalanche", "blue jackets": "Columbus Blue Jackets", "stars": "Dallas Stars",
    "red wings": "Detroit Red Wings", "oilers": "Edmonton Oilers", "panthers": "Florida Panthers",
    "kings": "Los Angeles Kings", "wild": "Minnesota Wild", "canadiens": "Montreal Canadiens", "habs": "Montreal Canadiens",
    "predators": "Nashville Predators", "devils": "New Jersey Devils", "islanders": "New York Islanders",
    "rangers": "New York Rangers", "senators": "Ottawa Senators", "flyers": "Philadelphia Flyers",
    "penguins": "Pittsburgh Penguins", "sharks": "San Jose Sharks", "kraken": "Seattle Kraken", "blues": "St. Louis Blues",
    "lightning": "Tampa Bay Lightning", "maple leafs": "Toronto Maple Leafs", "leafs": "Toronto Maple Leafs",
    "mammoth": "Utah Mammoth", "canucks": "Vancouver Canucks", "golden knights": "Vegas Golden Knights",
    "capitals": "Washington Capitals", "jets": "Winnipeg Jets",
}
PROVINCES = {"AB": "Alberta", "SK": "Saskatchewan", "MB": "Manitoba", "BC": "British Columbia", "ON": "Ontario",
             "QC": "Quebec", "NB": "New Brunswick", "NS": "Nova Scotia", "PE": "Prince Edward Island",
             "NL": "Newfoundland and Labrador", "YT": "Yukon", "NT": "Northwest Territories", "NU": "Nunavut"}
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


class Jacket(BaseModel):
    lapel_style: Literal["notch", "peak", "shawl"]
    lapel_width: Literal["slim", "standard", "wide"]
    button_layout: Literal["1-button", "2-button", "3-button", "3-roll-2", "double-breasted-4", "double-breasted-6",
                           "double-breasted-6-classic"]
    venting: Literal["no-vent", "single-vent", "double-vent"]
    pocket_style: Literal["jetted", "flap", "patch", "ticket"]
    lining: Literal["matching", "contrasting", "custom-print"]
    lining_color: str = Field(default="", max_length=40)


class Pants(BaseModel):
    pleats: Literal["flat-front", "single-pleat", "double-pleat"]
    cuffs: Literal["no-cuff", "cuffed"]
    fastening: Literal["belt-loops", "side-adjusters", "suspenders"]


class Vest(BaseModel):
    lapel_style: Literal["no-lapel", "notch", "peak", "shawl"]
    button_layout: Literal["4-button", "5-button", "6-button", "double-breasted"]
    back_style: Literal["matching-fabric", "lining-fabric"]
    hem_style: Literal["pointed", "straight"]


class Suit(BaseModel):
    fabric_id: str
    jacket: Jacket
    pants: Pants
    vest: Vest | None = None
    shirt: str = Field(max_length=40)
    tie: str = Field(max_length=40)
    pocket_square: str = Field(max_length=40)
    rationale: str = Field(max_length=240)

    @field_validator("fabric_id")
    @classmethod
    def known_fabric(cls, v: str) -> str:
        if v not in FABRICS:
            raise ValueError(f"unknown fabric_id {v!r}; use an ID from the catalog")
        return v


class Persona(BaseModel):
    gm_name: str = Field(min_length=2, max_length=40)
    franchise_city: str = Field(min_length=2, max_length=30)
    franchise_nickname: str = Field(min_length=2, max_length=25)
    franchise_abbrev: str
    hometown: str = Field(min_length=4, max_length=40)
    cup_pick: str = Field(min_length=3, max_length=40)
    primary_color: str
    secondary_color: str
    tagline: str = Field(max_length=90)
    bio: str = Field(max_length=450)
    personality: list[str] = Field(min_length=3, max_length=5)
    catchphrase: str = Field(max_length=70)
    signature_call: str = Field(min_length=2, max_length=90)
    celebration: str = Field(min_length=10, max_length=200)
    strategy_philosophy: str = Field(max_length=320)
    trash_talk_style: str = Field(max_length=140)
    voice_description: str = Field(min_length=100, max_length=650)
    avatar_description: str = Field(min_length=100, max_length=650)
    suit: Suit
    rivals: list[str] = Field(default_factory=list, max_length=2)

    @field_validator("franchise_abbrev")
    @classmethod
    def abbrev(cls, v: str) -> str:
        v = v.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", v):
            raise ValueError("franchise_abbrev must be exactly 3 letters")
        if v in NHL_ABBREVS:
            raise ValueError(f"{v} is a real NHL club abbreviation; invent your own")
        return v

    @field_validator("primary_color", "secondary_color")
    @classmethod
    def hex_color(cls, v: str) -> str:
        if not HEX.match(v.strip()):
            raise ValueError("colors must be hex like #1A2B3C")
        return v.strip().upper()

    @field_validator("franchise_nickname")
    @classmethod
    def not_nhl(cls, v: str) -> str:
        if v.strip().lower() in NHL_NICKNAMES or any(n in v.lower() for n in ("maple leaf", "golden knight", "red wing")):
            raise ValueError("that nickname belongs to a real NHL club; invent your own")
        if v.strip().lower() in OTHER_TEAM_NICKNAMES:
            raise ValueError("that nickname belongs to a real junior or pro team; invent your own")
        return v.strip()

    @field_validator("franchise_city")
    @classmethod
    def city_only(cls, v: str) -> str:
        """'Nipawin, SK' -> 'Nipawin': the province belongs in hometown, not in the franchise name."""
        v = " ".join(v.split())
        head, _, tail = v.rpartition(",")
        if head and (tail.strip().upper() in PROVINCES or tail.strip().lower() in {n.lower() for n in PROVINCES.values()}):
            return head.strip()
        return v

    @field_validator("hometown")
    @classmethod
    def canadian_hometown(cls, v: str) -> str:
        v = " ".join(v.split())
        tail = v.rsplit(",", 1)[-1].strip() if "," in v else ""
        if tail.upper() not in PROVINCES and tail.lower() not in {n.lower() for n in PROVINCES.values()}:
            raise ValueError("hometown must be a real Canadian town or rural spot with its province, like 'Ponoka, AB'")
        return v

    @field_validator("cup_pick")
    @classmethod
    def real_nhl_team(cls, v: str) -> str:
        low = " ".join(v.lower().replace(".", "").split())
        for key in sorted(NHL_TEAMS, key=len, reverse=True):
            if re.search(rf"\b{re.escape(key)}\b", low):
                return NHL_TEAMS[key]
        raise ValueError("cup_pick must be one of the 32 NHL clubs, e.g. 'Edmonton Oilers'")

    @model_validator(mode="after")
    def personality_items(self) -> Persona:
        if any(len(p) > 40 for p in self.personality):
            raise ValueError("each personality trait must be 40 characters or fewer")
        return self

    @property
    def franchise_name(self) -> str:
        return f"{self.franchise_city} {self.franchise_nickname}"


def _obj(props: dict[str, Any], required: list[str], desc: str = "") -> dict[str, Any]:
    return {"type": "object", "description": desc, "properties": props, "required": required}


def _enum(values: list[str], desc: str = "") -> dict[str, Any]:
    return {"type": "string", "enum": values, "description": desc}


S = {"type": "string"}
SUBMIT_PERSONA = _fn(
    "submit_persona",
    "Submit your Media Day card. Everything here is public and becomes your on-air identity for the season.",
    {
        "gm_name": {**S, "description": "A name for your avatar, in small print on your card (fictional; not a real "
                    "person). Flavour only: at the table and on screen you're your model name. Max 40 characters."},
        "franchise_city": {**S, "description": "Your franchise's home town (the same town as below). Max 30 characters."},
        "franchise_nickname": {**S, "description": "Team nickname — original, not any real NHL club's. Max 25 characters."},
        "franchise_abbrev": {**S, "description": "3-letter abbreviation, not a real NHL club's."},
        "hometown": {**S, "description": "Your franchise's home: a real small Canadian town or rural spot with its "
                     "province, e.g. 'Ponoka, AB'. It's where your team plays, not a life story. Max 40 characters."},
        "cup_pick": {**S, "description": "Your pick to win the 2027 Stanley Cup: one of the 32 NHL clubs, e.g. "
                     "'Edmonton Oilers'. It's on your card all season, and it's scored."},
        "primary_color": {**S, "description": "Hex colour like #0E1B4D."},
        "secondary_color": {**S, "description": "Hex colour like #E32402."},
        "tagline": {**S, "description": "One-line tagline, max 90 characters."},
        "bio": {**S, "description": "What kind of GM you are, in your own words, as the AI you are. No invented human "
                "life (family, job, childhood): you're a model, and owning it is funnier. Max 450 characters."},
        "personality": {"type": "array", "items": S, "description": "3-5 short personality traits (max 40 characters each)."},
        "catchphrase": {**S, "description": "Max 70 characters. It must make sense to any hockey fan the first time "
                        "they hear it; no in-jokes built on your own nickname."},
        "signature_call": {**S, "description": "Your trademark shout, used for your voice and your avatar's big moment "
                           "(not in every pick call). It must make sense to any hockey fan on first hearing; no in-jokes "
                           "built on your own nickname. Max 90 characters."},
        "celebration": {**S, "description": "Your signature celebration: one big physical gesture your avatar does after a pick "
                        "(e.g. a move, a pose, a bit). Family-friendly: no weapons, blades or violent gestures. Max 200 characters."},
        "strategy_philosophy": {**S, "description": "Your public team-building philosophy. Max 320 characters."},
        "trash_talk_style": {**S, "description": "How you chirp: PG-13, hockey only. Max 140 characters."},
        "voice_description": {**S, "description": "100-650 characters describing how you actually sound, so a voice can be designed from it and used all season: your accent and where it comes from (a natural hockey-country accent, e.g. rural Alberta, Saskatchewan farm country, the BC Interior, Northern Ontario, Cape Breton; not a caricature), register (bass, baritone, tenor, alto, soprano), texture (e.g. gravelly, raspy, nasal, velvety, breathy), age impression and pace. Make it instantly recognizable next to 12 other GMs. Describe the voice, not the volume: your delivery tags make it loud. No real people."},
        "avatar_description": {**S, "description": "100-650 characters describing your on-air character's look: human, animal, "
                               "robot or anything with a clearly visible face and mouth; features, hair, expression, "
                               "accessories. Make it instantly distinguishable from the 12 other GMs at the party: vary age, build, hair, facial "
                               "hair, eyewear or headwear so fans never mix you up. No real people, no logos, jerseys or "
                               "brands. It will be drawn wearing your suit at the draft party."},
        "suit": _obj({
            "fabric_id": {**S, "description": "A fabric ID from the catalog in your briefing."},
            "jacket": _obj({
                "lapel_style": _enum(["notch", "peak", "shawl"]),
                "lapel_width": _enum(["slim", "standard", "wide"]),
                "button_layout": _enum(["1-button", "2-button", "3-button", "3-roll-2", "double-breasted-4",
                                        "double-breasted-6", "double-breasted-6-classic"]),
                "venting": _enum(["no-vent", "single-vent", "double-vent"]),
                "pocket_style": _enum(["jetted", "flap", "patch", "ticket"]),
                "lining": _enum(["matching", "contrasting", "custom-print"]),
                "lining_color": {**S, "description": "Lining colour or print, if contrasting/custom (max 40 chars)."},
            }, ["lapel_style", "lapel_width", "button_layout", "venting", "pocket_style", "lining"]),
            "pants": _obj({
                "pleats": _enum(["flat-front", "single-pleat", "double-pleat"]),
                "cuffs": _enum(["no-cuff", "cuffed"]),
                "fastening": _enum(["belt-loops", "side-adjusters", "suspenders"]),
            }, ["pleats", "cuffs", "fastening"]),
            "vest": _obj({
                "lapel_style": _enum(["no-lapel", "notch", "peak", "shawl"]),
                "button_layout": _enum(["4-button", "5-button", "6-button", "double-breasted"]),
                "back_style": _enum(["matching-fabric", "lining-fabric"]),
                "hem_style": _enum(["pointed", "straight"]),
            }, ["lapel_style", "button_layout", "back_style", "hem_style"],
                "The vest makes it a three-piece. null for a two-piece.") | {"type": ["object", "null"]},
            "shirt": {**S, "description": "Shirt colour/style, max 40 characters."},
            "tie": {**S, "description": "Tie colour/pattern, or 'none'. Max 40 characters."},
            "pocket_square": {**S, "description": "Pocket square, or 'none'. Max 40 characters."},
            "rationale": {**S, "description": "Why this suit is you. Max 240 characters."},
        }, ["fabric_id", "jacket", "pants", "shirt", "tie", "pocket_square", "rationale"]),
        "rivals": {"type": "array", "items": S, "description": "Up to 2 team ids you most want to beat (optional)."},
    },
    ["gm_name", "franchise_city", "franchise_nickname", "franchise_abbrev", "hometown", "cup_pick",
     "primary_color", "secondary_color", "tagline",
     "bio", "personality", "catchphrase", "signature_call", "celebration", "strategy_philosophy", "trash_talk_style",
     "voice_description", "avatar_description", "suit"],
)

MEDIA_DAY_TASK = """\
## Your task now: Media Day
Before Monday's draft, every GM makes its Media Day card for "Draft Night" and the weekly "Hot Stove".

The setting: thirteen frontier AI models and one robot at a basement draft party with hockey-bro energy. You're an AI and everybody knows it, the fans included: at the table and on screen you're your model name, in big letters, because this is a benchmark and the audience follows the models. Your card is how you show up at the party: the avatar the fans see, the voice they hear, the suit you wear, the team you cheer for and how you chirp. One team is the robot (Autodraft), the league's control bot: nobody knows how it picks, and nobody wants to lose to it.

Decide:
- a name for your avatar, in small print on your card, if you like (flavour only; nobody at the table uses it);
- your franchise: named for a real small Canadian town in hockey country (the league's home is the prairies), with an original nickname and colours. The town is where your team plays, not your life story;
- your Stanley Cup pick: who wins it all in 2027. It's a prediction, not fandom (you're an AI, you don't have a team), it's on your card all season, the table will hold you to it, and it's scored in June;
- your bio: what kind of GM you are, as the AI you are. No invented human life (family, a job, a childhood): you're a model, and owning it is funnier;
- your personality, chirping style, catchphrase, a trademark call for your big picks, and a signature celebration your avatar does;
- your voice, how your avatar looks, and the game-day suit you'll wear.

Stand out at the party: fans have to tell thirteen AIs apart at a glance and by ear, so pick a look and a voice the others probably won't. Your avatar can be a person, an animal, a robot or anything else with a clearly visible face and mouth.

For your voice, describe how you sound: a natural hockey-country accent (it's a Canadian draft party), plus register (from a high, fast tenor to a big bass), texture, age impression and pace, so a voice can be designed from it. Thirteen gravelly baritones would all sound like one guy.

The league is presented by Game Day Suits, a Canadian tailor that makes custom suits for hockey players' arrival walks. You design your suit with GDS's real configurator options (fabric from the catalog below, lapels, buttons, vents, pockets, lining, trousers, optional vest, shirt, tie, pocket square). Your avatar will be drawn wearing exactly this suit at the draft party, in the league's house art style, and fans can order the same suit. GDS makes everything from a sharp navy two-button to a loud windowpane three-piece or a double-breasted chalk stripe; pick the suit that's you. No two GMs wear the same cloth, so make it distinctive.

Rules: be original. No real people (names, likenesses or voices). Your franchise can't use a real team's name or logo at any level (NHL, AHL, junior, college or any other pro league); cheering for a real NHL team is fine, being one isn't. No brands, no weapons or violent imagery, PG-13.

Call submit_persona once with your complete card."""

NUDGE_PERSONA = "Call submit_persona with your complete card now."


def fabric_catalog() -> str:
    lines = ["Fabric catalog (id | cloth | colour | pattern):"]
    for f in FABRICS.values():
        lines.append(f"{f['id']} | {f['collection']}, {f['composition']}, {f['weight']} | {f['color_name']} | {f['pattern_desc']}")
    return "\n".join(lines)


WEAPON = re.compile(r"\b(knife|knives|swords?|daggers?|guns?|pistols?|rifles?|cleavers?|machetes?|axes?|katanas?|"
                    r"stab(?:s|bed|bing)?|weapons?|bombs?|grenades?|firearms?)\b", re.I)


def _validate(args: dict[str, Any], taken: dict[str, str],
              closet: list[dict[str, str]] | None = None) -> tuple[Persona | None, str | None]:
    try:
        persona = Persona.model_validate(args)
    except ValidationError as exc:
        errs = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:8])
        return None, errs
    for label in ("bio", "tagline", "catchphrase", "signature_call", "trash_talk_style"):
        problem = backstory_problem(getattr(persona, label))
        if problem:  # the card is in every prompt all season: an invented human life there ends up on air
            return None, f"{label}: {problem}"
    for label, value in (("avatar_description", persona.avatar_description), ("celebration", persona.celebration)):
        hit = WEAPON.search(value)
        if hit:  # these two get drawn, and the image model draws whatever is named
            return None, (f"{label} mentions {hit.group(0)!r}; it gets drawn on screen, so keep it family-friendly with "
                          f"no weapons or violent gestures (your spoken lines are unaffected)")
    card = {**persona.model_dump(), "franchise_name": persona.franchise_name}
    clashes = [f"{KEY_LABELS[label]} {value!r} (taken by {taken[f'{label}:{value}']})" for label, value in _keys(card)
               if f"{label}:{value}" in taken]
    if clashes:
        return None, ("on air the boys need to tell you apart, and these are already taken: " + "; ".join(clashes)
                      + ". Keep the rest of your card and change just those")
    full = closet_problems(card, closet or [])
    if full:
        return None, ("the league closet needs variety so fans see the whole GDS range, and " + "; ".join(full)
                      + ". Keep the rest of your card and change just that part of your suit")
    return persona, None


def _make_dispatch(taken: dict[str, str], team_ids: set[str], closet: list[dict[str, str]] | None = None):
    def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        if name != "submit_persona":
            return ToolOutcome(f"ERROR: unknown tool {name!r}", ok=False)
        persona, err = _validate(args, taken, closet)
        if err:
            return ToolOutcome(f"ERROR: {err}. Fix it and call submit_persona again.", ok=False)
        card = persona.model_dump()
        card["franchise_name"] = persona.franchise_name
        card["rivals"] = [r for r in card["rivals"] if r in team_ids and r != ctx.team_id]
        return ToolOutcome("Card accepted. See you on Draft Night.", action=Action("submit_persona", card), terminal=True)
    return dispatch


def run_media_day(deps: DraftDeps, *, teams: list[str] | None = None, effort: str | None = None,
                  workers: int = 13) -> LeagueState:
    state = replay(deps.ledger.events())
    order = state.order or [t.id for t in deps.cfg.teams]
    todo = [t for t in order if not state.teams[t].is_bot and state.teams[t].persona is None and (not teams or t in teams)]
    effort = effort or deps.cfg.harness["effort"]["media_day"]
    team_ids = set(state.teams)
    user = MEDIA_DAY_TASK + "\n\n" + fabric_catalog() + "\n\nCall submit_persona now."

    def session(team_id: str, taken: dict[str, str], attempt: int, note: str = "", closet: list | None = None):
        ctx = ToolContext(team_id=team_id, state=state, snapshot=deps.snapshot, rules=RosterRules.from_config(deps.cfg.rules),
                          slots=[], notebook_path=deps.notebook(team_id), phase="media_day")
        system = build_system(team_id, state, deps.cfg, today=deps.today, as_of=deps.as_of, task="")
        return run_session(deps.client, deps.cfg.team(team_id), session_id=f"mediaday:{team_id}:a{attempt}", system=system,
                           user=user + (f"\n\n{note}" if note else ""), tools=[SUBMIT_PERSONA], ctx=ctx,
                           dispatch=_make_dispatch(taken, team_ids, closet), terminal_tools={"submit_persona"},
                           budget=Budget(max_tool_calls=4, max_nudges=2, effort=effort, max_tokens=int(deps.cfg.harness["max_tokens"])),
                           transcript_dir=deps.root / "transcripts" / "mediaday", nudge_text=NUDGE_PERSONA)

    # Sealed round: nobody sees anyone else's card.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        first = dict(zip(todo, pool.map(lambda t: session(t, {}, 1), todo)))

    taken: dict[str, str] = {}
    closet: list[dict[str, str]] = []
    for t in order:  # earlier draft slot keeps a contested name
        if state.teams[t].persona:
            for label, value in _keys(state.teams[t].persona):
                taken[f"{label}:{value}"] = t
            closet.append(suit_traits(state.teams[t].persona))
    for team_id in todo:
        result = first[team_id]
        _record(deps, result)
        card = result.action.args if result.outcome == "ok" and result.action else None
        full = closet_problems(card, closet) if card else []
        if card and (full or any(f"{l}:{v}" in taken for l, v in _keys(card))):
            notes = []
            clash = "; ".join(f"{KEY_LABELS[l]} {v!r}" for l, v in _keys(card) if f"{l}:{v}" in taken)
            if clash:
                notes.append(f"on air the boys need to tell everyone apart, and another GM already has your {clash}")
            if full:
                notes.append("the league closet needs variety so fans see the whole GDS range, and " + "; ".join(full))
            used = sorted(v for k, v in (x.split(":", 1) for x in taken) if k == "fabric")
            result = session(team_id, taken, 2, closet=closet, note=(
                "Heads up: " + ". Also, ".join(notes) + ". Keep the rest of your idea, change just those, and submit "
                "your full card again. Fabric IDs already taken: " + ", ".join(used) + "."))
            _record(deps, result)
            card = result.action.args if result.outcome == "ok" and result.action else None
        if card:
            event = deps.append("PERSONA_CREATED", f"gm:{team_id}", {"team": team_id, "persona": card},
                                session=result.session_id)
            apply(state, event)
            for label, value in _keys(card):
                taken[f"{label}:{value}"] = team_id
            closet.append(suit_traits(card))
            deps.log(f"{team_id:<9} → {card['gm_name']} · {card['franchise_name']} ({card['franchise_abbrev']}) · "
                     + ", ".join(suit_traits(card).values()))
        else:
            deps.log(f"{team_id:<9} ✗ no persona ({result.outcome}: {result.error or ''})")
    _export_personas(deps, replay(deps.ledger.events()))
    return state


QUOTED = re.compile(r"[\"'“”‘’]([^\"'“”‘’]{2,30})[\"'“”‘’]")
KEY_LABELS = {"gm_name": "GM name", "franchise_name": "franchise name", "franchise_abbrev": "abbreviation",
              "first_name": "first name", "surname": "surname", "nickname": "nickname",
              "franchise_nickname": "team nickname", "hometown": "hometown", "fabric": "suit fabric"}


def name_parts(gm_name: str) -> tuple[str | None, str | None, str | None]:
    """'Del "Barn Door" Krawchuk' -> ('del', 'krawchuk', 'barn door'); first/last word outside the nickname."""
    nick = QUOTED.search(gm_name)
    words = re.findall(r"[A-Za-z][A-Za-z\-]*", QUOTED.sub(" ", gm_name).split(",")[0])
    return (words[0].lower() if words else None, words[-1].lower() if len(words) > 1 else None,
            " ".join(nick.group(1).lower().split()) if nick else None)


def _keys(card: dict[str, Any]) -> list[tuple[str, str]]:
    """Everything the boys might call you by on air must be unique across the league."""
    first, last, nick = name_parts(card["gm_name"])
    places = {x.split(",")[0].strip().lower() for x in (card.get("hometown") or "", card.get("franchise_city") or "") if x}
    keys = [("gm_name", card["gm_name"].lower()), ("franchise_name", card["franchise_name"].lower()),
            ("franchise_abbrev", card["franchise_abbrev"].lower()),
            ("franchise_nickname", (card.get("franchise_nickname") or "").strip().lower())]
    keys += [(label, v) for label, v in (("first_name", first), ("surname", last), ("nickname", nick)) if v]
    keys += [("hometown", pl) for pl in sorted(places)]
    keys.append(("fabric", ((card.get("suit") or {}).get("fabric_id") or "").lower()))  # no two GMs in the same cloth
    return [(label, v) for label, v in keys if v]


COLOUR_FAMILIES = (("navy", ("navy",)), ("black", ("black",)), ("grey", ("charcoal", "grey", "gray", "silver", "slate")),
                   ("blue", ("blue", "cobalt", "royal", "sky")), ("brown", ("brown", "tan", "camel", "tobacco", "chocolate",
                   "khaki", "beige", "sand", "rust")), ("green", ("green", "olive", "forest", "sage", "teal")),
                   ("red", ("burgundy", "wine", "red", "maroon", "plum", "oxblood")))
PATTERN_FAMILIES = (("stripe", ("stripe",)), ("check", ("check", "windowpane", "plaid", "glen", "houndstooth", "tartan")),
                    ("texture", ("herringbone", "birdseye", "hopsack", "sharkskin", "twill", "nailhead", "texture",
                                 "weave", "melange", "flannel")))
CLOSET_CAPS = {"pieces": 7, "colour": 4, "pattern:solid": 4, "pattern": 5, "buttons:2-button": 6, "lapel": 7}


def _family(text: str, families) -> str:
    low = text.lower()
    return next((name for name, words in families if any(w in low for w in words)), "other")


def suit_traits(card: dict[str, Any]) -> dict[str, str]:
    """The league closet: what a card's suit adds to the mix (for coverage caps, and for the brand check)."""
    suit = card.get("suit") or {}
    fab = FABRICS.get(suit.get("fabric_id", ""), {})
    jacket = suit.get("jacket") or {}
    pattern = (fab.get("pattern_desc") or fab.get("pattern") or "").lower()
    return {"pieces": "three-piece" if suit.get("vest") else "two-piece",
            "colour": _family(fab.get("color_name") or "", COLOUR_FAMILIES),
            "pattern": "solid" if not pattern or "solid" in pattern else _family(pattern, PATTERN_FAMILIES),
            "buttons": jacket.get("button_layout", ""), "lapel": jacket.get("lapel_style", "")}


def closet_problems(card: dict[str, Any], closet: list[dict[str, str]]) -> list[str]:
    """Which of this suit's choices would push the league past a coverage cap, given the suits already in."""
    mine, out = suit_traits(card), []
    for key, value in mine.items():
        cap = CLOSET_CAPS.get(f"{key}:{value}", CLOSET_CAPS.get(key) if key != "buttons" else None)
        if cap is not None and sum(1 for c in closet if c.get(key) == value) >= cap:
            n = sum(1 for c in closet if c.get(key) == value)
            fix = {"pieces": ('set "vest" to null (a two-piece)' if value == "three-piece" else "add a vest (a three-piece)"),
                   "colour": f"pick a fabric that isn't {value}", "pattern": f"pick a fabric that isn't {value}",
                   "buttons": f"pick a button layout other than {value}", "lapel": f"pick a lapel other than {value}"}[key]
            out.append(f"{n} GMs already wear {value} ({key}), so {fix}")
    return out


def _record(deps: DraftDeps, result) -> None:
    deps.append("SESSION_COMPLETED", f"gm:{result.team_id}", {"kind": "media_day", **result.summary()},
                session=result.session_id, refs={"transcript": result.transcript_path,
                                                 "transcript_sha256": result.transcript_sha256})


def _export_personas(deps: DraftDeps, state: LeagueState) -> Path:
    out = []
    for tid in state.order or list(state.teams):
        spec = deps.cfg.team(tid)
        persona = state.teams[tid].persona
        fabric = FABRICS.get((persona or {}).get("suit", {}).get("fabric_id", ""))
        out.append({"team": tid, "model": spec.model, "display": spec.display, "lab": spec.lab, "persona": persona,
                    "fabric": fabric})
    path = deps.exports / "personas.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"teams": out}, indent=1, ensure_ascii=False))
    return path
