-- =====================================================================
-- GTA6 Clip Farm — Supabase schema
-- Paste this whole file into Supabase → SQL Editor → Run.
-- It is safe to re-run (everything is "if not exists" / "on conflict").
-- =====================================================================

-- ---------- Who may use the dashboard --------------------------------
-- Only emails in this table can read/write anything from the dashboard.
-- The GitHub worker uses the service-role key, which bypasses RLS.
create table if not exists public.dashboard_users (
  email text primary key
);
alter table public.dashboard_users enable row level security;

-- >>> CHANGE THIS to the email you will log into the dashboard with <<<
insert into public.dashboard_users (email) values ('you@example.com')
on conflict do nothing;

create or replace function public.is_owner() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from public.dashboard_users
    where lower(email) = lower(coalesce(auth.jwt() ->> 'email', ''))
  );
$$;

-- ---------- Creators you clip from ------------------------------------
create table if not exists public.creators (
  id                    uuid primary key default gen_random_uuid(),
  handle                text,                 -- e.g. @SomeCreator (resolved to channel_id by the worker)
  channel_id            text unique,          -- UCxxxxxxxx
  name                  text,
  enabled               boolean not null default true,
  permission_confirmed  boolean not null default false,  -- worker skips creators without this
  permission_note       text,                 -- where/how they gave permission
  max_clips_per_video   int,                  -- null = use global setting
  last_checked_at       timestamptz,
  created_at            timestamptz not null default now()
);

-- ---------- Videos found on those channels ---------------------------
create table if not exists public.source_videos (
  id             text primary key,            -- YouTube video id
  creator_id     uuid references public.creators(id) on delete cascade,
  title          text,
  published_at   timestamptz,
  duration_s     int,
  status         text not null default 'new'
                 check (status in ('new','processing','clipped','skipped','failed')),
  attempts       int not null default 0,
  error          text,
  discovered_at  timestamptz not null default now(),
  processed_at   timestamptz
);
create index if not exists source_videos_status_idx on public.source_videos(status, published_at desc);

-- ---------- Rendered clips --------------------------------------------
create table if not exists public.clips (
  id                uuid primary key default gen_random_uuid(),
  source_video_id   text references public.source_videos(id) on delete cascade,
  creator_id        uuid references public.creators(id) on delete set null,
  start_s           numeric not null,
  end_s             numeric not null,
  score             numeric,
  method            text,                     -- heatmap | captions | audio | spread
  title             text,
  description       text,
  tags              text[],
  status            text not null default 'ready'
                    check (status in ('needs_review','ready','rejected','uploading','posted','failed')),
  storage_path      text,                     -- path inside the "clips" storage bucket
  size_bytes        bigint,
  youtube_video_id  text,
  error             text,
  created_at        timestamptz not null default now(),
  posted_at         timestamptz
);
create index if not exists clips_status_idx on public.clips(status, score desc);

-- ---------- Single-row settings ---------------------------------------
create table if not exists public.settings (
  id                    int primary key default 1 check (id = 1),
  paused                boolean not null default false,
  require_approval      boolean not null default false,   -- true = clips wait in "needs_review"
  upload_mode           text not null default 'manual'    -- 'manual' until your API project passes YouTube's audit
                        check (upload_mode in ('api','manual')),
  privacy_status        text not null default 'public' check (privacy_status in ('public','unlisted','private')),
  timezone              text not null default 'America/Vancouver',
  post_times            text[] not null default array['09:00','13:00','18:00'],
  clips_per_slot        int not null default 1,
  last_slot_posted      timestamptz,
  clips_per_video       int not null default 3,
  clip_target_s         int not null default 40,
  clip_min_s            int not null default 20,
  clip_max_s            int not null default 58,
  max_video_age_hours   int not null default 72,
  min_source_duration_s int not null default 180,          -- skip Shorts / very short uploads
  videos_per_run        int not null default 3,
  max_ready_backlog     int not null default 12,             -- stop rendering when this many clips are waiting
  storage_budget_mb     int not null default 800,            -- Supabase free tier storage is 1 GB
  burn_captions         boolean not null default true,
  show_credit           boolean not null default true,
  title_template        text not null default '{hook} #GTA6 #Shorts',
  description_template  text not null default
    E'{hook}\n\n🎥 Clipped from {creator}: {source_url}\nGo support the original creator → {channel_url}\n\n{hashtags}',
  default_tags          text[] not null default array['GTA6','GTA 6','Grand Theft Auto VI','GTA VI','gaming','shorts'],
  delete_after_post     boolean not null default true,
  keep_unposted_days    int not null default 14,
  updated_at            timestamptz not null default now()
);
insert into public.settings (id) values (1) on conflict do nothing;

-- ---------- Run log ----------------------------------------------------
create table if not exists public.runs (
  id           bigint generated always as identity primary key,
  job          text not null,
  started_at   timestamptz not null default now(),
  finished_at  timestamptz,
  ok           boolean,
  summary      jsonb
);
create index if not exists runs_started_idx on public.runs(started_at desc);

-- ---------- Row level security ----------------------------------------
do $$
declare t text;
begin
  foreach t in array array['creators','source_videos','clips','settings','runs'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('drop policy if exists owner_all on public.%I', t);
    execute format('create policy owner_all on public.%I for all to authenticated using (public.is_owner()) with check (public.is_owner())', t);
  end loop;
end $$;

-- ---------- Storage bucket for rendered clips -------------------------
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('clips', 'clips', false, 52428800, array['video/mp4'])
on conflict (id) do nothing;

drop policy if exists "owner read clips" on storage.objects;
create policy "owner read clips" on storage.objects
  for select to authenticated using (bucket_id = 'clips' and public.is_owner());

drop policy if exists "owner delete clips" on storage.objects;
create policy "owner delete clips" on storage.objects
  for delete to authenticated using (bucket_id = 'clips' and public.is_owner());

-- ---------- Handy stats view for the dashboard ------------------------
create or replace view public.farm_stats with (security_invoker = true) as
select
  (select count(*) from public.creators where enabled)                         as creators_enabled,
  (select count(*) from public.source_videos where status = 'new')             as videos_pending,
  (select count(*) from public.clips where status = 'needs_review')            as clips_review,
  (select count(*) from public.clips where status = 'ready')                   as clips_ready,
  (select count(*) from public.clips where status = 'posted')                  as clips_posted,
  (select count(*) from public.clips where status = 'posted'
     and posted_at > now() - interval '24 hours')                              as posted_24h;
