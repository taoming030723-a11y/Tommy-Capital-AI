// A completed valuation study is an explicitly dated baseline, not a live price.
export function reusableValuation(value){return value&&['complete','not_applicable'].includes(value.status)&&typeof value.price_as_of==='string';}
export function baselineView(value,currentSession){return {...value,baseline_as_of:value.price_as_of||value.session,report_session:currentSession,cached_baseline:true,baseline_note:'复用首次完成的基本面/估值基准；非当日重新估值，技术刷新不触发财务重评'};}
