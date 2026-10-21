"""KeyClack - a light background desktop app that plays a mechanical
keyboard click for every keystroke, with per-click variation in pitch,
volume and timing so it never sounds like the same WAV fired on repeat."""

import ctypes
from ctypes import wintypes
import threading
import queue
import random
import math
import wave
import struct
import os
import sys
import tempfile
import shutil
import time
import numpy as np
import sounddevice as sd
import tkinter as tk
from tkinter import ttk

# Optional: soundfile enables FLAC/OGG/etc. support. Falls back to WAV-only.
try:
    import soundfile as sf
    HAS_SOUNDFILE = True
except Exception:
    sf = None
    HAS_SOUNDFILE = False

# ---------------------------------------------------------------- audio
SR = 44100


PROFILES = {
    "standard":  dict(f0=160, decay=220, transient=0.85, hf=0.45, dur=0.013, amp=1.00, noise=0.60),
    "space":     dict(f0=105, decay=130, transient=0.55, hf=0.20, dur=0.018, amp=1.15, noise=0.50),
    "enter":     dict(f0=125, decay=170, transient=0.75, hf=0.30, dur=0.015, amp=1.05, noise=0.55),
    "backspace": dict(f0=210, decay=320, transient=1.00, hf=0.65, dur=0.011, amp=0.95, noise=0.70),
    "modifier":  dict(f0=150, decay=280, transient=0.35, hf=0.15, dur=0.010, amp=0.70, noise=0.30),
}


def gen_click(profile="standard"):
    p = PROFILES.get(profile, PROFILES["standard"])
    dur = p["dur"] * random.uniform(0.98, 1.02)
    n = int(SR * dur)
    f0 = p["f0"] * random.uniform(0.97, 1.03)
    decay = p["decay"] * random.uniform(0.95, 1.05)
    out = []
    for i in range(n):
        t = i / SR
        attack = t / 0.0004 if t < 0.0004 else 1.0
        if attack > 1.0:
            attack = 1.0
        env = math.exp(-t * decay) * attack
        s = math.sin(2 * math.pi * f0 * t) * env * p["amp"] * 0.5
        if 0.001 < t < 0.004:
            s += (math.sin(2 * math.pi * (f0 * 1.45) * t)
                  * math.exp(-(t - 0.001) * 900) * 0.18 * p["amp"])
        if t < 0.0025:
            s += ((random.random() * 2 - 1)
                  * (1 - t / 0.0025) * p["transient"] * p["noise"])
        if t < 0.001:
            s += (random.random() * 2 - 1) * p["hf"] * 0.45
        v = int(max(-32767, min(32767, s * 32767)))
        out.append(v)
    return out


def gen_base():
    return gen_click("standard")


def resample_amp(base, pitch, amp):
    L = len(base)
    new_len = max(4, int(L / pitch))
    out = []
    for i in range(new_len):
        pos = i * pitch
        i0 = int(pos)
        frac = pos - i0
        if i0 + 1 < L:
            val = base[i0] * (1 - frac) + base[i0 + 1] * frac
        else:
            val = base[min(i0, L - 1)]
        val *= amp
        out.append(int(max(-32767, min(32767, val))))
    return out


def write_wav(path, samples):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(struct.pack("<%dh" % len(samples), *samples))


# ---------------------------------------------------------------- sample loading
def resource_path(relative):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLES_DIR = resource_path("samples")

AUDIO_EXTS = {".wav", ".flac", ".ogg", ".aiff", ".aif"}
if not HAS_SOUNDFILE:
    AUDIO_EXTS = {".wav"}


def _resample_rate(data, src_sr, dst_sr):
    if src_sr == dst_sr:
        return data
    ratio = dst_sr / src_sr
    new_len = int(len(data) * ratio)
    out = []
    for i in range(new_len):
        pos = i / ratio
        i0 = int(pos)
        frac = pos - i0
        if i0 + 1 < len(data):
            val = data[i0] * (1 - frac) + data[i0 + 1] * frac
        else:
            val = data[min(i0, len(data) - 1)]
        out.append(val)
    return out


