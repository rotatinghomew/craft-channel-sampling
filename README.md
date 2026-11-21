# 共用文创柜的运营坪效

经营演练资料均为合成数据，不涉及真实参赛者、顾客或合作方。 `data/drill.json` 为 UTF-8 JSON；顶层键为来源表名，值为行数组。日期按当地工作日历，编号是代号而非名称。 金额使用整数分；`null` 表示未取得该事实，不等于零。

面积以平方厘米计、时段首含末不含。柜格面积扣过道与安全边界见 `operation` 和 `next_layout`。`holds` 给出实际占用或已经收到的预告，共用且未签认的 `shares.confirmed_percent` 不提供给定分配。`display.visitors` 按商品观测，不能相加作为独立来客数。跨期馆内标记相同不保证物理范围相同，`bridges` 的未签认候选不是明确身份。`candidate.fee` 是该方案当期报价；`promise.min_cm2_hour` 是合同按面积小时保留的下限。

## 资料预检

使用 Python 3.11 或更高版本，无第三方依赖。 在项目根目录运行 `python3 -m unittest discover -s tests -v` 核对演练资料的表结构、事实关联及业务场景所需的原始记录。
