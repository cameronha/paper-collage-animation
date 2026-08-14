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
    "in the lower left'. "
    "Each piece must be SEPARABLE: do not choose a piece that overlaps or contains part of "
    "another piece you have chosen. A table that people are sitting behind, or a desk their "
    "arms rest on, cannot be cut out cleanly — it drags ghosts of them along with it. Prefer "
    "whole figures and objects that stand clear of everything else. "
    "Reply with ONLY a JSON array of strings."
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
    payload = S.post_json(PLAN_MODEL, body)

    text = "".join(p.get("text", "") for p in payload["candidates"][0]["content"]["parts"])
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise SystemExit(f"could not parse a piece list from:\n{text[:400]}")
    return [str(x) for x in json.loads(m.group(0))][:n]


def fetch(url, workdir):
    """Download a remote image so the rest of the pipeline sees an ordinary local file.

    Kept deliberately strict: it must come back as an image, and it is written inside the
    run's own work dir so a shot always carries its own source.
    """
    import shutil
    import urllib.parse
    os.makedirs(workdir, exist_ok=True)
    EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
           "image/gif": ".gif"}
    req = urllib.request.Request(url, headers={"User-Agent": "broll/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60, context=S.SSL_CTX) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            if not ctype.startswith("image/"):
                raise SystemExit(f"that URL returned {ctype or 'no content-type'}, not an image")
            dst = os.path.join(workdir, "source" + EXT.get(ctype, ".img"))
            with open(dst, "wb") as f:
                shutil.copyfileobj(r, f)
    except urllib.error.URLError as e:
        # certifi lags the macOS keychain on new CA roots (e.g. ISRG Root YR), so perfectly
        # valid sites fail here. Fall back to curl, which validates against the SYSTEM trust
        # store. Verification is still fully on — this is a fresher trust store, not a laxer one.
        if "CERTIFICATE_VERIFY_FAILED" not in str(e):
            raise SystemExit(f"could not fetch that URL: {e}")
        print("       certifi rejected the cert chain; retrying via system trust store")
        import subprocess
        dst = os.path.join(workdir, "source.img")
        p = subprocess.run(["curl", "-fsSL", "--max-time", "60", "-A", "broll/1.0",
                            "-w", "%{content_type}", "-o", dst, url],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise SystemExit(f"could not fetch that URL (curl exit {p.returncode})")
        ctype = (p.stdout or "").split(";")[0].strip()
        if not ctype.startswith("image/"):
            raise SystemExit(f"that URL returned {ctype or 'no content-type'}, not an image")
        better = os.path.join(workdir, "source" + EXT.get(ctype, ".img"))
        if better != dst:
            os.replace(dst, better); dst = better
    Image.open(dst).verify()                      # refuse anything Pillow can't read
    name = os.path.basename(urllib.parse.urlparse(url).path) or "downloaded"
    print(f"       downloaded {name} ({os.path.getsize(dst)//1024} KB)")
    return dst


def slug(s, i):
    return f"{i:02d}-" + (re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:32] or "piece")


def frame_size(scene_path, height=1080):
    """Match the scene's aspect so nothing is stretched. h264 wants even dimensions."""
    w, h = Image.open(scene_path).size
    return (int(round(height * w / h / 2) * 2), height)


def main():
    ap = argparse.ArgumentParser(description="Photo -> stop-motion B-roll clip.")
    ap.add_argument("photo", nargs="?",
                    help="source photo or URL; omit when using --from or --text")
    ap.add_argument("--from", dest="from_scene", default=None,
                    help="an existing stylized scene.png to make a variant of")
    ap.add_argument("--change", default=None,
                    help='what to change, e.g. "same people, now in a kitchen"')
    ap.add_argument("--text", default=None,
                    help='generate the scene from a description instead of a photo, e.g. '
                         '--text "a hand closing an old padlock, close up". Requires --out. '
                         "Tuned for a normal subject, not a wide establishing shot.")
    ap.add_argument("--scene", dest="existing_scene", default=None,
                    help="use this ALREADY-STYLIZED image as-is, no generation at all. For "
                         "animating a still you already approved. Requires --out. This is "
                         "the fix for a real recurring mistake: running an approved still "
                         "through the normal photo path re-stylized it a second time, "
                         "costing money and drifting the image (extra flagpoles appeared "
                         "on a city hall shot this way). Now there's a code path where that "
                         "cannot happen.")
    ap.add_argument("--out", default=None, help="output mp4 (default: alongside the photo)")
    ap.add_argument("--pieces", type=int, default=5)
    ap.add_argument("--dur", type=float, default=5.0)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--stagger", type=float, default=0.30,
                    help="seconds between each piece starting its entry (default 0.30)")
    ap.add_argument("--entry-dur", type=float, default=0.8,
                    help="seconds each piece takes to land (default 0.8, slower/more dramatic)")
    ap.add_argument("--bg", default="E98F58",
                    help="hex colour for the paper ground. Default E98F58 (brand orange). "
                         "Others on brand: EC5C6B, 8CBDDC. Use 'none' for the raw cream. "
                         "Applied locally at render time, so it is free.")
    ap.add_argument("--no-assemble", action="store_true")
    ap.add_argument("--no-drift", action="store_true")
    ap.add_argument("--reuse", action="store_true",
                    help="reuse an existing work dir instead of regenerating (free)")
    ap.add_argument("--work", default=None,
                    help="use THIS work dir instead of one named after --out. Lets you render "
                         "variants (different --bg, --dur, --drift) off one paid generation.")
    a = ap.parse_args()

    modes = [m for m in (a.from_scene, a.text, a.existing_scene) if m]
    if len(modes) > 1:
        raise SystemExit("use only one of --from, --text, --scene")

    if a.existing_scene:
        if not a.out:
            raise SystemExit("--out is required with --scene")
    elif a.from_scene:
        if not a.change:
            raise SystemExit("--from needs --change describing what to alter")
        if not a.out:
            raise SystemExit("--out is required with --from")
        a.photo = a.from_scene          # only used for naming from here on
    elif a.text:
        if not a.out:
            raise SystemExit("--out is required with --text")
    elif not a.photo:
        raise SystemExit("give a photo path, a URL, or use --from / --text / --scene")

    is_url = bool(a.photo) and a.photo.startswith(("http://", "https://"))
    if is_url and not a.out:
        raise SystemExit("--out is required when the source is a URL")
    if a.text or a.existing_scene:
        out = a.out
    else:
        stem = os.path.splitext(os.path.basename(a.photo.split("?")[0]))[0] or "downloaded"
        out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.photo)), f"{stem}-broll.mp4")
    work = a.work or os.path.splitext(out)[0] + ".work"
    # Guard: --reuse with a work dir that does not exist silently regenerates and charges
    # for it. That has bitten twice. Fail loudly instead.
    if a.reuse and not os.path.exists(os.path.join(work, "scene.png")):
        raise SystemExit(
            f"--reuse was given but there is no scene at {work}/scene.png\n"
            f"    Point --work at an existing .work dir, or drop --reuse to pay for a new one.")
    os.makedirs(os.path.join(work, "iso"), exist_ok=True)
    os.makedirs(os.path.join(work, "pieces"), exist_ok=True)
    scene = os.path.join(work, "scene.png")

    if is_url:
        print(f"[0/4] fetching {a.photo[:70]}{'...' if len(a.photo) > 70 else ''}")
        a.photo = fetch(a.photo, work)

    if a.reuse and os.path.exists(scene):
        print(f"[1/4] reusing {scene}")
    elif a.existing_scene:
        import shutil
        print(f"[1/4] using {os.path.basename(a.existing_scene)} as-is, no generation")
        shutil.copy(a.existing_scene, scene)
    elif a.from_scene:
        print(f"[1/4] varying {os.path.basename(a.from_scene)}")
        print(f"       change: {a.change}")
        S.vary(a.from_scene, a.change, scene)
    elif a.text:
        print(f"[1/4] generating scene from text ...")
        print(f"       {a.text}")
        S.text_scene(a.text, scene)
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
        drift=0.0 if a.no_drift else 6.0, stagger=a.stagger, entry_dur=a.entry_dur,
        bg=None if str(a.bg).lower() == "none" else a.bg)

    print(f"\n{out}")
    print(f"work dir: {work}  (re-run with --reuse to re-render for free)")
    os.system(f'open "{out}"')


if __name__ == "__main__":
    main()
