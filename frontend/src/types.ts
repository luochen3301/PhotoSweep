export type Classification = {
  category: "screenshot" | "screen_recording" | "app_image";
  app_source?: "WeChat" | "QQ" | "Other";
  confidence: number;
  reasons: string[];
  decision: "review" | "keep";
};

export type Media = {
  id: number;
  library_id: number;
  library_name: string;
  library_writable: boolean;
  relative_path: string;
  filename: string;
  extension: string;
  media_type: "photo" | "video";
  status: string;
  size: number;
  mtime_ns: number;
  captured_at?: string;
  width?: number;
  height?: number;
  duration_ms?: number;
  fps?: number;
  codec?: string;
  camera_make?: string;
  camera_model?: string;
  metadata_error?: string;
  classifications: Classification[];
};

export type Library = {
  id: number;
  name: string;
  root_path: string;
  enabled: boolean;
  writable: boolean;
  last_scan_at?: string;
};

export type DirectoryNode = {
  name: string;
  path: string;
  exists: boolean;
  readable: boolean;
  writable: boolean;
  is_symlink: boolean;
  has_children: boolean;
  selectable: boolean;
  is_root: boolean;
};

export type DirectoryListing = {
  current?: DirectoryNode | null;
  parent_path?: string | null;
  entries: DirectoryNode[];
};

export type OnboardingStatus = {
  has_library: boolean;
  library_count: number;
  dismissed: boolean;
  allowed_roots: DirectoryNode[];
  media_available: boolean;
};

export type Job = {
  id: number;
  kind: "scan" | "duplicate";
  status: "queued" | "running" | "completed" | "failed" | "cancelled" | "interrupted";
  phase: string;
  total?: number;
  processed: number;
  percent?: number;
  result?: Record<string, number>;
  error?: string;
};

export type SummaryItem = { count: number; bytes: number };
export type DashboardSummary = {
  photos: SummaryItem;
  videos: SummaryItem;
  total_bytes: number;
  screenshot: SummaryItem;
  screen_recording: SummaryItem;
  duplicate_photos: SummaryItem;
  duplicate_videos: SummaryItem;
  app_images: SummaryItem;
  potential_savings: number;
};

export type TrashItem = {
  id: number;
  media_id?: number;
  filename: string;
  library_name: string;
  original_relative_path: string;
  size: number;
  category_snapshot: string[];
  moved_at: string;
};

export type DuplicateMember = Media & { is_keep: boolean; is_hardlink: boolean };
export type DuplicateGroup = {
  id: number;
  media_type: "photo" | "video";
  size: number;
  copy_count: number;
  potential_savings: number;
  sha256: string;
  members: DuplicateMember[];
};
