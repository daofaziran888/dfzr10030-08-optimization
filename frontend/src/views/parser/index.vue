<template>
  <section class="page" data-module="parser">
    <header class="page-head">
      <div>
        <h2>格式解析器管理</h2>
        <p class="page-desc">维护解析配置，围绕解析编号、输入格式、输出格式、字段映射做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记解析配置</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出格式解析器清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无格式解析器数据，可先登记解析配置</td>
        </tr>
      </tbody>
    </table>

    <!-- 解析指纹复用：同一文件只解析一次 -->
    <section class="pipeline-section" data-testid="parser-pipeline">
      <header class="section-head">
        <div>
          <h3>文件解析与指纹复用</h3>
          <p class="page-desc">
            同一文件重复导入时按 SHA-256 文件指纹复用已完成字段解析；复用结果绑定解析配置版本，
            映射升级时可导出差异核对。
          </p>
        </div>
        <div class="page-actions">
          <button class="btn" data-testid="export-fingerprints" type="button" @click="downloadFingerprints">
            导出解析指纹结果
          </button>
          <button class="btn" data-testid="export-diffs" type="button" @click="downloadDiffs">
            导出映射差异文件
          </button>
          <button class="btn" data-testid="download-package" type="button" @click="downloadPackage">
            打包下载
          </button>
        </div>
      </header>

      <div class="stat-row">
        <article v-for="item in pipelineStats" :key="item.label" class="stat-card" data-testid="fp-stat">
          <span class="stat-label">{{ item.label }}</span>
          <strong class="stat-value">{{ item.value }}</strong>
        </article>
      </div>

      <form class="pipeline-form" @submit.prevent="submitUpload">
        <label>
          <span>文件名</span>
          <input v-model="uploadForm.file_name" data-testid="upload-file-name" placeholder="orders.json" />
        </label>
        <label>
          <span>选择本地文件</span>
          <input data-testid="upload-file" type="file" @change="readLocalFile" />
        </label>
        <label>
          <span>复用策略</span>
          <select v-model="uploadForm.strategy" data-testid="upload-strategy">
            <option value="fingerprint_first">文件指纹优先（复用旧结果）</option>
            <option value="mapping_first">映射版本优先（按新映射重解析）</option>
          </select>
        </label>
        <label>
          <span>解析配置版本</span>
          <select v-model="uploadForm.config_version" data-testid="upload-version">
            <option value="">当前生效版本</option>
            <option v-for="version in configVersions" :key="version.version" :value="version.version">
              {{ version.version }}（{{ version.status }}）
            </option>
          </select>
        </label>
        <label class="pipeline-content">
          <span>文件内容</span>
          <textarea
            v-model="uploadForm.content"
            data-testid="upload-content"
            rows="4"
            placeholder="可直接粘贴 JSON / CSV 文本，或选择本地文件自动读取"
          />
        </label>
        <div class="page-actions">
          <button class="btn primary" data-testid="upload-submit" type="submit" :disabled="uploading">
            {{ uploading ? '解析中…' : '导入并解析' }}
          </button>
          <button class="btn" data-testid="upload-batch" type="button" :disabled="uploading" @click="submitBatch">
            批量导入示例批次
          </button>
          <button class="btn ghost" data-testid="upload-demo" type="button" @click="fillDemo">填充示例文件</button>
        </div>
      </form>

      <p v-if="uploadMessage" class="pipeline-message" data-testid="upload-message">{{ uploadMessage }}</p>

      <div v-if="lastResult" class="result-card" data-testid="upload-result">
        <div class="result-grid">
          <div><span>指纹编号</span><strong>{{ lastResult['指纹编号'] }}</strong></div>
          <div><span>文件指纹</span><strong class="mono">{{ shortHash(lastResult['文件指纹']) }}</strong></div>
          <div><span>是否复用</span><strong>{{ lastResult['是否复用'] ? '是' : '否' }}</strong></div>
          <div><span>命中类型</span><strong>{{ lastResult['命中类型'] }}</strong></div>
          <div><span>结果版本</span><strong>{{ lastResult['结果版本'] }}</strong></div>
          <div><span>解析配置版本</span><strong>{{ lastResult['解析配置版本'] }}</strong></div>
          <div><span>解析耗时</span><strong>{{ lastResult['解析耗时毫秒'] }} ms</strong></div>
          <div><span>处理回执</span><strong>{{ lastResult['处理回执号'] }}</strong></div>
        </div>
        <h4>映射差异</h4>
        <table class="data-table" data-testid="upload-diff">
          <thead><tr><th>字段</th><th>旧映射</th><th>新映射</th><th>差异类型</th></tr></thead>
          <tbody>
            <tr v-for="detail in lastDiffDetails" :key="detail['字段']">
              <td>{{ detail['字段'] }}</td>
              <td>{{ detail['旧映射'] ?? '—' }}</td>
              <td>{{ detail['新映射'] ?? '—' }}</td>
              <td>{{ detail['差异类型'] }}</td>
            </tr>
            <tr v-if="!lastDiffDetails.length">
              <td colspan="4" class="empty-state">映射无变化，可直接复用字段结果</td>
            </tr>
          </tbody>
        </table>
        <h4>字段解析结果</h4>
        <pre class="result-json" data-testid="upload-fields">{{ JSON.stringify(lastResult['字段结果'], null, 2) }}</pre>
      </div>

      <h4 class="subsection-title">解析指纹结果（同一文件只保留可追溯的解析版本）</h4>
      <table class="data-table">
        <thead>
          <tr><th>指纹编号</th><th>文件名</th><th>结果来源</th><th>结果版本</th><th>配置版本</th><th>校验状态</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in fingerprints" :key="String(item.id)" :data-testid="`fp-row-${item.id}`">
            <td>{{ item['指纹编号'] }}</td>
            <td>{{ item['文件名'] }}</td>
            <td>{{ item['结果来源'] }}{{ item['是否复用'] ? `（复用 ${item['复用来源编号']}）` : '' }}</td>
            <td>{{ item['结果版本'] }}</td>
            <td>{{ item['解析配置版本'] }}</td>
            <td>{{ item['校验状态'] }}</td>
            <td><button class="link" type="button" :data-testid="`fp-detail-${item.id}`" @click="showFingerprint(item)">查看详情</button></td>
          </tr>
          <tr v-if="!fingerprints.length">
            <td colspan="7" class="empty-state">暂无解析指纹结果，导入文件后生成</td>
          </tr>
        </tbody>
      </table>

      <h4 class="subsection-title">映射差异文件</h4>
      <table class="data-table">
        <thead>
          <tr><th>差异编号</th><th>文件名</th><th>差异类型</th><th>变更字段数</th><th>处理决策</th><th>处理回执</th><th>可导出</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in diffs" :key="String(item.id)" :data-testid="`diff-row-${item.id}`">
            <td>{{ item['差异编号'] }}</td>
            <td>{{ item['文件名'] }}</td>
            <td>{{ item['差异类型'] }}</td>
            <td>{{ item['变更字段数'] }}</td>
            <td>{{ item['处理决策'] }}</td>
            <td>{{ item['处理回执号'] }}</td>
            <td>{{ item['可导出'] ? '是' : '否' }}</td>
          </tr>
          <tr v-if="!diffs.length">
            <td colspan="7" class="empty-state">暂无映射差异文件</td>
          </tr>
        </tbody>
      </table>

      <h4 class="subsection-title">处理回执（导出、下载、打包均可回查）</h4>
      <table class="data-table">
        <thead><tr><th>回执编号</th><th>业务类型</th><th>文件名</th><th>说明</th><th>创建时间</th></tr></thead>
        <tbody>
          <tr v-for="item in receipts" :key="String(item.id)" :data-testid="`receipt-row-${item.id}`">
            <td>{{ item['回执编号'] }}</td>
            <td>{{ item['业务类型'] }}</td>
            <td>{{ item['文件名'] || '—' }}</td>
            <td>{{ item['说明'] }}</td>
            <td>{{ item['创建时间'] }}</td>
          </tr>
          <tr v-if="!receipts.length">
            <td colspan="5" class="empty-state">暂无处理回执</td>
          </tr>
        </tbody>
      </table>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记解析配置</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="detail" class="modal-backdrop" data-testid="fingerprint-modal">
      <div class="modal">
        <h3>解析指纹结果 {{ detail['指纹编号'] }}</h3>
        <dl class="detail-grid">
          <div><dt>文件名</dt><dd>{{ detail['文件名'] }}</dd></div>
          <div><dt>文件指纹</dt><dd class="mono">{{ detail['文件指纹'] }}</dd></div>
          <div><dt>结果来源</dt><dd>{{ detail['结果来源'] }}</dd></div>
          <div><dt>结果根版本</dt><dd>{{ detail['结果根版本'] }}</dd></div>
          <div><dt>映射差异编号</dt><dd>{{ detail['映射差异编号'] }}</dd></div>
          <div><dt>处理回执号</dt><dd>{{ detail['处理回执号'] }}</dd></div>
        </dl>
        <pre class="result-json">{{ JSON.stringify(detail['字段结果'], null, 2) }}</pre>
        <div class="page-actions">
          <button class="btn" type="button" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条格式解析器记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type JsonRow = Record<string, any>

