import { useEffect, useRef, useState } from "react";
import { useIsMutating, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  LIBRARY_PAGE_SIZE,
  deleteLibraryFile,
  getLibraryItem,
  removeLibraryItem,
  useLibraryItem,
  useLibraryList,
} from "../api/library";
import type {
  LibraryItem,
  LibraryListResult,
  LibraryMediaFile,
} from "../api/library";
import { ApiRequestError } from "../api/client";
import { storageTypeLabel } from "../features/library/display";
import { LibraryDetail } from "../features/library/LibraryDetail";
import { LibraryTable } from "../features/library/LibraryTable";

/** Mutation input: the catalog id to delete plus the title for messages. */
interface RemoveVariables {
  itemId: string;
  title: string;
}

interface DeleteVariables extends RemoveVariables {
  copy: LibraryMediaFile;
}

const libraryOperationKey = ["library", "operation"];

function deletedFileMessage(target: DeleteVariables) {
  return `Deleted file for “${target.title}”: ${storageTypeLabel(target.copy.storage_type)} — ${target.copy.display_path ?? target.copy.path}.`;
}

/**
 * Library page (PRODUCT_SPEC §20–21, ARCHITECTURE §37): newest-first
 * paginated catalog list plus a detail panel for the selected item. The
 * API order is the display order; no client-side re-sorting. The detail's
 * Remove from Library action is record-only: it deletes the catalog entry,
 * never the physical media file.
 */
