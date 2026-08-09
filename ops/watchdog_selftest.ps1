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
                         [hashtable]$counters = $null, [hashtable]$tiers = $null) {
    $nowMs = [DateTimeOffset]::Now.ToUnixTimeMilliseconds()
    $savedMs = $nowMs - [long]($savedAgoS * 1000)
    $snapMs = $savedMs - [long]($snapAgoS * 1000)
    if ($null -eq $counters) {
        $counters = @{ prices_seen = 1000; prices_missing = 0; candles_1m = 5000
                       tier2_orderbook_snaps = 900; ranking_snaps = 700 }
    }
    if ($null -eq $tiers) { $tiers = @{} }
    $obj = @{ version = 1; session = $session; saved_ms = $savedMs
              last_ranking_snap_ms = $snapMs; counters = $counters
              tiers = $tiers; watchlist = @() }
    [IO.File]::WriteAllText((Join-Path $dir "data\collector_state.json"),
        ($obj | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
}

# A tier membership map as collector_state.json carries it: symbol -> tier.
function New-Tiers([int]$tier2, [int]$tier3) {
    $t = @{}
    for ($i = 0; $i -lt $tier2; $i++) { $t["T2SYM$i"] = 2 }
    for ($i = 0; $i -lt $tier3; $i++) { $t["T3SYM$i"] = 3 }
    return $t
}

# Drive N watchdog cycles in which tier2_orderbook_snaps NEVER advances while the rest of
# the collector keeps working. Modelling "the rest keeps working" matters: if every counter
# froze, the counter-freeze detector would fire first and the case under test would never
# be reached. So candles/rankings/prices advance each cycle, exactly as they do live when
# only the tier2 orderbook sweep is yielding.
#
# skipRate / skip429 are the per-cycle increments of the two deliberate-skip counters -
# they are what separates "the collector chose to skip" from "the loop is dead".
function Invoke-Tier2FlatCycles([string]$dir, [int]$cycles, [int]$skipRate, [int]$skip429,
                                [hashtable]$tiers, [string[]]$logExtra = @()) {
    $rate = 2000; $n429 = 250; $candles = 5000; $ranks = 700; $seen = 1000
    $rc = 0
    for ($i = 0; $i -lt $cycles; $i++) {
        $rate += $skipRate
        $n429 += $skip429
        $candles += 40; $ranks += 25; $seen += 500      # everything else still moves
        Write-StateFile $dir "regular" 20 25 @{
            prices_seen = $seen; prices_missing = 0; candles_1m = $candles
            ranking_snaps = $ranks
            tier2_orderbook_snaps = 22069            # FROZEN - the observation under test
            tier2_orderbook_skipped_rate = $rate
            tier2_orderbook_skipped_429 = $n429
        } $tiers
        Write-CollectorLog $dir "regular" 25 25 0 $logExtra
        $rc = Invoke-Watchdog $dir
    }
    return $rc
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

function _EmptyTaskInfo([string]$dir) {
    $p = Join-Path $dir "taskinfo_empty.json"
    if (-not (Test-Path $p)) { [IO.File]::WriteAllText($p, "[]", [Text.UTF8Encoding]::new($false)) }
    return $p
}

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
        # Same reason as the disk thresholds above: without this the sibling-task check
        # queries THIS machine's real Task Scheduler, and every case that asserts "no
        # ALERT files" fails whenever a real tossmon task happens to be in a failed
        # state. A unit test must not depend on the state of the box it runs on.
        TaskInfoJson      = (_EmptyTaskInfo $dir)
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
    # Presence of the heading is not enough - it was present on 2026-08-06 too, saying the
    # wrong thing. A progress-stall verdict genuinely does rest on session freshness, so
    # this family must keep the 2026-08-04 wording.
    Assert "T1" "a progress-stall restart is judged on session freshness" ($body -match "NOT TRUSTED.*probably wrong") $body
    Assert "T1" "and NOT on the process counts" ($body -notmatch "PROCESS COUNTS") $body
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
    #
    # The session here is deliberately NOT TRUSTED (state file 3000s old, log fallback the
    # same) - this is ALERT_20260806_094100 replayed: session=unknown, sup=0, col=0, and
    # the restart was exactly right (it revived collection at 09:41). The shared
    # self-assessment used to fire its first bullet on that file and tell the morning
    # reader the restart was "probably wrong, a watchdog defect worth reporting".
    $d = New-Sandbox
    Write-StateFile $d "day" 3000 20
    Write-CollectorLog $d "day" 3000 20
    Write-SettledOpenState $d
    $rc = Invoke-Watchdog $d @{ CollectorPattern = "NOTHING_MATCHES_THIS_XYZZY"
                                SupervisorPattern = "NOTHING_MATCHES_THIS_XYZZY" }
    $log = Get-WatchdogLog $d
    $alerts = Get-Alerts $d "ALERT"
    Assert "T3" "a restart was performed for process_dead" ($log -match "RESTART-DRYRUN reason=process_dead") $log
    Assert "T3" "the session really was untrusted" ($log -match "session=unknown\(raw=day") $log
    $body = Get-AlertBody $d $alerts "watch_process_dead"
    Assert "T3" "the restart alert says how to judge it" ($body -match "WAS THIS RESTART JUSTIFIED") $body
    Assert "T3" "a process-absence restart is judged on the sup/col counts" ($body -match "PROCESS COUNTS.*sup=0 col=0") $body
    Assert "T3" "it does not blame the stale session reading" ($body -notmatch "probably wrong") $body
    Assert "T3" "it says an absent process cannot be defended by a session reading" ($body -match "cannot be\s+defended by a session reading") $body
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

# --------------------------------------------------------------------------- #
# TRADEOFF vs FAULT (2026-08-05). The night of 08-05 produced five ALERT_ files for
# tier2_orderbook_flat while the collector was doing exactly what it was designed to do:
# tier3 filled to capacity for the first time and the tier2 orderbook sweep yielded its
# budget. Two neighbouring blocks graded the same event in opposite directions.
#
# The TR* cases below are the fix. The TC* cases are the CONTROL GROUP and they must pass
# both before and after - without them, "no more false ALERTs" is indistinguishable from
# "the alarm was switched off".
# --------------------------------------------------------------------------- #

function Test-TR1-BudgetYieldIsATradeoffNotAnAlert {
    # The 2026-08-05 night, replayed with its measured shape: snaps frozen at 22,069,
    # skipped_rate climbing, skipped_429 flat, tier2 populated.
    $d = New-Sandbox
    Write-SettledOpenState $d
    Invoke-Tier2FlatCycles $d 3 420 0 (New-Tiers 287 10) | Out-Null
    $alerts = Get-Alerts $d "ALERT"
    $trade = Get-Alerts $d "TRADEOFF"
    Assert "TR1" "a TRADEOFF_ file was written" ($trade.Count -eq 1) ($trade -join ",")
    Assert "TR1" "NO ALERT_ file at all - the morning rule survives" ($alerts.Count -eq 0) ($alerts -join ",")
    $body = Get-AlertBody $d $trade "tier2_orderbook_yield"
    Assert "TR1" "reason names budget pressure" ($body -match "budget pressure \(skipped_rate \+\d+\)") $body
    Assert "TR1" "says plainly it is not a fault" ($body -match "not a fault") $body
}

function Test-TR2-Cooldown429IsATradeoffWithItsOwnReason {
    # Same shape, different cause. The reason must be distinguishable in the body -
    # a 429 cooldown and a budget-headroom yield call for different responses.
    $d = New-Sandbox
    Write-SettledOpenState $d
    Invoke-Tier2FlatCycles $d 3 0 12 (New-Tiers 200 10) | Out-Null
    $trade = Get-Alerts $d "TRADEOFF"
    Assert "TR2" "a TRADEOFF_ file was written" ($trade.Count -eq 1) ($trade -join ",")
    Assert "TR2" "no ALERT_" ((Get-Alerts $d "ALERT").Count -eq 0) ""
    $body = Get-AlertBody $d $trade "tier2_orderbook_yield"
    Assert "TR2" "reason names the 429 cooldown" ($body -match "429 cooldown \(skipped_429 \+\d+\)") $body
    Assert "TR2" "does NOT claim budget pressure" ($body -notmatch "budget pressure") $body
}

function Test-TR3-TradeoffBodyCarriesAllFourMandatoryFields {
    # A tradeoff record missing any of the four is a rumour. Each is checked for its
    # CONTENT, not just its heading - an empty "HOW MUCH:" would pass a heading-only test.
    $d = New-Sandbox
    Write-SettledOpenState $d
    Invoke-Tier2FlatCycles $d 3 420 0 (New-Tiers 287 10) | Out-Null
    $body = Get-AlertBody $d (Get-Alerts $d "TRADEOFF") "tier2_orderbook_yield"
    Assert "TR3" "1 WHAT was given up names the stream" ($body -match "1\. WHAT was given up\s*: tier2 orderbook snapshots") $body
    Assert "TR3" "2 FOR WHAT carries numbers" ($body -match "2\. FOR WHAT\s*:.*\+\d+") $body
    Assert "TR3" "2 FOR WHAT carries the MARKET_DATA budget" ($body -match "MARKET_DATA budget at this moment: \S") $body
    Assert "TR3" "2 FOR WHAT carries tier populations" ($body -match "tier2 members waiting: \d+, tier3 members: \d+") $body
    Assert "TR3" "3 HOW LONG is a duration in seconds" ($body -match "3\. HOW LONG\s*: \d+s continuous") $body
    Assert "TR3" "4 HOW MUCH is a yield percentage" ($body -match "4\. HOW MUCH\s*: [\d.]+% of attempted polls yielded") $body
    Assert "TR3" "states the no-escalation rule" ($body -match "NEVER escalates to ALERT_") $body
}

function Test-TR4-LongDurationStillDoesNotEscalate {
    # User decision 2026-08-05: lasting is not breaking. Six cycles of continuous yield
    # must still be a TRADEOFF_ - escalating would read as "a restart would help".
    $d = New-Sandbox
    Write-SettledOpenState $d
    Invoke-Tier2FlatCycles $d 6 420 0 (New-Tiers 287 10) | Out-Null
    Assert "TR4" "still no ALERT_ after 6 flat cycles" ((Get-Alerts $d "ALERT").Count -eq 0) ""
    Assert "TR4" "still a TRADEOFF_" ((Get-Alerts $d "TRADEOFF").Count -ge 1) ""
}

function Test-TR5-ZeroTier2MembersIsANoteNotAnAlert {
    # Nothing to poll is neither a fault nor a tradeoff - no data was given up because
    # there was no candidate. It must not be filed as either.
    $d = New-Sandbox
    Write-SettledOpenState $d
    Invoke-Tier2FlatCycles $d 3 0 0 (New-Tiers 0 5) | Out-Null
    $notes = Get-Alerts $d "NOTE"
    Assert "TR5" "no ALERT_" ((Get-Alerts $d "ALERT").Count -eq 0) ((Get-Alerts $d "ALERT") -join ",")
    Assert "TR5" "no TRADEOFF_ either - nothing was sacrificed" ((Get-Alerts $d "TRADEOFF").Count -eq 0) ""
    Assert "TR5" "filed as NOTE_" (@($notes | Where-Object { $_ -match "tier2_orderbook_no_members" }).Count -eq 1) ($notes -join ",")
}

# ---- CONTROL GROUP: these must be green BEFORE and AFTER the fix ----

function Test-TC1-RealStallIsStillAnAlert {
    # THE ONE THAT MATTERS. Snaps flat, BOTH skip counters flat, tier2 populated.
    # The collector is not choosing to skip - it is not running. This must stay loud.
    $d = New-Sandbox
    Write-SettledOpenState $d
    Invoke-Tier2FlatCycles $d 3 0 0 (New-Tiers 287 10) | Out-Null
    $alerts = Get-Alerts $d "ALERT"
    Assert "TC1" "still ALERT_" (@($alerts | Where-Object { $_ -match "tier2_orderbook_flat" }).Count -eq 1) ($alerts -join ",")
    Assert "TC1" "not filed as TRADEOFF_" ((Get-Alerts $d "TRADEOFF").Count -eq 0) ""
    $body = Get-AlertBody $d $alerts "tier2_orderbook_flat"
    Assert "TC1" "body says no skip counter moved" ($body -match "did NOT record any deliberate skip") $body
}

function Test-TC2-DisabledPollingIsStillAnAlert {
    # polling.tier2_orderbook_s = 0 makes the loop idle: no snaps, no skips, members
    # present. Observationally identical to a stall, and it must stay ALERT_ - the body
    # points at the cheap check rather than pretending to know which one it is.
    $d = New-Sandbox
    Write-SettledOpenState $d
    $ts = (Get-Date).AddSeconds(-30).ToString("yyyy-MM-dd HH:mm:ss")
    Invoke-Tier2FlatCycles $d 3 0 0 (New-Tiers 287 10) @(
        "$ts,000 INFO    tier2 orderbook: disabled (polling.tier2_orderbook_s=0)") | Out-Null
    $alerts = Get-Alerts $d "ALERT"
    Assert "TC2" "still ALERT_" (@($alerts | Where-Object { $_ -match "tier2_orderbook_flat" }).Count -eq 1) ($alerts -join ",")
    $body = Get-AlertBody $d $alerts "tier2_orderbook_flat"
    Assert "TC2" "body names the disabled-config hypothesis" ($body -match "polling.tier2_orderbook_s is 0 or unset") $body
}

function Test-TC3-YieldTurningIntoAStallIsNotDedupedAway {
    # The subtle one. If both verdicts shared a dedup key, a yield that later became a real
    # stall would be silenced for the rest of the dedup window - suppressed by the very
    # record that said "this is fine".
    $d = New-Sandbox
    Write-SettledOpenState $d
    Invoke-Tier2FlatCycles $d 3 420 0 (New-Tiers 287 10) | Out-Null
    Assert "TC3" "phase 1 is a TRADEOFF_" ((Get-Alerts $d "TRADEOFF").Count -eq 1) ""
    Invoke-Tier2FlatCycles $d 3 0 0 (New-Tiers 287 10) | Out-Null   # skips stop, snaps still flat
    $alerts = Get-Alerts $d "ALERT"
    Assert "TC3" "phase 2 raises a real ALERT_ despite the recent TRADEOFF_" `
        (@($alerts | Where-Object { $_ -match "tier2_orderbook_flat" }).Count -eq 1) ($alerts -join ",")
}

# --------------------------------------------------------------------------- #
# LOG-PATTERN WINDOW (2026-08-09). The six log-pattern checks shared one 500-LINE
# window with no time bound. Measured span of those 500 lines:
#
#     Fri regular 23:30 -> 113 min      Fri regular 03:00 ->   74 min
#     Fri regular 01:00 ->  77 min      Sun idle    09:33 -> 1507 min (25.1 h)
#
# So on a quiet weekend one ERROR line stays inside the window for a day, and with
# Raise-Alert's 3600s dedup it re-fires every hour: one 08-08 08:46 'precision drift'
# produced 25 ALERT files by 08-09 09:21. The damage is one-directional - a dead event
# shouting forever - because the log is 5-minute telemetry, not per-request.
#
# The fix is a TIME cap on top of the line cap, plus the matched line's timestamp in the
# body so a reader knows at a glance whether it happened now or yesterday.
# --------------------------------------------------------------------------- #
function _LogLine([double]$agoMin, [string]$text) {
    $t = (Get-Date).AddMinutes(-$agoMin).ToString("yyyy-MM-dd HH:mm:ss")
    return "$t,000 ERROR   $text"
}

function Test-LP1-A25HourOldEventMustNotKeepAlerting {
    # THE PRE-FIX FAILURE. Before the time cap this raised ALERT_log_precision_drift,
    # and kept doing it every hour for as long as the log stayed quiet.
    $d = New-Sandbox
    Write-StateFile $d "closed" 20 30
    Write-CollectorLog $d "closed" 25 30 0 @(
        (_LogLine 1507 "precision drift: max decimal digits 7 -> 8 (sample '5.70891193')"))
    Write-SettledOpenState $d
    Invoke-Watchdog $d | Out-Null
    $alerts = Get-Alerts $d "ALERT"
    Assert "LP1" "a 25h-old log line raises no ALERT" `
        (@($alerts | Where-Object { $_ -match "log_precision_drift" }).Count -eq 0) ($alerts -join ",")
}

function Test-LP2-ARecentEventIsStillCaught {
    # CONTROL GROUP. The cap must not turn the detector off - this is the whole risk of
    # a time bound, and the reason the bound is 180min and not 30min.
    $d = New-Sandbox
    Write-StateFile $d "day" 20 30
    Write-CollectorLog $d "day" 25 30 0 @(
        (_LogLine 10 "precision drift: max decimal digits 7 -> 8 (sample '5.70891193')"))
    Write-SettledOpenState $d
    Invoke-Watchdog $d | Out-Null
    $alerts = Get-Alerts $d "ALERT"
    Assert "LP2" "a 10min-old log line still raises an ALERT" `
        (@($alerts | Where-Object { $_ -match "log_precision_drift" }).Count -eq 1) ($alerts -join ",")
}

function Test-LP3-TheBodyCarriesTheMatchedLineTimestamp {
    # Opening an alert file used to tell you 'Occurrences in the last 500 log lines: 1'
    # and nothing else - you could not tell a fresh fault from a day-old one.
    $d = New-Sandbox
    Write-StateFile $d "day" 20 30
    $stamp = (Get-Date).AddMinutes(-12).ToString("yyyy-MM-dd HH:mm")
    Write-CollectorLog $d "day" 25 30 0 @(
        (_LogLine 12 "precision drift: max decimal digits 7 -> 8 (sample '5.70891193')"))
    Write-SettledOpenState $d
    Invoke-Watchdog $d | Out-Null
    $body = Get-AlertBody $d (Get-Alerts $d "ALERT") "log_precision_drift"
    Assert "LP3" "body names when the newest match happened" ($body -match [regex]::Escape($stamp)) $body
    Assert "LP3" "body states the time bound it used" ($body -match "within \d+ ?min") $body
}

function Test-LP4-AnAgedOutMatchIsRecordedNotSilent {
    # A suppression nobody can see is just a different blind spot. It must not become an
    # ALERT (that would recreate the disease), so it goes to watchdog.log.
    $d = New-Sandbox
    Write-StateFile $d "closed" 20 30
    Write-CollectorLog $d "closed" 25 30 0 @(
        (_LogLine 1507 "precision drift: max decimal digits 7 -> 8 (sample '5.70891193')"))
    Write-SettledOpenState $d
    Invoke-Watchdog $d | Out-Null
    $log = Get-WatchdogLog $d
    Assert "LP4" "watchdog.log records the aged-out match" `
        ($log -match "LOG-PATTERN-AGED key=log_precision_drift") $log
    Assert "LP4" "and says how old the newest one was" ($log -match "newest=\d{4}-\d{2}-\d{2}") $log
}

function Test-LP5-ACriticalPatternKeepsTheSameBound {
    # CONTROL GROUP + the judgement call: all six patterns are 'a line appeared'
    # detectors, so the bound answers 'how long after it STOPS do we keep shouting',
    # which does not depend on severity. A live CRIT keeps producing fresh lines.
    $d = New-Sandbox
    Write-StateFile $d "day" 20 30
    Write-CollectorLog $d "day" 25 30 0 @((_LogLine 5 "AUTH-FAILURE token rejected"))
    Write-SettledOpenState $d
    Invoke-Watchdog $d | Out-Null
    $alerts = Get-Alerts $d "ALERT"
    Assert "LP5" "a fresh CRIT still alerts" `
        (@($alerts | Where-Object { $_ -match "log_auth_failure" }).Count -eq 1) ($alerts -join ",")

    $d2 = New-Sandbox
    Write-StateFile $d2 "closed" 20 30
    Write-CollectorLog $d2 "closed" 25 30 0 @((_LogLine 1507 "AUTH-FAILURE token rejected"))
    Write-SettledOpenState $d2
    Invoke-Watchdog $d2 | Out-Null
    Assert "LP5" "a day-old CRIT does not re-fire forever either" `
        (@((Get-Alerts $d2 "ALERT") | Where-Object { $_ -match "log_auth_failure" }).Count -eq 0) ""
}

function Test-LP6-ACountingPatternIsNotTrimmedByTheBound {
    # log_tape_gap needs Min=20. A time cap can silently lower a COUNT threshold's
    # numerator - that is why the bound (180min) sits above the measured busiest-session
    # span of the 500-line window (113min), so during market hours lines never age out.
    $d = New-Sandbox
    Write-StateFile $d "regular" 20 30
    $gaps = @()
    for ($i = 0; $i -lt 25; $i++) { $gaps += (_LogLine (100 + $i) "tape gap SYM$i prev=1 this=2 n=3") }
    Write-CollectorLog $d "regular" 25 30 0 $gaps
    Write-SettledOpenState $d
    Invoke-Watchdog $d | Out-Null
    $notes = Get-Alerts $d "NOTE"
    Assert "LP6" "25 tape gaps spread over 100-124min still clear Min=20" `
        (@($notes | Where-Object { $_ -match "log_tape_gap" }).Count -eq 1) ($notes -join ",")
}

function Test-LP7-AnUndatableMatchIsKeptNotDropped {
    # Dropping a line we cannot date is the dangerous direction: it loses a real alert
    # silently. Keep it, and say in the body that it could not be dated.
    $d = New-Sandbox
    Write-StateFile $d "day" 20 30
    Write-CollectorLog $d "day" 25 30 0 @("    ...continuation... precision drift in traceback")
    Write-SettledOpenState $d
    Invoke-Watchdog $d | Out-Null
    $alerts = Get-Alerts $d "ALERT"
    Assert "LP7" "an undatable match still alerts" `
        (@($alerts | Where-Object { $_ -match "log_precision_drift" }).Count -eq 1) ($alerts -join ",")
    $body = Get-AlertBody $d $alerts "log_precision_drift"
    Assert "LP7" "and the body says it could not be dated" ($body -match "undated") $body
}

# --------------------------------------------------------------------------- #
# SIBLING SCHEDULED TASK RESULTS (2026-08-09). tossmon-dailyhealth ran at 08:52:01 and
# died with 0xC000013A (STATUS_CONTROL_C_EXIT). The morning report was simply absent and
# NOT ONE ALERT FIRED - the coordinator only noticed by counting files by hand.
#
# The trap this must avoid: tossmon-watchdog legitimately exits 1 on every cycle that
# found problems (and 2 when it restarted the collector), and tossmon-collector-oneshot
# has carried a failed result since 08-04 with no next run. Alerting on either would make
# this feature a standing alarm - the same disease as the log-pattern window.
# --------------------------------------------------------------------------- #
function _TaskInfo([string]$dir, [array]$rows) {
    $p = Join-Path $dir "taskinfo.json"
    [IO.File]::WriteAllText($p, (ConvertTo-Json @($rows) -Depth 4), [Text.UTF8Encoding]::new($false))
    return $p
}

function _Task([string]$name, $result, [double]$ranHoursAgo) {
    return @{ TaskName = $name
              LastRunTime = (Get-Date).AddHours(-$ranHoursAgo).ToString("yyyy-MM-dd HH:mm:ss")
              LastTaskResult = $result }
}

function _RunWithTasks([string]$d, [array]$rows) {
    Write-StateFile $d "day" 20 30
    Write-CollectorLog $d "day" 25 30
    Write-SettledOpenState $d
    Invoke-Watchdog $d @{ TaskInfoJson = (_TaskInfo $d $rows) } | Out-Null
}

function Test-TS1-ATaskThatRanAndDiedRaisesAnAlert {
    # THE PRE-FIX FAILURE: this exact record produced no alert at all on 2026-08-09.
    $d = New-Sandbox
    _RunWithTasks $d @((_Task "tossmon-dailyhealth" 3221225786 1.0))
    $alerts = Get-Alerts $d "ALERT"
    Assert "TS1" "a dead sibling task raises an ALERT" `
        (@($alerts | Where-Object { $_ -match "task_result_tossmon_dailyhealth" }).Count -eq 1) ($alerts -join ",")
    $body = Get-AlertBody $d $alerts "task_result_tossmon_dailyhealth"
    Assert "TS1" "body decodes the exit code" ($body -match "STATUS_CONTROL_C_EXIT") $body
    Assert "TS1" "body names when it ran" ($body -match "\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}") $body
    Assert "TS1" "body says what was expected instead" ($body -match "expected : 0") $body
}

function Test-TS2-TheWatchdogsOwnExitOneIsNormal {
    # CONTROL GROUP. watchdog.ps1 exits 1 whenever $problems is non-empty. If that counted
    # as failure this check would fire on nearly every cycle and mean nothing.
    $d = New-Sandbox
    _RunWithTasks $d @((_Task "tossmon-watchdog" 1 0.1), (_Task "tossmon-sentinel" 0 0.1))
    Assert "TS2" "watchdog result=1 raises nothing" `
        (@((Get-Alerts $d "ALERT") | Where-Object { $_ -match "task_result" }).Count -eq 0) ""
}

function Test-TS3-TheWatchdogsRestartExitIsAlsoNormal {
    # exit 2 = "I restarted the collector" - already reported by its own alert files.
    $d = New-Sandbox
    _RunWithTasks $d @((_Task "tossmon-watchdog" 2 0.1))
    Assert "TS3" "watchdog result=2 raises nothing" `
        (@((Get-Alerts $d "ALERT") | Where-Object { $_ -match "task_result" }).Count -eq 0) ""
}

function Test-TS4-AllCleanTasksAreQuiet {
    $d = New-Sandbox
    _RunWithTasks $d @((_Task "tossmon-dailyhealth" 0 1.0), (_Task "tossmon-logrotate" 0 9.0),
                       (_Task "tossmon-sentinel" 0 0.1))
    Assert "TS4" "all-zero results raise nothing" `
        (@((Get-Alerts $d "ALERT") | Where-Object { $_ -match "task_result" }).Count -eq 0) ""
}

function Test-TS5-AStaleOneShotFailureIsRecordedNotAlerted {
    # CONTROL GROUP. tossmon-collector-oneshot has held 0xC000013A since 08-04 with no
    # next run. Without the recency bound this alerts every hour, forever.
    $d = New-Sandbox
    _RunWithTasks $d @((_Task "tossmon-collector-oneshot" 3221225786 120.0))
    Assert "TS5" "a 5-day-old one-shot failure raises nothing" `
        (@((Get-Alerts $d "ALERT") | Where-Object { $_ -match "task_result" }).Count -eq 0) ""
    Assert "TS5" "but watchdog.log records it" `
        ((Get-WatchdogLog $d) -match "TASK-RESULT-AGED task=tossmon-collector-oneshot") (Get-WatchdogLog $d)
}

function Test-TS6-ARunningTaskIsNotAFailure {
    # 267009 = 0x41301 SCHED_S_TASK_RUNNING. Reading it as an exit code says 'failed'.
    $d = New-Sandbox
    _RunWithTasks $d @((_Task "tossmon-dailyhealth" 267009 0.2),
                       (_Task "tossmon-logrotate" 267011 0.2))
    Assert "TS6" "running / never-ran codes raise nothing" `
        (@((Get-Alerts $d "ALERT") | Where-Object { $_ -match "task_result" }).Count -eq 0) ""
}

function Test-TS7-AnUnregisteredTaskIsItsOwnEvent {
    # A task that is not registered at all is a different fault from one that ran and died.
    $d = New-Sandbox
    Write-StateFile $d "day" 20 30
    Write-CollectorLog $d "day" 25 30
    Write-SettledOpenState $d
    $p = Join-Path $d "taskinfo.json"
    [IO.File]::WriteAllText($p, (ConvertTo-Json @(@{ TaskName = "tossmon-logrotate"
        LastRunTime = $null; LastTaskResult = $null; Missing = $true }) -Depth 4),
        [Text.UTF8Encoding]::new($false))
    Invoke-Watchdog $d @{ TaskInfoJson = $p } | Out-Null
    $body = Get-AlertBody $d (Get-Alerts $d "ALERT") "task_result_tossmon_logrotate"
    Assert "TS7" "an unregistered task alerts with its own wording" `
        ($body -match "not registered") $body
}

function Test-TS8-ATimeoutKillIsDecodedToo {
    $d = New-Sandbox
    _RunWithTasks $d @((_Task "tossmon-dailyhealth" 267014 0.5))
    $body = Get-AlertBody $d (Get-Alerts $d "ALERT") "task_result_tossmon_dailyhealth"
    Assert "TS8" "0x41306 is decoded as a scheduler termination" `
        ($body -match "SCHED_S_TASK_TERMINATED") $body
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
        "Test-S4-SymbolNotFoundIsANote",
        "Test-TR1-BudgetYieldIsATradeoffNotAnAlert",
        "Test-TR2-Cooldown429IsATradeoffWithItsOwnReason",
        "Test-TR3-TradeoffBodyCarriesAllFourMandatoryFields",
        "Test-TR4-LongDurationStillDoesNotEscalate",
        "Test-TR5-ZeroTier2MembersIsANoteNotAnAlert",
        "Test-TC1-RealStallIsStillAnAlert",
        "Test-TC2-DisabledPollingIsStillAnAlert",
        "Test-TC3-YieldTurningIntoAStallIsNotDedupedAway",
        "Test-LP1-A25HourOldEventMustNotKeepAlerting",
        "Test-LP2-ARecentEventIsStillCaught",
        "Test-LP3-TheBodyCarriesTheMatchedLineTimestamp",
        "Test-LP4-AnAgedOutMatchIsRecordedNotSilent",
        "Test-LP5-ACriticalPatternKeepsTheSameBound",
        "Test-LP6-ACountingPatternIsNotTrimmedByTheBound",
        "Test-LP7-AnUndatableMatchIsKeptNotDropped",
        "Test-TS1-ATaskThatRanAndDiedRaisesAnAlert",
        "Test-TS2-TheWatchdogsOwnExitOneIsNormal",
        "Test-TS3-TheWatchdogsRestartExitIsAlsoNormal",
        "Test-TS4-AllCleanTasksAreQuiet",
        "Test-TS5-AStaleOneShotFailureIsRecordedNotAlerted",
        "Test-TS6-ARunningTaskIsNotAFailure",
        "Test-TS7-AnUnregisteredTaskIsItsOwnEvent",
        "Test-TS8-ATimeoutKillIsDecodedToo")
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
