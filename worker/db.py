"""Supabase access via its REST (PostgREST) and Storage HTTP APIs, using the service-role key."""
from __future__ import annotations

import os
import urllib.parse
from datetime import datetime, timezone

from .net import request


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"Missing required environment variable: {name}")
    return v


class DB:
    def __init__(self):
        self.url = _env("SUPABASE_URL").rstrip("/")
        self.key = _env("SUPABASE_SERVICE_ROLE_KEY")
        # New-style secret keys (sb_secret_…) go in the apikey header only;
        # legacy service_role JWTs (eyJ…) also need the Authorization header.
        self.h = {"apikey": self.key}
        if not self.key.startswith("sb_"):
            self.h["Authorization"] = f"Bearer {self.key}"

    # ---------------- tables ----------------
    def select(self, table: str, query: str = "") -> list[dict]:
        return request("GET", f"{self.url}/rest/v1/{table}?{query}", headers=self.h) or []

    def insert(self, table: str, rows, *, on_conflict: str | None = None, ignore_dupes=False) -> list[dict]:
        h = {**self.h, "Prefer": "return=representation"}
        q = ""
        if on_conflict:
            q = f"?on_conflict={on_conflict}"
            h["Prefer"] += ",resolution=" + ("ignore-duplicates" if ignore_dupes else "merge-duplicates")
        return request("POST", f"{self.url}/rest/v1/{table}{q}", headers=h, data=rows) or []

    def update(self, table: str, match: str, values: dict) -> list[dict]:
        h = {**self.h, "Prefer": "return=representation"}
        return request("PATCH", f"{self.url}/rest/v1/{table}?{match}", headers=h, data=values) or []

    def settings(self) -> dict:
        rows = self.select("settings", "id=eq.1")
        if not rows:
            raise SystemExit("settings row missing — did you run supabase/schema.sql?")
        return rows[0]

    # ---------------- run log ----------------
    def start_run(self, job: str) -> int | None:
        try:
            return self.insert("runs", {"job": job})[0]["id"]
        except Exception:
            return None

    def finish_run(self, run_id: int | None, ok: bool, summary: dict):
        if run_id is None:
            return
        try:
            self.update("runs", f"id=eq.{run_id}",
                        {"finished_at": now_iso(), "ok": ok, "summary": summary})
        except Exception:
            pass

    # ---------------- storage ----------------
    def upload_file(self, bucket: str, path: str, file_path: str, content_type="video/mp4"):
        with open(file_path, "rb") as f:
            data = f.read()
        h = {**self.h, "Content-Type": content_type, "x-upsert": "true"}
        return request("POST", f"{self.url}/storage/v1/object/{bucket}/{_q(path)}",
                       headers=h, raw=data, timeout=600)

    def download_file(self, bucket: str, path: str, dest: str):
        data = request("GET", f"{self.url}/storage/v1/object/authenticated/{bucket}/{_q(path)}",
                       headers=self.h, timeout=600)
        if isinstance(data, str):
            data = data.encode()
        with open(dest, "wb") as f:
            f.write(data)
        return dest

    def delete_files(self, bucket: str, paths: list[str]):
        if not paths:
            return
        request("DELETE", f"{self.url}/storage/v1/object/{bucket}",
                headers=self.h, data={"prefixes": paths})


def _q(path: str) -> str:
    return urllib.parse.quote(path, safe="/")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def q_ts(dt: datetime) -> str:
    """Timestamp safe to put in a PostgREST query string (no '+' which URLs read as a space)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
