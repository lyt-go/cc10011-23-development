import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/subscription'
const columns = [
  "订阅编号",
  "订阅内容",
  "接收人",
  "推送渠道",
  "推送频率",
  "数据粒度",
  "静默时段",
  "下次推送时间"
]
const actions = [
  "提交订阅评审",
  "启用订阅",
  "暂停订阅"
]

type Row = Record<string, string | number | null | boolean>
type BoundSnapshot = {
  订阅id: number
  订阅编号: string
  接收人: string
  绑定报告id: number | null
  绑定快照编号: string | null
  快照版本: number | null
  跟随最新发布: boolean
  下次生成: string | null
  快照状态: string | null
}

export default function SubscriptionView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [boundRows, setBoundRows] = useState<BoundSnapshot[]>([])
  const [bindReportId, setBindReportId] = useState('')
  const [bindTarget, setBindTarget] = useState<number | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const [listRes, boundRes] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request(`${ENDPOINT}/bound-snapshots`),
      ])
      if (!listRes.ok || !boundRes.ok) {
        throw new Error('订阅配置列表读取失败')
      }
      const payload = await listRes.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      setBoundRows((await boundRes.json()).items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '订阅推送列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '订阅推送动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '订阅推送操作失败')
    }
  }

  async function bindSnapshot(row: Row) {
    setErrorMessage('')
    const reportId = bindTarget === Number(row.id) ? bindReportId.trim() : ''
    if (!reportId) {
      setErrorMessage('请先填写要绑定的用户洞察报告 id')
      return
    }
    try {
      const response = await request(`${ENDPOINT}/${row.id}/bind-snapshot`, {
        method: 'POST',
        body: JSON.stringify({ values: { report_id: Number(reportId) } }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message ?? '快照绑定失败')
      }
      setBindTarget(null)
      setBindReportId('')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '快照绑定失败')
    }
  }

  async function sendNow(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/send`, { method: 'POST' })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message ?? '订阅推送失败')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '订阅推送失败')
    }
  }

  return (
    <section className="page" data-module="subscription">
      <header className="page-head">
        <div>
          <h2>订阅推送管理</h2>
          <p className="page-desc">
            订阅正文绑定用户洞察报告快照与下次生成时间；报告重新发布后跟随订阅随快照同步更新，避免推送过期报告。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('订阅配置登记入口尚未接入审批流')}>
            登记订阅配置
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出订阅推送清单
          </button>
        </div>
      </header>

      <h3 className="detail-subtitle">订阅快照同步消费（与用户洞察报告详情同源）</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>订阅编号</th><th>接收人</th><th>绑定报告</th><th>绑定快照</th>
            <th>版本</th><th>跟随最新发布</th><th>下次生成</th><th>快照状态</th>
          </tr>
        </thead>
        <tbody>
          {boundRows.map((item) => (
            <tr key={item.订阅id}>
              <td>{item.订阅编号}</td>
              <td>{item.接收人}</td>
              <td>{item.绑定报告id ?? '—'}</td>
              <td>{item.绑定快照编号 ?? '未绑定'}</td>
              <td>{item.快照版本 === null ? '—' : `v${item.快照版本}`}</td>
              <td>
                <span className={`trend-tag ${item.跟随最新发布 ? 'trend-new' : 'trend-flat'}`}>
                  {item.跟随最新发布 ? '跟随' : '锁版'}
                </span>
              </td>
              <td>{item.下次生成 ?? '—'}</td>
              <td>{item.快照状态 ?? '—'}</td>
            </tr>
          ))}
          {boundRows.length === 0 && (
            <tr><td className="empty-state" colSpan={8}>暂无订阅数据</td></tr>
          )}
        </tbody>
      </table>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>绑定快照</th>
            <th>下次生成</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{(row['绑定快照编号'] as string | null) ?? '未绑定'}</td>
              <td>{(row['下次生成'] as string | null) ?? '—'}</td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => setBindTarget(Number(row.id))}>
                  绑定报告快照
                </button>
                <button className="link" type="button" onClick={() => void sendNow(row)}>
                  立即推送
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无订阅推送数据，可先登记订阅配置</td>
            </tr>
          )}
        </tbody>
      </table>

      {bindTarget !== null && (
        <div className="detail-panel">
          <div className="detail-head">
            <h4 className="detail-subtitle">为订阅 #{bindTarget} 绑定用户洞察报告快照</h4>
            <button className="btn ghost" type="button" onClick={() => setBindTarget(null)}>取消</button>
          </div>
          <div className="filter-bar">
            <label className="filter-item">
              <span>用户洞察报告 id（绑定该报告当前已发布的最新快照）</span>
              <input value={bindReportId} placeholder="如 3" onChange={(e) => setBindReportId(e.target.value)} />
            </label>
            <button
              className="btn primary"
              type="button"
              onClick={() => void bindSnapshot({ id: bindTarget } as Row)}
            >
              确认绑定
            </button>
          </div>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条订阅推送记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
