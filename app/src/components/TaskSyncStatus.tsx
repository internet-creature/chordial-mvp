interface Props {
  error: string | null;
  updatedAt: Date | null;
  onRefresh: () => void;
}

export default function TaskSyncStatus({ error, updatedAt, onRefresh }: Props) {
  return (
    <div className={`task-sync${error ? " stale" : ""}`}>
      <span
        role="status"
        title={
          updatedAt
            ? `Last checked at ${updatedAt.toLocaleTimeString()}`
            : undefined
        }
      >
        <span className="status-light" aria-hidden="true" />
        {error ?? (updatedAt ? "tasks up to date" : "loading your tasks…")}
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
