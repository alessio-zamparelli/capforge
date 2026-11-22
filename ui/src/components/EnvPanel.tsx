import { useState } from "react";
import { KeyRound } from "lucide-react";
import { toast } from "sonner";
import * as api from "@/api";
import { isAuthError } from "@/api";
import type { EnvInfo } from "@/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";

export function EnvPanel({
  name,
  env,
  onChanged,
  onAuthLost,
}: {
  name: string;
  env: EnvInfo[];
  onChanged: () => Promise<void>;
  onAuthLost: () => void;
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fail = (err: unknown) => {
    if (isAuthError(err)) {
      onAuthLost();
      return;
    }
    setError(err instanceof Error ? err.message : String(err));
  };

  const restartMessage = (result: api.EnvChange, verb: string) =>
    result.restart_error
      ? `${verb}, but the web instance did not restart: ${result.restart_error}`
      : result.restarted
        ? `${verb}; the web instance was restarted.`
        : `${verb}.`;

  const save = async (key: string) => {
    const value = drafts[key] ?? "";
    if (!value) return;
    setBusy(key);
    setError(null);
    try {
      const result = await api.setEnv(name, key, value);
      setDrafts((current) => ({ ...current, [key]: "" }));
      toast.success(restartMessage(result, "Saved"));
      await onChanged();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const clear = async (key: string) => {
    setBusy(key);
    setError(null);
    try {
      const result = await api.clearEnv(name, key);
      setDrafts((current) => ({ ...current, [key]: "" }));
      toast.success(restartMessage(result, "Removed"));
      await onChanged();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="flex flex-col gap-3">
      <div className="flex items-center gap-2 text-sm font-medium">
        <KeyRound data-icon="inline-start" />
        Environment
      </div>
      <p className="text-xs text-muted-foreground">
        Values are injected into this capability's code at runtime and are never returned by
        the console. Only store credentials you trust it to use.
      </p>

      {error && (
        <Alert variant="destructive">
          <AlertTitle>Environment change failed</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <FieldGroup>
        {env.map((item) => {
          const placeholder = item.configured
            ? "replace value"
            : item.default !== null
              ? `default: ${item.default}`
              : "value";
          const draft = drafts[item.name] ?? "";
          return (
            <Field key={item.name} className="rounded-lg border p-3">
              <FieldLabel htmlFor={`env-${item.name}`} className="flex-wrap gap-2">
                <code className="rounded bg-muted px-1.5 py-0.5 text-xs">{item.name}</code>
                <Badge variant="outline">{item.type}</Badge>
                <Badge
                  variant={
                    item.required && !item.configured ? "destructive" : "secondary"
                  }
                >
                  {item.configured ? "set" : item.required ? "missing" : "optional"}
                </Badge>
                {!item.declared && <Badge variant="outline">not declared</Badge>}
              </FieldLabel>
              {item.description && <FieldDescription>{item.description}</FieldDescription>}
              {item.default !== null && (
                <FieldDescription>
                  default: <code>{item.default}</code>
                </FieldDescription>
              )}
              <div className="flex flex-wrap items-center gap-2">
                {item.type === "bool" ? (
                  <Select
                    value={draft}
                    onValueChange={(value) =>
                      setDrafts((current) => ({ ...current, [item.name]: value }))
                    }
                  >
                    <SelectTrigger id={`env-${item.name}`} className="min-w-48 flex-1">
                      <SelectValue placeholder={item.configured ? "replace value" : "choose…"} />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        <SelectItem value="true">true</SelectItem>
                        <SelectItem value="false">false</SelectItem>
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                ) : (
                  <Input
                    id={`env-${item.name}`}
                    type={item.type === "int" || item.type === "float" ? "number" : "password"}
                    step={item.type === "int" ? "1" : item.type === "float" ? "any" : undefined}
                    autoComplete="new-password"
                    value={draft}
                    onChange={(event) =>
                      setDrafts((current) => ({ ...current, [item.name]: event.target.value }))
                    }
                    placeholder={placeholder}
                    className="min-w-48 flex-1"
                  />
                )}
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void save(item.name)}
                  disabled={busy !== null || !draft}
                >
                  {busy === item.name && <Spinner data-icon="inline-start" />}
                  Save
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => void clear(item.name)}
                  disabled={busy !== null || !item.configured}
                >
                  Clear
                </Button>
              </div>
            </Field>
          );
        })}
      </FieldGroup>
    </section>
  );
}
