<#
.SYNOPSIS
    Sandbox self-test for ops/watchdog.ps1 - owner: W5. ASCII-only (same reason as the
    watchdog itself: Windows PowerShell 5.1 parses BOM-less scripts as ANSI).

.DESCRIPTION
    Runs the real ops/watchdog.ps1 as a child process against a throwaway DataDir full of
    synthetic collector_state.json / collector.log fixtures, then asserts on what it wrote:
    exit code, ALERT_/NOTE_ files, and data/watchdog.log lines.

    It NEVER touches the live collector: -DryRunRestart makes a restart a recorded
    intention instead of an action, the process patterns match two dummy powershell.exe
    processes this script starts and kills itself, and DataDir/StateDir point into $env:TEMP.

    Two of these cases are the 2026-08-04 08:55 incident replayed byte for byte (F1/F2),
    and the rest exist because "I added a guard" is worth nothing unless the guard is also
    shown to still let the REAL fault through (T1/T2/T3).

        powershell -NoProfile -ExecutionPolicy Bypass -File ops\watchdog_selftest.ps1

    Exit code 0 = all cases passed, 1 = at least one failed.
#>
param(
    [string]$Watchdog = (Join-Path $PSScriptRoot "watchdog.ps1"),
    [switch]$KeepSandbox
)

$ErrorActionPreference = "Stop"
$script:Pass = 0
$script:Fail = 0
$script:Failures = @()

