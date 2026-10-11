-- TO-Breath cloud, part 3 of 5: ingestion from the bins (ingest_batch).
-- Run the five files in order (1 to 5), each in its own SQL Editor query. Each can be run again.

-- ---------------------------------------------------------------------------
-- 3. Ingestion (called by the Vercel API for a bin)
-- ---------------------------------------------------------------------------

-- Stores one batch from a bin. Every part is idempotent, so the board can resend a batch after a
-- failure. Returns null for an unknown token, otherwise what was stored and the waiting commands.
create or replace function public.ingest_batch(device_token text, batch jsonb)
returns jsonb
language sql
volatile
security definer
set search_path = ''
as $$
  with bin as (
    select b.id, b.code from public.bins b where b.token_hash = public.token_hash(device_token)
  ), minute_rows as (
    select distinct on (e.value ->> 'ts') e.value as x
    from jsonb_array_elements(coalesce(batch -> 'readings', '[]'::jsonb)) with ordinality as e (value, n)
    order by e.value ->> 'ts', e.n desc
  ), new_readings as (
    insert into public.readings (bin_id, minute_at, samples, temperature_c, humidity_pct, tvoc_ppb,
                                 tvoc_max_ppb, eco2_ppm, h2_raw, ethanol_raw)
    select bin.id, to_timestamp((r.x ->> 'ts')::double precision), (r.x ->> 'samples')::smallint,
           (r.x ->> 'temperature_c')::real, (r.x ->> 'humidity_pct')::real, (r.x ->> 'tvoc_ppb')::real,
           (r.x ->> 'tvoc_max_ppb')::integer, (r.x ->> 'eco2_ppm')::real, (r.x ->> 'h2_raw')::real,
           (r.x ->> 'ethanol_raw')::real
    from bin, minute_rows r
    on conflict (bin_id, minute_at) do update
      set samples = excluded.samples, temperature_c = excluded.temperature_c,
          humidity_pct = excluded.humidity_pct, tvoc_ppb = excluded.tvoc_ppb,
          tvoc_max_ppb = excluded.tvoc_max_ppb, eco2_ppm = excluded.eco2_ppm,
          h2_raw = excluded.h2_raw, ethanol_raw = excluded.ethanol_raw
    returning 1
  ), new_fill as (
    insert into public.fill_measurements (bin_id, measured_at, fill_pct, peak_pct, is_emptied, source)
    select bin.id, to_timestamp((x ->> 'ts')::double precision), (x ->> 'fill_pct')::real,
           (x ->> 'peak_pct')::real, coalesce((x ->> 'emptied')::boolean, false),
           coalesce(x ->> 'source', 'camera')
    from bin, jsonb_array_elements(coalesce(batch -> 'fill', '[]'::jsonb)) as x
    on conflict (bin_id, measured_at) do nothing
    returning 1
  ), new_events as (
    insert into public.bin_events (bin_id, device_event_id, occurred_at, type, severity, title, message, details)
    select bin.id, x ->> 'uid', to_timestamp((x ->> 'ts')::double precision), x ->> 'type',
           x ->> 'severity', x ->> 'title', coalesce(x ->> 'message', ''), coalesce(x -> 'values', '{}'::jsonb)
    from bin, jsonb_array_elements(coalesce(batch -> 'events', '[]'::jsonb)) as x
    on conflict (bin_id, device_event_id) do nothing
    returning 1
  ), new_status as (
    insert into public.bin_status (bin_id, state, reported_at)
    select bin.id, batch -> 'status', now() from bin where batch -> 'status' is not null
    on conflict (bin_id) do update
      set state = excluded.state, reported_at = excluded.reported_at, updated_at = now()
    returning 1
  ), seen as (
    update public.bins b
    set last_seen_at = now(),
        address = case when coalesce(batch ->> 'address', '') <> '' then left(batch ->> 'address', 200)
                       else b.address end
    from bin
    where b.id = bin.id
    returning 1
  ), handed as (
    -- Commands older than a day are stale (the request no longer makes sense) and are dropped.
    update public.bin_commands c
    set delivered_at = now()
    from bin
    where c.bin_id = bin.id and c.delivered_at is null and c.created_at > now() - interval '1 day'
    returning c.id, c.command, c.created_at
  )
  select case when exists (select 1 from bin) then jsonb_build_object(
    'bin', (select code from bin),
    'readings', (select count(*) from new_readings),
    'fill', (select count(*) from new_fill),
    'events', (select count(*) from new_events),
    'commands', coalesce((select jsonb_agg(jsonb_build_object('id', h.id, 'command', h.command,
                                                              'ts', extract(epoch from h.created_at))
                                           order by h.created_at)
                          from handed h), '[]'::jsonb))
  end
$$;
