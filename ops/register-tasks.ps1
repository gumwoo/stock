# 윈도우 작업 스케줄러에 stock-worker / stock-api / stock-web을 등록한다(현재 사용자, 로그인할 때 시작).
# Claude 앱이 닫혀도 아침 작업(07:00~08:57)이 돌게 하려는 것이다(2026-10-07: 앱이 닫혀 그날 아침이 통째로 빠졌다).
# 다시 실행해도 된다(같은 이름은 덮어쓴다). 지우기: Unregister-ScheduledTask -TaskName stock-worker -Confirm:$false
# 코드를 바꾼 뒤 다시 띄우기: ops\restart.ps1 -Part worker|api|web|all (Stop-ScheduledTask만 하면 python·node가 남는다)
# 등록만 하고 시작하지는 않는다 — 처음 등록한 뒤에는 Start-ScheduledTask로 한 번 띄운다(다음부터는 로그인할 때 저절로).
# 꺼진 뒤 다시 띄우는 일은 run.ps1의 루프가 한다(작업 스케줄러의 재시작 설정은 실행 자체가 실패할 때만 쓰인다고 알려져 있어
# 루프로 대신한다 — 루프는 2026-10-07에 워커를 강제 종료해 실측했다).

$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "ops\run.ps1"
$user = "$env:USERDOMAIN\$env:USERNAME"

foreach ($part in @("worker", "api", "web")) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -Part $part" `
        -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable -MultipleInstances IgnoreNew -Priority 4  # 4 = 보통(기본 7은 낮음이라 08:50 판정이 밀릴 수 있다)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName "stock-$part" -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description "stock 프로젝트 $part (ops/run.ps1). 로그: logs\$part.log" -Force | Out-Null
    Write-Output "registered stock-$part"
}
