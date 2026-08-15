import type { Metadata } from "next";
// React Flow 基础样式必须先于 globals.css 导入，保证 globals.css 中的
// .react-flow__* / .bb-* 自定义覆盖生效（否则黑板画布渲染为一团乱码）。
import "@xyflow/react/dist/style.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Aemeath — CTF Agent Command Deck",
  description: "Observe and command the autonomous CTF solver swarm (自建 agent · 直连 LLM 平台).",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
