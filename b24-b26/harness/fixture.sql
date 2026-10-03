-- =====================================================================
-- B24/B26 — FIXTURE DE TEST (PAS LE SCHÉMA NAATAL+)
-- ---------------------------------------------------------------------
-- Le dépôt ne contient aucun schéma ni aucune fonction NAATAL+ (dépôt vide
-- au 2026-10-03). Ce fichier crée une maquette MINIMALE, préfixée "fx_",
-- uniquement pour observer le comportement réel du moteur PostgreSQL 16
-- (verrous, horloges, isolation, deadlocks, crash) dans les scénarios
-- B24/B26.
--
-- Ce n'est PAS une proposition d'architecture. Les résultats obtenus sur
-- cette maquette ne valent PAS résultat pour NAATAL+ : ils décrivent ce que
-- fait PostgreSQL selon la stratégie d'implémentation choisie. Chaque
-- variante (v_*) correspond à un choix d'implémentation plausible ; le
-- schéma réel devra être passé dans le même harnais.
-- =====================================================================

drop schema if exists fx cascade;
create schema fx;
set search_path = fx;

create table drivers (
  id        int primary key,
  eligible  boolean not null default true
);

create table rides (
  id                  int primary key,
  status              text not null default 'dispatching'
                      check (status in ('dispatching','assigned','cancelled')),
  version             int  not null default 0,
  dispatch_generation int  not null default 1,
  driver_id           int  references drivers(id)
);

create table offers (
  id                  serial primary key,
  ride_id             int  not null references rides(id),
  driver_id           int  not null references drivers(id),
  dispatch_generation int  not null,
  status              text not null default 'pending'
                      check (status in ('pending','accepted','lost','expired','superseded')),
  expires_at          timestamptz not null,
  decided_at          timestamptz
);
create index on offers(ride_id);

-- Journal des transitions de course : sert à compter les doubles transitions.
create table transitions (
  id          bigserial primary key,
  ride_id     int not null,
  from_status text,
  to_status   text,
  driver_id   int,
  request_id  text,
  txid        bigint default txid_current(),
  at_clock    timestamptz default clock_timestamp()
);

-- Effet de bord simulé (ex. notification push) : sert à compter les doublons.
create table side_effects (
  id          bigserial primary key,
  request_id  text,
  ride_id     int,
  kind        text,
  at_clock    timestamptz default clock_timestamp()
);

create table request_dedup (
  request_id  text primary key,
  operation   text not null,
  status      text not null check (status in ('pending','done')),
  result      jsonb,
  created_at  timestamptz default clock_timestamp(),
  finished_at timestamptz
);

-- ---------------------------------------------------------------------
-- Variante v_lock : verrou pessimiste sur la course (SELECT ... FOR UPDATE),
-- horloge paramétrable : 'now' | 'statement' | 'clock'.
-- Retourne un texte de résultat ; lève une exception métier (P0001) si refus.
-- ---------------------------------------------------------------------
create or replace function accept_v_lock(
  p_offer int, p_driver int, p_request text, p_clock text default 'now',
  p_check_eligibility text default 'read')   -- 'read' | 'share' | 'none'
returns text language plpgsql as $$
declare
  r rides%rowtype; o offers%rowtype; d drivers%rowtype; t timestamptz;
begin
  select * into r from rides
   where id = (select ride_id from offers where id = p_offer)
   for update;

  t := case p_clock when 'now' then now()
                    when 'statement' then statement_timestamp()
                    else clock_timestamp() end;

  select * into o from offers where id = p_offer for update;
  if o.driver_id <> p_driver then raise exception 'OFFER_NOT_YOURS'; end if;
  if r.status <> 'dispatching' then raise exception 'RIDE_NOT_DISPATCHING:%', r.status; end if;
  if o.status <> 'pending' then raise exception 'OFFER_NOT_PENDING:%', o.status; end if;
  if o.dispatch_generation <> r.dispatch_generation then raise exception 'STALE_GENERATION'; end if;
  if o.expires_at <= t then raise exception 'OFFER_EXPIRED'; end if;

  if p_check_eligibility = 'share' then
    select * into d from drivers where id = p_driver for share;
  elsif p_check_eligibility = 'read' then
    select * into d from drivers where id = p_driver;
  end if;
  if p_check_eligibility <> 'none' and not d.eligible then
    raise exception 'DRIVER_NOT_ELIGIBLE';
  end if;

  update rides set status = 'assigned', driver_id = p_driver, version = version + 1
   where id = r.id;
  update offers set status = 'accepted', decided_at = clock_timestamp() where id = p_offer;
  update offers set status = 'lost', decided_at = clock_timestamp()
   where ride_id = r.id and id <> p_offer and status = 'pending';
  insert into transitions(ride_id, from_status, to_status, driver_id, request_id)
       values (r.id, r.status, 'assigned', p_driver, p_request);
  insert into side_effects(request_id, ride_id, kind) values (p_request, r.id, 'notify_rider');
  return 'ACCEPTED';
