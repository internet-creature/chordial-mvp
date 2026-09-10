import {
  test,
  expect,
  type BrowserContext,
  type Page,
  type WebSocketRoute,
} from "@playwright/test";
import type { FocusState } from "../src/api/sidecar";
import type { TaskRow, TodayPayload } from "../src/api/types";

const task = (id: number, title: string): TaskRow => ({
  id,
  public_id: `task-${id}`,
  title,
  status: "todo",
  priority: "medium",
  scheduled: "2026-09-10",
  window: null,
  pom_estimate: 1,
  plan_title: null,
  helper: null,
  description: null,
  next_action: null,
  set_aside_on: null,
});

async function mockWorkspace(context: BrowserContext, linked = true) {
  let tasks = [
    task(1, "Pomodoro 2: record the real-world examples"),
    task(2, "Pomodoro 3: edit the narration"),
    task(3, "Chordial Willowden: sketch the first clearing"),
  ];
  let offline = false;
  let focus: FocusState = { running: false, banked: {} };
  const sockets: WebSocketRoute[] = [];
  const sidecars: WebSocketRoute[] = [];
  const messages = [
    {
      id: 1,
      author: "user",
      author_type: "user",
      content: "I’d like to make a little room for Willowden today.",
      at: "2026-09-10T18:00:00Z",
      platform: "app",
    },
    {
      id: 2,
      author: "vel",
      author_type: "assistant",
      content:
        "*settles into the clearing* We can start small. One sketch, one useful piece — something that makes the next step feel a little closer. 🌿",
      at: "2026-09-10T18:01:00Z",
      platform: "app",
    },
  ];
  const today = (): TodayPayload => ({
    today: "2026-09-10",
    user: { name: "Megan" },
    pom_minutes: 25,
    break_minutes: 5,
    buckets: {
      today: tasks.filter((t) => !t.set_aside_on),
      overdue: [],
      in_progress: [],
      done: [],
      set_aside: tasks.filter((t) => !!t.set_aside_on),
    },
    focus: { active_task_id: null, seconds: {} },
    server_time: new Date().toISOString(),
  });
  if (linked)
    await context.addInitScript(() =>
      localStorage.setItem("chordial.device_token", "test-only-token"),
    );
  await context.routeWebSocket(/\/api\/v1\/ws/, (socket) => {
    sockets.push(socket);
    socket.onMessage(() => socket.send(JSON.stringify({ type: "hello" })));
  });
  await context.routeWebSocket(/localhost:8485/, (socket) => {
    sidecars.push(socket);
  });
  // Every external request is intercepted. This suite never accesses a database,
  // a real sidecar, or the deployed server (even if a developer has one running).
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin === "http://localhost:1422") return route.continue();
    const path = url.pathname;
    let body: unknown = {};
    if (path === "/api/v1/today") {
      if (offline)
        return route.fulfill({ status: 503, json: { error: "offline" } });
      body = today();
    } else if (path === "/api/v1/council") {
      body = {
        council: [
          ["vel", "🦌", "presence"],
          ["edwin", "🦉", "perspective"],
          ["juniper", "🐺", "connection"],
          ["mabel", "🐻", "care"],
          ["pip", "🐿️", "small steps"],
          ["remy", "🦝", "craft"],
          ["skip", "🐇", "momentum"],
        ].map(([id, emoji, lane]) => ({
          id,
          emoji,
          lane,
          name: id,
          specialty: lane,
          status: id === "vel" ? "active" : "not_met",
          chair: id === "vel",
          species: id,
        })),
      };
    } else if (path === "/api/v1/rooms/current") {
      body = {
        room: {
          id: "room-1",
          type: "daily",
          date: "2026-09-10",
          status: "open",
        },
        chat_available: true,
      };
    } else if (path === "/api/v1/rooms/current/messages") {
      if (route.request().method() === "POST") {
        tasks = tasks.filter((t) => !t.title.startsWith("Pomodoro"));
        const text = route.request().postDataJSON().text;
        messages.push({
          id: 3,
          author: "user",
          author_type: "user",
          content: text,
          at: "2026-09-10T18:02:00Z",
          platform: "app",
        });
        messages.push({
          id: 4,
          author: "vel",
          author_type: "assistant",
          content:
            "Those video tasks are backlogged. Willowden has the clearing today. 🌿",
          at: "2026-09-10T18:03:00Z",
          platform: "app",
        });
        body = {
          ok: true,
          messages: [
            {
              type: "message",
              author: "vel",
              content: messages[3].content,
              room: "room-1",
            },
          ],
        };
      } else body = { messages };
    } else if (
      path.startsWith("/api/v1/tasks/") &&
      !path.endsWith("/status") &&
      route.request().method() === "PATCH"
    ) {
      const id = Number(path.split("/").pop());
      tasks = tasks.map((t) =>
        t.id === id ? { ...t, set_aside_on: "2026-09-10" } : t,
      );
      body = { ok: true, task: tasks.find((t) => t.id === id) };
    } else if (path === "/api/v1/rooms") {
      body = {
        rooms: [
          {
            id: 1,
            room_uuid: "yesterday",
            room_type: "daily",
            status: "closed",
            date: "2026-09-09",
            summary: null,
            summary_line: "A few small steps, and a place to begin again.",
          },
        ],
      };
    } else if (path === "/api/v1/rooms/yesterday/messages") body = { messages };
    else if (path === "/api/v1/cycle") body = { cycle: null };
    else if (path === "/api/v1/rooms/cycle") body = { doors: null };
    else if (path === "/api/v1/arc") body = { arc: null };
    else if (path.endsWith("/status")) body = { ok: true };
    else if (url.port === "8485") {
      if (path === "/v1/focus/finish") focus = { running: false, banked: {} };
      body = { focus, linked: true, line: null, sync_error: null };
    }
    await route.fulfill({ json: body });
  });
  return {
    setOffline: (value: boolean) => {
      offline = value;
    },
    removeVideoTasks: () => {
      tasks = tasks.filter((t) => !t.title.startsWith("Pomodoro"));
    },
    deliver: () =>
      sockets.forEach((socket) =>
        socket.send(
          JSON.stringify({
            type: "message",
            author: "vel",
            content: "Your tasks have changed.",
            room: "room-1",
          }),
        ),
      ),
    setFocus: (value: FocusState) => {
      focus = value;
      sidecars.forEach((socket) =>
        socket.send(JSON.stringify({ type: "state", focus })),
      );
    },
  };
}

