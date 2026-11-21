# 共用文创柜的运营坪效

经营演练资料均为合成数据，不涉及真实参赛者、顾客或合作方。 `data/drill.json` 为 UTF-8 JSON；顶层键为来源表名，值为行数组。日期按当地工作日历，编号是代号而非名称。 金额使用整数分；`null` 表示未取得该事实，不等于零。

面积以平方厘米计、时段首含末不含。柜格面积扣过道与安全边界见 `operation` 和 `next_layout`。`holds` 给出实际占用或已经收到的预告，共用且未签认的 `shares.confirmed_percent` 不提供给定分配。`display.visitors` 按商品观测，不能相加作为独立来客数。跨期馆内标记相同不保证物理范围相同，`bridges` 的未签认候选不是明确身份。`candidate.fee` 是该方案当期报价；`promise.min_cm2_hour` 是合同按面积小时保留的下限。

## 资料预检

使用 Python 3.11 或更高版本，无第三方依赖。 在项目根目录运行 `python3 -m unittest discover -s tests -v` 核对演练资料的表结构、事实关联及业务场景所需的原始记录。

## 分析工具

`cabinet_analytics` 是无第三方依赖的分析包，全部使用整数分、平方厘米和半开整数时段；比率在内部用 `Fraction` 计算，只在输出时转成浮点。

### 命令行

```bash
python3 -m cabinet_analytics report        # 月末汇总，所有口径一次产出
python3 -m cabinet_analytics plan          # 三个布局/费用方案试算
python3 -m cabinet_analytics plan --plan P1
python3 -m cabinet_analytics notices       # 签字后新增讲解/闭馆通知的影响
python3 -m cabinet_analytics sensitivity   # 未归属收入处理方式对排序的影响
python3 -m cabinet_analytics trace-take P3-S1-0-2
python3 -m cabinet_analytics trace-promise P0 A2
python3 -m cabinet_analytics trace-position 甲 S1 K1
```

`--data PATH` 可指向另一份同结构资料。数据问题写到 stderr，结果写到 stdout。

### 口径（与资料里的每条规则对应）

| 问题 | 做法 | 不这么做会怎样 |
| --- | --- | --- |
| 柜格面积 | `gross_cm2 - exclusion_cm2` | 把过道算成可售面积 |
| 机会时长 | `open = held + display + unallocated`，三段互不重叠 | 把讲解时段算成陈列机会，或把同一小时算两遍 |
| 共用柜格 | 签认份额直接相乘；未签认部分按 `[0, 剩余份额]` 报告区间 | 自行把剩余面积平均分给多家 |
| 贡献归属 | 结账带 `take` 时归到**取货**的柜格、版型和取货日；跨柜结账、延后结账都不搬收入 | 把收入挪到收银台或付款日 |
| 无法归属的收入 | 单列 `unattributed`，并证明 `attributed + unattributed = cashier_total` | 把没有取货凭证的钱摊进各柜，虚增坪效 |
| 已取货未结账 | 单列 `identified_unsettled`，按同 SKU 观测均价给估值区间 | 当成零收入，或当成已归属收入 |
| 顾客数 | 只在同一商品行上做“取货数/观测顾客数”；不跨柜相加 | 把商品观测当独立来客数 |
| 坪效 | 已归属收入 ÷ **本工作室签认面积小时**；未签认时给出保守下界（用全部剩余份额）与上界（只用签认份额） | 用整柜面积当分母，让共用者互相稀释 |
| 版型变化 | 只使用 `supported=true` 且映射唯一的 `bridges`；S2 因有两个候选而排除 | 把旧 S2 强行并入 S2 或 S3 |
| 变化拆解 | 五个可乘因子（机会、客流、转化、结账捕获、客单）做 Shapley 分解，分项之和恰等于收入变化 | 把“补录改善”当成转化提升 |

### 保底位与方案试算

* 方案交付的面积小时用 `cm2 × 该柜当日扣除讲解/闭馆后的可售小时`；不能使用名义营业时间。
* 同一柜格分给多家且其中一家在该时段有签认保底时，记为 `priority_slot_promised_to_multiple_studios`；没有签认份额的共用记为 `sharing_needs_signed_shares`。两者都不把面积相加当改善。
* 分配面积超过柜格有效面积时记为超配，**该柜上的各工作室收益排序不参与相加**。
* 把工作室挪到别的柜格只算 `reassignment_required`：方案本身不解除原保底，必须有明确改签。
* 保守经营收益 = 真正满足最低面积小时且无冲突的已签位费用 + 可行的新位费用；其余为 `at_risk_fee_cents`。销售预测单列，**从不与费用相加**。
* 新增讲解/闭馆通知不降低已承诺的最低面积小时；`notices` 会列出必须协商补足的工作室、缺口和可调剩余时段，剩余时段已扣除所有占用。位置只在明确改签后才转给他人。

### 月末回溯

`report` 输出里每个坪效、方案缺口都带可查编号。`trace-take` 能落到取货记录、营业记录、版型、份额、占用和结账；`trace-promise` 能落到合同下限、下一版型、占用通知和实际分配；`trace-position` 汇总到该工作室/柜格的全部取货与未结账取货。下季度用同一入口（`report` / `plan`）即可。
