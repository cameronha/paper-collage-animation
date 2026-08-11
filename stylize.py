#!/usr/bin/env python3
"""
Stylize an image (or generate an element from text) via the Gemini image models,
in Cam's locked B-roll style. Reads GEMINI_API_KEY from ~/.config/keys/.env so the
key never lands in argv or shell history.

Subjects come back as black-and-white halftone cut-outs on a flat chroma field,
so key_to_alpha() can lift them to transparent PNGs exactly (the subject contains
no saturated color, so there is nothing to spill).

Usage:
  stylize.py photo <input_image> <output.png>
  stylize.py element "<subject description>" <output.png>
"""
import base64
import json
import mimetypes
import os
import ssl
import subprocess
import sys
import urllib.request

import certifi

# python.org's Python on macOS ships without the system cert store wired up.
SSL_CTX = ssl.create_default_context(cafile=certifi.where())

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODEL = "gemini-3.1-flash-image"

# Flat chroma field to generate against, then key out. Any saturated hue works because
# the subject is pure black-and-white; this green is far from any grey.
CHROMA = "pure vivid chroma-key green (#00B140)"

# Style block derived from Cam's 8 picked reference images (2026-08-11).
# Deliberately NO geometric accents — those were my invention, not his references.
STYLE = (
    "high-contrast BLACK-AND-WHITE HALFTONE PHOTOGRAPHIC CUT-OUT, printed zine texture with "
    "visible halftone dot pattern and print grain, clean hard scissor-cut edges with a thin "
    "white paper border and a subtle paper drop shadow. Pure black and white only, no color "
    "in the subject. Flat scanned lighting, no gradients, no photorealism, no 3D render, no "
    "CGI. The subject sits alone on a completely flat, uniform {chroma} background with "
    "nothing else in the frame: no other objects, no text, no decorative shapes, no lines, "
    "no circles, no border."
)

PHOTO_PROMPT = (
    "Recreate this scene as a {style} Keep the original composition and pose. Remove all "
    "background people and scenery. Make the person generic and unrecognizable, not a "
    "likeness of any real individual."
)

ELEMENT_PROMPT = "Create a single isolated {subject} as a {style}"

# Scene / plate modes keep the setting instead of stripping it. The plate is the
# backdrop that alpha elements get animated on top of, so the focal subject is removed.
SCENE_STYLE = (
    "high-contrast BLACK-AND-WHITE HALFTONE PHOTOGRAPHIC CUT-OUT COLLAGE, printed zine "
    "texture with visible halftone dot pattern and print grain. Every element is a separate "
    "hand-cut paper piece with clean hard scissor-cut edges, a thin white paper border and a "
    "real paper drop shadow, clearly layered with visible separation between pieces. Subjects "
    "are pure black and white. Flat scanned lighting, no gradients, no photorealism, no 3D "
    "render, no CGI. Everything sits on a single completely flat uniform {bg} paper "
    "background. No text, no lettering, no decorative shapes, no lines, no circles."
)

SCENE_PROMPT = (
    "Recreate this entire scene, including the background people and setting, as a {style} "
    "Keep the original composition. Make every person generic and unrecognizable, not a "
    "likeness of any real individual."
)

PLATE_PROMPT = (
    "Recreate the BACKGROUND ONLY of this scene as a {style} "
    "Completely remove the main foreground person holding the microphone: they must be "
    "entirely absent from the image. Keep the seated audience and the room. Make every "
    "person generic and unrecognizable. Leave the space where the foreground person stood "
    "empty and filled in with background."
)


def api_key() -> str:
    path = os.path.expanduser("~/.config/keys/.env")
    with open(path) as f:
        for line in f:
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("GEMINI_API_KEY not found in ~/.config/keys/.env")


def _call(parts, dst):
    req = urllib.request.Request(
        ENDPOINT.format(model=MODEL),
        data=json.dumps({"contents": [{"parts": parts}]}).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key()},
    )
    try:
        with urllib.request.urlopen(req, timeout=180, context=SSL_CTX) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code}\n{e.read().decode()[:600]}")

    for part in payload["candidates"][0]["content"]["parts"]:
        blob = part.get("inlineData") or part.get("inline_data")
        if blob:
            with open(dst, "wb") as f:
                f.write(base64.b64decode(blob["data"]))
            return dst
        if part.get("text"):
            print(f"  model returned text: {part['text'][:200]}")
    raise SystemExit("no image in response")


