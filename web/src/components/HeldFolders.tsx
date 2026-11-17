// A device's folder held back for its name (H8, V3-P103 §6.2): adopting it joins it to the project of that name (made
// when there is none). The hub takes it only while that device is connected and still reports the name; otherwise
// its words are shown. After a lost answer the projects are read again, which shows whether it happened.
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { adoptHeldFolder } from "@/api/hub";
import { hubKeys } from "@/app/queries";
import { answerLost } from "@/features/hub/unconfirmed";
import { toast } from "@/features/toast";
import { failureText } from "./ProjectRequestCard";
import { Button } from "./ui/button";

export function AdoptButton({ project, runner, connected, className }: {
  project: string; runner: string; connected: boolean; className?: string;
}) {
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const adopt = async () => {
    setBusy(true);
    try {
      await adoptHeldFolder(project, runner);
      toast(`${runner}'s folder joined ${project}.`);
    } catch (error) {
      toast(answerLost(error) ? "The hub did not answer; the projects are read again to show whether it happened."
        : failureText(error), "error");
    } finally {
      setBusy(false);
      void client.invalidateQueries({ queryKey: hubKeys.projects });
    }
  };
  return (
    <Button className={className} disabled={busy || !connected} title={connected ? undefined : "The device is not connected."}
      aria-label={`Adopt ${project} from ${runner}`} onClick={() => void adopt()}>
      Adopt
    </Button>
  );
}
