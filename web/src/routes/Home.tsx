import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router";
import { ApiError } from "@/api/client";
import { createConversation, sendMessage } from "@/api/himari";
import { keys } from "@/app/queries";
import { Composer } from "@/components/Composer";
import { PageHeader } from "@/components/Shell";

/** A new task: the first message makes the conversation (V3-P102 §4.1; agent sessions join this in W3). */
export function Home() {
  const navigate = useNavigate();
  const client = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const start = async (text: string) => {
    setError(null);
    try {
      const made = await createConversation();
      await sendMessage(made.id, text);
      await client.invalidateQueries({ queryKey: keys.conversations });
      navigate(`/c/${encodeURIComponent(made.id)}`);
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.message : "Could not start the conversation.");
      throw failure; // the draft stays
    }
  };

  return (
    <>
      <PageHeader />
      <div className="flex flex-1 flex-col items-center justify-center px-4 pb-[12vh]">
        <div className="w-full max-w-2xl">
          <h1 className="mb-6 text-center text-2xl font-semibold tracking-tight">What should we work on?</h1>
          <Composer draftKey="home" onSend={start} autoFocus placeholder="Ask HIMARI, or hand it a task" />
          {error && <p role="alert" className="mt-2 text-sm text-danger">{error}</p>}
        </div>
      </div>
    </>
  );
}
