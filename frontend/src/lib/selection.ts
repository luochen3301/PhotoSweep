// All actions submit explicit, currently loaded IDs. API batches are limited to 500.
export const MAX_SELECTION = 500;

export function isSuggestedCopy(item: { is_keep: boolean; is_hardlink: boolean; library_writable: boolean; status: string }): boolean {
  return !item.is_keep && !item.is_hardlink && item.library_writable && item.status === "active";
}

export function selectLoaded<T extends { id: number }>(items: T[], allowed: (item: T) => boolean = () => true): Set<number> {
  return new Set(items.filter(allowed).slice(0, MAX_SELECTION).map((item) => item.id));
}

export function toggleSelection(current: Set<number>, id: number): Set<number> {
  const next = new Set(current);
  if (next.has(id)) next.delete(id);
  else if (next.size < MAX_SELECTION) next.add(id);
  return next;
}

export function nextPage(meta: Record<string, number | string | boolean | null>): number | undefined {
  const page = Number(meta.page);
  const pages = Number(meta.pages ?? Math.ceil(Number(meta.total) / Number(meta.limit)));
  return page < pages ? page + 1 : undefined;
}
