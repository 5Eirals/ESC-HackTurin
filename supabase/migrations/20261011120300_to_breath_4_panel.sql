-- TO-Breath cloud, part 4 of 5: control panel (panel_bins, panel_snapshot, panel_command).
-- Run the five files in order (1 to 5), each in its own SQL Editor query. Each can be run again.

-- ---------------------------------------------------------------------------
-- 4. Control panel (called by the Vercel API for the dashboard)
-- ---------------------------------------------------------------------------

create or replace function public.panel_bins(panel_key text)
returns jsonb
language sql
stable
security definer
set search_path = ''
as $$
  select case when public.panel_key_ok(panel_key) then coalesce((
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
  ), '[]'::jsonb) end
$$;

-- One bin in the shape the dashboard already understands (the snapshot message of the board), plus
-- the 30 day fill series from the cloud copy and the last 100 events of every bin (control centre).
-- The series keeps the last measurement of every hour and the two around each emptying (the peak).
create or replace function public.panel_snapshot(panel_key text, bin_code text)
returns jsonb
language sql
stable
security definer
set search_path = ''
as $$
  with target as (
    select b.id, b.code, b.address, b.last_seen_at from public.bins b where b.code = upper(trim(bin_code))
  ), st as (
    select s.state, s.reported_at from public.bin_status s join target t on t.id = s.bin_id
  ), m as (
    select f.measured_at, f.fill_pct, f.peak_pct, f.is_emptied,
           lead(date_trunc('hour', f.measured_at)) over w as next_hour,
           lead(f.is_emptied) over w as next_emptied
    from public.fill_measurements f
    join target t on t.id = f.bin_id
    where f.measured_at > now() - interval '30 days'
    window w as (order by f.measured_at)
  ), chart as (
    select coalesce(jsonb_agg(jsonb_build_array(round(extract(epoch from m.measured_at)), m.fill_pct,
                                                m.peak_pct, m.is_emptied) order by m.measured_at), '[]'::jsonb) as v
    from m
    where m.next_hour is null or m.next_hour <> date_trunc('hour', m.measured_at)
       or m.is_emptied or m.next_emptied
  ), last_fill as (
    select jsonb_build_object('ts', extract(epoch from f.measured_at), 'fill_pct', f.fill_pct,
                              'peak_pct', f.peak_pct, 'source', f.source) as v
    from public.fill_measurements f
    join target t on t.id = f.bin_id
    order by f.measured_at desc
    limit 1
  ), recent_events as (
    select coalesce(jsonb_agg(e.event order by e.occurred_at), '[]'::jsonb) as v
    from (
      select ev.occurred_at,
             jsonb_build_object('id', ev.id, 'ts', extract(epoch from ev.occurred_at), 'bin_id', b.code,
                                'address', b.address, 'type', ev.type, 'severity', ev.severity,
                                'title', ev.title, 'message', ev.message, 'values', ev.details) as event
      from public.bin_events ev
      join public.bins b on b.id = ev.bin_id
      order by ev.occurred_at desc
      limit 100
    ) e
  )
  select case
    when not public.panel_key_ok(panel_key) then null
    when not exists (select 1 from target) then jsonb_build_object('error', 'not_found')
    else jsonb_build_object(
      'bin', (select jsonb_build_object('id', t.code, 'address', t.address) from target t),
      'last_seen_at', (select extract(epoch from t.last_seen_at) from target t),
      'online', (select coalesce(t.last_seen_at > now() - interval '2 minutes', false) from target t),
      'reported_at', (select extract(epoch from s.reported_at) from st s),
      'sample', (select s.state -> 'sample' from st s),
      'recent', coalesce((select s.state -> 'recent' from st s), '[]'::jsonb),
      'alarm', (select s.state -> 'alarm' from st s),
      'odor', (select s.state -> 'odor' from st s),
      'fill', (select (s.state -> 'fill') || jsonb_build_object('chart', (select v from chart))
               from st s where s.state -> 'fill' is not null),
      'last_fill', (select v from last_fill),
      'events', (select v from recent_events))
  end
$$;

-- Pressing the button twice does not queue the same request twice.
create or replace function public.panel_command(panel_key text, bin_code text, command text)
returns jsonb
language sql
volatile
security definer
set search_path = ''
as $$
  with allowed as (
    select public.panel_key_ok(panel_command.panel_key) as ok,
           panel_command.command in ('mark_emptied') as known
  ), target as (
    select b.id from public.bins b, allowed a
    where b.code = upper(trim(panel_command.bin_code)) and a.ok
  ), pending as (
    select c.id from public.bin_commands c join target t on t.id = c.bin_id
    where c.command = panel_command.command and c.delivered_at is null
  ), queued as (
    insert into public.bin_commands (bin_id, command)
    select t.id, panel_command.command from target t, allowed a
    where a.known and not exists (select 1 from pending)
    returning id
  )
  select case
    when not (select ok from allowed) then null
    when not (select known from allowed) then jsonb_build_object('error', 'unknown_command')
    when not exists (select 1 from target) then jsonb_build_object('error', 'not_found')
    else jsonb_build_object('id', coalesce((select id from queued), (select id from pending)),
                            'command', panel_command.command)
  end
$$;
