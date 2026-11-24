#!/usr/bin/env python3
"""
Free live-stream clipper.

* Records a live stream with yt-dlp -> ffmpeg in 30 s MPEG-TS pieces.
* Reads live chat (YouTube: yt-dlp's live_chat subtitle; Twitch: anonymous IRC).
* Every WINDOW seconds (default 10) it counts chat messages and measures the
  streamer's loudness. When BOTH spike versus a rolling baseline it cuts a
  vertical 30 s clip starting 20 s before the spike window, burns in
  faster-whisper captions plus a title made from what chat said, and saves it
  to clips/ while the stream is still live.

Usage:  python clipper.py https://www.twitch.tv/somechannel
        python clipper.py https://www.youtube.com/watch?v=XXXXXXXXXXX
"""
from __future__ import annotations

import argparse
import atexit
import collections
import json
import logging
import math
import os
import queue
import random
import re
import shutil
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

log = logging.getLogger("clipper")
SR = 16000  # sample rate used for loudness + whisper
# keep the audio on the video's clock: a gap in the timestamps becomes silence instead of being squeezed out,
# which would slide the sound (and the captions built from it) against the picture
SYNC_AF = "aresample=async=1:first_pts=0"
CHILDREN: list[subprocess.Popen] = []  # helper processes (chat yt-dlp) to stop when we quit


def kill_children() -> None:
    for p in CHILDREN:
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass


atexit.register(kill_children)

STOPWORDS = set(
    "the a an and or but is are was were be been to of in on at it its this that these those "
    "i you he she we they me my your our so for with just not do did does have has had im "
    "what how why who when if then than as by from up out".split()
)


# --------------------------------------------------------------------------- helpers
def ytdlp_cmd() -> list[str]:
    exe = shutil.which("yt-dlp")
    return [exe] if exe else [sys.executable, "-m", "yt_dlp"]


def detect_platform(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    if "twitch.tv" in host:
        return "twitch"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    return None


def probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def probe_start(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=start_time", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def join_segments(files: list[Path], out: Path, work: Path) -> None:
    """Glue recorded segments into ONE continuous transport stream, byte for byte.

    ffmpeg's concat demuxer moves the audio against the video by 0.04-0.08 s at every join, which made the
    picture lag behind the sound (captions are built from the sound, so they came out early). The recorder writes
    segments on one continuous timeline, so plain byte-joining keeps video and audio exactly where they were. If a
    segment's timestamps jump (e.g. the recorder reconnected), that segment is shifted as a whole - video and audio
    together - so it starts right where the previous one ended."""
    prev_end = None
    with open(out, "wb") as o:
        for i, f in enumerate(files):
            start, dur, src = probe_start(f), probe_duration(f), f
            if prev_end is not None and abs(start - prev_end) > 0.5:
                tmp = work / f"shift_{i}.ts"
                r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-copyts", "-i", str(f), "-c", "copy",
                                    "-output_ts_offset", f"{prev_end - start:.3f}", "-muxdelay", "0", "-muxpreload", "0",
                                    "-f", "mpegts", str(tmp)], capture_output=True)
                if r.returncode == 0 and tmp.is_file():
                    src, start = tmp, probe_start(tmp)
            with open(src, "rb") as fh:
                shutil.copyfileobj(fh, o)
            prev_end = start + dur


def decode_audio(path: Path) -> np.ndarray:
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"],
        capture_output=True,
    )
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def load_wav_16k(path: Path) -> np.ndarray:
    """Read the 16 kHz mono 16-bit wav that ffmpeg wrote, as float32 samples in [-1, 1]."""
    import wave
    with wave.open(str(path), "rb") as w:
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def find_font(args):
    """Return (font_file, family) to hand to libass. ffmpeg on Windows often has no usable fontconfig, in which
    case subtitles render as nothing at all - so we always ship a real font file with the render."""
    if args.font_file:
        return Path(args.font_file), args.font_name
    win = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for path, family in [
        (win / "arialbd.ttf", "Arial"), (win / "arial.ttf", "Arial"), (win / "segoeuib.ttf", "Segoe UI"),
        (win / "verdanab.ttf", "Verdana"), (win / "tahomabd.ttf", "Tahoma"),
        (Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"), "Arial"),
        (Path("/Library/Fonts/Arial Bold.ttf"), "Arial"),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"), "DejaVu Sans"),
        (Path("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"), "DejaVu Sans"),
        (Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"), "Liberation Sans"),
    ]:
        if path.is_file():
            return path, family
    return None


# --------------------------------------------------------------------------- Hindi -> Roman (Hinglish)
_CONS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "ng", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "ळ": "l", "व": "v",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
}
_NUKTA = {"क": "q", "ख": "kh", "ग": "g", "ज": "z", "ड": "d", "ढ": "dh", "फ": "f"}
_VOWELS = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri", "ए": "e", "ऐ": "ai",
           "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e"}
_MATRAS = {"ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o", "ौ": "au",
           "ॉ": "o", "ॅ": "e"}
_HALANT, _NUKTA_MARK = "्", "़"
_DEV_RUN = re.compile(r"[ऀ-ॿ]+")
# the commonest words, spelled the way people type Hindi in Roman letters
_HINGLISH_WORDS = {
    "है": "hai", "हैं": "hain", "हूँ": "hoon", "हूं": "hoon", "हो": "ho", "था": "tha", "थी": "thi", "थे": "the",
    "रहा": "raha", "रही": "rahi", "रहे": "rahe", "क्या": "kya", "ये": "ye", "यह": "yeh", "वो": "wo", "वह": "woh",
    "नहीं": "nahi", "नही": "nahi", "मैं": "main", "मैंने": "maine", "में": "mein", "मे": "me", "और": "aur",
    "तो": "to", "भी": "bhi", "से": "se", "को": "ko", "का": "ka", "की": "ki", "के": "ke", "कि": "ki", "ही": "hi",
    "पर": "par", "यार": "yaar", "भाई": "bhai", "अरे": "arre", "अब": "ab", "कुछ": "kuch", "बहुत": "bahut",
    "मत": "mat", "चलो": "chalo", "देखो": "dekho", "हुआ": "hua", "गया": "gaya", "गई": "gayi", "गयी": "gayi",
    "सही": "sahi", "ठीक": "theek", "कैसे": "kaise", "कौन": "kaun", "कब": "kab", "कहाँ": "kahan", "कहां": "kahan",
    "क्यों": "kyon", "क्यूँ": "kyun", "अच्छा": "accha", "बस": "bas", "जल्दी": "jaldi", "पता": "pata",
    "चाहिए": "chahiye", "लेकिन": "lekin", "अभी": "abhi", "सब": "sab", "एक": "ek", "मेरा": "mera", "मेरी": "meri",
    "तेरा": "tera", "तुम": "tum", "तू": "tu", "आप": "aap", "हम": "hum", "वहाँ": "wahan", "वहां": "wahan",
    "यहाँ": "yahan", "यहां": "yahan", "इसको": "isko", "उसको": "usko", "मुझे": "mujhe", "तुझे": "tujhe",
    "हमें": "hamein", "उसने": "usne", "इसने": "isne", "कर": "kar", "करो": "karo", "करना": "karna",
    "कैसा": "kaisa", "ऐसा": "aisa", "वैसा": "waisa", "जैसा": "jaisa", "कितना": "kitna", "इतना": "itna",
    "ओके": "okay", "हाँ": "haan", "हां": "haan", "जी": "ji", "अरे": "arre", "चल": "chal", "बोल": "bol",
    "सुन": "sun", "देख": "dekh", "वाह": "wah", "आज": "aaj", "कल": "kal", "फिर": "phir", "तभी": "tabhi",
}


