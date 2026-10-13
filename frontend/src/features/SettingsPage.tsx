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
import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { DirectoryPicker } from "../components/DirectoryPicker";
import { Button, ErrorState, Input, Modal } from "../components/ui";
import { JobProgress } from "../components/JobProgress";
import { jobFinished, lastScanJob, rememberScanJob, useJob } from "../lib/useJob";
import { api, formatDate } from "../lib/api";
import type { DirectoryNode, Job, Library } from "../types";

export function SettingsPage() {
  const queryClient = useQueryClient();
  const [addOpen, setAddOpen] = useState(false);
  const [name, setName] = useState("");
  const [directory, setDirectory] = useState<DirectoryNode>();
  const [activeJobId, setActiveJobId] = useState<number | undefined>(lastScanJob);
  const [editLibrary, setEditLibrary] = useState<Library>();
  const [editName, setEditName] = useState("");
  const libraries = useQuery({
    queryKey: ["libraries"],
    queryFn: () => api<Library[]>("/libraries"),
  });
  const activeJob = useJob(activeJobId);
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
    onSuccess: (response) => { setActiveJobId(response.data.id); rememberScanJob(response.data.id); },
  });
  const remove = useMutation({
    mutationFn: (id: number) => api(`/libraries/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["libraries"] });
      queryClient.invalidateQueries({ queryKey: ["summary"] });
      queryClient.invalidateQueries({ queryKey: ["onboarding"] });
      queryClient.invalidateQueries({ queryKey: ["media"] });
      queryClient.invalidateQueries({ queryKey: ["duplicate-runs"] });
    },
  });
  const update = useMutation({
    mutationFn: ({ id, ...changes }: { id: number; name?: string; enabled?: boolean }) => api(`/libraries/${id}`, { method: "PATCH", body: changes }),
    onSuccess: () => { setEditLibrary(undefined); void queryClient.invalidateQueries({ queryKey: ["libraries"] }); },
  });
  const job = activeJob.data?.data;
  const scanning = Boolean(activeJobId && !activeJob.error && !jobFinished(job));

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
      {libraries.error && <ErrorState error={libraries.error} retry={() => libraries.refetch()} />}
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
                {!library.enabled && <small>已停用</small>}
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
                  disabled={!library.enabled || scan.isPending || scanning}
                >
                  <Play size={14} weight="fill" />
                  增量扫描
                </Button>
                <Button
                  variant="ghost"
                  onClick={() => scan.mutate({ id: library.id, mode: "full" })}
                  disabled={!library.enabled || scan.isPending || scanning}
                >
                  Full Scan
                </Button>
                <Button variant="ghost" onClick={() => { setEditName(library.name); setEditLibrary(library); }}>编辑</Button>
                <Button variant="ghost" disabled={update.isPending || scanning} onClick={() => update.mutate({ id: library.id, enabled: !library.enabled })}>{library.enabled ? "停用" : "启用"}</Button>
                <Button
                  variant="ghost"
                  aria-label={`移除 ${library.name}`}
                  disabled={remove.isPending || scanning}
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
          {libraries.isLoading && <div className="grid-loading">正在加载图库…</div>}
          {!libraries.isLoading && !libraries.error && !libraries.data?.data.length && (
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
      <JobProgress jobId={activeJobId} />
      <FileOperationIssues />
      <InstanceSettings />
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
      {(create.error || scan.error || remove.error || update.error) && (
        <div role="alert" className="toast-error">{(create.error ?? scan.error ?? remove.error ?? update.error)?.message}</div>
      )}
      {editLibrary && <Modal title="编辑 Library" onClose={() => setEditLibrary(undefined)}><form className="modal-form" onSubmit={(event) => { event.preventDefault(); update.mutate({ id: editLibrary.id, name: editName.trim() }); }}><label>Library 名称<Input value={editName} onChange={(event) => setEditName(event.target.value)} required maxLength={120} autoFocus /></label><footer><Button type="button" onClick={() => setEditLibrary(undefined)}>取消</Button><Button type="submit" variant="primary" disabled={!editName.trim() || update.isPending}>保存</Button></footer></form></Modal>}
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

type InstanceSettingsData = { thumbnail_cache_gb: number; metadata_workers: number };

type FileOperationIssue = { id: number; operation: string; state: string; media_id?: number; trash_item_id?: number; error?: string; updated_at?: string };

function FileOperationIssues() {
  const issues = useQuery({ queryKey: ["file-operations"], queryFn: () => api<FileOperationIssue[]>("/file-operations?limit=50") });
  if (issues.error) return <ErrorState error={issues.error} retry={() => issues.refetch()} />;
  const items = issues.data?.data ?? [];
  if (!items.length) return null;
  const operationName: Record<string, string> = { move: "移入回收站", restore: "恢复", delete: "永久删除" };
  return <section className="settings-section operation-issues">
    <header><div><h2>有文件操作需要检查</h2><p>共 {issues.data?.meta.total ?? items.length} 项，显示最近 {items.length} 项。系统会尽量保留原文件或副本；状态不明确时，不会自动删除任何一侧。</p></div><Button onClick={() => issues.refetch()}>刷新</Button></header>
    <p className="muted-note">请根据操作编号和错误检查 NAS 上的原目录、回收站及服务日志。确认文件完整前，不要自动重试删除。</p>
    <ul>{items.map((item) => <li key={item.id}><strong>#{item.id} · {operationName[item.operation] ?? item.operation} · {item.state === "failed" ? "失败" : "未完成"}</strong><span>媒体 #{item.media_id ?? "—"} / 回收站 #{item.trash_item_id ?? "—"} · {formatDate(item.updated_at)}</span><p>{item.error ?? "操作尚未完成，请检查文件状态。"}</p></li>)}</ul>
  </section>;
}

function InstanceSettings() {
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api<InstanceSettingsData>("/settings") });
  const [draft, setDraft] = useState<InstanceSettingsData>();
  const save = useMutation({ mutationFn: () => api("/settings", { method: "PATCH", body: draft }), onSuccess: () => { setDraft(undefined); void queryClient.invalidateQueries({ queryKey: ["settings"] }); } });
  const values = draft ?? settings.data?.data;
  return <section className="settings-section"><header><div><h2>缓存与扫描资源</h2><p>缩略图按需生成；缓存达到配额时清理旧缓存，不影响原始文件。</p></div></header>
    {settings.error && <ErrorState error={settings.error} retry={() => settings.refetch()} />}
    {values && <form className="instance-settings" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
      <label>缩略图缓存上限（GB）<Input type="number" min={1} max={100} required value={values.thumbnail_cache_gb} onChange={(event) => { save.reset(); setDraft({ ...values, thumbnail_cache_gb: Number(event.target.value) }); }} /></label>
      <label>元数据扫描并发数<Input type="number" min={1} max={8} required value={values.metadata_workers} onChange={(event) => { save.reset(); setDraft({ ...values, metadata_workers: Number(event.target.value) }); }} /></label>
      <Button type="submit" variant="primary" disabled={!draft || save.isPending}>{save.isPending ? "正在保存…" : "保存设置"}</Button>
      {save.isSuccess && <span role="status">已保存</span>}{save.error && <ErrorState error={save.error} />}
    </form>}
  </section>;
}
