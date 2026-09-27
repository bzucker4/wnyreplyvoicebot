-- Voice dispatch bot tables for the wny-snow-lead-bot Supabase project.
--
-- Reads the existing `businesses` table (owned by the SMS bot) as-is and never
-- alters it. Everything the voice bot writes lives in voice_* tables, keyed to
-- the business by its Twilio number.
--
-- RLS is enabled with no policies: the anon and authenticated keys can't read
-- call transcripts or caller details. The voice service connects with the
-- database role, which bypasses RLS.

-- Optional per-business voice settings. With no row, an active business takes
-- calls using the service defaults.
create table if not exists public.voice_settings (
  twilio_number text primary key references public.businesses (twilio_number) on delete cascade,
  enabled boolean not null default true,
  on_call_phone text,           -- Priority One transfers; falls back to businesses.alert_phone
  callback_timeframe text,      -- "[TIMEFRAME]" in the prompt pack, e.g. '30 minutes'
  owner_names text,             -- comma-separated names callers may ask for
  service_towns text,           -- comma-separated; enables strict service-area checks
  service_zip_prefixes text,    -- comma-separated, e.g. '140,141,142'
  updated_at timestamptz not null default now()
);

-- One row per phone call: Claude conversation history and call state.
create table if not exists public.voice_calls (
  call_sid text primary key,              -- Twilio CallSid
  to_number text not null,                -- the business's Twilio number (To)
  from_number text not null,              -- caller (From)
  history jsonb not null default '[]'::jsonb,
  no_input_count integer not null default 0,
  status text not null default 'active',  -- active | completed | transferred | dropped
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists ix_voice_calls_to_number on public.voice_calls (to_number);

-- Existing customers per business, so the bot can recognize them.
create table if not exists public.voice_customers (
  id bigint generated always as identity primary key,
  business_number text not null references public.businesses (twilio_number) on delete cascade,
  name text not null,
  phone text,
  address text not null,
  address_key text not null,   -- normalized address for matching
  town text,
  zip_code text,
  property_type text,
  plan text,
  notes text,
  created_at timestamptz not null default now()
);
create index if not exists voice_customers_phone_idx on public.voice_customers (business_number, phone);
create index if not exists voice_customers_address_idx on public.voice_customers (business_number, address_key);

-- Leads, customer requests, complaints and emergencies captured on calls.
create table if not exists public.voice_tickets (
  id bigint generated always as identity primary key,
  business_number text not null references public.businesses (twilio_number) on delete cascade,
  call_sid text,
  call_type text not null check (call_type in ('new_lead', 'existing_customer', 'complaint', 'emergency')),
  priority text not null check (priority in ('normal', 'priority_one')),
  status text not null check (status in ('new', 'contacted', 'scheduled', 'closed')),
  customer_id bigint references public.voice_customers (id) on delete set null,
  caller_phone text,
  caller_name text,
  callback_number text,
  preferred_contact text,
  property_type text,
  service_type text,
  contract_type text,
  address text,
  town text,
  zip_code text,
  access_notes text,
  details text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists voice_tickets_open_idx on public.voice_tickets (business_number, status);

alter table public.voice_settings enable row level security;
alter table public.voice_calls enable row level security;
alter table public.voice_customers enable row level security;
alter table public.voice_tickets enable row level security;
