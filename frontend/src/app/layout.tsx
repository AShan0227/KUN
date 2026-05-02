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
      <body className="min-h-screen flex flex-col">
        <header className="px-6 py-3 bg-white border-b border-gray-200 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className="text-2xl font-bold tracking-tight">鲲</span>
            <span className="text-sm text-gray-500">KUN · Agent OS</span>
          </div>
          <nav className="flex gap-4 text-sm">
            <Link href="/" className="hover:text-kun-accent">
              主工作区
            </Link>
            <Link href="/nuo" className="hover:text-kun-accent">
              傩 · 管家
            </Link>
            <Link href="/billing" className="hover:text-kun-accent">
              计费透明
            </Link>
            <Link href="/account" className="hover:text-kun-accent">
              会话 / 账号
            </Link>
          </nav>
        </header>
        <main className="flex-1">{children}</main>
      </body>
    </html>
  );
}
