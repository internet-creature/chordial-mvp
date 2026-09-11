import { afterEach, describe, expect, it } from "vitest";
import type { StuckEpisode, StuckProposal } from "../api/types";
import {
  boundaryShowing,
  containerLabel,
  enoughLine,
  handoffFor,
  makeRequest,
  readStuckRequest,
  restEvidence,
  resumePoint,
  setWhyHidden,
  STUCK_REQUEST_KEY,
  suggestsSleep,
  thinkingLine,
  thingLabel,
  titleMap,
  visibleProposal,
  whyHidden,
  writeStuckRequest,
} from "./stuck";

class MemoryStorage implements Storage {
  private map = new Map<string, string>();
  get length() { return this.map.size; }
  clear() { this.map.clear(); }
  getItem(k: string) { return this.map.get(k) ?? null; }
  key(i: number) { return [...this.map.keys()][i] ?? null; }
  removeItem(k: string) { this.map.delete(k); }
  setItem(k: string, v: string) { this.map.set(k, v); }
}

function proposal(over: Partial<StuckProposal> = {}): StuckProposal {
  return {
    proposal_id: "p1", generation: 1, source: "model", kind: "step",
    action: "start_task", line: "open the doc and write one sentence.",
    enough: "one sentence, any sentence", minutes: 2, why: null,
    why_register: "matters", thing: { task_id: 7 },
    prepared_step: { task_id: 7, next_action: "write one sentence" },
    rationale_codes: [], ...over,
  };
}

function episode(over: Partial<StuckEpisode> = {}): StuckEpisode {
  return {
    episode_id: "e1", status: "ready", generation: 1, surface: "companion",
    task_id: null, source: "model",
    proposals: [
      proposal({ proposal_id: "p1" }),
      proposal({ proposal_id: "p2", kind: "body", action: "body_here",
                 thing: null, prepared_step: null, minutes: null }),
      proposal({ proposal_id: "p3", kind: "rest", action: "rest_here",
                 thing: null, prepared_step: null, minutes: null,
                 rationale_codes: ["late_last_night"] }),
    ],
    rejected_proposal_ids: [], rejected_kinds: [], exhausted: false,
    accepted_proposal_id: null, execution: null, error: null,
    opened_at: null, ready_at: null, closed_at: null, ...over,
  };
}

afterEach(() => {
  // nothing global
});

describe("the request that crosses windows", () => {
  it("is written once and read once", () => {
    const storage = new MemoryStorage();
    const req = makeRequest("companion", 7, 1000);
    expect(req.request_id).toMatch(/^[0-9a-f-]{36}$/);
    writeStuckRequest(storage, req);
    expect(storage.getItem(STUCK_REQUEST_KEY)).not.toBeNull();
    expect(readStuckRequest(storage, 1500)).toEqual(req);
    expect(readStuckRequest(storage, 1500)).toBeNull();      // cleared
  });

  it("drops a stale or malformed press instead of replaying it", () => {
    const storage = new MemoryStorage();
    writeStuckRequest(storage, makeRequest("tray", null, 1000));
    expect(readStuckRequest(storage, 1000 + 61_000)).toBeNull();
    storage.setItem(STUCK_REQUEST_KEY, "{not json");
    expect(readStuckRequest(storage)).toBeNull();
    storage.setItem(STUCK_REQUEST_KEY, JSON.stringify({ surface: "home" }));
    expect(readStuckRequest(storage)).toBeNull();
  });
});

describe("hide the why", () => {
  it("is a remembered preference", () => {
    const storage = new MemoryStorage();
    expect(whyHidden(storage)).toBe(false);
    setWhyHidden(storage, true);
    expect(whyHidden(storage)).toBe(true);
    setWhyHidden(storage, false);
    expect(whyHidden(storage)).toBe(false);
  });
});

describe("which card", () => {
  it("shows the first proposal the person hasn't turned down", () => {
    const ep = episode();
    expect(visibleProposal(ep)?.proposal_id).toBe("p1");
    expect(visibleProposal({ ...ep, rejected_proposal_ids: ["p1"] })?.proposal_id)
      .toBe("p2");
    expect(visibleProposal({ ...ep, rejected_proposal_ids: ["p1", "p2", "p3"] }))
      .toBeNull();
  });

  it("shows nothing while thinking or once settled", () => {
    expect(visibleProposal(episode({ status: "thinking" }))).toBeNull();
    expect(visibleProposal(episode({ status: "accepted" }))).toBeNull();
    expect(visibleProposal(null)).toBeNull();
  });

  it("only looks at the current generation", () => {
    const ep = episode({
      generation: 2,
      proposals: [proposal({ proposal_id: "old", generation: 1 }),
                  proposal({ proposal_id: "new", generation: 2, kind: "company",
                             action: "start_company" })],
    });
    expect(visibleProposal(ep)?.proposal_id).toBe("new");
  });

  it("carries the rest proposal's evidence to the rest branch", () => {
    expect(restEvidence(episode())).toEqual(["late_last_night"]);
    expect(suggestsSleep(["late_last_night"])).toBe(true);
    expect(suggestsSleep(["no_runs_today"])).toBe(false);
    expect(restEvidence(null)).toEqual([]);
  });
});

