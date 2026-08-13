# broll — stop-motion B-roll from a description

Describe a scene, get a paper-collage stop-motion clip. No photo required — though you can
feed one in instead if you want the tool to work from something real.

**Rule:** nothing appears in the frame beyond what you specify. No stock mics, megaphones,
or invented props filling space that wasn't asked for. Motion comes from the scene itself.

## Contents

- [Examples](#examples)
- [Setup](#setup)
- [Quick start](#quick-start)
- [Generating from a description](#generating-from-a-description)
- [Using a photo instead](#using-a-photo-instead)
- [Background colour](#background-colour)
- [Reusing a generation](#reusing-a-generation)
- [Variations](#variations)
- [Framing for square crops](#framing-for-square-crops)
- [One folder per video](#one-folder-per-video)
- [Settings that are dialled in](#settings-that-are-dialled-in)
- [Gotchas](#gotchas)
- [v2: the shot has an event](#v2-the-shot-has-an-event)
- [Billing](#billing)
- [The pipeline by hand](#the-pipeline-by-hand)

## Examples

Static previews can't show what this actually does — the point is the motion. All three
below were generated from a written description, no photo involved: `--text "..."` in,
still + clip out.

| Generated still | Result |
|---|---|
| <img src="examples/lock-source.png" width="360"> | <img src="examples/lock-demo.gif" width="360"> |
| <img src="examples/laptop-source.png" width="360"> | <img src="examples/laptop-demo.gif" width="360"> |
| <img src="examples/interview-source.png" width="360"> | <img src="examples/interview-demo.gif" width="360"> |

## Setup

- `GEMINI_API_KEY` in `~/.config/keys/.env` (its own Google Cloud project, billing on).
  Uses Google's Gemini image model (`gemini-3.1-flash-image`) — not on the free tier,
  ~$0.07/image, ~$0.40 for a finished clip.
- `ffmpeg`, `python3` with `pillow`, `numpy`, `certifi`

## Quick start

```bash
python3 broll.py --text "a hand closing an old padlock" --out lock.mp4
python3 broll.py --text "a hand closing an old padlock" --out lock.mp4 --reuse   # free re-render
```

It stylizes the scene, asks the model to name its own cut-out pieces, isolates and keys
each one (skipping any that fail rather than dying), derives the frame size from the
scene's aspect, and renders. Intermediates live in a `.work` dir beside the output, so
`--reuse` re-renders with different motion or colour settings at no API cost.

## Generating from a description

`--text "a description"` is the default path. Requires `--out`. The prompt is tuned for a
normal subject — a person, an object, two people.

NOT tuned for a wide establishing shot (a building, a skyline) — those want the subject
pulled back and centred, a different composition pattern that isn't built into `--text` yet.

One bug found and fixed the first time this shipped: a tight close-up subject (a hand on a
padlock) doesn't naturally fill a 16:9 frame, and the model filled the empty space with an
invented person nobody asked for. The prompt now explicitly forbids adding any subject
beyond what the description names, and tells it to fill empty space with plain scenery
instead.

## Using a photo instead

```bash
python3 broll.py photo.jpg                              # photo in, mp4 out
python3 broll.py "https://…/photo.jpg" --out shot.mp4    # remote source (--out required)
python3 broll.py photo.jpg --pieces 6 --dur 4
python3 broll.py photo.jpg --reuse                       # re-render from cached pieces, free
```

Same pipeline downstream — pieces get picked, isolated, and animated identically whether
the scene came from a description or a photo. The difference is only in how the first
stylized still gets made. A photo source also carries the stronger version of the rule
above: every piece in the shot must trace back to something in the photo, not just to
what you asked for.

## Background colour

`--bg E98F58` swaps the paper ground for any hex colour. This happens locally at render
time and costs nothing — do NOT regenerate a scene to change its colour.

It works because the subjects are pure black and white, so every pixel carrying saturation
is ground. Luminance is preserved, so paper grain and halftone texture survive. Grey props
stay grey, which reads as cut paper on a coloured page.

Cream is a weak default: it has almost no contrast with the white cut-out borders, which
are the signature of the style. Cam's brand colours are `E98F58`, `EC5C6B`, `8CBDDC`.

## Reusing a generation

`--work <dir>` points at an existing `.work` folder instead of one named after `--out`.
That is how you render variants — different colour, length or motion — off one paid
generation. `--reuse` refuses to run if the work folder has no scene in it, because
silently regenerating and charging for it happened twice in one afternoon.

```bash
python3 broll.py --work old.work --out orange.mp4 --bg E98F58 --reuse   # colour variant, free
python3 broll.py photo.jpg --no-drift                                    # motion variant
```

## Variations

`--from` takes a scene you already like and makes another in the same series. Same people,
same style, different setting.

```bash
python3 broll.py --from scenes/shot.png --change "same people, now in a kitchen" --out kitchen.mp4
```

Unlike the normal path this mode is ALLOWED to invent, since "add a character" is the point.

Two things it does badly. It copies the original composition unless told hard not to, so the
prompt demands new poses and new spacing — it obeys on poses and mostly ignores camera angle.
And it drifts: clothing and faces shift a little each generation. Fine for shots used at
different points in a video, risky for shots cut back to back.

**The script must come first.** Without knowing what a shot is FOR, a variation will happily
change the subject — asked for "same man, new room", it turned a man taking out the trash
into a man making coffee, which killed the point of the video.

## Framing for square crops

Videos often get cropped to square for LinkedIn and X. A square crop keeps the middle 56% of
a 16:9 frame. To survive it, a scene prompt needs all three of these:

1. the artwork fills the frame edge to edge, full bleed, no inset panel
2. people and key objects sit in the middle third
3. the left and right quarters show ordinary room background

Leave out (3) and the model renders empty cream bands instead of background. Leave out (1)
and it insets the whole picture in a panel.

Note this pulls against piece selection: pushing furniture to the edges makes the piece
picker more likely to choose furniture.

## One folder per video

```
~/Claude-files/broll-styles/01-podcast/
    transcript.txt
    scenes/   stills
    clips/    mp4s
    *.work/   intermediates (keep — makes --reuse free)
```

Work folders must sit beside the OUTPUT file. `--reuse` looks for `<out-path>.work`, so
putting the mp4 in `clips/` and the work folder at the root makes it silently regenerate.

## Settings that are dialled in (don't change without a reason)

| Setting | Value | Why |
|---|---|---|
| `FPS` | 12 | Low on purpose. Smooth motion kills the paper feel. |
| `step` | 2 | Shoot on twos. Each pose holds 2 frames. Per-frame re-rolls read as film grain, not animation. |
| `amp` | 0.5 | Jitter distance. ~0.5px / 0.175°. More than this reads as agitated. |
| `PUSH_DEFAULT` | 0.065 | Slow eased zoom. The camera never jitters — only the pieces do. |
| pieces | ~5 | 2 is too subtle. |
| `assemble` | 1.0 | Pieces land over the first ~1.3s onto a knocked-out paper ground. Gives the shot an event. |
| `drift` | 6 | Slow travel on top of the boil. Higher looks livelier but widens the paper gap (see below). |

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
- **Baked-in paper margins.** Scene generation sometimes insets the collage on a page. That
  border is visible in the shot. `margin_box()` detects and crops it off the scene and every
  piece identically, and runs automatically.
- **Dark scenes and the ground fill.** The assembly fills knocked-out piece regions with the
  scene's paper colour. That can't be the plain modal colour: halftone spreads the cream
  ground over hundreds of near-identical values while a silhouette is one solid tone, so on a
  dark scene the mode is near-black and the holes fill with ink. `paper_colour()` quantises
  first, then takes the most common *light* tone.
- **Drift vs the paper gap.** The assembly knocks a hole in the base for each piece. That hole
  is deliberately dilated by `6 + drift` px, because `key_to_alpha` erodes the piece ~2px and
  `drift` walks it away from home — without the dilation the piece slides off its own hole and
  exposes a doubled copy of itself. The cost is a visible paper gap around each piece that
  grows with drift. Cam picked drift=6 as the balance.
- **Large headline text cannot be removed.** Small incidental writing (papers, laptop screens,
  whiteboards) gets scrambled reliably. Big sharp headline text does not — tested with the text
  rule as a priority-one override AND as a dedicated second image-to-image pass, both no effect,
  with the model claiming success each time. If text is the point of a photo source, pick
  another photo.
- **Hallucinated content.** The model will invent detail to fill blurred or low-resolution
  areas of a photo source — it put a person in an empty window on a 275x183 source. The scene
  prompt forbids inventing anything not in the source and tells it to render low-detail areas
  as plain shapes. Risk scales inversely with source resolution.

## v2: the shot has an event

`assemble` knocks every piece's region out of the base to flat paper stock, then the
pieces slide in and rebuild the scene. `drift` adds slow travel on top of the boil. Both
are on by default — Cam picked the combination over either alone.

## Billing

The project is on **prepaid credits**. When they run out the API returns a 429 whose message
says "prepayment credits are depleted" — `post_json` detects that and fails immediately
rather than retrying, since it will never resolve on its own. Top up or switch to
pay-as-you-go at https://ai.studio/projects.

## The pipeline by hand

Everything above runs through `broll.py`. This is what it's doing underneath, if you ever
need to drive it manually.

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

Cost is roughly one generation for the scene plus one per piece, so ~$0.25–0.45 for a
5-piece shot depending on scene size.
