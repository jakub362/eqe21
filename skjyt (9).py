#!/usr/bin/env python3
"""
skjyt - watch YouTube (or any video) as text, in colour, with sound.

Runs entirely on the device. On iOS it lives inside a-Shell: yt-dlp scrapes
YouTube directly, a-Shell's native ffmpeg decodes, audio plays through
AVFoundation reached with ctypes. No server, no Invidious or Piped instance,
no second app. The same file runs on Linux, macOS and Windows.

Setup:
    iOS      pip install --upgrade yt-dlp --no-deps      (ffmpeg is built in)
    Linux    pip install yt-dlp   +   apt install ffmpeg
    macOS    pip install yt-dlp   +   brew install ffmpeg
    Windows  pip install yt-dlp   +   ffmpeg on PATH

Usage:
    python3 skjyt.py                       menu: search, queue, settings
    python3 skjyt.py "bad apple"
    python3 skjyt.py https://youtu.be/...
    python3 skjyt.py ~/Videos/clip.mkv     local file, no network
    python3 skjyt.py --bench               measure render cost, all modes
    python3 skjyt.py --record out.cast     save the session
    python3 skjyt.py --replay out.cast     play it back

Render modes, by how many source pixels fit in one character cell:
    braille  8 pixels (2x4). The most a terminal can hold. No colour.
    quad     4 pixels (2x2). Colour, two per cell, quadrant glyphs.
    blocks   2 pixels (1x2). Colour above and below, half-block.
    squares  1 pixel. Solid coloured block.
    color    1 pixel. ASCII character tinted with its own colour.
    ascii    1 pixel. Plain text. By far the cheapest.

Lists show a small picture per row (settings -> p, or --no-thumbs).

Keys during playback:
    space / k       pause
    left / right    seek 5s          (or , and .)
    down / up       seek 30s         (or j and l)
    - / +           slower / faster  (0.25x up to 4x)
    f               fast forward     (2x, or back to 1x)
    0               back to 1x
    n / p           next / previous in the queue
    [               subtitles on or off
    r               force a clean repaint
    q               quit

Some terminals never hand a running program its keystrokes at all - a-Shell
on iOS is one, confirmed with `skjyt probe`. There, run with
`--control line`: the picture renders on a worker thread and you type
commands at a prompt.

    p pause   q quit   + - speed   f 2x   s 30 seek   n next

`--control auto` (the default) uses keys, and switches itself to the typed
prompt for the next video if a whole one plays without a single keystroke
arriving. `--limit SECONDS` stops a video with no input at all.

Everything else lives in the settings screen.
"""

import argparse
import ctypes
import ctypes.util
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

RAMP = " .:-=+*#%@"
MODES = ("braille", "quad", "blocks", "squares", "color", "ascii")

# How many source pixels each character cell carries, (across, down).
SUBPIXELS = {
    "ascii":   (1, 1),
    "color":   (1, 1),
    "squares": (1, 1),
    "blocks":  (1, 2),
    "quad":    (2, 2),
    "braille": (2, 4),
}

# bit0 top-left, bit1 top-right, bit2 bottom-left, bit3 bottom-right
QUADRANTS = (" ", "\u2598", "\u259d", "\u2580", "\u2596", "\u258c", "\u259e",
             "\u259b", "\u2597", "\u259a", "\u2590", "\u259c", "\u2584",
             "\u2599", "\u259f", "\u2588")

# Braille dot numbering is not raster order; this is the bit per (x, y).
BRAILLE_BITS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))

DEPTHS = ("truecolor", "256")
BACKENDS = ("auto", "apple", "ffplay")
QUALITIES = (144, 240, 360, 480)

DIM = "\x1b[38;2;110;118;129m"
ACCENT = "\x1b[38;2;120;200;255m"
WARN = "\x1b[38;2;240;170;90m"
GOOD = "\x1b[38;2;120;220;150m"
BOLD = "\x1b[1m"
OFF = "\x1b[0m"

UPPER_HALF = "\u2580"
FULL_BLOCK = "\u2588"

IS_WINDOWS = os.name == "nt"


# ==========================================================================
# Platform
# ==========================================================================

