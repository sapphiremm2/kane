# Kane — AI Answer Bot

Automates multiple-choice quiz questions using a local vision model (Llama 3.2 Vision via Ollama). No cloud, no API keys — everything runs on your machine.

---

## Download

Go to [Releases](../../releases) and download `Kane.exe`. Double-click to run — no Python or terminal needed.

> **Requirement:** [Ollama](https://ollama.com) must be installed and running with the vision model pulled:
> ```
> ollama pull llama3.2-vision
> ```

---

## How it works

1. Waits a randomised delay (simulates reading time)
2. Takes a screenshot of the selected monitor
3. Sends it to Llama 3.2 Vision — asks which answer (1–4) is correct
4. Uses image analysis to locate the answer buttons on screen
5. Moves the mouse in a human-like curved path and clicks

---

## Run from source

```bash
pip install -r requirements.txt
python app.py
```

## Build EXE locally

```bash
pyinstaller --noconsole --onefile --collect-all customtkinter --name Kane app.py
# Output: dist/Kane.exe
```

---

## Tech stack

- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) — GUI
- [Ollama](https://ollama.com) + [Llama 3.2 Vision](https://ollama.com/library/llama3.2-vision) — vision model
- [mss](https://python-mss.readthedocs.io) — fast screen capture
- [pydirectinput](https://github.com/learncodebygaming/pydirectinput) — DirectInput mouse clicks
- [Pillow](https://pillow.readthedocs.io) — image processing & button detection
