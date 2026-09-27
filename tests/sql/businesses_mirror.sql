-- Mirror of the live `businesses` table (owned by the SMS bot), for local Postgres tests only.
create table if not exists public.businesses (
  twilio_number text primary key,
  business_name text not null,
  service_area text not null,
  booking_link text not null,
  price_bands text not null,
  contract_type text default 'seasonal',
  active boolean not null default true,
  created_at timestamptz not null default now(),
  alert_phone text
);