def _probe_subprocess():
    """a-Shell runs commands in-process with no fork/exec, so pipes fail
    there. Everywhere else they work and are the better tool. Ask the
    system, don't guess which platform we are on."""
    if IS_WINDOWS:
        return True
    try:
        subprocess.run(
            [sys.executable, "-c", ""], timeout=10,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


HAVE_SUBPROCESS = _probe_subprocess()

# Desktop terminals are far faster than hterm in a WebView.
DEFAULT_CELLS = 12000 if HAVE_SUBPROCESS else 2400

RESIZED = [False]
KEYS_SEEN = [False]
LIMIT = [0.0]          # --limit: stop after N seconds, no keyboard needed

# Commands typed at a line prompt while the picture renders on another
# thread. This is the only channel a-Shell gives a running program: it
# delivers nothing to select, to a non-blocking read, to a blocking read on
# either thread - only to input().
PENDING = []            # not COMMANDS: that name is the CLI help text
PENDING_LOCK = threading.Lock()


LAST_COMMAND = [""]


def parse_time(text):
    """Accept 90, 1:30, or 1:02:03. Returns seconds, or None."""
    text = text.strip()
    if not text:
        return None
    sign = 1.0
    if text[0] in "+-":
        sign = -1.0 if text[0] == "-" else 1.0
        text = text[1:].strip()
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        return None
    total = 0.0
    for part in parts:
        total = total * 60 + part
    return sign * total


def push_command(text):
    """Turn a typed line into the same tokens the key handler uses.

    Forgiving on purpose. The seek syntax was ambiguous enough that it read
    as "seek does nothing": all of `s 90`, `s 1:30`, `s +30`, `90`, `1:30`,
    `>>` and a bare `s` now do something sensible.
    """
    text = (text or "").strip().lower()
    if not text:
        return
    word, _, rest = text.partition(" ")
    rest = rest.strip()
    table = {"p": "space", "pause": "space", "resume": "space", "play": "space",
             "q": "q", "quit": "q", "stop": "q", "exit": "q",
             "n": "n", "next": "n", "prev": "p", "back": "p", "b": "p",
             "f": "f", "ff": "f", "0": "0", "normal": "0", "1x": "0",
             "+": "+", "faster": "+", "-": "-", "slower": "-",
             "subs": "[", "cc": "[", "sub": "["}
    tokens = []

    if word in ("s", "seek", "goto", "go", "jump", "t", "time"):
        if not rest:
            tokens.append("seek:+10")            # bare `s` nudges forward
        else:
            seconds = parse_time(rest)
            if seconds is None:
                tokens.append("bad")
            elif rest[0] in "+-":
                tokens.append("seek:%+f" % seconds)
            else:
                tokens.append("seek:%f" % seconds)
    elif word in (">>", ">"):
        tokens.append("seek:+30")
    elif word in ("<<", "<"):
        tokens.append("seek:-30")
    elif word == "speed":
        tokens.append("speed:" + rest)
    elif word in table:
        tokens.append(table[word])
    elif parse_time(text) is not None and any(c.isdigit() for c in text):
        # A bare number or timestamp means "go there".
        seconds = parse_time(text)
        tokens.append(("seek:%+f" if text[0] in "+-" else "seek:%f") % seconds)
    elif len(text) <= 3:
        # Only treat loose characters as keys for very short input. Scanning
        # a whole word turned a typo like "banana" into two "next" commands.
        for ch in text:
            if ch in "+-0fnpq[":
                tokens.append(ch)

    if tokens == ["bad"]:
        LAST_COMMAND[0] = "%s \u2192 not a time (try 90 or 1:30)" % text
        return

    def describe(token):
        if token.startswith("seek:"):
            value = token[5:]
            amount = float(value)
            if value[0] in "+-":
                return "seek %s%ss" % ("+" if amount >= 0 else "-",
                                       abs(int(amount)))
            return "seek to " + clock_str(amount)
        if token.startswith("speed:"):
            return "speed " + token[6:]
        return {"space": "pause/resume", "q": "quit", "n": "next",
                "p": "previous", "f": "2x", "0": "1x", "+": "faster",
                "-": "slower", "[": "subtitles"}.get(token, token)

    LAST_COMMAND[0] = ("%s \u2192 %s" % (text, ", ".join(describe(t) for t in tokens))
                       if tokens else "%s \u2192 not understood" % text)
    with PENDING_LOCK:
        PENDING.extend(tokens)


def take_commands():
    with PENDING_LOCK:
        if not PENDING:
            return []
        out = list(PENDING)
        del PENDING[:]
    return out


def _on_winch(*_):
    RESIZED[0] = True


def install_handlers():
    if hasattr(signal, "SIGWINCH"):
        try:
            signal.signal(signal.SIGWINCH, _on_winch)
        except Exception:
            pass


def enable_vt():
    """Windows consoles need ANSI turning on, and UTF-8 for the blocks."""
    if IS_WINDOWS:
        try:
            k = ctypes.windll.kernel32
            handle = k.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            k.GetConsoleMode(handle, ctypes.byref(mode))
            k.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def restore_terminal():
    try:
        sys.stdout.write(OFF + "\x1b[r\x1b[?25h\n")
        sys.stdout.flush()
    except Exception:
        pass


def die(msg):
    restore_terminal()
    print("skjyt: " + msg, file=sys.stderr)
    sys.exit(1)


def run_ffmpeg(args):
    """The one place that knows how to invoke ffmpeg on both worlds.

    -nostdin matters: without it ffmpeg reads the terminal for its own
    interactive commands, swallowing the keystrokes meant for playback
    (and a 'q' would quietly stop the decode)."""
    args = ["-nostdin"] + list(args)
    if HAVE_SUBPROCESS:
        try:
            return subprocess.call(["ffmpeg"] + args,
                                   stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            die("ffmpeg not found on PATH")
    return os.system("ffmpeg " + " ".join('"%s"' % a for a in args))


# ==========================================================================
# Settings and saved state
# ==========================================================================

def _writable(path):
    parent = os.path.dirname(path) or "."
    try:
        if not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        probe = path + ".probe"
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
        return True
    except Exception:
        return False


def _config_dir_candidates():
    """a-Shell's home is the app container and is NOT writable - only
    ~/Documents is. Writing to ~/.skjyt.json there failed silently, which
    is what 'settings never save' turned out to be."""
    home = os.path.expanduser("~")
    out = []
    override = os.environ.get("SKJYT_HOME")
    if override:
        out.append(os.path.expanduser(override))
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        out.append(os.path.join(xdg, "skjyt"))
    out.append(os.path.join(home, "Documents", ".skjyt"))
    out.append(os.path.join(home, ".config", "skjyt"))
    out.append(os.path.join(home, ".skjyt"))
    out.append(os.path.join(tempfile.gettempdir(), "skjyt"))
    return out


def _pick_home():
    candidates = _config_dir_candidates()
    for path in candidates:
        if os.path.isfile(os.path.join(path, "config.json")):
            return path, _writable(os.path.join(path, "config.json"))
    legacy = os.path.expanduser("~/.skjyt.json")
    for path in candidates:
        if _writable(os.path.join(path, "config.json")):
            if os.path.isfile(legacy):
                try:
                    shutil.copyfile(legacy, os.path.join(path, "config.json"))
                except Exception:
                    pass
            return path, True
    return candidates[-1], False


SKJ_HOME, HOME_WRITABLE = _pick_home()
CONFIG = os.path.join(SKJ_HOME, "config.json")
STATE = os.path.join(SKJ_HOME, "state.json")
CACHE = os.path.join(SKJ_HOME, "cache")


class Settings:
    FIELDS = ("mode", "fps", "threshold", "audio", "quality", "results",
              "cells", "quant", "backend", "cell_aspect", "dither",
              "cache_mb", "subs", "resume", "chunk", "latency", "region", "thumbs", "depth", "scaler",
              "sharpen", "contrast", "ctrl_c_menu", "control")

    def __init__(self):
        self.mode = "blocks"
        self.fps = 12
        self.threshold = None
        self.audio = True
        self.quality = 240
        self.results = 8
        self.cells = DEFAULT_CELLS
        self.quant = 24
        self.backend = "auto"
        self.cell_aspect = 2.0     # cell height / width
        self.dither = False
        self.cache_mb = 1024
        self.subs = False
        self.resume = True
        self.chunk = 15            # seconds decoded per slice
        self.latency = 0.0         # audio start offset, seconds
        self.region = "PL"         # two-letter code for popular listings
        self.thumbs = True         # picture previews in lists
        self.depth = "truecolor"   # or "256": shorter escapes, coarser colour
        self.scaler = "lanczos"    # ffmpeg downscale filter
        self.sharpen = 0.8         # unsharp strength, 0 disables
        self.contrast = 1.0        # eq contrast, 1.0 disables
        self.ctrl_c_menu = True    # ctrl-c pauses and offers a menu
        self.control = "auto"      # auto | keys | line

    @classmethod
    def load(cls):
        cfg = cls()
        try:
            with open(CONFIG) as fh:
                data = json.load(fh)
            for key in cls.FIELDS:
                if key in data:
                    setattr(cfg, key, data[key])
        except Exception:
            pass
        cfg.sanitise()
        return cfg

    def sanitise(self):
        if self.mode not in MODES:
            self.mode = "blocks"
        if self.backend not in BACKENDS:
            self.backend = "auto"
        if self.control not in ("auto", "keys", "line"):
            self.control = "auto"
        self.fps = min(60, max(1, int(self.fps or 12)))
        self.quality = int(self.quality or 240)
        self.results = min(20, max(1, int(self.results or 8)))
        self.cells = min(40000, max(300, int(self.cells or DEFAULT_CELLS)))
        self.quant = min(64, max(1, int(self.quant or 1)))
        self.cell_aspect = min(3.0, max(1.0, float(self.cell_aspect or 2.0)))
        self.cache_mb = max(0, int(self.cache_mb or 0))
        self.chunk = min(600, max(5, int(self.chunk or 15)))
        self.latency = max(-2.0, min(2.0, float(self.latency or 0.0)))
        self.region = "".join(c for c in str(self.region or "").upper()
                              if c.isalpha())[:2]
        if self.depth not in DEPTHS:
            self.depth = "truecolor"
        if self.scaler not in ("lanczos", "bicubic", "bilinear", "neighbor"):
            self.scaler = "lanczos"
        self.sharpen = min(3.0, max(0.0, float(self.sharpen or 0.0)))
        self.contrast = min(3.0, max(0.3, float(self.contrast or 1.0)))
        if abs(self.contrast - 1.0) < 0.01:
            self.contrast = 0.0
        for flag in ("audio", "dither", "subs", "resume", "thumbs",
                     "ctrl_c_menu"):
            setattr(self, flag, bool(getattr(self, flag)))
        if self.threshold is not None:
            self.threshold = min(255, max(0, int(self.threshold)))

    def save(self):
        """None on success, else the reason it failed."""
        try:
            if not os.path.isdir(SKJ_HOME):
                os.makedirs(SKJ_HOME, exist_ok=True)
            with open(CONFIG, "w") as fh:
                json.dump({k: getattr(self, k) for k in self.FIELDS}, fh, indent=1)
            with open(CONFIG) as fh:     # a write that left nothing behind is
                json.load(fh)            # exactly the bug we are chasing
            return None
        except Exception as exc:
            return "%s: %s" % (type(exc).__name__, exc)

    def cut(self):
        return None if self.mode in ("blocks", "quad") else self.threshold

    def summary(self):
        bits = [self.mode, "%dfps" % self.fps, "%d cells" % self.cells]
        if self.cut() is not None:
            bits.append("cut %d" % self.threshold)
        if self.dither and self.mode in ("ascii", "braille"):
            bits.append("dither")
        if self.depth == "256":
            bits.append("256col")
        if self.subs:
            bits.append("subs")
        bits.append("audio on" if self.audio else "muted")
        return " \u00b7 ".join(bits)


def terminal_id():
    """Which terminal we learned something about.

    A learned "keys never arrive here" must not follow the user to another
    terminal, or to the same one after it starts working - which is how a
    PC ended up stuck in typed mode because of what a phone taught it.
    """
    return "%s/%s" % (sys.platform, os.environ.get("TERM", "?"))


def learned_mode(state):
    blob = state.get("input_mode")
    if isinstance(blob, dict):
        return blob.get("mode") if blob.get("where") == terminal_id() else None
    return None                    # old flat value: ignore, it has no context


def learn_mode(state, mode):
    state["input_mode"] = {"where": terminal_id(), "mode": mode,
                           "when": time.time()}
    save_state(state)


def load_state():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(state):
    try:
        if not os.path.isdir(SKJ_HOME):
            os.makedirs(SKJ_HOME, exist_ok=True)
        with open(STATE, "w") as fh:
            json.dump(state, fh)
    except Exception:
        pass


# ==========================================================================
# Audio
# ==========================================================================

class AppleSound:
    """AVAudioPlayer, in-process. iOS (a-Shell) and macOS."""

    FRAMEWORKS = (
        "/System/Library/Frameworks/Foundation.framework/Foundation",
        "/System/Library/Frameworks/AVFoundation.framework/AVFoundation",
    )

    def __init__(self, path, duration=0.0):
        self.ready = False
        self.error = None
        self._player = None
        try:
            self._bridge()
            self._start(path)
            self.ready = True
        except Exception as exc:
            self.error = str(exc)

    def _bridge(self):
        for fw in self.FRAMEWORKS:
            ctypes.cdll.LoadLibrary(fw)
        libobjc = ctypes.util.find_library("objc") or "/usr/lib/libobjc.dylib"
        self.objc = ctypes.CDLL(libobjc)
        self.objc.objc_getClass.restype = ctypes.c_void_p
        self.objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self.objc.sel_registerName.restype = ctypes.c_void_p
        self.objc.sel_registerName.argtypes = [ctypes.c_char_p]

    def _cls(self, name):
        ref = self.objc.objc_getClass(name.encode())
        if not ref:
            raise RuntimeError("class %s not found" % name)
        return ctypes.c_void_p(ref)

    def _send(self, receiver, selector, restype=ctypes.c_void_p, argtypes=(), *args):
        """arm64 needs a real signature; objc_msgSend called as varargs
        passes the arguments in the wrong registers."""
        proto = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)
        fn = ctypes.cast(self.objc.objc_msgSend, proto)
        sel = ctypes.c_void_p(self.objc.sel_registerName(selector.encode()))
        return fn(receiver, sel, *args)

    def _nsstring(self, text):
        return ctypes.c_void_p(self._send(
            self._cls("NSString"), "stringWithUTF8String:",
            ctypes.c_void_p, (ctypes.c_char_p,), text.encode(),
        ))

    def _start(self, path):
        void_p = ctypes.c_void_p
        session = void_p(self._send(self._cls("AVAudioSession"), "sharedInstance"))
        self._send(session, "setCategory:error:", ctypes.c_bool,
                   (ctypes.c_void_p, ctypes.c_void_p),
                   self._nsstring("AVAudioSessionCategoryPlayback"), None)
        self._send(session, "setActive:error:", ctypes.c_bool,
                   (ctypes.c_bool, ctypes.c_void_p), True, None)

        url = void_p(self._send(
            self._cls("NSURL"), "fileURLWithPath:", ctypes.c_void_p,
            (ctypes.c_void_p,), self._nsstring(os.path.abspath(path)),
        ))
        player = void_p(self._send(self._cls("AVAudioPlayer"), "alloc"))
        player = void_p(self._send(
            player, "initWithContentsOfURL:error:", ctypes.c_void_p,
            (ctypes.c_void_p, ctypes.c_void_p), url, None,
        ))
        if not player.value:
            raise RuntimeError("AVAudioPlayer could not load the file")

        # Held on self: ctypes has no ARC, a released player goes silent.
        self._player = player
        # enableRate has to be set BEFORE prepareToPlay. Setting it later,
        # when the speed changes, is accepted and reported back correctly
        # and does nothing - which is why fast forward played at 1x and
        # then got its audio muted by the speed check.
        self._send(player, "setEnableRate:", None, (ctypes.c_bool,), True)
        self._send(player, "prepareToPlay", ctypes.c_bool)
        if not self._send(player, "play", ctypes.c_bool):
            raise RuntimeError("play() refused")

    def time(self):
        return self._send(self._player, "currentTime", ctypes.c_double)

    def seek(self, seconds):
        self._send(self._player, "setCurrentTime:", None,
                   (ctypes.c_double,), max(0.0, seconds))

    def set_rate(self, rate):
        """AVAudioPlayer ignores rate unless enableRate is set first, and
        rate is a float, not a double - the wrong signature silently passes
        garbage on arm64. Read it back: if the value did not stick there is
        no point pretending the speed changed."""
        try:
            self._send(self._player, "setEnableRate:", None,
                       (ctypes.c_bool,), True)   # already on; harmless
            self._send(self._player, "setRate:", None,
                       (ctypes.c_float,), ctypes.c_float(rate))
            got = self._send(self._player, "rate", ctypes.c_float)
            return abs(float(got) - float(rate)) < 0.05
        except Exception:
            return False

    def duration(self):
        return self._send(self._player, "duration", ctypes.c_double)

    def playing(self):
        return bool(self._send(self._player, "isPlaying", ctypes.c_bool))

    def pause(self):
        self._send(self._player, "pause", ctypes.c_void_p)

    def resume(self):
        self._send(self._player, "play", ctypes.c_bool)

    def stop(self):
        if self._player is not None:
            try:
                self._send(self._player, "stop", ctypes.c_void_p)
            except Exception:
                pass


class CommandSound:
    """ffplay, driven as a child process. Linux and Windows.

    ffplay has no queryable position, so we keep the clock ourselves and
    respawn with -ss to pause and seek. That costs an audible gap on seek;
    the real fix is mpv with --input-ipc-server, at the price of a second
    dependency.
    """

    def __init__(self, path, duration=0.0):
        self.ready = False
        self.error = None
        self.path = os.path.abspath(path)
        self._proc = None
        self._exe = shutil.which("ffplay")
        self._offset = 0.0
        self._since = time.monotonic()
        self._paused = False
        self._duration = duration or 0.0
        self.rate = 1.0

        if not HAVE_SUBPROCESS:
            self.error = "no subprocess on this platform"
            return
        if not self._exe:
            self.error = "ffplay not found (it comes with ffmpeg)"
            return
        try:
            self._spawn(0.0)
            # No audio device (headless box, WSL without a sound server)
            # makes ffplay exit at once. Catch it here rather than letting
            # the video track a clock that already stopped.
            time.sleep(0.35)
            if self._proc.poll() is not None:
                self.error = "ffplay exited immediately - no audio device?"
                self._proc = None
                return
            self.ready = True
        except Exception as exc:
            self.error = str(exc)

    @staticmethod
    def _atempo(rate):
        """atempo only accepts 0.5-2.0, so chain filters for anything else."""
        parts = []
        left = float(rate)
        while left > 2.0:
            parts.append("atempo=2.0")
            left /= 2.0
        while left < 0.5:
            parts.append("atempo=0.5")
            left /= 0.5
        parts.append("atempo=%.4f" % left)
        return ",".join(parts)

    def set_rate(self, rate):
        # Position first: time() scales by self.rate, so setting the rate
        # before reading it makes the position jump by however long the
        # last spawn has been running.
        where = self.time()
        self.rate = rate
        if not self._paused:
            self._spawn(where)            # only way to change ffplay's speed
        return True

    def _spawn(self, position):
        self._kill()
        args = [self._exe, "-nodisp", "-autoexit", "-loglevel", "quiet",
                "-ss", "%.3f" % position]
        if abs(getattr(self, "rate", 1.0) - 1.0) > 0.01:
            args += ["-af", self._atempo(self.rate)]
        self._proc = subprocess.Popen(
            args + [self.path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._offset = position
        self._since = time.monotonic()
        self._paused = False

    def _kill(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None

    def time(self):
        if self._paused:
            return self._offset
        # Wall time advances at 1x; the audio is playing at self.rate.
        return self._offset + (time.monotonic() - self._since) * self.rate

    def duration(self):
        return self._duration

    def playing(self):
        return bool(self._proc) and self._proc.poll() is None

    def pause(self):
        self._offset = self.time()
        self._kill()
        self._paused = True

    def resume(self):
        self._spawn(self._offset)

    def seek(self, seconds):
        target = max(0.0, seconds)
        if self._paused:
            self._offset = target
        else:
            self._spawn(target)

    def stop(self):
        self._kill()


class DeadSound:
    ready = False

    def __init__(self, error):
        self.error = error

    def stop(self):
        pass


def make_sound(path, duration=0.0, backend="auto"):
    tried = []
    for name, cls in (("apple", AppleSound), ("ffplay", CommandSound)):
        if backend not in ("auto", name):
            continue
        made = cls(path, duration)
        if made.ready:
            return made
        tried.append("%s: %s" % (name, made.error))
        if backend == name:
            return made
    return DeadSound("; ".join(tried) or "no audio backend")


SPEEDS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)


class Clock:
    """Playback position, backed by the audio player when there is one.

    Speed is the reason this is not just a stopwatch. With audio, the
    player's own position already runs at the chosen rate, so the video
    follows for free. Without audio the wall clock is scaled here, and
    every change re-anchors so the position never jumps.
    """

    def __init__(self, sound, latency=0.0):
        self.sound = sound if (sound and sound.ready) else None
        self.latency = latency
        self.paused = False
        self.rate = 1.0
        self.silenced = None               # audio paused because it cannot
        self.audio_dropped = False         # keep up with the chosen speed
        self._verify = None                # ("rate"|"seek", ...) - see check()
        self._pos = 0.0                    # position at the last anchor
        self._wall = time.monotonic()      # when that anchor was set

    def _anchor(self, position=None):
        self._pos = self.time() if position is None else position
        self._wall = time.monotonic()

    def time(self):
        if self.sound:
            return max(0.0, self.sound.time() + self.latency)
        if self.paused:
            return self._pos
        return self._pos + (time.monotonic() - self._wall) * self.rate

    def set_rate(self, rate):
        """Change speed, and make sure the picture really follows.

        The video takes its position from the audio player when there is
        one. So if the backend cannot change its own rate, setting a number
        here would do nothing visible at all - the bar would say 2x while
        everything ran at 1x. In that case the audio is paused and the
        picture runs on the wall clock instead; going back to 1x brings the
        sound back where it left off.
        """
        rate = min(4.0, max(0.25, float(rate)))
        if abs(rate - self.rate) < 0.001:
            return self.rate
        here = self.time()
        self.rate = rate

        if self.sound is not None:
            worked = False
            try:
                worked = bool(self.sound.set_rate(rate))
            except Exception:
                worked = False
            if not worked:
                self.silenced = self.sound
                try:
                    self.sound.pause()
                except Exception:
                    pass
                self.sound = None
                self.audio_dropped = True
        elif self.silenced is not None and abs(rate - 1.0) < 0.001:
            # Back to normal speed: the sound can rejoin.
            try:
                self.silenced.seek(max(0.0, here - self.latency))
                self.silenced.resume()
                self.sound = self.silenced
                self.silenced = None
                self.audio_dropped = False
            except Exception:
                pass

        self._anchor(here)
        # Setting a rate is not the same as it taking effect. AVAudioPlayer
        # will accept enableRate/rate, report the value back correctly, and
        # still play at 1x - which looks exactly like "the bar says 2x but
        # the picture is the same speed". So check the position afterwards.
        if self.sound is not None and abs(self.rate - 1.0) > 0.01:
            self._verify = ("rate", time.monotonic(), self.time(), self.rate)
        else:
            self._verify = None
        return self.rate

    def check(self):
        """Confirm a speed change actually moved the playhead.

        Called every frame. If the audio clock is not advancing at the
        requested rate after a moment, the audio is not honouring it: pause
        it and run the picture on the wall clock instead, which is the only
        way the speed control means anything.
        """
        if not self._verify or self.paused:
            return False
        kind, started, value, wanted = self._verify
        elapsed = time.monotonic() - started
        if elapsed < (0.5 if kind == "seek" else 0.6):
            return False
        self._verify = None
        if self.sound is None:
            return False

        if kind == "seek":
            drift = abs(self.time() - (value + elapsed * wanted))
            if drift < 1.5:
                return False                # it went where it was told
            here = value + elapsed * wanted
        else:
            observed = (self.time() - value) / max(0.001, elapsed)
            if observed >= wanted * 0.7:
                return False                # it is keeping up
            here = self.time()
        self.silenced = self.sound
        try:
            self.sound.pause()
        except Exception:
            pass
        self.sound = None
        self.audio_dropped = True
        self._anchor(here)
        return True

    def faster(self):
        higher = [s for s in SPEEDS if s > self.rate + 0.001]
        return self.set_rate(higher[0] if higher else SPEEDS[-1])

    def slower(self):
        lower = [s for s in SPEEDS if s < self.rate - 0.001]
        return self.set_rate(lower[-1] if lower else SPEEDS[0])

    def toggle(self):
        self._verify = None
        if self.sound:
            self.paused = not self.paused
            self.sound.pause() if self.paused else self.sound.resume()
            return
        if not self.paused:
            # Anchor while still running: time() reads _pos once paused is
            # set, so flipping the flag first loses the position entirely.
            self._anchor()
            self.paused = True
        else:
            self.paused = False
            self._wall = time.monotonic()

    def goto(self, target):
        """One clamp for both streams, so audio and video cannot land on
        different frames near zero."""
        target = max(0.0, target)
        # A seek moves the playhead a long way at once, which would look to
        # check() like the audio racing or running backwards.
        self._verify = None
        self._anchor(target)
        if self.sound:
            self.sound.seek(max(0.0, target - self.latency))
            # And verify it: a backend that ignores setCurrentTime leaves the
            # clock exactly where it was, so nothing moves at all - which is
            # what "seek is broken" looks like from the outside.
            self._verify = ("seek", time.monotonic(), target, self.rate)

    def nudge(self, delta):
        self.goto(self.time() + delta)

    def finished(self):
        return bool(self.sound) and not self.paused and not self.sound.playing()

    def disown(self):
        """Audio stopped but the video has not. Carry on without it."""
        here = self.time()
        self.sound = None
        self.paused = False
        self._anchor(here)


class InputPump:
    """One reader thread, alive only while a video is playing.

    Everything about stdin here is dictated by two facts learned the hard
    way. In a-Shell a non-blocking read returns nothing, /dev/tty is
    permission-denied, and even a blocking read in a thread produces
    nothing - but plain input() has always worked. On a desktop the
    reverse: threads and raw mode work fine, and the O_NONBLOCK trick broke
    stdout.

    So: exactly one thing owns stdin at a time. The pump runs during
    playback for immediate keys; menus use input(). `retire()` asks the
    thread to stop after its next read, and `ask()` waits briefly for that
    so the keystroke it swallows on the way out is not lost.
    """

    def __init__(self):
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.ok = False
        self.error = None
        self.saw_any = False
        self.retiring = False
        self.thread = None
        self.method = "msvcrt" if IS_WINDOWS else "os.read"
        try:
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            self.ok = True
        except Exception as exc:
            self.error = str(exc)

    def _loop(self):
        while True:
            try:
                if IS_WINDOWS:
                    import msvcrt
                    data = msvcrt.getwch().encode("utf-8", "replace")
                else:
                    data = os.read(0, 64)
                    if not data:
                        break                     # EOF
            except Exception as exc:
                self.error = "%s: %s" % (type(exc).__name__, exc)
                break
            with self.lock:
                self.buf.extend(data)
                self.saw_any = True
            if self.retiring:
                break                             # handed the keystroke over
        self.ok = False

    def alive(self):
        return bool(self.thread and self.thread.is_alive())

    def retire(self):
        """Stop after the next read. It cannot be interrupted mid-read."""
        self.retiring = True

    def take(self):
        with self.lock:
            if not self.buf:
                return ""
            data = bytes(self.buf)
            self.buf.clear()
        return data.decode("utf-8", "replace")

    def take_line(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        checked = False
        while True:
            with self.lock:
                cut = self.buf.find(b"\n")
                if cut < 0:
                    cut = self.buf.find(b"\r")
                if cut >= 0:
                    line = bytes(self.buf[:cut])
                    del self.buf[:cut + 1]
                    return line.decode("utf-8", "replace").strip()
            if timeout == 0 and checked:
                return None
            checked = True
            if deadline and time.monotonic() > deadline:
                return None
            time.sleep(0.02)

    def clear(self):
        with self.lock:
            self.buf.clear()


PUMP = None


def input_pump():
    """The current reader, started fresh if the last one has retired."""
    global PUMP
    if PUMP is None or not PUMP.ok:
        PUMP = InputPump()
    return PUMP


def emit(text):
    """Write to the terminal, tolerating a non-blocking stdout.

    stdin and stdout usually share one open file description, so anything
    that marks stdin O_NONBLOCK marks stdout too, and a frame-sized write
    then fails with BlockingIOError part-way through. Keys avoids causing
    that now, but a parent process can hand us a non-blocking stdout as
    well, and losing half a frame is not an acceptable outcome.
    """
    remaining = text
    while remaining:
        try:
            sys.stdout.write(remaining)
            sys.stdout.flush()
            return
        except BlockingIOError as exc:
            done = getattr(exc, "characters_written", 0)
            remaining = remaining[done:] if done else remaining
            time.sleep(0.004)
        except (BrokenPipeError, ValueError):
            return


class Keys:
    """Immediate keypresses, where the terminal allows them.

    Raw mode is best-effort now. If cbreak works you get single keys; if it
    does not, the same keys still arrive when Enter is pressed, because the
    bytes come from InputPump either way. Nothing depends on a non-blocking
    read succeeding, which is what kept failing.
    """

    ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}

    def __init__(self):
        self.ok = False
        self.raw = False
        self.reason = ""
        self._fd = None
        self._attrs = None
        self.pump = None
        self.saw_any = False

    def __enter__(self):
        self.enable()
        return self

    def enable(self):
        # No reader thread. A blocking read cannot be cancelled, so the
        # thread was always still holding stdin when the menu prompt
        # started and it stole the first keypress - which is what made the
        # menu "sometimes not work" after a video. The main-thread poll
        # below does the same job with nothing to clean up.
        self.pump = None
        self.ok = True
        if IS_WINDOWS:
            self.raw = True
            return True
        try:
            import termios
            import tty
            if os.isatty(0):
                self._fd = 0
                self._attrs = termios.tcgetattr(0)
                tty.setcbreak(0)
                self.raw = True
        except Exception as exc:
            self.reason = str(exc)       # not fatal: line mode still works
            self.raw = False
        return True

    def reapply(self):
        if self.raw and not IS_WINDOWS and self._fd is not None:
            try:
                import tty
                tty.setcbreak(self._fd)
            except Exception:
                pass

    def flush(self):
        if self.pump:
            self.pump.clear()

    def describe(self):
        if not self.ok:
            return "unavailable: %s" % (self.reason or "unknown")
        mode = "immediate (raw mode)" if self.raw else "line mode, press Enter"
        state = "delivering" if self.saw_any else "nothing received yet"
        return "%s, %s, polled on the main thread" % (mode, state)

    def drain(self):
        """Poll stdin on the main thread. select says whether anything is
        waiting; the read is non-blocking for the instant it takes, so
        neither call can stall the video and nothing is left holding
        stdin afterwards."""
        raw = self._read_here() if self.ok else ""
        if raw:
            self.saw_any = True
        return self._parse(raw) if raw else []

    # Windows console: arrows and other special keys arrive from getwch()
    # as a '\x00' or '\xe0' prefix followed by a scan code.
    WIN_SCAN = {"H": "up", "P": "down", "M": "right", "K": "left"}

    def _read_windows(self):
        """msvcrt polling. kbhit() never blocks, and getwch() only runs
        when a key is already waiting, so the video never stalls."""
        try:
            import msvcrt
        except Exception:
            return ""
        out = []
        try:
            while msvcrt.kbhit() and len(out) < 64:
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    code = msvcrt.getwch()
                    arrow = self.WIN_SCAN.get(code)
                    if arrow:
                        # Hand it to _parse as the ANSI sequence it knows.
                        out.append("\x1b[" + {"up": "A", "down": "B",
                                               "right": "C",
                                               "left": "D"}[arrow])
                    continue
                out.append(ch)
        except Exception:
            pass
        return "".join(out)

    def _read_here(self):
        if IS_WINDOWS:
            return self._read_windows()
        if not os.isatty(0):
            return ""
        try:
            import select as _select
            if not _select.select([0], [], [], 0)[0]:
                return ""
        except Exception:
            return ""
        try:
            import fcntl
            flags = fcntl.fcntl(0, fcntl.F_GETFL)
        except Exception:
            return ""
        try:
            fcntl.fcntl(0, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            data = os.read(0, 64)
        except (BlockingIOError, InterruptedError, OSError):
            data = b""
        finally:
            try:
                fcntl.fcntl(0, fcntl.F_SETFL, flags)   # never leave it set:
            except Exception:                          # stdout shares it
                pass
        return data.decode("utf-8", "replace")

    def _parse(self, raw):
        keys = []
        i = 0
        while i < len(raw):
            ch = raw[i]
            if ch == "\x1b":
                if raw[i + 1:i + 2] in ("[", "O"):
                    j = i + 2
                    while j < len(raw) and not raw[j].isalpha() and raw[j] != "~":
                        j += 1
                    final = raw[j] if j < len(raw) else ""
                    arrow = self.ARROWS.get(final)
                    if arrow:
                        keys.append(arrow)
                    i = j + 1
                    continue
                keys.append("esc")
                i += 1
                continue
            if ch == " ":
                keys.append("space")
            elif ch in ("\r", "\n"):
                keys.append("enter")
            elif ch.isprintable():
                keys.append(ch.lower())
            i += 1
        return keys

    def restore(self):
        if IS_WINDOWS or self._fd is None:
            return
        try:
            import termios
            if self._attrs is not None:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._attrs)
                self._attrs = None
        except Exception:
            pass
        self._fd = None
        self.raw = False

    def __exit__(self, *_):
        self.restore()


class NowPlaying:
    """Title and progress on the iOS lock screen and Control Center.

    Read-only: it publishes what is playing. Acting on the lock-screen
    buttons would mean handing MPRemoteCommandCenter an Objective-C block,
    which ctypes cannot build safely here, and a malformed block crashes the
    app rather than raising. Not worth it untested.
    """

    def __init__(self, sound):
        self.ok = False
        self._objc = getattr(sound, "objc", None)
        self._send = getattr(sound, "_send", None)
        self._cls = getattr(sound, "_cls", None)
        self._nsstring = getattr(sound, "_nsstring", None)
        self._last = -1.0
        if not self._objc:
            return
        try:
            self._mp = ctypes.CDLL(
                "/System/Library/Frameworks/MediaPlayer.framework/MediaPlayer")
            self._centre = ctypes.c_void_p(self._send(
                self._cls("MPNowPlayingInfoCenter"), "defaultCenter"))
            if not self._centre.value:
                return
            # The property names are NSString constants in the framework;
            # read the real symbols rather than guessing their contents.
            self._keys = {
                name: ctypes.c_void_p.in_dll(self._mp, name)
                for name in ("MPMediaItemPropertyTitle",
                             "MPMediaItemPropertyArtist",
                             "MPMediaItemPropertyPlaybackDuration",
                             "MPNowPlayingInfoPropertyElapsedPlaybackTime",
                             "MPNowPlayingInfoPropertyPlaybackRate")
            }
            self.ok = True
        except Exception:
            self.ok = False

    def _number(self, value):
        return ctypes.c_void_p(self._send(
            self._cls("NSNumber"), "numberWithDouble:", ctypes.c_void_p,
            (ctypes.c_double,), float(value)))

    def update(self, title, artist, elapsed, duration, rate):
        if not self.ok:
            return
        # Once a second is plenty; the system smooths between updates.
        if rate and abs(elapsed - self._last) < 1.0:
            return
        self._last = elapsed
        try:
            info = ctypes.c_void_p(self._send(
                self._cls("NSMutableDictionary"), "dictionary"))
            pairs = (
                ("MPMediaItemPropertyTitle", self._nsstring(title[:120])),
                ("MPMediaItemPropertyArtist", self._nsstring(artist[:80] or "skjyt")),
                ("MPMediaItemPropertyPlaybackDuration", self._number(duration)),
                ("MPNowPlayingInfoPropertyElapsedPlaybackTime", self._number(elapsed)),
                ("MPNowPlayingInfoPropertyPlaybackRate", self._number(rate)),
            )
            for name, value in pairs:
                self._send(info, "setObject:forKey:", None,
                           (ctypes.c_void_p, ctypes.c_void_p),
                           value, self._keys[name])
            self._send(self._centre, "setNowPlayingInfo:", None,
                       (ctypes.c_void_p,), info)
        except Exception:
            self.ok = False

    def clear(self):
        if not self.ok:
            return
        try:
            self._send(self._centre, "setNowPlayingInfo:", None,
                       (ctypes.c_void_p,), None)
        except Exception:
            pass


def pump_runloop():
    """Give the OS a moment to service its own UI. No-op off Apple."""
    try:
        cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, ctypes.c_double,
                                          ctypes.c_bool]
        cf.CFRunLoopRunInMode.restype = ctypes.c_int
        mode = ctypes.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")
        return lambda: cf.CFRunLoopRunInMode(mode, 0.0, True)
    except Exception:
        return lambda: None


# ==========================================================================
# UI chrome
# ==========================================================================

def term_size():
    try:
        size = shutil.get_terminal_size()
        return max(24, size.columns), max(12, size.lines)
    except Exception:
        return 60, 24


def clip(text, width):
    text = "".join(ch for ch in str(text) if ch.isprintable())
    return text if len(text) <= width else text[: max(1, width - 1)] + "\u2026"


def clock_str(seconds):
    seconds = max(0, int(seconds))
    return "%d:%02d" % (seconds // 60, seconds % 60)


def compact_ui():
    """A phone with the keyboard up leaves about ten usable rows. At that
    size the three-line banner and every description scroll the top of the
    menu away, which reads as the UI being broken."""
    try:
        return term_size()[1] < 22
    except Exception:
        return False


def plain(text):
    global _PLAIN_RE
    if _PLAIN_RE is None:
        import re
        _PLAIN_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
    return _PLAIN_RE.sub("", text)


_PLAIN_RE = None


def banner(cols):
    if compact_ui():
        return ACCENT + BOLD + " skjyt " + OFF + ACCENT + \
            "\u2500" * max(0, cols - 8) + OFF + "\n"
    line = "\u2500" * (cols - 2)
    return (ACCENT + "\u256d" + line + "\u256e\n"
            "\u2502" + BOLD + clip(" skjyt", cols - 2).ljust(cols - 2) + OFF
            + ACCENT + "\u2502\n"
            "\u2570" + line + "\u256f" + OFF + "\n")


def screen(cols, subtitle=None):
    # \x1b[r releases any scroll region a previous video left behind. Without
    # it every menu after a typed-mode video renders inside a two-row strip,
    # which looks like two overlapping screens.
    sys.stdout.write("\x1b[r\x1b[?25h\x1b[2J\x1b[H" + banner(cols))
    if subtitle:
        sys.stdout.write(DIM + "  " + clip(subtitle, cols - 2) + OFF
                         + ("\n" if compact_ui() else "\n\n"))
    sys.stdout.flush()


def status(text, colour=DIM):
    sys.stdout.write("  " + colour + clip(text, 74) + OFF + "\n")
    sys.stdout.flush()


def ask(prompt):
    """Prompt for a line, on the one path that works everywhere: input().

    The only subtlety is handover. A pump thread from the video just played
    may still be blocked in a read and will swallow the next keystroke. So
    wait up to a second for it to finish and hand that line over through
    the buffer, and only call input() once no reader is left to steal it.
    """
    emit("  " + ACCENT + prompt + OFF)
    pump = PUMP
    if pump is not None:
        deadline = time.monotonic() + 1.0
        while True:
            line = pump.take_line(timeout=0)
            if line is not None:
                emit(line + "\n")
                return line
            if not pump.alive():
                # The reader has stopped, but it swallowed one keystroke on
                # its way out. In raw mode that keystroke has no newline
                # after it, so take_line will never see it - and waiting
                # for one is why the menu sometimes ignored the first key
                # pressed after a video.
                leftover = pump.take().strip()
                if leftover:
                    emit(leftover + "\n")
                    return leftover
                break
            if time.monotonic() > deadline:
                break
            time.sleep(0.03)
    try:
        return input().strip()
    except (EOFError, KeyboardInterrupt):
        return None


def row(key, label, value=""):
    if compact_ui():
        # Keep the state, drop the explanation: values are written as
        # "auto  <dim>what it means</dim>", so the first double space is
        # the seam.
        short = plain(value).split("  ")[0].strip()
        return "  %s%-3s%s %-13s %s%s%s" % (ACCENT, key, OFF, label,
                                            BOLD, short, OFF)
    return "  %s%-3s%s %-14s %s%s%s" % (ACCENT, key, OFF, label, BOLD, value, OFF)


def speed_str(speed):
    text = ("%.2f" % speed).rstrip("0").rstrip(".")
    return text + "x"


def progress_bar(pos, total, cols, paused, rate=0.0, buffered=1.0, speed=1.0):
    tail = " %s / %s" % (clock_str(pos), clock_str(total))
    if abs(speed - 1.0) > 0.01:
        tail += "  " + speed_str(speed)
    if rate:
        tail += "  %.0ffps" % rate
    width = max(8, cols - len(tail) - 5)
    frac = 0.0 if total <= 0 else min(1.0, pos / total)
    filled = int(frac * width)
    buf_end = min(width, int(min(1.0, buffered) * width))
    mark = (WARN + "\u23f8" + OFF) if paused else (ACCENT + "\u25b6" + OFF)
    bar = (ACCENT + FULL_BLOCK * filled + OFF
           + GOOD + "\u2500" * max(0, buf_end - filled) + OFF
           + DIM + "\u2500" * max(0, width - max(filled, buf_end)) + OFF)
    return " %s %s%s%s%s" % (mark, bar, DIM, tail, OFF)


# ==========================================================================
# Fetching
# ==========================================================================

class _QuietLogger:
    """yt-dlp prints its own ERROR block straight to stderr, which lands in
    the middle of the drawn screen. Swallow it and report it ourselves."""

    def __init__(self):
        self.last = None

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        self.last = msg


YT_LOG = _QuietLogger()


UNAVAILABLE = (
    ("private", "the video is private"),
    ("members-only", "members-only video"),
    ("members only", "members-only video"),
    ("confirm your age", "age-restricted, and there is no sign-in here"),
    ("age-restricted", "age-restricted, and there is no sign-in here"),
    ("age restricted", "age-restricted, and there is no sign-in here"),
    ("inappropriate for some users", "age-restricted, and there is no sign-in here"),
    ("removed by the uploader", "the uploader removed it"),
    ("terminated", "the channel was terminated"),
    ("copyright", "taken down on a copyright claim"),
    ("available in your country", "blocked in your country"),
    ("not available in your country", "blocked in your country"),
    ("geo restrict", "blocked in your country"),
    ("live event will begin", "a premiere that has not started"),
    ("this live event has ended", "a finished live stream with no replay"),
    ("sign in to confirm you're not a bot", "youtube wants a sign-in here"),
    ("not a bot", "youtube is asking for a sign-in to prove you are not a bot"),
    ("needs to be reloaded", "youtube rejected the player session. This one "
                             "is intermittent - retry, and run `skjyt update`"),
    ("sign in to confirm", "youtube wants a sign-in for this one"),
    ("not available", "youtube served no usable formats - try updating "
                      "yt-dlp (skjyt update)"),
    ("unavailable", "youtube served no usable formats - try updating "
                    "yt-dlp (skjyt update)"),
)


ANSI_RE = None


def strip_ansi(text):
    """yt-dlp colours its ERROR prefix, and those codes end up in the
    exception string. They would be printed literally in our message."""
    global ANSI_RE
    if ANSI_RE is None:
        import re
        ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
    return ANSI_RE.sub("", text)


def describe_error(exc):
    """A one-line reason a human can act on."""
    text = strip_ansi(str(exc))
    low = text.lower()
    for needle, reason in UNAVAILABLE:
        if needle in low:
            return reason
    if any(hint in low for hint in JS_HINTS):
        return "youtube's JS challenge, and there is no node runtime here"
    if "ssl" in low or "certificate" in low:
        return "TLS problem reaching youtube - check the clock and CA certs"
    if ("urlopen" in low or "timed out" in low or "connection" in low
            or "temporary failure in name resolution" in low):
        return "network problem"
    # Strip yt-dlp's prefix and the [youtube] id: tag so it reads as prose.
    text = text.replace("ERROR: ", "").strip()
    if text.startswith("[") and "] " in text:
        text = text.split("] ", 1)[1]
    if ": " in text[:24]:
        head, _, tail = text.partition(": ")
        if len(head) < 24 and " " not in head:
            text = tail
    return clip(text.splitlines()[0] if text else "unknown error", 100)


def load_ytdlp():
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        die("yt-dlp missing. Run: pip install --upgrade yt-dlp --no-deps")
    return YoutubeDL


def is_url(text):
    return text.startswith("http://") or text.startswith("https://")


def is_local(text):
    try:
        return os.path.isfile(os.path.expanduser(text))
    except Exception:
        return False


# Errors that are the truth: retrying them wastes time and the answer will
# not change. EVERYTHING ELSE is retried.
#
# The first version of this had it the other way round - a list of errors
# worth retrying - and it kept missing real ones. "The page needs to be
# reloaded" matched nothing in it, so the ladder gave up three rungs early.
# Enumerating YouTube's failure messages is a losing game; enumerating the
# handful that are genuinely final is not.
FINAL = (
    "private video", "members-only", "members only", "confirm your age",
    "age-restricted", "removed by the uploader", "terminated",
    "copyright", "available in your country", "has been removed",
    "does not exist", "no longer available",
)

JS_HINTS = ("nsig", "signature", "challenge", "player")


def _rungs():
    """What to try, in order, when the first attempt finds no formats.

    Deliberately starts with yt-dlp's own defaults and only then overrides
    the player client: a hardcoded client list rots fast, because which
    clients still serve free formats changes every few months. These are
    fallbacks, not preferences.
    """
    return (
        ("", {}),
        ("allowing formats that lack a PO token",
         {"extractor_args": {"youtube": {"formats": ["missing_pot"]}}}),
        ("trying the tv and safari players",
         {"extractor_args": {"youtube": {
             "player_client": ["tv", "web_safari", "android_vr"],
             "formats": ["missing_pot"]}}}),
        ("trying the mobile players",
         {"extractor_args": {"youtube": {
             "player_client": ["ios", "android", "web"],
             "formats": ["missing_pot"]}}}),
        ("trying the embedded players",
         {"extractor_args": {"youtube": {
             "player_client": ["mweb", "web_embedded", "tv_embedded"],
             "formats": ["missing_pot"]}}}),
        ("dropping the quality limit",
         {"extractor_args": {"youtube": {"formats": ["missing_pot"]}},
          "_loose": True}),
        ("one more try on the defaults", {}),
    )


def _extract(opts, target, download, report=None, retry=True):
    """yt-dlp with a fallback ladder.

    "This video is not available" nearly always means extraction failed,
    not that the video is gone - it plays fine in a browser. So a failure
    that looks like an extraction problem gets retried with progressively
    looser options before we believe it.
    """
    YoutubeDL = load_ytdlp()
    last = None
    attempts = 0
    for label, extra in (_rungs() if retry else _rungs()[:1]):
        attempt = dict(opts)
        loose = extra.pop("_loose", False) if extra else False
        attempt.update(extra)
        if loose:
            attempt.pop("format", None)       # take whatever exists
        if report and label:
            report(label + " \u2026")         # say it before, not after
        if attempts:
            # Some of these failures are intermittent session errors, so a
            # moment between tries is worth more than it costs.
            time.sleep(0.4)
        attempts += 1
        try:
            with YoutubeDL(attempt) as ydl:
                return ydl.extract_info(target, download=download), ydl
        except Exception as exc:
            last = exc
            low = str(exc).lower()
            if any(word in low for word in FINAL):
                raise                          # genuinely gone; stop here
    raise last


def search(query, count):
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "logger": YT_LOG}
    info, _ = _extract(opts, "ytsearch%d:%s" % (count, query), False)
    return [e for e in (info.get("entries") or []) if e.get("id")]


def playlist_entries(url):
    """Flat playlist listing, or None if it isn't one."""
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "logger": YT_LOG}
    try:
        info, _ = _extract(opts, url, False)
    except Exception:
        return None
    entries = info.get("entries")
    if not entries:
        return None
    out = []
    for e in entries:
        if e.get("id"):
            out.append({
                "id": e["id"],
                "title": e.get("title") or "?",
                "duration": e.get("duration") or 0,
                "url": e.get("url") or ("https://www.youtube.com/watch?v=" + e["id"]),
            })
    return out or None


def parse_vtt(path):
    """Minimal WebVTT reader -> [(start, end, text)]."""
    def stamp(text):
        try:
            parts = [float(p) for p in text.replace(",", ".").split(":")]
        except ValueError:
            return None
        total = 0.0
        for part in parts:
            total = total * 60 + part
        return total

    cues = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            block = []
            for line in list(fh) + [""]:
                line = line.rstrip("\n")
                if line.strip():
                    block.append(line)
                    continue
                if block:
                    times = [l for l in block if "-->" in l]
                    if times:
                        left, _, right = times[0].partition("-->")
                        start = stamp(left.strip())
                        end = stamp(right.strip().split()[0]) if right.strip() else None
                        body = [l for l in block if "-->" not in l
                                and not l.strip().isdigit()
                                and l.strip().upper() != "WEBVTT"]
                        text = " ".join(body).strip()
                        while "<" in text and ">" in text:
                            a = text.index("<")
                            b = text.index(">", a)
                            text = text[:a] + text[b + 1:]
                        if start is not None and end and text.strip():
                            cues.append((start, end, text.strip()))
                    block = []
    except Exception:
        return []
    cues.sort()
    return cues


class Media:
    def __init__(self, key, title, video, audio, duration, cues,
                 channel="", channel_url=""):
        self.key = key
        self.title = title
        self.video = video
        self.audio = audio
        self.duration = duration
        self.cues = cues
        self.channel = channel
        self.channel_url = channel_url


def has_audio(path):
    """Don't hand a silent file to the audio player and wait for nothing."""
    if not HAVE_SUBPROCESS or not shutil.which("ffprobe"):
        return True                      # can't check; let the player decide
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        return b"audio" in out
    except Exception:
        return True


def probe_duration(path):
    if not HAVE_SUBPROCESS or not shutil.which("ffprobe"):
        return 0.0
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path], stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        return float(out.strip())
    except Exception:
        return 0.0


