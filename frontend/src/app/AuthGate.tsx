import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Broom, LockKey, ShieldCheck } from "@phosphor-icons/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../lib/api";
import { Button, ErrorState, Input } from "../components/ui";

function AuthForm({
  setup,
  onSuccess,
}: {
  setup: boolean;
  onSuccess: () => void;
}) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const mutation = useMutation({
    mutationFn: () =>
      api(setup ? "/setup/admin" : "/auth/login", {
        method: "POST",
        body: { username, password },
      }),
    onSuccess,
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (setup && password !== confirm) return;
    mutation.mutate();
  }

  const mismatch = setup && confirm.length > 0 && password !== confirm;
  return (
    <main className="auth-page">
      <section className="auth-brand">
        <span className="brand-mark">
          <Broom weight="fill" size={28} />
        </span>
        <div>
          <strong>PhotoSweep</strong>
          <span>安全整理你的 NAS 照片库</span>
        </div>
      </section>
      <section className="auth-panel">
        <div className="auth-panel__icon">
          {setup ? <ShieldCheck size={26} /> : <LockKey size={24} />}
        </div>
        <p className="eyebrow">{setup ? "首次启动" : "欢迎回来"}</p>
        <h1>{setup ? "设置管理员" : "登录 PhotoSweep"}</h1>
        <p className="auth-copy">
          {setup
            ? "这个账号可以移动和永久删除文件。请使用至少 12 位密码。"
            : "使用你的单管理员账号继续。"}
        </p>
        <form onSubmit={submit}>
          <label>
            管理员用户名
            <Input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="username"
              required
              minLength={3}
            />
          </label>
          <label>
            密码
            <Input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete={setup ? "new-password" : "current-password"}
              required
              minLength={setup ? 12 : 1}
            />
          </label>
          {setup && (
            <label>
              再次输入密码
              <Input
                type="password"
                value={confirm}
                onChange={(event) => setConfirm(event.target.value)}
                autoComplete="new-password"
                required
              />
              {mismatch && <span className="field-error">两次输入的密码不一致</span>}
            </label>
          )}
          {mutation.error && <div className="form-error">{mutation.error.message}</div>}
          <Button
            variant="primary"
            type="submit"
            disabled={mutation.isPending || mismatch || !password}
          >
            {mutation.isPending ? "正在验证…" : setup ? "创建管理员并继续" : "安全登录"}
          </Button>
        </form>
        <p className="auth-footnote">
          PhotoSweep 不上传媒体，不使用 Synology 私有 API，也不会自动删除任何文件。
        </p>
      </section>
    </main>
  );
}

export function AuthGate({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  useEffect(() => {
    const expire = () => { void queryClient.invalidateQueries({ queryKey: ["me"] }); };
    window.addEventListener("photosweep:session-expired", expire);
    return () => window.removeEventListener("photosweep:session-expired", expire);
  }, [queryClient]);
  const setup = useQuery({
    queryKey: ["setup"],
    queryFn: () => api<{ configured: boolean }>("/setup/status"),
  });
  const configured = setup.data?.data.configured;
  const me = useQuery({
    queryKey: ["me"],
    queryFn: () => api<{ username: string }>("/auth/me"),
    enabled: configured === true,
    retry: false,
  });

  if (setup.isLoading || (configured && me.isLoading)) {
    return (
      <main className="splash">
        <span className="brand-mark">
          <Broom weight="fill" size={26} />
        </span>
        <span>正在打开 PhotoSweep…</span>
      </main>
    );
  }
  if (!configured) {
    if (setup.error) return <main className="splash"><ErrorState error={setup.error} retry={() => setup.refetch()} /></main>;
    return (
      <AuthForm
        setup
        onSuccess={() => {
          queryClient.invalidateQueries({ queryKey: ["setup"] });
          queryClient.invalidateQueries({ queryKey: ["me"] });
        }}
      />
    );
  }
  if (me.isError) {
    if (!(me.error instanceof ApiError && me.error.status === 401)) return <main className="splash"><ErrorState error={me.error} retry={() => me.refetch()} /></main>;
    return (
      <AuthForm
        setup={false}
        onSuccess={() => queryClient.invalidateQueries({ queryKey: ["me"] })}
      />
    );
  }
  return children;
}
