"""端到端验证：报告生成批次、差异快照、回填断点、订阅绑定与并发分区。

运行：PYTHONPATH=/workspace/backend /workspace/backend/.venv/bin/python -I \
      /workspace/scripts/verify_userinsight.py
"""
from __future__ import annotations

import sys
import threading

sys.path.insert(0, "/workspace/backend")

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
UI = "/api/userinsight"
SUB = "/api/subscription"
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def post(path: str, values: dict | None = None):
    return client.post(path, json={"values": values or {}})


def action(module: str, entry_id: int, act: str):
    return post(f"{module}/{entry_id}/actions", {"action": act})


# 1. 登记报告并重复提交生成：同一分析主题只能有一个生成中批次
r = post(UI, {"报告编号": "USER-NEW-1", "分析主题": "流失用户召回分析",
              "目标人群": "近30天未登录用户", "关键指标": "活跃用户数、付费转化率",
              "数据区间": "2026-09-07 ~ 2026-09-20"})
report_id = r.json()["entry"]["id"]
check("登记报告", r.json()["ok"] is True)

r1 = post(f"{UI}/reports/{report_id}/generation", {})
r2 = post(f"{UI}/reports/{report_id}/generation", {})
batches = client.get(f"{UI}/batches?report_id={report_id}").json()["items"]
check("重复提交不新增生成任务", len(batches) == 1, f"实际批次数 {len(batches)}")
check("提交后报告为生成中", r1.json()["entry"]["status"] == "生成中")

# 2. 生成中的报告不能发布
r = action(UI, report_id, "发布报告")
check("生成中禁止发布", r.json()["ok"] is False and "生成中" in r.json()["message"])

batch_id = batches[0]["id"]
boundaries = batches[0]["分区边界"]

# 3. 分区边界：区间外忽略、重复上报幂等
r = post(f"{UI}/batches/{batch_id}/partitions", {"boundary": "2026-01-01"})
check("区间外边界被忽略", r.json()["ok"] is True and "忽略" in r.json()["message"])
r = post(f"{UI}/batches/{batch_id}/partitions", {"boundary": boundaries[0]})
r_dup = post(f"{UI}/batches/{batch_id}/partitions", {"boundary": boundaries[0]})
check("重复边界幂等", "重复" in r_dup.json()["message"])
snaps = client.get(f"{UI}/snapshots?report_id={report_id}").json()["items"]
check("分区未齐不产出快照", len(snaps) == 0)

# 4. 两个数据分区并发完成：锁内按边界依次处理，最后一个边界产出快照
errors: list[str] = []


def complete(edge: str) -> None:
    try:
        post(f"{UI}/batches/{batch_id}/partitions", {"boundary": edge})
    except Exception as exc:  # pragma: no cover - 仅用于并发验证
        errors.append(str(exc))


threads = [threading.Thread(target=complete, args=(edge,)) for edge in boundaries]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("并发分区无异常", not errors, str(errors))
snaps = client.get(f"{UI}/snapshots?report_id={report_id}").json()["items"]
check("分区齐后生成快照 v1", len(snaps) == 1 and snaps[0]["版本"] == 1)
s1 = snaps[0]
check("快照保存目标人群与区间",
      s1["目标人群"] == "近30天未登录用户" and s1["数据区间起"] == "2026-09-07"
      and s1["数据区间止"] == "2026-09-20")
check("快照含指标变化与核心发现",
      all("指标" in m and "当前值" in m for m in s1["指标明细"]) and bool(s1["核心发现"]))
check("快照可追溯到批次", s1["批次编号"] == batches[0]["批次编号"])

# 5. 发布报告：快照只读，并记录报告绑定
r = action(UI, report_id, "发布报告")
check("快照就绪后允许发布", r.json()["ok"] is True, r.json()["message"])
published = client.get(f"{UI}/snapshots?report_id={report_id}").json()["items"][0]
check("发布后快照状态只读", published["状态"] == "已发布")
entry = client.get(f"{UI}/{report_id}").json()
check("报告详情引用同一快照", entry["当前快照编号"] == s1["快照编号"])

# 6. 订阅绑定快照：未发布报告拒绝；已发布报告绑定成功并带下次生成时间
sub_a = post(SUB, {"订阅编号": "SUBS-A", "订阅内容": "待绑定", "接收人": "运营一组"}).json()["entry"]
sub_b = post(SUB, {"订阅编号": "SUBS-B", "订阅内容": "待绑定", "接收人": "运营二组"}).json()["entry"]
r = post(f"{SUB}/{sub_b['id']}/bind-snapshot", {"report_id": 1})  # 报告1无快照
check("无发布快照拒绝绑定", r.json()["ok"] is False)
r = post(f"{SUB}/{sub_a['id']}/bind-snapshot", {"report_id": report_id})
check("订阅绑定发布快照", r.json()["ok"] is True
      and r.json()["entry"]["绑定快照编号"] == s1["快照编号"]
      and r.json()["entry"]["下次生成"] == s1["下次生成"])
# B 订阅固定锁版到 v1
r = post(f"{SUB}/{sub_b['id']}/bind-snapshot",
         {"report_id": report_id, "snapshot_no": s1["快照编号"]})
check("订阅可固定历史快照", r.json()["ok"] is True
      and r.json()["entry"]["跟随最新发布"] is False)

# 7. 未绑定快照的订阅拒绝推送，绑定后按同一快照推送
sub_c = post(SUB, {"订阅编号": "SUBS-C", "订阅内容": "未绑定", "接收人": "运营三组"}).json()["entry"]
r = post(f"{SUB}/{sub_c['id']}/send")
check("未绑定快照拒绝推送", r.json()["ok"] is False and "过期" in r.json()["message"])
r = post(f"{SUB}/{sub_a['id']}/send")
check("绑定后推送且同源", r.json()["ok"] is True
      and r.json()["entry"]["上次推送快照"] == s1["快照编号"])

