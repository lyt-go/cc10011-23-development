"""用户洞察接口：洞察报告、生成批次、差异快照与历史回填的入口。

路由顺序约定：静态路径（/batches、/snapshots、/backfills、/export）必须声明在
`/{entry_id}` 之前，否则会被动态路径先捕获。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.userinsight import userinsight_service as service

router = APIRouter(prefix="/api/userinsight", tags=["用户洞察"])

LIST_FIELDS = ["报告编号", "分析主题", "目标人群", "关键指标", "核心发现", "数据区间", "生成时间", "下次生成"]
STATUSES = ["草稿", "生成中", "已发布", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按报告编号检索"),
    status: str | None = Query(default=None, description="草稿、生成中、已发布、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按报告编号与状态过滤用户洞察列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# ---------------------------------------------------------------- 生成批次
@router.get("/batches", response_model=dict)
def list_batches(
    report_id: int | None = Query(default=None, description="按报告 id 过滤生成批次"),
    status: str | None = Query(default=None, description="生成中、已生成、已重算"),
) -> dict[str, Any]:
    """查看报告生成批次：分析主题、目标人群、关键指标、数据区间与分区完成情况都在这里。"""
    items = service.list_batches(report_id=report_id, status=status)
    return {"total": len(items), "items": items}


@router.post("/batches/{batch_id}/partitions", response_model=ActionResult)
def complete_partition(batch_id: int, payload: EntryPayload) -> ActionResult:
    """数据分区完成回调：按分区边界判断是否触发重算，全部就绪后生成差异快照。"""
    boundary = payload.values.get("boundary") or payload.values.get("边界")
    if not boundary:
        return ActionResult(ok=False, message="缺少分区边界 boundary（YYYY-MM-DD）")
    try:
        batch, message, snapshot = service.complete_partition(batch_id, boundary)
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"batch": batch, "snapshot": snapshot})


@router.post("/batches/{batch_id}/late-partitions", response_model=ActionResult)
def complete_late_partition(batch_id: int, payload: EntryPayload) -> ActionResult:
    """迟到数据分区回调：新边界补入批次计划，若快照已生成则按边界判定重算。"""
    boundary = payload.values.get("boundary") or payload.values.get("边界")
    if not boundary:
        return ActionResult(ok=False, message="缺少分区边界 boundary（YYYY-MM-DD）")
    try:
        batch, message, snapshot = service.extend_partition_boundary(batch_id, boundary)
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"batch": batch, "snapshot": snapshot})


# ---------------------------------------------------------------- 差异快照
@router.get("/snapshots", response_model=dict)
def list_snapshots(
    report_id: int | None = Query(default=None, description="按报告 id 过滤快照"),
) -> dict[str, Any]:
    """列出差异快照；历史快照（含已归档报告的旧版本）始终保持可读。"""
    items = service.list_snapshots(report_id=report_id)
    return {"total": len(items), "items": items}


@router.get("/snapshots/{snapshot_id}", response_model=dict)
def get_snapshot(snapshot_id: int) -> dict:
    """读取单份快照明细：数据区间、指标变化与核心发现均可追溯到生成批次。"""
    snapshot = service.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"快照 {snapshot_id} 不存在")
    return snapshot


@router.get("/reports/{entry_id}/diff-overview", response_model=dict)
def diff_overview(entry_id: int) -> dict[str, Any]:
    """报告页概览视图：目标人群、数据区间与关键指标变化的差异汇总。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"洞察报告 {entry_id} 不存在或已归档")
    return service.snapshot_diff_overview(entry_id)


# ---------------------------------------------------------------- 历史回填
@router.get("/backfills", response_model=dict)
def list_backfills(
    report_id: int | None = Query(default=None, description="按报告 id 过滤回填批次"),
) -> dict[str, Any]:
    """查看历史回填批次与断点续传进度。"""
    items = service.list_backfills(report_id=report_id)
    return {"total": len(items), "items": items}


@router.post("/reports/{entry_id}/backfills", response_model=ActionResult)
def submit_backfill(entry_id: int, payload: EntryPayload) -> ActionResult:
    """登记历史回填批次：同一回填批次号重复提交只写入一次。"""
    record, message = service.submit_backfill(entry_id, payload.values)
    if record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=record)


@router.post("/backfills/{backfill_id}/partitions", response_model=ActionResult)
def complete_backfill_partition(backfill_id: int, payload: EntryPayload) -> ActionResult:
    """回填分区完成回调：中断后从上一完整分区继续，全部完成后驱动生成批次。"""
    boundary = payload.values.get("boundary") or payload.values.get("边界")
    if not boundary:
        return ActionResult(ok=False, message="缺少分区边界 boundary（YYYY-MM-DD）")
    try:
        record, message = service.complete_backfill_partition(backfill_id, boundary)
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    if record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=record)


# ---------------------------------------------------------------- 报告明细与动作
@router.post("/reports/{entry_id}/generation", response_model=ActionResult)
def submit_generation(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交报告生成：同一分析主题已有生成中批次时复用，不新增生成任务。"""
    entry, message = service.submit_generation(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出用户洞察清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "userinsight", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条洞察报告明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"洞察报告 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条洞察报告，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="洞察报告已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条洞察报告执行提交生成、发布报告、归档报告；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
