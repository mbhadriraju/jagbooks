-- Transactional DB/RLS test. Storage metadata below is SYNTHETIC and rolled back.
-- This exercises commit bookkeeping, NOT actual Storage byte upload/download.
begin;
select set_config('finance.test_user',gen_random_uuid()::text,true);
select set_config('finance.test_outsider',gen_random_uuid()::text,true);
select set_config('finance.test_group',(select id::text from public.finance_groups where name='BVW Debate Financial Consolidation'),true);
select set_config('finance.test_sheet',gen_random_uuid()::text,true);
select set_config('finance.test_file',gen_random_uuid()::text,true);
insert into auth.users(id) values(current_setting('finance.test_user')::uuid),(current_setting('finance.test_outsider')::uuid);
insert into public.finance_members(group_id,user_id) values(current_setting('finance.test_group')::uuid,current_setting('finance.test_user')::uuid);
select set_config('request.jwt.claims',json_build_object('sub',current_setting('finance.test_user'),'role','authenticated')::text,true);
set local role authenticated;
insert into public.finance_sheets(id,group_id,name) values(current_setting('finance.test_sheet')::uuid,current_setting('finance.test_group')::uuid,'Rollback workbook verification');
insert into public.finance_files(id,group_id,sheet_id,uploaded_by,filename,mime_type,size_bytes,sha256,object_path)
values(current_setting('finance.test_file')::uuid,current_setting('finance.test_group')::uuid,current_setting('finance.test_sheet')::uuid,current_setting('finance.test_user')::uuid,'verify.pdf','application/pdf',10,repeat('a',64),current_setting('finance.test_group')||'/'||current_setting('finance.test_file')||'/source');
update public.finance_files set status='uploaded' where id=current_setting('finance.test_file')::uuid;
select public.finance_finish_extraction(current_setting('finance.test_file')::uuid,'[{"date":"2026-11-16","description":"Dues","amount_cents":1250,"financial_class":"revenue","category":"student_dues"}]');
do $$
declare o uuid:=gen_random_uuid(); sheet uuid:=current_setting('finance.test_sheet')::uuid;
 g uuid:=current_setting('finance.test_group')::uuid; source uuid:=current_setting('finance.test_file')::uuid;
 path text; first_id uuid; source_id uuid; manual_id uuid; restored_id uuid; result jsonb; bad_obj uuid:=gen_random_uuid();
begin
 path:=g::text || '/' || sheet::text || '/versions/' || o::text || '.xlsx';
 insert into public.finance_workbook_objects(id,sheet_id,group_id,object_path,sha256,size_bytes,created_by)
 values(o,sheet,g,path,repeat('a',64),10,auth.uid());
 -- Missing bytes/metadata cannot publish a current workbook.
 begin
  perform public.finance_commit_workbook(sheet,null,o,'initialize');
  raise exception 'Incomplete upload published';
 exception when raise_exception then
  if sqlerrm<>'Workbook upload incomplete' then raise; end if;
 end;
 -- Synthetic object record exists only inside this rolled-back transaction.
 insert into storage.objects(bucket_id,name,metadata) values('bvw-finance-workbooks',path,'{"size":10}');
 result:=public.finance_commit_workbook(sheet,null,o,'initialize','{"marker":"initial"}');
 first_id:=(result->'revision'->>'id')::uuid;
 if (select current_workbook_id from public.finance_sheets where id=sheet)<>first_id then raise exception 'Initial pointer'; end if;
 result:=public.finance_commit_workbook(sheet,first_id,o,'source','{"marker":"source"}',source);
 source_id:=(result->'revision'->>'id')::uuid;
 if (result->'revision'->>'version')::int<>2 then raise exception 'Source version'; end if;
 result:=public.finance_commit_workbook(sheet,first_id,o,'source','{}',source);
 if not (result->>'already_applied')::boolean then raise exception 'Duplicate source posting'; end if;
 result:=public.finance_commit_workbook(sheet,source_id,o,'manual','{"marker":"manual"}');
 manual_id:=(result->'revision'->>'id')::uuid;
 if not source=any((select applied_file_ids from public.finance_workbook_revisions where id=manual_id)::uuid[]) then raise exception 'Manual edit forgot source identity'; end if;
 result:=public.finance_commit_workbook(sheet,source_id,null,'restore','{}',null,first_id);
 if not (result->>'conflict')::boolean then raise exception 'Stale restore overwrote latest'; end if;
 result:=public.finance_commit_workbook(sheet,manual_id,null,'restore','{}',null,first_id);
 restored_id:=(result->'revision'->>'id')::uuid;
 if result->'revision'->'summary'->>'marker'<>'initial' then raise exception 'Restore did not replace summary'; end if;
 if (result->'revision'->>'version')::int<>4 then raise exception 'Restore version not monotonic'; end if;
 if cardinality((select applied_file_ids from public.finance_workbook_revisions where id=restored_id))<>0 then raise exception 'Restore lineage not restored'; end if;
 result:=public.finance_commit_workbook(sheet,restored_id,o,'source','{"marker":"reapplied"}',source);
 if (result->'revision'->>'version')::int<>5 then raise exception 'Restored source cannot reapply'; end if;
 if (select count(*) from public.finance_workbook_revisions where sheet_id=sheet)<>5 then raise exception 'History lost'; end if;
 begin
  update public.finance_workbook_revisions set summary='{}' where sheet_id=sheet;
  raise exception 'History mutable';
 exception when insufficient_privilege then null;
 end;
 begin
  delete from public.finance_workbook_objects where id=o;
  raise exception 'Workbook objects mutable';
 exception when insufficient_privilege then null;
 end;
end $$;
select set_config('request.jwt.claims',json_build_object('sub',current_setting('finance.test_outsider'),'role','authenticated')::text,true);
do $$
begin
 if exists(select 1 from public.finance_workbook_revisions where sheet_id=current_setting('finance.test_sheet')::uuid) then raise exception 'History leaked'; end if;
 if exists(select 1 from public.finance_workbook_objects where sheet_id=current_setting('finance.test_sheet')::uuid) then raise exception 'Workbook metadata leaked'; end if;
 if exists(select 1 from storage.objects where bucket_id='bvw-finance-workbooks' and name like current_setting('finance.test_group')||'/%') then raise exception 'Workbook storage metadata leaked'; end if;
 if exists(select 1 from public.finance_extractions where sheet_id=current_setting('finance.test_sheet')::uuid) then raise exception 'Extraction leaked'; end if;
end $$;
reset role;
select 'PASS: rolling head, missing-upload guard, source idempotency, stale-write denial, manual replacement lineage, restore/reapply, immutable history, group RLS (synthetic Storage metadata only)' as result;
rollback;
