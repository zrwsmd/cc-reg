# 启动和重启

本文档记录 `codex-console` 在 Windows PowerShell 下的常用启动、停止和重启命令。

## 项目目录

```powershell
cd E:\aaa\codex-console-main
```

## 前台启动

适合临时调试。命令执行后会占用当前终端窗口。

```powershell
python webui.py --host 127.0.0.1 --port 8000
```

启动后访问：

```text
http://127.0.0.1:8000/
http://localhost:8000/
```

## 后台启动

适合日常使用。命令执行后，Web UI 会作为独立进程留在后台运行。

```powershell
$py='C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe'
$cwd='E:\aaa\codex-console-main'
$cmd='\"' + $py + '\" webui.py --host 127.0.0.1 --port 8000'
Invoke-WmiMethod -Class Win32_Process -Name Create -ArgumentList $cmd, $cwd, $null
```

成功时会返回类似下面的信息：

```text
ReturnValue ProcessId
----------- ---------
0           29920
```

其中 `ProcessId` 就是后台进程 PID。

## 检查是否启动成功

```powershell
Invoke-WebRequest http://127.0.0.1:8000/ -UseBasicParsing
```

如果返回 `StatusCode : 200`，说明 Web UI 已正常启动。

## 查找当前 Web UI 进程

推荐按命令行过滤，避免误伤其他 Python 进程。

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*webui.py*' } |
  Select-Object ProcessId, CommandLine
```

## 停止 Web UI

先查到 PID，再停止：

```powershell
Stop-Process -Id 29920 -Force
```

请把 `29920` 替换成你当前实际查到的 PID。

## 重启 Web UI

### 方式一：分两步重启

1. 先查 PID

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*webui.py*' } |
  Select-Object ProcessId, CommandLine
```

2. 停止旧进程

```powershell
Stop-Process -Id <旧PID> -Force
```

3. 重新后台启动

```powershell
$py='C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe'
$cwd='E:\aaa\codex-console-main'
$cmd='\"' + $py + '\" webui.py --host 127.0.0.1 --port 8000'
Invoke-WmiMethod -Class Win32_Process -Name Create -ArgumentList $cmd, $cwd, $null
```

### 方式二：一段脚本完成重启

```powershell
$existing = Get-CimInstance Win32_Process |
  Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*webui.py*' }

if ($existing) {
  $existing | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}

$py='C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe'
$cwd='E:\aaa\codex-console-main'
$cmd='\"' + $py + '\" webui.py --host 127.0.0.1 --port 8000'
Invoke-WmiMethod -Class Win32_Process -Name Create -ArgumentList $cmd, $cwd, $null
```

## 重启后验证

```powershell
Invoke-WebRequest http://localhost:8000/ -UseBasicParsing
```

如果返回 `200`，说明重启完成。

## 常见说明

- 如果你是通过代理工具访问本地页面，建议让 `localhost` 和 `127.0.0.1` 直连，否则前端 `WebSocket` 监控可能反复断开。
- 如果端口 `8000` 已被占用，可以改成别的端口，例如：

```powershell
python webui.py --host 127.0.0.1 --port 8080
```

