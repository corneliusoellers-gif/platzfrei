-- Platzfrei: Bremse für "Schild-Foto lesen" (Edge Function schild-lesen)
-- Einmal im Supabase SQL Editor ausführen. Kann mehrfach laufen.
--
-- Jede Auslesung kostet ein paar Cent bei Anthropic. Damit niemand das
-- missbraucht, zählt die Funktion pro Anschluss (gehashte IP, nicht
-- umkehrbar) und insgesamt. Einträge werden nach 2 Tagen gelöscht.
-- Grenzen: 6 pro Stunde und 20 pro Tag je Anschluss, 300 pro Tag insgesamt.

create table if not exists public.schild_lesen_log (
  id         bigserial primary key,
  schluessel text not null,
  zeit       timestamptz not null default now()
);
create index if not exists schild_lesen_log_schluessel on public.schild_lesen_log (schluessel, zeit);
create index if not exists schild_lesen_log_zeit on public.schild_lesen_log (zeit);
alter table public.schild_lesen_log enable row level security;
revoke all on public.schild_lesen_log from anon, authenticated;

-- Gibt null zurück, wenn gelesen werden darf (und zählt mit),
-- sonst 'anschluss' oder 'gesamt'.
create or replace function public.schild_lesen_bremse(p_schluessel text)
returns text
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  n_stunde int;
  n_tag    int;
  n_alle   int;
begin
  if random() < 0.05 then
    delete from public.schild_lesen_log where zeit < now() - interval '2 days';
  end if;
  select count(*) filter (where zeit > now() - interval '1 hour'), count(*)
    into n_stunde, n_tag
    from public.schild_lesen_log
   where schluessel = p_schluessel and zeit > now() - interval '1 day';
  if n_stunde >= 6 or n_tag >= 20 then return 'anschluss'; end if;
  select count(*) into n_alle from public.schild_lesen_log where zeit > now() - interval '1 day';
  if n_alle >= 300 then return 'gesamt'; end if;
  insert into public.schild_lesen_log (schluessel) values (p_schluessel);
  return null;
end $$;

revoke all on function public.schild_lesen_bremse(text) from public, anon, authenticated;
grant execute on function public.schild_lesen_bremse(text) to service_role;

-- Kontrolle: eine Zeile "schild_lesen_bremse" = alles da.
select proname from pg_proc where proname = 'schild_lesen_bremse';
