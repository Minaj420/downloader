import type {
  LibraryListItem,
  LibraryMediaFile,
} from "../../api/library";
import { formatFileSize } from "../new-download/format";
import {
  formatLibraryDate,
  mediaTypeLabel,
  storageTypeLabel,
} from "./display";

interface LibraryTableProps {
  /** Newest-first page from the API; rendered in the received order. */
  items: LibraryListItem[];
  selectedItemId: string | null;
  /**
   * True while a removal request is in flight: every item selection
   * control (desktop table and the mobile card layout share this DOM)
   * sits disabled so the removal context cannot change mid-flight.
   */
  selectionDisabled: boolean;
  onSelect: (itemId: string) => void;
}

function copySize(copy: LibraryMediaFile): string {
  return formatFileSize(copy.size_bytes) ?? "—";
}

function copyLocation(copy: LibraryMediaFile): string {
  return storageTypeLabel(copy.storage_type);
}

function copyPath(copy: LibraryMediaFile): string {
  return copy.display_path ?? copy.path;
}

/**
 * Data cell with a real DOM field label. The label names the field inside
 * the mobile card layout; on desktop it is hidden because the column
 * headers carry the same text (see styles.css). Item-level fields span the
 * whole item row group via `rowSpan` so the physical-copy rows only carry
 * their own Size/Location/Path cells.
 */
function Cell({
  label,
  mono = false,
  rowSpan,
  children,
}: {
  label: string;
  mono?: boolean;
  rowSpan?: number;
  children: string;
}) {
  return (
    <td rowSpan={rowSpan}>
      <span className="library-cell-label">{label}</span>
      <span
        className={
          mono ? "library-cell-value library-path" : "library-cell-value"
        }
      >
        {children}
      </span>
    </td>
  );
}

/**
 * Scannable Library list (PRODUCT_SPEC §20, UI_DIRECTION §7): a desktop
 * table with the spec's columns that collapses into stacked cards on
 * mobile via CSS — the same fields, no content loss.
 *
 * Each logical item is one tbody row group of `rowCount` rows (rowCount =
 * max(1, physical copies)). The item-level fields (Name, Type, Downloaded
 * At, Format) span the whole group; each physical copy keeps its own row
 * with exactly its Size/Location/Path cells (columns 3/6/7), so one copy's
 * values always share a single semantic group, in stable API order.
 */
export function LibraryTable({
  items,
  selectedItemId,
  selectionDisabled,
  onSelect,
}: LibraryTableProps) {
  return (
    <table className="library-table">
      <thead>
        <tr>
          <th scope="col">Name</th>
          <th scope="col">Type</th>
          <th scope="col">Size</th>
          <th scope="col">Downloaded At</th>
          <th scope="col">Format</th>
          <th scope="col">Location</th>
          <th scope="col">Path</th>
        </tr>
      </thead>
      {items.map((item) => {
        const copies = item.media_files;
        const firstCopy = copies[0];
        const rowCount = Math.max(1, copies.length);
        const isSelected = item.id === selectedItemId;
        return (
          <tbody
            key={item.id}
            className={isSelected ? "library-item-selected" : undefined}
          >
            <tr>
              <th scope="rowgroup" rowSpan={rowCount}>
                <button
                  type="button"
                  className="library-item-select"
                  aria-pressed={isSelected}
                  disabled={selectionDisabled}
                  onClick={() => onSelect(item.id)}
                >
                  {item.title}
                </button>
              </th>
              <Cell label="Type" rowSpan={rowCount}>
                {mediaTypeLabel(item.media_type)}
              </Cell>
              <Cell label="Size">{firstCopy ? copySize(firstCopy) : "—"}</Cell>
              <Cell label="Downloaded At" rowSpan={rowCount}>
                {formatLibraryDate(item.created_at)}
              </Cell>
              <Cell label="Format" rowSpan={rowCount}>
                {item.format ?? "—"}
              </Cell>
              <Cell label="Location">
                {firstCopy ? copyLocation(firstCopy) : "—"}
              </Cell>
              <Cell label="Path" mono>
                {firstCopy ? copyPath(firstCopy) : "—"}
              </Cell>
            </tr>
            {copies.slice(1).map((copy) => (
              <tr key={copy.id} className="library-copy-row">
                <Cell label="Size">{copySize(copy)}</Cell>
                <Cell label="Location">{copyLocation(copy)}</Cell>
                <Cell label="Path" mono>{copyPath(copy)}</Cell>
              </tr>
            ))}
          </tbody>
        );
      })}
    </table>
  );
}
