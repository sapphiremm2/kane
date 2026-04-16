"""
app.py  —  Kane  |  AI Answer Bot
----------------------------------
Strategy change: model now returns answer NUMBER (1-4), not coordinates.
PIL scans the screenshot to detect actual button positions.
DPI scaling is handled automatically.

Dependencies:
    pip install customtkinter mss pillow ollama pydirectinput
"""

import base64
import ctypes
import io
import json
import math
import random
import re
import threading
import time
from datetime import datetime

import customtkinter as ctk
import mss
import ollama
import pydirectinput
from PIL import Image, ImageTk, ImageFilter

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# ── Palette (aino-inspired: sharp, monospace, near-black + cream) ──────────
BG       = "#0f0f0f"
SURFACE  = "#151515"
SURFACE2 = "#1c1c1c"
BORDER   = "#2e2e2e"
CREAM    = "#f0efe8"
DIM      = "#555555"
ACCENT   = "#d4ff00"   # electric yellow-green
GREEN    = "#00e676"
RED      = "#ff1744"
ORANGE   = "#ff9100"
LOG_TXT  = "#39d353"   # GitHub-contribution green

MONO  = ("Courier New", 11)
MONO_S = ("Courier New", 10)
MONO_L = ("Courier New", 13, "bold")
SANS  = ("Segoe UI", 10)

OLLAMA_MODEL = "llama3.2-vision"
MODEL_MAX_W  = 1024


# ── DPI ─────────────────────────────────────────────────────────────────────

def get_dpi_scale(mon_idx: int = 1) -> float:
    logical  = ctypes.windll.user32.GetSystemMetrics(0)
    with mss.mss() as sct:
        physical = sct.monitors[mon_idx]["width"]
    return (physical / logical) if logical else 1.0


# ── Screen capture ───────────────────────────────────────────────────────────

def list_monitors() -> list[dict]:
    with mss.mss() as sct:
        return sct.monitors[1:]


def capture_screen(mon_idx: int) -> tuple[Image.Image, dict]:
    with mss.mss() as sct:
        mon = sct.monitors[mon_idx]
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    return img, mon


def to_jpeg_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return base64.b64encode(buf.getvalue()).decode()


def downscale(img: Image.Image, max_w: int = MODEL_MAX_W) -> tuple[Image.Image, float]:
    w, h = img.size
    if w <= max_w:
        return img, 1.0
    scale = max_w / w
    return img.resize((max_w, int(h * scale)), Image.LANCZOS), scale


# ── Vision: ask ONLY for answer number ──────────────────────────────────────
#
# Asking for pixel coordinates was unreliable — the model hallucinated them.
# Instead we ask for a simple classification (1-4) which is a task vision
# models handle well, then we detect button positions ourselves with PIL.

CLASSIFY_PROMPT = """\
You are looking at a screenshot of a multiple-choice quiz.

The quiz has ONE question and FOUR answer options listed below it, \
numbered 1 to 4 from top to bottom.

Your job:
1. Read the question.
2. Decide which answer is correct.
3. Return ONLY this JSON (no markdown, no extra text):
{"answer": <1|2|3|4>, "rationale": "<brief reason>"}

"answer" is the position number of the correct option counting from the top. \
Do not guess — reason from the question and answer text."""


def classify_answer(img: Image.Image) -> tuple[int, str]:
    """Returns (answer_number 1-4, rationale)."""
    small, _ = downscale(img)
    resp = ollama.chat(
        model=OLLAMA_MODEL,
        messages=[{"role": "user", "content": CLASSIFY_PROMPT,
                   "images": [to_jpeg_b64(small)]}],
    )
    raw = resp["message"]["content"].strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"```$", "", raw, flags=re.MULTILINE).strip()

    try:
        data = json.loads(raw)
        return int(data["answer"]), str(data.get("rationale", "—"))
    except (json.JSONDecodeError, KeyError, ValueError):
        m = re.search(r'"answer"\s*:\s*([1-4])', raw)
        r = re.search(r'"rationale"\s*:\s*"([^"]+)"', raw)
        if m:
            return int(m.group(1)), (r.group(1) if r else "—")
        raise ValueError(f"Could not parse answer number from:\n{raw}")


# ── Button detection (PIL) ───────────────────────────────────────────────────
#
# Scans the right portion of the screenshot for 4 horizontal button bands.
# Works by computing row-level luminosity variance:
#   - Inside a button: low variance (uniform background color)
#   - Between buttons / background: higher variance or different luminosity

