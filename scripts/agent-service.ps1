[CmdletBinding()]
param(
    [ValidateSet('EnableAgent', 'DisableAgent', 'ClosePanel', 'EnableDshWeb', 'DisableDshWeb', 'Install', 'Update', 'Status')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
$dataServer = Join-Path $projectRoot 'scripts\stock_data_server.py'
$reportServer = Join-Path $projectRoot 'scripts\report_web_server.py'
$pushScript = Join-Path $projectRoot 'scripts\daily_push.py'
$pushConfig = Join-Path $projectRoot 'config\daily-push.json'
$pushConfigExample = Join-Path $projectRoot 'config\daily-push.example.json'
$taskInstaller = Join-Path $projectRoot 'scripts\install-daily-task.ps1'
$componentInstaller = Join-Path $projectRoot 'scripts\install-components.ps1'
$updater = Join-Path $projectRoot 'scripts\update-project.ps1'
$dailyTask = 'DSH A-Share Pre-open Research'
$reportTask = 'DSH A-Share Report Site'
$factorTask = 'DSH A-Share Weekly Factor Training'

function Test-PortListening([int]$Port) {
    return $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Start-ProjectPythonService([string]$Script, [int]$Port, [string]$Label) {
    if (Test-PortListening $Port) {
        return
    }
    Start-Process -FilePath $pythonExe -ArgumentList @($Script) -WorkingDirectory $projectRoot -WindowStyle Hidden
    foreach ($attempt in 1..15) {
        if (Test-PortListening $Port) {
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "$Label did not start on port $Port."
}

function Stop-ProjectPort([int]$Port, [string]$ExpectedCommand, [string]$Label) {
    $processIds = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($processId in $processIds) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
        if ($process -and $process.CommandLine -like "*$ExpectedCommand*") {
            Stop-Process -Id $processId -Force -ErrorAction Stop
            Write-Host "Stopped $Label."
        }
    }
}

function Set-TaskEnabled([string]$TaskName, [bool]$Enabled) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        return
    }
    if ($Enabled) {
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
    } else {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
    }
}

function Get-TaskState([string]$TaskName) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        return 'Missing'
    }
    return [string]$task.State
}

function Start-DshWebProcess([string]$CommandPath) {
    $logDirectory = Join-Path $projectRoot 'data'
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $stdoutLog = Join-Path $logDirectory 'dsh-web.stdout.log'
    $stderrLog = Join-Path $logDirectory 'dsh-web.stderr.log'
    $escapedPath = $CommandPath.Replace("'", "''")
    $launchCommand = "& '$escapedPath' web --no-open"
    $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($launchCommand))
    Write-Host "Launching DSH command: $CommandPath web --no-open"
    $process = Start-Process -FilePath 'powershell.exe' -ArgumentList @(
        '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encodedCommand
    ) -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru
    return $process
}

function Write-DshStartupLogs {
    $stdoutLog = Join-Path $projectRoot 'data\dsh-web.stdout.log'
    $stderrLog = Join-Path $projectRoot 'data\dsh-web.stderr.log'
    foreach ($logPath in @($stdoutLog, $stderrLog)) {
        if (Test-Path -LiteralPath $logPath) {
            $lines = Get-Content -LiteralPath $logPath -Tail 30 -ErrorAction SilentlyContinue
            if ($lines) {
                Write-Host "--- $(Split-Path -Leaf $logPath) ---"
                $lines | ForEach-Object { Write-Host $_ }
            }
        }
    }
}

