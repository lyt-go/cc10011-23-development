"""订阅推送接口：维护订阅配置，并把订阅正文绑定到用户洞察报告快照。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.subscription import subscription_service as service

router = APIRouter(prefix="/api/subscription", tags=["订阅推送"])

LIST_FIELDS = ["订阅编号", "订阅内容", "接收人", "推送渠道", "推送频率", "数据粒度", "静默时段", "下次推送时间"]
STATUSES = ["草稿", "订阅评审中", "已启用", "已暂停"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按订阅编号检索"),
    status: str | None = Query(default=None, description="草稿、订阅评审中、已启用、已暂停"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按订阅编号与状态过滤订阅推送列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/bound-snapshots", response_model=dict)
def list_bound_snapshots() -> dict[str, Any]:
    """订阅推送业务面：汇总每条订阅当前绑定的报告快照，随报告快照同步更新。"""
    items: list[dict[str, Any]] = []
    for row in service.list_entries(page=1, size=10000)[0]:
        snapshot = service.read_bound_snapshot(int(row["id"]))
        items.append({
            "订阅id": row["id"],
            "订阅编号": row.get("订阅编号"),
            "接收人": row.get("接收人"),
            "绑定报告id": row.get("绑定报告id"),
            "绑定快照编号": row.get("绑定快照编号"),
            "快照版本": row.get("快照版本"),
            "跟随最新发布": row.get("跟随最新发布", True),
            "下次生成": row.get("下次生成"),
            "快照状态": snapshot.get("状态") if snapshot else None,
        })
    return {"total": len(items), "items": items}


@router.post("/{entry_id}/bind-snapshot", response_model=ActionResult)
def bind_snapshot(entry_id: int, payload: EntryPayload) -> ActionResult:
    """把订阅正文绑定到报告快照；不传快照编号时绑定当前已发布的最新快照。"""
    report_id = payload.values.get("report_id") or payload.values.get("报告id")
    if report_id is None:
        return ActionResult(ok=False, message="缺少 report_id（要绑定的用户洞察报告 id）")
    try:
        report_id = int(report_id)
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="report_id 必须是整数")
    follow = bool(payload.values.get("follow_latest", True))
    snapshot_no = payload.values.get("snapshot_no") or payload.values.get("快照编号")
    entry, message = service.bind_snapshot(
        entry_id, report_id, follow_latest=follow, snapshot_no=snapshot_no,
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/send", response_model=ActionResult)
def send_subscription(entry_id: int) -> ActionResult:
    """触发一次订阅推送：校验正文与报告详情引用同一快照，拒绝推送过期报告。"""
    entry, message = service.send_subscription(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出订阅推送清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "subscription", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条订阅配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"订阅配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条订阅配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="订阅配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条订阅配置执行提交订阅评审、启用订阅、暂停订阅；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
