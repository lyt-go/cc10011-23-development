import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/userinsight'
const columns = [
  "报告编号",
  "分析主题",
  "目标人群",
  "关键指标",
  "核心发现",
  "数据区间",
  "生成时间",
  "下次生成"
]
const actions = [
  "提交生成",
  "发布报告",
  "归档报告"
]

type Row = Record<string, string | number | null>
type MetricDetail = {
  指标: string
  当前值: number | null
  基线值: number | null
  变化量: number | null
  变化率百分比: number | null
}
type Snapshot = {
  id: number
  快照编号: string
  版本: number
  批次编号: string
  目标人群: string
  数据区间起: string
  数据区间止: string
  指标明细: MetricDetail[]
  核心发现: string
  生成时间: string
  下次生成: string
  是否重算: boolean
  状态: string
}
type Batch = {
  id: number
  批次编号: string
  分析主题: string
  目标人群: string
  关键指标: string
  数据区间起: string
  数据区间止: string
  分区边界: string[]
  已完成边界: string[]
  状态: string
  生成时间: string
  重算次数: number
  快照编号: string | null
}
type Backfill = {
  id: number
  回填批次号: string
  数据区间起: string
  数据区间止: string
  分区边界: string[]
  已完成边界: string[]
  状态: string
  关联生成批次: string | null
}
type DiffOverview = {
  报告id: number
  版本数: number
  目标人群?: string
  数据区间?: string
  当前快照编号?: string
  差异概览: { 指标: string; 当前值: number | null; 变化量: number | null; 变化率百分比: number | null; 方向: string }[]
  快照: Snapshot[]
}

const directionClass: Record<string, string> = {
  上升: 'trend-up',
  下降: 'trend-down',
  持平: 'trend-flat',
  新增: 'trend-new',
}

