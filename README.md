# broll — stop-motion B-roll from your own photos

Turn a photo into a paper-collage stop-motion clip. Nothing is added to the frame that
wasn't in the source photo.

**Rule:** never insert generated elements (stock mics, megaphones, props). Every piece in
the shot comes from the photo. Motion comes from the scene itself.

## Setup

- `GEMINI_API_KEY` in `~/.config/keys/.env` (its own Google Cloud project, billing on —
  image generation is not on the free tier, ~$0.04/image)
- `ffmpeg`, `python3` with `pillow`, `numpy`, `certifi`

## One command

```bash
python3 broll.py photo.jpg                    # photo in, mp4 out
python3 broll.py photo.jpg --pieces 6 --dur 4
python3 broll.py photo.jpg --reuse            # re-render from cached pieces, free
python3 broll.py photo.jpg --no-drift         # motion variants
```

It stylizes the scene, asks the model to name its own cut-out pieces, isolates and keys
each one (skipping any that fail rather than dying), derives the frame size from the
scene's aspect, and renders. Intermediates live in a `.work` dir beside the output, so
`--reuse` re-renders with different motion settings at no API cost.

## The pipeline (what the wrapper does, if you need to drive it by hand)

```bash
cd ~/Coding/broll

# 1. Stylize the whole scene (background kept, everything as layered cut-outs)
python3 stylize.py scene ~/path/to/photo.jpg out/scene.png

# 2. Look at scene.png, pick ~5 pieces worth moving, then pull each one.
#    Run isolate against the STYLIZED scene, never the original photo — that is what
#    keeps each piece in perfect register.
python3 - <<'PY'
import stylize as S
scene = "out/scene.png"
for subj, name in [
    ("standing man in the white t-shirt at the far left", "00-white-tee"),
    ("open laptop with the spreadsheet on its screen",    "01-laptop"),
]:
    S.isolate(scene, subj, f"out/iso/{name}.png")
PY

# 3. Key the chroma field to alpha (by saturation, not hue — see stylize.key_to_alpha)
# 4. Render
python3 - <<'PY'
import glob, stylize
from animate import render_pieces
for f in sorted(glob.glob("out/iso/*.png")):
    stylize.key_to_alpha(f, f.replace("/iso/", "/pieces/"))
render_pieces("out/scene.png", sorted(glob.glob("out/pieces/*.png")),
              "out/shot.mp4", dur=5.0, W=1440, H=1080, amp=0.5, step=2)
PY
```

Cost is roughly one generation for the scene plus one per piece, so ~$0.25 for a 5-piece
shot.

## Settings that are dialled in (don't change without a reason)

| Setting | Value | Why |
|---|---|---|
| `FPS` | 12 | Low on purpose. Smooth motion kills the paper feel. |
| `step` | 2 | Shoot on twos. Each pose holds 2 frames. Per-frame re-rolls read as film grain, not animation. |
| `amp` | 0.5 | Jitter distance. ~0.5px / 0.175°. More than this reads as agitated. |
| `PUSH_DEFAULT` | 0.065 | Slow eased zoom. The camera never jitters — only the pieces do. |
| pieces | ~5 | 2 is too subtle. |
| `assemble` | 1.0 | Pieces land over the first ~1.3s onto a knocked-out paper ground. Gives the shot an event. |
| `drift` | 14 | Slow travel across the shot on top of the boil. |

Distance (`BOIL_PX`/`BOIL_DEG` × `amp`) and rate (`FPS`, `step`) are independent knobs.

## Gotchas

- Key by **saturation**, not hue. The model returns ~`#04A943` for a requested `#00B140`
  and print grain spreads it further. Subjects are pure B&W, so `max(rgb)-min(rgb)` is an
  exact separator.
- Erode the alpha before feathering, and neutralise any surviving saturated pixel, or you
  get a green fringe where one piece borders another.
- Scale pieces to the **frame's** zoomed size, never their own dimensions.
- Rotate each piece about its own alpha-bbox centre, not the canvas centre.
- `blur_under` is off. It made a worse halo than the doubling it was meant to hide.
- Gemini segmentation masks are a dead end on this key — models either 404, time out, or
  return `"..."` in the mask field. `isolate()` exists because of that.

## v2: the shot has an event

`assemble` knocks every piece's region out of the base to flat paper stock, then the
pieces slide in and rebuild the scene. `drift` adds slow travel on top of the boil. Both
are on by default — Cam picked the combination over either alone.

## Two things that will bite you

**Baked-in paper margins.** Scene generation sometimes insets the collage on a page. That
border is visible in the shot. `margin_box()` detects and crops it off the scene and every
piece identically, and runs automatically.

**Dark scenes and the ground fill.** The assembly fills knocked-out piece regions with the
scene's paper colour. That can't be the plain modal colour: halftone spreads the cream
ground over hundreds of near-identical values while a silhouette is one solid tone, so on a
dark scene the mode is near-black and the holes fill with ink. `paper_colour()` quantises
first, then takes the most common *light* tone.

**Hallucinated content.** The model will invent detail to fill blurred or low-resolution
areas — it put a person in an empty window on a 275x183 source. `SCENE_PROMPT` now forbids
inventing anything not in the source and tells it to render low-detail areas as plain
shapes. Risk scales inversely with source resolution.
