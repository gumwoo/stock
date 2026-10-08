# 윈도우 작업 스케줄러에 stock-worker / stock-api / stock-web을 등록한다(현재 사용자, 로그인할 때 + 매일 06:30부터 30분마다).
# Claude 앱이 닫혀도 아침 작업(07:00~08:57)이 돌게 하려는 것이다(2026-10-07: 앱이 닫혀 그날 아침이 통째로 빠졌다).
# 다시 실행해도 된다(같은 이름은 덮어쓴다). 지우기: Unregister-ScheduledTask -TaskName stock-worker -Confirm:$false
# 코드를 바꾼 뒤 다시 띄우기: ops\restart.ps1 -Part worker|api|web|all
# 등록만 하고 시작하지는 않는다 — 처음 등록한 뒤에는 Start-ScheduledTask로 한 번 띄운다(다음부터는 로그인·30분 트리거가 띄운다).
# 꺼진 뒤 다시 띄우는 일은 run.ps1의 루프가 한다(작업 스케줄러의 재시작 설정은 실행 자체가 실패할 때만 쓰인다고 알려져 있어
# 루프로 대신한다 — 루프는 2026-10-07에 워커를 강제 종료해 실측했다).
#
# conhost --headless로 띄운다(2026-10-08). 10/7 밤 세 작업이 루프까지 함께 0xC000013A(CTRL_CLOSE)로 끝나 10/8 아침이 빠졌다.
# 그때 콘솔은 기본 터미널 위임으로 Windows Terminal 패키지의 OpenConsole.exe가 맡고 있었고, 같은 밤 그 패키지의 스토어 업데이트
# 시도가 세 번(18:18·01:56·04:28, 0x80073D02 "사용 중") 있었다 — 업데이트가 패키지 프로세스를 닫아 붙은 콘솔이 끝났다고
# 추정한다(직접 근거는 없음, 비슷한 이슈: github.com/microsoft/terminal/issues/6808). 시스템 conhost를 직접 띄우면 위임을 타지
# 않는다(실측: 새 OpenConsole 없음). 헤드리스라 창도 없다. 참고: 보안 탐지 규칙 일부가 "headless conhost → PowerShell"을
# 의심 행위로 본다(EDR을 깔면 예외가 필요할 수 있다). conhost 종료 코드는 자식 코드를 그대로 전하지 않는다(루프는 끝나지 않는다).
#
# 30분 트리거: 원인이 무엇이든 루프까지 죽으면 로그인 전까지 아무도 다시 띄우지 않았다. 매일 06:30부터 23시간 59분 동안
# 30분마다 시작을 시도하고, 이미 떠 있으면 IgnoreNew로 무시된다(중복 없음). 반복 끝(StopAtDurationEnd)에 실행 중인 루프를
# 끝내지 않는다(기본 False — 등록 뒤 확인한다).

$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "ops\run.ps1"
$user = "$env:USERDOMAIN\$env:USERNAME"

foreach ($part in @("worker", "api", "web")) {
    $action = New-ScheduledTaskAction -Execute "conhost.exe" `
        -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`" -Part $part" `
        -WorkingDirectory $root
    $logon = New-ScheduledTaskTrigger -AtLogOn -User $user
    $daily = New-ScheduledTaskTrigger -Daily -At 06:30
    $daily.Repetition = (New-ScheduledTaskTrigger -Once -At 06:30 `
            -RepetitionInterval (New-TimeSpan -Minutes 30) `
            -RepetitionDuration (New-TimeSpan -Hours 23 -Minutes 59)).Repetition
    # 기본값이 False라고 알려져 있지만 이 PC(PowerShell 5.1)에서는 True로 등록됐다(2026-10-08 실측). True면 반복이 끝날 때
    # 실행 중인 루프를 끝내 버리므로 명시한다.
    $daily.Repetition.StopAtDurationEnd = $false
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable -MultipleInstances IgnoreNew -Priority 4  # 4 = 보통(기본 7은 낮음이라 08:50 판정이 밀릴 수 있다)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName "stock-$part" -Action $action -Trigger @($logon, $daily) -Settings $settings `
        -Principal $principal -Description "stock 프로젝트 $part (ops/run.ps1, conhost --headless). 로그: logs\$part.log" -Force | Out-Null
    Write-Output "registered stock-$part"
}
