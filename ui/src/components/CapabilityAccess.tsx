import { useCallback, useEffect, useState } from "react";
import { Bot, ShieldCheck } from "lucide-react";
import { toast } from "sonner";
import * as api from "@/api";
import { isAuthError } from "@/api";
import type { AuthInfo, LlmConfig, LlmSelection } from "@/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Field,
  FieldDescription,
  FieldLabel,
} from "@/components/ui/field";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";

function restartMessage(result: api.CapLlmChange, verb: string) {
  return result.restart_error
    ? `${verb}, but the web instance did not restart: ${result.restart_error}`
    : result.restarted
      ? `${verb}; the web instance was restarted.`
      : `${verb}.`;
}

export function ModelPanel({
  name,
  selection,
  onChanged,
  onAuthLost,
}: {
  name: string;
  selection: LlmSelection | null;
  onChanged: () => Promise<void>;
  onAuthLost: () => void;
}) {
  const [config, setConfig] = useState<LlmConfig | null>(null);
  const [provider, setProvider] = useState(selection?.provider ?? "");
  const [model, setModel] = useState(selection?.model ?? "");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fail = (err: unknown) => {
    if (isAuthError(err)) {
      onAuthLost();
      return;
    }
    setError(err instanceof Error ? err.message : String(err));
  };

  const load = useCallback(async () => {
    try {
      setConfig(await api.llm());
      setError(null);
    } catch (err) {
      fail(err);
    }
  }, [onAuthLost]);

  useEffect(() => {
    void load();
  }, [load]);

  const providerModels =
    config?.providers.find((item) => item.id === provider)?.models ?? [];

  const save = async () => {
    if (!provider || !model) return;
    setBusy("save");
    setError(null);
    try {
      const result = await api.setCapLlm(name, provider, model);
      toast.success(restartMessage(result, "Model selection saved"));
      await onChanged();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const reset = async () => {
    setBusy("reset");
    setError(null);
    try {
      const result = await api.clearCapLlm(name);
      toast.success(restartMessage(result, "Reset to the default model"));
      setProvider(result.llm?.provider ?? "");
      setModel(result.llm?.model ?? "");
      await onChanged();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
        <Bot data-icon="inline-start" />
        Model
        {selection ? (
          <>
            <Badge variant="secondary">{selection.provider_label}</Badge>
            <code className="rounded bg-muted px-1.5 py-0.5 text-xs">
              {selection.model_label}
            </code>
            <Badge variant="outline">
              {selection.source === "cap" ? "capability override" : "global default"}
            </Badge>
            {selection.stale && <Badge variant="destructive">stale</Badge>}
          </>
        ) : (
          <Badge variant="destructive">not configured</Badge>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        This capability declares <code>LLM = True</code>. The operator picks the model here;
        the capability can only use the selected one through the hub gateway.
      </p>

      {error && (
        <Alert variant="destructive">
          <AlertTitle>Model change failed</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {config !== null && config.providers.length === 0 ? (
        <Alert>
          <AlertTitle>No providers configured</AlertTitle>
          <AlertDescription>
            Add an OpenAI-compatible provider on the Models tab first.
          </AlertDescription>
        </Alert>
      ) : (
        <div className="flex flex-wrap items-end gap-2">
          <Field className="min-w-44">
            <FieldLabel htmlFor={`model-provider-${name}`}>Provider</FieldLabel>
            <Select
              value={provider}
              onValueChange={(value) => {
                setProvider(value);
                const next = config?.providers.find((item) => item.id === value);
                setModel(next?.models[0]?.id ?? "");
              }}
            >
              <SelectTrigger id={`model-provider-${name}`}>
                <SelectValue placeholder="choose…" />
              </SelectTrigger>
              <SelectContent>
                {(config?.providers ?? []).map((item) => (
                  <SelectItem key={item.id} value={item.id}>
                    {item.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field className="min-w-44">
            <FieldLabel htmlFor={`model-model-${name}`}>Model</FieldLabel>
            <Select value={model} onValueChange={setModel}>
              <SelectTrigger id={`model-model-${name}`}>
                <SelectValue placeholder="choose…" />
              </SelectTrigger>
              <SelectContent>
                {providerModels.map((item) => (
                  <SelectItem key={item.id} value={item.id}>
                    {item.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <FieldDescription>Applies to every run of the active tag.</FieldDescription>
          </Field>
          <Button
            size="sm"
            onClick={() => void save()}
            disabled={busy !== null || !provider || !model}
          >
            {busy === "save" && <Spinner data-icon="inline-start" />}
            Save
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void reset()}
            disabled={busy !== null || selection?.source !== "cap"}
          >
            {busy === "reset" && <Spinner data-icon="inline-start" />}
            Use default
          </Button>
        </div>
      )}
    </section>
  );
}

export function AuthPanel({ name, auth }: { name: string; auth: AuthInfo }) {
  const link = auth.login_url;
  return (
    <section className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
        <ShieldCheck data-icon="inline-start" />
        Authentication
        <Badge variant="secondary">{auth.provider}</Badge>
        <Badge variant={auth.configured ? "outline" : "destructive"}>
          {auth.configured ? "provider configured" : "provider missing"}
        </Badge>
      </div>
      <p className="text-xs text-muted-foreground">
        Requests to <code>/cap/{name}/</code> require a signed-in user. The hub forwards the
        identity as <code>X-Capforge-User</code>; client-supplied values are stripped.
      </p>
      <div className="flex flex-col gap-1 text-xs text-muted-foreground">
        {auth.allow_emails.length > 0 && (
          <span>
            emails:{" "}
            {auth.allow_emails.map((item) => (
              <code key={item} className="mr-1 rounded bg-muted px-1">
                {item}
              </code>
            ))}
          </span>
        )}
        {auth.allow_domains.length > 0 && (
          <span>
            domains:{" "}
            {auth.allow_domains.map((item) => (
              <code key={item} className="mr-1 rounded bg-muted px-1">
                {item}
              </code>
            ))}
          </span>
        )}
        {auth.allow_groups.length > 0 && (
          <span>
            groups:{" "}
            {auth.allow_groups.map((item) => (
              <code key={item} className="mr-1 rounded bg-muted px-1">
                {item}
              </code>
            ))}
          </span>
        )}
        {auth.public_paths.length > 0 && (
          <span>
            public paths:{" "}
            {auth.public_paths.map((item) => (
              <code key={item} className="mr-1 rounded bg-muted px-1">
                {item}
              </code>
            ))}
          </span>
        )}
        {auth.allow_emails.length === 0 &&
          auth.allow_domains.length === 0 &&
          auth.allow_groups.length === 0 && <span>no allow list: any verified identity</span>}
      </div>
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" size="sm" asChild>
          <a href={link} target="_blank" rel="noreferrer">
            Open sign-in
          </a>
        </Button>
      </div>
    </section>
  );
}
