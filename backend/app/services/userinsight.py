"""用户洞察业务规则：报告生成批次、差异快照、回填断点与订阅绑定都收在这里。

领域约定（与需求逐条对应）：

1. 报告生成批次（userinsight_batch）
   - 保存分析主题、目标人群、关键指标、数据区间与生成时间，是一次取数的完整凭证。
   - 同一分析主题在有「生成中」批次时重复提交不会多出生成任务（幂等）。
   - 两个数据分区并发完成时，按分区边界判断是否落在本批次区间内、是否需要重算，
     全部边界就绪后按同一区间计算关键指标变化并产出差异快照。

2. 报告差异快照（userinsight_snapshot）
   - 快照一旦生成即不可变，保存目标人群、数据区间、关键指标取值与变化、核心发现。
   - 生成中的报告不能发布；发布动作只会把报告指向一个已生成的快照。
   - 发布后的快照只允许订阅侧只读消费，任何重新生成都产生新版本快照，旧快照保留可读。

3. 历史回填（userinsight_backfill）
   - 每个回填批次有稳定批次号，重复提交同一批次只写入一次。
   - 按分区边界推进，中断后从上一完整分区继续；全部完成后驱动报告生成批次。

4. 订阅绑定（subscription 侧通过本服务只读读取快照）
   - 订阅正文绑定具体快照编号与下次生成时间；另一个业务面（订阅推送）随快照同步更新，
     保证订阅正文和报告详情引用同一份快照，不会推送已过期的报告。
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from typing import Any

from app.store import store

MODULE = "userinsight"
BATCH_MODULE = "userinsight_batch"
SNAPSHOT_MODULE = "userinsight_snapshot"
BACKFILL_MODULE = "userinsight_backfill"

REQUIRED_FIELDS = ["报告编号", "分析主题", "目标人群"]
STATUS_ORDER = ["草稿", "生成中", "已发布", "已归档"]
ACTION_RULES = {"提交生成": "生成中", "发布报告": "已发布", "归档报告": "已归档"}
NEGATIVE_ACTIONS = []

# 生成批次的生命周期：待分区 -> 生成中 -> 已生成（快照就绪）/ 已重算
BATCH_STATUS = ["生成中", "已生成", "已重算"]
SNAPSHOT_STATUS = ["已生成", "已发布", "已归档"]
BACKFILL_STATUS = ["进行中", "已完成"]

# 关键指标的模拟取数口径：同一数据区间在任何一次调用中都得到同一结果，
# 这样「按同一区间计算」才可复现、可追溯。真实项目里这里替换为指标查询层。
METRIC_NAMES = ["活跃用户数", "付费转化率", "次周留存率", "人均使用时长"]


def _today() -> date:
    return datetime.now().date()


def _parse_boundary(value: Any) -> date:
    """把分区边界（YYYY-MM-DD 字符串或 date）解析为 date，非法值直接报错。"""
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"分区边界「{value}」不是 YYYY-MM-DD 日期") from exc


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _metric_value(metric: str, day: date) -> float:
    """单个指标在某一天的确定性取值：只依赖指标名与日期，保证可重算。"""
    digest = sum(ord(ch) for ch in metric) + day.toordinal()
    if metric == "活跃用户数":
        return round(8000 + (digest % 4000) + day.day * 13, 1)
    if metric == "付费转化率":
        return round(2.5 + (digest % 300) / 100, 2)
    if metric == "次周留存率":
        return round(35 + (digest % 200) / 10, 2)
    return round(12 + (digest % 180) / 10, 2)


def _aggregate_metric(metric: str, start: date, end: date) -> float:
    """把区间内每天的确定性取值聚合成一个区间值（均值口径）。"""
    days = (end - start).days + 1
    total = sum(_metric_value(metric, start + timedelta(days=offset)) for offset in range(days))
    return round(total / max(days, 1), 2)


# 分区完成回调可能来自不同工作线程（两个数据分区并发到达），用模块级锁
# 串行化批次/快照/回填三张表的读改写，保证边界判断与重算决策的原子性。
_STORE_LOCK = threading.RLock()


class UserinsightService:
    def __init__(self) -> None:
        # 保留实例属性作为别名，所有状态流转都走同一把模块级锁
        self._lock = _STORE_LOCK

    # ------------------------------------------------------------------ 报告列表
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("报告编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with self._lock:
            rows = store.rows(MODULE)
            entry = {"id": _next_id(rows)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            # 登记时允许同时给出关键指标与数据区间，作为首次取数的默认口径
            for optional in ("关键指标", "数据区间"):
                if str(values.get(optional) or "").strip():
                    entry[optional] = values[optional]
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["当前快照编号"] = None
            rows.append(entry)
        return entry, []

    # ----------------------------------------------------------- 报告状态动作
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"洞察报告 {entry_id} 不存在或已归档"

            if action == "提交生成":
                return self.submit_generation(entry_id, {
                    "分析主题": entry.get("分析主题"),
                    "目标人群": entry.get("目标人群"),
                    "关键指标": entry.get("关键指标"),
                    "数据区间": entry.get("数据区间"),
                })

            if action == "发布报告":
                return self.publish_entry(entry_id)

            if action == "归档报告":
                entry["status"] = "已归档"
                entry["pending"] = False
                return entry, "洞察报告已归档"

            return None, f"动作「{action}」不属于用户洞察可执行范围"

    # ----------------------------------------------------------- 报告生成批次
    def submit_generation(
        self, entry_id: int, values: dict[str, Any], *, force_new: bool = False
    ) -> tuple[dict[str, Any] | None, str]:
        """提交一次报告生成：同一分析主题已有生成中批次时直接复用，不新增任务。

        ``force_new`` 仅供历史回填完成后的内部驱动使用：每个回填批次本身就是一次
        独立取数任务，需要自己的生成批次；手工/接口提交路径仍严格幂等。
        """
        with self._lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"洞察报告 {entry_id} 不存在或已归档"

            topic = str(values.get("分析主题") or entry.get("分析主题") or "").strip()
            audience = str(values.get("目标人群") or entry.get("目标人群") or "").strip()
            metrics_raw = str(values.get("关键指标") or entry.get("关键指标") or "").strip()
            window_raw = str(values.get("数据区间") or entry.get("数据区间") or "").strip()
            if not topic or not audience:
                return None, "缺少分析主题或目标人群，无法提交生成"

            metrics = [item.strip() for item in metrics_raw.split("、") if item.strip()] or list(METRIC_NAMES)
            window = self._safe_window(window_raw)
            boundaries = self._window_boundaries(window)

            batches = store.rows(BATCH_MODULE)
            report_batch_count = sum(1 for row in batches if int(row.get("报告id", 0)) == entry_id)
            # 幂等：同一报告 + 同一分析主题已有生成中批次，重复提交不会多出生成任务
            if not force_new:
                for batch in batches:
                    if (
                        int(batch.get("报告id", 0)) == entry_id
                        and batch.get("分析主题") == topic
                        and batch.get("状态") == "生成中"
                    ):
                        return entry, f"分析主题「{topic}」已有生成中的批次 {batch['批次编号']}，已复用未重复建任务"

            # 生成中的报告不能发布：提交生成即把报告置为生成中，发布动作会据此拦截
            entry["status"] = "生成中"
            entry["pending"] = True
            entry["分析主题"] = topic
            entry["目标人群"] = audience
            entry["关键指标"] = "、".join(metrics)

            batch = {
                "id": _next_id(batches),
                "批次编号": f"BATCH-{entry_id}-{report_batch_count + 1:03d}",
                "报告id": entry_id,
                "报告编号": entry.get("报告编号"),
                "分析主题": topic,
                "目标人群": audience,
                "关键指标": "、".join(metrics),
                "数据区间起": window["start"],
                "数据区间止": window["end"],
                "分区边界": [day.isoformat() for day in boundaries],
                "已完成边界": [],
                "状态": "生成中",
                "生成时间": _today().isoformat(),
                "重算次数": 0,
                "快照编号": None,
            }
            batches.append(batch)
            entry["当前批次编号"] = batch["批次编号"]
            return entry, f"生成批次 {batch['批次编号']} 已建立，等待 {len(boundaries)} 个数据分区完成"

    def complete_partition(
        self, batch_id: int, boundary: Any
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """上报某个数据分区在给定边界完成。

        并发语义：两个分区同时完成时，在锁内依次处理。边界落在批次区间外则忽略；
        边界已登记则幂等跳过；全部边界就绪后按同一区间计算并生成快照。
        若快照生成后又有区间内的迟到分区到达，则判定需要重算并新建批次口径的快照版本。
        """
        with self._lock:
            batch = store.find(BATCH_MODULE, batch_id)
            if batch is None:
                return None, f"生成批次 {batch_id} 不存在", None

            boundary_day = _parse_boundary(boundary)
            start = _parse_boundary(batch["数据区间起"])
            end = _parse_boundary(batch["数据区间止"])

            # 边界判定：只接受区间内的分区边界，区间外的完成事件不影响本批次
            if not start <= boundary_day <= end:
                return batch, f"边界 {boundary_day} 超出批次区间，已忽略，不触发重算", None

            done = [_parse_boundary(item) for item in batch["已完成边界"]]
            if boundary_day in done:
                # 已登记边界永远幂等：无论批次是否已生成，重复上报都不触发重算
                return batch, f"边界 {boundary_day} 已登记，重复上报已忽略", None
            done.append(boundary_day)
            done.sort()
            batch["已完成边界"] = [day.isoformat() for day in done]

            expected = [_parse_boundary(item) for item in batch["分区边界"]]
            snapshot = None
            if batch["状态"] == "生成中":
                if not all(day in done for day in expected):
                    return batch, f"边界 {boundary_day} 已登记，进度 {len(done)}/{len(expected)}", None
                snapshot = self._build_snapshot(batch, recompute=False)
                batch["状态"] = "已生成"
                batch["快照编号"] = snapshot["快照编号"]
                self._refresh_entry_state(int(batch["报告id"]))
                return batch, f"全部 {len(expected)} 个分区就绪，已生成快照 {snapshot['快照编号']}", snapshot

            # 仅当到达的是此前未登记过的新区间边界（迟到分区）时，才按边界判定需要重算
            batch["重算次数"] = int(batch.get("重算次数", 0)) + 1
            snapshot = self._build_snapshot(batch, recompute=True)
            batch["状态"] = "已重算"
            batch["快照编号"] = snapshot["快照编号"]
            self._refresh_entry_state(int(batch["报告id"]))
            return batch, f"检测到区间内迟到分区，已重算并生成快照 {snapshot['快照编号']}", snapshot

    def list_batches(
        self, *, report_id: int | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        rows = store.rows(BATCH_MODULE)
        if report_id is not None:
            rows = [row for row in rows if int(row.get("报告id", 0)) == report_id]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        return rows

    def extend_partition_boundary(self, batch_id: int, boundary: Any) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """快照生成后到达的迟到分区：把新边界纳入批次计划并立即完成，触发重算。

        与 ``complete_partition`` 的区别：这里登记的是一条原计划之外的新区间边界，
        语义上对应「数据分区延迟就绪、补齐了此前缺失的一段数据」。
        """
        with self._lock:
            batch = store.find(BATCH_MODULE, batch_id)
            if batch is None:
                return None, f"生成批次 {batch_id} 不存在", None
            boundary_day = _parse_boundary(boundary)
            start = _parse_boundary(batch["数据区间起"])
            end = _parse_boundary(batch["数据区间止"])
            if not start <= boundary_day <= end:
                return batch, f"边界 {boundary_day} 超出批次区间，已忽略", None

            planned = [_parse_boundary(item) for item in batch["分区边界"]]
            if boundary_day in planned:
                # 已在计划内的边界走普通完成流程，保持幂等语义
                return self.complete_partition(batch_id, boundary)
            if batch.get("状态") == "生成中":
                # 批次尚未结束，迟到分区直接补入计划，按正常分区完成处理即可
                planned.append(boundary_day)
                planned.sort()
                batch["分区边界"] = [day.isoformat() for day in planned]
                return self.complete_partition(batch_id, boundary_day)

            # 批次已生成快照：新边界代表一段此前缺失的数据，纳入计划后触发重算
            planned.append(boundary_day)
            planned.sort()
            batch["分区边界"] = [day.isoformat() for day in planned]
            return self.complete_partition(batch_id, boundary_day)

    # ----------------------------------------------------------- 差异快照
    def _build_snapshot(self, batch: dict[str, Any], *, recompute: bool) -> dict[str, Any]:
        """按批次同一数据区间计算关键指标变化，生成不可变快照。"""
        snapshots = store.rows(SNAPSHOT_MODULE)
        start = _parse_boundary(batch["数据区间起"])
        end = _parse_boundary(batch["数据区间止"])
        metrics = [item.strip() for item in str(batch["关键指标"]).split("、") if item.strip()]

        # 上一版本快照：同一报告最近一次快照，作为差异对比基线
        previous = next(
            (
                row for row in reversed(snapshots)
                if int(row.get("报告id", 0)) == int(batch["报告id"])
            ),
            None,
        )
        prev_values = {item["指标"]: item["当前值"] for item in previous["指标明细"]} if previous else {}

        details: list[dict[str, Any]] = []
        findings: list[str] = []
        for metric in metrics:
            current = _aggregate_metric(metric, start, end)
            base = prev_values.get(metric)
            delta = round(current - base, 2) if base is not None else None
            change_rate = round(delta / base * 100, 2) if base not in (None, 0) else None
            details.append({
                "指标": metric,
                "当前值": current,
                "基线值": base,
                "变化量": delta,
                "变化率百分比": change_rate,
            })
            if delta is None:
                findings.append(f"{metric} 首次纳入快照，区间值 {current}")
            elif delta > 0:
                findings.append(f"{metric} 较上一快照上升 {abs(delta)}（{change_rate}%）")
            elif delta < 0:
                findings.append(f"{metric} 较上一快照下降 {abs(delta)}（{abs(change_rate or 0)}%）")
            else:
                findings.append(f"{metric} 与上一快照持平（{current}）")

        version = (
            max(
                (int(row.get("版本", 0)) for row in snapshots
                 if int(row.get("报告id", 0)) == int(batch["报告id"])),
                default=0,
            ) + 1
        )
        next_run = (end + timedelta(days=7)).isoformat()
        snapshot = {
            "id": _next_id(snapshots),
            "快照编号": f"SNAP-{batch['报告id']}-{version:03d}",
            "版本": version,
            "报告id": batch["报告id"],
            "报告编号": batch["报告编号"],
            "批次编号": batch["批次编号"],
            "分析主题": batch["分析主题"],
            "目标人群": batch["目标人群"],
            "关键指标": batch["关键指标"],
            "数据区间起": batch["数据区间起"],
            "数据区间止": batch["数据区间止"],
            "指标明细": details,
            "核心发现": "；".join(findings),
            "生成时间": _today().isoformat(),
            "下次生成": next_run,
            "是否重算": recompute,
            "状态": "已生成",
            "来源": "生成批次",
        }
        snapshots.append(snapshot)
        return snapshot

    def list_snapshots(
        self, *, report_id: int | None = None, include_archived: bool = True
    ) -> list[dict[str, Any]]:
        rows = store.rows(SNAPSHOT_MODULE)
        if report_id is not None:
            rows = [row for row in rows if int(row.get("报告id", 0)) == report_id]
        if not include_archived:
            rows = [row for row in rows if row.get("状态") != "已归档"]
        return rows

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        """快照读取入口：历史快照始终保持可读（含已归档报告的旧版本）。"""
        return store.find(SNAPSHOT_MODULE, snapshot_id)

    def latest_published_snapshot(self, report_id: int) -> dict[str, Any] | None:
        """订阅侧只读入口：只返回报告当前发布指向的快照。"""
        with self._lock:
            entry = store.find(MODULE, report_id)
            if entry is None or not entry.get("当前快照编号"):
                return None
            return next(
                (
                    row for row in store.rows(SNAPSHOT_MODULE)
                    if row["快照编号"] == entry["当前快照编号"]
                ),
                None,
            )

    def _refresh_entry_state(self, entry_id: int) -> None:
        """批次状态变化后回写报告：仍有生成中批次则报告保持生成中，否则回到草稿待发布。"""
        entry = store.find(MODULE, entry_id)
        if entry is None or entry.get("status") in ("已发布", "已归档"):
            return
        open_batches = [
            row for row in store.rows(BATCH_MODULE)
            if int(row.get("报告id", 0)) == entry_id and row.get("状态") == "生成中"
        ]
        if open_batches:
            entry["status"] = "生成中"
            entry["pending"] = True
        else:
            entry["status"] = "草稿"
            entry["pending"] = True

    def publish_entry(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """发布报告：存在生成中批次（生成中的报告）时不能发布；发布后快照只读，供订阅消费。"""
        with self._lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"洞察报告 {entry_id} 不存在或已归档"
            open_batches = [
                row for row in store.rows(BATCH_MODULE)
                if int(row.get("报告id", 0)) == entry_id and row.get("状态") == "生成中"
            ]
            if open_batches:
                return None, (
                    f"报告仍在生成中，批次 {open_batches[0]['批次编号']} 尚未完成，不能发布"
                )

            candidate = None
            for snapshot in reversed(store.rows(SNAPSHOT_MODULE)):
                if int(snapshot.get("报告id", 0)) == entry_id:
                    candidate = snapshot
                    break
            if candidate is None:
                return None, "尚无已生成的差异快照，不能发布"

            # 旧发布快照保留为历史只读版本，新快照标记为当前发布版本
            for row in store.rows(SNAPSHOT_MODULE):
                if int(row.get("报告id", 0)) == entry_id and row.get("状态") == "已发布":
                    row["状态"] = "已归档"
            candidate["状态"] = "已发布"
            entry["当前快照编号"] = candidate["快照编号"]
            entry["status"] = "已发布"
            entry["pending"] = False
            entry["数据区间"] = f"{candidate['数据区间起']} ~ {candidate['数据区间止']}"
            entry["核心发现"] = candidate["核心发现"]
            entry["生成时间"] = candidate["生成时间"]
            entry["下次生成"] = candidate["下次生成"]

            # 另一个业务面（订阅推送）沿服务层路径同步消费新快照：
            # 跟随最新发布的订阅正文随快照一起更新，固定快照的订阅保持锁版。
            from app.services.subscription import subscription_service

            synced = subscription_service.sync_with_report(entry_id)
            extra = f"，已同步 {len(synced)} 条订阅" if synced else ""
            return entry, f"报告已发布并绑定快照 {candidate['快照编号']}，快照转为只读{extra}"

    def snapshot_diff_overview(self, report_id: int) -> dict[str, Any]:
        """报告页概览视图：汇总目标人群、数据区间与关键指标变化。"""
        with self._lock:
            snapshots = self.list_snapshots(report_id=report_id)
            if not snapshots:
                return {"报告id": report_id, "版本数": 0, "快照": [], "差异概览": []}
            latest = snapshots[-1]
            overview = [
                {
                    "指标": item["指标"],
                    "当前值": item["当前值"],
                    "变化量": item["变化量"],
                    "变化率百分比": item["变化率百分比"],
                    "方向": (
                        "新增" if item["变化量"] is None
                        else "上升" if item["变化量"] > 0
                        else "下降" if item["变化量"] < 0
                        else "持平"
                    ),
                }
                for item in latest["指标明细"]
            ]
            return {
                "报告id": report_id,
                "版本数": len(snapshots),
                "目标人群": latest["目标人群"],
                "数据区间": f"{latest['数据区间起']} ~ {latest['数据区间止']}",
                "当前快照编号": latest["快照编号"],
                "快照": snapshots,
                "差异概览": overview,
            }

    # ----------------------------------------------------------- 历史回填
    def submit_backfill(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """登记历史回填批次：同一回填批次号重复提交只写入一次。"""
        with self._lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"洞察报告 {entry_id} 不存在或已归档"
            backfill_no = str(values.get("回填批次号") or "").strip()
            if not backfill_no:
                return None, "缺少回填批次号"
            table = store.rows(BACKFILL_MODULE)
            for row in table:
                if row.get("回填批次号") == backfill_no:
                    # 幂等：重复提交直接返回已有批次，不重复写入
                    return row, f"回填批次 {backfill_no} 已存在，重复提交已忽略"

            window = self._safe_window(values.get("数据区间") or entry.get("数据区间"))
            boundaries = self._window_boundaries(window)
            record = {
                "id": _next_id(table),
                "回填批次号": backfill_no,
                "报告id": entry_id,
                "报告编号": entry.get("报告编号"),
                "目标人群": entry.get("目标人群"),
                "数据区间起": window["start"],
                "数据区间止": window["end"],
                "分区边界": [day.isoformat() for day in boundaries],
                "已完成边界": [],
                "状态": "进行中",
                "提交时间": _today().isoformat(),
                "关联生成批次": None,
            }
            table.append(record)
            return record, f"回填批次 {backfill_no} 已登记，共 {len(boundaries)} 个分区"

    def complete_backfill_partition(
        self, backfill_id: int, boundary: Any
    ) -> tuple[dict[str, Any] | None, str]:
        """回填分区上报：中断后从上一完整分区继续；全部完成后驱动生成批次与快照。"""
        with self._lock:
            record = store.find(BACKFILL_MODULE, backfill_id)
            if record is None:
                return None, f"回填批次 {backfill_id} 不存在"
            if record["状态"] == "已完成":
                return record, "回填批次已完成，分区上报已忽略"

            boundary_day = _parse_boundary(boundary)
            start = _parse_boundary(record["数据区间起"])
            end = _parse_boundary(record["数据区间止"])
            if not start <= boundary_day <= end:
                return record, f"边界 {boundary_day} 超出回填区间，已忽略"

            done = [_parse_boundary(item) for item in record["已完成边界"]]
            if boundary_day in done:
                return record, f"边界 {boundary_day} 已完成，重复上报已忽略"
            done.append(boundary_day)
            done.sort()
            record["已完成边界"] = [day.isoformat() for day in done]

            expected = [_parse_boundary(item) for item in record["分区边界"]]
            if not all(day in done for day in expected):
                # 断点续传：已完成边界持久化在记录里，中断重跑时从上一完整分区继续
                return record, f"回填进度 {len(done)}/{len(expected)}，下次从上一完整分区之后继续"

            record["状态"] = "已完成"
            entry = store.find(MODULE, int(record["报告id"]))
            topic = str(entry.get("分析主题") or "") if entry else f"回填-{record['回填批次号']}"
            # 回填是独立取数任务：强制建立自己的生成批次，不受手工生成任务幂等复用影响
            _, message = self.submit_generation(int(record["报告id"]), {
                "分析主题": topic,
                "目标人群": record.get("目标人群"),
                "数据区间": f"{record['数据区间起']} ~ {record['数据区间止']}",
            }, force_new=True)
            batch = next(
                (
                    row for row in reversed(store.rows(BATCH_MODULE))
                    if int(row.get("报告id", 0)) == int(record["报告id"])
                    and row.get("数据区间起") == record["数据区间起"]
                    and row.get("数据区间止") == record["数据区间止"]
                ),
                None,
            )
            if batch is not None:
                record["关联生成批次"] = batch["批次编号"]
                # 回填数据已按分区全部就绪，直接补齐批次的所有边界并产出快照
                for edge in batch["分区边界"]:
                    self.complete_partition(int(batch["id"]), edge)
            return record, f"回填全部完成，已驱动生成批次并产出快照（{message}）"

    def list_backfills(self, *, report_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(BACKFILL_MODULE)
        if report_id is not None:
            rows = [row for row in rows if int(row.get("报告id", 0)) == report_id]
        return rows

    # ----------------------------------------------------------- 内部工具
    def _normalize_window(self, raw: Any) -> dict[str, str]:
        """把「2026-09-01 ~ 2026-09-07」之类的区间规范成 start/end，缺省取最近 7 天。"""
        text = str(raw or "").strip()
        if "~" in text:
            left, right = [part.strip() for part in text.split("~", 1)]
        elif "至" in text:
            left, right = [part.strip() for part in text.split("至", 1)]
        elif text:
            left = right = text
        else:
            end = _today() - timedelta(days=1)
            start = end - timedelta(days=6)
            return {"start": start.isoformat(), "end": end.isoformat()}
        start_day, end_day = _parse_boundary(left), _parse_boundary(right)
        if start_day > end_day:
            start_day, end_day = end_day, start_day
        return {"start": start_day.isoformat(), "end": end_day.isoformat()}

    def _safe_window(self, raw: Any) -> dict[str, str]:
        """兼容历史数据：老记录的数据区间可能是说明文字而非日期，解析失败时退回默认窗口。"""
        try:
            return self._normalize_window(raw)
        except ValueError:
            end = _today() - timedelta(days=1)
            start = end - timedelta(days=6)
            return {"start": start.isoformat(), "end": end.isoformat()}

    def _window_boundaries(self, window: dict[str, str]) -> list[date]:
        """按周切分区：每周一条边界，落在区间内的周末作为分区完成边界。"""
        start = _parse_boundary(window["start"])
        end = _parse_boundary(window["end"])
        boundaries: list[date] = []
        cursor = start
        while cursor <= end:
            if cursor.weekday() == 6 or cursor == end:  # 周日或区间末日
                boundaries.append(cursor)
            cursor += timedelta(days=1)
        if not boundaries or boundaries[-1] != end:
            boundaries.append(end)
        return sorted(set(boundaries))


# 模块级单例：订阅服务通过它只读消费快照，保证沿同一条调用路径取数
userinsight_service = UserinsightService()
