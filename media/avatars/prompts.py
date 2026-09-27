"""Prompt composition for GM avatars: character + exact suit + one house framing.

Everything a GM chose on Media Day is used verbatim (avatar_description, shirt, tie,
pocket square); the structured suit options are translated into tailoring language.
Face type (human / beak / muzzle / fish / robot) switches the mouth and eyelid wording
so non-human characters still get lip-flap frames that read.
"""
from __future__ import annotations

import re

# ----------------------------------------------------------------------------- colours
NAMED_COLOURS = {
    "black": "#0A0A0A", "charcoal": "#333639", "dark grey": "#4A4A4A", "grey": "#808080",
    "silver": "#BFC3C7", "white": "#FFFFFF", "off-white": "#EFF0F5", "cream": "#F3E9CF",
    "beige": "#D9C9A8", "tan": "#C19A6B", "dark navy": "#0E1B3D", "navy": "#1F2F6B",
    "midnight blue": "#1B1F4A", "royal blue": "#3F63D8", "cobalt blue": "#0047AB",
    "steel blue": "#4682B4", "sky blue": "#7EC8F0", "ice blue": "#CFE8F3", "slate blue": "#5A6B8C",
    "deep teal": "#0B4F4A", "teal": "#0E8C80", "bright teal": "#2CE6C8", "aqua": "#4FE3F0",
    "turquoise": "#35C9C0", "forest green": "#1F6B33", "dark green": "#0D3B22", "olive": "#5B6B2F",
    "kelly green": "#2E9E45", "lime green": "#A4D65E", "mint": "#9FE2BF", "emerald": "#2F9E6E",
    "red": "#D22B2B", "crimson": "#A51C30", "scarlet": "#F02A12", "burgundy": "#6E1A2B",
    "maroon": "#4A1020", "orange": "#F28C28", "burnt orange": "#C45C26", "copper": "#B87333",
    "rust": "#A3401C", "amber": "#E8A33D", "gold": "#D4AF37", "mustard": "#D8A31A",
    "yellow": "#F5D02E", "purple": "#5E2B97", "violet": "#7F3FBF", "plum": "#5B2350",
    "lavender": "#B39DDB", "magenta": "#D0208A", "pink": "#F08CB4", "brown": "#6B4423",
    "chocolate": "#3F2415", "bronze": "#A97142",
}


def _lab(hexs: str) -> tuple[float, float, float]:
    r, g, b = (int(hexs[i : i + 2], 16) / 255 for i in (1, 3, 5))
    lin = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    r, g, b = lin(r), lin(g), lin(b)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116  # noqa: E731
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def colour_name(hexs: str) -> str:
    """Nearest plain-English colour name (helps the image model more than a bare hex)."""
    try:
        target = _lab(hexs)
    except (ValueError, IndexError):
        return hexs
    return min(NAMED_COLOURS, key=lambda n: sum((a - b) ** 2 for a, b in zip(_lab(NAMED_COLOURS[n]), target)))


def colour(hexs: str) -> str:
    return f"{colour_name(hexs)} ({hexs.upper()})"


# ----------------------------------------------------------------------------- face types
FACE_KEYWORDS = [  # whole words (plural allowed); first match wins
    ("robot", ["robot", "android", "automaton", "droid", "mech"]),
    ("beak", ["owl", "bird", "beak", "eagle", "hawk", "falcon", "raven", "crow", "penguin", "parrot",
              "puffin", "heron", "duck", "goose", "loon", "gull", "magpie", "jay", "sparrow", "pelican", "swan",
              "chicken", "rooster", "hen", "turkey", "flamingo", "woodpecker", "chickadee"]),
    ("fish", ["fish", "pufferfish", "puffer", "shark", "fin", "eel", "octopus", "goldfish", "catfish"]),
    ("muzzle", ["fox", "wolf", "dog", "cat", "leopard", "lion", "tiger", "bear", "otter", "raccoon",
                "lynx", "cougar", "muzzle", "snout", "badger", "rabbit", "hare", "deer", "moose",
                "beaver", "walrus", "seal", "husky", "panther", "jaguar", "horse", "goat", "ram", "jackalope",
                "jackrabbit", "antelope", "elk", "bison", "buffalo", "coyote", "gopher", "skunk", "porcupine",
                "squirrel", "mouse", "rat", "pig", "boar", "bull", "cow", "sheep", "donkey", "mule", "marmot"]),
]


