import {
  Check,
  Copy,
  HardDrives,
  Play,
  ShieldCheck,
  Trash,
} from "@phosphor-icons/react";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, EmptyState } from "../components/ui";
import { api, formatBytes } from "../lib/api";
import type { DuplicateGroup, Job, Library } from "../types";

type Run = {
  id: number;
  library_ids: number[];
  media_type: "photo" | "video" | "both";
  job: Job;
};

export function Duplicates({ mediaType }: { mediaType: "photo" | "video" }) {
  const queryClient = useQueryClient();
  const [selectedLibraries, setSelectedLibraries] = useState<Set<number>>(new Set());
  const [selection, setSelection] = useState<Set<number>>(new Set());
  const libraries = useQuery({
    queryKey: ["libraries"],
    queryFn: () => api<Library[]>("/libraries"),
  });
  const runs = useQuery({
    queryKey: ["duplicate-runs"],
    queryFn: () => api<Run[]>("/duplicate-runs"),
    refetchInterval: (query) =>
      query.state.data?.data.some((run) => ["queued", "running"].includes(run.job.status))
        ? 1000
        : false,
  });
  const currentRun = useMemo(
    () => runs.data?.data.find((run) => [mediaType, "both"].includes(run.media_type)),
    [runs.data, mediaType],
  );
  const groups = useQuery({
    queryKey: ["duplicate-groups", currentRun?.id, mediaType],
    queryFn: () =>
      api<DuplicateGroup[]>(
        `/duplicate-groups?run_id=${currentRun!.id}&media_type=${mediaType}&limit=100`,
      ),
    enabled: currentRun?.job.status === "completed",
  });
  const start = useMutation({
    mutationFn: () =>
      api<{ run_id: number; job: Job }>("/duplicate-runs", {
        method: "POST",
        body: {
          library_ids: [...selectedLibraries],
          media_type: mediaType,
        },
      }),
    onSuccess: () => {
      setSelection(new Set());
      queryClient.invalidateQueries({ queryKey: ["duplicate-runs"] });
    },
  });
  const keep = useMutation({
    mutationFn: ({ groupId, mediaId }: { groupId: number; mediaId: number }) =>
      api(`/duplicate-groups/${groupId}/keep`, {
        method: "PUT",
        body: { media_id: mediaId },
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["duplicate-groups"] }),
  });
  const move = useMutation({
    mutationFn: () =>
      api("/trash/actions/move", {
        method: "POST",
        body: { media_ids: [...selection] },
      }),
    onSuccess: () => {
      setSelection(new Set());
      queryClient.invalidateQueries({ queryKey: ["duplicate-groups"] });
      queryClient.invalidateQueries({ queryKey: ["summary"] });
      queryClient.invalidateQueries({ queryKey: ["trash"] });
    },
  });

  function toggleLibrary(id: number) {
    setSelectedLibraries((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function autoSelect() {
    const ids =
      groups.data?.data.flatMap((group) =>
        group.members.filter((member) => !member.is_keep).map((member) => member.id),
      ) ?? [];
    setSelection(new Set(ids));
  }

  const title = mediaType === "photo" ? "完全重复照片" : "完全重复视频";
  const hasReadOnlySelection =
    groups.data?.data.some((group) =>
      group.members.some(
        (member) => selection.has(member.id) && !member.library_writable,
      ),
    ) ?? false;
  const job = currentRun?.job;
  return (
    <section className="page duplicate-page">
      <header className="page-heading">
        <div>
          <p className="eyebrow">SHA-256 验证</p>
          <h1>{title}</h1>
          <p>只有文件内容 100% 相同才会出现在这里。</p>
        </div>
        <Button
          variant="primary"
          onClick={() => start.mutate()}
          disabled={selectedLibraries.size === 0 || start.isPending || job?.status === "running"}
        >
          <Play weight="fill" size={15} />
          开始检测
        </Button>
      </header>
      <section className="duplicate-scope">
        <div>
          <HardDrives size={20} />
          <div>
            <strong>选择本次检测范围</strong>
            <span>哈希会缓存；再次组合图库时无需重读未变化的文件。</span>
          </div>
        </div>
        <div className="library-chips">
          {libraries.data?.data.map((library) => (
            <button
              key={library.id}
              className={selectedLibraries.has(library.id) ? "selected" : ""}
              onClick={() => toggleLibrary(library.id)}
            >
              {selectedLibraries.has(library.id) && <Check size={13} />}
              {library.name}
            </button>
          ))}
        </div>
      </section>
      {job && ["queued", "running"].includes(job.status) && (
        <section className="job-progress">
          <div>
            <strong>{job.phase}</strong>
            <span>
              {job.processed.toLocaleString("zh-CN")}
              {job.total ? ` / ${job.total.toLocaleString("zh-CN")}` : ""}
            </span>
          </div>
          <progress value={job.percent ?? 3} max={100} />
          <small>PhotoSweep 只会为相同大小的候选文件计算哈希。</small>
        </section>
      )}
      {job?.status === "failed" && <div className="inline-error">{job.error}</div>}
      {groups.data?.data.length ? (
        <>
          <div className="duplicate-actions">
            <span>
              {groups.data.meta.total} 个重复组 · 可释放{" "}
              {formatBytes(
                groups.data.data.reduce((sum, group) => sum + group.potential_savings, 0),
              )}
            </span>
            <Button onClick={autoSelect}>
              <Copy size={15} />
              自动选择副本
            </Button>
          </div>
          <div className="duplicate-groups">
            {groups.data.data.map((group) => (
              <article className="duplicate-group" key={group.id}>
                <header>
                  <div>
                    <strong>重复组 #{group.id}</strong>
                    <span>
                      {group.copy_count} 份 · 每份 {formatBytes(group.size)}
                    </span>
                  </div>
                  <small>可释放 {formatBytes(group.potential_savings)}</small>
                </header>
                <div className="duplicate-members">
                  {group.members.map((member) => (
                    <div
                      key={member.id}
                      className={`duplicate-member ${member.is_keep ? "keep" : ""}`}
                    >
                      <button
                        className="duplicate-thumb"
                        onClick={() => {
                          if (!member.is_keep) {
                            setSelection((current) => {
                              const next = new Set(current);
                              if (next.has(member.id)) next.delete(member.id);
                              else next.add(member.id);
                              return next;
                            });
                          }
                        }}
                      >
                        <img
                          src={`/api/v1/media/${member.id}/thumbnail?size=256`}
                          alt=""
                          loading="lazy"
                        />
                        {!member.is_keep && (
                          <span className={`media-check ${selection.has(member.id) ? "checked" : ""}`}>
                            {selection.has(member.id) && <Check size={13} />}
                          </span>
                        )}
                      </button>
                      <div>
                        <strong title={member.filename}>{member.filename}</strong>
                        <span title={member.relative_path}>{member.library_name}</span>
                      </div>
                      {member.is_keep ? (
                        <em>
                          <ShieldCheck size={14} /> 保留
                        </em>
                      ) : (
                        <button
                          className="text-button"
                          onClick={() => keep.mutate({ groupId: group.id, mediaId: member.id })}
                        >
                          改为保留
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              </article>
            ))}
          </div>
        </>
      ) : job?.status === "completed" ? (
        <EmptyState
          icon={<ShieldCheck size={32} />}
          title={`没有${title}`}
          description="本次范围内没有发现通过 SHA-256 验证的完全重复文件。"
        />
      ) : (
        <EmptyState
          icon={<Copy size={32} />}
          title="选择图库后开始检测"
          description="检测不会移动任何文件，完成后仍需你明确选择副本。"
        />
      )}
      {selection.size > 0 && (
        <div className="selection-bar selection-bar--page">
          <span className="selection-bar__check">
            <Check size={15} />
          </span>
          <strong>已选择 {selection.size} 个副本</strong>
          <button onClick={() => setSelection(new Set())}>清空选择</button>
          <div />
          <Button
            variant="primary"
            onClick={() => move.mutate()}
            disabled={move.isPending || hasReadOnlySelection}
            title={hasReadOnlySelection ? "只读图库不能移动文件" : undefined}
          >
            <Trash size={16} />
            移入回收站
          </Button>
        </div>
      )}
      {(start.error || move.error || keep.error) && (
        <div className="toast-error">{(start.error ?? move.error ?? keep.error)?.message}</div>
      )}
    </section>
  );
}
