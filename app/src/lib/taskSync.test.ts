import { describe, expect, it, vi } from "vitest";
import { refreshQueue } from "./taskSync";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

describe("task refresh ordering", () => {
  it("re-reads after invalidation during a slow read, without overlapping requests", async () => {
    const first = deferred<string>();
    const load = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockResolvedValue("after chat edit");
    const apply = vi.fn();
    const queue = refreshQueue(load, apply, vi.fn());
    const running = queue.refresh();
    await queue.refresh();
    await queue.refresh();
    expect(load).toHaveBeenCalledTimes(1);
    first.resolve("before chat edit");
    await running;
    expect(load).toHaveBeenCalledTimes(2);
    expect(apply).toHaveBeenLastCalledWith("after chat edit");
  });

  it("never publishes a response from a previous session", async () => {
    const oldSession = deferred<string>();
    const apply = vi.fn();
    const queue = refreshQueue(() => oldSession.promise, apply, vi.fn());
    const running = queue.refresh();
    await queue.refresh();
    queue.dispose();
    oldSession.resolve("old account's tasks");
    await running;
    expect(apply).not.toHaveBeenCalled();
  });

  it("reports failures and allows the next refresh to recover", async () => {
    const fail = vi.fn();
    const apply = vi.fn();
    const queue = refreshQueue(
      vi
        .fn()
        .mockRejectedValueOnce(new Error("offline"))
        .mockResolvedValue("recovered"),
      apply,
      fail,
    );
    await queue.refresh();
    expect(fail).toHaveBeenCalledOnce();
    await queue.refresh();
    expect(apply).toHaveBeenCalledWith("recovered");
  });
});