end $$;

-- ---------------------------------------------------------------------
-- Variante v_cond : pas de FOR UPDATE, UPDATE conditionnel (compare-and-set)
-- sur status + version lus auparavant.
-- ---------------------------------------------------------------------
create or replace function accept_v_cond(p_offer int, p_driver int, p_request text)
returns text language plpgsql as $$
declare r rides%rowtype; o offers%rowtype; n int;
begin
  select * into o from offers where id = p_offer;
  select * into r from rides where id = o.ride_id;
  if r.status <> 'dispatching' or o.status <> 'pending'
     or o.dispatch_generation <> r.dispatch_generation or o.expires_at <= now() then
    raise exception 'PRECHECK_FAILED';
  end if;
  update rides set status = 'assigned', driver_id = p_driver, version = version + 1
   where id = r.id and status = 'dispatching' and version = r.version;
  get diagnostics n = row_count;
  if n = 0 then raise exception 'CAS_LOST'; end if;
  update offers set status = 'accepted', decided_at = clock_timestamp() where id = p_offer;
  update offers set status = 'lost', decided_at = clock_timestamp()
   where ride_id = r.id and id <> p_offer and status = 'pending';
  insert into transitions(ride_id, from_status, to_status, driver_id, request_id)
       values (r.id, r.status, 'assigned', p_driver, p_request);
  insert into side_effects(request_id, ride_id, kind) values (p_request, r.id, 'notify_rider');
  return 'ACCEPTED';
end $$;

-- ---------------------------------------------------------------------
-- Variante v_naive : lecture puis écriture SANS verrou ni condition
-- (anti-modèle, sert de témoin négatif pour vérifier que le harnais détecte
-- réellement une double affectation).
-- ---------------------------------------------------------------------
create or replace function accept_v_naive(p_offer int, p_driver int, p_request text)
returns text language plpgsql as $$
declare r rides%rowtype; o offers%rowtype;
begin
  select * into o from offers where id = p_offer;
  select * into r from rides where id = o.ride_id;
  if r.status <> 'dispatching' or o.status <> 'pending' then
    raise exception 'PRECHECK_FAILED';
  end if;
  perform pg_sleep(0.05);  -- élargit la fenêtre de course (témoin)
  update rides set status = 'assigned', driver_id = p_driver, version = version + 1 where id = r.id;
  update offers set status = 'accepted', decided_at = clock_timestamp() where id = p_offer;
  insert into transitions(ride_id, from_status, to_status, driver_id, request_id)
       values (r.id, r.status, 'assigned', p_driver, p_request);
  insert into side_effects(request_id, ride_id, kind) values (p_request, r.id, 'notify_rider');
  return 'ACCEPTED';
end $$;

-- ---------------------------------------------------------------------
-- Redispatch : verrou course, génération + 1, offres pending -> superseded,
-- création de nouvelles offres. p_offer_order : 'ride_first' (ordre prévu
-- course -> offres) ou 'offers_first' (ordre inverse, pour 01-I2-B).
-- ---------------------------------------------------------------------
create or replace function redispatch(p_ride int, p_new_drivers int[],
                                      p_order text default 'ride_first')
returns text language plpgsql as $$
declare r rides%rowtype; dd int;
begin
  if p_order = 'offers_first' then
    update offers set status = 'superseded', decided_at = clock_timestamp()
     where ride_id = p_ride and status = 'pending';
    perform pg_sleep(0.05);
    select * into r from rides where id = p_ride for update;
  else
    select * into r from rides where id = p_ride for update;
    update offers set status = 'superseded', decided_at = clock_timestamp()
     where ride_id = p_ride and status = 'pending';
  end if;
  if r.status <> 'dispatching' then raise exception 'RIDE_NOT_DISPATCHING:%', r.status; end if;
  update rides set dispatch_generation = dispatch_generation + 1, version = version + 1
   where id = p_ride;
  foreach dd in array p_new_drivers loop
    insert into offers(ride_id, driver_id, dispatch_generation, expires_at)
         values (p_ride, dd, r.dispatch_generation + 1, clock_timestamp() + interval '30 s');
  end loop;
  return 'REDISPATCHED';
