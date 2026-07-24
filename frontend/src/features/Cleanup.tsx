import {
  ArrowsDownUp,
  Check,
  CheckCircle,
  Funnel,
  ImageBroken,
  Info,
  ShieldCheck,
  Trash,
  X,
} from "@phosphor-icons/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Button, EmptyState } from "../components/ui";
import { api, formatBytes, formatDate } from "../lib/api";
import type { Media } from "../types";

type Category = "screenshot" | "screen_recording" | "app_image";
type Sort = "newest" | "oldest" | "largest" | "smallest";

const pageCopy: Record<
  Category,
  { title: string; description: string; empty: string; mediaType?: "photo" | "video" }
> = {
  screenshot: {
    title: "截图审核",
    description: "结合文件名、路径、尺寸比例与相机信息识别。",
    empty: "没有待审核的截图",
    mediaType: "photo",
  },
  screen_recording: {
    title: "屏幕录制",
    description: "只读取视频元数据，不分析视频内容。",
    empty: "没有待审核的屏幕录制",
    mediaType: "video",
  },
  app_image: {
    title: "App 图片",
    description: "这些图片不是垃圾，只是集中到这里方便你审核。",
    empty: "没有待审核的 App 图片",
    mediaType: "photo",
  },
};

function calculateColumns() {
  if (typeof window === "undefined") return 5;
  const availableWidth =
    window.innerWidth <= 760
      ? window.innerWidth
      : window.innerWidth <= 1050
        ? window.innerWidth - 70 - 286
        : window.innerWidth - 214 - 324;
  return Math.max(2, Math.min(6, Math.floor(availableWidth / 164)));
}

function useColumns() {
  const [columns, setColumns] = useState(calculateColumns);
  useEffect(() => {
    const update = () => setColumns(calculateColumns());
    window.addEventListener("resize", update);
    update();
    return () => window.removeEventListener("resize", update);
  }, []);
  return columns;
}

function MediaTile({
  media,
  active,
  selected,
  onOpen,
  onSelect,
}: {
  media: Media;
  active: boolean;
  selected: boolean;
  onOpen: () => void;
  onSelect: () => void;
}) {
  const [failed, setFailed] = useState(false);
  return (
    <button
      className={`media-tile ${active ? "active" : ""} ${selected ? "selected" : ""}`}
      onClick={onOpen}
      aria-label={`查看 ${media.filename}`}
    >
      {failed ? (
        <span className="media-tile__failed">
          <ImageBroken size={24} />
          <small>无法预览</small>
        </span>
      ) : (
        <img
          src={`/api/v1/media/${media.id}/thumbnail?size=512`}
          alt=""
          loading="lazy"
          onError={() => setFailed(true)}
        />
      )}
      <span
        role="checkbox"
        aria-checked={selected}
        className="media-check"
        onClick={(event) => {
          event.stopPropagation();
          onSelect();
        }}
      >
        {selected && <Check size={14} weight="bold" />}
      </span>
      <span className="media-tile__caption">
        <strong>{media.filename}</strong>
        <small>{formatBytes(media.size)}</small>
      </span>
    </button>
  );
}

