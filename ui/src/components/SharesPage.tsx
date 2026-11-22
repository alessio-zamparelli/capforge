import { Fragment, useCallback, useEffect, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  FolderPlus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import * as api from "@/api";
import { isAuthError } from "@/api";
import type { ShareFolder, ShareRequest } from "@/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
  EmptyContent,
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
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ShareAccessBadge } from "@/components/StatusBadge";

const POLL_MS = 5000;

export function SharesPage({ onAuthLost }: { onAuthLost: () => void }) {
  const [folders, setFolders] = useState<ShareFolder[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<ShareFolder | null>(null);

  const fail = (err: unknown) => {
    if (isAuthError(err)) {
      onAuthLost();
      return;
    }
    setError(err instanceof Error ? err.message : String(err));
  };

  const refresh = useCallback(async () => {
    try {
      setFolders(await api.shares());
      setError(null);
    } catch (err) {
      fail(err);
    }
  }, [onAuthLost]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const toggle = (folder: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(folder)) next.delete(folder);
      else next.add(folder);
      return next;
    });
  };

  const restartMessage = (result: api.ShareChange, verb: string) =>
    result.restart_error
      ? `${verb}, but the web instance did not restart: ${result.restart_error}`
      : result.restarted
        ? `${verb}; the web instance was restarted.`
        : `${verb}.`;

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy("create");
    setError(null);
    try {
      await api.createShare(name, note);
      toast.success(`Created shared folder ${name}.`);
      setCreateOpen(false);
      setName("");
      setNote("");
      await refresh();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const approve = async (folder: string, request: ShareRequest) => {
    if (!request.requested) return;
    setBusy(`${folder}:${request.cap}:approve`);
    setError(null);
    try {
      const result = await api.approveShare(request.cap, folder, request.requested);
      toast.success(
        restartMessage(result, `Approved ${request.cap} as ${request.requested}`)
      );
      await refresh();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const revoke = async (folder: string, cap: string) => {
    setBusy(`${folder}:${cap}:revoke`);
    setError(null);
    try {
      const result = await api.revokeShare(cap, folder);
      toast.success(restartMessage(result, `Revoked ${cap}`));
      await refresh();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const remove = async (folder: ShareFolder) => {
    setBusy(`${folder.name}:delete`);
    setError(null);
    try {
      await api.deleteShare(folder.name, folder.name);
      toast.success(`Deleted shared folder ${folder.name}.`);
      setDeleteTarget(null);
      await refresh();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const grantCount = folders?.reduce((total, folder) => total + folder.grants.length, 0) ?? 0;
  const pendingCount =
    folders?.reduce(
      (total, folder) =>
        total + folder.requests.filter((request) => request.state === "pending").length,
      0
    ) ?? 0;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold">Shared folders</h2>
          <p className="text-sm text-muted-foreground">
            Operator-owned volumes. A capability requests access in <code>deploy.py</code>;
            nothing is mounted until you approve it here.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => void refresh()}>
            <RefreshCw data-icon="inline-start" />
            Refresh
          </Button>
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <FolderPlus data-icon="inline-start" />
            New folder
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
        <Badge variant="outline">{folders?.length ?? 0} folders</Badge>
        <Badge variant="outline">{grantCount} grants</Badge>
        <Badge variant={pendingCount > 0 ? "destructive" : "outline"}>
          {pendingCount} pending
        </Badge>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertTitle>Shared folders unavailable</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {folders === null ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
        </div>
      ) : folders.length === 0 ? (
        <Empty>
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <FolderPlus />
            </EmptyMedia>
            <EmptyTitle>No shared folders</EmptyTitle>
            <EmptyDescription>
              Create one here, or approve a capability's declaration to create it automatically.
            </EmptyDescription>
          </EmptyHeader>
          <EmptyContent>
            <Button size="sm" onClick={() => setCreateOpen(true)}>
              <FolderPlus data-icon="inline-start" />
              New folder
            </Button>
          </EmptyContent>
        </Empty>
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8" />
                <TableHead>Folder</TableHead>
                <TableHead>Size</TableHead>
                <TableHead>Access</TableHead>
                <TableHead>Pending</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {folders.map((folder) => {
                const open = expanded.has(folder.name);
                const pending = folder.requests.filter(
                  (request) => request.state === "pending"
                ).length;
                return (
                  <Fragment key={folder.name}>
                    <TableRow>
                      <TableCell>
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          aria-label={open ? "Collapse" : "Expand"}
                          onClick={() => toggle(folder.name)}
                        >
                          {open ? <ChevronDown /> : <ChevronRight />}
                        </Button>
                      </TableCell>
                      <TableCell className="max-w-[22rem]">
                        <div className="flex flex-col gap-0.5">
                          <span className="font-medium">{folder.name}</span>
                          <code className="truncate text-xs text-muted-foreground">
                            {folder.path}
                          </code>
                          {!folder.exists && <Badge variant="destructive">not created</Badge>}
                        </div>
                      </TableCell>
                      <TableCell>
                        {formatSize(folder.size_bytes)}
                        {folder.size_truncated && <span className="text-muted-foreground">+</span>}
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-wrap gap-1">
                          {folder.grants.length === 0 ? (
                            <Badge variant="outline">no grants</Badge>
                          ) : (
                            folder.grants.map((grant) => (
                              <div key={grant.cap} className="flex items-center gap-1">
                                <Badge variant="secondary">{grant.cap}</Badge>
                                <ShareAccessBadge access={grant.access} />
                              </div>
                            ))
                          )}
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge variant={pending > 0 ? "destructive" : "outline"}>
                          {pending}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right">
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span>
                              <Button
                                variant="destructive"
                                size="sm"
                                disabled={folder.grants.length > 0 || busy !== null}
                                onClick={() => setDeleteTarget(folder)}
                              >
                                <Trash2 data-icon="inline-start" />
                                Delete
                              </Button>
                            </span>
                          </TooltipTrigger>
                          <TooltipContent>
                            {folder.grants.length > 0
                              ? "Revoke every grant first"
                              : "Delete this empty folder"}
                          </TooltipContent>
                        </Tooltip>
                      </TableCell>
                    </TableRow>
                    {open && (
                      <TableRow>
                        <TableCell colSpan={6} className="bg-muted/30">
                          <div className="flex flex-col gap-4 p-2">
                            {folder.note && (
                              <p className="text-sm text-muted-foreground">{folder.note}</p>
                            )}
                            <div>
                              <h3 className="mb-2 text-sm font-medium">Grants</h3>
                              {folder.grants.length === 0 ? (
                                <p className="text-sm text-muted-foreground">No grants yet.</p>
                              ) : (
                                <Table>
                                  <TableHeader>
                                    <TableRow>
                                      <TableHead>Capability</TableHead>
                                      <TableHead>Access</TableHead>
                                      <TableHead>Approved</TableHead>
                                      <TableHead className="text-right">Action</TableHead>
                                    </TableRow>
                                  </TableHeader>
                                  <TableBody>
                                    {folder.grants.map((grant) => (
                                      <TableRow key={grant.cap}>
                                        <TableCell>{grant.cap}</TableCell>
                                        <TableCell>
                                          <ShareAccessBadge access={grant.access} />
                                        </TableCell>
                                        <TableCell>{formatTime(grant.approved_at)}</TableCell>
                                        <TableCell className="text-right">
                                          <Button
                                            variant="outline"
                                            size="sm"
                                            disabled={busy !== null}
                                            onClick={() => void revoke(folder.name, grant.cap)}
                                          >
                                            {busy === `${folder.name}:${grant.cap}:revoke` && (
                                              <Spinner data-icon="inline-start" />
                                            )}
                                            Revoke
                                          </Button>
                                        </TableCell>
                                      </TableRow>
                                    ))}
                                  </TableBody>
                                </Table>
                              )}
                            </div>

                            <div>
                              <h3 className="mb-2 text-sm font-medium">Requests</h3>
                              {folder.requests.length === 0 ? (
                                <p className="text-sm text-muted-foreground">
                                  No capability requests this folder.
                                </p>
                              ) : (
                                <Table>
                                  <TableHeader>
                                    <TableRow>
                                      <TableHead>Capability</TableHead>
                                      <TableHead>Requested</TableHead>
                                      <TableHead>Granted</TableHead>
                                      <TableHead>State</TableHead>
                                      <TableHead className="text-right">Action</TableHead>
                                    </TableRow>
                                  </TableHeader>
                                  <TableBody>
                                    {folder.requests.map((request) => (
                                      <TableRow key={request.cap}>
                                        <TableCell>{request.cap}</TableCell>
                                        <TableCell>
                                          <ShareAccessBadge access={request.requested} />
                                        </TableCell>
                                        <TableCell>
                                          <ShareAccessBadge access={request.access} />
                                        </TableCell>
                                        <TableCell>
                                          <Badge
                                            variant={
                                              request.state === "pending"
                                                ? "destructive"
                                                : "secondary"
                                            }
                                          >
                                            {request.state}
                                          </Badge>
                                        </TableCell>
                                        <TableCell className="text-right">
                                          {request.state === "pending" && request.requested ? (
                                            <Button
                                              size="sm"
                                              disabled={busy !== null}
                                              onClick={() => void approve(folder.name, request)}
                                            >
                                              {busy ===
                                                `${folder.name}:${request.cap}:approve` && (
                                                <Spinner data-icon="inline-start" />
                                              )}
                                              Approve {request.requested}
                                            </Button>
                                          ) : request.state === "unused" ? (
                                            <Button
                                              variant="outline"
                                              size="sm"
                                              disabled={busy !== null}
                                              onClick={() =>
                                                void revoke(folder.name, request.cap)
                                              }
                                            >
                                              Revoke
                                            </Button>
                                          ) : (
                                            <span className="text-xs text-muted-foreground">
                                              approved
                                            </span>
                                          )}
                                        </TableCell>
                                      </TableRow>
                                    ))}
                                  </TableBody>
                                </Table>
                              )}
                            </div>
                          </div>
                        </TableCell>
                      </TableRow>
                    )}
                  </Fragment>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>New shared folder</DialogTitle>
            <DialogDescription>
              A folder is just an allowlisted directory under the capforge state directory.
              Approving a capability's request creates it automatically if needed.
            </DialogDescription>
          </DialogHeader>
          <form onSubmit={create}>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="share-name">Name</FieldLabel>
                <Input
                  id="share-name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="datasets"
                  autoComplete="off"
                />
                <FieldDescription>
                  Lowercase letters, digits, <code>-</code> and <code>_</code>; max 64
                  characters.
                </FieldDescription>
              </Field>
              <Field>
                <FieldLabel htmlFor="share-note">Note (optional)</FieldLabel>
                <Textarea
                  id="share-note"
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                  placeholder="What belongs in this folder?"
                  rows={3}
                />
              </Field>
            </FieldGroup>
            <DialogFooter className="mt-4">
              <Button
                type="button"
                variant="outline"
                onClick={() => setCreateOpen(false)}
                disabled={busy === "create"}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={busy === "create" || !name}>
                {busy === "create" && <Spinner data-icon="inline-start" />}
                Create
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        title={`Delete ${deleteTarget?.name ?? "folder"}?`}
        description={
          <>
            This permanently removes the folder and everything inside it. It is refused while
            any grant exists, so revoke every capability first.
          </>
        }
        confirmLabel="Delete folder"
        confirmText={deleteTarget?.name}
        busy={deleteTarget !== null && busy === `${deleteTarget.name}:delete`}
        onConfirm={() => {
          if (deleteTarget) return remove(deleteTarget);
        }}
      />
    </div>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[index]}`;
}

function formatTime(value: number): string {
  if (!value) return "—";
  return new Date(value * 1000).toLocaleString();
}
