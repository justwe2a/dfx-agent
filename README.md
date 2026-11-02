# DFX Agent

面向本地项目的自主代码修复 Agent。通过网页或命令行输入任务，由模型探索代码、修改工作副本、执行检查并提交结果。提供通用文件工具，以及 HarmonyOS 构建、部署、页面观察和日志工具。

## 快速开始

建议使用 Windows 11、Python 3.13 x64 和 Git for Windows。当前命令执行与进程管理主要针对 Windows；其他系统尚未验证。HarmonyOS 任务另需安装官方 DevEco Studio、相应 SDK 和测试设备。

首次在项目根目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-workbench.ps1 -Install
```

安装依赖，并从 DeepSeek 官方地址下载本地 tokenizer 后，打开 `http://127.0.0.1:8765/`。默认是零 API 模式，不连接设备。点击“载入零 API 演示”，可验证网页、真实本地工具和导出流程；演示动作来自固定夹具，不代表模型修复能力。

实际修复时，在本机设置 `DEEPSEEK_API_KEY`，重启终端，再执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-workbench.ps1 -Live
```

点击“新建任务”，选择你有权处理的项目，描述问题和预期行为。在设置中指定资料范围 JSON，核对后启动。默认使用 DeepSeek Flash、思考开启/high。需要设备时额外加 `-Device`，并在任务设置中选择主机配置及实际允许的能力。

资料范围与主机配置示例在 `examples/`；将其中的示例路径改为本机路径。范围文件里的 `outbound_approved: true` 表示你已允许这些项目资料发送给模型服务，不能仅为绕过检查而设置。

保持启动终端运行。任务在独立副本中修改，原项目不自动覆盖；运行中由 Agent 决定步骤。结果页可查看最终声明、补丁、实际工具记录并导出。`Submitted` 表示已提交声明，是否修复成功还要看实际检查。

## 文档

- [使用说明](docs/usage.md)：网页、CLI、结果与显式继续。
- [技术设计](docs/design.md)：任务循环、共享工具与数据流。
- [输入输出 Spec](docs/spec.md)：任务范围、工具与结果合同。
- [数据与隐私](docs/privacy.md)：模型外发、运行记录和发布边界。
- [开发与测试](docs/development.md)：本地合成测试。
- [第三方声明](THIRD_PARTY_NOTICES.md)。

源码使用 [MIT](LICENSE) 许可证。此版本不包含个人身份信息、任务数据集、历史模型对话、设备日志、运行证据、签名、密钥或原开发仓库历史。自行运行产生的记录不应直接提交到公共仓库。
