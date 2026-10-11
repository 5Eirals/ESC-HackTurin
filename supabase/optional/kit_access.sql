-- Optional, only for a database that also has the IAMAimpresa kit schema (public.has_access and
-- public.features): signed-in users who hold the bins feature can read the TO-Breath tables directly.
-- Run it after the TO-Breath migration. It can be run again without errors.

insert into public.features (key, name, description, default_level, sort_order)
values ('bins', 'Cassonetti', 'TO-Breath bins: fill level, gas, odour and events.', 'read', 50)
on conflict (key) do nothing;

grant select (id, code, address, last_seen_at, created_at, updated_at) on public.bins to authenticated;
grant select on public.bin_status, public.readings, public.fill_measurements, public.bin_events to authenticated;

drop policy if exists "bins: read" on public.bins;
create policy "bins: read" on public.bins
  for select to authenticated using (public.has_access('bins', 'read'));

drop policy if exists "bin_status: read" on public.bin_status;
create policy "bin_status: read" on public.bin_status
  for select to authenticated using (public.has_access('bins', 'read'));

drop policy if exists "readings: read" on public.readings;
create policy "readings: read" on public.readings
  for select to authenticated using (public.has_access('bins', 'read'));

drop policy if exists "fill_measurements: read" on public.fill_measurements;
create policy "fill_measurements: read" on public.fill_measurements
  for select to authenticated using (public.has_access('bins', 'read'));

drop policy if exists "bin_events: read" on public.bin_events;
create policy "bin_events: read" on public.bin_events
  for select to authenticated using (public.has_access('bins', 'read'));
