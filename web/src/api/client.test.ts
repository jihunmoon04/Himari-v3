// A call whose answer breaks off on the way may have been done: it fails as a connection that broke (status 0),
// never as a plain error.
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./client";

describe("api", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("fails as offline when the answer's body breaks off", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      status: 200, ok: true, statusText: "OK", text: () => Promise.reject(new TypeError("network error")),
    })));
    const failed = await api("POST", "/api/conversations/c1/messages", {}).catch((error: unknown) => error);
    expect(failed).toBeInstanceOf(ApiError);
    expect(failed).toMatchObject({ status: 0, code: "offline" });
  });
});
