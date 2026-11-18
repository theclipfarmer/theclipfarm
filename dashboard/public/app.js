import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const cfg = window.CLIPFARM_CONFIG || {};
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ago = (iso) => {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};
const fmtT = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), 3500);
}

if (!cfg.supabaseUrl || !cfg.supabaseAnonKey) {
  $("#setup").classList.remove("hidden");
  throw new Error("Missing Supabase config");
}
const sb = createClient(cfg.supabaseUrl, cfg.supabaseAnonKey);
let settings = null;
let queueFilter = "all";
let creatorsById = {};

// ─────────────────────────────── auth
async function boot() {
  const { data: { session } } = await sb.auth.getSession();
  if (!session) return showLogin();
  const { data: owner, error } = await sb.rpc("is_owner");
  if (error || owner !== true) {
    showLogin(`Signed in as ${session.user.email}, but that email isn't in dashboard_users. Add it in Supabase, then refresh.`);
    return;
  }
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  await loadAll();
  setInterval(() => refreshActive(), 30000);
}
function showLogin(msg = "") {
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#login-msg").textContent = msg;
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const email = $("#login-email").value.trim();
  const { error } = await sb.auth.signInWithOtp({ email, options: { emailRedirectTo: location.origin } });
  $("#login-msg").textContent = error ? error.message : "Check your inbox for the sign-in link ✉️";
});
$("#logout").addEventListener("click", async () => { await sb.auth.signOut(); location.reload(); });
sb.auth.onAuthStateChange((evt) => { if (evt === "SIGNED_IN" && $("#app").classList.contains("hidden")) boot(); });

