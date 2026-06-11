export type PiiRisk = "none" | "low" | "medium" | "high";

export interface CommandSpec {
  enabled: boolean;
  mutation: boolean;
  azCommand: string[];
  description?: string | null;
  timeoutSeconds?: number | null;
  rbacRoles?: string[] | null;
  piiRisk?: PiiRisk | null;
  piiNotes?: string | null;
}

export interface ResourceSpec {
  displayName?: string | null;
  description?: string | null;
  commands: Record<string, CommandSpec>;
}

export interface ServiceSpec {
  displayName?: string | null;
  description?: string | null;
  resources: Record<string, ResourceSpec>;
}

export interface AppConfig {
  $schema?: string | null;
  version: number;
  defaults: { timeoutSeconds: number; subscriptionId: string | null };
  services: Record<string, ServiceSpec>;
}

const base = "/admin/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(base + path, {
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return (await res.json()) as T;
}

export const api = {
  getConfig: () => request<AppConfig>("/config"),
  putConfig: (cfg: AppConfig) =>
    request<AppConfig>("/config", { method: "PUT", body: JSON.stringify(cfg) }),
  reload: () => request<{ ok: boolean; enabled_commands: number }>("/reload", { method: "POST" }),
};
