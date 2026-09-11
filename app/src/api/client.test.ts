import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchToday } from "./client";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function stalledFetch() {
  const fetch = vi.fn(
    (_url: string, init: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener("abort", () =>
          reject(new DOMException("Aborted", "AbortError")),
        );
      }),
  );
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

describe("today reads", () => {
  it("times out a stalled read so the next refresh can retry", async () => {
    vi.useFakeTimers();
    const fetch = stalledFetch();
    const result = expect(fetchToday("test-token")).rejects.toMatchObject({
      name: "AbortError",
    });
    expect(fetch.mock.calls[0][1].cache).toBe("no-store");
    await vi.advanceTimersByTimeAsync(15_000);
    await result;
    expect(vi.getTimerCount()).toBe(0);
  });

  it("cancels reads and clears the timeout when a session ends", async () => {
    vi.useFakeTimers();
    stalledFetch();
    const controller = new AbortController();
    const result = expect(
      fetchToday("test-token", controller.signal),
    ).rejects.toMatchObject({ name: "AbortError" });
    controller.abort();
    await result;
    expect(vi.getTimerCount()).toBe(0);
  });
});
