"""yt-dlp + ffmpeg wrappers: metadata, captions, audio, section downloads and vertical rendering."""
from __future__ import annotations

import glob
import json
import os
import subprocess
from pathlib import Path

WATCH = "https://www.youtube.com/watch?v={id}"


class MediaError(RuntimeError):
    pass


def _ytdlp_base() -> list[str]:
    args = ["yt-dlp", "--no-playlist", "--no-warnings", "--retries", "5",
            "--extractor-retries", "3", "--socket-timeout", "30"]
    cookies = os.environ.get("YTDLP_COOKIES_FILE")
    if cookies and os.path.exists(cookies) and os.path.getsize(cookies) > 0:
        args += ["--cookies", cookies]
    proxy = os.environ.get("YTDLP_PROXY")
    if proxy:
        args += ["--proxy", proxy]
    return args


def _run(cmd: list[str], *, cwd: str | None = None, timeout: int = 1800, capture=True) -> str:
    p = subprocess.run(cmd, cwd=cwd, timeout=timeout, text=True,
                       stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE)
    if p.returncode != 0:
        err = (p.stderr or "")[-1500:]
        if "confirm you" in err and "bot" in err:
            err = ("YouTube bot-check blocked the download. Add/refresh the YTDLP_COOKIES secret "
                   "or run on a self-hosted runner. Details: " + err[-400:])
        raise MediaError(f"{cmd[0]} failed ({p.returncode}): {err}")
    return p.stdout or ""


# ---------------------------------------------------------------- yt-dlp
def video_info(video_id: str) -> dict:
    out = _run(_ytdlp_base() + ["-J", "--skip-download", WATCH.format(id=video_id)], timeout=300)
    return json.loads(out)


def fetch_caption_words(video_id: str, workdir: str):
    """Download English captions (manual if present, else auto) as json3 and parse to words."""
    from .highlights import parse_json3
    tmpl = os.path.join(workdir, "subs.%(ext)s")
    try:
        _run(_ytdlp_base() + ["--skip-download", "--write-subs", "--write-auto-subs",
                              "--sub-langs", "en.*,en", "--sub-format", "json3",
                              "-o", tmpl, WATCH.format(id=video_id)], timeout=300)
    except MediaError:
        return None
    files = sorted(glob.glob(os.path.join(workdir, "subs*.json3")),
                   key=lambda p: ("orig" in p, len(p)))  # prefer plain 'en'
    for f in files:
        try:
            words = parse_json3(Path(f).read_text(encoding="utf-8"))
            if len(words) > 20:
                return words
        except Exception:
            continue
    return None


def fetch_audio_signal(video_id: str, workdir: str, max_duration: float = 4 * 3600):
    """Download low-bitrate audio and compute a per-second loudness-spike signal."""
    from .highlights import audio_signal
    tmpl = os.path.join(workdir, "audio.%(ext)s")
    _run(_ytdlp_base() + ["-f", "wa[acodec!=none]/ba[abr<=96]/ba", "-o", tmpl,
                          "--download-sections", f"*0-{int(max_duration)}",
                          WATCH.format(id=video_id)], timeout=1800)
    src = next(iter(glob.glob(os.path.join(workdir, "audio.*"))), None)
    if not src:
        return None
    sr = 8000
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-ac", "1", "-ar", str(sr),
                        "-f", "s16le", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=1800)
    os.remove(src)
    if p.returncode != 0:
        return None
    return audio_signal(p.stdout, sr)


def download_section(video_id: str, start: float, end: float, dest_base: str) -> str:
    """Download only [start, end] of the video (max 1080p) — no need to fetch the whole VOD."""
    s, e = max(0.0, start - 0.5), end + 0.5
    _run(_ytdlp_base() + [
        "-f", "bv*[height<=1080][fps<=60]+ba/b[height<=1080]/bv*+ba/b",
        "--download-sections", f"*{s:.2f}-{e:.2f}", "--force-keyframes-at-cuts",
        "--merge-output-format", "mp4", "-o", dest_base + ".%(ext)s",
        WATCH.format(id=video_id)], timeout=1800)
    files = [f for f in glob.glob(dest_base + ".*") if not f.endswith((".part", ".ytdl"))]
    if not files:
        raise MediaError("section download produced no file")
    return files[0]


