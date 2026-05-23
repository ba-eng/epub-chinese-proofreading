---
name: epub-chinese-proofreading
description: 中文 EPUB 出版校对。统一专有名词/人名，消除翻译腔/网文腔，输出校对后 EPUB。触发词：「校对 EPUB」「第N轮校对」。
when_to_use: 用户需要进行 EPUB 中文出版校对时触发。
allowed-tools: [Bash, Read, Write, Edit, Glob]
model: sonnet
user-invocable: true
---

# EPUB 中文出版校对

Python 自动完成机械准备；Claude 随后按 batch 深度校对。用户只需提供 EPUB 路径，通常只在两轮校对完成后决定是否进入第 3 轮文学润色。

## 步骤 1：运行 pipeline

支持 UTF-8、GBK、Big5 编码的 XHTML 正文解码与回写。

```bash
python "${CLAUDE_SKILL_DIR}/scripts/proofread.py" pipeline "EPUB文件路径"
```

可选参数：`--profile fantasy|romance|general|minimal`（黑名单配置）、`--glossary glossary.json`（外部术语表）、`--config config.json`（本次运行的临时覆盖配置）、`--work-dir 自定义工作目录`。默认每卷 100K 字（校对质量最优）；1M+ 上下文模型可用 `--max-chars 150000` 减少分卷（质量略降）；小模型用 `--max-chars 50000`。

`pipeline` 自动完成：解包 → 提取正文 → 术语/黑名单机械预处理 → 导出全文（自动按 100K 字分卷 + 相邻卷 2000 字上下文重叠）→ 生成 TASK.md → **自动应用机械修正**（分号 `；` → `，` + 黑名单词替换 + 英文段落删除）。

## 步骤 2：逐 batch 深度校对

`pipeline` 完成时自动输出 `TASK.md` 路径。**立即读取 TASK.md 并按指示逐 batch 操作，无需等待用户确认。**

### 强制执行规则

**以下规则不可跳过、不可缩短、不可"快速扫描"：**

1. **每个 batch 必须完整、逐段深入阅读**，覆盖该 batch 的全部内容。无论 batch 大小，不可只读开头或抽样。
2. **认真审读每一段文本**，不要只扫描 `[? ...]` 标记。许多术语变体和翻译腔不会自动标记，需要人工发现。
3. **禁止跳过 batch**。全书所有 batch 都必须逐个处理，不得以"术语已经够多"为由跳过后面的 batch。
4. **每个 batch 处理完必须立即 apply-corrections**。后面的 batch 可能发现前面遗漏的术语变体；apply 时 reprocess 会自动把这些新术语传播到全量章节（包括已校对过的章节）。

### 两轮策略

**第 1 轮 — 术语发现 + 黑名单：**
- 逐 batch 深度阅读，重点搜集人名/地名/神名的同指异译
- **跨 batch 人名追踪**：同一个角色名可能在多个 batch 中以不同译名出现。注意**音近字**（如"约书亚/耶苏亚"、"爱卢亚/艾露亚"、"海辛瑟/海辛特"——同一外文名 Hyacinthe 的两种音译）、**形近字**、**同音异字**。即使两个译名看起来差异较大，只要它们指向同一外文名或同一角色，就必须统一。**关键启发式**：首字相同 + 长度相近 + 出现于相似角色语境中 → 几乎必是同一人名变体。
- **格言/重复句式统一**：全书中反复出现的格言、座右铭、仪式用语（如"爱，随心所欲"）必须在所有出现处**逐字一致**。处理每个 batch 时，注意是否有前文出现过的固定短语在本 batch 以不同措辞出现——跨 batch 对照，而非仅看当前 batch。
- 每发现一组变体，立即写入 `glossary_additions`。**变体的 term 写异译名（中文字符串），translation 写统一的译名**。这样 reprocess 会自动把所有异译名替换为统一译名。
- 处理所有 `[? 需替换网文词: xxx]` 标记，必须替换为中性表达
- 每 batch 完成后立即 `apply-corrections`
- **第 1 轮全部 batch 处理完后，必须做以下三项全书审计：**

  **审计 A — 遗漏变体扫描**：快速回看各 batch 的 glossary_additions，检查是否有遗漏的变体（同一外文名可能还有一个你没发现的译法）。

  **审计 B — Glossary 目标去重**：通读 glossary.json 的全部条目，检查是否有**不同 target 指向同一概念**。重点关注：
  - 结构变体：`X之Y` vs `X的Y`（如 `库希尔之镖` vs `库希尔的飞镖`）、带"的"与不带"的"
  - 语素共享但词序/结构不同：`痛苦之愉者` vs `痛苦者`、`天使之地` vs `安吉之地`
  - 一旦发现，将所有变体统一到同一个 target，并确保已存在的 term→target 映射也更新

  **审计 C — 概念等价扫描**：检查是否有**字形/读音完全不同但指向同一原文概念**的译名对。此检查不依赖音近/形近，而是依赖对小说世界观的理解：
  - 同一虚构民族有两种完全不同的中文称呼（如 Cruithne 被同时译为 `克鲁伊特尼` 和 `皮克特`）
  - 同一机构/院名被不同译者用不同植物/意象翻译（如 Mandrake 被译为 `曼德拉草院` 和 `刺槐院`）
  - 结合上下文判断：两个词是否出现在相似语境、修饰同一类对象、由同一类角色说出
  - 发现后写入 glossary_additions，统一到同一译名

