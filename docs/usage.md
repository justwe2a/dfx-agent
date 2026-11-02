# 使用说明

## 网页工作台

首次运行 `scripts/start-workbench.ps1 -Install`；之后直接运行同一脚本。`-Live` 开启真实模型，`-Device` 允许任务选择设备能力，`-Port` 修改端口。工作台只绑定本机回环地址；该服务没有多人身份认证，不应暴露到公网。

在“新建任务”选择原项目，输入自然语言目标，例如“修复配置覆盖时 false 被丢弃的问题，保留输入对象，运行工程测试并说明结果”。不需要提供根因或补丁。先在设置中选择模型模式、资料范围以及可选的日志和主机配置，再核对并启动。

新建的范围文件可参考 `examples/scope.json`。`repo` 必须与项目绝对路径一致；`exclude` 中的文件不进入工作副本，`read_only` 限制修改。原始会话、密钥、独立验收答案和其他项目不应加入资料范围。

运行中可查看工具记录、停止任务。刷新网页不会重新派发；明确等待操作者处理环境时，完成相应操作后可在同一任务继续，保留历史和累计额度。未知中断、传输失败或终态任务不会自动重跑。

开源版本提供“新建任务”通用入口，不附带预分配的任务目录或私有数据下载入口。

## 命令行

先按 README 安装依赖和 tokenizer，并配置本机 `DEEPSEEK_API_KEY`。以下为示例，路径需自行替换：

```powershell
.\.venv\Scripts\python.exe -m dfx_agent solve --repo D:\projects\sample-app --task "修复所述故障，运行现有检查并提交结果" --scope D:\projects\sample-scope.json --provider deepseek --model deepseek-flash --model-protocol tool-calls --thinking enabled --reasoning-effort high --max-steps 40 --closing-requests 1 --max-output-tokens 32768 --model-timeout 180 --out D:\dfx-runs\sample-001
```

输出目录必须是尚不存在的目录，且位于原项目之外。可用 `--task-file` 读取任务文件，用 `--host` 提供 HarmonyOS 配置。`--allow-device`、`--allow-dependency-install`、`--allow-system-navigation` 和 `--allow-screen-unlock` 分别开启对应能力；屏幕解锁仅支持明确配置的普通无密码锁屏。完整参数见 `python -m dfx_agent solve --help`。

不要把工作台的结果目录选为待修复项目。API 密钥只通过进程环境变量传入，不写入任务、范围文件或命令参数。

## 结果

网页结果页提供补丁、声明和完整事件的查看与导出。CLI 输出包括 `result.json`、`report.md`、`patch.diff`、`trajectory.json`、`model/` 和 `tools/`。这些文件可能包含完整源码片段、路径、命令输出及模型对话，分享前需要检查。

模型声明与实际检查分别记录。构建通过不能替代业务验证；工具失败或未执行的检查应按真实结果解释。修复完成后审阅补丁，再将需要的变更合入自己的项目。