def _dev_word_to_roman(w: str) -> str:
    import unicodedata as ud
    w = ud.normalize("NFD", w)
    units, i, n = [], 0, len(w)  # unit = [consonant cluster, explicit vowel | None (inherent a) | "" (none), nasal]
    while i < n:
        ch = w[i]
        if ch in _CONS:
            cluster = []
            while True:
                r = _CONS[w[i]]
                if i + 1 < n and w[i + 1] == _NUKTA_MARK:
                    r = _NUKTA.get(w[i], r)
                    i += 1
                cluster.append(r)
                i += 1
                if i + 1 < n and w[i] == _HALANT and w[i + 1] in _CONS:
                    i += 1
                    continue
                break
            vowel = None
            if i < n and w[i] in _MATRAS:
                vowel = _MATRAS[w[i]]
                i += 1
            elif i < n and w[i] == _HALANT:
                vowel = ""
                i += 1
            c = "".join(cluster)
            units.append([("gy" if c == "jny" else c), vowel, ""])
        elif ch in _VOWELS:
            units.append(["", _VOWELS[ch], ""])
            i += 1
        elif ch in "ंँ":
            if units:
                units[-1][2] = "n"
            i += 1
        elif ch == "ः":
            if units:
                units[-1][2] = "h"
            i += 1
        elif ch == "।":
            units.append(["", "", ""]); units[-1][0] = "."
            i += 1
        elif "०" <= ch <= "९":
            units.append([str(ord(ch) - ord("०")), "", ""])
            i += 1
        else:
            i += 1  # stray nukta, zero-width joiner, ...
    out, kept_prev = [], False
    last = len(units) - 1
    for k, (cons, vowel, nasal) in enumerate(units):
        if vowel is None:  # inherent 'a': drop it at the end of a word and in the usual medial pattern
            nxt = units[k + 1][1] if k < last else None
            delete = (k == last and k > 0 and not nasal) or (
                0 < k < last and kept_prev and nxt not in (None, ""))
            v = "" if delete else "a"
        elif vowel == "aa":
            v = "a" if (k == last and cons) else "aa"
        elif vowel == "ee":
            v = "i" if (k == last and cons) else "ee"
        elif vowel == "oo":
            v = "u" if (k == last and cons) else "oo"
        else:
            v = vowel
        kept_prev = bool(v) or (cons == "" and bool(vowel))
        out.append(cons + v + nasal)
    return "".join(out)


def hindi_to_roman(text: str) -> str:
    """'ये क्या कर रहा है' -> 'ye kya kar raha hai'. Latin letters (English words) are left as they are."""
    def conv(m: re.Match) -> str:
        w = m.group(0)
        core = w.rstrip("।")
        tail = "." * (len(w) - len(core))
        return (_HINGLISH_WORDS.get(core) or _dev_word_to_roman(core)) + tail
    return _DEV_RUN.sub(conv, text)


HALLUCINATIONS = ("thanks for watching", "thank you for watching", "subtitles by", "amara.org", "subscribe",
                  "सब्सक्राइब", "देखने के लिए धन्यवाद", "www.", "♪", "[music]", "(music)")


def slugify(text: str, n: int = 40) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:n].strip("-") or "clip"