HUMAN_WORDS = re.compile(r"\b(human|man|woman|guy|fella|fellow|gentleman|lady|person|dude|grandpa|uncle)\b")
IDIOMS = re.compile(r"crow'?s[- ]feet|walrus[- ](?:moustache|mustache|stache)|bear[- ]hug|eagle[- ]eye[ds]?|"
                    r"hawk[- ](?:eyed|nose[d]?)|fox[- ]like|bull[- ]neck(?:ed)?|barrel[- ]chest(?:ed)?|"
                    r"cat[- ](?:like|eye)|wolf(?:ish)?[- ]grin|puppy[- ]dog|owl[- ](?:eyed|glasses)|fish[- ]eye")


def face_type(description: str) -> str:
    """Mouth rig for a character. An explicitly human character is always human: descriptive idioms like "crow's
    feet" or "walrus moustache" must never turn a man's mouth into a beak or a muzzle."""
    d = IDIOMS.sub(" ", description.lower())
    if HUMAN_WORDS.search(d) and "anthropomorphic" not in d:
        return "human"
    for kind, words in FACE_KEYWORDS:
        if re.search(r"\b(" + "|".join(map(re.escape, words)) + r")(s|es)?\b", d):
            return kind
    return "muzzle" if "anthropomorphic" in d else "human"


FACE = {
    "human": {
        "mouth_region": "mouth and jaw",
        "eye_region": "eyelids",
        "base": "Face: front-facing, eyes open looking straight at the viewer, mouth fully closed (lips together, "
                "no teeth showing), relaxed natural expression.",
        "mid": "Change ONLY the mouth: part the lips a little, as if in the middle of saying \"eh\" - a hint "
               "of the upper teeth visible, jaw barely dropped.",
        "wide": "Change ONLY the mouth: open it wide, as if saying a big, clear \"ah!\" - jaw dropped, upper "
                "teeth and a little tongue visible, the dark inside of the mouth clearly visible.",
        "blink": "Change ONLY the eyes: close both eyes completely, as in the middle of a natural blink - "
                 "upper eyelids fully down and relaxed.",
        "mouth_guard": "The eyes, eyebrows and the rest of the face stay exactly as they are - same expression, "
                       "not furrowed, not raised, not squinting.",
        "eye_guard": "Keep the mouth exactly as it is (closed) and the eyebrows where they are.",
    },
    "beak": {
        "mouth_region": "beak",
        "eye_region": "eyelids",
        "base": "Face: front-facing, eyes open looking straight at the viewer, beak FULLY CLOSED - the upper "
                "beak rests on the lower beak with no gap and no visible inside of the mouth or tongue; any smile "
                "shows only as a slight upturn at the beak corners and in the eyes. Draw the beak clearly and a "
                "little larger than life so it can open for talking animation, and keep the beak and the chin "
                "feathers below it unobstructed.",
        "mid": "Change ONLY the beak: open it slightly, as if in the middle of a word - the lower beak drops a "
               "little so a clear dark gap (the inside of the mouth) shows between the upper and lower beak.",
        "wide": "Change ONLY the beak: open it wide, as if calling out a big \"ah!\" - the lower beak dropped "
                "well down, the dark inside of the mouth and a small tongue clearly visible.",
        "blink": "Change ONLY the eyes: close both eyes completely, as in a natural blink - the feathered "
                 "eyelids come fully down over the eyes.",
        "mouth_guard": "The eyes, brow feathers, facial disc, any glasses and everything else stay exactly as "
                       "they are - same expression.",
        "eye_guard": "Keep the beak exactly as it is (closed), and keep any glasses exactly where they are, "
                     "now in front of the closed eyes.",
    },
    "muzzle": {
        "mouth_region": "mouth and lower jaw",
        "eye_region": "eyelids",
        "base": "Face: front-facing (muzzle pointing at the viewer, not in profile), eyes open looking at the "
                "viewer, mouth fully closed (no teeth, tongue or inside of the mouth showing) with a clearly drawn "
                "mouth line on the muzzle so it can open for talking animation.",
        "mid": "Change ONLY the mouth on the muzzle: open it slightly, as if in the middle of a word - the "
               "lower jaw drops a little, showing a narrow dark gap and a hint of teeth.",
        "wide": "Change ONLY the mouth on the muzzle: open it wide, as if saying a big \"ah!\" - lower jaw "
                "dropped, teeth, tongue and the dark inside of the mouth clearly visible.",
        "blink": "Change ONLY the eyes: close both eyes completely, as in a natural blink - furred eyelids "
                 "fully down and relaxed.",
        "mouth_guard": "The eyes, brows, ears, whiskers and the rest of the face stay exactly as they are - "
                       "same expression.",
        "eye_guard": "Keep the mouth exactly as it is (closed).",
    },
    "fish": {
        "mouth_region": "mouth",
        "eye_region": "eyelids",
        "base": "Face: front-facing, eyes open looking at the viewer, mouth fully closed (lips together, no "
                "gap) with clearly drawn lips so it can open for talking animation.",
        "mid": "Change ONLY the mouth: part the lips a little, as if in the middle of a word - a narrow dark "
               "gap between them.",
        "wide": "Change ONLY the mouth: open it wide into a round \"ah\" shape - the dark inside of the mouth "
                "clearly visible.",
        "blink": "Change ONLY the eyes: close both eyes completely, as in a blink - eyelids fully down.",
        "mouth_guard": "The eyes, fins and the rest of the face stay exactly as they are - same expression.",
        "eye_guard": "Keep the mouth exactly as it is (closed).",
    },
    "robot": {
        "mouth_region": "mouth (jaw plate or mouth slot)",
        "eye_region": "eye shutters",
        "base": "Face: front-facing, eyes open and lit, mouth fully closed (no gap). The mouth must be a clearly "
                "visible part that can open for talking animation (for example a hinged jaw plate or a mouth slot).",
        "mid": "Change ONLY the mouth: open it slightly, as if in the middle of a word - the jaw plate (or "
               "mouth slot) opens a little, showing a narrow dark gap.",
        "wide": "Change ONLY the mouth: open it wide, as if saying a big \"ah!\" - the jaw plate (or mouth "
                "slot) drops wide open, the dark inside clearly visible.",
        "blink": "Change ONLY the eyes: close both eyes, as in a blink - the eyelid shutters (or eye covers) "
                 "come fully down over the eye lenses.",
        "mouth_guard": "The eyes, antenna, faceplate and everything else stay exactly as they are.",
        "eye_guard": "Keep the mouth exactly as it is (closed).",
    },
}

