# Codex → ZCode

让 Codex 指挥 ZCode 的 **GLM-5.3-Flash / GLM-5.3** 完成实现和测试，由 Codex 验收、定向返工并交付，减少 Codex 直接编码与重复读取日志的用量。

A local Codex skill that delegates implementation to ZCode, verifies the actual GLM model, collects test evidence, and leaves final review to Codex.

## 能做什么

- Flash 执行常规任务，GLM-5.3 处理复杂实现或只读 review；最多两轮定向返工。
- 隔离单轮模型配置，核对会话实际模型，收集测试与 Git diff 证据。
- 自动登记到 ZCode Desktop 对应项目的对话记录，保留原生历史；客户端无需后台常驻。
- 确认 ZCode **5 小时额度窗口耗尽**后，由活跃 Codex 检查已有增量并接续原任务。

Runner 是单轮工具，调度、质量 review 和额度接管由 Codex 执行。节省幅度因任务而异；没有活跃 Codex 会话时不会后台监控。

## 安装到 Codex

在本地 Codex 中发送：

> 使用 skill-installer，从 GitHub 的 KYLeonis/codex-zcode 仓库根目录安装 codex-zcode Skill。

也可调用 Codex 附带的安装器：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-installer/scripts/install-skill-from-github.py" \
  --repo KYLeonis/codex-zcode --path . --name codex-zcode
```

安装器遇到已存在的目录会停止；更新前备份旧 Skill 再替换。新安装在下一轮对话中使用；未显示时重新打开 Codex 会话。
前置要求：Python 3.10+、Git、已登录且套餐可用的 ZCode CLI。只安装 Skill 不会安装 ZCode 或配置账户。
CLI 路径、BigModel / Z.ai 套餐选择、macOS 内置 CLI 启动器见 [配置与兼容性](references/setup.md)。

## 使用

在目标项目的 Codex 对话中输入：

> 使用 $codex-zcode 完成当前已授权任务：常规实现交给 GLM-5.3-Flash，复杂问题用 GLM-5.3。你检查变更、测试和验收结果；ZCode 5 小时额度耗尽后由你接续。

Codex 会读取项目约束、准备任务和检查清单、运行 runner，再审查真实增量。仓库的范围、阶段结束与 Human Review 规则继续生效。

手动运行时先将 `/.zcode-control/` 加入本地 Git exclude，在该目录创建 `task.md` 和可选 `checks.json`，然后从目标项目根目录调用：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/codex-zcode/scripts/zcode_delegate.py" \
  --task-file .zcode-control/task.md \
  --model GLM-5.3-Flash \
  --checks-file .zcode-control/checks.json
```

默认使用 BigModel Coding Plan；Z.ai 用户加 `--provider account:zai-individual-coding-plan`。只读任务加 `--mode plan`。
检查清单使用 argv 数组，不通过 shell 拼接：

```json
{"checks": [["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]]}
```

结果位于 `.zcode-control/runs/<run-id>/summary.json`。`checks_passed_review_required` 仍需要 Codex review；`no_checks_review_required` 表示没有配置测试。
模型不匹配、worker 失败、测试失败或 diff 检查失败会返回非零退出码。Desktop 登记独立报告在 `desktop_task` 中；失败时不重复实现任务。

## 验证与边界

```bash
python3 -m unittest discover -s tests -v
```

测试使用临时 Git 仓库与模拟 SQLite 数据，不联网、不调用真实模型、不消耗 ZCode 额度。
覆盖模型隔离、实际模型校验、失败 / 超时、暂存 / 未暂存 diff、日志忽略、Desktop 历史关联与重复登记。

[GitHub Actions 模板](references/test-workflow.yml) 可复制到 `.github/workflows/test.yml` 后启用 Linux / macOS、Python 3.10 / 3.13 测试。首版仅附模板，尚未执行云端 CI。

已实测 macOS + Desktop **3.14.5** / CLI **0.16.9**。Desktop 登记依赖内部数据库契约，升级后须重新验证；已打开客户端的列表可能需要搜索标题或重开项目刷新。
Windows 进程树清理仅做 mock 验证。执行模式及提示词不构成操作系统沙盒，文件范围靠任务约束和 Codex 验收。

本仓库不包含 ZCode 程序、provider 配置、凭据、真实对话数据库或运行日志；这是独立适配工具，与 ZCode 或 OpenAI 无官方关联。
