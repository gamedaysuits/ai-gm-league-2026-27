"""Plain-word helpers for spoken host lines. Text only: no logos, no marks, just how a host says it."""

from __future__ import annotations

# Spoken form of NHL club abbreviations (the screen shows the abbreviation text only).
CLUB_SPOKEN = {
    "ANA": "Anaheim", "ARI": "Arizona", "BOS": "Boston", "BUF": "Buffalo", "CAR": "Carolina",
    "CBJ": "Columbus", "CGY": "Calgary", "CHI": "Chicago", "COL": "Colorado", "DAL": "Dallas",
    "DET": "Detroit", "EDM": "Edmonton", "FLA": "Florida", "LAK": "Los Angeles", "LA": "Los Angeles",
    "MIN": "Minnesota", "MTL": "Montreal", "NJD": "New Jersey", "NJ": "New Jersey", "NSH": "Nashville",
    "NYI": "the Islanders", "NYR": "the Rangers", "OTT": "Ottawa", "PHI": "Philadelphia", "PIT": "Pittsburgh",
    "SEA": "Seattle", "SJS": "San Jose", "SJ": "San Jose", "STL": "St. Louis", "TBL": "Tampa Bay",
    "TB": "Tampa Bay", "TOR": "Toronto", "UTA": "Utah", "UTAH": "Utah", "VAN": "Vancouver",
    "VGK": "Vegas", "VEG": "Vegas", "WPG": "Winnipeg", "WSH": "Washington",
}

POS_DISPLAY = {"C": "C", "L": "LW", "LW": "LW", "R": "RW", "RW": "RW", "D": "D", "G": "G", "F": "F"}
POS_SPOKEN = {
    "C": "centre", "L": "left wing", "LW": "left wing", "R": "right wing", "RW": "right wing",
    "D": "defenceman", "G": "goaltender", "F": "forward",
}

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
         "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_ORD_IRREG = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
              "nine": "ninth", "twelve": "twelfth"}


def number_words(n: int) -> str:
    if n < 0:
        return "minus " + number_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] + ("" if o == 0 else "-" + _ONES[o])
    if n < 1000:
        h, r = divmod(n, 100)
        return _ONES[h] + " hundred" + ("" if r == 0 else " and " + number_words(r))
    th, r = divmod(n, 1000)
    return number_words(th) + " thousand" + ("" if r == 0 else " " + number_words(r))


def ordinal_words(n: int) -> str:
    words = number_words(n)
    head, sep, last = words.rpartition(" ")
    if "-" in last:
        a, _, b = last.rpartition("-")
        last = a + "-" + _ord_word(b)
    else:
        last = _ord_word(last)
    return (head + sep + last) if head else last


def _ord_word(w: str) -> str:
    if w in _ORD_IRREG:
        return _ORD_IRREG[w]
    if w.endswith("y"):
        return w[:-1] + "ieth"
    return w + "th"


def ordinal_suffix(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def club_spoken(abbrev: str) -> str:
    return CLUB_SPOKEN.get((abbrev or "").upper(), " ".join(abbrev or "").strip())


def pos_display(pos: str) -> str:
    return POS_DISPLAY.get((pos or "").upper(), (pos or "").upper())


def pos_spoken(pos: str) -> str:
    return POS_SPOKEN.get((pos or "").upper(), "skater")


def join_words(items: list[str], conj: str = "and") -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f", {conj} " + items[-1]