# ----------------------------------------------------------------------------- house style
STYLE_INTRO = {
    "pixel": "Create a 16-bit era pixel-art character portrait, like a portrait from a cut-scene in an "
             "early-1990s console hockey video game, of a general manager in a fictional hockey league.",
    "painted": "Create an early-1990s painted trading-card portrait - a hand-painted illustration in gouache "
               "and acrylic with visible brushwork - of a general manager in a fictional hockey league.",
}
STYLE_RULES = {
    "pixel": "Pixel-art rules: crisp, hard-edged square pixels on one consistent grid - it should look like a "
             "roughly 192x192-pixel sprite scaled up with nearest-neighbour. Limited palette of about 32-48 "
             "colours, flat colour clusters, dark outlines, simple 2-3 step shading. No anti-aliasing, no blur, "
             "no smooth gradients, no painterly texture.",
    "painted": "Painting rules: rich dramatic lighting (warm key light on the face, cool rim light on the "
               "shoulders), realistic proportions, visible brushwork, in the manner of classic painted "
               "sports-card art. Full-bleed: the painting fills the entire square canvas edge to edge - no white "
               "margins, no unpainted canvas, no border.",
}
STYLE_EXTRA = {
    "pixel": " (the same pixel grid, pixel size and limited palette, crisp hard-edged pixels)",
    "painted": " (the same painted brushwork)",
}
FRAMING = (
    "Framing: waist-up portrait, body and head square to the camera (straight-on front view), head level and "
    "not tilted, centered horizontally, shoulders fully in frame, with comfortable empty headroom above the "
    "head (the top of the head, ears, tufts or antenna sits about 10-15% of the image height below the top "
    "edge). Square 1:1 composition."
)
NO_TEXT = (
    "Strictly no text, letters, numbers, logos, emblems, crests, team marks, jerseys, brand names, signage, "
    "advertising, UI elements, borders, frames or watermarks anywhere in the image."
)
NOT_REAL = (
    "This is an original, fictional character: not based on and not resembling any real person, celebrity, "
    "athlete or public figure."
)
PROPS = (
    "If the character holds a prop, keep it low beside the chest so it never covers the face, mouth, tie or "
    "the front of the suit."
)
SWATCH_NOTE = (
    "The attached photo is only a close-up reference of the exact suit cloth: match its colour and weave or "
    "pattern for the jacket, trousers and waistcoat. Do not show the swatch itself anywhere."
)

