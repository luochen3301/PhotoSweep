import {
  ArrowRight,
  Camera,
  ChatCircleDots,
  Copy,
  FilmStrip,
  HardDrives,
  Images,
  Play,
} from "@phosphor-icons/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Button, EmptyState } from "../components/ui";
import { api, formatBytes, formatDate } from "../lib/api";
import type { DashboardSummary, Job, Library } from "../types";

const cards = [
  { key: "screenshot", label: "截图", path: "/screenshots", icon: Camera },
  { key: "screen_recording", label: "屏幕录制", path: "/screen-recordings", icon: FilmStrip },
  { key: "duplicate_photos", label: "重复照片", path: "/duplicate-photos", icon: Copy },
  { key: "duplicate_videos", label: "重复视频", path: "/duplicate-videos", icon: Images },
  { key: "app_images", label: "App 图片", path: "/app-images", icon: ChatCircleDots },
] as const;

export function Dashboard() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const libraries = useQuery({
    queryKey: ["libraries"],
    queryFn: () => api<Library[]>("/libraries"),
  });
  const summary = useQuery({
    queryKey: ["summary"],
    queryFn: () => api<DashboardSummary>("/dashboard/summary"),
  });
  const scan = useMutation({
    mutationFn: (libraryId: number) =>
      api<Job>(`/libraries/${libraryId}/scans`, {
        method: "POST",
        body: { mode: "incremental" },
      }),
    onSuccess: () => {
      setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ["summary"] });
        queryClient.invalidateQueries({ queryKey: ["libraries"] });
      }, 1200);
    },
  });
  const library = libraries.data?.data[0];
  if (!libraries.isLoading && !library) {
    return (
      <section className="page page--center">
        <EmptyState
          icon={<HardDrives size={32} />}
          title="添加你的第一个照片图库"
          description="PhotoSweep 只扫描你明确添加的容器内路径，不会访问 NAS 的其他目录。"
          action={
            <Button variant="primary" onClick={() => navigate("/onboarding")}>
              继续安装
              <ArrowRight size={16} />
            </Button>
          }
        />
      </section>
    );
  }
  const data = summary.data?.data;
  return (
    <section className="page dashboard">
      <header className="page-heading">
        <div>
          <p className="eyebrow">图库概览</p>
          <h1>整理之前，先看清楚。</h1>
          <p>
            {library
              ? `${library.name} · 上次扫描 ${formatDate(library.last_scan_at)}`
              : "正在读取图库…"}
          </p>
        </div>
        <Button
          variant="primary"
          disabled={!library || scan.isPending}
          onClick={() => library && scan.mutate(library.id)}
        >
          <Play weight="fill" size={15} />
          {scan.isPending ? "正在创建扫描…" : "开始增量扫描"}
        </Button>
      </header>
      {scan.error && <div className="inline-error">{scan.error.message}</div>}
      <div className="library-totals">
        <div>
          <span>照片</span>
          <strong>{data?.photos.count.toLocaleString("zh-CN") ?? "—"}</strong>
          <small>{formatBytes(data?.photos.bytes ?? 0)}</small>
        </div>
        <div>
          <span>视频</span>
          <strong>{data?.videos.count.toLocaleString("zh-CN") ?? "—"}</strong>
          <small>{formatBytes(data?.videos.bytes ?? 0)}</small>
        </div>
        <div className="library-totals__wide">
          <span>图库总占用</span>
          <strong>{formatBytes(data?.total_bytes ?? 0)}</strong>
          <small>仅统计已扫描的活动文件</small>
        </div>
        <div className="library-totals__accent">
          <span>潜在可释放空间</span>
          <strong>{formatBytes(data?.potential_savings ?? 0)}</strong>
          <small>仅包含 SHA-256 验证的完全重复项</small>
        </div>
      </div>
      <div className="section-title">
        <div>
          <h2>需要你审核</h2>
          <p>PhotoSweep 只提供候选，最终决定始终由你做出。</p>
        </div>
      </div>
      <div className="cleanup-cards">
        {cards.map((card) => {
          const item = data?.[card.key];
          const Icon = card.icon;
          return (
            <button key={card.key} className="cleanup-card" onClick={() => navigate(card.path)}>
              <span className="cleanup-card__icon">
                <Icon size={20} />
              </span>
              <span className="cleanup-card__copy">
                <strong>{card.label}</strong>
                <small>{formatBytes(item?.bytes ?? 0)}</small>
              </span>
              <b>{item?.count.toLocaleString("zh-CN") ?? "—"}</b>
              <ArrowRight size={16} />
            </button>
          );
        })}
      </div>
    </section>
  );
}
