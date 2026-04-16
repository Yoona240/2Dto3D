# 文档索引

> 本文档索引所有项目文档，按类别组织。
> 
> **重要提示**：本文档由 AI 维护，不要在文档中直接评论，如有修改建议请通过其他方式提出。

---

## 📚 核心文档（根目录）

| 文档 | 说明 |
|------|------|
| [README.md](/README.md) | 项目主文档：功能介绍、使用指南、API说明 |
| [AGENTS.md](/AGENTS.md) | 开发者指南：代码规范、架构说明、维护记录 |
| [CHANGELOG.md](/CHANGELOG.md) | 版本变更记录 |

---

## 📖 使用指南 (docs/guide/)

| 文档 | 说明 |
|------|------|
| [cli.md](guide/cli.md) | CLI 命令行工具完整指南 |
| [batch-render.md](guide/batch-render.md) | 批量渲染实现指南 |
| [http-client.md](guide/http-client.md) | HTTP 客户端使用规范 |
| [uv-env.md](guide/uv-env.md) | UV 环境使用指南 |

---

## 🏗️ 架构设计 (docs/architecture/)

| 文档 | 说明 |
|------|------|
| [PROJECT_STRUCTURE_OVERVIEW.md](architecture/PROJECT_STRUCTURE_OVERVIEW.md) | 项目结构详细概览 |
| [render-module.md](architecture/render-module.md) | 渲染模块设计文档 |
| [multiview-guardrail-prompt-plan.md](architecture/multiview-guardrail-prompt-plan.md) | 多视角编辑固定约束 Prompt 落地执行计划 |
| [taxonomy-design.md](architecture/taxonomy-design.md) | 数据分类体系设计 |
| [style-diversity.md](architecture/style-diversity.md) | 风格多样性设计方案 |

---

## 📋 快速参考 (docs/reference/)

| 文档 | 说明 |
|------|------|
| [QUICK_REFERENCE.md](reference/QUICK_REFERENCE.md) | 数据模型、API端点、过滤条件速查 |

---

## 📦 历史归档 (docs/archive/)

存放已完成任务的临时文档和历史记录：

| 文档 | 说明 | 日期 |
|------|------|------|
| [config_refactoring_2026-02-26.md](archive/config_refactoring_2026-02-26.md) | 配置系统重构详细文档 | 2026-02-26 |
| [REFACTORING_SUMMARY.md](archive/REFACTORING_SUMMARY.md) | 配置重构总结 | 2026-02-26 |
| [VERIFICATION_CHECKLIST.md](archive/VERIFICATION_CHECKLIST.md) | 配置重构验证清单 | 2026-02-26 |
| [FRONTEND_INTERACTION_CHECK.md](archive/FRONTEND_INTERACTION_CHECK.md) | 前端交互检查 | 2026-02-26 |
| [tripo_view_selection_test_new_render.md](archive/tripo_view_selection_test_new_render.md) | Tripo 视角选择测试 | 2026-02-27 |
| [tripo_enhancement_requirements.md](archive/tripo_enhancement_requirements.md) | Tripo 增强需求 | 2026-02-25 |
| [continue.md](archive/continue.md) | 会话连续性记录 | - |

---

## 📝 文档管理规则

### 文档命名规范

1. **根目录文档**：使用大写命名（README.md, AGENTS.md, CHANGELOG.md）
2. **子目录文档**：使用小写，单词间用连字符分隔（batch-render.md, taxonomy-design.md）
3. **文档类别**：
   - `guide/`：用户操作指南
   - `architecture/`：系统架构和设计文档
   - `reference/`：快速参考手册
   - `archive/`：历史归档（不活跃文档）

### 文档维护原则

1. **重要文档放在根目录**：README.md、AGENTS.md 必须保持在根目录
2. **临时文档及时归档**：完成任务后，相关文档应移动到 archive/ 目录
3. **更新 AGENTS.md**：每次重大变更后，在 AGENTS.md 中记录维护记录
4. **保持链接有效**：移动文档时，检查并更新所有引用该文档的链接

### 更新本文档

当新增、移动或删除文档时，需要同步更新此索引文件。