# --------------------------------------------------------------------------- chat store
class ChatStore:
    """Thread-safe per-window chat counts and message samples (keyed by wall-clock window index)."""

    def __init__(self, window: int):
        self.w = window
        self.lock = threading.Lock()
        self.counts: collections.Counter[int] = collections.Counter()
        self.texts: dict[int, list[str]] = collections.defaultdict(list)
        self.last_msg = 0.0

    def add(self, ts: float, text: str) -> None:
        k = int(ts // self.w)
        with self.lock:
            self.counts[k] += 1
            if len(self.texts[k]) < 600:
                self.texts[k].append(text)
            self.last_msg = time.time()

    def count(self, k: int) -> int:
        with self.lock:
            return self.counts.get(k, 0)

    def messages(self, ks) -> list[str]:
        with self.lock:
            return [m for k in ks for m in self.texts.get(k, [])]

    def prune(self, before: int) -> None:
        with self.lock:
            for k in [k for k in self.counts if k < before]:
                self.counts.pop(k, None)
                self.texts.pop(k, None)


# --------------------------------------------------------------------------- chat sources
TWITCH_RE = re.compile(r"^(?:@(?P<tags>\S+) )?:(?P<user>[^!\s]+)![^ ]+ PRIVMSG #\S+ :(?P<msg>.*)$")


def twitch_chat(channel: str, store: ChatStore, stop: threading.Event, shift: float) -> None:
    """Anonymous Twitch IRC (justinfan nick, no OAuth needed)."""
    host = "irc.chat.twitch.tv"
    while not stop.is_set():
        try:
            raw = socket.create_connection((host, 6697), timeout=30)
            s = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            s.settimeout(30)
            for line in (
                "CAP REQ :twitch.tv/tags",
                "PASS SCHMOOPIIE",
                f"NICK justinfan{random.randint(10000, 99999)}",
                f"JOIN #{channel}",
            ):
                s.sendall((line + "\r\n").encode())
            log.info("twitch chat: joined #%s anonymously", channel)
            buf = b""
            while not stop.is_set():
                try:
                    data = s.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data
                while b"\r\n" in buf:
                    raw_line, buf = buf.split(b"\r\n", 1)
                    line = raw_line.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        s.sendall(b"PONG :tmi.twitch.tv\r\n")
                        continue
                    m = TWITCH_RE.match(line)
                    if m:
                        text = m.group("msg").replace("\x01ACTION ", "").strip("\x01").strip()
                        if text:
                            store.add(time.time() + shift, text)
        except Exception as e:  # network hiccup -> reconnect
            log.warning("twitch chat error: %s", e)
        finally:
            try:
                s.close()  # type: ignore[name-defined]
            except Exception:
                pass
        stop.wait(5)


def _iter_renderers(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if k in ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer") and isinstance(v, dict):
                yield v
            else:
                yield from _iter_renderers(v)
    elif isinstance(o, list):
        for x in o:
            yield from _iter_renderers(x)


def parse_youtube_chat_line(line: str):
    """Yield (epoch_seconds, text, id) for every chat message found in one yt-dlp live_chat JSON line."""
    try:
        obj = json.loads(line)
    except ValueError:
        return
    for r in _iter_renderers(obj):
        runs = (r.get("message") or {}).get("runs") or []
        parts = []
        for run in runs:
            if "text" in run:
                parts.append(run["text"])
            elif "emoji" in run:
                sc = run["emoji"].get("shortcuts") or []
                parts.append(" " + (sc[0] if sc else run["emoji"].get("emojiId", "")) + " ")
        text = re.sub(r"\s+", " ", "".join(parts)).strip()
        try:
            ts = int(r["timestampUsec"]) / 1e6
        except (KeyError, ValueError):
            ts = time.time()
        if text:
            yield ts, text, r.get("id")


def youtube_chat(url: str, chat_dir: Path, store: ChatStore, stop: threading.Event, shift: float,
                 extra: list[str]) -> None:
    """Live chat through yt-dlp: it writes <chat_dir>/chat.live_chat.json as the stream runs; we tail it."""
    chat_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    logf = open(chat_dir / "ytdlp_chat.log", "ab")
    while not stop.is_set():
        for f in chat_dir.glob("chat*"):  # best effort: a stray old process may still hold a file (Windows)
            if f.name != "ytdlp_chat.log":
                try:
                    f.unlink()
                except OSError:
                    pass
        base = f"chat_{int(time.time())}"  # fresh name every start, so a locked old file never matters
        cmd = ytdlp_cmd() + ["--skip-download", "--write-subs", "--sub-langs", "live_chat", "--no-part",
                             "--no-warnings", "--no-playlist", "-o", str(chat_dir / base), *extra, url]
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
        CHILDREN.append(proc)
        fh, buf = None, ""
        log.info("youtube chat: yt-dlp live_chat started")
        try:
            while not stop.is_set():
                if fh is None:
                    files = sorted(chat_dir.glob(f"{base}*.live_chat.json*"))
                    if files:
                        fh = open(files[0], "r", encoding="utf-8", errors="replace")
                chunk = fh.read() if fh else ""
                if chunk:
                    buf += chunk
                    *lines, buf = buf.split("\n")
                    for ln in lines:
                        for ts, text, mid in parse_youtube_chat_line(ln):
                            if mid and mid in seen:
                                continue
                            if mid:
                                seen.add(mid)
                            store.add(ts + shift, text)
                    continue
                if proc.poll() is not None:
                    break
                time.sleep(1)
        except Exception as e:  # never let the chat thread die silently
            log.warning("youtube chat error: %s", e)
        finally:
            if proc.poll() is None:
                proc.terminate()
            if fh:
                fh.close()
        if stop.wait(15):
            break
        log.info("youtube chat: restarting yt-dlp live_chat")


def resolve_stream_urls(url: str, a) -> tuple[list[str], list[str]]:
    """Ask yt-dlp for the direct stream URL(s). YouTube live often offers only separate video-only and
    audio-only HLS streams (2 URLs); Twitch is usually one muxed stream (1 URL)."""
    h = a.max_height
    fmt = f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
    r = subprocess.run(ytdlp_cmd() + ["--no-warnings", "--no-playlist", "-f", fmt, "-g", *a.ytdlp_args, url],
                       capture_output=True, text=True, timeout=120)
    urls = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("http")]
    return urls, (r.stderr or "").strip().splitlines()


DEFAULT_CAM = (0.68, 0.55, 0.32, 0.45)  # bottom-right corner, the most common facecam spot
CAM_PRESETS = {
    "bottom-right": DEFAULT_CAM, "bottom-left": (0.0, 0.55, 0.32, 0.45),
    "top-right": (0.68, 0.0, 0.32, 0.45), "top-left": (0.0, 0.0, 0.32, 0.45),
}


def expand_rect(r, k: float, dy: float = 0.0):
    """Grow a rect by factor k around its centre, then move it down by dy * (new height); stays in the frame."""
    x, y, w, h = r
    nw, nh = min(1.0, w * k), min(1.0, h * k)
    return (min(max(0.0, x - (nw - w) / 2), 1 - nw), min(max(0.0, y - (nh - h) / 2 + dy * nh), 1 - nh), nw, nh)


def detect_cam_region(frames: list[Path]):
    """Find the streamer's facecam automatically: a face that stays at the SAME spot in several frames
    (in-game faces move around). Returns (x, y, w, h) as frame fractions with a 9:8 shape that fills the
    top half of the clip, or None if no steady face was found."""
    try:
        import cv2
    except ImportError:
        log.warning('automatic facecam detection needs OpenCV:  pip install "opencv-python-headless==4.10.0.84"')
        return None
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if casc.empty():
        log.warning('OpenCV has no face model (OpenCV 5 dropped it) - install:  '
                    'pip install "opencv-python-headless==4.10.0.84"')
        return None
    hits, n_frames, aspect = [], 0, 16 / 9
    for f in frames:
        img = cv2.imread(str(f))
        if img is None:
            continue
        n_frames += 1
        gh, gw = img.shape[:2]
        aspect = gw / gh
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        for (x, y, fw, fh) in casc.detectMultiScale(gray, 1.1, 6, minSize=(max(24, int(0.04 * gh)),) * 2):
            hits.append(((x + fw / 2) / gw, (y + fh / 2) / gh, fh / gh))
    if not hits:
        return None
    best: list = []
    for cx, cy, _ in hits:
        grp = [t for t in hits if abs(t[0] - cx) < 0.06 and abs(t[1] - cy) < 0.08]
        if len(grp) > len(best):
            best = grp
    if len(best) < (1 if n_frames <= 2 else 2):
        return None
    cx = sum(t[0] for t in best) / len(best)
    cy = sum(t[1] for t in best) / len(best)
    fh = sum(t[2] for t in best) / len(best)
    hh = min(1.0, 2.4 * fh)                        # head + shoulders, as a fraction of frame height
    ww = hh * 9 / 8 / aspect                       # 9:8 in pixels -> fraction of frame width
    if ww > 1.0:
        ww, hh = 1.0, 1.0 * aspect * 8 / 9
    x = min(max(0.0, cx - ww / 2), 1 - ww)
    y = min(max(0.0, cy + 0.08 * hh - hh / 2), 1 - hh)  # face sits a little above the middle
    return (x, y, ww, hh)


def make_layout_preview(args) -> None:
    """Save one frame with the cam (red) and game (green) boxes, plus the finished 9:16 split, then exit."""
    work = args.work
    work.mkdir(parents=True, exist_ok=True)
    if Path(args.url).is_file():  # a local video/image also works
        video = args.url
    else:
        urls, err = resolve_stream_urls(args.url, args)
        if not urls:
            sys.exit("yt-dlp could not get the stream: " + (" | ".join(err[-3:]) or "no output"))
        video = urls[0]
    frame, boxes, result = work / "layout_frame.png", work / "layout_boxes.png", work / "layout_result.png"
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", video, "-frames:v", "1", str(frame)],
                       capture_output=True, text=True)
    if r.returncode or not frame.is_file():
        sys.exit("could not grab a frame: " + (r.stderr or "").strip()[-300:])
    vw, vh = probe_size(frame)
    cam_rect, detected = args.cam_rect, False
    if args.cam_auto:
        for old in work.glob("camdet_*.png"):
            old.unlink()
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", video, "-vf", "fps=1/2,scale=960:-2", "-frames:v", "5",
                        str(work / "camdet_%02d.png")], capture_output=True, text=True)
        found = detect_cam_region(sorted(work.glob("camdet_*.png")))
        if found:
            cam_rect, detected = found, True
            print("Automatic facecam detection: found the streamer's face.")
        else:
            print("Automatic facecam detection: NO face found - showing the default bottom-right area instead.\n"
                  "  Send me .clipper_work\\layout_frame.png or set --cam-region (bottom-left, top-right, ... or x,y,w,h).")
    cam, game = compute_layout(cam_rect, args.game_rect, vw, vh, args.bottom)
    hide = ((expand_rect(cam, 2.2, 0.22) if detected else cam), args.hide_cam) if (
        args.bottom == "full" and args.hide_cam != "off" and (detected or not args.cam_auto)) else None
    box = lambda rect, col: ("drawbox=x=iw*{:.5f}:y=ih*{:.5f}:w=iw*{:.5f}:h=ih*{:.5f}:color={}:t=8"
                             .format(*rect, col))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(frame), "-vf",
                    f"{box(cam, 'red')}" + (f",{box(game, 'lime')}" if game else ""), str(boxes)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(frame), "-filter_complex",
                    build_filter("split", False, False, (cam, game, hide)), "-map", "[v]", "-frames:v", "1",
                    str(result)], check=True)
    print(f"Frame size {vw}x{vh}")
    print(f"  cam  (red box)   = {cam[0]:.3f},{cam[1]:.3f},{cam[2]:.3f},{cam[3]:.3f}")
    if game:
        print(f"  game (green box) = {game[0]:.3f},{game[1]:.3f},{game[2]:.3f},{game[3]:.3f}")
    else:
        print("  bottom half     = the whole frame (nothing cut off)")
    print(f"Saved:\n  {boxes}   <- where the cam and game areas are\n  {result}   <- the finished 9:16 layout")
    print("Wrong? change --cam-region x,y,w,h (fractions 0-1: x,y = top-left corner of the facecam) and run again.")


