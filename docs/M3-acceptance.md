# M3 可靠传输验收（0.3.0rc1）

本文件区分已执行的本机检查与仍需在两台真实 Windows 电脑上执行的验收。M3 的重试从头上传，不提供断点续传。接收端当前凭全局授权码接收，历史记录不标注未经认证的发送设备身份。

## 已完成的本机检查

- `python -m pytest -q`：33 项通过，覆盖授权失败、取消清理、同名冲突、磁盘满、双端历史和重启恢复。
- `python -m ruff check .`：通过。
- Linux 无显示器桌面启动及 HTTPS 健康检查：通过；接收历史定时刷新：通过。
- 1 GiB 本机回环 HTTPS 传输：接收端文件和两端记录均完成，源文件与接收文件 SHA-256 相同；进程峰值内存约 58 MiB。此检查不能替代 Windows 双机实测。

## Windows 双机验收记录

在两台电脑上使用同一份源码构建的 `0.3.0rc1` 安装包。关闭旧版应用后，在项目根目录运行：

源码 ZIP 中的 `Start-AI-Device-Bridge.bat` 可以直接双击运行源码，适合先检查功能；它不能代替本节的安装包验收。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\build_windows_release.ps1"
```

确认安装包显示 `0.3.0rc1`，两端应用数据仍位于 `%APPDATA%\AI Device Bridge`。在接收电脑上确认节点健康接口：

```powershell
curl.exe -k --max-time 3 https://127.0.0.1:8765/api/v1/health
```

| 场景 | 通过条件 | 结果/证据 |
| --- | --- | --- |
| 安装与升级 | 旧证书、配对和已收文件保留；本机服务可启动、停止、再次启动 | 待实测 |
| 普通文件 | 发送端进度增长并进入接收校验；两端历史均为完成；两端文件哈希一致 | 待实测 |
| 上传中取消 | 发送端显示已取消；接收端没有正式文件；`Received\.bridge-staging` 没有残留 `.part` | 待实测 |
| 断网或接收端停止 | 发送端不显示成功；失败或结果未知可解释；重试前能核对接收端文件 | 待实测 |
| 从头重试与同名 | 失败后重试生成新的历史记录；已有同名文件返回冲突且原文件不变 | 待实测 |
| 重启恢复 | 两端历史仍可见；中断中的记录不显示为完成；遗留暂存文件被清理 | 待实测 |
| 1 GiB 文件 | 两端 `Get-FileHash -Algorithm SHA256` 一致，界面进度与最终结果正确 | 待实测 |

比较哈希时，将路径换成实际源文件和接收文件：

```powershell
Get-FileHash -LiteralPath "<源文件路径>" -Algorithm SHA256
Get-FileHash -LiteralPath "<接收文件路径>" -Algorithm SHA256
```

接收目录默认在 `%APPDATA%\AI Device Bridge\Received\<目标目录>`。完成表格并记录失败时的界面文字后，才能将候选版改为正式 `0.3.0`。
