-- TO-Breath cloud, part 5 of 5: privileges and Row Level Security.
-- Run the five files in order (1 to 5), each in its own SQL Editor query. Each can be run again.

-- ---------------------------------------------------------------------------
-- 5. Privileges and Row Level Security
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