# --------------------------------------------------------------------------- recorder
class Recorder(threading.Thread):
    """yt-dlp (stdout) | ffmpeg segment muxer -> seg_000001.ts, seg_000002.ts ... (seg_seconds each)."""

    def __init__(self, url: str, seg_dir: Path, args, stop: threading.Event):
        super().__init__(daemon=True)
        self.url, self.seg_dir, self.args, self.stop = url, seg_dir, args, stop
        self.active = False
        self.done = False

    def _next_index(self) -> int:
        idx = [int(p.stem.split("_")[1]) for p in self.seg_dir.glob("seg_*.ts")]
        return max(idx, default=0) + 1

    def _run_once(self) -> None:
        a = self.args
        urls, err = resolve_stream_urls(self.url, a)
        if not urls:
            log.warning("yt-dlp could not get a stream URL: %s", " | ".join(err[-3:]) or "no output")
            return
        # -copyts keeps each stream's real timestamps, so separately opened video and audio stay in sync
        # (without it ffmpeg rebases every input to 0 and the audio drifts by however far apart they opened)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts", "-copyts"]
        for i, u in enumerate(urls[:2]):
            if u.startswith("http"):
                cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
            if i == 1 and len(urls) >= 2 and a.audio_offset:
                cmd += ["-itsoffset", str(a.audio_offset)]
            cmd += ["-i", u]
        if len(urls) >= 2:
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        else:
            cmd += ["-map", "0:v:0?", "-map", "0:a:0?"]
        cmd += ["-c", "copy", "-f", "segment", "-segment_time", str(a.seg_seconds), "-segment_format", "mpegts",
                "-segment_start_number", str(self._next_index()),
                str(self.seg_dir / "seg_%06d.ts")]
        logf = open(self.seg_dir.parent / "recorder.log", "ab")
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=logf)
        log.info("recording started (%d stream input%s)", len(urls[:2]), "s" if len(urls) > 1 else "")
        try:
            while p.poll() is None and not self.stop.is_set():
                time.sleep(1)
        finally:
            if p.poll() is None:
                p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
            logf.close()
        if p.returncode not in (0, None, -15) and not self.stop.is_set():
            try:
                tail = (self.seg_dir.parent / "recorder.log").read_text(errors="replace").strip().splitlines()[-3:]
                log.warning("ffmpeg recorder stopped: %s", " | ".join(tail))
            except OSError:
                pass

    def run(self) -> None:
        fails = 0
        while not self.stop.is_set():
            before = len(list(self.seg_dir.glob("seg_*.ts")))
            self.active = True
            self._run_once()
            self.active = False
            if self.stop.is_set():
                break
            produced = len(list(self.seg_dir.glob("seg_*.ts"))) > before
            fails = 0 if produced else fails + 1
            if fails >= self.args.retries:
                log.info("stream is offline / ended (see %s) - stopping", self.seg_dir.parent / "recorder.log")
                break
            log.info("recorder exited, retrying in %ss (%d/%d)", self.args.retry_wait, fails, self.args.retries)
            self.stop.wait(self.args.retry_wait)
        self.done = True


# --------------------------------------------------------------------------- titles
def make_title(messages: list[str], fallback: str = "Stream highlight") -> str:
    clean = []
    for m in messages:
        m = re.sub(r"https?://\S+", "", m)
        m = re.sub(r"\s+", " ", m).strip()
        if m and not m.startswith("!") and len(m) <= 120:
            clean.append(m)
    if not clean:
        return fallback
    by_norm = collections.Counter(m.lower() for m in clean)
    dominant = [m for m, c in by_norm.most_common(3) if c >= 2]
    if dominant:
        picked = []
        for low in dominant[:2]:
            originals = collections.Counter(m for m in clean if m.lower() == low)
            picked.append(originals.most_common(1)[0][0])
        return " / ".join(picked)[:70]
    toks = lambda m: [t for t in re.findall(r"[\w']+", m.lower()) if t not in STOPWORDS and len(t) > 1]
    freq = collections.Counter(t for m in clean for t in toks(m))

    def score(m: str) -> float:
        t = toks(m)
        return sum(freq[x] for x in t) / math.sqrt(max(1, len(t))) if t else 0.0

    best = max(set(clean), key=lambda m: (score(m), -len(m)))
    return best.strip(" .")[:70] or fallback


# --------------------------------------------------------------------------- captions (ASS)
def group_words(words, max_words: int = 5, max_dur: float = 3.0, max_gap: float = 0.7):
    cues, cur = [], []

    def flush():
        if cur:
            cues.append([cur[0][0], cur[-1][1], " ".join(w[2] for w in cur)])
            cur.clear()

    for w in words:
        if cur and (len(cur) >= max_words or w[1] - cur[0][0] > max_dur or w[0] - cur[-1][1] > max_gap
                    or cur[-1][2].endswith((".", "?", "!"))):
            flush()
        cur.append(w)
    flush()
    for i, c in enumerate(cues):  # no overlaps, minimum on-screen time
        nxt = cues[i + 1][0] if i + 1 < len(cues) else c[1] + 1
        c[1] = max(c[1], min(c[0] + 0.35, nxt))
        c[1] = min(c[1], nxt)
    return cues


def snap_cues_to_audio(cues, x: np.ndarray, sr: int = SR):
    """Whisper often stamps the first word after a pause BEFORE the voice really starts, so the caption shows up
    early. Move each caption's start to the moment sound actually begins (never earlier, only when the audio just
    before it is clearly quieter). Loud continuous game audio simply means nothing is moved."""
    hop = int(0.02 * sr)
    n = x.size // hop
    if not cues or n < 10:
        return cues
    frames = x[: n * hop].astype(np.float64).reshape(n, hop)
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(1)) + 1e-6)
    thr = float(np.percentile(db, 10)) + 12.0      # 12 dB above the quiet floor of this clip = "sound"
    out = []
    for st, en, text in cues:
        i0, i1 = int(max(0.0, st) / 0.02), min(n, int(min(en - 0.2, st + 0.8) / 0.02))
        j = i0
        while j < i1 and db[j] < thr:
            j += 1
        new = j * 0.02 - 0.04                       # 40 ms lead-in
        out.append([new if (j < i1 and new - st >= 0.08) else st, en, text])
    return out


