import { getChatGPTUser } from '@/app/chatgpt-auth';
import { codeValue } from '@/lib/controls.mjs';
import { fetchValuation } from '@/lib/market-data.mjs';
import { reusableValuation, baselineView } from '@/lib/valuation-cache.mjs';
import { studyContext, readStudy, claimStudy, saveStudy, releaseStudy } from '@/lib/studies';
export const dynamic='force-dynamic';
const headers={'Cache-Control':'private, no-store',Vary:'Cookie'};
const json=(value:unknown,status=200)=>Response.json(value,{status,headers});
export async function GET(request:Request){return lookup(request,false);}
export async function POST(request:Request){
  if(request.headers.get('origin')!==new URL(request.url).origin)return json({error:'请求来源不匹配。'},403);
  return lookup(request,true);
}
async function lookup(request:Request,force:boolean){
  const user=await getChatGPTUser();if(!user)return json({error:'请先登录你的ChatGPT账号。'},401);
  let code:string;try{code=codeValue(new URL(request.url).searchParams.get('code'));}catch{return json({error:'请提供六位A股代码。'},400);}
  let token:string|null=null;
  try{
    const {report,scope}=await studyContext(user.userId);
    if(![...report.top_candidates,...scope].some((r:any)=>r.code===code))return json({error:'该股票不在当前报告或观察范围。'},404);
    const old=await readStudy(user.userId,code,'valuation');
    if(!force&&reusableValuation(old?.value))return json(baselineView(old!.value,report.session));
    token=await claimStudy(user.userId,code,'valuation',report.session);
    if(!token)return json({code,status:'pending',label:'估值基准正在核验，未重复抓取'},202);
    const context=report.top_candidates.find((r:any)=>r.code===code);
    const value=!force&&reusableValuation(report.valuations?.[code])?report.valuations[code]:await fetchValuation(code,report.session,context);
    if(reusableValuation(value))await saveStudy(user.userId,code,'valuation',value.price_as_of||value.session,value,token);
    return json(baselineView(value,report.session));
  }catch{return json({code,status:'unavailable',label:'估值待核验',reasons:['真实估值源暂不可用；未重复评估已完成基准']});}
  finally{if(token)await releaseStudy(user.userId,code,'valuation',token).catch(()=>{});}
}
