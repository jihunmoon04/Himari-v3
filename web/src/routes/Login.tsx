import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { ApiError } from "@/api/client";
import { signIn } from "@/api/himari";
import { keys } from "@/app/queries";
import { Button } from "@/components/ui/button";

export function Login() {
  const client = useQueryClient();
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!token.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await signIn(token.trim());
      setToken("");
      await client.invalidateQueries({ queryKey: keys.session });
    } catch (failure) {
      setError(failure instanceof ApiError && failure.status === 401 ? "That token is not valid." : "Could not sign in.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-dvh items-center justify-center bg-bg px-4 text-ink">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-2xl border border-line bg-panel p-6 shadow-sm">
        <div>
          <h1 className="text-lg font-semibold">HIMARI</h1>
          <p className="mt-1 text-sm text-muted">Sign in with the operator token from the server.</p>
        </div>
        <input
          type="password"
          aria-label="Operator token"
          autoComplete="current-password"
          autoFocus
          value={token}
          onChange={(event) => setToken(event.target.value)}
          className="w-full rounded-lg border border-line bg-bg px-3 py-2 text-sm outline-none focus:border-faint"
        />
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <Button type="submit" variant="primary" className="w-full" disabled={busy || !token.trim()}>
          Sign in
        </Button>
      </form>
    </div>
  );
}
