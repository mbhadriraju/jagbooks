-- BVW financial consolidation. Apply with Supabase MCP; never on application startup.
create table public.finance_groups (
 id uuid primary key default gen_random_uuid(), name text not null unique,
 created_at timestamptz not null default now()
);
create table public.finance_members (
 group_id uuid not null references public.finance_groups(id),
 user_id uuid not null references auth.users(id) on delete cascade,
 primary key(group_id,user_id)
);
alter table public.finance_members enable row level security;
create policy member_self on public.finance_members for select to authenticated using (user_id=(select auth.uid()));
create function public.finance_is_member(g uuid) returns boolean language sql stable security invoker set search_path='' as $$
 select exists(select 1 from public.finance_members where group_id=g and user_id=(select auth.uid()))
$$;
revoke all on function public.finance_is_member(uuid) from public,anon;
grant execute on function public.finance_is_member(uuid) to authenticated;
create table public.finance_sheets (
 id uuid primary key default gen_random_uuid(), group_id uuid not null references public.finance_groups(id),
 name text not null check(length(name) between 1 and 160), created_at timestamptz not null default now(), unique(id,group_id)
);
create table public.finance_files (
 id uuid primary key, group_id uuid not null references public.finance_groups(id),
 sheet_id uuid not null, uploaded_by uuid not null references auth.users(id),
 filename text not null, mime_type text not null check(mime_type in ('application/pdf','image/png','image/jpeg','image/webp')),
 size_bytes bigint not null check(size_bytes between 1 and 10485760), sha256 text not null check(length(sha256)=64),
 object_path text not null unique, status text not null default 'uploading' check(status in ('uploading','uploaded','processed','failed')),
 created_at timestamptz not null default now(), unique(id,sheet_id,group_id), unique(sheet_id,sha256),
 foreign key(sheet_id,group_id) references public.finance_sheets(id,group_id)
);
create table public.finance_transactions (
 id uuid primary key default gen_random_uuid(), group_id uuid not null, sheet_id uuid not null,
 source_file_id uuid, source_index integer, reference text, date date, description text,
 amount_cents bigint check(amount_cents between -100000000000 and 100000000000),
 financial_class text check(financial_class in ('revenue','expense','asset','liability','cash_flow','purchase_order')),
 category text, review_status text not null default 'draft' check(review_status in ('draft','approved','rejected')),
 created_at timestamptz not null default now(),
 foreign key(sheet_id,group_id) references public.finance_sheets(id,group_id),
 foreign key(source_file_id,sheet_id,group_id) references public.finance_files(id,sheet_id,group_id),
 unique(source_file_id,source_index),
 check(review_status <> 'approved' or (date is not null and amount_cents is not null and financial_class is not null and category is not null and description is not null))
);
create table public.finance_sheet_versions (
 id uuid primary key default gen_random_uuid(), sheet_id uuid not null, group_id uuid not null,
 version integer not null check(version>0), created_by uuid not null references auth.users(id),
 created_at timestamptz not null default now(), snapshot jsonb not null,
 unique(sheet_id,version), foreign key(sheet_id,group_id) references public.finance_sheets(id,group_id)
);
create index finance_members_user on public.finance_members(user_id);
create index finance_sheets_group on public.finance_sheets(group_id);
create index finance_files_sheet on public.finance_files(sheet_id,group_id);
create index finance_transactions_sheet on public.finance_transactions(sheet_id,group_id);
create index finance_transactions_source on public.finance_transactions(source_file_id,sheet_id,group_id);
create index finance_versions_group on public.finance_sheet_versions(group_id);
alter table public.finance_groups enable row level security;
create policy group_read on public.finance_groups for select to authenticated using(public.finance_is_member(id));
alter table public.finance_sheets enable row level security;
create policy member_read on public.finance_sheets for select to authenticated using(public.finance_is_member(group_id));
create policy member_insert on public.finance_sheets for insert to authenticated with check(public.finance_is_member(group_id));
create policy member_update on public.finance_sheets for update to authenticated using(public.finance_is_member(group_id)) with check(public.finance_is_member(group_id));
alter table public.finance_files enable row level security;
create policy member_read on public.finance_files for select to authenticated using(public.finance_is_member(group_id));
create policy member_insert on public.finance_files for insert to authenticated with check(public.finance_is_member(group_id));
create policy member_update on public.finance_files for update to authenticated using(public.finance_is_member(group_id)) with check(public.finance_is_member(group_id));
alter table public.finance_transactions enable row level security;
create policy member_read on public.finance_transactions for select to authenticated using(public.finance_is_member(group_id));
create policy member_insert on public.finance_transactions for insert to authenticated with check(public.finance_is_member(group_id));
create policy member_update on public.finance_transactions for update to authenticated using(public.finance_is_member(group_id)) with check(public.finance_is_member(group_id));
alter table public.finance_sheet_versions enable row level security;
create policy member_read on public.finance_sheet_versions for select to authenticated using(public.finance_is_member(group_id));
create policy member_insert on public.finance_sheet_versions for insert to authenticated with check(public.finance_is_member(group_id));
grant select on public.finance_groups,public.finance_members to authenticated;
grant select,insert,update on public.finance_sheets,public.finance_files,public.finance_transactions to authenticated;
grant select,insert on public.finance_sheet_versions to authenticated;
-- Entire extraction committed atomically; locking prevents duplicate concurrent processing.
create function public.finance_finish_extraction(p_file uuid, p_rows jsonb) returns void language plpgsql security invoker set search_path='' as $$
declare f public.finance_files;
begin
 select * into f from public.finance_files where id=p_file for update;
 if not found then raise exception 'File unavailable'; end if;
 if f.status='processed' then return; end if;
 if f.status not in ('uploaded','failed') then raise exception 'Upload incomplete'; end if;
 insert into public.finance_transactions(group_id,sheet_id,source_file_id,source_index,reference,date,description,amount_cents,financial_class,category)
 select f.group_id,f.sheet_id,f.id,(r.ordinality-1)::integer,r.value->>'reference',
 (r.value->>'date')::date,r.value->>'description',(r.value->>'amount_cents')::bigint,r.value->>'financial_class',r.value->>'category'
 from jsonb_array_elements(p_rows) with ordinality r;
 update public.finance_files set status='processed' where id=f.id;