def detect_buttons(img: Image.Image) -> list[tuple[int, int]] | None:
    """
    Returns a list of 4 (x, y) centers in top-to-bottom order,
    or None if detection fails.
    """
    w, h = img.size

    # The answer buttons are in the right portion of the screen.
    # Crop to right 55%, skip top 15% (nav/header) and bottom 10%.
    x0 = int(w * 0.45)
    y0 = int(h * 0.15)
    x1 = int(w * 0.98)
    y1 = int(h * 0.90)

    crop = img.crop((x0, y0, x1, y1))
    cw, ch = crop.size

    # Slight blur to reduce noise before analysis
    blurred = crop.filter(ImageFilter.GaussianBlur(radius=2))
    gray    = blurred.convert("L")
    pixels  = gray.load()

    # Build per-row average luminosity (sample every 4 px for speed)
    step = max(1, cw // 64)
    row_avg = []
    for y in range(ch):
        vals = [pixels[x, y] for x in range(0, cw, step)]
        row_avg.append(sum(vals) / len(vals))

    # Smooth with a 7-row rolling mean
    def smooth(data, k=7):
        out = []
        for i in range(len(data)):
            s = max(0, i - k)
            e = min(len(data), i + k + 1)
            out.append(sum(data[s:e]) / (e - s))
        return out

    smoothed = smooth(row_avg)
    global_avg = sum(smoothed) / len(smoothed)

    # Detect "button bands": rows that are meaningfully brighter OR darker
    # than the overall average (the quiz background is one solid color,
    # buttons have a different shade).
    threshold = 8
    in_band = False
    band_start = 0
    bands: list[tuple[int, int]] = []

    for y, lum in enumerate(smoothed):
        is_button = abs(lum - global_avg) > threshold
        if is_button and not in_band:
            in_band, band_start = True, y
        elif not is_button and in_band:
            in_band = False
            if y - band_start >= 12:         # min height filter
                bands.append((band_start, y))

    if in_band and ch - band_start >= 12:
        bands.append((band_start, ch))

    # Keep the 4 tallest bands, then sort by Y
    bands = sorted(
        sorted(bands, key=lambda b: b[1] - b[0], reverse=True)[:4],
        key=lambda b: b[0],
    )

    if len(bands) != 4:
        return None

    x_center = x0 + cw // 2
    return [(x_center, y0 + (s + e) // 2) for s, e in bands]


def find_click_target(img: Image.Image, answer_num: int) -> tuple[int, int] | None:
    """
    Combine PIL detection + answer number to get the click coordinate.
    Falls back to None if detection can't find 4 buttons.
    """
    buttons = detect_buttons(img)
    if buttons and 1 <= answer_num <= 4:
        return buttons[answer_num - 1]
    return None


# ── Mouse movement ───────────────────────────────────────────────────────────

def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return u**3*p0 + 3*u**2*t*p1 + 3*u*t**2*p2 + t**3*p3


def curved_move(tx: int, ty: int, steps: int = 55, delay: float = 0.008):
    class _PT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    pt = _PT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    sx, sy = pt.x, pt.y
    dx, dy = tx - sx, ty - sy
    dist = math.hypot(dx, dy) or 1
    ps = random.uniform(0.2, 0.45) * dist * random.choice([-1, 1])
    mx, my = (sx + tx) / 2, (sy + ty) / 2
    cp1x = sx + dx*random.uniform(.15,.35) + (-dy/dist)*ps*random.uniform(.5,1)
    cp1y = sy + dy*random.uniform(.15,.35) + ( dx/dist)*ps*random.uniform(.5,1)
    cp2x = mx + dx*random.uniform(.10,.25) + (-dy/dist)*ps*random.uniform(.3,.7)
    cp2y = my + dy*random.uniform(.10,.25) + ( dx/dist)*ps*random.uniform(.3,.7)
    for i in range(1, steps + 1):
        t = i / steps
        te = t*t*(3 - 2*t)
        px = int(round(_bezier(sx, cp1x, cp2x, tx, te) + random.randint(-2, 2)))
        py = int(round(_bezier(sy, cp1y, cp2y, ty, te) + random.randint(-2, 2)))
        pydirectinput.moveTo(px, py)
        time.sleep(delay / (1 + 1.5*math.sin(math.pi*t)))
    pydirectinput.moveTo(tx, ty)


def human_click(x: int, y: int, speed: float = 0.008):
    curved_move(x, y, delay=speed)
    time.sleep(random.uniform(0.04, 0.15))
    pydirectinput.click(x, y)


# ════════════════════════════════════════════════════════════════════════════
# GUI
# ════════════════════════════════════════════════════════════════════════════

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("KANE")
        self.geometry("960x620")
        self.minsize(860, 540)
        self.configure(fg_color=BG)

        self._running  = False
        self._stop_evt = threading.Event()
        self._thread   = None
        self._cycle    = 0
        self._monitors = list_monitors()

        self._build()
        self._refresh_monitors()

    # ── Layout ──────────────────────────────────────────────────────────────

    def _build(self):
        # ── Header bar ───────────────────────────────────────────────────
        header = ctk.CTkFrame(self, fg_color=SURFACE, height=48, corner_radius=0)
        header.pack(fill="x")
        header.pack_propagate(False)

        ctk.CTkLabel(header, text="KANE", font=("Courier New", 14, "bold"),
                     text_color=CREAM).pack(side="left", padx=20)

        ctk.CTkLabel(header, text="/ AI ANSWER BOT", font=MONO,
                     text_color=DIM).pack(side="left", padx=0)

        # status pill
        pill = ctk.CTkFrame(header, fg_color=SURFACE2, corner_radius=0,
                             border_width=1, border_color=BORDER)
        pill.pack(side="left", padx=16)
        self._led = ctk.CTkLabel(pill, text="●", font=("Courier New", 11),
                                  text_color=DIM, width=16)
        self._led.pack(side="left", padx=(10, 4), pady=7)
        self._status_lbl = ctk.CTkLabel(pill, text="IDLE", font=MONO_S,
                                         text_color=DIM, width=72)
        self._status_lbl.pack(side="left", padx=(0, 10), pady=7)

        # cycle counter
        self._cycle_lbl = ctk.CTkLabel(header, text="CYC: 000",
                                        font=MONO_S, text_color=DIM)
        self._cycle_lbl.pack(side="right", padx=20)

        # start/stop
        self._btn = ctk.CTkButton(
            header, text="[ START ]", width=100, height=32,
            font=MONO, fg_color="transparent", hover_color=SURFACE2,
            border_width=1, border_color=ACCENT, text_color=ACCENT,
            corner_radius=0, command=self._toggle,
        )
        self._btn.pack(side="right", padx=(0, 8))

        # ── Body ─────────────────────────────────────────────────────────
        body = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        body.pack(fill="both", expand=True)

        # Left: log ───────────────────────────────────────────────────────
        log_frame = ctk.CTkFrame(body, fg_color=SURFACE, corner_radius=0,
                                  border_width=1, border_color=BORDER)
        log_frame.pack(side="left", fill="both", expand=True,
                       padx=(8, 4), pady=8)

        log_head = ctk.CTkFrame(log_frame, fg_color=SURFACE2,
                                 height=28, corner_radius=0)
        log_head.pack(fill="x")
        log_head.pack_propagate(False)
        ctk.CTkLabel(log_head, text="  REASONING LOG", font=MONO_S,
                     text_color=DIM, anchor="w").pack(side="left")

        self._log_box = ctk.CTkTextbox(
            log_frame, font=MONO,
            fg_color="#000000", text_color=LOG_TXT,
            scrollbar_button_color=BORDER,
            wrap="word", state="disabled", corner_radius=0,
            border_width=0,
        )
        self._log_box.pack(fill="both", expand=True)

        # Right panel ─────────────────────────────────────────────────────
        right = ctk.CTkFrame(body, fg_color=BG, width=286, corner_radius=0)
        right.pack(side="right", fill="y", padx=(4, 8), pady=8)
        right.pack_propagate(False)

        # Preview
        prev = ctk.CTkFrame(right, fg_color=SURFACE, corner_radius=0,
                             border_width=1, border_color=BORDER)
        prev.pack(fill="x", pady=(0, 6))

        ph = ctk.CTkFrame(prev, fg_color=SURFACE2, height=28, corner_radius=0)
        ph.pack(fill="x")
        ph.pack_propagate(False)
        ctk.CTkLabel(ph, text="  LAST CAPTURE", font=MONO_S,
                     text_color=DIM, anchor="w").pack(side="left")

        self._preview = ctk.CTkLabel(
            prev, text="—", fg_color="#000000",
            width=268, height=162, corner_radius=0,
            font=MONO_S, text_color=DIM,
        )
        self._preview.pack(padx=8, pady=(6, 4))

        self._target_lbl = ctk.CTkLabel(
            prev, text="TARGET  —", font=MONO_S, text_color=ACCENT,
            anchor="w",
        )
        self._target_lbl.pack(fill="x", padx=10, pady=(0, 8))

        # Settings card
        cfg = ctk.CTkFrame(right, fg_color=SURFACE, corner_radius=0,
                           border_width=1, border_color=BORDER)
        cfg.pack(fill="x")

        ch = ctk.CTkFrame(cfg, fg_color=SURFACE2, height=28, corner_radius=0)
        ch.pack(fill="x")
        ch.pack_propagate(False)
        ctk.CTkLabel(ch, text="  SETTINGS", font=MONO_S,
                     text_color=DIM, anchor="w").pack(side="left")

        # monitor selector
        self._mk("DISPLAY", cfg)
        self._monitor_var = ctk.StringVar(value="MON 1")
        self._mon_menu = ctk.CTkOptionMenu(
            cfg, variable=self._monitor_var, values=["MON 1"],
            fg_color=SURFACE2, button_color=BORDER,
            button_hover_color=SURFACE, text_color=CREAM,
            font=MONO_S, corner_radius=0, width=268,
            command=lambda _: None,
        )
        self._mon_menu.pack(padx=8, pady=(0, 8))

        # delay
        self._mk("DELAY MIN / MAX (s)", cfg)
        dr = ctk.CTkFrame(cfg, fg_color="transparent")
        dr.pack(fill="x", padx=8, pady=(0, 8))
        self._dmin = ctk.IntVar(value=15)
        self._dmax = ctk.IntVar(value=45)
        self._dmin_l = ctk.CTkLabel(dr, text="15", width=24, font=MONO_S, text_color=CREAM)
        self._dmax_l = ctk.CTkLabel(dr, text="45", width=24, font=MONO_S, text_color=CREAM)
        ctk.CTkSlider(dr, from_=1, to=120, variable=self._dmin, width=96,
                       button_color=ACCENT, progress_color=ACCENT,
                       command=lambda v: (self._dmin.set(int(v)),
                                          self._dmin_l.configure(text=str(int(v))))
                       ).pack(side="left", padx=(0, 4))
        self._dmin_l.pack(side="left", padx=(0, 8))
        ctk.CTkSlider(dr, from_=1, to=120, variable=self._dmax, width=96,
                       button_color=ACCENT, progress_color=ACCENT,
                       command=lambda v: (self._dmax.set(int(v)),
                                          self._dmax_l.configure(text=str(int(v))))
                       ).pack(side="left", padx=(0, 4))
        self._dmax_l.pack(side="left")

        # speed
        self._mk("MOUSE SPEED", cfg)
        sr = ctk.CTkFrame(cfg, fg_color="transparent")
        sr.pack(fill="x", padx=8, pady=(0, 12))
        self._speed = ctk.DoubleVar(value=0.008)
        self._speed_l = ctk.CTkLabel(sr, text="0.008", width=44,
                                      font=MONO_S, text_color=CREAM)
        ctk.CTkSlider(sr, from_=0.002, to=0.030, number_of_steps=140,
                       variable=self._speed, button_color=ACCENT,
                       progress_color=ACCENT,
                       command=lambda v: self._speed_l.configure(
                           text=f"{v:.3f}")
                       ).pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._speed_l.pack(side="left")

        # ── Footer ────────────────────────────────────────────────────────
        foot = ctk.CTkFrame(self, fg_color=SURFACE, height=26, corner_radius=0)
        foot.pack(fill="x", side="bottom")
        foot.pack_propagate(False)
        ctk.CTkLabel(foot, text="  OLLAMA  ·  LLAMA 3.2 VISION  ·  LOCAL",
                     font=MONO_S, text_color=DIM).pack(side="left")

    def _mk(self, label: str, parent):
        ctk.CTkLabel(parent, text=f"  {label}", font=MONO_S,
                     text_color=DIM, anchor="w").pack(fill="x", pady=(6, 2))

    # ── Monitor helpers ──────────────────────────────────────────────────────

    def _refresh_monitors(self):
        self._monitors = list_monitors()
        labels = [f"MON {i+1}  {m['width']}×{m['height']}"
                  for i, m in enumerate(self._monitors)]
        self._mon_menu.configure(values=labels or ["MON 1"])
        if labels:
            self._monitor_var.set(labels[0])

    def _mon_idx(self) -> int:
        try:
            return int(self._monitor_var.get().split()[1])
        except (IndexError, ValueError):
            return 1

    # ── Start / stop ─────────────────────────────────────────────────────────

    def _toggle(self):
        if self._running:
            self._running = False
            self._stop_evt.set()
            self._set_status("STOP…", ORANGE)
            self._btn.configure(state="disabled")
            threading.Thread(target=self._await_stop, daemon=True).start()
        else:
            self._running = True
            self._stop_evt.clear()
            self._set_status("RUN", GREEN)
            self._btn.configure(text="[ STOP  ]", border_color=RED,
                                 text_color=RED)
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()

    def _await_stop(self):
        if self._thread:
            self._thread.join()
        self.after(0, self._idle)

    def _idle(self):
        self._btn.configure(text="[ START ]", border_color=ACCENT,
                             text_color=ACCENT, state="normal")
        self._set_status("IDLE", DIM)

    # ── Worker ────────────────────────────────────────────────────────────────

    def _worker(self):
        while not self._stop_evt.is_set():
            self._cycle += 1
            self.after(0, self._cycle_lbl.configure,
                       {"text": f"CYC: {self._cycle:03d}"})

            # Reading delay (interruptible)
            delay = random.uniform(self._dmin.get(), self._dmax.get())
            self._log(f"━━━ CYCLE {self._cycle:03d} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            self._log(f"WAIT  {delay:.1f}s")
            end = time.monotonic() + delay
            while time.monotonic() < end:
                if self._stop_evt.is_set():
                    return
                time.sleep(0.1)

            if self._stop_evt.is_set():
                return

            # Capture
            mon = self._mon_idx()
            self._log(f"CAPTURE  monitor {mon}")
            try:
                img, mon_info = capture_screen(mon)
            except Exception as e:
                self._log(f"ERR  capture: {e}")
                continue

            self._update_preview(img)
            dpi = get_dpi_scale(mon)
            self._log(f"DPI scale: {dpi:.2f}×   image: {img.size[0]}×{img.size[1]}")

            # Classify
            self._log("MODEL  classifying answer…")
            try:
                answer_num, rationale = classify_answer(img)
            except Exception as e:
                self._log(f"ERR  model: {e}")
                continue

            self._log(f"ANSWER  #{answer_num}  —  {rationale}")

            if self._stop_evt.is_set():
                return

            # Detect button positions with PIL
            self._log("DETECT  scanning for buttons…")
            phys_xy = find_click_target(img, answer_num)

            if phys_xy is None:
                self._log("WARN  button detection failed — skipping click")
                continue

            phys_x, phys_y = phys_xy

            # Add monitor offset, then divide by DPI scale → logical coords
            abs_phys_x = phys_x + mon_info["left"]
            abs_phys_y = phys_y + mon_info["top"]
            log_x = int(round(abs_phys_x / dpi))
            log_y = int(round(abs_phys_y / dpi))

            self._log(f"PHYS ({abs_phys_x},{abs_phys_y})  →  CLICK ({log_x},{log_y})")
            self.after(0, self._target_lbl.configure,
                       {"text": f"TARGET  ({log_x}, {log_y})"})

            if self._stop_evt.is_set():
                return

            # Click
            self._log("CLICK")
            try:
                human_click(log_x, log_y, speed=self._speed.get())
            except Exception as e:
                self._log(f"ERR  click: {e}")
                continue

            self._log("OK ✓")

    # ── UI helpers ────────────────────────────────────────────────────────────

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.after(0, self._append_log, f"[{ts}] {msg}\n")

    def _append_log(self, line: str):
        self._log_box.configure(state="normal")
        self._log_box.insert("end", line)
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _set_status(self, text: str, color: str):
        self.after(0, self._led.configure,        {"text_color": color})
        self.after(0, self._status_lbl.configure, {"text": text, "text_color": color})

    def _update_preview(self, img: Image.Image):
        thumb = img.copy()
        thumb.thumbnail((268, 162), Image.LANCZOS)
        tk_img = ImageTk.PhotoImage(thumb)
        self.after(0, self._set_preview, tk_img)

    def _set_preview(self, tk_img):
        self._preview._tk_image = tk_img
        self._preview.configure(image=tk_img, text="")


if __name__ == "__main__":
    App().mainloop()
