"""
app.py  —  Kane  |  AI Answer Bot
----------------------------------
Dependencies:
    pip install customtkinter mss pillow ollama pydirectinput
"""

import base64
import ctypes
import io
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime

import customtkinter as ctk
import mss
import ollama
import pydirectinput
from PIL import Image, ImageTk, ImageFilter

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# ── Version & update config ───────────────────────────────────────────────────
VERSION      = "1.0.0"
GITHUB_REPO  = "sapphiremm2/kane"
RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def _parse_version(tag: str) -> tuple[int, ...]:
    """'v1.2.3' or '1.2.3' → (1, 2, 3)"""
    return tuple(int(x) for x in tag.lstrip("v").split(".") if x.isdigit())


def fetch_latest_release() -> dict | None:
    """
    Returns {"version": "1.2.0", "url": "<exe download url>", "notes": "..."}
    or None if the check fails / no .exe asset found.
    """
    try:
        req = urllib.request.Request(
            RELEASES_URL,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "Kane-Updater"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())

        tag   = data.get("tag_name", "")
        notes = data.get("body", "")
        exe_url = next(
            (a["browser_download_url"] for a in data.get("assets", [])
             if a["name"].lower().endswith(".exe")),
            None,
        )
        if not exe_url or not tag:
            return None
        return {"version": tag.lstrip("v"), "url": exe_url, "notes": notes}
    except Exception:
        return None


