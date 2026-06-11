# PZ ComfyW - safe ComfyUI API restart
# Starts ComfyUI only as a Python/API backend on 127.0.0.1:8000.
# It does not start the Comfy Desktop GUI.

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LogDir = Join-Path $Root 'data\logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir 'comfy_safe_restart.log'

function LogLine([string]$Text) {
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Text
    Write-Host $line
    Add-Content -LiteralPath $Log -Encoding UTF8 -Value $line
}

function Expand-PathValue([string]$PathValue) {
    if ([string]::IsNullOrWhiteSpace($PathValue)) { return '' }
    return [Environment]::ExpandEnvironmentVariables($PathValue.Trim())
}

function Get-ConfigValue($Object, [string]$Name, [string]$Default = '') {
    if ($null -ne $Object -and $Object.PSObject.Properties.Name -contains $Name) {
        $v = [string]$Object.$Name
        if (-not [string]::IsNullOrWhiteSpace($v)) { return (Expand-PathValue $v) }
    }
    return (Expand-PathValue $Default)
}

function First-ExistingPath([string[]]$Candidates) {
    foreach ($p0 in $Candidates) {
        $p = Expand-PathValue $p0
        if (-not [string]::IsNullOrWhiteSpace($p) -and (Test-Path -LiteralPath $p)) {
            return (Resolve-Path -LiteralPath $p).Path
        }
    }
    return $null
}

function Quote-Arg([string]$Arg) {
    if ($Arg -match '[\s&()\[\]{}^=;!,''+`~]') {
        return '"' + ($Arg -replace '"','\"') + '"'
    }
    return $Arg
}

function Stop-ByProcessId([int]$ProcessIdToStop, [string]$Reason) {
    if ($ProcessIdToStop -le 0) { return }
    if ($ProcessIdToStop -eq $PID) { return }
    try {
        $p = Get-Process -Id $ProcessIdToStop -ErrorAction Stop
        LogLine ("STOP PID {0} ({1}) - {2}" -f $ProcessIdToStop, $p.ProcessName, $Reason)
        Stop-Process -Id $ProcessIdToStop -Force -ErrorAction Stop
    } catch {
        LogLine ("WARN cannot stop PID {0}: {1}" -f $ProcessIdToStop, $_.Exception.Message)
    }
}

function Test-ComfyApi([int]$Port) {
    try {
        $r = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/queue" -f $Port) -UseBasicParsing -TimeoutSec 3
        return ($r.StatusCode -lt 400)
    } catch {
        return $false
    }
}