export default function UserinsightView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [activeReportId, setActiveReportId] = useState<number | null>(null)
  const [overview, setOverview] = useState<DiffOverview | null>(null)
  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const [batches, setBatches] = useState<Batch[]>([])
  const [backfills, setBackfills] = useState<Backfill[]>([])
  const [backfillNo, setBackfillNo] = useState('')
  const [backfillWindow, setBackfillWindow] = useState('')
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('洞察报告列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '用户洞察列表读取失败')
    }
  }, [filters])

  const loadReportDetail = useCallback(async (reportId: number) => {
    try {
      const [overviewRes, snapshotRes, batchRes, backfillRes] = await Promise.all([
        request(`${ENDPOINT}/reports/${reportId}/diff-overview`),
        request(`${ENDPOINT}/snapshots?report_id=${reportId}`),
        request(`${ENDPOINT}/batches?report_id=${reportId}`),
        request(`${ENDPOINT}/backfills?report_id=${reportId}`),
      ])
      if (!overviewRes.ok || !snapshotRes.ok || !batchRes.ok || !backfillRes.ok) {
        throw new Error('报告差异数据读取失败')
      }
      setOverview(await overviewRes.json())
      setSnapshots((await snapshotRes.json()).items ?? [])
      setBatches((await batchRes.json()).items ?? [])
      setBackfills((await backfillRes.json()).items ?? [])
      setActiveReportId(reportId)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '报告差异数据读取失败')
    }
  }, [])

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
        throw new Error(payload.message ?? '用户洞察动作未生效，请稍后重试')
      }
      await reload()
      if (activeReportId === Number(row.id)) {
        await loadReportDetail(Number(row.id))
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '用户洞察操作失败')
    }
  }

  async function submitBackfill(row: Row) {
    setErrorMessage('')
    if (!backfillNo.trim()) {
      setErrorMessage('请先填写回填批次号')
      return
    }
    try {
      const response = await request(`${ENDPOINT}/reports/${row.id}/backfills`, {
        method: 'POST',
        body: JSON.stringify({ values: { 回填批次号: backfillNo, 数据区间: backfillWindow } }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message ?? '回填批次登记失败')
      }
      setBackfillNo('')
      await loadReportDetail(Number(row.id))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '回填批次登记失败')
    }
  }

  return (
    <section className="page" data-module="userinsight">
      <header className="page-head">
        <div>
          <h2>用户洞察管理</h2>
          <p className="page-desc">
            报告按生成批次取数并产出不可变差异快照；生成中的报告不能发布，发布后的快照只供订阅读取。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('洞察报告登记入口尚未接入审批流')}>
            登记洞察报告
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出用户洞察清单
          </button>
        </div>
      </header>

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
            <th>状态</th>
            <th>绑定快照</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={activeReportId === Number(row.id) ? 'row-active' : ''}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td><span className={`status-tag status-${String(row.status ?? '')}`}>{String(row.status ?? '—')}</span></td>
              <td>{(row['当前快照编号'] as string | null) ?? '—'}</td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void loadReportDetail(Number(row.id))}>
                  差异概览
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无用户洞察数据，可先登记洞察报告</td>
            </tr>
          )}
        </tbody>
      </table>

      {activeReportId !== null && overview && (
        <div className="detail-panel">
          <div className="detail-head">
            <h3>报告 #{activeReportId} 差异快照与生成批次</h3>
            <button className="btn ghost" type="button" onClick={() => setActiveReportId(null)}>收起</button>
          </div>

          <div className="stat-row">
            <article className="stat-card">
              <span className="stat-label">目标人群</span>
              <strong className="stat-value stat-text">{overview.目标人群 ?? '—'}</strong>
            </article>
            <article className="stat-card">
              <span className="stat-label">数据区间</span>
              <strong className="stat-value stat-text">{overview.数据区间 ?? '—'}</strong>
            </article>
            <article className="stat-card">
              <span className="stat-label">快照版本数</span>
              <strong className="stat-value">{overview.版本数}</strong>
            </article>
            <article className="stat-card">
              <span className="stat-label">当前发布快照</span>
              <strong className="stat-value stat-text">{overview.当前快照编号 ?? '未发布'}</strong>
            </article>
          </div>

          <h4 className="detail-subtitle">关键指标变化（概览视图）</h4>
          <table className="data-table">
            <thead>
              <tr><th>关键指标</th><th>当前值</th><th>变化量</th><th>变化率</th><th>方向</th></tr>
            </thead>
            <tbody>
              {overview.差异概览.map((item) => (
                <tr key={item.指标}>
                  <td>{item.指标}</td>
                  <td>{item.当前值 ?? '—'}</td>
                  <td>{item.变化量 ?? '—'}</td>
                  <td>{item.变化率百分比 === null ? '—' : `${item.变化率百分比}%`}</td>
                  <td><span className={`trend-tag ${directionClass[item.方向] ?? ''}`}>{item.方向}</span></td>
                </tr>
              ))}
              {overview.差异概览.length === 0 && (
                <tr><td className="empty-state" colSpan={5}>尚无快照，提交生成并等待分区完成后自动产出</td></tr>
              )}
            </tbody>
          </table>

          <h4 className="detail-subtitle">报告生成批次（取数凭证可追溯）</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>批次编号</th><th>分析主题</th><th>目标人群</th><th>数据区间</th>
                <th>分区进度</th><th>状态</th><th>重算次数</th><th>快照编号</th><th>生成时间</th>
              </tr>
            </thead>
            <tbody>
              {batches.map((batch) => (
                <tr key={batch.id}>
                  <td>{batch.批次编号}</td>
                  <td>{batch.分析主题}</td>
                  <td>{batch.目标人群}</td>
                  <td>{batch.数据区间起} ~ {batch.数据区间止}</td>
                  <td>{batch.已完成边界.length}/{batch.分区边界.length}</td>
                  <td><span className={`status-tag status-${batch.状态}`}>{batch.状态}</span></td>
                  <td>{batch.重算次数}</td>
                  <td>{batch.快照编号 ?? '—'}</td>
                  <td>{batch.生成时间}</td>
                </tr>
              ))}
              {batches.length === 0 && (
                <tr><td className="empty-state" colSpan={9}>暂无生成批次</td></tr>
              )}
            </tbody>
          </table>

          <h4 className="detail-subtitle">差异快照（历史版本保持可读）</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>快照编号</th><th>版本</th><th>来源批次</th><th>数据区间</th>
                <th>核心发现</th><th>下次生成</th><th>是否重算</th><th>状态</th>
              </tr>
            </thead>
            <tbody>
              {snapshots.map((snapshot) => (
                <tr key={snapshot.id}>
                  <td>{snapshot.快照编号}</td>
                  <td>v{snapshot.版本}</td>
                  <td>{snapshot.批次编号}</td>
                  <td>{snapshot.数据区间起} ~ {snapshot.数据区间止}</td>
                  <td className="finding-cell">{snapshot.核心发现}</td>
                  <td>{snapshot.下次生成}</td>
                  <td>{snapshot.是否重算 ? '是' : '否'}</td>
                  <td><span className={`status-tag status-${snapshot.状态}`}>{snapshot.状态}</span></td>
                </tr>
              ))}
              {snapshots.length === 0 && (
                <tr><td className="empty-state" colSpan={8}>暂无快照</td></tr>
              )}
            </tbody>
          </table>

          <h4 className="detail-subtitle">历史数据回填（重复批次只写一次，中断后从上一完整分区继续）</h4>
          <div className="filter-bar">
            <label className="filter-item">
              <span>回填批次号</span>
              <input value={backfillNo} placeholder="如 BF-202609-002" onChange={(e) => setBackfillNo(e.target.value)} />
            </label>
            <label className="filter-item">
              <span>数据区间（YYYY-MM-DD ~ YYYY-MM-DD）</span>
              <input value={backfillWindow} placeholder="2026-07-01 ~ 2026-07-14" onChange={(e) => setBackfillWindow(e.target.value)} />
            </label>
            <button className="btn primary" type="button" onClick={() => void submitBackfill({ id: activeReportId } as Row)}>
              提交回填批次
            </button>
          </div>
          <table className="data-table">
            <thead>
              <tr>
                <th>回填批次号</th><th>数据区间</th><th>分区进度</th><th>状态</th><th>关联生成批次</th>
              </tr>
            </thead>
            <tbody>
              {backfills.map((item) => (
                <tr key={item.id}>
                  <td>{item.回填批次号}</td>
                  <td>{item.数据区间起} ~ {item.数据区间止}</td>
                  <td>{item.已完成边界.length}/{item.分区边界.length}</td>
                  <td><span className={`status-tag status-${item.状态}`}>{item.状态}</span></td>
                  <td>{item.关联生成批次 ?? '—'}</td>
                </tr>
              ))}
              {backfills.length === 0 && (
                <tr><td className="empty-state" colSpan={5}>暂无回填批次</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条用户洞察记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