KEEP = (
    " Keep everything else in the image exactly identical: the same character and face, the same head "
    "position, size and angle, the same pose, framing and crop, the same suit, shirt, tie, pocket square and "
    "colours, the same background, lighting and art style{extra}. Do not zoom, shift, crop, re-light or redraw "
    "anything outside the {region}. Output the full image at the same size and aspect ratio. No text, logos or "
    "watermarks."
)
STRICT = (
    " IMPORTANT: this is one frame of a lip-sync animation and must line up exactly with the original. Treat "
    "it as a tiny local touch-up of the {region} only: every other pixel must stay unchanged, and the face must "
    "remain the same character with the same eyes, brows, nose, hair, fur or feathers and head size. Do not "
    "re-render or restyle the image."
)
RETRY_CLOSE = (" The previous attempt left the {region} partly open. Close it completely - no gap, and no teeth, "
               "tongue or inside of the mouth visible.")
RETRY = {
    "drift": " The previous attempt changed too much of the picture. This time change nothing except the "
             "{region}; leave every other pixel exactly as in the input image.",
    "weak": " The previous attempt barely changed anything. Make the change to the {region} clearly visible "
            "at a small size, while leaving everything else untouched.",
}

# ----------------------------------------------------------------------------- suit
BUTTONS = {
    "1-button": "single-breasted one-button jacket",
    "2-button": "single-breasted two-button jacket, top button fastened",
    "3-button": "single-breasted three-button jacket, middle button fastened",
    "3-roll-2": "single-breasted three-roll-two jacket (three buttons, the lapel rolls softly past the top "
                "button, fastened at the middle button)",
    "double-breasted-4": "double-breasted jacket with four buttons in two columns of two (4x2), fastened",
    "double-breasted-6": "six-button double-breasted jacket (6x2: two columns of three buttons), fastened",
    "double-breasted-6-classic": "classic six-button double-breasted jacket (6x2 with the top pair of buttons "
                                 "set wider apart), fastened",
}
LAPEL_WIDTH = {"slim": "slim", "standard": "medium-width", "wide": "wide"}
LAPEL = {"notch": "notch lapels", "peak": "peak lapels", "shawl": "a shawl lapel"}
POCKETS = {
    "jetted": "jetted hip pockets (clean slits, no flaps)",
    "flap": "flap hip pockets",
    "patch": "patch hip pockets",
    "ticket": "flap hip pockets with a small ticket pocket above the right one",
}
VEST_BUTTONS = {
    "4-button": "four-button", "5-button": "five-button", "6-button": "six-button",
    "double-breasted": "double-breasted",
}
VEST_LAPEL = {"no-lapel": "no lapels", "notch": "notch lapels", "peak": "peak lapels", "shawl": "a shawl lapel"}


def _none(v: str | None) -> bool:
    return not v or v.strip().lower() in ("none", "no", "n/a", "-")