def _ass_t(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _ass_esc(t: str) -> str:
    return t.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(title: str | None, cues, length: float, upper: bool = True, title_seconds: float = 5.0,
              font: str = "Arial", position: str = "bottom") -> str:
    """position 'middle' centres the captions on the screen (the seam of the split layout)."""
    if position == "middle":  # ASS alignment 5 = middle-centre
        cap_style = "Caption,Arial,74,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,5,60,60,0,1"
        title_style = "Title,Arial,56,&H0000E0FF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,8,70,70,60,1"
    else:
        cap_style = "Caption,Arial,78,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,60,60,430,1"
        title_style = "Title,Arial,62,&H0000E0FF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,8,70,70,170,1"
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: {cap_style}\nStyle: {title_style}\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    if title:
        lines.append(f"Dialogue: 1,{_ass_t(0)},{_ass_t(min(title_seconds, length))},Title,,0,0,0,,{_ass_esc(title)}")
    for s, e, text in cues:
        text = text.upper() if upper else text
        lines.append(f"Dialogue: 0,{_ass_t(s)},{_ass_t(e)},Caption,,0,0,0,,{_ass_esc(text)}")
    return head.replace(",Arial,", f",{font},") + "\n".join(lines) + "\n"


def parse_region(s: str, name: str) -> tuple[float, float, float, float]:
    """'x,y,w,h' as fractions of the frame (0-1), e.g. 0.70,0.55,0.30,0.45"""
    try:
        x, y, w, h = (float(v) for v in s.split(","))
    except ValueError:
        sys.exit(f"{name} needs 4 numbers x,y,w,h as fractions of the frame, e.g. 0.70,0.55,0.30,0.45")
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1.001 and y + h <= 1.001):
        sys.exit(f"{name}: every value must be between 0 and 1 and the box must stay inside the frame "
                 f"(x+w <= 1 and y+h <= 1)")
    return x, y, w, h


def probe_size(path: Path) -> tuple[int, int]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        w, h = r.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except ValueError:
        return 1920, 1080


def compute_layout(cam, game, vw: int, vh: int, bottom: str = "full"):
    """Return (cam_rect, game_rect) as fractions of the frame.
    bottom='full' (default): game_rect is None = the bottom half shows the WHOLE frame, nothing cut off.
    bottom='game': the bottom half is a crop that stays clear of the facecam - the widest 9:8 area,
    unless --game-region is given."""
    if game is None and bottom == "full":
        return cam, None
    if game is None:
        gw = min(1.0, 1.125 * vh / vw)
        gh = 1.0 if gw < 1.0 else min(1.0, (8 / 9) * vw / vh)
        xs = [0.0, 1 - gw, (1 - gw) / 2]  # left, right, centre
        if cam[0] + cam[2] / 2 < 0.5:  # cam on the left -> prefer the right side
            xs = [1 - gw, 0.0, (1 - gw) / 2]

        def overlap(x: float) -> float:
            return max(0.0, min(x + gw, cam[0] + cam[2]) - max(x, cam[0]))

        best = min(xs, key=overlap)
        game = (best, (1 - gh) / 2, gw, gh)
        if overlap(best) > 0:
            log.warning("the facecam overlaps the game area - set --cam-region / --game-region "
                        "(run with --preview-layout to see them)")
    return cam, game


def _crop(r) -> str:
    x, y, w, h = r
    ev = lambda v, ax: f"trunc({ax}*{v:.5f}/2)*2"  # even numbers keep yuv420p happy
    return f"crop={ev(w, 'iw')}:{ev(h, 'ih')}:{ev(x, 'iw')}:{ev(y, 'ih')}"


def build_filter(mode: str, with_subs: bool, fontsdir: bool = False, layout=None) -> str:
    if mode == "split":
        cam, game, hide = layout
        top = f"[c]{_crop(cam)},scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960,setsar=1[top];"
        if game is None:  # whole frame, scaled to the full 1080 width, on a blurred copy of itself - no side cut
            hid = "[gb]null[gh];"  # optionally remove the facecam from the bottom half (it is shown on top)
            if hide:
                hr, hmode = hide
                if hmode == "black":
                    hid = ("[gb]drawbox=x=iw*{:.5f}:y=ih*{:.5f}:w=iw*{:.5f}:h=ih*{:.5f}:color=black:t=fill[gh];"
                           .format(*hr))
                else:
                    hid = (f"[gb]split=2[gb1][gb2];[gb2]{_crop(hr)},boxblur=45:6[hb];"
                           f"[gb1][hb]overlay=x=trunc(W*{hr[0]:.5f}/2)*2:y=trunc(H*{hr[1]:.5f}/2)*2[gh];")
            core = ("[0:v]split=3[c][ga][gb];" + top + hid +
                    "[ga]scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960,boxblur=30:4[gbg];"
                    "[gh]scale=1080:960:force_original_aspect_ratio=decrease[gfg];"
                    "[gbg][gfg]overlay=(W-w)/2:(H-h)/2,setsar=1[bot];"
                    "[top][bot]vstack=inputs=2")
        else:
            core = ("[0:v]split=2[c][g];" + top +
                    f"[g]{_crop(game)},scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960,setsar=1[bot];"
                    "[top][bot]vstack=inputs=2")
    elif mode == "crop":
        core = "[0:v]crop=trunc(ih*9/32)*2:ih,scale=1080:1920,setsar=1"
    else:  # blurred-background letterbox: the whole frame stays visible
        core = ("[0:v]split=2[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,boxblur=30:4[bg];[b]scale=1080:-2[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")
    subs = ",subtitles=clip.ass" + (":fontsdir=fonts" if fontsdir else "")
    return core + (subs if with_subs else "") + "[v]"


# --------------------------------------------------------------------------- clip worker
@dataclass
class Job:
    anchor: float
    dir: Path
    files: list[Path]
    offset: float
    length: float
    title: str
    stats: dict
    chat_sample: list[str] = field(default_factory=list)


