-- Run through Supabase MCP. All fixtures, including Auth users, roll back.
begin;
select set_config('finance.test_user',gen_random_uuid()::text,true);
select set_config('finance.test_outsider',gen_random_uuid()::text,true);
select set_config('finance.test_group',(select id::text from public.finance_groups where name='BVW Debate Financial Consolidation'),true);
select set_config('finance.test_sheet',gen_random_uuid()::text,true);
select set_config('finance.test_file',gen_random_uuid()::text,true);
insert into auth.users(id) values (current_setting('finance.test_user')::uuid),(current_setting('finance.test_outsider')::uuid);
insert into public.finance_members(group_id,user_id) values(current_setting('finance.test_group')::uuid,current_setting('finance.test_user')::uuid);
select set_config('request.jwt.claims', json_build_object('sub',current_setting('finance.test_user'),'role','authenticated')::text,true);
set local role authenticated;
insert into public.finance_sheets(id,group_id,name) values(current_setting('finance.test_sheet')::uuid,current_setting('finance.test_group')::uuid,'Rollback verification');
insert into public.finance_files(id,group_id,sheet_id,uploaded_by,filename,mime_type,size_bytes,sha256,object_path,status)
values(current_setting('finance.test_file')::uuid,current_setting('finance.test_group')::uuid,current_setting('finance.test_sheet')::uuid,current_setting('finance.test_user')::uuid,'verify.pdf','application/pdf',10,repeat('a',64),current_setting('finance.test_group')||'/'||current_setting('finance.test_file')||'/source','uploading');
update public.finance_files set status='uploaded' where id=current_setting('finance.test_file')::uuid;
do $$
begin
 begin
  perform public.finance_finish_extraction(current_setting('finance.test_file')::uuid,
   '[{"date":"2026-11-16","description":"Valid first row","amount_cents":1250,"financial_class":"revenue","category":"student_dues"},{"financial_class":"revenue","category":"travel_expenses"}]');
  raise exception 'Invalid extraction accepted';
 exception when check_violation then null;
 end;
 if exists(select 1 from public.finance_transactions where sheet_id=current_setting('finance.test_sheet')::uuid) then raise exception 'Partial batch committed'; end if;
 begin
  update public.finance_files set filename='tampered.pdf' where id=current_setting('finance.test_file')::uuid;
  raise exception 'Metadata mutation accepted';
 exception when raise_exception then
  if sqlerrm<>'Source metadata is immutable' then raise; end if;
 end;
end $$;
select public.finance_finish_extraction(current_setting('finance.test_file')::uuid,'[{"date":"2026-11-16","description":"Dues","amount_cents":1250,"financial_class":"revenue","category":"student_dues"}]');
select public.finance_finish_extraction(current_setting('finance.test_file')::uuid,'[]');
do $$
begin
 if (select count(*) from public.finance_transactions where sheet_id=current_setting('finance.test_sheet')::uuid)<>1 then raise exception 'Duplicate processing'; end if;
 if (select status from public.finance_files where id=current_setting('finance.test_file')::uuid)<>'processed' then raise exception 'Posting state'; end if;
 if jsonb_array_length((public.finance_snapshot(current_setting('finance.test_sheet')::uuid)).snapshot)<>0 then raise exception 'Draft counted'; end if;
 begin
  update public.finance_transactions set category='travel_expenses' where sheet_id=current_setting('finance.test_sheet')::uuid;
  raise exception 'Invalid category accepted';
 exception when check_violation then null;
 end;
end $$;
update public.finance_transactions set review_status='approved' where sheet_id=current_setting('finance.test_sheet')::uuid;
do $$
declare v public.finance_sheet_versions;
begin
 v:=public.finance_snapshot(current_setting('finance.test_sheet')::uuid);
 if (public.finance_summary(current_setting('finance.test_sheet')::uuid)->'monthly'->0->>'revenue_cents')::bigint<>1250 then raise exception 'Summary total'; end if;
 if v.version<>2 or jsonb_array_length(v.snapshot)<>1 then raise exception 'Version snapshot'; end if;
 begin
  update public.finance_sheet_versions set snapshot='[]' where sheet_id=current_setting('finance.test_sheet')::uuid;
  raise exception 'History mutable';
 exception when insufficient_privilege then null;
 end;
end $$;
select set_config('request.jwt.claims',json_build_object('sub',current_setting('finance.test_outsider'),'role','authenticated')::text,true);
do $$
begin
 if exists(select 1 from public.finance_sheets where id=current_setting('finance.test_sheet')::uuid) then raise exception 'Sheet leaked'; end if;
 if exists(select 1 from public.finance_files where id=current_setting('finance.test_file')::uuid) then raise exception 'File leaked'; end if;
 if exists(select 1 from public.finance_sheet_versions where sheet_id=current_setting('finance.test_sheet')::uuid) then raise exception 'History leaked'; end if;
 begin
  insert into public.finance_sheets(group_id,name) values(current_setting('finance.test_group')::uuid,'Unauthorized');
  raise exception 'Unauthorized insert accepted';
 exception when insufficient_privilege then null;
 end;
end $$;
reset role;
select 'PASS: member access, outsider denial, atomic/idempotent extraction and rollback, draft exclusion, category constraints, immutable source metadata/versions' as result;
rollback;
