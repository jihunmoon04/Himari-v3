// The companion's devices (V3-P83) and quiet (V3-P88 §6.2): add one (its pairing code is shown once), rename it,
// revoke it; be quiet everywhere for a while, or end it.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "@/api/client";
import { addDevice, readDevices, renameDevice, revokeDevice, setQuiet, type Device } from "@/api/settings";
import { Button } from "@/components/ui/button";
import { toast } from "@/features/toast";

const field = "rounded-md border border-line bg-bg px-2 py-1.5 text-sm outline-none focus:border-faint";
const why = (failure: unknown, fallback: string) => (failure instanceof ApiError ? failure.message : fallback);

function nextMorning(): Date {
  const at = new Date();
  at.setDate(at.getDate() + 1);
  at.setHours(8, 0, 0, 0);
  return at;
}

function Row({ device, onChanged }: { device: Device; onChanged: () => void }) {
  const [name, setName] = useState<string | null>(null);
  const act = async (run: () => Promise<unknown>, done: string) => {
    try {
      await run();
      toast(done);
      setName(null);
    } catch (failure) {
      toast(why(failure, "Could not change the device."), "error");
    } finally {
      onChanged();
    }
  };
  return (
    <li className="flex flex-wrap items-center gap-2 rounded-lg border border-line px-3 py-2 text-sm">
      {name === null ? (
        <div className="min-w-0 flex-1">
          <span className="font-medium">{device.name}</span>
          <span className="ml-2 text-xs text-faint">
            {!device.paired ? "not paired yet" : device.connected ? "connected" : "not connected"}
            {device.current ? " · current" : ""}
            {device.last_seen_at ? ` · seen ${new Date(device.last_seen_at).toLocaleString()}` : ""}
          </span>
        </div>
      ) : (
        <input aria-label="Device name" value={name} maxLength={40} autoFocus onChange={(event) => setName(event.target.value)}
          className={`${field} min-w-0 flex-1`} />
      )}
      {name === null ? (
        <>
          <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => setName(device.name)}>Rename</Button>
          <Button variant="ghost" className="px-2 py-0.5 text-xs text-danger" onClick={() => {
            if (window.confirm(`Revoke “${device.name}”? It is signed out and has to be paired again.`)) {
              void act(() => revokeDevice(device.device_id), `Revoked “${device.name}”.`);
            }
          }}>Revoke</Button>
        </>
      ) : (
        <>
          <Button variant="ghost" onClick={() => setName(null)}>Cancel</Button>
          <Button variant="primary" disabled={!name.trim()} onClick={() => void act(() => renameDevice(device.device_id, name.trim()), "Renamed.")}>Save</Button>
        </>
      )}
    </li>
  );
}

export function DevicesSettings() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["settings", "devices"], queryFn: readDevices });
  const [name, setName] = useState("");
  const [made, setMade] = useState<{ name: string; code: string; expires_at: string } | null>(null);
  const refresh = () => void client.invalidateQueries({ queryKey: ["settings", "devices"] });
  if (query.isLoading) return <p className="text-sm text-faint">Loading…</p>;
  if (query.error || !query.data) return <p role="alert" className="text-sm text-danger">Could not read the devices.</p>;
  const { items, enabled, quiet_until } = query.data;
  const quiet = quiet_until && new Date(quiet_until) > new Date() ? new Date(quiet_until) : null;
  const add = async () => {
    try {
      const device = await addDevice(name.trim());
      setMade({ name: device.name, code: device.code, expires_at: device.expires_at });
      setName("");
    } catch (failure) {
      toast(why(failure, "Could not add the device."), "error");
    } finally {
      refresh();
    }
  };
  const quietFor = async (until: Date | null) => {
    try {
      await setQuiet(until);
      toast(until ? `Quiet until ${until.toLocaleString()}.` : "Quiet ended.");
    } catch (failure) {
      toast(why(failure, "Could not change quiet."), "error");
    } finally {
      refresh();
    }
  };
  return (
    <section className="space-y-4" aria-label="Devices">
      <h2 className="text-base font-semibold">Devices</h2>
      {!enabled && <p className="text-sm text-wait">The companion is off on this server.</p>}
      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-line px-3 py-2 text-sm" aria-label="Quiet">
        <span className="flex-1">{quiet ? `Quiet until ${quiet.toLocaleString()}: HIMARI does not speak first.` : "Not quiet."}</span>
        <Button variant="ghost" onClick={() => void quietFor(new Date(Date.now() + 60 * 60 * 1000))}>Quiet for an hour</Button>
        <Button variant="ghost" onClick={() => void quietFor(nextMorning())}>Until tomorrow 8:00</Button>
        {quiet && <Button onClick={() => void quietFor(null)}>End quiet</Button>}
      </div>
      <ul className="space-y-2" aria-label="Devices">
        {items.map((device) => <Row key={device.device_id} device={device} onChanged={refresh} />)}
      </ul>
      {items.length === 0 && <p className="text-sm text-faint">No devices yet.</p>}
      {enabled && (
        <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); void add(); }}>
          <input aria-label="New device name" placeholder="New device's name" value={name} maxLength={40}
            onChange={(event) => setName(event.target.value)} className={`${field} min-w-0 flex-1`} />
          <Button type="submit" variant="primary" disabled={!name.trim()}>Add device</Button>
        </form>
      )}
      {made && (
        <div role="status" className="rounded-lg border border-accent/40 bg-accent/5 px-3 py-2 text-sm">
          Pair “{made.name}” with this code (shown once, until {new Date(made.expires_at).toLocaleTimeString()}):
          <span className="ml-2 select-all font-mono text-base font-semibold" aria-label="Pairing code">{made.code}</span>
        </div>
      )}
    </section>
  );
}
