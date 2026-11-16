"""订阅推送业务规则：订阅正文与用户洞察报告快照绑定，状态流转收在这里。

订阅与报告更新的先后冲突，在两种做法之间权衡后采用「固定快照 + 跟随开关」：

- 做法 A（推送时实时取最新报告）：正文可能引用到一份生成中、尚未发布的报告，
  订阅正文和报告详情会出现两个版本，且可能发出已过期的洞察报告，放弃。
- 做法 B（订阅绑定具体快照编号）：订阅正文与报告详情永远引用同一份不可变快照；
  默认 ``跟随最新发布``，报告重新发布时订阅内容随快照同步更新并刷新下次生成时间，
  需要锁版的接收人可以关闭跟随、固定在某个历史快照上。本服务实现做法 B。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "subscription"
REQUIRED_FIELDS = ["订阅编号", "订阅内容", "接收人"]
STATUS_ORDER = ["草稿", "订阅评审中", "已启用", "已暂停"]
ACTION_RULES = {"提交订阅评审": "订阅评审中", "启用订阅": "已启用", "暂停订阅": "已暂停"}
NEGATIVE_ACTIONS = []


class SubscriptionService:
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
            rows = [row for row in rows if keyword in str(row.get("订阅编号", ""))]
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
        rows = store.rows(MODULE)
        entry = {"id": _next_id(rows)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        # 订阅侧新增的快照绑定字段：登记时未绑定，待报告快照发布后再关联
        entry["绑定报告id"] = values.get("绑定报告id")
        entry["绑定快照编号"] = None
        entry["跟随最新发布"] = True
        entry["快照版本"] = None
        entry["下次生成"] = None
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"订阅配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于订阅推送可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"订阅配置已{action}"

    # ---------------------------------------------------------- 快照绑定
    def bind_snapshot(
        self, entry_id: int, report_id: int, *, follow_latest: bool = True,
        snapshot_no: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """把订阅正文绑定到某份用户洞察报告快照。

        延迟导入用户洞察服务：订阅推送是另一个业务面，只沿服务层这条调用路径
        只读消费快照，避免路由层跨模块直接翻表。
        """
        from app.services.userinsight import userinsight_service

        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"订阅配置 {entry_id} 不存在或已归档"

        snapshot = None
        if snapshot_no:
            for row in userinsight_service.list_snapshots(report_id=report_id):
                if row["快照编号"] == snapshot_no:
                    snapshot = row
                    break
            if snapshot is None:
                return None, f"报告 {report_id} 下找不到快照 {snapshot_no}"
            follow_latest = False
        else:
            snapshot = userinsight_service.latest_published_snapshot(report_id)
            if snapshot is None:
                return None, f"报告 {report_id} 尚无已发布快照，无法绑定，请先在用户洞察侧发布"

        entry["绑定报告id"] = report_id
        entry["绑定快照编号"] = snapshot["快照编号"]
        entry["快照版本"] = snapshot["版本"]
        entry["跟随最新发布"] = follow_latest
        # 订阅正文与下次生成时间都来自快照，保证正文和报告详情引用同一份数据
        entry["订阅内容"] = self._render_content(snapshot)
        entry["下次生成"] = snapshot["下次生成"]
        entry["下次推送时间"] = snapshot["下次生成"]
        return entry, f"订阅已绑定快照 {snapshot['快照编号']}，正文与下次生成时间同步刷新"

    def sync_with_report(self, report_id: int) -> list[dict[str, Any]]:
        """报告快照发布后调用：跟随最新发布的订阅随快照同步更新。

        固定快照（跟随最新发布=False）的订阅不动，保留锁版正文。
        """
        from app.services.userinsight import userinsight_service

        snapshot = userinsight_service.latest_published_snapshot(report_id)
        if snapshot is None:
            return []
        synced: list[dict[str, Any]] = []
        for entry in store.rows(MODULE):
            if int(entry.get("绑定报告id") or 0) != report_id:
                continue
            if not entry.get("跟随最新发布", True):
                continue
            entry["绑定快照编号"] = snapshot["快照编号"]
            entry["快照版本"] = snapshot["版本"]
            entry["订阅内容"] = self._render_content(snapshot)
            entry["下次生成"] = snapshot["下次生成"]
            entry["下次推送时间"] = snapshot["下次生成"]
            synced.append(entry)
        return synced

    def read_bound_snapshot(self, entry_id: int) -> dict[str, Any] | None:
        """订阅发送前读取绑定的快照：发送的正文必须与报告详情是同一份快照。"""
        from app.services.userinsight import userinsight_service

        entry = store.find(MODULE, entry_id)
        if entry is None or not entry.get("绑定快照编号"):
            return None
        report_id = int(entry["绑定报告id"])
        for row in userinsight_service.list_snapshots(report_id=report_id):
            if row["快照编号"] == entry["绑定快照编号"]:
                return row
        return None

    def send_subscription(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """模拟一次订阅推送：未绑定快照或报告仍在生成中时拒绝发送，避免推过期报告。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"订阅配置 {entry_id} 不存在或已归档"
        snapshot = self.read_bound_snapshot(entry_id)
        if snapshot is None:
            return None, "订阅尚未绑定已发布快照，拒绝推送以免发出过期洞察报告"
        if snapshot.get("状态") == "已生成" and not entry.get("绑定快照编号"):
            return None, "快照未发布，不能推送"
        entry["上次推送快照"] = snapshot["快照编号"]
        entry["上次推送时间"] = snapshot["生成时间"]
        return entry, f"已按快照 {snapshot['快照编号']} 推送，正文与报告详情同源"

    @staticmethod
    def _render_content(snapshot: dict[str, Any]) -> str:
        return (
            f"【{snapshot['分析主题']}】快照 {snapshot['快照编号']}｜"
            f"数据区间 {snapshot['数据区间起']} ~ {snapshot['数据区间止']}｜"
            f"目标人群 {snapshot['目标人群']}｜{snapshot['核心发现']}"
        )


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


subscription_service = SubscriptionService()
