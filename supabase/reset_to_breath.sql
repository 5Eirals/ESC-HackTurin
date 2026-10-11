-- TO-Breath: removes every TO-Breath table and function, with all their data, so that the migration in
-- supabase/migrations can be run on a clean slate (for example after a first attempt that stopped half
-- way). It does not touch anything else in the database, including the IAMAimpresa kit schema.
-- Afterwards register the bins and create the control panel key again: the old tokens stop working.

drop table if exists public.bin_commands cascade;
drop table if exists public.bin_events cascade;
drop table if exists public.fill_measurements cascade;
drop table if exists public.readings cascade;
drop table if exists public.bin_status cascade;
drop table if exists public.panel_keys cascade;
drop table if exists public.bins cascade;

drop function if exists public.panel_command(text, text, text);
drop function if exists public.panel_snapshot(text, text);
drop function if exists public.panel_bins(text);
drop function if exists public.ingest_batch(text, jsonb);
drop function if exists public.panel_key_ok(text);
drop function if exists public.create_panel_key(text);
drop function if exists public.register_bin(text, text);
drop function if exists public.token_hash(text);

-- Types created by the first version of the migration (the current one does not use them).
drop type if exists public.bin_command_kind;
drop type if exists public.fill_source;
drop type if exists public.bin_event_severity;