def download_update(url: str, dest: str, progress_cb=None) -> bool:
    """
    Download url → dest. progress_cb(pct: float) called every chunk.
    Returns True on success.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Kane-Updater"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            done  = 0
            chunk = 65536
            with open(dest, "wb") as f:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    if progress_cb and total:
                        progress_cb(done / total)
        return True
    except Exception:
        return False


def launch_updater(new_exe: str, current_exe: str):
    """
    Writes a tiny .bat to %TEMP% that:
      1. Waits for this process to exit
      2. Replaces the old .exe with the new one
      3. Relaunches Kane
      4. Deletes itself
    Then launches it detached and exits.
    """
    bat = os.path.join(tempfile.gettempdir(), "kane_update.bat")
    with open(bat, "w") as f:
        f.write(
            f'@echo off\n'
            f':wait\n'
            f'tasklist /FI "PID eq {os.getpid()}" 2>NUL | find /I "{os.getpid()}" >NUL\n'
            f'if not errorlevel 1 (timeout /t 1 /nobreak >nul & goto wait)\n'
            f'move /y "{new_exe}" "{current_exe}"\n'
            f'start "" "{current_exe}"\n'
            f'del "%~f0"\n'
        )
    subprocess.Popen(["cmd", "/c", bat],
                     creationflags=subprocess.CREATE_NO_WINDOW |
                                   subprocess.DETACHED_PROCESS)
    sys.exit(0)


# ── Update dialog ─────────────────────────────────────────────────────────────

class UpdateDialog(ctk.CTkToplevel):
    def __init__(self, parent, release: dict):
        super().__init__(parent)
        self.title("Update Available")
        self.geometry("480x280")
        self.resizable(False, False)
        self.configure(fg_color=SURFACE)
        self.grab_set()
        self.lift()

        self._release = release
        self._accepted = False

        # Running as bundled .exe?
        self._frozen = getattr(sys, "frozen", False)

        ctk.CTkLabel(self, text="UPDATE AVAILABLE", font=("Courier New",13,"bold"),
                     text_color=ACCENT).pack(pady=(22,4))
        ctk.CTkLabel(self,
                     text=f"  v{VERSION}  →  v{release['version']}  ",
                     font=MONO, text_color=CREAM).pack()

        notes_box = ctk.CTkTextbox(self, height=80, font=MONO_S,
                                   fg_color="#000", text_color=DIM2,
                                   corner_radius=0, border_width=0)
        notes_box.pack(fill="x", padx=20, pady=12)
        notes_box.insert("end", release["notes"] or "No release notes.")
        notes_box.configure(state="disabled")

        # Progress bar (hidden until download starts)
        self._prog = ctk.CTkProgressBar(self, height=6, corner_radius=0,
                                        fg_color=BORDER, progress_color=ACCENT)
        self._prog.set(0)

        self._info_lbl = ctk.CTkLabel(self, text="", font=MONO_S, text_color=DIM2)

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(pady=(0, 18))

        if not self._frozen:
            # Running from source — can't self-replace, just link to release
            ctk.CTkLabel(self,
                         text="Run from source: update via  git pull",
                         font=MONO_S, text_color=DIM2).pack(pady=(0,8))
            ctk.CTkButton(btn_row, text="[ CLOSE ]", width=120, height=30,
                          font=MONO, fg_color="transparent", corner_radius=0,
                          border_width=1, border_color=BORDER, text_color=DIM2,
                          command=self.destroy).pack()
            return

        self._update_btn = ctk.CTkButton(
            btn_row, text="[ UPDATE NOW ]", width=140, height=30,
            font=MONO, fg_color="transparent", corner_radius=0,
            border_width=1, border_color=ACCENT, text_color=ACCENT,
            command=self._start_download,
        )
        self._update_btn.pack(side="left", padx=8)

        ctk.CTkButton(
            btn_row, text="[ SKIP ]", width=90, height=30,
            font=MONO, fg_color="transparent", corner_radius=0,
            border_width=1, border_color=BORDER, text_color=DIM2,
            command=self.destroy,
        ).pack(side="left", padx=8)

    def _start_download(self):
        self._update_btn.configure(state="disabled", text="DOWNLOADING...")
        self._prog.pack(fill="x", padx=20, pady=(0, 4))
        self._info_lbl.pack()

        threading.Thread(target=self._download_worker, daemon=True).start()

    def _download_worker(self):
        current_exe = sys.executable
        tmp = current_exe + ".update.exe"

        def progress(pct):
            self.after(0, self._prog.set, pct)
            self.after(0, self._info_lbl.configure,
                       {"text": f"  {pct*100:.0f}%  downloading…"})

        ok = download_update(self._release["url"], tmp, progress)

        if ok:
            self.after(0, self._info_lbl.configure,
                       {"text": "  Installing — app will restart…"})
            self.after(800, lambda: launch_updater(tmp, current_exe))
        else:
            self.after(0, self._info_lbl.configure,
                       {"text": "  Download failed. Try again later."})
            self.after(0, self._update_btn.configure,
                       {"state": "normal", "text": "[ RETRY ]"})

# ── Palette ──────────────────────────────────────────────────────────────────
BG       = "#0f0f0f"
SURFACE  = "#151515"
SURFACE2 = "#1c1c1c"
BORDER   = "#2a2a2a"
CREAM    = "#f0efe8"
DIM      = "#4a4a4a"
DIM2     = "#666666"
ACCENT   = "#d4ff00"
GREEN    = "#00e676"
RED      = "#ff1744"
ORANGE   = "#ff9100"
SYS_TXT  = "#39d353"   # system log green
AI_TXT   = "#58a6ff"   # reasoning log blue

MONO   = ("Courier New", 11)
MONO_S = ("Courier New", 10)
MONO_L = ("Courier New", 13, "bold")

OLLAMA_MODEL = "llama3.2-vision"
MODEL_MAX_W  = 1024

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# ── DPI ──────────────────────────────────────────────────────────────────────

def get_dpi_scale(mon_idx: int = 1) -> float:
    logical = ctypes.windll.user32.GetSystemMetrics(0)
    with mss.mss() as sct:
        physical = sct.monitors[mon_idx]["width"]
    return (physical / logical) if logical else 1.0


# ── Capture ───────────────────────────────────────────────────────────────────

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


# ── Vision (streaming so Stop works immediately) ──────────────────────────────

CLASSIFY_PROMPT = """\
You are looking at a screenshot of a multiple-choice quiz.
There is a question and between 2 and 4 answer options shown as buttons below it.

