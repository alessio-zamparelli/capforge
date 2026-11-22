// Thin client for the console API. Writes carry the X-CSRF header the
// server requires; a 401 anywhere flips the app back to the login view.

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export function isAuthError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401;
}

async function jsonOrThrow(resp: Response) {
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const message = (body as { error?: string }).error || `HTTP ${resp.status}`;
    throw new ApiError(message, resp.status);
  }
  return body;
}

export async function login(token: string): Promise<void> {
  await jsonOrThrow(
    await fetch("../api/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token }),
    })
  );
}

export async function session(): Promise<{ authed: boolean; console_enabled: boolean }> {
  return jsonOrThrow(await fetch("../api/session")) as Promise<{
    authed: boolean;
    console_enabled: boolean;
  }>;
}

export interface Endpoint {
  method: string;
  path: string;
  summary?: string;
}

export interface ServedInfo {
  status: string;
  url: string | null;
  version: string | null;
  stale: boolean;
  desired: string;
  endpoints: Endpoint[];
}

export interface EnvInfo {
  name: string;
  declared: boolean;
  configured: boolean;
  updated_at: number;
  type: "str" | "int" | "float" | "bool";
  required: boolean;
  default: string | null;
  description: string;
}

export interface EnvChange {
  ok: boolean;
  restarted?: boolean;
  restart_error?: string;
  env: EnvInfo[];
}

export type ShareAccess = "ro" | "rw";

export interface ShareStatus {
  name: string;
  requested: ShareAccess | null;
  access: ShareAccess | null;
  state: "pending" | "approved" | "unused";
  approved_at: number | null;
  weaker: boolean;
}

export interface ShareGrant {
  cap: string;
  access: ShareAccess;
  approved_at: number;
}

export interface ShareRequest {
  cap: string;
  requested: ShareAccess | null;
  access: ShareAccess | null;
  state: "pending" | "approved" | "unused";
  approved_at: number | null;
}

export interface ShareFolder {
  name: string;
  path: string;
  exists: boolean;
  created_at: number;
  note: string;
  size_bytes: number;
  size_truncated: boolean;
  grants: ShareGrant[];
  requests: ShareRequest[];
}

export interface ShareChange {
  ok: boolean;
  restarted?: boolean;
  restart_error?: string;
  shares?: ShareStatus[];
  folders?: ShareFolder[];
}

export interface CapInfo {
  name: string;
  description: string;
  kind: "cli" | "web" | "both";
  modes: ("cli" | "web")[];
  runs_cli: boolean;
  serves_web: boolean;
  web_route: string | null;
  cli_entrypoint: string | null;
  web_entrypoint: string | null;
  app: string;
  active: string | null;
  draft_ready: boolean;
  draft_error: string | null;
  draft_changed: boolean;
  served: ServedInfo | null;
  env: EnvInfo[];
  shares: ShareStatus[];
}

export interface TagInfo {
  tag: string;
  created_at?: number;
  content_hash?: string;
  note?: string;
  kind?: "cli" | "web" | "both";
  modes?: ("cli" | "web")[];
  cli_entrypoint?: string;
  web_entrypoint?: string;
  active?: boolean;
}

export interface PipelineInfo {
  state: string | null;
  staging_id: string | null;
  sha: string | null;
  tag: string | null;
  updated: number | null;
  error: string | null;
}

export interface LogTail {
  text: string;
  bytes: number;
  truncated: boolean;
}

export interface CapDetail extends CapInfo {
  data: "version" | "shared";
  active_content_hash: string;
  tags: TagInfo[];
  pruned: string[];
  pipeline: PipelineInfo;
  log: LogTail | null;
}

export async function caps(): Promise<CapInfo[]> {
  const body = (await jsonOrThrow(await fetch("../api/caps"))) as { caps: CapInfo[] };
  return body.caps;
}

export async function capDetail(name: string): Promise<CapDetail> {
  return (await jsonOrThrow(
    await fetch(`../api/caps/${encodeURIComponent(name)}`)
  )) as CapDetail;
}