switch ($Action) {
    'EnableAgent' {
        if (-not (Test-Path -LiteralPath $pythonExe)) {
            throw "Python environment not found: $pythonExe. Run install-and-start.ps1 once first."
        }
        if (-not (Test-Path -LiteralPath $pushConfig)) {
            Copy-Item -LiteralPath $pushConfigExample -Destination $pushConfig
        }
        & $taskInstaller
        Set-TaskEnabled $dailyTask $true
        Set-TaskEnabled $reportTask $true
        Set-TaskEnabled $factorTask $true
        Start-ProjectPythonService $dataServer 8765 'Local stock-data service'
        Start-ProjectPythonService $reportServer 8766 'Report website'
        Write-Host 'Agent enabled: daily report tasks are active. DSH Web was not started.'
    }
    'DisableAgent' {
        Set-TaskEnabled $dailyTask $false
        Set-TaskEnabled $reportTask $false
        Set-TaskEnabled $factorTask $false
        Stop-ProjectPort 8766 $reportServer 'report website'
        $pushProcesses = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -like "*$pushScript*" }
        foreach ($process in $pushProcesses) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
        if (-not (Test-PortListening 3080)) {
            Stop-ProjectPort 8765 $dataServer 'local stock-data service'
            Write-Host 'Agent disabled: daily report tasks and local agent services have stopped.'
        } else {
            Write-Host 'Agent disabled: daily report tasks have stopped. Data service remains available for DSH Web.'
        }
    }
    'ClosePanel' {
        Set-TaskEnabled $dailyTask $false
        Set-TaskEnabled $reportTask $false
        Stop-ProjectPort 8766 $reportServer 'report website'
        $pushProcesses = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -like "*$pushScript*" }
        foreach ($process in $pushProcesses) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
        if (-not (Test-PortListening 3080)) {
            Stop-ProjectPort 8765 $dataServer 'local stock-data service'
        }
        Write-Host 'Control panel closed; weekly factor training remains scheduled.'
    }
    'EnableDshWeb' {
        if (-not (Test-Path -LiteralPath $pythonExe)) {
            throw "Python environment not found: $pythonExe. Run install-and-start.ps1 once first."
        }
        Start-ProjectPythonService $dataServer 8765 'Local stock-data service'
        if (Test-PortListening 3080) {
            Write-Host 'DSH Web is already running.'
            break
        }
        $dsh = Get-Command dsh.cmd -CommandType Application -ErrorAction SilentlyContinue
        if (-not $dsh) {
            $dsh = Get-Command dsh -ErrorAction SilentlyContinue
        }
        if (-not $dsh) {
            throw 'dsh command was not found. Run install-and-start.ps1 once first.'
        }
        $dshProcess = Start-DshWebProcess $dsh.Source
        foreach ($attempt in 1..30) {
            if (Test-PortListening 3080) {
                Write-Host 'DSH Web is running.'
                break
            }
            if ($dshProcess.HasExited -and $attempt -ge 5) {
                break
            }
            Start-Sleep -Seconds 1
        }
        if (-not (Test-PortListening 3080)) {
            if (-not $dshProcess.HasExited) {
                Stop-Process -Id $dshProcess.Id -Force -ErrorAction SilentlyContinue
                Start-Sleep -Milliseconds 250
            }
            Write-DshStartupLogs
            throw 'DSH Web did not start within 30 seconds.'
        }
    }
    'DisableDshWeb' {
        $processIds = Get-NetTCPConnection -LocalPort 3080 -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($processId in $processIds) {
            $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
            if ($process -and $process.CommandLine -match '(?i)@deepseek-ai[\\/]dsh|\bdsh(?:\.cmd|\.js)?\b') {
                Stop-Process -Id $processId -Force -ErrorAction Stop
                Write-Host 'DSH Web stopped.'
            }
        }
        if ((Get-TaskState $dailyTask) -notin @('Ready', 'Running')) {
            Stop-ProjectPort 8765 $dataServer 'local stock-data service'
        }
    }
    'Install' {
        Set-TaskEnabled $dailyTask $false
        Set-TaskEnabled $reportTask $false
        Set-TaskEnabled $factorTask $false
        & (Join-Path $projectRoot 'scripts\stop-services.ps1')
        & $componentInstaller
    }
    'Update' {
        & $updater
    }
    'Status' {
        Write-Host '[Check 1/4] Reading Windows scheduled tasks...'
        $agentTaskState = Get-TaskState $dailyTask
        $reportTaskState = Get-TaskState $reportTask
        $factorTaskState = Get-TaskState $factorTask
        Write-Host '[Check 2/4] Checking service ports 8765, 8766, and 3080...'
        $dataServiceRunning = Test-PortListening 8765
        $reportSiteRunning = Test-PortListening 8766
        $dshWebRunning = Test-PortListening 3080
        Write-Host '[Check 3/4] Checking Node.js, Python, Git, and the virtual environment...'
        $nodeAvailable = $null -ne (Get-Command node -ErrorAction SilentlyContinue)
        $pythonAvailable = $null -ne (Get-Command python -ErrorAction SilentlyContinue)
        $gitAvailable = $null -ne (Get-Command git -ErrorAction SilentlyContinue)
        $gitCheckout = Test-Path -LiteralPath (Join-Path $projectRoot '.git')
        $venvReady = Test-Path -LiteralPath $pythonExe
        $dshAvailable = $null -ne (Get-Command dsh -ErrorAction SilentlyContinue)
        Write-Host '[Check 4/4] Checking the DSH finance plugin...'
        $manifestPath = Join-Path $env:USERPROFILE '.dsh\profiles\web\package.json'
        $pluginInstalled = $false
        if (Test-Path -LiteralPath $manifestPath) {
            try {
                $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
                $pluginInstalled = $null -ne $manifest.dependencies.'dsh-finance-agent'
            } catch {
                $pluginInstalled = $false
            }
        }
        [pscustomobject]@{
            agent_task = $agentTaskState
            report_task = $reportTaskState
            factor_task = $factorTaskState
            data_service = $dataServiceRunning
            report_site = $reportSiteRunning
            dsh_web = $dshWebRunning
            node_available = $nodeAvailable
            python_available = $pythonAvailable
            git_available = $gitAvailable
            git_checkout = $gitCheckout
            venv_ready = $venvReady
            dsh_available = $dshAvailable
            plugin_installed = $pluginInstalled
        } | ConvertTo-Json -Compress
    }
}
