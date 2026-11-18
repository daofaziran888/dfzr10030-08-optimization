<template>
  <section class="page" data-module="validator">
    <header class="page-head">
      <div>
        <h2>采集结果校验管理</h2>
        <p class="page-desc">维护校验规则，围绕规则编号、校验字段、校验类型、期望值做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记校验规则</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出采集结果校验清单</button>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无采集结果校验数据，可先登记校验规则</td>
        </tr>
      </tbody>
    </table>

    <!-- 解析指纹抽样校验 -->
    <section class="fp-section" data-testid="validator-fingerprints">
      <header class="section-head">
        <div>
          <h3>解析指纹抽样校验</h3>
          <p class="page-desc">
            读取解析指纹结果与映射差异文件，对复用文件与重解析文件分别抽样校验；
            跨模块待办一并写入待办台账，校验记录可按编号回读。
          </p>
        </div>
      </header>

      <div class="stat-row">
        <article v-for="item in fingerprintStats" :key="item.label" class="stat-card">
          <span class="stat-label">{{ item.label }}</span>
          <strong class="stat-value">{{ item.value }}</strong>
        </article>
      </div>

      <form class="check-form" @submit.prevent="runFingerprintCheck">
        <label>
          <span>采样比例</span>
          <input v-model="checkForm.sampling_ratio" data-testid="check-ratio" type="number" step="0.1" min="0.1" max="1" />
        </label>
        <button class="btn primary" data-testid="run-check" type="submit" :disabled="checking">
          {{ checking ? '校验中…' : '执行抽样校验' }}
        </button>
      </form>
      <p v-if="checkMessage" class="check-message" data-testid="check-message">{{ checkMessage }}</p>

      <div v-if="checkSummary" class="check-summary" data-testid="check-summary">
        <span>复用文件：{{ checkSummary['复用文件']?.['文件数'] ?? 0 }} 个</span>
        <span>重解析文件：{{ checkSummary['重解析文件']?.['文件数'] ?? 0 }} 个</span>
        <span>问题：{{ (checkSummary['复用文件']?.['问题数'] ?? 0) + (checkSummary['重解析文件']?.['问题数'] ?? 0) }} 处</span>
        <span>待办：{{ checkSummary['待办数'] ?? 0 }} 条</span>
        <span>处理回执：{{ checkSummary['处理回执号'] }}</span>
      </div>

      <h4 class="subsection-title">校验记录（协作业务面可回读）</h4>
      <table class="data-table">
        <thead>
          <tr><th>记录编号</th><th>类型</th><th>文件名</th><th>结果版本</th><th>结果根版本</th><th>抽样条数</th><th>问题数</th><th>校验结果</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in checks" :key="String(item.id)" :data-testid="`check-row-${item.id}`">
            <td>{{ item['记录编号'] }}</td>
            <td>{{ item['类型'] }}</td>
            <td>{{ item['文件名'] }}</td>
            <td>{{ item['结果版本'] }}</td>
            <td>{{ item['结果根版本'] }}</td>
            <td>{{ item['抽样条数'] }}</td>
            <td>{{ item['问题数'] }}</td>
            <td>{{ item['校验结果'] }}</td>
            <td><button class="link" type="button" :data-testid="`check-readback-${item.id}`" @click="readBackCheck(item)">回读</button></td>
          </tr>
          <tr v-if="!checks.length">
            <td colspan="9" class="empty-state">暂无校验记录，执行抽样校验后生成</td>
          </tr>
        </tbody>
      </table>

      <h4 class="subsection-title">跨模块待办台账</h4>
      <table class="data-table">
        <thead>
          <tr><th>待办编号</th><th>来源模块</th><th>事项</th><th>关联指纹</th><th>关联差异</th><th>级别</th><th>状态</th><th>可回读记录</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in todos" :key="String(item.id)" :data-testid="`todo-row-${item.id}`">
            <td>{{ item['待办编号'] }}</td>
            <td>{{ item['来源模块'] }}</td>
            <td>{{ item['事项'] }}</td>
            <td>{{ item['关联指纹编号'] || '—' }}</td>
            <td>{{ item['关联差异编号'] || '—' }}</td>
            <td>{{ item['级别'] }}</td>
            <td>{{ item['状态'] }}</td>
            <td>{{ item['可回读记录编号'] || '—' }}</td>
            <td>
              <button
                class="link"
                type="button"
                :data-testid="`todo-toggle-${item.id}`"
                @click="toggleTodo(item)"
              >{{ item['状态'] === '待处理' ? '关闭' : '重开' }}</button>
            </td>
          </tr>
          <tr v-if="!todos.length">
            <td colspan="9" class="empty-state">暂无待办，执行抽样校验后写入台账</td>
          </tr>
        </tbody>
      </table>

      <div v-if="checkDetail" class="result-card" data-testid="check-detail">
        <h4>回读校验记录 {{ checkDetail['记录编号'] }}</h4>
        <pre class="result-json">{{ JSON.stringify(checkDetail, null, 2) }}</pre>
      </div>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记校验规则</h3>
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

    <footer class="page-foot">
      <span>共 {{ total }} 条采集结果校验记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type JsonRow = Record<string, any>

