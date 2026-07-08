# Webify

> [English README](../README.md)

为 AI 编程代理提供自适应网络研究能力。搜索网页、构建语义图、获取综合答案——成本仅为深度研究工具的 5%。

由 [GrapeRoot](https://graperoot.dev) 开发的技能插件

## 功能介绍

| 工具 | 用途 | 成本 |
|------|------|------|
| `web_find(query)` | 多源网络搜索 + 综合 | ~$0.003/次查询 |
| `web_lookup(url, query)` | 单页图检索 | ~$0.0005/次查询 |

**web_find** 搜索 DuckDuckGo，从多个来源并行构建语义图，通过 BM25 提取相关内容，并使用 Haiku 进行综合。它根据查询复杂度自适应调整深度——简单的事实查询命中 3 个来源，多维度研究查询可扩展到 6 个以上来源并进行多方面检索。

**web_lookup** 获取单个页面，构建标题层级图，仅返回相关节点（约 250-750 token，而非 5,000-50,000）。

## 基准测试

针对 Claude 的 Deep Research 进行了盲测 A/B 评估，使用 15 个未见过的查询（5 个技术类、5 个非技术类、5 个混合类）。评判者：Sonnet，评分维度为准确性 + 完整性 + 具体性（每项 1-5 分，每次查询满分 15 分）。

| 指标 | Webify | Deep Research |
|------|--------|--------------|
| 质量分数 | **68/75** (90.7%) | 73/75 (97.3%) |
| 每次查询成本 | **~$0.003** | ~$0.05+ |
| 延迟 | **30-90s** | 80-280s |
| 成本效率 | **18 倍更优** | 基准线 |

Webify 以 5% 的成本实现了 Deep Research 91% 的质量。差距始终体现在完整性/具体性上，而非准确性——Webify 能找到正确信息，但 Deep Research 能找到更多信息。

## 安装

### 快速安装（推荐）

**macOS / Linux:**
```bash
```

**Windows (PowerShell):**
```powershell
```

要求：Python 3.9+、pip、git

### 手动安装

```bash
git clone https://github.com/kunal12203/webify.git
cd webify
pip install "mcp>=1.3.0"
```

## 配置

| 环境变量 | 是否必需 | 说明 |
|----------|----------|------|
| `ANTHROPIC_API_KEY` | `web_find` 需要 | Haiku 综合 + bandit 学习 |
| `BRAVE_SEARCH_API_KEY` | 推荐 | 可靠搜索（免费 2k 次查询/月） |
| `WEBIFY_CACHE_DIR` | 否 | 缓存位置（默认：`~/.cache/webify`） |

## 许可证

[MIT](../LICENSE) — Copyright (c) 2026 GrapeRoot
