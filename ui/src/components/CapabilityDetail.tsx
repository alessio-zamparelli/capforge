import { useCallback, useEffect, useState } from "react";
import { RefreshCw, RotateCcw, Terminal } from "lucide-react";
import { toast } from "sonner";
import * as api from "@/api";
import { isAuthError } from "@/api";
import type { CapDetail as CapDetailInfo, LogTail } from "@/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Spinner } from "@/components/ui/spinner";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { StatusBadge } from "@/components/StatusBadge";

export function CapabilityDetail({
  name,
  onChanged,
  onAuthLost,
}: {
  name: string;
  onChanged: () => Promise<void>;
  onAuthLost: () => void;
}) {
  const [detail, setDetail] = useState<CapDetailInfo | null>(null);
  const [logKind, setLogKind] = useState<"pipeline" | "served">("pipeline");
  const [log, setLog] = useState<LogTail | null>(null);
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
      const next = await api.capDetail(name);
      setDetail(next);
      setLog(next.log);
      setError(null);
    } catch (err) {
      fail(err);
    }
  }, [name, onAuthLost]);

  useEffect(() => {
    void load();
  }, [load]);

  const refreshLog = async (kind: "pipeline" | "served" = logKind) => {
    setBusy("log");
    setError(null);
    try {
      setLog(await api.logs(name, kind));
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const rollback = async (tag: string) => {
    setBusy(`rollback:${tag}`);
    setError(null);
    try {
      await api.rollback(name, tag);
      toast.success(`Rolled back to ${tag}.`);
      await load();
      await onChanged();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  const pipeline = detail?.pipeline;
  const tags = detail?.tags ?? [];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
          <Terminal data-icon="inline-start" />
          Pipeline
          {pipeline?.state && (
            <StatusBadge warn={pipeline.state !== "promoted"}>{pipeline.state}</StatusBadge>
          )}
          {pipeline?.staging_id && (
            <span className="text-xs text-muted-foreground">{pipeline.staging_id}</span>
          )}
          {pipeline?.sha && (
            <code className="rounded bg-muted px-1.5 py-0.5 text-xs">
              {pipeline.sha.slice(0, 12)}
            </code>
          )}
        </div>
        {pipeline?.error && (
          <Alert variant="destructive">
            <AlertTitle>Pipeline error</AlertTitle>
            <AlertDescription>{pipeline.error}</AlertDescription>
          </Alert>
        )}
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>kind {detail?.kind ?? "cli"}</span>
          <span>data {detail?.data ?? "version"}</span>
          <span>draft {detail?.draft_changed ? "changed" : "current"}</span>
          {detail?.active_content_hash && <span>hash {detail.active_content_hash}</span>}
        </div>
      </div>

      <Separator />

      <div className="flex flex-col gap-2">
        <h3 className="text-sm font-medium">Tags</h3>
        {tags.length === 0 ? (
          <p className="text-xs text-muted-foreground">No tags yet.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Tag</TableHead>
                <TableHead>Hash</TableHead>
                <TableHead>Note</TableHead>
                <TableHead className="text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {[...tags].reverse().map((tag) => (
                <TableRow key={tag.tag}>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <code className="rounded bg-muted px-1.5 py-0.5 text-xs">{tag.tag}</code>
                      {tag.active && <Badge variant="secondary">active</Badge>}
                    </div>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {tag.content_hash ?? "—"}
                  </TableCell>
                  <TableCell className="max-w-64 truncate text-xs text-muted-foreground">
                    {tag.note || "—"}
                  </TableCell>
                  <TableCell className="text-right">
                    {!tag.active && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => void rollback(tag.tag)}
                        disabled={busy !== null}
                      >
                        {busy === `rollback:${tag.tag}` ? (
                          <Spinner data-icon="inline-start" />
                        ) : (
                          <RotateCcw data-icon="inline-start" />
                        )}
                        Roll back
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>

      <Separator />

      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-medium">Logs</h3>
          <Tabs
            value={logKind}
            onValueChange={(value) => {
              const kind = value as "pipeline" | "served";
              setLogKind(kind);
              void refreshLog(kind);
            }}
          >
            <TabsList>
              <TabsTrigger value="pipeline">pipeline</TabsTrigger>
              <TabsTrigger value="served">served</TabsTrigger>
            </TabsList>
          </Tabs>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void refreshLog()}
            disabled={busy !== null}
          >
            {busy === "log" ? (
              <Spinner data-icon="inline-start" />
            ) : (
              <RefreshCw data-icon="inline-start" />
            )}
            Refresh
          </Button>
        </div>
        <pre className="max-h-64 overflow-auto rounded-lg bg-muted p-3 text-xs whitespace-pre-wrap">
          {log?.text || "(no log yet)"}
        </pre>
        {log?.truncated && (
          <p className="text-xs text-muted-foreground">Showing the most recent tail.</p>
        )}
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertTitle>Action failed</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
    </div>
  );
}