def suit_text(suit: dict, fabric: dict | None) -> str:
    j = suit.get("jacket") or {}
    vest = suit.get("vest")
    if fabric:
        cloth = (
            f"{fabric.get('summary', '').rstrip('.')} - base colour {fabric.get('color_name', '')} "
            f"({fabric.get('color_hex', '')}), pattern: {fabric.get('pattern_desc') or fabric.get('pattern', 'solid')}"
        )
    else:
        cloth = "dark suiting cloth"
    pieces = "three-piece" if vest else "two-piece"
    jacket = BUTTONS.get(j.get("button_layout", ""), "single-breasted two-button jacket, top button fastened")
    lapel = f"{LAPEL_WIDTH.get(j.get('lapel_width', ''), 'medium-width')} {LAPEL.get(j.get('lapel_style', ''), 'peak lapels')}"
    if lapel.endswith("a shawl lapel"):
        lapel = "a " + lapel.replace(" a shawl lapel", " shawl lapel")
    pockets = POCKETS.get(j.get("pocket_style", ""), "flap hip pockets")
    parts = [
        f"Suit - render it exactly: a {pieces} suit in {cloth}.",
        f"Jacket: {jacket}, with {lapel} and {pockets}; a single breast pocket on the chest.",
    ]
    if vest:
        vb = VEST_BUTTONS.get(vest.get("button_layout", ""), "five-button")
        vl = VEST_LAPEL.get(vest.get("lapel_style", ""), "no lapels")
        hem = vest.get("hem_style", "pointed")
        where = (
            "its top edge shows in the V of the buttoned double-breasted jacket"
            if "double" in j.get("button_layout", "")
            else "visible in the V opening of the jacket"
        )
        parts.append(f"Waistcoat: matching cloth, {vb}, {vl}, {hem} hem - {where}.")
    shirt = (suit.get("shirt") or "white").strip().rstrip(".")
    parts.append(f"Dress shirt: {shirt}.")
    tie = (suit.get("tie") or "").strip().rstrip(".")
    if _none(tie):
        parts.append("No tie, open collar.")
    else:
        parts.append(f"Tie: {tie}{'' if 'tie' in tie.lower() else ' tie'}, neatly knotted.")
    sq = suit.get("pocket_square")
    parts.append("No pocket square." if _none(sq) else f"Pocket square in the breast pocket: {sq}.")
    return " ".join(parts)


WEAPON = re.compile(r"\b(knife|knives|swords?|daggers?|guns?|pistols?|rifles?|cleavers?|machetes?|axes?|katanas?|"
                    r"stab(?:s|bed|bing)?|weapons?|bombs?|grenades?|firearms?)\b", re.I)


WEAPON_CHECK = ("true if any weapon - knife, sword, gun, axe or anything similar - or a violent gesture is shown; a "
                "hockey stick or a harmless prop does not count")


def harmless(text: str) -> str:
    """Drop the clauses of a persona's visual text that call for a weapon: the image model draws whatever is named,
    whatever the rules say (a 'stabs a chef's knife into the podium' celebration came back holding the knife)."""
    pieces = re.split(r"(\s*[—–]\s*|,\s+|;\s+|\.\s+)", text.strip())
    out: list[str] = []
    for i in range(0, len(pieces), 2):
        if not WEAPON.search(pieces[i]):
            out.append((pieces[i - 1] if out and i else "") + pieces[i])
    kept = re.sub(r"^(?:and\s+)?then\s+|^and\s+", "", "".join(out).strip(" ,;—–"), flags=re.I)
    return kept[:1].upper() + kept[1:]


# ----------------------------------------------------------------------------- base + edit prompts
def base_prompt(style: str, persona: dict, fabric: dict | None, with_swatch: bool) -> tuple[str, str]:
    desc = harmless(persona["avatar_description"])
    kind = face_type(desc)
    primary, secondary = persona.get("primary_color", "#0E1B4D"), persona.get("secondary_color", "#E32402")
    setting = persona.get("setting", "rec_room")
    if setting == "rec_room":
        bg_place = ("a cozy wood-panelled basement rec room at a hockey draft party - warm string lights, a worn couch "
                    "and a glass-door drinks fridge with no labels or writing, softly out of focus" if style == "painted" else
                    "a cozy wood-panelled basement rec room at a hockey draft party - blocky string lights, a worn couch "
                    "and a glass-door drinks fridge with no labels or writing")
    elif setting == "studio":
        bg_place = ("a simple broadcast studio set - soft out-of-focus panels and lights"
                    if style == "painted" else "a simple broadcast studio set - plain panels and a few blocky lights")
    else:
        bg_place = ("a simple, softly painted hockey arena interior - blurred rink lights and stands"
                    if style == "painted"
                    else "a simple stylized hockey arena - plain rink boards with no advertising and a blocky pixel crowd")
    background = (
        f"Background: {bg_place}, in the franchise colours: mostly {colour(primary)} with {colour(secondary)} "
        f"accent lights and trim. Keep it simple and a little lower in contrast than the character, and add a "
        f"soft {colour_name(secondary)} rim light on the shoulders and head so the suit's silhouette separates "
        f"clearly from the background."
    )
    parts = [
        STYLE_INTRO[style],
        f"Character: {desc}",
        NOT_REAL,
        FACE[kind]["base"],
        "In THIS frame the mouth stays closed even if the description mentions an open mouth, a grin with teeth, "
        "fangs or a missing tooth - those show up in the talking frames made from this one.",
        suit_text(persona.get("suit") or {}, fabric),
    ]
    if re.search(r"\b(hold|holds|holding|carries|carrying|grips|clutches)\b", desc.lower()):
        parts.append(PROPS)
    parts += [FRAMING, background, STYLE_RULES[style], NO_TEXT]
    if with_swatch:
        parts.append(SWATCH_NOTE)
    return " ".join(parts), kind