**第 2 轮 — 精修：**
- `[? 英文段落]`（有相邻中文译文）已由 pipeline 自动删除，**跳过**
- `[? 英文段落·待翻译]`（无相邻中文译文）→ 翻译为中文
- 修正 AI 套话、翻译腔、风格突变。逐段对照以下翻译腔模式（不确定的保留，不要强行改正常中文）：
  - 「被……所……」→ 改主动语态
  - 「是……的」→ 去掉冗余
  - 「一个……的」→ 合并形容词
  - 「……着……着」→ 简化表达
  - 「开始……起来」→ 换具体动词
  - 过于正式的代词（「该」「其」）→ 口语化
  - 被动语态过滥（「被人们」「被大家」）→ 主动或删除施动者
  - **翻译完整性检查**：对话/动作引导句中，检查中文是否遗漏了原文的状态修饰——「他说，笑着」不应只译「他说」（漏了「笑着」），「她叹了口气回答」不应只译「她回答」。常见易漏词：笑着说、叹了口气、低声、冷冷地、轻声、喃喃、眯起眼、点了点头等。对照上下文判断——如果后文没有通过其他方式传达该情绪/动作，可能是 AI 翻译时省略了引导句中的修饰。
- 同样逐 batch 深度阅读，不可跳过

### 校对输出格式

你可以将 glossary_additions 和 corrections 分两个 json 代码块输出，脚本会自动合并（不会丢失任何一个块）。JSON 尾部逗号也会被自动修复。

```json
{"glossary_additions": [{"term": "异译名", "translation": "统一译名"}]}
```

```json
{"corrections": [{"chapter": 0, "segment_id": 3, "corrected": "修正后的文本"}]}
```

### 坐标和规则

- `segment_id` 用文本中的 `[cN.sM]` 或 `[cN.sM.K]` 坐标。`[cN.sM]` 写整数 `M`；`[cN.sM.K]` 必须写完整的 `M.K`（如 `3.0`），不要省略子段号。
- `[? 需替换网文词: xxx]` 标记的词**必须替换**为中性表达
- `[? 英文段落]` — pipeline 已自动删除，无需处理
- `[? 英文段落·待翻译]` — 英文段无中文配对→**翻译为中文**
- **全中文化**：所有外文单词（包括虚构世界专有术语如 `anguissette`、`vrajna` 等）都必须翻译为中文。发现后写入 glossary_additions，reprocess 自动传播
- **glossary_additions 的 term 格式**：term 写**中文异译名**（如 `"库希尔"`），translation 写**统一中文译名**（如 `"库什艾尔"`）。脚本会自动把全书中出现的"库希尔"替换为"库什艾尔"。term 也可以是英文（如 `"Kushiel"`）→脚本会同时替换英文原文和中文异译。
- 术语统一只针对**同一外文名出现了不同中译**的情况，不要把简称替换成全名
- **glossary_additions 的 term 禁止使用 target 的前缀子串**：如果 target 是 `艾格勒莫特`，不要添加 `艾格勒莫` → `艾格勒莫特`（term 是 target 的前缀）。这会导致 reprocess 时正则匹配到 target 内部的 term 前缀子串，将正确的 `艾格勒莫特` 破坏为 `艾格勒莫特特`。如需修复省略/缺失字的情况，只添加**完整异译名**作为 term，而非残缺子串。脚本已内置前缀自愈逻辑，但仍应避免主动创建此类条目。
- **人名分量规则**：添加全名映射（如 `查尔德里克·德·埃索姆斯` → `奇尔德里克·德·埃索姆斯`）时，**必须同时检查是否需要添加人名分量映射**（如 `查尔德里克` → `奇尔德里克`）。角色在全书中常以单名（given name only）、姓氏（surname only）、或带头衔的短名出现——正则只做精确匹配，`查尔德里克·德·埃索姆斯` 的映射不会自动覆盖单独出现的 `查尔德里克`。同样适用于地名、机构名（如 `德·艾格勒莫` → `艾格勒莫特` 不会自动覆盖 `艾格勒莫` → `艾格勒莫特`）。
- **glossary_additions 的 target 禁止包含叠字**：如 `野野蕾薇院`、`艾格勒莫特特` 等含连续重复 CJK 字符的 target 会被脚本自动拒绝（`add_terms_batch` 内置检测）。重复字符几乎总是 LLM 输出错误——正确的 canonical form 不应有叠字。
- 引号内的对话不改变原意，只修正错别字和语病
- 删除 AI 套话/元语言（如"在上一章中""综上所述""话说回来"等过渡性废话）
- 注意相邻段落风格突变——可能是 AI 分块翻译的拼接痕迹，须平滑衔接
- 保留原文的换段和标点风格