const ENDPOINT = '/api/parser'
const columns = ["解析编号", "输入格式", "输出格式", "字段映射", "类型转换", "空值处理", "异常策略", "解析状态"]
const actions = ["提交配置", "启用解析", "废弃解析"]
const statuses = ["草稿", "已配置", "生效中", "已废弃"]
const metricNames = ["解析器总数", "生效中解析器", "已废弃解析器"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

const DEMO_CONTENT = JSON.stringify([
  { order_id: 'SO-20261118-001', amount: 128.5, customer: { name: '张三' }, status: '已支付', channel: 'web' },
  { order_id: 'SO-20261118-002', amount: 66, customer: { name: '李四' }, status: '已发货', channel: 'app' },
], null, 2)

const pipelineStats = ref<{ label: string; value: number }[]>([])
const configVersions = ref<JsonRow[]>([])
const uploadForm = ref({ file_name: 'orders.json', content: '', strategy: 'fingerprint_first', config_version: '' })
const uploading = ref(false)
const uploadMessage = ref('')
const lastResult = ref<JsonRow | null>(null)
const fingerprints = ref<JsonRow[]>([])
const diffs = ref<JsonRow[]>([])
const receipts = ref<JsonRow[]>([])
const detail = ref<JsonRow | null>(null)

const lastDiffDetails = computed<JsonRow[]>(() => lastResult.value?.['映射差异']?.['明细'] ?? [])

function shortHash(value: unknown) {
  const text = String(value ?? '')
  return text.length > 16 ? `${text.slice(0, 12)}…` : text
}

function fillDemo() {
  uploadForm.value.file_name = uploadForm.value.file_name || 'orders.json'
  uploadForm.value.content = DEMO_CONTENT
}

async function readLocalFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploadForm.value.file_name = file.name
  uploadForm.value.content = await file.text()
}