# 8. 报告重新生成 -> 快照 v2；再发布后跟随订阅同步、固定订阅锁版
r = post(f"{UI}/reports/{report_id}/generation", {})
check("已发布报告可再次提交生成", r.json()["ok"] is True)
b2 = client.get(f"{UI}/batches?report_id={report_id}&status=生成中").json()["items"][0]
for edge in b2["分区边界"]:
    post(f"{UI}/batches/{b2['id']}/partitions", {"boundary": edge})
snaps = client.get(f"{UI}/snapshots?report_id={report_id}").json()["items"]
check("重新生成产出 v2 且 v1 保留", len(snaps) == 2 and snaps[0]["版本"] == 1 and snaps[1]["版本"] == 2)
check("v2 指标变化以 v1 为基线",
      all(m["基线值"] is not None for m in snaps[1]["指标明细"]))
r = action(UI, report_id, "发布报告")
check("重新发布成功并同步订阅", r.json()["ok"] is True and "同步 1 条订阅" in r.json()["message"])
sub_a_row = client.get(f"{SUB}/{sub_a['id']}").json()
sub_b_row = client.get(f"{SUB}/{sub_b['id']}").json()
check("跟随订阅同步到 v2", sub_a_row["绑定快照编号"] == snaps[1]["快照编号"])
check("固定订阅保持 v1 锁版", sub_b_row["绑定快照编号"] == s1["快照编号"])

# 9. 快照已生成后迟到分区到达 -> 新区间边界补入计划并判定重算，新版本快照
r = post(f"{UI}/reports/{report_id}/generation", {})
b3 = client.get(f"{UI}/batches?report_id={report_id}&status=生成中").json()["items"][0]
for edge in b3["分区边界"]:
    post(f"{UI}/batches/{b3['id']}/partitions", {"boundary": edge})
# 原计划之外的新边界（区间内非周末日期），模拟数据分区延迟就绪
late = "2026-09-16"
assert late not in b3["分区边界"], "测试前提：迟到边界不在原分区计划内"
r = post(f"{UI}/batches/{b3['id']}/late-partitions", {"boundary": late})
check("迟到分区触发重算", r.json()["ok"] is True and "重算" in r.json()["message"])
b3_row = client.get(f"{UI}/batches?report_id={report_id}").json()["items"][-1]
check("批次记录重算次数", b3_row["状态"] == "已重算" and b3_row["重算次数"] == 1)
# 同一迟到边界重复回调必须幂等，不能再次重算
r_again = post(f"{UI}/batches/{b3['id']}/late-partitions", {"boundary": late})
b3_again = client.get(f"{UI}/batches?report_id={report_id}").json()["items"][-1]
check("迟到边界重复回调幂等", b3_again["重算次数"] == 1 and "重复" in r_again.json()["message"])

# 10. 历史回填：重复批次只写一次，中断续跑，完成后驱动生成批次与快照
r = post(f"{UI}/reports/{report_id}/backfills",
         {"回填批次号": "BF-TEST-001", "数据区间": "2026-07-01 ~ 2026-07-14"})
r2 = post(f"{UI}/reports/{report_id}/backfills",
          {"回填批次号": "BF-TEST-001", "数据区间": "2026-07-01 ~ 2026-07-14"})
bfs = client.get(f"{UI}/backfills?report_id={report_id}").json()["items"]
check("回填批次幂等写入", len(bfs) == 1 and "已存在" in r2.json()["message"])
bf = bfs[0]
bf_edges = bf["分区边界"]
post(f"{UI}/backfills/{bf['id']}/partitions", {"boundary": bf_edges[0]})
mid = client.get(f"{UI}/backfills?report_id={report_id}").json()["items"][0]
check("回填中断后保留进度", mid["状态"] == "进行中" and len(mid["已完成边界"]) == 1)
for edge in bf_edges[1:]:
    post(f"{UI}/backfills/{bf['id']}/partitions", {"boundary": edge})
done = client.get(f"{UI}/backfills?report_id={report_id}").json()["items"][0]
check("回填完成并关联生成批次", done["状态"] == "已完成" and bool(done["关联生成批次"]))
new_snaps = client.get(f"{UI}/snapshots?report_id={report_id}").json()["items"]
check("回填驱动新快照", len(new_snaps) >= 4)

# 11. 历史快照始终可读（含已被新版本替代的 v1）
r = client.get(f"{UI}/snapshots/1")  # seed 里的历史快照
check("种子历史快照可读", r.status_code == 200 and r.json()["快照编号"] == "SNAP-3-001")
r = client.get(f"{UI}/snapshots/{s1['id']}")
check("旧版本快照可读", r.status_code == 200)

# 12. 报告页概览视图：目标人群、数据区间、关键指标变化
r = client.get(f"{UI}/reports/{report_id}/diff-overview")
ov = r.json()
check("差异概览结构完整",
      ov["目标人群"] == "近30天未登录用户" and "数据区间" in ov
      and len(ov["差异概览"]) == 2
      and all("方向" in item for item in ov["差异概览"]))

# 13. 订阅业务面快照消费视图
r = client.get(f"{SUB}/bound-snapshots")
bound = [item for item in r.json()["items"] if item["绑定快照编号"]]
check("订阅面同步消费快照", len(bound) >= 2 and all(item["快照状态"] for item in bound))

print("\n" + ("全部通过 ✅" if not failures else f"{len(failures)} 项失败：{failures} ❌"))
sys.exit(1 if failures else 0)
