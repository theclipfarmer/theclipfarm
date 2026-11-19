"""yt-dlp + ffmpeg wrappers: metadata, captions, audio, section downloads and vertical rendering."""
from __future__ import annotations

import glob
import json
import os
import subprocess
from pathlib import Path

WATCH = "https://www.youtube.com/watch?v={id}"
TWITCH_VOD = "https://www.twitch.tv/videos/{id}"


def is_twitch(video_id: str) -> bool:
    return video_id.startswith("tw_")


def url_for(video_id: str) -> str:
    """Our ids: YouTube video ids as-is; Twitch VODs as 'tw_<digits>'."""
    if is_twitch(video_id):
        return TWITCH_VOD.format(id=video_id[3:])
    return WATCH.format(id=video_id)


def source_link(video_id: str, start_s: float) -> str:
    t = int(start_s)
    if is_twitch(video_id):
        return f"{url_for(video_id)}?t={t // 3600}h{t % 3600 // 60}m{t % 60}s"
    return f"{url_for(video_id)}&t={t}s"


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


def _say(msg: str):
    print(f"    · {msg}", flush=True)


def _run(cmd: list[str], *, cwd: str | None = None, timeout: int = 1800, capture=True) -> str:
    try:
        p = subprocess.run(cmd, cwd=cwd, timeout=timeout, text=True,
                           stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        raise MediaError(f"{cmd[0]} timed out after {timeout // 60} min") from None
    if p.returncode != 0:
        err = (p.stderr or "")[-1500:]
        if "confirm you" in err and "bot" in err:
            err = ("YouTube bot-check blocked the download. Add/refresh the YTDLP_COOKIES secret "
                   "or run on a self-hosted runner. Details: " + err[-400:])
        raise MediaError(f"{cmd[0]} failed ({p.returncode}): {err}")
    return p.stdout or ""


# ---------------------------------------------------------------- yt-dlp
def video_info(video_id: str) -> dict:
    _say("reading video info")
    out = _run(_ytdlp_base() + ["-J", "--skip-download", url_for(video_id)], timeout=300)
    return json.loads(out)


def fetch_caption_words(video_id: str, workdir: str):
    """Download English captions (manual if present, else auto) as json3 and parse to words."""
    from .highlights import parse_json3
    tmpl = os.path.join(workdir, "subs.%(ext)s")
    _say("fetching captions")
    try:
        _run(_ytdlp_base() + ["--skip-download", "--write-subs", "--write-auto-subs",
                              "--sub-langs", "en.*,en", "--sub-format", "json3",
                              "-o", tmpl, url_for(video_id)], timeout=300)
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


def fetch_audio_signal(video_id: str, workdir: str, max_duration: float = 4 * 3600,
                       start: float = 0.0):
    """Download low-bitrate audio for [start, start+max_duration] and compute a per-second
    loudness-spike signal (index 0 = `start`)."""
    from .highlights import audio_signal
    tmpl = os.path.join(workdir, "audio.%(ext)s")
    if is_twitch(video_id):
        # Twitch is HLS: section downloads are fast and avoid fetching hours of stream.
        _say(f"downloading stream audio {int(start) // 60}–{int(start + max_duration) // 60} min")
        _run(_ytdlp_base() + ["-f", "audio_only/160p/worst", "-o", tmpl, "--concurrent-fragments", "8",
                              "--download-sections", f"*{int(start)}-{int(start + max_duration)}",
                              url_for(video_id)], timeout=1800)
        trim = []
    else:
        # YouTube throttles section downloads to ~real time, so fetch the whole (small,
        # lowest-quality) audio track with the fast chunked downloader and trim locally.
        _say("downloading audio for loudness analysis")
        _run(_ytdlp_base() + ["-f", "wa[acodec!=none]/ba[abr<=96]/ba", "-o", tmpl,
                              "--http-chunk-size", "10M", "--concurrent-fragments", "4",
                              url_for(video_id)], timeout=900)
        trim = ["-ss", str(int(start)), "-t", str(int(max_duration))]
    src = next(iter(glob.glob(os.path.join(workdir, "audio.*"))), None)
    if not src:
        return None
    sr = 8000
    _say("analysing loudness")
    p = subprocess.run(["ffmpeg", "-v", "error", *trim, "-i", src, "-ac", "1", "-ar", str(sr),
                        "-f", "s16le", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=1800)
    os.remove(src)
    if p.returncode != 0:
        return None
    return audio_signal(p.stdout, sr)


def twitch_vods(user: str, limit: int = 5) -> list[dict]:
    """Newest past broadcasts (VODs) of a Twitch channel, including one still being recorded."""
    out = _run(_ytdlp_base() + ["--flat-playlist", "-J", "--playlist-end", str(limit),
                                f"https://www.twitch.tv/{user}/videos?filter=archives&sort=time"],
               timeout=300)
    data = json.loads(out)
    vods = []
    for e in data.get("entries") or []:
        vid = str(e.get("id") or "").lstrip("v")
        if vid.isdigit():
            vods.append({"id": f"tw_{vid}", "title": e.get("title") or f"{user} stream",
                         "timestamp": e.get("timestamp")})
    return vods


def download_section(video_id: str, start: float, end: float, dest_base: str) -> str:
    """Download only [start, end] of the video (max 1080p) — no need to fetch the whole VOD."""
    s, e = max(0.0, start - 0.5), end + 0.5
    _say(f"downloading clip {int(s) // 60}:{int(s) % 60:02d}–{int(e) // 60}:{int(e) % 60:02d}")
    _run(_ytdlp_base() + [
        "-f", ("1080p60/1080p/best[height<=1080]/best" if is_twitch(video_id)
               else "bv*[height<=1080][fps<=60]+ba/b[height<=1080]/bv*+ba/b"),
        "--download-sections", f"*{s:.2f}-{e:.2f}", "--force-keyframes-at-cuts",
        "--merge-output-format", "mp4", "-o", dest_base + ".%(ext)s",
        url_for(video_id)], timeout=900)
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
