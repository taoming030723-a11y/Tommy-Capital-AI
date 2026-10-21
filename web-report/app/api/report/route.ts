import { latestReport } from '@/lib/report';
import { publicStudyStock } from '@/lib/technical-review.mjs';
import { fetchTechnicalReview, latestTechnicalSession } from '@/lib/technical-market.mjs';
export const dynamic='force-dynamic';
const headers={'Cache-Control':'no-store'};
export async function GET(request:Request){
  try{
    const report:any=await latestReport();
    const code=new URL(request.url).searchParams.get('public_technical_code');
    if(code!==null){
      const stock=publicStudyStock(report,code);
      if(!stock)return Response.json({error:'仅可读取未忽略且累计三次的公开Top10观察；不读取私人持仓。'},{status:404,headers});
      const session=await latestTechnicalSession(),context=report.top_candidates.find((r:any)=>r.code===code);
      const value=await fetchTechnicalReview(code,session,context?.route);
      // Read-only public market facts: no private settings, D1 writes or model calls.
      return Response.json({...value,name:stock.name,public:true,source_report_session:report.session},{headers});
    }
    return Response.json(report,{headers});
  }catch(error){return Response.json({error:error instanceof Error?error.message:'报告读取失败，请稍后重试。'},{status:503,headers});}
}
