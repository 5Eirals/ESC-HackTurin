-- TO-Breath cloud: what the bins (Arduino UNO Q) send and what the control panel reads.
--
-- Nobody touches these tables directly. Bins and the control panel go through the Vercel API, which calls
-- the security definer functions below with the publishable key:
--   ingest_batch(device_token, batch)     a bin sends readings, fill measurements, events and its status
--   panel_bins(panel_key)                 the control panel lists the bins
--   panel_snapshot(panel_key, bin_code)   the control panel reads one bin (same shape as the board's dashboard)
--   panel_command(panel_key, bin_code, …) the control panel asks a bin to do something (e.g. "just emptied")
-- Tokens are random, shown once and stored only as sha256. Admins create them from the SQL editor:
--   select public.register_bin('TO-0001', 'Via Vincenzo Vela 17, Torino');   -- device token
--   select public.create_panel_key('Centro di controllo');                   -- control panel key
--
-- Works on its own or next to the IAMAimpresa kit schema: when public.has_access() exists, signed-in users
-- with the 'bins' feature can also read the tables directly (RLS).

create extension if not exists pgcrypto with schema extensions;

-- ---------------------------------------------------------------------------
-- 1. Types
-- ---------------------------------------------------------------------------

create type public.bin_event_severity as enum ('info', 'warning', 'critical');
create type public.fill_source as enum ('camera', 'sim');
create type public.bin_command_kind as enum ('mark_emptied');

-- ---------------------------------------------------------------------------
-- 2. Tables
-- ---------------------------------------------------------------------------

create table public.bins (
  id uuid primary key default gen_random_uuid(),
  code text not null unique check (code ~ '^[A-Z0-9][A-Z0-9-]{1,31}$'),
  address text not null default '' check (length(address) <= 200),
  token_hash bytea not null unique,
  last_seen_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
comment on table public.bins is
  'One row per bin (one Arduino UNO Q each). The board authenticates with the token whose sha256 is token_hash.';

create table public.bin_status (
  bin_id uuid primary key references public.bins (id) on delete cascade,
  state jsonb not null default '{}'::jsonb,
  reported_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
comment on table public.bin_status is
  'Latest state reported by each bin (gas alarm, odour, fill prediction and statistics, last readings), replaced at every sync.';

create table public.readings (
  id uuid primary key default gen_random_uuid(),
  bin_id uuid not null references public.bins (id) on delete cascade,
  minute_at timestamptz not null,
  samples smallint not null check (samples between 1 and 120),
  temperature_c real,
  humidity_pct real,
  tvoc_ppb real,
  tvoc_max_ppb integer,
  eco2_ppm real,
  h2_raw real,
  ethanol_raw real,
  created_at timestamptz not null default now(),
  unique (bin_id, minute_at)
);
comment on table public.readings is
  'Gas, temperature and humidity averaged per minute (the board reads every second and keeps the raw data).';

create table public.fill_measurements (
  id uuid primary key default gen_random_uuid(),
  bin_id uuid not null references public.bins (id) on delete cascade,
  measured_at timestamptz not null,
  fill_pct real not null check (fill_pct between 0 and 100),
  peak_pct real not null check (peak_pct between 0 and 100),
  is_emptied boolean not null default false,
  source public.fill_source not null default 'camera',
  created_at timestamptz not null default now(),
  unique (bin_id, measured_at)
);
comment on table public.fill_measurements is
  'Fill level of a bin from the camera and depth model, one row per photo (copy of data/fill_log.csv on the board).';

create table public.bin_events (
  id uuid primary key default gen_random_uuid(),
  bin_id uuid not null references public.bins (id) on delete cascade,
  device_event_id text not null check (length(device_event_id) between 1 and 64),
  occurred_at timestamptz not null,
  type text not null check (type ~ '^[A-Z][A-Z_]{1,39}$'),
  severity public.bin_event_severity not null,
  title text not null check (length(trim(title)) between 1 and 200),
  message text not null default '' check (length(message) <= 1000),
  "values" jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (bin_id, device_event_id)
);
comment on table public.bin_events is
  'Events a bin raised for the control centre (gas alarm, almost full, emptied, odour, sensor faults).';

create table public.bin_commands (
  id uuid primary key default gen_random_uuid(),
  bin_id uuid not null references public.bins (id) on delete cascade,
  command public.bin_command_kind not null,
  delivered_at timestamptz,
  created_at timestamptz not null default now()
);
comment on table public.bin_commands is
  'Requests from the control panel to a bin; handed to the bin in the answer to its next sync.';

create table public.panel_keys (
  id uuid primary key default gen_random_uuid(),
  name text not null check (length(trim(name)) between 1 and 80),
  key_hash bytea not null unique,
  revoked_at timestamptz,
  created_at timestamptz not null default now()
);
comment on table public.panel_keys is
  'Keys that open the control panel (stored as sha256). Revoke one by setting revoked_at.';

create index readings_bin_minute_idx on public.readings (bin_id, minute_at desc);
create index fill_measurements_bin_measured_idx on public.fill_measurements (bin_id, measured_at desc);
create index bin_events_occurred_idx on public.bin_events (occurred_at desc);
create index bin_events_bin_occurred_idx on public.bin_events (bin_id, occurred_at desc);
create index bin_commands_pending_idx on public.bin_commands (bin_id) where delivered_at is null;

-- ---------------------------------------------------------------------------
-- 3. Triggers
-- ---------------------------------------------------------------------------

-- Same body as the kit's: harmless if the kit schema already created it.
create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

create trigger bins_set_updated_at before update on public.bins
  for each row execute function public.set_updated_at();
create trigger bin_status_set_updated_at before update on public.bin_status
  for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- 4. Tokens
-- ---------------------------------------------------------------------------

create or replace function public.token_hash(token text)
returns bytea
language sql
immutable
set search_path = ''
as $$
  select extensions.digest(coalesce(token, ''), 'sha256');
$$;

-- Creates a bin, or gives an existing one a new token (the old one stops working). Returns the token once.
create or replace function public.register_bin(bin_code text, bin_address text default '')
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
  new_token text := 'tbd_' || encode(extensions.gen_random_bytes(24), 'hex');
begin
  insert into public.bins (code, address, token_hash)
  values (upper(trim(bin_code)), coalesce(trim(bin_address), ''), public.token_hash(new_token))
  on conflict (code) do update
    set token_hash = excluded.token_hash,
        address = case when excluded.address <> '' then excluded.address else public.bins.address end;
  return new_token;
end;
$$;

create or replace function public.create_panel_key(key_name text)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
  new_key text := 'tbp_' || encode(extensions.gen_random_bytes(24), 'hex');
begin
  insert into public.panel_keys (name, key_hash) values (trim(key_name), public.token_hash(new_key));
  return new_key;
end;
$$;

create or replace function public.panel_key_ok(panel_key text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.panel_keys k
    where k.key_hash = public.token_hash(panel_key) and k.revoked_at is null
  );
$$;

-- ---------------------------------------------------------------------------
-- 5. Ingestion (called by the Vercel API for a bin)
-- ---------------------------------------------------------------------------

-- Stores one batch from a bin. Every part is idempotent, so the board can resend a batch after a failure.
-- Returns null for an unknown token; otherwise what was stored and the commands waiting for the bin.
create or replace function public.ingest_batch(device_token text, batch jsonb)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  bin public.bins%rowtype;
  n_readings integer := 0;
  n_fill integer := 0;
  n_events integer := 0;
  pending jsonb;
begin
  select * into bin from public.bins where token_hash = public.token_hash(device_token);
  if not found then
    return null;
  end if;

  insert into public.readings as r (bin_id, minute_at, samples, temperature_c, humidity_pct, tvoc_ppb,
                                    tvoc_max_ppb, eco2_ppm, h2_raw, ethanol_raw)
  select bin.id, to_timestamp((x ->> 'ts')::double precision), (x ->> 'samples')::smallint,
         (x ->> 'temperature_c')::real, (x ->> 'humidity_pct')::real, (x ->> 'tvoc_ppb')::real,
         (x ->> 'tvoc_max_ppb')::integer, (x ->> 'eco2_ppm')::real, (x ->> 'h2_raw')::real,
         (x ->> 'ethanol_raw')::real
  from jsonb_array_elements(coalesce(batch -> 'readings', '[]'::jsonb)) as x
  on conflict (bin_id, minute_at) do update
    set samples = excluded.samples, temperature_c = excluded.temperature_c,
        humidity_pct = excluded.humidity_pct, tvoc_ppb = excluded.tvoc_ppb,
        tvoc_max_ppb = excluded.tvoc_max_ppb, eco2_ppm = excluded.eco2_ppm,
        h2_raw = excluded.h2_raw, ethanol_raw = excluded.ethanol_raw;
  get diagnostics n_readings = row_count;

  insert into public.fill_measurements (bin_id, measured_at, fill_pct, peak_pct, is_emptied, source)
  select bin.id, to_timestamp((x ->> 'ts')::double precision), (x ->> 'fill_pct')::real,
         (x ->> 'peak_pct')::real, coalesce((x ->> 'emptied')::boolean, false),
         coalesce(x ->> 'source', 'camera')::public.fill_source
  from jsonb_array_elements(coalesce(batch -> 'fill', '[]'::jsonb)) as x
  on conflict (bin_id, measured_at) do nothing;
  get diagnostics n_fill = row_count;

  insert into public.bin_events (bin_id, device_event_id, occurred_at, type, severity, title, message, "values")
  select bin.id, x ->> 'uid', to_timestamp((x ->> 'ts')::double precision), x ->> 'type',
         (x ->> 'severity')::public.bin_event_severity, x ->> 'title', coalesce(x ->> 'message', ''),
         coalesce(x -> 'values', '{}'::jsonb)
  from jsonb_array_elements(coalesce(batch -> 'events', '[]'::jsonb)) as x
  on conflict (bin_id, device_event_id) do nothing;
  get diagnostics n_events = row_count;

  if batch ? 'status' then
    insert into public.bin_status (bin_id, state, reported_at)
    values (bin.id, batch -> 'status', now())
    on conflict (bin_id) do update set state = excluded.state, reported_at = excluded.reported_at;
  end if;

  update public.bins
  set last_seen_at = now(),
      address = case when coalesce(batch ->> 'address', '') <> '' then left(batch ->> 'address', 200)
                     else address end
  where id = bin.id;

  -- Commands older than a day are stale (the request no longer makes sense): they are dropped.
  with handed as (
    update public.bin_commands c set delivered_at = now()
    where c.bin_id = bin.id and c.delivered_at is null and c.created_at > now() - interval '1 day'
    returning c.id, c.command, c.created_at
  )
  select coalesce(jsonb_agg(jsonb_build_object('id', h.id, 'command', h.command,
                                               'ts', extract(epoch from h.created_at))
                            order by h.created_at), '[]'::jsonb)
  into pending from handed h;

  return jsonb_build_object('bin', bin.code, 'readings', n_readings, 'fill', n_fill,
                            'events', n_events, 'commands', pending);
end;
$$;

-- ---------------------------------------------------------------------------
-- 6. Control panel (called by the Vercel API for the dashboard)
-- ---------------------------------------------------------------------------

create or replace function public.panel_bins(panel_key text)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
begin
  if not public.panel_key_ok(panel_key) then
    return null;
  end if;
  return coalesce((
    select jsonb_agg(jsonb_build_object(
             'code', b.code,
             'address', b.address,
             'last_seen_at', extract(epoch from b.last_seen_at),
             'online', coalesce(b.last_seen_at > now() - interval '2 minutes', false),
             'fill_pct', s.state #> '{fill,last,fill_pct}',
             'fill_level', s.state #>> '{fill,last,level}',
             'alarm_state', s.state #>> '{alarm,state}',
             'odor_level', s.state #>> '{odor,level}')
           order by b.code)
    from public.bins b
    left join public.bin_status s on s.bin_id = b.id
  ), '[]'::jsonb);
end;
$$;

-- One bin in the shape the dashboard already understands (the board's "snapshot" message), plus the
-- 30-day fill series from the cloud copy and the last 100 events of every bin (the control centre).
create or replace function public.panel_snapshot(panel_key text, bin_code text)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  bin public.bins%rowtype;
  st public.bin_status%rowtype;
  chart jsonb;
  last_fill jsonb;
  recent_events jsonb;
  fill jsonb;
begin
  if not public.panel_key_ok(panel_key) then
    return null;
  end if;
  select * into bin from public.bins where code = upper(trim(bin_code));
  if not found then
    return jsonb_build_object('error', 'not_found');
  end if;
  select * into st from public.bin_status where bin_id = bin.id;

  -- Last measurement of every hour, plus the two measurements around each emptying (keeps the peak).
  with m as (
    select f.measured_at, f.fill_pct, f.peak_pct, f.is_emptied,
           lead(date_trunc('hour', f.measured_at)) over w as next_hour,
           lead(f.is_emptied) over w as next_emptied
    from public.fill_measurements f
    where f.bin_id = bin.id and f.measured_at > now() - interval '30 days'
    window w as (order by f.measured_at)
  )
  select coalesce(jsonb_agg(jsonb_build_array(round(extract(epoch from m.measured_at)), m.fill_pct,
                                              m.peak_pct, m.is_emptied) order by m.measured_at), '[]'::jsonb)
  into chart
  from m
  where m.next_hour is null or m.next_hour <> date_trunc('hour', m.measured_at)
     or m.is_emptied or m.next_emptied;

  select jsonb_build_object('ts', extract(epoch from f.measured_at), 'fill_pct', f.fill_pct,
                            'peak_pct', f.peak_pct, 'source', f.source)
  into last_fill
  from public.fill_measurements f
  where f.bin_id = bin.id
  order by f.measured_at desc
  limit 1;

  select coalesce(jsonb_agg(e.event order by e.occurred_at), '[]'::jsonb)
  into recent_events
  from (
    select ev.occurred_at,
           jsonb_build_object('id', ev.id, 'ts', extract(epoch from ev.occurred_at), 'bin_id', b.code,
                              'address', b.address, 'type', ev.type, 'severity', ev.severity,
                              'title', ev.title, 'message', ev.message, 'values', ev."values") as event
    from public.bin_events ev
    join public.bins b on b.id = ev.bin_id
    order by ev.occurred_at desc
    limit 100
  ) e;

  -- The board computes level, prediction and statistics from its own CSV; the series comes from here.
  if st.state ? 'fill' then
    fill := (st.state -> 'fill') || jsonb_build_object('chart', chart);
  end if;

  return jsonb_build_object(
    'bin', jsonb_build_object('id', bin.code, 'address', bin.address),
    'last_seen_at', extract(epoch from bin.last_seen_at),
    'online', coalesce(bin.last_seen_at > now() - interval '2 minutes', false),
    'reported_at', extract(epoch from st.reported_at),
    'sample', st.state -> 'sample',
    'recent', coalesce(st.state -> 'recent', '[]'::jsonb),
    'alarm', st.state -> 'alarm',
    'odor', st.state -> 'odor',
    'fill', fill,
    'last_fill', last_fill,
    'events', recent_events
  );
end;
$$;

create or replace function public.panel_command(panel_key text, bin_code text, command text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  target uuid;
  queued uuid;
  kind public.bin_command_kind;
begin
  if not public.panel_key_ok(panel_key) then
    return null;
  end if;
  if command is null or command not in ('mark_emptied') then
    return jsonb_build_object('error', 'unknown_command');
  end if;
  kind := command::public.bin_command_kind;
  select b.id into target from public.bins b where b.code = upper(trim(bin_code));
  if target is null then
    return jsonb_build_object('error', 'not_found');
  end if;
  -- One pending request of a kind is enough: pressing the button twice does not queue it twice.
  select c.id into queued from public.bin_commands c
  where c.bin_id = target and c.command = kind and c.delivered_at is null;
  if queued is null then
    insert into public.bin_commands (bin_id, command) values (target, kind) returning id into queued;
  end if;
  return jsonb_build_object('id', queued, 'command', kind);
end;
$$;

-- ---------------------------------------------------------------------------
-- 7. Privileges and Row Level Security
-- ---------------------------------------------------------------------------

alter table public.bins enable row level security;
alter table public.bin_status enable row level security;
alter table public.readings enable row level security;
alter table public.fill_measurements enable row level security;
alter table public.bin_events enable row level security;
alter table public.bin_commands enable row level security;
alter table public.panel_keys enable row level security;

revoke all on public.bins, public.bin_status, public.readings, public.fill_measurements, public.bin_events,
  public.bin_commands, public.panel_keys from anon, authenticated;

revoke all on function public.token_hash(text), public.register_bin(text, text), public.create_panel_key(text),
  public.panel_key_ok(text), public.ingest_batch(text, jsonb), public.panel_bins(text),
  public.panel_snapshot(text, text), public.panel_command(text, text, text) from public, anon, authenticated;

-- The only doors: each one checks its own token or key.
grant execute on function public.ingest_batch(text, jsonb), public.panel_bins(text),
  public.panel_snapshot(text, text), public.panel_command(text, text, text) to anon, authenticated;

-- Next to the IAMAimpresa kit: a 'bins' feature and read access for signed-in users who hold it.
do $$
begin
  if exists (select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
             where n.nspname = 'public' and p.proname = 'has_access')
     and to_regclass('public.features') is not null then
    insert into public.features (key, name, description, default_level, sort_order)
    values ('bins', 'Cassonetti', 'TO-Breath bins: fill level, gas, odour and events.', 'read', 50)
    on conflict (key) do nothing;

    grant select (id, code, address, last_seen_at, created_at, updated_at) on public.bins to authenticated;
    grant select on public.bin_status, public.readings, public.fill_measurements, public.bin_events
      to authenticated;

    create policy "bins: read" on public.bins
      for select to authenticated using (public.has_access('bins', 'read'));
    create policy "bin_status: read" on public.bin_status
      for select to authenticated using (public.has_access('bins', 'read'));
    create policy "readings: read" on public.readings
      for select to authenticated using (public.has_access('bins', 'read'));
    create policy "fill_measurements: read" on public.fill_measurements
      for select to authenticated using (public.has_access('bins', 'read'));
    create policy "bin_events: read" on public.bin_events
      for select to authenticated using (public.has_access('bins', 'read'));
  end if;
end;
$$;