# ---------------------------------------------------------------- captions (ASS)
W, H = 1080, 1920


def _ass_escape(t: str) -> str:
    return t.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _ts(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_ass(*, clip_start: float, clip_len: float, words=None, hook: str | None = None,
              credit: str | None = None, font: str = "DejaVu Sans") -> str:
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # Big punchy captions below the gameplay
        f"Style: Cap,{font},84,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,2,60,60,520,1",
        # Hook / title in the top band (white text on a dark box)
        f"Style: Hook,{font},62,&H00FFFFFF,&H000000FF,&H00101010,&HB0000000,-1,0,0,0,100,100,0,0,3,18,0,8,70,70,250,1",
        # Credit line near the bottom
        f"Style: Credit,{font},40,&H30FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,40,40,150,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    end_all = _ts(clip_len)
    if hook:
        lines.append(f"Dialogue: 1,{_ts(0)},{end_all},Hook,,0,0,0,,{_ass_escape(hook)}")
    if credit:
        lines.append(f"Dialogue: 1,{_ts(0)},{end_all},Credit,,0,0,0,,{_ass_escape(credit)}")
    for s, e, text in caption_chunks(words or [], clip_start, clip_len):
        # Pop-in animation: scale from 80% to 100% over 80 ms
        anim = r"{\fscx80\fscy80\t(0,80,\fscx100\fscy100)}"
        lines.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},Cap,,0,0,0,,{anim}{_ass_escape(text)}")
    return "\n".join(lines) + "\n"


def caption_chunks(words, clip_start: float, clip_len: float, max_words: int = 3,
                   max_span: float = 1.3):
    """Group words into short, punchy on-screen chunks (relative to the clip)."""
    ws = [(t - clip_start, w) for t, w in words if clip_start - 0.1 <= t < clip_start + clip_len]
    ws = [(t, w) for t, w in ws if w and not w.startswith("[")]
    chunks, cur = [], []
    for t, w in ws:
        if cur and (len(cur) >= max_words or t - cur[0][0] > max_span):
            chunks.append(cur)
            cur = []
        cur.append((t, w))
    if cur:
        chunks.append(cur)
    out = []
    for i, ch in enumerate(chunks):
        s = max(0.0, ch[0][0])
        nxt = chunks[i + 1][0][0] if i + 1 < len(chunks) else clip_len
        e = min(nxt, ch[-1][0] + 1.2, clip_len)
        if e - s < 0.25:
            e = min(clip_len, s + 0.4)
        out.append((s, e, " ".join(w for _, w in ch).upper()))
    return out


# ---------------------------------------------------------------- render
def render_vertical(src: str, dest: str, *, length: float, ass_path: str | None,
                    offset: float = 0.5, crf: int = 23) -> str:
    """16:9 (or anything) → 1080x1920 Short: blurred fill background + sharp centred video + captions."""
    vf = (
        "[0:v]split=2[a][b];"
        f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        "boxblur=25:2,eq=brightness=-0.12:saturation=1.2[bg];"
        f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )
    workdir = os.path.dirname(os.path.abspath(dest))
    if ass_path:
        vf += f",ass={os.path.basename(ass_path)}"
    vf += ",fps=30,format=yuv420p[v]"
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{offset:.2f}", "-i", os.path.abspath(src),
           "-t", f"{length:.2f}", "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
           "-maxrate", "5M", "-bufsize", "10M", "-c:a", "aac", "-b:a", "128k",
           "-ar", "48000", "-movflags", "+faststart", os.path.abspath(dest)]
    _run(cmd, cwd=workdir, timeout=1800)
    if not os.path.exists(dest) or os.path.getsize(dest) < 10_000:
        raise MediaError("render produced an empty file")
    return dest
