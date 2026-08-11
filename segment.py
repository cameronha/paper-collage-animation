#!/usr/bin/env python3
"""
Cut a stylized collage scene into its individual white-outlined paper pieces, so each
piece can be jittered independently (the actual stop-motion move).

Uses Gemini's segmentation output: a JSON list of {box_2d, mask, label} where box_2d is
[y0,x0,y1,x1] normalized to 0-1000 and mask is a base64 PNG probability map sized to the
box. Each piece is written as a full-canvas transparent PNG so it composites back at its
original position with no bookkeeping.
"""
import base64
import io
import json
import mimetypes
import os
import re
import ssl
import sys
import urllib.request

import certifi
import numpy as np
from PIL import Image

SSL_CTX = ssl.create_default_context(cafile=certifi.where())
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODEL = "gemini-flash-latest"

PROMPT = (
    "This is a paper-collage illustration. Every figure and object is a separate paper "
    "cut-out with a thin white border around it. Give me segmentation masks for each "
    "distinct cut-out piece: each person, and each prominent object. Include the thin "
    "white paper border as part of each mask. Output a JSON list where each entry has "
    "\"box_2d\", \"mask\", and a short descriptive \"label\". Limit to the 12 most "
    "prominent pieces."
)


def api_key() -> str:
    with open(os.path.expanduser("~/.config/keys/.env")) as f:
        for line in f:
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("GEMINI_API_KEY not found")


def segment(src, outdir, model=MODEL):
    os.makedirs(outdir, exist_ok=True)
    # Downscale before sending. box_2d and masks come back normalized to 0-1000, so they
    # rescale to the full-size scene for free, and the call returns far faster.
    small = Image.open(src).convert("RGB")
    small.thumbnail((768, 768), Image.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, format="JPEG", quality=88)
    b64 = base64.b64encode(buf.getvalue()).decode()
    mime = "image/jpeg"

    body = json.dumps({
        "contents": [{"parts": [{"text": PROMPT},
                                {"inline_data": {"mime_type": mime, "data": b64}}]}],
        # thinkingBudget 0 matters: with thinking on, mask generation blows past 4 minutes.
        "generationConfig": {"temperature": 0.0},
    }).encode()
    req = urllib.request.Request(
        ENDPOINT.format(model=model), data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key()})
    try:
        with urllib.request.urlopen(req, timeout=900, context=SSL_CTX) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code}\n{e.read().decode()[:600]}")

    text = "".join(p.get("text", "") for p in payload["candidates"][0]["content"]["parts"])
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise SystemExit(f"no JSON in response:\n{text[:500]}")
    items = json.loads(m.group(0))

    scene = Image.open(src).convert("RGBA")
    W, H = scene.size
    scene_a = np.array(scene)
    pieces = []

    for i, it in enumerate(items):
        if "mask" not in it or "box_2d" not in it:
            continue
        y0, x0, y1, x1 = it["box_2d"]
        x0, x1 = int(x0 / 1000 * W), int(x1 / 1000 * W)
        y0, y1 = int(y0 / 1000 * H), int(y1 / 1000 * H)
        if x1 <= x0 or y1 <= y0:
            continue

        raw = it["mask"].split(",", 1)[-1]
        mask = Image.open(io.BytesIO(base64.b64decode(raw))).convert("L")
        mask = mask.resize((x1 - x0, y1 - y0), Image.BILINEAR)

        full = np.zeros((H, W), dtype=np.uint8)
        full[y0:y1, x0:x1] = np.array(mask)
        full = (full > 127) * 255

        piece = scene_a.copy()
        piece[..., 3] = full.astype(np.uint8)
        label = re.sub(r"[^a-z0-9]+", "-", it.get("label", f"piece{i}").lower()).strip("-")
        path = os.path.join(outdir, f"{i:02d}-{label or 'piece'}.png")
        Image.fromarray(piece).save(path)
        cover = 100 * (full > 0).sum() / (W * H)
        print(f"  {os.path.basename(path)}  bbox=({x0},{y0})-({x1},{y1})  {cover:.1f}% of frame")
        pieces.append(path)

    print(f"{len(pieces)} pieces -> {outdir}")
    return pieces


if __name__ == "__main__":
    segment(sys.argv[1], sys.argv[2])
