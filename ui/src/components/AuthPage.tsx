import { useCallback, useEffect, useState } from "react";
import { KeyRound, Pencil, Plus, RefreshCw, ShieldCheck, Trash2, Zap } from "lucide-react";
import { toast } from "sonner";
import * as api from "@/api";
import { isAuthError } from "@/api";
import type { OidcConfig, OidcProvider, OidcProviderInput } from "@/api";
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

const PROVIDER_ID_RE = /^[a-z0-9][a-z0-9_-]{0,39}$/;

interface ProviderDraft {
  id: string;
  label: string;
  issuer: string;
  client_id: string;
  scopes: string;
  token_auth: "basic" | "post";
  session_ttl: string;
  email_claim: string;
  name_claim: string;
  groups_claim: string;
}

function emptyDraft(): ProviderDraft {
  return {
    id: "",
    label: "",
    issuer: "",
    client_id: "",
    scopes: "openid email profile",
    token_auth: "basic",
    session_ttl: "28800",
    email_claim: "email",
    name_claim: "name",
    groups_claim: "groups",
  };
}

function draftFrom(provider: OidcProvider): ProviderDraft {
  return {
    id: provider.id,
    label: provider.label,
    issuer: provider.issuer,
    client_id: provider.client_id,
    scopes: provider.scopes.join(" "),
    token_auth: provider.token_auth,
    session_ttl: String(provider.session_ttl),
    email_claim: provider.email_claim,
    name_claim: provider.name_claim,
    groups_claim: provider.groups_claim,
  };
}

