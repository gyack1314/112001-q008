# 工程投入与设施交付数据

按区域和年度比较项目投入与网络设施交付的本地数仓。Python 3.11+，仅使用标准库
（文件型 SQLite）。

## 设计要点

三类来源记录保持各自的粒度，绝不拼成一张宽表：

| 来源 | 表 | 一行表示 | 金额/数量事实 |
| --- | --- | --- | --- |
| 投资明细 | `payments` | 一笔合同付款 | 唯一的金额粒度 |
| 施工台账 | `packages` | 一个工程包 | 完工状态 |
| 资产系统 | `facilities` | 一项建成设施 | 验收状态 |

报表层分别聚合金额与设施数量，因此一个工程包下的多个设施不会让付款金额膨胀。
合同与项目/工程包/区域之间的关系只来自显式的 `allocations` 记录，且每条记录
必须带业务依据（`evidence`）。没有分摊依据的合同余额作为“未分配”事实列示，
按合同和原因代码解释，不按设施数或工程包数均摊。

工程完工、资产验收、付款确认是三个独立状态：报表分别统计
`package_completion_states`、`facility_acceptance_states`、
`payment_confirmation_states`，任何一方变化都不会替代另外两个。

## 运行

    python -m compileall src
    python -m unittest discover -s tests

装载合成数据并输出报告：

    python -m src.cli --db warehouse.sqlite3 load --input data/facts.json
    python -m src.cli --db warehouse.sqlite3 report --period 2026
    python -m src.cli --db warehouse.sqlite3 report --period 2026-Q2 --format json
    python -m src.cli --db warehouse.sqlite3 issues

`--period` 支持 `all`、`2026`、`2026-Q2`、`2026-05`、`2026-05-01..2026-06-30`。

## 查询未归属原因

    python -m src.cli --db warehouse.sqlite3 explain --period 2026 --contract C-1
    python -m src.cli --db warehouse.sqlite3 explain --period 2026 --payment P-1
    python -m src.cli --db warehouse.sqlite3 explain --period 2026 --asset AS-1
    python -m src.cli --db warehouse.sqlite3 explain --period 2026 --package PK-1

原因代码示例：`no_allocation_evidence`（没有分摊证据）、
`partial_allocation_evidence`（分摊未覆盖全部付款）、
`allocation_unknown_package`（引用的工程包不存在）、
`asset_missing_package`（设施未登记工程包）、`package_missing_region` 等。

## 历史发布

发布把某报告期结果冻结为不可变快照（含当时的映射折算和三种确认状态）：

    python -m src.cli --db warehouse.sqlite3 publish --period 2026
    python -m src.cli --db warehouse.sqlite3 publications
    python -m src.cli --db warehouse.sqlite3 publications --diff PUB-xxxxxxxx
    python -m src.cli --db warehouse.sqlite3 show PUB-xxxxxxxx

补充关联后重新 `load` 只会改变当前视图；先前发布的内容由数据库触发器保护，
不可更新或删除。`--diff` 列出新增/变更的映射及当前与发布时点的差异。
`report --as-of YYYY-MM-DD` 可复算某时点当时可用的归属证据。

## 输入格式

`data/facts.json` 是记录数组，每行以 `fact_kind` 区分：

```json
{"fact_kind": "payment", "payment_id": "P-1", "contract_id": "C-1", "amount": 500000,
 "currency": "CNY", "status": "confirmed", "confirmed_at": "2026-03-10"}
{"fact_kind": "package", "package_id": "PK-1", "project_id": "PR-1", "region_id": "north",
 "status": "completed", "completed_at": "2026-04-20"}
{"fact_kind": "asset_acceptance", "asset_id": "AS-1", "package_id": "PK-1",
 "status": "accepted", "accepted_at": "2026-04-30"}
{"fact_kind": "allocation", "allocation_id": "A-1", "contract_id": "C-1",
 "package_id": "PK-1", "ratio": 0.6, "evidence": "联合采购协议 2026-01 第 4 条",
 "effective_at": "2026-02-01"}
```

* 付款、工程包、设施的状态缺省时由日期推断；显式状态优先。带状态但缺日期的
  记录仍保留，但不会进入任何报告期，并在 `issues` 中提示。
* `allocation` 必须提供 `evidence`，`amount` 与 `ratio` 二选一，且至少指向
  package/project/region 之一。比例以合同已确认付款总额为基数折算。
* 无效记录逐条拒绝并计入 `records_rejected`；其余记录照常装载。