# ---- Load config -----------------------------------------------------------
$configPath = Join-Path $Root 'config.json'
$config = $null
try { $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
$cs = $null
if ($null -ne $config -and $config.PSObject.Properties.Name -contains 'comfy_start') { $cs = $config.comfy_start }

$PortText = Get-ConfigValue $cs 'port' '8000'
[int]$Port = 8000
try { $Port = [int]$PortText } catch { $Port = 8000 }

$BaseDir = Get-ConfigValue $cs 'base_dir' '%USERPROFILE%\Documents\ComfyUI'
$UserDir = Get-ConfigValue $cs 'user_dir' '%USERPROFILE%\Documents\ComfyUI\user'
$InputDir = Get-ConfigValue $cs 'input_dir' '%USERPROFILE%\Documents\ComfyUI\input'
$OutputDir = Get-ConfigValue $cs 'output_dir' '%USERPROFILE%\Documents\ComfyUI\output'
$ModelPaths = Get-ConfigValue $cs 'model_paths' '%APPDATA%\Comfy Desktop\shared_model_paths.yaml'
$DatabaseUrl = Get-ConfigValue $cs 'database_url' ('sqlite:///' + $UserDir + '\comfyui.db')
$ExtraArgsRaw = Get-ConfigValue $cs 'extra_args' '--disable-mmap --disable-dynamic-vram'
$ForceKillPortOwnerText = Get-ConfigValue $cs 'force_kill_port_8000' 'true'
$ForceKillPortOwner = ($ForceKillPortOwnerText -match '^(1|true|yes|ano)$')

LogLine '============================================================'
LogLine 'PZ COMFYW - SAFE COMFY API RESTART'
LogLine ('Root: {0}' -f $Root)
LogLine ('Port: {0}' -f $Port)
LogLine 'Mode: API backend only, no Comfy Desktop GUI'

# ---- Stop old Comfy safely ------------------------------------------------
if (Test-ComfyApi $Port) {
    LogLine 'Comfy API is online - sending /interrupt before restart.'
    try { Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/interrupt" -f $Port) -Method Post -TimeoutSec 3 | Out-Null } catch {}
    Start-Sleep -Seconds 2
}

LogLine 'Closing old Comfy processes.'
try {
    $all = Get-CimInstance Win32_Process | Where-Object {
        $name = [string]$_.Name
        $cmd = [string]$_.CommandLine
        (($name -match '(?i)^ComfyUI\.exe$|^Comfy Desktop\.exe$') -or
         ($cmd -match '(?i)ComfyUI.*main\.py|main\.py.*ComfyUI|--port\s+8000|Comfy Desktop'))
    }
    foreach ($proc in $all) {
        Stop-ByProcessId ([int]$proc.ProcessId) 'old Comfy/Desktop/API process'
    }
} catch {
    LogLine ('WARN process scan failed: ' + $_.Exception.Message)
}

Start-Sleep -Seconds 2

# Free the selected API port. This fixes the common case where an old backend
# stays in memory and blocks 127.0.0.1:8000.
try {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
    foreach ($c in $conns) {
        $owner = [int]$c.OwningProcess
        if ($owner -le 0) { continue }
        $pinfo = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $owner) -ErrorAction SilentlyContinue
        $desc = (([string]$pinfo.Name) + ' ' + ([string]$pinfo.CommandLine))
        if ($desc -match '(?i)Comfy|python|main\.py|--port\s+8000' -or $ForceKillPortOwner) {
            Stop-ByProcessId $owner ("owner of port {0}" -f $Port)
        } else {
            LogLine ("WARN port {0} is held by non-Comfy process PID {1}; not killing it." -f $Port, $owner)
        }
    }
} catch {
    # Older Windows sometimes lacks Get-NetTCPConnection; use netstat fallback.
    try {
        $lines = netstat -ano | Select-String (":" + $Port + " ") | Select-String 'LISTENING'
        foreach ($line in $lines) {
            $parts = ($line.ToString() -split '\s+') | Where-Object { $_ }
            $ownerText = $parts[-1]
            [int]$owner = 0
            if ([int]::TryParse($ownerText, [ref]$owner)) { Stop-ByProcessId $owner ("owner of port {0}" -f $Port) }
        }
    } catch {}
}

Start-Sleep -Seconds 2

# ---- Find Comfy main.py ----------------------------------------------------
$ConfiguredMain = Get-ConfigValue $cs 'main_py' ''
$MainPy = First-ExistingPath @(
    $ConfiguredMain,
    '%USERPROFILE%\Documents\ComfyUI\main.py',
    '%USERPROFILE%\Documents\ComfyUI\ComfyUI\main.py',
    '%USERPROFILE%\ComfyUI-Installs\ComfyUI\main.py',
    '%USERPROFILE%\ComfyUI-Installs\ComfyUI\ComfyUI\main.py',
    '%LOCALAPPDATA%\Programs\@comfyorgcomfyui-electron\resources\ComfyUI\main.py'
)

if (-not $MainPy) {
    LogLine '[ERROR] ComfyUI main.py was not found.'
    LogLine 'Set comfy_start.main_py in config.json to your real ComfyUI main.py path.'
    exit 10
}
$ComfyRoot = Split-Path -Parent $MainPy

# ---- Find Python -----------------------------------------------------------
$ConfiguredPython = Get-ConfigValue $cs 'python_exe' ''
$PythonExe = First-ExistingPath @(
    $ConfiguredPython,
    (Join-Path $BaseDir '.venv\Scripts\python.exe'),
    (Join-Path $ComfyRoot '.venv\Scripts\python.exe'),
    (Join-Path (Split-Path -Parent $ComfyRoot) '.venv\Scripts\python.exe'),
    (Join-Path $BaseDir 'python_embeded\python.exe'),
    (Join-Path $ComfyRoot 'python_embeded\python.exe')
)
$PythonPrefixArgs = @()
if (-not $PythonExe) {
    $pyCmd = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyCmd) {
        $PythonExe = $pyCmd.Source
        $PythonPrefixArgs = @('-3')
    } else {
        $pythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($pythonCmd) { $PythonExe = $pythonCmd.Source }
    }
}

if (-not $PythonExe) {
    LogLine '[ERROR] Python was not found. Set comfy_start.python_exe in config.json.'
    exit 11
}

# Ensure folders exist. These are Comfy folders, not app folders.
foreach ($dir in @($BaseDir, $UserDir, $InputDir, $OutputDir)) {
    if (-not [string]::IsNullOrWhiteSpace($dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
}

# ---- Start API-only backend ------------------------------------------------
$args = @()
$args += $PythonPrefixArgs
$args += '-s'
$args += $MainPy
$args += '--listen'
$args += '127.0.0.1'
$args += '--port'
$args += ([string]$Port)
if (Test-Path -LiteralPath $BaseDir) { $args += @('--base-directory', $BaseDir) }
if (Test-Path -LiteralPath $UserDir) { $args += @('--user-directory', $UserDir) }
if (-not [string]::IsNullOrWhiteSpace($DatabaseUrl)) { $args += @('--database-url', $DatabaseUrl) }
if (Test-Path -LiteralPath $ModelPaths) { $args += @('--extra-model-paths-config', $ModelPaths) }
if (Test-Path -LiteralPath $InputDir) { $args += @('--input-directory', $InputDir) }
if (Test-Path -LiteralPath $OutputDir) { $args += @('--output-directory', $OutputDir) }
if (-not [string]::IsNullOrWhiteSpace($ExtraArgsRaw)) {
    $args += ($ExtraArgsRaw -split '\s+' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

$argLine = ($args | ForEach-Object { Quote-Arg ([string]$_) }) -join ' '
LogLine ('main.py: ' + $MainPy)
LogLine ('python:  ' + $PythonExe)
LogLine ('args:    ' + $argLine)
LogLine 'Starting ComfyUI API backend in a separate window.'

try {
    Start-Process -FilePath $PythonExe -ArgumentList $argLine -WorkingDirectory $ComfyRoot -WindowStyle Normal
} catch {
    LogLine ('[ERROR] Cannot start ComfyUI: ' + $_.Exception.Message)
    exit 12
}

# ---- Wait for API ----------------------------------------------------------
for ($i=1; $i -le 120; $i++) {
    if (Test-ComfyApi $Port) {
        LogLine 'Comfy API is ready.'
        exit 0
    }
    Start-Sleep -Seconds 1
}

LogLine '[WARN] Comfy API did not answer within 120 seconds. Check the Comfy window/log.'
exit 2
