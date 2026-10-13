# 配置与兼容性

## 前置条件

- 本地 Codex、Python 3.10+、Git，以及可运行的 ZCode CLI。
- ZCode 账户已通过官方入口登录，所选 Coding Plan 可使用目标模型。
- 目标项目是本地 Git 仓库 / worktree；Desktop 至少启动过一次以建立原生任务索引。

不打包 ZCode、Node、provider 配置或凭据。账户配置和付费套餐由用户在官方 ZCode 中管理。
若已有 CLI，先运行 `zcode --help` 和 `zcode doctor --json`。账号登录状态与套餐绑定需分别确认；不打印凭据文件、不复制密钥、不自动切换计费入口。

## 使用 macOS 应用内置 CLI

已安装 ZCode Desktop 且尚无 `zcode` 命令时，可创建自己的启动器。先确认以下文件存在，以及 `node` 在 PATH 中：

```text
/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs
/Applications/ZCode.app/Contents/Resources/config/provider/zcode-builtin.json
```

创建 `~/.local/bin/zcode`，内容为：

```sh
#!/bin/sh
export ZCODE_BUILTIN_PROVIDER_CONFIG_FILE="${ZCODE_BUILTIN_PROVIDER_CONFIG_FILE:-/Applications/ZCode.app/Contents/Resources/config/provider/zcode-builtin.json}"
exec node /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs "$@"
```

赋予执行权限，将 `~/.local/bin` 加入 PATH，或为 runner 设置 `ZCODE_BIN="$HOME/.local/bin/zcode"`。
应用装在其他目录时替换应用路径；启动器不是账号登录或套餐配置工具。

## Runner 配置

| 项目 | 配置方式 |
| --- | --- |
| CLI | PATH 上的 `zcode`；或 `ZCODE_BIN` 指向一个可执行文件 / 启动器，不是带参数的命令字符串 |
| 内置 provider 配置 | `--builtin-config /path/to/zcode-builtin.json`；否则使用 `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE`；最后尝试上述 macOS 默认路径 |
| 套餐 | 默认 `account:bigmodel-individual-coding-plan`；Z.ai 使用 `--provider account:zai-individual-coding-plan` |
| 模型 | `--model GLM-5.3-Flash`（默认）或 `--model GLM-5.3` |
| 存储 | 默认 `~/.zcode`；自定义时 `ZCODE_STORAGE_DIR` 必须与实际 CLI / Desktop 一致 |
| 模式 | `--mode edit`（默认）；只读任务使用 `plan` |
| 时限 | `--zcode-timeout 1200`、`--test-timeout 300`，单位秒 |

CLI 0.16.9 未提供 `--model` 参数。Runner 使用临时 provider 配置及 `defaultModelSelection`，必须包含 `options.reasoningLevel: high`，并从 SQLite 会话元数据核对实际模型。临时配置仅有本轮模型 / 套餐规则，不包含复制的账号凭据；全局配置不改写。
禁止 `Task,Agent` 工具，返工由 Codex 控制；执行模式仍不是操作系统沙盒。

## Desktop 索引与升级

已验证组合：macOS、ZCode Desktop **3.14.5**、内置 CLI **0.16.9**。其他版本与平台需要重新验证；Windows 子进程清理仅有 mock 测试，未做真机验收。

Runner 只读 `cli/db/db.sqlite` 的 `session`、`session_entry` 和 `part`；向既有 `v2/tasks-index.sqlite` 的 `tasks` 表插入本轮索引。
使用事务与真实 workspace / session ID；不重写历史、已有标题、归档或删除状态，不新建缺失的 Desktop 数据库。
这是基于已安装版本内部契约的适配，并非官方公开 API。列表登记失败不会改变 worker / checks 结果，查看 `desktop_task.error` 排障；不要为此重跑模型。

更新 ZCode 后，先核对 CLI / Desktop 版本和上述 schema，在隔离 Git 项目跑无写入的短任务，确认 summary 的实际模型，并在 Desktop 搜索标题、打开原始历史。
缺少模型证据时 runner 拒绝报告检查成功。官方登录失败、网络失败和额度错误不进入代码返工流程。
明确达到 5 小时额度窗口限制时，按 SKILL.md 接管；超时与一般 429 不能代替额度证据。

## 本地证据与隐私

`.zcode-control/` 包含任务文本、命令、日志、路径和 review 证据。写任务前加入 Git 本地 exclude；runner 启动时也会补充该规则，已被 Git 跟踪的目录会阻止启动。
Git 忽略不会让已有跟踪文件停止跟踪，提交前仍需检查 staged / untracked 清单。不要把真实运行目录、SQLite 数据库或账户文件作为 issue 附件上传。
