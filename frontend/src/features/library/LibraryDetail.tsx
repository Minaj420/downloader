import type { UseQueryResult } from "@tanstack/react-query";
import type { LibraryItem, LibraryMediaFile } from "../../api/library";
import { formatDuration, formatFileSize } from "../new-download/format";
import { platformLabel } from "../new-download/platforms";
import {
  formatBitrate,
  formatLibraryDate,
  httpUrlOrNull,
  mediaTypeLabel,
  storageTypeLabel,
} from "./display";

/**
 * Record-only removal action (PRODUCT_SPEC §21, UI_DIRECTION §7): the
 * state lives in the page so a success can clear the selection and the
 * query caches; the detail renders it next to the loaded item only.
 */
export interface LibraryRemoveAction {
  /** True while the removal request is in flight. */
  isPending: boolean;
  disabled: boolean;
  /** Removal failure; the loaded item stays visible for a retry. */
  error: Error | null;
  /** Starts the record-only removal for the loaded item. */
  onRemove: (item: LibraryItem) => void;
  /** Re-runs the last failed removal. */
  onRetry: () => void;
}

interface LibraryDetailProps {
  query: UseQueryResult<LibraryItem>;
  removeAction: LibraryRemoveAction;
  deleteAction: {
    disabled: boolean;
    blockedCopyId: number | null;
    onDelete: (item: LibraryItem, copy: LibraryMediaFile, trigger: HTMLButtonElement) => void;
  };
}

function SourceUrl({ url }: { url: string | null }) {
  const safeUrl = httpUrlOrNull(url);
  if (safeUrl == null) {
    return <>{url ?? "—"}</>;
  }
  return (
    <a href={safeUrl} target="_blank" rel="noreferrer noopener">
      {safeUrl}
    </a>
  );
}

function LoadedDetail({
  item,
  removeAction,
  deleteAction,
}: {
  item: LibraryItem;
  removeAction: LibraryRemoveAction;
  deleteAction: LibraryDetailProps["deleteAction"];
}) {
  // Nullable catalog fields render only when they carry information; raw
  // database ids stay out of the user-facing detail view.
  const duration = formatDuration(item.duration);
  const bitrate = formatBitrate(item.bitrate);
  return (
    <div className="library-detail-body">
      <h3 className="library-detail-title">{item.title}</h3>
      <dl className="library-detail-grid">
        <div>
          <dt>Type</dt>
          <dd>{mediaTypeLabel(item.media_type)}</dd>
        </div>
        <div>
          <dt>Downloaded At</dt>
          <dd>{formatLibraryDate(item.created_at)}</dd>
        </div>
        {item.format != null && (
          <div>
            <dt>Format</dt>
            <dd>{item.format}</dd>
          </div>
        )}
        {item.codec != null && (
          <div>
            <dt>Codec</dt>
            <dd>{item.codec}</dd>
          </div>
        )}
        {bitrate != null && (
          <div>
            <dt>Bitrate</dt>
            <dd>{bitrate}</dd>
          </div>
        )}
        {item.resolution != null && (
          <div>
            <dt>Resolution</dt>
            <dd>{item.resolution}</dd>
          </div>
        )}
        {duration != null && (
          <div>
            <dt>Duration</dt>
            <dd>{duration}</dd>
          </div>
        )}
        {item.artist != null && (
          <div>
            <dt>Artist</dt>
            <dd>{item.artist}</dd>
          </div>
        )}
        {item.album != null && (
          <div>
            <dt>Album</dt>
            <dd>{item.album}</dd>
          </div>
        )}
        {item.platform != null && (
          <div>
            <dt>Platform</dt>
            <dd>{platformLabel(item.platform)}</dd>
          </div>
        )}
        <div>
          <dt>Source URL</dt>
          <dd>
            <SourceUrl url={item.source_url} />
          </dd>
        </div>
      </dl>
      <div>
        <h4 className="library-copies-heading">Physical Copies</h4>
        {item.media_files.length === 0 ? (
          <p className="muted-note">No physical copies recorded.</p>
        ) : (
          <ul className="library-copies">
            {item.media_files.map((copy) => (
              <li key={copy.id} className="library-copy">
                <strong className="library-copy-location">
                  {storageTypeLabel(copy.storage_type)}
                </strong>
                <span className="library-path">
                  {copy.display_path ?? copy.path}
                </span>
                <span className="library-copy-size">
                  {formatFileSize(copy.size_bytes) ?? "—"}
                </span>
                <button
                  type="button"
                  className="button-danger"
                  aria-label={`Delete File: ${storageTypeLabel(copy.storage_type)} — ${copy.display_path ?? copy.path}`}
                  disabled={deleteAction.disabled || deleteAction.blockedCopyId === copy.id}
                  onClick={(event) => deleteAction.onDelete(item, copy, event.currentTarget)}
                >
                  Delete File
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="library-detail-actions">
        <button
          type="button"
          className="button-secondary"
          disabled={removeAction.disabled}
          onClick={() => removeAction.onRemove(item)}
        >
          {removeAction.isPending ? "Removing…" : "Remove from Library"}
        </button>
        <p className="muted-note">
          Record only: removes this item from the Library. The file on disk
          is not deleted.
        </p>
        {removeAction.error != null && (
          <div className="error-banner" role="alert">
            <p>
              {removeAction.error instanceof Error && removeAction.error.message
                ? `Could not remove from the Library: ${removeAction.error.message}`
                : "Could not remove this item from the Library."}
            </p>
            <button
              type="button"
              className="button-secondary"
              disabled={removeAction.disabled}
              onClick={removeAction.onRetry}
            >
              Retry
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Detail panel for the selected Library item (PRODUCT_SPEC §20). Shows
 * loading, error+retry and loaded states; query identity is the selected
 * item id, so a pending response can never leak under a newer selection.
 */
export function LibraryDetail({ query, removeAction, deleteAction }: LibraryDetailProps) {
  return (
    <section className="library-detail" aria-label="Library item details">
      <header className="library-detail-header">
        <h2>Item Details</h2>
      </header>
      {query.isPending ? (
        <div className="library-detail-body">
          <p className="muted-note" role="status">
            Loading details…
          </p>
        </div>
      ) : query.isError ? (
        <div className="library-detail-body">
          <div className="error-banner" role="alert">
            <p>
              {query.error instanceof Error
                ? query.error.message
                : "Could not load the item details."}
            </p>
            <button
              type="button"
              className="button-secondary"
              disabled={removeAction.disabled}
              onClick={() => void query.refetch()}
            >
              Retry
            </button>
          </div>
        </div>
      ) : (
        <LoadedDetail item={query.data} removeAction={removeAction} deleteAction={deleteAction} />
      )}
    </section>
  );
}
