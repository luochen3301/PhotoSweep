import { useMutation } from "@tanstack/react-query";
import { api } from "../lib/api";
import { jobFinished, jobStatusLabel, useJob } from "../lib/useJob";
import { Button, ErrorState } from "./ui";

export function JobProgress({ jobId }: { jobId?: number }) {
  const query = useJob(jobId);
  const cancel = useMutation({ mutationFn: () => api(`/jobs/${jobId}/cancel`, { method: "POST" }) });
  if (!jobId) return null;
  const job = query.data?.data;
  if (query.error) return <ErrorState error={query.error} retry={() => query.refetch()} />;
  return <section className="settings-section job-card" aria-live="polite">
    <header><div><h2>{job?.phase || "正在读取任务…"}</h2><p>已处理 {job?.processed.toLocaleString("zh-CN") ?? 0} 项 · 错误 {job?.result?.errors ?? 0}</p></div>
      <span>{job ? jobStatusLabel[job.status] : "连接中"}</span></header>
    <progress value={job?.percent ?? (job?.status === "completed" ? 100 : undefined)} max={100} />
    {job?.error && <div className="inline-error">{job.error}</div>}
    {job && !jobFinished(job) && <Button disabled={cancel.isPending || cancel.isSuccess} onClick={() => cancel.mutate()}>{cancel.isSuccess ? "正在请求取消…" : "取消任务"}</Button>}
    {cancel.error && <ErrorState error={cancel.error} />}
  </section>;
}