Instructions:
- Read the question.
- Identify the correct answer button.
- Count the buttons from the top: the topmost button is 1, next is 2, etc.
- Output ONLY the following JSON. No markdown. No explanation outside the JSON.

{"answer": <integer 1-4>, "rationale": "<one sentence>"}

If you output anything other than that exact JSON your response will be rejected."""


def parse_answer_response(raw: str) -> tuple[int, str]:
    """
    Multi-stage parser — handles JSON, partial JSON, and markdown prose fallbacks.
    """
    # Strip markdown code fences
    raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"```$",          "", raw, flags=re.MULTILINE).strip()

    # 1. Try clean JSON parse
    try:
        data = json.loads(raw)
        return int(data["answer"]), str(data.get("rationale", "—"))
    except (json.JSONDecodeError, KeyError, ValueError):
        pass

    # 2. JSON anywhere inside the response
    m = re.search(r'\{[^{}]*"answer"\s*:\s*([1-4])[^{}]*\}', raw)
    if m:
        try:
            data = json.loads(m.group(0))
            return int(data["answer"]), str(data.get("rationale", "—"))
        except Exception:
            pass

    # 3. Pull answer number from common prose patterns
    #    e.g. "answer": 1  /  Correct Answer: 1  /  Option 1  /  **1.**
    patterns = [
        r'"answer"\s*:\s*([1-4])',                              # json key
        r'(?:correct\s+answer|answer\s+is)\D{0,10}([1-4])\b',  # "correct answer: 1"
        r'\bOption\s+([1-4])\b',                                # "Option 1"
        r'^\*{0,2}([1-4])[.\)]\s',                             # "1. text" at line start
        r'\b([1-4])\s+is\s+(?:correct|the\s+answer)',          # "1 is correct"
    ]
    for pat in patterns:
        m = re.search(pat, raw, re.IGNORECASE | re.MULTILINE)
        if m:
            rat_m = re.search(r'"rationale"\s*:\s*"([^"]+)"', raw)
            return int(m.group(1)), (rat_m.group(1) if rat_m else "—")

    raise ValueError(f"Could not extract answer number from model output:\n{raw}")


def classify_answer_stream(
    img: Image.Image,
    stop_evt: threading.Event,
    on_token,           # callback(str) — each streamed token
) -> tuple[int, str]:
    """
    Streams tokens from the model so we can:
      • Show live reasoning in the AI log
      • Abort immediately when stop_evt fires
    Raises InterruptedError if stopped mid-stream.
    """
    small, _ = downscale(img)
    full = ""

    for chunk in ollama.chat(
        model=OLLAMA_MODEL,
        messages=[{
            "role": "user",
            "content": CLASSIFY_PROMPT,
            "images": [to_jpeg_b64(small)],
        }],
        stream=True,
    ):
        if stop_evt.is_set():
            raise InterruptedError("Stopped by user")
        token = chunk["message"]["content"]
        full += token
        on_token(token)

    return parse_answer_response(full)


# ── Button detection ──────────────────────────────────────────────────────────
#
# Works for both layouts seen so far:
#   • Centered quiz (buttons fill ~40-70% of screen width, horizontally centered)
#   • Left-panel + right-answers layout
#
# Strategy:
#   1. Scan full image width (skip thin edges + nav bar + taskbar)
#   2. Find horizontal bands whose average luminosity differs from the background
#   3. For each band, find the actual left/right extent of the "different" pixels
#      so the X center is the true button center, not a fixed crop offset
#   4. Accept 2-4 buttons (not hardcoded to 4)

def _smooth(data: list[float], k: int = 7) -> list[float]:
    out = []
    for i in range(len(data)):
        s, e = max(0, i-k), min(len(data), i+k+1)
        out.append(sum(data[s:e]) / (e - s))
    return out


def detect_buttons(img: Image.Image) -> list[tuple[int, int]] | None:
    w, h = img.size

    # Scan from 30% down — question/passage cards are always above the buttons.
    # Skip taskbar (~8% from bottom) and thin side margins.
    x0, y0 = int(w * .03), int(h * .30)
    x1, y1 = int(w * .97), int(h * .92)
    crop = img.crop((x0, y0, x1, y1))
    cw, ch = crop.size

    blurred = crop.filter(ImageFilter.GaussianBlur(radius=3))
    gray    = blurred.convert("L")
    pixels  = gray.load()

    step = max(1, cw // 120)
    row_avg = [
        sum(pixels[x, y] for x in range(0, cw, step)) / max(1, cw // step)
        for y in range(ch)
    ]
    smoothed   = _smooth(row_avg, k=6)
    global_avg = sum(smoothed) / len(smoothed)
    variance   = sum((v - global_avg)**2 for v in smoothed) / len(smoothed)
    threshold  = max(8.0, variance**0.5 * 0.6)

    # Button height constraints (as fraction of the cropped region height):
    #   min: buttons are at least ~2% of crop height
    #   max: buttons are at most ~18% of crop height — anything taller is a
    #        question card, image, or reading passage, NOT a clickable button.
    min_h = max(12, int(ch * .02))
    max_h = int(ch * .18)

    in_band = False
    band_start = 0
    bands: list[tuple[int, int]] = []
    for y, lum in enumerate(smoothed):
        is_btn = abs(lum - global_avg) > threshold
        if is_btn and not in_band:
            in_band, band_start = True, y
        elif not is_btn and in_band:
            in_band = False
            band_h = y - band_start
            if min_h <= band_h <= max_h:      # ← rejects question cards (too tall)
                bands.append((band_start, y))
    if in_band:
        band_h = ch - band_start
        if min_h <= band_h <= max_h:
            bands.append((band_start, ch))

    # Keep 2–4 bands, tallest first then sort top-to-bottom
    bands = sorted(
        sorted(bands, key=lambda b: b[1] - b[0], reverse=True)[:4],
        key=lambda b: b[0],
    )
    if len(bands) < 2:
        return None

    # For each band find the true horizontal centre of the button pixels
    results: list[tuple[int, int]] = []
    for bs, be in bands:
        mid_y = (bs + be) // 2
        btn_cols = [
            x for x in range(0, cw, step)
            if abs(pixels[x, mid_y] - global_avg) > threshold
        ]
        if btn_cols:
            cx = x0 + (min(btn_cols) + max(btn_cols)) // 2
        else:
            cx = w // 2
        results.append((cx, y0 + (bs + be) // 2))

    return results


# ── Mouse ─────────────────────────────────────────────────────────────────────

def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return u**3*p0 + 3*u**2*t*p1 + 3*u*t**2*p2 + t**3*p3


def curved_move(tx: int, ty: int, stop_evt: threading.Event,
                steps: int = 55, delay: float = 0.008):
    class _PT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    pt = _PT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    sx, sy = pt.x, pt.y
    dx, dy = tx-sx, ty-sy
    dist = math.hypot(dx, dy) or 1
    ps = random.uniform(.2, .45) * dist * random.choice([-1, 1])
    mx, my = (sx+tx)/2, (sy+ty)/2
    cp1x = sx + dx*random.uniform(.15,.35) + (-dy/dist)*ps*random.uniform(.5,1)
    cp1y = sy + dy*random.uniform(.15,.35) + ( dx/dist)*ps*random.uniform(.5,1)
    cp2x = mx + dx*random.uniform(.10,.25) + (-dy/dist)*ps*random.uniform(.3,.7)
    cp2y = my + dy*random.uniform(.10,.25) + ( dx/dist)*ps*random.uniform(.3,.7)
    for i in range(1, steps+1):
        if stop_evt.is_set():
            return
        t = i / steps
        te = t*t*(3-2*t)
        px = int(round(_bezier(sx,cp1x,cp2x,tx,te) + random.randint(-2,2)))
        py = int(round(_bezier(sy,cp1y,cp2y,ty,te) + random.randint(-2,2)))
        pydirectinput.moveTo(px, py)
        time.sleep(delay / (1 + 1.5*math.sin(math.pi*t)))
    pydirectinput.moveTo(tx, ty)


def human_click(x: int, y: int, stop_evt: threading.Event, speed: float = 0.008):
    curved_move(x, y, stop_evt, delay=speed)
    if stop_evt.is_set():
        return
    time.sleep(random.uniform(.04, .15))
    pydirectinput.click(x, y)


# ════════════════════════════════════════════════════════════════════════════
# GUI
# ════════════════════════════════════════════════════════════════════════════

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("KANE")
        self.geometry("1100x660")
        self.minsize(900, 540)
        self.configure(fg_color=BG)

        self._running   = False
        self._stop_evt  = threading.Event()
        self._thread    = None
        self._cycle     = 0
        self._monitors  = list_monitors()

        # animation state
        self._spin_i    = 0
        self._anim_on   = False
        self._anim_state   = "idle"   # idle | waiting | model | detect | click
        self._anim_start   = 0.0
        self._anim_total   = 0.0

        self._build()
        self._refresh_monitors()
        # Check for updates in background so startup isn't blocked
        threading.Thread(target=self._check_update, daemon=True).start()

    # ─────────────────────────────────────────────────────────────────────────
    # Build UI
    # ─────────────────────────────────────────────────────────────────────────

    def _build(self):
        # ── Header ───────────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(self, fg_color=SURFACE, height=46, corner_radius=0)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        ctk.CTkLabel(hdr, text="KANE", font=("Courier New",14,"bold"),
                     text_color=CREAM).pack(side="left", padx=(18,0))
        ctk.CTkLabel(hdr, text=" / AI ANSWER BOT", font=MONO_S,
                     text_color=DIM2).pack(side="left")

        # status pill
        pill = ctk.CTkFrame(hdr, fg_color=SURFACE2, corner_radius=0,
                            border_width=1, border_color=BORDER)
        pill.pack(side="left", padx=14)
        self._led = ctk.CTkLabel(pill, text="●", font=("Courier New",12),
                                 text_color=DIM, width=16)
        self._led.pack(side="left", padx=(10,3), pady=6)
        self._status_lbl = ctk.CTkLabel(pill, text="IDLE", font=MONO_S,
                                        text_color=DIM, width=68)
        self._status_lbl.pack(side="left", padx=(0,10), pady=6)

        self._cycle_lbl = ctk.CTkLabel(hdr, text="CYC: 000",
                                       font=MONO_S, text_color=DIM2)
        self._cycle_lbl.pack(side="right", padx=18)

        self._btn = ctk.CTkButton(
            hdr, text="[ START ]", width=100, height=30,
            font=MONO, fg_color="transparent", hover_color=SURFACE2,
            border_width=1, border_color=ACCENT, text_color=ACCENT,
            corner_radius=0, command=self._toggle,
        )
        self._btn.pack(side="right", padx=(0,6))

        # ── Animation status bar ──────────────────────────────────────────────
        anim_bar = ctk.CTkFrame(self, fg_color=SURFACE2, height=26,
                                corner_radius=0)
        anim_bar.pack(fill="x")
        anim_bar.pack_propagate(False)

        self._anim_lbl = ctk.CTkLabel(
            anim_bar, text="  ——", font=MONO_S, text_color=DIM2, anchor="w",
        )
        self._anim_lbl.pack(side="left", fill="x", padx=4)

        # ── Body: two log panels ──────────────────────────────────────────────
        body = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        body.pack(fill="both", expand=True, padx=8, pady=(6,0))

        # System log (left)
        self._sys_log = self._log_panel(body, "SYSTEM LOG", SYS_TXT, side="left")

        # AI reasoning log (right)
        self._ai_log = self._log_panel(body, "AI REASONING", AI_TXT, side="right")

        # ── Settings bar ──────────────────────────────────────────────────────
        cfg = ctk.CTkFrame(self, fg_color=SURFACE, height=52, corner_radius=0,
                           border_width=0)
        cfg.pack(fill="x", padx=8, pady=(4,0))
        cfg.pack_propagate(False)

        # Monitor
        self._mk_cfg(cfg, "DISPLAY")
        self._monitor_var = ctk.StringVar(value="MON 1")
        self._mon_menu = ctk.CTkOptionMenu(
            cfg, variable=self._monitor_var, values=["MON 1"],
            fg_color=SURFACE2, button_color=BORDER, button_hover_color=SURFACE,
            text_color=CREAM, font=MONO_S, corner_radius=0, width=190,
        )
        self._mon_menu.pack(side="left", padx=(0, 24), pady=8)

        # Delay
        self._mk_cfg(cfg, "DELAY MIN")
        self._dmin = ctk.IntVar(value=15)
        self._dmin_l = ctk.CTkLabel(cfg, text="15s", font=MONO_S,
                                    text_color=CREAM, width=28)
        ctk.CTkSlider(cfg, from_=1, to=120, variable=self._dmin, width=110,
                      button_color=ACCENT, progress_color=ACCENT,
                      command=lambda v: (self._dmin.set(int(v)),
                                         self._dmin_l.configure(text=f"{int(v)}s"))
                      ).pack(side="left", padx=(0,2))
        self._dmin_l.pack(side="left", padx=(0,16))

        self._mk_cfg(cfg, "MAX")
        self._dmax = ctk.IntVar(value=45)
        self._dmax_l = ctk.CTkLabel(cfg, text="45s", font=MONO_S,
                                    text_color=CREAM, width=28)
        ctk.CTkSlider(cfg, from_=1, to=120, variable=self._dmax, width=110,
                      button_color=ACCENT, progress_color=ACCENT,
                      command=lambda v: (self._dmax.set(int(v)),
                                         self._dmax_l.configure(text=f"{int(v)}s"))
                      ).pack(side="left", padx=(0,2))
        self._dmax_l.pack(side="left", padx=(0,24))

        # Speed
        self._mk_cfg(cfg, "MOUSE SPEED")
        self._speed = ctk.DoubleVar(value=0.008)
        self._speed_l = ctk.CTkLabel(cfg, text="0.008", font=MONO_S,
                                     text_color=CREAM, width=40)
        ctk.CTkSlider(cfg, from_=0.002, to=0.030, number_of_steps=140,
                      variable=self._speed, width=110,
                      button_color=ACCENT, progress_color=ACCENT,
                      command=lambda v: self._speed_l.configure(text=f"{v:.3f}")
                      ).pack(side="left", padx=(0,2))
        self._speed_l.pack(side="left")

        # ── Footer ────────────────────────────────────────────────────────────
        foot = ctk.CTkFrame(self, fg_color=SURFACE, height=24, corner_radius=0)
        foot.pack(fill="x", side="bottom")
        foot.pack_propagate(False)
        ctk.CTkLabel(foot, text=f"  OLLAMA  ·  LLAMA 3.2 VISION  ·  LOCAL  ·  v{VERSION}",
                     font=MONO_S, text_color=DIM).pack(side="left")

    def _log_panel(self, parent, title: str, txt_color: str,
                   side: str) -> ctk.CTkTextbox:
        frame = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=0,
                             border_width=1, border_color=BORDER)
        frame.pack(side=side, fill="both", expand=True,
                   padx=(0,4) if side=="left" else (4,0))

        hdr = ctk.CTkFrame(frame, fg_color=SURFACE2, height=26, corner_radius=0)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text=f"  {title}", font=MONO_S,
                     text_color=DIM2, anchor="w").pack(side="left")

        box = ctk.CTkTextbox(
            frame, font=MONO,
            fg_color="#000000", text_color=txt_color,
            scrollbar_button_color=BORDER,
            wrap="word", state="disabled", corner_radius=0, border_width=0,
        )
        box.pack(fill="both", expand=True)
        return box

    def _mk_cfg(self, parent, label: str):
        ctk.CTkLabel(parent, text=f" {label} ", font=MONO_S,
                     text_color=DIM2).pack(side="left")

    # ── Monitor ───────────────────────────────────────────────────────────────

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

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def _toggle(self):
        if self._running:
            # Pull the cord immediately
            self._running = False
            self._stop_evt.set()
            self._btn.configure(state="disabled")
            self._set_status("STOP…", ORANGE)
            self._stop_anim()
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
        self.after(0, self._idle_ui)

    def _check_update(self):
        """Runs in background thread at startup."""
        release = fetch_latest_release()
        if release is None:
            return
        latest  = _parse_version(release["version"])
        current = _parse_version(VERSION)
        if latest > current:
            self.after(0, lambda: UpdateDialog(self, release))

    def _idle_ui(self):
        self._btn.configure(text="[ START ]", border_color=ACCENT,
                            text_color=ACCENT, state="normal")
        self._set_status("IDLE", DIM)
        self._stop_anim()

    # ── Animation ─────────────────────────────────────────────────────────────

    def _start_anim(self, state: str, total: float = 0):
        self._anim_state = state
        self._anim_start = time.monotonic()
        self._anim_total = total
        if not self._anim_on:
            self._anim_on = True
            self.after(100, self._tick)

    def _stop_anim(self):
        self._anim_on    = False
        self._anim_state = "idle"
        self.after(0, self._anim_lbl.configure, {"text": "  ——"})

    def _tick(self):
        if not self._anim_on:
            return
        self._spin_i = (self._spin_i + 1) % len(SPINNER)
        s = SPINNER[self._spin_i]

        if self._anim_state == "waiting":
            elapsed = time.monotonic() - self._anim_start
            pct     = min(elapsed / self._anim_total, 1.0) if self._anim_total else 1.0
            rem     = max(0, self._anim_total - elapsed)
            W       = 22
            filled  = int(pct * W)
            bar     = "█" * filled + "░" * (W - filled)
            txt     = f"  {s}  WAITING  [{bar}]  {rem:.0f}s"
        elif self._anim_state == "model":
            txt = f"  {s}  MODEL THINKING..."
        elif self._anim_state == "detect":
            txt = f"  {s}  SCANNING SCREEN FOR BUTTONS..."
        elif self._anim_state == "click":
            txt = f"  {s}  MOVING MOUSE..."
        else:
            txt = "  ——"

        self._anim_lbl.configure(text=txt)
        self.after(100, self._tick)

    # ── Worker ────────────────────────────────────────────────────────────────

    def _worker(self):
        while not self._stop_evt.is_set():
            self._cycle += 1
            self.after(0, self._cycle_lbl.configure,
                       {"text": f"CYC: {self._cycle:03d}"})

            self._syslog(f"━━━ CYCLE {self._cycle:03d} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

            # ── Reading delay with live progress bar ──────────────────────────
            delay = random.uniform(self._dmin.get(), self._dmax.get())
            self._syslog(f"WAIT  {delay:.1f}s")
            self._start_anim("waiting", delay)
            end = time.monotonic() + delay
            while time.monotonic() < end:
                if self._stop_evt.is_set():
                    self._stop_anim()
                    return
                time.sleep(0.1)
            self._stop_anim()

            if self._stop_evt.is_set():
                return

            # ── Capture ───────────────────────────────────────────────────────
            mon = self._mon_idx()
            self._syslog(f"CAPTURE  monitor {mon}")
            try:
                img, mon_info = capture_screen(mon)
            except Exception as e:
                self._syslog(f"ERR  capture: {e}", error=True)
                continue

            dpi = get_dpi_scale(mon)
            self._syslog(f"IMG  {img.size[0]}×{img.size[1]}  DPI: {dpi:.2f}×")

            # ── Model: stream tokens live to AI log ───────────────────────────
            self._syslog("MODEL  querying llama3.2-vision…")
            self._ailog("\n── NEW RESPONSE ─────────────────────────────────\n")
            self._start_anim("model")

            try:
                answer_num, rationale = classify_answer_stream(
                    img,
                    self._stop_evt,
                    on_token=self._stream_token,   # each token → AI log live
                )
            except InterruptedError:
                self._stop_anim()
                self._syslog("STOPPED by user")
                return
            except Exception as e:
                self._stop_anim()
                self._syslog(f"ERR  model: {e}", error=True)
                self._ailog(f"\n[MODEL ERROR] {e}\n", error=True)
                continue

            self._stop_anim()

            if self._stop_evt.is_set():
                return

            self._syslog(f"ANSWER  #{answer_num}  —  {rationale}")

            # ── Detect buttons ────────────────────────────────────────────────
            self._syslog("DETECT  scanning for buttons…")
            self._start_anim("detect")
            try:
                phys_xy = detect_buttons(img)
                if phys_xy:
                    n = len(phys_xy)
                    answer_num = max(1, min(n, answer_num))
                    px, py = phys_xy[answer_num - 1]
                    self._syslog(f"DETECT  found {n} button(s), targeting #{answer_num}")
                else:
                    px, py = None, None
            except Exception as e:
                self._syslog(f"ERR  detect: {e}", error=True)
                self._stop_anim()
                continue
            self._stop_anim()

            if px is None:
                self._syslog("WARN  no buttons detected — skipping", error=True)
                continue

            # physical → absolute → logical
            abs_px = px + mon_info["left"]
            abs_py = py + mon_info["top"]
            log_x  = int(round(abs_px / dpi))
            log_y  = int(round(abs_py / dpi))

            self._syslog(f"PHYS ({abs_px},{abs_py})  →  CLICK ({log_x},{log_y})")

            if self._stop_evt.is_set():
                return

            # ── Click ─────────────────────────────────────────────────────────
            self._syslog("CLICK")
            self._start_anim("click")
            try:
                human_click(log_x, log_y, self._stop_evt, speed=self._speed.get())
            except Exception as e:
                self._syslog(f"ERR  click: {e}", error=True)
                self._stop_anim()
                continue
            self._stop_anim()

            if self._stop_evt.is_set():
                return

            self._syslog("OK ✓")

    # ── Logging helpers ───────────────────────────────────────────────────────

    def _syslog(self, msg: str, error: bool = False):
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.after(0, self._append, self._sys_log, line,
                   "#ff4444" if error else None)

    def _ailog(self, msg: str, error: bool = False):
        self.after(0, self._append, self._ai_log, msg,
                   "#ff4444" if error else None)

    def _stream_token(self, token: str):
        """Called from worker thread for each streaming token."""
        self.after(0, self._append, self._ai_log, token, None)

    def _append(self, box: ctk.CTkTextbox, text: str, color: str | None):
        box.configure(state="normal")
        if color:
            tag = f"col_{color.replace('#','')}"
            box._textbox.tag_configure(tag, foreground=color)
            box._textbox.insert("end", text, tag)
        else:
            box.insert("end", text)
        box.see("end")
        box.configure(state="disabled")

    def _set_status(self, text: str, color: str):
        self.after(0, self._led.configure,        {"text_color": color})
        self.after(0, self._status_lbl.configure, {"text": text, "text_color": color})


if __name__ == "__main__":
    App().mainloop()
