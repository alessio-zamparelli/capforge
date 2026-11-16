import { useCallback, useEffect, useState } from "react";
import { KeyRound, ShieldOff } from "lucide-react";
import * as api from "./api";
import Dashboard from "./Dashboard";

export default function App() {
  const [phase, setPhase] = useState<"loading" | "login" | "dashboard" | "disabled">("loading");

  useEffect(() => {
    api
      .session()
      .then((s) => setPhase(s.authed ? "dashboard" : s.console_enabled ? "login" : "disabled"))
      .catch(() => setPhase("disabled"));
  }, []);

  const onLogin = useCallback(async (token: string) => {
    await api.login(token);
    setPhase("dashboard");
  }, []);

  if (phase === "loading") {
    return <Center>loading…</Center>;
  }
  if (phase === "disabled") {
    return (
      <Center>
        <div className="flex flex-col items-center gap-3 text-center">
          <ShieldOff className="h-10 w-10 text-muted-foreground" />          <p className="text-lg">Console disabled</p>
          <p className="max-w-sm text-sm text-muted-foreground">
            Set <code className="rounded bg-muted px-1">CAPFORGE_WEB_TOKEN</code> on the server to
            enable the console. The capability proxy keeps working without it.
          </p>
        </div>
      </Center>
    );
  }
  if (phase === "login") {
    return <Login onLogin={onLogin} />;
  }
  return <Dashboard onAuthLost={() => setPhase("login")} />;
}

function Center({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center p-6">{children}</div>
  );
}

function Login({ onLogin }: { onLogin: (token: string) => Promise<void> }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onLogin(token);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <form
        onSubmit={submit}
        className="w-full max-w-sm space-y-4 rounded-lg border border-border bg-card p-6 text-card-foreground shadow"
      >
        <div className="flex items-center gap-2">
          <KeyRound className="h-5 w-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">capforge console</h1>
        </div>
        <input
          type="password"
          autoFocus
          placeholder="Access token"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-1 focus:ring-ring"
        />
        {error && <p className="text-sm text-red-400">{error}</p>}
        <button
          type="submit"
          disabled={busy || !token}
          className="w-full rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