def fetch(target, workdir, cfg, report):
    """Returns a Media. Local files skip the network entirely."""
    if is_local(target):
        path = os.path.abspath(os.path.expanduser(target))
        return Media("local:" + os.path.basename(path), os.path.basename(path),
                     path, path if cfg.audio else None, probe_duration(path), [])

    # One extraction, not two. Asking separately for video and then audio
    # ran the whole fallback ladder twice, doubled the time, and doubled the
    # chance of hitting YouTube's intermittent failures - which is exactly
    # how it broke: the video came back fine and the audio pass died.
    #
    # A pre-muxed format is preferred so there is nothing to merge; the
    # single file is handed to both the frame decoder and the audio player,
    # the same way a local file already works.
    if cfg.audio:
        want = ("w[height<=%d]/wv*[height<=%d]+wa/w[height<=%d]/worst"
                % (cfg.quality, cfg.quality, cfg.quality))
    else:
        want = ("wv*[height<=%d]/w*[height<=%d]/worst"
                % (cfg.quality, cfg.quality))

    report("downloading \u2026")
    vopts = {
        "quiet": True, "no_warnings": True, "logger": YT_LOG,
        "outtmpl": os.path.join(workdir, "media.%(ext)s"),
        "format": want,
        "merge_output_format": "mp4",
    }
    info, ydl = _extract(vopts, target, True, report)
    video = ydl.prepare_filename(info)
    if not os.path.exists(video):
        # A merge renames the file; find whatever actually landed.
        for name in sorted(os.listdir(workdir)):
            if name.startswith("media."):
                video = os.path.join(workdir, name)
                break
    title = info.get("title", "video")
    duration = info.get("duration") or 0
    key = info.get("id") or title

    audio = video if (cfg.audio and has_audio(video)) else None

    cues = []
    if cfg.subs:
        report("fetching subtitles \u2026")
        try:
            sopts = {
                "quiet": True, "no_warnings": True, "logger": YT_LOG,
                "skip_download": True,
                "writesubtitles": True, "writeautomaticsub": True,
                "subtitleslangs": ["en", "en-orig", "pl"],
                "subtitlesformat": "vtt",
                "outtmpl": os.path.join(workdir, "sub.%(ext)s"),
            }
            _extract(sopts, target, True, None, retry=False)
            for name in sorted(os.listdir(workdir)):
                if name.startswith("sub.") and name.endswith(".vtt"):
                    cues = parse_vtt(os.path.join(workdir, name))
                    if cues:
                        break
        except Exception:
            cues = []

    return Media(key, title, video, audio, duration, cues,
                 info.get("channel") or info.get("uploader") or "",
                 info.get("channel_url") or "")


# ==========================================================================
# Decoding
# ==========================================================================

