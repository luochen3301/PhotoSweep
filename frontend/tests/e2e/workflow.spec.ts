import { test, expect } from '@playwright/test';

const password = 'test-only-photosweep-2026';

test('首次设置 → 目录绑定 → 确认扫描 → Keep → Trash → Restore → 永久删除', async ({ page }) => {
  const browserErrors: string[] = [];
  page.on('pageerror', error => browserErrors.push(error.message));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '设置管理员' })).toBeVisible();
  await page.getByLabel('密码', { exact: true }).fill(password);
  await page.getByLabel('再次输入密码').fill(password);
  await page.getByRole('button', { name: '创建管理员并继续' }).click();
  await expect(page).toHaveURL(/onboarding/);
  await page.getByRole('button', { name: '开始设置' }).click();
  await page.getByRole('button', { name: '选择 personal', exact: true }).click();
  await page.getByRole('button', { name: '继续', exact: true }).click();
  await page.getByLabel('Library 名称').fill('测试照片');
  await page.getByRole('button', { name: '创建 Library', exact: true }).click();
  await expect(page.getByRole('heading', { name: '准备首次扫描' })).toBeVisible();
  // Creation alone must never start a scan.
  expect((await (await page.request.get('/api/v1/dashboard/summary')).json()).data.photos.count).toBe(0);
  await page.getByRole('button', { name: '确认并开始 Full Scan' }).click();
  await expect(page.getByRole('heading', { name: '首次扫描完成' })).toBeVisible({ timeout: 30000 });
  await page.getByRole('button', { name: '进入总览' }).click();
  expect((await (await page.request.get('/api/v1/dashboard/summary')).json()).data.photos.count).toBe(3);
  await page.goto('/screenshots');
  await expect(page.getByRole('heading', { name: '截图审核' })).toBeVisible();
  const first = 'Screenshot_2026_01.png';
  await page.getByRole('checkbox', { name: `选择 ${first}`, exact: true }).click();
  await page.getByRole('button', { name: '保留', exact: true }).click();
  await expect(page.getByRole('checkbox', { name: `选择 ${first}`, exact: true })).toHaveCount(0);
  await page.getByRole('combobox', { name: '审核状态' }).selectOption('keep');
  await expect(page.getByRole('checkbox', { name: `选择 ${first}`, exact: true })).toBeVisible();
  await page.getByRole('checkbox', { name: `选择 ${first}`, exact: true }).click();
  await page.getByRole('button', { name: '恢复待审核' }).click();
  await page.getByRole('combobox', { name: '审核状态' }).selectOption('review');
  await page.getByRole('checkbox', { name: `选择 ${first}`, exact: true }).click();
  await page.getByRole('button', { name: '移入回收站' }).click();
  await expect(page.getByRole('checkbox', { name: `选择 ${first}`, exact: true })).toHaveCount(0);
  await page.goto('/trash');
  await page.getByRole('button', { name: `选择 ${first}`, exact: true }).click();
  await page.getByRole('button', { name: '恢复', exact: true }).click();
  await expect(page.getByText('回收站是空的')).toBeVisible();
  await page.goto('/screenshots');
  await page.getByRole('checkbox', { name: `选择 ${first}`, exact: true }).click();
  await page.getByRole('button', { name: '移入回收站' }).click();
  await expect(page.getByRole('checkbox', { name: `选择 ${first}`, exact: true })).toHaveCount(0);
  await page.goto('/trash');
  await page.getByRole('button', { name: `选择 ${first}`, exact: true }).click();
  await page.getByRole('button', { name: '永久删除', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('重新输入管理员密码').fill(password);
  await dialog.getByRole('button', { name: '永久删除', exact: true }).click();
  await expect(page.getByText('回收站是空的')).toBeVisible();
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 820, height: 1180 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await page.goto('/screenshots');
    await expect(page.getByRole('heading', { name: '截图审核' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBeTruthy();
    await page.screenshot({ path: `test-results/screenshots-${viewport.width}.png`, fullPage: true });
  }
  expect(browserErrors).toEqual([]);
});

test('无挂载说明、稍后配置及继续安装入口', async ({ page }) => {
  await page.route('**/api/v1/libraries', route => route.fulfill({ json: { data: [], meta: {} } }));
  await page.route('**/api/v1/setup/onboarding', route => route.fulfill({ json: { data: { has_library: false, library_count: 0, dismissed: true, allowed_roots: [], media_available: false }, meta: {} } }));
  await page.route('**/api/v1/setup/directories*', route => route.fulfill({ json: { data: { current: null, parent_path: null, entries: [] }, meta: {} } }));
  await page.goto('/onboarding');
  await page.getByLabel('密码', { exact: true }).fill(password);
  await page.getByRole('button', { name: '安全登录' }).click();
  await page.getByRole('button', { name: '开始设置' }).click();
  await expect(page.getByText('没有发现已挂载目录')).toBeVisible();
  await expect(page.getByText('先在群晖 Container Manager 添加映射')).toBeVisible();
  await page.getByRole('button', { name: '稍后配置' }).click();
  await expect(page.getByRole('button', { name: /继续安装/ }).or(page.getByRole('link', { name: /继续安装/ }))).toBeVisible();
});
