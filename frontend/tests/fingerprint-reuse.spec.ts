import { expect, test } from '@playwright/test'

test('解析指纹复用：上传同一文件命中、映射变更差异可导出、校验回读', async ({ page }) => {
  test.setTimeout(180_000)
  const errors: string[] = []
  page.on('pageerror', e => errors.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status() >= 400) errors.push(`${r.status()} ${r.url()}`) })

  await page.goto('/parser')
  await expect(page.getByTestId('parser-pipeline')).toBeVisible()
  await expect(page.getByTestId('row-1')).toBeVisible()

  // 每轮生成唯一内容，保证是本次会话的首次导入
  const runId = `E2E${Date.now()}`
  const content = JSON.stringify([
    { order_id: `${runId}-1`, amount: 12.5, customer: { name: '张三' }, status: '已支付', channel: 'web' },
    { order_id: `${runId}-2`, amount: 30, customer: { name: '李四' }, status: '已发货', channel: 'app' },
  ], null, 2)

  await page.getByTestId('upload-file-name').fill(`${runId}.json`)
  await page.getByTestId('upload-content').fill(content)
  await page.getByTestId('upload-submit').click()
  await expect(page.getByTestId('upload-result')).toBeVisible({ timeout: 15000 })
  await expect(page.getByTestId('upload-message')).toContainText('首次导入')
  await expect(page.getByTestId('upload-result')).toContainText('首次解析')

  // 同内容再传一次：指纹命中且复用，耗时 0ms
  await page.getByTestId('upload-submit').click()
  await expect(page.getByTestId('upload-message')).toContainText('指纹命中', { timeout: 15000 })
  await expect(page.getByTestId('upload-result')).toContainText('复用')
  await expect(page.getByTestId('upload-result')).toContainText('0 ms')

  // 文件指纹统计命中（以本次会话增量断言，避免依赖服务已有数据）
  const hitStat = page.getByTestId('fp-stat').nth(2)
  await expect(hitStat).toBeVisible()
  await expect(hitStat).not.toContainText('0')

  // 同一批源文件一起上传：批内重复内容同样按指纹复用
  await page.getByTestId('upload-batch').click()
  await expect(page.getByTestId('upload-message')).toContainText('批次处理完成', { timeout: 15000 })
  await expect(page.getByTestId('upload-message')).toContainText('指纹命中')

  // 映射差异导出与打包下载
  for (const [testid, filename] of [
    ['export-fingerprints', 'parser-fingerprints.csv'],
    ['export-diffs', 'parser-mapping-diffs.csv'],
    ['download-package', 'parser-parse-package.zip'],
  ] as const) {
    const downloadPromise = page.waitForEvent('download')
    await page.getByTestId(testid).click()
    const download = await downloadPromise
    expect(download.suggestedFilename()).toBe(filename)
  }

  // 校验页：抽样校验 + 记录回读 + 待办台账
  await page.goto('/validator')
  await expect(page.getByTestId('validator-fingerprints')).toBeVisible()
  await expect(page.getByTestId('row-1')).toBeVisible()
  await page.getByTestId('run-check').click()
  await expect(page.getByTestId('check-message')).toContainText('抽样校验完成', { timeout: 15000 })
  await expect(page.getByTestId('check-summary')).toContainText('复用文件')
  const checkRows = page.locator('tbody tr[data-testid^="check-row-"]')
  expect(await checkRows.count()).toBeGreaterThan(0)
  await checkRows.first().getByRole('button', { name: '回读' }).click()
  await expect(page.getByTestId('check-detail')).toBeVisible()
  const todoRows = page.locator('tbody tr[data-testid^="todo-row-"]')
  expect(await todoRows.count()).toBeGreaterThan(0)

  // 既有 CRUD 区块仍正常
  await page.goto('/parser')
  expect(await page.locator('tbody tr[data-testid^="row-"]').count()).toBeGreaterThanOrEqual(3)

  expect(errors).toEqual([])
})
