# Character rig schema — `gds-rig/1`

One rig per speaker: `media/assets/avatars/<team>/rig/rig.json`.
The manifest points at it: `media/assets/avatars/manifest.json` → `teams.<team>.rig` (path relative to
`media/assets/avatars/`), plus `teams.<team>.rig_kind` = `"rig"` (real) or `"mock"` (built from the old
whole-image frames, for integration only). A real rig replaces the mock **at the same path**; the driver never
has to change paths.

Everything is pixel art at the character's **native grid** (qwen/gemini/host/autodraft: 256×256 ×3;
fugu/deepseek/mimo/fable/glm: 192×192 ×4). All coordinates and images in a rig are **native pixels**. Render at
native size and scale the finished frame by `native.scale` with nearest-neighbour (`image-rendering: pixelated`),
or upscale every layer by `scale` first. Never smooth-scale a layer.

All file paths inside `rig.json` are **relative to the folder that holds `rig.json`**.

## Compositing (one video frame)

```
canvas = native.w × native.h
1. background            bg.png (static plate; optional — you may use your own set)
2. body frame            gestures[g].frames[i].file        RGBA, full canvas, drawn at (0,0)
3. eye patch             eyes[<state>]                      RGBA, head-crop size, drawn at P
4. mouth patch           visemes[<mood>][<viseme>]          RGBA, head-crop size, drawn at P
5. occluder (if any)     gestures[g].frames[i].occluder     RGBA, full canvas, drawn at (0,0)
6. scale × native.scale  nearest-neighbour

P = (head.box[0] + head_offset[0], head.box[1] + head_offset[1])
```

* Steps 3–5 apply only when the frame's `"face"` is `"rig"`. A `"baked"` frame (mock rigs only) has its own face
  drawn in: do not paint patches on it (the mouth cannot move during it — use it for short reaction holds).
* Every `"rig"` body frame already contains the **canonical head** (mouth at rest, eyes open) at `head_offset`, so
  "no patch" = rest mouth + open eyes. Patches only contain the pixels that change (alpha is 0 or 255, never
  partial) and never overlap each other (eye patches live above the nose, mouth patches below it).
* The occluder holds the body-frame pixels of a hand/arm/can that lie over the face, so a hand in front of the
  face stays in front of the moving mouth/eyes. `null` = nothing over the face.

## rig.json

