<#
.SYNOPSIS
    tossmon 무인 운영용 Windows 작업 스케줄러 등록/해제 스크립트 (소유: W5).

.DESCRIPTION
    3개의 예약 작업을 만든다:
      - tossmon-collector   : ops/supervisor.py 를 로그온 시 + 시스템 시작 시 실행(collector 상시 실행 + 크래시 재시작)
      - tossmon-healthcheck : ops/healthcheck.py 를 5분마다 실행, WARN/CRIT 이면 종료코드!=0 → 작업 실패로 기록됨
      - tossmon-logrotate   : ops/rotate_logs.py 를 매일 00:10에 실행

    기본 동작은 **DryRun**(실제로 아무 것도 등록하지 않고 계획만 출력)이다.
    실제로 등록하려면 `-Confirm:$true` 를 명시해야 한다 — 사용자 실행 환경(Task Scheduler)에
    영구적인 항목을 만드는 되돌리기 번거로운 작업이라, 이 스크립트를 실행하는 사람이 명시적으로
    승인해야 하도록 설계했다.

.PARAMETER Action
    Register | Unregister | Status

.PARAMETER Confirm
    실제 schtasks 호출 여부. 생략(기본 $false)이면 DryRun.

.PARAMETER RepoRoot
    리포 루트 경로. 기본값은 이 스크립트의 상위 디렉터리.

.EXAMPLE
    # 계획만 확인 (아무 것도 등록하지 않음)
    powershell -File ops/register_task_scheduler.ps1 -Action Register

.EXAMPLE
    # 실제 등록
    powershell -File ops/register_task_scheduler.ps1 -Action Register -Confirm:$true

.EXAMPLE
    powershell -File ops/register_task_scheduler.ps1 -Action Unregister -Confirm:$true
    powershell -File ops/register_task_scheduler.ps1 -Action Status
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Register", "Unregister", "Status")]
    [string]$Action,

    [bool]$Confirm = $false,

    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"

$TaskNames = @{
    Collector   = "tossmon-collector"
    Healthcheck = "tossmon-healthcheck"
    LogRotate   = "tossmon-logrotate"
}

function Get-PythonExe {
    $venvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) { return $venvPy }
    return "python"
}

function Get-TaskPlan {
    $py = Get-PythonExe
    return @(
        @{
            Name        = $TaskNames.Collector
            Description = "tossmon collector supervisor (crash 감시 + 자동 재시작)"
            Command     = "`"$py`" -m ops.supervisor --config ops\ops_config.yaml"
            Triggers    = @("AtStartup", "AtLogOn")
            Restart     = $true
        },
        @{
            Name        = $TaskNames.Healthcheck
            Description = "tossmon healthcheck — 5분마다 수집 상태/디스크 점검"
            Command     = "`"$py`" -m ops.healthcheck --config ops\ops_config.yaml"
            Triggers    = @("Every5Min")
            Restart     = $false
        },
        @{
            Name        = $TaskNames.LogRotate
            Description = "tossmon 로그 로테이션 — 매일 00:10"
            Command     = "`"$py`" -m ops.rotate_logs --config ops\ops_config.yaml"
            Triggers    = @("Daily0010")
            Restart     = $false
        }
    )
}

function Show-Plan($plan) {
    Write-Host "=== DryRun (아무 것도 등록하지 않음. -Confirm:`$true 로 실제 실행) ===" -ForegroundColor Yellow
    foreach ($t in $plan) {
        Write-Host ""
        Write-Host "Task: $($t.Name)"
        Write-Host "  Description : $($t.Description)"
        Write-Host "  WorkingDir  : $RepoRoot"
        Write-Host "  Command     : $($t.Command)"
        Write-Host "  Triggers    : $($t.Triggers -join ', ')"
        Write-Host "  AutoRestart : $($t.Restart)"
    }
    Write-Host ""
    Write-Host "참고: '로그온 시' 트리거는 무인 재부팅 시 자동 시작하려면 계정 자동 로그온 또는"
    Write-Host "'사용자 로그온 여부와 무관하게 실행'(-RunLevel/암호 저장, schtasks /RU /RP) 설정이 필요하다."
    Write-Host "암호 저장은 시크릿 취급 사항이므로 이 스크립트는 기본적으로 요구하지 않는다 — 필요하면"
    Write-Host "운영자가 수동으로 'schtasks /Change /TN <task> /RU <user> /RP' 를 실행할 것."
}

function New-Trigger($kind) {
    switch ($kind) {
        "AtStartup" { return New-ScheduledTaskTrigger -AtStartup }
        "AtLogOn" { return New-ScheduledTaskTrigger -AtLogOn }
        "Every5Min" {
            return New-ScheduledTaskTrigger -Once -At (Get-Date) `
                -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration ([TimeSpan]::MaxValue)
        }
        "Daily0010" { return New-ScheduledTaskTrigger -Daily -At "00:10" }
        default { throw "unknown trigger kind: $kind" }
    }
}

function Register-Tasks($plan) {
    foreach ($t in $plan) {
        $existing = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
        if ($existing) {
            Write-Host "이미 존재 — 교체: $($t.Name)"
            Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
        }
        $triggers = $t.Triggers | ForEach-Object { New-Trigger $_ }
        $action = New-ScheduledTaskAction -Execute "cmd.exe" `
            -Argument "/c cd /d `"$RepoRoot`" && $($t.Command)" `
            -WorkingDirectory $RepoRoot
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
        Register-ScheduledTask -TaskName $t.Name -Description $t.Description `
            -Action $action -Trigger $triggers -Settings $settings | Out-Null
        Write-Host "등록됨: $($t.Name)"
    }
}

function Unregister-Tasks($plan) {
    foreach ($t in $plan) {
        $existing = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
            Write-Host "해제됨: $($t.Name)"
        }
        else {
            Write-Host "없음(스킵): $($t.Name)"
        }
    }
}

function Show-Status($plan) {
    foreach ($t in $plan) {
        $existing = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
        if ($existing) {
            $info = Get-ScheduledTaskInfo -TaskName $t.Name
            Write-Host "$($t.Name): State=$($existing.State) LastRunTime=$($info.LastRunTime) LastResult=$($info.LastTaskResult)"
        }
        else {
            Write-Host "$($t.Name): (미등록)"
        }
    }
}

$plan = Get-TaskPlan

switch ($Action) {
    "Status" {
        Show-Status $plan
        return
    }
    "Register" {
        if (-not $Confirm) { Show-Plan $plan; return }
        Register-Tasks $plan
        return
    }
    "Unregister" {
        if (-not $Confirm) {
            Write-Host "DryRun: 다음 작업을 해제할 예정입니다 (-Confirm:`$true 로 실제 실행):"
            $plan | ForEach-Object { Write-Host "  - $($_.Name)" }
            return
        }
        Unregister-Tasks $plan
        return
    }
}
