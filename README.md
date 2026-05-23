# EPUB 中文出版校对

一键懒人化：丢一个 EPUB 进去，Python 自动完成机械准备，Claude 按 batch 深度校对术语、翻译腔和网文词，最终输出校对后的 EPUB。

## 安装

```bash
# 1. 安装依赖
pip install lxml

# 2. 克隆到 Claude Code 全局 skills 目录（所有项目可用）
cd ~/.claude/skills
git clone https://github.com/kiloiam/epub-chinese-proofreading.git

# 或者只装到当前项目
cd 你的项目
mkdir -p .claude/skills
git clone https://github.com/kiloiam/epub-chinese-proofreading.git .claude/skills/epub-chinese-proofreading
```

## 使用

### 方式一：Claude Code 技能（推荐，全自动）

在 Claude Code 对话中说「校对 EPUB」，或直接指定路径：

```
校对 EPUB：/home/user/小说.epub
```

Claude 自动完成全部流程：

| 阶段 | 谁做 | 内容 |
|------|------|------|
| pipeline | Python | 解包→提取→机械预处理→分卷→术语变体预扫描→英文术语检测→自动修正→生成 TASK.md |
| 第1轮校对 | LLM | 逐 batch 深度阅读，搜集术语变体，处理黑名单标记 |
| 第2轮校对 | LLM | 逐 batch 精修：英文处理、翻译腔消除、AI套话、风格统一 |
| 检查+注入+打包 | Python | check --diff→inject→pack→check --glossary → 输出统计报告 |
| 第3轮润色 | LLM | 翻译腔深化、欧化句拆分、标点规范、角色声音增强、朗读节奏（**用户确认后执行**） |

全程无剧透（终端只显示计数），最终输出 `output.epub`。

> **注意**：如果全书超过 10 万字，pipeline 会自动分卷（每卷 100K 字 + 相邻卷 2000 字上下文重叠），LLM 逐个 batch 处理。

### pipeline 自动术语预扫描

`pipeline` 第4步**默认自动执行**两项扫描，结果写入 TASK.md：

- **疑似术语变体**（`_find_suspected_variants`）：统计推断全书中的同指异译对（共享字符+同首字+长度相近），标注共现频率，供 LLM 第1轮逐对确认
- **未翻译英文术语**（`_find_english_terms`）：检测高频英文词（出现≥3次），识别应翻译为中文的专有术语

无需手动触发。LLM 在 TASK.md 中直接看到扫描结果。

### 方式二：pipeline + 手动 LLM

```bash
# 步骤 1：一键机械准备
python scripts/proofread.py pipeline 小说.epub --profile fantasy --work-dir ./work/

# 步骤 2：将 full_text.txt（或 proofread_batches/ 下的分卷）
#        发给任意 LLM 进行校对，输出 corrections.json

# 步骤 3：应用 LLM 校对结果
python scripts/proofread.py apply-corrections ./work/ corrections.json

# 步骤 4：检查 + 注入 + 打包
python scripts/proofread.py check --diff ./work/
python scripts/proofread.py inject ./work/
python scripts/proofread.py pack ./work/
python scripts/proofread.py check --glossary ./work/
```

> 不传 `--work-dir` 时，默认工作目录是 `proofread/{EPUB文件名}/work/`。

## 如何检查校对内容（防剧透设计）

所有终端输出只显示计数（如"3 segments modified"），不会打印小说正文。

如需审查，已完成阅读的用户可以导出详细对比文件：

```bash
python scripts/proofread.py check --diff-log diff.txt ./work/
```

生成的 `diff.txt` 头部带 **SPOILER WARNING** 横幅，逐句列出原文与校对后对比。用户自主决定是否查看。

## 命令参考

| 命令 | 作用 |
|------|------|
| `pipeline in.epub [--profile fantasy] [--max-chars N]` | 一键机械准备 + 术语预扫描 + 自动修正 |
| `dump-text ./work/ [--max-chars N]` | 导出全书（自动分卷） |
| `apply-corrections ./work/ corrections.json` | 应用 LLM 校对（自动 reprocess） |
| `check --diff ./work/` | 检查改动量（无剧透） |
| `check --glossary ./work/` | pack 后验证术语覆盖率 + glossary 自检 |
| `check --diff-log diff.txt ./work/` | 逐句对比（带剧透警告） |
| `extract-terms ./work/` | 自动提取术语映射 |
| `add-term ./work/ "原词" "替换词"` | 手动添加术语 |
| `add-terms ./work/ '[{"term":"x","translation":"y"}]'` | 批量添加术语 |
| `reprocess ./work/` | glossary 更新后重跑预处理 |
| `inject ./work/` | 校对文本注入 EPUB |
| `pack ./work/` | 打包输出 EPUB |
| `config ./work/ --show` | 显示当前配置 |

## 测试

```bash
cd scripts
python test_regression.py        # 86 项单元测试
python test_e2e.py                # 62 步端到端测试
python test_skill_workflow.py     # 完整技能流程仿真
python test_variant_detection.py  # 术语变体检测评估
python test_targeted_fixes.py     # 针对性修复验证
python test_variant_iso.py        # 变体检测隔离评估
python test_variant_improvement.py # 变体检测候选算法评估
```

## 成本对比（传统出版校对 vs 本技能）

以 55 万字翻译小说为基准（基于 Kushiel's Dart 实测）：

| | 传统出版校对 | 本技能 |
|---|---|---|
| 一校 | ¥16,500-27,500（2-3 月） | $10-150 API 调用（< 2 小时） |
| 二校 | ¥11,000-22,000（1-2 月） | 已含 |
| 翻译 | ¥55,000-110,000（3-6 月） | —（AI 翻译底本） |
| **合计** | **¥9-18 万 · 6-12 月 · 2-3 人** | **$10-150 · 2 小时内 · 0 人** |

### 质量对比

| | 传统一校 | 本技能 |
|---|---|---|
| 术语一致性 | 人工追踪 1000+ 专名必然有漏 | 1201 条零遗漏，跨章机械传播 |
| 英文残留 | 能清，但体力活 | 3层防护：自动检测+删除/翻译+注入前清扫 |
| 网文词替换 | 编辑凭经验，标准因人而异 | 36 词黑名单 + LLM 上下文感知替换 |
| 翻译腔检测 | 编辑凭语感 | 7 种模式对照 + LLM 逐段判断 |
| 速度 | 50-80 页/天 | 55 万字/小时 |

成本差三个数量级，时间差三个数量级。本技能的价值不是替代人类编辑，而是让人类编辑不必做术语追踪、黑名单筛选这类体力活，把精力留给文学判断。
