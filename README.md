# EPUB 中文出版校对

一键懒人化：用户提供 EPUB 小说路径，Claude 自动完成术语统一、翻译腔消除、网文词替换，输出校对后的 EPUB。

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

在 Claude Code 对话中说「校对 EPUB」，Claude 会激活技能并询问文件路径。或者直接说：

```
校对 EPUB：/home/user/小说.epub
```

Claude 自动完成全部流程：
1. 解包 EPUB，提取正文（过滤 CSS/JS/注释）
2. 机械预处理（glossary 术语替换 + 网文词标记）
3. 导出全文（100K 字自动分卷 + 上下文重叠）
4. 逐卷校对（术语统一、翻译腔修正、网文词替换）
5. 检查改动量 → 二进制注入 → 打包输出

全程无剧透（终端只显示计数），最终输出 `output.epub`。

> **注意**：如果全书超过 15 万字，Claude 需要分多轮逐个 batch 处理。只需回复"继续下一个 batch"即可。

### 可选：术语预扫描

默认不会做术语预扫描。只有当你明确说“先做术语预扫描”“长篇术语很多，先建术语表”等需求时，才使用这个流程。

适合：长篇/系列小说、专有名词密集、AI 分块翻译异译严重的 EPUB。

```bash
python scripts/proofread.py init 小说.epub ./work/
python scripts/proofread.py extract ./work/
python scripts/proofread.py term-prescan ./work/
# 读取 ./work/TERM_SCAN_TASK.md，让 LLM 输出 term_scan_result.json
python scripts/proofread.py apply-term-scan ./work/ term_scan_result.json
python scripts/proofread.py preprocess ./work/
python scripts/proofread.py dump-text ./work/ --max-chars 150000
```

`term-prescan` 只生成扫描材料，不会改正文；`apply-term-scan` 只应用明确输出的 `glossary_additions`。不确定项应放入 `conflicts_need_human`，不会自动应用。

### 方式二：命令行手动（不依赖 Claude）

```bash
# 步骤 1：机械准备（解包 + 提取 + 预处理 + 导出）
python scripts/proofread.py pipeline 小说.epub --profile fantasy

# 步骤 2：将 full_text.txt（或 proofread_batches/ 下的分卷）
#        发给任意 LLM 进行校对，输出 corrections.json

# 步骤 3：应用 LLM 校对结果
python scripts/proofread.py apply-corrections ./work/ corrections.json

# 步骤 4：检查 + 注入 + 打包
python scripts/proofread.py check --diff ./work/
python scripts/proofread.py inject ./work/
python scripts/proofread.py pack ./work/

# 输出在 ~/.claude/proofread/{书名}/output.epub
```

## 如何检查校对内容（防剧透设计）

**默认行为**：所有终端输出只显示计数（如"3 segments modified"），不会打印小说正文。保护未读完的读者不被剧透。

**如需审查**：已完成阅读的用户可以导出详细对比文件：

```bash
python scripts/proofread.py check --diff-log diff.txt ./work/
```

生成的 `diff.txt` 文件头部有 **SPOILER WARNING** 横幅，逐句列出原文与校对后的对比（`-` 原文 / `+` 校对后）。由用户自主决定是否查看。

如果是 Claude Code 技能模式，对 Claude 说"帮我导出 diff 检查一下校对结果"即可。

## 命令参考

| 命令 | 作用 |
|------|------|
| `pipeline in.epub [--profile fantasy]` | 一键机械准备 |
| `dump-text ./work/ [--max-chars 150000]` | 导出全书（自动分卷） |
| `apply-corrections ./work/ corrections.json` | 应用 LLM 校对 |
| `check --diff ./work/` | 检查改动量（无剧透） |
| `check --diff-log diff.txt ./work/` | 逐句对比（带剧透警告） |
| `extract-terms ./work/` | 自动提取术语映射 |
| `add-term ./work/ "原词" "替换词"` | 手动添加术语 |
| `reprocess ./work/` | glossary 更新后重跑预处理 |
| `inject ./work/` | 校对文本注入 EPUB |
| `pack ./work/` | 打包输出 EPUB |

## 测试

```bash
cd scripts
python test_regression.py    # 86 项单元测试
python test_e2e.py            # 62 步端到端测试
python test_skill_workflow.py # 完整技能流程仿真
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
| 术语一致性 | 人工追踪 1000+ 专名必然有漏 | 1181 条零遗漏，跨章机械传播 |
| 英文残留 | 能清，但体力活 | 自动检测+删除/翻译 |
| 网文词替换 | 编辑凭经验，标准因人而异 | 36 词黑名单 + LLM 上下文感知替换 |
| 翻译腔检测 | 编辑凭语感 | 7 种模式对照 + LLM 逐段判断 |
| 速度 | 50-80 页/天 | 55 万字/小时 |

成本差三个数量级，时间差三个数量级。本技能的价值不是替代人类编辑，而是让人类编辑不必做术语追踪、黑名单筛选这类体力活，把精力留给文学判断。