end $$;
create function public.finance_snapshot(p_sheet uuid) returns public.finance_sheet_versions language plpgsql security invoker set search_path='' as $$
declare s public.finance_sheets; v public.finance_sheet_versions;
begin
 select * into s from public.finance_sheets where id=p_sheet for update;
 if not found then raise exception 'Sheet unavailable'; end if;
 insert into public.finance_sheet_versions(sheet_id,group_id,version,created_by,snapshot)
 select s.id,s.group_id,coalesce((select max(version) from public.finance_sheet_versions where sheet_id=s.id),0)+1,auth.uid(),
 coalesce((select jsonb_agg(to_jsonb(t) order by t.date,t.id) from public.finance_transactions t where t.sheet_id=s.id and t.review_status='approved'),'[]'::jsonb)
 returning * into v;
 return v;
end $$;
revoke all on function public.finance_finish_extraction(uuid,jsonb),public.finance_snapshot(uuid) from public,anon;
grant execute on function public.finance_finish_extraction(uuid,jsonb),public.finance_snapshot(uuid) to authenticated;
insert into public.finance_groups(name) values ('BVW Debate Financial Consolidation');
insert into storage.buckets(id,name,public,file_size_limit,allowed_mime_types)
values ('bvw-finance-files','bvw-finance-files',false,10485760,array['application/pdf','image/png','image/jpeg','image/webp']);
create policy bvw_file_read on storage.objects for select to authenticated using (
 bucket_id='bvw-finance-files' and exists(select 1 from public.finance_files f where f.object_path=name and public.finance_is_member(f.group_id))
);
create policy bvw_file_upload on storage.objects for insert to authenticated with check (
 bucket_id='bvw-finance-files' and exists(select 1 from public.finance_files f where f.object_path=name and f.uploaded_by=(select auth.uid()) and f.status='uploading' and public.finance_is_member(f.group_id))
);
notify pgrst, 'reload schema';
