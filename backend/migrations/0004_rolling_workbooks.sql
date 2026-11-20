-- Separate immutable source documents from versioned XLSX objects.
create table public.finance_extractions (
 file_id uuid primary key, sheet_id uuid not null, group_id uuid not null,
 rows jsonb not null check(jsonb_typeof(rows)='array'), created_at timestamptz not null default now(),
 foreign key(file_id,sheet_id,group_id) references public.finance_files(id,sheet_id,group_id)
);
create index finance_extractions_sheet on public.finance_extractions(sheet_id,group_id);
alter table public.finance_extractions enable row level security;
create policy extraction_read on public.finance_extractions for select to authenticated using(public.finance_is_member(group_id));
create policy extraction_insert on public.finance_extractions for insert to authenticated with check(public.finance_is_member(group_id));
revoke all on public.finance_extractions from anon,authenticated;
grant select,insert on public.finance_extractions to authenticated;
-- Preserve previously extracted records if migrating a populated installation.
insert into public.finance_extractions(file_id,sheet_id,group_id,rows)
select f.id,f.sheet_id,f.group_id,coalesce((select jsonb_agg(to_jsonb(t) - 'id' - 'group_id' - 'sheet_id' - 'source_file_id' - 'source_index' - 'created_at' order by source_index) from public.finance_transactions t where t.source_file_id=f.id),'[]'::jsonb)
from public.finance_files f where f.status='processed';

create or replace function public.finance_finish_extraction(p_file uuid,p_rows jsonb) returns void language plpgsql security invoker set search_path='' as $$
declare f public.finance_files;
begin
 select * into f from public.finance_files where id=p_file for update;
 if not found then raise exception 'File unavailable'; end if;
 if f.status='processed' then return; end if;
 if f.status not in ('uploaded','failed') then raise exception 'Upload incomplete'; end if;
 insert into public.finance_transactions(group_id,sheet_id,source_file_id,source_index,reference,date,description,amount_cents,financial_class,category)
 select f.group_id,f.sheet_id,f.id,(r.ordinality-1)::integer,r.value->>'reference',(r.value->>'date')::date,
 r.value->>'description',(r.value->>'amount_cents')::bigint,r.value->>'financial_class',r.value->>'category'
 from jsonb_array_elements(p_rows) with ordinality r;
 insert into public.finance_extractions(file_id,sheet_id,group_id,rows) values(f.id,f.sheet_id,f.group_id,p_rows);
 update public.finance_files set status='processed' where id=f.id;
end $$;

create table public.finance_workbook_objects (
 id uuid primary key, sheet_id uuid not null, group_id uuid not null,
 object_path text not null unique, sha256 text not null check(sha256 ~ '^[a-f0-9]{64}$'),
 size_bytes bigint not null check(size_bytes between 1 and 10485760),
 created_by uuid not null references auth.users(id), created_at timestamptz not null default now(),
 unique(id,sheet_id,group_id), foreign key(sheet_id,group_id) references public.finance_sheets(id,group_id),
 check(object_path=group_id::text || '/' || sheet_id::text || '/versions/' || id::text || '.xlsx')
);
create table public.finance_workbook_revisions (
 id uuid primary key default gen_random_uuid(), sheet_id uuid not null, group_id uuid not null,
 object_id uuid not null, version integer not null check(version>0), parent_id uuid,
 operation text not null check(operation in ('initialize','source','manual','restore','checkpoint')),
 source_file_id uuid, restored_from_id uuid, applied_file_ids uuid[] not null default '{}',
 summary jsonb not null check(jsonb_typeof(summary)='object'),
 created_by uuid not null references auth.users(id), created_at timestamptz not null default now(),
 unique(sheet_id,version),unique(id,sheet_id,group_id),
 foreign key(object_id,sheet_id,group_id) references public.finance_workbook_objects(id,sheet_id,group_id),
 foreign key(parent_id,sheet_id,group_id) references public.finance_workbook_revisions(id,sheet_id,group_id),
 foreign key(restored_from_id,sheet_id,group_id) references public.finance_workbook_revisions(id,sheet_id,group_id),
 foreign key(source_file_id,sheet_id,group_id) references public.finance_files(id,sheet_id,group_id),
 foreign key(sheet_id,group_id) references public.finance_sheets(id,group_id)
);
alter table public.finance_sheets add column current_workbook_id uuid;
alter table public.finance_sheets add constraint finance_current_workbook_fkey foreign key(current_workbook_id,id,group_id) references public.finance_workbook_revisions(id,sheet_id,group_id);
create index finance_objects_sheet on public.finance_workbook_objects(sheet_id,group_id);
create index finance_revisions_object on public.finance_workbook_revisions(object_id,sheet_id,group_id);
create index finance_revisions_parent on public.finance_workbook_revisions(parent_id,sheet_id,group_id);
create index finance_revisions_restore on public.finance_workbook_revisions(restored_from_id,sheet_id,group_id);
create index finance_revisions_source on public.finance_workbook_revisions(source_file_id,sheet_id,group_id);
create index finance_current_workbook on public.finance_sheets(current_workbook_id,id,group_id);
alter table public.finance_workbook_objects enable row level security;
alter table public.finance_workbook_revisions enable row level security;
create policy workbook_object_read on public.finance_workbook_objects for select to authenticated using(public.finance_is_member(group_id));
create policy workbook_object_insert on public.finance_workbook_objects for insert to authenticated with check(public.finance_is_member(group_id) and created_by=(select auth.uid()));
create policy workbook_revision_read on public.finance_workbook_revisions for select to authenticated using(public.finance_is_member(group_id));
create policy workbook_revision_insert on public.finance_workbook_revisions for insert to authenticated with check(public.finance_is_member(group_id) and created_by=(select auth.uid()));
revoke all on public.finance_workbook_objects,public.finance_workbook_revisions from anon,authenticated;
grant select,insert on public.finance_workbook_objects,public.finance_workbook_revisions to authenticated;
insert into storage.buckets(id,name,public,file_size_limit,allowed_mime_types)
values('bvw-finance-workbooks','bvw-finance-workbooks',false,10485760,array['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet']);
create policy bvw_workbook_read on storage.objects for select to authenticated using(
 bucket_id='bvw-finance-workbooks' and exists(select 1 from public.finance_workbook_objects o where o.object_path=name and public.finance_is_member(o.group_id))
);
create policy bvw_workbook_insert on storage.objects for insert to authenticated with check(
 bucket_id='bvw-finance-workbooks' and exists(select 1 from public.finance_workbook_objects o where o.object_path=name and o.created_by=(select auth.uid()) and public.finance_is_member(o.group_id))
);