CLOSE = {
    "human": "Change ONLY the mouth: close it - lips gently together, no teeth or inside of the mouth visible, "
             "the same friendly expression.",
    "beak": "Change ONLY the beak: close it fully - the upper beak rests on the lower beak with no gap and no "
            "visible inside of the mouth or tongue; any smile shows only as a slight upturn at the beak corners.",
    "muzzle": "Change ONLY the mouth: close it - lips together, no teeth, tongue or inside of the mouth visible.",
    "fish": "Change ONLY the mouth: close it - lips together, no gap.",
    "robot": "Change ONLY the mouth: close it - the jaw plate (or mouth slot) fully shut, no gap.",
}


def close_prompt(style: str, kind: str) -> str:
    """Fix-up edit for a base image that came back with the mouth/beak open."""
    region = FACE[kind]["mouth_region"]
    return ("Edit this image. " + CLOSE[kind] + " " + FACE[kind]["mouth_guard"]
            + KEEP.format(extra=STYLE_EXTRA[style], region=region) + STRICT.format(region=region))


MOUTH_WORD = {"human": "mouth", "beak": "beak", "muzzle": "mouth", "fish": "mouth", "robot": "mouth (jaw plate or slot)"}


def qa_prompt(kind: str) -> str:
    return (
        "You are checking one frame of a character portrait used for lip-sync animation. The character's "
        f"mouth is a {MOUTH_WORD[kind]}. Reply with ONLY a JSON object with these keys: "
        '"mouth_closed" (true only if it is fully closed: no gap, and no teeth, tongue or inside of the mouth '
        'visible), "mouth_open_amount" (0 = closed, 1 = slightly open, 2 = wide open), "eyes_open" (true if both '
        'eyes are open), "text_or_logo" (true if there are ANY letters, numbers, words, logos, emblems, crests, '
        'brand marks or watermarks anywhere in the image), "front_facing" (true if face and body face the '
        f'viewer), "weapon_visible" ({WEAPON_CHECK}), "notes" (at most 15 words).'
    )


def edit_prompt(style: str, kind: str, variant: str, retry: str | None = None) -> str:
    f = FACE[kind]
    region = f["eye_region"] if variant == "blink" else f["mouth_region"]
    guard = f["eye_guard"] if variant == "blink" else f["mouth_guard"]
    text = "Edit this image. " + f[variant] + " " + guard
    text += KEEP.format(extra=STYLE_EXTRA[style], region=region) + STRICT.format(region=region)
    if retry in RETRY:
        text += RETRY[retry].format(region=region)
    return text


