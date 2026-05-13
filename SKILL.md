---
name: epub-chinese-proofreading
description: 中文 EPUB 出版校对。统一专有名词/人名，消除翻译腔/网文腔，输出校对后 EPUB。触发词：「校对 EPUB」。
when_to_use: 用户需要进行 EPUB 中文出版校对时触发。
allowed-tools: [Bash, Read, Write, Edit, Glob]
model: sonnet
user-invocable: true
---

# EPUB 中文出版校对

一键自动化。用户只需提供 EPUB 路径，你负责全程。

## 步骤 1：运行 pipeline

支持 UTF-8、GBK、Big5 编码的 XHTML 正文解码与回写。

```bash
python "${CLAUDE_SKILL_DIR}/scripts/proofread.py" pipeline "EPUB文件路径"
```

可选参数：`--profile fantasy|romance|general|minimal`（黑名单配置）、`--glossary glossary.json`（外部术语表）、`--config config.json`（本次运行的临时覆盖配置）、`--work-dir 自定义工作目录`。大上下文模型（1M+）可用 `--max-chars 200000` 减少分卷；小模型用 `--max-chars 50000`。

`pipeline` 自动完成：解包 → 提取正文 → 术语/黑名单机械预处理 → 导出全文（自动按 80K 字分卷 + 相邻卷 2000 字上下文重叠）→ 生成 TASK.md。

## 步骤 2：读取 TASK.md 并按轮次校对

`pipeline` 输出 `TASK.md` 的路径。读取它，按指示操作。

全书采用**两轮策略**：

**第 1 轮 — 术语发现 + 黑名单：**
- 逐 batch 阅读，重点搜集人名/地名/神名的同指异译
- 每发现一组变体，立即写入 `glossary_additions`
- 处理所有 `[? 需替换网文词: xxx]` 标记
- 每 batch 完成后 `apply-corrections`，reprocess 自动把新术语传播到全量章节

**第 2 轮 — 英文处理 + 精修：**
- 处理 `[? 英文段落]` 标记：
  - 如果该段英文的相邻段落（5 段内）有对应的中文译文 → 将英文段替换为空格（删除英文，保留中文）
  - 如果找不到中文译文 → 翻译为中文
- 修正 AI 套话、翻译腔、风格突变

### 校对输出格式

你可以将 glossary_additions 和 corrections 分两个 json 代码块输出，脚本会自动合并（不会丢失任何一个块）。JSON 尾部逗号也会被自动修复。

```json
{"glossary_additions": [{"term": "异译名", "translation": "统一译名"}]}
```

```json
{"corrections": [{"chapter": 0, "segment_id": 3, "corrected": "修正后的文本"}]}
```

### 坐标和规则

- `segment_id` 用文本中的 `[cN.sM]` 或 `[cN.sM.K]` 坐标。**写整数，不要写 3.0**（脚本会自动把 `3.0` 转成 `3`，但直接写整数更可靠）
- `[? 需替换网文词: xxx]` 标记的词**必须替换**为中性表达
- `[? 英文段落]` — 英文段有相邻中文配对→**删除英文**（保留中文译文）
- `[? 英文段落·待翻译]` — 英文段无中文配对→**翻译为中文**
- **全中文化**：所有外文单词（包括虚构世界专有术语如 `anguissette`、`vrajna` 等）都必须翻译为中文。发现后写入 glossary_additions，reprocess 自动传播
- 术语统一只针对**同一外文名出现了不同中译**的情况，不要把简称替换成全名
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
python "${CLAUDE_SKILL_DIR}/scripts/proofread.py" check --diff {work_dir} && \
python "${CLAUDE_SKILL_DIR}/scripts/proofread.py" inject {work_dir} && \
python "${CLAUDE_SKILL_DIR}/scripts/proofread.py" pack {work_dir}
```

- `check --diff` 终端只显示计数（无剧透）。如需逐句对比：`check --diff-log diff.txt {work_dir}`（文件带剧透警告头）。
- `check` 返回的非零退出码通常是英文删除导致的 change ratio 超阈值，属于预期行为。如果 diff 报告中修改段数与你的 corrections 数量吻合，可以直接 inject + pack。
- `pack` 完成后打印输出 EPUB 的路径。告知用户该路径。
- `pack` 会排除工具生成的 `.json` 文件；当前目标是小说类 EPUB，不支持依赖 JSON 数据文件的 EPUB3 互动书。
- **不要删 work 目录**——用户可能需要检查 diff 或进行多轮校对。

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
