import type { Metadata } from "next";
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