# ----------------------------------------------------------------------------- fixed characters
FIXED = {
    "host": {
        "team": "host",
        "display": "The Commissioner",
        "persona": {
            "gm_name": "The Commissioner",
            "franchise_name": "League office",
            "primary_color": "#0E1B4D",
            "secondary_color": "#4770DB",
            "setting": "studio",
            "avatar_description": (
                "An original, fictional legendary arena play-by-play announcer, a man in his early fifties - a big, "
                "barrel-chested showman with a booming presence, the voice that makes a Game 7 overtime winner sound "
                "like the moon landing. Magnificent swept-back silver pompadour, thick dark expressive eyebrows, a "
                "strong square jaw, clean-shaven, bright eyes that go wide at big moments, and a huge camera-ready "
                "grin. A small broadcast earpiece in one ear. Leaning into the moment, larger than life, but "
                "impartial: he roots for no team, only for the call."
            ),
            "suit": {
                "fabric_id": "cavani-1100-11000-20",
                "jacket": {"lapel_style": "peak", "lapel_width": "standard", "button_layout": "2-button",
                           "pocket_style": "flap"},
                "shirt": "Crisp white, semi-spread collar",
                "tie": "Royal blue (#4770DB) silk",
                "pocket_square": "White linen, straight fold",
            },
        },
    },
    "autodraft": {
        "team": "autodraft",
        "display": "Autodraft (control bot)",
        "persona": {
            "gm_name": "Autodraft",
            "franchise_name": "League control bot",
            "primary_color": "#0E1B4D",
            "secondary_color": "#E32402",
            "avatar_description": (
                "A friendly, boxy retro robot GM: a rounded-rectangle head of brushed steel with a navy-blue "
                "faceplate, two large round eye lenses glowing soft white-blue with small metal eyelid shutters "
                "above them, a short antenna topped with a small red bulb, and a wide rectangular hinged jaw-plate "
                "mouth, closed in a friendly straight smile line. Simple rivets and rounded edges, a cheerful "
                "1950s toy-robot look - clearly a machine, helpful and approachable - with boxy metal hands."
            ),
            "suit": {
                "fabric_id": "cavani-1100-11000-8",
                "jacket": {"lapel_style": "peak", "lapel_width": "standard", "button_layout": "2-button",
                           "pocket_style": "jetted"},
                "shirt": "Crisp white, spread collar",
                "tie": "Solid red (#E32402) silk",
                "pocket_square": "Royal blue (#4770DB) silk, puff fold",
            },
        },
    },
}


# ----------------------------------------------------------------------------- gesture poses
LIMBS = {  # how arms / hands / pointing read for each face type
    "human": {"arms": "arms", "fists": "fists", "hand": "hand", "hands": "hands", "finger": "index finger"},
    "beak": {"arms": "wings (used like arms)", "fists": "wing-tips curled like fists", "hand": "wing-hand",
             "hands": "wing-hands", "finger": "wing-tip feather (used like a finger)"},
    "muzzle": {"arms": "arms", "fists": "paws clenched like fists", "hand": "paw", "hands": "paws",
               "finger": "clawed index finger"},
    "fish": {"arms": "fins (used like arms)", "fists": "fin-tips curled like fists", "hand": "fin", "hands": "fins",
             "finger": "fin-tip"},
    "robot": {"arms": "metal arms", "fists": "metal fists", "hand": "metal hand", "hands": "metal hands",
              "finger": "metal index finger"},
}
MOUTH = {  # (slightly open, wide open)
    "human": ("mouth slightly open", "mouth wide open"),
    "beak": ("beak slightly open", "beak wide open"),
    "muzzle": ("mouth slightly open", "mouth wide open"),
    "fish": ("mouth slightly open", "mouth wide open"),
    "robot": ("jaw plate slightly open", "jaw plate wide open"),
}
POSES = {
    "hype": {
        "talk": True,
        "desc": "a huge \"LET'S GO!\" celebration: both {arms} thrown up high with {fists}, {wide} yelling, eyes wide "
                "open with excitement (normal eyes - no glow, no fire effects)",
        "check": "both arms/wings raised high in celebration and the mouth wide open yelling",
    },
    "point": {
        "talk": True,
        "desc": "the chirp pose: one {arms_one} extended straight toward the viewer, {finger} pointing right at the "
                "camera (foreshortened), a smug grin with the {slight}; the other {hand} relaxed at the side",
        "check": "pointing straight at the viewer with a smug, cocky grin",
    },
    "shock": {
        "talk": False,
        "desc": "an \"OH NO!\" reaction: both {hands} clutching the top and sides of the head in disbelief, eyes wide, "
                "{wide} in shock - the {hands} must not cover the eyes or the mouth",
        "check": "hands/wings on the head in shock with the mouth open",
    },
    "celebrate": {
        "talk": False,
        "desc": "a big fist pump: one {fist_one} pulled down hard at chest height, the other {arm_one} at the side, "
                "{wide} shouting \"YES!\"",
        "check": "an excited celebration such as a fist pump",
    },
}
POSE_KEEP = (
    " Keep the SAME character (same face, species, colouring, hair, fur or feathers, glasses and accessories), the "
    "SAME outfit exactly (suit cloth and colour, lapels, buttons, shirt, tie, pocket square), the SAME background, "
    "lighting and art style{extra}. Keep the same camera, framing and scale: waist-up, square to the camera, the "
    "head in about the same place and the same size as in the input image; raised hands may reach the top edge. "
    "The face stays front-facing and clearly visible - nothing covers the eyes or mouth. Make the pose big, bold and "
    "instantly readable at a small size, like a sports-broadcast reaction. No text, logos, jerseys or watermarks."
)
POSE_RETRY = (
    " IMPORTANT: the previous attempt did not pass review. It must be unmistakably the same character wearing the "
    "same outfit as the input image, with the face clearly visible, and the pose must be exactly: {desc}. One single "
    "picture with the same waist-up framing - never a comic strip, collage or multiple panels."
)


