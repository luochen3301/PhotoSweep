import {
  ArrowLeft,
  Check,
  Folder,
  FolderOpen,
  LockSimple,
  SpinnerGap,
  WarningCircle,
} from "@phosphor-icons/react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Button } from "./ui";
import { api } from "../lib/api";
import type { DirectoryListing, DirectoryNode } from "../types";

type DirectoryPickerProps = {
  value?: DirectoryNode;
  onChange: (directory?: DirectoryNode) => void;
};

export function DirectoryPicker({ value, onChange }: DirectoryPickerProps) {
  const queryClient = useQueryClient();
  const [path, setPath] = useState<string>();
  const directories = useQuery({
    queryKey: ["setup-directories", path ?? "roots"],
    queryFn: () =>
      api<DirectoryListing>(
        path ? `/setup/directories?path=${encodeURIComponent(path)}` : "/setup/directories",
      ),
  });
  const listing = directories.data?.data;

  useEffect(() => {
    if (
      !path &&
      listing?.entries.length === 1 &&
      listing.entries[0].readable &&
      listing.entries[0].has_children
    ) {
      setPath(listing.entries[0].path);
    }
  }, [listing, path]);

  function open(directory: DirectoryNode) {
    if (directory.readable && !directory.is_symlink) setPath(directory.path);
  }

  return (
    <section className="directory-picker" aria-label="容器媒体目录">
      <header className="directory-picker__header">
        <div>
          <span>容器内目录</span>
          <code>{listing?.current?.path ?? "允许的媒体根目录"}</code>
        </div>
        {listing?.parent_path && (
          <Button type="button" variant="ghost" onClick={() => setPath(listing.parent_path ?? undefined)}>
            <ArrowLeft size={15} />
            返回上级
          </Button>
        )}
        <Button type="button" disabled={directories.isFetching} onClick={() => {
          onChange(undefined);
          void queryClient.invalidateQueries({ queryKey: ["setup-directories"] });
          void queryClient.invalidateQueries({ queryKey: ["onboarding"] });
        }}>重新检测</Button>
      </header>

      {directories.isLoading && (
        <div className="directory-picker__loading">
          <SpinnerGap className="spin" size={20} />
          正在检测目录权限…
        </div>
      )}

      {directories.error && (
        <div className="directory-picker__error">
          <WarningCircle size={18} />
          <div>
            <strong>无法读取这个目录</strong>
            <span>{directories.error.message}</span>
          </div>
        </div>
      )}

      {!directories.isLoading && !directories.error && (
        <div className="directory-picker__tree" role="list" aria-label="可用目录">
          {listing?.current?.selectable && (
            <DirectoryRow
              directory={listing.current}
              selected={value?.path === listing.current.path}
              current
              onOpen={open}
              onSelect={onChange}
            />
          )}
          {listing?.entries.map((directory) => (
            <DirectoryRow
              key={directory.path}
              directory={directory}
              selected={value?.path === directory.path}
              onOpen={open}
              onSelect={onChange}
            />
          ))}
          {!listing?.entries.length && !listing?.current && (
            <div className="directory-picker__empty">
              <FolderOpen size={23} />
              <strong>没有发现已挂载目录</strong>
              <span>请先在群晖 Container Manager 中添加文件夹映射。</span>
            </div>
          )}
          {!listing?.entries.length && listing?.current && (
            <div className="directory-picker__empty directory-picker__empty--compact">
              <Folder size={21} />
              <strong>这个目录没有子目录</strong>
              <span>可以直接选择当前目录作为 Library。</span>
            </div>
          )}
        </div>
      )}

      {value && (
        <footer className="directory-picker__selection">
          <Check size={17} weight="bold" />
          <div>
            <span>已选择</span>
            <code>{value.path}</code>
          </div>
          <PermissionBadge directory={value} />
        </footer>
      )}
    </section>
  );
}

function DirectoryRow({
  directory,
  selected,
  current = false,
  onOpen,
  onSelect,
}: {
  directory: DirectoryNode;
  selected: boolean;
  current?: boolean;
  onOpen: (directory: DirectoryNode) => void;
  onSelect: (directory: DirectoryNode) => void;
}) {
  const unavailable = !directory.readable || directory.is_symlink;
  return (
    <div
      className={[
        "directory-row",
        selected ? "is-selected" : "",
        unavailable ? "is-disabled" : "",
      ].join(" ")}
      role="listitem"
      aria-disabled={unavailable}
    >
      <button
        type="button"
        className="directory-row__main"
        disabled={unavailable}
        onClick={() => (directory.has_children ? onOpen(directory) : directory.selectable && onSelect(directory))}
      >
        {unavailable ? <LockSimple size={19} /> : <Folder size={19} weight="fill" />}
        <span>
          <strong>{current ? "使用当前目录" : directory.name}</strong>
          <code>{directory.path}</code>
        </span>
      </button>
      <div className="directory-row__actions">
        <PermissionBadge directory={directory} />
        {directory.has_children && !unavailable && (
          <Button type="button" variant="ghost" onClick={() => onOpen(directory)}>
            浏览
          </Button>
        )}
        <Button
          type="button"
          variant={selected ? "primary" : "default"}
          disabled={!directory.selectable || unavailable}
          aria-pressed={selected}
          aria-label={`${selected ? "已选择" : "选择"} ${directory.name}`}
          onClick={() => onSelect(directory)}
        >
          {selected ? "已选择" : "选择"}
        </Button>
      </div>
    </div>
  );
}

function PermissionBadge({ directory }: { directory: DirectoryNode }) {
  if (directory.is_symlink) {
    return <span className="permission-badge permission-badge--blocked">符号链接</span>;
  }
  if (!directory.readable) {
    return <span className="permission-badge permission-badge--blocked">不可读取</span>;
  }
  if (!directory.writable) {
    return <span className="permission-badge permission-badge--readonly">只读 · 仅扫描</span>;
  }
  return <span className="permission-badge permission-badge--writable">可读写</span>;
}