function Inspector({ media, category, onClose }: { media?: Media; category: Category; onClose: () => void }) {
  if (!media) {
    return (
      <aside className="inspector inspector--empty">
        <Info size={24} />
        <strong>选择一个项目查看详情</strong>
        <p>这里会显示文件信息和 PhotoSweep 的识别依据。</p>
      </aside>
    );
  }
  const classification = media.classifications.find((item) => item.category === category);
  return (
    <aside className="inspector">
      <header>
        <strong title={media.filename}>{media.filename}</strong>
        <button className="icon-button" onClick={onClose} aria-label="关闭详情">
          <X size={17} />
        </button>
      </header>
      <div className="inspector__preview">
        <img src={`/api/v1/media/${media.id}/thumbnail?size=512`} alt="" />
      </div>
      <dl className="metadata">
        <div>
          <dt>图库</dt>
          <dd>{media.library_name}</dd>
        </div>
        <div>
          <dt>路径</dt>
          <dd title={media.relative_path}>{media.relative_path}</dd>
        </div>
        <div>
          <dt>尺寸</dt>
          <dd>{media.width && media.height ? `${media.width} × ${media.height}` : "未知"}</dd>
        </div>
        <div>
          <dt>文件大小</dt>
          <dd>{formatBytes(media.size)}</dd>
        </div>
        <div>
          <dt>修改时间</dt>
          <dd>{formatDate(media.mtime_ns)}</dd>
        </div>
        {media.duration_ms && (
          <div>
            <dt>时长</dt>
            <dd>{Math.round(media.duration_ms / 1000)} 秒</dd>
          </div>
        )}
      </dl>
      {classification && (
        <section className="evidence">
          <div className="confidence">
            <span>置信度</span>
            <strong>{classification.confidence}%</strong>
          </div>
          <h3>识别依据</h3>
          <ul>
            {classification.reasons.map((reason) => (
              <li key={reason}>
                <CheckCircle size={17} weight="fill" />
                {reason}
              </li>
            ))}
          </ul>
          <p>
            <ShieldCheck size={15} />
            这只是规则判断，PhotoSweep 不会自动移动文件。
          </p>
        </section>
      )}
    </aside>
  );
}

