import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bot,
  KeyRound,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
  Zap,
} from "lucide-react";
import { toast } from "sonner";
import * as api from "@/api";
import { isAuthError } from "@/api";
import type { LlmAuth, LlmConfig, LlmProvider } from "@/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
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
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";

const PROVIDER_ID_RE = /^[a-z0-9][a-z0-9_-]{0,39}$/;

interface ProviderDraft {
  id: string;
  label: string;
  base_url: string;
  auth: LlmAuth;
  header_name: string;
  models: string;
}

function emptyDraft(): ProviderDraft {
  return {
    id: "",
    label: "",
    base_url: "",
    auth: "bearer",
    header_name: "Authorization",
    models: "",
  };
}

function draftFrom(provider: LlmProvider): ProviderDraft {
  return {
    id: provider.id,
    label: provider.label,
    base_url: provider.base_url,
    auth: provider.auth,
    header_name: provider.header_name,
    models: provider.models.map((model) => model.id).join("\n"),
  };
}

function parseModels(text: string): string[] {
  return text
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function ModelsPage({ onAuthLost }: { onAuthLost: () => void }) {
  const [config, setConfig] = useState<LlmConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [editing, setEditing] = useState<ProviderDraft | null>(null);
  const [creating, setCreating] = useState(false);
  const [keyTarget, setKeyTarget] = useState<LlmProvider | null>(null);
  const [keyValue, setKeyValue] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<LlmProvider | null>(null);
  const [defaultProvider, setDefaultProvider] = useState("");
  const [defaultModel, setDefaultModel] = useState("");

  const fail = (err: unknown) => {
    if (isAuthError(err)) {
      onAuthLost();
      return;
    }
    setError(err instanceof Error ? err.message : String(err));
  };

  const refresh = useCallback(async () => {
    try {
      const next = await api.llm();
      setConfig(next);
      setDefaultProvider(next.default?.provider ?? "");
      setDefaultModel(next.default?.model ?? "");
      setError(null);
    } catch (err) {
      fail(err);
    }
  }, [onAuthLost]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const modelsForDefault = useMemo(() => {
    const provider = config?.providers.find((item) => item.id === defaultProvider);
    return provider?.models ?? [];
  }, [config, defaultProvider]);

  const saveDefault = async () => {
    if (!defaultProvider || !defaultModel) return;
    setBusy("default");
    setError(null);
    try {
      const next = await api.setLlmDefault(defaultProvider, defaultModel);
      setConfig(next);
      toast.success("Default model saved.");
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const saveProvider = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!editing) return;
    const id = editing.id.trim();
    if (!PROVIDER_ID_RE.test(id)) {
      setError("Provider id must be lowercase letters, digits, '-' or '_' (max 40).");
      return;
    }
    const models = parseModels(editing.models);
    if (models.length === 0) {
      setError("Add at least one model id.");
      return;
    }
    setBusy("provider");
    setError(null);
    try {
      const next = await api.saveLlmProvider(id, {
        label: editing.label.trim() || id,
        base_url: editing.base_url.trim(),
        auth: editing.auth,
        header_name: editing.header_name.trim() || "Authorization",
        models,
      });
      setConfig(next);
      setEditing(null);
      setCreating(false);
      toast.success(`Saved provider ${id}.`);
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const saveKey = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!keyTarget || !keyValue.trim()) return;
    setBusy(`key:${keyTarget.id}`);
    setError(null);
    try {
      await api.setLlmSecret(keyTarget.id, keyValue.trim());
      setKeyTarget(null);
      setKeyValue("");
      toast.success(`Stored the API key for ${keyTarget.id}.`);
      await refresh();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const testProvider = async (provider: LlmProvider) => {
    setBusy(`test:${provider.id}`);
    setError(null);
    try {
      const result = await api.testLlmProvider(provider.id);
      if (result.ok) toast.success(`${provider.id} answered ${result.status ?? ""}.`);
      else toast.error(result.error ?? "Provider test failed.");
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const removeProvider = async (provider: LlmProvider) => {
    setBusy(`delete:${provider.id}`);
    setError(null);
    try {
      const next = await api.deleteLlmProvider(provider.id);
      setConfig(next);
      setDeleteTarget(null);
      toast.success(`Deleted provider ${provider.id}.`);
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const providers = config?.providers ?? [];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold">Models</h2>
          <p className="text-sm text-muted-foreground">
            OpenAI-compatible providers. Capabilities call the hub gateway and never see a
            provider key.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => void refresh()}>
            <RefreshCw data-icon="inline-start" />
            Refresh
          </Button>
          <Button
            size="sm"
            onClick={() => {
              setEditing(emptyDraft());
              setCreating(true);
            }}
          >
            <Plus data-icon="inline-start" />
            New provider
          </Button>
        </div>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertTitle>Models request failed</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-sm">
            <Zap data-icon="inline-start" />
            Default model
          </CardTitle>
          <CardDescription>
            Pre-selected for every capability that declares <code>LLM = True</code>. You can
            override it per capability.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {providers.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Add a provider before choosing a default model.
            </p>
          ) : (
            <div className="flex flex-wrap items-end gap-2">
              <Field className="min-w-48">
                <FieldLabel htmlFor="llm-default-provider">Provider</FieldLabel>
                <Select
                  value={defaultProvider}
                  onValueChange={(value) => {
                    setDefaultProvider(value);
                    const provider = providers.find((item) => item.id === value);
                    setDefaultModel(provider?.models[0]?.id ?? "");
                  }}
                >
                  <SelectTrigger id="llm-default-provider">
                    <SelectValue placeholder="choose…" />
                  </SelectTrigger>
                  <SelectContent>
                    {providers.map((provider) => (
                      <SelectItem key={provider.id} value={provider.id}>
                        {provider.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
              <Field className="min-w-48">
                <FieldLabel htmlFor="llm-default-model">Model</FieldLabel>
                <Select value={defaultModel} onValueChange={setDefaultModel}>
                  <SelectTrigger id="llm-default-model">
                    <SelectValue placeholder="choose…" />
                  </SelectTrigger>
                  <SelectContent>
                    {modelsForDefault.map((model) => (
                      <SelectItem key={model.id} value={model.id}>
                        {model.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
              <Button
                size="sm"
                onClick={() => void saveDefault()}
                disabled={busy !== null || !defaultProvider || !defaultModel}
              >
                {busy === "default" && <Spinner data-icon="inline-start" />}
                Save default
              </Button>
            </div>
          )}
        </CardContent>
      </Card>

      {config === null ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      ) : providers.length === 0 ? (
        <Empty>
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <Bot />
            </EmptyMedia>
            <EmptyTitle>No model providers</EmptyTitle>
            <EmptyDescription>
              Add an OpenAI-compatible endpoint, then select the default model.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Provider</TableHead>
                <TableHead>Endpoint</TableHead>
                <TableHead>Models</TableHead>
                <TableHead>API key</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {providers.map((provider) => (
                <TableRow key={provider.id}>
                  <TableCell>
                    <div className="flex flex-col gap-0.5">
                      <span className="flex items-center gap-2 font-medium">
                        {provider.label}
                        {config.default?.provider === provider.id && (
                          <Badge variant="secondary">default</Badge>
                        )}
                      </span>
                      <code className="text-xs text-muted-foreground">{provider.id}</code>
                    </div>
                  </TableCell>
                  <TableCell className="max-w-64 truncate text-xs text-muted-foreground">
                    {provider.base_url}
                  </TableCell>
                  <TableCell>
                    <div className="flex max-w-64 flex-wrap gap-1">
                      {provider.models.slice(0, 4).map((model) => (
                        <Badge key={model.id} variant="outline">
                          {model.id}
                        </Badge>
                      ))}
                      {provider.models.length > 4 && (
                        <Badge variant="outline">+{provider.models.length - 4}</Badge>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant={provider.api_key_configured ? "secondary" : "destructive"}
                    >
                      {provider.api_key_configured ? "set" : "missing"}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex flex-wrap justify-end gap-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={busy !== null}
                        onClick={() => setKeyTarget(provider)}
                      >
                        <KeyRound data-icon="inline-start" />
                        Key
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={busy !== null}
                        onClick={() => void testProvider(provider)}
                      >
                        {busy === `test:${provider.id}` ? (
                          <Spinner data-icon="inline-start" />
                        ) : (
                          <Zap data-icon="inline-start" />
                        )}
                        Test
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={busy !== null}
                        onClick={() => {
                          setEditing(draftFrom(provider));
                          setCreating(false);
                        }}
                      >
                        <Pencil data-icon="inline-start" />
                        Edit
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={busy !== null}
                        onClick={() => setDeleteTarget(provider)}
                      >
                        <Trash2 data-icon="inline-start" />
                        Delete
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <Dialog
        open={editing !== null}
        onOpenChange={(open) => {
          if (!open) setEditing(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{creating ? "New provider" : `Edit ${editing?.id}`}</DialogTitle>
            <DialogDescription>
              Only OpenAI-compatible endpoints are supported. The gateway forwards the path
              after <code>/v1</code>.
            </DialogDescription>
          </DialogHeader>
          {editing && (
            <form onSubmit={saveProvider}>
              <FieldGroup>
                <Field>
                  <FieldLabel htmlFor="llm-id">Provider id</FieldLabel>
                  <Input
                    id="llm-id"
                    value={editing.id}
                    disabled={!creating}
                    onChange={(event) =>
                      setEditing({ ...editing, id: event.target.value })
                    }
                    placeholder="openai"
                    autoComplete="off"
                  />
                </Field>
                <Field>
                  <FieldLabel htmlFor="llm-label">Label</FieldLabel>
                  <Input
                    id="llm-label"
                    value={editing.label}
                    onChange={(event) =>
                      setEditing({ ...editing, label: event.target.value })
                    }
                    placeholder="OpenAI"
                    autoComplete="off"
                  />
                </Field>
                <Field>
                  <FieldLabel htmlFor="llm-base-url">Base URL</FieldLabel>
                  <Input
                    id="llm-base-url"
                    value={editing.base_url}
                    onChange={(event) =>
                      setEditing({ ...editing, base_url: event.target.value })
                    }
                    placeholder="https://api.openai.com/v1"
                    autoComplete="off"
                  />
                  <FieldDescription>
                    Must be https unless it is a loopback address.
                  </FieldDescription>
                </Field>
                <Field>
                  <FieldLabel htmlFor="llm-auth">Authentication</FieldLabel>
                  <Select
                    value={editing.auth}
                    onValueChange={(value) =>
                      setEditing({ ...editing, auth: value as LlmAuth })
                    }
                  >
                    <SelectTrigger id="llm-auth">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="bearer">Authorization: Bearer</SelectItem>
                      <SelectItem value="header">Custom header</SelectItem>
                      <SelectItem value="none">No key</SelectItem>
                    </SelectContent>
                  </Select>
                </Field>
                {editing.auth === "header" && (
                  <Field>
                    <FieldLabel htmlFor="llm-header">Header name</FieldLabel>
                    <Input
                      id="llm-header"
                      value={editing.header_name}
                      onChange={(event) =>
                        setEditing({ ...editing, header_name: event.target.value })
                      }
                      placeholder="x-api-key"
                      autoComplete="off"
                    />
                  </Field>
                )}
                <Field>
                  <FieldLabel htmlFor="llm-models">Models</FieldLabel>
                  <Textarea
                    id="llm-models"
                    value={editing.models}
                    onChange={(event) =>
                      setEditing({ ...editing, models: event.target.value })
                    }
                    placeholder={"gpt-4o-mini\ngpt-4.1"}
                    rows={4}
                  />
                  <FieldDescription>
                    One model id per line. The id is sent to the provider unchanged.
                  </FieldDescription>
                </Field>
              </FieldGroup>
              <DialogFooter className="mt-4">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setEditing(null)}
                  disabled={busy === "provider"}
                >
                  Cancel
                </Button>
                <Button type="submit" disabled={busy === "provider"}>
                  {busy === "provider" && <Spinner data-icon="inline-start" />}
                  Save provider
                </Button>
              </DialogFooter>
            </form>
          )}
        </DialogContent>
      </Dialog>

      <Dialog
        open={keyTarget !== null}
        onOpenChange={(open) => {
          if (!open) {
            setKeyTarget(null);
            setKeyValue("");
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>API key for {keyTarget?.id}</DialogTitle>
            <DialogDescription>
              Write-only. The key is stored under the state directory and injected by the
              gateway, never handed to capability code.
            </DialogDescription>
          </DialogHeader>
          <form onSubmit={saveKey}>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="llm-key">Key</FieldLabel>
                <Input
                  id="llm-key"
                  type="password"
                  autoComplete="new-password"
                  value={keyValue}
                  onChange={(event) => setKeyValue(event.target.value)}
                  placeholder="sk-…"
                />
              </Field>
            </FieldGroup>
            <DialogFooter className="mt-4">
              <Button
                type="button"
                variant="outline"
                onClick={() => setKeyTarget(null)}
                disabled={busy?.startsWith("key:") ?? false}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={(busy?.startsWith("key:") ?? false) || !keyValue.trim()}
              >
                {busy?.startsWith("key:") && <Spinner data-icon="inline-start" />}
                Save key
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        title={`Delete ${deleteTarget?.id ?? "provider"}?`}
        description="The provider, its key and its model list are removed. Capabilities that selected it fall back to the default."
        confirmLabel="Delete provider"
        confirmText={deleteTarget?.id}
        busy={deleteTarget !== null && busy === `delete:${deleteTarget.id}`}
        onConfirm={() => {
          if (deleteTarget) return removeProvider(deleteTarget);
        }}
      />
    </div>
  );
}
