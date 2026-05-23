# 使用示例

## 基本用法

### 场景 1：快速校对

将 `input.epub` 放在当前目录，然后说：

> 校对这本 EPUB

Claude 会自动完成全流程。

### 场景 2：带术语表校对

已有 `glossary.json`：

> 校对这本 EPUB，用已有的术语表

### 场景 3：已知这是奇幻/言情小说

> 校对这本 EPUB，用奇幻小说宽松模式（降低黑名单灵敏度）

或者：

> 校对这本 EPUB，跳过黑名单检查

### 场景 4：只统一专有名词

> 只帮我统一这本 EPUB 里的人名和地名，不做其他校对

### 场景 5：更新术语表

> 在这个 EPUB 里把所有"哈利"改成"哈里"，加入术语表

---

## 分步流程（手动控制）

```bash
# 1. 初始化
python .claude/skills/epub-chinese-proofreading/scripts/proofread.py init input.epub work/ --glossary glossary.json

# 2. 提取文本
python .claude/skills/epub-chinese-proofreading/scripts/proofread.py extract work/

# 3. [Claude 校对] — 读取 work/extracted/chapter_*.json，逐一校对

# 4. 注入 & 打包
python .claude/skills/epub-chinese-proofreading/scripts/proofread.py inject work/
python .claude/skills/epub-chinese-proofreading/scripts/proofread.py pack work/ output.epub

# 5. 清理
rm -rf work/
```

## Config 自定义

```json
{
  "blacklist": [
    "心头一颤",
    "美眸",
    "红唇"
  ],
  "proofreading": {
    "rules": {
      "naturalize_word_order": true,
      "remove_translation_patterns": true,
      "trim_redundancy": false,
      "minor_restructure": false,
      "normalize_punctuation": true,
      "enforce_glossary": true
    }
  }
}
```
