# 对账工作台

从两套流水（银行 / 账务）中找出对应交易的完整演示：一对一、拆单、合单、人工否决、
迟到流水补录、策略变更与人工确认的冲突处理、余额差异的逐笔归因，以及匹配中断后的恢复。
**所有数据都是虚构的，系统不执行任何真实转账。**

## 快速开始

```bash
python3 scripts/scenario.py data/scenario.json   # 生成确定性虚构数据
python3 scripts/dev.py --reset --seed            # 启动三服务 + 网页并载入数据
# 打开 http://127.0.0.1:8100
```

只想启动、稍后再灌数据：

```bash
python3 scripts/dev.py --reset
python3 -m recon.seed --web http://127.0.0.1:8100
```

运行测试（不需要浏览器、不需要第三方依赖）：

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

## 结构

三个后端进程 + 一个静态网页进程，各自独立启动、独立重启：

| 服务 | 默认端口 | 职责 | 数据 |
| --- | --- | --- | --- |
| 接入 `recon.ingest` | 8101 | 接收批次、去重、迟到标记、流水查询 | `data/ingest.db` |
| 匹配 `recon.matcher` | 8102 | 策略、候选生成、匹配计划、人工确认/否决/释放 | `data/matcher.db` |
| 余额 `recon.balance` | 8103 | 只读消费上述两端，解释未达账项，快照/漂移 | `data/balance.db` |
| 网页 `web/server.py` | 8100 | 静态页面 + 同源反向代理 | — |

依赖方向是单向的：余额分析 → 匹配 → 接入。接入服务不知道匹配的存在，因此任何一端
重启或升级都不会让上游持有过期副本。余额端在匹配端宕机时返回 `matcher_down`，不会用
旧数据伪装成“已对账”。

端口、数据库路径、上游地址都可以用环境变量覆盖（`INGEST_PORT`、`MATCHER_URL` 等，
见 `recon/*.py` 的 `main()`）。

## 匹配语义

### 候选

`recon/matching.py` 是纯函数库，输入两套流水和策略，输出可解释的候选组：

* `one_to_one` 一对一
* `split` 1 笔银行 → N 笔账务（拆单）
* `merge` N 笔银行 → 1 笔账务（合单）

每个候选带分项得分（金额 / 日期 / 引用 / 账号）和人可读的依据，例如
“1 笔银行 4800.00 = 2 笔账务 3000.00+1800.00（差 +0.00）”。同分候选按 key 排序，
同样输入必然得到同样计划。

### 硬约束（不可绕过）

1. **一笔流水只能出现在一个有效匹配里。** `validate_plan` 在每次落库前检查，冲突直接
   500 拒绝，绝不写入半成品。
2. **人工确认不会被静默改写。** 自动重算只删除 `origin='auto'` 的记录；人工记录只能由
   人显式确认、否决或释放。
3. **人工否决必须带原因，且在后续自动重配中被抑制。** 相同组合不会被再次提出（`suppressed_rejected`）。
4. **否决与释放是两件事。** `rejected` 保留并抑制该组合；`released`（显式释放一个人工确认）
   保留审计记录，但把流水放回自动池——重新匹配可能再次以**自动提案**身份提出它，绝不会
   静默恢复成人工确认。

### 人工确认与策略变更

人工可以在自动规则之外确认一组匹配（例如相差 2 元、低于自动阈值），但有两层边界：

* **硬上限**：方向必须一致、单侧 ≤10 笔、金额差 ≤50 元、日期差 ≤31 天。越过任何一条
  都无法确认（`evidence_insufficient`）。
* **警告**：超出当前策略的容差/窗口/分数，需要带 `acknowledge_warnings` 显式承担，
  警告会写进匹配记录。

策略变更后重新运行匹配时：

* 若策略与确认时相同：确认保持有效，只显示警告。
* 若策略已变且该组在新策略下不再成立：状态变为 `stale`——确认记录保留（不删除、
  不改成别的流水），但**不再参与对账**，其占用的流水也被保留，不会被自动匹配拿去
  配给别人。只有人可以“重新确认”或“释放”。
* 即使把策略改回去，`stale` 也不会自动复活（`already_stale`）；必须人工重新确认。

