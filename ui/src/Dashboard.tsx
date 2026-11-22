import { useCallback, useEffect, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Globe,
  KeyRound,
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
            <CapRow
              key={cap.name}
              cap={cap}
              busy={busy}
              onAct={act}
              onChanged={refresh}
              onAuthLost={onAuthLost}
            />
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
  const hasEndpoints = !!served && served.endpoints.length > 0;
  const expandable = hasEndpoints || cap.env.length > 0;
  const running = served?.status === "running";
  const envSet = cap.env.filter((item) => item.configured).length;
  const envMissing = cap.env.length - envSet;

  return (
    <li className="rounded-lg border border-border bg-card text-card-foreground">
      <div className="flex items-center gap-3 p-4">
        <button
          onClick={() => expandable && setOpen((o) => !o)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
          disabled={!expandable}
        >
          {expandable ? (
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
          {cap.env.length > 0 && (
            <Badge warn={envMissing > 0}>
              env {envSet}/{cap.env.length}
            </Badge>
          )}
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

      {open && expandable && (
        <div className="space-y-3 border-t border-border px-4 py-3">
          {hasEndpoints && (
            <div>
              {served?.url && (
                <p className="mb-2 text-sm text-muted-foreground">
                  base URL <code className="rounded bg-muted px-1">{served.url}</code>
                </p>
              )}
              <ul className="space-y-1">
                {served!.endpoints.map((ep) => (
                  <li
                    key={`${ep.method} ${ep.path}`}
                    className="flex items-center gap-2 text-sm"
                  >
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
          {cap.env.length > 0 && (
            <EnvPanel cap={cap} onChanged={onChanged} onAuthLost={onAuthLost} />
          )}
        </div>
      )}
    </li>
  );
}

function EnvPanel({
  cap,
  onChanged,
  onAuthLost,
}: {
  cap: CapInfo;
  onChanged: () => Promise<void>;
  onAuthLost: () => void;
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const fail = (err: unknown) => {
    if (isAuthError(err)) {
      onAuthLost();
      return;
    }
    setError(err instanceof Error ? err.message : String(err));
  };

  const save = async (key: string) => {
    const value = drafts[key] ?? "";
    if (!value) return;
    setBusy(key);
    setError(null);
    setNotice(null);
    try {
      const result = await api.setEnv(cap.name, key, value);
      setDrafts((current) => ({ ...current, [key]: "" }));
      setNotice(
        result.restart_error
          ? `Saved, but the web instance did not restart: ${result.restart_error}`
          : result.restarted
            ? "Saved; the web instance was restarted."
            : "Saved."
      );
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
    setNotice(null);
    try {
      const result = await api.clearEnv(cap.name, key);
      setDrafts((current) => ({ ...current, [key]: "" }));
      setNotice(
        result.restart_error
          ? `Removed, but the web instance did not restart: ${result.restart_error}`
          : result.restarted
            ? "Removed; the web instance was restarted."
            : "Removed."
      );
      await onChanged();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div>
      <div className="mb-2 flex items-center gap-2 text-sm font-medium">
        <KeyRound className="h-4 w-4 text-muted-foreground" />
        Environment
      </div>
      <p className="mb-2 text-xs text-muted-foreground">
        Values are injected into this capability's code at runtime and are never
        returned by the console. Only store credentials you trust it to use.
      </p>
      <ul className="space-y-2">
        {cap.env.map((item) => (
          <li key={item.name} className="flex flex-wrap items-center gap-2 text-sm">
            <code className="min-w-40 rounded bg-muted px-1.5 py-0.5 text-xs">
              {item.name}
            </code>
            <Badge warn={!item.configured}>{item.configured ? "set" : "missing"}</Badge>
            {!item.declared && (
              <span className="text-xs text-muted-foreground">(not declared)</span>
            )}
            <input
              type="password"
              autoComplete="new-password"
              value={drafts[item.name] ?? ""}
              onChange={(event) =>
                setDrafts((current) => ({ ...current, [item.name]: event.target.value }))
              }
              placeholder={item.configured ? "replace value" : "value"}
              className="h-8 min-w-48 flex-1 rounded-md border border-border bg-background px-2 text-sm"
            />
            <button
              onClick={() => void save(item.name)}
              disabled={busy !== null || !(drafts[item.name] ?? "")}
              className="flex h-8 items-center gap-1.5 rounded-md border border-border px-2.5 text-sm hover:bg-accent disabled:opacity-40"
            >
              {busy === item.name ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : null}
              Save
            </button>
            <button
              onClick={() => void clear(item.name)}
              disabled={busy !== null || !item.configured}
              className="h-8 rounded-md border border-border px-2.5 text-sm text-muted-foreground hover:bg-accent disabled:opacity-40"
            >
              Clear
            </button>
          </li>
        ))}
      </ul>
      {error && <p className="mt-2 text-xs text-red-300">{error}</p>}
      {notice && <p className="mt-2 text-xs text-muted-foreground">{notice}</p>}
    </div>
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
