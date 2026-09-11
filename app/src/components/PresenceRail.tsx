import type { CouncilMember } from "../api/types";
import { memberHue } from "../lib/council";
import { useState } from "react";
import { showCompanion } from "../lib/tauriWindow";
import { useTheme } from "../lib/useTheme";

interface Props {
  council: CouncilMember[];
  view: "home" | "room" | "archive" | "cycle" | "stuck";
  onNavigate: (view: "home" | "room") => void;
}

/** the left rail: where you are, and who lives here. unmet members render
 * dimmed - residents you haven't been introduced to yet, not absences. */
export default function PresenceRail({ council, view, onNavigate }: Props) {
  const visible = council.filter((m) => m.status !== "declined");
  const [error, setError] = useState<string | null>(null);
  const [theme, setTheme] = useTheme();

  return (
    <nav className="rail" aria-label="Main navigation">
      <button
        className="rail-mark"
        onClick={() => onNavigate("home")}
        title="home"
      >
        <img src="/chordial-icon.png" alt="" /> <span>chordial</span>
      </button>

      <div className="rail-nav">
        <button
          className={view === "home" ? "rail-link active" : "rail-link"}
          onClick={() => onNavigate("home")}
          aria-current={view === "home" ? "page" : undefined}
        >
          <span className="nav-glyph" aria-hidden="true">
            ⌂
          </span>{" "}
          home
        </button>
        <button
          className={view === "room" ? "rail-link active" : "rail-link"}
          onClick={() => onNavigate("room")}
          aria-current={view === "room" ? "page" : undefined}
        >
          <span className="nav-glyph" aria-hidden="true">
            ›_
          </span>{" "}
          today’s room
        </button>
      </div>

      <div className="rail-council">
        <ul>
          {visible.map((m) => {
            const met = m.status === "active" || m.status === "introducing";
            return (
              <li
                key={m.id}
                className={met ? "member" : "member unmet"}
                title={met ? m.specialty : "not yet introduced"}
              >
                <span
                  className="member-avatar"
                  style={{ borderColor: met ? memberHue(m.id) : "transparent" }}
                  aria-hidden="true"
                >
                  {m.emoji}
                </span>
                <span className="member-text">
                  <span className="member-name">{m.name}</span>
                  <span className="member-lane">
                    {met ? m.lane : "not yet met"}
                  </span>
                </span>
              </li>
            );
          })}
        </ul>
      </div>
      <div className="rail-footer">
        <button
          className="companion-launch"
          onClick={() => {
            setError(null);
            void showCompanion().catch((err) =>
              setError(
                err instanceof Error
                  ? err.message
                  : "Couldn’t open the companion. Try Show companion in the tray menu.",
              ),
            );
          }}
        >
          <span aria-hidden="true">↗</span> open companion
        </button>
        {error && (
          <p className="link-error" role="alert">
            {error}
          </p>
        )}
        <div className="theme-switch">
          <span id="theme-label">theme</span>
          <button
            className="theme-track"
            role="switch"
            aria-checked={theme === "dark"}
            aria-labelledby="theme-label"
            title={theme === "dark" ? "switch to light" : "switch to dark"}
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          >
            <span className="theme-notch" aria-hidden="true">
              ☀
            </span>
            <span className="theme-notch" aria-hidden="true">
              ☾
            </span>
            <span className="theme-knob" aria-hidden="true" />
          </button>
        </div>
      </div>
    </nav>
  );
}