export function Cleanup({ category }: { category: Category }) {
  const copy = pageCopy[category];
  const queryClient = useQueryClient();
  const [sort, setSort] = useState<Sort>("newest");
  const [appSource, setAppSource] = useState<string>("");
  const [selection, setSelection] = useState<Set<number>>(new Set());
  const [activeId, setActiveId] = useState<number>();
  const scrollRef = useRef<HTMLDivElement>(null);
  const columns = useColumns();
  const mediaQuery = useInfiniteQuery({
    queryKey: ["media", category, sort, appSource],
    initialPageParam: 1,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({
        category,
        sort,
        page: String(pageParam),
        limit: "100",
      });
      if (copy.mediaType) params.set("type", copy.mediaType);
      if (appSource) params.set("app_source", appSource);
      return api<Media[]>(`/media?${params}`);
    },
    getNextPageParam: (lastPage) => {
      const page = Number(lastPage.meta.page);
      return page < Number(lastPage.meta.pages) ? page + 1 : undefined;
    },
  });
  const items = useMemo(
    () => mediaQuery.data?.pages.flatMap((page) => page.data) ?? [],
    [mediaQuery.data],
  );
  const total = Number(mediaQuery.data?.pages[0]?.meta.total ?? 0);
  const active = items.find((item) => item.id === activeId);
  const rowCount = Math.ceil(items.length / columns);
  const virtualizer = useVirtualizer({
    count: rowCount,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 218,
    overscan: 2,
  });
  const selectedBytes = items
    .filter((item) => selection.has(item.id))
    .reduce((sum, item) => sum + item.size, 0);
  const hasReadOnlySelection = items.some(
    (item) => selection.has(item.id) && !item.library_writable,
  );

  const decision = useMutation({
    mutationFn: () =>
      api("/media/batch-decision", {
        method: "POST",
        body: { media_ids: [...selection], category, decision: "keep" },
      }),
    onSuccess: () => {
      setSelection(new Set());
      queryClient.invalidateQueries({ queryKey: ["media"] });
      queryClient.invalidateQueries({ queryKey: ["summary"] });
    },
  });
  const move = useMutation({
    mutationFn: () =>
      api("/trash/actions/move", {
        method: "POST",
        body: { media_ids: [...selection] },
      }),
    onSuccess: () => {
      setSelection(new Set());
      setActiveId(undefined);
      queryClient.invalidateQueries({ queryKey: ["media"] });
      queryClient.invalidateQueries({ queryKey: ["summary"] });
      queryClient.invalidateQueries({ queryKey: ["trash"] });
    },
  });

  function toggle(id: number) {
    setSelection((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <section className="review-page">
      <div className="review-main">
        <header className="review-header">
          <div>
            <h1>{copy.title}</h1>
            <p>
              {total.toLocaleString("zh-CN")} 项 · 已加载 {items.length} 项
            </p>
          </div>
          <div className="review-tools">
            <label className="select-control">
              <Funnel size={16} />
              <span className="sr-only">筛选</span>
              {category === "app_image" ? (
                <select value={appSource} onChange={(event) => setAppSource(event.target.value)}>
                  <option value="">全部来源</option>
                  <option value="WeChat">微信</option>
                  <option value="QQ">QQ</option>
                  <option value="Other">其他</option>
                </select>
              ) : (
                <span>待审核</span>
              )}
            </label>
            <label className="select-control">
              <ArrowsDownUp size={16} />
              <select value={sort} onChange={(event) => setSort(event.target.value as Sort)}>
                <option value="newest">最新优先</option>
                <option value="oldest">最早优先</option>
                <option value="largest">最大优先</option>
                <option value="smallest">最小优先</option>
              </select>
            </label>
          </div>
        </header>
        <div className="review-context">
          <span>{copy.description}</span>
          {items.length > 0 && (
            <button
              onClick={() => setSelection(new Set(items.map((item) => item.id)))}
              disabled={selection.size === items.length}
            >
              选择已加载项目
            </button>
          )}
        </div>
        {mediaQuery.isLoading ? (
          <div className="grid-loading">正在读取媒体索引…</div>
        ) : items.length === 0 ? (
          <EmptyState
            icon={<CheckCircle size={32} />}
            title={copy.empty}
            description="扫描后符合规则且尚未标记为“保留”的项目会显示在这里。"
          />
        ) : (
          <div className="media-scroll" ref={scrollRef}>
            <div
              className="virtual-grid"
              style={{ height: virtualizer.getTotalSize() }}
            >
              {virtualizer.getVirtualItems().map((virtualRow) => {
                const rowItems = items.slice(
                  virtualRow.index * columns,
                  virtualRow.index * columns + columns,
                );
                return (
                  <div
                    className="virtual-row"
                    key={virtualRow.key}
                    style={{
                      transform: `translateY(${virtualRow.start}px)`,
                      gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
                    }}
                  >
                    {rowItems.map((media) => (
                      <MediaTile
                        key={media.id}
                        media={media}
                        active={activeId === media.id}
                        selected={selection.has(media.id)}
                        onOpen={() => setActiveId(media.id)}
                        onSelect={() => toggle(media.id)}
                      />
                    ))}
                  </div>
                );
              })}
            </div>
            {mediaQuery.hasNextPage && (
              <Button
                className="load-more"
                onClick={() => mediaQuery.fetchNextPage()}
                disabled={mediaQuery.isFetchingNextPage}
              >
                {mediaQuery.isFetchingNextPage ? "正在加载…" : "再加载 100 项"}
              </Button>
            )}
          </div>
        )}
      </div>
      <Inspector media={active} category={category} onClose={() => setActiveId(undefined)} />
      {selection.size > 0 && (
        <div className="selection-bar">
          <span className="selection-bar__check">
            <Check size={15} weight="bold" />
          </span>
          <strong>已选择 {selection.size} 项</strong>
          <span>{formatBytes(selectedBytes)}</span>
          <button onClick={() => setSelection(new Set())}>清空选择</button>
          <div />
          <Button onClick={() => decision.mutate()} disabled={decision.isPending || move.isPending}>
            <ShieldCheck size={16} />
            保留
          </Button>
          <Button
            variant="primary"
            onClick={() => move.mutate()}
            disabled={decision.isPending || move.isPending || hasReadOnlySelection}
            title={hasReadOnlySelection ? "只读图库不能移动文件" : undefined}
          >
            <Trash size={16} />
            移入回收站
          </Button>
        </div>
      )}
      {(decision.error || move.error) && (
        <div className="toast-error">{(decision.error ?? move.error)?.message}</div>
      )}
    </section>
  );
}
