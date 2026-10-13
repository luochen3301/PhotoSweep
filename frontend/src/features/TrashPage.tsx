import {
  ArrowCounterClockwise,
  Check,
  LockKey,
  Trash,
  Warning,
} from "@phosphor-icons/react";
import { useEffect, useMemo, useState } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Button, EmptyState, ErrorState, Input, Modal } from "../components/ui";
import { Thumbnail } from "../components/Thumbnail";
import { MAX_SELECTION, nextPage, selectLoaded, toggleSelection } from "../lib/selection";
import { useLibraryScope } from "../app/LibraryScope";
import { api, formatBytes, formatDate } from "../lib/api";
import type { TrashItem } from "../types";

export function TrashPage() {
  const { libraryId } = useLibraryScope();
  return <TrashView key={libraryId} libraryId={libraryId} />;
}

function TrashView({ libraryId }: { libraryId: string }) {
  const queryClient = useQueryClient();
  const [selection, setSelection] = useState<Set<number>>(new Set());
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [password, setPassword] = useState("");
  const trash = useInfiniteQuery({
    queryKey: ["trash", libraryId],
    initialPageParam: 1,
    queryFn: ({ pageParam }) => api<TrashItem[]>(`/trash?limit=100&page=${pageParam}${libraryId ? `&library_ids=${libraryId}` : ""}`),
    getNextPageParam: (page) => nextPage(page.meta),
  });
  const restore = useMutation({
    mutationFn: () =>
      api("/trash/actions/restore", {
        method: "POST",
        body: { trash_item_ids: [...selection] },
      }),
    onSuccess: () => {
      setSelection(new Set());
      queryClient.invalidateQueries({ queryKey: ["trash"] });
      queryClient.invalidateQueries({ queryKey: ["media"] });
      queryClient.invalidateQueries({ queryKey: ["summary"] });
      queryClient.invalidateQueries({ queryKey: ["duplicate-groups"] });
      queryClient.invalidateQueries({ queryKey: ["duplicate-members"] });
    },
    onError: () => {
      setSelection(new Set());
      for (const key of ["trash", "media", "summary", "file-operations"]) void queryClient.invalidateQueries({ queryKey: [key] });
    },
  });
  const permanent = useMutation({
    mutationFn: () =>
      api("/trash/actions/permanent-delete", {
        method: "POST",
        body: { trash_item_ids: [...selection], password },
      }),
    onSuccess: () => {
      setDeleteOpen(false);
      setPassword("");
      setSelection(new Set());
      queryClient.invalidateQueries({ queryKey: ["trash"] });
    },
    onError: () => {
      void queryClient.invalidateQueries({ queryKey: ["trash"] });
      void queryClient.invalidateQueries({ queryKey: ["file-operations"] });
    },
  });
  const items = useMemo(() => trash.data?.pages.flatMap((page) => page.data) ?? [], [trash.data]);
  useEffect(() => {
    if (!trash.data) return;
    const loaded = new Set(items.map((item) => item.id));
    setSelection((current) => {
      const next = new Set([...current].filter((id) => loaded.has(id)));
      return next.size === current.size ? current : next;
    });
  }, [items, trash.data]);
  const hasReadOnlySelection = items.some((item) => selection.has(item.id) && !item.library_writable);
  const busy = restore.isPending || permanent.isPending;
  const selectedSize = items
    .filter((item) => selection.has(item.id))
    .reduce((sum, item) => sum + item.size, 0);

  function toggle(id: number) {
    if (!busy) setSelection((current) => toggleSelection(current, id));
  }
  function closeDelete() { if (!permanent.isPending) { setDeleteOpen(false); setPassword(""); permanent.reset(); } }

  return (
    <section className="page trash-page">
      <header className="page-heading">
        <div>
          <p className="eyebrow">可恢复区域</p>
          <h1>回收站</h1>
          <p>文件只有在这里执行永久删除后才会真正移除。</p>
          <p>{trash.data?.pages[0].meta.total ?? 0} 项 · 已加载 {items.length} 项 · 每次最多选择 {MAX_SELECTION} 项</p>
        </div>
        {items.length > 0 && (
          <Button disabled={busy} onClick={() => setSelection(selectLoaded(items))}>
            选择已加载项目
          </Button>
        )}
      </header>
      {trash.error && <ErrorState error={trash.error} retry={() => trash.refetch()} />}
      {trash.isLoading && <div className="grid-loading">正在加载回收站…</div>}
      {items.length === 0 && !trash.isLoading && !trash.error ? (
        <EmptyState
          icon={<Trash size={32} />}
          title="回收站是空的"
          description="从审核页移入回收站的文件会保留在这里，直到你恢复或永久删除。"
        />
      ) : (
        <div className="trash-list">
          {items.map((item) => (
            <button
              key={item.id}
              className={selection.has(item.id) ? "trash-row selected" : "trash-row"}
              onClick={() => toggle(item.id)}
              aria-pressed={selection.has(item.id)}
              aria-label={`选择 ${item.filename}`}
              disabled={busy}
            >
              <span className="media-check">
                {selection.has(item.id) && <Check size={13} weight="bold" />}
              </span>
              <span className="trash-thumbnail"><Thumbnail src={`/api/v1/trash/${item.id}/thumbnail?size=256`} /></span>
              <span className="trash-row__name">
                <strong>{item.filename}</strong>
                <small>
                  {item.library_name} / {item.original_relative_path}{!item.library_writable && " · 原图库只读"}
                </small>
              </span>
              <span>
                <strong>{formatBytes(item.size)}</strong>
                <small>{item.category_snapshot.map((category) => ({ screenshot: "截图", screen_recording: "屏幕录制", app_image: "App 图片" })[category] ?? category).join(" · ") || "未分类"}</small>
              </span>
              <span>
                <strong>移入时间</strong>
                <small>{formatDate(item.moved_at)}</small>
              </span>
            </button>
          ))}
        </div>
      )}
      {trash.hasNextPage && <Button className="load-more" disabled={trash.isFetchingNextPage} onClick={() => trash.fetchNextPage()}>{trash.isFetchingNextPage ? "正在加载…" : "加载更多回收站项目"}</Button>}
      {selection.size > 0 && (
        <div className="selection-bar selection-bar--page">
          <span className="selection-bar__check">
            <Check size={15} />
          </span>
          <strong>已选择 {selection.size} 项</strong>
          <span>{formatBytes(selectedSize)}</span>
          <button disabled={busy} onClick={() => setSelection(new Set())}>清空选择</button>
          <div />
          <Button onClick={() => restore.mutate()} disabled={busy || hasReadOnlySelection} title={hasReadOnlySelection ? "原图库只读，不能恢复" : "同名文件会自动重命名，不会覆盖"}>
            <ArrowCounterClockwise size={16} />
            恢复
          </Button>
          {hasReadOnlySelection && <small className="selection-note">原图库只读，不能恢复</small>}
          <Button variant="danger" disabled={busy} onClick={() => setDeleteOpen(true)}>
            <Trash size={16} />
            永久删除
          </Button>
        </div>
      )}
      {deleteOpen && (
        <Modal title="确认永久删除" onClose={closeDelete}>
          <div className="danger-summary">
            <Warning size={22} weight="fill" />
            <p>
              将永久删除 <strong>{selection.size}</strong> 个文件，共{" "}
              <strong>{formatBytes(selectedSize)}</strong>。此操作无法恢复。
            </p>
          </div>
          <label>
            重新输入管理员密码
            <span className="password-input">
              <LockKey size={17} />
              <Input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoFocus
                autoComplete="current-password"
              />
            </span>
          </label>
          {permanent.error && <div className="form-error">{permanent.error.message}</div>}
          <footer>
            <Button disabled={permanent.isPending} onClick={closeDelete}>取消</Button>
            <Button
              variant="danger"
              disabled={!password || permanent.isPending || selection.size === 0}
              onClick={() => permanent.mutate()}
            >
              {permanent.isPending ? "正在验证并删除…" : "永久删除"}
            </Button>
          </footer>
        </Modal>
      )}
      {restore.error && <div className="toast-error">{restore.error.message}</div>}
    </section>
  );
}