test("chat changes refresh the companion without reopening it", async ({
  page,
  context,
}) => {
  await mockWorkspace(context);
  const companion = await context.newPage();
  await companion.setViewportSize({ width: 320, height: 560 });
  await companion.goto("/deer.html");
  await expect(
    companion.getByText("Pomodoro 2: record", { exact: false }),
  ).toBeVisible();
  await page.goto("/");
  await page.getByRole("button", { name: "Enter today’s room" }).click();
  await page
    .getByRole("textbox")
    .fill("Backlog all today’s Pomodoro video tasks.");
  await page.getByRole("button", { name: "send", exact: true }).click();
  await expect(
    companion.getByText("Pomodoro 2: record", { exact: false }),
  ).toHaveCount(0);
  await expect(
    companion.getByText("Chordial Willowden:", { exact: false }),
  ).toBeVisible();
  await expect(companion.getByText("tasks up to date")).toBeVisible();
});

test("server deliveries and companion edits update an already-open home", async ({
  page,
  context,
}) => {
  const mock = await mockWorkspace(context);
  await page.goto("/");
  await expect(
    page.getByText("Pomodoro 2: record", { exact: false }),
  ).toBeVisible();
  mock.removeVideoTasks();
  mock.deliver();
  await expect(
    page.getByText("Pomodoro 2: record", { exact: false }),
  ).toHaveCount(0);
  const companion = await context.newPage();
  await companion.goto("/deer.html");
  await companion
    .getByRole("button", {
      name: "Chordial Willowden: sketch the first clearing",
      exact: true,
    })
    .click();
  await companion
    .getByRole("button", { name: "set aside for today", exact: true })
    .click();
  await expect(page.locator(".task-group.muted")).toContainText(
    "Chordial Willowden",
  );
});

test("failed task reads keep the last list, show a warning, and recover on retry", async ({
  page,
  context,
}) => {
  const mock = await mockWorkspace(context);
  await page.goto("/deer.html");
  await expect(page.getByText("tasks up to date")).toBeVisible();
  mock.setOffline(true);
  await page.getByRole("button", { name: "Refresh tasks" }).click();
  await expect(
    page.getByText("can’t reach your tasks — retrying"),
  ).toBeVisible();
  await expect(
    page.getByText("Pomodoro 2: record", { exact: false }),
  ).toBeVisible();
  mock.setOffline(false);
  mock.removeVideoTasks();
  await page.getByRole("button", { name: "Refresh tasks" }).click();
  await expect(page.getByText("tasks up to date")).toBeVisible();
  await expect(
    page.getByText("Pomodoro 2: record", { exact: false }),
  ).toHaveCount(0);
});

