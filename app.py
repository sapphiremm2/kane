"""
app.py  —  AI Answer Bot
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
from PIL import Image, ImageTk

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ── Colours ────────────────────────────────────────────────────────────────
BG       = "#0e1117"
PANEL    = "#161b22"
BORDER   = "#30363d"
ACCENT   = "#238636"
ACCENT_H = "#2ea043"
STOP_C   = "#b91c1c"
STOP_H   = "#dc2626"
TXT      = "#e6edf3"
TXT_DIM  = "#8b949e"
LOG_BG   = "#0d1117"
LOG_TXT  = "#3fb950"

OLLAMA_MODEL  = "llama3.2-vision"
MODEL_MAX_W   = 960   # downscale to this width before sending — keeps tokens low


# ── Screen capture ──────────────────────────────────────────────────────────

def list_monitors() -> list[dict]:
    """Return mss monitor dicts (index 0 = all screens, skip it)."""
    with mss.mss() as sct:
        return sct.monitors[1:]   # [1] = primary, [2]+ = additional


def capture_screen(monitor_idx: int) -> tuple[Image.Image, dict]:
    """Capture monitor at 1-based mss index. Returns (image, monitor_dict)."""
    with mss.mss() as sct:
        mon = sct.monitors[monitor_idx]
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    return img, mon


def get_dpi_scale(monitor_mss_idx: int = 1) -> float:
    """
    Physical pixels (mss) ÷ logical pixels (pydirectinput / Win32).
    e.g. 150% Windows scaling → 1.5, 200% → 2.0, 100% → 1.0.
    We compare GetSystemMetrics (logical) to the mss monitor width (physical).
    """
    logical_w = ctypes.windll.user32.GetSystemMetrics(0)   # SM_CXSCREEN
    with mss.mss() as sct:
        physical_w = sct.monitors[monitor_mss_idx]["width"]
    return (physical_w / logical_w) if logical_w > 0 else 1.0


def image_to_base64(img: Image.Image) -> str:
    buf = io.BytesIO()
    # JPEG at q=85 is ~10× smaller than PNG — dramatically faster inference
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def downscale(img: Image.Image, max_w: int = MODEL_MAX_W) -> tuple[Image.Image, float]:
    """Shrink image so the model gets fewer tokens. Returns (img, scale)."""
    w, h = img.size
    if w <= max_w:
        return img, 1.0
    scale = max_w / w
    return img.resize((max_w, int(h * scale)), Image.LANCZOS), scale


# ── Vision model ────────────────────────────────────────────────────────────

PROMPT = """\
You are looking at a screenshot of an e-learning quiz.

Layout description:
- There may be a reading passage or image on the LEFT side of the screen — IGNORE that area entirely.
- On the RIGHT side there is a question followed by FOUR answer choices.
- Each answer choice is a dark rounded-rectangle button with a small circular radio indicator on its left edge and short text inside it (e.g. "a prototype", "a slogan").

Your task:
1. Read the question text.
2. Decide which of the four answer buttons is correct.
3. Return the pixel coordinates of the CENTER of that button.

Rules:
- Only target the clickable answer buttons — NOT the passage text, NOT images, NOT the question text itself.
- x and y must be real pixel positions inside the button as it appears in this image.
- Do NOT copy or invent numbers. Look at the image and measure carefully.

