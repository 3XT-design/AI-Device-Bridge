# AI Device Bridge

个人设备之间的文件交付工具。当前版本支持设备配对、自然语言查找候选文件、人工审核传输计划，以及通过 HTTPS 发送文件并校验完整性。

## M3 验收候选版（0.3.0rc1）：可靠传输

发送端现在显示源文件校验、已发送字节数和接收端校验阶段；上传过程中可以取消。每次发送尝试都会保存独立的任务与历史，失败后可对同一已确认计划重新发送。重试会从头上传，不支持断点续传。应用异常退出后，未收到接收端完成确认的任务会显示为“结果未知”；重试前请先检查接收端文件，以免遇到同名冲突。接收端只有完整校验成功后才发布文件，磁盘空间不足会返回明确错误。

发送端和接收端现在都保存逐笔历史；接收端只有全局接收授权码，无法据此可靠识别发送设备，因此接收历史不标注发送者。服务重启会清理本程序遗留的临时文件，并将未确认完成的接收记录标为结果未知。M3 的 Windows 安装包及双机操作仍需实测。

双机验收时，用同版安装包在两台 Windows 电脑上依次检查：正常发送时进度增长且接收端哈希一致；发送大文件中途取消后接收端没有正式文件或残留的 `.part`；重新发送产生一条新的完成记录，重启应用后历史仍可见；断网或超时后界面不显示成功，先检查接收端再决定是否重试。最后用 1 GiB 文件比较两端 `Get-FileHash -Algorithm SHA256` 结果。

## M1-09 发布与验收

M1-09 补充双节点端到端测试、Windows 可执行文件/安装包构建脚本、安装说明、演示步骤和 1 GiB 大文件验收流程。Windows 发布包需在 Windows 电脑上构建；本源码包不包含预编译的 `.exe`。

## 开发环境

### 双击启动源码包（Windows）

将源码 ZIP 解压到一个新目录，然后双击解压目录最外层的 `Start-AI-Device-Bridge.bat`。它会自动找到项目，并在 `%LOCALAPPDATA%\AI-Device-Bridge\venv` 创建或复用虚拟环境，避免解压路径较长时 PySide6 安装失败；首次启动时需要已安装的 Python 3.12 或 3.13，并会安装依赖。此前解压目录中的 `.venv` 不再使用，也无需手动删除。以后更新版本时，下载并解压新版 ZIP，再双击新版 BAT 即可，不需要修改压缩包名称或 PowerShell 命令。启动期间保留的命令行窗口会显示安装或运行错误。BAT 运行的是源码；M3 的正式验收仍需单独检查 Windows 安装包。

需要 Python 3.12 或 3.13。解压源码包，在 PowerShell 进入包含 `pyproject.toml` 的项目目录：

```powershell
$zipPath = Join-Path $env:USERPROFILE "Downloads\AI-Device-Bridge-M1-09.zip"
$projectParent = Join-Path $env:USERPROFILE "Projects"
Expand-Archive -LiteralPath $zipPath -DestinationPath $projectParent -Force
Set-Location (Join-Path $projectParent "ai-device-bridge")
Test-Path .\pyproject.toml
```

最后一条命令应显示 `True`。创建开发环境并运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup_windows.ps1
.\scripts\run_windows.ps1
```

检查代码：

```powershell
python -m pytest -q
python -m ruff check .
```

## 构建 Windows 发布包

在 Windows 电脑的项目根目录运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\build_windows_release.ps1
```

脚本会安装 PyInstaller 并生成便携版文件夹和 ZIP，放在 `release` 目录。安装 Inno Setup 6 后再运行同一脚本，还会生成 `AI-Device-Bridge-Setup.exe`。安装器以当前用户权限安装，不需要管理员权限。首次运行仍需允许 Windows 防火墙专用网络上的 TCP 8765 入站连接。

升级时关闭应用并运行新版安装器，或解压新版便携包覆盖应用文件。用户数据库、本机 TLS 证书、接收授权码和接收文件保存在 `%APPDATA%\AI Device Bridge`，不会写入安装目录，也不会随卸载删除。

## 双机演示流程

两台 Windows 电脑都运行同一版程序，并处于可以互相访问的局域网。

1. 两边启动应用并点击“启动本机服务”。接收电脑运行 `ipconfig`，记下局域网 IPv4 地址。
2. 发送电脑在目标地址中输入 `接收端IPv4:8765` 并检查设备。首次配对前，通过可信渠道逐字符核对双方显示的 TLS 指纹，再确认保存配对。
3. 接收电脑复制本机授权码；发送电脑选中接收设备，粘贴授权码并点击“保存授权码”。
4. **手动文件演示：**选择一个普通文件，选中目标设备，计算 SHA-256，检查文件名、大小、目标设备和目录，再确认计划。
5. **AI 文件演示：**选择授权目录，确认 Ollama 服务和模型可用，输入自然语言文件描述并查找候选。选中候选并载入后，程序会计算 SHA-256，再弹窗询问是否加入最近传输计划。确认后计划入列，发送仍需单独确认。
6. 在最近计划中选择要发送的计划并点击“发送所选计划”。发送端收到接收端大小和 SHA-256 校验成功的响应后，才显示成功。
7. 接收文件位于 `%APPDATA%\AI Device Bridge\Received\<目标目录>`。接收端不覆盖同名文件。可以在计划列表中删除记录；删除不会移除本地源文件、接收文件或相关传输历史。

## 1 GiB 大文件验证

在发送电脑项目根目录创建一个 1 GiB 测试文件（约占用 1 GiB 磁盘空间）：

```powershell
python .\scripts\create_test_file.py
```

脚本会在下载目录生成 `ai-device-bridge-test-1GiB.bin`，并输出 SHA-256。按上面的手动流程将它发到另一台电脑，等待校验完成后，在发送端和接收端分别执行：

```powershell
Get-FileHash "$env:USERPROFILE\Downloads\ai-device-bridge-test-1GiB.bin" -Algorithm SHA256
Get-FileHash "$env:APPDATA\AI Device Bridge\Received\AI Device Bridge Inbox\ai-device-bridge-test-1GiB.bin" -Algorithm SHA256
```

两个哈希必须一致。测试后可删除这两个测试文件释放磁盘空间。此验证应在目标两台 Windows 电脑上执行；自动化测试使用较小文件验证了同一 TLS 上传、授权、落盘与哈希校验路径。

## 安全与数据范围

- 首次信任通过人工比较 TLS 指纹建立。接收授权码是长期共享凭据，请通过可信渠道传递。
- AI 查找只扫描用户选择目录中的文件名和元数据，不读文件正文；最多向配置的 Ollama 服务发送 200 个候选的相对路径、文件名、大小、修改时间以及用户描述。配置远程 Ollama 时这些信息会离开本机。
- AI 只推荐目录清单里的候选 ID，用户仍需手动选择文件和接收设备并确认计划；AI 不会触发发送。
- 尚不支持断点续传、单文件接收确认和远程撤销授权码。

## 项目结构

- `src/ai_device_bridge/`：桌面界面、节点 API、传输、AI 检索和 SQLite 仓储
- `tests/`：领域、API、仓储、AI 检索和双节点传输测试
- `scripts/`：Windows 开发、发布构建和大文件测试辅助脚本
- `installer/`：Inno Setup 安装器脚本