test("companion refreshes on focus and after 30 seconds with no main window", async ({
  page,
  context,
}) => {
  const mock = await mockWorkspace(context);
  await page.clock.install();
  await page.goto("/deer.html");
  await expect(page.getByText("tasks up to date")).toBeVisible();
  mock.setOffline(true);
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(
    page.getByText("can’t reach your tasks — retrying"),
  ).toBeVisible();
  mock.setOffline(false);
  mock.removeVideoTasks();
  await page.clock.fastForward(30_000);
  await expect(
    page.getByText("Pomodoro 2: record", { exact: false }),
  ).toHaveCount(0);
  await expect(page.getByText("tasks up to date")).toBeVisible();
});

test("sidebar opens and reopens the companion in browser preview", async ({
  page,
  context,
}) => {
  await mockWorkspace(context);
  await page.goto("/");
  const first = context.waitForEvent("page");
  await page.getByRole("button", { name: "open companion" }).click();
  const companion = await first;
  await expect(companion.getByText("tasks up to date")).toBeVisible();
  await companion.close();
  const second = context.waitForEvent("page");
  await page.getByRole("button", { name: "open companion" }).click();
  await expect((await second).getByText("tasks up to date")).toBeVisible();
});

test("layouts: home, room, archive, companion and timer", async ({
  page,
  context,
}) => {
  const mock = await mockWorkspace(context);
  // no stored choice: the system preference decides, so ask for dark
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/");
  await expect(page.getByText("tasks up to date")).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: "artifacts/home.png" });
  await page.getByRole("button", { name: "Enter today’s room" }).click();
  await expect(
    page.getByText("settles into the clearing", { exact: false }),
  ).toBeVisible();
  await page.screenshot({ path: "artifacts/room.png" });
  await page.setViewportSize({ width: 760, height: 520 });
  await expect(
    page.getByRole("button", { name: "open companion" }),
  ).toBeInViewport();
  await page.screenshot({ path: "artifacts/room-small.png" });
  await page.setViewportSize({ width: 1100, height: 720 });
  await page.getByRole("button", { name: "home", exact: true }).click();
  await page.getByRole("button", { name: /Wed, Sep 9/ }).click();
  await expect(
    page.getByText("settles into the clearing", { exact: false }),
  ).toBeVisible();
  await page.screenshot({ path: "artifacts/archive.png" });
  await page.setViewportSize({ width: 320, height: 560 });
  await page.goto("/deer.html");
  await expect(page.getByText("tasks up to date")).toBeVisible();
  await page
    .getByRole("button", {
      name: "Chordial Willowden: sketch the first clearing",
      exact: true,
    })
    .click();
  await page.screenshot({ path: "artifacts/companion.png" });
  mock.setFocus({
    running: true,
    banked: {},
    task_id: 3,
    label: "Chordial Willowden: sketch the first clearing",
    started_at: new Date(Date.now() - 432_000).toISOString(),
    target_minutes: 25,
  });
  await page.setViewportSize({ width: 440, height: 56 });
  await expect(page.getByRole("button", { name: "show tasks" })).toBeVisible();
  await page.screenshot({ path: "artifacts/timer.png" });
});

test("link screen uses the app identity", async ({ page, context }) => {
  await mockWorkspace(context, false);
  await page.goto("/");
  await expect(page.getByRole("button", { name: "link device" })).toBeVisible();
  await page
    .locator(".link-mark img")
    .evaluate((img: HTMLImageElement) => img.decode());
  await page.screenshot({ path: "artifacts/link.png" });
});

test("an initial task outage is never presented as an empty day", async ({
  page,
  context,
}) => {
  const mock = await mockWorkspace(context);
  mock.setOffline(true);
  await page.goto("/deer.html");
  await expect(
    page.getByText("can’t reach your tasks — retrying"),
  ).toBeVisible();
  await expect(page.getByText("tasks unavailable")).toBeVisible();
  await expect(
    page.getByText("nothing on the list yet", { exact: false }),
  ).toHaveCount(0);
  mock.setOffline(false);
  await page.getByRole("button", { name: "Refresh tasks" }).click();
  await expect(page.getByText("tasks up to date")).toBeVisible();
});