```jsonc
{
  "schema": "gds-rig/1",
  "team": "qwen",
  "kind": "rig",                       // "rig" | "mock"
  "generated_at": "2026-09-27T05:00:00+00:00",
  "face_type": "human",                // human | robot | beak | muzzle | fish (mouth wording only)
  "native": {"w": 256, "h": 256, "scale": 3, "out": 768},
  "background": "bg.png",              // native, opaque, static
  "body_alpha": "opaque",              // "opaque": body frames include the room (identical to bg outside the
                                       //  character); "keyed": body frames are transparent outside the character
  "pointing": "screen-left",           // side of the screen the pointing hand is on / points toward
                                       //  ("screen-left" | "screen-right"); point gesture aims at the viewer

  "head": {
    "box": [68, 7, 126, 87],           // x0,y0,x1,y1 in canvas px (base pose), TIGHT: hair top .. chin, ear to
                                       //  ear, containing every patch. Patch origin. x1/y1 exclusive
    "image": "head.png",               // canonical head crop (RGBA, box size; alpha = the character's pixels,
                                       //  0 = room behind the head) — identical in every rig frame
    "face_mask": "face_mask.png",      // L, box size: 255 where any face patch may paint (union of patches)
    "mouth_anchor": [64, 94],          // head-crop coords: centre of the closed lips
    "mouth_box": [44, 84, 86, 110],    // head-crop coords: union bbox of all mouth patches
    "eye_line": 62,                    // head-crop y of the pupils
    "eyes_box": [40, 50, 88, 70],      // head-crop coords: union bbox of all eye/brow patches
    "eye_centres": [[52, 62], [76, 62]],// head-crop coords, screen-left eye first
    "generation_crop": [49, 1, 145, 97] // internal: the square crop the face sheets were drawn on (ignore)
  },

  "visemes": {                         // mood -> viseme -> patch (RGBA, head-crop size)
    "neutral": {
      "rest": "mouth/neutral/rest.png",        // silence (usually an empty patch = canonical mouth)
      "closed": "mouth/neutral/closed.png",    // M B P — lips pressed
      "small_open": "...",                     // most consonants, short E/I
      "medium_open": "...",                    // short A, long E/I, ai/ay/au/aw
      "wide_open": "...",                      // stressed/long A, shouted vowels
      "round": "...",                          // O U W, oo/ou/ow/wh/qu
      "teeth": "...",                          // F V (ph)
      "laugh": "...",                          // big open smile (laugh tags)
      "smirk": "..."                           // closed-mouth smirk (sarcasm)
    },
    "happy": { "...": "same keys" },   // grinning while talking (rest = closed grin); real rigs have it
    "angry": { "...": "same keys" }    // scowling/snarling while talking (rest = tight frown)
    // a mood's round/teeth/laugh/smirk may point at the neutral files: that is intended
  },
  "viseme_fallback": {"round": "medium_open", "teeth": "small_open", "laugh": "wide_open", "smirk": "rest",
                      "closed": "rest", "small_open": "medium_open", "medium_open": "wide_open"},
                                       // resolve a missing viseme by following this chain; a missing mood
                                       //  falls back to "neutral" visemes (keep the mood's eyes)

  "eyes": {                            // state -> patch (RGBA, head-crop size); eyes + brows band
    "open": "eyes/open.png",           // canonical (empty patch)
    "blink": "eyes/blink.png",         // fully shut
    "half": "eyes/half.png",           // half-lid (blink in-between, bored/sly)
    "happy": "eyes/happy.png",         // smiling squint (pair with laugh / happy visemes)
    "angry": "eyes/angry.png",         // brows down and in
    "surprised": "eyes/surprised.png", // brows up, eyes wide
    "skeptical": "eyes/skeptical.png", // one brow up
    "look_left": "eyes/look_left.png", // pupils toward the SCREEN's LEFT edge (the character's own right)
    "look_right": "eyes/look_right.png"// pupils toward the SCREEN's RIGHT edge (the character's own left)
  },
  // LEFT / RIGHT ARE ALWAYS SCREEN DIRECTIONS in this schema (look_*, pointing, "LEFT side of the picture").
  // Glance sprites are MEASURED, never trusted from the prompt: the (look_left, look_right) pair is chosen so that
  // the renderer's own white-of-the-eye measure (gmbench_media/rig.py look_directions) reads -1 / +1 AND the iris
  // centroid does not disagree; robot lenses get drawn pupils. qa.face.look_*: renderer [dir, white shift], iris_dx
  // [screen-left eye, screen-right eye] (native px, negative = toward screen-left). rig.py validate enforces it; a
  // direction nothing reads correctly is left out (eyes_fallback -> open).
  "eyes_fallback": {"half": "blink", "happy": "open", "angry": "open", "surprised": "open",
                    "skeptical": "open", "look_left": "open", "look_right": "open"},

  "gestures": {
    "idle_breathe": {
      "fps": 3, "loop": true, "hold": null, "exit": "cut",
      "frames": [
        {"file": "body/idle_breathe_0.png", "head_offset": [0, 0], "occluder": null, "face": "rig",
         "hand_over_face": false},
        {"file": "body/idle_breathe_1.png", "head_offset": [0, -1], "occluder": null, "face": "rig",
         "hand_over_face": false}
      ]
    },
    "point": {
      "fps": 9, "loop": false, "hold": 1, "exit": "reverse",
      "phases": {"in": [0, 1], "hold": [1, 2], "out": [0]},   // explicit in / hold / out frame lists
      "hold_fps": 3,                                           // cycle phases.hold at this rate while holding
      "frames": [ /* 0 = in-between, 1 = key pose, 2 = key pose with upper body 1 px up (head_offset [0,-1]) */ ]
    }
    // talk_hands (6-frame loop with ±1 px head bob), point, fist_pump, shrug, laugh (loop), thumbs_up, open_arms,
    // drink_sip, facepalm, react_shock (listener: hands up "whoa"), react_happy (listener: clapping loop with bob)
  },
  "transitions": {                     // optional, real rigs: smooth the pop from the base pose into a gesture
    "enter_from_base": {"to": "talk_hands", "fps": 12, "frames": [ /* 4 in-betweens: base pose -> talk_hands[0] */ ]},
    "exit_to_base": {"from": "talk_hands", "fps": 12, "frames": [ /* the same 4, reversed */ ]}
  },
  "gesture_fallback": {"fist_pump": "point", "thumbs_up": "point", "open_arms": "shrug", "drink_sip": "idle_breathe",
                       "talk_hands": "idle_breathe", "laugh": "idle_breathe", "shrug": "idle_breathe",
                       "facepalm": "shrug", "react_shock": "shrug", "react_happy": "laugh",
                       "shock": "react_shock", "celebrate": "react_happy"},   // aliases: shock / celebrate resolve

  "qa": {
    "status": "ok",                    // ok | partial | mock
    "head_identical": true,            // every rig body frame: head pixels == head.png (outside occluders)
    "max_head_diff_px": 0,
    "vision": {"same_character": true, "text_or_logo": false, "weapon": false, "notes": "..."},
    "viseme_source": "gemini-inpaint", // where the mouth shapes came from
    "flags": [],
    "preview": "preview.gif",          // flip-book of every viseme, eye state and gesture
    "contact_sheet": "preview.png",
    "cost_usd": 0.0, "pixellab_generations": 0
  }
}
```

