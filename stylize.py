#!/usr/bin/env python3
"""
Stylize an image (or generate an element from text) via the Gemini image models,
in Cam's locked B-roll style. Reads GEMINI_API_KEY from ~/.config/keys/.env so the
key never lands in argv or shell history.

Subjects come back as black-and-white halftone cut-outs on a flat chroma field,
so key_to_alpha() can lift them to transparent PNGs exactly (the subject contains
no saturated color, so there is nothing to spill).

Usage:
  stylize.py scene <input_image> <output.png>
"""
import base64
import http.client
import json
import mimetypes
import os
import ssl
import time
import sys
import urllib.request

import certifi

# python.org's Python on macOS ships without the system cert store wired up.
SSL_CTX = ssl.create_default_context(cafile=certifi.where())

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODEL = "gemini-3.1-flash-image"




# Scene / plate modes keep the setting instead of stripping it. The plate is the
# backdrop that alpha elements get animated on top of, so the focal subject is removed.
SCENE_STYLE = (
    "high-contrast BLACK-AND-WHITE HALFTONE PHOTOGRAPHIC CUT-OUT COLLAGE, printed zine "
    "texture with visible halftone dot pattern and print grain. Every element is a separate "
    "hand-cut paper piece with clean hard scissor-cut edges, a thin white paper border and a "
    "real paper drop shadow, clearly layered with visible separation between pieces. Subjects "
    "are pure black and white. Flat scanned lighting, no gradients, no photorealism, no 3D "
    "render, no CGI. Everything sits on a single completely flat uniform {bg} paper "
    "background. No text, no lettering, no decorative shapes, no lines, no circles. "
    "FULL BLEED: the artwork must extend all the way to all four edges of the image. No "
    "page margin, no border, no frame, no mat, no white space around the artwork."
)

SCENE_PROMPT = (
    # Text rule goes FIRST and is stated as an override. Buried at the end it lost to the
    # faithfulness clauses whenever text was large and sharp: the model scrambled small
    # incidental writing and rendered a big headline perfectly, reading it as content worth
    # preserving rather than as text to remove.
    "RULE ONE, OVERRIDES EVERYTHING BELOW: this image must contain NO READABLE TEXT. "
    "Replace every piece of writing with abstract illegible marks — wavy lines, dashes and "
    "grey blocks. This applies to every surface without exception (projection screens, "
    "slides, monitors, chalkboards, whiteboards, posters, signage, labels, paperwork, book "
    "spines, clothing) and it applies NO MATTER how large, sharp, central or important the "
    "text looks. Big bold headline text especially must be replaced — do not preserve it "
    "because it seems to be the subject of the photo. Being faithful to the source does NOT "
    "include reproducing its words. A viewer must not be able to read a single word or "
    "recognise a single letter anywhere in the image. "
    "Now, with that rule absolute: "
    "recreate this entire scene, including its background and setting, as a {style} "
    "Keep the original composition of the source photo — same subjects, same arrangement, "
    "same viewpoint. Do not restage it. "
    "COMPOSITION: the artwork fills the whole frame edge to edge, full bleed, with no page "
    "margin and no inset panel. Within that faithful composition, frame it so the people and "
    "the important objects fall in the MIDDLE "
    "THIRD of the frame, so a square crop taken from the centre keeps them whole. Adjust the "
    "framing and crop to achieve this, not the arrangement of the subjects. The far "
    "left and far right show ordinary background — wall, furniture, room — never empty "
    "space, and nothing the viewer needs to see. All three of those must hold together: "
    "drop the last one and the model leaves blank bands at the sides instead of background. "
    "FACES: exempt faces from the halftone screen. Render the skin of each face in smooth "
    "continuous tone with clean unbroken edges and no dot pattern, so the eyes, nose and "
    "mouth resolve cleanly even on a small face. A halftone screen coarse enough to see is "
    "always too coarse for a small face, and dot-scale features read as uncanny. "
    "This exemption applies to FACIAL SKIN ONLY — hair, clothing, hands, bodies, furniture "
    "and every other surface keep the full halftone dot pattern and print grain. "
    "Faces must be present and complete, never blanked, blurred, masked or erased. "
    "DO NOT INVENT CONTENT: do not add any person, object, sign or detail that is not "
    "already present in the source photo. Where an area of the source is blurred, dark or "
    "low-detail, render it as a plain simplified shape rather than inventing something to "
    "fill it. Empty stays empty. "
    "Finally, re-read RULE ONE: no readable text anywhere, regardless of prominence."
)



def api_key() -> str:
    path = os.path.expanduser("~/.config/keys/.env")
    with open(path) as f:
        for line in f:
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("GEMINI_API_KEY not found in ~/.config/keys/.env")


class GenerationError(RuntimeError):
    """A generation call failed. A normal Exception, NOT SystemExit — callers need to be
    able to skip one bad piece and carry on with the shot."""


