"""The heavy job: turn new source videos into ready-to-post vertical clips.

For each pending video (newest first):
  metadata (yt-dlp) → find best moments (heatmap → captions → audio → spread)
  → download only those sections → render 1080x1920 with captions + credit
  → upload to Supabase Storage → clip row = ready (or needs_review)

Run:  python -m worker.process
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone

from . import highlights, media, titles
from .db import DB, now_iso, q_ts
from .tick import work_pending


def log(msg: str):
    print(msg, flush=True)


def process_video(db: DB, st: dict, video: dict, creator: dict) -> dict:
    vid = video["id"]
    creator_name = creator.get("name") or creator.get("handle") or "the creator"
    work = tempfile.mkdtemp(prefix=f"clip_{vid}_")
    made = []
    try:
        info = media.video_info(vid)
        live = info.get("live_status")
        if live in ("is_upcoming", "is_live", "post_live"):
            # Stream not finished / VOD not ready yet — try again on a later run.
            db.update("source_videos", f"id=eq.{vid}", {"status": "new", "error": f"live_status={live}"})
            return {"video": vid, "deferred": live}
        duration = float(info.get("duration") or 0)
        db.update("source_videos", f"id=eq.{vid}", {"duration_s": int(duration)})
        if duration < st["min_source_duration_s"]:
            db.update("source_videos", f"id=eq.{vid}", {"status": "skipped", "error": "too short"})
            return {"video": vid, "skipped": "too short"}

        k = creator.get("max_clips_per_video") or st["clips_per_video"]
        heatmap = info.get("heatmap")
        words = media.fetch_caption_words(vid, work)
        audio = None
        if not heatmap and not words:
            try:
                audio = media.fetch_audio_signal(vid, work)
            except media.MediaError as e:
                log(f"  audio analysis failed: {e}")
        picks = highlights.pick_clips(duration=duration, k=k, target=st["clip_target_s"],
                                      min_len=st["clip_min_s"], max_len=st["clip_max_s"],
                                      heatmap=heatmap, words=words, audio=audio)
        log(f"  {len(picks)} moments via {[p.method for p in picks]}")

        tags = list(dict.fromkeys((st.get("default_tags") or []) + [creator_name]))
        for i, w in enumerate(picks, 1):
            try:
                base = os.path.join(work, f"raw_{i}")
                raw = media.download_section(vid, w.start, w.end, base)
                transcript = titles.transcript_for(words, w.start, w.end)
                hook = titles.make_hook(source_title=video.get("title") or "", creator=creator_name,
                                        transcript=transcript, index=i)
                ass_path = None
                if st["burn_captions"] or st["show_credit"]:
                    ass_path = os.path.join(work, f"subs_{i}.ass")
                    with open(ass_path, "w", encoding="utf-8") as f:
                        f.write(media.build_ass(
                            clip_start=w.start, clip_len=w.length,
                            words=words if st["burn_captions"] else None,
                            hook=hook if st["burn_captions"] else None,
                            credit=f"via {creator_name}" if st["show_credit"] else None))
                out = os.path.join(work, f"clip_{i}.mp4")
                media.render_vertical(raw, out, length=w.length, ass_path=ass_path,
                                      offset=w.start - max(0.0, w.start - 0.5))
                values = dict(
                    hook=hook, creator=creator_name, source_title=video.get("title") or "",
                    source_url=f"https://www.youtube.com/watch?v={vid}&t={int(w.start)}s",
                    channel_url=f"https://www.youtube.com/channel/{creator.get('channel_id')}",
                    hashtags=titles.hashtags(tags), part=i)
                title = titles.fill(st["title_template"], **values)[:100]
                desc = titles.fill(st["description_template"], **values)

                row = db.insert("clips", {
                    "source_video_id": vid, "creator_id": creator["id"],
                    "start_s": w.start, "end_s": w.end, "score": w.score, "method": w.method,
                    "title": title, "description": desc, "tags": tags,
                    "status": "needs_review" if st["require_approval"] else "ready",
                    "size_bytes": os.path.getsize(out)})[0]
                path = f"{vid}/{row['id']}.mp4"
                try:
                    db.upload_file("clips", path, out)
                except Exception as e:
                    db.update("clips", f"id=eq.{row['id']}",
                              {"status": "failed", "error": f"storage upload: {e}"[:1000]})
                    raise
                db.update("clips", f"id=eq.{row['id']}", {"storage_path": path})
                made.append(row["id"])
                log(f"  ✓ clip {i}: {w.start:.0f}-{w.end:.0f}s [{w.method} {w.score}] {title}")
                for p in (raw, out):
                    if os.path.exists(p):
                        os.remove(p)
            except Exception as e:
                log(f"  ! clip {i} failed: {e}")
        status = "clipped" if made else "failed"
        db.update("source_videos", f"id=eq.{vid}",
                  {"status": status, "processed_at": now_iso(),
                   "error": None if made else "no clips could be produced"})
        return {"video": vid, "clips": len(made)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    db = DB()
    st = db.settings()
    run_id = db.start_run("process")
    results, ok = [], True
    try:
        pending, why = work_pending(db, st)
        if not pending:
            log(f"Nothing to do: {why}")
        cutoff = q_ts(datetime.now(timezone.utc) - timedelta(hours=st["max_video_age_hours"]))
        # Too-old videos are no longer worth clipping.
        db.update("source_videos", f"status=eq.new&discovered_at=lt.{cutoff}",
                  {"status": "skipped", "error": "too old by the time it was reached"})
        videos = db.select("source_videos", "status=eq.new&order=published_at.desc"
                                            f"&limit={st['videos_per_run']}") if pending else []
        creators = {c["id"]: c for c in db.select("creators", "select=*")}
        for v in videos:
            # Re-check the backlog between videos so we never overproduce.
            if work_pending(db, st)[0] == 0:
                break
            c = creators.get(v["creator_id"])
            if not c or not c.get("enabled") or not c.get("permission_confirmed"):
                db.update("source_videos", f"id=eq.{v['id']}", {"status": "skipped",
                                                                  "error": "creator disabled"})
                continue
            log(f"▶ {v['title']}  ({v['id']})")
            db.update("source_videos", f"id=eq.{v['id']}",
                      {"status": "processing", "attempts": (v.get("attempts") or 0) + 1,
                       "processed_at": now_iso()})
            try:
                results.append(process_video(db, st, v, c))
            except Exception as e:
                ok = False
                traceback.print_exc()
                attempts = (v.get("attempts") or 0) + 1
                db.update("source_videos", f"id=eq.{v['id']}",
                          {"status": "new" if attempts < 3 else "failed", "error": str(e)[:1000]})
                results.append({"video": v["id"], "error": str(e)[:300]})
    except Exception as e:
        ok = False
        traceback.print_exc()
        results.append({"error": str(e)[:500]})
    db.finish_run(run_id, ok, {"results": results})
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
