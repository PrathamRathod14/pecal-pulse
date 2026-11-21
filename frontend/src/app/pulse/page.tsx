import { Suspense } from "react";
import { ControlCenter } from "@/modules/pulse/ControlCenter";
import { Loading } from "@/modules/pulse/ui";

export default function Page() {
  return (
    <Suspense fallback={<Loading />}>
      <ControlCenter />
    </Suspense>
  );
}