class ClipWorker(threading.Thread):
    def __init__(self, args):
        super().__init__(daemon=True)
        self.args = args
        self.q: queue.Queue[Job | None] = queue.Queue()
        self.model = None
        self.auto_cam = None  # facecam area found automatically (cached once found)

    def transcribe(self, wav: Path):
        a = self.args
        if a.no_captions:
            return []
        try:
            if self.model is None:
                from faster_whisper import WhisperModel
                ctype = a.compute_type
                if ctype == "auto" and a.device in ("auto", "cpu"):
                    try:  # no NVIDIA GPU -> int8 is much faster on a CPU than the float32 fallback
                        import ctranslate2
                        if ctranslate2.get_cuda_device_count() == 0:
                            ctype = "int8"
                    except Exception:
                        ctype = "int8"
                log.info("loading faster-whisper model '%s' (%s; first run downloads it) ...", a.whisper_model, ctype)
                self.model = WhisperModel(a.whisper_model, device=a.device, compute_type=ctype)
            # pass raw samples: avoids faster-whisper's PyAV file decoding (version mismatches break it)
            segs, info = self.model.transcribe(
                load_wav_16k(wav), word_timestamps=True, vad_filter=not a.no_vad, beam_size=5,
                language=a.language or None, task="transcribe", condition_on_previous_text=False,
                no_speech_threshold=0.6, log_prob_threshold=-1.0, compression_ratio_threshold=2.4,
                initial_prompt=a.prompt or None)
            words, dropped = [], 0
            for s in segs:
                text = (getattr(s, "text", "") or "").strip()
                # keep only what looks like real talking: drop music / game noise / typical whisper inventions
                if (getattr(s, "no_speech_prob", 0.0) > 0.6 or getattr(s, "avg_logprob", 0.0) < -1.0
                        or getattr(s, "compression_ratio", 0.0) > 2.4
                        or any(h in text.lower() for h in HALLUCINATIONS)):
                    dropped += 1
                    continue
                for w in (s.words or []):
                    t = w.word.strip()
                    if t:
                        words.append((w.start, w.end, hindi_to_roman(t) if a.script == "roman" else t))
            if dropped:
                log.info("captions: ignored %d segment(s) that did not look like the streamer talking "
                         "(music / game audio / noise)", dropped)
        except Exception as e:  # clip is still saved, just without captions
            log.error("CAPTIONS FAILED: %s  (check: python -c \"import faster_whisper\")", e)
            return []
        log.info("captions: %d words (language=%s, confidence=%.2f)", len(words), info.language,
                 info.language_probability)
        if not words:
            log.warning("captions: no speech found in this clip - if the streamer was talking, try --no-vad "
                        "or a bigger --whisper-model")
        return words

    def _ffmpeg(self, cmd: list[str], cwd: Path) -> None:
        r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", *cmd], cwd=cwd,
                           capture_output=True)
        err = (r.stderr or b"").decode(errors="replace").strip()
        if r.returncode:
            raise subprocess.CalledProcessError(r.returncode, cmd, r.stdout, r.stderr)
        noise = ("could not seek to position", "co located POCs", "mmco: unref short", "Last message repeated",
                 "Packet corrupt", "corrupt input packet")  # the last two: harmless counter reset where segments join
        lines = [ln for ln in err.splitlines() if ln.strip() and not any(n in ln for n in noise)]
        if lines:  # e.g. libass "fontselect: failed to find any fallback" = captions would be invisible
            log.warning("ffmpeg says: %s", " | ".join(lines[-4:]))

    def process(self, job: Job) -> Path:
        a, d = self.args, job.dir
        # Join the segments byte-for-byte into ONE continuous transport stream. (ffmpeg's concat demuxer put a small
        # gap into the video at every segment join, so the picture slid behind the audio and the captions, which are
        # built from the audio, came out early - by a different amount in every clip.)
        join_segments(job.files, d / "joined.ts", d)
        # -ss/-t as INPUT options so frame timestamps start at 0 and the ASS cue times line up with the clip
        src = ["-ss", f"{job.offset:.3f}", "-t", f"{job.length:.3f}", "-i", "joined.ts"]
        self._ffmpeg(src + ["-vn", "-af", SYNC_AF, "-ac", "1", "-ar", str(SR), "clip.wav"], d)
        words = self.transcribe(d / "clip.wav")
        cues = group_words(words)
        if cues and not a.no_snap:
            cues = snap_cues_to_audio(cues, load_wav_16k(d / "clip.wav"))
        if a.caption_delay:  # fine-tune: + = captions later, - = earlier
            cues = [[max(0.0, c[0] + a.caption_delay), max(0.0, c[1] + a.caption_delay), c[2]] for c in cues]
        font = find_font(a)
        family = "Arial"
        if font:
            (d / "fonts").mkdir(exist_ok=True)
            shutil.copy(font[0], d / "fonts" / font[0].name)
            family = font[1]
        else:
            log.warning("no font file found - captions may be invisible; pass --font-file C:\\Windows\\Fonts\\arial.ttf")
        layout = None
        if a.vertical == "split":
            vw, vh = probe_size(job.files[0])
            cam_rect, detected = a.cam_rect, False
            if a.cam_auto:
                if self.auto_cam is None:
                    self._ffmpeg(["-i", "joined.ts", "-vf", "fps=1/4,scale=960:-2",
                                  "-frames:v", "7", "camdet_%02d.png"], d)
                    self.auto_cam = detect_cam_region(sorted(d.glob("camdet_*.png")))
                    if self.auto_cam:
                        log.info("facecam found automatically at x,y,w,h = %s", ",".join(f"{v:.3f}" for v in self.auto_cam))
                    else:
                        log.warning("no steady face found - using the default bottom-right area for the top half. "
                                    "Set --cam-region bottom-left / top-right / x,y,w,h (see --preview-layout)")
                if self.auto_cam:
                    cam_rect, detected = self.auto_cam, True
            cam, game = compute_layout(cam_rect, a.game_rect, vw, vh, a.bottom)
            hide = ((expand_rect(cam, 2.2, 0.22) if detected else cam), a.hide_cam) if (
                a.bottom == "full" and a.hide_cam != "off" and (detected or not a.cam_auto)) else None
            layout = (cam, game, hide)
        (d / "clip.ass").write_text(build_ass(None if a.no_title else job.title, cues, job.length,
                                              upper=not a.no_upper, font=family,
                                              position="middle" if a.vertical == "split" else "bottom"))
        a.out.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(job.anchor))
        out = (a.out / f"{stamp}_{slugify(job.title)}.mp4").resolve()
        enc = ["-map", "[v]", "-map", "0:a:0?", "-af", SYNC_AF, "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
               "-metadata", f"title={job.title}", str(out)]
        try:
            self._ffmpeg(src + ["-filter_complex", build_filter(a.vertical, True, bool(font), layout)] + enc, d)
        except subprocess.CalledProcessError as e:
            log.warning("burning subtitles failed (ffmpeg without libass?): %s",
                        (e.stderr or b"").decode(errors="replace")[-300:])
            self._ffmpeg(src + ["-filter_complex", build_filter(a.vertical, False, False, layout)] + enc, d)
            shutil.copy(d / "clip.ass", out.with_suffix(".ass"))
        out.with_suffix(".json").write_text(json.dumps({
            "title": job.title,
            "clip_start": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(job.anchor - a.pre)),
            "stats": job.stats,
            "transcript": " ".join(w[2] for w in words),
            "chat_sample": job.chat_sample[:20],
        }, indent=2, ensure_ascii=False))
        return out

    def run(self) -> None:
        while True:
            job = self.q.get()
            if job is None:
                break
            try:
                out = self.process(job)
                log.info("CLIP SAVED  %s   title: %s", out, job.title)
            except Exception:
                log.exception("clip failed")
            finally:
                shutil.rmtree(job.dir, ignore_errors=True)
                self.q.task_done()


# --------------------------------------------------------------------------- analysis / orchestration
@dataclass
class Seg:
    path: Path
    wall_start: float
    wall_end: float