def post_json(model, body, timeout=180, attempts=4):
    """POST to the Gemini API with backoff on transient failures.

    EVERY call to the API must go through here. 503s are frequent, and a second
    un-retried code path is exactly how a failure slips through (plan_pieces had its own
    urlopen and died on a 503 that _call would have ridden out).
    """
    for n in range(attempts):
        req = urllib.request.Request(
            ENDPOINT.format(model=model), data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key()})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            # A 429 usually means rate limiting and is worth retrying. "Credits depleted"
            # arrives as a 429 too and will never resolve on its own, so fail fast on it
            # instead of backing off three times for nothing.
            if "credits are depleted" in detail or "RESOURCE_EXHAUSTED" in detail and "credit" in detail:
                raise GenerationError(
                    "Gemini prepayment credits are depleted — top up or switch the project "
                    "to pay-as-you-go at https://ai.studio/projects")
            if e.code in (429, 500, 503) and n < attempts - 1:
                wait = 5 * (2 ** n)
                print(f"       HTTP {e.code}, retrying in {wait}s ...")
                time.sleep(wait)
                continue
            raise GenerationError(f"HTTP {e.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                http.client.HTTPException) as e:
            if n < attempts - 1:
                wait = 5 * (2 ** n)
                print(f"       {type(e).__name__}, retrying in {wait}s ...")
                time.sleep(wait)
                continue
            raise GenerationError(str(e))
    raise GenerationError("no response after retries")


def _call(parts, dst):
    payload = post_json(MODEL, json.dumps({"contents": [{"parts": parts}]}).encode())

    for part in payload["candidates"][0]["content"]["parts"]:
        blob = part.get("inlineData") or part.get("inline_data")
        if blob:
            with open(dst, "wb") as f:
                f.write(base64.b64decode(blob["data"]))
            return dst
        if part.get("text"):
            print(f"  model returned text: {part['text'][:200]}")
    raise GenerationError("no image in response")


def _from_image(src, prompt, dst):
    mime = mimetypes.guess_type(src)[0] or "image/jpeg"
    with open(src, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return _call([{"text": prompt},
                  {"inline_data": {"mime_type": mime, "data": b64}}], dst)


def scene(src, dst, bg="warm cream"):
    """Whole scene kept, rendered as layered cut-outs. Background preserved."""
    return _from_image(src, SCENE_PROMPT.format(style=SCENE_STYLE.format(bg=bg)), dst)


VARY_PROMPT = (
    "This is an existing paper-collage illustration in a locked house style. Produce a NEW "
    "image that is unmistakably from the same series. "
    "KEEP IDENTICAL: the visual style in every respect — high-contrast black-and-white "
    "halftone cut-outs, clean hard scissor-cut edges with a thin white paper border and a "
    "real paper drop shadow, visible halftone dots and print grain, flat scanned lighting, "
    "no gradients, no photorealism, no 3D, no CGI, everything on a flat uniform warm cream "
    "paper background, full bleed to all four edges. "
    "KEEP THE SAME PEOPLE: any person who appears in both images must be recognisably the "
    "same individual — same face, same hair, same build, same clothing — rendered the same "
    "way. Faces stay exempt from the halftone screen: facial skin in smooth continuous tone "
    "with eyes, nose and mouth resolving cleanly. Never blank, blur or erase a face. "
    "CHANGE THE MOMENT: this is a DIFFERENT moment in time, not the same photograph with a "
    "new backdrop. Give the people NEW poses, NEW body positions, NEW spacing and a NEW "
    "arrangement in the frame, doing something that makes sense in the new setting. Vary the "
    "camera angle and distance too. Do NOT copy the original composition, do NOT keep people "
    "standing in the same spots holding the same things at the same angles — identical poses "
    "against a changed background look like a cut-out pasted onto a new photo, which is "
    "exactly what this must not look like. Same cast, different scene. "
    "NO READABLE TEXT ANYWHERE: any writing becomes wavy scribbles, dashes or grey blocks, "
    "no matter how large or prominent. "
    "THE CHANGE TO MAKE: {change}"
)


def vary(stylized_scene, change, dst):
    """Make a new scene in the same series as an existing one.

    Unlike scene(), this mode is ALLOWED to invent — "add a character" is the point. The
    faithfulness clauses that keep scene() honest would forbid exactly what's wanted here,
    so this prompt drops them and leans on style and character continuity instead.
    """
    return _from_image(stylized_scene, VARY_PROMPT.format(change=change), dst)


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


MODES = {"scene": scene, "vary": vary}

if __name__ == "__main__":
    # scene is the only mode worth a CLI; isolate needs a scene + a description.
    mode, arg, out = sys.argv[1], sys.argv[2], sys.argv[3]
    scene(arg, out)
    print(f"[{mode}] {os.path.basename(arg)[:40]} -> {out}")
