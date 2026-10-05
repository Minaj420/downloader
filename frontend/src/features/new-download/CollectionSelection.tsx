import type { CollectionInfo } from "../../api/analysis";
import { formatDuration } from "./format";

interface CollectionSelectionProps {
  collection: CollectionInfo;
  /** Snapshot-scoped backend item ids currently selected. */
  selectedItemIds: readonly string[];
  /** Pending analyze/create must not mutate the snapshot selection. */
  disabled: boolean;
  onToggleItem: (itemId: string) => void;
  onSelectAll: () => void;
  onDeselectAll: () => void;
}

const UNAVAILABLE_REASON_LABELS: Record<string, string> = {
  invalid_url: "Not selectable: no usable direct link in this snapshot",
  unavailable: "Not selectable: unavailable in this snapshot",
};

/**
 * Selectable item list for an analysis-window collection snapshot. Rows keep
 * the raw source order provided by the backend, are keyed by the stable
 * snapshot `item_id` (never by nullable/duplicated source ids) and show the
 * source's own positions without renumbering. Select All only covers the
 * analyzed, selectable items of this snapshot, within the backend cap.
 */
export function CollectionSelection({
  collection,
  selectedItemIds,
  disabled,
  onToggleItem,
  onSelectAll,
  onDeselectAll,
}: CollectionSelectionProps) {
  const selectedCount = selectedItemIds.length;
  const atCap = selectedCount >= collection.selected_item_limit;
  const sourceTotalLabel = collection.total_items_known
    ? `${collection.total_items} items in source`
    : `At least ${collection.total_items} items in source`;

  return (
    <section className="collection-preview" aria-label="Collection items">
      <h3>
        {collection.analyzed_items} analyzed · {collection.selectable_items}{" "}
        selectable · {selectedCount} selected
      </h3>
      <p className="muted-note">{sourceTotalLabel}</p>
      <div className="result-actions">
        <button
          type="button"
          className="button-secondary"
          onClick={onSelectAll}
          disabled={disabled || atCap || collection.selectable_items === 0}
        >
          Select All
        </button>
        <button
          type="button"
          className="button-secondary"
          onClick={onDeselectAll}
          disabled={disabled || selectedCount === 0}
        >
          Deselect All
        </button>
      </div>
      <ul>
        {collection.items.map((item) => {
          const checked = selectedItemIds.includes(item.item_id);
          const reasonLabel = item.unavailable_reason != null
            ? UNAVAILABLE_REASON_LABELS[item.unavailable_reason]
            : null;
          const duration = formatDuration(item.duration_seconds);
          return (
            <li key={item.item_id} className="collection-item">
              <label className="format-option">
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={disabled || !item.selectable || (atCap && !checked)}
                  onChange={() => onToggleItem(item.item_id)}
                />
                {item.position != null && (
                  <span className="item-position">
                    {String(item.position).padStart(2, "0")}
                  </span>
                )}
                <span className="item-title">{item.title ?? "Untitled"}</span>
                {duration && <span className="meta-item">{duration}</span>}
                {!item.selectable && (
                  <span className="muted-note">
                    {reasonLabel ?? "Not selectable"}
                  </span>
                )}
              </label>
            </li>
          );
        })}
      </ul>
      <p className="muted-note">{collection.selection_limit_message}</p>
      {collection.selection_truncated && (
        <p className="muted-note">
          Selection covers only the {collection.analyzed_items} analyzed items
          of this snapshot; items outside this window are not included.
        </p>
      )}
    </section>
  );
}
