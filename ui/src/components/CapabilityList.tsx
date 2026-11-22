import { useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  ExternalLink,
  Globe,
  Play,
  RefreshCw,
  Square,
  Terminal,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import * as api from "@/api";
import { isAuthError } from "@/api";
import type { CapInfo } from "@/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardHeader,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Spinner } from "@/components/ui/spinner";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { CapabilityDetail } from "@/components/CapabilityDetail";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EnvPanel } from "@/components/EnvPanel";
import { SharesPanel } from "@/components/SharesPanel";
import { StatusBadge } from "@/components/StatusBadge";

export function CapabilityList({
  caps,
  busy,
  onAct,
  onChanged,
  onAuthLost,
}: {
  caps: CapInfo[];
  busy: string | null;
  onAct: (name: string, action: "start" | "stop" | "restart") => Promise<void>;
  onChanged: () => Promise<void>;
  onAuthLost: () => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      {caps.map((cap) => (
        <CapabilityCard
          key={cap.name}
          cap={cap}
          busy={busy}
          onAct={onAct}
          onChanged={onChanged}
          onAuthLost={onAuthLost}
        />
      ))}
    </div>
  );
}

function CapabilityCard({
  cap,
  busy,
  onAct,
  onChanged,
  onAuthLost,
}: {
  cap: CapInfo;
  busy: string | null;
  onAct: (name: string, action: "start" | "stop" | "restart") => Promise<void>;
  onChanged: () => Promise<void>;
  onAuthLost: () => void;
}) {
  const [open, setOpen] = useState(false);
  const served = cap.served;
  const running = served?.status === "running";
  const envSet = cap.env.filter((item) => item.configured).length;
  const envMissing = cap.env.filter((item) => item.required && !item.configured).length;
  const shares = cap.shares ?? [];
  const sharePending = shares.filter((share) => share.state === "pending").length;

  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-center gap-3">
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={open ? "Collapse" : "Expand"}
          onClick={() => setOpen((value) => !value)}
        >
          {open ? <ChevronDown /> : <ChevronRight />}
        </Button>

        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
          <Button
            variant="ghost"
            className="h-auto min-w-0 max-w-full justify-start gap-2 px-0 hover:bg-transparent"
            onClick={() => setOpen((value) => !value)}
          >
            {cap.runs_cli && <Terminal data-icon="inline-start" />}
            {cap.serves_web && <Globe data-icon="inline-start" />}
            <span className="truncate font-medium">{cap.name}</span>
          </Button>
          {cap.runs_cli && <Badge variant="secondary">CLI</Badge>}
          {cap.serves_web && <Badge variant="outline">Web</Badge>}
          {cap.serves_web && cap.web_route && <RouteLink route={cap.web_route} />}
          {cap.active && <Badge variant="secondary">{cap.active}</Badge>}
          {cap.draft_changed && <StatusBadge warn>draft changed</StatusBadge>}
          {cap.env.length > 0 && (
            <StatusBadge warn={envMissing > 0}>
              env {envSet}/{cap.env.length}
            </StatusBadge>
          )}
          {sharePending > 0 ? (
            <StatusBadge warn>{sharePending} pending</StatusBadge>
          ) : shares.length > 0 ? (
            <StatusBadge ok>
              {shares.length} share{shares.length === 1 ? "" : "s"}
            </StatusBadge>
          ) : null}
          {served && (
            <StatusBadge warn={served.status !== "running"}>
              {served.status}
              {served.stale ? " (stale)" : ""}
            </StatusBadge>
          )}
          <span className="min-w-0 truncate text-sm text-muted-foreground">
            {cap.description}
          </span>
        </div>

        {cap.serves_web && (
          <div className="flex shrink-0 items-center gap-1">
            <ActionButton
              label="Start"
              disabled={running || busy !== null}
              busy={busy === `${cap.name}:start`}
              onClick={() => void onAct(cap.name, "start")}
            >
              <Play />
            </ActionButton>
            <ActionButton
              label="Stop"
              disabled={!running || busy !== null}
              busy={busy === `${cap.name}:stop`}
              onClick={() => void onAct(cap.name, "stop")}
            >
              <Square />
            </ActionButton>
            <ActionButton
              label="Restart"
              disabled={busy !== null || (!running && !served)}
              busy={busy === `${cap.name}:restart`}
              onClick={() => void onAct(cap.name, "restart")}
            >
              <RefreshCw />
            </ActionButton>
          </div>
        )}
      </CardHeader>

      {open && (
        <CardContent className="flex flex-col gap-4 border-t pt-4">
          <CapabilityDetail name={cap.name} onChanged={onChanged} onAuthLost={onAuthLost} />

          {served && served.endpoints.length > 0 && (
            <>
              <Separator />
              <section className="flex flex-col gap-2">
                <h3 className="text-sm font-medium">Endpoints</h3>
                {served.url && (
                  <p className="text-sm text-muted-foreground">
                    base URL <code className="rounded bg-muted px-1">{served.url}</code>
                  </p>
                )}
                <ul className="flex flex-col gap-1">
                  {served.endpoints.map((endpoint) => (
                    <li
                      key={`${endpoint.method} ${endpoint.path}`}
                      className="flex flex-wrap items-center gap-2 text-sm"
                    >
                      <Badge variant="outline">{endpoint.method}</Badge>
                      <code>{endpoint.path}</code>
                      {endpoint.summary && (
                        <span className="text-muted-foreground">— {endpoint.summary}</span>
                      )}
                    </li>
                  ))}
                </ul>
              </section>
            </>
          )}

          {cap.env.length > 0 && (
            <>
              <Separator />
              <EnvPanel
                name={cap.name}
                env={cap.env}
                onChanged={onChanged}
                onAuthLost={onAuthLost}
              />
            </>
          )}

          {shares.length > 0 && (
            <>
              <Separator />
              <SharesPanel
                name={cap.name}
                shares={shares}
                onChanged={onChanged}
                onAuthLost={onAuthLost}
              />
            </>
          )}

          <Separator />
          <DeleteCapability
            name={cap.name}
            onChanged={onChanged}
            onAuthLost={onAuthLost}
          />
        </CardContent>
      )}
    </Card>
  );
}

