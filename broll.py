#!/usr/bin/env python3
"""
One command: photo in, stop-motion B-roll clip out.

    python3 broll.py photo.jpg
    python3 broll.py photo.jpg --pieces 6 --dur 4 --out out/shot.mp4

Pipeline (see README): stylize the scene -> ask the model to name its own cut-out pieces
-> isolate each one against the stylized frame -> key chroma to alpha -> render on twos
with assembly and drift.

Intermediates land in a work dir next to the output so a shot can be re-rendered, or a
single bad piece re-isolated, without paying for the whole thing again.
"""
import argparse
import glob
import json
import os
import re
import sys
import urllib.request

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import animate
import stylize as S

PLAN_MODEL = "gemini-3.1-flash-lite"   # labels only — fast, and masks were never usable
PLAN_PROMPT = (
    "This is a paper-collage illustration. Every figure and object is a separate paper "
    "cut-out with a thin white border. List the {n} most prominent, visually distinct "
    "cut-out pieces — the ones worth animating separately. Prefer whole subjects (a "
    "person, a laptop, a screen, a plant) over parts of them, and pieces that are clearly "
    "separated from their neighbours. For each, give a short noun phrase that identifies "
    "it unambiguously by position and appearance, e.g. 'seated man in the checked shirt "
    "in the lower left'. Reply with ONLY a JSON array of strings."
)


def plan_pieces(scene_path, n=5):
    """Ask the model to name its own cut-outs. Returns a list of isolate descriptions."""
    import base64
    import io
    im = Image.open(scene_path).convert("RGB")
    im.thumbnail((768, 768), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=88)

    body = json.dumps({
        "contents": [{"parts": [
            {"text": PLAN_PROMPT.format(n=n)},
            {"inline_data": {"mime_type": "image/jpeg",
                             "data": base64.b64encode(buf.getvalue()).decode()}}]}],
        "generationConfig": {"temperature": 0.0},
    }).encode()
    req = urllib.request.Request(
        S.ENDPOINT.format(model=PLAN_MODEL), data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": S.api_key()})
    with urllib.request.urlopen(req, timeout=180, context=S.SSL_CTX) as r:
        payload = json.load(r)

    text = "".join(p.get("text", "") for p in payload["candidates"][0]["content"]["parts"])
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise SystemExit(f"could not parse a piece list from:\n{text[:400]}")
    return [str(x) for x in json.loads(m.group(0))][:n]


def slug(s, i):
    return f"{i:02d}-" + (re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:32] or "piece")


def frame_size(scene_path, height=1080):
    """Match the scene's aspect so nothing is stretched. h264 wants even dimensions."""
    w, h = Image.open(scene_path).size
    return (int(round(height * w / h / 2) * 2), height)


def main():
    ap = argparse.ArgumentParser(description="Photo -> stop-motion B-roll clip.")
    ap.add_argument("photo")
    ap.add_argument("--out", default=None, help="output mp4 (default: alongside the photo)")
    ap.add_argument("--pieces", type=int, default=5)
    ap.add_argument("--dur", type=float, default=5.0)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--no-assemble", action="store_true")
    ap.add_argument("--no-drift", action="store_true")
    ap.add_argument("--reuse", action="store_true",
                    help="reuse an existing work dir instead of regenerating (free)")
    a = ap.parse_args()

    stem = os.path.splitext(os.path.basename(a.photo))[0]
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.photo)), f"{stem}-broll.mp4")
    work = os.path.splitext(out)[0] + ".work"
    os.makedirs(os.path.join(work, "iso"), exist_ok=True)
    os.makedirs(os.path.join(work, "pieces"), exist_ok=True)
    scene = os.path.join(work, "scene.png")

    if a.reuse and os.path.exists(scene):
        print(f"[1/4] reusing {scene}")
    else:
        print(f"[1/4] stylizing {os.path.basename(a.photo)} ...")
        S.scene(a.photo, scene)

    plan_file = os.path.join(work, "pieces.json")
    if a.reuse and os.path.exists(plan_file):
        names = json.load(open(plan_file))
        print(f"[2/4] reusing {len(names)} piece descriptions")
    else:
        print(f"[2/4] choosing {a.pieces} pieces ...")
        names = plan_pieces(scene, a.pieces)
        json.dump(names, open(plan_file, "w"), indent=2)
    for i, n in enumerate(names):
        print(f"       {i}. {n}")

    print(f"[3/4] isolating and keying ...")
    for i, name in enumerate(names):
        iso = os.path.join(work, "iso", slug(name, i) + ".png")
        pie = os.path.join(work, "pieces", slug(name, i) + ".png")
        if a.reuse and os.path.exists(pie):
            continue
        try:
            S.isolate(scene, name, iso)
            S.key_to_alpha(iso, pie)
            bb = Image.open(pie).getchannel("A").getbbox()
            if not bb:
                os.remove(pie)
                print(f"       ! piece {i} came back empty, skipping")
        except Exception as e:                      # one bad piece shouldn't kill the shot
            print(f"       ! piece {i} failed ({type(e).__name__}), skipping")

    pieces = sorted(glob.glob(os.path.join(work, "pieces", "*.png")))
    if not pieces:
        raise SystemExit("no usable pieces — try --pieces with a different count")

    W, H = frame_size(scene, a.height)
    print(f"[4/4] rendering {len(pieces)} pieces at {W}x{H} ...")
    animate.render_pieces(
        scene, pieces, out, dur=a.dur, W=W, H=H, amp=0.5, step=2,
        assemble=0.0 if a.no_assemble else 1.0,
        drift=0.0 if a.no_drift else 14.0)

    print(f"\n{out}")
    print(f"work dir: {work}  (re-run with --reuse to re-render for free)")
    os.system(f'open "{out}"')


if __name__ == "__main__":
    main()
