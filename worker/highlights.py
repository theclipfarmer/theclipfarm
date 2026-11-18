"""Finding the best moments in a video.

Every detector turns the video into a per-second "interest" signal; then one shared
routine picks the best non-overlapping windows. Detectors, best first:

  1. heatmap  — YouTube's "Most replayed" graph (what viewers actually rewatched)
  2. captions — reaction words, laughter, shouting, fast speech in the auto-captions
  3. audio    — loudness spikes (screams, explosions, hype)
  4. spread   — evenly spaced fallback so a video never yields nothing
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass


@dataclass
class Window:
    start: float
    end: float
    score: float
    method: str

    @property
    def length(self) -> float:
        return self.end - self.start


# ------------------------------------------------------------------ signals
def heatmap_signal(heatmap: list[dict] | None, duration: float) -> list[float] | None:
    if not heatmap or duration <= 0:
        return None
    n = int(math.ceil(duration))
    sig = [0.0] * n
    for seg in heatmap:
        s = int(seg.get("start_time", 0))
        e = int(math.ceil(seg.get("end_time", 0)))
        v = float(seg.get("value", 0) or 0)
        for i in range(max(0, s), min(n, e)):
            sig[i] = v
    return sig if any(sig) else None


HYPE_WORDS = {
    # reactions
    "no way": 3, "oh my god": 3, "omg": 2, "what the": 3, "wtf": 3, "holy": 2, "insane": 3,
    "crazy": 2, "bro": 1.5, "dude": 1.5, "yo": 1, "wait": 1.5, "what": 1, "let's go": 3,
    "lets go": 3, "lmao": 2, "lol": 1.5, "hahaha": 3, "haha": 2, "dead": 1.5, "run": 1.5,
    "help": 1.5, "nooo": 3, "no no no": 3, "oh no": 2.5, "yes": 1, "clip that": 4,
    "clip it": 4, "chat": 1, "did you see": 2.5, "look at": 1.5, "wanted level": 2,
    # GTA-flavoured
    "cops": 1.5, "police": 1.5, "chase": 2, "five stars": 3, "5 stars": 3, "wasted": 3,
    "heist": 2, "explode": 2.5, "explosion": 2.5, "crash": 2, "lucia": 1, "jason": 1,
    "vice city": 1.5, "secret": 2, "easter egg": 3, "glitch": 2.5, "bug": 1.5,
}
_TAG_RE = re.compile(r"\[(laughter|laughs|music|applause|screaming|cheering)\]", re.I)


def parse_json3(text: str) -> list[tuple[float, str]]:
    """YouTube json3 captions → list of (time_seconds, word)."""
    data = json.loads(text)
    words: list[tuple[float, str]] = []
    for ev in data.get("events", []):
        base = ev.get("tStartMs", 0)
        for seg in ev.get("segs") or []:
            w = (seg.get("utf8") or "").strip()
            if not w or w == "\n":
                continue
            t = (base + seg.get("tOffsetMs", 0)) / 1000.0
            for part in w.split():
                words.append((t, part))
    words.sort(key=lambda x: x[0])
    return words


def captions_signal(words: list[tuple[float, str]], duration: float) -> list[float] | None:
    if not words or duration <= 0:
        return None
    n = int(math.ceil(duration)) + 1
    sig = [0.0] * n
    rate = [0] * n
    buckets: list[list[str]] = [[] for _ in range(n)]
    for t, w in words:
        i = int(t)
        if 0 <= i < n:
            rate[i] += 1
            buckets[i].append(w)
    # speech-rate component: talking fast/excitedly
    avg = (sum(rate) / max(1, sum(1 for r in rate if r))) or 1
    for i, r in enumerate(rate):
        if r > avg * 1.4:
            sig[i] += 0.6 * (r / avg)
    # keyword component over a sliding 4-second text window
    for i in range(n):
        raw = " ".join(w for b in buckets[i:i + 4] for w in b)
        if not raw:
            continue
        chunk = raw.lower()
        s = 0.0
        for kw, weight in HYPE_WORDS.items():
            if kw in chunk:
                s += weight
        s += 3 * len(_TAG_RE.findall(chunk))
        s += 0.5 * chunk.count("!")
        if sum(1 for c in raw if c.isupper()) > 8:
            s += 1  # shouting in caps
        sig[i] += s
    return sig[: int(math.ceil(duration))] if any(sig) else None


def audio_signal(pcm_s16le_mono: bytes, sample_rate: int) -> list[float] | None:
    """Per-second loudness (RMS) from raw 16-bit mono PCM."""
    import array
    a = array.array("h")
    a.frombytes(pcm_s16le_mono[: len(pcm_s16le_mono) // 2 * 2])
    if not a:
        return None
    sig = []
    for i in range(0, len(a), sample_rate):
        block = a[i:i + sample_rate]
        if not block:
            break
        # subsample for speed
        step = max(1, len(block) // 2000)
        sq = [block[j] * block[j] for j in range(0, len(block), step)]
        sig.append(math.sqrt(sum(sq) / len(sq)))
    if not any(sig):
        return None
    # Emphasise spikes relative to the local (60 s) baseline, not overall loudness.
    out = []
    for i, v in enumerate(sig):
        lo, hi = max(0, i - 30), min(len(sig), i + 30)
        base = sorted(sig[lo:hi])[(hi - lo) // 2] or 1
        out.append(max(0.0, v / base - 1.0))
    return out


# ------------------------------------------------------------------ selection
def best_windows(signal: list[float], *, length: int, k: int, method: str,
                 skip_start: int = 0, skip_end: int = 0, lead_in: float = 0.35,
                 min_rel: float = 0.45) -> list[Window]:
    """Pick up to k non-overlapping windows of `length` seconds with the highest mean signal.
    The window is placed so the peak sits ~lead_in of the way in (gives context before the moment).
    Windows scoring below min_rel × the best window are dropped — better fewer, stronger clips."""
    n = len(signal)
    if n < length or k <= 0:
        return []
    # prefix sums
    ps = [0.0]
    for v in signal:
        ps.append(ps[-1] + v)
    lo = min(skip_start, max(0, n - length))
    hi = max(lo, n - length - skip_end)
    cands = []
    for s in range(lo, hi + 1):
        cands.append(((ps[s + length] - ps[s]) / length, s))
    cands.sort(reverse=True)

    chosen: list[Window] = []
    gap = max(5, length // 2)                 # keep picks clearly separate moments
    top = cands[0][0] if cands else 0
    for score, s in cands:
        if score <= 0 or score < min_rel * top:
            break
        # re-centre around the peak inside this window
        seg = signal[s:s + length]
        peak = s + max(range(len(seg)), key=seg.__getitem__)
        ns = int(max(0, min(n - length, peak - lead_in * length)))
        start = ns if not _overlaps(ns, length, chosen, gap) else s
        if _overlaps(start, length, chosen, gap):
            continue
        chosen.append(Window(float(start), float(start + length), round(score, 4), method))
        if len(chosen) >= k:
            break
    return sorted(chosen, key=lambda w: -w.score)


def _overlaps(s: int, length: int, chosen: list[Window], gap: int = 5) -> bool:
    return any(s < c.end + gap and s + length > c.start - gap for c in chosen)


def spread_windows(duration: float, *, length: int, k: int) -> list[Window]:
    if duration < length:
        return [Window(0.0, float(duration), 0.0, "spread")] if duration >= 10 else []
    usable = duration - length
    out = []
    for i in range(k):
        s = usable * (i + 1) / (k + 1)
        out.append(Window(round(s, 1), round(s + length, 1), 0.0, "spread"))
    return out


def snap_to_speech(w: Window, words: list[tuple[float, str]] | None,
                   min_len: int, max_len: int, slack: float = 3.0) -> Window:
    """Nudge cut points into pauses between words so clips don't start/end mid-sentence."""
    if not words:
        return w
    times = [t for t, _ in words]

    def best_gap(target: float) -> float:
        best, best_gap_len = target, 0.0
        for a, b in zip(times, times[1:]):
            mid = (a + b) / 2
            if abs(mid - target) <= slack and (b - a) > best_gap_len:
                best, best_gap_len = mid, b - a
        return best if best_gap_len >= 0.35 else target

    s, e = best_gap(w.start), best_gap(w.end)
    if e - s < min_len or e - s > max_len:
        return w
    return Window(round(max(0.0, s), 2), round(e, 2), w.score, w.method)


def pick_clips(*, duration: float, k: int, target: int, min_len: int, max_len: int,
               heatmap=None, words=None, audio=None) -> list[Window]:
    """Run detectors in priority order and return k windows."""
    skip_start = min(30, int(duration * 0.05))   # intros are always 'most replayed'
    skip_end = min(20, int(duration * 0.03))     # outros / end screens
    length = max(min_len, min(max_len, target))
    picks: list[Window] = []
    for method, sig in (("heatmap", heatmap_signal(heatmap, duration)),
                        ("captions", captions_signal(words or [], duration)),
                        ("audio", audio)):
        if not sig or len(picks) >= k:
            continue
        for w in best_windows(sig, length=length, k=k, method=method,
                              skip_start=skip_start, skip_end=skip_end):
            if len(picks) >= k:
                break
            if not _overlaps(int(w.start), int(w.length), picks, max(5, length // 2)):
                picks.append(w)
    if not picks:  # only fall back to evenly spaced clips if nothing stood out at all
        for w in spread_windows(duration, length=length, k=k):
            if len(picks) >= k:
                break
            if not _overlaps(int(w.start), int(w.length), picks):
                picks.append(w)
    picks = [snap_to_speech(w, words, min_len, max_len) for w in picks]
    return picks
