import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "鲲 · KUN",
  description: "Agent OS · Agent 管家",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN">
      <body className="kun-shell flex flex-col">
        <header className="kun-topbar">
          <div className="kun-topbar-inner">
            <div className="kun-brand">
              <span className="kun-brand-mark">鲲</span>
              <div className="min-w-0">
                <div className="kun-brand-title">KUN 控制台</div>
                <div className="kun-brand-subtitle">Agent OS · 内测工作台</div>
              </div>
            </div>
            <nav className="kun-nav">
              <Link href="/" className="kun-nav-link">
                主工作区
              </Link>
              <Link href="/nuo" className="kun-nav-link">
                傩 · 管家
              </Link>
              <Link href={{ pathname: "/control-plane" }} className="kun-nav-link">
                任务驾驶舱
              </Link>
              <Link href="/billing" className="kun-nav-link">
                计费透明
              </Link>
              <Link href="/account" className="kun-nav-link">
                会话 / 账号
              </Link>
            </nav>
          </div>
        </header>
        <main className="flex-1">{children}</main>
      </body>
    </html>
  );
}