def photo(src, dst):
    mime = mimetypes.guess_type(src)[0] or "image/jpeg"
    with open(src, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    prompt = PHOTO_PROMPT.format(style=STYLE.format(chroma=CHROMA))
    return _call([{"text": prompt},
                  {"inline_data": {"mime_type": mime, "data": b64}}], dst)


def element(subject, dst):
    prompt = ELEMENT_PROMPT.format(subject=subject, style=STYLE.format(chroma=CHROMA))
    return _call([{"text": prompt}], dst)


def _from_image(src, prompt, dst):
    mime = mimetypes.guess_type(src)[0] or "image/jpeg"
    with open(src, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return _call([{"text": prompt},
                  {"inline_data": {"mime_type": mime, "data": b64}}], dst)


def scene(src, dst, bg="warm cream"):
    """Whole scene kept, rendered as layered cut-outs. Background preserved."""
    return _from_image(src, SCENE_PROMPT.format(style=SCENE_STYLE.format(bg=bg)), dst)


def plate(src, dst, bg="warm cream"):
    """Background plate only — focal subject removed, for animating elements on top."""
    return _from_image(src, PLATE_PROMPT.format(style=SCENE_STYLE.format(bg=bg)), dst)


ISOLATE_PROMPT = (
    "From this paper-collage illustration, keep ONLY the {subject} exactly as it appears — "
    "identical position, identical scale, identical rotation, identical cropping within the "
    "frame, including its thin white paper border. Replace absolutely everything else in the "
    "image with flat uniform pure vivid chroma-key green (#00B140). Do not move, resize, "
    "redraw or re-centre the kept piece. Do not add anything. Output the same image "
    "dimensions."
)


def isolate(stylized_scene, subject, dst):
    """Pull one registered cut-out piece off an ALREADY-STYLIZED scene.

    Run against the stylized frame, never the original photo — that is what keeps the piece
    in perfect register with the scene it will be composited back onto.
    """
    return _from_image(stylized_scene, ISOLATE_PROMPT.format(subject=subject), dst)


def key_to_alpha(src, dst, sat_cut=60, feather=1):
    """Lift the chroma field to transparency by SATURATION, not by hue.

    The subject is pure black-and-white, so R==G==B on every subject pixel, while the
    chroma field is strongly saturated. Keying on saturation is immune to the print grain
    and to the model drifting off the exact hex it was asked for (it returns ~#04A943 for
    a requested #00B140, and the grain spreads that further).
    """
    import numpy as np
    from PIL import Image, ImageFilter

    a = np.array(Image.open(src).convert("RGB")).astype(np.int16)
    sat = a.max(axis=2) - a.min(axis=2)          # 0 for any grey, high for the field
    alpha = np.where(sat >= sat_cut, 0, 255).astype(np.uint8)

    am = Image.fromarray(alpha, mode="L")
    # Erode before feathering. The pixels straight along the cut are half-chroma and read
    # as a green fringe once composited; shrinking the mask a touch drops them entirely.
    am = am.filter(ImageFilter.MinFilter(5))
    if feather:
        am = am.filter(ImageFilter.GaussianBlur(feather))

    # Erosion alone misses fringe where a piece borders ANOTHER piece rather than the
    # chroma field. Subjects are pure black-and-white, so any surviving saturated pixel is
    # residual chroma by definition — flatten those to their own luminance.
    residual = sat >= 25
    if residual.any():
        grey = a.mean(axis=2, keepdims=True).repeat(3, axis=2)
        a = np.where(residual[..., None], grey, a)

    out = Image.fromarray(a.astype(np.uint8)).convert("RGBA")
    out.putalpha(am)
    out.save(dst)
    return dst


MODES = {"photo": photo, "element": element, "scene": scene, "plate": plate}

if __name__ == "__main__":
    mode, arg, out = sys.argv[1], sys.argv[2], sys.argv[3]
    MODES[mode](arg, out)
    print(f"[{mode}] {os.path.basename(arg)[:40]} -> {out}")
