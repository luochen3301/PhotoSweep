import assert from "node:assert/strict";
import { test } from "node:test";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const source = await readFile(new URL("../src/lib/selection.ts", import.meta.url), "utf8");
const transpiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { MAX_SELECTION, isSuggestedCopy, nextPage, selectLoaded, toggleSelection } = await import(`data:text/javascript;base64,${Buffer.from(transpiled).toString("base64")}`);

test("select loaded submits only explicit loaded IDs and respects the 500 item API limit", () => {
  const items = Array.from({ length: 720 }, (_, i) => ({ id: i + 10 }));
  const selected = selectLoaded(items);
  assert.equal(selected.size, 500);
  assert.deepEqual([...selected], items.slice(0, 500).map(({ id }) => id));
});

test("automatic proposal excludes Keep, hardlinks and read-only items", () => {
  const items = [
    { id: 1, is_keep: true, is_hardlink: false, library_writable: true, status: "active" },
    { id: 2, is_keep: false, is_hardlink: true, library_writable: true, status: "active" },
    { id: 3, is_keep: false, is_hardlink: false, library_writable: false, status: "active" },
    { id: 4, is_keep: false, is_hardlink: false, library_writable: true, status: "active" },
    { id: 5, is_keep: false, is_hardlink: false, library_writable: true, status: "trashed" },
  ];
  assert.deepEqual([...selectLoaded(items, isSuggestedCopy)], [4]);
});

test("selection limit never prevents deselecting and does not mutate old selection", () => {
  const current = new Set(Array.from({ length: MAX_SELECTION }, (_, i) => i));
  assert.equal(toggleSelection(current, 1000).size, MAX_SELECTION);
  assert.equal(toggleSelection(current, 0).size, MAX_SELECTION - 1);
  assert.equal(current.size, MAX_SELECTION);
});

test("pagination works with explicit pages and total/limit fallback", () => {
  assert.equal(nextPage({ page: 1, pages: 3, total: 101, limit: 50 }), 2);
  assert.equal(nextPage({ page: 2, total: 101, limit: 50 }), 3);
  assert.equal(nextPage({ page: 3, total: 101, limit: 50 }), undefined);
  assert.equal(nextPage({ page: 1, total: 0, limit: 50 }), undefined);
});