Respond with ONLY this JSON (no markdown fences, no extra text):
{"rationale": "<brief reason>", "coordinates": {"x": <integer>, "y": <integer>}}"""


def ask_vision_model(img: Image.Image) -> tuple[str, int, int]:
    """
    Downscales the image, queries the model, then scales coordinates back up.
    Returns (rationale, x, y) relative to the original full-res image.
    """
    small, scale = downscale(img)

    response = ollama.chat(
        model=OLLAMA_MODEL,
        messages=[{"role": "user", "content": PROMPT, "images": [image_to_base64(small)]}],
    )
    raw = response["message"]["content"].strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"```$",          "", raw, flags=re.MULTILINE).strip()

    try:
        data      = json.loads(raw)
        rationale = str(data.get("rationale", "—"))
        coords    = data["coordinates"]
        rx, ry    = int(coords["x"]), int(coords["y"])
    except (json.JSONDecodeError, KeyError):
        rat = re.search(r'"rationale"\s*:\s*"([^"]+)"', raw)
        xm  = re.search(r'"x"\s*:\s*(\d+)', raw)
        ym  = re.search(r'"y"\s*:\s*(\d+)', raw)
        if xm and ym:
            rationale = rat.group(1) if rat else "—"
            rx, ry    = int(xm.group(1)), int(ym.group(1))
        else:
            raise ValueError(f"Could not parse model output:\n{raw}")

    # Scale back to original image resolution
    if scale != 1.0:
        rx = int(round(rx / scale))
        ry = int(round(ry / scale))

    return rationale, rx, ry


# ── Mouse movement ──────────────────────────────────────────────────────────

def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return u**3*p0 + 3*u**2*t*p1 + 3*u*t**2*p2 + t**3*p3


def move_mouse_curved(tx: int, ty: int, steps: int = 60, base_delay: float = 0.008):
    class _PT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    pt = _PT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    sx, sy = pt.x, pt.y

    dx, dy = tx - sx, ty - sy
    dist   = math.hypot(dx, dy) or 1
    ps     = random.uniform(0.2, 0.5) * dist * random.choice([-1, 1])
    mx, my = (sx + tx) / 2, (sy + ty) / 2

    cp1x = sx + dx*random.uniform(0.15,0.35) + (-dy/dist)*ps*random.uniform(0.5,1.0)
    cp1y = sy + dy*random.uniform(0.15,0.35) + ( dx/dist)*ps*random.uniform(0.5,1.0)
    cp2x = mx + dx*random.uniform(0.10,0.25) + (-dy/dist)*ps*random.uniform(0.3,0.7)
    cp2y = my + dy*random.uniform(0.10,0.25) + ( dx/dist)*ps*random.uniform(0.3,0.7)

    for i in range(1, steps + 1):
        t  = i / steps
        te = t*t*(3 - 2*t)
        px = int(round(_bezier(sx, cp1x, cp2x, tx, te) + random.randint(-2, 2)))
        py = int(round(_bezier(sy, cp1y, cp2y, ty, te) + random.randint(-2, 2)))
        pydirectinput.moveTo(px, py)
        time.sleep(base_delay / (1 + 1.5*math.sin(math.pi*t)))

    pydirectinput.moveTo(tx, ty)


def human_click(x: int, y: int, speed: float = 0.008):
    move_mouse_curved(x, y, base_delay=speed)
    time.sleep(random.uniform(0.05, 0.18))
    pydirectinput.click(x, y)


# ── GUI ─────────────────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("AI Answer Bot")
        self.geometry("900x640")
        self.minsize(860, 580)
        self.configure(fg_color=BG)

        self._running  = False
        self._stop_evt = threading.Event()
        self._thread   = None
        self._cycle    = 0
        self._monitors = list_monitors()   # list of mss dicts

        self._build_ui()
        self._refresh_monitors()

    # ── Build ───────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Top bar ──────────────────────────────────────────────────────
        topbar = ctk.CTkFrame(self, fg_color=PANEL, height=52, corner_radius=0)
        topbar.pack(fill="x", side="top")
        topbar.pack_propagate(False)

        ctk.CTkLabel(topbar, text="  AI Answer Bot",
                     font=("Segoe UI", 15, "bold"), text_color=TXT,
                     image=None).pack(side="left", padx=16, pady=0)

        # status pill
        self._status_frame = ctk.CTkFrame(topbar, fg_color="#1c2128", corner_radius=20)
        self._status_frame.pack(side="left", padx=8)
        self._led = ctk.CTkLabel(self._status_frame, text="●",
                                  font=("Segoe UI", 13), text_color="#444",
                                  width=18)
        self._led.pack(side="left", padx=(10,2), pady=6)
        self._status_lbl = ctk.CTkLabel(self._status_frame, text="Idle",
                                         font=("Segoe UI", 11), text_color=TXT_DIM,
                                         width=64)
        self._status_lbl.pack(side="left", padx=(0,10), pady=6)

        self._toggle_btn = ctk.CTkButton(
            topbar, text="▶  Start", width=110, height=34,
            font=("Segoe UI", 12, "bold"),
            fg_color=ACCENT, hover_color=ACCENT_H, corner_radius=8,
            command=self._toggle,
        )
        self._toggle_btn.pack(side="right", padx=16)

        # cycle counter
        self._cycle_lbl = ctk.CTkLabel(topbar, text="Cycle: 0",
                                        font=("Segoe UI", 11), text_color=TXT_DIM)
        self._cycle_lbl.pack(side="right", padx=4)

        # ── Body ─────────────────────────────────────────────────────────
        body = ctk.CTkFrame(self, fg_color=BG)
        body.pack(fill="both", expand=True, padx=12, pady=(8, 0))

        # Left: log
        log_frame = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=10)
        log_frame.pack(side="left", fill="both", expand=True, padx=(0, 6))

        ctk.CTkLabel(log_frame, text="Reasoning Log",
                     font=("Segoe UI", 11, "bold"), text_color=TXT_DIM,
                     anchor="w").pack(fill="x", padx=14, pady=(10, 4))

        self._log_box = ctk.CTkTextbox(
            log_frame,
            font=("Consolas", 11),
            fg_color=LOG_BG, text_color=LOG_TXT,
            scrollbar_button_color=BORDER,
            wrap="word", state="disabled", corner_radius=8,
        )
        self._log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # Right: preview + settings
        right = ctk.CTkFrame(body, fg_color=BG, width=270)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        # Preview card
        prev_card = ctk.CTkFrame(right, fg_color=PANEL, corner_radius=10)
        prev_card.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(prev_card, text="Last Screenshot",
                     font=("Segoe UI", 11, "bold"), text_color=TXT_DIM,
                     anchor="w").pack(fill="x", padx=14, pady=(10, 6))

        self._preview_lbl = ctk.CTkLabel(
            prev_card, text="No capture yet",
            fg_color="#0d1117", width=246, height=155,
            corner_radius=8, text_color=TXT_DIM,
            font=("Segoe UI", 10),
        )
        self._preview_lbl.pack(padx=12, pady=(0, 8))

        self._target_lbl = ctk.CTkLabel(prev_card, text="Target: —",
                                         font=("Consolas", 11), text_color="#7c8cff")
        self._target_lbl.pack(pady=(0, 10))

        # Settings card
        cfg_card = ctk.CTkFrame(right, fg_color=PANEL, corner_radius=10)
        cfg_card.pack(fill="x")

        ctk.CTkLabel(cfg_card, text="Settings",
                     font=("Segoe UI", 11, "bold"), text_color=TXT_DIM,
                     anchor="w").pack(fill="x", padx=14, pady=(10, 8))

        # Monitor selector
        self._build_setting_row(cfg_card, "Display")
        self._monitor_var = ctk.StringVar(value="Monitor 1")
        self._monitor_menu = ctk.CTkOptionMenu(
            cfg_card, variable=self._monitor_var,
            values=["Monitor 1"],
            fg_color="#1c2128", button_color=BORDER,
            button_hover_color="#444c56", text_color=TXT,
            font=("Segoe UI", 11), corner_radius=6,
            command=lambda _: None,
        )
        self._monitor_menu.pack(fill="x", padx=12, pady=(0, 10))

        # Delay slider
        self._build_setting_row(cfg_card, "Reading delay (s)")
        self._delay_min = ctk.IntVar(value=15)
        self._delay_max = ctk.IntVar(value=45)

        delay_row = ctk.CTkFrame(cfg_card, fg_color="transparent")
        delay_row.pack(fill="x", padx=12, pady=(0, 8))

        ctk.CTkLabel(delay_row, text="Min", font=("Segoe UI", 10),
                     text_color=TXT_DIM, width=26).pack(side="left")
        ctk.CTkSlider(delay_row, from_=1, to=120, number_of_steps=119,
                       variable=self._delay_min, width=70,
                       button_color=ACCENT, progress_color=ACCENT
                       ).pack(side="left", padx=4)
        self._dmin_lbl = ctk.CTkLabel(delay_row, text="15s", width=28,
                                       font=("Segoe UI", 10), text_color=TXT)
        self._dmin_lbl.pack(side="left")

        ctk.CTkLabel(delay_row, text="Max", font=("Segoe UI", 10),
                     text_color=TXT_DIM, width=30).pack(side="left", padx=(6,0))
        ctk.CTkSlider(delay_row, from_=1, to=120, number_of_steps=119,
                       variable=self._delay_max, width=70,
                       button_color=ACCENT, progress_color=ACCENT
                       ).pack(side="left", padx=4)
        self._dmax_lbl = ctk.CTkLabel(delay_row, text="45s", width=28,
                                       font=("Segoe UI", 10), text_color=TXT)
        self._dmax_lbl.pack(side="left")

        self._delay_min.trace_add("write", lambda *_: self._dmin_lbl.configure(
            text=f"{self._delay_min.get()}s"))
        self._delay_max.trace_add("write", lambda *_: self._dmax_lbl.configure(
            text=f"{self._delay_max.get()}s"))

        # Mouse speed slider
        self._build_setting_row(cfg_card, "Mouse speed")
        spd_row = ctk.CTkFrame(cfg_card, fg_color="transparent")
        spd_row.pack(fill="x", padx=12, pady=(0, 14))

        self._speed_var = ctk.DoubleVar(value=0.008)
        ctk.CTkSlider(spd_row, from_=0.002, to=0.030, number_of_steps=140,
                       variable=self._speed_var,
                       button_color=ACCENT, progress_color=ACCENT
                       ).pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._spd_lbl = ctk.CTkLabel(spd_row, text="0.008", width=40,
                                      font=("Consolas", 10), text_color=TXT)
        self._spd_lbl.pack(side="left")
        self._speed_var.trace_add("write", lambda *_: self._spd_lbl.configure(
            text=f"{self._speed_var.get():.3f}"))

        # ── Bottom bar ────────────────────────────────────────────────────
        btm = ctk.CTkFrame(self, fg_color=PANEL, height=28, corner_radius=0)
        btm.pack(fill="x", side="bottom")
        btm.pack_propagate(False)
        ctk.CTkLabel(btm, text="Ollama  ·  llama3.2-vision  ·  local",
                     font=("Segoe UI", 9), text_color=TXT_DIM
                     ).pack(side="left", padx=12)

    def _build_setting_row(self, parent, label: str):
        ctk.CTkLabel(parent, text=label,
                     font=("Segoe UI", 10), text_color=TXT_DIM,
                     anchor="w").pack(fill="x", padx=14, pady=(2, 2))

    # ── Monitor helpers ──────────────────────────────────────────────────────

    def _refresh_monitors(self):
        self._monitors = list_monitors()
        labels = [f"Monitor {i+1}  ({m['width']}×{m['height']})"
                  for i, m in enumerate(self._monitors)]
        self._monitor_menu.configure(values=labels)
        if labels:
            self._monitor_var.set(labels[0])

    def _selected_monitor_idx(self) -> int:
        """Returns 1-based mss index for the selected monitor."""
        label = self._monitor_var.get()
        try:
            n = int(label.split()[1]) - 1   # 0-based list index
            return n + 1                     # mss is 1-based
        except (IndexError, ValueError):
            return 1

    # ── Start / Stop ─────────────────────────────────────────────────────────

    def _toggle(self):
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self):
        self._running = True
        self._stop_evt.clear()
        self._set_status("Running", "#2ecc71")
        self._toggle_btn.configure(text="■  Stop",
                                    fg_color=STOP_C, hover_color=STOP_H)
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _stop(self):
        self._running = False
        self._stop_evt.set()
        self._set_status("Stopping…", "#f39c12")
        self._toggle_btn.configure(state="disabled")
        threading.Thread(target=self._await_stop, daemon=True).start()

    def _await_stop(self):
        if self._thread:
            self._thread.join()
        self.after(0, self._idle_ui)

    def _idle_ui(self):
        self._toggle_btn.configure(text="▶  Start",
                                    fg_color=ACCENT, hover_color=ACCENT_H,
                                    state="normal")
        self._set_status("Idle", "#444")

    # ── Worker thread ────────────────────────────────────────────────────────

    def _worker(self):
        while not self._stop_evt.is_set():
            self._cycle += 1
            self.after(0, self._cycle_lbl.configure,
                       {"text": f"Cycle: {self._cycle}"})

            # Interruptible reading delay
            delay = random.uniform(self._delay_min.get(), self._delay_max.get())
            self._log(f"━━━  Cycle {self._cycle}  ━━━")
            self._log(f"Waiting {delay:.1f}s before acting…")
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                if self._stop_evt.is_set():
                    return
                time.sleep(0.1)

            if self._stop_evt.is_set():
                return

            # Capture
            mon_idx = self._selected_monitor_idx()
            self._log(f"Capturing monitor {mon_idx}…")
            try:
                img, mon = capture_screen(mon_idx)
            except Exception as exc:
                self._log(f"[ERROR] Capture failed: {exc}")
                continue

            self._update_preview(img)

            # Vision
            self._log("Querying Llama 3.2 Vision…")
            try:
                rationale, rel_x, rel_y = ask_vision_model(img)
            except Exception as exc:
                self._log(f"[ERROR] Vision model: {exc}")
                continue

            # Physical pixel coords (image-relative → absolute physical)
            phys_x = rel_x + mon["left"]
            phys_y = rel_y + mon["top"]

            # Convert physical → logical pixels (Windows DPI scaling)
            # mss captures at physical resolution; pydirectinput uses logical coords.
            # e.g. 150% scaling: divide by 1.5 so mouse lands in the right place.
            dpi    = get_dpi_scale(mon_idx)
            log_x  = int(round(phys_x / dpi))
            log_y  = int(round(phys_y / dpi))

            self._log(f"Rationale: {rationale}")
            self._log(f"Physical: ({phys_x}, {phys_y})  DPI scale: {dpi:.2f}×  →  Click: ({log_x}, {log_y})")
            self.after(0, self._target_lbl.configure,
                       {"text": f"Click: ({log_x}, {log_y})  [{dpi:.2f}×]"})

            if self._stop_evt.is_set():
                return

            # Click
            self._log("Moving mouse and clicking…")
            try:
                human_click(log_x, log_y, speed=self._speed_var.get())
            except Exception as exc:
                self._log(f"[ERROR] Click: {exc}")
                continue

            self._log(f"Clicked ✓")

    # ── UI thread helpers ────────────────────────────────────────────────────

    def _log(self, msg: str):
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.after(0, self._append_log, line)

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
        thumb.thumbnail((246, 155), Image.LANCZOS)
        tk_img = ImageTk.PhotoImage(thumb)
        self.after(0, self._set_preview, tk_img)

    def _set_preview(self, tk_img):
        self._preview_lbl._tk_image = tk_img
        self._preview_lbl.configure(image=tk_img, text="")


if __name__ == "__main__":
    App().mainloop()
