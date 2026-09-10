interface Props {
  error: string | null;
  updatedAt: Date | null;
  onRefresh: () => void;
  /** the day as quantities, shown while the tasks are healthy */
  summary?: string;
  /** a brief chrome notice (an action that did not go through) */
  notice?: string | null;
}

export default function TaskSyncStatus({
  error,
  updatedAt,
  onRefresh,
  summary,
  notice,
}: Props) {
  const tone = notice ? " notice" : error ? " stale" : "";
  const text =
    notice ??
    error ??
    (updatedAt ? (summary ?? "tasks up to date") : "loading your tasks…");
  return (
    <div className={`task-sync${tone}`}>
      <span
        role="status"
        title={
          updatedAt
            ? `Last checked at ${updatedAt.toLocaleTimeString()}`
            : undefined
        }
      >
        <span className="status-light" aria-hidden="true" />
        {text}
      </span>
      <button
        onClick={onRefresh}
        aria-label="Refresh tasks"
        title="Refresh tasks"
      >
        ↻
      </button>
    </div>
  );
}
