import { useCallback, useEffect, useState } from "react";
import { KeyRound, ShieldOff } from "lucide-react";
import * as api from "@/api";
import Dashboard from "@/Dashboard";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";

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

  return (
    <TooltipProvider>
      {phase === "loading" && <Center>loading…</Center>}
      {phase === "disabled" && <Disabled />}
      {phase === "login" && <Login onLogin={onLogin} />}
      {phase === "dashboard" && <Dashboard onAuthLost={() => setPhase("login")} />}
      <Toaster position="bottom-right" />
    </TooltipProvider>
  );
}

function Center({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center p-6">{children}</div>
  );
}

function Disabled() {
  return (
    <Center>
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldOff data-icon="inline-start" />
            Console disabled
          </CardTitle>
        </CardHeader>
        <CardContent>
          <Alert>
            <AlertTitle>Set an access token</AlertTitle>
            <AlertDescription>
              Set <code className="rounded bg-muted px-1">CAPFORGE_WEB_TOKEN</code> on the
              server to enable the console. The capability proxy keeps working without it.
            </AlertDescription>
          </Alert>
        </CardContent>
      </Card>
    </Center>
  );
}

function Login({ onLogin }: { onLogin: (token: string) => Promise<void> }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
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
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <KeyRound data-icon="inline-start" />
            capforge console
          </CardTitle>
          <CardDescription>Sign in with the operator token to manage capabilities.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit}>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="access-token">Access token</FieldLabel>
                <Input
                  id="access-token"
                  type="password"
                  autoFocus
                  autoComplete="current-password"
                  placeholder="Access token"
                  value={token}
                  onChange={(event) => setToken(event.target.value)}
                />
                <FieldDescription>
                  The same value as <code className="rounded bg-muted px-1">CAPFORGE_WEB_TOKEN</code>.
                </FieldDescription>
              </Field>
              {error && (
                <Alert variant="destructive">
                  <AlertTitle>Sign-in failed</AlertTitle>
                  <AlertDescription>{error}</AlertDescription>
                </Alert>
              )}
              <Button type="submit" disabled={busy || !token} className="w-full">
                {busy && <Spinner data-icon="inline-start" />}
                {busy ? "Signing in…" : "Sign in"}
              </Button>
            </FieldGroup>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
