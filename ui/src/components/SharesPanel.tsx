import { useState } from "react";
import { FolderKey, ShieldAlert } from "lucide-react";
import { toast } from "sonner";
import * as api from "@/api";
import { isAuthError } from "@/api";
import type { ShareStatus } from "@/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Spinner } from "@/components/ui/spinner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ShareAccessBadge } from "@/components/StatusBadge";

export function SharesPanel({
  name,
  shares,
  onChanged,
  onAuthLost,
}: {
  name: string;
  shares: ShareStatus[];
  onChanged: () => Promise<void>;
  onAuthLost: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<string | null>(null);

  const fail = (err: unknown) => {
    if (isAuthError(err)) {
      onAuthLost();
      return;
    }
    setError(err instanceof Error ? err.message : String(err));
  };

  const restartMessage = (result: api.ShareChange, verb: string) =>
    result.restart_error
      ? `${verb}, but the web instance did not restart: ${result.restart_error}`
      : result.restarted
        ? `${verb}; the web instance was restarted.`
        : `${verb}.`;

  const approve = async (folder: string, access: "ro" | "rw") => {
    setBusy(`${folder}:${access}`);
    setError(null);
    try {
      const result = await api.approveShare(name, folder, access);
      toast.success(restartMessage(result, `Approved ${folder} as ${access}`));
      await onChanged();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const revoke = async (folder: string) => {
    setBusy(`${folder}:revoke`);
    setError(null);
    try {
      const result = await api.revokeShare(name, folder);
      toast.success(restartMessage(result, `Revoked ${folder}`));
      setRevokeTarget(null);
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
        <FolderKey data-icon="inline-start" />
        Shared folders
      </div>
      <p className="text-xs text-muted-foreground">
        <code>deploy.py</code> declares a request; the operator approves the actual access
        here. A change restarts a running web instance because the environment and Landlock
        policy are fixed at spawn.
      </p>

      <Alert>
        <ShieldAlert data-icon="inline-start" />
        <AlertTitle>Read-write is mutual trust</AlertTitle>
        <AlertDescription>
          Every capability granted <code>rw</code> can modify or delete everything in the
          folder. Use <code>ro</code> unless the capability must write.
        </AlertDescription>
      </Alert>

      {error && (
        <Alert variant="destructive">
          <AlertTitle>Share change failed</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {shares.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          This capability does not declare any shared folders.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          {shares.map((share) => (
            <div
              key={share.name}
              className="flex flex-col gap-2 rounded-lg border p-3 text-sm"
            >
              <div className="flex flex-wrap items-center gap-2">
                <code className="rounded bg-muted px-1.5 py-0.5 text-xs">{share.name}</code>
                <Badge variant="outline">requested {share.requested ?? "none"}</Badge>
                <Badge variant={share.state === "pending" ? "destructive" : "secondary"}>
                  {share.state}
                </Badge>
                {share.state === "approved" && <ShareAccessBadge access={share.access} />}
                {share.weaker && <Badge variant="destructive">weaker than requested</Badge>}
              </div>

              {share.state === "pending" && share.requested && (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs text-muted-foreground">
                    The capability cannot run until this request is approved.
                  </span>
                  <Button
                    size="sm"
                    onClick={() => void approve(share.name, share.requested!)}
                    disabled={busy !== null}
                  >
                    {busy === `${share.name}:${share.requested}` && (
                      <Spinner data-icon="inline-start" />
                    )}
                    Approve {share.requested}
                  </Button>
                </div>
              )}

              {share.state === "approved" && share.weaker && share.requested && (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs text-destructive">
                    Approved read-only, but the capability requires read-write.
                  </span>
                  <Button
                    size="sm"
                    onClick={() => void approve(share.name, share.requested!)}
                    disabled={busy !== null}
                  >
                    {busy === `${share.name}:${share.requested}` && (
                      <Spinner data-icon="inline-start" />
                    )}
                    Approve read-write
                  </Button>
                </div>
              )}

              {share.state === "unused" && (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs text-muted-foreground">
                    Granted, but no longer declared by the draft or active tag.
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setRevokeTarget(share.name)}
                    disabled={busy !== null}
                  >
                    Revoke
                  </Button>
                </div>
              )}

              {share.state === "approved" && !share.weaker && (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs text-muted-foreground">
                    Approved {formatTime(share.approved_at)}.
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setRevokeTarget(share.name)}
                    disabled={busy !== null}
                  >
                    Revoke
                  </Button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      <Separator />
      <ConfirmDialog
        open={revokeTarget !== null}
        onOpenChange={(open) => !open && setRevokeTarget(null)}
        title={`Revoke ${revokeTarget ?? "share"}?`}
        description={
          <>
            The capability loses access to <code>{revokeTarget}</code> on its next run.
            The folder and its data remain on disk.
          </>
        }
        confirmLabel="Revoke"
        busy={busy === `${revokeTarget}:revoke`}
        onConfirm={() => revoke(revokeTarget!)}
      />
    </section>
  );
}

function formatTime(value: number | null): string {
  if (!value) return "just now";
  return new Date(value * 1000).toLocaleString();
}
