import { requireChatGPTUser } from "./chatgpt-auth";
import ReportDashboard from "./report-dashboard";

export const dynamic = "force-dynamic";

export default async function Home() {
  await requireChatGPTUser("/");
  return <ReportDashboard />;
}