def _one(word: str) -> str:
    """'wings (used like arms)' -> 'wing (used like an arm)'; 'metal arms' -> 'metal arm'."""
    return (word.replace("wings", "wing").replace("fins", "fin").replace("like arms", "like an arm")
            .replace("arms", "arm").replace("wing-tips curled like fists", "wing-tip curled like a fist")
            .replace("fin-tips curled like fists", "fin-tip curled like a fist")
            .replace("paws clenched like fists", "paw clenched like a fist").replace("fists", "fist"))


def pose_desc(kind: str, pose: str, persona: dict) -> str:
    L, (slight, wide) = LIMBS[kind], MOUTH[kind]
    celebration = harmless(persona.get("celebration") or "")
    if pose == "celebrate" and celebration:
        return (f"the single most iconic PEAK MOMENT of their signature celebration - one frozen frame, not the whole "
                f"sequence - from: \"{celebration.rstrip('.')}\". Big, excited face ({wide})")
    return POSES[pose]["desc"].format(
        arms=L["arms"], fists=L["fists"], hand=L["hand"], hands=L["hands"], finger=L["finger"], slight=slight,
        wide=wide, arms_one=_one(L["arms"]), fist_one=_one(L["fists"]), arm_one=_one(L["arms"]))


def pose_prompt(style: str, kind: str, pose: str, persona: dict, retry: bool = False) -> str:
    desc = pose_desc(kind, pose, persona)
    text = (f"Edit this image: change the character's pose and expression to {desc}." + POSE_KEEP.format(extra=STYLE_EXTRA[style])
            + " Keep it family-friendly: no weapons, knives, blades, guns or violent gestures; any prop must be harmless.")
    if retry:
        text += POSE_RETRY.format(desc=desc)
    return text


def pose_qa_prompt(kind: str, pose: str, persona: dict) -> str:
    want = POSES[pose]["check"]
    celebration = harmless(persona.get("celebration") or "")
    if pose == "celebrate" and celebration:
        want = (f"one clear peak moment of this celebration (a single frame cannot show the whole sequence, so judge "
                f"only whether it reads as part of it): {celebration}")
    return (
        "You are QA for an animated sports-show character. Image 1 is the character's reference portrait; image 2 "
        "is a new pose of the same character. Reply with ONLY a JSON object with these keys: "
        '"same_character" (true if image 2 is clearly the same character: same species, face, colouring and '
        'features), "same_outfit" (true if suit, shirt, tie and pocket square match image 1 in colour and style), '
        '"same_style" (true if the art style matches image 1), "single_scene" (true only if image 2 is ONE single '
        'picture with the same waist-up framing - false for a comic strip, collage, grid, multiple panels or several '
        'copies of the character), "face_visible" (true if eyes and mouth are clearly '
        f'visible, not covered), "pose_ok" (true if image 2 shows {want}), "mouth_open_amount" (0 closed, 1 slightly '
        'open, 2 wide open, in image 2), "text_or_logo" (true if ANY letters, numbers, words, logos or marks appear '
        f'in image 2), "weapon_visible" ({WEAPON_CHECK}, in image 2), "notes" (at most 15 words).'
    )