这条规则保证“失效关系不会继续影响未达账项”，同时“人工确认不能被静默改掉”。

### 全局重配 vs 局部释放重配

| | 全局 `mode=global` | 局部 `mode=local` |
| --- | --- | --- |
| 触发 | 策略调整后 | 释放某个人工/自动匹配后 |
| 删除 | 全部 `origin='auto'` | 只删除与被释放流水相交的自动匹配 |
| 求解 | 所有流水重新竞争 | 范围外的自动匹配视为锁定，只有受影响流水重新竞争 |
| 成本 | 候选数 = 全量，`candidates_generated` 大 | 候选数 = 受影响范围，`elapsed_ms` 小 |
| 人工记录 | 只做重新校验，不删除 | 不触碰 |

两个模式下人工确认都不会被自动删除；局部重配也不会顺手改动无关的自动匹配。

## 余额差异归因

余额端不“抹平”差额，而是把它拆到具体来源：

```
账务合计 − 银行合计 = Σ 匹配内接受差额 + 仅账务流水 − 仅银行流水
```

* `identity_check_cents` 必须为 0；非 0 说明有匹配引用了已不存在的流水（`missing_row_refs`）。
* 每笔未达流水都有一个 `bucket`：`late`（迟到）、`amount`（时序接近但金额不符，带最接近
  对手方提示）、`standard`（没有合理对手方）。
* `periods` 按月份给出对差额的**有符号贡献**，各期间之和等于原始差额
  （`period_check_cents == 0`），银行侧未达按冲减计。

网页“总览 / 余额分析”两页直接展示这条恒等式；任何一刻数字对不上都会显式标红。

## 迟到流水

接入服务以“该来源已有的最大业务日期”为水位，批次里业务日期落后水位超过
`LATE_GRACE_DAYS`（默认 2 天）的流水标记 `late=1`，但**照常入库、照常参与匹配**。同一
批次内部互不判定为迟到。重复的 `(source, external_ref)` 会被拒绝并回报原因。

网页“恢复演练”页有“补入迟到流水”按钮（数据来自 `web/static/late.json`），补入后重新
匹配即可看到迟到项被吸收。

## 中断与恢复

* `POST /api/simulate/interrupt`：在写入匹配的事务中间抛出异常，事务回滚，旧匹配原样保留，
  事件记录在 `simulation_events`（`outcome=rolled_back`）。
* `POST /api/simulate/outage`：N 秒内匹配全部接口（含 `/health`）返回 503；余额端据此降级，
  快照请求返回 409 而不是写入不完整快照。
* `POST /api/run mode=preview`：只算不写，用来看策略调整会让哪些人工确认失效。

## HTTP API 摘要

接入：`GET /api/overview`、`GET /api/rows`、`GET /api/rows/{id}`、`POST /api/batches`、
`GET /api/batches`、`GET /api/summary`、`POST /api/rows/lookup`（按 ID 批量取流水）。

匹配：`GET|PUT /api/strategy`、`GET /api/candidates?row_id=`、`POST /api/run`
（`global|local|preview`）、`GET /api/matches`、`GET /api/matches/{id}`、
`POST /api/matches/{id}/{confirm|reject|release|reinstate}`、`POST /api/manual`、
`POST /api/candidates/decide`、`POST /api/simulate/{interrupt,outage}`、
`GET /api/simulate/events`、`GET /api/effective`。候选确认走
`POST /api/candidates/decide`：若该组合已存在自动提案，会直接升级该记录为人工确认，
而不是新建一条互相冲突的记录。

余额：`GET /api/overview`、`GET /api/explain`、`POST /api/snapshot`、`GET /api/drift`。

错误统一为 `{"error": {"code", "message", "details?"}}`，`code` 是稳定的机器可读值。

## 保证与限制

* 只用 Python 标准库；无外部服务、无真实转账、无真实数据。
* 匹配求解是确定性的贪心 + 有界局部改进，不是全局最优；结果对同一输入可重放。
* 金额以整数分存储与比较，不使用浮点。
* 三个服务各自用 SQLite 单文件（WAL 模式），定位是演示与规则验证，不是高并发生产部署。