-- CAS after Storage upload. A stale writer cannot replace an intervening manual
-- edit/restore/source update. Immutable objects survive a retry or conflict.
create function public.finance_commit_workbook(p_sheet uuid,p_expected uuid,p_object uuid,p_operation text,
 p_summary jsonb default '{}',p_source uuid default null,p_restored uuid default null)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare s public.finance_sheets; head public.finance_workbook_revisions;
 target public.finance_workbook_revisions; result public.finance_workbook_revisions;
 applied uuid[] := '{}'; obj public.finance_workbook_objects;
begin
 select * into s from public.finance_sheets where id=p_sheet for update;
 if not found then raise exception 'Sheet unavailable'; end if;
 if s.current_workbook_id is not null then
  select * into strict head from public.finance_workbook_revisions where id=s.current_workbook_id;
  applied:=head.applied_file_ids;
 end if;
 if p_operation='source' and p_source=any(applied) then return jsonb_build_object('revision',to_jsonb(head),'already_applied',true); end if;
 if s.current_workbook_id is distinct from p_expected then
  return jsonb_build_object('conflict',true,'current_id',s.current_workbook_id);
 end if;
 if p_operation not in ('initialize','source','manual','restore','checkpoint') then raise exception 'Invalid operation'; end if;
 if (p_operation='initialize') <> (s.current_workbook_id is null) then raise exception 'Initialization required or already completed'; end if;
 if p_operation='restore' then
  select * into target from public.finance_workbook_revisions where id=p_restored and sheet_id=s.id and group_id=s.group_id;
  if not found then raise exception 'Restore version unavailable'; end if;
  p_object:=target.object_id; applied:=target.applied_file_ids; p_summary:=target.summary;
 elsif p_operation='checkpoint' then
  p_object:=head.object_id; p_summary:=head.summary;
 elsif p_operation='source' then
  if not exists(select 1 from public.finance_extractions where file_id=p_source and sheet_id=s.id and group_id=s.group_id) then raise exception 'Source extraction unavailable'; end if;
  applied:=array_append(applied,p_source);
 end if;
 select * into obj from public.finance_workbook_objects where id=p_object and sheet_id=s.id and group_id=s.group_id;
 if not found then raise exception 'Workbook object unavailable'; end if;
 if not exists(select 1 from storage.objects where bucket_id='bvw-finance-workbooks' and name=obj.object_path) then
  raise exception 'Workbook upload incomplete';
 end if;
 insert into public.finance_workbook_revisions(sheet_id,group_id,object_id,version,parent_id,operation,source_file_id,restored_from_id,applied_file_ids,summary,created_by)
 select s.id,s.group_id,p_object,coalesce((select max(version) from public.finance_workbook_revisions where sheet_id=s.id),0)+1,
 s.current_workbook_id,p_operation,case when p_operation='source' then p_source end,case when p_operation='restore' then p_restored end,applied,p_summary,auth.uid()
 returning * into result;
 update public.finance_sheets set current_workbook_id=result.id where id=s.id;
 return jsonb_build_object('revision',to_jsonb(result));
end $$;
revoke all on function public.finance_commit_workbook(uuid,uuid,uuid,text,jsonb,uuid,uuid) from public,anon;
grant execute on function public.finance_commit_workbook(uuid,uuid,uuid,text,jsonb,uuid,uuid) to authenticated;
notify pgrst,'reload schema';
