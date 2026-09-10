import { useState } from "react";
import { linkDevice } from "../api/client";

interface Props {
  onLinked: (token: string, deviceId: string) => void;
}

/** the front porch: paste a one-time code, become a device. codes come from
 * vel in chat ("link my computer") or `dev_db.py link-code` in dev. */
export default function LinkScreen({ onLinked }: Props) {
  const [code, setCode] = useState("");
  const [name, setName] = useState("my computer");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!code.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await linkDevice(code, name.trim() || "my computer");
      onLinked(result.token, result.device_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "something went wrong");
      setBusy(false);
    }
  }

  return (
    <div className="link-screen">
      <form className="link-card" onSubmit={submit}>
        <div className="link-mark" aria-hidden="true">
          <img src="/chordial-icon.png" alt="" />
        </div>
        <h1>chordial</h1>
        <label className="field">
          <span>link code</span>
          <input
            autoFocus
            value={code}
            onChange={(e) => setCode(e.currentTarget.value.toUpperCase())}
            placeholder="ABC123"
            spellCheck={false}
            autoComplete="off"
          />
        </label>
        <p className="link-help">
          Ask vel to “link my computer”, then paste the code here.
        </p>
        <label className="field">
          <span>call this device</span>
          <input
            value={name}
            onChange={(e) => setName(e.currentTarget.value)}
            maxLength={80}
          />
        </label>
        {error && <p className="link-error">{error}</p>}
        <button type="submit" disabled={busy || !code.trim()}>
          {busy ? "linking…" : "link device"}
        </button>
      </form>
    </div>
  );
}
