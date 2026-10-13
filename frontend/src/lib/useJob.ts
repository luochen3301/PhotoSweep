import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ApiEnvelope } from "./api";
import type { Job } from "../types";

export const jobFinished = (job?: Job) => Boolean(job && ["completed", "failed", "cancelled", "interrupted"].includes(job.status));
export const jobStatusLabel: Record<Job["status"], string> = {
  queued: "等待执行", running: "正在执行", completed: "已完成", failed: "失败", cancelled: "已取消", interrupted: "已中断",
};

export function lastScanJob(): number | undefined {
  try { return Number(sessionStorage.getItem("photosweep.scan-job")) || undefined; } catch { return undefined; }
}
export function rememberScanJob(id: number) {
  try { sessionStorage.setItem("photosweep.scan-job", String(id)); } catch { /* Session tracking remains in memory. */ }
}

export function useJob(jobId?: number) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api<Job>(`/jobs/${jobId}`),
    enabled: Boolean(jobId),
    refetchInterval: (state) => jobFinished(state.state.data?.data) ? false : 5000,
  });
  const terminal = jobFinished(query.data?.data);
  useEffect(() => {
    if (!jobId || terminal || typeof EventSource === "undefined") return;
    const source = new EventSource(`/api/v1/events/jobs/${jobId}`, { withCredentials: true });
    const update = (event: MessageEvent) => {
      try {
        const data = JSON.parse(event.data) as Job;
        if (data.id !== jobId) return;
        queryClient.setQueryData<ApiEnvelope<Job>>(["job", jobId], { data, meta: {} });
      } catch { /* Polling remains available if an intermediary damages SSE. */ }
    };
    ["snapshot", "queued", "progress", "completed", "failed", "cancelled", "interrupted"].forEach((name) => source.addEventListener(`job.${name}`, update as EventListener));
    return () => source.close();
  }, [jobId, terminal, queryClient]);
  useEffect(() => {
    if (!terminal) return;
    for (const key of ["libraries", "summary", "media", "duplicate-runs", "duplicate-groups", "duplicate-members"]) {
      void queryClient.invalidateQueries({ queryKey: [key] });
    }
  }, [terminal, queryClient]);
  return query;
}
