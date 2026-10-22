import {and,eq} from 'drizzle-orm';
import {getChatGPTUser} from '@/app/chatgpt-auth';
import {getDb} from '@/db';
import {reportStudies} from '@/db/schema';
import {emptyJournal,applyJournalAction,localDay} from '@/lib/decision-support.mjs';
export const dynamic='force-dynamic';
const headers={'Cache-Control':'private, no-store',Vary:'Cookie'},json=(v:unknown,s=200)=>Response.json(v,{status:s,headers});
const key=(uid:string)=>and(eq(reportStudies.userId,uid),eq(reportStudies.code,'000000'),eq(reportStudies.kind,'journal'));
export async function GET(){
 const user=await getChatGPTUser();if(!user)return json({error:'请先登录你的ChatGPT账号。'},401);
 try{const [row]=await getDb().select().from(reportStudies).where(key(user.userId)).limit(1);return json(row?JSON.parse(row.payload):emptyJournal());}
 catch{return json({error:'私人研究记录暂未读取，未清空已有数据。'},503);}
}
export async function POST(request:Request){
 const user=await getChatGPTUser();if(!user)return json({error:'请先登录你的ChatGPT账号。'},401);
 if(request.headers.get('origin')!==new URL(request.url).origin||!request.headers.get('content-type')?.startsWith('application/json'))return json({error:'请求来源不匹配。'},403);
 let body:any;try{const raw=await request.text();if(raw.length>20000)throw Error();body=JSON.parse(raw);if(!Number.isSafeInteger(body.revision)||body.revision<0)throw Error();}catch{return json({error:'研究操作格式无效。'},400);}
 try{
  const db=getDb(),[old]=await db.select().from(reportStudies).where(key(user.userId)).limit(1),state=old?JSON.parse(old.payload):emptyJournal();
  if(state.revision!==body.revision)return json({error:'另一窗口已更新研究或流水，请刷新后重试。'},409);
  let next;try{next=applyJournalAction(state,body);}catch(e){return json({error:e instanceof Error?e.message:'输入无效'},400);}
  const record={userId:user.userId,code:'000000',kind:'journal',session:localDay(),payload:JSON.stringify(next),updatedAt:new Date().toISOString()};
  const saved=old?await db.update(reportStudies).set(record).where(and(key(user.userId),eq(reportStudies.payload,old.payload))).returning({code:reportStudies.code}):await db.insert(reportStudies).values(record).onConflictDoNothing().returning({code:reportStudies.code});
  if(!saved.length)return json({error:'记录已被另一窗口修改，未覆盖。'},409);return json(next);
 }catch{return json({error:'私人研究暂未保存，请稍后重试；未写入公开GitHub或日志。'},503);}
}
