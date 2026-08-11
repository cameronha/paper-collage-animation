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

    def draw_stepped(self, canvas, z, W, H, frame_i, step):
        dx, dy, rot = self.pose(frame_i, step)
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
                  push=PUSH_DEFAULT, amp=1.0, blur_under=0, step=2):
    """Steady eased push on the scene; every cut-out piece twitches independently on top.

    blur_under is off by default: at 2px of jitter the piece still covers its own original
    almost entirely, and blurring the region underneath produced a visible dark halo that
    was far worse than the sliver of doubling it was meant to hide.
    """
    scene = Image.open(scene_path).convert("RGBA")
    pieces = [Piece(p, amp=amp, seed=i) for i, p in enumerate(piece_paths)]

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
        for p in pieces:
            p.draw_stepped(frame, z, W, H, i, step)
        proc.stdin.write(frame.convert("RGB").tobytes())

    proc.stdin.close()
    proc.wait()
    print(f"-> {out} ({frames} frames @ {FPS}fps, {len(pieces)} pieces, on {step}s)")
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