class Clipper:
    def __init__(self, args, chat: ChatStore, rec: Recorder, worker: ClipWorker):
        self.args, self.chat, self.rec, self.worker = args, chat, rec, worker
        self.W = args.window
        self.segs: list[Seg] = []
        self.processed: set[str] = set()
        self.loud: dict[int, list] = {}
        self.latest_end = 0.0
        self.next_win: int | None = None
        self.hist_c: collections.deque[float] = collections.deque(maxlen=args.baseline)
        self.hist_l: collections.deque[float] = collections.deque(maxlen=args.baseline)
        self.last_loud = self.last_chat = -10 ** 9
        self.last_anchor = -10 ** 9
        self.manual = False  # set by the Enter-key listener, handled in the main loop

    # ---- ingest finished segments
    def poll_segments(self) -> int:
        files = sorted(self.rec.seg_dir.glob("seg_*.ts"))
        n = 0
        for i, f in enumerate(files):
            if f.name in self.processed:
                continue
            if not (i + 1 < len(files) or not self.rec.active):
                break  # still being written
            self.processed.add(f.name)
            self._ingest(f)
            n += 1
        return n

    def _ingest(self, f: Path) -> None:
        dur = probe_duration(f)
        if dur < 1:
            return
        guess = f.stat().st_mtime - dur
        ws = self.segs[-1].wall_end if self.segs and abs(guess - self.segs[-1].wall_end) < 4 else guess
        x = decode_audio(f)
        self._add_audio(ws, x)
        self.segs.append(Seg(f, ws, ws + dur))
        self.latest_end = ws + dur

    def _add_audio(self, ws: float, x: np.ndarray) -> None:
        if x.size == 0:
            return
        idx = ((ws + np.arange(x.size) / SR) // self.W).astype(np.int64)
        base = int(idx[0])
        rel = idx - base
        ss = np.bincount(rel, weights=x.astype(np.float64) ** 2)
        cn = np.bincount(rel)
        for i, (s, c) in enumerate(zip(ss, cn)):
            e = self.loud.setdefault(base + i, [0.0, 0])
            e[0] += float(s)
            e[1] += int(c)

    # ---- evaluate completed windows
    def evaluate_windows(self) -> None:
        if self.next_win is None:
            if not self.loud:
                return
            self.next_win = min(self.loud)
        while (self.next_win + 1) * self.W <= self.latest_end:
            w = self.next_win
            self.next_win += 1
            e = self.loud.pop(w, None)
            if not e or e[1] < 0.5 * self.W * SR:
                continue
            db = max(-80.0, 10 * math.log10(e[0] / e[1] + 1e-10))
            self.step(w, self.chat.count(w), db)

    def step(self, w: int, c: int, db: float) -> None:
        a = self.args
        chat_spike = loud_spike = False
        zc = zl = 0.0
        mc, sc, ml, sl = 0.0, 1.0, db, 1.5
        warmed = len(self.hist_l) >= a.warmup
        if warmed:
            mc = statistics.fmean(self.hist_c)
            sc = max(statistics.pstdev(self.hist_c), math.sqrt(max(mc, 1.0)), 1.0)
            zc = (c - mc) / sc
            chat_spike = zc >= a.chat_z and c >= max(a.min_chat, 2 * mc)
            ml = statistics.fmean(self.hist_l)
            sl = max(statistics.pstdev(self.hist_l), 1.5)
            zl = (db - ml) / sl
            loud_spike = zl >= a.loud_z and db - ml >= a.min_loud_db
        if a.no_chat:
            chat_spike = True
        # baseline update (winsorised so a spike does not inflate its own baseline)
        self.hist_c.append(min(c, mc + 2 * sc) if warmed else c)
        self.hist_l.append(min(db, ml + 2 * sl) if warmed else db)
        log.debug("window %s chat=%d (z=%.1f) loud=%.1fdB (z=%.1f)%s%s", time.strftime("%H:%M:%S", time.localtime(w * self.W)),
                  c, zc, db, zl, " CHAT-SPIKE" if chat_spike and not a.no_chat else "", " LOUD-SPIKE" if loud_spike else "")
        if loud_spike:
            self.last_loud = w
        if chat_spike:
            self.last_chat = w
        tol = a.tolerance
        if (w - self.last_loud <= tol and w - self.last_chat <= tol and (self.last_loud == w or self.last_chat == w)):
            self.last_loud = self.last_chat = -10 ** 9
            self.trigger(w, {"chat_messages": c, "chat_z": round(zc, 2), "chat_baseline": round(mc, 2),
                             "loudness_db": round(db, 1), "loudness_z": round(zl, 2), "loudness_baseline_db": round(ml, 1)})

    # ---- cut
    def manual_clip(self) -> None:
        """Cut a clip of the most recent footage right now (press Enter) - handy for testing format/captions."""
        a = self.args
        if not self.segs:
            log.warning("nothing recorded yet - wait for the first %ss segment, then press Enter again", a.seg_seconds)
            return
        anchor = self.latest_end - a.length + a.pre  # clip = the last `length` seconds of finished footage
        w = int(anchor // self.W)
        self.trigger(w, {"manual": True, "chat_messages": self.chat.count(w), "loudness_db": None}, anchor=anchor)

    def trigger(self, w: int, stats: dict, anchor: float | None = None) -> None:
        a = self.args
        manual = anchor is not None
        anchor = w * self.W if anchor is None else anchor
        if not manual and anchor - self.last_anchor < a.cooldown:
            log.info("spike at %s skipped (cooldown)", time.strftime("%H:%M:%S", time.localtime(anchor)))
            return
        t0 = anchor - a.pre
        t1 = t0 + a.length
        segs = [s for s in self.segs if s.wall_end > t0 and s.wall_start < t1]
        if not segs:
            log.warning("spike detected but footage already deleted (raise --keep)")
            return
        self.last_anchor = anchor
        job_dir = Path(tempfile.mkdtemp(prefix="clip_", dir=a.work))
        files = []
        for s in segs:
            dst = job_dir / s.path.name
            shutil.copy(s.path, dst)
            files.append(dst)
        offset = max(0.0, t0 - segs[0].wall_start)
        length = min(a.length, segs[-1].wall_end - (segs[0].wall_start + offset))
        msgs = self.chat.messages(range(w - 2, w + 1)) if not a.no_chat else []
        title = make_title(msgs, a.fallback_title)
        log.info("%s at %s  chat=%s loud=%sdB -> cutting clip (%s)", "MANUAL CLIP" if manual else "SPIKE",
                 time.strftime("%H:%M:%S", time.localtime(anchor)), stats["chat_messages"], stats["loudness_db"], title)
        self.worker.q.put(Job(anchor, job_dir, files, offset, length, title, stats, msgs[:40]))

    def cleanup(self) -> None:
        if self.args.keep <= 0:
            return
        cutoff = self.latest_end - self.args.keep
        while self.segs and self.segs[0].wall_end < cutoff:
            self.segs.pop(0).path.unlink(missing_ok=True)
        self.chat.prune(int(cutoff // self.W) - 5)


# --------------------------------------------------------------------------- main
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Free live-stream clipper (yt-dlp + ffmpeg + faster-whisper)")
    p.add_argument("url", help="YouTube or Twitch live URL")
    p.add_argument("--out", type=Path, default=Path("clips"))
    p.add_argument("--work", type=Path, default=Path(".clipper_work"), help="scratch dir (segments, logs)")
    g = p.add_argument_group("timing")
    g.add_argument("--seg-seconds", type=int, default=30, help="recording piece length")
    g.add_argument("--window", type=int, default=10, help="chat/loudness measurement window (s)")
    g.add_argument("--pre", type=float, default=20, help="clip starts this many seconds before the spike window")
    g.add_argument("--length", type=float, default=30, help="clip length (s)")
    g.add_argument("--cooldown", type=float, default=30, help="min seconds between clips (avoids overlaps)")
    g.add_argument("--keep", type=float, default=180, help="seconds of raw footage kept on disk (0 = keep all)")
    g = p.add_argument_group("detection")
    g.add_argument("--baseline", type=int, default=36, help="rolling baseline length in windows (36 = 6 min)")
    g.add_argument("--warmup", type=int, default=12, help="windows to observe before detecting (12 = 2 min)")
    g.add_argument("--chat-z", type=float, default=2.5)
    g.add_argument("--loud-z", type=float, default=2.0)
    g.add_argument("--min-chat", type=int, default=5, help="minimum messages in a window to count as a chat spike")
    g.add_argument("--min-loud-db", type=float, default=4.0, help="minimum rise over baseline loudness (dB)")
    g.add_argument("--tolerance", type=int, default=1, help="windows apart the chat & loudness spikes may be")
    g.add_argument("--chat-shift", type=float, default=0.0,
                   help="seconds added to chat timestamps to line them up with the video (tune if chat lags)")
    g.add_argument("--no-chat", action="store_true", help="loudness-only mode (no chat source)")
    g = p.add_argument_group("output")
    g.add_argument("--vertical", choices=["split", "crop", "blur"], default="split",
                   help="split (default): 9:16 with the streamer's facecam on the top half and the game "
                        "(facecam cut out) on the bottom half, captions in the middle; crop: centre 9:16 crop; "
                        "blur: whole 16:9 frame over a blurred background")
    g.add_argument("--cam-region", default="auto",
                   help="where the streamer's facecam is. 'auto' (default) finds the face by itself; or a corner "
                        "name (bottom-right, bottom-left, top-right, top-left); or fractions of the frame x,y,w,h. "
                        "Use --preview-layout to check it")
    g.add_argument("--hide-cam", choices=["off", "blur", "black"], default="off",
                   help="off (default): the streamer stays visible in the bottom half too; blur/black: cover the "
                        "facecam there so the streamer only shows on top")
    g.add_argument("--bottom", choices=["full", "game"], default="full",
                   help="full (default): the bottom half shows the WHOLE stream frame, nothing cut off; "
                        "game: a crop of the stream that leaves the facecam out")
    g.add_argument("--game-region", default=None,
                   help="with --bottom game: x,y,w,h (fractions) of the area to show; default = automatic, "
                        "the widest area that does not contain the facecam")
    g.add_argument("--preview-layout", action="store_true",
                   help="grab one frame from the stream, save pictures showing the cam/game boxes and the "
                        "finished 9:16 layout to .clipper_work, then exit")
    g.add_argument("--whisper-model", default="large-v3-turbo",
                   help="large-v3-turbo (default, best Hindi, ~1.6 GB first download) / medium / small / base "
                        "(smaller = faster but worse Hindi)")
    g.add_argument("--device", default="auto")
    g.add_argument("--compute-type", default="auto")
    g.add_argument("--language", default="hi", help="language the streamer speaks (default hi = Hindi; "
                   "en = English; empty = auto-detect)")
    g.add_argument("--script", choices=["roman", "devanagari"], default="roman",
                   help="roman (default): Hindi written in English letters, e.g. 'ye kya kar raha hai'; "
                        "devanagari: keep the Hindi script")
    g.add_argument("--prompt", default="Ye kya kar raha hai? Kya hua? Arre yaar, bhai, chalo.",
                   help="hint text that helps whisper with the streamer's style of speaking")
    g.add_argument("--no-captions", action="store_true")
    g.add_argument("--no-snap", action="store_true",
                   help="do not move caption starts to where the sound really begins")
    g.add_argument("--caption-delay", type=float, default=0.0,
                   help="shift captions in seconds to fine-tune lip sync: 0.2 = captions 0.2 s later, "
                        "-0.2 = earlier (default 0)")
    g.add_argument("--font-file", default=None, help="TTF font for captions (default: auto-detect Arial etc.)")
    g.add_argument("--font-name", default="Arial", help="family name of --font-file")
    g.add_argument("--no-vad", action="store_true",
                   help="turn off whisper's speech detection filter (use if speech over music/game audio is missed)")
    g.add_argument("--no-title", action="store_true", help="do not burn the title into the video")
    g.add_argument("--no-upper", action="store_true", help="do not UPPERCASE captions")
    g.add_argument("--fallback-title", default="Stream highlight")
    g = p.add_argument_group("recording")
    g.add_argument("--max-height", type=int, default=1080)
    g.add_argument("--audio-offset", type=float, default=0.0,
                   help="seconds to shift audio vs video when recording (negative = audio earlier, "
                        "e.g. -1.5 if the audio still lags the picture)")
    g.add_argument("--retries", type=int, default=5)
    g.add_argument("--retry-wait", type=int, default=10)
    g.add_argument("--ytdlp-args", nargs=argparse.REMAINDER, default=[],
                   help="everything after this flag is passed to the yt-dlp recorder (e.g. --cookies-from-browser firefox)")
    p.add_argument("-v", "--verbose", action="store_true", help="log every window's numbers")
    args = p.parse_args(argv)
    cr = args.cam_region.strip().lower()
    args.cam_auto = cr == "auto"
    args.cam_rect = (DEFAULT_CAM if args.cam_auto else CAM_PRESETS.get(cr) or parse_region(args.cam_region, "--cam-region"))
    args.game_rect = parse_region(args.game_region, "--game-region") if args.game_region else None
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found on PATH - install ffmpeg first")
    if args.preview_layout:
        make_layout_preview(args)
        return 0
    platform = detect_platform(args.url)
    if platform is None and not args.no_chat:
        sys.exit("Unsupported URL for chat (need youtube.com / youtu.be / twitch.tv). Use --no-chat for loudness-only.")

    args.work.mkdir(parents=True, exist_ok=True)
    seg_dir = args.work / "segments"
    seg_dir.mkdir(exist_ok=True)
    for f in seg_dir.glob("seg_*.ts"):
        f.unlink()

    stop = threading.Event()
    chat = ChatStore(args.window)
    rec = Recorder(args.url, seg_dir, args, stop)
    worker = ClipWorker(args)
    worker.start()
    clipper = Clipper(args, chat, rec, worker)

    if not args.no_chat:
        if platform == "twitch":
            channel = urlparse(args.url).path.strip("/").split("/")[0].lower()
            t = threading.Thread(target=twitch_chat, args=(channel, chat, stop, args.chat_shift), daemon=True)
        else:
            t = threading.Thread(target=youtube_chat, args=(args.url, args.work / "chat", chat, stop,
                                                            args.chat_shift, []), daemon=True)
        t.start()
    rec.start()
    log.info("clipper running - clips go to %s/ (Ctrl-C to stop)", args.out)
    log.info("clips are only cut when chat AND loudness spike together; press ENTER at any time to cut a "
             "test clip of the last %ss now", int(args.length))

    def _enter_listener() -> None:
        try:
            while sys.stdin.readline():
                clipper.manual = True
        except Exception:
            pass

    threading.Thread(target=_enter_listener, daemon=True).start()

    warned = False
    try:
        while True:
            if clipper.manual:
                clipper.manual = False
                clipper.manual_clip()
            n = clipper.poll_segments()
            clipper.evaluate_windows()
            clipper.cleanup()
            if rec.done and n == 0:
                break
            if (not args.no_chat and not warned and clipper.latest_end and
                    time.time() - max(chat.last_msg, clipper.segs[0].wall_start if clipper.segs else 0) > 180):
                log.warning("no chat messages for 3 min - chat may be disabled, or the chat reader is not "
                            "working (see %s)", args.work / "chat")
                warned = True
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("stopping ...")
    finally:
        stop.set()
        kill_children()
        rec.join(timeout=15)
        pending = worker.q.qsize()
        if pending:
            log.info("finishing %d queued clip(s) - Ctrl-C again to abort", pending)
        try:
            worker.q.join()
        except KeyboardInterrupt:
            pass
        worker.q.put(None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
