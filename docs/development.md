# 开发与测试

按 README 安装依赖与 tokenizer 后，在 Windows PowerShell 中运行：

```powershell
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

公开测试使用临时合成项目和模型/设备夹具，覆盖 Agent 循环、工具协议、工作副本、等待继续、网页结果与导出。不调用真实模型 API 或设备。测试会执行本机临时命令，需安装 Git 和 PowerShell。

本版本不附带依赖私有现场的回归测试、历史测试输出或真实业务证据。合成测试通过不表示任意应用都能被自主修复；实际修复需要目标工程的业务检查。

`scripts/demo-agent.py --model-protocol tool-calls` 可执行固定动作的工具往返演示。生成记录位于忽略目录，勿提交。测试脚本和演示脚本中的 `TEST_SECRET` 等字符串均为夹具值。
