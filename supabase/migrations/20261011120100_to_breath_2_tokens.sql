-- TO-Breath cloud, part 2 of 5: tokens and keys (register_bin, create_panel_key).
-- Run the five files in order (1 to 5), each in its own SQL Editor query. Each can be run again.

-- ---------------------------------------------------------------------------
-- 2. Tokens and keys (updated_at is set by the functions that change a row)
-- ---------------------------------------------------------------------------

create or replace function public.token_hash(token text)
returns bytea
language sql
immutable
set search_path = ''
as $$
  select extensions.digest(coalesce(token, ''), 'sha256')
$$;

-- Creates a bin, or gives an existing one a new token (the old one stops working). Returns the token once.
create or replace function public.register_bin(bin_code text, bin_address text default '')
returns text
language sql
volatile
security definer
set search_path = ''
as $$
  with new_token as (
    select 'tbd_' || encode(extensions.gen_random_bytes(24), 'hex') as token
  ), saved as (
    insert into public.bins (code, address, token_hash)
    select upper(trim(bin_code)), coalesce(trim(bin_address), ''), public.token_hash(n.token)
    from new_token n
    on conflict (code) do update
      set token_hash = excluded.token_hash,
          address = case when excluded.address <> '' then excluded.address else public.bins.address end,
          updated_at = now()
    returning id
  )
  select n.token from new_token n, saved s
$$;

create or replace function public.create_panel_key(key_name text)
returns text
language sql
volatile
security definer
set search_path = ''
as $$
  with new_key as (
    select 'tbp_' || encode(extensions.gen_random_bytes(24), 'hex') as token
  ), saved as (
    insert into public.panel_keys (name, key_hash)
    select trim(key_name), public.token_hash(k.token) from new_key k
    returning id
  )
  select k.token from new_key k, saved s
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
  )
$$;
