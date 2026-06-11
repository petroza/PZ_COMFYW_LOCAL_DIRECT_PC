# Stops old PZ local_server.py instance so START_ALL can be run repeatedly.
$ErrorActionPreference = 'Continue'
$ThisPid = $PID
try {
    $procs = Get-CimInstance Win32_Process | Where-Object { ([string]$_.CommandLine) -match 'local_server\.py' }
    foreach ($p in $procs) {
        $procId = [int]$p.ProcessId
        if ($procId -gt 0 -and $procId -ne $ThisPid) { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }
    }
} catch {}
try {
    $conns = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction Stop
    foreach ($c in $conns) {
        $procId = [int]$c.OwningProcess
        if ($procId -gt 0 -and $procId -ne $ThisPid) { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }
    }
} catch {}
