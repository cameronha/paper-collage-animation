#!/usr/bin/env python3
"""
Stop-motion compositor: a stylized backdrop plus alpha cut-out elements that snap in,
rendered at a low frame rate with per-frame "boil" so it reads as hand-placed paper
rather than a smooth digital tween.

Boil is the whole trick. Every element gets a tiny random offset and rotation that
re-rolls only on each rendered frame, so settled pieces still breathe.
"""
import math
import random
import subprocess
import sys

from PIL import Image, ImageChops, ImageFilter

FPS = 12          # low on purpose — smooth motion kills the paper feel
BOIL_PX = 1.0     # per-frame positional jitter, PIECES ONLY (halved: Cam, 2026-08-11)
BOIL_DEG = 0.35   # per-frame rotational jitter, PIECES ONLY (halved: Cam, 2026-08-11)

# The camera never boils. Jitter belongs to the paper pieces; a shaking camera just makes
# the push judder. Push is the slow continuous zoom, expressed as fraction of frame over
# the whole shot — 0.065 reads as a slow drift at 4s.
PUSH_DEFAULT = 0.065


def ease_back(t):
    """Overshoot then settle — the paper 'snap'."""
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


class Element:
    def __init__(self, path, x, y, scale=1.0, t_in=0.0, dur_in=0.45, rot=0.0):
        self.img = Image.open(path).convert("RGBA")
        if scale != 1.0:
            self.img = self.img.resize(
                (int(self.img.width * scale), int(self.img.height * scale)), Image.LANCZOS)
        self.x, self.y, self.t_in, self.dur_in, self.rot = x, y, t_in, dur_in, rot

    def draw(self, canvas, t, rng):
        if t < self.t_in:
            return
        p = min(1.0, (t - self.t_in) / self.dur_in)
        s = ease_back(p)
        # snap in from slightly enlarged, so it lands rather than flies
        scale = 1.35 - 0.35 * s
        im = self.img
        if abs(scale - 1.0) > 0.01:
            im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                           Image.LANCZOS)
        rot = self.rot + rng.uniform(-BOIL_DEG, BOIL_DEG)
        if abs(rot) > 0.01:
            im = im.rotate(rot, resample=Image.BICUBIC, expand=True)
        dx = rng.uniform(-BOIL_PX, BOIL_PX)
        dy = rng.uniform(-BOIL_PX, BOIL_PX)
        canvas.alpha_composite(im, (int(self.x - im.width / 2 + dx),
                                    int(self.y - im.height / 2 + dy)))




def paper_colour(scene, bright_cut=170):
    """The paper stock a scene is printed on, for filling knocked-out piece regions.

    Not simply the most common colour: halftone spreads the cream ground across hundreds
    of near-identical values while a silhouette or a dark wall is one solid tone, so a raw
    mode returns near-black on dark scenes and fills the holes with ink instead of paper.
    Quantise first to collapse the halftone spread, then take the most common tone that is
    actually light. Falls back to the plain mode if a scene has no light ground at all.
    """
    q = scene.convert("RGB").quantize(colors=16, method=Image.MEDIANCUT)
    pal = q.getpalette()
    best, best_n = None, -1
    for n, idx in (q.getcolors() or []):
        r, g, b = pal[idx * 3:idx * 3 + 3]
        if 0.299 * r + 0.587 * g + 0.114 * b >= bright_cut and n > best_n:
            best, best_n = (r, g, b), n
    if best:
        return best
    counts = {}
    for n, c in (scene.convert("RGB").getcolors(maxcolors=1 << 24) or []):
        counts[c] = n
    return max(counts, key=counts.get) if counts else (240, 235, 220)


def margin_box(scene_path, tol=12, frac=0.995, max_margin=0.15):
    """Find the artwork inside any flat paper margin the model baked in.

    A margin is only a margin if a WHOLE edge row or column is uniform. Sampling a single
    centre row/column (the first version) mistook a large flat wall in the middle of a
    scene for a border and cropped away half the picture. Also caps how much can ever be
    taken off any one side, because a real page margin is thin.
    """
    import numpy as np
    a = np.array(Image.open(scene_path).convert("RGB")).astype(int)
    H, W, _ = a.shape
    corner = a[2, 2]

    def uniform(line):                       # line: (n, 3) pixels along one row/column
        return (np.abs(line - corner).sum(axis=1) < tol * 3).mean() >= frac

    def count(get, n, cap):
        i = 0
        while i < cap and uniform(get(i)):
            i += 1
        return i

    cap_v, cap_h = int(H * max_margin), int(W * max_margin)
    top = count(lambda i: a[i, :], H, cap_v)
    bot = count(lambda i: a[H - 1 - i, :], H, cap_v)
    left = count(lambda i: a[:, i], W, cap_h)
    right = count(lambda i: a[:, W - 1 - i], W, cap_h)

    box = (left, top, W - right, H - bot)
    if box[2] - box[0] < W * 0.7 or box[3] - box[1] < H * 0.7:
        return (0, 0, W, H)
    return box