## Gesture clip semantics

* Clip frames do **not** include the neutral pose: idle is `idle_breathe`. Enter a clip from idle at frame 0.
* `loop: true` (idle_breathe, talk_hands, laugh): cycle frames 0..n-1 at `fps` for as long as needed.
* `loop: false`: play 0..`hold`, **hold** frame `hold` while the line needs it (the face keeps lip-syncing through
  the patches), then `exit`. Real rigs also give `phases` = {"in": [...], "hold": [...], "out": [...]}: play `in`
  once at `fps`, cycle `hold` at `hold_fps` (a 1-px breathing bob so a held pose never freezes), play `out` at `fps`,
  then idle. `phases` and `hold`/`exit` describe the same clip; use whichever your driver prefers.
  * `"reverse"` — play `hold-1 .. 0` back down, then return to idle (point, fist_pump, thumbs_up, open_arms, shrug)
  * `"forward"` — play `hold+1 .. n-1`, then idle (drink_sip: lower the can)
  * `"cut"` — straight back to idle
* `head_offset` is per frame ([dx, dy] native px, usually 0 or ±1): the face patches move with the head. Apply it
  to P above; the body frame already has the head drawn at that offset.
* `occluder` (per frame, or null) + `hand_over_face` (true when the occluder actually covers the mouth/eye zones,
  e.g. `drink_sip` can at the mouth, `facepalm` hand over the eyes): draw the occluder AFTER the face patches.
* Frames may repeat inside a clip (`talk_hands` = 0,1^,0,2v,0^,3 around a centre pose, where ^ / v are the same
  pose with the upper body and head 1 px up / down, recorded in `head_offset`); `fps` can be fractional
  (`idle_breathe` 1.2 fps = one breath every ~1.7 s).
* Listener clips are built for the reaction cam (the show's `crop="bust"`: a 145 native px square, ~1.3
  head-heights on 256-native rigs, from just above the hair). `react_shock` = the "oh no!" face-clutch (hands on
  the cheeks) or hands up beside the face, held with a bob. `react_happy` = the best of, in order: applause beside
  the face / two thumbs-up beside the cheeks / hands-up bounce / a hands-free laughing bounce (the face does the
  work) - whichever keeps the hands inside the crop. `qa.reach.<frame>.inside_pct` = share of hand pixels inside
  the crop; `qa.react_happy` names the variant. Pair them with `eyes.surprised` / `eyes.happy` and a `wide_open` /
  `laugh` mouth.