describe("the card's words", () => {
  it("writes enough = once", () => {
    expect(enoughLine("one sentence")).toBe("enough = one sentence");
    expect(enoughLine("enough = you came back")).toBe("enough = you came back");
    expect(enoughLine("  Enough = you stood up ")).toBe("Enough = you stood up");
  });

  it("names the thing from today's titles", () => {
    const titles = new Map([[7, "stuck-mode design"]]);
    expect(thingLabel(proposal(), titles)).toBe("stuck-mode design");
    expect(thingLabel(proposal({ thing: { task_id: 99 } }), titles))
      .toBe("a task on today’s list");
    expect(thingLabel(proposal({ thing: { label: "the rain playlist" } }), titles))
      .toBe("the rain playlist");
    expect(thingLabel(proposal({ thing: null }), titles)).toBeNull();
  });

  it("labels the container by the action", () => {
    expect(containerLabel(proposal())).toBe("2 min · then you choose");
    expect(containerLabel(proposal({ action: "pause_and_away", kind: "body",
                                     minutes: 8 })))
      .toBe("8 min away · the step waits");
    expect(containerLabel(proposal({ minutes: null }))).toBeNull();
  });

  it("rotates the thinking lines and holds the last", () => {
    expect(thinkingLine(0)).toBe("reading the day so far…");
    expect(thinkingLine(4)).toBe("almost.");
    expect(thinkingLine(40)).toBe("almost.");
    expect(thinkingLine(-3)).toBe("reading the day so far…");
  });
});

describe("the handoff after do this", () => {
  const titles = new Map([[7, "stuck-mode design"]]);
  const exec = (over: object) => ({
    execution_id: "x", episode_id: "e1", action: "start_task" as const,
    kind: "step" as const, task_id: 7, next_action: "write one sentence",
    minutes: 2, line: "open the doc.", ...over,
  });

  const execution = { execution_id: "x", episode_id: "e1" };

  it("starts the task with the scoped label, the container, and the execution", () => {
    expect(handoffFor(exec({}), titles)).toEqual({
      kind: "start", taskId: 7, label: "stuck-mode design: write one sentence",
      minutes: 2, execution,
    });
    expect(handoffFor(exec({ next_action: null, minutes: null }), titles))
      .toEqual({ kind: "start", taskId: 7, label: "stuck-mode design", minutes: 2,
                 execution });
  });

  it("company is an unnamed five-minute block", () => {
    expect(handoffFor(exec({ action: "start_company", kind: "company",
                             task_id: null, next_action: null, minutes: 5 }),
                      titles))
      .toEqual({ kind: "start", taskId: null, label: "just sitting", minutes: 5,
                 execution });
  });

  it("everything else shows its line", () => {
    for (const action of ["pause_and_away", "body_here", "rest_here",
                          "point_to_sound"] as const) {
      expect(handoffFor(exec({ action, line: "water, then outside." }), titles))
        .toEqual({ kind: "line", line: "water, then outside." });
    }
  });

  it("builds the title map from every bucket", () => {
    const map = titleMap({ today: [{ id: 1, title: "a" }],
                           overdue: [{ id: 2, title: "b" }], done: [] });
    expect(map.get(1)).toBe("a");
    expect(map.get(2)).toBe("b");
    expect(titleMap(undefined).size).toBe(0);
  });
});


describe("the boundary", () => {
  it("shows on a stuck run over target until the person chooses", () => {
    const base = { running: true, runMode: "stuck", runId: 4, overtime: true,
                   dismissedRunId: null, questionOpen: false };
    expect(boundaryShowing(base)).toBe(true);
    expect(boundaryShowing({ ...base, runMode: null })).toBe(false);
    expect(boundaryShowing({ ...base, overtime: false })).toBe(false);
    expect(boundaryShowing({ ...base, dismissedRunId: 4 })).toBe(false);
    expect(boundaryShowing({ ...base, dismissedRunId: 3 })).toBe(true);
    expect(boundaryShowing({ ...base, questionOpen: true })).toBe(false);
    expect(boundaryShowing({ ...base, running: false })).toBe(false);
  });

  it("writes where they stopped, capped like any scope", () => {
    expect(resumePoint("write one sentence"))
      .toBe("pick up where you stopped: write one sentence");
    expect(resumePoint(null)).toBe("pick up where you stopped");
    expect(resumePoint("   ")).toBe("pick up where you stopped");
    const long = resumePoint("x".repeat(200));
    expect(long.length).toBeLessThanOrEqual(140);
    expect(long.endsWith("…")).toBe(true);
  });
});
