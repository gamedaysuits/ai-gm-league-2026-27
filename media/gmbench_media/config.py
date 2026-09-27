"""Show configuration: built-in defaults <- media/show.yaml <- profile overrides <- CLI overrides."""

from __future__ import annotations

import copy
from typing import Any

import yaml

from .paths import SHOW_YAML

from .tags import ALLOWED

# Delivery tags allowed in tts_text (ElevenLabs v3 audio tags): the league allowlist. GM lines keep their own
# allowlisted tags; anything else is stripped before synthesis; tags never reach display_text / captions.
SAFE_TAGS = list(ALLOWED)

DEFAULTS: dict[str, Any] = {
    "fps": 30,
    "sample_rate": 48000,
    "width": 1920,
    "height": 1080,
    "show_title": "Draft Night",
    "show_kicker": "GM-Bench · The Suits 2026–27",
    "script": {
        "format": "party",  # party: host cue -> the GM's own two-beat call (react to the last pick, then mine) | broadcast
        "target_minutes": 52.0,  # Monday: 45-60 min
        "est_chars_per_sec": 11.7,  # fitting estimate; measured on eleven_v3 with the tagged cast (Sep 27): 11.7 chars/s
        "est_gap_s": 0.18,
        "cold_open": True,
        "cold_open_montage": 5,  # hype montage of the best GM calls/chirps (their own words, first sentence(s))
        "montage_max_chars": 110,
        "title_hold_s": 2.4,
        "meet": True,
        "meet_read": "both",  # tagline | catchphrase | both (used when a persona has no signature_call)
        "meet_gms_limit": None,
        "meet_opener": True,
        "cold_open_teasers": 2,
        "previously_on": "auto",  # auto | true | false  (reads exports/history/*.md when present)
        "previously_on_max_items": 4,
        "draft_order": True,
        "round1_on_the_clock": True,
        "round1_intro": True,
        "round1_picks_limit": None,
        "reactions_per_pick": None,  # None = all reactions tied to a pick
        "reactions_total_limit": None,
        "close_summary": True,
        "round_breaks": True,
        "condensed_rounds": True,
        "trash_tape": True,
        "trash_tape_lines": 5,
        "tape_max_chars": 180,
        "close": True,
        "round_end": True,  # "THAT'S ROUND N!" round-summary beat (board + grid, the Commissioner celebrates)
        "report_card": True,  # post-draft Report Card closer (needs exports/grades.json or REPORT_SUBMITTED events)
        "report": {"ends": None, "middle": "full", "bottom_comments": 2, "top_comments": 2, "comments": 1,
                   "delusion": 3, "humble": 1},
        "rounds_limit": None,  # e.g. 3 = only rounds 1..3
        "max_gm_line_chars": 520,  # longer GM lines are trimmed at a sentence boundary (logged)
        "host_polish": {"enabled": False, "model": "cohere/command-a-plus"},  # non-competing; template-only by default
        "director": {"mode": "rules", "model": None},  # rules | llm (llm may only add SAFE_TAGS)
    },
    "tts": {
        "engine": "say",
        "line_lufs": -19.0,
        "trim_silence_db": -50.0,
        "head_pad_ms": 20,
        "tail_pad_ms": 40,
        "say": {"rate_wpm": 200, "hype_rate_wpm": 225},
        "elevenlabs": {
            "model_id": "eleven_v3",
            "output_format": "mp3_44100_128",
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
            "timestamps": True,  # character alignment -> word-exact captions, pops and name slams (falls back)
            "language_code": "en",
            "timeout_s": 120,
        },
        "safe_tags": SAFE_TAGS,
    },
    "mix": {
        "seed": 20260928,
        "gap_ms": [60, 200],  # hockey-bro pacing: tight
        "overlap_ms": [120, 320],  # reactions jump in over the tail of the previous line (crosstalk)
        "roast_gap_ms": [180, 320],  # room for the crowd OOOH after a chirp
        "beat_ms": [380, 620],  # suspense beat: "...ON THE CLOCK!" -> beat -> the call
        "pick_gap_ms": [160, 300],
        "segment_gap_ms": [450, 750],
        "montage_gap_ms": [90, 160],
        "cutaway_ms": [1000, 1250],  # after a report-card roast: hold on the roasted GM (shock / celebrate)
        "cue_ms": [120, 260],  # "Ponoka, you're up!" -> the GM's call
        "sfx_style": "party",  # party: the room cheers / groans / laughs, horn only for the big moments | arena
        "preroll_s": 0.6,
        "postroll_s": 3.0,
        "room_tone_dbfs": -60.0,
        "music": {"path": None, "gain_db": -21.0, "duck": True},
        "theme": "media/assets/music/theme-30s.mp3",  # opens and closes
        "bed": "media/assets/music/bed-30s.mp3",  # looped under segments, ducked
        "theme_lufs": -19.0,
        "bed_lufs": -27.0,
        "music_duck_db": 10.0,
        "crowd_lufs": -33.0,
        "sfx": True,
        "master_lufs": -16.0,
        "master_tp": -1.5,
        "master_lra": 11.0,
    },
    "avatars": {
        "manifest": "avatars.json",
        "blink_interval_s": [2.6, 6.2],
        "blink_frames": 4,
        "mid_threshold": 0.26,
        "open_threshold": 0.52,
        "hold_frames": 2,
        "lead_frames": 1,
    },
    "captions": {"font_px": 44, "max_width_px": 1300, "max_lines": 2, "hold_s": 0.25, "font": "archivo",
                 "weight": 800, "width": 100, "side_font_px": 48, "side_width_px": 852},
    "motion": {"bob_px": 16, "tile_bob_px": 5, "squash": 0.06, "shake_px": 12, "zoom": 1.07, "confetti": 44,
               "slam": True},
    "render": {
        "chunk_max_s": 150.0,
        "quality": "standard",
        "crf": 19,
        "workers": "auto",
        "jobs": 2,
        "hyperframes": "hyperframes@0.8.23",
        "audio_bitrate": "192k",
    },
    "profiles": {
        "full": {},
        # COMPLETE Draft Night for YouTube: the record of the night. Every pick, every comeback, all table talk, the
        # Report Card and the Delusion Index; no fitting and no trims (only a pick with unverified facts is called by
        # the host); a cold-viewer intro; the comic-timing pass (beat map) on every GM line.
        "complete": {
            "script": {"complete": True, "comic_timing": True, "cold_viewer_intro": True, "cold_open_montage": 0,
                       "trash_tape": False, "max_gm_line_chars": 4000, "target_minutes": 999.0},
        },
        # The 2-3 minute acceptance cut: cold open -> meet 3 GMs -> first 4 picks (statements + reactions) -> close.
        # ~60 s Round-1 clip: three picks, full Game-7 treatment (clock -> beat -> call -> GM -> crosstalk)
        "r1clip": {
            "script": {
                "cold_open": False, "meet": False, "draft_order": False, "previously_on": False,
                "round1_picks_limit": 3, "round_breaks": False, "condensed_rounds": False, "trash_tape": False,
                "close": False, "max_gm_line_chars": 150,
            },
            "mix": {"preroll_s": 0.8, "postroll_s": 2.5},
        },
        # Round-1 highlight cut (energy-1 moments; set script.highlights.moments to curate, else auto top-N)
        "r1high": {
            "script": {
                "mode": "highlights",
                "max_gm_line_chars": 110,
                "highlights": {
                    "round": 1, "max_chars": 105, "table_talk": 0, "button": True,
                    "moments": [
                        {"pick": 1},
                        {"pick": 2},
                        {"pick": 5, "full": True},
                        {"pick": 6, "full_says": [52]},
                        {"pick": 9},
                        {"pick": 10, "full_says": [63]},
                    ],
                },
            },
            "mix": {"preroll_s": 0.4, "postroll_s": 2.6, "theme_open_s": 6.0, "theme_end_s": 4.0, "tempo": 1.12},
        },
        # Report Card preview: the full-show closer on its own (bottom 2 + top 2 in full, the middle as a blitz)
        "report": {
            "script": {"mode": "report", "max_gm_line_chars": 220,
                       "report": {"ends": 2, "middle": "blitz", "bottom_comments": 2, "top_comments": 2, "comments": 1,
                                  "delusion": 3, "humble": 1}},
            "mix": {"preroll_s": 0.5, "postroll_s": 3.0, "theme_open_s": 5.0, "theme_end_s": 4.5, "tempo": 1.12},
        },
        # Round-1 highlight cut for any run: the n best picks by chirps/energy, in draft order (no hand-curated list)
        # Draft-party highlight cut: the best chains of consecutive picks (each GM plays off the last). Pin chains in
        # show.yaml with script.highlights.chains: [[5, 6, 7, 8], [11, 12, 13]].
        "highlights": {
            "script": {"mode": "highlights", "max_gm_line_chars": 1000,
                       "highlights": {"round": 1, "n_chains": 2, "chain_len": 3, "reactions": 0, "button": True}},
            "mix": {"preroll_s": 0.4, "postroll_s": 2.6, "theme_open_s": 6.0, "theme_end_s": 4.0, "tempo": 1.08},
        },
        # vertical shorts: hook -> moment; built by `short` (spec from the CLI)
        "short": {
            "script": {"max_gm_line_chars": 400},
            "mix": {"preroll_s": 0.35, "postroll_s": 4.3, "theme_end_s": 4.0, "cutaway_ms": [1500, 1650]},
            "captions": {"font_px": 62, "max_width_px": 960, "max_lines": 2, "hold_s": 0.2},
        },
        "acceptance": {
            "script": {
                "meet_gms_limit": 3,
                "meet_opener": False,
                "cold_open_teasers": 0,
                "reactions_total_limit": 1,
                "close_summary": False,
                "round1_picks_limit": 4,
                "round_breaks": False,
                "condensed_rounds": False,
                "previously_on": False,
                "draft_order": False,
            },
            "mix": {"postroll_s": 3.0, "gap_ms": [110, 380], "pick_gap_ms": [480, 720], "segment_gap_ms": [950, 1350]},
        },
    },
}


def deep_merge(base: dict, over: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(profile: str = "full", overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if SHOW_YAML.exists():
        cfg = deep_merge(cfg, yaml.safe_load(SHOW_YAML.read_text()) or {})
    profiles = cfg.get("profiles", {})
    if profile not in profiles:
        raise SystemExit(f"unknown profile {profile!r}; known: {', '.join(sorted(profiles))}")
    cfg = deep_merge(cfg, profiles[profile])
    cfg = deep_merge(cfg, overrides or {})
    cfg["profile"] = profile
    return cfg
