import {
  CheckCircle,
  Database,
  FolderOpen,
  HardDrive,
  Play,
  Plus,
  ShieldCheck,
  Trash,
  WarningCircle,
} from "@phosphor-icons/react";
import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { DirectoryPicker } from "../components/DirectoryPicker";
import { Button, Input, Modal } from "../components/ui";
import { api, formatDate } from "../lib/api";
import type { DirectoryNode, Job, Library } from "../types";

export function SettingsPage() {
  const queryClient = useQueryClient();
  const [addOpen, setAddOpen] = useState(false);
  const [name, setName] = useState("");
  const [directory, setDirectory] = useState<DirectoryNode>();
  const [activeJobId, setActiveJobId] = useState<number>();
  const libraries = useQuery({
    queryKey: ["libraries"],
    queryFn: () => api<Library[]>("/libraries"),
  });
  const activeJob = useQuery({
    queryKey: ["job", activeJobId],
    queryFn: () => api<Job>(`/jobs/${activeJobId}`),
    enabled: Boolean(activeJobId),
    refetchInterval: (query) =>
      query.state.data &&
      ["completed", "failed", "cancelled", "interrupted"].includes(query.state.data.data.status)
        ? false
        : 800,
  });
  const create = useMutation({
    mutationFn: () =>
      api<Library>("/libraries", {
        method: "POST",
        body: { name, root_path: directory?.path },
      }),
    onSuccess: () => {
      setAddOpen(false);
      setName("");
      setDirectory(undefined);
      queryClient.invalidateQueries({ queryKey: ["libraries"] });
      queryClient.invalidateQueries({ queryKey: ["onboarding"] });
    },
  });
  const scan = useMutation({
    mutationFn: ({ id, mode }: { id: number; mode: "full" | "incremental" }) =>
      api<Job>(`/libraries/${id}/scans`, { method: "POST", body: { mode } }),
    onSuccess: (response) => setActiveJobId(response.data.id),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api(`/libraries/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["libraries"] });
      queryClient.invalidateQueries({ queryKey: ["summary"] });
    },
  });
  const job = activeJob.data?.data;
  useEffect(() => {
    if (job && ["completed", "failed", "cancelled"].includes(job.status)) {
      queryClient.invalidateQueries({ queryKey: ["libraries"] });
      queryClient.invalidateQueries({ queryKey: ["summary"] });
    }
  }, [job?.status, queryClient]);

  function submit(event: FormEvent) {
    event.preventDefault();
    if (directory) create.mutate();
  }

  return (
    <section className="page settings-page">
      <header className="page-heading">
        <div>
          <p className="eyebrow">实例设置</p>
          <h1>图库与安全</h1>
          <p>PhotoSweep 只管理容器内路径，无法看到 NAS 主机上的其他目录。</p>
        </div>
        <Button
          variant="primary"
          onClick={() => {
            setDirectory(undefined);
            setAddOpen(true);
          }}
        >
          <Plus size={16} />
          添加 Library
        </Button>
      </header>
      <section className="settings-section">
        <header>
          <div>
            <h2>照片图库</h2>
            <p>Full Scan 会重读元数据；Incremental Scan 会跳过未变化文件。</p>
          </div>
        </header>
        <div className="library-list">
          {libraries.data?.data.map((library) => (
            <article className="library-row" key={library.id}>
              <span className="library-row__icon">
                <FolderOpen size={21} />
              </span>
              <div>
                <strong>{library.name}</strong>
                <code>{library.root_path}</code>
              </div>
              <div className="library-state">
                {library.writable ? (
                  <span className="state-ok">
                    <CheckCircle size={15} weight="fill" /> 可扫描和清理
                  </span>
                ) : (
                  <span className="state-warning">
                    <WarningCircle size={15} weight="fill" /> 只读，仅扫描
                  </span>
                )}
                <small>上次扫描 {formatDate(library.last_scan_at)}</small>
              </div>
              <div className="library-actions">
                <Button
                  onClick={() => scan.mutate({ id: library.id, mode: "incremental" })}
                  disabled={scan.isPending || job?.status === "running"}
                >
                  <Play size={14} weight="fill" />
                  增量扫描
                </Button>
                <Button
                  variant="ghost"
                  onClick={() => scan.mutate({ id: library.id, mode: "full" })}
                  disabled={scan.isPending || job?.status === "running"}
                >
                  Full Scan
                </Button>
                <Button
                  variant="ghost"
                  aria-label={`移除 ${library.name}`}
                  onClick={() => {
                    if (window.confirm("只移除 PhotoSweep 索引，不会删除任何媒体文件。继续吗？")) {
                      remove.mutate(library.id);
                    }
                  }}
                >
                  <Trash size={15} />
                </Button>
              </div>
            </article>
          ))}
          {!libraries.data?.data.length && (
            <div className="settings-empty">
              <HardDrive size={24} />
              <div>
                <strong>还没有 Library</strong>
                <span>先把 NAS 目录挂载到容器 `/media`，再在这里添加容器内路径。</span>
              </div>
            </div>
          )}
        </div>
      </section>
      {job && (
        <section className="settings-section job-card">
          <header>
            <div>
              <h2>{job.phase}</h2>
              <p>
                已处理 {job.processed.toLocaleString("zh-CN")} 个文件
                {job.result?.errors ? ` · ${job.result.errors} 个元数据错误` : ""}
              </p>
            </div>
            <span className={`job-badge job-badge--${job.status}`}>{job.status}</span>
          </header>
          <progress value={job.percent ?? (job.status === "completed" ? 100 : 2)} max={100} />
          {job.error && <div className="inline-error">{job.error}</div>}
        </section>
      )}
      <section className="settings-grid">
        <article>
          <span>
            <ShieldCheck size={21} />
          </span>
          <div>
            <h3>安全删除策略</h3>
            <p>所有候选必须先移入回收站；永久删除需要管理员密码复核。</p>
          </div>
        </article>
        <article>
          <span>
            <Database size={21} />
          </span>
          <div>
            <h3>本地数据库</h3>
            <p>SQLite 只保存索引、判断依据和操作记录，不保存照片内容。</p>
          </div>
        </article>
      </section>
      {(create.error || scan.error || remove.error) && (
        <div className="toast-error">{(create.error ?? scan.error ?? remove.error)?.message}</div>
      )}
      {addOpen && (
        <Modal title="添加照片 Library" onClose={() => setAddOpen(false)}>
          <form onSubmit={submit} className="modal-form">
            <label>
              Library 名称
              <Input
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="Personal Photos"
                autoFocus
                required
              />
            </label>
            <label>容器内路径</label>
            <DirectoryPicker value={directory} onChange={setDirectory} />
            <div className="path-note">
              <ShieldCheck size={18} />
              <p>
                路径必须位于允许的媒体根目录中。PhotoSweep 不接受 NAS 主机路径，也不会跟随目录符号链接。
              </p>
            </div>
            {create.error && <div className="form-error">{create.error.message}</div>}
            <footer>
              <Button type="button" onClick={() => setAddOpen(false)}>
                取消
              </Button>
              <Button
                variant="primary"
                type="submit"
                disabled={!name.trim() || !directory || create.isPending}
              >
                {create.isPending ? "正在验证路径…" : "添加 Library"}
              </Button>
            </footer>
          </form>
        </Modal>
      )}
    </section>
  );
}