const ENDPOINT = '/api/validator'
const columns = ["规则编号", "校验字段", "校验类型", "期望值", "容错范围", "采样比例", "阻断级别", "规则状态"]
const actions = ["提交测试", "启用规则", "停用规则"]
const statuses = ["草稿", "待测试", "已启用", "已停用"]
const metricNames = ["规则总数", "已启用规则", "待测试规则"]

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

const checkForm = ref({ sampling_ratio: 0.5 })
const checking = ref(false)
const checkMessage = ref('')
const checkSummary = ref<JsonRow | null>(null)
const checkDetail = ref<JsonRow | null>(null)
const checks = ref<JsonRow[]>([])
const todos = ref<JsonRow[]>([])
const fingerprintStats = ref<{ label: string; value: number }[]>([])

async function runFingerprintCheck() {
  checking.value = true
  checkMessage.value = ''
  checkDetail.value = null
  try {
    const response = await request(`${ENDPOINT}/checks`, {
      method: 'POST',
      body: JSON.stringify({ sampling_ratio: Number(checkForm.value.sampling_ratio) }),
    })
    const data = await response.json()
    if (!response.ok || !data.ok) throw new Error(data.message || '抽样校验失败')
    checkMessage.value = data.message
    checkSummary.value = data.entry?.summary ?? null
    await reloadFingerprints()
  } catch (error) {
    checkMessage.value = error instanceof Error ? error.message : '抽样校验失败'
    checkSummary.value = null
  } finally {
    checking.value = false
  }
}

async function readBackCheck(item: JsonRow) {
  try {
    const response = await request(`${ENDPOINT}/checks/${item.id}`)
    if (!response.ok) throw new Error('校验记录回读失败')
    checkDetail.value = await response.json()
  } catch (error) {
    checkMessage.value = error instanceof Error ? error.message : '校验记录回读失败'
  }
}

async function toggleTodo(item: JsonRow) {
  const action = item['状态'] === '待处理' ? '关闭' : '重开'
  try {
    const response = await request(`${ENDPOINT}/todo-ledger/${item.id}/actions`, {
      method: 'POST', body: JSON.stringify({ action }),
    })
    const data = await response.json()
    if (!response.ok || !data.ok) throw new Error(data.message || '待办更新失败')
    await reloadFingerprints()
  } catch (error) {
    checkMessage.value = error instanceof Error ? error.message : '待办更新失败'
  }
}

async function reloadFingerprints() {
  try {
    const [checkRes, todoRes] = await Promise.all([
      request(`${ENDPOINT}/checks?size=20`),
      request(`${ENDPOINT}/todo-ledger?size=20`),
    ])
    const checkData = await checkRes.json()
    checks.value = checkData.items ?? []
    todos.value = (await todoRes.json()).items ?? []
    fingerprintStats.value = [
      { label: '校验记录数', value: checkData.total ?? checks.value.length },
      { label: '待处理待办', value: todos.value.filter((item) => item['状态'] === '待处理').length },
      { label: '复用文件记录', value: checks.value.filter((item) => item['类型'] === '复用文件').length },
      { label: '重解析文件记录', value: checks.value.filter((item) => item['类型'] === '重解析文件').length },
    ]
  } catch {
    // 指纹面板读取失败不影响既有校验规则列表
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
    link.download = 'validator.csv'
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
      throw new Error('采集结果校验动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集结果校验操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('校验规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集结果校验列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void reloadFingerprints()
})
</script>

<style scoped>
.fp-section { margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--border); }
.section-head h3 { margin: 0; }
.check-form { display: flex; gap: 12px; align-items: flex-end; margin-top: 12px; }
.check-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.check-form input { border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; font-size: 13px; width: 120px; }
.check-message { font-size: 13px; color: var(--brand); }
.check-summary { display: flex; flex-wrap: wrap; gap: 16px; padding: 10px 12px; background: #fff; border: 1px solid var(--border); border-radius: 8px; font-size: 13px; }
.subsection-title { margin: 20px 0 8px; font-size: 14px; }
.result-card { margin-top: 12px; padding: 12px 16px; border: 1px solid var(--border); border-radius: 8px; background: #fff; }
.result-card h4 { margin: 0 0 8px; font-size: 13px; }
.result-json { background: #0f172a; color: #e2e8f0; border-radius: 6px; padding: 12px; font-size: 12px; max-height: 260px; overflow: auto; }
</style>