export function LibraryPage() {
  const [offset, setOffset] = useState(0);
  const [selectedItemId, setSelectedItemId] = useState<string | null>(null);
  // A fresh object per removal so the focus effect below runs for every
  // success, even when two removed items share a title.
  const [removedNotice, setRemovedNotice] = useState<{ title: string } | null>(
    null,
  );
  const listQuery = useLibraryList(offset);
  const detailQuery = useLibraryItem(selectedItemId);
  const queryClient = useQueryClient();
  const sharedPending = useIsMutating({ mutationKey: libraryOperationKey });
  // Read the cache synchronously too: render notifications can lag behind a
  // second click or a newly mounted observer. Each QueryClient is isolated.
  const operationPending = () => queryClient.isMutating({ mutationKey: libraryOperationKey }) > 0;
  const mutationLock = useRef(false);
  const [deleteTarget, setDeleteTarget] = useState<DeleteVariables | null>(null);
  const [deletedNotice, setDeletedNotice] = useState<string | null>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const deleteStatusRef = useRef<HTMLDivElement>(null);
  const deleteTrigger = useRef<HTMLButtonElement | null>(null);
  const restoreDeleteFocus = useRef(false);
  const pageHeadingRef = useRef<HTMLHeadingElement>(null);
  const shouldFocusPageHeading = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  // Separate GET state: a failed reconciliation must never turn a committed
  // DELETE into a retryable destructive failure. Selection stays locked until
  // this read settles. Query-owned reads prevent cancelled/older responses
  // from overwriting a newer read, independently of the page's lifetime.
  // Keep cache work in the mutation's main callbacks: per-call callbacks
  // depend on a subscribed observer and are not guaranteed after unmount.
  const refreshMutation = useMutation({
    mutationKey: libraryOperationKey,
    mutationFn: async (target: DeleteVariables) => {
      const queryKey = ["library", "detail", target.itemId];
      const previous = queryClient.getQueryData<LibraryItem>(queryKey);
      try {
        const fetched = await queryClient.fetchQuery({
          queryKey, queryFn: () => getLibraryItem(target.itemId), staleTime: 0, retry: false,
        });
        // fetchQuery's raw result may differ from the structurally shared
        // cached value. Query state identity is the generation guard.
        return { item: queryClient.getQueryData<LibraryItem>(queryKey) ?? fetched, state: queryClient.getQueryState(queryKey) };
      } catch (error) {
        if (error instanceof ApiRequestError && error.status === 404) {
          return { item: null, state: queryClient.getQueryState(queryKey) };
        }
        // Keep the already cleaned detail usable on read failure, without
        // restoring it over a newer read. Only the refresh is retryable.
        if (previous && queryClient.getQueryState(queryKey)?.error === error) {
          queryClient.setQueryData(queryKey, previous);
        }
        throw error;
      }
    },
    retry: false,
    onSuccess: async ({ item, state }, target) => {
      const queryKey = ["library", "detail", target.itemId];
      if (!state || queryClient.getQueryState(queryKey) !== state) return;
      // Detail is already reconciled by fetchQuery. Cancel only stale lists,
      // not a newer detail read that may have superseded this operation.
      await queryClient.cancelQueries({ queryKey: ["library", "list"] });
      if (queryClient.getQueryState(queryKey) !== state) return;
      queryClient.setQueriesData<LibraryListResult>(
        { queryKey: ["library", "list"] },
        (previous) => previous ? {
          ...previous,
          items: previous.items.flatMap((cached) => cached.id !== target.itemId ? [cached] : item ? [item] : []),
        } : previous,
      );
      if (!item) {
        if (mounted.current) setSelectedItemId((selected) => selected === target.itemId ? null : selected);
        queryClient.removeQueries({ queryKey: ["library", "detail", target.itemId], exact: true });
      }
      if (mounted.current) setDeletedNotice(`${deletedFileMessage(target)} ${item ? item.media_files.length ? "Other copies remain in the Library." : "The Library item has no physical copies recorded." : "The Library item is no longer present."}`);
    },
    onSettled: (_data, error) => {
      mutationLock.current = false;
      void queryClient.invalidateQueries({ queryKey: ["library", "list"], refetchType: error ? "none" : "active" });
    },
  });

  const deleteMutation = useMutation({
    mutationKey: libraryOperationKey,
    mutationFn: (target: DeleteVariables) => deleteLibraryFile(target.itemId, target.copy.id),
    retry: false,
    onSuccess: async (_result, target) => {
      // Cancel pre-delete reads before updating every cached page: a late
      // background response must not resurrect the deleted copy.
      await queryClient.cancelQueries({ queryKey: ["library"] });
      queryClient.setQueriesData<LibraryListResult>(
        { queryKey: ["library", "list"] },
        (previous) => previous ? {
          ...previous,
          items: previous.items.flatMap((item) => {
            if (item.id !== target.itemId) return [item];
            return [{
              ...item,
              media_files: item.media_files.filter((copy) => copy.id !== target.copy.id),
            }];
          }),
        } : previous,
      );
      queryClient.setQueryData<LibraryItem>(
        ["library", "detail", target.itemId],
        (previous) => previous ? {
          ...previous,
          media_files: previous.media_files.filter((copy) => copy.id !== target.copy.id),
        } : previous,
      );
      if (mounted.current) setDeletedNotice(deletedFileMessage(target));
      // mutateAsync rejects on GET failure, not on DELETE failure. Its own
      // error state offers a read-only retry; absorb rejection here.
      await refreshMutation.mutateAsync(target).catch(() => undefined);
    },
    onSettled: () => { mutationLock.current = false; },
  });

  useEffect(() => {
    if (deleteTarget) cancelRef.current?.focus();
    else if (restoreDeleteFocus.current) {
      restoreDeleteFocus.current = false;
      deleteTrigger.current?.focus();
    }
  }, [deleteTarget]);
  useEffect(() => {
    if (deleteMutation.isPending || deleteMutation.isError) deleteStatusRef.current?.focus();
    if (deletedNotice) pageHeadingRef.current?.focus();
  }, [deleteMutation.isPending, deleteMutation.isError, deletedNotice]);

  const removeMutation = useMutation({
    mutationKey: libraryOperationKey,
    mutationFn: ({ itemId }: RemoveVariables) => removeLibraryItem(itemId),
    onSuccess: (_result, variables) => {
      // Drop the removed item's cached detail so a later re-selection can
      // never resurrect it from the cache...
      queryClient.removeQueries({
        queryKey: ["library", "detail", variables.itemId],
      });
      // ...take it out of the visible page immediately...
      queryClient.setQueryData<LibraryListResult>(
        ["library", "list", { limit: LIBRARY_PAGE_SIZE, offset }],
        (previous) =>
          previous
            ? {
                ...previous,
                items: previous.items.filter(
                  (item) => item.id !== variables.itemId,
                ),
              }
            : previous,
      );
      // ...then re-sync the active list queries with the server's current
      // state (removed items must not linger after a refetch either).
      void queryClient.invalidateQueries({ queryKey: ["library", "list"] });
      if (mounted.current) {
        setSelectedItemId((selected) => selected === variables.itemId ? null : selected);
        setRemovedNotice({ title: variables.title });
      }
    },
    onSettled: () => { mutationLock.current = false; },
  });

  const pageNumber = offset / LIBRARY_PAGE_SIZE + 1;

  // Confirmation and either destructive request lock the selection context.
  // While a request is in flight the whole selection context is
  // locked: item selection and page transitions sit disabled (and their
  // handlers are guarded), so the request always settles against the same
  // selected item and page it started with — a reset cannot cancel an
  // in-flight request, so the context must simply not change under it.
  const isRemoving = sharedPending > 0 || removeMutation.isPending || deleteMutation.isPending || refreshMutation.isPending || deleteTarget != null;

  // While any page request is in flight the pagination controls sit in a
  // safe state (disabled), so rapid repeated clicks can never advance the
  // offset more than one page per transition. The query reflects the new
  // key's pending state in the render the click commits.
  const isChangingPage = listQuery.isPending || listQuery.isFetching;

  // When a user-initiated page transition settles (loaded or failed), move
  // keyboard focus to the page status heading: the pagination shell stays
  // mounted, but the clicked control is disabled while fetching, which
  // would otherwise drop focus to <body>. The ref keeps the effect one-shot
  // so later background refetches do not steal focus back.
  useEffect(() => {
    if (!shouldFocusPageHeading.current) {
      return;
    }
    if (listQuery.isPending || listQuery.isFetching) {
      return;
    }
    shouldFocusPageHeading.current = false;
    pageHeadingRef.current?.focus();
  }, [listQuery.isPending, listQuery.isFetching]);

  // A successful removal unmounts the detail panel and its Remove button,
  // which would drop keyboard focus to <body>. Move it to the page status
  // heading instead — a stable target right above the confirmation message.
  useEffect(() => {
    if (removedNotice == null) {
      return;
    }
    pageHeadingRef.current?.focus();
  }, [removedNotice]);

  const goToOffset = (nextOffset: number) => {
    if (operationPending() || mutationLock.current || isRemoving || isChangingPage) {
      return;
    }
    shouldFocusPageHeading.current = true;
    // A page change hides the previous page's rows, so a selection that is
    // no longer visible (and its detail) is cleared instead of lingering.
    setSelectedItemId(null);
    setRemovedNotice(null);
    setDeletedNotice(null);
    deleteMutation.reset();
    refreshMutation.reset();
    setOffset(nextOffset);
  };

  // Retrying a failed page reload is a page transition too: while a retry
  // is already in flight further clicks are ignored (the button is also
  // disabled), and the settled result moves focus to the page heading.
  const handleListRetry = () => {
    if (operationPending() || mutationLock.current || isRemoving || listQuery.isFetching) {
      return;
    }
    shouldFocusPageHeading.current = true;
    void listQuery.refetch();
  };

  // A pending removal locks the selection context entirely (see the
  // isRemoving guards), so a different item can only be selected once the
  // mutation has settled. At that point closing the notice and resetting
  // the settled mutation is safe: no in-flight request remains, so one
  // item's removal outcome can never leak onto another item's detail.
  const handleSelectItem = (itemId: string) => {
    if (operationPending() || mutationLock.current || isRemoving) {
      return;
    }
    if (itemId !== selectedItemId) {
      setRemovedNotice(null);
      removeMutation.reset();
      deleteMutation.reset();
      refreshMutation.reset();
      setDeletedNotice(null);
    }
    setSelectedItemId(itemId);
  };

  const handleRemove = (item: LibraryItem) => {
    if (operationPending() || mutationLock.current || isRemoving) return;
    mutationLock.current = true;
    deleteMutation.reset();
    refreshMutation.reset();
    setDeletedNotice(null);
    removeMutation.mutate({ itemId: item.id, title: item.title });
  };

  // Retry re-runs the last removal with its original variables; the guard
  // only fires when there is nothing to retry.
  const handleRemoveRetry = () => {
    if (!operationPending() && !mutationLock.current && !isRemoving && removeMutation.variables != null) {
      mutationLock.current = true;
      removeMutation.mutate(removeMutation.variables);
    }
  };

  const handleDelete = (item: LibraryItem, copy: LibraryMediaFile, trigger: HTMLButtonElement) => {
    if (operationPending() || mutationLock.current || isRemoving || item.id !== selectedItemId) return;
    if (deleteMutation.variables?.copy.id === copy.id && deleteMutation.isError &&
        !(deleteMutation.error instanceof ApiRequestError && deleteMutation.error.retryable)) return;
    deleteTrigger.current = trigger;
    removeMutation.reset();
    setRemovedNotice(null);
    setDeletedNotice(null);
    refreshMutation.reset();
    setDeleteTarget({ itemId: item.id, title: item.title, copy: { ...copy } });
  };

  const cancelDelete = () => {
    if (mutationLock.current) return;
    restoreDeleteFocus.current = true;
    setDeleteTarget(null);
  };

  const confirmDelete = () => {
    if (operationPending() || mutationLock.current || !deleteTarget || deleteTarget.itemId !== selectedItemId) return;
    mutationLock.current = true;
    const target = deleteTarget;
    setDeleteTarget(null);
    deleteMutation.mutate(target);
  };

  const items = listQuery.data?.items ?? [];
  // The pagination shell never unmounts once the user is paging: on any
  // offset > 0 page (loading, error or empty) Previous stays reachable so
  // the user can never get stranded past page 1.
  const showPagination =
    offset > 0 || (listQuery.isSuccess && items.length > 0);

  let pageHeadingText = `Page ${pageNumber}`;
  if (listQuery.isPending) {
    pageHeadingText = `Loading page ${pageNumber}`;
  } else if (listQuery.isError) {
    pageHeadingText = `Page ${pageNumber} failed to load`;
  }

  return (
    <section>
      <h1>Library</h1>
      <h2 ref={pageHeadingRef} className="library-page-heading" tabIndex={-1}>
        {pageHeadingText}
      </h2>
      {deletedNotice && <p className="muted-note" role="status">{deletedNotice}</p>}
      {sharedPending > 0 && !deleteMutation.isPending && !refreshMutation.isPending && !removeMutation.isPending && <p role="status">A Library operation is in progress…</p>}
      {refreshMutation.isPending && <p role="status">Refreshing Library state…</p>}
      {refreshMutation.isError && (
        <div className="error-banner" role="alert">
          <p>The file was deleted, but the current Library state could not be verified. {refreshMutation.error.message}</p>
          <button type="button" className="button-secondary" disabled={isRemoving} onClick={() => {
            if (operationPending() || mutationLock.current || isRemoving || !refreshMutation.variables) return;
            mutationLock.current = true;
            void refreshMutation.mutateAsync(refreshMutation.variables).catch(() => undefined);
          }}>Retry Library refresh</button>
        </div>
      )}
      {deleteTarget && (
        <section className="library-detail-body" aria-labelledby="delete-file-confirm-title"
          onKeyDown={(event) => { if (event.key === "Escape") cancelDelete(); }}>
          <h3 id="delete-file-confirm-title">Confirm Delete File</h3>
          <p>Delete the physical file on disk for “{deleteTarget.title}”?</p>
          <p className="library-path">{storageTypeLabel(deleteTarget.copy.storage_type)} — {deleteTarget.copy.display_path ?? deleteTarget.copy.path}</p>
          <p>This permanently deletes only the selected physical copy and its catalog record. If this is the last copy when deleted, the Library item will also be removed.</p>
          <button ref={cancelRef} type="button" className="button-secondary" onClick={cancelDelete}>Cancel</button>
          <button type="button" className="button-danger" onClick={confirmDelete}>Confirm Delete File</button>
        </section>
      )}
      {((deleteMutation.isPending && !deletedNotice) || deleteMutation.isError) && (
        <div ref={deleteStatusRef} tabIndex={-1} className={deleteMutation.isError ? "error-banner" : "muted-note"}
          role={deleteMutation.isError ? "alert" : "status"}>
          {deleteMutation.isPending ? "Deleting selected file…" : <>
            <p>Could not complete Delete File: {deleteMutation.error?.message}</p>
            {deleteMutation.error instanceof ApiRequestError && deleteMutation.error.code === "media_file_catalog_commit_failed" &&
              <p>The physical file was deleted, but the catalog is out of sync. The displayed catalog has not been changed.</p>}
            {deleteMutation.error instanceof ApiRequestError && deleteMutation.error.retryable &&
              <button type="button" className="button-secondary" disabled={isRemoving} onClick={(event) => {
                if (operationPending() || mutationLock.current || isRemoving || !deleteMutation.variables) return;
                deleteTrigger.current = event.currentTarget;
                setDeleteTarget(deleteMutation.variables);
              }}>Retry Delete File</button>}
          </>}
        </div>
      )}
      {removedNotice != null && (
        <p className="muted-note" role="status">
          Removed “{removedNotice.title}” from the Library. The file on disk
          was not deleted.
        </p>
      )}
      {listQuery.isPending ? (
        <p className="placeholder" role="status">
          Loading library…
        </p>
      ) : listQuery.isError ? (
        <div className="error-banner" role="alert">
          <p>
            {listQuery.error instanceof Error
              ? listQuery.error.message
              : "Could not load the Library."}
          </p>
          <button
            type="button"
            className="button-secondary"
            disabled={listQuery.isFetching || isRemoving}
            onClick={handleListRetry}
          >
            Retry
          </button>
        </div>
      ) : items.length === 0 ? (
        <p className="placeholder">
          {offset === 0
            ? "No downloads in the Library yet."
            : "No downloads on this page."}
        </p>
      ) : (
        <div className="library-panel">
          <LibraryTable
            items={items}
            selectedItemId={selectedItemId}
            selectionDisabled={isRemoving}
            onSelect={handleSelectItem}
          />
        </div>
      )}
      {showPagination && (
        <nav className="library-pagination" aria-label="Library pagination">
          <button
            type="button"
            className="button-secondary"
            disabled={offset === 0 || isChangingPage || isRemoving}
            onClick={() =>
              goToOffset(Math.max(0, offset - LIBRARY_PAGE_SIZE))
            }
          >
            Previous
          </button>
          <button
            type="button"
            className="button-secondary"
            disabled={
              !listQuery.data?.has_more || isChangingPage || isRemoving
            }
            onClick={() => goToOffset(offset + LIBRARY_PAGE_SIZE)}
          >
            Next
          </button>
        </nav>
      )}
      {selectedItemId != null && (
        <LibraryDetail
          query={detailQuery}
          deleteAction={{
            disabled: isRemoving,
            blockedCopyId: deleteMutation.isError && !(deleteMutation.error instanceof ApiRequestError && deleteMutation.error.retryable) ? deleteMutation.variables?.copy.id ?? null : null,
            onDelete: handleDelete,
          }}
          removeAction={{
            isPending: removeMutation.isPending,
            disabled: isRemoving,
            error: removeMutation.error,
            onRemove: handleRemove,
            onRetry: handleRemoveRetry,
          }}
        />
      )}
    </section>
  );
}