// ─────────────────────────────── tabs
let activeTab = "overview";
$$("#tabs button").forEach((b) => b.addEventListener("click", () => {
  activeTab = b.dataset.tab;
  $$("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
  $$("[data-panel]").forEach((p) => p.classList.toggle("hidden", p.dataset.panel !== activeTab));
  refreshActive();
}));
function refreshActive() {
  ({ overview: loadOverview, creators: loadCreators, queue: loadQueue, posted: loadPosted,
     settings: loadSettings, activity: loadActivity })[activeTab]?.();
}
async function loadAll() {
  await loadSettingsRow();
  await loadCreatorsMap();
  loadOverview();
}
async function loadSettingsRow() {
  const { data, error } = await sb.from("settings").select("*").eq("id", 1).single();
  if (error) return toast(error.message, true);
  settings = data;
}
async function loadCreatorsMap() {
  const { data } = await sb.from("creators").select("*");
  creatorsById = Object.fromEntries((data || []).map((c) => [c.id, c]));
}

// ─────────────────────────────── overview
async function loadOverview() {
  const [{ data: stats }, { data: runs }, { data: next }] = await Promise.all([
    sb.from("farm_stats").select("*").single(),
    sb.from("runs").select("*").order("started_at", { ascending: false }).limit(6),
    sb.from("clips").select("*").eq("status", "ready").not("storage_path", "is", null)
      .order("score", { ascending: false, nullsFirst: false }).limit(4),
  ]);
  await loadSettingsRow();
  const s = stats || {};
  $("#stats").innerHTML = [
    ["Creators", s.creators_enabled], ["Videos waiting", s.videos_pending],
    ["Needs review", s.clips_review], ["Ready to post", s.clips_ready, true],
    ["Posted (24h)", s.posted_24h], ["Posted total", s.clips_posted],
  ].map(([l, n, hot]) => `<div class="stat ${hot ? "hot" : ""}"><div class="n">${n ?? 0}</div><div class="l">${l}</div></div>`).join("");
  $("#queue-count").textContent = (s.clips_review || 0) + (s.clips_ready || 0) || "";
  $("#pause-toggle").checked = !!settings?.paused;

  const banner = $("#mode-banner");
  if (settings?.upload_mode === "manual") {
    banner.innerHTML = "📥 <b>Manual upload mode.</b> Clips are rendered and waiting in the Queue — download and post them from YouTube Studio or the YouTube app. Switch to <b>API</b> mode in Settings once your Google project passes YouTube's API audit.";
    banner.classList.remove("hidden");
  } else if (settings?.paused) {
    banner.innerHTML = "⏸ <b>The farm is paused.</b> Nothing will be clipped or posted until you unpause.";
    banner.classList.remove("hidden");
  } else banner.classList.add("hidden");

  $("#next-up").innerHTML = (next || []).map(clipCard).join("") || `<p class="muted">Nothing ready yet.</p>`;
  bindClipCards($("#next-up"));
  $("#recent-runs").innerHTML = (runs || []).map(runRow).join("") || `<p class="muted">No runs yet — the GitHub workflow runs hourly.</p>`;
}
$("#pause-toggle").addEventListener("change", async (e) => {
  const { error } = await sb.from("settings").update({ paused: e.target.checked, updated_at: new Date().toISOString() }).eq("id", 1);
  toast(error ? error.message : e.target.checked ? "Farm paused" : "Farm running", !!error);
  loadOverview();
});
async function trigger(action) {
  const { data: { session } } = await sb.auth.getSession();
  $("#trigger-msg").textContent = "Starting…";
  try {
    const r = await fetch("/.netlify/functions/trigger", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${session.access_token}` },
      body: JSON.stringify({ action }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    $("#trigger-msg").textContent = `✅ Started (${j.started}). GitHub takes ~30s to pick it up; check Activity in a few minutes.`;
  } catch (err) {
    $("#trigger-msg").textContent = `❌ ${err.message}`;
  }
}
$("#run-now").addEventListener("click", () => trigger("run"));
$("#post-now").addEventListener("click", () => {
  if (settings?.upload_mode !== "api") return toast("Posting via API is off (manual mode). Download clips from the Queue instead.", true);
  trigger("post");
});

// ─────────────────────────────── creators
async function loadCreators() {
  const { data, error } = await sb.from("creators").select("*").order("created_at");
  if (error) return toast(error.message, true);
  creatorsById = Object.fromEntries(data.map((c) => [c.id, c]));
  const rows = data.map((c) => `
    <tr data-id="${c.id}">
      <td><b>${esc(c.name || c.handle || c.channel_id)}</b><br>
        <span class="muted small">${c.channel_id ? `<a target="_blank" href="https://www.youtube.com/channel/${esc(c.channel_id)}">${esc(c.handle || c.channel_id)}</a>` : esc(c.handle) + " · resolving on next run"}</span></td>
      <td><label class="check"><input type="checkbox" data-k="enabled" ${c.enabled ? "checked" : ""}/> On</label></td>
      <td><label class="check"><input type="checkbox" data-k="permission_confirmed" ${c.permission_confirmed ? "checked" : ""}/> ${c.permission_confirmed ? "Yes" : "<span style='color:var(--red)'>No</span>"}</label>
        <div class="muted small">${esc(c.permission_note || "")}</div></td>
      <td><input type="number" min="1" max="10" data-k="max_clips_per_video" value="${c.max_clips_per_video ?? ""}" placeholder="${settings?.clips_per_video ?? 3}"/></td>
      <td class="muted small">${ago(c.last_checked_at)}</td>
      <td><button class="btn small danger" data-del>Remove</button></td>
    </tr>`).join("");
  $("#creators-table").innerHTML = `<tr><th>Creator</th><th>Enabled</th><th>Permission</th><th>Clips / video</th><th>Checked</th><th></th></tr>` +
    (rows || `<tr><td colspan="6" class="empty">Add your first creator above.</td></tr>`);
  $$("#creators-table tr[data-id]").forEach((tr) => {
    const id = tr.dataset.id;
    $$("input[data-k]", tr).forEach((inp) => inp.addEventListener("change", async () => {
      const k = inp.dataset.k;
      const v = inp.type === "checkbox" ? inp.checked : inp.value === "" ? null : Number(inp.value);
      const { error } = await sb.from("creators").update({ [k]: v }).eq("id", id);
      toast(error ? error.message : "Saved", !!error);
      if (inp.type === "checkbox") loadCreators();
    }));
    $("[data-del]", tr).addEventListener("click", async () => {
      if (!confirm("Remove this creator and all of their clips?")) return;
      const { error } = await sb.from("creators").delete().eq("id", id);
      toast(error ? error.message : "Removed", !!error);
      loadCreators();
    });
  });
}
$("#creator-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const raw = $("#creator-input").value.trim();
  const idMatch = raw.match(/UC[0-9A-Za-z_-]{22}/);
  const handleMatch = raw.match(/@([A-Za-z0-9._-]{3,})/);
  const row = {
    channel_id: idMatch ? idMatch[0] : null,
    handle: idMatch ? null : handleMatch ? "@" + handleMatch[1] : "@" + raw.replace(/^@/, ""),
    permission_confirmed: $("#creator-perm").checked,
    permission_note: $("#creator-note").value.trim() || null,
  };
  const { error } = await sb.from("creators").insert(row);
  if (error) return toast(error.message, true);
  e.target.reset();
  toast("Creator added — they'll be checked on the next run");
  loadCreators();
});

// ─────────────────────────────── queue
$$("#queue-filter button").forEach((b) => b.addEventListener("click", () => {
  queueFilter = b.dataset.f;
  $$("#queue-filter button").forEach((x) => x.classList.toggle("active", x === b));
  loadQueue();
}));
async function loadQueue() {
  let q = sb.from("clips").select("*").order("created_at", { ascending: false }).limit(60);
  q = queueFilter === "all" ? q.in("status", ["needs_review", "ready", "failed"]) : q.eq("status", queueFilter);
  const { data, error } = await q;
  if (error) return toast(error.message, true);
  $("#queue").innerHTML = data.map(clipCard).join("") || `<p class="muted">Queue is empty. New clips appear here after each run.</p>`;
  bindClipCards($("#queue"));
}
function clipCard(c) {
  const cr = creatorsById[c.creator_id];
  const thumb = `https://i.ytimg.com/vi/${esc(c.source_video_id)}/hqdefault.jpg`;
  const src = `https://www.youtube.com/watch?v=${esc(c.source_video_id)}&t=${Math.floor(c.start_s)}s`;
  const manual = settings?.upload_mode !== "api";
  return `
  <div class="clip" data-id="${c.id}" data-path="${esc(c.storage_path || "")}">
    <div class="media" style="background-image:url('${thumb}')">
      <div class="badges"><span class="badge ${c.status}">${c.status.replace("_", " ")}</span>
        <span class="badge">${esc(c.method || "")} ${c.score != null ? Number(c.score).toFixed(2) : ""}</span>
        <span class="badge">${Math.round(c.end_s - c.start_s)}s</span></div>
      ${c.storage_path ? `<div class="play">▶</div>` : `<div class="muted small">no file</div>`}
    </div>
    <div class="body">
      <textarea class="title" rows="2" data-title>${esc(c.title)}</textarea>
      <div class="meta">${esc(cr?.name || cr?.handle || "")} · <a target="_blank" href="${src}">source @ ${fmtT(c.start_s)}</a> · ${ago(c.created_at)}</div>
      ${c.error ? `<div class="err">${esc(c.error).slice(0, 220)}</div>` : ""}
      <div class="actions">
        ${c.status === "needs_review" ? `<button class="btn small primary" data-act="approve">Approve</button>` : ""}
        ${c.status === "failed" && c.storage_path ? `<button class="btn small" data-act="retry">Retry</button>` : ""}
        ${c.storage_path ? `<button class="btn small" data-act="download">⬇ Download</button>` : ""}
        ${manual && c.status !== "posted" ? `<button class="btn small" data-act="posted">Mark posted</button>` : ""}
        ${c.status !== "posted" ? `<button class="btn small danger" data-act="reject">Reject</button>` : ""}
        <button class="btn small ghost" data-act="copy" title="Copy title + description">Copy text</button>
      </div>
    </div>
  </div>`;
}
function bindClipCards(root) {
  $$(".clip", root).forEach((card) => {
    const id = card.dataset.id, path = card.dataset.path;
    const media = $(".media", card);
    media.addEventListener("click", async () => {
      if (!path || $("video", media)) return;
      const { data, error } = await sb.storage.from("clips").createSignedUrl(path, 3600);
      if (error) return toast(error.message, true);
      media.innerHTML = `<video src="${data.signedUrl}" controls autoplay playsinline></video>`;
    });
    const ta = $("[data-title]", card);
    ta.addEventListener("change", async () => {
      const { error } = await sb.from("clips").update({ title: ta.value.trim().slice(0, 100) }).eq("id", id);
      toast(error ? error.message : "Title saved", !!error);
    });
    $$("[data-act]", card).forEach((b) => b.addEventListener("click", () => clipAction(b.dataset.act, id, path, card)));
  });
}
async function clipAction(act, id, path, card) {
  let upd = null;
  if (act === "approve") upd = { status: "ready" };
  if (act === "retry") upd = { status: "ready", error: null };
  if (act === "reject") upd = { status: "rejected" };
  if (act === "posted") {
    const url = prompt("Paste the YouTube URL of the posted Short (optional):") ?? null;
    if (url === null) return;
    const m = url.match(/(?:shorts\/|v=|youtu\.be\/)([A-Za-z0-9_-]{11})/);
    upd = { status: "posted", posted_at: new Date().toISOString(), youtube_video_id: m ? m[1] : null };
  }
  if (act === "download") {
    const { data, error } = await sb.storage.from("clips").createSignedUrl(path, 3600, { download: `clip-${id.slice(0, 8)}.mp4` });
    if (error) return toast(error.message, true);
    location.href = data.signedUrl;
    return;
  }
  if (act === "copy") {
    const { data } = await sb.from("clips").select("title,description").eq("id", id).single();
    await navigator.clipboard.writeText(`${data.title}\n\n${data.description || ""}`);
    return toast("Title + description copied");
  }
  if (!upd) return;
  const { error } = await sb.from("clips").update(upd).eq("id", id);
  if (error) return toast(error.message, true);
  toast("Updated");
  card.remove();
  if (activeTab === "overview") loadOverview();
}

// ─────────────────────────────── posted
async function loadPosted() {
  const { data, error } = await sb.from("clips").select("*").eq("status", "posted").order("posted_at", { ascending: false }).limit(100);
  if (error) return toast(error.message, true);
  $("#posted-table").innerHTML = `<tr><th>Title</th><th>Creator</th><th>Source</th><th>Posted</th><th>YouTube</th></tr>` + (data.map((c) => {
    const cr = creatorsById[c.creator_id];
    return `<tr><td>${esc(c.title)}</td><td>${esc(cr?.name || cr?.handle || "")}</td>
      <td><a target="_blank" href="https://www.youtube.com/watch?v=${esc(c.source_video_id)}&t=${Math.floor(c.start_s)}s">${fmtT(c.start_s)}</a></td>
      <td class="muted small">${ago(c.posted_at)}</td>
      <td>${c.youtube_video_id ? `<a target="_blank" href="https://youtube.com/shorts/${esc(c.youtube_video_id)}">Open ↗</a>` : "—"}</td></tr>`;
  }).join("") || `<tr><td colspan="5" class="empty">Nothing posted yet.</td></tr>`);
}

// ─────────────────────────────── settings
const FIELDS = [
  ["Posting", [
    ["upload_mode", "select", "Upload mode", ["manual", "api"], "Use 'manual' until your Google Cloud project passes YouTube's API audit — before that, API uploads are locked private."],
    ["post_times", "list", "Post times (24h, comma-separated)", null, "e.g. 09:00, 13:00, 18:00"],
    ["timezone", "text", "Timezone", null, "IANA name, e.g. America/Vancouver"],
    ["clips_per_slot", "number", "Clips per post time"],
    ["privacy_status", "select", "Privacy", ["public", "unlisted", "private"]],
    ["require_approval", "bool", "Require my approval before posting"],
  ]],
  ["Clipping", [
    ["clips_per_video", "number", "Clips per source video"],
    ["clip_target_s", "number", "Target clip length (s)"],
    ["clip_min_s", "number", "Min length (s)"],
    ["clip_max_s", "number", "Max length (s)", null, "Keep ≤ 60 for maximum Shorts compatibility"],
    ["max_video_age_hours", "number", "Only clip videos newer than (hours)"],
    ["min_source_duration_s", "number", "Skip source videos shorter than (s)"],
    ["videos_per_run", "number", "Videos processed per run"],
    ["max_ready_backlog", "number", "Stop rendering when this many clips are waiting"],
  ]],
  ["Look & text", [
    ["burn_captions", "bool", "Burn in captions + hook title"],
    ["show_credit", "bool", "Show 'via Creator' credit on video"],
    ["title_template", "text", "Title template", null, "Variables: {hook} {creator} {source_title} {part}"],
    ["description_template", "textarea", "Description template", null, "Variables: {hook} {creator} {source_url} {channel_url} {hashtags}"],
    ["default_tags", "list", "Tags (comma-separated)"],
  ]],
  ["Storage", [
    ["storage_budget_mb", "number", "Storage budget (MB)", null, "Supabase free tier = 1 GB"],
    ["keep_unposted_days", "number", "Delete unposted clips after (days)"],
    ["delete_after_post", "bool", "Delete clip file after it's posted"],
  ]],
];
async function loadSettings() {
  await loadSettingsRow();
  const s = settings;
  const field = ([k, type, label, opts, hint]) => {
    const v = s[k];
    const h = hint ? `<span class="hint">${esc(hint)}</span>` : "";
    if (type === "bool") return `<label class="inline"><input type="checkbox" name="${k}" ${v ? "checked" : ""}/> ${esc(label)}</label>`;
    if (type === "select") return `<label>${esc(label)}<select name="${k}">${opts.map((o) => `<option ${o === v ? "selected" : ""}>${o}</option>`).join("")}</select>${h}</label>`;
    if (type === "textarea") return `<label>${esc(label)}<textarea name="${k}">${esc(v)}</textarea>${h}</label>`;
    if (type === "list") return `<label>${esc(label)}<input name="${k}" data-list value="${esc((v || []).join(", "))}"/>${h}</label>`;
    return `<label>${esc(label)}<input name="${k}" type="${type}" value="${esc(v)}"/>${h}</label>`;
  };
  $("#settings-form").innerHTML = FIELDS.map(([legend, fs]) =>
    `<fieldset><legend>${legend}</legend>${fs.map(field).join("")}</fieldset>`).join("") +
    `<div class="save"><button class="btn primary" type="submit">Save settings</button></div>`;
}
$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const upd = { updated_at: new Date().toISOString() };
  for (const [, fs] of FIELDS) for (const [k, type] of fs) {
    const el = e.target.elements[k];
    if (type === "bool") upd[k] = el.checked;
    else if (type === "number") upd[k] = Number(el.value);
    else if (type === "list") upd[k] = el.value.split(",").map((x) => x.trim()).filter(Boolean);
    else upd[k] = el.value;
  }
  if (upd.post_times.some((t) => !/^\d{1,2}:\d{2}$/.test(t))) return toast("Post times must look like 09:00", true);
  if (upd.clip_min_s > upd.clip_max_s) return toast("Min length can't exceed max length", true);
  const { error } = await sb.from("settings").update(upd).eq("id", 1);
  toast(error ? error.message : "Settings saved", !!error);
  if (!error) await loadSettingsRow();
});

// ─────────────────────────────── activity
function runRow(r) {
  const s = r.summary || {};
  const bits = [];
  if (s.discover) bits.push(`${s.discover.new_videos} new videos`);
  if (s.publish?.posted?.length) bits.push(`posted ${s.publish.posted.length}`);
  if (s.pending != null) bits.push(`${s.pending} to clip`);
  if (s.results) bits.push(`${s.results.reduce((a, x) => a + (x.clips || 0), 0)} clips made`);
  if (s.paused) bits.push("paused");
  const status = r.ok == null ? `<span class="status processing">running</span>` : r.ok ? `<span class="status ok">ok</span>` : `<span class="status bad">error</span>`;
  return `<details class="run"><summary>${status}<b>${esc(r.job)}</b><span class="muted small">${ago(r.started_at)}</span><span class="small">${esc(bits.join(" · "))}</span></summary><pre>${esc(JSON.stringify(s, null, 2))}</pre></details>`;
}
async function loadActivity() {
  const [{ data: vids }, { data: runs }] = await Promise.all([
    sb.from("source_videos").select("*").order("discovered_at", { ascending: false }).limit(40),
    sb.from("runs").select("*").order("started_at", { ascending: false }).limit(40),
  ]);
  $("#videos-table").innerHTML = `<tr><th>Video</th><th>Creator</th><th>Published</th><th>Status</th><th></th></tr>` + ((vids || []).map((v) => {
    const cr = creatorsById[v.creator_id];
    return `<tr data-id="${esc(v.id)}"><td><a target="_blank" href="https://www.youtube.com/watch?v=${esc(v.id)}">${esc(v.title)}</a>
      ${v.error ? `<div class="err small" style="color:var(--red)">${esc(v.error).slice(0, 200)}</div>` : ""}</td>
      <td>${esc(cr?.name || cr?.handle || "")}</td><td class="muted small">${ago(v.published_at)}</td>
      <td><span class="status ${v.status}">${v.status}</span></td>
      <td>${["failed", "skipped"].includes(v.status) ? `<button class="btn small" data-requeue>Retry</button>` : ""}</td></tr>`;
  }).join("") || `<tr><td colspan="5" class="empty">No videos discovered yet.</td></tr>`);
  $$("#videos-table [data-requeue]").forEach((b) => b.addEventListener("click", async () => {
    const id = b.closest("tr").dataset.id;
    const { error } = await sb.from("source_videos").update({ status: "new", attempts: 0, error: null, discovered_at: new Date().toISOString() }).eq("id", id);
    toast(error ? error.message : "Re-queued for the next run", !!error);
    loadActivity();
  }));
  $("#runs").innerHTML = (runs || []).map(runRow).join("") || `<p class="muted">No runs yet.</p>`;
}

boot();