### 每 batch 处理完立即运行

```bash
python "${CLAUDE_SKILL_DIR}/scripts/proofread.py" apply-corrections {work_dir} corrections.json
```

此命令自动完成：术语写入 glossary → 修正确认 → 如有新术语则 reprocess（reprocess 会同时更新 `_corrected.json` 和 `_preprocessed.json`，确保已校对的章节也能获得新的术语替换）。

### 数据文件说明（有助于理解状态流）

| 文件 | 含义 |
|------|------|
| `chapter_NNNN.json` | 原始提取（永不修改） |
| `chapter_NNNN_preprocessed.json` | 机械预处理后（glossary + 风格修正） |
| `chapter_NNNN_corrected.json` | LLM 校对后（有此文件说明该章已被校对） |
| `chapter_NNNN.corrected` | 哨兵文件（标记 `_corrected.json` 有效，防止回退到旧数据） |

reprocess 只会从 `chapter_NNNN.json` 重建 `_preprocessed.json`，同时更新 `_corrected.json` 中的术语。`_preprocessed.json` 永远不会被 LLM 文本污染。

## 步骤 3：检查 + 注入 + 打包

全部 batch 处理完后：

```bash
python "${CLAUDE_SKILL_DIR}/scripts/proofread.py" check --diff {work_dir}
python "${CLAUDE_SKILL_DIR}/scripts/proofread.py" inject {work_dir}
python "${CLAUDE_SKILL_DIR}/scripts/proofread.py" check --glossary {work_dir}
python "${CLAUDE_SKILL_DIR}/scripts/proofread.py" pack {work_dir}
```

- `check --diff` 终端只显示计数（无剧透）。如需逐句对比：`check --diff-log diff.txt {work_dir}`（文件带剧透警告头）。
- `check --glossary` 必须在 `inject` 之后运行。它扫描已注入的 HTML/XHTML 中残留的 CJK glossary key——即术语表中有映射但文本中仍未替换的异译名。如果发现残留，说明对应的 `_corrected.json` 段未能覆盖该处文本或 inject 遗漏了该节点，需用 corrections 修补后再重新 inject + check --glossary + pack。
- `check` 返回的非零退出码通常是英文删除导致的 change ratio 超阈值，属于预期行为。不要用 `&&` 串联最终命令；如果 diff 报告中修改段数与你的 corrections 数量吻合，可以继续 inject + check --glossary + pack。
- `pack` 完成后打印输出 EPUB 的路径。
- `pack` 会排除工具生成的 `.json` 文件；当前目标是小说类 EPUB，不支持依赖 JSON 数据文件的 EPUB3 互动书。
- **不要删 work 目录**——用户可能需要检查 diff 或进行多轮校对。

## 步骤 4：输出无剧透统计报告

`pack` 完成后，必须自动输出一份校对统计报告。报告只含数字和类别名称，**严禁包含**：
- 情节内容、角色命运、关键事件
- 任何具体人名（可用"主角""主要配角"等代称）
- 章节标题或具体段落内容
- diff 中的文本片段（check --diff 终端输出本身已无剧透，可直接引用）

### 报告模板

```
## 校对完成报告

| 项目 | 数值 |
|------|------|
| 全书字数 | xxx 字符 |
| 章节数 | xx 章 |
| 分段数 | xxxx 句 |
| Batch 数 | x 个 |

### 术语统一

| 类别 | 数量 |
|------|------|
| 术语总条数 | xxx |
| 主要人物名 | xx |
| 神祇/天使名 | xx |
| 院名/机构名 | xx |
| 地名/国名 | xx |
| 头衔/专有术语 | xx |
| 其他专名 | xx |

### 修正统计

| 类型 | 数量 |
|------|------|
| 黑名单词替换（手动） | xxx |
| 黑名单词替换（自动） | xxx |
| 英文段落删除 | xxx |
| 英文段落翻译 | xx |

### 输出

```
{output.epub 路径}
```

**xxx 段修改，xxx 条术语写入 glossary。** work 目录已保留。
```