def smoothstep(t):
    """Ease the push in and out so it never starts or stops abruptly."""
    return t * t * (3 - 2 * t)


class Piece:
    """A white-outlined cut-out carved out of the scene, sitting at its original position.

    It only twitches — no entrance, no travel. Each piece gets its own random phase so the
    crowd doesn't pulse in unison, which is what would give the trick away.
    """

    def __init__(self, path, amp=1.0, seed=0):
        self.img = Image.open(path).convert("RGBA")
        self.amp = amp
        self.seed = seed
        self.rng = random.Random(seed * 977 + 13)
        # Rotate about the piece's OWN centre, not the canvas centre. Each piece is stored
        # full-canvas, so spinning about the canvas centre swings an off-centre piece
        # through an arc and leaves a visible ghost of its original position.
        bb = self.img.getchannel("A").getbbox() or (0, 0, self.img.width, self.img.height)
        self.centre = ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)
        self.t_in = None          # None = present from frame 0 (no assembly)
        self.dur_in = 0.5
        self.from_dx = self.from_dy = self.from_rot = 0.0
        self.drift = (0.0, 0.0)

    def entry(self, t):
        """Where this piece sits on its way in. Returns (dx, dy, rot, landed).

        Slides in from off its home spot with a `back` overshoot so it lands rather than
        glides. Translation and rotation only — no scaling, because scaling a full-canvas
        piece scales about the canvas centre and would drag the piece across the frame.
        """
        if self.t_in is None:
            return 0.0, 0.0, 0.0, True
        if t < self.t_in:
            return None, None, None, False          # not placed yet
        p = (t - self.t_in) / self.dur_in
        if p >= 1.0:
            return 0.0, 0.0, 0.0, True
        s = ease_back(p)
        return (self.from_dx * (1 - s), self.from_dy * (1 - s),
                self.from_rot * (1 - s), False)

    def set_entry(self, t_in, dur_in=0.5, dist=140):
        self.t_in, self.dur_in = t_in, dur_in
        r = random.Random(self.seed * 31 + 5)
        ang = r.uniform(0, 6.2832)
        self.from_dx = math.cos(ang) * dist
        self.from_dy = math.sin(ang) * dist
        self.from_rot = r.uniform(-9, 9)

    def set_drift(self, px):
        """A slow intentional translation across the whole shot, on top of the boil."""
        r = random.Random(self.seed * 131 + 17)
        ang = r.uniform(0, 6.2832)
        self.drift = (math.cos(ang) * px, math.sin(ang) * px)

    def pose(self, frame_i, step):
        """The offset for this frame. Poses HOLD for `step` frames, then change.

        This is 'shooting on twos'. Re-rolling every frame reads as vibration or grain;
        holding a pose and then stepping to a new one reads as a hand moving paper. Keyed
        off the pose index so the same pose is reproduced for every frame it spans.
        """
        rng = random.Random(self.seed * 977 + 13 + (frame_i // max(1, step)) * 7919)
        return (rng.uniform(-BOIL_PX, BOIL_PX) * self.amp,
                rng.uniform(-BOIL_PX, BOIL_PX) * self.amp,
                rng.uniform(-BOIL_DEG, BOIL_DEG) * self.amp)

    def draw_stepped(self, canvas, z, W, H, frame_i, step, t=0.0, prog=0.0):
        edx, edy, erot, landed = self.entry(t)
        if edx is None:
            return                                  # hasn't been placed yet
        dx, dy, rot = self.pose(frame_i, step)
        if not landed:
            dx, dy, rot = 0.0, 0.0, 0.0             # no boil mid-flight
        dx += edx + self.drift[0] * prog
        dy += edy + self.drift[1] * prog
        rot += erot
        im = self.img
        if abs(rot) > 0.01:
            im = im.rotate(rot, resample=Image.BICUBIC, center=self.centre)
        bw, bh = int(W * z), int(H * z)
        im = im.resize((bw, bh), Image.LANCZOS)
        ox, oy = (bw - W) // 2, (bh - H) // 2
        canvas.alpha_composite(im.crop((ox, oy, ox + W, oy + H)), (int(dx), int(dy)))

    def draw(self, canvas, z, W, H):
        im = self.img
        rot = self.rng.uniform(-BOIL_DEG, BOIL_DEG) * self.amp
        if abs(rot) > 0.01:
            im = im.rotate(rot, resample=Image.BICUBIC, center=self.centre)
        # Scale to the FRAME's zoomed size, not the piece's own — the base is resized to
        # (W*z, H*z), so using the piece's native dimensions here lands it off-register.
        bw, bh = int(W * z), int(H * z)
        im = im.resize((bw, bh), Image.LANCZOS)
        ox, oy = (bw - W) // 2, (bh - H) // 2
        dx = self.rng.uniform(-BOIL_PX, BOIL_PX) * self.amp
        dy = self.rng.uniform(-BOIL_PX, BOIL_PX) * self.amp
        canvas.alpha_composite(im.crop((ox, oy, ox + W, oy + H)), (int(dx), int(dy)))


def render_pieces(scene_path, piece_paths, out, dur=4.0, W=1920, H=1080,
                  push=PUSH_DEFAULT, amp=1.0, blur_under=0, step=2,
                  assemble=1.0, stagger=0.14, drift=6.0, autocrop=True):
    """Steady eased push on the scene; every cut-out piece twitches independently on top.

    blur_under is off by default: at 2px of jitter the piece still covers its own original
    almost entirely, and blurring the region underneath produced a visible dark halo that
    was far worse than the sliver of doubling it was meant to hide.
    """
    scene = Image.open(scene_path).convert("RGBA")
    pieces = [Piece(p, amp=amp, seed=i) for i, p in enumerate(piece_paths)]

    if autocrop:
        box = margin_box(scene_path)
        if box != (0, 0, scene.width, scene.height):
            print(f"   cropped baked-in paper margin: {box}")
            scene = scene.crop(box)
            for p in pieces:
                p.img = p.img.crop(box)
                bb = p.img.getchannel("A").getbbox() or (0, 0, p.img.width, p.img.height)
                p.centre = ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)

    if assemble:
        # A piece cannot fly in while a copy of it sits in the base. Knock every piece's
        # region out of the base to flat paper stock, then let the pieces rebuild the
        # scene onto it. Fill colour = the scene's most common colour (the paper ground).
        ground = paper_colour(scene)
        union = Image.new("L", scene.size, 0)
        for p in pieces:
            union = ImageChops.lighter(union, p.img.getchannel("A"))
        # The hole must be BIGGER than the piece, or the piece slides off it and exposes
        # the original underneath. Two things shrink/move it: key_to_alpha erodes ~2px to
        # kill the chroma fringe, and drift walks the piece up to `drift` px from home.
        # Cheap dilation: blur then threshold low.
        grow = 6 + int(drift)
        union = union.filter(ImageFilter.GaussianBlur(grow * 0.6)).point(
            lambda v: 255 if v > 16 else 0)
        scene = Image.composite(Image.new("RGBA", scene.size, ground + (255,)), scene, union)
        for i, p in enumerate(pieces):
            p.set_entry(i * stagger, dur_in=0.5)

    if drift:
        for p in pieces:
            p.set_drift(drift)

    base = scene.copy()
    if blur_under and pieces:
        union = Image.new("L", scene.size, 0)
        for p in pieces:
            union = ImageChops.lighter(union, p.img.getchannel("A"))
        blurred = scene.filter(ImageFilter.GaussianBlur(blur_under))
        base = Image.composite(blurred, scene, union)

    frames = int(dur * FPS)
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-an",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", out],
        stdin=subprocess.PIPE)

    for i in range(frames):
        z = 1.0 + push * smoothstep(i / max(1, frames - 1))
        bw, bh = int(W * z), int(H * z)
        ox, oy = (bw - W) // 2, (bh - H) // 2
        frame = base.resize((bw, bh), Image.LANCZOS).crop((ox, oy, ox + W, oy + H))
        prog = i / max(1, frames - 1)
        for p in pieces:
            p.draw_stepped(frame, z, W, H, i, step, t=i / FPS, prog=prog)
        proc.stdin.write(frame.convert("RGB").tobytes())

    proc.stdin.close()
    proc.wait()
    extra = []
    if assemble: extra.append("assembling")
    if drift: extra.append(f"drift {drift}px")
    print(f"-> {out} ({frames} frames @ {FPS}fps, {len(pieces)} pieces, on {step}s"
          + (", " + ", ".join(extra) if extra else "") + ")")
    return out


def render(backdrop_path, elements, out, dur=4.0, W=1920, H=1080, push=PUSH_DEFAULT):
    bg = Image.open(backdrop_path).convert("RGBA")
    frames = int(dur * FPS)
    rng = random.Random(7)

    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-an",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", out],
        stdin=subprocess.PIPE)

    for i in range(frames):
        t = i / FPS
        # Smooth, slow, eased push. No boil here — the camera stays dead steady.
        z = 1.0 + push * smoothstep(i / max(1, frames - 1))
        bw, bh = int(W * z), int(H * z)
        ox, oy = (bw - W) // 2, (bh - H) // 2
        frame = bg.resize((bw, bh), Image.LANCZOS).crop((ox, oy, ox + W, oy + H))

        for el in elements:
            el.draw(frame, t, rng)
        proc.stdin.write(frame.convert("RGB").tobytes())

    proc.stdin.close()
    proc.wait()
    print(f"-> {out} ({frames} frames @ {FPS}fps)")
    return out
