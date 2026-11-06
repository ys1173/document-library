-- Settings changed at runtime from the portal. Webhook URLs can carry tokens, so the read-only agent role gets no access.
create table doclib.settings (
  key        text primary key,
  value      jsonb not null,
  updated_at timestamptz not null default now()
);
grant select, insert, update, delete on doclib.settings to doclib_rw;
