-- Document Library schema. {{EMBEDDING_DIM}} is substituted at migrate time.

do $$ begin
  if not exists (select from pg_roles where rolname = 'doclib_rw') then create role doclib_rw nologin; end if;
  if not exists (select from pg_roles where rolname = 'doclib_ro') then create role doclib_ro nologin; end if;
end $$;

create table doclib.sources (
  source_id      text primary key,
  tenant_id      text not null default 'default',
  rclone_remote  text not null,
  root_path      text not null,
  config         jsonb not null,
  origin         text not null default 'file' check (origin in ('file', 'api')),
  enabled        boolean not null default true,
  scan_requested_at timestamptz,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  last_scan_at   timestamptz,
  last_scan_ok   boolean,
  last_scan_error text,
  last_success_at timestamptz,
  baseline_at    timestamptz
);

create table doclib.files (
  file_id        uuid primary key,
  tenant_id      text not null,
  source_id      text not null references doclib.sources,
  path           text not null,
  size_bytes     bigint,
  modtime        timestamptz,
  content_hash   text,
  content_version text not null,
  indexed_version text,
  extractor      text,
  mime_type      text,
  language       text,
  title          text,
  tags           text[] not null default '{}',
  acl            jsonb,
  status         text not null check (status in ('pending','indexed','failed','deleted','skipped')),
  error          text,
  indexed_at     timestamptz,
  changed_at     timestamptz not null default now(),
  deleted_at     timestamptz
);
create unique index files_live_path on doclib.files (source_id, path) where deleted_at is null;
create index files_source_status on doclib.files (source_id, status);
create index files_tenant on doclib.files (tenant_id);

create table doclib.outlines (
  file_id        uuid references doclib.files on delete cascade,
  content_version text,
  outline        jsonb not null,
  primary key (file_id, content_version)
);

create table doclib.chunks (
  chunk_id       text primary key,
  file_id        uuid not null references doclib.files on delete cascade,
  tenant_id      text not null,
  content_version text not null,
  ordinal        int not null,
  anchor         text not null,
  heading_path   text,
  page_start     int,
  page_end       int,
  block_types    text[] not null default '{}',
  extraction_path text,
  quality        text,
  text           text not null,
  token_count    int,
  embedding      vector({{EMBEDDING_DIM}}),
  embedding_model text,
  embedding_dim  int,
  doc_label      text,  -- words from the file name and title, so a document can be found by what it's called
  tsv            tsvector generated always as
                   (to_tsvector('simple', coalesce(doc_label, '') || ' ' || coalesce(heading_path, '') || ' ' || text)) stored
);
create index chunks_embedding on doclib.chunks using hnsw (embedding vector_cosine_ops);
create index chunks_tsv on doclib.chunks using gin (tsv);
create index chunks_file on doclib.chunks (file_id, ordinal);
create index chunks_tenant on doclib.chunks (tenant_id);

create table doclib.jobs (
  job_id         bigserial primary key,
  kind           text not null,
  file_id        uuid references doclib.files on delete cascade,
  content_version text,
  payload        jsonb,
  priority       int not null default 100,
  attempts       int not null default 0,
  run_after      timestamptz not null default now(),
  locked_by      text,
  locked_at      timestamptz,
  last_error     text,
  status         text not null default 'queued' check (status in ('queued','running','done','dead')),
  created_at     timestamptz not null default now(),
  finished_at    timestamptz
);
create index jobs_claim on doclib.jobs (status, priority, run_after);
-- One live job per (file_id, content_version): retries and duplicate scans are safe.
create unique index jobs_live_key on doclib.jobs (kind, file_id, content_version)
  where status in ('queued', 'running');

create table doclib.webhook_deliveries (
  delivery_id    uuid primary key,
  tenant_id      text not null,
  source_id      text not null,
  payload        jsonb not null,
  status         text not null default 'pending' check (status in ('pending','sent','dead')),
  attempts       int not null default 0,
  next_attempt_at timestamptz not null default now(),
  last_error     text,
  created_at     timestamptz not null default now(),
  sent_at        timestamptz
);
create index webhook_deliveries_due on doclib.webhook_deliveries (status, next_attempt_at);

create table doclib.change_events (
  event_id       bigserial primary key,
  tenant_id      text not null,
  source_id      text not null,
  scan_id        uuid not null,
  file_id        uuid not null references doclib.files on delete cascade,
  change         text not null check (change in ('new','changed','deleted','moved')),
  path           text not null,
  previous_path  text,
  content_version text not null,
  state          text not null default 'pending' check (state in ('pending','ready')),
  outcome        text check (outcome in ('indexed','failed','skipped','deleted','moved')),
  error          text,
  delivery_id    uuid references doclib.webhook_deliveries,
  created_at     timestamptz not null default now()
);
create unique index change_events_key on doclib.change_events (file_id, content_version, change)
  where delivery_id is null;
create index change_events_unsent on doclib.change_events (source_id, scan_id) where delivery_id is null;
create index change_events_file_pending on doclib.change_events (file_id) where state = 'pending';

-- The API role is read-only. This is its single, narrow write path: open_doc found the live
-- file differs from the index, so queue a re-index at the live version.
create function doclib.request_reindex(p_file_id uuid, p_tenant_id text, p_live_version text)
returns boolean language plpgsql security definer set search_path = doclib, pg_temp as $$
declare f doclib.files%rowtype;
begin
  select * into f from doclib.files
   where file_id = p_file_id and tenant_id = p_tenant_id and deleted_at is null for update;
  if not found or f.content_version = p_live_version then
    if found then
      insert into doclib.jobs (kind, file_id, content_version, priority)
      select 'index', p_file_id, p_live_version, 50
       where f.indexed_version is distinct from p_live_version and f.status = 'pending'
      on conflict do nothing;
    end if;
    return false;
  end if;
  update doclib.files set content_version = p_live_version, status = 'pending', error = null,
         changed_at = now() where file_id = p_file_id;
  insert into doclib.jobs (kind, file_id, content_version, priority)
  values ('index', p_file_id, p_live_version, 50) on conflict do nothing;
  insert into doclib.change_events (tenant_id, source_id, scan_id, file_id, change, path, content_version)
  values (f.tenant_id, f.source_id, gen_random_uuid(), p_file_id, 'changed', f.path, p_live_version)
  on conflict do nothing;
  return true;
end $$;

grant usage on schema doclib to doclib_ro, doclib_rw;
grant select on all tables in schema doclib to doclib_ro;
grant select, insert, update, delete on all tables in schema doclib to doclib_rw;
grant usage, select on all sequences in schema doclib to doclib_rw;
revoke all on function doclib.request_reindex(uuid, text, text) from public;
grant execute on function doclib.request_reindex(uuid, text, text) to doclib_ro, doclib_rw;