async function submitUpload() {
  uploading.value = true
  uploadMessage.value = ''
  errorMessage.value = ''
  try {
    const payload: JsonRow = {
      file_name: uploadForm.value.file_name || 'upload.json',
      content: uploadForm.value.content,
      strategy: uploadForm.value.strategy,
    }
    if (uploadForm.value.config_version) payload.config_version = uploadForm.value.config_version
    const response = await request(`${ENDPOINT}/upload`, {
      method: 'POST', body: JSON.stringify(payload),
    })
    const data = await response.json()
    if (!response.ok || !data.ok) throw new Error(data.message || '解析失败')
    uploadMessage.value = data.message
    lastResult.value = data.entry
    await reloadPipeline()
  } catch (error) {
    uploadMessage.value = error instanceof Error ? error.message : '解析失败'
    lastResult.value = null
  } finally {
    uploading.value = false
  }
}

async function submitBatch() {
  uploading.value = true
  uploadMessage.value = ''
  errorMessage.value = ''
  try {
    const stamp = Date.now()
    const response = await request(`${ENDPOINT}/upload-batch`, {
      method: 'POST',
      body: JSON.stringify({
        strategy: uploadForm.value.strategy,
        files: [
          {
            file_name: `batch-${stamp}-a.json`,
            content: JSON.stringify([{ order_id: `B${stamp}-1`, amount: 10, customer: { name: '批次甲' }, status: '已支付', channel: 'web' }]),
          },
          {
            file_name: `batch-${stamp}-b.json`,
            content: JSON.stringify([{ order_id: `B${stamp}-2`, amount: 20, customer: { name: '批次乙' }, status: '已发货', channel: 'app' }]),
          },
          {
            // 与第 1 份内容一致：批次内同样按指纹复用
            file_name: `batch-${stamp}-a-copy.json`,
            content: JSON.stringify([{ order_id: `B${stamp}-1`, amount: 10, customer: { name: '批次甲' }, status: '已支付', channel: 'web' }]),
          },
        ],
      }),
    })
    const data = await response.json()
    if (!response.ok || !data.ok) throw new Error(data.message || '批量导入失败')
    uploadMessage.value = data.message
    const items: JsonRow[] = data.entry?.items ?? []
    lastResult.value = items[items.length - 1] ?? null
    await reloadPipeline()
  } catch (error) {
    uploadMessage.value = error instanceof Error ? error.message : '批量导入失败'
    lastResult.value = null
  } finally {
    uploading.value = false
  }
}