function parseScopes(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function AuthPage({ onAuthLost }: { onAuthLost: () => void }) {
  const [config, setConfig] = useState<OidcConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [editing, setEditing] = useState<ProviderDraft | null>(null);
  const [creating, setCreating] = useState(false);
  const [secretTarget, setSecretTarget] = useState<OidcProvider | null>(null);
  const [secretValue, setSecretValue] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<OidcProvider | null>(null);

  const fail = (err: unknown) => {
    if (isAuthError(err)) {
      onAuthLost();
      return;
    }
    setError(err instanceof Error ? err.message : String(err));
  };

  const refresh = useCallback(async () => {
    try {
      setConfig(await api.oidc());
      setError(null);
    } catch (err) {
      fail(err);
    }
  }, [onAuthLost]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const saveProvider = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!editing) return;
    const id = editing.id.trim();
    if (!PROVIDER_ID_RE.test(id)) {
      setError("Provider id must be lowercase letters, digits, '-' or '_' (max 40).");
      return;
    }
    const ttl = Number(editing.session_ttl);
    if (!Number.isInteger(ttl) || ttl < 300) {
      setError("Session lifetime must be an integer number of seconds (at least 300).");
      return;
    }
    const payload: OidcProviderInput = {
      label: editing.label.trim() || id,
      issuer: editing.issuer.trim(),
      client_id: editing.client_id.trim(),
      scopes: parseScopes(editing.scopes),
      token_auth: editing.token_auth,
      session_ttl: ttl,
      email_claim: editing.email_claim.trim() || "email",
      name_claim: editing.name_claim.trim() || "name",
      groups_claim: editing.groups_claim.trim() || "groups",
    };
    setBusy("provider");
    setError(null);
    try {
      const next = await api.saveOidcProvider(id, payload);
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

  const saveSecret = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!secretTarget || !secretValue.trim()) return;
    setBusy(`secret:${secretTarget.id}`);
    setError(null);
    try {
      await api.setOidcSecret(secretTarget.id, secretValue.trim());
      setSecretTarget(null);
      setSecretValue("");
      toast.success(`Stored the client secret for ${secretTarget.id}.`);
      await refresh();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const testProvider = async (provider: OidcProvider) => {
    setBusy(`test:${provider.id}`);
    setError(null);
    try {
      const result = await api.testOidcProvider(provider.id);
      if (result.ok) toast.success(`Discovery OK for ${result.issuer ?? provider.id}.`);
      else toast.error(result.error ?? "Discovery failed.");
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const removeProvider = async (provider: OidcProvider) => {
    setBusy(`delete:${provider.id}`);
    setError(null);
    try {
      const next = await api.deleteOidcProvider(provider.id);
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
          <h2 className="text-base font-semibold">Authentication</h2>
          <p className="text-sm text-muted-foreground">
            OIDC providers for served capabilities. A capability opts in with{" "}
            <code>AUTH</code> in <code>deploy.py</code>.
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
          <AlertTitle>Authentication request failed</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {config && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-sm">
              <ShieldCheck data-icon="inline-start" />
              Redirect URI
            </CardTitle>
            <CardDescription>
              Register this exact URI with every provider. It is derived from{" "}
              <code>CAPFORGE_PUBLIC_URL</code>.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <code className="block overflow-auto rounded bg-muted p-2 text-xs">
              {config.redirect_uri}
            </code>
          </CardContent>
        </Card>
      )}

      {config === null ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      ) : providers.length === 0 ? (
        <Empty>
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <ShieldCheck />
            </EmptyMedia>
            <EmptyTitle>No OIDC providers</EmptyTitle>
            <EmptyDescription>
              Add an issuer and client, then reference the provider id from a capability's{" "}
              <code>deploy.py</code>.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Provider</TableHead>
                <TableHead>Issuer</TableHead>
                <TableHead>Client</TableHead>
                <TableHead>Secret</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {providers.map((provider) => (
                <TableRow key={provider.id}>
                  <TableCell>
                    <div className="flex flex-col gap-0.5">
                      <span className="font-medium">{provider.label}</span>
                      <code className="text-xs text-muted-foreground">{provider.id}</code>
                    </div>
                  </TableCell>
                  <TableCell className="max-w-64 truncate text-xs text-muted-foreground">
                    {provider.issuer}
                  </TableCell>
                  <TableCell className="max-w-48 truncate text-xs text-muted-foreground">
                    {provider.client_id}
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant={
                        provider.client_secret_configured ? "secondary" : "destructive"
                      }
                    >
                      {provider.client_secret_configured ? "set" : "missing"}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex flex-wrap justify-end gap-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={busy !== null}
                        onClick={() => setSecretTarget(provider)}
                      >
                        <KeyRound data-icon="inline-start" />
                        Secret
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
            <DialogTitle>{creating ? "New OIDC provider" : `Edit ${editing?.id}`}</DialogTitle>
            <DialogDescription>
              Discovery is read from <code>/.well-known/openid-configuration</code>. The
              client secret is optional for public clients using PKCE.
            </DialogDescription>
          </DialogHeader>
          {editing && (
            <form onSubmit={saveProvider}>
              <FieldGroup>
                <Field>
                  <FieldLabel htmlFor="oidc-id">Provider id</FieldLabel>
                  <Input
                    id="oidc-id"
                    value={editing.id}
                    disabled={!creating}
                    onChange={(event) =>
                      setEditing({ ...editing, id: event.target.value })
                    }
                    placeholder="corp"
                    autoComplete="off"
                  />
                </Field>
                <Field>
                  <FieldLabel htmlFor="oidc-label">Label</FieldLabel>
                  <Input
                    id="oidc-label"
                    value={editing.label}
                    onChange={(event) =>
                      setEditing({ ...editing, label: event.target.value })
                    }
                    placeholder="Corporate SSO"
                    autoComplete="off"
                  />
                </Field>
                <Field>
                  <FieldLabel htmlFor="oidc-issuer">Issuer</FieldLabel>
                  <Input
                    id="oidc-issuer"
                    value={editing.issuer}
                    onChange={(event) =>
                      setEditing({ ...editing, issuer: event.target.value })
                    }
                    placeholder="https://login.example.com"
                    autoComplete="off"
                  />
                </Field>
                <Field>
                  <FieldLabel htmlFor="oidc-client">Client id</FieldLabel>
                  <Input
                    id="oidc-client"
                    value={editing.client_id}
                    onChange={(event) =>
                      setEditing({ ...editing, client_id: event.target.value })
                    }
                    autoComplete="off"
                  />
                </Field>
                <Field>
                  <FieldLabel htmlFor="oidc-scopes">Scopes</FieldLabel>
                  <Input
                    id="oidc-scopes"
                    value={editing.scopes}
                    onChange={(event) =>
                      setEditing({ ...editing, scopes: event.target.value })
                    }
                    placeholder="openid email profile"
                    autoComplete="off"
                  />
                  <FieldDescription>
                    Space or comma separated. <code>openid</code> is required.
                  </FieldDescription>
                </Field>
                <Field>
                  <FieldLabel htmlFor="oidc-token-auth">Client authentication</FieldLabel>
                  <Select
                    value={editing.token_auth}
                    onValueChange={(value) =>
                      setEditing({ ...editing, token_auth: value as "basic" | "post" })
                    }
                  >
                    <SelectTrigger id="oidc-token-auth">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="basic">HTTP Basic (client_secret_basic)</SelectItem>
                      <SelectItem value="post">Form post (client_secret_post)</SelectItem>
                    </SelectContent>
                  </Select>
                </Field>
                <Field>
                  <FieldLabel htmlFor="oidc-ttl">Session lifetime (seconds)</FieldLabel>
                  <Input
                    id="oidc-ttl"
                    type="number"
                    min={300}
                    value={editing.session_ttl}
                    onChange={(event) =>
                      setEditing({ ...editing, session_ttl: event.target.value })
                    }
                  />
                </Field>
                <div className="grid gap-3 sm:grid-cols-3">
                  <Field>
                    <FieldLabel htmlFor="oidc-email">Email claim</FieldLabel>
                    <Input
                      id="oidc-email"
                      value={editing.email_claim}
                      onChange={(event) =>
                        setEditing({ ...editing, email_claim: event.target.value })
                      }
                      autoComplete="off"
                    />
                  </Field>
                  <Field>
                    <FieldLabel htmlFor="oidc-name">Name claim</FieldLabel>
                    <Input
                      id="oidc-name"
                      value={editing.name_claim}
                      onChange={(event) =>
                        setEditing({ ...editing, name_claim: event.target.value })
                      }
                      autoComplete="off"
                    />
                  </Field>
                  <Field>
                    <FieldLabel htmlFor="oidc-groups">Groups claim</FieldLabel>
                    <Input
                      id="oidc-groups"
                      value={editing.groups_claim}
                      onChange={(event) =>
                        setEditing({ ...editing, groups_claim: event.target.value })
                      }
                      autoComplete="off"
                    />
                  </Field>
                </div>
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
        open={secretTarget !== null}
        onOpenChange={(open) => {
          if (!open) {
            setSecretTarget(null);
            setSecretValue("");
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Client secret for {secretTarget?.id}</DialogTitle>
            <DialogDescription>
              Write-only. Leave it unset for a public client that relies on PKCE alone.
            </DialogDescription>
          </DialogHeader>
          <form onSubmit={saveSecret}>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="oidc-secret">Secret</FieldLabel>
                <Input
                  id="oidc-secret"
                  type="password"
                  autoComplete="new-password"
                  value={secretValue}
                  onChange={(event) => setSecretValue(event.target.value)}
                />
              </Field>
            </FieldGroup>
            <DialogFooter className="mt-4">
              <Button
                type="button"
                variant="outline"
                onClick={() => setSecretTarget(null)}
                disabled={busy?.startsWith("secret:") ?? false}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={(busy?.startsWith("secret:") ?? false) || !secretValue.trim()}
              >
                {busy?.startsWith("secret:") && <Spinner data-icon="inline-start" />}
                Save secret
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        title={`Delete ${deleteTarget?.id ?? "provider"}?`}
        description="The provider and its client secret are removed. Capabilities that reference it will refuse to serve."
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
