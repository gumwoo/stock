# 워커·API·화면을 Claude 앱과 상관없이 띄운다. 윈도우 작업 스케줄러가 로그인할 때 부른다(ops/register-tasks.ps1).
# 경로에 공백이 없다는 전제(C:/Users/GUNWOO/Documents/stock). 공백이 생기면 따옴표 처리를 다시 볼 것.
# 사용: powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ops\run.ps1 -Part worker|api|web
# 출력은 logs\<part>.log에 덧붙인다(cmd 리다이렉트: PowerShell 5.1의 >>는 UTF-16으로 써서 쓰지 않는다).
# 프로세스가 끝나면(오류든 아니든) 60초 뒤 이 스크립트가 다시 띄운다 — 작업 스케줄러의 "실패 시 다시 시작"은 프로그램이
# 오류 코드로 끝난 경우에는 동작하지 않아서다. Stop-ScheduledTask는 이 PowerShell까지 끝내므로 루프도 함께 멈춘다.
param([Parameter(Mandatory = $true)][ValidateSet("worker", "api", "web")][string]$Part)

$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$log = Join-Path $logs "$Part.log"
$env:PYTHONUTF8 = "1"
$env:NO_COLOR = "1"
$python = Join-Path $root "backend\.venv\Scripts\python.exe"

while ($true) {
    Add-Content -Path $log -Value ("==== start " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " ====") -Encoding utf8
    switch ($Part) {
        "worker" {
            Set-Location (Join-Path $root "backend")
            cmd.exe /c "$python -m app.worker >> $log 2>&1"
        }
        "api" {
            Set-Location $root
            cmd.exe /c "$python -m uvicorn app.main:app --app-dir backend --port 8000 >> $log 2>&1"
        }
        "web" {
            Set-Location $root
            cmd.exe /c "npm.cmd --prefix frontend run dev >> $log 2>&1"
        }
    }
    $code = $LASTEXITCODE
    Add-Content -Path $log -Value ("==== exit " + $code + " at " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + ", restarting in 60s ====") -Encoding utf8
    Start-Sleep -Seconds 60
}