function showFingerprint(item: JsonRow) {
  detail.value = item
}

async function downloadFile(path: string, filename: string) {
  errorMessage.value = ''
  try {
    const response = await request(path)
    if (!response.ok) throw new Error('下载失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    link.click()
    URL.revokeObjectURL(url)
    await reloadPipeline()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '下载失败'
  }
}

function downloadFingerprints() {
  void downloadFile(`${ENDPOINT}/fingerprints/export`, 'parser-fingerprints.csv')
}

function downloadDiffs() {
  void downloadFile(`${ENDPOINT}/diffs/export`, 'parser-mapping-diffs.csv')
}

function downloadPackage() {
  void downloadFile(`${ENDPOINT}/package`, 'parser-parse-package.zip')
}

async function reloadPipeline() {
  try {
    const [statsRes, fpRes, diffRes, receiptRes, versionRes] = await Promise.all([
      request(`${ENDPOINT}/stats`),
      request(`${ENDPOINT}/fingerprints?size=20`),
      request(`${ENDPOINT}/diffs?size=20`),
      request(`${ENDPOINT}/receipts?size=10`),
      request(`${ENDPOINT}/config-versions`),
    ])
    const statsData = await statsRes.json()
    pipelineStats.value = [
      { label: '文件导入次数', value: statsData['文件导入次数'] ?? 0 },
      { label: '解析执行次数', value: statsData['解析执行次数'] ?? 0 },
      { label: '指纹命中次数', value: statsData['指纹命中次数'] ?? 0 },
      { label: '映射冲突数', value: statsData['映射冲突数'] ?? 0 },
    ]
    fingerprints.value = (await fpRes.json()).items ?? []
    diffs.value = (await diffRes.json()).items ?? []
    receipts.value = (await receiptRes.json()).items ?? []
    configVersions.value = (await versionRes.json()).items ?? []
  } catch {
    // 复用面板读取失败不阻塞既有列表
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'parser.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('格式解析器动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '格式解析器操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('解析配置列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '格式解析器列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void reloadPipeline()
})
</script>

<style scoped>
.pipeline-section { margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--border); }
.section-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.section-head h3 { margin: 0; }
.pipeline-form { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; margin-top: 12px; }
.pipeline-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.pipeline-form input, .pipeline-form select, .pipeline-form textarea {
  border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; font-size: 13px; font-family: inherit;
}
.pipeline-content { flex-basis: 100%; }
.pipeline-content textarea { width: 100%; resize: vertical; }
.pipeline-message { font-size: 13px; color: var(--brand); }
.result-card { margin-top: 12px; padding: 12px 16px; border: 1px solid var(--border); border-radius: 8px; background: #fff; }
.result-card h4 { margin: 14px 0 6px; font-size: 13px; }
.result-grid { display: grid; grid-template-columns: repeat(4, minmax(140px, 1fr)); gap: 10px; }
.result-grid span { display: block; color: var(--muted); font-size: 12px; }
.result-grid strong { font-size: 13px; }
.result-json {
  background: #0f172a; color: #e2e8f0; border-radius: 6px; padding: 12px;
  font-size: 12px; max-height: 240px; overflow: auto;
}
.mono { font-family: monospace; font-size: 12px; }
.subsection-title { margin: 20px 0 8px; font-size: 14px; }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.detail-grid dt { color: var(--muted); font-size: 12px; }
.detail-grid dd { margin: 0 0 6px; font-size: 13px; word-break: break-all; }
</style>
