import type { ReactNode } from "react";
import { PulseLayout } from "@/modules/pulse/PulseLayout";

export const metadata = { title: "Agent Ops · SignalPilot" };

export default function Layout({ children }: { children: ReactNode }) {
  return <PulseLayout>{children}</PulseLayout>;
}
