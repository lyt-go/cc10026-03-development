import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ========== 账单差额复核 ==========

export type DiffCandidate = {
  audit_id: number
  record_no: string
  response_code: string
  latency_ms: number
  result_rows: number
  timestamp: string
  is_success: boolean
  is_timeout: boolean
}

export type ReconcileDiff = {
  diff_id: number
  diff_no: string
  usage_id: number
  record_no: string
  caller: string
  api_name: string
  billing_cycle: string
  logical_request_key: string
  candidate_audits: DiffCandidate[]
  candidate_count: number
  evidence: string
  confidence: string
  retry_chain: boolean
  relation_note: string
  has_success_evidence: boolean
  has_timeout_evidence: boolean
  estimated_amount: number
  verified_amount: number
  call_count: number
  data_rows: number
  status: string
  confirmed_audit_ids: number[]
  confirmed_record_nos: string[]
  reviewer: string
  remark: string
  supplement_id: number | null
  created_at: string
  updated_at: string
}

export type ReconcileSummary = {
  module: string
  billing_cycle: string
  check_items: {
    usage_id: number
    record_no: string
    caller: string
    api_name: string
    billing_cycle: string
    diff_count: number
    investigating_amount: number
    verified_amount: number
    recorded_amount: number
  }[]
  diff_total: number
  pending_count: number
  investigating_count: number
  verified_count: number
  recorded_count: number
  investigating_amount: number
  verified_amount: number
  recorded_amount: number
  unbilled_amount: number
  supplement_count: number
}

export type ReconcileSupplement = {
  supplement_id: number
  supplement_no: string
  diff_id: number
  diff_no: string
  usage_id: number
  record_no: string
  caller: string
  api_name: string
  billing_cycle: string
  confirmed_audit_ids: number[]
  confirmed_record_nos: string[]
  added_call_count: number
  added_data_rows: number
  added_amount: number
  status: string
  created_at: string
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    const detail = (await response.json().catch(() => ({}))).detail ?? '操作失败'
    throw new Error(detail)
  }
  return await response.json()
}

export async function buildReconcileDiffs(payload: {
  billing_cycle: string
  caller?: string
  api_name?: string
  unit_amount?: number
}): Promise<{ ok: boolean; message: string; items: ReconcileDiff[]; total: number }> {
  return postJson('/api/reconciliation/diffs/build', payload)
}

export async function listReconcileDiffs(
  billingCycle?: string,
  status?: string,
): Promise<{ items: ReconcileDiff[]; total: number }> {
  const params = new URLSearchParams()
  if (billingCycle) params.set('billing_cycle', billingCycle)
  if (status) params.set('status', status)
  const response = await request(`/api/reconciliation/diffs?${params.toString()}`)
  if (!response.ok) throw new Error('差额明细读取失败')
  return await response.json()
}

export async function confirmReconcileDiff(
  diffId: number,
  reviewer = '复核人员',
  remark = '',
): Promise<{ ok: boolean; message: string; entry: ReconcileDiff }> {
  return postJson(`/api/reconciliation/diffs/${diffId}/confirm`, { reviewer, remark })
}

export async function supplementReconcileDiff(
  diffId: number,
): Promise<{ ok: boolean; message: string; entry: ReconcileSupplement }> {
  return postJson(`/api/reconciliation/diffs/${diffId}/supplement`, {})
}

export async function loadReconcileSummary(
  billingCycle?: string,
): Promise<ReconcileSummary> {
  const params = new URLSearchParams()
  if (billingCycle) params.set('billing_cycle', billingCycle)
  const response = await request(`/api/reconciliation/summary?${params.toString()}`)
  if (!response.ok) throw new Error('复核汇总读取失败')
  return await response.json()
}

export async function listReconcileSupplements(
  billingCycle?: string,
): Promise<{ items: ReconcileSupplement[]; total: number }> {
  const params = new URLSearchParams()
  if (billingCycle) params.set('billing_cycle', billingCycle)
  const response = await request(`/api/reconciliation/supplements?${params.toString()}`)
  if (!response.ok) throw new Error('补记结果读取失败')
  return await response.json()
}
