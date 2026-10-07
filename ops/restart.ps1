# 작업 스케줄러로 띄운 stock 프로세스를 다시 띄운다. 코드를 바꾼 뒤 이것을 쓴다.
# 사용: powershell -NoProfile -ExecutionPolicy Bypass -File ops\restart.ps1 -Part worker|api|web|all
# Stop-ScheduledTask는 run.ps1(PowerShell)만 끝내고 그 아래 python·node는 남긴다(2026-10-07 실측). 남은 것을 그대로 두고
# 다시 시작하면 워커가 두 벌 돈다(KIS·LLM 호출이 두 번). 그래서 (1) 작업을 멈추고 (2) run.ps1 루프 PowerShell을 명령줄로 찾아
# 나무째 끝내고(멈춤이 늦거나 손으로 띄운 루프가 60초 뒤 다시 띄우지 않게) (3) 남은 자식을 전체 경로로 찾아 끝낸 뒤 시작한다.
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
    Start-ScheduledTask -TaskName "stock-$p"
}
Start-Sleep -Seconds 3
Get-ScheduledTask -TaskName "stock-*" | Select-Object TaskName, State | Format-Table -AutoSize
Write-Output "PowerShell이 뜨는 데 1분쯤 걸릴 수 있다. 확인: Get-CimInstance Win32_Process -Filter ""CommandLine like '%app.worker%'"""