def read_audio_file(path):
    if HAS_SOUNDFILE:
        data, sr = sf.read(path, dtype="float64", always_2d=False)
        if getattr(data, "ndim", 1) > 1:
            data = data.mean(axis=1)
        data = _resample_rate(list(data), sr, SR)
        return [int(max(-32767, min(32767, x * 32767))) for x in data]

    if not path.lower().endswith(".wav"):
        return None
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        n = w.getnframes()
        raw = w.readframes(n)
    if sw == 1:
        vals = [b - 128 for b in raw]
    elif sw == 2:
        vals = list(struct.unpack("<%dh" % (len(raw) // 2), raw))
    elif sw == 4:
        vals = list(struct.unpack("<%di" % (len(raw) // 4), raw))
        vals = [v >> 16 for v in vals]
    else:
        return None
    if ch > 1:
        mono = []
        for i in range(0, len(vals), ch):
            mono.append(int(sum(vals[i:i + ch]) / ch))
        vals = mono
    vals = _resample_rate(vals, sr, SR)
    return [int(max(-32767, min(32767, v))) for v in vals]


all_sample_files = []
enabled_sample_files = set()
sample_bases = []


def scan_samples():
    global all_sample_files
    files = []
    if os.path.isdir(SAMPLES_DIR):
        for fn in sorted(os.listdir(SAMPLES_DIR)):
            p = os.path.join(SAMPLES_DIR, fn)
            if os.path.isfile(p) and os.path.splitext(fn)[1].lower() in AUDIO_EXTS:
                files.append(fn)
    all_sample_files = files
    # default: enable all if nothing chosen yet
    if not enabled_sample_files and files:
        enabled_sample_files.update(files)
    # prune
    enabled_sample_files.intersection_update(files)
    return files


def classify_key(vk):
    if vk == VK_SPACE:
        return "space"
    if vk == VK_RETURN:
        return "enter"
    if vk == VK_BACK:
        return "backspace"
    if vk in MODIFIER_VKS:
        return "modifier"
    return "standard"


TRANSFORMS = {
    "space":     dict(pitch=0.82, amp=1.12, transient=0.9,  decay_env=False, lowpass=False),
    "enter":     dict(pitch=0.90, amp=1.05, transient=1.0,  decay_env=False, lowpass=False),
    "backspace": dict(pitch=1.18, amp=0.95, transient=1.35, decay_env=True,  env_rate=180, lowpass=False),
    "modifier":  dict(pitch=0.95, amp=0.62, transient=0.5,  decay_env=True,  env_rate=120, lowpass=True),
}


def transform_click(base, cls):
    t = TRANSFORMS.get(cls)
    if not t:
        return list(base)
    # pitch shift via resampling
    pitch = t["pitch"]
    new_len = max(4, int(len(base) / pitch))
    arr = []
    for i in range(new_len):
        pos = i * pitch
        i0 = int(pos)
        frac = pos - i0
        if i0 + 1 < len(base):
            val = base[i0] * (1 - frac) + base[i0 + 1] * frac
        else:
            val = base[min(i0, len(base) - 1)]
        arr.append(val)
    # amplitude
    arr = [s * t["amp"] for s in arr]
    # transient boost (first 2 ms)
    trans_n = min(len(arr), int(SR * 0.002))
    for i in range(trans_n):
        arr[i] = arr[i] * t["transient"]
    # decay envelope (shorter tail)
    if t.get("decay_env"):
        rate = t.get("env_rate", 150)
        for i in range(len(arr)):
            arr[i] = arr[i] * math.exp(-(i / SR) * rate)
    # lowpass for muted
    if t.get("lowpass"):
        out = [arr[0]] if arr else []
        for i in range(1, len(arr) - 1):
            out.append((arr[i - 1] + arr[i] + arr[i + 1]) / 3.0)
        if len(arr) > 1:
            out.append(arr[-1])
        arr = out
    arr = [int(max(-32767, min(32767, v))) for v in arr]
    arr = trim_click(arr)
    return arr


def trim_click(samples):
    if not samples:
        return samples
    peak = max(abs(s) for s in samples) or 1
    thr = max(1, int(peak * 0.05))
    start = 0
    while start < len(samples) and abs(samples[start]) < thr:
        start += 1
    end = len(samples)
    while end > start and abs(samples[end - 1]) < thr:
        end -= 1
    if end <= start:
        seg = samples[:]
    else:
        seg = samples[start:end]
    cap = int(SR * (max_click_ms[0] / 1000.0))
    if len(seg) > cap:
        seg = seg[:cap]
    return seg if len(seg) > 10 else samples[:max(10, min(len(samples), cap))]


def load_sample_bases():
    bases = []
    for fn in sorted(enabled_sample_files):
        path = os.path.join(SAMPLES_DIR, fn)
        try:
            arr = read_audio_file(path)
            if arr and len(arr) > 10:
                bases.append((fn, trim_click(arr)))
        except Exception as e:
            print("skip sample", fn, e)
    return bases


def sample_duration(fn):
    path = os.path.join(SAMPLES_DIR, fn)
    try:
        if HAS_SOUNDFILE:
            info = sf.info(path)
            return info.frames / info.samplerate
        else:
            with wave.open(path, "rb") as w:
                return w.getnframes() / w.getframerate()
    except Exception:
        return 0.0


# ---------------------------------------------------------------- mixer
def audio_callback(outdata, frames, time_info, status):
    out = np.zeros(frames, dtype=np.float32)
    with active_lock:
        survivors = []
        for snd in active_sounds:
            data = snd["data"]
            pos = snd["pos"]
            remaining = len(data) - pos
            if remaining <= 0:
                continue
            n = min(frames, remaining)
            out[:n] += data[pos:pos + n]
            snd["pos"] = pos + n
            if snd["pos"] < len(data):
                survivors.append(snd)
        active_sounds[:] = survivors
    # soft clip / normalize to avoid harsh clipping
    np.clip(out, -1.0, 1.0, out=out)
    outdata[:, 0] = out


def start_audio_stream():
    try:
        stream = sd.OutputStream(samplerate=SR, channels=1, dtype="float32",
                                 callback=audio_callback, blocksize=64,
                                 latency="low")
        stream.start()
        audio_stream[0] = stream
        return True
    except Exception as e:
        print("audio stream error:", e)
        return False


def stop_audio_stream():
    s = audio_stream[0]
    if s is not None:
        try:
            s.stop()
            s.close()
        except Exception:
            pass
        audio_stream[0] = None


# ---------------------------------------------------------------- state
enabled_flag = threading.Event()
enabled_flag.set()

stop_event = threading.Event()
key_queue = queue.Queue(maxsize=256)

pools_ref = [{}]         # dict: sound class -> list of np.ndarray variants
timing_jitter = [0.003]
max_click_ms = [120]
distinct_sounds = [True]
pools_lock = threading.Lock()
rebuild_lock = threading.Lock()

# ---- mixer state ----
active_sounds = []       # list of dicts: {'data': ndarray, 'pos': int}
active_lock = threading.Lock()
audio_stream = [None]
pressed_vks = set()
last_play = [0.0]
play_count = [0]

# ---------------------------------------------------------------- global hook
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_KEYUP = 0x0101
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

# Virtual-key codes for special keys
VK_BACK = 0x08
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_SPACE = 0x20
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5

MODIFIER_VKS = {VK_SHIFT, VK_LSHIFT, VK_RSHIFT, VK_CONTROL, VK_LCONTROL,
                VK_RCONTROL, VK_MENU, VK_LMENU, VK_RMENU}

LLKHF_INJECTED = 0x00000010
LLKHF_LOWER_IL_INJECTED = 0x00000002

SOUND_CLASSES = ("standard", "space", "enter", "backspace", "modifier")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_int
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

hook_id = [None]
callback_ref = [None]
hook_tid = [None]


def low_level_handler(nCode, wParam, lParam):
    if nCode >= 0:
        if wParam in (WM_KEYUP, WM_SYSKEYUP):
            try:
                ks = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                pressed_vks.discard(ks.vkCode)
            except Exception:
                pass
        elif wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            try:
                ks = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            except Exception:
                ks = None
            if ks is not None:
                flags = ks.flags
                if not (flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED)):
                    vk = ks.vkCode
                    # suppress auto-repeat while key is held
                    if vk in pressed_vks:
                        pass
                    else:
                        pressed_vks.add(vk)
                        if enabled_flag.is_set():
                            now = time.perf_counter()
                            if now - last_play[0] >= 0.015:
                                last_play[0] = now
                                if distinct_sounds[0]:
                                    cls = classify_key(vk)
                                else:
                                    cls = "standard"
                                with pools_lock:
                                    pool = pools_ref[0].get(cls) or pools_ref[0].get("standard", [])
                                if pool:
                                    choice = random.choice(pool)
                                    jitter = random.uniform(0.0, timing_jitter[0])
                                    pad = int(SR * jitter)
                                    if pad > 0:
                                        data = np.concatenate([np.zeros(pad, dtype=np.float32), choice])
                                    else:
                                        data = choice
                                    with active_lock:
                                        active_sounds.append({"data": data, "pos": 0})
                                    play_count[0] += 1
                    vk = None
    return user32.CallNextHookEx(hook_id[0], nCode, wParam, lParam)


def hook_thread():
    hook_tid[0] = kernel32.GetCurrentThreadId()
    callback_ref[0] = HOOKPROC(low_level_handler)
    hook_id[0] = user32.SetWindowsHookExW(WH_KEYBOARD_LL, callback_ref[0], None, 0)
    if not hook_id[0]:
        return
    msg = wintypes.MSG()
    while not stop_event.is_set():
        ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if ret == 0 or ret == -1:
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    if hook_id[0]:
        user32.UnhookWindowsHookEx(hook_id[0])
        hook_id[0] = None


# ---------------------------------------------------------------- pool build
def rebuild_all(volume, pitch_spread):
    with rebuild_lock:
        sample_bases = load_sample_bases()
        std_bases = [b[1] for b in sample_bases] if sample_bases else [gen_click("standard")]
        std_bases = [trim_click(b) for b in std_bases]
        if not std_bases:
            std_bases = [trim_click(gen_click("standard"))]
        new_pools = {}
        for cls in SOUND_CLASSES:
            if cls == "standard":
                bases = std_bases
            else:
                bases = [transform_click(b, cls) for b in std_bases]
                bases = [b for b in bases if len(b) > 10]
                if not bases:
                    bases = [transform_click(gen_click("standard"), cls)]
            pool = []
            for _ in range(40):
                base = random.choice(bases)
                p = 1.0 + random.uniform(-pitch_spread, pitch_spread)
                v = volume * random.uniform(0.82, 1.0)
                samples = resample_amp(base, p, v)
                arr = np.asarray(samples, dtype=np.float32) / 32768.0
                arr = arr * 0.9
                pool.append(arr)
            new_pools[cls] = pool
        with pools_lock:
            pools_ref[0] = new_pools


# ---------------------------------------------------------------- UI
# ---- palette (monotone dark) ----
BG = "#0b0d12"
SURFACE = "#151821"
SURFACE2 = "#1d2130"
BORDER = "#262b3a"
FG = "#e6e8ee"
DIM = "#7d8394"
ACCENT = "#6366f1"
ACCENT_HOVER = "#7c3aed"
SUCCESS = "#22c55e"
DANGER = "#ef4444"


class App:
    def __init__(self, root):
        self.root = root
        root.title("KeyClack")
        root.geometry("480x780")
        root.resizable(False, False)
        root.configure(bg=BG)

        # ---- theme ----
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(".", background=BG, foreground=FG, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=SURFACE, relief="flat")
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Dim.TLabel", background=BG, foreground=DIM, font=("Segoe UI", 9))
        style.configure("Card.TLabel", background=SURFACE, foreground=FG)
        style.configure("CardDim.TLabel", background=SURFACE, foreground=DIM, font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI", 20, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=DIM, font=("Segoe UI", 10))
        style.configure("StatusOn.TLabel", background=BG, foreground=SUCCESS, font=("Segoe UI", 11, "bold"))
        style.configure("StatusOff.TLabel", background=BG, foreground=DANGER, font=("Segoe UI", 11, "bold"))
        style.configure("TLabelframe", background=SURFACE, foreground=ACCENT, bordercolor=BORDER, relief="flat")
        style.configure("TLabelframe.Label", background=SURFACE, foreground=ACCENT, font=("Segoe UI", 10, "bold"))
        style.configure("TButton", background=ACCENT, foreground="#ffffff", borderwidth=0,
                       focusthickness=0, padding=(12, 6), font=("Segoe UI", 10, "bold"))
        style.map("TButton", background=[("active", ACCENT_HOVER), ("pressed", ACCENT_HOVER)])
        style.configure("Ghost.TButton", background=SURFACE2, foreground=FG, borderwidth=0,
                       padding=(10, 5), font=("Segoe UI", 9))
        style.map("Ghost.TButton", background=[("active", BORDER)])
        style.configure("TCheckbutton", background=BG, foreground=FG, font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("TScale", background=BG, troughcolor=SURFACE2, sliderthickness=16, sliderrelief="flat")
        style.map("TScale", background=[("active", ACCENT)])
        style.configure("TScrollbar", background=SURFACE2, troughcolor=BG, borderwidth=0, arrowsize=12)

        outer = tk.Frame(root, bg=BG)
        outer.pack(fill="both", expand=True, padx=18, pady=16)

        # ---- header ----
        hdr = tk.Frame(outer, bg=BG)
        hdr.pack(fill="x", pady=(0, 12))
        tk.Label(hdr, text="⌨  KeyClack", bg=BG, fg=FG, font=("Segoe UI", 20, "bold")).pack(anchor="w")
        tk.Label(hdr, text="Mechanical keyboard clicks for every keystroke", bg=BG, fg=DIM,
                 font=("Segoe UI", 10)).pack(anchor="w", pady=(2, 0))

        # status row
        status_row = tk.Frame(outer, bg=BG)
        status_row.pack(fill="x", pady=(0, 10))
        self.status_dot = tk.Label(status_row, text="●", bg=BG, fg=SUCCESS, font=("Segoe UI", 12))
        self.status_dot.pack(side="left")
        self.status = tk.Label(status_row, text="  Active", bg=BG, fg=FG, font=("Segoe UI", 11, "bold"))
        self.status.pack(side="left")
        self.count_label = tk.Label(status_row, text="Clicks: 0", bg=BG, fg=DIM, font=("Segoe UI", 10))
        self.count_label.pack(side="right")

        self.toggle_btn = ttk.Button(outer, text="Disable", command=self.toggle, style="TButton")
        self.toggle_btn.pack(fill="x", pady=(0, 14))

        # ---- samples card ----
        samp_card = tk.Frame(outer, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        samp_card.pack(fill="both", pady=(0, 12))
        tk.Label(samp_card, text="  Samples", bg=SURFACE, fg=ACCENT, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 4), padx=10)
        self.sample_info = tk.Label(samp_card, text="", bg=SURFACE, fg=DIM, font=("Segoe UI", 9))
        self.sample_info.pack(anchor="w", padx=10, pady=(0, 4))

        list_wrap = tk.Frame(samp_card, bg=SURFACE)
        list_wrap.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.sample_canvas = tk.Canvas(list_wrap, height=96, bg=SURFACE2, highlightthickness=0, relief="flat")
        self.sample_scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=self.sample_canvas.yview)
        self.sample_inner = tk.Frame(self.sample_canvas, bg=SURFACE2)
        self.sample_inner.bind("<Configure>", lambda e: self.sample_canvas.configure(scrollregion=self.sample_canvas.bbox("all")))
        self.sample_canvas.create_window((0, 0), window=self.sample_inner, anchor="nw")
        self.sample_canvas.configure(yscrollcommand=self.sample_scroll.set)
        self.sample_canvas.pack(side="left", fill="both", expand=True)
        self.sample_scroll.pack(side="right", fill="y")

        btn_row = tk.Frame(samp_card, bg=SURFACE)
        btn_row.pack(fill="x", padx=10, pady=(4, 10))
        ttk.Button(btn_row, text="↻ Refresh", command=self.refresh_samples, style="Ghost.TButton").pack(side="left")
        ttk.Button(btn_row, text="All on", command=self.select_all, style="Ghost.TButton").pack(side="left", padx=4)
        ttk.Button(btn_row, text="All off", command=self.select_none, style="Ghost.TButton").pack(side="left")
        ttk.Button(btn_row, text="▶ Preview", command=self.preview_selected, style="Ghost.TButton").pack(side="right")

        self.sample_vars = {}

        # ---- sound card ----
        snd_card = tk.Frame(outer, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        snd_card.pack(fill="x", pady=(0, 12))
        tk.Label(snd_card, text="  Sound", bg=SURFACE, fg=ACCENT, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 4), padx=10)

        def slider_row(parent, label, var, frm, to, on_release, on_motion):
            r = tk.Frame(parent, bg=SURFACE)
            r.pack(fill="x", padx=10, pady=(6, 2))
            tk.Label(r, text=label, bg=SURFACE, fg=FG, font=("Segoe UI", 9)).pack(anchor="w")
            row = tk.Frame(r, bg=SURFACE)
            row.pack(fill="x")
            sc = ttk.Scale(row, from_=frm, to=to, orient="horizontal", variable=var, command=lambda e: None, style="TScale")
            sc.pack(side="left", fill="x", expand=True)
            lbl = tk.Label(row, text=str(var.get()), bg=SURFACE, fg=ACCENT, font=("Segoe UI", 9, "bold"), width=4)
            lbl.pack(side="right")
            sc.bind("<ButtonRelease-1>", lambda e: on_release())
            sc.bind("<Motion>", lambda e: on_motion(lbl))
            return sc, lbl

        self.vol = tk.IntVar(value=70)
        self.vol_scale, self.vol_lbl = slider_row(snd_card, "Volume", self.vol, 0, 100,
            self.on_params_changed, lambda l: l.config(text=str(self.vol.get())))

        self.pitch = tk.IntVar(value=6)
        self.pitch_scale, self.pitch_lbl = slider_row(snd_card, "Pitch variation (%)", self.pitch, 0, 15,
            self.on_params_changed, lambda l: l.config(text=str(self.pitch.get())))

        self.jitter = tk.IntVar(value=3)
        self.jitter_scale, self.jitter_lbl = slider_row(snd_card, "Timing jitter (ms)", self.jitter, 0, 30,
            self.on_jitter_changed, lambda l: l.config(text=str(self.jitter.get())))

        self.maxlen = tk.IntVar(value=120)
        self.maxlen_scale, self.maxlen_lbl = slider_row(snd_card, "Max click length (ms)", self.maxlen, 30, 300,
            self.on_maxlen_changed, lambda l: l.config(text=str(self.maxlen.get())))

        # ---- options ----
        opt_card = tk.Frame(outer, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        opt_card.pack(fill="x", pady=(0, 12))
        tk.Label(opt_card, text="  Options", bg=SURFACE, fg=ACCENT, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 4), padx=10)
        self.ontop = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_card, text="Always on top", variable=self.ontop,
                        command=self.on_top_changed, style="TCheckbutton").pack(anchor="w", padx=10, pady=2)
        self.distinct = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_card, text="Distinct sounds for special keys", variable=self.distinct,
                        command=self.on_distinct_changed, style="TCheckbutton").pack(anchor="w", padx=10, pady=(2, 10))

        # ---- footer ----
        foot = tk.Frame(outer, bg=BG)
        foot.pack(fill="x", pady=(4, 0))
        ttk.Button(foot, text="Test click", command=self.test_click, style="TButton").pack(side="left")
        tk.Label(foot, text="Close window to quit  ·  hook is system-wide", bg=BG, fg=DIM,
                 font=("Segoe UI", 8)).pack(side="right")

        self.rebuild_debounce = None
        self.refresh_samples()
        self.on_params_changed()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.update_counter()


    def refresh_samples(self):
        scan_samples()
        for w in self.sample_inner.winfo_children():
            w.destroy()
        self.sample_vars.clear()
        for fn in all_sample_files:
            var = tk.BooleanVar(value=fn in enabled_sample_files)
            dur = sample_duration(fn)
            label = "%s   (%.2fs)" % (fn, dur)
            cb = ttk.Checkbutton(self.sample_inner, text=label, variable=var,
                                 command=self.on_sample_toggle, style="TCheckbutton")
            cb.pack(anchor="w", padx=2, pady=1)
            self.sample_vars[fn] = var
        if not all_sample_files:
            tk.Label(self.sample_inner, text="(no samples found)", bg=SURFACE2, fg=DIM,
                     font=("Segoe UI", 9)).pack(anchor="w")
        n_en = len(enabled_sample_files)
        self.sample_info.config(
            text="%d file(s) found, %d enabled" % (len(all_sample_files), n_en))

    def on_sample_toggle(self):
        enabled_sample_files.clear()
        for fn, var in self.sample_vars.items():
            if var.get():
                enabled_sample_files.add(fn)
        self.sample_info.config(
            text="%d file(s) found, %d enabled" % (len(all_sample_files), len(enabled_sample_files)))
        self.on_params_changed()

    def select_all(self):
        for var in self.sample_vars.values():
            var.set(True)
        self.on_sample_toggle()

    def select_none(self):
        for var in self.sample_vars.values():
            var.set(False)
        self.on_sample_toggle()

    def preview_selected(self):
        chosen = [fn for fn, var in self.sample_vars.items() if var.get()]
        if not chosen:
            return
        fn = random.choice(chosen)
        path = os.path.join(SAMPLES_DIR, fn)
        try:
            arr = read_audio_file(path)
            if arr:
                arr = trim_click(arr)
                a = np.asarray(arr, dtype=np.float32) / 32768.0
                with active_lock:
                    active_sounds.append({"data": a, "pos": 0})
        except Exception:
            pass

    def toggle(self):
        if enabled_flag.is_set():
            enabled_flag.clear()
            pressed_vks.clear()
            self.status.config(text="Status: OFF", style="StatusOff.TLabel")
            self.toggle_btn.config(text="Enable")
        else:
            enabled_flag.set()
            self.status.config(text="Status: ON", style="StatusOn.TLabel")
            self.toggle_btn.config(text="Disable")

    def on_params_changed(self):
        if self.rebuild_debounce is not None:
            try:
                self.root.after_cancel(self.rebuild_debounce)
            except Exception:
                pass
        self.rebuild_debounce = self.root.after(300, self._do_rebuild)

    def _do_rebuild(self):
        self.rebuild_debounce = None
        volume = self.vol.get() / 100.0
        pitch_spread = self.pitch.get() / 100.0
        threading.Thread(target=rebuild_all, args=(volume, pitch_spread), daemon=True).start()

    def on_jitter_changed(self, _=None):
        timing_jitter[0] = self.jitter.get() / 1000.0
        self.jitter_lbl.config(text=str(self.jitter.get()))

    def on_maxlen_changed(self, _=None):
        max_click_ms[0] = self.maxlen.get()
        self.maxlen_lbl.config(text=str(self.maxlen.get()))
        self.on_params_changed()

    def on_top_changed(self):
        self.root.attributes("-topmost", self.ontop.get())

    def on_distinct_changed(self):
        distinct_sounds[0] = bool(self.distinct.get())

    def test_click(self):
        with pools_lock:
            pool = pools_ref[0].get("standard", [])
        if pool:
            with active_lock:
                active_sounds.append({"data": random.choice(pool), "pos": 0})

    def update_counter(self):
        self.count_label.config(text="Clicks: %d" % play_count[0])
        self.root.after(200, self.update_counter)

    def on_close(self):
        stop_event.set()
        try:
            if hook_tid[0] is not None:
                user32.PostThreadMessageW(hook_tid[0], WM_QUIT, 0, 0)
        except Exception:
            pass
        stop_audio_stream()
        self.root.destroy()


def main():
    scan_samples()
    rebuild_all(0.7, 0.06)

    start_audio_stream()
    ht = threading.Thread(target=hook_thread, daemon=True)
    ht.start()

    root = tk.Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
