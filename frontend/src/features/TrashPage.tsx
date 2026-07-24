import {
  ArrowCounterClockwise,
  Check,
  LockKey,
  Trash,
  Warning,
} from "@phosphor-icons/react";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, EmptyState, Input, Modal } from "../components/ui";
import { api, formatBytes, formatDate } from "../lib/api";
import type { TrashItem } from "../types";

export function TrashPage() {
  const queryClient = useQueryClient();
  const [selection, setSelection] = useState<Set<number>>(new Set());
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [password, setPassword] = useState("");
  const trash = useQuery({
    queryKey: ["trash"],
    queryFn: () => api<TrashItem[]>("/trash?limit=100"),
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
  });
  const items = trash.data?.data ?? [];
  const selectedSize = items
    .filter((item) => selection.has(item.id))
    .reduce((sum, item) => sum + item.size, 0);

  function toggle(id: number) {
    setSelection((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <section className="page trash-page">
      <header className="page-heading">
        <div>
          <p className="eyebrow">可恢复区域</p>
          <h1>回收站</h1>
          <p>文件只有在这里执行永久删除后才会真正移除。</p>
        </div>
        {items.length > 0 && (
          <Button onClick={() => setSelection(new Set(items.map((item) => item.id)))}>
            选择已加载项目
          </Button>
        )}
      </header>
      {items.length === 0 && !trash.isLoading ? (
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
            >
              <span className="media-check">
                {selection.has(item.id) && <Check size={13} weight="bold" />}
              </span>
              {item.media_id ? (
                <img src={`/api/v1/media/${item.media_id}/thumbnail?size=256`} alt="" />
              ) : (
                <span className="trash-row__placeholder">
                  <Trash size={20} />
                </span>
              )}
              <span className="trash-row__name">
                <strong>{item.filename}</strong>
                <small>
                  {item.library_name} / {item.original_relative_path}
                </small>
              </span>
              <span>
                <strong>{formatBytes(item.size)}</strong>
                <small>{item.category_snapshot.join(" · ") || "未分类"}</small>
              </span>
              <span>
                <strong>移入时间</strong>
                <small>{formatDate(item.moved_at)}</small>
              </span>
            </button>
          ))}
        </div>
      )}
      {selection.size > 0 && (
        <div className="selection-bar selection-bar--page">
          <span className="selection-bar__check">
            <Check size={15} />
          </span>
          <strong>已选择 {selection.size} 项</strong>
          <span>{formatBytes(selectedSize)}</span>
          <button onClick={() => setSelection(new Set())}>清空选择</button>
          <div />
          <Button onClick={() => restore.mutate()} disabled={restore.isPending}>
            <ArrowCounterClockwise size={16} />
            恢复
          </Button>
          <Button variant="danger" onClick={() => setDeleteOpen(true)}>
            <Trash size={16} />
            永久删除
          </Button>
        </div>
      )}
      {deleteOpen && (
        <Modal title="确认永久删除" onClose={() => setDeleteOpen(false)}>
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
              />
            </span>
          </label>
          {permanent.error && <div className="form-error">{permanent.error.message}</div>}
          <footer>
            <Button onClick={() => setDeleteOpen(false)}>取消</Button>
            <Button
              variant="danger"
              disabled={!password || permanent.isPending}
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
