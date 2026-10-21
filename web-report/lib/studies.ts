import { and, eq, sql } from 'drizzle-orm';
import { getDb } from '@/db';
import { reportPreferences, reportStudies } from '@/db/schema';
import { emptyPreferences, observationScope } from './controls.mjs';
import { latestReport } from './report';

type Kind='technical'|'valuation';
const key=(userId:string,code:string,kind:Kind)=>and(eq(reportStudies.userId,userId),eq(reportStudies.code,code),eq(reportStudies.kind,kind));
export async function studyContext(userId:string){
  const db=getDb(),report:any=await latestReport();
  const [row]=await db.select().from(reportPreferences).where(eq(reportPreferences.userId,userId)).limit(1);
  const preferences=row?JSON.parse(row.payload):emptyPreferences();
  const scope=observationScope(report.tracking,preferences,report.public_ignored);
  return {db,report,preferences,scope};
}
export async function readStudy(userId:string,code:string,kind:Kind){
  const [row]=await getDb().select().from(reportStudies).where(key(userId,code,kind)).limit(1);
  return row?{...row,value:JSON.parse(row.payload)}:null;
}
export async function claimStudy(userId:string,code:string,kind:Kind,session:string){
  const now=new Date().toISOString(),token=crypto.randomUUID(),until=new Date(Date.now()+180000).toISOString();
  const saved=await getDb().insert(reportStudies).values({userId,code,kind,session,payload:'{}',updatedAt:now,leaseUntil:until,lockToken:token})
    .onConflictDoUpdate({target:[reportStudies.userId,reportStudies.code,reportStudies.kind],set:{leaseUntil:until,lockToken:token},
      setWhere:sql`${reportStudies.leaseUntil} IS NULL OR ${reportStudies.leaseUntil} < ${now}`}).returning({token:reportStudies.lockToken});
  return saved.length?token:null;
}
export async function saveStudy(userId:string,code:string,kind:Kind,session:string,value:unknown,token:string){
  const saved=await getDb().update(reportStudies).set({session,payload:JSON.stringify(value),updatedAt:new Date().toISOString(),leaseUntil:null,lockToken:null})
    .where(and(key(userId,code,kind),eq(reportStudies.lockToken,token))).returning({code:reportStudies.code});
  if(!saved.length)throw Error('此分析已被较新的刷新替代，未覆盖新结果');
}
export async function releaseStudy(userId:string,code:string,kind:Kind,token:string){
  await getDb().update(reportStudies).set({leaseUntil:null,lockToken:null}).where(and(key(userId,code,kind),eq(reportStudies.lockToken,token)));
}
export async function listStudies(userId:string,kind:Kind){
  const rows=await getDb().select().from(reportStudies).where(and(eq(reportStudies.userId,userId),eq(reportStudies.kind,kind)));
  return rows.map(row=>({code:row.code,session:row.session,updated_at:row.updatedAt,value:JSON.parse(row.payload)}));
}
