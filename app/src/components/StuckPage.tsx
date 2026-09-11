import { useCallback, useEffect, useRef, useState } from "react";
import type { StuckEpisode, StuckProposal, StuckReaction } from "../api/types";
import { fetchStuck, isAuthError, openStuck, reactStuck } from "../api/client";
import { startFocus } from "../api/sidecar";
import { showCompanion } from "../lib/tauriWindow";
import { useToday } from "../lib/useToday";
import {
  containerLabel,
  enoughLine,
  handoffFor,
  KIND_EYEBROW,
  newRequestId,
  POLL_MS,
  restEvidence,
  setWhyHidden,
  STUCK_COPY,
  suggestsSleep,
  THINKING_LINE_MS,
  thinkingLine,
  thingLabel,
  titleMap,
  visibleProposal,
  whyHidden,
  type StuckRequest,
} from "../lib/stuck";

interface Props {
  token: string;
  request: StuckRequest;
  onClose: () => void;
  onAuthLost: () => void;
}

/** the page behind the button (docs/STUCK_MODE_DESIGN.md §2): nothing
 * else of the app on screen. thinking, then ONE card; "something
 * different" rotates the kind, "that's too much" is the rest branch
 * (local until they leave - the too_much reaction lands when they
 * actually rest, so the door back costs nothing). the deer is the door to
 * a fresh turn. */