$Root = Join-Path $env:TEMP ("wdtest_" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
$SUP_TAG = "WDSELFTEST_SUPERVISOR_" + [Guid]::NewGuid().ToString("N").Substring(0, 6)
$COL_TAG = "WDSELFTEST_COLLECTOR_" + [Guid]::NewGuid().ToString("N").Substring(0, 6)

# ---------------- fixtures ----------------

function New-Sandbox {
    $d = Join-Path $Root ([Guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Force (Join-Path $d "data\ops_state") | Out-Null
    return $d
}

# A collector_state.json as the collector actually writes it. savedAgoS controls how long
# ago it was last written (the freeze), snapAgoS how long before THAT the last ranking
# snapshot was taken.
function Write-StateFile([string]$dir, [string]$session, [double]$savedAgoS, [double]$snapAgoS,
                         [hashtable]$counters = $null) {
    $nowMs = [DateTimeOffset]::Now.ToUnixTimeMilliseconds()
    $savedMs = $nowMs - [long]($savedAgoS * 1000)
    $snapMs = $savedMs - [long]($snapAgoS * 1000)
    if ($null -eq $counters) {
        $counters = @{ prices_seen = 1000; prices_missing = 0; candles_1m = 5000
                       tier2_orderbook_snaps = 900; ranking_snaps = 700 }
    }
    $obj = @{ version = 1; session = $session; saved_ms = $savedMs
              last_ranking_snap_ms = $snapMs; counters = $counters
              tiers = @{}; watchlist = @() }
    [IO.File]::WriteAllText((Join-Path $dir "data\collector_state.json"),
        ($obj | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
}

# A collector.log whose newest telemetry line is `session` and `agoS` seconds old.
# floodLines pads it with the tier-promotion chatter that made a line-count window useless
# on 2026-08-03, so the time-based scan is exercised rather than assumed.
function Write-CollectorLog([string]$dir, [string]$session, [double]$agoS,
                            [int]$rankingSnapAgeS = 20, [int]$floodLines = 0,
                            [string[]]$extra = @()) {
    $sb = New-Object Text.StringBuilder
    $t = (Get-Date).AddSeconds(-$agoS)
    for ($i = 0; $i -lt $floodLines; $i++) {
        $ts = $t.AddSeconds(-($floodLines - $i) * 0.2).ToString("yyyy-MM-dd HH:mm:ss")
        [void]$sb.AppendLine("$ts,000 INFO    tier2:SYM$i promoted (price activity)")
    }
    $line = ("{0},000 INFO    telemetry session={1} watch=1500 api_errors=0 auth_failures=0 " +
             "loop_errors=0 schema_mismatch=0 symbol_not_found=0 event_write_failures=0 " +
             "promotion_write_failures=0 rankings_write_failures=0 rankings_clamped=0 " +
             "ranking_snap_age_s={2} prices_missing=0 fetch_success_pct=100.0 candles_1m=5000 " +
             "tier2_orderbook_snaps=900 tier2_orderbook_skipped=0 | budget MARKET_DATA=1.00/7.00"
            ) -f $t.ToString("yyyy-MM-dd HH:mm:ss"), $session, $rankingSnapAgeS
    [void]$sb.AppendLine($line)
    foreach ($e in $extra) { [void]$sb.AppendLine($e) }
    [IO.File]::WriteAllText((Join-Path $dir "data\collector.log"), $sb.ToString(),
        [Text.UTF8Encoding]::new($false))
}

function Write-WatchdogState([string]$dir, [hashtable]$props) {
    [IO.File]::WriteAllText((Join-Path $dir "data\ops_state\watchdog_state.json"),
        ($props | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
}

# Watchdog state that says "we have been watching an open session for hours" - the normal
# steady state during a trading session, and the precondition for a stall verdict.
function Write-SettledOpenState([string]$dir, [hashtable]$extra = $null) {
    $p = @{ session_was_open = $true
            session_open_since = ([int][DateTimeOffset]::Now.ToUnixTimeSeconds() - 7200) }
    if ($null -ne $extra) { foreach ($k in $extra.Keys) { $p[$k] = $extra[$k] } }
    Write-WatchdogState $dir $p
}

# ---------------- runner ----------------

function Invoke-Watchdog([string]$dir, [hashtable]$override = $null) {
    $p = [ordered]@{
        RepoRoot          = $dir
        DataDir           = (Join-Path $dir "data")
        StateDir          = (Join-Path $dir "data\ops_state")
        ProcName          = "powershell.exe"
        SupervisorPattern = $SUP_TAG
        CollectorPattern  = $COL_TAG
        ExeLike           = "*"
        # keep the disk branch out of the way: this machine's real free space must not
        # decide whether a watchdog unit test passes
        DiskSurveyGB      = 0
        DiskReclaimGB     = 0
        DiskWarnGB        = 0
        DiskCritGB        = 0
    }
    if ($null -ne $override) { foreach ($k in $override.Keys) { $p[$k] = $override[$k] } }
    $cmdArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $Watchdog, "-DryRunRestart")
    foreach ($k in $p.Keys) { $cmdArgs += @("-$k", [string]$p[$k]) }
    & powershell.exe @cmdArgs 2>&1 | Out-Null
    return $LASTEXITCODE
}

# "Did the watchdog actually restart anything?" The log also contains the word 'restarting'
# inside sentences explaining that it did NOT, so match the action lines only.
function Test-Restarted([string]$log) { return ($log -match "RESTART-DRYRUN|RESTART reason=") }

function Get-Alerts([string]$dir, [string]$prefix = "ALERT") {
    return @(Get-ChildItem (Join-Path $dir "data") -Filter "${prefix}_*.txt" -ErrorAction SilentlyContinue |
             ForEach-Object { $_.Name })
}

# Body of the first alert/note file whose name matches, or "" if there is none. Returning
# "" instead of throwing matters: the interesting run of this harness is the one against a
# BROKEN watchdog, and it has to get through every case to show which ones fail.
function Get-AlertBody([string]$dir, [string[]]$names, [string]$rx) {
    $hit = @($names | Where-Object { $_ -match $rx })
    if ($hit.Count -eq 0) { return "" }
    $p = Join-Path $dir ("data\" + $hit[0])
    if (-not (Test-Path $p)) { return "" }
    return (Get-Content $p -Raw)
}

function Get-WatchdogLog([string]$dir) {
    $p = Join-Path $dir "data\watchdog.log"
    if (-not (Test-Path $p)) { return "" }
    return (Get-Content $p -Raw)
}

function Assert([string]$case, [string]$what, [bool]$cond, [string]$detail = "") {
    if ($cond) {
        $script:Pass++
        Write-Host ("  PASS  {0}: {1}" -f $case, $what)
    } else {
        $script:Fail++
        $script:Failures += ("{0}: {1}{2}" -f $case, $what, $(if ($detail -ne "") { " -- $detail" } else { "" }))
        Write-Host ("  FAIL  {0}: {1}  {2}" -f $case, $what, $detail) -ForegroundColor Red
    }
}

# ---------------- dummy processes ----------------
# Two long-sleeping powershell.exe processes whose command lines carry the tags the
# watchdog is told to look for. Without them every case would trip 'both_dead' first and
# never reach the code under test.

$dummies = @()
function Start-Dummies {
    foreach ($tag in @($SUP_TAG, $COL_TAG)) {
        $p = Start-Process powershell.exe -PassThru -WindowStyle Hidden -ArgumentList @(
            "-NoProfile", "-Command", "`$tag='$tag'; Start-Sleep -Seconds 900")
        $script:dummies += $p
    }
    Start-Sleep -Milliseconds 700   # let the command lines land in Win32_Process
}
function Stop-Dummies {
    foreach ($p in $script:dummies) {
        try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch { }
    }
}

# ================= cases =================

function Test-F1-FrozenStateDoesNotRestart {
    # THE 2026-08-04 08:55 INCIDENT, replayed.
    # collector_state.json froze at 08:49:44 saying session=after; 372s later the watchdog
    # read it, believed 'after', computed ranking_snap_age_s against the wall clock (382s),
    # and restarted a perfectly healthy collector. The log telemetry 44s earlier already
    # said session=closed.
    $d = New-Sandbox
    Write-StateFile $d "after" 372 12
    Write-CollectorLog $d "closed" 44 311 400
    Write-SettledOpenState $d
    $rc = Invoke-Watchdog $d
    $log = Get-WatchdogLog $d
    $alerts = Get-Alerts $d "ALERT"
    Assert "F1" "no restart was attempted" (-not (Test-Restarted $log)) $log
    Assert "F1" "exit code is not 2 (restart)" ($rc -ne 2) "rc=$rc"
    Assert "F1" "no ranking_snap_stalled ALERT" (@($alerts | Where-Object { $_ -match "ranking_snap_stalled" }).Count -eq 0) ($alerts -join ",")
    Assert "F1" "the frozen 'after' was rejected and the log's 'closed' used" ($log -match "session=closed") $log
    Assert "F1" "watchdog.log records the skipped judgment" ($log -match "ranking judgment SKIPPED") $log
}

function Test-F2-FrozenStateAndNoLogFallback {
    # Same freeze, but the log is unreadable too - the fallback cannot save us. The trust
    # test alone must still refuse to judge: an unreadable session is 'unknown', and
    # unknown is not open.
    $d = New-Sandbox
    Write-StateFile $d "after" 372 900
    # a log with no telemetry line at all
    [IO.File]::WriteAllText((Join-Path $d "data\collector.log"),
        ((Get-Date).ToString("yyyy-MM-dd HH:mm:ss") + ",000 INFO    tier2:AAA promoted`r`n"),
        [Text.UTF8Encoding]::new($false))
    Write-SettledOpenState $d
    $rc = Invoke-Watchdog $d
    $log = Get-WatchdogLog $d
    $alerts = Get-Alerts $d "ALERT"
    Assert "F2" "no restart was attempted" (-not (Test-Restarted $log)) $log
    Assert "F2" "no ranking_snap_stalled ALERT" (@($alerts | Where-Object { $_ -match "ranking_snap_stalled" }).Count -eq 0) ($alerts -join ",")
    Assert "F2" "blindness is still reported as its own CRIT" (@($alerts | Where-Object { $_ -match "no_telemetry" }).Count -eq 1) ($alerts -join ",")
}

function Test-F3-SessionJustOpened {
    # 09:00 KST: the calendar hole ends, session flips to day, and the collector has not
    # taken its first ranking snapshot of the new session yet. last_ranking_snap_ms still
    # points into the previous session, so the age is huge and completely legitimate.
    $d = New-Sandbox
    Write-StateFile $d "day" 20 700
    Write-CollectorLog $d "day" 25 700
    Write-WatchdogState $d @{ session_was_open = $false; session_open_since = 0 }
    $rc = Invoke-Watchdog $d
    $log = Get-WatchdogLog $d
    $alerts = Get-Alerts $d "ALERT"
    $notes = Get-Alerts $d "NOTE"
    Assert "F3" "no restart was attempted" (-not (Test-Restarted $log)) $log
    Assert "F3" "no ranking_snap_stalled ALERT" (@($alerts | Where-Object { $_ -match "ranking_snap_stalled" }).Count -eq 0) ($alerts -join ",")
    Assert "F3" "the refusal is visible as a NOTE, not silent" (@($notes | Where-Object { $_ -match "ranking_stall_suppressed" }).Count -eq 1) ($notes -join ",")
}

function Test-F4-CollectorJustStarted {
    # A collector that started seconds ago resumes last_ranking_snap_ms from the state
    # file, so it inherits the previous session's value. Restarting on that is a restart
    # LOOP - the failure mode that turns one bad verdict into an outage.
    $d = New-Sandbox
    Write-StateFile $d "day" 20 700
    Write-CollectorLog $d "day" 25 700
    Write-SettledOpenState $d          # session settled, so only the warmup guard can save us
    $rc = Invoke-Watchdog $d @{ SessionOpenGraceS = 0 }
    $log = Get-WatchdogLog $d
    $alerts = Get-Alerts $d "ALERT"
    Assert "F4" "no restart was attempted" (-not (Test-Restarted $log)) $log
    Assert "F4" "no ranking_snap_stalled ALERT" (@($alerts | Where-Object { $_ -match "ranking_snap_stalled" }).Count -eq 0) ($alerts -join ",")
    Assert "F4" "the reason names the warmup guard" ($log -match "only been up") $log
}

function Test-F5-FreshStateButAlreadyWrong {
    # The nastiest variant, and the one a freshness threshold alone does NOT catch.
    # At 08:50:55 the real state file was only 71s old - well inside any trust window - and
    # its session was ALREADY wrong: the collector had logged "session after -> closed" at
    # 08:50:09 and then had no work left to trigger a rewrite. A file can be fresh and
    # stale at the same time. Only the newer telemetry line can settle it.
    $d = New-Sandbox
    Write-StateFile $d "after" 71 900      # fresh, but its session is a lie
    Write-CollectorLog $d "closed" 40 900  # newer, and it says closed
    Write-SettledOpenState $d
    $rc = Invoke-Watchdog $d @{ SessionOpenGraceS = 0; CollectorWarmupS = 0 }
    $log = Get-WatchdogLog $d
    $alerts = Get-Alerts $d "ALERT"
    Assert "F5" "no restart was attempted" (-not (Test-Restarted $log)) $log
    Assert "F5" "no ranking_snap_stalled ALERT" (@($alerts | Where-Object { $_ -match "_\d{8}_\d{6}_ranking_snap_stalled\.txt$" }).Count -eq 0) ($alerts -join ",")
    Assert "F5" "the reason names the fresher contradicting source" ($log -match "is NEWER than this observation") $log
}

function Test-F6-SessionNameChangeRestartsTheGrace {
    # The grace period must restart on ANY session change, not only closed->open. If the
    # log fallback was unreadable during the calendar hole the watchdog never observes
    # 'closed' at all, so keying the reset on that alone would silently skip the guard
    # exactly on the day it is needed. Here the recorded session is 'after' and the new
    # one is 'day' with no 'closed' ever seen in between.
    $d = New-Sandbox
    Write-StateFile $d "day" 20 900
    Write-CollectorLog $d "day" 25 900
    Write-WatchdogState $d @{ session_was_open = $true; session_name = "after"
                              session_open_since = ([int][DateTimeOffset]::Now.ToUnixTimeSeconds() - 7200) }
    $rc = Invoke-Watchdog $d @{ CollectorWarmupS = 0 }
    $log = Get-WatchdogLog $d
    $alerts = Get-Alerts $d "ALERT"
    Assert "F6" "no restart was attempted" (-not (Test-Restarted $log)) $log
    Assert "F6" "no ranking_snap_stalled ALERT" (@($alerts | Where-Object { $_ -match "_\d{8}_\d{6}_ranking_snap_stalled\.txt$" }).Count -eq 0) ($alerts -join ",")
    Assert "F6" "the grace clock restarted at the session change" ($log -match "only been open") $log
}

function Test-F7-FallbackMustNotGoBackwards {
    # State file too old to be a status (400s), but the newest telemetry line is far older
    # (3000s). Falling back to the log would report a 50-minute-old observation and trip
    # log_stale -> restart, even though we can see the collector was alive 400s ago.
    $d = New-Sandbox
    Write-StateFile $d "day" 400 20
    Write-CollectorLog $d "day" 3000 20
    Write-SettledOpenState $d
    $rc = Invoke-Watchdog $d
    $log = Get-WatchdogLog $d
    Assert "F7" "no restart was attempted" (-not (Test-Restarted $log)) $log
    Assert "F7" "the newer state observation was kept" ($log -match "src=state") $log
    Assert "F7" "its session is still not trusted" ($log -match "session=unknown\(raw=day") $log
}

function Test-T1-RealStallStillRestarts {
    # THE ONE THAT MATTERS. Everything above is a reason NOT to fire; this is the fault the
    # alarm exists for and it must survive all of it: session open and fresh, collector up
    # for a long time, state file being rewritten every loop - and the ranking loop alone
    # is dead. Both grace guards are opened explicitly because the sandbox's dummy
    # processes are necessarily seconds old.
    $d = New-Sandbox
    Write-StateFile $d "day" 20 900          # fresh file, ranking 900s behind at save time
    Write-CollectorLog $d "day" 25 900
    Write-SettledOpenState $d
    $rc = Invoke-Watchdog $d @{ SessionOpenGraceS = 0; CollectorWarmupS = 0 }
    $log = Get-WatchdogLog $d
    $alerts = Get-Alerts $d "ALERT"
    # anchor on the full key: 'watch_ranking_snap_stalled' is a different (also expected) file
    Assert "T1" "ranking_snap_stalled ALERT was written" (@($alerts | Where-Object { $_ -match "_\d{8}_\d{6}_ranking_snap_stalled\.txt$" }).Count -eq 1) ($alerts -join ",")
    Assert "T1" "a restart was performed" ($log -match "RESTART-DRYRUN reason=ranking_snap_stalled") $log
    Assert "T1" "exit code is 2" ($rc -eq 2) "rc=$rc"
    $body = Get-AlertBody $d $alerts "watch_ranking_snap_stalled"
    Assert "T1" "the restart alert carries the EVIDENCE block" ($body -match "EVIDENCE:") $body
    Assert "T1" "the restart alert says how to judge it" ($body -match "WAS THIS RESTART JUSTIFIED") $body
}

function Test-T2-StaleObservationStillRestartsOnLogStale {
    # Refusing to judge the SESSION must not become refusing to notice that nothing has
    # been written for a long time. Both sources ancient -> log_stale -> restart.
    $d = New-Sandbox
    Write-StateFile $d "day" 3000 20
    Write-CollectorLog $d "day" 3000 20
    Write-SettledOpenState $d
    $rc = Invoke-Watchdog $d
    $log = Get-WatchdogLog $d
    Assert "T2" "a restart was performed for log_stale" ($log -match "RESTART-DRYRUN reason=log_stale") $log
    Assert "T2" "exit code is 2" ($rc -eq 2) "rc=$rc"
}

function Test-T3-DeadProcessStillRestarts {
    # The plainest fault of all still works with the dummy processes gone.
    $d = New-Sandbox
    Write-StateFile $d "day" 20 20
    Write-CollectorLog $d "day" 25 20
    Write-SettledOpenState $d
    $rc = Invoke-Watchdog $d @{ CollectorPattern = "NOTHING_MATCHES_THIS_XYZZY"
                                SupervisorPattern = "NOTHING_MATCHES_THIS_XYZZY" }
    $log = Get-WatchdogLog $d
    Assert "T3" "a restart was performed for process_dead" ($log -match "RESTART-DRYRUN reason=process_dead") $log
}

function Test-S1-SchemaMismatchAllStockNotFound {
    # 2026-08-03 23:16-23:21: five schema_mismatch bumps, every one an http-404
    # code=stock-not-found. The alert said "the API response shape changed ... before
    # trusting today's data". It must now be a NOTE and say what it actually saw.
    $d = New-Sandbox
    $ts = (Get-Date).AddSeconds(-60).ToString("yyyy-MM-dd HH:mm:ss")
    $lines = @(
        "$ts,000 WARNING tier2:BLRK: schema mismatch (skip): http-404 code=stock-not-found message=x",
        "$ts,100 WARNING tier2:CABR: schema mismatch (skip): http-404 code=stock-not-found message=x")
    Write-StateFile $d "day" 20 20 @{ schema_mismatch = 5; prices_seen = 10 }
    Write-CollectorLog $d "day" 30 20 0 $lines
    Write-SettledOpenState $d @{ last_counters = @{ schema_mismatch = 3 } }
    Invoke-Watchdog $d | Out-Null
    $alerts = Get-Alerts $d "ALERT"
    $notes = Get-Alerts $d "NOTE"
    Assert "S1" "not filed as an ALERT" (@($alerts | Where-Object { $_ -match "schema_mismatch" }).Count -eq 0) ($alerts -join ",")
    Assert "S1" "filed as a NOTE" (@($notes | Where-Object { $_ -match "schema_mismatch" }).Count -eq 1) ($notes -join ",")
    $body = Get-AlertBody $d $notes "schema_mismatch"
    # non-empty guard: a missing file must not pass a -notmatch assertion by vacuity
    Assert "S1" "the body no longer asserts a shape change" ($body -ne "" -and $body -notmatch "response shape changed") $body
    Assert "S1" "the body shows the evidence lines" ($body -match "code=stock-not-found") $body
}

function Test-S2-SchemaMismatchRealShapeChange {
    # A genuine shape change must still be CRIT with the strong wording. If this passes
    # only because everything is downgraded, the fix would be worthless.
    $d = New-Sandbox
    $ts = (Get-Date).AddSeconds(-60).ToString("yyyy-MM-dd HH:mm:ss")
    $lines = @(
        "$ts,000 WARNING tier2:AAA: schema mismatch (skip): candles.price: expected number, got str",
        "$ts,100 WARNING tier2:BBB: schema mismatch (skip): http-404 code=stock-not-found message=x")
    Write-StateFile $d "day" 20 20 @{ schema_mismatch = 5; prices_seen = 10 }
    Write-CollectorLog $d "day" 30 20 0 $lines
    Write-SettledOpenState $d @{ last_counters = @{ schema_mismatch = 3 } }
    Invoke-Watchdog $d | Out-Null
    $alerts = Get-Alerts $d "ALERT"
    Assert "S2" "still filed as an ALERT" (@($alerts | Where-Object { $_ -match "schema_mismatch" }).Count -eq 1) ($alerts -join ",")
    $body = Get-AlertBody $d $alerts "schema_mismatch"
    Assert "S2" "level is CRIT" ($body -match "\[CRIT\]") $body
    Assert "S2" "the strong wording is kept for the real case" ($body -match "response shape may have changed") $body
}

function Test-S3-SchemaMismatchNoEvidence {
    # Counter rose but the lines are not in the scanned window. The alert must say it does
    # not know, rather than assert either way.
    $d = New-Sandbox
    Write-StateFile $d "day" 20 20 @{ schema_mismatch = 5; prices_seen = 10 }
    Write-CollectorLog $d "day" 30 20
    Write-SettledOpenState $d @{ last_counters = @{ schema_mismatch = 3 } }
    Invoke-Watchdog $d | Out-Null
    $alerts = Get-Alerts $d "ALERT"
    $hit = @($alerts | Where-Object { $_ -match "schema_mismatch" })
    Assert "S3" "filed as an ALERT (WARN, unknown cause)" ($hit.Count -eq 1) ($alerts -join ",")
    if ($hit.Count -eq 1) {
        $body = Get-AlertBody $d $alerts "schema_mismatch"
        Assert "S3" "level is WARN, not CRIT" ($body -match "\[WARN\]") $body
        Assert "S3" "the body admits the cause is unknown" ($body -match "UNKNOWN") $body
    }
}

function Test-S4-SymbolNotFoundIsANote {
    # Once the running collector carries main 4397130, the harmless case has its own
    # counter. It must land as a NOTE and never as a CRIT.
    $d = New-Sandbox
    Write-StateFile $d "day" 20 20 @{ symbol_not_found = 7; prices_seen = 10 }
    Write-CollectorLog $d "day" 30 20
    Write-SettledOpenState $d @{ last_counters = @{ symbol_not_found = 4 } }
    Invoke-Watchdog $d | Out-Null
    $alerts = Get-Alerts $d "ALERT"
    $notes = Get-Alerts $d "NOTE"
    Assert "S4" "not filed as an ALERT" (@($alerts | Where-Object { $_ -match "symbol_not_found" }).Count -eq 0) ($alerts -join ",")
    Assert "S4" "filed as a NOTE" (@($notes | Where-Object { $_ -match "symbol_not_found" }).Count -eq 1) ($notes -join ",")
}

function Test-R1-HealthySessionIsQuiet {
    # The baseline: a normal open session with everything fresh writes no ALERT at all.
    # Without this, "no alert" in the cases above could just mean the watchdog crashed.
    $d = New-Sandbox
    Write-StateFile $d "day" 20 30
    Write-CollectorLog $d "day" 25 30
    Write-SettledOpenState $d
    $rc = Invoke-Watchdog $d
    $log = Get-WatchdogLog $d
    $alerts = Get-Alerts $d "ALERT"
    Assert "R1" "exit code is 0" ($rc -eq 0) "rc=$rc"
    Assert "R1" "no ALERT files at all" ($alerts.Count -eq 0) ($alerts -join ",")
    Assert "R1" "summary line reports an OK cycle" ($log -match "OK sup=1 col=1") $log
    Assert "R1" "summary exposes the new trust fields" ($log -match "open_for=\d+s col_up=\d+s") $log
}

# ================= main =================

Write-Host "watchdog self-test - sandbox root: $Root"
Write-Host "watchdog under test: $Watchdog"
New-Item -ItemType Directory -Force $Root | Out-Null
Start-Dummies
try {
    $cases = @(
        "Test-R1-HealthySessionIsQuiet",
        "Test-F1-FrozenStateDoesNotRestart",
        "Test-F2-FrozenStateAndNoLogFallback",
        "Test-F3-SessionJustOpened",
        "Test-F4-CollectorJustStarted",
        "Test-F5-FreshStateButAlreadyWrong",
        "Test-F6-SessionNameChangeRestartsTheGrace",
        "Test-F7-FallbackMustNotGoBackwards",
        "Test-T1-RealStallStillRestarts",
        "Test-T2-StaleObservationStillRestartsOnLogStale",
        "Test-T3-DeadProcessStillRestarts",
        "Test-S1-SchemaMismatchAllStockNotFound",
        "Test-S2-SchemaMismatchRealShapeChange",
        "Test-S3-SchemaMismatchNoEvidence",
        "Test-S4-SymbolNotFoundIsANote")
    foreach ($c in $cases) {
        try { & $c } catch { Assert $c "case ran to completion" $false $_.Exception.Message }
    }
} finally {
    Stop-Dummies
    if (-not $KeepSandbox) {
        try { Remove-Item $Root -Recurse -Force -ErrorAction SilentlyContinue } catch { }
    } else {
        Write-Host "sandbox kept at $Root"
    }
}

Write-Host ""
Write-Host ("RESULT: {0} passed, {1} failed" -f $script:Pass, $script:Fail)
if ($script:Fail -gt 0) {
    foreach ($f in $script:Failures) { Write-Host ("  - " + $f) }
    exit 1
}
exit 0
