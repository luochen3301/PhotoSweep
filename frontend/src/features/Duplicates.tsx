import { Check, Copy, HardDrives, Play, ShieldCheck, Trash } from "@phosphor-icons/react";
import { useEffect, useMemo, useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { Button, EmptyState, ErrorState } from "../components/ui";
import { Thumbnail } from "../components/Thumbnail";
import { JobProgress } from "../components/JobProgress";
import { api, formatBytes, mediaThumbnailUrl, type ApiEnvelope } from "../lib/api";
import { MAX_SELECTION, isSuggestedCopy, nextPage } from "../lib/selection";
import type { DuplicateGroup, DuplicateMember, Job, Library } from "../types";

type Run = { id: number; library_ids: number[]; media_type: "photo" | "video" | "both"; job: Job };

export function Duplicates({ mediaType }: { mediaType: "photo" | "video" }) {
  return <DuplicateView key={mediaType} mediaType={mediaType} />;
}

function DuplicateView({ mediaType }: { mediaType: "photo" | "video" }) {
  const queryClient = useQueryClient();
  const [selectedLibraries, setSelectedLibraries] = useState<number[]>([]);
  const [selection, setSelection] = useState<Map<number, DuplicateMember>>(new Map());
  const [runId, setRunId] = useState<string>("");
  const libraries = useQuery({ queryKey: ["libraries"], queryFn: () => api<Library[]>("/libraries") });
  const runs = useQuery({
    queryKey: ["duplicate-runs"], queryFn: () => api<Run[]>("/duplicate-runs"),
    refetchInterval: (query) => query.state.data?.data.some((run) => ["queued", "running"].includes(run.job.status)) ? 1500 : false,
  });
  const matchingRuns = runs.data?.data.filter((run) => [mediaType, "both"].includes(run.media_type)) ?? [];
  const currentRun = matchingRuns.find((run) => String(run.id) === runId) ?? matchingRuns[0];
  useEffect(() => setSelection(new Map()), [currentRun?.id]);
  const groups = useInfiniteQuery({
    queryKey: ["duplicate-groups", currentRun?.id, mediaType], initialPageParam: 1,
    queryFn: ({ pageParam }) => api<DuplicateGroup[]>(`/duplicate-groups?run_id=${currentRun!.id}&media_type=${mediaType}&limit=20&page=${pageParam}`),
    getNextPageParam: (page) => nextPage(page.meta), enabled: currentRun?.job.status === "completed",
  });
  const groupItems = useMemo(() => groups.data?.pages.flatMap((page) => page.data) ?? [], [groups.data]);
  const start = useMutation({
    mutationFn: () => api<{ run_id: number; job: Job }>("/duplicate-runs", { method: "POST", body: { library_ids: selectedLibraries, media_type: mediaType } }),
    onSuccess: (response) => { setSelection(new Map()); setRunId(String(response.data.run_id)); void queryClient.invalidateQueries({ queryKey: ["duplicate-runs"] }); },
  });
  const keep = useMutation({
    mutationFn: ({ groupId, mediaId }: { groupId: number; mediaId: number }) => api(`/duplicate-groups/${groupId}/keep`, { method: "PUT", body: { media_id: mediaId } }),
    onSuccess: () => {
      // A changed Keep must never remain in a previous proposed-delete selection.
      setSelection(new Map());
      void queryClient.invalidateQueries({ queryKey: ["duplicate-groups"] });
      void queryClient.invalidateQueries({ queryKey: ["duplicate-members"] });
    },
  });
  const move = useMutation({
    mutationFn: () => api("/trash/actions/move", { method: "POST", body: { media_ids: [...selection.keys()] } }),
    onSuccess: () => {
      setSelection(new Map());
      for (const key of ["duplicate-groups", "duplicate-members", "summary", "trash", "media"]) void queryClient.invalidateQueries({ queryKey: [key] });
    },
    onError: () => {
      setSelection(new Map());
      for (const key of ["duplicate-groups", "duplicate-members", "summary", "trash", "media", "file-operations"]) void queryClient.invalidateQueries({ queryKey: [key] });
    },
  });
  function autoSelect() {
    const selected = new Map<number, DuplicateMember>();
    for (const group of groupItems) {
      const cached = queryClient.getQueryData<InfiniteData<ApiEnvelope<DuplicateMember[]>>>(["duplicate-members", group.id]);
      for (const member of cached?.pages.flatMap((page) => page.data) ?? group.members) {
        if (isSuggestedCopy(member) && selected.size < MAX_SELECTION) selected.set(member.id, member);
      }
    }
    setSelection(selected);
  }
  function toggle(member: DuplicateMember) {
    if (member.is_keep || member.status !== "active") return;
    setSelection((current) => {
      const next = new Map(current);
      if (next.has(member.id)) next.delete(member.id);
      else if (next.size < MAX_SELECTION) next.set(member.id, member);
      return next;
    });
  }
  const title = mediaType === "photo" ? "完全重复照片" : "完全重复视频";
  const job = currentRun?.job;
  const busy = start.isPending || keep.isPending || move.isPending;
  const hasReadOnlySelection = [...selection.values()].some((member) => !member.library_writable);
  const error = libraries.error ?? runs.error ?? start.error ?? keep.error ?? move.error;
  return <section className="page duplicate-page">
    <header className="page-heading"><div><p className="eyebrow">SHA-256 验证</p><h1>{title}</h1><p>只有大小与完整文件 SHA-256 一致才会建立重复组。</p></div>
      <Button variant="primary" onClick={() => start.mutate()} disabled={!selectedLibraries.length || busy || Boolean(job && ["queued", "running"].includes(job.status))}><Play weight="fill" size={15} />开始检测</Button></header>
    {error && <ErrorState error={error} retry={() => { void libraries.refetch(); void runs.refetch(); }} />}
    <section className="duplicate-scope"><div><HardDrives size={20} /><div><strong>选择本次检测范围</strong><span>点击顺序决定默认保留的图库优先级；之后按拍摄时间、修改时间和路径选择。</span></div></div>
      <div className="library-chips">{libraries.data?.data.filter((library) => library.enabled).map((library) => <button key={library.id} aria-pressed={selectedLibraries.includes(library.id)} className={selectedLibraries.includes(library.id) ? "selected" : ""} onClick={() => setSelectedLibraries((current) => current.includes(library.id) ? current.filter((id) => id !== library.id) : [...current, library.id])}>
        {selectedLibraries.includes(library.id) && <Check size={13} />}{selectedLibraries.includes(library.id) ? `${selectedLibraries.indexOf(library.id) + 1}. ` : ""}{library.name}{!library.writable && " · 只读"}</button>)}</div>
      {!libraries.isLoading && !libraries.data?.data.length && <p>请先在设置中添加并扫描 Library。</p>}
    </section>
    {matchingRuns.length > 0 && <label className="select-control">检测记录<select aria-label="检测记录" value={currentRun?.id ?? ""} onChange={(event) => { setRunId(event.target.value); setSelection(new Map()); }}>
      {matchingRuns.map((run) => <option key={run.id} value={run.id}>#{run.id} · {run.library_ids.map((id) => libraries.data?.data.find((library) => library.id === id)?.name ?? `Library ${id}`).join("、")}</option>)}
    </select></label>}
    <JobProgress jobId={job?.id} />
    {groups.error && <ErrorState error={groups.error} retry={() => groups.refetch()} />}
    {job?.status === "completed" && groups.isLoading && <div className="grid-loading">正在加载重复组…</div>}
    {groupItems.length > 0 ? <>
      <div className="duplicate-actions"><span>{groups.data?.pages[0].meta.total} 个重复组 · 已加载 {groupItems.length} 组 / 可释放 {formatBytes(groupItems.reduce((sum, group) => sum + group.potential_savings, 0))}</span>
        <Button disabled={busy} onClick={autoSelect}><Copy size={15} />自动选择已加载副本</Button></div>
      <p className="muted-note">仅建议可写图库中的非 Keep、非硬链接副本，每次最多 {MAX_SELECTION} 项；请审核后再移入回收站。</p>
      <div className="duplicate-groups">{groupItems.map((group) => <GroupCard key={group.id} group={group} selection={selection} busy={busy} onToggle={toggle} onKeep={(mediaId) => keep.mutate({ groupId: group.id, mediaId })} />)}</div>
      {groups.hasNextPage && <Button className="load-more" disabled={groups.isFetchingNextPage} onClick={() => groups.fetchNextPage()}>{groups.isFetchingNextPage ? "正在加载…" : "加载更多重复组"}</Button>}
    </> : job?.status === "completed" && !groups.isLoading && !groups.error ? <EmptyState icon={<ShieldCheck size={32} />} title={`没有${title}`} description="本次检测范围中没有可显示的完全重复组。" /> : !job ? <EmptyState icon={<Copy size={32} />} title="选择图库后开始检测" description="检测不会移动任何文件，完成后仍需明确选择副本。" /> : null}
    {selection.size > 0 && <div className="selection-bar selection-bar--page"><span className="selection-bar__check"><Check size={15} /></span><strong>已选择 {selection.size} 个副本</strong><span>{formatBytes([...selection.values()].reduce((sum, member) => sum + member.size, 0))}</span><button onClick={() => setSelection(new Map())}>清空选择</button><div />
      {hasReadOnlySelection && <small className="selection-note">只读图库不能移动</small>}<Button variant="primary" disabled={busy || hasReadOnlySelection} onClick={() => move.mutate()}><Trash size={16} />移入回收站</Button></div>}
  </section>;
}

function GroupCard({ group, selection, busy, onToggle, onKeep }: { group: DuplicateGroup; selection: Map<number, DuplicateMember>; busy: boolean; onToggle: (member: DuplicateMember) => void; onKeep: (id: number) => void }) {
  const members = useInfiniteQuery({
    queryKey: ["duplicate-members", group.id], initialPageParam: 1,
    queryFn: ({ pageParam }) => api<DuplicateMember[]>(`/duplicate-groups/${group.id}/members?page=${pageParam}&limit=50`),
    initialData: { pages: [{ data: group.members, meta: { page: 1, limit: 50, total: group.members_total ?? group.copy_count } }], pageParams: [1] },
    getNextPageParam: (page) => nextPage(page.meta),
  });
  return <article className="duplicate-group"><header><div><strong>重复组 #{group.id}</strong><span>{group.copy_count} 份 · 每份 {formatBytes(group.size)}</span></div><small>可释放 {formatBytes(group.potential_savings)}</small></header>
    <div className="duplicate-members">{members.data.pages.flatMap((page) => page.data).map((member) => <div key={member.id} className={`duplicate-member ${member.is_keep ? "keep" : ""}`}>
      <button className="duplicate-thumb" aria-label={`${member.is_keep ? "已保留" : "选择副本"} ${member.filename}`} aria-pressed={selection.has(member.id)} disabled={member.is_keep || busy || member.status !== "active"} onClick={() => onToggle(member)}>
        <Thumbnail src={mediaThumbnailUrl(member, 256)} />{!member.is_keep && <span className={`media-check ${selection.has(member.id) ? "checked" : ""}`}>{selection.has(member.id) && <Check size={13} />}</span>}
      </button><div><strong title={member.filename}>{member.filename}</strong><span title={member.relative_path}>{member.library_name} / {member.relative_path}</span><span>{member.is_hardlink ? "硬链接 · 不计释放空间" : ""}{!member.library_writable ? " · 只读" : ""}</span></div>
      {member.is_keep ? <em><ShieldCheck size={14} />{member.is_default_keep ? "默认保留" : "已指定保留"}</em> : <button className="text-button" disabled={busy || member.status !== "active"} onClick={() => onKeep(member.id)}>改为保留</button>}
    </div>)}</div>
    {members.error && <ErrorState error={members.error} retry={() => members.refetch()} />}
    {members.hasNextPage && <Button className="load-more" disabled={members.isFetchingNextPage} onClick={() => members.fetchNextPage()}>{members.isFetchingNextPage ? "正在加载…" : "加载更多组内文件"}</Button>}
  </article>;
}
