import {
  Broom,
  Camera,
  ChatCircleDots,
  Copy,
  FilmStrip,
  Gear,
  House,
  Images,
  SignOut,
  Trash,
} from "@phosphor-icons/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import type { DashboardSummary, Library } from "../types";
import { useLibraryScope } from "./LibraryScope";
import { useEffect } from "react";

const navigation = [
  { to: "/", label: "总览", icon: House },
  { to: "/screenshots", label: "截图", icon: Camera, key: "screenshot" },
  { to: "/screen-recordings", label: "屏幕录制", icon: FilmStrip, key: "screen_recording" },
  { to: "/duplicate-photos", label: "重复照片", icon: Copy, key: "duplicate_photos" },
  { to: "/duplicate-videos", label: "重复视频", icon: Images, key: "duplicate_videos" },
  { to: "/app-images", label: "App 图片", icon: ChatCircleDots, key: "app_images" },
  { to: "/trash", label: "回收站", icon: Trash },
  { to: "/settings", label: "设置", icon: Gear },
] as const;

export function Shell({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { libraryId, setLibraryId } = useLibraryScope();
  const me = useQuery({
    queryKey: ["me"],
    queryFn: () => api<{ username: string }>("/auth/me"),
  });
  const libraries = useQuery({
    queryKey: ["libraries"],
    queryFn: () => api<Library[]>("/libraries"),
  });
  const summary = useQuery({
    queryKey: ["summary", libraryId],
    queryFn: () => api<DashboardSummary>(`/dashboard/summary${libraryId ? `?library_ids=${libraryId}` : ""}`),
  });
  const logout = useMutation({
    mutationFn: () => api("/auth/logout", { method: "POST" }),
    onSuccess: () => {
      queryClient.clear();
      navigate("/");
      window.location.reload();
    },
  });
  const activeLibrary = libraries.data?.data.find((library) => String(library.id) === libraryId);
  useEffect(() => {
    if (libraryId && libraries.data && !activeLibrary) setLibraryId("");
  }, [libraryId, libraries.data, activeLibrary, setLibraryId]);
  return (
    <div className="app-shell">
      <header className="topbar">
        <button className="topbar__brand" onClick={() => navigate("/")}>
          <span className="brand-mark brand-mark--small">
            <Broom weight="fill" size={20} />
          </span>
          <strong>PhotoSweep</strong>
        </button>
        <span className="topbar__divider" />
        <label className="library-switcher">
          <span>图库</span>
          <select aria-label="当前图库范围" value={libraryId} onChange={(event) => setLibraryId(event.target.value)}>
            <option value="">全部图库</option>
            {libraries.data?.data.map((library) => <option key={library.id} value={library.id}>{library.name}{!library.writable ? "（只读）" : ""}</option>)}
          </select>
        </label>
        <div className="topbar__spacer" />
        <button className="account-button" aria-label="退出登录" disabled={logout.isPending} onClick={() => logout.mutate()}>
          <span className="avatar">{me.data?.data.username?.slice(0, 1).toUpperCase()}</span>
          <span>{me.data?.data.username}</span>
          <SignOut size={16} />
        </button>
      </header>
      <aside className="sidebar" aria-label="主导航">
        <nav>
          {navigation.map((item) => {
            const Icon = item.icon;
            const count =
              "key" in item && item.key
                ? summary.data?.data[item.key as keyof DashboardSummary]
                : undefined;
            return (
              <NavLink
                key={item.to}
                to={item.to}
                aria-label={item.label}
                title={item.label}
                end={item.to === "/"}
                className={({ isActive }) => (isActive ? "nav-item active" : "nav-item")}
              >
                <Icon size={19} weight={location.pathname === item.to ? "fill" : "regular"} />
                <span>{item.label}</span>
                {count && typeof count === "object" && "count" in count && (
                  <em>{count.count.toLocaleString("zh-CN")}</em>
                )}
              </NavLink>
            );
          })}
        </nav>
        <div className="sidebar__status">
          <span className={libraries.data?.data.length ? "status-dot ok" : "status-dot"} />
          <div>
            <strong>{libraries.data?.data.length ? "图库已连接" : "等待添加图库"}</strong>
            <small>{libraries.data?.data.length ? `${libraries.data.data.length} 个图库` : "继续首次安装"}</small>
          </div>
        </div>
      </aside>
      <main className="workspace">{children}</main>
      {logout.error && <div role="alert" className="toast-error">{logout.error.message}</div>}
    </div>
  );
}
