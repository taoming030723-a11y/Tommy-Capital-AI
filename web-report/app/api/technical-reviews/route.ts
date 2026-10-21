import { env } from 'cloudflare:workers';
import { getChatGPTUser } from '@/app/chatgpt-auth';
import { codeValue } from '@/lib/controls.mjs';
import { fetchTechnicalReview, latestTechnicalSession } from '@/lib/technical-market.mjs';
import { REVIEW_VERSION, POLICY_ID } from '@/lib/technical-review.mjs';
import { reusableTechnical } from '@/lib/study-policy.mjs';
import { explainTechnical, AI_PROMPT_VERSION } from '@/lib/ai-technical.mjs';
import { studyContext, readStudy, claimStudy, saveStudy, releaseStudy, listStudies } from '@/lib/studies';
export const dynamic='force-dynamic';
const headers={'Cache-Control':'private, no-store',Vary:'Cookie'};
const json=(value:unknown,status=200)=>Response.json(value,{status,headers});
const apiKey=()=>env.OPENAI_API_KEY || process.env.OPENAI_API_KEY;
const model=()=>env.OPENAI_TECHNICAL_MODEL || process.env.OPENAI_TECHNICAL_MODEL || 'gpt-5-mini';
export async function GET(){
  const user=await getChatGPTUser();if(!user)return json({error:'请先登录你的ChatGPT账号。'},401);
  try{
    const {report,scope}=await studyContext(user.userId),session=await latestTechnicalSession(),codes=new Set(scope.map((r:any)=>r.code));
    const rows=(await listStudies(user.userId,'technical')).filter(r=>codes.has(r.code)&&r.value.version===REVIEW_VERSION&&r.value.policy_id===POLICY_ID);
    return json({session,report_session:report.session,scope,rows,api_configured:!!apiKey(),model:model(),fundamental_policy:'已完成的估值基准不随技术刷新重复评估'});
  }catch{return json({error:'技术评估暂未读取完成，原名单未更改。'},503);}
}
export async function POST(request:Request){
  const user=await getChatGPTUser();if(!user)return json({error:'请先登录你的ChatGPT账号。'},401);
  if(request.headers.get('origin')!==new URL(request.url).origin||!request.headers.get('content-type')?.startsWith('application/json'))return json({error:'请求来源不匹配。'},403);
  let body:any,code:string;
  try{const raw=await request.text();if(raw.length>2000)throw Error();body=JSON.parse(raw);code=codeValue(body.code);if(body.force!==undefined&&typeof body.force!=='boolean')throw Error();}
  catch{return json({error:'请输入当前观察名单内的六位代码。'},400);}
  let token:string|null=null;
  try{
    const {report,scope}=await studyContext(user.userId);
    const session=await latestTechnicalSession();
    if(!scope.some((r:any)=>r.code===code))return json({error:'该股票不在当前持续观察或持仓范围，未发起行情或模型请求。'},404);
    const old=await readStudy(user.userId,code,'technical');
    const fresh=reusableTechnical(old,session,{force:body.force===true});
    if(fresh&&!body.force&&(!apiKey()||(old!.value.ai?.status==='complete'&&old!.value.ai.model===model()&&old!.value.ai.prompt_version===AI_PROMPT_VERSION)))return json({...old!.value,reused:true});
    token=await claimStudy(user.userId,code,'technical',session);
    if(!token)return json({status:'pending',code,message:'同一股票已有分析正在进行，未重复调用。'},202);
    const context=report.top_candidates.find((r:any)=>r.code===code);
    const published=report.technical_reviews?.rows?.find((r:any)=>r.code===code&&r.session===session&&r.version===REVIEW_VERSION&&r.policy_id===POLICY_ID&&r.data_complete);
    const review=fresh&&!body.force?{...old!.value}:published&&!body.force?{...published}:await fetchTechnicalReview(code,session,context?.route);
    try{review.ai=await explainTechnical(review,{apiKey:apiKey(),model:model(),previous:old?.value.ai});}
    catch{review.ai={status:'unavailable',label:'ChatGPT解释未完成，程序规则评估保留。'};}
    await saveStudy(user.userId,code,'technical',session,review,token);
    return json(review);
  }catch{return json({error:'技术评估暂未完成，未改变原排名、持仓或买点资格。'},503);}
  finally{if(token)await releaseStudy(user.userId,code,'technical',token).catch(()=>{});}
}