test("interface omits decorative labels", async ({ page, context }) => {
  await mockWorkspace(context);
  await page.goto("/");
  await expect(page.getByText("tasks up to date")).toBeVisible();
  await expect(
    page.getByText(
      /your daily clearing|a little room to grow|the council|beside you|a place to untangle|the den|the burrow|the meadow|the clearing/,
    ),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "open companion" }),
  ).toBeVisible();
});

const RUNNING = {
  running: true,
  banked: {},
  task_id: 3,
  label: "Chordial Willowden: sketch the first clearing",
  started_at: new Date(Date.now() - 432_000).toISOString(),
  target_minutes: 25,
};

/** the bar's two-press finish: the first press asks, the second lands */
async function finishFromBar(page: Page) {
  await page.getByRole("button", { name: "finished", exact: true }).click();
  await page.getByRole("button", { name: /early/ }).click();
}

test("finishing a block draws leaves, survives resize and re-render, then clears", async ({
  page,
  context,
}) => {
  const mock = await mockWorkspace(context);
  await page.clock.install();
  await page.setViewportSize({ width: 320, height: 560 });
  await page.goto("/deer.html");
  await expect(page.getByText("tasks up to date")).toBeVisible();
  // the page's fake clock runs a few ms ahead of this process: pause just ahead
  await page.clock.pauseAt(Date.now() + 1_000);
  // quieter actions do not celebrate: adding a task draws nothing
  await page.getByRole("textbox", { name: "New task title" }).fill("a task");
  await page.getByRole("button", { name: "Add task" }).click();
  await expect(page.locator(".leaf-flourish")).toHaveCount(0);
  mock.setFocus(RUNNING);
  await page.setViewportSize({ width: 440, height: 56 });
  await finishFromBar(page);
  const leaves = page.locator(".leaf-flourish");
  await expect(leaves).toHaveCount(1);
  await page.clock.runFor(300);
  expect(
    await leaves.evaluate((canvas: HTMLCanvasElement) => {
      const pixels = canvas
        .getContext("2d")!
        .getImageData(0, 0, canvas.width, canvas.height).data;
      return pixels.some((value, index) => index % 4 === 3 && value > 0);
    }),
  ).toBe(true);
  await page.screenshot({ path: "artifacts/leaf-flourish.png" });
  // the clock stopped, so the window grows back to the full form mid-burst
  await page.setViewportSize({ width: 320, height: 560 });
  await page.clock.runFor(300);
  await expect(leaves).toHaveJSProperty("height", 560);
  await page.clock.runFor(1050);
  await expect(leaves).toHaveCount(0);
});

test("a failed finish and reduced motion do not show leaves", async ({
  page,
  context,
}) => {
  const mock = await mockWorkspace(context);
  await page.setViewportSize({ width: 440, height: 56 });
  await page.goto("/deer.html");
  mock.setFocus(RUNNING);
  await context.route("**/v1/focus/finish", (route) =>
    route.fulfill({ status: 503, json: { error: "finish failed" } }),
  );
  await finishFromBar(page);
  await expect(page.getByText("finish failed")).toBeVisible();
  await expect(page.locator(".leaf-flourish")).toHaveCount(0);
  await context.unroute("**/v1/focus/finish");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await finishFromBar(page);
  await expect(
    page.getByRole("button", { name: "finished", exact: true }),
  ).toHaveCount(0);
  await expect(page.locator(".leaf-flourish")).toHaveCount(0);
});

test("the theme switch is shared by both windows and remembered", async ({
  page,
  context,
}) => {
  await mockWorkspace(context);
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/");
  await expect(page.getByText("tasks up to date")).toBeVisible();
  const html = page.locator("html");
  await expect(html).toHaveAttribute("data-theme", "dark");
  const toggle = page.getByRole("switch", { name: "theme" });
  await expect(toggle).toHaveAttribute("aria-checked", "true");
  await toggle.click();
  await expect(html).toHaveAttribute("data-theme", "light");
  await expect(toggle).toHaveAttribute("aria-checked", "false");
  await page.screenshot({ path: "artifacts/home-light.png" });
  // the companion, already open, follows without a reload
  const companion = await context.newPage();
  await companion.setViewportSize({ width: 320, height: 560 });
  await companion.goto("/deer.html");
  await expect(companion.locator("html")).toHaveAttribute("data-theme", "light");
  await companion.screenshot({ path: "artifacts/companion-light.png" });
  await toggle.click();
  await expect(companion.locator("html")).toHaveAttribute("data-theme", "dark");
  // the choice survives a reload
  await page.reload();
  await expect(html).toHaveAttribute("data-theme", "dark");
  await expect(page.getByRole("switch", { name: "theme" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
});
