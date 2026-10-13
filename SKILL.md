---
name: codex-zcode
description: 在本地 Codex 中指挥 ZCode 的 GLM-5.3 与 GLM-5.3-Flash 实现、测试和返工，由 Codex 把控范围与最终验收，减少 Codex 编码用量。用户要求 ZCode 派工、GLM 执行项目或节省 Codex 额度时使用。
---

# Codex 指挥 ZCode

Codex 负责范围、验收和关键决策；ZCode 负责实现、定位、测试和定向返工。
逐个完成已授权的任务，保留项目阶段结束、Human Review 等既有关卡。
需要能访问 ZCode CLI 的本地环境；没有活跃 Codex 会话时不会持续监督或自动接管。

## 模型分工与用量

- 默认 `GLM-5.3-Flash`：局部修复、检索、文档、测试补齐和常规实现。
- `GLM-5.3`：跨模块设计、科学计算、持久化、并发、复杂失败；独立 review 使用全新只读任务。
- Codex 读取必要的规范、摘要、失败日志和相关 diff；让 ZCode 返回变更、测试、证据路径与未解决问题，避免反复加载完整日志。
- 同一范围最多 1 次实现 + 2 次定向返工。账户、网络和配置失败不作代码返工，不自动重试；额度问题按下方流程处置。节省幅度取决于任务，不能承诺百分比。

## 派工

1. 核对工作目录、分支及 `git status --short --untracked-files=all`，阅读项目 `AGENTS.md`、状态入口及相关代码规范。
   保留用户已有修改；写入任务需要时使用隔离 worktree，不让多个执行者同时写同一 worktree。
2. 首次使用或环境变化时跑 `zcode --help`、`zcode doctor --json`。按 [配置与兼容性](references/setup.md) 确认 CLI、套餐入口、模型与存储目录。
   使用用户已选择的 Coding Plan，不以其他供应商或普通计费 API 替代。不读取或输出原始凭据。
3. 将 `/.zcode-control/` 加入当前仓库的本地 Git exclude，再写 `.zcode-control/task.md`：
   目标、worktree / branch、允许文件、保留的契约、验收标准、运行方式与简洁返回格式。
   禁止未经任务授权的 commit / push / merge、reset / clean、删用户文件、装依赖、读取凭据和调用其他 Agent。
   Runner 也会在启动前检查并补充本地 exclude；若该目录已被 Git 跟踪，先处理跟踪状态，不直接派工。
4. 需要检查时写 `.zcode-control/checks.json`，如 `{"checks": [["python3", "-m", "unittest", "discover", "-s", "tests"]]}`。
   选择项目实际使用的解释器和 argv 命令，检查范围与改动相称。
5. 从目标 Git 项目根目录运行 Skill 附带的 `scripts/zcode_delegate.py`。默认安装位置示例：

   ```bash
   python3 "${CODEX_HOME:-$HOME/.codex}/skills/codex-zcode/scripts/zcode_delegate.py" \
     --task-file .zcode-control/task.md --model GLM-5.3-Flash \
     --checks-file .zcode-control/checks.json
   ```

   复杂任务换 `--model GLM-5.3`；只读任务加 `--mode plan`；无检查时省略 `--checks-file`。
   默认 provider 为 BigModel 套餐，Z.ai 用户必须显式加 `--provider account:zai-individual-coding-plan`。
   配置位置与 CLI 路径的选项见 [配置与兼容性](references/setup.md)。不要把默认安装目录当作实际 Skill 路径的唯一来源。

Runner 每轮创建只含所选套餐与模型的临时配置，使用 `high` 思考强度，结束后删除临时配置，保留全局模型设置。
默认单轮最多 1200 秒，检查最多 300 秒。超时先检查已写入的 diff，再决定续派；轮询工具的运行会话并向用户报告有意义的进展。
`edit` / `plan` 和提示词不是操作系统沙盒，允许文件仍需 Codex 验收。

## Desktop 对话记录

Runner 自动将本轮 CLI 会话登记到 ZCode Desktop 的对应项目 / worktree，沿用原生标题及历史；不需要客户端后台常驻。
客户端已打开时侧栏可能缓存旧列表，可搜索任务标题（macOS 使用 ⌘K）或重新打开项目 / 客户端。
检查 summary 的 `desktop_task`；登记失败只排查索引，不因此重复调用模型。
超时 / 失败会话仅在工作区、创建时间和完整任务文本能唯一匹配时登记，否则保留本地日志。
此功能依赖 ZCode 内部数据库布局，升级后按 [配置与兼容性](references/setup.md) 重新验证。

## 5 小时额度限制时接管

默认在确认 ZCode 的 5 小时额度窗口耗尽后，由 Codex 接续原任务；用户指定其他策略时以用户指令为准。

1. 用明确额度错误或账户额度状态确认窗口耗尽；单轮 `timed_out`、一般 HTTP 429、累计运行 5 小时均不能单独证明限额。
   确认后停止派工，不换 GLM 型号或计费入口重试。确认本轮 ZCode 已退出，避免并发写入。
2. 阅读任务、项目状态、最近 summary 及必要日志，核对实际 worktree / branch、暂存与未暂存 diff 和新文件。
   对照验收标准区分已验证、已实现待验证、未完成和阻塞项，保留用户改动与有效增量。
3. 在 `.zcode-control/handoff.md` 记录限额证据、完成情况、测试、遗留问题与确切下一步；简短报告“ZCode 5 小时额度已用尽，Codex 接续”。
4. Codex 完成原任务剩余实现、测试、review 和文档收尾，不等待额度恢复。保留范围、阶段结束与 Human Review 关卡；接管不授权新阶段或额外外部操作。

接管由活跃 Codex 执行；runner 只执行单轮任务，不是后台额度监视器。

## 质量验收与返工

证据位于 `.zcode-control/runs/<run-id>/summary.json` 及日志。
先看 status、`actual_model_selection`、测试结果和暂存 / 未暂存 diff 检查，再读必要日志及 diff，打开新建源文件与测试。
Runner 只读核对 CLI 元数据中的实际模型；`model_mismatch` / `model_verification_failed` 属配置阻塞，先核对版本与元数据，不作代码返工。
没有 checks 的成功仅代表调用完成；exit 0 或测试通过均不代替逻辑、契约、边界与范围 review。

返工写清失败原因、涉及文件与预期行为，不重发原任务。独立 review 使用新的 `--mode plan --model GLM-5.3` 调用，禁止修改实现。
范围外修改或破坏性行为出现时停止派工、保留证据，不自动回滚。提交、推送、集成及 GUI 真人验收沿用用户授权与仓库规则。
