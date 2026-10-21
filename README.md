# KeyClack

A light background desktop app (Python + tkinter) that plays a mechanical
keyboard click sound for every keystroke. Each click is varied in pitch,
volume and timing so it never sounds like the same WAV fired on repeat.

## Adding your own click samples

Drop sound files into the `samples/` folder (WAV/FLAC/OGG/AIFF). They are
loaded automatically on startup and used as the base click; per-click pitch,
volume and timing variation is applied on top. If the folder is empty, a
synthetic click is used.

Install the optional decoder for FLAC/OGG support:

```
pip install -r requirements.txt
```

## Run

```
python main.py
```

or double-click `run.bat`.

## Controls

- **Enable/Disable** toggle
- **Volume** (0-100)
- **Pitch variation** (0-12%) spread applied per click
- **Timing jitter** (0-25 ms) random delay before each click
- **Test click** button
- **Always on top** checkbox

Clicks now overlap instead of cutting each other off (mixed via sounddevice).

Special keys (Space, Enter, Backspace, Shift/Ctrl/Alt) get distinct profiles;
toggle with "Distinct sounds for special keys".

The keyboard hook is system-wide, so clicks play for typing in any app.

## Requirements

Python 3.8+ on Windows (uses `ctypes`, `winsound`, `tkinter` — all stdlib).
