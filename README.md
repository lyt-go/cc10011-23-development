# 实时指标计算与监控大盘平台

面向实时指标计算引擎、多维度下钻分析、告警阈值管理、指标异常检测、数据服务开放与可视化看板配置的数据分析与应用工程平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实时指标 | `realtimemetric` | 指标定义 | 指标编号、指标名称、计算窗口秒 |
| 监控大盘 | `dashboard` | 大盘配置 | 大盘编号、大盘名称、包含指标组 |
| 多维下钻 | `drilldown` | 下钻路径 | 路径编号、起始维度、下钻层级序列 |
| 阈值管理 | `threshold` | 阈值规则 | 规则编号、关联指标、阈值下限 |
| 异常检测 | `anomalydetect` | 检测模型 | 模型编号、监控指标、检测算法 |
| 数据服务 | `dataservice` | 服务接口 | 服务编号、服务名称、接口路径 |
| 告警历史 | `alarmhistory` | 告警记录 | 记录编号、告警规则、触发时间 |
| 同比环比 | `comparison` | 对比配置 | 配置编号、目标指标、对比类型 |
| 聚合配置 | `aggregate` | 聚合规则 | 规则编号、源数据粒度、目标粒度 |
| 筛选预设 | `filterpreset` | 预设模板 | 预设编号、预设名称、维度条件组 |
| 用户洞察 | `userinsight` | 洞察报告 | 报告编号、分析主题、目标人群 |
| 事件分析 | `eventanalysis` | 分析模型 | 模型编号、事件名称、分组维度 |
| 分群画像 | `segmentprofile` | 画像定义 | 画像编号、分群名称、筛选规则 |
| 趋势预测 | `trendforecast` | 预测方案 | 方案编号、预测指标、算法模型 |
| 可视化模板 | `viztemplate` | 图表模板 | 模板编号、图表类型、数据绑定 |
| 订阅推送 | `subscription` | 订阅配置 | 订阅编号、订阅内容、接收人 |
| 维度映射 | `dimensionmap` | 映射规则 | 规则编号、原始维度名、标准维度名 |
| 数据质量分 | `qualityscore` | 评分规则 | 规则编号、数据集名称、完整性权重 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 用户洞察：生成批次与差异快照

用户洞察报告的每次取数都落到可追溯的批次与不可变快照上，订阅推送只消费已发布快照：

- 生成批次（`userinsight_batch`）：保存分析主题、目标人群、关键指标、数据区间、
  分区边界与生成时间。同一分析主题存在「生成中」批次时重复提交直接复用，不新增任务。
- 差异快照（`userinsight_snapshot`）：分区边界全部就绪后按同一区间计算关键指标变化
  与核心发现，快照只读、带版本号；生成中的报告不能发布，发布后快照只供订阅读取，
  历史快照（含已被替代的旧版本）始终可读。
- 并发分区：分区完成回调在服务层用锁串行化，区间外边界忽略、已登记边界幂等；
  快照生成后到达的**新**迟到分区通过 `/batches/{id}/late-partitions` 补入计划并触发重算。
- 历史回填（`userinsight_backfill`）：回填批次号幂等（重复提交只写一次），
  按分区边界推进，中断后从上一完整分区继续，全部完成后驱动生成批次与快照。
- 订阅绑定：订阅正文绑定具体快照编号与下次生成时间。默认「跟随最新发布」，
  报告重新发布时订阅随快照同步更新；也可固定到某个历史快照锁版。未绑定已发布快照的
  订阅拒绝推送，避免发出过期报告。

### 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/userinsight/reports/{id}/generation` | 提交生成（同主题生成中批次幂等复用） |
| GET | `/api/userinsight/batches` | 生成批次列表，可按报告/状态过滤 |
| POST | `/api/userinsight/batches/{id}/partitions` | 分区完成回调（边界判定、产出快照） |
| POST | `/api/userinsight/batches/{id}/late-partitions` | 迟到分区回调（新边界补入并触发重算） |
| GET | `/api/userinsight/snapshots` | 差异快照列表（历史版本保持可读） |
| GET | `/api/userinsight/reports/{id}/diff-overview` | 报告页概览：人群、区间、指标变化 |
| POST | `/api/userinsight/reports/{id}/backfills` | 登记回填批次（批次号幂等） |
| POST | `/api/userinsight/backfills/{id}/partitions` | 回填分区回调（断点续传） |
| POST | `/api/subscription/{id}/bind-snapshot` | 订阅绑定报告快照（支持锁版） |
| GET | `/api/subscription/bound-snapshots` | 订阅业务面同步消费的快照视图 |
| POST | `/api/subscription/{id}/send` | 按绑定快照推送，未绑定则拒绝 |

端到端验证脚本在 `scripts/verify_userinsight.py`（依赖 `httpx` 供 TestClient 使用，
仅测试需要，不进运行时依赖）：

```bash
cd backend && .venv/bin/pip install httpx
.venv/bin/python -I ../scripts/verify_userinsight.py
```

领域：数据工程
子方向：数据分析与数据应用
