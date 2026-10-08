# 작업 스케줄러로 띄운 stock 프로세스를 다시 띄운다. 코드를 바꾼 뒤 이것을 쓴다.
# 사용: powershell -NoProfile -ExecutionPolicy Bypass -File ops\restart.ps1 -Part worker|api|web|all
# Stop-ScheduledTask는 run.ps1(PowerShell)만 끝내고 그 아래 python·node는 남긴다(2026-10-07 실측, conhost 전환 전 — 지금은 작업 프로세스가 conhost라 다를 수 있다). 남은 것을 그대로 두고
# 다시 시작하면 워커가 두 벌 돈다(KIS·LLM 호출이 두 번). 그래서 (1) 작업을 멈추고 (2) run.ps1 루프 PowerShell을 명령줄로 찾아
# 나무째 끝내고(멈춤이 늦거나 손으로 띄운 루프가 60초 뒤 다시 띄우지 않게) (3) 남은 자식을 전체 경로로 찾아 끝낸 뒤 시작한다.
# 2026-10-08부터 작업은 conhost --headless로 뜬다. 루프 탐지는 conhost와 powershell 명령줄 둘 다에 걸린다(나무째 끝내므로 무해).
param([ValidateSet("worker", "api", "web", "all")][string]$Part = "all")

$root = Split-Path -Parent $PSScriptRoot
$python = [regex]::Escape((Join-Path $root "backend\.venv\Scripts\python.exe"))
$patterns = @{
    worker = "$python\s+-m app\.worker"
    api    = "$python\s+-m uvicorn app\.main:app"
    web    = [regex]::Escape((Join-Path $root "logs\web.log")) + "|" + [regex]::Escape((Join-Path $root "frontend\node_modules"))
}
$loop = [regex]::Escape((Join-Path $root "ops\run.ps1"))
$parts = if ($Part -eq "all") { @("worker", "api", "web") } else { @($Part) }

function Stop-Tree([object[]]$procs) {
    foreach ($proc in $procs) {
        if ($proc.ProcessId -eq $PID) { continue }
        # /T로 앞에서 이미 끝난 자식이면 "not found"가 나온다 — 정상이라 숨긴다.
        cmd.exe /c "taskkill /PID $($proc.ProcessId) /T /F >nul 2>&1"
    }
}

foreach ($p in $parts) {
    Stop-ScheduledTask -TaskName "stock-$p" -ErrorAction SilentlyContinue
    $all = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine }
    # 전체 경로든 상대 경로(ops\run.ps1)든 이 저장소의 run.ps1 루프를 찾는다.
    $loops = @($all | Where-Object {
            $_.CommandLine -match ($loop + '"?\s+-Part ' + $p + '\b') -or
            $_.CommandLine -match ('(^|[\\"\s])ops\\run\.ps1"?\s+-Part ' + $p + '\b')
        })
    Stop-Tree $loops
    Start-Sleep -Seconds 1
    $left = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match $patterns[$p] })
    Stop-Tree $left
    Write-Output ("stock-" + $p + ": stopped (loops " + $loops.Count + ", left-over processes " + $left.Count + ")")
}
Start-Sleep -Seconds 3
foreach ($p in $parts) {
    # 작업이 아직 Running으로 보이면 MultipleInstances IgnoreNew 때문에 시작이 조용히 무시된다(2026-10-08 실측: conhost로
    # 바꾼 뒤 restart가 아무것도 띄우지 못했다). Running이 풀릴 때까지 기다렸다가 시작하고, 시작됐는지 확인해 한 번 더 시도한다.
    # 확인은 작업 State가 아니라 실제 루프 프로세스로 한다(프로세스가 없는데 Running으로 남는 경우가 있었다).
    $mine = $loop + '"?\s+-Part ' + $p + '\b'
    $started = $false
    for ($try = 0; $try -lt 2 -and -not $started; $try++) {
        for ($i = 0; $i -lt 30 -and (Get-ScheduledTask -TaskName "stock-$p").State -eq "Running"; $i++) {
            Start-Sleep -Seconds 1
        }
        if ((Get-ScheduledTask -TaskName "stock-$p").State -eq "Running") {
            Stop-ScheduledTask -TaskName "stock-$p" -ErrorAction SilentlyContinue  # 프로세스는 이미 끝냈다
            Start-Sleep -Seconds 2
        }
        Start-ScheduledTask -TaskName "stock-$p"
        for ($i = 0; $i -lt 15 -and -not $started; $i++) {
            Start-Sleep -Seconds 1
            $started = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match $mine }).Count -gt 0
        }
    }
    if (-not $started) { Write-Output ("stock-" + $p + ": NOT STARTED - Get-ScheduledTask stock-" + $p + " 로 상태를 확인할 것") }
}
Get-ScheduledTask -TaskName "stock-*" | Select-Object TaskName, State | Format-Table -AutoSize
Write-Output "python·node가 뜨는 데 수십 초 걸릴 수 있다. 확인: Get-CimInstance Win32_Process -Filter ""CommandLine like '%app.worker%'"""
