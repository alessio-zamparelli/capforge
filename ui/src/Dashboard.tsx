import { useCallback, useEffect, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Globe,
  Loader2,
  RefreshCw,
  Square,
  Terminal,
  TriangleAlert,
  Play,
} from "lucide-react";
import * as api from "./api";
import { isAuthError } from "./api";
import type { CapInfo } from "./api";

const POLL_MS = 3000;

export default function Dashboard({ onAuthLost }: { onAuthLost: () => void }) {
  const [caps, setCaps] = useState<CapInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setCaps(await api.caps());
      setError(null);
    } catch (err) {
      if (isAuthError(err)) {
        onAuthLost();
        return;
      }
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [onAuthLost]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const act = useCallback(
    async (name: string, action: "start" | "stop" | "restart") => {
      setBusy(`${name}:${action}`);
      try {
        await api.control(name, action);
        await refresh();
      } catch (err) {
        if (isAuthError(err)) {
          onAuthLost();
          return;
        }
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(null);
      }
    },
    [refresh, onAuthLost]
  );

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6">
      <header className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">capforge console</h1>
        <button
          onClick={() => void refresh()}
          className="flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-sm text-muted-foreground hover:bg-accent"
        >
          <RefreshCw className="h-3.5 w-3.5" /> Refresh
        </button>
      </header>

      {error && (
        <p className="flex items-center gap-2 rounded-md border border-destructive/40 bg-destructive/20 px-3 py-2 text-sm text-red-300">
          <TriangleAlert className="h-4 w-4" /> {error}
        </p>
      )}

      {caps === null ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading capabilities…
        </p>
      ) : caps.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No capabilities yet. Scaffold one through the MCP surface.
        </p>
      ) : (
        <ul className="space-y-2">
          {caps.map((cap) => (
            <CapRow key={cap.name} cap={cap} busy={busy} onAct={act} />
          ))}
        </ul>
      )}
    </div>
  );
}

function CapRow({
  cap,
  busy,
  onAct,
}: {
  cap: CapInfo;
  busy: string | null;
  onAct: (name: string, action: "start" | "stop" | "restart") => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const served = cap.served;
  const hasEndpoints = !!served && served.endpoints.length > 0;
  const running = served?.status === "running";

  return (
    <li className="rounded-lg border border-border bg-card text-card-foreground">
      <div className="flex items-center gap-3 p-4">
        <button
          onClick={() => hasEndpoints && setOpen((o) => !o)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
          disabled={!hasEndpoints}
        >
          {hasEndpoints ? (
            open ? (
              <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
            ) : (
              <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
            )
          ) : (
            <span className="w-4 shrink-0" />
          )}
          {cap.kind === "web" ? (
            <Globe className="h-4 w-4 shrink-0 text-muted-foreground" />
          ) : (
            <Terminal className="h-4 w-4 shrink-0 text-muted-foreground" />
          )}
          <span className="truncate font-medium">{cap.name}</span>
          {cap.kind === "web" && <Badge>{cap.kind}</Badge>}
          {cap.active && <Badge>{cap.active}</Badge>}
          {cap.draft_changed && <Badge warn>draft changed</Badge>}
          {served && (
            <Badge warn={served.status !== "running"}>
              {served.status}
              {served.stale ? " (stale)" : ""}
            </Badge>
          )}
          <span className="truncate text-sm text-muted-foreground">{cap.description}</span>
        </button>

        {cap.kind === "web" && (
          <div className="flex shrink-0 items-center gap-1.5">
            <IconButton
              label="Start"
              icon={<Play className="h-3.5 w-3.5" />}
              disabled={running || busy !== null}
              busy={busy === `${cap.name}:start`}
              onClick={() => void onAct(cap.name, "start")}
            />
            <IconButton
              label="Stop"
              icon={<Square className="h-3.5 w-3.5" />}
              disabled={!running || busy !== null}
              busy={busy === `${cap.name}:stop`}
              onClick={() => void onAct(cap.name, "stop")}
            />
            <IconButton
              label="Restart"
              icon={<RefreshCw className="h-3.5 w-3.5" />}
              disabled={busy !== null || (!running && !served)}
              busy={busy === `${cap.name}:restart`}
              onClick={() => void onAct(cap.name, "restart")}
            />
          </div>
        )}
      </div>

      {open && hasEndpoints && (
        <div className="border-t border-border px-4 py-3">
          {served?.url && (
            <p className="mb-2 text-sm text-muted-foreground">
              base URL <code className="rounded bg-muted px-1">{served.url}</code>
            </p>
          )}
          <ul className="space-y-1">
            {served!.endpoints.map((ep) => (
              <li key={`${ep.method} ${ep.path}`} className="flex items-center gap-2 text-sm">
                <code className="rounded bg-muted px-1.5 py-0.5 text-xs font-semibold">
                  {ep.method}
                </code>
                <code>{ep.path}</code>
                {ep.summary && (
                  <span className="text-muted-foreground">— {ep.summary}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </li>
  );
}

function Badge({ children, warn }: { children: React.ReactNode; warn?: boolean }) {
  return (
    <span
      className={`shrink-0 rounded px-1.5 py-0.5 text-xs font-medium ${
        warn ? "bg-amber-500/15 text-amber-300" : "bg-muted text-muted-foreground"
      }`}
    >
      {children}
    </span>
  );
}

function IconButton({
  label,
  icon,
  disabled,
  busy,
  onClick,
}: {
  label: string;
  icon: React.ReactNode;
  disabled: boolean;
  busy: boolean;
  onClick: () => void;
}) {
  return (
    <button
      title={label}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className="flex h-8 w-8 items-center justify-center rounded-md border border-border text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-40"
    >
      {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : icon}
    </button>
  );
}