### 获取统计数据的方法

- 术语总数：`grep -c '"term"' {work_dir}/glossary.json`
- 字典条目数：`python -c "import json; d=json.load(open('{work_dir}/glossary.json')); print(len(d))"`
- 各 batch 的 corrections 条数：从每次 `apply-corrections` 的输出累加
- check --diff 的输出可直接引用（已无剧透）

## 步骤 5：暂停并询问是否进入第 3 轮

**步骤 4 统计报告输出后，必须暂停，等待用户确认。不可自动进入第 3 轮。**

提示用户：

> 两轮校对已完成（纠错级，术语+黑名单+英文+翻译腔）。如需进一步提升文学质量，可进入第 3 轮润色（翻译腔深化、欧化句拆分、标点规范、角色声音增强、朗读节奏）。是否继续？

**用户回复"是/好/继续"之前，不得开始第 3 轮。**

### 第 3 轮文学润色

第 1-2 轮完成的是**纠错级校对**。第 3 轮进入**文学润色**——提升散文质量，不改变原意和情节。

### 润色范围（区别于纠错）

| 类别 | 具体操作 | 参考标准 |
|------|----------|----------|
| 翻译腔消除 | 检测并改写「被……所……」「是……的」「开始……起来」等 7 种模式 | `references/publishing-standards.md` |
| 欧化长句拆分 | 超过 100 字的单句，判断是否需要断句 | 不强制。排比/意识流/古典口吻的 deliberate 长句应保留 |
| 标点/数字规范 | 引号统一为 `""` 双层（内嵌 `''`）——大陆标准。省略号 `……`、数字汉字化 | `references/publishing-standards.md` |
| 角色声音辨识 | 对照 `voice_cards.md`（第 1 轮生成的角色对话样本），检查各角色说话风格是否一致（文雅/粗鄙/简洁/啰嗦），辨识度低的微调措辞使角色声音更鲜明 | 不改变性格和情节。voice_cards 供风格参考，不作为硬性模板 |
| 朗读节奏 | 标记拗口长句，提供顺滑建议 | 保持原文风格 |

### 润色约束（必须遵守）

- **不改变情节、人物性格、对话原意**
- **不统一的描写不强行统一**——角色视角变化带来的语气差异是合理的
- **不确定的不要改**——宁可漏过，不可过度润色
- **禁止大面积重写**——每段修改量控制在原文 20% 以内（翻译腔改写除外）
- **「不禁/不由得/不由」不可一律删除**——这类词承载"身不由己、下意识"的语义。仅当语境已充分传达被动意味、纯属填充时才删；情感张力强的场景（面对主顾、重要抉择、身体失控时）必须保留

### 操作流程

**与第 1-2 轮完全一致——全自动、无剧透、用户只看统计。**

a) 逐 batch 重新阅读（此时黑名单标记和英文段落应已全部消失，术语已统一）
b) 按润色范围进行修正，输出标准 corrections.json 格式：

```json
{"corrections": [{"chapter": 3, "segment_id": 15, "corrected": "润色后全文"}]}
```

c) 每个 batch 完成后立即 `apply-corrections`
d) 全部 batch 完成后：`check --diff` → `inject` → `check --glossary` → `pack`
e) 输出无剧透统计报告（追加润色统计，见下）

**用户全程不读正文**，只看到 pack 后的统计数据。与第 1-2 轮的无剧透保证一致。

### 润色统计（追加到校对报告）

```
### 第 3 轮润色

| 类别 | 数量 |
|------|------|
| 翻译腔消除 | xx |
| 欧化长句拆分 | xx |
| 标点/数字规范 | xx |
| 角色声音增强 | xx |
| 朗读节奏优化 | xx |
| 总修改段数 | xxx |
```

## 多轮校对

如果需要追加术语或补充修正：重新运行 `apply-corrections`。之前的 `_corrected.json` 和哨兵会被保留，增量合并。如果不放心注入质量，可以用累积的 glossary 重新跑 `pipeline --glossary glossary.json` 做干净重建。

## 其他实用命令

| 命令 | 作用 |
|------|------|
| `extract-terms {work_dir}` | 从 LLM 校对中自动提取新的术语映射（需出现≥3次且长度 2-10 字） |
| `add-term {work_dir} "原词" "替换词"` | 逐个添加术语 |
| `add-terms {work_dir} '[{"term":"x","translation":"y"}]'` | 批量添加术语（JSON 数组） |
| `reprocess {work_dir}` | 更新 glossary 后重跑机械预处理（同时更新 `_corrected.json`） |
| `config {work_dir}` | 显示当前配置（`--show`）/ 重置为默认（`--reset`） |
