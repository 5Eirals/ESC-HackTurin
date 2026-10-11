-- TO-Breath cloud, part 1 of 5: extension and tables.
-- Run the five files in order (1 to 5), each in its own SQL Editor query. Each can be run again.
--
-- TO-Breath cloud: what the bins (Arduino UNO Q) send and what the control panel reads.
--
-- Nobody touches these tables directly. Bins and the control panel go through the Vercel API, which
-- calls the security definer functions below with the publishable key:
--   ingest_batch(device_token, batch)       a bin sends readings, fill measurements, events, status
--   panel_bins(panel_key)                   the control panel lists the bins
--   panel_snapshot(panel_key, bin_code)     the control panel reads one bin
--   panel_command(panel_key, bin_code, cmd) the control panel asks a bin to act (mark_emptied)
-- Tokens are random, shown once and stored only as sha256. Admins create them from the SQL Editor
-- with public.register_bin(code, address) and public.create_panel_key(name), see the README.
--
-- Written to survive any SQL client: every function is one SQL statement (no semicolons inside a
-- body), no DO blocks, no enum types, and every statement can be run again without errors.
-- Signed-in users of the IAMAimpresa kit can also read the tables: see supabase/optional/kit_access.sql

create extension if not exists pgcrypto with schema extensions;

-- ---------------------------------------------------------------------------
-- 1. Tables
-- ---------------------------------------------------------------------------

create table if not exists public.bins (
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

create table if not exists public.bin_status (
  bin_id uuid primary key references public.bins (id) on delete cascade,
  state jsonb not null default '{}'::jsonb,
  reported_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
comment on table public.bin_status is
  'Latest state reported by each bin (gas alarm, odour, fill prediction and statistics, last readings), replaced at every sync.';

create table if not exists public.readings (
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

create table if not exists public.fill_measurements (
  id uuid primary key default gen_random_uuid(),
  bin_id uuid not null references public.bins (id) on delete cascade,
  measured_at timestamptz not null,
  fill_pct real not null check (fill_pct between 0 and 100),
  peak_pct real not null check (peak_pct between 0 and 100),
  is_emptied boolean not null default false,
  source text not null default 'camera' check (source in ('camera', 'sim')),
  created_at timestamptz not null default now(),
  unique (bin_id, measured_at)
);
comment on table public.fill_measurements is
  'Fill level of a bin from the camera and depth model, one row per photo (copy of data/fill_log.csv on the board).';

create table if not exists public.bin_events (
  id uuid primary key default gen_random_uuid(),
  bin_id uuid not null references public.bins (id) on delete cascade,
  device_event_id text not null check (length(device_event_id) between 1 and 64),
  occurred_at timestamptz not null,
  type text not null check (type ~ '^[A-Z][A-Z_]{1,39}$'),
  severity text not null check (severity in ('info', 'warning', 'critical')),
  title text not null check (length(trim(title)) between 1 and 200),
  message text not null default '' check (length(message) <= 1000),
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (bin_id, device_event_id)
);
comment on table public.bin_events is
  'Events a bin raised for the control centre (gas alarm, almost full, emptied, odour, sensor faults).';

create table if not exists public.bin_commands (
  id uuid primary key default gen_random_uuid(),
  bin_id uuid not null references public.bins (id) on delete cascade,
  command text not null check (command in ('mark_emptied')),
  delivered_at timestamptz,
  created_at timestamptz not null default now()
);
comment on table public.bin_commands is
  'Requests from the control panel to a bin, handed to the bin in the answer to its next sync.';

create table if not exists public.panel_keys (
  id uuid primary key default gen_random_uuid(),
  name text not null check (length(trim(name)) between 1 and 80),
  key_hash bytea not null unique,
  revoked_at timestamptz,
  created_at timestamptz not null default now()
);
comment on table public.panel_keys is
  'Keys that open the control panel (stored as sha256). Revoke one by setting revoked_at.';

create index if not exists readings_bin_minute_idx on public.readings (bin_id, minute_at desc);
create index if not exists fill_measurements_bin_measured_idx on public.fill_measurements (bin_id, measured_at desc);
create index if not exists bin_events_occurred_idx on public.bin_events (occurred_at desc);
create index if not exists bin_events_bin_occurred_idx on public.bin_events (bin_id, occurred_at desc);
create index if not exists bin_commands_pending_idx on public.bin_commands (bin_id) where delivered_at is null;
