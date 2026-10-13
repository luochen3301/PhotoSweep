import { test, expect, type Page } from '@playwright/test';

const library = { id: 1, name: '测试图库', root_path: '/media/personal', enabled: true, writable: true };
const media = (id: number, writable = true) => ({
  id, library_id: 1, library_name: library.name, library_writable: writable,
  relative_path: `Screenshot_${id}.png`, filename: `Screenshot_${id}.png`, extension: '.png',
  media_type: 'photo', status: 'active', size: 1024, mtime_ns: 1700000000000000000,
  width: 1080, height: 1920,
  classifications: [{ category: 'screenshot', confidence: 95, reasons: ['文件名符合截图模式'], decision: 'review' }],
});

async function mockApp(page: Page, count: number, writable = true) {
  const submitted: Record<string, unknown>[] = [];
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/\/$/, '').replace('/api/v1', '');
    const send = (data: unknown, meta = {}) => route.fulfill({ json: { data, meta } });
    if (path === '/setup/status') return send({ configured: true });
    if (path === '/auth/me') return send({ username: 'admin' });
    if (path === '/setup/onboarding') return send({ has_library: true, library_count: 1, dismissed: false, allowed_roots: [], media_available: true });
    if (path === '/libraries') return send([{ ...library, writable }]);
    if (path === '/dashboard/summary') return send({ photos: { count, bytes: count * 1024 }, videos: { count: 0, bytes: 0 }, total_bytes: count * 1024, potential_savings: 0 });
    if (path === '/media') {
      const number = Number(url.searchParams.get('page') ?? 1);
      const rows = Array.from({ length: count }, (_, index) => media(index + 1, writable));
      return send(rows.slice((number - 1) * 100, number * 100), { page: number, limit: 100, total: count, pages: Math.ceil(count / 100), total_bytes: count * 1024 });
    }
    if (path === '/media/batch-decision') { submitted.push(route.request().postDataJSON()); return send({ updated: 500 }); }
    if (path.endsWith('/thumbnail')) return route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="#286578"/></svg>' });
    return route.fulfill({ status: 404, json: { detail: `未模拟 ${path}` } });
  });
  return submitted;
}

test('手机端保留与只读限制可达，证据可打开，筛选变更清除旧选择', async ({ page }) => {
  await mockApp(page, 1, false);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/screenshots');
  await page.getByRole('checkbox', { name: '选择 Screenshot_1.png' }).click();
  await expect(page.getByRole('button', { name: '保留', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '移入回收站' })).toBeDisabled();
  await page.getByRole('button', { name: '查看 Screenshot_1.png' }).click();
  await expect(page.locator('.inspector')).toBeVisible();
  await expect(page.getByText('文件名符合截图模式')).toBeVisible();
  await page.getByRole('button', { name: '关闭详情' }).press('Enter');
  await page.getByRole('combobox', { name: '审核状态' }).selectOption('keep');
  await expect(page.locator('.selection-bar')).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBeTruthy();
});

test('全选仅提交已加载的最多500个明确ID，分页可继续浏览', async ({ page }) => {
  const submitted = await mockApp(page, 620);
  await page.goto('/screenshots');
  for (let loaded = 100; loaded < 600; loaded += 100) {
    await expect(page.getByText(new RegExp(`已加载 ${loaded} 项`))).toBeVisible();
    await page.getByRole('button', { name: '再加载 100 项' }).click();
  }
  await expect(page.getByText(/已加载 600 项/)).toBeVisible();
  await page.getByRole('button', { name: '选择已加载项目' }).click();
  await expect(page.getByText('已选择 500 项', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '保留', exact: true }).click();
  await expect.poll(() => submitted.length).toBe(1);
  expect(submitted[0]).toEqual({ media_ids: Array.from({ length: 500 }, (_, index) => index + 1), category: 'screenshot', decision: 'keep' });
});

test('重复组只建议已加载可写副本，改Keep清除旧选择，成员可分页', async ({ page }) => {
  await mockApp(page, 53);
  let keepId = 1;
  const job = { id: 7, kind: 'duplicate', status: 'completed', phase: '完成', total: 53, processed: 53, percent: 100, result: {} };
  const members = () => Array.from({ length: 53 }, (_, index) => ({ ...media(index + 1, index !== 2), is_keep: index + 1 === keepId, is_default_keep: index === 0, is_hardlink: index === 1 }));
  await page.route('**/api/v1/duplicate-**', async route => {
    const url = new URL(route.request().url());
    const send = (data: unknown, meta = {}) => route.fulfill({ json: { data, meta } });
    if (url.pathname.endsWith('/duplicate-runs')) return send([{ id: 1, library_ids: [1], media_type: 'photo', job }]);
    if (url.pathname.endsWith('/keep')) { keepId = route.request().postDataJSON().media_id; return send({ keep_media_id: keepId }); }
    if (url.pathname.endsWith('/members')) {
      const number = Number(url.searchParams.get('page') ?? 1);
      return send(members().slice((number - 1) * 50, number * 50), { page: number, limit: 50, total: 53, pages: 2 });
    }
    return send([{ id: 1, media_type: 'photo', size: 1024, copy_count: 53, potential_savings: 51 * 1024, sha256: 'test-hash', members: members().slice(0, 50), members_total: 53, members_has_more: true }], { page: 1, limit: 20, total: 1, pages: 1 });
  });
  await page.route('**/api/v1/jobs/7', route => route.fulfill({ json: { data: job, meta: {} } }));
  await page.goto('/duplicate-photos');
  await page.getByRole('button', { name: '自动选择已加载副本' }).click();
  await expect(page.getByText('已选择 47 个副本', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '改为保留', exact: true }).first().click();
  await expect(page.locator('.selection-bar')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '已保留 Screenshot_2.png' })).toBeDisabled();
  await page.getByRole('button', { name: '加载更多组内文件' }).click();
  await expect(page.getByRole('button', { name: '选择副本 Screenshot_53.png' })).toBeVisible();
  await page.getByRole('button', { name: '自动选择已加载副本' }).click();
  await expect(page.getByText('已选择 51 个副本', { exact: true })).toBeVisible();
});
