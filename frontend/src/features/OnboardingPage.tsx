import {
  ArrowRight,
  Broom,
  Check,
  CheckCircle,
  ClipboardText,
  FolderOpen,
  HardDrives,
  Info,
  Play,
  ShieldCheck,
  SpinnerGap,
  WarningCircle,
} from "@phosphor-icons/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { DirectoryPicker } from "../components/DirectoryPicker";
import { Button, Input } from "../components/ui";
import { api } from "../lib/api";
import type { DirectoryNode, Job, Library, OnboardingStatus } from "../types";

const steps = ["开始", "选择目录", "创建 Library", "首次扫描"];
const mountExample = "/volume1/homes/<DSM用户名>/Photos:/media/personal:ro";

export function OnboardingPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [step, setStep] = useState(0);
  const [directory, setDirectory] = useState<DirectoryNode>();
  const [name, setName] = useState("个人照片");
  const [library, setLibrary] = useState<Library>();
  const [jobId, setJobId] = useState<number>();
  const [copied, setCopied] = useState(false);
  const onboarding = useQuery({
    queryKey: ["onboarding"],
    queryFn: () => api<OnboardingStatus>("/setup/onboarding"),
  });
  const dismiss = useMutation({
    mutationFn: () =>
      api("/setup/onboarding/dismiss", { method: "POST", body: { dismissed: true } }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["onboarding"] });
      navigate("/");
    },
  });
  const createLibrary = useMutation({
    mutationFn: () =>
      api<Library>("/libraries", {
        method: "POST",
        body: { name: name.trim(), root_path: directory?.path },
      }),
    onSuccess: async (response) => {
      setLibrary(response.data);
      setStep(3);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["libraries"] }),
        queryClient.invalidateQueries({ queryKey: ["onboarding"] }),
      ]);
    },
  });
  const startScan = useMutation({
    mutationFn: () =>
      api<Job>(`/libraries/${library?.id}/scans`, {
        method: "POST",
        body: { mode: "full" },
      }),
    onSuccess: (response) => setJobId(response.data.id),
  });
  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api<Job>(`/jobs/${jobId}`),
    enabled: Boolean(jobId),
    refetchInterval: (query) =>
      query.state.data &&
      ["completed", "failed", "cancelled", "interrupted"].includes(
        query.state.data.data.status,
      )
        ? false
        : 800,
  });
  const status = onboarding.data?.data;
  const scanJob = job.data?.data;
  const finished = scanJob?.status === "completed";
  const terminalFailure =
    scanJob && ["failed", "cancelled", "interrupted"].includes(scanJob.status);
  const errors = useMemo(() => Number(scanJob?.result?.errors ?? 0), [scanJob?.result]);

  async function copyMountExample() {
    await navigator.clipboard.writeText(mountExample);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  }

  function submitLibrary(event: FormEvent) {
    event.preventDefault();
    if (directory && name.trim()) createLibrary.mutate();
  }

  if (onboarding.isLoading) {
    return (
      <div className="onboarding-page onboarding-page--loading">
        <SpinnerGap className="spin" size={24} />
        正在检查安装状态…
      </div>
    );
  }

  if (status?.has_library && !library) {
    return (
      <div className="onboarding-page onboarding-page--complete">
        <section className="onboarding-complete">
          <span><CheckCircle size={34} weight="fill" /></span>
          <p className="eyebrow">安装状态</p>
          <h1>PhotoSweep 已经配置完成</h1>
          <p>当前实例已有 {status.library_count} 个 Library。你仍可在设置中添加其他目录。</p>
          <Button variant="primary" onClick={() => navigate("/")}>
            进入总览 <ArrowRight size={16} />
          </Button>
        </section>
      </div>
    );
  }

  return (
    <div className="onboarding-page">
      <header className="onboarding-topbar">
        <div className="onboarding-brand">
          <span className="brand-mark brand-mark--small"><Broom size={20} weight="fill" /></span>
          <strong>PhotoSweep</strong>
          <span>首次安装</span>
        </div>
        <button className="onboarding-skip" onClick={() => dismiss.mutate()} disabled={dismiss.isPending}>
          稍后配置
        </button>
      </header>

      <main className="onboarding-layout">
        <aside className="onboarding-steps" aria-label="安装进度">
          <p>安装进度</p>
          <ol>
            {steps.map((label, index) => (
              <li
                key={label}
                className={index === step ? "is-active" : index < step ? "is-complete" : ""}
                aria-current={index === step ? "step" : undefined}
              >
                <span>{index < step ? <Check size={14} weight="bold" /> : index + 1}</span>
                <div>
                  <strong>{label}</strong>
                  <small>
                    {index === 0 && "了解安全边界"}
                    {index === 1 && "选择已挂载目录"}
                    {index === 2 && "命名并确认权限"}
                    {index === 3 && "明确启动 Full Scan"}
                  </small>
                </div>
              </li>
            ))}
          </ol>
          <div className="onboarding-privacy">
            <ShieldCheck size={18} />
            <p>照片始终留在你的 NAS。PhotoSweep 只读取已映射到容器的目录。</p>
          </div>
        </aside>

        <section className="onboarding-card">
          {step === 0 && (
            <div className="onboarding-welcome">
              <span className="onboarding-hero-icon"><HardDrives size={34} /></span>
              <p className="eyebrow">欢迎使用 PhotoSweep</p>
              <h1>先绑定一个照片目录</h1>
              <p className="onboarding-lead">
                PhotoSweep 不会自动搜索群晖，也不能在容器内部创建宿主机挂载。它只浏览你已经映射到
                <code>/media</code> 下的目录。
              </p>
              <div className="onboarding-promises">
                <article>
                  <ShieldCheck size={20} />
                  <div><strong>默认安全</strong><span>目录边界、符号链接与权限都会重新验证。</span></div>
                </article>
                <article>
                  <FolderOpen size={20} />
                  <div><strong>只读也能扫描</strong><span>只读 Library 不会启用移动或清理操作。</span></div>
                </article>
                <article>
                  <Play size={20} />
                  <div><strong>扫描由你启动</strong><span>创建 Library 后仍需再次确认，绝不自动扫描。</span></div>
                </article>
              </div>
              <footer className="onboarding-actions">
                <span>约 2 分钟完成</span>
                <Button variant="primary" onClick={() => setStep(1)}>
                  开始设置 <ArrowRight size={16} />
                </Button>
              </footer>
            </div>
          )}

          {step === 1 && (
            <div>
              <header className="onboarding-section-heading">
                <p className="eyebrow">步骤 2 / 4</p>
                <h1>选择已挂载的目录</h1>
                <p>这里只会显示允许媒体根目录内的文件夹，不会暴露群晖的其他路径。</p>
              </header>
              <DirectoryPicker value={directory} onChange={setDirectory} />
              {!status?.media_available && (
                <MountHelp copied={copied} onCopy={copyMountExample} />
              )}
              {status?.media_available && (
                <details className="onboarding-details">
                  <summary>这里没有想要的目录？查看群晖映射说明</summary>
                  <MountHelp copied={copied} onCopy={copyMountExample} compact />
                </details>
              )}
              <footer className="onboarding-actions">
                <Button onClick={() => setStep(0)}>返回</Button>
                <Button variant="primary" disabled={!directory} onClick={() => setStep(2)}>
                  继续 <ArrowRight size={16} />
                </Button>
              </footer>
            </div>
          )}

          {step === 2 && directory && (
            <form onSubmit={submitLibrary}>
              <header className="onboarding-section-heading">
                <p className="eyebrow">步骤 3 / 4</p>
                <h1>确认 Library</h1>
                <p>Library 只是 PhotoSweep 中的索引名称，不会重命名原始目录。</p>
              </header>
              <label className="onboarding-field">
                Library 名称
                <Input
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="例如：个人照片"
                  autoFocus
                  required
                />
              </label>
              <div className="onboarding-review">
                <span><FolderOpen size={22} /></span>
                <div>
                  <small>容器内真实路径</small>
                  <code>{directory.path}</code>
                </div>
                <b className={directory.writable ? "is-writable" : "is-readonly"}>
                  {directory.writable ? "可读写" : "只读 · 仅扫描"}
                </b>
              </div>
              {!directory.writable && (
                <div className="onboarding-readonly-note">
                  <Info size={18} />
                  <p><strong>这个目录为只读。</strong>扫描与审核可正常使用；移入回收站、恢复和永久删除会保持禁用。</p>
                </div>
              )}
              {createLibrary.error && <div className="form-error">{createLibrary.error.message}</div>}
              <footer className="onboarding-actions">
                <Button type="button" onClick={() => setStep(1)}>返回选择</Button>
                <Button
                  type="submit"
                  variant="primary"
                  disabled={!name.trim() || createLibrary.isPending}
                >
                  {createLibrary.isPending ? "正在验证并创建…" : "创建 Library"}
                </Button>
              </footer>
            </form>
          )}

          {step === 3 && library && (
            <div>
              <header className="onboarding-section-heading">
                <p className="eyebrow">步骤 4 / 4</p>
                <h1>{finished ? "首次扫描完成" : "准备首次扫描"}</h1>
                <p>
                  {finished
                    ? "Library 已建立索引，现在可以进入总览开始审核。"
                    : "Full Scan 会读取文件信息并建立索引，不会移动、修改或删除照片。"}
                </p>
              </header>
              <div className="onboarding-review onboarding-review--scan">
                <span><FolderOpen size={22} /></span>
                <div><strong>{library.name}</strong><code>{library.root_path}</code></div>
                <b className={library.writable ? "is-writable" : "is-readonly"}>
                  {library.writable ? "可读写" : "只读"}
                </b>
              </div>
              {!jobId && (
                <div className="onboarding-scan-consent">
                  <ShieldCheck size={20} />
                  <p>只有点击下方按钮后才会开始扫描。耗时取决于目录大小和 NAS 磁盘性能。</p>
                </div>
              )}
              {jobId && (
                <div className="onboarding-job" aria-live="polite">
                  <div>
                    <span>{scanJob?.phase ?? "正在准备扫描…"}</span>
                    <strong>{Math.round(scanJob?.percent ?? 0)}%</strong>
                  </div>
                  <progress value={scanJob?.percent ?? 1} max={100} />
                  <p>
                    已处理 {scanJob?.processed.toLocaleString("zh-CN") ?? 0} 个文件
                    {errors > 0 ? ` · ${errors} 个文件记录了错误` : " · 暂无错误"}
                  </p>
                </div>
              )}
              {terminalFailure && (
                <div className="onboarding-readonly-note onboarding-readonly-note--error">
                  <WarningCircle size={18} />
                  <p><strong>扫描未完成。</strong>{scanJob.error ?? "任务已中断，可以前往设置重新扫描。"}</p>
                </div>
              )}
              {finished && (
                <div className="onboarding-result">
                  <CheckCircle size={22} weight="fill" />
                  <div>
                    <strong>索引已经建立</strong>
                    <span>
                      共处理 {scanJob.processed.toLocaleString("zh-CN")} 个文件
                      {errors ? `，其中 ${errors} 个文件有读取错误。` : "，没有发现读取错误。"}
                    </span>
                  </div>
                </div>
              )}
              {(startScan.error || job.error) && (
                <div className="form-error">{(startScan.error ?? job.error)?.message}</div>
              )}
              <footer className="onboarding-actions">
                {!jobId && <span>不会自动开始</span>}
                {jobId && !finished && !terminalFailure && <span>可保持此页面打开查看进度</span>}
                {!jobId && (
                  <Button variant="primary" onClick={() => startScan.mutate()} disabled={startScan.isPending}>
                    {startScan.isPending ? <SpinnerGap className="spin" size={16} /> : <Play size={15} weight="fill" />}
                    确认并开始 Full Scan
                  </Button>
                )}
                {(finished || terminalFailure) && (
                  <Button
                    variant="primary"
                    onClick={async () => {
                      await Promise.all([
                        queryClient.invalidateQueries({ queryKey: ["summary"] }),
                        queryClient.invalidateQueries({ queryKey: ["libraries"] }),
                      ]);
                      navigate("/");
                    }}
                  >
                    进入总览 <ArrowRight size={16} />
                  </Button>
                )}
              </footer>
            </div>
          )}
        </section>
      </main>
    </div>
  );
}

function MountHelp({
  copied,
  onCopy,
  compact = false,
}: {
  copied: boolean;
  onCopy: () => void;
  compact?: boolean;
}) {
  return (
    <section className={`mount-help ${compact ? "mount-help--compact" : ""}`}>
      <header>
        <HardDrives size={21} />
        <div>
          <strong>先在群晖 Container Manager 添加映射</strong>
          <p>编辑容器的卷设置并重建或重启容器，然后回到这里重新检测。</p>
        </div>
      </header>
      <ol>
        <li>打开「Container Manager」→「项目」→ PhotoSweep。</li>
        <li>编辑卷映射，把群晖照片目录绑定到容器的 <code>/media/...</code>。</li>
        <li>首轮建议使用只读权限，确认扫描结果后再决定是否开放写入。</li>
      </ol>
      <div className="mount-example">
        <div>
          <small>映射示例</small>
          <code>{mountExample}</code>
        </div>
        <Button type="button" onClick={onCopy}>
          {copied ? <Check size={15} /> : <ClipboardText size={15} />}
          {copied ? "已复制" : "复制"}
        </Button>
      </div>
    </section>
  );
}
