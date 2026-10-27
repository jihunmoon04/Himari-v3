/* What the user wrote and has not sent or saved, kept in this browser
 * (V3-P78 §5): IndexedDB "himari-local", with "drafts" (the composer's, by
 * place) and "unsaved" (a file's, by path; V3-P77 2c's ink goes there too).
 *
 * Every call settles, never throws: a browser without the store (a private
 * window, a full disk, Safari dropping the connection in the background)
 * keeps what the page holds in memory. A connection that failed is opened
 * again on the next call. After a sign-out's wipe, writes stay closed until
 * the next sign-in opens them (§5.3), here and in the other tabs told. */

const NAME = "himari-local";
const VERSION = 1;
export const STORES = ["drafts", "unsaved"];
const CHANNEL = "himari-local";

/* This tab, as the writer of what it keeps (§5.2): a random id per page. */
export const WRITER = (() => {
  try {
    return crypto.randomUUID();
  } catch (ignored) {
    return `w${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
  }
})();

let opening = null; // the open connection's promise, or null to open on the next call
let writable = false; // closed until enter() opens it, and again after a wipe
const wipedListeners = [];
let channel = null;

function connect() {
  if (opening) return opening;
  opening = new Promise((resolve) => {
    let request;
    try {
      request = indexedDB.open(NAME, VERSION);
    } catch (ignored) {
      resolve(null);
      return;
    }
    request.onupgradeneeded = () => {
      const db = request.result;
      for (const store of STORES) if (!db.objectStoreNames.contains(store)) db.createObjectStore(store);
    };
    request.onsuccess = () => {
      const db = request.result;
      db.onversionchange = () => { db.close(); opening = null; }; // a newer page upgrades it (N9)
      db.onclose = () => { opening = null; }; // lost in the background: opened again next time
      resolve(db);
    };
    request.onerror = () => resolve(null);
    request.onblocked = () => resolve(null);
  }).then((db) => {
    if (db === null) opening = null;
    return db;
  });
  return opening;
}

/* One transaction on ``store``: ``work(objectStore)`` returns the request whose result is wanted
 * (or none). Null on any failure. */
async function run(store, mode, work) {
  const db = await connect();
  if (db === null) return null;
  return new Promise((resolve) => {
    let tx;
    let request = null;
    try {
      tx = db.transaction(store, mode);
      request = work(tx.objectStore(store)) || null;
    } catch (ignored) {
      opening = null; // a closed connection throws here: open it again next time
      resolve(null);
      return;
    }
    tx.oncomplete = () => resolve(request ? request.result : true);
    tx.onerror = () => resolve(null);
    tx.onabort = () => resolve(null);
  });
}

/* Writes from now on (enter(), after a sign-in). */
export function openWrites() {
  writable = true;
}

/* Sign out (§5.3): nothing more is written from here, before anything is left or wiped. */
export function closeWrites() {
  writable = false;
}

export function writesOpen() {
  return writable;
}

/* Every record of ``store`` as [key, value] pairs ([] without the store). */
export async function entries(store) {
  const db = await connect();
  if (db === null) return [];
  return new Promise((resolve) => {
    const found = [];
    let tx;
    try {
      tx = db.transaction(store, "readonly");
      const cursor = tx.objectStore(store).openCursor();
      cursor.onsuccess = () => {
        const at = cursor.result;
        if (!at) return;
        found.push([at.key, at.value]);
        at.continue();
      };
    } catch (ignored) {
      opening = null;
      resolve([]);
      return;
    }
    tx.oncomplete = () => resolve(found);
    tx.onerror = () => resolve([]);
    tx.onabort = () => resolve([]);
  });
}

export async function get(store, key) {
  const value = await run(store, "readonly", (s) => s.get(key));
  return value === null ? null : value ?? null;
}

export async function put(store, key, value) {
  if (!writable) return false;
  return (await run(store, "readwrite", (s) => s.put(value, key))) !== null;
}

/* Removes ``key``; with ``writer``, only while the record is that writer's (§5.2). */
export async function remove(store, key, writer = null) {
  if (!writable) return false;
  return (await run(store, "readwrite", (s) => {
    if (writer === null) return s.delete(key);
    const read = s.get(key);
    read.onsuccess = () => {
      if (read.result && read.result.writer === writer) s.delete(key);
    };
    return read;
  })) !== null;
}

/* Keeps at most ``max`` records of ``store`` none older than ``maxAgeMs`` (by their ``at``): the oldest go. */
export async function prune(store, max, maxAgeMs, now = Date.now()) {
  if (!writable) return;
  const all = await entries(store);
  const old = all.filter(([, v]) => !v || typeof v.at !== "number" || now - v.at > maxAgeMs).map(([k]) => k);
  const kept = all.filter(([k]) => !old.includes(k)).sort((a, b) => b[1].at - a[1].at);
  const over = kept.slice(max).map(([k]) => k);
  const gone = [...old, ...over];
  if (!gone.length) return;
  await run(store, "readwrite", (s) => { for (const key of gone) s.delete(key); });
}

/* How many records there are in all (for the sign-out's confirmation). */
export async function count() {
  let total = 0;
  for (const store of STORES) {
    const n = await run(store, "readonly", (s) => s.count());
    total += typeof n === "number" ? n : 0;
  }
  return total;
}

/* Sign out (§5.3): writes close first, then every record goes, then the other tabs are told. */
export async function wipe() {
  writable = false;
  for (const store of STORES) await run(store, "readwrite", (s) => s.clear());
  try {
    if (channel) channel.postMessage({ wiped: true });
  } catch (ignored) { /* the other tabs find no session, and sign in again */ }
}

/* Whether the tab that is ``writer`` (its WRITER) is open now (V3-P82 §4: its kept ink is then asked about,
 * not put back on its own): asked over the channel, an answer waited for ``ms``. False without a channel. */
export function alive(writer, ms = 300) {
  if (!channel || writer === WRITER) return Promise.resolve(writer === WRITER);
  const ask = channel;
  return new Promise((resolve) => {
    const answered = (event) => {
      if (!event.data || event.data.here !== writer) return;
      done(true);
    };
    const timer = setTimeout(() => done(false), ms);
    function done(result) {
      clearTimeout(timer);
      ask.removeEventListener("message", answered);
      resolve(result);
    }
    ask.addEventListener("message", answered);
    try {
      ask.postMessage({ alive: writer });
    } catch (ignored) {
      done(false);
    }
  });
}

/* ``listener()`` runs when another tab wiped the store; writes are closed here first. */
export function onWiped(listener) {
  wipedListeners.push(listener);
}

try {
  channel = new BroadcastChannel(CHANNEL);
  if (typeof channel.unref === "function") channel.unref(); // Node (the smokes): it never holds the process open
  channel.onmessage = (event) => {
    if (event.data && event.data.alive === WRITER) channel.postMessage({ here: WRITER }); // open: this tab
    if (!event.data || !event.data.wiped) return;
    writable = false;
    for (const listener of wipedListeners) listener();
  };
} catch (ignored) {
  channel = null; // no BroadcastChannel: another tab learns of the sign-out at its next request
}
