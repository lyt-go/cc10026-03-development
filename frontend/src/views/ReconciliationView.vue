<template>
  <section class="page" data-module="reconciliation">
    <header class="page-head">
      <div>
        <h2>账单差额复核</h2>
        <p class="page-desc">
          按 API、调用方、账期核对用量汇总与请求记录，归并失败重试链，区分待查与已核定金额，确认后补记并携带调用关联。
        </p>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">差额总数</span>
        <strong class="stat-value">{{ summary?.diff_total ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待查（关联不全）</span>
        <strong class="stat-value">{{ summary?.investigating_count ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已核定</span>
        <strong class="stat-value">{{ summary?.verified_count ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已补记</span>
        <strong class="stat-value">{{ summary?.recorded_count ?? 0 }}</strong>
      </article>
    </div>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">待查金额（不参与计费）</span>
        <strong class="stat-value">¥{{ formatAmount(summary?.investigating_amount) }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已核定金额</span>
        <strong class="stat-value">¥{{ formatAmount(summary?.verified_amount) }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已补记金额</span>
        <strong class="stat-value">¥{{ formatAmount(summary?.recorded_amount) }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">仍未参与计费金额</span>
        <strong class="stat-value">¥{{ formatAmount(summary?.unbilled_amount) }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="build">
      <label class="filter-item">
        <span>计费周期</span>
        <input v-model="form.billing_cycle" data-testid="filter-billing-cycle" placeholder="如 2026-11" required />
      </label>
      <label class="filter-item">
        <span>调用方</span>
        <input v-model="form.caller" data-testid="filter-caller" placeholder="可选" />
      </label>
      <label class="filter-item">
        <span>API名称</span>
        <input v-model="form.api_name" data-testid="filter-api" placeholder="可选" />
      </label>
      <label class="filter-item">
        <span>单笔核定金额</span>
        <input v-model.number="form.unit_amount" data-testid="filter-unit-amount" type="number" min="0" step="0.01" />
      </label>
      <button class="btn primary" data-testid="build-diffs" type="submit" :disabled="loading">
        {{ loading ? '核对中…' : '生成差额核对' }}
      </button>
      <button class="btn ghost" type="button" @click="reload">刷新</button>
    </form>

    <h3>差额明细</h3>
    <table class="data-table" data-testid="diff-table">
      <thead>
        <tr>
          <th>差额编号</th>
          <th>调用方 / API</th>
          <th>账期</th>
          <th>候选请求（审计记录）</th>
          <th>证据与请求关系</th>
          <th>置信度</th>
          <th>状态</th>
          <th>待查金额</th>
          <th>已核定金额</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="diff in diffs"
          :key="diff.diff_id"
          :data-testid="`diff-row-${diff.diff_id}`"
          :data-status="diff.status"
        >
          <td>{{ diff.diff_no }}</td>
          <td>
            {{ diff.caller }}<br />
            <span class="muted">{{ diff.api_name }}</span>
          </td>
          <td>{{ diff.billing_cycle }}</td>
          <td>
            <div v-for="candidate in diff.candidate_audits" :key="candidate.audit_id" class="candidate-line">
              <span :class="candidate.is_success ? 'tag ok' : candidate.is_timeout ? 'tag warn' : 'tag'">
                {{ candidate.response_code }}
              </span>
              {{ candidate.record_no }}
              <span class="muted">{{ candidate.timestamp }}</span>
            </div>
          </td>
          <td class="relation-cell">
            <p>{{ diff.relation_note }}</p>
            <p class="muted">证据：{{ diff.evidence }}</p>
            <p v-if="diff.retry_chain" class="muted">含失败重试链，按成功笔仅计费一次</p>
          </td>
          <td>
            <span :class="diff.confidence === '明确' ? 'tag ok' : 'tag warn'">{{ diff.confidence }}</span>
          </td>
          <td><span :class="statusClass(diff.status)">{{ diff.status }}</span></td>
          <td>¥{{ formatAmount(diff.estimated_amount) }}</td>
          <td>¥{{ formatAmount(diff.verified_amount) }}</td>
          <td class="row-actions">
            <button
              v-if="diff.status === '待确认'"
              class="link"
              type="button"
              :data-testid="`confirm-${diff.diff_id}`"
              @click="confirm(diff)"
            >
              确认核定
            </button>
            <button
              v-if="diff.status === '已核定'"
              class="link"
              type="button"
              :data-testid="`supplement-${diff.diff_id}`"
              @click="supplement(diff)"
            >
              补记
            </button>
            <span v-if="diff.status === '已补记'" class="muted">已补记 {{ diff.supplement_id }}</span>
            <span v-if="diff.status === '待查'" class="muted">继续追查</span>
          </td>
        </tr>
        <tr v-if="!diffs.length">
          <td colspan="10" class="empty-state">暂无差额，请先按账期生成核对</td>
        </tr>
      </tbody>
    </table>

    <h3>补记结果（携带已确认调用关联）</h3>
    <table class="data-table" data-testid="supplement-table">
      <thead>
        <tr>
          <th>补记编号</th>
          <th>差额编号</th>
          <th>用量记录</th>
          <th>账期</th>
          <th>已确认调用关联</th>
          <th>补记调用次数</th>
          <th>补记金额</th>
          <th>状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in supplements" :key="item.supplement_id" :data-testid="`supp-row-${item.supplement_id}`">
          <td>{{ item.supplement_no }}</td>
          <td>{{ item.diff_no }}</td>
          <td>{{ item.record_no }}</td>
          <td>{{ item.billing_cycle }}</td>
          <td>{{ item.confirmed_record_nos.join('、') || '—' }}</td>
          <td>{{ item.added_call_count }}</td>
          <td>¥{{ formatAmount(item.added_amount) }}</td>
          <td>{{ item.status }}</td>
        </tr>
        <tr v-if="!supplements.length">
          <td colspan="8" class="empty-state">暂无补记结果</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ diffs.length }} 笔差额、{{ supplements.length }} 笔补记</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import {
  buildReconcileDiffs,
  confirmReconcileDiff,
  listReconcileDiffs,
  listReconcileSupplements,
  loadReconcileSummary,
  supplementReconcileDiff,
  type ReconcileDiff,
  type ReconcileSupplement,
  type ReconcileSummary,
} from '@/api/backend-service'

const form = reactive({
  billing_cycle: '',
  caller: '',
  api_name: '',
  unit_amount: 0,
})
const diffs = ref<ReconcileDiff[]>([])
const supplements = ref<ReconcileSupplement[]>([])
const summary = ref<ReconcileSummary | null>(null)
const loading = ref(false)
const errorMessage = ref('')

function formatAmount(value?: number): string {
  return Number(value ?? 0).toFixed(2)
}

function statusClass(status: string): string {
  if (status === '待查') return 'tag warn'
  if (status === '已核定') return 'tag ok'
  if (status === '已补记') return 'tag done'
  return 'tag'
}

async function build() {
  loading.value = true
  errorMessage.value = ''
  try {
    const result = await buildReconcileDiffs({
      billing_cycle: form.billing_cycle,
      caller: form.caller || undefined,
      api_name: form.api_name || undefined,
      unit_amount: form.unit_amount || 0,
    })
    diffs.value = result.items
    await refreshSummaryAndSupplements()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '差额核对失败'
  } finally {
    loading.value = false
  }
}

async function confirm(diff: ReconcileDiff) {
  errorMessage.value = ''
  try {
    await confirmReconcileDiff(diff.diff_id)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '核定失败'
  }
}

async function supplement(diff: ReconcileDiff) {
  errorMessage.value = ''
  try {
    await supplementReconcileDiff(diff.diff_id)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补记失败'
  }
}

async function refreshSummaryAndSupplements() {
  const cycle = form.billing_cycle || undefined
  const [summaryResult, supplementResult] = await Promise.all([
    loadReconcileSummary(cycle),
    listReconcileSupplements(cycle),
  ])
  summary.value = summaryResult
  supplements.value = supplementResult.items
}

async function reload() {
  errorMessage.value = ''
  try {
    const cycle = form.billing_cycle || undefined
    const diffResult = await listReconcileDiffs(cycle)
    diffs.value = diffResult.items
    await refreshSummaryAndSupplements()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复核数据读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.muted {
  color: var(--muted);
  font-size: 12px;
}
.candidate-line {
  white-space: nowrap;
  margin-bottom: 2px;
}
.relation-cell {
  max-width: 280px;
}
.relation-cell p {
  margin: 2px 0;
}
.tag {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 12px;
  background: #e5e7eb;
  color: #374151;
}
.tag.ok {
  background: #dcfae6;
  color: #027a48;
}
.tag.warn {
  background: #fef0c7;
  color: #b54708;
}
.tag.done {
  background: #dbeafe;
  color: #1d4ed8;
}
h3 {
  margin: 18px 0 8px;
}
</style>