function DeleteCapability({
  name,
  onChanged,
  onAuthLost,
}: {
  name: string;
  onChanged: () => Promise<void>;
  onAuthLost: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const remove = async () => {
    setBusy(true);
    try {
      await api.remove(name, name);
      toast.success(`Deleted ${name}.`);
      setOpen(false);
      await onChanged();
    } catch (err) {
      if (isAuthError(err)) {
        onAuthLost();
        return;
      }
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      <Button variant="destructive" size="sm" onClick={() => setOpen(true)}>
        <Trash2 data-icon="inline-start" />
        Delete
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={`Delete ${name}?`}
        description="This removes the capability, tags, environments and server state. Shared folders are kept, but their grants for this capability are revoked."
        confirmLabel="Delete capability"
        confirmText={name}
        busy={busy}
        onConfirm={remove}
      />
    </div>
  );
}

function RouteLink({ route }: { route: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <a
          href={route}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 rounded-md border border-border px-1.5 py-0.5 font-mono text-xs text-muted-foreground hover:text-foreground"
        >
          {route}
          <ExternalLink className="size-3" aria-hidden="true" />
        </a>
      </TooltipTrigger>
      <TooltipContent>Open the served web capability</TooltipContent>
    </Tooltip>
  );
}

function ActionButton({
  label,
  disabled,
  busy,
  onClick,
  children,
}: {
  label: string;
  disabled: boolean;
  busy: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="outline"
          size="icon-sm"
          aria-label={label}
          disabled={disabled}
          onClick={onClick}
        >
          {busy ? <Spinner /> : children}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}
