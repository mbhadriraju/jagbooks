-- Guard source metadata and history against client-side edits; status alone may change.
create function public.finance_guard_source() returns trigger language plpgsql security invoker set search_path='' as $$
begin
 if (to_jsonb(new)-'status') is distinct from (to_jsonb(old)-'status') then
  raise exception 'Source metadata is immutable';
 end if;
 if new.status<>old.status and not ((old.status='uploading' and new.status='uploaded') or (old.status in ('uploaded','failed') and new.status='processed')) then
  raise exception 'Invalid source state transition';
 end if;
 return new;
end $$;
revoke all on function public.finance_guard_source() from public,anon,authenticated;
create trigger finance_source_immutable before update on public.finance_files for each row execute function public.finance_guard_source();
drop policy member_insert on public.finance_files;
create policy member_insert on public.finance_files for insert to authenticated with check(public.finance_is_member(group_id) and uploaded_by=(select auth.uid()) and status='uploading');
drop policy member_insert on public.finance_sheet_versions;
create policy member_insert on public.finance_sheet_versions for insert to authenticated with check(public.finance_is_member(group_id) and created_by=(select auth.uid()));
create function public.finance_summary(p_sheet uuid) returns jsonb language sql stable security invoker set search_path='' as $$
 with approved as (select * from public.finance_transactions where sheet_id=p_sheet and review_status='approved'),
 monthly as (select to_char(date,'YYYY-MM') as month,
 coalesce(sum(amount_cents) filter(where financial_class='revenue'),0) as revenue_cents,
 coalesce(sum(amount_cents) filter(where financial_class='expense'),0) as expense_cents
 from approved where financial_class in ('revenue','expense') group by to_char(date,'YYYY-MM'))
 select jsonb_build_object('approved_count',(select count(*) from approved),'monthly',
 coalesce((select jsonb_agg(jsonb_build_object('month',month,'revenue_cents',revenue_cents,'expense_cents',expense_cents,'net_cents',revenue_cents-expense_cents) order by month) from monthly),'[]'::jsonb))
$$;
revoke all on function public.finance_summary(uuid) from public,anon;
grant execute on function public.finance_summary(uuid) to authenticated;
notify pgrst,'reload schema';