def filter_chain(width, height, fps, mode, cfg):
    """One filter string. Quantisation, dithering, sharpening and the cell
    aspect correction all happen here - ffmpeg does them far faster than a
    Python loop, and the renderer stops caring."""
    parts = []
    fix = aspect_fix(mode, cfg.cell_aspect)
    if abs(fix - 1.0) > 0.01:
        parts.append("scale=iw*%.4f:ih" % fix)
    parts.append("fps=%d" % fps)

    if cfg.sharpen:
        # Downscaling this far throws away edges; put some back first.
        parts.append("unsharp=5:5:%.2f:5:5:0.0" % cfg.sharpen)

    parts.append("scale=%d:%d:force_original_aspect_ratio=decrease:flags=%s"
                 % (width, height, cfg.scaler))
    parts.append("pad=%d:%d:(ow-iw)/2:(oh-ih)/2" % (width, height))

    if cfg.contrast:
        parts.append("eq=contrast=%.2f" % cfg.contrast)

    if mode in ("ascii", "braille"):
        if cfg.dither:
            parts.append("format=monob")     # swscale dithers to 1-bit
        parts.append("format=gray")
    elif cfg.quant > 1:
        step = cfg.quant
        expr = "trunc(val/%d)*%d+%d" % (step, step, step // 2)
        parts.append("lutrgb=r=%s:g=%s:b=%s" % (expr, expr, expr))
    return ",".join(parts)


class Decoder:
    """Produces the raw frame file without making you wait for all of it.

    PC: one ffmpeg writes the whole file while we read it as it grows.
    a-Shell: no fork/exec, so decode a slice at a time on demand and let
    playback stall briefly at the boundary.
    """

    def __init__(self, src, workdir, width, height, fps, mode, cfg, duration):
        self.src = src
        self.width = width
        self.height = height
        self.fps = fps
        self.mode = mode
        self.cfg = cfg
        self.duration = duration or 0.0
        self.channels = 1 if mode in ("ascii", "braille") else 3
        self.frame_bytes = width * height * self.channels
        self.path = os.path.join(workdir, "frames.raw")
        self.pix = "gray" if self.channels == 1 else "rgb24"
        self.vf = filter_chain(width, height, fps, mode, cfg)
        self.proc = None
        self.done = False
        self.slices = 0
        self.slice_frames = {}      # slice index -> (first frame, count)
        self.error = None
        self.expected = int(math.ceil(self.duration * fps)) if self.duration else 0

    def projected_bytes(self):
        return self.expected * self.frame_bytes

    def check_space(self, target_dir):
        need = self.projected_bytes()
        if not need:
            return None
        try:
            free = shutil.disk_usage(target_dir).free
        except Exception:
            return None
        return (need, free) if need > free * 0.8 else None

    def start(self):
        if HAVE_SUBPROCESS:
            args = ["ffmpeg", "-nostdin", "-v", "error", "-y",
                    "-i", self.src, "-an",
                    "-vf", self.vf, "-f", "rawvideo", "-pix_fmt", self.pix,
                    self.path]
            try:
                self.proc = subprocess.Popen(
                    args, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except FileNotFoundError:
                die("ffmpeg not found on PATH")
        else:
            open(self.path, "wb").close()
            self._slice()

    def _slice_path(self, index):
        return "%s.s%03d" % (self.path, index)

    def _decode_slice(self, index):
        """Decode one slice to its own file. Returns frames produced."""
        start = index * self.cfg.chunk
        if self.duration and start >= self.duration:
            return 0
        target = self._slice_path(index)
        if os.path.exists(target):
            return os.path.getsize(target) // self.frame_bytes
        part = target + ".part"
        code = run_ffmpeg([
            "-v", "error", "-y", "-ss", "%d" % start, "-t", "%d" % self.cfg.chunk,
            "-i", self.src, "-an", "-vf", self.vf, "-f", "rawvideo",
            "-pix_fmt", self.pix, part,
        ])
        if code != 0 or not os.path.exists(part):
            return 0
        size = os.path.getsize(part)
        if size == 0:
            os.remove(part)
            return 0
        os.replace(part, target)
        # A slice starts at a whole second, so its first frame index is
        # fixed by the fps - no need to count what came before it.
        self.slice_frames[index] = (index * self.cfg.chunk * self.fps,
                                    size // self.frame_bytes)
        return size // self.frame_bytes

    def _slice(self):
        """Decode the next slice in order. The sequential path."""
        index = self.slices
        frames = self._decode_slice(index)
        if not frames:
            if self.available() == 0 and not self.slice_frames:
                self.error = "ffmpeg failed on slice %d" % index
            self.done = True
            return
        self.slices = index + 1
        if self.duration and self.slices * self.cfg.chunk >= self.duration:
            self.done = True

    def locate(self, frame):
        """Which slice file holds this frame, and where in it.

        The old design appended every slice to one file, so jumping to the
        end of a long video meant decoding all of it first. Slices are now
        separate files keyed by their start time, so a seek decodes exactly
        the one slice it lands in.
        """
        if self.proc is not None or not self.slice_frames:
            return None
        per = self.cfg.chunk * self.fps
        index = frame // per
        entry = self.slice_frames.get(index)
        if not entry:
            return None
        start, count = entry
        offset = frame - start
        if not 0 <= offset < count:
            return None
        return self._slice_path(index), offset

    def want(self, frame):
        """Make `frame` available, decoding just the slice it falls in."""
        if self.proc is not None:
            self.poll()
            return frame < self.available()
        if self.locate(frame):
            return True
        per = self.cfg.chunk * self.fps
        index = frame // per
        if self.duration and index * self.cfg.chunk >= self.duration:
            return False
        if self._decode_slice(index):
            if index >= self.slices:
                self.slices = index + 1
            self.done = False
            return bool(self.locate(frame))
        return False

    def available(self):
        if self.proc is not None or not self.slice_frames:
            try:
                return os.path.getsize(self.path) // self.frame_bytes
            except OSError:
                return 0
        # Contiguous run from zero, which is what the progress bar means.
        per = self.cfg.chunk * self.fps
        total = 0
        index = 0
        while index in self.slice_frames:
            total = index * per + self.slice_frames[index][1]
            index += 1
        return total

    def poll(self):
        if self.proc is not None and self.proc.poll() is not None:
            if self.proc.returncode != 0 and self.available() == 0:
                try:
                    msg = (self.proc.stderr.read() or b"").decode("utf8", "replace")
                except Exception:
                    msg = ""
                lines = [l for l in msg.strip().splitlines() if l.strip()]
                self.error = lines[-1] if lines else \
                    "ffmpeg exited %d" % self.proc.returncode
            self.done = True
            self.proc = None

    def ensure(self, frame):
        """Make sure `frame` exists, decoding more where we can."""
        self.poll()
        if frame < self.available() or self.locate(frame):
            return True
        if self.proc is not None:
            return False
        return self.want(frame)

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                pass
            self.proc = None


# -- cache -----------------------------------------------------------------

def cache_name(key, width, height, fps, mode, cfg):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(key))[:60]
    extra = "dither" if (mode == "ascii" and cfg.dither) else "q%d" % cfg.quant
    thr = "t%s" % (cfg.cut() if cfg.cut() is not None else "n")
    return "%s-%dx%d-%dfps-%s-%s-%s-a%.2f.raw" % (
        safe, width, height, fps, mode, extra, thr, cfg.cell_aspect)


def cache_meta_path(path):
    return path + ".json"


def cache_lookup(name, frame_bytes=0, expect=None):
    """A cached file is only trusted if its sidecar agrees with it.

    The filename encodes the settings, but a filename is a promise, not
    evidence: a half-written or truncated file has the same name as a good
    one. The sidecar records the frame size and count, and the file has to
    still be exactly that long.
    """
    path = os.path.join(CACHE, name)
    meta_path = cache_meta_path(path)
    if not (os.path.isfile(path) and os.path.isfile(meta_path)):
        return None
    try:
        with open(meta_path) as fh:
            meta = json.load(fh)
        size = os.path.getsize(path)
        if meta.get("bytes") != size:
            return None
        if frame_bytes and size % frame_bytes:
            return None
        if frame_bytes and meta.get("frame_bytes") not in (None, frame_bytes):
            return None
        if expect and meta.get("source") not in (None, expect):
            return None
        os.utime(path, None)
        return path
    except Exception:
        return None


def cache_store(src, name, limit_mb, frame_bytes=0, source=None):
    if limit_mb <= 0:
        return None
    try:
        os.makedirs(CACHE, exist_ok=True)
        dst = os.path.join(CACHE, name)
        shutil.move(src, dst)
        size = os.path.getsize(dst)
        with open(cache_meta_path(dst), "w") as fh:
            json.dump({"bytes": size, "frame_bytes": frame_bytes,
                       "frames": size // frame_bytes if frame_bytes else None,
                       "source": source, "when": time.time()}, fh)
        cache_trim(limit_mb)
        return dst
    except Exception:
        return None


def cache_trim(limit_mb):
    try:
        files = []
        for name in os.listdir(CACHE):
            if name.endswith(".json") or name.endswith(".ok"):
                continue
            if name.startswith("thumb-"):
                continue          # tiny, and losing them just means refetching
            path = os.path.join(CACHE, name)
            files.append((os.path.getmtime(path), os.path.getsize(path), path))
        total = sum(f[1] for f in files)
        files.sort()
        while total > limit_mb * 1048576 and files:
            _, size, path = files.pop(0)
            os.remove(path)
            for extra in (path + ".json", path + ".ok"):
                if os.path.exists(extra):
                    os.remove(extra)
            total -= size
    except Exception:
        pass


def cache_size_mb():
    try:
        return sum(os.path.getsize(os.path.join(CACHE, n))
                   for n in os.listdir(CACHE)) / 1048576.0
    except Exception:
        return 0.0


class FrameReader:
    """Reads a frame by index, whichever file it happens to live in."""

    def __init__(self, decoder):
        self.decoder = decoder
        self._path = None
        self._fh = None

    def _open(self, path):
        if path != self._path:
            self.close()
            self._fh = open(path, "rb")
            self._path = path
        return self._fh

    def read(self, frame):
        dec = self.decoder
        size = dec.frame_bytes
        where = dec.locate(frame)
        if where:
            path, offset = where
            fh = self._open(path)
            fh.seek(offset * size)
        else:
            if frame >= dec.available():
                return None
            fh = self._open(dec.path)
            fh.seek(frame * size)
        buf = fh.read(size)
        return buf if len(buf) == size else None

    def close(self):
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass
        self._fh = None
        self._path = None


# ==========================================================================
# Rendering
# ==========================================================================

def _cube(value):
    return int(value * 5 / 255.0 + 0.5)


def _ansi256(r, g, b):
    if abs(r - g) < 12 and abs(g - b) < 12 and abs(r - b) < 12:
        grey = 232 + min(23, int(r * 23 / 255.0 + 0.5))   # 24-step grey ramp
        return grey
    return 16 + 36 * _cube(r) + 6 * _cube(g) + _cube(b)


_COLOUR_CACHE = {}


def colour_code(r, g, b, depth, background=False):
    """ANSI colour escape, memoised.

    Quantisation means a frame only holds a few dozen distinct colours, so
    the cache hits almost every time and we stop reformatting strings for
    every cell of every frame.
    """
    key = (r, g, b, depth, background)
    hit = _COLOUR_CACHE.get(key)
    if hit is not None:
        return hit
    if depth == "256":
        code = "\x1b[%d;5;%dm" % (48 if background else 38, _ansi256(r, g, b))
    else:
        code = "\x1b[%d;2;%d;%d;%dm" % (48 if background else 38, r, g, b)
    if len(_COLOUR_CACHE) < 100000:
        _COLOUR_CACHE[key] = code
    return code


def ascii_table(threshold):
    if threshold is not None:
        return bytes((0x40 if v >= threshold else 0x20) for v in range(256))
    top = len(RAMP) - 1
    return bytes(ord(RAMP[v * top // 255]) for v in range(256))


def render_lines(buf, width, height, mode, table, depth="truecolor"):
    """Draw one frame as a list of lines.

    width and height are in source pixels. How many pixels go into a cell
    depends on the mode: 1x1 for the flat modes, 1x2 for half-blocks,
    2x2 for quadrants, 2x4 for braille.
    """
    if mode == "ascii":
        return [buf[y * width:(y + 1) * width].translate(table).decode("ascii")
                for y in range(height)]
    if mode == "blocks":
        return _render_blocks(buf, width, height, depth)
    if mode == "quad":
        return _render_quad(buf, width, height, depth)
    if mode == "braille":
        return _render_braille(buf, width, height, table, depth)
    return _render_flat(buf, width, height, mode == "squares", table, depth)


def _render_flat(buf, width, height, solid, table, depth):
    """One pixel per cell: squares and coloured characters."""
    lines = []
    for y in range(height):
        base = y * width * 3
        out = []
        prev = None
        for x in range(width):
            i = base + x * 3
            r, g, b = buf[i], buf[i + 1], buf[i + 2]
            glyph = table[(r * 299 + g * 587 + b * 114) // 1000]
            if glyph == 0x20:
                out.append(" ")
                prev = None
                continue
            if (r, g, b) != prev:
                out.append(colour_code(r, g, b, depth))
                prev = (r, g, b)
            out.append(FULL_BLOCK if solid else chr(glyph))
        out.append(OFF)
        lines.append("".join(out))
    return lines


def _render_blocks(buf, width, height, depth):
    """Two pixel rows per cell, upper-half block."""
    lines = []
    for y in range(0, height - 1, 2):
        top = y * width * 3
        bot = (y + 1) * width * 3
        out = []
        prev_fg = prev_bg = None
        for x in range(width):
            i = top + x * 3
            j = bot + x * 3
            fg = (buf[i], buf[i + 1], buf[i + 2])
            bg = (buf[j], buf[j + 1], buf[j + 2])
            if fg != prev_fg:
                out.append(colour_code(fg[0], fg[1], fg[2], depth))
                prev_fg = fg
            if bg != prev_bg:
                out.append(colour_code(bg[0], bg[1], bg[2], depth, True))
                prev_bg = bg
            out.append(UPPER_HALF)
        out.append(OFF)
        lines.append("".join(out))
    return lines


def _render_quad(buf, width, height, depth):
    """Four pixels per cell. Twice the horizontal detail of half-blocks.

    A cell has two colours to spend on four pixels, so it takes the
    brightest pixel as foreground and the darkest as background and picks
    the quadrant glyph from which pixels sit above the midpoint. Picking
    extremes rather than averaging the two groups looks the same at this
    size and is much cheaper - averaging cost 10.5 ms a frame, this is
    about a third of that.
    """
    lines = []
    row3 = width * 3
    quads = QUADRANTS
    code = colour_code
    for cy in range(0, height - 1, 2):
        out = []
        add = out.append
        prev_fg = prev_bg = None
        base = cy * row3
        lower = base + row3
        for cx in range(0, width - 1, 2):
            a = base + cx * 3
            b = a + 3
            c = lower + cx * 3
            d = c + 3

            ra, ga, ba = buf[a], buf[a + 1], buf[a + 2]
            rb, gb, bb = buf[b], buf[b + 1], buf[b + 2]
            rc, gc, bc = buf[c], buf[c + 1], buf[c + 2]
            rd, gd, bd = buf[d], buf[d + 1], buf[d + 2]

            la = ra * 299 + ga * 587 + ba * 114
            lb = rb * 299 + gb * 587 + bb * 114
            lc = rc * 299 + gc * 587 + bc * 114
            ld = rd * 299 + gd * 587 + bd * 114

            hi = la
            fr, fg_, fb = ra, ga, ba
            if lb > hi:
                hi, fr, fg_, fb = lb, rb, gb, bb
            if lc > hi:
                hi, fr, fg_, fb = lc, rc, gc, bc
            if ld > hi:
                hi, fr, fg_, fb = ld, rd, gd, bd

            lo = la
            br, bg_, bb2 = ra, ga, ba
            if lb < lo:
                lo, br, bg_, bb2 = lb, rb, gb, bb
            if lc < lo:
                lo, br, bg_, bb2 = lc, rc, gc, bc
            if ld < lo:
                lo, br, bg_, bb2 = ld, rd, gd, bd

            mid = (hi + lo) >> 1
            bits = 0
            if la > mid:
                bits = 1
            if lb > mid:
                bits |= 2
            if lc > mid:
                bits |= 4
            if ld > mid:
                bits |= 8
            if hi == lo:
                bits = 15                  # flat cell: solid, glyph is moot

            fore = (fr, fg_, fb)
            back = (br, bg_, bb2)
            if fore != prev_fg:
                add(code(fr, fg_, fb, depth))
                prev_fg = fore
            if back != prev_bg:
                add(code(br, bg_, bb2, depth, True))
                prev_bg = back
            add(quads[bits])
        add(OFF)
        lines.append("".join(out))
    return lines


def _render_braille(buf, width, height, table, depth):
    """Eight pixels per cell. The most detail available in a terminal.

    One colour per cell, so it trades colour fidelity for resolution -
    which is the right trade for line art, text and silhouette animation.
    """
    lines = []
    grey = len(buf) == width * height
    step = 1 if grey else 3
    row = width * step
    cut = None
    for value in range(256):                 # reuse the ascii threshold
        if table[value] != 0x20:
            cut = value
            break
    if cut is None:
        cut = 128

    for cy in range(0, height - 3, 4):
        out = []
        prev = None
        base = cy * row
        for cx in range(0, width - 1, 2):
            bits = 0
            lit = []
            for dx in (0, 1):
                for dy in (0, 1, 2, 3):
                    i = base + dy * row + (cx + dx) * step
                    if grey:
                        lum = buf[i]
                        pixel = (lum, lum, lum)
                    else:
                        pixel = (buf[i], buf[i + 1], buf[i + 2])
                        lum = (pixel[0] * 299 + pixel[1] * 587
                               + pixel[2] * 114) // 1000
                    if lum >= cut:
                        bits |= BRAILLE_BITS[dx][dy]
                        lit.append(pixel)
            if bits and not grey:
                colour = (sum(p[0] for p in lit) // len(lit),
                          sum(p[1] for p in lit) // len(lit),
                          sum(p[2] for p in lit) // len(lit))
                if colour != prev:
                    out.append(colour_code(colour[0], colour[1], colour[2], depth))
                    prev = colour
            out.append(chr(0x2800 + bits))
        out.append(OFF)
        lines.append("".join(out))
    return lines


def rendered_rows(mode, height):
    return height // SUBPIXELS[mode][1]


def pixel_size(mode, cells_across, cells_down):
    """Cells to source pixels."""
    across, down = SUBPIXELS[mode]
    return cells_across * across, cells_down * down


def aspect_fix(mode, cell_aspect):
    """How much to widen the source so square things look square.

    A cell is about `cell_aspect` times taller than wide. A mode that packs
    `down` pixels vertically and `across` horizontally makes each pixel
    cell_aspect * across / down as tall as it is wide.
    """
    across, down = SUBPIXELS[mode]
    return cell_aspect * across / float(down)


class Painter:
    """Writes only the lines that changed.

    a-Shell draws through hterm, a JavaScript terminal in a WebView, so a
    frame costs roughly its byte count, and full repaints are what made
    this feel laggy.

    It repaints everything periodically anyway: it can only diff against
    its own last frame, so anything else that writes to the screen would
    otherwise stay there until that line happened to change.
    """

    def __init__(self, top_row, repaint_every=100):
        self.top = top_row
        self.prev = []
        self.sent = 0
        self.count = 0
        self.every = repaint_every

    def paint(self, lines):
        self.count += 1
        force = self.every and self.count % self.every == 0
        prev = [] if force else self.prev
        same = len(prev) == len(lines)
        out = []
        for i, line in enumerate(lines):
            if same and prev[i] == line:
                continue
            out.append("\x1b[%d;1H\x1b[K" % (self.top + i))
            out.append(line)
        self.prev = lines
        payload = "".join(out)
        self.sent += len(payload)
        return payload

    def reset(self):
        self.prev = []


def subtitle_lines(cues, when, cols):
    if not cues:
        return []
    for start, end, text in cues:
        if start <= when <= end:
            rows, line = [], ""
            for word in text.split():
                if len(line) + len(word) + 1 > cols - 4:
                    rows.append(line)
                    line = word
                else:
                    line = (line + " " + word).strip()
            if line:
                rows.append(line)
            return [r.center(cols - 2) for r in rows[-2:]]
        if start > when:
            break
    return []


# ==========================================================================
# Playback
# ==========================================================================

def interrupt_menu(keys, clock, cfg, length):
    """What Ctrl-C does during playback, when the menu is enabled.

    It is a deliberate surprise - Ctrl-C normally kills a program - so it
    says plainly what happened and how to actually quit. Turn it off in
    settings ("ctrl-c") and Ctrl-C stops the video instead.
    """
    keys.restore()
    emit(OFF + "\x1b[?25h\n")
    was_paused = clock.paused
    if not was_paused:
        clock.toggle()
    while True:
        print()
        print("  " + WARN + "\u26a0 ctrl-c did not quit \u2014 it paused the "
              "video and opened this." + OFF)
        print("  " + DIM + "press q here to actually quit, or turn this off "
              "in settings (ctrl-c)." + OFF)
        print()
        print(DIM + "  paused at %s / %s  \u00b7  speed %s"
              % (clock_str(clock.time()), clock_str(length),
                 speed_str(clock.rate)) + OFF)
        print(row("enter", "resume") + row("s", "seek") + row("n", "next")
              + row("q", "quit"))
        print(row("+ / -", "speed", "step through %s"
                  % ", ".join(speed_str(s) for s in SPEEDS)))
        print(row("x", "set speed", "type a number, e.g. 1.75"))
        print(row("f", "fast forward", "2x, or back to 1x"))
        print(row("<< / >>", "jump", "back or forward 30s"))
        choice = ask("\u203a ")
        if choice is None:
            return "quit"
        choice = choice.lower()
        if choice in ("", "p", "r"):
            if not was_paused:
                clock.toggle()
            keys.enable()
            return None
        if choice == "q":
            return "quit"
        if choice == "n":
            return "next"
        if choice == "s":
            where = ask("seek to seconds, or +/-N \u203a ") or ""
            try:
                if where.startswith(("+", "-")):
                    clock.goto(clock.time() + float(where))
                else:
                    clock.goto(float(where))
            except ValueError:
                continue
        elif choice in ("+", "faster"):
            clock.faster()
        elif choice in ("-", "slower"):
            clock.slower()
        elif choice == "f":
            clock.set_rate(1.0 if clock.rate > 1.01 else 2.0)
        elif choice in ("x", "speed"):
            value = ask("speed 0.25 to 4, or 1 for normal \u203a ")
            try:
                clock.set_rate(float(value))
            except (TypeError, ValueError):
                continue
        elif choice == ">>":
            clock.nudge(30)
        elif choice == "<<":
            clock.nudge(-30)


def play(decoder, media, cfg, cols, start_at=0.0, recorder=None, queue_info="",
         use_keys=True):
    """Returns (result, position, frames, bytes).
    result is one of end / quit / next / prev / resize."""
    fps = cfg.fps
    mode = cfg.mode
    frame_bytes = decoder.frame_bytes
    table = ascii_table(cfg.cut())

    sound = None
    if media.audio:
        sound = make_sound(media.audio, media.duration, cfg.backend)
        if not sound.ready:
            status("no audio: " + clip(sound.error, cols - 14), WARN)
            time.sleep(1.2)

    clock = Clock(sound, cfg.latency)
    if start_at > 0.5:
        clock.goto(start_at)

    length = media.duration or (decoder.expected / float(fps) if decoder.expected else 0)
    if sound and sound.ready:
        length = sound.duration() or length

    term_rows = max(12, term_size()[1])
    body_rows = rendered_rows(mode, decoder.height)
    painter = Painter(2)
    bar_row = 2 + body_rows + 1
    sub_row = bar_row + 2
    interval = 1.0 / fps
    shown = -1
    last_bar = -1
    painted = 0
    rate = 0.0
    window_frames = 0
    window_start = time.monotonic()
    result = "end"
    last_subs = None
    lost_audio = False
    learned_keys = [False]
    hint_until = 0.0
    speed_note = False
    told_speed = False
    hint_checked = False
    RESIZED[0] = False

    title_room = cols - (len(queue_info) + 4 if queue_info else 2)
    header = " " + BOLD + clip(media.title, title_room) + OFF
    if queue_info:
        header += DIM + "  " + queue_info + OFF

    # The typing strip needs at least one usable row, and the grid maths
    # can put sub_row at the very bottom on a short screen.
    prompt_row = min(sub_row + 2, max(3, term_rows - 1))
    emit("\x1b[r")                 # drop anything the last video set up
    if not use_keys:
        # Typing happens on the screen at the same time as rendering. Two
        # things make that survivable: a scroll region so pressing Enter
        # scrolls only the bottom strip instead of the picture, and
        # save/restore around every frame so the cursor goes back to the
        # middle of whatever is half-typed.
        emit("\x1b[2J\x1b[%d;%dr\x1b[%d;1H" % (prompt_row, term_rows,
                                                prompt_row))
        emit("\x1b7\x1b[1;1H" + header + "\x1b8")
    else:
        emit("\x1b[2J\x1b[?25l\x1b[1;1H" + header)

    keys = Keys()
    if use_keys:
        keys.enable()      # starts the reader thread on first use
    # In typed-command mode the main thread owns stdin through input(), so
    # no reader is started here. Two things reading stdin means one of them
    # silently swallows the line the other is waiting for.
    hint = (" type: p pause  q quit  + - speed  f 2x  s 90 / s +30 seek"
            "  n next"
            if not use_keys else
            " space pause  \u2190 \u2192 seek  - + speed  f 2x  n p queue"
            "  q quit"
            + ("  \u00b7 ctrl-c menu" if cfg.ctrl_c_menu else "")
            if keys.ok
            else " no key control (%s) \u00b7 ctrl-c to stop"
                 % (keys.reason or "terminal refused raw mode"))
    now_playing = NowPlaying(sound) if (sound and getattr(sound, "ready", False)) else None
    pump = pump_runloop()
    reader = FrameReader(decoder)
    base_hint = hint
    position = start_at
    take_commands()        # drop anything typed at the previous video

    # Ctrl-C stops the video and returns to the menu. Handled through a
    # flag rather than an exception so the terminal is always put back.
    interrupted = [False]

    def on_sigint(_sig, _frame):
        interrupted[0] = True

    # Only the main thread may install handlers. In typed-command mode
    # play() runs on a worker, so there is simply no handler here - and the
    # restore below must not run either, or it raises inside `finally` and
    # takes the whole exit path with it.
    previous_sigint = None
    try:
        previous_sigint = signal.signal(signal.SIGINT, on_sigint)
    except Exception:
        previous_sigint = None

    try:
        while True:
            if RESIZED[0]:
                result = "resize"
                break

            if interrupted[0]:
                interrupted[0] = False
                if not cfg.ctrl_c_menu:
                    result = "quit"
                    break
                action = interrupt_menu(keys, clock, cfg, length)
                painter.reset()
                shown = -1
                last_bar = -1
                emit("\x1b[2J\x1b[?25l\x1b[1;1H" + header)
                if action:
                    result = action
                    break
                continue

            # Every key waiting, not one per frame.
            stop = None
            pressed = keys.drain()
            if pressed:
                KEYS_SEEN[0] = True
                if not learned_keys[0]:
                    learned_keys[0] = True
            for key in pressed + take_commands():
                if key.startswith("seek:") or key.startswith("speed:"):
                    what, _, value = key.partition(":")
                    try:
                        number = float(value)
                    except ValueError:
                        continue
                    if what == "speed":
                        clock.set_rate(number)
                        speed_note = clock.audio_dropped
                    elif value.startswith(("+", "-")):
                        clock.nudge(number)
                    else:
                        clock.goto(number)
                    shown = -1
                    last_bar = -1
                    continue
                if key == "q":
                    stop = "quit"
                elif key == "n":
                    stop = "next"
                elif key == "p":
                    stop = "prev"
                elif key == "space" or key == "k":
                    clock.toggle()
                    last_bar = -1
                elif key in ("left", ","):
                    clock.nudge(-5)
                    shown = -1
                elif key in ("right", "."):
                    clock.nudge(5)
                    shown = -1
                elif key in ("down", "j"):
                    clock.nudge(-30)
                    shown = -1
                elif key in ("up", "l"):
                    clock.nudge(30)
                    shown = -1
                elif key in ("+", "="):
                    clock.faster()
                    last_bar = -1
                    speed_note = clock.audio_dropped
                elif key == "-":
                    clock.slower()
                    last_bar = -1
                    speed_note = clock.audio_dropped
                elif key == "0":
                    clock.set_rate(1.0)
                    last_bar = -1
                    speed_note = clock.audio_dropped
                elif key == "f":
                    # Toggle fast-forward, rather than creeping up a step.
                    clock.set_rate(1.0 if clock.rate > 1.01 else 2.0)
                    last_bar = -1
                    speed_note = clock.audio_dropped
                elif key == "[":
                    cfg.subs = not cfg.subs
                    last_subs = None
                    painter.reset()
                elif key == "r":
                    painter.reset()          # force a clean repaint
            if stop:
                result = stop
                break

            if clock.check():
                speed_note = True           # audio could not keep up
            position = clock.time()
            now = position
            wanted = int(now * fps)
            known = decoder.expected or decoder.available()

            if now_playing:
                now_playing.update(media.title, media.channel, now, length,
                                   0.0 if clock.paused else 1.0)
            pump()

            if LIMIT[0] and now >= LIMIT[0]:
                result = "end"
                break
            if known and wanted >= known and decoder.done:
                break
            if clock.finished() and wanted <= shown:
                if length and now < length - 1.0:
                    # The sound died early; the picture should not die too.
                    clock.disown()
                    if not lost_audio:
                        lost_audio = True
                        emit("\x1b[%d;1H\x1b[K" % (bar_row + 1) + WARN
                                  + " audio stopped - continuing without it"
                                  + OFF)
                    continue
                break

            if wanted <= shown:
                if int(now) != last_bar:
                    last_bar = int(now)
                    frac = (decoder.available() / float(known)) if known else 1.0
                    bar = ("\x1b[%d;1H\x1b[K" % bar_row
                           + progress_bar(now, length, cols, clock.paused,
                                          rate, frac, clock.rate))
                    emit(("\x1b7" + bar + "\x1b8") if not use_keys else bar)
                time.sleep(interval / 4)
                continue

            have = decoder.locate(wanted) or wanted < decoder.available()
            if not have:
                decoder.poll()
                if decoder.done and not decoder.want(wanted):
                    break
                if decoder.proc is None:
                    # a-Shell: decoding blocks the process, so hold the
                    # audio rather than let it run away from the picture.
                    was_playing = not clock.paused
                    if was_playing:
                        clock.toggle()
                    emit("\x1b[%d;1H\x1b[K" % bar_row + DIM
                              + " buffering \u2026" + OFF)
                    made = decoder.want(wanted)   # just the slice we land in
                    keys.reapply()                # the child took the terminal
                    if was_playing:
                        clock.toggle()
                    painter.reset()
                    if not made:
                        # Past the end, or that slice has nothing in it.
                        decoder.done = True
                        break
                else:
                    time.sleep(0.05)
                continue

            buf = reader.read(wanted)
            if buf is None:
                time.sleep(0.02)          # growing mid-read; try again
                continue
            shown = wanted

            chunk = painter.paint(
                render_lines(buf, decoder.width, decoder.height, mode, table,
                         cfg.depth))

            subs = subtitle_lines(media.cues, now, cols) if cfg.subs else []
            if subs != last_subs:
                last_subs = subs
                chunk += "\x1b[%d;1H\x1b[K" % sub_row
                chunk += (ACCENT + subs[0] + OFF) if subs else ""
                chunk += "\x1b[%d;1H\x1b[K" % (sub_row + 1)
                chunk += (ACCENT + subs[1] + OFF) if len(subs) > 1 else ""

            if not use_keys and LAST_COMMAND[0]:
                # Echo how the typed line was read, so a command that does
                # nothing can be told apart from one that was misread. Held
                # for a few seconds - a one-frame flash is unreadable.
                hint = " " + LAST_COMMAND[0]
                hint_until = time.monotonic() + 4.0
                LAST_COMMAND[0] = ""
                last_bar = -1
            elif hint_until and time.monotonic() > hint_until:
                hint = base_hint
                hint_until = 0.0
                last_bar = -1
            if speed_note and not told_speed:
                told_speed = True
                hint = (" this audio cannot change speed - muted while not "
                        "at 1x")
                last_bar = -1
            if int(now) != last_bar:
                last_bar = int(now)
                frac = (decoder.available() / float(known)) if known else 1.0
                chunk += ("\x1b[%d;1H\x1b[K" % bar_row
                          + progress_bar(now, length, cols, clock.paused, rate,
                                         frac, clock.rate)
                          + "\x1b[%d;1H\x1b[K" % (bar_row + 1)
                          + DIM + clip(hint, cols - 1) + OFF)

            if chunk:
                emit(("\x1b7" + chunk + "\x1b8") if not use_keys else chunk)
                if recorder:
                    recorder.write(chunk)

            # If nothing has ever arrived from the keyboard, stop showing a
            # hint that promises keys work. Say what actually does.
            if (use_keys and not hint_checked and painted > fps * 3
                    and not keys.saw_any):
                hint_checked = True
                hint = (" keys are not reaching skjyt \u00b7 ctrl-c "
                        + ("opens a menu" if cfg.ctrl_c_menu else "stops it"))
                last_bar = -1

            painted += 1
            window_frames += 1
            span = time.monotonic() - window_start
            if span >= 1.0:
                rate = window_frames / span
                window_frames = 0
                window_start = time.monotonic()
    except KeyboardInterrupt:
        result = "quit"
    finally:
        if previous_sigint is not None:
            try:
                signal.signal(signal.SIGINT, previous_sigint)
            except Exception:
                pass
        try:
            position = clock.time()
        except Exception:
            pass
        pass
        reader.close()
        if now_playing:
            now_playing.clear()
        if sound:
            sound.stop()
        keys.restore()
        # Unconditional: an exception on any path must not leave the next
        # screen trapped in this video's scroll region.
        emit(OFF + "\x1b[r\x1b[?25h\x1b[%d;1H\n" % (sub_row + 2))

    return result, position, painted, painter.sent, learned_keys[0]


# ==========================================================================
# Record and replay
# ==========================================================================

class Recorder:
    def __init__(self, path):
        self.fh = open(path, "w", encoding="utf-8")
        self.t0 = time.monotonic()
        self.fh.write(json.dumps({"version": 1, "kind": "skjyt"}) + "\n")

    def write(self, payload):
        self.fh.write(json.dumps([time.monotonic() - self.t0, payload]) + "\n")

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass


def replay(path):
    try:
        fh = open(path, encoding="utf-8")
    except OSError as exc:
        die(str(exc))
    try:
        json.loads(fh.readline())
    except Exception:
        die("not a skjyt recording")
    sys.stdout.write("\x1b[2J\x1b[?25l")
    start = time.monotonic()
    try:
        for line in fh:
            try:
                when, payload = json.loads(line)
            except Exception:
                continue
            wait = when - (time.monotonic() - start)
            if wait > 0:
                time.sleep(wait)
            sys.stdout.write(payload)
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        fh.close()
        restore_terminal()


# ==========================================================================
# Benchmark
# ==========================================================================

def measure(mode, cfg, cells_across=60, cells_down=40, frames=16):
    """Render synthetic frames. Returns (ms, diff bytes, full bytes, cells)."""
    width, height = pixel_size(mode, cells_across, cells_down)
    channels = 1 if mode in ("ascii", "braille") else 3
    base = bytearray(width * height * channels)
    for i in range(len(base)):
        base[i] = (i * 7) % 256
    table = ascii_table(cfg.cut())

    clips = []
    for step in range(frames):
        frame = bytearray(base)
        for i in range(len(frame) // 3):
            frame[i] = (frame[i] + step * 11) % 256
        clips.append(bytes(frame))

    painter = Painter(2, repaint_every=0)
    full = 0
    t0 = time.perf_counter()
    for clip_ in clips:
        lines = render_lines(clip_, width, height, mode, table, cfg.depth)
        full += sum(len(l) + 6 for l in lines)
        painter.paint(lines)
    ms = (time.perf_counter() - t0) / frames * 1000
    return ms, painter.sent // frames, full // frames, cells_across * cells_down


def bench(cfg):
    cols, rows = term_size()
    print(BOLD + "skjyt benchmark" + OFF)
    print(DIM + "  python %s, %s, subprocess=%s"
          % (sys.version.split()[0], sys.platform, HAVE_SUBPROCESS) + OFF)
    print(DIM + "  terminal %dx%d, budget %d cells, %d fps, %s"
          % (cols, rows, cfg.cells, cfg.fps, cfg.depth) + OFF)
    print()
    print("  %-9s %8s %9s %11s %11s"
          % ("mode", "pixels", "ms/frame", "B/frame full", "B/frame diff"))
    for mode in MODES:
        across, down = SUBPIXELS[mode]
        ms, diff, full, cells = measure(mode, cfg)
        print("  %-9s %8s %9.2f %11d %11d"
              % (mode, "%dx" % (across * down), ms, full, diff))
    budget = 1048576 // max(1, cfg.fps)
    print()
    print(DIM + "  pixels = source pixels carried per character cell" + OFF)
    print(DIM + "  full = every line rewritten, diff = only changed lines" + OFF)
    print(DIM + "  stay under %d B/frame at %d fps to keep below 1 MB/s"
          % (budget, cfg.fps) + OFF)


# ==========================================================================
# Recommendations
#
# YouTube's own feed needs your account cookies, which a phone can't hand
# over. So the feed is built here instead, from what you actually watched:
# more from those channels, searches for words that keep coming up in the
# titles, channels you follow, and popular as filler. No API key, no login.
# ==========================================================================

STOPWORDS = set("""
a an the and or of to in on for with from by at is are was were be been it
its this that these those you your my our their his her not no as if then
than so but too very can will just now new old how what why who when where
official video music audio lyrics lyric full hd 4k live remix cover ft feat
part episode ep vol season trailer teaser shorts short clip best top
i w z na do nie to jest sie sie oraz jak co dla przez tego tym
""".split())

FEED_TTL = 6 * 3600
FEED_MAX = 24


def remember_watch(state, media, seconds):
    """One row per video watched for more than a few seconds."""
    if seconds < 15:
        return
    history = state.setdefault("history", [])
    key = str(media.key)
    history[:] = [h for h in history if h.get("id") != key]
    history.append({
        "id": key,
        "title": media.title,
        "channel": media.channel,
        "channel_url": media.channel_url,
        "when": time.time(),
        "watched": round(seconds, 1),
        "duration": media.duration or 0,
    })
    del history[:-200]


def _tokens(title):
    word = []
    out = []
    for ch in title.lower():
        if ch.isalnum():
            word.append(ch)
        else:
            if word:
                out.append("".join(word))
            word = []
    if word:
        out.append("".join(word))
    return [w for w in out if len(w) > 3 and w not in STOPWORDS and not w.isdigit()]


def muted(state):
    return state.setdefault("muted", {"channels": [], "words": []})


def mute_item(state, item):
    """Suppress the reason this showed up, not just the one video."""
    box = muted(state)
    label = item.get("channel") or ""
    why = item.get("why") or ""
    what = None
    if why.startswith("you follow ") or why.startswith("more from "):
        label = label or why.split(" ", 2)[-1]
    if "\u201c" in why and "\u201d" in why:            # because you watched "x"
        word = why.split("\u201c", 1)[1].split("\u201d", 1)[0]
        if word and word not in box["words"]:
            box["words"].append(word)
            what = "keyword %s" % word
    if not what and label:
        if label not in box["channels"]:
            box["channels"].append(label)
        what = "channel %s" % label
    state.pop("feed", None)                        # the cached feed is stale
    save_state(state)
    return what or "nothing to suppress"


def taste(state):
    """What the history says you like: (channels, keywords), both weighted."""
    history = state.get("history", [])
    now = time.time()
    channels = {}
    words = {}
    for record in history:
        age_days = max(0.0, (now - record.get("when", now)) / 86400.0)
        recency = 0.5 ** (age_days / 30.0)          # half-life of a month
        share = 1.0
        if record.get("duration"):
            share = min(1.0, record.get("watched", 0) / float(record["duration"]))
        weight = recency * (0.3 + share)

        name = record.get("channel") or ""
        if name:
            entry = channels.setdefault(name, {"weight": 0.0, "url": "", "seed": ""})
            entry["weight"] += weight * 2.0
            entry["url"] = entry["url"] or record.get("channel_url") or ""
            entry["seed"] = entry["seed"] or record.get("title", "")
        for token in set(_tokens(record.get("title", ""))):
            words[token] = words.get(token, 0.0) + weight

    ranked_channels = sorted(channels.items(), key=lambda kv: -kv[1]["weight"])
    ranked_words = sorted(words.items(), key=lambda kv: -kv[1])
    return ranked_channels, ranked_words


def _entry_to_item(entry, why, score):
    vid = entry.get("id")
    if not vid:
        return None
    url = entry.get("url") or ("https://www.youtube.com/watch?v=" + vid)
    return {
        "target": url,
        "url": url,
        "title": entry.get("title") or "?",
        "id": vid,
        "duration": entry.get("duration") or 0,
        "channel": entry.get("channel") or entry.get("uploader") or "",
        "thumbnails": entry.get("thumbnails") or [],
        "why": why,
        "score": score,
    }


def _flat(url, limit):
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "playlistend": limit}
    info, _ = _extract(opts, url, False)
    return [e for e in (info.get("entries") or []) if e.get("id")][:limit]


def build_feed(state, cfg, report=None):
    """Assemble and score the feed. Network-heavy, so it gets cached."""
    def say(text):
        if report:
            report(text)

    seen = {str(h.get("id")) for h in state.get("history", [])}
    box = muted(state)
    hidden_channels = {c.lower() for c in box["channels"]}
    hidden_words = {w.lower() for w in box["words"]}
    picked = {}

    def add(entry, why, score):
        item = _entry_to_item(entry, why, score)
        if not item or item["id"] in seen:
            return
        if (item["channel"] or "").lower() in hidden_channels:
            return
        title_words = set(_tokens(item["title"]))
        if title_words & hidden_words:
            return
        current = picked.get(item["id"])
        if current is None or current["score"] < score:
            picked[item["id"]] = item

    channels, words = taste(state)

    for row_ in following(state)[:6]:
        say("following: %s ..." % clip(row_.get("name", ""), 26))
        try:
            for n, entry in enumerate(channel_tab(row_["url"], "videos", 5)):
                add(entry, "you follow %s" % clip(row_.get("name", ""), 22),
                    9.0 - n * 0.1)
        except Exception:
            continue

    for name, info in channels[:4]:
        url = info.get("url")
        if not url or name.lower() in hidden_channels:
            continue
        say("more from %s ..." % clip(name, 30))
        try:
            for n, entry in enumerate(_flat(url.rstrip("/") + "/videos", 6)):
                add(entry, "more from %s" % clip(name, 22),
                    6.0 + info["weight"] - n * 0.1)
        except Exception:
            continue

    for token, weight in words[:5]:
        if token in hidden_words:
            continue
        say("because you watched ... %s" % token)
        try:
            for n, entry in enumerate(search(token, 5)):
                add(entry, "because you watched \u201c%s\u201d" % token,
                    3.0 + weight - n * 0.1)
        except Exception:
            continue

    if len(picked) < FEED_MAX:
        entries, label = popular("now", cfg, 15, say)
        for n, entry in enumerate(entries):
            add(entry, "popular \u00b7 %s" % clip(label, 24), 1.0 - n * 0.01)

    return sorted(picked.values(), key=lambda i: -i["score"])[:FEED_MAX]


def cached_feed(state, cfg, cols, force=False):
    blob = state.get("feed") or {}
    fresh = (not force and blob.get("items")
             and time.time() - blob.get("when", 0) < FEED_TTL)
    if fresh:
        return blob["items"]

    screen(cols, "building your feed \u2026")
    if not state.get("history") and not following(state):
        status("nothing watched yet, so this is just what's popular")
    items = build_feed(state, cfg, lambda m: status(m))
    if items:
        state["feed"] = {"when": time.time(), "items": items}
        save_state(state)
    return items


def feed_screen(state, cfg, cols, force=False):
    """Returns queue items, or None."""
    while True:
        try:
            items = cached_feed(state, cfg, cols, force)
        except Exception as exc:
            screen(cols, "could not build the feed")
            status(clip(str(exc), cols - 4), WARN)
            ask("enter to continue \u203a ")
            return None
        force = False

        if not items:
            screen(cols, "feed is empty")
            status("watch something first, then come back")
            ask("enter to continue \u203a ")
            return None

        age = int((time.time() - state.get("feed", {}).get("when", 0)) / 60)
        whys = {i["id"]: i.get("why", "") for i in items}
        result = pick_from(items, cols, cfg,
                           "for you \u00b7 %d picks \u00b7 %dm old" % (len(items), age),
                           extra_rows=(row("-", "less of this", "hide a source"),),
                           whys=whys)
        if result is None:
            return None
        if result == "refresh":
            force = True
            continue
        if result == "-":
            which = ask("number to see less of \u203a ")
            try:
                target = items[int(which) - 1]
            except (TypeError, ValueError, IndexError):
                continue
            status("hiding " + mute_item(state, target), DIM)
            time.sleep(0.7)
            force = True
            continue
        if isinstance(result, str):
            continue
        return result


# ==========================================================================
# Popular, and channels
#
# YouTube retired the single global Trending page in July 2025; what is left
# is category charts. So nothing here trusts one URL - each category has a
# list of candidates and the first that returns anything wins, with a plain
# search as the last resort. No sign-in anywhere: every one of these is a
# public page yt-dlp can read without cookies.
# ==========================================================================

POPULAR = ("now", "music", "gaming", "movies")

# feed/trending is gone - YouTube retired it in July 2025 and it now
# redirects to the home page, which yt-dlp reports as "channel/playlist does
# not exist". It stays in the list only in case some region still serves it.
POPULAR_PAGES = {
    "now": ("music", "gaming", "movies"),      # merged: see popular()
    "music": ("music",),
    "gaming": ("gaming",),
    "movies": ("movies",),
}


# Only used if every chart page fails. Deliberately describes the content
# rather than using the word "trending", which matches hashtag spam - the
# old seed returned twenty livestreams with #trending in the title.
POPULAR_SEEDS = {
    "now": "popular videos this week",
    "music": "official music video new",
    "gaming": "gameplay new",
    "movies": "official trailer new",
}


def popular_urls(category, region):
    tail = ("?gl=" + region) if region else ""
    base = "https://www.youtube.com/"
    pages = POPULAR_PAGES.get(category, ("music",))
    return [base + page + tail for page in pages] + [base + "feed/trending" + tail]


def popular(category, cfg, limit=20, report=None):
    """Returns (entries, source_label).

    "now" merges the category charts rather than searching, because the
    search fallback returns whatever is titled #trending - which in practice
    is livestream spam, not popular video.
    """
    def say(text):
        if report:
            report(text)

    pages = POPULAR_PAGES.get(category, ("music",))
    merged = []
    seen = set()
    per = max(4, limit // max(1, len(pages)))

    for page in pages:
        url = "https://www.youtube.com/%s%s" % (page, ("?gl=" + cfg.region)
                                                if cfg.region else "")
        say("%s \u2026" % page)
        try:
            entries = _flat(url, per)
        except Exception:
            continue
        for entry in entries:
            if entry.get("id") and entry["id"] not in seen:
                seen.add(entry["id"])
                merged.append(entry)

    if merged:
        return merged[:limit], " + ".join(pages)

    # Last resorts, in order of how much they can be trusted.
    say("charts empty, trying the trending page \u2026")
    try:
        entries = _flat("https://www.youtube.com/feed/trending"
                        + (("?gl=" + cfg.region) if cfg.region else ""), limit)
        if entries:
            return entries, "feed/trending"
    except Exception:
        pass

    say("searching instead \u2026")
    seed = POPULAR_SEEDS.get(category, category)
    try:
        return search(seed, limit), "search: " + seed
    except Exception:
        return [], "nothing reachable"


# -- channels --------------------------------------------------------------

CHANNEL_TABS = ("videos", "shorts", "streams", "playlists")


def channel_url(text):
    """Accepts a full URL, an @handle, or a bare name."""
    text = text.strip().rstrip("/")
    if is_url(text):
        for tab in CHANNEL_TABS:
            if text.endswith("/" + tab):
                text = text[: -(len(tab) + 1)]
        return text
    if not text.startswith("@"):
        text = "@" + text
    return "https://www.youtube.com/" + text


def channel_tab(url, tab, limit=30):
    return _flat(url.rstrip("/") + "/" + tab, limit)


def channel_search(url, query, limit=30):
    """Search within one channel. Public page, no sign-in."""
    return _flat("%s/search?query=%s"
                 % (url.rstrip("/"), urllib.parse.quote(query)), limit)


def following(state):
    return state.setdefault("following", [])


def follow(state, name, url):
    rows = following(state)
    url = url.rstrip("/")
    if any(r.get("url") == url for r in rows):
        return False
    rows.append({"name": name or url, "url": url})
    save_state(state)
    return True


def unfollow(state, needle):
    rows = following(state)
    needle = needle.lower()
    keep = [r for r in rows
            if needle not in r.get("name", "").lower()
            and needle not in r.get("url", "").lower()]
    dropped = len(rows) - len(keep)
    rows[:] = keep
    save_state(state)
    return dropped


def is_followed(state, url):
    url = url.rstrip("/")
    return any(r.get("url") == url for r in following(state))


# ==========================================================================
# Requirements and first-run setup
# ==========================================================================

def have_ffmpeg():
    """a-Shell's ffmpeg is built in and not on PATH, so ask it to run."""
    if HAVE_SUBPROCESS:
        return bool(shutil.which("ffmpeg"))
    return os.system("ffmpeg -version > /dev/null 2>&1") == 0


def have_ytdlp():
    """yt-dlp keeps its version in yt_dlp.version, not on the package."""
    try:
        import yt_dlp
    except Exception:
        return None
    for getter in (lambda: yt_dlp.version.__version__,
                   lambda: yt_dlp.__version__,
                   lambda: __import__("yt_dlp.version",
                                      fromlist=["__version__"]).__version__):
        try:
            value = getter()
            if value:
                return str(value)
        except Exception:
            continue
    return "yes"


def apple_audio_available():
    """Load the frameworks and look up the class, without playing anything."""
    try:
        for fw in AppleSound.FRAMEWORKS:
            ctypes.cdll.LoadLibrary(fw)
        libobjc = ctypes.util.find_library("objc") or "/usr/lib/libobjc.dylib"
        objc = ctypes.CDLL(libobjc)
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        return bool(objc.objc_getClass(b"AVAudioPlayer"))
    except Exception:
        return False


def truecolor():
    term = (os.environ.get("COLORTERM") or "").lower()
    if "truecolor" in term or "24bit" in term:
        return "yes"
    if IS_WINDOWS:
        return "probably (VT enabled)"
    return "unknown - if colours look wrong, use ascii mode"


def pip_install(package):
    """Returns None on success, else a message."""
    if HAVE_SUBPROCESS:
        code = subprocess.call([sys.executable, "-m", "pip", "install",
                                "--upgrade", package])
    else:
        # a-Shell: pure-python wheels only, so --no-deps matters.
        code = os.system("pip install --upgrade %s --no-deps" % package)
    return None if code == 0 else "pip exited %s" % code


def input_report():
    """Describe key input without touching stdin.

    Deliberately passive: an earlier version answered this by constructing
    a Keys, which started a reader thread that then stole the menu's input.
    """
    if IS_WINDOWS:
        try:
            import msvcrt  # noqa: F401
            return (True, "key input", "windows console", None)
        except Exception as exc:
            return (False, "key input", str(exc), None)
    bits = []
    ok = True
    try:
        tty_ok = os.isatty(0)
    except Exception:
        tty_ok = False
    bits.append("stdin is a tty" if tty_ok else "stdin is not a tty")
    if not tty_ok:
        ok = False
    for name in ("termios", "tty"):
        try:
            __import__(name)
        except Exception:
            bits.append("no " + name)
            ok = False
    return (ok, "key input", ", ".join(bits), None)


def requirements(cfg):
    """Returns a list of (ok, name, detail, fixable)."""
    out = []
    ok_py = sys.version_info >= (3, 7)
    out.append((ok_py, "python", sys.version.split()[0]
                + ("" if ok_py else "  needs 3.7+"), None))

    version = have_ytdlp()
    note = version or "not installed - this is the one hard requirement"
    if version and version != "yes":
        # yt-dlp versions are dates. An old one is the single most common
        # cause of "this video is not available" on a video that plays fine.
        digits = "".join(c for c in str(version) if c.isdigit())[:8]
        try:
            age = (time.time() - time.mktime(time.strptime(digits, "%Y%m%d"))) / 86400
            if age > 60:
                note += "  (%d days old - `skjyt update`)" % age
        except Exception:
            pass
    out.append((bool(version), "yt-dlp", note, None if version else "yt-dlp"))

    ff = have_ffmpeg()
    detail = "found" if ff else "missing"
    if not ff:
        detail += " - " + ("brew install ffmpeg" if sys.platform == "darwin"
                           else "apt install ffmpeg" if not IS_WINDOWS
                           else "get it from ffmpeg.org and put it on PATH")
    out.append((ff, "ffmpeg", detail, None))

    if cfg.audio:
        apple = apple_audio_available()
        ffplay = bool(shutil.which("ffplay")) if HAVE_SUBPROCESS else False
        if apple:
            out.append((True, "audio", "AVFoundation, in-process", None))
        elif ffplay:
            out.append((True, "audio", "ffplay", None))
        else:
            out.append((False, "audio", "no backend - video will be silent", None))
    else:
        out.append((True, "audio", "muted by choice", None))

    out.append((HOME_WRITABLE, "settings",
                SKJ_HOME if HOME_WRITABLE else SKJ_HOME + " NOT writable", None))

    cols, rows = term_size()
    roomy = cols >= 40 and rows >= 16
    out.append((roomy, "terminal", "%dx%d%s" % (cols, rows, "" if roomy
                else "  small - shrink the font, hide the keyboard"), None))

    out.append((True, "colour", truecolor(), None))

    try:
        free = shutil.disk_usage(SKJ_HOME if os.path.isdir(SKJ_HOME)
                                 else tempfile.gettempdir()).free
        enough = free > 300 * 1048576
        out.append((enough, "disk", "%d MB free" % (free // 1048576), None))
    except Exception:
        pass

    out.append((True, "subprocess", "yes, PC paths" if HAVE_SUBPROCESS
                else "no, a-Shell paths (chunked decode, os.system)", None))

    out.append(input_report())

    # Cheap guard against the mistake that broke the settings screen once:
    # a mode added to MODES but not to a table that is indexed by mode.
    gaps = []
    for label, table in (("SUBPIXELS", SUBPIXELS), ("MODE_BLURB", MODE_BLURB)):
        for mode in MODES:
            if mode not in table:
                gaps.append("%s/%s" % (label, mode))
    out.append((not gaps, "modes",
                "all %d described" % len(MODES) if not gaps
                else "missing " + ", ".join(gaps), None))
    return out


PRESETS = {
    "performance": {
        "mode": "ascii", "fps": 15, "quant": 32, "dither": False,
        "quality": 144, "thumbs": False, "depth": "256",
        "scaler": "bilinear", "sharpen": 0.0,
        "cells": 1200 if not HAVE_SUBPROCESS else 6000,
        "note": "plain text, one pixel a cell. Thirty times cheaper than "
                "anything with colour.",
    },
    "balanced": {
        "mode": "blocks", "fps": 15, "quant": 24, "dither": False,
        "quality": 240, "thumbs": True, "depth": "256",
        "scaler": "lanczos", "sharpen": 0.8,
        "cells": 2400 if not HAVE_SUBPROCESS else 10000,
        "note": "half-blocks: colour, two pixels a cell, 256-colour escapes "
                "to keep the byte count down.",
    },
    "quality": {
        "mode": "quad", "fps": 24, "quant": 8, "dither": False,
        "quality": 360, "thumbs": True, "depth": "truecolor",
        "scaler": "lanczos", "sharpen": 1.0,
        "cells": 4000 if not HAVE_SUBPROCESS else 16000,
        "note": "quadrants: four pixels a cell, same bytes as half-blocks "
                "for double the detail. Costs more Python time.",
    },
    "sharpest": {
        "mode": "braille", "fps": 15, "dither": True,
        "quality": 240, "thumbs": True, "depth": "256",
        "scaler": "lanczos", "sharpen": 1.2,
        "cells": 2000 if not HAVE_SUBPROCESS else 9000,
        "note": "braille: eight pixels a cell, the most a terminal can hold, "
                "but no colour. Best there is for line art and silhouettes.",
    },
}


def apply_preset(cfg, name):
    preset = PRESETS[name]
    for key, value in preset.items():
        if key != "note":
            setattr(cfg, key, value)
    cfg.sanitise()
    return preset


def autotune(cfg, report=None):
    """Measure this machine and pick a cell budget it can actually sustain.

    Two ceilings: how long Python takes to build a frame, and how many bytes
    the terminal has to swallow. Whichever is tighter wins.
    """
    def say(text):
        if report:
            report(text)

    say("measuring %s mode \u2026" % cfg.mode)
    ms, diff_bytes, _full, cells = measure(cfg.mode, cfg)

    interval_ms = 1000.0 / cfg.fps
    # Spend at most 40% of the frame on rendering; the rest is I/O and slack.
    time_ratio = (interval_ms * 0.40) / max(0.001, ms)

    # Bytes the terminal can take per frame. hterm in a WebView is far
    # slower than a native desktop terminal, so the budgets differ a lot.
    per_second = 350000 if not HAVE_SUBPROCESS else 4000000
    byte_ratio = (per_second / float(cfg.fps)) / max(1, diff_bytes)

    ratio = min(time_ratio, byte_ratio)
    limiter = "python render speed" if time_ratio < byte_ratio \
        else "terminal throughput"
    suggested = int(cells * ratio)

    # There is no point budgeting for more cells than the screen has.
    cols, rows = term_size()
    screen_cells = max(1, (cols - 1) * max(6, rows - 6))
    if screen_cells < suggested:
        limiter = "screen size (%dx%d) - it could take more" % (cols, rows)
        suggested = screen_cells
    suggested = max(600, min(suggested, 40000))

    say("%.2f ms and %d B per frame at %d cells" % (ms, diff_bytes, cells))
    return suggested, limiter, ms, diff_bytes


def setup_screen(cfg, cols, first_run=False):
    """Requirement check, preference, auto-tune. Returns True if saved."""
    while True:
        screen(cols, "setup" + (" \u00b7 first run" if first_run else ""))
        checks = requirements(cfg)
        missing = []
        for ok, name, detail, fix in checks:
            mark = (GOOD + "\u2713" + OFF) if ok else (WARN + "\u2717" + OFF)
            print("  %s %-11s %s%s%s" % (mark, name, DIM, clip(detail, cols - 18), OFF))
            if fix:
                missing.append(fix)
        print()

        print("  " + BOLD + "what matters more?" + OFF)
        for key in ("performance", "balanced", "quality", "sharpest"):
            preset = PRESETS[key]
            mine = " " + ACCENT + "\u2190 current" + OFF \
                if cfg.mode == preset["mode"] else ""
            letter = {"balanced": "b1", "sharpest": "s2"}.get(key, key[0])
            print(row(letter, key, "%s, %dfps, %d cells%s"
                      % (preset["mode"], preset["fps"], preset["cells"], mine)))
            print("      %s%s%s" % (DIM, clip(preset["note"], cols - 8), OFF))
        print()
        if missing:
            print(row("i", "install", "pip install " + ", ".join(missing)))
        print(row("c", "how to control", control_label(cfg, load_state())))
        print(row("t", "auto-tune", "measure this device and set the budget"))
        print(row("s", "settings", "every knob, individually"))
        print(row("b", "done" if not first_run else "skip", ""))

        pick = ask("\u203a ")
        if pick is None or pick in ("b", ""):
            problem = cfg.save()
            if problem:
                status("settings NOT saved: " + clip(problem, cols - 24), WARN)
                ask("enter to continue \u203a ")
                return False
            return True

        if pick == "c":
            order = ("auto", "keys", "line")
            cfg.control = order[(order.index(cfg.control) + 1) % len(order)]
            cfg.sanitise()
            cfg.save()
            continue

        if pick == "i" and missing:
            for package in missing:
                screen(cols, "installing " + package)
                problem = pip_install(package)
                status(("failed: " + problem) if problem else "installed",
                       WARN if problem else GOOD)
            ask("enter to continue \u203a ")
            continue

        if pick in ("p", "performance"):
            preset = apply_preset(cfg, "performance")
        elif pick in ("q", "quality"):
            preset = apply_preset(cfg, "quality")
        elif pick in ("b1", "balanced"):
            preset = apply_preset(cfg, "balanced")
        elif pick in ("s2", "sharpest"):
            preset = apply_preset(cfg, "sharpest")
        elif pick == "t":
            screen(cols, "auto-tune")
            suggested, limiter, ms, diff = autotune(cfg, lambda m: status(m))
            print()
            status("limited by %s" % limiter,
                   WARN if limiter.startswith("terminal") else DIM)
            status("suggested budget: %d cells (currently %d)"
                   % (suggested, cfg.cells), GOOD)
            answer = ask("apply? Y/n \u203a ")
            if answer is None or not answer.lower().startswith("n"):
                cfg.cells = suggested
                cfg.sanitise()
                cfg.save()
            continue
        elif pick == "s":
            settings_screen(cfg, cols)
            continue
        else:
            continue

        # A preset was chosen: tune it to this device straight away.
        screen(cols, "%s mode" % cfg.mode)
        status(preset["note"])
        suggested, limiter, ms, diff = autotune(cfg, lambda m: status(m))
        status("limited by %s" % limiter)
        answer = ask("use %d cells instead of %d? Y/n \u203a "
                     % (suggested, cfg.cells))
        if answer is None or not answer.lower().startswith("n"):
            cfg.cells = suggested
        cfg.sanitise()
        problem = cfg.save()
        status(("NOT saved: " + problem) if problem else "saved",
               WARN if problem else GOOD)
        time.sleep(0.9)
        first_run = False


def selftest(cfg):
    """Measure speed control on this device, with no keyboard involved.

    Speed reported as not working on iOS twice. Rather than guess a third
    time, this exercises the clock and the real audio backend and prints
    the positions it measures, so the numbers come from the device.
    """
    print(BOLD + "skjyt selftest" + OFF)
    print(DIM + "  platform %s, subprocess=%s, backend %s"
          % (sys.platform, HAVE_SUBPROCESS, cfg.backend) + OFF)
    print()
    ok = True

    def measure(clock, seconds=1.0):
        start = clock.time()
        time.sleep(seconds)
        return clock.time() - start

    print(BOLD + "  1. clock with no audio" + OFF)
    clock = Clock(None)
    at1 = measure(clock)
    clock.set_rate(2.0)
    at2 = measure(clock)
    good = at1 > 0.8 and 1.6 < at2 < 2.4
    ok = ok and good
    print("     1x advanced %.2fs, 2x advanced %.2fs  %s"
          % (at1, at2, GOOD + "OK" + OFF if good else WARN + "WRONG" + OFF))

    if not cfg.audio:
        print(DIM + "\n  audio is off, so nothing else to test." + OFF)
        return 0 if ok else 1

    print()
    print(BOLD + "  2. audio backend" + OFF)
    work = tempfile.mkdtemp(prefix="skjyt-test-")
    tone = os.path.join(work, "tone.m4a")
    code = run_ffmpeg(["-v", "error", "-y", "-f", "lavfi",
                       "-i", "sine=frequency=440:duration=12", tone])
    if code != 0 or not os.path.exists(tone):
        print(WARN + "     could not make a test tone with ffmpeg" + OFF)
        shutil.rmtree(work, ignore_errors=True)
        return 1

    sound = make_sound(tone, 12.0, cfg.backend)
    print("     backend  : %s" % type(sound).__name__)
    print("     ready    : %s%s" % (sound.ready,
                                    "" if sound.ready else "  (%s)" % sound.error))
    if not sound.ready:
        print(DIM + "     no audio here, so playback uses the wall clock and"
                    " speed will work." + OFF)
        shutil.rmtree(work, ignore_errors=True)
        return 0 if ok else 1

    try:
        clock = Clock(sound)
        moved = measure(clock, 1.2)
        print("     1x       : advanced %.2fs in 1.2s  %s"
              % (moved, GOOD + "OK" + OFF if moved > 0.9
                 else WARN + "audio clock is not moving" + OFF))

        clock.set_rate(2.0)
        fast = measure(clock, 1.2)
        rate_ok = 1.9 < fast < 3.0
        print("     2x       : advanced %.2fs in 1.2s  %s"
              % (fast, GOOD + "OK" + OFF if rate_ok else WARN + "TOO SLOW" + OFF))
        print("     audio    : %s" % ("paused, picture on its own clock"
                                      if clock.audio_dropped
                                      else "following the speed change"))
        if not rate_ok:
            ok = False
            print(WARN + "     -> speed is not taking effect here. Send this "
                         "block." + OFF)

        clock.set_rate(1.0)
        back = measure(clock, 1.0)
        print("     back to 1x: advanced %.2fs  %s"
              % (back, GOOD + "OK" + OFF if 0.8 < back < 1.3
                 else WARN + "WRONG" + OFF))
    finally:
        try:
            sound.stop()
        except Exception:
            pass
        shutil.rmtree(work, ignore_errors=True)

    print()
    print((GOOD + "  everything measured correctly." + OFF) if ok
          else (WARN + "  something above is wrong - send the output." + OFF))
    return 0 if ok else 1


def probe_input():
    """Test each way of reading a key, one at a time, on the main thread.

    Four rounds of fixing playback controls by hypothesis failed because
    a-Shell reports every capability as present and then delivers nothing.
    This tries each mechanism in isolation and says which, if any, works -
    so the next change is based on a result instead of a guess.
    """
    print(BOLD + "skjyt probe" + OFF)
    print(DIM + "  Each test waits 8 seconds. Press any key when asked."
          + OFF)
    print()
    results = []

    def attempt(name, note, fn):
        print("  %s%s%s  %s" % (BOLD, name, OFF, DIM + note + OFF))
        emit("     press a key now \u2026 ")
        try:
            got = fn()
        except Exception as exc:
            got = "%s: %s" % (type(exc).__name__, exc)
            print(WARN + "failed (%s)" % got + OFF)
            results.append((name, False, got))
            return
        if got:
            print(GOOD + "GOT %r" % got + OFF)
            results.append((name, True, repr(got)))
        else:
            print(WARN + "nothing" + OFF)
            results.append((name, False, "no data"))

    def with_cbreak(fn):
        try:
            import termios
            import tty
            saved = termios.tcgetattr(0)
            tty.setcbreak(0)
        except Exception:
            saved = None
        try:
            return fn()
        finally:
            if saved is not None:
                try:
                    import termios
                    termios.tcsetattr(0, termios.TCSADRAIN, saved)
                except Exception:
                    pass

    def select_then_read():
        import select as sel
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if sel.select([0], [], [], 0.2)[0]:
                return os.read(0, 16).decode("utf-8", "replace")
        return ""

    def nonblocking_read():
        import fcntl
        flags = fcntl.fcntl(0, fcntl.F_GETFL)
        fcntl.fcntl(0, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    data = os.read(0, 16)
                    if data:
                        return data.decode("utf-8", "replace")
                except (BlockingIOError, OSError):
                    pass
                time.sleep(0.05)
            return ""
        finally:
            fcntl.fcntl(0, fcntl.F_SETFL, flags)

    def threaded_read():
        box = []

        def grab():
            try:
                box.append(os.read(0, 16).decode("utf-8", "replace"))
            except Exception:
                pass
        thread = threading.Thread(target=grab, daemon=True)
        thread.start()
        thread.join(8)
        return box[0] if box else ""

    def alarm_read():
        if not hasattr(signal, "SIGALRM"):
            raise OSError("no SIGALRM on this platform")

        def bang(_s, _f):
            raise OSError("timed out")
        old = signal.signal(signal.SIGALRM, bang)
        signal.alarm(8)
        try:
            return os.read(0, 16).decode("utf-8", "replace")
        except OSError:
            return ""
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    attempt("select + read ", "raw mode, poll then read",
            lambda: with_cbreak(select_then_read))
    attempt("non-blocking  ", "raw mode, O_NONBLOCK loop",
            lambda: with_cbreak(nonblocking_read))
    attempt("blocking read ", "raw mode, main thread, SIGALRM timeout",
            lambda: with_cbreak(alarm_read))
    attempt("input()       ", "line mode - type something and press Enter",
            lambda: input(""))
    # Last on purpose: this one cannot be cancelled. If the terminal never
    # delivers, the thread stays blocked on stdin for the rest of the run
    # and would swallow the input() test that used to follow it.
    attempt("thread + read ", "raw mode, blocking read off-thread "
                              "(leaves a reader behind)",
            lambda: with_cbreak(threaded_read))

    print()
    print("  ctrl-c: press it within 8 seconds \u2026")
    caught = [False]

    def on_int(_s, _f):
        caught[0] = True
    try:
        previous = signal.signal(signal.SIGINT, on_int)
    except Exception:
        previous = None
    stop = time.monotonic() + 8
    while time.monotonic() < stop and not caught[0]:
        time.sleep(0.05)
    if previous is not None:
        try:
            signal.signal(signal.SIGINT, previous)
        except Exception:
            pass
    results.append(("ctrl-c", caught[0], "delivered" if caught[0] else "never arrived"))
    print("     " + (GOOD + "GOT IT" + OFF if caught[0]
                     else WARN + "nothing" + OFF))

    print()
    print(BOLD + "  summary" + OFF)
    for name, good, detail in results:
        print("    %s %-15s %s" % ("OK  " if good else "no  ", name.strip(), detail))
    working = [n for n, good, _ in results if good and n != "input()"]
    print()
    if working:
        print(GOOD + "  usable during playback: %s" % ", ".join(w.strip()
                                                               for w in working) + OFF)
    else:
        print(WARN + "  nothing works during playback on this terminal." + OFF)
        print(DIM + "  Use --limit SECONDS to stop a video without a keyboard."
              + OFF)
    return 0


def keys_check():
    """Interactive input diagnostic. Prints what it detects, then echoes
    keys until q, so a control problem stops being guesswork."""
    keys = Keys()
    ok = keys.enable()
    print(BOLD + "skjyt keys" + OFF)
    print("  input path : %s" % keys.describe())
    print("  stdin tty  : %s" % (os.isatty(0) if not IS_WINDOWS else "n/a"))
    if not IS_WINDOWS:
        try:
            probe = os.open("/dev/tty", os.O_RDONLY)
            print("  /dev/tty   : opens, isatty=%s" % os.isatty(probe))
            os.close(probe)
        except Exception as exc:
            print("  /dev/tty   : %s: %s" % (type(exc).__name__, exc))
        for name, mod in (("termios", "termios"), ("tty", "tty"),
                          ("fcntl", "fcntl"), ("select", "select")):
            try:
                __import__(mod)
                print("  %-10s : present" % name)
            except Exception as exc:
                print("  %-10s : MISSING (%s)" % (name, exc))
        try:
            import fcntl
            print("  stdout flag: O_NONBLOCK=%s (must be False)"
                  % bool(fcntl.fcntl(1, fcntl.F_GETFL) & os.O_NONBLOCK))
        except Exception:
            pass
    # No InputPump here: a second reader blocked on stdin would race the
    # main-thread poll below and eat half the keys it is meant to show.
    print("  reader     : %s, main-thread poll"
          % ("msvcrt" if IS_WINDOWS else "select + os.read"))

    if not ok:
        print(WARN + "\n  no key control here. ctrl-c still stops a video."
              + OFF)
        return 1

    if not keys.raw:
        print(DIM + "\n  raw mode is off here, so keys arrive when you press "
                    "Enter." + OFF)
    print(DIM + "  press keys - they should appear below. q to finish, "
                "or wait 20s." + OFF)
    seen = 0
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            batch = keys.drain()
            for key in batch:
                seen += 1
                print("    %s" % key)
                if key == "q":
                    raise KeyboardInterrupt
            time.sleep(0.03)
    except KeyboardInterrupt:
        pass
    finally:
        keys.restore()
    if not seen:
        print(WARN + "  nothing arrived from the keyboard." + OFF)
        print(DIM + "  This terminal will not give a running program its "
                    "keystrokes." + OFF)
        print(DIM + "  Checking ctrl-c still reaches us - press it within "
                    "15 seconds." + OFF)
        caught = [False]

        def on_int(_s, _f):
            caught[0] = True

        try:
            previous = signal.signal(signal.SIGINT, on_int)
        except Exception:
            previous = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not caught[0]:
            time.sleep(0.05)
        if previous is not None:
            try:
                signal.signal(signal.SIGINT, previous)
            except Exception:
                pass
        if caught[0]:
            print(GOOD + "  ctrl-c reaches the program, so you can at least "
                         "stop a video." + OFF)
            return 0
        print(WARN + "  ctrl-c did not arrive either." + OFF)
        return 1
    print(GOOD + "  %d keypresses read. Controls work." % seen + OFF)
    return 0


def doctor(cfg):
    """The requirement check, for the command line."""
    print(BOLD + "skjyt doctor" + OFF)
    bad = 0
    for ok, name, detail, fix in requirements(cfg):
        mark = (GOOD + "OK  " + OFF) if ok else (WARN + "FAIL" + OFF)
        print("  %s %-11s %s" % (mark, name, detail))
        if not ok:
            bad += 1
    print()
    if bad:
        print(WARN + "  %d problem%s. `skjyt setup` can install what's missing."
              % (bad, "" if bad == 1 else "s") + OFF)
    else:
        print(GOOD + "  everything needed is present." + OFF)
    print()
    print(DIM + "  libraries: yt-dlp (pip, required) is the only one." + OFF)
    print(DIM + "  binaries:  ffmpeg (required), ffplay (audio off Apple)." + OFF)
    print(DIM + "  stdlib only otherwise - no numpy, no curses, no requests." + OFF)
    return 0 if not bad else 1


# ==========================================================================
# Thumbnails
#
# Lists get a small picture per row, drawn with the same renderer as the
# video. Images come from i.ytimg.com over plain urllib (no requests
# dependency) and are cached as decoded pixels, so a list you have seen
# before draws instantly.
# ==========================================================================

THUMB_W = 12           # cells wide
THUMB_H = 4            # cells tall
THUMB_UA = "Mozilla/5.0 (compatible; skjyt)"


def thumb_url(entry):
    """Prefer what yt-dlp gave us; otherwise the well-known still URL."""
    thumbs_list = entry.get("thumbnails") or []
    best = None
    for candidate in thumbs_list:
        url = candidate.get("url")
        if not url:
            continue
        width = candidate.get("width") or 0
        # Smallest image that is still at least a bit bigger than we draw.
        if best is None or (width and width < best[0] and width >= 120):
            best = (width or 9999, url)
    if best:
        return best[1]
    vid = entry.get("id")
    return ("https://i.ytimg.com/vi/%s/mqdefault.jpg" % vid) if vid else None


def thumb_key(entry, mode, cfg):
    vid = entry.get("id") or (entry.get("title") or "")[:24]
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(vid))[:48]
    return "thumb-%s-%dx%d-%s-a%.2f.raw" % (safe, THUMB_W, THUMB_H, mode,
                                            cfg.cell_aspect)


def thumb_image(entry):
    """Local path to the thumbnail jpg, downloading it if needed."""
    vid = entry.get("id") or (entry.get("title") or "")[:24]
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(vid))[:48]
    path = os.path.join(CACHE, "thumb-%s.img" % safe)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    url = thumb_url(entry)
    if not url:
        return None
    try:
        os.makedirs(CACHE, exist_ok=True)
        request = urllib.request.Request(url, headers={"User-Agent": THUMB_UA})
        with urllib.request.urlopen(request, timeout=8) as response:
            data = response.read(2 << 20)
        if not data:
            return None
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        return path
    except Exception:
        return None


def prefetch_thumbs(entries, cfg, workers=6):
    """Download a page of thumbnails at once.

    Serially this was one round trip per row - eight rows meant eight waits.
    Only the downloads are parallel: decoding goes through ffmpeg, which on
    a-Shell runs in-process via os.system and is not safe to call from
    several threads.
    """
    if not cfg.thumbs:
        return
    missing = [e for e in entries if e.get("id")]
    if not missing:
        return
    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(thumb_image, missing))
    except Exception:
        for entry in missing:              # threads unavailable: plain loop
            thumb_image(entry)


def thumb_pixels(entry, mode, cfg):
    """Returns raw pixel bytes for one thumbnail, or None."""
    channels = 1 if mode in ("ascii", "braille") else 3
    width, height = pixel_size(mode, THUMB_W, THUMB_H)
    want = width * height * channels

    cached = os.path.join(CACHE, thumb_key(entry, mode, cfg))
    try:
        if os.path.getsize(cached) == want:
            with open(cached, "rb") as fh:
                return fh.read()
    except OSError:
        pass

    image = thumb_image(entry)
    if not image:
        return None

    tmp = tempfile.mkdtemp(prefix="skjyt-th-")
    try:
        parts = []
        fix = aspect_fix(mode, cfg.cell_aspect)
        if abs(fix - 1.0) > 0.01:
            parts.append("scale=iw*%.4f:ih" % fix)
        parts.append("scale=%d:%d:force_original_aspect_ratio=decrease:flags=%s"
                     % (width, height, cfg.scaler))
        parts.append("pad=%d:%d:(ow-iw)/2:(oh-ih)/2" % (width, height))
        if mode in ("ascii", "braille"):
            parts.append("format=gray")

        out = os.path.join(tmp, "t.raw")
        code = run_ffmpeg(["-v", "error", "-y", "-i", image, "-vf",
                           ",".join(parts), "-frames:v", "1", "-f", "rawvideo",
                           "-pix_fmt", "gray" if channels == 1 else "rgb24", out])
        if code != 0 or not os.path.exists(out):
            return None
        with open(out, "rb") as fh:
            pixels = fh.read()
        if len(pixels) != want:
            return None
        try:
            os.makedirs(CACHE, exist_ok=True)
            with open(cached, "wb") as fh:
                fh.write(pixels)
        except Exception:
            pass
        return pixels
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def thumb_lines(entry, cfg):
    """THUMB_H lines of drawn thumbnail, or blanks if it could not be had."""
    mode = cfg.mode if cfg.mode in ("ascii", "braille") else "quad"
    blank = [" " * THUMB_W] * THUMB_H
    if not cfg.thumbs:
        return None
    pixels = thumb_pixels(entry, mode, cfg)
    if not pixels:
        return blank
    width, height = pixel_size(mode, THUMB_W, THUMB_H)
    table = ascii_table(None)
    try:
        lines = render_lines(pixels, width, height, mode, table, cfg.depth)
    except Exception:
        return blank
    while len(lines) < THUMB_H:
        lines.append(" " * THUMB_W)
    return lines[:THUMB_H]


def beside(picture, text_lines, gap="  "):
    """Lay text to the right of a fixed-width picture block."""
    rows = max(len(picture), len(text_lines))
    out = []
    for i in range(rows):
        left = picture[i] if i < len(picture) else (" " * THUMB_W)
        right = text_lines[i] if i < len(text_lines) else ""
        out.append("  " + left + OFF + gap + right)
    return out


def entry_caption(entry, width, extra=""):
    """The text that sits next to a thumbnail."""
    dur = entry.get("duration") or 0
    who = entry.get("channel") or entry.get("uploader") or ""
    lines = [BOLD + clip(entry.get("title") or "?", width) + OFF]
    meta = []
    if dur:
        meta.append(clock_str(dur))
    if who:
        meta.append(clip(who, max(8, width - 10)))
    if meta:
        lines.append(DIM + " \u00b7 ".join(meta) + OFF)
    if extra:
        lines.append(DIM + clip(extra, width) + OFF)
    return lines


# ==========================================================================
# Screens
# ==========================================================================

MODE_BLURB = {
    "braille": "8 px per cell, no colour, sharpest",
    "quad":    "4 px per cell, colour",
    "blocks":  "2 px per cell, colour",
    "squares": "1 px per cell, solid colour",
    "color":   "1 px per cell, tinted characters",
    "ascii":   "1 px per cell, plain text, fastest",
}


def mode_blurb(mode):
    """Never let a missing description take down the settings screen."""
    across, down = SUBPIXELS.get(mode, (1, 1))
    return MODE_BLURB.get(mode, "%d px per cell" % (across * down))


def control_label(cfg, state=None):
    """What 'how to control' is actually doing right now."""
    if cfg.control == "keys":
        return "single keys  %sspace, arrows, q%s" % (DIM, OFF)
    if cfg.control == "line":
        return "typed commands  %spress Enter after each%s" % (DIM, OFF)
    resolved = learned_mode(state or {}) or "keys"
    return "auto  %s\u2192 currently %s%s" % (
        DIM, "typed commands (press Enter)" if resolved == "line"
        else "single keys", OFF)


def settings_screen(cfg, cols, state=None):
    while True:
        where = SKJ_HOME if HOME_WRITABLE else SKJ_HOME + "  (NOT WRITABLE)"
        screen(cols, "settings \u00b7 " + clip(where, cols - 14))
        print(row("1", "mode",
                  "%s  %s(%s)%s" % (cfg.mode, DIM, mode_blurb(cfg.mode), OFF)))
        print(row("2", "fps", str(cfg.fps)))
        if cfg.mode in ("blocks", "quad"):
            print(row("3", "threshold", "%sn/a in %s mode%s" % (DIM, cfg.mode, OFF)))
        else:
            print(row("3", "threshold",
                      "off" if cfg.threshold is None else str(cfg.threshold)))
        print(row("4", "audio", "on" if cfg.audio else "off"))
        print(row("5", "quality", "%dp" % cfg.quality))
        print(row("6", "results", str(cfg.results)))
        print(row("7", "detail", "%d cells  %slower = smoother%s" % (cfg.cells, DIM, OFF)))
        if cfg.mode in ("ascii", "braille"):
            print(row("8", "colour step", "%sn/a, %s has no colour%s"
                      % (DIM, cfg.mode, OFF)))
        else:
            print(row("8", "colour step",
                      "%d  %sbigger = fewer escapes%s" % (cfg.quant, DIM, OFF)))
        print(row("9", "audio backend", cfg.backend))
        print(row("a", "cell aspect",
                  "%.2f  %sheight / width%s" % (cfg.cell_aspect, DIM, OFF)))
        if cfg.mode in ("ascii", "braille"):
            print(row("d", "dither", "on" if cfg.dither else "off"))
        else:
            print(row("d", "dither", "%sascii and braille only%s" % (DIM, OFF)))
        print(row("s", "subtitles", "on" if cfg.subs else "off"))
        print(row("r", "resume", "on" if cfg.resume else "off"))
        print(row("c", "cache", "%d MB limit, %.0f MB used"
                  % (cfg.cache_mb, cache_size_mb())))
        print(row("k", "chunk", "%ds  %sdecode slice%s" % (cfg.chunk, DIM, OFF)))
        print(row("l", "audio delay", "%+.2fs" % cfg.latency))
        print(row("g", "region", cfg.region or "default"))
        print(row("i", "how to control", control_label(cfg, state)))
        print(row("ctrl-c", "ctrl-c",
                  ("pauses and opens a menu" if cfg.ctrl_c_menu
                   else "stops the video")))
        print(row("p", "thumbnails", ("on  %spictures in lists%s" % (DIM, OFF))
                  if cfg.thumbs else "off"))
        print(row("x", "colour depth", "%s  %s256 is ~38%% fewer bytes%s"
                  % (cfg.depth, DIM, OFF)))
        print(row("z", "scaler", "%s  %sdownscale filter%s" % (cfg.scaler, DIM, OFF)))
        print(row("u", "sharpen", ("%.2f" % cfg.sharpen) if cfg.sharpen else "off"))
        print(row("v", "contrast", ("%.2f" % cfg.contrast) if cfg.contrast else "off"))
        print()
        print(row("b", "back", "%ssaves automatically%s" % (DIM, OFF)))

        pick = ask("\u203a ")
        if pick is None or pick in ("b", "", "q"):
            cfg.sanitise()
            problem = cfg.save()
            if problem:
                print()
                status("settings NOT saved", WARN)
                status(CONFIG)
                status(clip(problem, cols - 4))
                ask("enter to continue \u203a ")
            return

        if pick == "1":
            cfg.mode = MODES[(MODES.index(cfg.mode) + 1) % len(MODES)]
        elif pick == "2":
            value = ask("fps (1-60) \u203a ")
            if value and value.isdigit():
                cfg.fps = int(value)
        elif pick == "3" and cfg.mode not in ("blocks", "quad"):
            value = ask("threshold 0-255, blank for off \u203a ")
            if value is not None:
                cfg.threshold = int(value) if value.isdigit() else None
        elif pick == "4":
            cfg.audio = not cfg.audio
        elif pick == "5":
            here = QUALITIES.index(cfg.quality) if cfg.quality in QUALITIES else 1
            cfg.quality = QUALITIES[(here + 1) % len(QUALITIES)]
        elif pick == "6":
            value = ask("results (1-20) \u203a ")
            if value and value.isdigit():
                cfg.results = int(value)
        elif pick == "7":
            value = ask("cells 300-40000 (phone 2400, desktop 12000) \u203a ")
            if value and value.isdigit():
                cfg.cells = int(value)
        elif pick == "8" and cfg.mode not in ("ascii", "braille"):
            value = ask("colour step 1-64 (1 exact, 24 fast) \u203a ")
            if value and value.isdigit():
                cfg.quant = int(value)
        elif pick == "9":
            cfg.backend = BACKENDS[(BACKENDS.index(cfg.backend) + 1) % len(BACKENDS)]
        elif pick == "a":
            value = ask("cell aspect 1.0-3.0 (2.0 typical) \u203a ")
            try:
                cfg.cell_aspect = float(value)
            except (TypeError, ValueError):
                pass
        elif pick == "d" and cfg.mode in ("ascii", "braille"):
            cfg.dither = not cfg.dither
        elif pick == "s":
            cfg.subs = not cfg.subs
        elif pick == "r":
            cfg.resume = not cfg.resume
        elif pick == "c":
            value = ask("cache MB (0 disables), or 'clear' \u203a ")
            if value == "clear":
                shutil.rmtree(CACHE, ignore_errors=True)
            elif value and value.isdigit():
                cfg.cache_mb = int(value)
        elif pick == "k":
            value = ask("chunk seconds 5-600 \u203a ")
            if value and value.isdigit():
                cfg.chunk = int(value)
        elif pick == "l":
            value = ask("audio delay seconds, e.g. -0.3 \u203a ")
            try:
                cfg.latency = float(value)
            except (TypeError, ValueError):
                pass
        elif pick == "i":
            order = ("auto", "keys", "line")
            cfg.control = order[(order.index(cfg.control) + 1) % len(order)]
            # Choosing explicitly also clears what auto had learned, so the
            # old decision cannot come back the next time auto is picked.
            if state is not None:
                state.pop("input_mode", None)
                save_state(state)
        elif pick in ("ctrl-c", "ctrlc"):
            cfg.ctrl_c_menu = not cfg.ctrl_c_menu
        elif pick == "g":
            value = ask("region, two letters (PL, US, GB), blank for default \u203a ")
            if value is not None:
                cfg.region = value
        elif pick == "p":
            cfg.thumbs = not cfg.thumbs
        elif pick == "x":
            cfg.depth = DEPTHS[(DEPTHS.index(cfg.depth) + 1) % len(DEPTHS)]
        elif pick == "z":
            order = ("lanczos", "bicubic", "bilinear", "neighbor")
            here = order.index(cfg.scaler) if cfg.scaler in order else 0
            cfg.scaler = order[(here + 1) % len(order)]
        elif pick == "u":
            value = ask("sharpen 0-3, 0 disables (0.8 is a good start) \u203a ")
            try:
                cfg.sharpen = float(value)
            except (TypeError, ValueError):
                pass
        elif pick == "v":
            value = ask("contrast 0.3-3.0, 1.0 disables \u203a ")
            try:
                cfg.contrast = float(value)
            except (TypeError, ValueError):
                pass
        cfg.sanitise()


def search_screen(query, cfg, cols):
    """Returns a list of queue items, or None."""
    while True:
        if not query:
            screen(cols, "search, paste a url, or type a file path \u00b7 blank to go back")
            query = ask("\u203a ")
            if not query:
                return None

        if is_local(query):
            return [{"target": os.path.expanduser(query),
                     "title": os.path.basename(query)}]

        if is_url(query):
            entries = playlist_entries(query)
            if entries and len(entries) > 1:
                screen(cols, "%d videos in this playlist" % len(entries))
                print(row("1", "queue all"))
                print(row("2", "just the first"))
                print(row("b", "back"))
                pick = ask("\u203a ")
                if pick == "1":
                    return [{"target": e["url"], "title": e["title"]} for e in entries]
                if pick == "2":
                    return [{"target": entries[0]["url"], "title": entries[0]["title"]}]
                return None
            return [{"target": query, "title": query}]

        screen(cols, "searching \u2026")
        try:
            entries = search(query, cfg.results)
        except Exception as exc:
            screen(cols, "search failed")
            status(clip(str(exc), cols - 4), WARN)
            if any(h in str(exc).lower() for h in JS_HINTS):
                status("that looks like YouTube's JS challenge; a-Shell has no node")
            ask("enter to continue \u203a ")
            query = None
            continue

        if not entries:
            screen(cols, "nothing found for %s" % clip(query, cols - 22))
            time.sleep(1.2)
            query = None
            continue

        result = pick_from(entries, cols, cfg,
                           "results for %s" % clip(query, cols - 22),
                           extra_rows=(row("s", "new search"),))
        if result is None:
            return None
        if isinstance(result, str):
            query = None
            continue
        return result


def render_row(n, entry, cols, cfg, extra=""):
    """One list row: thumbnail plus caption, or a compact two-line text row."""
    number = "%s%2d%s" % (ACCENT, n, OFF)
    if cfg.thumbs:
        picture = thumb_lines(entry, cfg)
        if picture is not None:
            width = cols - THUMB_W - 8
            caption = entry_caption(entry, width, extra)
            rows = beside(picture, caption)
            rows[0] = number + rows[0][2:]     # number sits in the margin
            return rows
    dur = entry.get("duration") or 0
    who = entry.get("channel") or entry.get("uploader") or ""
    out = ["  %s  %s%5s%s  %s" % (number, DIM,
                                  clock_str(dur) if dur else "--:--", OFF,
                                  clip(entry.get("title") or "?", cols - 14))]
    tail = " \u00b7 ".join(x for x in (who, extra) if x)
    if tail:
        out.append("      " + DIM + clip(tail, cols - 8) + OFF)
    return out


def page_size(cols, cfg):
    """How many rows fit, given how tall a row is with pictures on."""
    _, rows = term_size()
    per_row = THUMB_H + 1 if cfg.thumbs else 2
    room = max(4, rows - 9)          # banner, subtitle, nav, prompt
    return max(2, room // per_row)


def pick_from(entries, cols, cfg, title, extra_rows=(), whys=None):
    """Shared paged list picker.

    Returns queue items, the string 'refresh', another key the caller
    handles, or None for back.
    """
    page = 0
    per = page_size(cols, cfg)
    pages = max(1, (len(entries) + per - 1) // per)

    while True:
        page = max(0, min(page, pages - 1))
        start = page * per
        chunk = entries[start:start + per]

        label = title
        if pages > 1:
            label += "  \u00b7  page %d/%d" % (page + 1, pages)
        screen(cols, label)
        if cfg.thumbs:
            status("loading pictures \u2026")
            prefetch_thumbs(chunk, cfg)             # one wait, not one per row
            sys.stdout.write("\x1b[1A\x1b[2K")

        for offset, entry in enumerate(chunk):
            n = start + offset + 1
            extra = (whys or {}).get(entry.get("id"), "")
            for line in render_row(n, entry, cols, cfg, extra):
                print(line)

        print()
        for text in extra_rows:
            print(text)
        nav = row("a", "queue all") + row("r", "refresh")
        if pages > 1:
            nav = row("</>", "page", "%d of %d" % (page + 1, pages)) + nav
        print(nav + row("b", "back"))

        pick = ask("\u203a ")
        if pick is None or pick == "b":
            return None
        if pick == "r":
            return "refresh"
        if pick in (">", ".", "+"):
            page += 1
            continue
        if pick in ("<", ",", "-"):
            page -= 1
            continue
        if pick == "a":
            return [{"target": e.get("url") or
                     ("https://www.youtube.com/watch?v=" + e["id"]),
                     "title": e.get("title") or "?"} for e in entries]
        if pick and not pick.isdigit():
            return pick
        try:
            idx = int(pick) - 1
            if not 0 <= idx < len(entries):
                raise ValueError
        except (ValueError, TypeError):
            continue
        chosen = entries[idx]
        return [{"target": chosen.get("url") or
                 ("https://www.youtube.com/watch?v=" + chosen["id"]),
                 "title": chosen.get("title") or "?"}]


def popular_screen(cfg, cols, category="now"):
    """Returns queue items, or None."""
    while True:
        screen(cols, "popular \u00b7 %s \u00b7 %s"
               % (category, cfg.region or "default region"))
        entries, label = popular(category, cfg, 20, lambda m: status(m))
        if not entries:
            status("nothing reachable. YouTube retired the global trending", WARN)
            status("page in July 2025; try the music, gaming or movies charts")
            ask("enter to continue \u203a ")
            return None

        tabs = "  ".join(
            (ACCENT + BOLD + c + OFF) if c == category else (DIM + c + OFF)
            for c in POPULAR)
        result = pick_from(
            entries, cols, cfg,
            "popular \u00b7 %s \u00b7 %s" % (label, cfg.region or "default"),
            extra_rows=("  " + tabs,
                            row("n/m/g/v", "switch chart", "now music gaming movies"),
                            row("c", "change region", cfg.region or "default")))

        if result is None:
            return None
        if result == "refresh":
            continue
        if isinstance(result, str):
            if result in ("n", "m", "g", "v"):
                category = {"n": "now", "m": "music",
                            "g": "gaming", "v": "movies"}[result]
                continue
            if result == "c":
                value = ask("two-letter country code, blank for default \u203a ")
                if value is not None:
                    cfg.region = value
                    cfg.sanitise()
                    cfg.save()
                continue
            continue
        return result


def channel_screen(target, state, cfg, cols, tab="videos"):
    """Browse one channel. Returns queue items, or None."""
    url = channel_url(target)
    name = url.rstrip("/").split("/")[-1]
    query = None
    while True:
        where = ("search: " + query) if query else tab
        screen(cols, "channel %s \u00b7 %s" % (name, where))
        status("loading \u2026")
        try:
            entries = (channel_search(url, query, 30) if query
                       else channel_tab(url, tab, 30))
        except Exception as exc:
            screen(cols, "could not open that channel")
            status(clip(str(exc), cols - 4), WARN)
            ask("enter to continue \u203a ")
            return None

        real = (entries[0].get("channel") or entries[0].get("uploader")) \
            if entries else ""
        shown = real or name
        tabs = "  ".join(
            (ACCENT + BOLD + t + OFF) if (t == tab and not query) else (DIM + t + OFF)
            for t in CHANNEL_TABS)
        followed = is_followed(state, url)
        controls = ("  " + tabs,
                    row("1/2/3/4", "tab", "videos shorts streams playlists"),
                    row("/", "search", "within this channel"),
                    row("f", "unfollow" if followed else "follow", shown))

        if not entries:
            status("nothing here" if query else "no %s on this channel" % tab, WARN)
            for line in controls:
                print(line)
            print(row("b", "back"))
            result = ask("\u203a ")
        else:
            result = pick_from(entries, cols, cfg,
                               "%s \u00b7 %s" % (clip(shown, cols - 20), where),
                               extra_rows=controls)

        if result is None or result == "b":
            return None
        if result == "refresh":
            continue
        if isinstance(result, str):
            if result in ("1", "2", "3", "4"):
                tab = CHANNEL_TABS[int(result) - 1]
                query = None
            elif result == "/":
                asked = ask("search this channel, blank to clear \u203a ")
                query = asked or None
            elif result == "f":
                if followed:
                    unfollow(state, url)
                    status("unfollowed", DIM)
                else:
                    follow(state, shown, url)
                    status("followed", GOOD)
                time.sleep(0.4)
            continue
        return result


def following_screen(state, cfg, cols):
    while True:
        rows = following(state)
        screen(cols, "following \u00b7 %d channel%s"
               % (len(rows), "" if len(rows) == 1 else "s"))
        if not rows:
            status("none yet. open a channel and press f")
        for n, item in enumerate(rows, 1):
            print("  %s%2d%s  %s" % (ACCENT, n, OFF, clip(item["name"], cols - 8)))
            print("      %s%s%s" % (DIM, clip(item["url"], cols - 8), OFF))
        print("\n" + row("number", "open") + row("d", "unfollow") + row("b", "back"))
        pick = ask("\u203a ")
        if pick is None or pick == "b":
            return None
        if pick == "d":
            needle = ask("name or url fragment \u203a ")
            if needle:
                unfollow(state, needle)
            continue
        try:
            idx = int(pick) - 1
            if not 0 <= idx < len(rows):
                raise ValueError
        except ValueError:
            continue
        found = channel_screen(rows[idx]["url"], state, cfg, cols)
        if found:
            return found


def history_screen(state, cols, limit=20):
    rows = list(reversed(state.get("history", [])))[:limit]
    screen(cols, "history \u00b7 last %d" % len(rows))
    if not rows:
        status("nothing watched yet")
    for n, item in enumerate(rows, 1):
        share = ""
        if item.get("duration"):
            share = " %d%%" % (100.0 * item.get("watched", 0) / item["duration"])
        print("  %s%2d%s  %s" % (ACCENT, n, OFF, clip(item.get("title", "?"), cols - 8)))
        print("      %s%s%s%s" % (DIM, clip(item.get("channel", ""), cols - 16),
                                  share, OFF))
    print("\n" + row("c", "clear history") + row("b", "back"))
    if ask("\u203a ") == "c":
        state["history"] = []
        state.pop("feed", None)
        save_state(state)


def queue_screen(queue, cols):
    screen(cols, "queue \u00b7 %d item%s" % (len(queue), "" if len(queue) == 1 else "s"))
    if not queue:
        status("empty")
    for n, item in enumerate(queue[:20], 1):
        print("  %s%2d%s  %s" % (ACCENT, n, OFF, clip(item["title"], cols - 8)))
    if len(queue) > 20:
        status("... and %d more" % (len(queue) - 20))
    print("\n" + row("c", "clear") + row("b", "back"))
    if ask("\u203a ") == "c":
        del queue[:]


def menu(cfg, queue, state, cols):
    """Returns 'play', or None to quit."""
    while True:
        tight = compact_ui()
        screen(cols, None if tight else cfg.summary())
        print(row("1", "for you", "" if tight else
                  "%sbuilt from what you watched%s" % (DIM, OFF)))
        print(row("2", "popular", "" if tight else
                  "%scharts, no sign-in%s" % (DIM, OFF)))
        print(row("3", "search", "" if tight else
                  "%ssearch, url, playlist or file%s" % (DIM, OFF)))
        print(row("4", "channel", "" if tight else
                  "%sopen an @handle or url%s" % (DIM, OFF)))
        print(row("5", "following", "%d channel%s"
                  % (len(following(state)),
                     "" if len(following(state)) == 1 else "s")))
        print(row("6", "queue", "%d waiting" % len(queue)))
        print(row("7", "history", "%d watched" % len(state.get("history", []))))
        print(row("8", "settings", "" if tight else
                  "%slook, colour, quality%s" % (DIM, OFF)))
        if tight:
            # Two rarely-used entries folded onto one line, and the
            # settings summary dropped: on a ten-row screen every line
            # spent is a line of the menu scrolled away.
            print("  %s9%s benchmark   %s0%s setup   %sq%s quit"
                  % (ACCENT, OFF, ACCENT, OFF, ACCENT, OFF))
        else:
            print(row("9", "benchmark", "%smeasure render cost%s" % (DIM, OFF)))
            print(row("0", "setup", "%srequirements and presets%s" % (DIM, OFF)))
            print()
            print(row("q", "quit"))

        pick = ask("\u203a ")
        if pick is None or pick == "q":
            return None
        if pick == "":
            continue          # a stray Enter must not launch anything
        found = None
        if pick in ("1", "f"):
            found = feed_screen(state, cfg, cols)
        elif pick == "2":
            found = popular_screen(cfg, cols)
        elif pick in ("3", "s"):
            found = search_screen(None, cfg, cols)
        elif pick == "4":
            target = ask("channel @handle or url \u203a ")
            if target:
                found = channel_screen(target, state, cfg, cols)
        elif pick == "5":
            found = following_screen(state, cfg, cols)
        elif pick == "6":
            queue_screen(queue, cols)
            if queue:
                answer = ask("play now? y/N \u203a ")
                if answer and answer.lower().startswith("y"):
                    return "play"
        elif pick == "7":
            history_screen(state, cols)
        elif pick == "8":
            settings_screen(cfg, cols, state)
        elif pick == "9":
            screen(cols, "benchmark")
            bench(cfg)
            ask("\nenter to continue \u203a ")
        elif pick == "0":
            setup_screen(cfg, cols)

        if found:
            queue.extend(found)
            return "play"


# ==========================================================================
# Driving one item
# ==========================================================================

def grid_for(cfg, cols, rows, reserve=6):
    width = cols - 1                      # never let a full line wrap
    out_rows = max(6, rows - reserve)     # title, bar, hint, 2 subs, spare
    if width * out_rows > cfg.cells:
        factor = (float(cfg.cells) / (width * out_rows)) ** 0.5
        width = max(20, int(width * factor))
        out_rows = max(6, int(out_rows * factor))
    return pixel_size(cfg.mode, width, out_rows)


def keys_ever_worked():
    return KEYS_SEEN[0]


def play_with_prompt(decoder, media, cfg, cols, start_at, recorder, queue_info):
    """Render on a worker thread; take commands from input() on the main one.

    a-Shell hands a running program nothing - not select, not a
    non-blocking read, not a blocking read on either thread. The one thing
    that works is input(). So the rendering moves off the main thread and
    the main thread does the only thing this terminal supports: waits at a
    prompt.
    """
    box = {}

    def render():
        try:
            box["result"] = play(decoder, media, cfg, cols, start_at,
                                 recorder, queue_info, use_keys=False)
        except Exception as exc:
            box["result"] = ("end", start_at, 0, 0, False)
            box["error"] = exc
        # The main thread is sitting in input() and cannot be interrupted,
        # so tell the user the one thing that will free it - but only if
        # they have not already typed something that ends the loop, or the
        # extra Enter falls through onto the next screen.
        if not box.get("user_quit"):
            emit("\n" + DIM + "  finished - press Enter" + OFF + "\n")

    worker = threading.Thread(target=render, daemon=True)
    worker.start()
    time.sleep(0.5)          # let it draw once and set its scroll region
    while worker.is_alive():
        try:
            line = input("")
        except (EOFError, KeyboardInterrupt):
            push_command("q")
            break
        push_command(line)
        if line.strip().lower() in ("q", "quit", "stop", "n", "next"):
            box["user_quit"] = True
            break
    worker.join(3)
    if box.get("error"):
        raise box["error"]
    return box.get("result", ("end", start_at, 0, 0, False))


def run_one(item, cfg, state, recorder, queue_info=""):
    workdir = tempfile.mkdtemp(prefix="skjyt-")
    decoder = None
    start_at = 0.0
    media = None

    try:
        while True:                       # re-enters on resize
            cols, rows = term_size()
            mode_now = cfg.control
            if mode_now == "auto":
                mode_now = learned_mode(state) or "keys"
            # Typed mode needs a couple of rows at the bottom to type into.
            width, height = grid_for(cfg, cols, rows,
                                     8 if mode_now == "line" else 6)

            screen(cols, cfg.summary())
            if media is None:             # only fetch once across resizes
                try:
                    media = fetch(item["target"], workdir, cfg, status)
                except Exception as exc:
                    # One dead video must not end the session.
                    status("skipping: " + describe_error(exc), WARN)
                    status(clip(item.get("title", item["target"]), cols - 4))
                    return "skip"
            status(clip(media.title, cols - 4))

            if cfg.resume and start_at == 0.0:
                saved = state.get("pos", {}).get(str(media.key))
                if saved and saved > 10 and (not media.duration
                                             or saved < media.duration - 15):
                    answer = ask("resume at %s? Y/n \u203a " % clock_str(saved))
                    if answer is None or not answer.lower().startswith("n"):
                        start_at = saved

            decoder = Decoder(media.video, workdir, width, height,
                              cfg.fps, cfg.mode, cfg, media.duration)

            name = cache_name(media.key, width, height, cfg.fps, cfg.mode, cfg)
            cached = (cache_lookup(name, decoder.frame_bytes,
                                   "%s:%s" % (media.key, media.duration))
                      if cfg.cache_mb > 0 else None)
            if cached:
                status("cached frames, no decode needed", GOOD)
                decoder.path = cached
                decoder.done = True
                decoder.expected = decoder.available()
            else:
                tight = decoder.check_space(workdir)
                if tight:
                    need, free = tight
                    status("needs ~%d MB, only %d MB free"
                           % (need // 1048576, free // 1048576), WARN)
                    answer = ask("continue anyway? y/N \u203a ")
                    if not answer or not answer.lower().startswith("y"):
                        return "end"
                status("decoding %dx%d \u2026" % (width, height))
                decoder.start()
                lead = max(1, cfg.fps * 2)
                spin = 0
                while decoder.available() < lead and not decoder.done:
                    decoder.poll()
                    if decoder.proc is None:
                        decoder.ensure(lead)
                        break
                    time.sleep(0.1)
                    spin += 1
                    if spin % 10 == 0:
                        status("buffered %ds" % (decoder.available() // cfg.fps))
                if decoder.error:
                    status(clip(decoder.error, cols - 4), WARN)
                    ask("enter to continue \u203a ")
                    return "end"

            mode = mode_now
            if mode == "line":
                result, position, painted, sent, saw_keys = play_with_prompt(
                    decoder, media, cfg, cols, start_at, recorder, queue_info)
            else:
                result, position, painted, sent, saw_keys = play(
                    decoder, media, cfg, cols, start_at, recorder, queue_info)
                if saw_keys and learned_mode(state) == "line":
                    # Keys work here after all: stop forcing the prompt.
                    learn_mode(state, "keys")
                if painted > cfg.fps * 3 and not keys_ever_worked():
                    # Learned, not guessed: this terminal gave a running
                    # program nothing. Use the typed prompt next time.
                    if learned_mode(state) != "line":
                        learn_mode(state, "line")
                        status("keys never arrived - switching to the typed "
                               "prompt for the next video", WARN)
                        status("settings 'i' puts it back to single keys")
                        time.sleep(1.8)

            remember_watch(state, media, position)
            if cfg.resume:
                positions = state.setdefault("pos", {})
                if media.duration and position > media.duration - 15:
                    positions.pop(str(media.key), None)
                elif position > 10:
                    positions[str(media.key)] = round(position, 1)
            save_state(state)

            if result == "resize":
                start_at = position
                decoder.stop()
                decoder = None
                status("terminal resized, re-decoding \u2026")
                time.sleep(0.4)
                continue

            if (cfg.cache_mb > 0 and not cached and decoder.done
                    and not decoder.error and os.path.exists(decoder.path)):
                cache_store(decoder.path, name, cfg.cache_mb,
                            decoder.frame_bytes,
                            "%s:%s" % (media.key, media.duration))

            emit("\x1b[r\x1b[?25h\x1b[2J\x1b[H")
            if painted:
                print(DIM + "  %d frames, %.1f MB to the terminal (%d B/frame)"
                      % (painted, sent / 1048576.0, sent // painted) + OFF)
                time.sleep(0.8)
            return result
    finally:
        if decoder:
            decoder.stop()
        shutil.rmtree(workdir, ignore_errors=True)


# ==========================================================================
# Entry
# ==========================================================================

COMMANDS = """
  skjyt                        the menu
  skjyt <search terms>         search and play
  skjyt <url>                  video, playlist or channel url
  skjyt <file>                 a local file, no network

  skjyt feed                   your feed, rebuilt
  skjyt popular [now|music|gaming|movies]
  skjyt channel <@handle|url> [videos|shorts|streams|playlists]
  skjyt channel <@handle|url> search <words>
  skjyt follow <@handle|url>
  skjyt unfollow <name fragment>
  skjyt following
  skjyt muted [clear]          things the feed is hiding
  skjyt history [n]
  skjyt cache [info|clear]
  skjyt config [path|get|set <key> <value>]
  skjyt bench
  skjyt replay <file>
  skjyt update                 update yt-dlp (the usual fix for extraction)
  skjyt setup                  requirements, presets, auto-tune
  skjyt doctor                 check requirements and exit
  skjyt keys                   test playback controls, show the input path
  skjyt probe                  try every way of reading a key, report which
  skjyt selftest               measure speed control on this device
  skjyt tune                   measure this device, set the budget
  skjyt commands               this list

None of these sign in. Everything reads public pages.
"""


def cmd_cache(rest, cfg):
    what = rest[0] if rest else "info"
    if what == "clear":
        shutil.rmtree(CACHE, ignore_errors=True)
        print("cache cleared")
    else:
        print("cache: %s" % CACHE)
        print("used:  %.1f MB of %d MB limit" % (cache_size_mb(), cfg.cache_mb))
        try:
            names = [n for n in os.listdir(CACHE)
                     if not n.endswith(".json") and not n.endswith(".ok")]
        except Exception:
            names = []
        print("files: %d" % len(names))
        for name in sorted(names)[:15]:
            print("  " + name)


def cmd_config(rest, cfg):
    action = rest[0] if rest else "get"
    if action == "path":
        print(SKJ_HOME)
        print("writable: %s" % HOME_WRITABLE)
        return
    if action == "set" and len(rest) >= 3:
        key, raw = rest[1], " ".join(rest[2:])
        if key not in Settings.FIELDS:
            die("unknown setting %r. try: %s" % (key, ", ".join(Settings.FIELDS)))
        current = getattr(cfg, key)
        try:
            if isinstance(current, bool):
                value = raw.lower() in ("1", "true", "yes", "on")
            elif isinstance(current, int) or (current is None and raw.isdigit()):
                value = int(raw)
            elif isinstance(current, float):
                value = float(raw)
            elif raw == "" or raw.lower() == "none":
                value = None
            else:
                value = raw
        except ValueError:
            die("could not read %r as a value for %s" % (raw, key))
        setattr(cfg, key, value)
        cfg.sanitise()
        problem = cfg.save()
        if problem:
            die("not saved - " + problem)
        print("%s = %s" % (key, getattr(cfg, key)))
        return
    for key in Settings.FIELDS:
        print("  %-12s %s" % (key, getattr(cfg, key)))


def cmd_history(rest, state):
    try:
        limit = int(rest[0]) if rest else 20
    except ValueError:
        limit = 20
    rows = list(reversed(state.get("history", [])))[:limit]
    if not rows:
        print("nothing watched yet")
        return
    for item in rows:
        share = ""
        if item.get("duration"):
            share = " (%d%%)" % (100.0 * item.get("watched", 0) / item["duration"])
        print("  %-46s %s%s" % (clip(item.get("title", "?"), 46),
                                clip(item.get("channel", ""), 20), share))


def main():
    ap = argparse.ArgumentParser(
        description="Watch video as text, on-device. No sign-in.",
        epilog=COMMANDS, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("words", nargs="*", help="a command, or what to play")
    ap.add_argument("--mode", choices=MODES)
    ap.add_argument("--threshold", type=int)
    ap.add_argument("--mute", action="store_true")
    ap.add_argument("--fps", type=int)
    ap.add_argument("--quality", type=int)
    ap.add_argument("--results", type=int)
    ap.add_argument("--cells", type=int)
    ap.add_argument("--quant", type=int)
    ap.add_argument("--region")
    ap.add_argument("--backend", choices=BACKENDS)
    ap.add_argument("--dither", action="store_true")
    ap.add_argument("--subs", action="store_true")
    ap.add_argument("--no-thumbs", action="store_true")
    ap.add_argument("--control", choices=("auto", "keys", "line"),
                    help="how to control playback")
    ap.add_argument("--limit", type=float, metavar="SECONDS",
                    help="stop each video after this long (an exit that "
                         "needs no keyboard)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--record", metavar="FILE")
    ap.add_argument("--replay", metavar="FILE")
    args = ap.parse_args()

    enable_vt()
    install_handlers()

    cfg = Settings.load()
    for name in ("mode", "fps", "quality", "results", "threshold",
                 "backend", "cells", "quant", "region", "control"):
        value = getattr(args, name)
        if value is not None:
            setattr(cfg, name, value)
    if args.mute:
        cfg.audio = False
    if args.dither:
        cfg.dither = True
    if args.subs:
        cfg.subs = True
    if args.no_thumbs:
        cfg.thumbs = False
    LIMIT[0] = args.limit or 0.0
    if args.no_cache:
        cfg.cache_mb = 0
    cfg.sanitise()

    # First run: nothing configured yet, and the user did not ask for a
    # one-off command. Walk them through it once.
    if (not os.path.isfile(CONFIG) and not args.words
            and not args.bench and not args.replay):
        setup_screen(cfg, term_size()[0], first_run=True)

    state = load_state()
    words = list(args.words)
    verb = words[0].lower() if words else ""
    rest = words[1:]

    # -- commands that print and exit --------------------------------------
    if args.replay or verb == "replay":
        replay(args.replay or (rest[0] if rest else die("replay needs a file")))
        return
    if args.bench or verb == "bench":
        bench(cfg)
        return
    if verb in ("commands", "help"):
        print(COMMANDS)
        return
    if verb == "update":
        print("updating yt-dlp \u2026")
        problem = pip_install("yt-dlp")
        print(problem or "done. run `skjyt doctor` to check the version.")
        return
    if verb == "selftest":
        sys.exit(selftest(cfg))
    if verb == "probe":
        sys.exit(probe_input())
    if verb == "keys":
        sys.exit(keys_check())
    if verb == "doctor":
        sys.exit(doctor(cfg))
    if verb == "tune":
        suggested, limiter, ms, diff = autotune(cfg, lambda m: print("  " + m))
        print("  limited by %s" % limiter)
        print("  suggested %d cells, currently %d" % (suggested, cfg.cells))
        if rest[:1] == ["apply"]:
            cfg.cells = suggested
            cfg.sanitise()
            print("  " + (cfg.save() or "saved"))
        else:
            print("  add 'apply' to save it")
        return
    if verb == "setup":
        setup_screen(cfg, term_size()[0])
        return
    if verb == "cache":
        cmd_cache(rest, cfg)
        return
    if verb == "config":
        cmd_config(rest, cfg)
        return
    if verb == "history" and not rest[:1] == ["screen"]:
        cmd_history(rest, state)
        return
    if verb == "follow":
        if not rest:
            die("follow needs an @handle or url")
        url = channel_url(" ".join(rest))
        label = url.rstrip("/").split("/")[-1]      # @handle, not the full url
        print("followed %s" % label if follow(state, label, url)
              else "already following %s" % label)
        return
    if verb == "unfollow":
        if not rest:
            die("unfollow needs a name fragment")
        print("unfollowed %d" % unfollow(state, " ".join(rest)))
        return
    if verb == "muted":
        box = muted(state)
        if rest[:1] == ["clear"]:
            box["channels"] = []
            box["words"] = []
            state.pop("feed", None)
            save_state(state)
            print("cleared")
            return
        if not box["channels"] and not box["words"]:
            print("nothing hidden. press - in the feed to hide a source.")
        for name in box["channels"]:
            print("  channel  %s" % name)
        for word in box["words"]:
            print("  keyword  %s" % word)
        return
    if verb == "following" and not rest:
        rows = following(state)
        if not rows:
            print("not following anything. skjyt follow @someone")
        for r in rows:
            print("  %-28s %s" % (clip(r["name"], 28), r["url"]))
        return

    # -- commands that open a screen and can queue -------------------------
    recorder = Recorder(args.record) if args.record else None
    queue = []
    index = 0
    failures = 0
    one_shot = False
    cols = term_size()[0]

    try:
        found = None
        if verb == "feed":
            found = feed_screen(state, cfg, cols, force=True)
        elif verb == "popular":
            category = rest[0].lower() if rest and rest[0].lower() in POPULAR else "now"
            found = popular_screen(cfg, cols, category)
        elif verb == "channel":
            if not rest:
                die("channel needs an @handle or url")
            tab = "videos"
            if rest[-1].lower() in CHANNEL_TABS and len(rest) > 1:
                tab = rest[-1].lower()
                rest = rest[:-1]
            found = channel_screen(" ".join(rest), state, cfg, cols, tab)
        elif words:
            target = " ".join(words)
            one_shot = True
            if is_local(target) or is_url(target):
                if is_url(target) and "/watch" not in target and "youtu.be" not in target:
                    entries = playlist_entries(target)
                    if entries and len(entries) > 1:
                        found = search_screen(target, cfg, cols)
                    else:
                        found = [{"target": target, "title": target}]
                else:
                    found = [{"target": target, "title": target}]
            else:
                found = search_screen(target, cfg, cols)
        else:
            if menu(cfg, queue, state, cols) is None:
                return

        if found:
            queue.extend(found)
        elif words:
            return

        while True:
            if not queue or index >= len(queue) or index < 0:
                if one_shot:
                    return
                if menu(cfg, queue, state, term_size()[0]) is None:
                    return
                index = min(max(0, index), max(0, len(queue) - 1))
                continue

            info = "%d/%d" % (index + 1, len(queue)) if len(queue) > 1 else ""
            try:
                result = run_one(queue[index], cfg, state, recorder, info)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                # Anything unexpected in one item: say so, keep the session.
                restore_terminal()
                status("this one failed: " + describe_error(exc), WARN)
                result = "skip"

            if result == "skip":
                failures += 1
                remaining = len(queue) - index - 1
                if remaining > 0:
                    status("%d left in the queue" % remaining)
                    time.sleep(1.2)
                elif one_shot:
                    return 1 if failures else 0
                else:
                    ask("enter to continue \u203a ")
                index += 1
                continue

            failures = 0
            if result == "quit":
                if one_shot:
                    return
                index = len(queue)
            elif result == "prev":
                index = max(0, index - 1)
            else:
                index += 1
    except KeyboardInterrupt:
        pass
    finally:
        if recorder:
            recorder.close()
        save_state(state)
        restore_terminal()


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except BrokenPipeError:
        # Piping into head/less closes the pipe under us. Not an error.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except Exception:
            pass
        sys.exit(0)