export async function logs(
  name: string,
  kind: "pipeline" | "scaffold" | "served",
  tail?: number
): Promise<LogTail & { kind: string }> {
  const query = new URLSearchParams({ kind });
  if (tail !== undefined) query.set("tail", String(tail));
  return (await jsonOrThrow(
    await fetch(`../api/caps/${encodeURIComponent(name)}/logs?${query.toString()}`)
  )) as LogTail & { kind: string };
}

export async function rollback(name: string, tag: string): Promise<unknown> {
  return jsonOrThrow(
    await fetch(`../api/caps/${encodeURIComponent(name)}/rollback`, {
      method: "POST",
      headers: { "content-type": "application/json", "X-CSRF": "1" },
      body: JSON.stringify({ tag }),
    })
  );
}

export async function remove(name: string, confirm: string): Promise<unknown> {
  return jsonOrThrow(
    await fetch(`../api/caps/${encodeURIComponent(name)}/delete`, {
      method: "POST",
      headers: { "content-type": "application/json", "X-CSRF": "1" },
      body: JSON.stringify({ confirm }),
    })
  );
}

export async function control(
  name: string,
  action: "start" | "stop" | "restart"
): Promise<unknown> {
  return jsonOrThrow(
    await fetch(`../api/caps/${name}/${action}`, {
      method: "POST",
      headers: { "content-type": "application/json", "X-CSRF": "1" },
      body: JSON.stringify({}),
    })
  );
}

export async function setEnv(name: string, key: string, value: string): Promise<EnvChange> {
  return (await jsonOrThrow(
    await fetch(`../api/caps/${encodeURIComponent(name)}/env`, {
      method: "POST",
      headers: { "content-type": "application/json", "X-CSRF": "1" },
      body: JSON.stringify({ name: key, value }),
    })
  )) as EnvChange;
}

export async function clearEnv(name: string, key: string): Promise<EnvChange> {
  return (await jsonOrThrow(
    await fetch(
      `../api/caps/${encodeURIComponent(name)}/env/${encodeURIComponent(key)}`,
      { method: "DELETE", headers: { "X-CSRF": "1" } }
    )
  )) as EnvChange;
}

export async function shares(): Promise<ShareFolder[]> {
  const body = (await jsonOrThrow(await fetch("../api/shares"))) as {
    folders: ShareFolder[];
  };
  return body.folders;
}

export async function createShare(name: string, note: string): Promise<ShareChange> {
  return (await jsonOrThrow(
    await fetch("../api/shares", {
      method: "POST",
      headers: { "content-type": "application/json", "X-CSRF": "1" },
      body: JSON.stringify({ name, note }),
    })
  )) as ShareChange;
}

export async function deleteShare(name: string, confirm: string): Promise<ShareChange> {
  return (await jsonOrThrow(
    await fetch(`../api/shares/${encodeURIComponent(name)}`, {
      method: "DELETE",
      headers: { "content-type": "application/json", "X-CSRF": "1" },
      body: JSON.stringify({ confirm }),
    })
  )) as ShareChange;
}

export async function capShares(name: string): Promise<ShareStatus[]> {
  const body = (await jsonOrThrow(
    await fetch(`../api/caps/${encodeURIComponent(name)}/shares`)
  )) as { shares: ShareStatus[] };
  return body.shares;
}

export async function approveShare(
  name: string,
  folder: string,
  access: ShareAccess
): Promise<ShareChange> {
  return (await jsonOrThrow(
    await fetch(
      `../api/caps/${encodeURIComponent(name)}/shares/${encodeURIComponent(folder)}`,
      {
        method: "PUT",
        headers: { "content-type": "application/json", "X-CSRF": "1" },
        body: JSON.stringify({ access }),
      }
    )
  )) as ShareChange;
}

export async function revokeShare(name: string, folder: string): Promise<ShareChange> {
  return (await jsonOrThrow(
    await fetch(
      `../api/caps/${encodeURIComponent(name)}/shares/${encodeURIComponent(folder)}`,
      { method: "DELETE", headers: { "X-CSRF": "1" } }
    )
  )) as ShareChange;
}