* `transitions.enter_from_base` (4 frames, 12 fps): play it when a character leaves the base pose (crossed arms,
  hands in pockets, a can in hand) for talk_hands or any gesture; `exit_to_base` when returning to idle. Their last /
  first frame is next to `talk_hands[0]`; from there other clips' frame 0 is a small move.
* Suggested use: `talk_hands` under normal speech; `point` for chirps at a rival/the camera; `fist_pump` on a
  good pick; `shrug` for "eh, whatever"; `laugh` with `[laughs]` tags (pair with `eyes.happy` + `visemes.*.laugh`;
  its frames bob the whole character 1 px, head_offset included); `open_arms` for "boys!"; `thumbs_up`;
  `drink_sip` for pauses between lines (do not lip-sync while sipping: the can covers the mouth); `facepalm` for a
  bad pick by someone else (eyes are covered, the mouth keeps talking under the hand).
* Every rig frame except `idle_breathe` is a hand/arm pose over the SAME head: switching gestures never changes the
  face. Clips are short (2 frames + hold); chain them freely, e.g. point → hold while talking → reverse → talk_hands.

## Driving the face from text (the lip-sync contract)

`media/gmbench_media/lipsync.py` produces one viseme per show frame from the TTS character alignment
(`rest closed small_open medium_open wide_open round teeth laugh smirk`). Those names are exactly the keys of
`visemes.<mood>`. Eyes: blink = `half` → `blink` → `half` (one frame each at 30 fps) every 2–6 s; hold a mood's
eyes (`angry`, `happy`, `skeptical`, ...) for whole phrases; `look_left` / `look_right` to react to whoever is
talking on that side of the screen.

## Real rigs: how they are made (so QA flags make sense)

* Face: the head crop is edited as a 2×2 sheet of identical copies (4 mouth shapes or 4 eye states per call); each
  copy is sampled back to the native grid, aligned to the canonical head, snapped to the character's palette, and
  only the pixels that changed **inside the mouth zone / eye-brow zone** become the patch. Outside the zone nothing can
  change, by construction.
* Body: gesture frames come from 2×2 sheets of the full portrait at 2K (4 frames per call, in-betweens drawn next to
  their key pose). Each frame keeps only the moving parts (arms, hands, can, the torso they reveal); head and room
  are the base pixels. `qa.head_identical` / `qa.max_head_diff_px` is the pixel proof.
* `idle_breathe_1` and the `laugh` bob frames are exact 1-px moves of the character silhouette.
* `qa.vision` is a cheap vision-model review of all frames (same character/outfit, no text/logos/weapons, broken
  hands); `qa.flags` lists anything a human should look at in `preview.png`.

## Tools

* `uv run --with numpy --with pillow python media/avatars/rig.py validate [teams]` — every referenced file exists
  at the right size (all rigs pass).
* `media/avatars/rig.py` also has `Rig(rig_dir).compose(gesture, frame, eyes=..., mood=..., viseme=...)`, the
  reference compositor used for the previews (returns a native RGBA array; scale ×native.scale nearest-neighbour).
* Build / rebuild: `uv run media/avatars/generate.py --personas <personas.json> --rig [--teams ...] --budget N`
  (idempotent: cached sheets in `<team>/rig/raw/`, rebuilt layers cost $0; `--rig-redo body_c` remakes one sheet).

## Background

`bg.png` and every body frame share one static room. Readable text on background signs (qwen's and glm's "BEER"
fridge) is repainted as plain surface by one edit whose changes are kept only well away from the character
(`qa.bg_text`); character pixels are never touched.

## Mock rigs

`kind: "mock"` rigs are built only from the existing whole-image frames (base / mid / wide / blink / poses):
visemes rest/closed/smirk = empty patch, small_open/medium_open/round/teeth = the old "mid" mouth, wide_open/laugh
= the old "wide" mouth; eyes open/blink only (+ fallbacks); `idle_breathe` = the base frame; the four old poses
are single-frame `"face": "baked"` gestures (hype → fist_pump, point, shock, celebrate). Same file layout and keys,
so a driver written against a mock works unchanged on the real rig.
