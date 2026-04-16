"""
answer_clicker.py

UI automation: captures screen, asks Llama 3.2 Vision to identify the correct
answer box, then moves the mouse in a human-like curved path and clicks it.

Dependencies:
    pip install mss pillow ollama pydirectinput
"""

import base64
import io
import json
import math
import random
import re
import time

import mss
import mss.tools
import ollama
import pydirectinput
from PIL import Image

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OLLAMA_MODEL = "llama3.2-vision"
DELAY_MIN = 15   # seconds
DELAY_MAX = 45   # seconds

# Curve parameters
CURVE_STEPS = 60          # mouse positions along the path
STEP_DELAY  = 0.008       # seconds between each step (≈120 steps/sec)
JITTER_PX   = 3           # ± pixel noise added to each step


# ---------------------------------------------------------------------------
# Screen capture
# ---------------------------------------------------------------------------

def capture_screen() -> Image.Image:
    """Capture the primary monitor and return a PIL Image."""
    with mss.mss() as sct:
        monitor = sct.monitors[1]   # monitors[0] is the virtual "all screens"
        raw = sct.grab(monitor)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    return img


def image_to_base64(img: Image.Image) -> str:
    """Encode a PIL image as a base64 PNG string."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ---------------------------------------------------------------------------
# Vision model query
# ---------------------------------------------------------------------------

PROMPT = (
    "Identify the four clickable answer boxes visible on screen. "
    "Based on the question being asked, determine which box contains the correct answer. "
    "Return ONLY a JSON object with keys 'x' and 'y' representing the approximate "
    "center pixel coordinates of the correct answer box. "
    "Example: {\"x\": 540, \"y\": 720}. "
    "Do not include any other text or explanation."
)


def ask_vision_model(img: Image.Image) -> tuple[int, int]:
    """
    Send the screenshot to Llama 3.2 Vision and parse the returned (x, y).
    Returns (x, y) as ints, or raises ValueError if parsing fails.
    """
    b64 = image_to_base64(img)

    response = ollama.chat(
        model=OLLAMA_MODEL,
        messages=[
            {
                "role": "user",
                "content": PROMPT,
                "images": [b64],
            }
        ],
    )

    raw_text = response["message"]["content"].strip()
    print(f"[vision] raw response: {raw_text}")

    # Try direct JSON parse first, then fall back to regex extraction
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        match = re.search(r'\{[^}]*"x"\s*:\s*(\d+)[^}]*"y"\s*:\s*(\d+)[^}]*\}', raw_text)
        if not match:
            match = re.search(r'"x"\s*:\s*(\d+).*?"y"\s*:\s*(\d+)', raw_text, re.DOTALL)
        if not match:
            raise ValueError(f"Could not parse coordinates from model output: {raw_text!r}")
        return int(match.group(1)), int(match.group(2))

    x = int(data["x"])
    y = int(data["y"])
    return x, y


# ---------------------------------------------------------------------------
# Human-like mouse movement
# ---------------------------------------------------------------------------

def _bezier(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
    """Cubic Bézier interpolation for a single axis."""
    u = 1 - t
    return u**3 * p0 + 3 * u**2 * t * p1 + 3 * u * t**2 * p2 + t**3 * p3


def move_mouse_curved(target_x: int, target_y: int) -> None:
    """
    Move the mouse from its current position to (target_x, target_y) along a
    randomised cubic Bézier curve, with per-step jitter.
    """
    # pydirectinput works in screen pixels; get current position via ctypes
    import ctypes
    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    pt = _POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    start_x, start_y = pt.x, pt.y

    dx = target_x - start_x
    dy = target_y - start_y
    dist = math.hypot(dx, dy)

    # Control points: offset perpendicular to the straight line
    perp_scale = random.uniform(0.2, 0.5) * dist * random.choice([-1, 1])
    mid_x = (start_x + target_x) / 2
    mid_y = (start_y + target_y) / 2

    cp1_x = start_x + dx * random.uniform(0.15, 0.35) + (-dy / dist) * perp_scale * random.uniform(0.5, 1.0)
    cp1_y = start_y + dy * random.uniform(0.15, 0.35) + ( dx / dist) * perp_scale * random.uniform(0.5, 1.0)
    cp2_x = mid_x   + dx * random.uniform(0.10, 0.25) + (-dy / dist) * perp_scale * random.uniform(0.3, 0.7)
    cp2_y = mid_y   + dy * random.uniform(0.10, 0.25) + ( dx / dist) * perp_scale * random.uniform(0.3, 0.7)

    # Vary step delay: faster in the middle, slower at start/end (ease-in-out)
    for i in range(1, CURVE_STEPS + 1):
        t = i / CURVE_STEPS

        # Ease-in-out: remap t so motion accelerates then decelerates
        t_eased = t * t * (3 - 2 * t)

        bx = _bezier(start_x, cp1_x, cp2_x, target_x, t_eased)
        by = _bezier(start_y, cp1_y, cp2_y, target_y, t_eased)

        jx = random.randint(-JITTER_PX, JITTER_PX)
        jy = random.randint(-JITTER_PX, JITTER_PX)

        px = int(round(bx + jx))
        py = int(round(by + jy))

        pydirectinput.moveTo(px, py)

        # Ease: slower near endpoints, faster mid-curve
        speed_factor = 1 + 1.5 * math.sin(math.pi * t)   # peaks at t=0.5
        time.sleep(STEP_DELAY / speed_factor)

    # Settle exactly on the target
    pydirectinput.moveTo(target_x, target_y)


def human_click(x: int, y: int) -> None:
    """Move to (x, y) along a curve then left-click."""
    move_mouse_curved(x, y)
    time.sleep(random.uniform(0.05, 0.18))   # brief hover before clicking
    pydirectinput.click(x, y)
    print(f"[click] clicked ({x}, {y})")


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def answer_question() -> None:
    """
    Full pipeline:
      1. Wait a human-like reading delay.
      2. Capture the screen.
      3. Ask the vision model for the correct answer box coordinates.
      4. Move the mouse along a curved path and click.
    """
    delay = random.uniform(DELAY_MIN, DELAY_MAX)
    print(f"[delay] waiting {delay:.1f}s to simulate reading …")
    time.sleep(delay)

    print("[capture] taking screenshot …")
    img = capture_screen()

    print("[vision] querying Llama 3.2 Vision …")
    try:
        x, y = ask_vision_model(img)
    except ValueError as exc:
        print(f"[error] {exc}")
        return

    print(f"[target] answer box center: ({x}, {y})")
    human_click(x, y)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Run once; wrap in a loop if you need to handle multiple questions.
    answer_question()
