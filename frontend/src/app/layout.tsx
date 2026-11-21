import type { Metadata } from "next";
import "./globals.css";
import "./pulse/pulse.css";
import { AppShell } from "@/components/layout/AppShell";
export const metadata: Metadata = {
  title: "SignalPilot · Agentic customer intelligence",
  description:
    "Mock sales-assistant workspace for the Perschmann calibration hackathon.",
};
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