export default function StuckPage({ token, request, onClose, onAuthLost }: Props) {
  const { today } = useToday(token, onAuthLost);
  const [episode, setEpisode] = useState<StuckEpisode | null>(null);
  const [requestId, setRequestId] = useState(request.request_id);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [resting, setResting] = useState(false);
  const [tick, setTick] = useState(0);
  const [hideWhy, setHideWhy] = useState(() => whyHidden(window.localStorage));
  const [handoffLine, setHandoffLine] = useState<string | null>(null);
  const closedRef = useRef(false);

  const titles = titleMap(today?.buckets);
  const thinking = episode?.status === "thinking";

  const authGuard = useCallback(
    (e: unknown, fallback: string) => {
      if (isAuthError(e)) {
        onAuthLost();
        return;
      }
      setError(e instanceof Error ? e.message : fallback);
    },
    [onAuthLost],
  );

  // one press, one episode. a re-press (the deer) mints a new request id
  // and opens a fresh one; the old episode is closed best-effort.
  useEffect(() => {
    let cancelled = false;
    setEpisode(null);
    setError(null);
    setResting(false);
    setHandoffLine(null);
    setTick(0);
    openStuck(token, {
      surface: request.surface,
      request_id: requestId,
      task_id: request.task_id,
    })
      .then((result) => {
        if (!cancelled) setEpisode(result.episode);
      })
      .catch((e) => {
        if (!cancelled) authGuard(e, STUCK_COPY.errorOpen);
      });
    return () => {
      cancelled = true;
    };
  }, [token, requestId, request.surface, request.task_id, authGuard]);

  // the poll while the house thinks (§5.1: every 1.5s)
  useEffect(() => {
    if (!episode || !thinking) return;
    const controller = new AbortController();
    const id = window.setInterval(() => {
      fetchStuck(token, episode.episode_id, controller.signal)
        .then((result) => setEpisode(result.episode))
        .catch((e) => {
          if (controller.signal.aborted) return;
          if (isAuthError(e)) onAuthLost();
          // a missed poll is not an error the person needs; the next tick asks again
        });
    }, POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(id);
    };
  }, [token, episode, thinking, onAuthLost]);

  // the sub-line rotates while thinking
  useEffect(() => {
    if (!thinking) return;
    const id = window.setInterval(() => setTick((t) => t + 1), THINKING_LINE_MS);
    return () => window.clearInterval(id);
  }, [thinking]);

  const react = useCallback(
    async (reaction: StuckReaction, proposal?: StuckProposal) => {
      if (!episode || busy) return null;
      setBusy(true);
      setError(null);
      try {
        const result = await reactStuck(token, episode.episode_id, {
          reaction,
          generation: episode.generation,
          request_id: newRequestId(),
          proposal_id: proposal?.proposal_id,
        });
        setEpisode(result.episode);
        return result;
      } catch (e) {
        if (isAuthError(e)) {
          onAuthLost();
          return null;
        }
        // 409: the card moved under us (another window, the sweep) - the
        // fresh episode is the honest state
        try {
          const fresh = await fetchStuck(token, episode.episode_id);
          setEpisode(fresh.episode);
          setError(STUCK_COPY.stale);
        } catch {
          setError(e instanceof Error ? e.message : STUCK_COPY.errorReact);
        }
        return null;
      } finally {
        setBusy(false);
      }
    },
    [episode, busy, token, onAuthLost],
  );

  /** leaving: the ledger learns how (rested or closed), best-effort */
  const leave = useCallback(
    (how: "too_much" | "closed") => {
      if (closedRef.current) return;
      closedRef.current = true;
      const terminal =
        !episode ||
        ["accepted", "rested", "closed", "failed"].includes(episode.status);
      if (!terminal && episode) {
        void reactStuck(token, episode.episode_id, {
          reaction: how,
          generation: episode.generation,
          request_id: newRequestId(),
        }).catch(() => undefined);
      }
      onClose();
    },
    [episode, token, onClose],
  );

  const onDifferent = async () => {
    const card = visibleProposal(episode);
    if (!card) return;
    const result = await react("different", card);
    if (result?.outcome === "exhausted") setResting(true);
  };

  const onDoThis = async () => {
    const card = visibleProposal(episode);
    if (!card) return;
    const result = await react("accepted", card);
    if (!result?.execution) return;
    const plan = handoffFor(result.execution, titles);
    if (plan.kind === "start") {
      try {
        await startFocus(plan.taskId, plan.label, plan.minutes);
        await showCompanion().catch(() => undefined);
        setHandoffLine(STUCK_COPY.startedLine);
      } catch (e) {
        setError(e instanceof Error ? e.message : STUCK_COPY.errorReact);
      }
      return;
    }
    setHandoffLine(plan.line);
  };

  const askAgain = () => {
    if (episode && !["accepted", "rested", "closed", "failed"].includes(episode.status)) {
      void reactStuck(token, episode.episode_id, {
        reaction: "closed",
        generation: episode.generation,
        request_id: newRequestId(),
      }).catch(() => undefined);
    }
    setRequestId(newRequestId());
  };

  const toggleWhy = () => {
    const next = !hideWhy;
    setWhyHidden(window.localStorage, next);
    setHideWhy(next);
  };

  // --- render --------------------------------------------------------------

  const card = visibleProposal(episode);
  const failed = episode?.status === "failed";
  const chosen = episode?.status === "accepted";
  const showRest =
    !chosen && !handoffLine &&
    (resting || failed || (episode && !thinking && !card && episode.status !== "rested"
      && episode.status !== "closed"));
  const sleep = suggestsSleep(restEvidence(episode));

  const deer = (
    <button
      className="stuck-deer"
      onClick={askAgain}
      title={STUCK_COPY.askAgain}
      aria-label={STUCK_COPY.askAgain}
      disabled={busy || !episode}
    >
      🦌
    </button>
  );

  return (
    <div className="stuck">
      <div className="stuck-column">
        {deer}

        {error && (
          <p className="stuck-error" role="alert">
            {error}
          </p>
        )}

        {handoffLine ? (
          <>
            <p className="stuck-rest-line">{handoffLine}</p>
            <button className="stuck-do" onClick={() => leave("closed")}>
              {STUCK_COPY.close}
            </button>
          </>
        ) : chosen ? (
          <>
            <p className="stuck-rest-line">{STUCK_COPY.chosenLine}</p>
            <button className="stuck-do" onClick={() => leave("closed")}>
              {STUCK_COPY.close}
            </button>
          </>
        ) : !episode || thinking ? (
          <div className="stuck-thinking" role="status" aria-live="polite">
            <h1 className="stuck-head">
              {episode && episode.generation > 1
                ? STUCK_COPY.differentAngleHead
                : STUCK_COPY.thinkingHead}
            </h1>
            <span className="stuck-breath" aria-hidden="true" />
            <p className="stuck-sub">{thinkingLine(tick)}</p>
            <button className="stuck-alt stuck-leave" onClick={() => leave("closed")}>
              {STUCK_COPY.close}
            </button>
          </div>
        ) : showRest ? (
          <div className="stuck-rest">
            {failed ? (
              <p className="stuck-rest-line">{STUCK_COPY.failedHead}</p>
            ) : (
              <>
                <p className="stuck-rest-line">{STUCK_COPY.restLine}</p>
                <p className="stuck-rest-line">{STUCK_COPY.restLineTwo}</p>
              </>
            )}
            {!failed && sleep && (
              <p className="stuck-sub">{STUCK_COPY.restSleep}</p>
            )}
            <button
              className="stuck-do"
              onClick={() => leave(failed ? "closed" : "too_much")}
            >
              {STUCK_COPY.close}
            </button>
            {failed ? (
              <button className="stuck-alt" onClick={askAgain} disabled={busy}>
                {STUCK_COPY.tryHouseAgain}
              </button>
            ) : card ? (
              <button className="stuck-alt" onClick={() => setResting(false)}>
                {STUCK_COPY.oneSmallThing}
              </button>
            ) : null}
          </div>
        ) : card ? (
          <>
            <p className="stuck-intro">
              {episode.source === "fallback"
                ? STUCK_COPY.fallbackHead
                : STUCK_COPY.cardIntro}
            </p>
            <article className="stuck-card" aria-live="polite">
              <p className="stuck-kind">{KIND_EYEBROW[card.kind]}</p>
              <p className="stuck-line">{card.line}</p>
              {thingLabel(card, titles) && (
                <span className="stuck-thing">{thingLabel(card, titles)}</span>
              )}
              <p className="stuck-enough">{enoughLine(card.enough)}</p>
              {containerLabel(card) && (
                <span className="stuck-container">{containerLabel(card)}</span>
              )}
              {card.why && !hideWhy && (
                <p className="stuck-why">{card.why}</p>
              )}
              <div className="stuck-actions">
                <button className="stuck-do" onClick={onDoThis} disabled={busy}>
                  {STUCK_COPY.doThis}
                </button>
                <button className="stuck-alt" onClick={onDifferent} disabled={busy}>
                  {STUCK_COPY.different}
                </button>
                <button
                  className="stuck-alt"
                  onClick={() => setResting(true)}
                  disabled={busy}
                >
                  {STUCK_COPY.tooMuch}
                </button>
              </div>
            </article>
            {episode.source === "fallback" && (
              <button className="stuck-alt stuck-foot" onClick={askAgain} disabled={busy}>
                {STUCK_COPY.tryHouseAgain}
              </button>
            )}
            {card.why && (
              <button className="stuck-alt stuck-foot" onClick={toggleWhy}>
                {hideWhy ? STUCK_COPY.showWhy : STUCK_COPY.hideWhy}
              </button>
            )}
          </>
        ) : null}
      </div>
    </div>
  );
}
