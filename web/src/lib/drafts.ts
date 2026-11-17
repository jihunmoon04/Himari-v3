// What the user wrote and has not sent, kept in this tab (the composer's sessionStorage, ``himari.next.draft.*``), and
// what the classic page kept in this browser before W6 (IndexedDB "himari-local", store "drafts", by place: "home"
// or a conversation's id; V3-P78 §5).
//
// - Signed in: the classic page's drafts come into this tab's (an empty place only, never over what is here), and
//   its store is deleted (V3-P102 W6d: the classic page is gone, so nothing else would ever read it).
// - Signing out (the user's own Sign out): both are wiped here and in this browser's other tabs (told over a
//   BroadcastChannel), as the classic page's sign-out did: a shared browser keeps nothing. A sign-in that merely
//   ended (it expired, or went in another tab) keeps the draft for the same person signing back in (W1).
const PREFIX = "himari.next.draft.";
const LEGACY = "himari-local";
const CHANNEL = "himari-drafts";

let channel: BroadcastChannel | null = null;
try {
  channel = new BroadcastChannel(CHANNEL);
  channel.onmessage = (event: MessageEvent<unknown>) => { if (event.data === "wipe") wipeHere(); };
} catch {
  channel = null; // no channel here: only this tab is wiped
}

/** The composer's key for a place of the classic page. */
const keyOf = (place: string) => (place === "home" ? "home" : `c:${place}`);

function readLegacy(): Promise<[string, string][]> {
  return new Promise((resolve) => {
    let request: IDBOpenDBRequest;
    try {
      request = indexedDB.open(LEGACY);
    } catch {
      return resolve([]);
    }
    // never made here: do not make it now
    request.onupgradeneeded = () => request.transaction?.abort();
    request.onerror = () => resolve([]);
    request.onblocked = () => resolve([]);
    request.onsuccess = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains("drafts")) {
        db.close();
        return resolve([]);
      }
      const out: [string, string][] = [];
      try {
        const cursor = db.transaction("drafts", "readonly").objectStore("drafts").openCursor();
        cursor.onsuccess = () => {
          const at = cursor.result;
          if (!at) {
            db.close();
            return resolve(out);
          }
          const text = (at.value as { text?: unknown } | null)?.text;
          if (typeof at.key === "string" && typeof text === "string" && text.trim()) out.push([at.key, text]);
          at.continue();
        };
        cursor.onerror = () => { db.close(); resolve(out); };
      } catch {
        db.close();
        resolve(out);
      }
    };
  });
}

function deleteLegacy() {
  try {
    indexedDB.deleteDatabase(LEGACY);
  } catch {
    // no IndexedDB here: nothing was kept in it
  }
}

let moved = false;

/** Once per page, signed in: the classic page's drafts into this tab's places that hold none. */
export async function moveLegacyDrafts(): Promise<number> {
  if (moved) return 0;
  moved = true;
  const found = await readLegacy();
  let count = 0;
  for (const [place, text] of found) {
    try {
      const key = PREFIX + keyOf(place);
      if (!window.sessionStorage.getItem(key)) {
        window.sessionStorage.setItem(key, text);
        count += 1;
      }
    } catch {
      return count; // storage is off: keep the old store, it may be read later
    }
  }
  deleteLegacy();
  return count;
}

/** The user signed out: nothing they wrote stays in this browser, in this tab or the others. */
export function wipeDrafts() {
  wipeHere();
  try {
    channel?.postMessage("wipe");
  } catch {
    // the channel closed: the other tabs wipe theirs when they sign out
  }
}

function wipeHere() {
  try {
    for (let index = window.sessionStorage.length - 1; index >= 0; index -= 1) {
      const key = window.sessionStorage.key(index);
      if (key?.startsWith(PREFIX)) window.sessionStorage.removeItem(key);
    }
  } catch {
    // storage is off: nothing was kept
  }
  deleteLegacy();
  moved = false;
}