end $$;

-- Contrôle d'invariants 01-H, renvoie les lignes en violation.
create or replace view invariant_violations as
  select r.id as ride_id, 'assigned_without_exactly_one_accepted_offer' as violation
    from rides r
   where r.status = 'assigned'
     and (select count(*) from offers o where o.ride_id = r.id and o.status = 'accepted') <> 1
  union all
  select r.id, 'accepted_offer_driver_differs_from_ride_driver'
    from rides r join offers o on o.ride_id = r.id and o.status = 'accepted'
   where r.driver_id is distinct from o.driver_id
  union all
  select r.id, 'accepted_offer_on_non_assigned_ride'
    from rides r join offers o on o.ride_id = r.id and o.status = 'accepted'
   where r.status <> 'assigned'
  union all
  select r.id, 'pending_offer_on_assigned_ride'
    from rides r join offers o on o.ride_id = r.id and o.status = 'pending'
   where r.status = 'assigned'
  union all
  select r.id, 'more_than_one_assign_transition'
    from rides r
   where (select count(*) from transitions t where t.ride_id = r.id and t.to_status = 'assigned') > 1
  union all
  select r.id, 'accepted_offer_from_stale_generation'
    from rides r join offers o on o.ride_id = r.id and o.status = 'accepted'
   where o.dispatch_generation <> r.dispatch_generation
;

-- ---------------------------------------------------------------------
-- 01-G2 : modification d'éligibilité.
--  disable_flag  : bascule simple du drapeau.
--  disable_guard : refuse si le chauffeur a une course active (lecture puis
--                  écriture -> sujet au write skew sous READ COMMITTED /
--                  REPEATABLE READ).
-- ---------------------------------------------------------------------
create or replace function disable_flag(p_driver int) returns text
language plpgsql as $$
begin
  update drivers set eligible = false where id = p_driver;
  return 'DISABLED';
end $$;

create or replace function disable_guard(p_driver int) returns text
language plpgsql as $$
begin
  if exists (select 1 from rides where driver_id = p_driver and status = 'assigned') then
    raise exception 'DRIVER_HAS_ACTIVE_RIDE';
  end if;
  perform pg_sleep(0.02);
  update drivers set eligible = false where id = p_driver;
  return 'DISABLED';
end $$;

-- ---------------------------------------------------------------------
-- Idempotence (01-C / 01-D / 01-D2) — deux stratégies de maquette.
-- dedup_atomic   : request_dedup écrit DANS la même transaction que l'effet
--                  métier (ON CONFLICT DO NOTHING ; si déjà présent on renvoie
--                  le résultat mémorisé).
-- dedup_begin / dedup_finish : stratégie en 3 transactions
--                  (1) réserver 'pending', (2) métier, (3) passer à 'done'.
-- ---------------------------------------------------------------------
create or replace function accept_dedup_atomic(p_offer int, p_driver int, p_request text)
returns text language plpgsql as $$
declare n int; prev request_dedup%rowtype; res text;
begin
  insert into request_dedup(request_id, operation, status)
       values (p_request, 'accept_offer', 'pending')
  on conflict (request_id) do nothing;
  get diagnostics n = row_count;
  if n = 0 then
    select * into prev from request_dedup where request_id = p_request;
    return 'REPLAY:' || prev.status || ':' || coalesce(prev.result->>'r', 'null');
  end if;
  res := accept_v_lock(p_offer, p_driver, p_request);
  update request_dedup set status = 'done', result = jsonb_build_object('r', res),
         finished_at = clock_timestamp()
   where request_id = p_request;
  return res;
end $$;

create or replace function dedup_begin(p_request text) returns text
language plpgsql as $$
declare n int; prev request_dedup%rowtype;
begin
  insert into request_dedup(request_id, operation, status)
       values (p_request, 'accept_offer', 'pending')
  on conflict (request_id) do nothing;
  get diagnostics n = row_count;
  if n = 1 then return 'NEW'; end if;
  select * into prev from request_dedup where request_id = p_request;
  return 'EXISTS:' || prev.status;
end $$;

create or replace function dedup_finish(p_request text, p_result text) returns text
language plpgsql as $$
begin
  update request_dedup set status = 'done', result = jsonb_build_object('r', p_result),
         finished_at = clock_timestamp()
   where request_id = p_request;
  return 'FINISHED';
end $$;
