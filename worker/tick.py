"""The hourly lightweight job (standard library only — runs in seconds):

  1. discover  — check every creator's RSS feed for new uploads
  2. publish   — if a posting slot is due, upload the best ready clip(s) to YouTube
  3. cleanup   — free storage, un-stick jobs
  4. report    — tell GitHub Actions whether the heavy "process" job needs to run

Run:  python -m worker.tick            (FORCE_PUBLISH=1 to post one clip right now)
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import youtube as yt
from .db import DB, now_iso, q_ts


def log(msg: str):
    print(msg, flush=True)


# ------------------------------------------------------------------ discover
def discover(db: DB, st: dict) -> dict:
    found, errors = 0, []
    creators = db.select("creators", "enabled=is.true&order=created_at.asc")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=st["max_video_age_hours"])
    for c in creators:
        label = c.get("name") or c.get("handle") or c.get("channel_id")
        if not c.get("permission_confirmed"):
            log(f"- skip {label}: permission not confirmed")
            continue
        try:
            cid = c.get("channel_id")
            if not cid:
                cid, handle = yt.parse_creator_input(c.get("handle") or "")
                name = None
                if not cid and handle:
                    if not yt.have_credentials():
                        raise RuntimeError("need YouTube credentials to resolve @handles")
                    res = yt.resolve_handle(handle)
                    if not res:
                        raise RuntimeError(f"no channel found for {handle}")
                    cid, name = res
                if not cid:
                    raise RuntimeError("could not parse creator handle / channel id")
                if not name and yt.have_credentials():
                    name = yt.channel_info(cid)
                db.update("creators", f"id=eq.{c['id']}",
                          {"channel_id": cid, **({"name": name} if name else {})})
                label = name or label
            new_rows = []
            for v in yt.recent_uploads(cid):
                if v["is_short"] or not v["published_at"]:
                    continue
                if yt.parse_ts(v["published_at"]) < cutoff:
                    continue
                new_rows.append({"id": v["id"], "creator_id": c["id"], "title": v["title"],
                                 "published_at": v["published_at"]})
            if new_rows:
                inserted = db.insert("source_videos", new_rows, on_conflict="id", ignore_dupes=True)
                found += len(inserted)
                for r in inserted:
                    log(f"+ new video from {label}: {r['title']}")
            db.update("creators", f"id=eq.{c['id']}", {"last_checked_at": now_iso()})
        except Exception as e:
            errors.append(f"{label}: {e}")
            log(f"! {label}: {e}")
    return {"creators_checked": len(creators), "new_videos": found, "errors": errors}


# ------------------------------------------------------------------ publish
def due_slot(st: dict, now: datetime | None = None) -> datetime | None:
    """Most recent posting slot that has passed and hasn't been served yet (3h grace window,
    so a late or skipped GitHub cron run still catches up)."""
    try:
        tz = ZoneInfo(st.get("timezone") or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    slots = []
    for day in (now.date() - timedelta(days=1), now.date()):
        for hhmm in st.get("post_times") or []:
            try:
                h, m = (int(x) for x in hhmm.strip().split(":"))
            except ValueError:
                continue
            slots.append(datetime(day.year, day.month, day.day, h, m, tzinfo=tz))
    past = [s for s in slots if s <= now and now - s <= timedelta(hours=3)]
    if not past:
        return None
    slot = max(past)
    last = st.get("last_slot_posted")
    if last and yt.parse_ts(last) >= slot.astimezone(timezone.utc):
        return None
    return slot


def pick_clips(db: DB, n: int) -> list[dict]:
    fresh = q_ts(datetime.now(timezone.utc) - timedelta(hours=72))
    rows = db.select("clips", f"status=eq.ready&storage_path=not.is.null&created_at=gte.{fresh}"
                              f"&order=score.desc.nullslast,created_at.desc&limit={n}")
    if len(rows) < n:
        more = db.select("clips", "status=eq.ready&storage_path=not.is.null"
                                  f"&order=created_at.desc&limit={n * 3}")
        seen = {r["id"] for r in rows}
        rows += [r for r in more if r["id"] not in seen][: n - len(rows)]
    return rows


def publish(db: DB, st: dict, force: bool) -> dict:
    if st["upload_mode"] != "api":
        return {"skipped": "upload_mode is manual — download clips from the dashboard"}
    if not yt.have_credentials():
        return {"skipped": "YouTube credentials not set"}
    slot = None if force else due_slot(st)
    if not force and not slot:
        return {"skipped": "no slot due"}
    n = 1 if force else max(1, st["clips_per_slot"])
    posted, errors = [], []
    for clip in pick_clips(db, n):
        db.update("clips", f"id=eq.{clip['id']}", {"status": "uploading"})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = db.download_file("clips", clip["storage_path"], os.path.join(tmp, "clip.mp4"))
                vid = yt.upload_video(path, title=clip["title"], description=clip["description"] or "",
                                      tags=clip.get("tags") or [], privacy=st["privacy_status"])
            upd = {"status": "posted", "youtube_video_id": vid, "posted_at": now_iso(), "error": None}
            if st["delete_after_post"]:
                db.delete_files("clips", [clip["storage_path"]])
                upd["storage_path"] = None
            db.update("clips", f"id=eq.{clip['id']}", upd)
            posted.append(vid)
            log(f"▲ posted https://youtube.com/shorts/{vid}  —  {clip['title']}")
        except Exception as e:
            msg = str(e)
            errors.append(msg[:300])
            quota = "quotaExceeded" in msg or "uploadLimitExceeded" in msg
            db.update("clips", f"id=eq.{clip['id']}",
                      {"status": "ready" if quota else "failed", "error": msg[:1000]})
            log(f"! upload failed: {msg[:300]}")
            if quota:
                break
    if slot and posted:
        db.update("settings", "id=eq.1", {"last_slot_posted": slot.astimezone(timezone.utc).isoformat()})
    return {"posted": posted, "errors": errors, "slot": slot.isoformat() if slot else "forced"}


# ------------------------------------------------------------------ cleanup
def cleanup(db: DB, st: dict) -> dict:
    now = datetime.now(timezone.utc)
    # un-stick jobs that died mid-way
    stale = q_ts(now - timedelta(hours=3))
    for v in db.select("source_videos", f"status=eq.processing&processed_at=lt.{stale}"):
        db.update("source_videos", f"id=eq.{v['id']}",
                  {"status": "new" if v["attempts"] < 3 else "failed"})
    db.update("clips", f"status=eq.uploading&created_at=lt.{stale}", {"status": "ready"})
    # expire unposted clips to keep storage under the free tier
    old = q_ts(now - timedelta(days=st["keep_unposted_days"]))
    expired = db.select("clips", f"storage_path=not.is.null&status=neq.posted&created_at=lt.{old}")
    if expired:
        db.delete_files("clips", [c["storage_path"] for c in expired])
        for c in expired:
            db.update("clips", f"id=eq.{c['id']}", {"storage_path": None, "status": "rejected",
                                                    "error": "expired before posting"})
    # free storage of rejected clips
    rej = db.select("clips", "status=eq.rejected&storage_path=not.is.null")
    if rej:
        db.delete_files("clips", [c["storage_path"] for c in rej])
        for c in rej:
            db.update("clips", f"id=eq.{c['id']}", {"storage_path": None})
    return {"expired": len(expired), "freed_rejected": len(rej)}


def work_pending(db: DB, st: dict) -> tuple[int, str]:
    """How many videos the heavy job should process now (0 = skip it)."""
    if st["paused"]:
        return 0, "paused"
    backlog = db.select("clips", "status=in.(ready,needs_review)&select=id,size_bytes,storage_path")
    if len(backlog) >= st["max_ready_backlog"]:
        return 0, f"backlog full ({len(backlog)} clips waiting)"
    used = sum((c.get("size_bytes") or 0) for c in db.select(
        "clips", "storage_path=not.is.null&select=size_bytes"))
    if used / 1e6 >= st["storage_budget_mb"]:
        return 0, f"storage budget reached ({used / 1e6:.0f} MB)"
    pending = db.select("source_videos", "status=eq.new&select=id&limit=50")
    return len(pending), "ok"


# ------------------------------------------------------------------ main
def main() -> int:
    db = DB()
    st = db.settings()
    run_id = db.start_run("tick")
    summary: dict = {}
    ok = True
    force = os.environ.get("FORCE_PUBLISH", "").lower() in ("1", "true", "yes")
    try:
        if st["paused"] and not force:
            summary["paused"] = True
            log("Farm is paused (Settings → Paused).")
        else:
            log("== discover ==");  summary["discover"] = discover(db, st)
            log("== publish ==");   summary["publish"] = publish(db, st, force)
            log(f"   {summary['publish']}")
        log("== cleanup ==");       summary["cleanup"] = cleanup(db, st)
        pending, why = work_pending(db, db.settings())
        summary["pending"] = pending
        summary["pending_reason"] = why
        log(f"== pending videos for processing: {pending} ({why}) ==")
    except Exception as e:
        ok = False
        summary["error"] = str(e)[:1000]
        traceback.print_exc()
        pending = 0
    db.finish_run(run_id, ok, summary)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"pending={pending}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
