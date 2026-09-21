# restart-dsh-web.ps1
# 用途：重启 dsh web（PID 由 3080 端口动态探测），以便加载新安装的 dsh-task-notify 插件。
# 本脚本由 WMI (Win32_Process.Create) 启动，父进程是 WmiPrvSE，脱离 DSH 进程树，
# 因此杀掉 dsh web 不会连带杀掉本脚本。
# 每次执行 append 到 dsh-web-restart.log。

$ErrorActionPreference = 'SilentlyContinue'
$log = 'C:\Users\yiding\Documents\ChatGPT\CNS规划系统\dsh-web-restart.log'

function Log([string]$m) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Out-File -LiteralPath $log -Append -Encoding utf8
}

$node = 'C:\Program Files\nodejs\node.exe'
$bin  = 'D:\tools\npm-cache\_npx\1e7f6d9597241db0\node_modules\@deepseek-ai\dsh\lib\bin.js'
$wd   = 'C:\Users\yiding\Documents\ChatGPT\CNS规划系统'
$port = 3080

$listenPattern = ":$port\s+0\.0\.0\.0:0\s+LISTENING"

function Get-ListenerPid {
    $line = netstat -ano | Select-String $listenPattern | Select-Object -First 1
    if (-not $line) { return $null }
    return [int](($line.ToString() -split '\s+')[-1])
}

Log '=== 重启任务开始（看门脚本 PID ' + $PID + '）==='
Log "等待 45 秒，让正在生成的回复先发出去"
Start-Sleep -Seconds 45

$old = Get-ListenerPid
if ($old) {
    Log "停止旧 dsh web 进程 PID $old"
    Stop-Process -Id $old -Force
    Start-Sleep -Seconds 4
    if (Get-ListenerPid) { Log "警告：停止后 $port 仍被占用" } else { Log "$port 已释放" }
} else {
    Log "未发现监听 $port 的进程，直接启动"
}

Log '启动新的 dsh web'
$p = Start-Process -FilePath $node `
    -ArgumentList @($bin, 'web', '--port', "$port") `
    -WorkingDirectory $wd `
    -WindowStyle Hidden `
    -PassThru
Log "新进程 PID = $($p.Id)"

$ok = $false
for ($i = 1; $i -le 30; $i++) {
    Start-Sleep -Seconds 2
    $newPid = Get-ListenerPid
    if ($newPid) { Log "$port 已监听（等待约 $($i * 2) 秒，PID $newPid）"; $ok = $true; break }
}
if (-not $ok) { Log '启动失败：60 秒内未监听端口，请手动重启' }
Log '=== 重启任务结束 ==='
