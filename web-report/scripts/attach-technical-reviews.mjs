// Small public watch universe only; never send private holdings to Actions.
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fetchTechnicalReview } from '../lib/technical-market.mjs';
import { explainTechnical } from '../lib/ai-technical.mjs';
import { persistEnrichedReport } from './attach-valuations.mjs';
const root=process.argv[2]||'reports/latest';
if(root==='--help'){console.log('Usage: node web-report/scripts/attach-technical-reviews.mjs [reports/latest]');process.exit(0);}
const report=JSON.parse(await readFile(path.join(root,'report.json'),'utf8'));
if(report.scan_phase!=='closing'||!['complete','partial'].includes(report.status)){console.log('非正式收盘报告，未运行观察技术评估。');process.exit(0);}
const monitor=report.macd_monitor;
const tracking=report.top10_tracking,ignored=new Set(tracking?.preferences?.ignored_codes||[]);
if(tracking?.preferences_status!=='ok')throw Error('公开技术观察缺少已核验忽略设置');
const scope=(tracking.rows||[]).filter(r=>r.appearance_count>=3&&!ignored.has(r.code)).map(r=>({code:r.code,name:r.name,sources:['Top10观察']}));
if(monitor&&(monitor.public!==true||monitor.scope.some(r=>r.sources.some(s=>s!=='Top10观察')||!scope.some(x=>x.code===r.code))))throw Error('公开收盘只允许已核验的Top10观察，禁止个人持仓');
const rows=new Array(scope.length);let cursor=0;const deadline=Date.now()+360000;
async function worker(){while(cursor<scope.length){const i=cursor++,stock=scope[i];
  if(Date.now()>deadline){rows[i]={code:stock.code,session:report.session,status:'unavailable',decision:'评估达到时间预算，未补造数据',data_complete:false,frames:{},orders_authorized:false};continue;}
  try{const context=[...report.rankings,...report.observations].find(r=>r.code===stock.code);const value=await fetchTechnicalReview(stock.code,report.session,context?.route);
    value.name=stock.name;
    try{value.ai=await explainTechnical(value,{apiKey:process.env.OPENAI_API_KEY,model:process.env.OPENAI_TECHNICAL_MODEL||'gpt-5-mini'});}
    catch{value.ai={status:'unavailable',label:'ChatGPT解释未完成，保留已取得的技术数据与程序评估'};}
    rows[i]=value;
  }catch{rows[i]={code:stock.code,name:stock.name,session:report.session,status:'unavailable',decision:'数据或模型请求未完成，不能确认买点',data_complete:false,frames:{},orders_authorized:false};}
}}
await Promise.all([worker(),worker()]);
report.technical_reviews={schema:1,public:true,session:report.session,checked_at:new Date().toISOString(),status:rows.every(r=>r.data_complete)?'complete':'partial',stock_count:rows.length,rows,influences_ranking:false,fundamental_policy:'本模块只评估技术面，复用已有基本面估值，不重复研究'};
await persistEnrichedReport(root,report);
console.log(JSON.stringify({focused_stock_count:rows.length,status:report.technical_reviews.status,chatgpt_completed:rows.filter(r=>r.ai?.status==='complete').length}));
