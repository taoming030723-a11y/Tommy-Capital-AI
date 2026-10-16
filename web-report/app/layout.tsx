import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Tommy Capital · 交互报告",
  description: "最新研究报告、持续观察与私人持仓管理。",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body className="antialiased">{children}</body>
    </html>
  );
}
