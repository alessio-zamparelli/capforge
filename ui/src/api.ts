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

export interface CapInfo {
  name: string;
  description: string;
  kind: "cli" | "web";
  active: string | null;
  draft_ready: boolean;
  draft_error: string | null;
  draft_changed: boolean;
  served: ServedInfo | null;
  env: EnvInfo[];
}

export async function caps(): Promise<CapInfo[]> {
  const body = (await jsonOrThrow(await fetch("../api/caps"))) as { caps: CapInfo[] };
  return body.caps;
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
