import { useEffect, useState } from "react";
import { api, AppConfig } from "./api";

export function App() {
  const [cfg, setCfg] = useState<AppConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.getConfig().then(setCfg).catch((e) => setError(String(e)));
  }, []);

  function toggle(svc: string, res: string, cmd: string) {
    if (!cfg) return;
    const next: AppConfig = structuredClone(cfg);
    const c = next.services[svc].resources[res].commands[cmd];
    c.enabled = !c.enabled;
    setCfg(next);
    setDirty(true);
    setInfo(null);
  }

  async function save() {
    if (!cfg) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await api.putConfig(cfg);
      setCfg(saved);
      setDirty(false);
      setInfo("Saved. MCP tools updated.");
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  if (!cfg && !error) return <div className="container">Loading…</div>;

  return (
    <div className="container">
      <header>
        <h1>cli-mcp-tool · Admin</h1>
        <p>
          Toggle which Azure CLI commands are exposed as MCP tools. Mutating
          commands (yellow) require explicit opt-in.
        </p>
      </header>

      <div className="toolbar">
        <button onClick={save} disabled={!dirty || saving}>
          {saving ? "Saving…" : "Save changes"}
        </button>
        <button
          className="secondary"
          onClick={() => api.reload().then((r) => setInfo(`Reloaded (${r.enabled_commands} tools).`))}
        >
          Reload from disk
        </button>
        {dirty && <span style={{ color: "#9a6700" }}>Unsaved changes</span>}
      </div>

      {error && <div className="error">{error}</div>}
      {info && <div className="success">{info}</div>}

      {cfg && Object.entries(cfg.services).map(([svcName, svc]) => (
        <section key={svcName} className="service">
          <div className="service-header">
            {svc.displayName ?? svcName} <code>{svcName}</code>
          </div>
          {Object.entries(svc.resources).map(([resName, res]) => (
            <div key={resName} className="resource">
              <div className="resource-name">
                {res.displayName ?? resName} <code>{resName}</code>
              </div>
              <table>
                <thead>
                  <tr>
                    <th style={{ width: 80 }}>Enabled</th>
                    <th style={{ width: 140 }}>Command</th>
                    <th>az</th>
                    <th style={{ width: 80 }}>Mutation</th>
                    <th style={{ width: 180 }}>RBAC role</th>
                    <th style={{ width: 110 }}>PII risk</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(res.commands).map(([cmdName, cmd]) => (
                    <tr key={cmdName} className={cmd.mutation ? "mut" : ""}>
                      <td>
                        <input
                          type="checkbox"
                          checked={cmd.enabled}
                          onChange={() => toggle(svcName, resName, cmdName)}
                        />
                      </td>
                      <td>{cmdName}</td>
                      <td><code>az {cmd.azCommand.join(" ")}</code></td>
                      <td>{cmd.mutation ? "yes" : "no"}</td>
                      <td>
                        {cmd.rbacRoles && cmd.rbacRoles.length > 0
                          ? cmd.rbacRoles.map((r) => (
                              <span key={r} className="badge rbac">{r}</span>
                            ))
                          : <span className="muted">—</span>}
                      </td>
                      <td title={cmd.piiNotes ?? ""}>
                        {cmd.piiRisk
                          ? <span className={`badge pii pii-${cmd.piiRisk}`}>{cmd.piiRisk}</span>
                          : <span className="muted">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}
