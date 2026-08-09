<#
.SYNOPSIS
    tossmon OS-level watchdog / sentinel - owner: W5. (ASCII-only on purpose:
    Windows PowerShell 5.1 parses BOM-less scripts as ANSI, so non-ASCII here
    is a latent parse hazard. Korean docs live in docs/11 section 14.)

.DESCRIPTION
    Runs every 5 minutes from Task Scheduler (task: tossmon-watchdog), fully
    independent of any agent session. Checks:
      (a) supervisor + collector process liveness
      (b) collector.log telemetry freshness
      (c) telemetry counter advance during open sessions (frozen counters with
          a live process = API death, the docs/11 section 11-1 blind spot)
      (d) disk free space (rotate/cleanup below thresholds)
      (e) token state (expired + open session + RuntimeError spam = token path dead)
      (f) AC/battery state (alert on battery, low battery)
      (g) sentinel heartbeat (mutual watch; sentinel watches this script back)
    On failure: automatic restart through ops/launch_collector.cmd (env is
    guaranteed inside the launcher - docs/11 section 11-1), with a rolling
    restart budget so a permanently broken collector cannot be restart-hammered.
    Every action writes data/ALERT_<timestamp>_<reason>.txt and a line in
    data/watchdog.log.

    Role 'sentinel' (task: tossmon-sentinel, every 30 min) only checks the
    watchdog heartbeat and re-kicks the watchdog task if it went silent.

    If data/ops_state/STOP exists the watchdog stands down completely
    (an operator stop is intentional - never fight it).

    ---------------------------------------------------------------------------
    FILE GRADE CONTRACT - four prefixes, and what each one asks of the reader.
    (User decision 2026-08-05. The Korean copy, the census below, and the
    proof-of-failure table live in docs/34_alert_grades.md.)

      ALERT_     A FAULT. Something is broken.          -> fix it
      PLANNED_   A human did this on purpose.           -> ignore it
      NOTE_      Worked as designed; recorded for info. -> read if curious
      TRADEOFF_  The SYSTEM GAVE SOMETHING UP. Not a    -> read and decide
                 fault, but the user must judge it.

    Why TRADEOFF_ exists: on 2026-08-05 tier2_orderbook_flat fired five ALERTs
    overnight for a collector that was working exactly as designed - tier3 had
    filled to capacity for the first time and the tier2 orderbook sweep, which
    this very file elsewhere calls "the designed sacrifice order", yielded its
    budget. Two neighbouring blocks graded the same event in opposite ways
    depending on which counter you happened to look at. That is not noise, it is
    self-contradiction, and it re-broke the rule that any ALERT_ file means
    trouble.

    A TRADEOFF_ body MUST carry all four of these. If any is missing it is not a
    tradeoff record, it is a rumour:
      1. WHAT was given up  (which data stream)
      2. FOR WHAT           (where the budget went - with numbers)
      3. HOW LONG           (continuous duration of the yield)
      4. HOW MUCH           (yield rate: skipped vs attempted)

    TRADEOFF_ NEVER escalates to ALERT_, no matter how long it lasts (user
    decision). Lasting a long time is not a fault - it is the design doing what
    it does, and filing it as ALERT_ reads as "a restart would help", which
    provokes exactly the wrong response. Record duration and yield rate instead
    and let the size speak. Do not invent a new threshold.

    The four grades are counted, with a one-line legend, in the morning report
    (ops/daily_health.py) - do not assume the reader remembers what they mean.

    ADDING A NEW RESTART REASON? Decide its FAMILY first. A restart alert's
    "WAS THIS RESTART JUSTIFIED?" block is written per family, because the two
    families rest on different evidence: process-absence (process_dead /
    supervisor_dead / collector_dead) is decided by the sup/col counts, and
    progress-stall (log_stale / counters_frozen / ranking_snap_* /
    auth_failures / token_dead) by session freshness. The list lives at the
    'act' block near the end of this file; a reason not in it silently gets the
    progress-stall wording, which on 2026-08-06 told a morning reader that a
    correct sup=0 col=0 restart was "probably wrong". docs/34 section 9.

    ADDING A NEW CHECK? Decide its grade FIRST, and write down which observation
    separates a fault from a designed behaviour. If your check fires on the
    ABSENCE of something (a counter not advancing, an age exceeding a bound),
    ask: can the collector produce this absence on purpose? If yes, you must
    read the counter that proves it and grade accordingly - otherwise you are
    grading a phenomenon without knowing its cause, which is how the false
    ALERTs above happened. docs/34 section 4 holds the census of that family:
    12 absence-triggered checks, 4 of which a designed behaviour can imitate,
    3 of which now separate the cause. Add your check to that table.
#>
param(
    [ValidateSet("watchdog", "sentinel")]
    [string]$Role = "watchdog",

    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataDir = "",
    [string]$StateDir = "",

    # Process identification (overridable for sandbox tests)
    [string]$ProcName = "python.exe",
    [string]$SupervisorPattern = "ops\.supervisor",
    [string]$CollectorPattern = "tossmon\.collector",
    [string]$ExeLike = "",          # default: <RepoRoot>\.venv\*

    [string]$LauncherCmd = "",      # default: <RepoRoot>\ops\launch_collector.cmd
    [switch]$DryRunRestart,          # tests: record the restart instead of executing

    [double]$FreshCritMin = 15.0,
    [int]$FreezeStrikesToRestart = 2,
    [int]$ScratchDbMinMB = 100,      # below this a scratch .db is not worth reclaiming
    [double]$DiskSurveyGB = 8.0,
    [double]$DiskReclaimGB = 6.0,
    [double]$DiskWarnGB = 7.0,
    [double]$DiskCritGB = 5.0,
    [int]$RestartMax = 3,
    [int]$RestartWindowS = 7200,
    [double]$TokenGraceMin = 10.0,
    [int]$RuntimeErrorMin = 5,

    # W4 watchdog contract (main 27abc3f) thresholds.
    # ranking_snap_age_s is the HIGHEST-priority alarm: rankings cannot be fetched
    # retroactively, so a silently stalled ranking loop is permanent data loss.
    [int]$RankingSnapAgeCritS = 300,

    # How old an observation may be before its 'session' stops being evidence about NOW.
    # collector_state.json is only rewritten when a loop does work, so it FREEZES at the
    # last open-session value the moment the calendar closes (2026-08-04 08:55 incident:
    # a 6-minute-old file still said session=after while the collector had logged
    # "session after -> closed" at 08:50:09). Beyond this age the file is a historical
    # record, not a status - fall back to the log, or judge nothing.
    [int]$StateTrustS = 300,

    # Grace periods for the ranking-stall verdict. A ranking snapshot older than the
    # threshold only becomes evidence of a STALL once the loop has had time to take one:
    # right after a session opens, and right after a (re)start, last_ranking_snap_ms
    # legitimately still points into the previous session. -1 means "same as
    # RankingSnapAgeCritS", which is the only value that makes sense in production; they
    # exist as separate knobs so ops/watchdog_selftest.ps1 can exercise each guard alone.
    [int]$SessionOpenGraceS = -1,
    [int]$CollectorWarmupS = -1,

    [int]$AuthFailuresToRestart = 3,
    [int]$LoopErrorSurge = 500,
    [double]$FetchSuccessWarnPct = 90.0,

    [double]$WatchdogStaleS = 900,   # sentinel: watchdog heartbeat older -> alert
    [double]$SentinelStaleS = 4200,  # watchdog: sentinel heartbeat older -> alert

    # Planned-maintenance window. While active, alerts are written as PLANNED_*.txt
    # instead of ALERT_*.txt so "any ALERT file means trouble" stays true for a morning
    # glance. Self-expiring on purpose: a forgotten marker must never silence a real
    # outage for days, so it is auto-removed once it expires.
    [double]$PlannedDefaultMin = 30,

    [string]$WatchdogTaskName = "tossmon-watchdog",
    [string]$SentinelTaskName = "tossmon-sentinel",

    # Sibling scheduled tasks whose LAST RESULT this watchdog checks (see section 8).
    [string[]]$SiblingTasks = @("tossmon-dailyhealth", "tossmon-logrotate",
                                "tossmon-watchdog", "tossmon-sentinel",
                                "tossmon-collector-oneshot"),
    # Only judge a run this recent. Beyond it the result is history, not news - see the
    # comment on section 8 for why this bound exists at all.
    [double]$TaskResultMaxAgeH = 24,
    # Test seam: read task info from a JSON file instead of Task Scheduler, for the same
    # reason the disk thresholds are overridable - a unit test must not depend on the
    # real state of this machine. Empty (default) = query the live scheduler.
    [string]$TaskInfoJson = ""
)

$ErrorActionPreference = "Stop"
if ($DataDir -eq "") { $DataDir = Join-Path $RepoRoot "data" }
if ($StateDir -eq "") { $StateDir = Join-Path $DataDir "ops_state" }
if ($ExeLike -eq "") { $ExeLike = (Join-Path $RepoRoot ".venv") + "*" }
if ($SessionOpenGraceS -lt 0) { $SessionOpenGraceS = $RankingSnapAgeCritS }
if ($CollectorWarmupS -lt 0) { $CollectorWarmupS = $RankingSnapAgeCritS }
if ($LauncherCmd -eq "") { $LauncherCmd = Join-Path $RepoRoot "ops\launch_collector.cmd" }

$WatchdogLog = Join-Path $DataDir "watchdog.log"
$StateFile = Join-Path $StateDir "watchdog_state.json"
$StopFile = Join-Path $StateDir "STOP"
$HeartbeatFile = Join-Path $StateDir "watchdog_heartbeat.txt"
$PlannedFile = Join-Path $StateDir "PLANNED"
$SentinelHeartbeatFile = Join-Path $StateDir "sentinel_heartbeat.txt"
$CollectorLog = Join-Path $DataDir "collector.log"
$StateJson = Join-Path $DataDir "collector_state.json"
$TokenStateFile = Join-Path $DataDir "token_state.json"

$NowEpoch = [int][DateTimeOffset]::Now.ToUnixTimeSeconds()
$NowStamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

function Write-Log([string]$line) {
    $dir = Split-Path -Parent $WatchdogLog
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    [IO.File]::AppendAllText($WatchdogLog, "$NowStamp [$Role] $line`r`n", [Text.Encoding]::UTF8)
}

function Load-State {
    if (Test-Path $StateFile) {
        try { return Get-Content $StateFile -Raw | ConvertFrom-Json } catch { }
    }
    return New-Object PSObject
}

function Save-State($state) {
    $dir = Split-Path -Parent $StateFile
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    [IO.File]::WriteAllText($StateFile, ($state | ConvertTo-Json -Depth 6), [Text.Encoding]::UTF8)
}

function Get-Prop($obj, [string]$name, $default) {
    if ($null -ne $obj -and $null -ne $obj.PSObject.Properties[$name]) { return $obj.$name }
    return $default
}

function Set-Prop($obj, [string]$name, $value) {
    if ($null -ne $obj.PSObject.Properties[$name]) { $obj.$name = $value }
    else { $obj | Add-Member -NotePropertyName $name -NotePropertyValue $value }
}

# Planned-maintenance window: an operator drops $PlannedFile before an intentional
# stop/restart. Optional content (one key=value per line):
#     reason=rebase onto main, picking up the W4 blindspot fix
#     until=2026-08-03 10:45:00
# With no 'until' the window lasts $PlannedDefaultMin minutes from the file's mtime.
# Expired markers are DELETED here, not honoured - a forgotten marker must never turn
# into a permanent alert silencer.
function Get-PlannedWindow {
    if (-not (Test-Path $PlannedFile)) { return @{ active = $false; reason = ""; until = $null } }
    $reason = ""
    $until = $null
    try {
        foreach ($line in (Get-Content $PlannedFile -ErrorAction SilentlyContinue)) {
            if ($line -match "^\s*reason\s*=\s*(.+?)\s*$") { $reason = $Matches[1] }
            elseif ($line -match "^\s*until\s*=\s*(.+?)\s*$") {
                try { $until = [datetime]::Parse($Matches[1]) } catch { }
            }
        }
    } catch { }
    if ($null -eq $until) {
        $until = (Get-Item $PlannedFile).LastWriteTime.AddMinutes($PlannedDefaultMin)
    }
    if ((Get-Date) -gt $until) {
        try {
            Remove-Item $PlannedFile -Force
            Write-Log ("planned window EXPIRED at {0:yyyy-MM-dd HH:mm:ss} - marker auto-removed, " -f $until +
                       "alerts are live again")
        } catch { }
        return @{ active = $false; reason = $reason; until = $until }
    }
    return @{ active = $true; reason = $reason; until = $until }
}

# Alert with per-key dedup: fires once per state transition, re-fires after $repeatS.
# During a planned window (or for inherently-operator-driven keys) the file is written
# as PLANNED_*.txt with a marker header, so an unattended morning glance can keep using
# the simple rule "any ALERT_* file means something went wrong".
function Raise-Alert($state, [string]$key, [string]$level, [string]$body,
                     [double]$repeatS = 21600, [bool]$alwaysPlanned = $false) {
    $alerts = Get-Prop $state "alert_last" (New-Object PSObject)
    $last = Get-Prop $alerts $key 0
    if (($NowEpoch - $last) -lt $repeatS) {
        Write-Log "ALERT-SUPPRESSED key=$key (deduped)"
        return $false
    }
    Set-Prop $alerts $key $NowEpoch
    Set-Prop $state "alert_last" $alerts

    # Prefix decides what a morning glance means. See the FILE GRADE CONTRACT in the
    # header for the full four-grade table and the rule for adding new checks.
    #   ALERT_ = fault, PLANNED_ = a human did it, NOTE_ = worked as designed,
    #   TRADEOFF_ = the system gave something up and the user must judge it.
    # Filing a designed behaviour as ALERT_ re-breaks "any ALERT_ file is trouble".
    $planned = $alwaysPlanned -or $script:PlannedNow.active
    $prefix = "ALERT"
    if ($level -eq "INFO") { $prefix = "NOTE" }
    if ($level -eq "TRADEOFF") { $prefix = "TRADEOFF" }
    $header = ""
    if ($planned) {
        $prefix = "PLANNED"
        $why = $script:PlannedNow.reason
        if ($alwaysPlanned -and $why -eq "") { $why = "operator-driven action (STOP file)" }
        if ($why -eq "") { $why = "(no reason recorded)" }
        $hdrUntil = ""
        if ($null -ne $script:PlannedNow.until -and -not $alwaysPlanned) {
            $hdrUntil = " window until {0:yyyy-MM-dd HH:mm:ss}" -f $script:PlannedNow.until
        }
        $header = "PLANNED MAINTENANCE - this was expected, not an outage.$hdrUntil`r`n" +
                  "reason: $why`r`n" +
                  "(Written as PLANNED_ instead of ALERT_ so that any ALERT_ file still means trouble.)`r`n`r`n"
    }
    $fname = "{0}_{1}_{2}.txt" -f $prefix, (Get-Date -Format "yyyyMMdd_HHmmss"), $key
    $path = Join-Path $DataDir $fname
    if ($null -eq $body -or $body.Trim() -eq "") {
        # An alert with no body is not an alert. If the caller handed us nothing, say so
        # in the file itself rather than leaving a mystery for the morning reader.
        $body = "(no detail was supplied by the check that raised this - this is itself a " +
                "defect in ops/watchdog.ps1; the key above says which check it was)"
        Write-Log "ALERT-BODY-EMPTY key=$key - wrote a placeholder body, fix the caller"
    }
    $text = "$header[$level] $NowStamp  key=$key`r`n`r`n$body`r`n"
    # No BOM: these are plain-text files read by people and by simple tools, and a BOM
    # makes the first line look corrupt in some readers.
    [IO.File]::WriteAllText($path, $text, [Text.UTF8Encoding]::new($false))
    # Verify what actually landed on disk. A zero-byte alert file is a silent failure of
    # the alerting path itself, which is the worst possible place to have one.
    $written = 0
    try { $written = (Get-Item $path -ErrorAction Stop).Length } catch { }
    if ($written -le 0) {
        Write-Log "ALERT-WRITE-FAILED key=$key file=$fname wrote ${written} bytes - retrying once"
        try {
            [IO.File]::WriteAllText($path, $text, [Text.UTF8Encoding]::new($false))
            $written = (Get-Item $path -ErrorAction Stop).Length
        } catch { }
        if ($written -le 0) {
            Write-Log "ALERT-WRITE-FAILED-TWICE key=$key file=$fname - alert content is LOST, detail follows in this log"
            Write-Log ("lost alert body [$level] key=$key : " + ($body -replace "`r?`n", " "))
        }
    }
    Write-Log "$prefix[$level] key=$key file=$fname bytes=$written"
    return $true
}

function Clear-AlertKey($state, [string]$key) {
    $alerts = Get-Prop $state "alert_last" $null
    if ($null -ne $alerts -and $null -ne $alerts.PSObject.Properties[$key]) {
        $alerts.PSObject.Properties.Remove($key)
    }
}

function Get-TossProcs([string]$pattern) {
    # $PID exclusion: when patterns are passed as CLI arguments (sandbox tests) the
    # watchdog's own command line would otherwise match itself.
    Get-CimInstance Win32_Process -Filter "Name='$ProcName'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ProcessId -ne $PID -and
            $_.CommandLine -match $pattern -and
            ($ExeLike -eq "*" -or ($_.ExecutablePath -and $_.ExecutablePath -like $ExeLike))
        }
}

# Telemetry from the log, scanned by TIME rather than by line count.
#
# 2026-08-03 incident: this used a fixed `-Tail 400`. Telemetry is emitted every 5
# minutes, but tier-promotion logging can produce ~300 lines/minute, so 400 lines covered
# roughly 1.5 minutes and contained ZERO telemetry lines. The watchdog went blind for
# 20+ minutes while the collector was perfectly healthy. A window the log volume can
# outrun is not a window - so grow the tail until it actually spans $minutes, with a
# hard cap so a runaway log cannot make this expensive.
function Get-LastTelemetry([double]$minutes = 20.0, [int]$maxLines = 20000) {
    if (-not (Test-Path $CollectorLog)) { return $null }
    $want = 800
    while ($true) {
        $tail = Get-Content $CollectorLog -Tail $want -ErrorAction SilentlyContinue
        if ($null -eq $tail -or $tail.Count -eq 0) { return $null }
        $line = $tail | Where-Object { $_ -match "telemetry session=" } | Select-Object -Last 1
        if ($null -ne $line) {
            if ($line -match "^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+\S+\s+telemetry session=(\S+)\s+(.*)$") {
                return @{
                    ts = [datetime]::ParseExact($Matches[1], "yyyy-MM-dd HH:mm:ss", $null)
                    ts_str = $Matches[1]
                    session = $Matches[2]
                    counters = $Matches[3].Trim()
                    scanned = $tail.Count
                }
            }
            return $null   # found but unparseable: format drift, report as such
        }
        # Did this tail already span the requested time? If so, there is genuinely no
        # telemetry in the window and reading more lines will not help.
        $spanned = $false
        $first = $tail | Where-Object { $_ -match "^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})," } | Select-Object -First 1
        if ($null -ne $first -and $first -match "^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),") {
            $spanned = ((Get-Date) - [datetime]::ParseExact($Matches[1], "yyyy-MM-dd HH:mm:ss", $null)).TotalMinutes -ge $minutes
        }
        if ($spanned -or $tail.Count -lt $want -or $want -ge $maxLines) { return $null }
        $want = [math]::Min($want * 4, $maxLines)
    }
}

# Collector state file -> the same normalized shape. This is the PRIMARY source: it is a
# small fixed-size JSON the collector rewrites every loop, so unlike the log it cannot be
# outrun by log volume. Counters absent from the file mean zero (the collector only
# stores keys it has bumped), so the known contract keys are filled in explicitly - that
# way a delta check sees 0 rather than "unsupported".
$script:W4_COUNTER_KEYS = @(
    "auth_failures", "loop_errors", "schema_mismatch", "symbol_not_found",
    "event_write_failures", "promotion_write_failures", "rankings_write_failures",
    "rankings_clamped", "prices_missing", "candles_1m", "api_errors", "tier2_orderbook_snaps"
)
function Get-TelemetryFromState {
    if (-not (Test-Path $StateJson)) { return $null }
    try {
        $j = Get-Content $StateJson -Raw -ErrorAction Stop | ConvertFrom-Json
    } catch { return $null }
    if ($null -eq $j -or $null -eq $j.saved_ms) { return $null }
    $ts = [DateTimeOffset]::FromUnixTimeMilliseconds([long]$j.saved_ms).LocalDateTime
    $c = @{}
    if ($null -ne $j.counters) {
        foreach ($p in $j.counters.PSObject.Properties) {
            $v = 0.0
            if ([double]::TryParse([string]$p.Value, [ref]$v)) { $c[$p.Name] = $v }
        }
    }
    foreach ($k in $script:W4_COUNTER_KEYS) { if (-not $c.ContainsKey($k)) { $c[$k] = 0.0 } }
    # derived values the telemetry line computes but the state file stores as parts
    $seen = 0.0; $miss = 0.0
    if ($c.ContainsKey("prices_seen")) { $seen = $c["prices_seen"] }
    if ($c.ContainsKey("prices_missing")) { $miss = $c["prices_missing"] }
    $c["fetch_success_pct"] = 100.0
    if (($seen + $miss) -gt 0) { $c["fetch_success_pct"] = [math]::Round(100.0 * $seen / ($seen + $miss), 1) }
    $c["tier2_orderbook_skipped"] = 0.0
    foreach ($k in @("tier2_orderbook_skipped_rate", "tier2_orderbook_skipped_429")) {
        if ($c.ContainsKey($k)) { $c["tier2_orderbook_skipped"] += $c[$k] }
    }
    # How many symbols the tier2 orderbook sweep actually iterates. It walks members(2)
    # ONLY - tier3 is excluded because its own loop polls far more densely - so the right
    # population is "tier == 2", not "tier >= 2". Getting this wrong would make a busy
    # tier3 look like a populated tier2 and hide the "nothing to poll" case.
    $c["tier2_members"] = -1.0
    $c["tier3_members"] = -1.0
    if ($null -ne $j.tiers) {
        $n2 = 0; $n3 = 0
        foreach ($p in $j.tiers.PSObject.Properties) {
            if ([int]$p.Value -eq 2) { $n2++ } elseif ([int]$p.Value -eq 3) { $n3++ }
        }
        $c["tier2_members"] = [double]$n2
        $c["tier3_members"] = [double]$n3
    }
    # ranking_snap_age_s is measured against saved_ms - the instant this observation was
    # taken - NOT against the wall clock. Against 'now' the value grows without bound the
    # moment the file stops being rewritten, so a perfectly healthy collector sitting in a
    # closed session crosses any threshold just by waiting (2026-08-04 08:55: 382 s while
    # the collector's own telemetry 44 s earlier said 311 s and session=closed). Measured
    # against saved_ms it means what the alarm assumes it means: "how far behind was the
    # ranking loop at the last moment we could actually see it". How stale that sighting
    # is, is a separate fact and is reported separately as age_s.
    $c["ranking_snap_age_s"] = -1.0
    if ($null -ne $j.last_ranking_snap_ms -and [long]$j.last_ranking_snap_ms -gt 0) {
        $c["ranking_snap_age_s"] = [math]::Max(0,
            [math]::Floor(([double]$j.saved_ms - [double]$j.last_ranking_snap_ms) / 1000.0))
    }
    $lastSnapStr = "(never)"
    if ($null -ne $j.last_ranking_snap_ms -and [long]$j.last_ranking_snap_ms -gt 0) {
        $lastSnapStr = [DateTimeOffset]::FromUnixTimeMilliseconds(
            [long]$j.last_ranking_snap_ms).LocalDateTime.ToString("yyyy-MM-dd HH:mm:ss")
    }
    $sess = "unknown"
    if ($null -ne $j.session) { $sess = [string]$j.session }
    $sig = (($c.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join " ")
    return @{ ts = $ts; ts_str = $ts.ToString("yyyy-MM-dd HH:mm:ss"); session = $sess
              counters = $c; counters_sig = $sig; source = "state"; last_snap_str = $lastSnapStr
              skip_split_available = $true }
}

# Read the collector's health from the best available source, and say clearly when it
# could not be read at all. Being unable to see is itself an incident: the counter-freeze
# detection - the only early signal we have for a silent API death - is dead while blind.
#
# 2026-08-04 08:55 incident: the state file was accepted as authoritative at up to
# $FreshCritMin (15 min) old. But the collector only rewrites it when a loop does work, so
# entering a closed session freezes it mid-sentence - it kept saying session=after for the
# whole 08:50-09:00 KST calendar hole while the collector had already logged
# "session after -> closed". The watchdog believed the frozen word, decided the session was
# open, and restarted a perfectly healthy collector. A restart is this project's #1 cause of
# data loss, and that hole exists every single day.
#
# So the state file only speaks for the present while it is younger than $StateTrustS.
# Past that it is history: fall back to the telemetry line (which the collector keeps
# emitting every 5 minutes even in a closed session, and which therefore carries the TRUE
# session), and if that is unavailable too, say the session is unknown and judge nothing.
function Get-CollectorSnapshot {
    $tried = @()
    $frozen = $null
    $s = Get-TelemetryFromState
    if ($null -ne $s) {
        $ageS = ((Get-Date) - $s.ts).TotalSeconds
        if ($ageS -le $StateTrustS) { return @{ ok = $true; snap = $s; reason = ""; frozen = $null } }
        # Keep the frozen reading for the report - "what the stale file claimed" is exactly
        # the fact a morning reader needs to tell a false alarm from a real one.
        $frozen = @{ session = $s.session; ts_str = $s.ts_str; age_s = [int]$ageS }
        $tried += ("collector_state.json is {0}s old (trust window {1}s) - the collector " -f [int]$ageS, $StateTrustS +
                   "stopped rewriting it at $($s.ts_str), so its session='$($s.session)' is a " +
                   "historical record, not a status")
    } elseif (Test-Path $StateJson) {
        $tried += "collector_state.json exists but could not be parsed (truncated or corrupt JSON?)"
    } else {
        $tried += "collector_state.json does not exist at $StateJson"
    }
    $t = Get-LastTelemetry
    if ($null -ne $t) {
        # Falling back must not mean falling BACKWARDS. If the newest telemetry line is
        # older than the state file we just rejected, the log is the worse observation and
        # using it would understate freshness - straight into a log_stale restart of a
        # collector we can see was alive more recently. Keep the newer sighting; the trust
        # gate downstream still refuses to read a session off it.
        if ($null -ne $s -and $s.ts -gt $t.ts) {
            return @{ ok = $true; snap = $s; frozen = $frozen
                      reason = ("state file too old to be a status but still newer than the " +
                                "newest telemetry line ($($t.ts_str)); kept it as the observation, " +
                                "session not trusted: " + ($tried -join "; ")) }
        }
        $t["counters_raw"] = [string]$t.counters      # budget segment lives here only
        $t["counters"] = Parse-Counters $t.counters
        # The telemetry line prints tier2 = len(at_least(2)), i.e. tier2 AND tier3, but the
        # tier2 orderbook sweep walks members(2) only. Subtract tier3 to get the population
        # the loop actually iterates.
        $t.counters["tier2_members"] = -1.0
        $t.counters["tier3_members"] = -1.0
        if ($t.counters.ContainsKey("tier2") -and $t.counters.ContainsKey("tier3")) {
            $t.counters["tier2_members"] = [math]::Max(0.0, $t.counters["tier2"] - $t.counters["tier3"])
            $t.counters["tier3_members"] = $t.counters["tier3"]
        }
        # The log line only carries the COMBINED tier2_orderbook_skipped; the per-reason
        # split (rate vs 429) exists solely in collector_state.json. Say so rather than
        # letting a missing key read as "no skips".
        $t["skip_split_available"] = $false
        $t["counters_sig"] = (($t.counters.GetEnumerator() | Sort-Object Name |
            ForEach-Object { "$($_.Name)=$($_.Value)" }) -join " ")
        $t["source"] = "log"
        $t["last_snap_str"] = "(not recorded in the telemetry line)"
        return @{ ok = $true; snap = $t; frozen = $frozen
                  reason = ("state file not usable as a status, fell back to log: " + ($tried -join "; ")) }
    }
    if (-not (Test-Path $CollectorLog)) { $tried += "collector.log does not exist at $CollectorLog" }
    else {
        $sz = [math]::Round((Get-Item $CollectorLog).Length / 1MB, 1)
        $tried += ("no parseable 'telemetry session=' line within the time-based log scan " +
                   "(log is ${sz}MB, last modified $((Get-Item $CollectorLog).LastWriteTime.ToString('HH:mm:ss')))")
    }
    return @{ ok = $false; snap = $null; reason = ($tried -join "; "); frozen = $frozen }
}

# Youngest live collector process, in seconds. A collector that started moments ago legally
# carries a large ranking_snap_age_s: last_ranking_snap_ms is RESUMED from the state file,
# so it still points at the previous session. Judging a stall on that would make the
# watchdog restart-loop a healthy collector. -1 means "could not tell" and every caller
# treats that as "no grace", i.e. behaves exactly as before this guard existed.
function Get-CollectorUptimeS($procs) {
    $best = -1.0
    foreach ($p in @($procs)) {
        try {
            $started = $p.CreationDate
            if ($null -eq $started) { continue }
            if ($started -isnot [datetime]) { $started = [Management.ManagementDateTimeConverter]::ToDateTime([string]$started) }
            $u = ((Get-Date) - $started).TotalSeconds
            if ($best -lt 0 -or $u -lt $best) { $best = $u }
        } catch { }
    }
    return $best
}

# Parse "k=v k=v ..." telemetry counters into a hashtable (W4 contract, main 27abc3f).
# The budget section after "|" is skipped - it is a rate display, not a counter.
function Parse-Counters([string]$counters) {
    $out = @{}
    if ($null -eq $counters) { return $out }
    $head = ($counters -split "\|")[0]
    foreach ($m in [regex]::Matches($head, "([a-z0-9_]+)=(-?[0-9]+(?:\.[0-9]+)?)")) {
        $out[$m.Groups[1].Value] = [double]$m.Groups[2].Value
    }
    return $out
}

function Get-LogTail([int]$lines = 400) {
    if (-not (Test-Path $CollectorLog)) { return @() }
    $t = Get-Content $CollectorLog -Tail $lines -ErrorAction SilentlyContinue
    if ($null -eq $t) { return @() }
    return $t
}

# A collector.log line starts with 'yyyy-MM-dd HH:mm:ss,mmm'. Returns $null when the line
# has no such prefix (continuation lines of a traceback, for instance). Callers must treat
# $null as "cannot date this" and KEEP the line - never as "old".
function Get-LogLineTime([string]$line) {
    if ($line -match "^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.]\d+") {
        try { return [datetime]::ParseExact($Matches[1], "yyyy-MM-dd HH:mm:ss", $null) } catch { return $null }
    }
    return $null
}

function Get-PowerInfo {
    $bat = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $bat) { return @{ has_battery = $false; on_ac = $true; pct = 100 } }
    $online = $true
    try {
        $bs = Get-CimInstance -Namespace root\wmi -ClassName BatteryStatus -ErrorAction Stop |
            Select-Object -First 1
        if ($null -ne $bs) { $online = [bool]$bs.PowerOnline }
    } catch {
        # fall back: Win32_Battery.BatteryStatus 2 = on AC
        $online = ($bat.BatteryStatus -eq 2)
    }
    return @{ has_battery = $true; on_ac = $online; pct = [int]$bat.EstimatedChargeRemaining }
}

function Get-FreeGB {
    $drive = [IO.Path]::GetPathRoot((Resolve-Path $DataDir).Path)
    $du = [IO.DriveInfo]::new($drive)
    return [math]::Round($du.AvailableFreeSpace / 1GB, 2)
}

# Bounded top-consumer survey. A full-drive walk every 5 minutes is far too expensive,
# so this looks only where disk actually disappears on this machine: dynamically
# expanding virtual disks (WSL / Claude VM - they grow and never shrink on their own),
# agent scratchpads under Temp, and our own data dir.
function Get-TopConsumers([int]$top = 8) {
    $items = @()
    $probeDirs = @(
        (Join-Path $env:LOCALAPPDATA "wsl"),
        (Join-Path $env:LOCALAPPDATA "Packages"),
        (Join-Path $env:LOCALAPPDATA "Docker")
    )
    foreach ($d in $probeDirs) {
        if (-not (Test-Path $d)) { continue }
        try {
            Get-ChildItem $d -Recurse -File -Force -Filter "*.vhdx" -ErrorAction SilentlyContinue |
                ForEach-Object { $items += [PSCustomObject]@{ MB = [math]::Round($_.Length/1MB,1); Path = $_.FullName } }
        } catch { }
    }
    $tempClaude = Join-Path $env:TEMP "claude"
    if (Test-Path $tempClaude) {
        try {
            Get-ChildItem $tempClaude -Directory -ErrorAction SilentlyContinue | ForEach-Object {
                $sz = (Get-ChildItem $_.FullName -Recurse -File -Force -ErrorAction SilentlyContinue |
                    Measure-Object Length -Sum).Sum
                if ($sz -gt 50MB) {
                    $items += [PSCustomObject]@{ MB = [math]::Round($sz/1MB,1); Path = $_.FullName }
                }
            }
            # Call out scratch DB piles explicitly - the 2026-08-03 incident was 8 copies
            # inside one scratchpad, which a per-directory total does not make obvious.
            $dbs = @(Get-ChildItem $tempClaude -Recurse -File -Force -Filter "*.db" -ErrorAction SilentlyContinue |
                Where-Object { $_.Length -ge ($ScratchDbMinMB * 1MB) })
            foreach ($g in ($dbs | Group-Object DirectoryName)) {
                if ($g.Count -lt 2) { continue }
                $tot = ($g.Group | Measure-Object Length -Sum).Sum
                $items += [PSCustomObject]@{
                    MB = [math]::Round($tot/1MB,1)
                    Path = "$($g.Name) [$($g.Count) scratch DB copies - reclaimable except newest]"
                }
            }
        } catch { }
    }
    try {
        $sz = (Get-ChildItem $DataDir -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
        $items += [PSCustomObject]@{ MB = [math]::Round($sz/1MB,1); Path = "$DataDir (collection data)" }
    } catch { }
    return ($items | Sort-Object MB -Descending | Select-Object -First $top)
}

# Is another process holding this file open? Opening with FileShare.None fails if so.
# Cheap, and far more trustworthy than guessing from timestamps.
function Test-FileInUse([string]$path) {
    try {
        $fs = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        $fs.Close(); $fs.Dispose()
        return $false
    } catch [IO.IOException] {
        return $true
    } catch {
        return $true   # unreadable for any other reason: treat as in use, never delete
    }
}

# Delete a file plus its SQLite sidecars. Returns MB actually freed.
function Remove-DbFile([string]$path) {
    $mb = 0.0
    foreach ($p in @($path, "$path-wal", "$path-shm")) {
        if (-not (Test-Path $p)) { continue }
        try {
            $sz = (Get-Item $p).Length / 1MB
            Remove-Item $p -Force -ErrorAction Stop
            $mb += $sz
        } catch { }
    }
    return [math]::Round($mb, 1)
}

# Agent scratch DB copies (2026-08-03 incident). Analysis workers copy the ~500MB
# collection DB into their scratchpad to avoid lock contention - that part is correct -
# but a fresh numbered copy each run (live_snapshot.db, live2.db ... live8.db) piled up
# to 3.9GB in one day. The age-based rule missed every one of them because they were all
# created that same day, so the watchdog alerted three times and reclaimed 0MB.
# Rule: within one scratchpad directory, keep the NEWEST copy (an analysis may be using
# it) and reclaim the older siblings regardless of age. Files still held open are always
# skipped. This is the second line of defence behind "workers should reuse one copy".
function Invoke-ScratchDbReclaim([int]$minMB = 100) {
    $freed = 0.0
    $items = @()
    $tempClaude = Join-Path $env:TEMP "claude"
    if (-not (Test-Path $tempClaude)) { return @{ freed = 0.0; items = $items } }
    $dbs = @()
    try {
        $dbs = @(Get-ChildItem $tempClaude -Recurse -File -Force -Filter "*.db" -ErrorAction SilentlyContinue |
            Where-Object { $_.Length -ge ($minMB * 1MB) })
    } catch { }
    if ($dbs.Count -eq 0) { return @{ freed = 0.0; items = $items } }
    foreach ($grp in ($dbs | Group-Object DirectoryName)) {
        if ($grp.Count -lt 2) { continue }   # a lone copy is someone's working set
        $ordered = @($grp.Group | Sort-Object LastWriteTime -Descending)
        $keep = $ordered[0]
        Write-Log ("scratch-db: {0} copies in {1}; keeping newest {2} ({3}MB, {4:HH:mm:ss})" -f `
            $grp.Count, $grp.Name, $keep.Name, [math]::Round($keep.Length/1MB,1), $keep.LastWriteTime)
        foreach ($old in ($ordered | Select-Object -Skip 1)) {
            if (Test-FileInUse $old.FullName) {
                Write-Log "scratch-db: SKIP $($old.FullName) - file is open by another process"
                continue
            }
            $mb = Remove-DbFile $old.FullName
            if ($mb -gt 0) {
                $freed += $mb
                $items += "$($old.FullName) (${mb}MB, superseded copy)"
                Write-Log "reclaim: deleted $($old.FullName) (${mb}MB, superseded scratch DB copy)"
            }
        }
    }
    return @{ freed = [math]::Round($freed,1); items = $items }
}

# Reclaim what is genuinely ours to reclaim. Never touches the live DB, never touches a
# file another process holds open, and never touches the VHDX files - those need an
# elevated compaction the watchdog cannot do. Returns both the total and an itemised
# list, because a bare "0MB reclaimed" is what made today's incident so slow to diagnose.
function Invoke-Reclaim([int]$staleH = 24) {
    $freed = 0.0
    $items = @()
    # (1) superseded agent scratch DB copies - age-independent, newest always preserved
    $scratch = Invoke-ScratchDbReclaim $ScratchDbMinMB
    $freed += $scratch.freed
    $items += $scratch.items

    $cut = (Get-Date).AddHours(-$staleH)
    $tempClaude = Join-Path $env:TEMP "claude"
    if (Test-Path $tempClaude) {
        foreach ($pat in @("*.db", "*.db-wal", "*.db-shm", "*.log", "*.zip", "*.tar", "*.tmp")) {
            try {
                Get-ChildItem $tempClaude -Recurse -File -Force -Filter $pat -ErrorAction SilentlyContinue |
                    Where-Object { $_.LastWriteTime -lt $cut -and $_.Length -gt 1MB } |
                    ForEach-Object {
                        if (Test-FileInUse $_.FullName) {
                            Write-Log "reclaim: SKIP $($_.FullName) - file is open by another process"
                            return
                        }
                        $mb = [math]::Round($_.Length/1MB,1)
                        try { Remove-Item $_.FullName -Force -ErrorAction Stop; $freed += $mb
                              $items += "$($_.FullName) (${mb}MB, stale ${staleH}h+)"
                              Write-Log "reclaim: deleted $($_.FullName) (${mb}MB, stale ${staleH}h+)" } catch { }
                    }
            } catch { }
        }
    }
    # rotated/compressed logs anywhere in our data dir, beyond the retention the
    # rotate_logs policy already implies
    try {
        Get-ChildItem $DataDir -File -Force -Filter "*.gz" -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } |
            ForEach-Object {
                $mb = [math]::Round($_.Length/1MB,1)
                try { Remove-Item $_.FullName -Force -ErrorAction Stop; $freed += $mb
                      $items += "$($_.Name) (${mb}MB, rotated log 7d+)"
                      Write-Log "reclaim: deleted rotated log $($_.Name) (${mb}MB)" } catch { }
            }
    } catch { }
    # user-scope Windows temp leftovers (no elevation needed, 7d+ untouched)
    try {
        Get-ChildItem $env:TEMP -File -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) -and $_.Length -gt 10MB } |
            ForEach-Object {
                if (Test-FileInUse $_.FullName) { return }
                $mb = [math]::Round($_.Length/1MB,1)
                try { Remove-Item $_.FullName -Force -ErrorAction Stop; $freed += $mb
                      $items += "$($_.Name) (${mb}MB, user temp 7d+)"
                      Write-Log "reclaim: deleted temp $($_.Name) (${mb}MB)" } catch { }
            }
    } catch { }
    if ($freed -le 0) { Write-Log "reclaim: nothing eligible - freed 0MB" }
    return @{ freed = [math]::Round($freed, 1); items = $items }
}

function Invoke-Restart($state, [string]$reason) {
    # rolling restart budget - a broken collector must not be restart-hammered
    $hist = @(@(Get-Prop $state "restarts" @()) | Where-Object { ($NowEpoch - $_) -lt $RestartWindowS })
    if ($hist.Count -ge $RestartMax) {
        Raise-Alert $state "restart_budget_exhausted" "CRIT" (
            "Watchdog wanted to restart the collector (reason: $reason) but the restart budget " +
            "($RestartMax per $RestartWindowS s) is exhausted. NOT restarting. " +
            "Manual intervention required. Check data/watchdog.log and recent ALERT files.") 3600 | Out-Null
        return $false
    }

    if ($DryRunRestart) {
        Set-Prop $state "restarts" (@($hist) + $NowEpoch)
        Set-Prop $state "last_restart_dryrun" "$NowStamp reason=$reason launcher=$LauncherCmd"
        Write-Log "RESTART-DRYRUN reason=$reason (would run: $LauncherCmd)"
        return $true
    }

    # kill remnants first so a half-dead tree cannot double-run against the API lease
    $rem = @(Get-TossProcs $SupervisorPattern) + @(Get-TossProcs $CollectorPattern)
    foreach ($p in $rem) {
        try { & taskkill /PID $p.ProcessId /T /F 2>&1 | Out-Null } catch { }
    }
    if ($rem.Count -gt 0) { Start-Sleep -Seconds 3 }

    Write-Log "RESTART reason=$reason launcher=$LauncherCmd"
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$LauncherCmd`"" `
        -WindowStyle Hidden -WorkingDirectory $RepoRoot
    Start-Sleep -Seconds 25

    $supAfter = @(Get-TossProcs $SupervisorPattern)
    $colAfter = @(Get-TossProcs $CollectorPattern)
    $ok = ($supAfter.Count -ge 1 -and $colAfter.Count -ge 1)
    Set-Prop $state "restarts" (@($hist) + $NowEpoch)
    $outcome = "FAILED"
    if ($ok) { $outcome = "OK" }
    Raise-Alert $state "restarted_$reason" "WARN" (
        "Watchdog restarted the collector. reason=$reason outcome=$outcome`r`n" +
        "supervisor_procs=$($supAfter.Count) collector_procs=$($colAfter.Count)`r`n" +
        "If outcome=FAILED check data/supervisor.stdout.log and data/collector.log tails.") 60 | Out-Null
    Write-Log "RESTART outcome=$outcome sup=$($supAfter.Count) col=$($colAfter.Count)"
    return $ok
}

# Evaluate once per run, before anything can raise an alert (also performs the
# auto-expiry of a stale marker).
$script:PlannedNow = Get-PlannedWindow
if ($script:PlannedNow.active) {
    Write-Log ("planned window ACTIVE until {0:yyyy-MM-dd HH:mm:ss} - alerts this cycle are " -f $script:PlannedNow.until +
               "written as PLANNED_ (reason: $($script:PlannedNow.reason))")
}

# ---------------- sentinel role ----------------
if ($Role -eq "sentinel") {
    $state = Load-State
    [IO.File]::WriteAllText($SentinelHeartbeatFile, "$NowStamp epoch=$NowEpoch", [Text.Encoding]::UTF8)
    $ok = $true
    if (Test-Path $HeartbeatFile) {
        $age = $NowEpoch - [int](Get-Item $HeartbeatFile).LastWriteTimeUtc.Subtract(
            [datetime]'1970-01-01').TotalSeconds
        if ($age -gt $WatchdogStaleS) {
            $ok = $false
            Raise-Alert $state "watchdog_silent" "CRIT" (
                "Watchdog heartbeat is $([int]$age)s old (threshold $WatchdogStaleS s). " +
                "Re-kicking task '$WatchdogTaskName'.") 3600 | Out-Null
            try { & schtasks /Run /TN $WatchdogTaskName 2>&1 | Out-Null } catch { }
        }
    } else {
        $ok = $false
        Raise-Alert $state "watchdog_heartbeat_missing" "CRIT" (
            "Watchdog heartbeat file does not exist: $HeartbeatFile. " +
            "Re-kicking task '$WatchdogTaskName'.") 3600 | Out-Null
        try { & schtasks /Run /TN $WatchdogTaskName 2>&1 | Out-Null } catch { }
    }
    if ($ok) { Write-Log "sentinel OK (watchdog heartbeat fresh)" }
    Save-State $state
    exit 0
}

# ---------------- watchdog role ----------------
$state = Load-State
[IO.File]::WriteAllText($HeartbeatFile, "$NowStamp epoch=$NowEpoch", [Text.Encoding]::UTF8)

# 0. operator STOP -> stand down completely
if (Test-Path $StopFile) {
    if (-not (Get-Prop $state "stop_seen" $false)) {
        Set-Prop $state "stop_seen" $true
        # A STOP file is by definition an operator action, so this is always PLANNED -
        # it must never look like an outage on the morning check.
        Raise-Alert $state "stop_observed" "INFO" (
            "STOP file present ($StopFile) - watchdog standing down (no checks, no restarts) " +
            "until the STOP file is removed. This is the intended operator-stop path.") 1 $true | Out-Null
    }
    Write-Log "STOP present - standing down"
    Save-State $state
    exit 0
}
if (Get-Prop $state "stop_seen" $false) {
    Set-Prop $state "stop_seen" $false
    Clear-AlertKey $state "stop_observed"
    Write-Log "STOP cleared - resuming watch"
}

$problems = @()
$restartReason = $null

# (a) process liveness
$sup = @(Get-TossProcs $SupervisorPattern)
$col = @(Get-TossProcs $CollectorPattern)
if ($sup.Count -eq 0 -and $col.Count -eq 0) {
    $problems += "both_dead"
    $restartReason = "process_dead"
} elseif ($sup.Count -eq 0) {
    # collector alive but unsupervised: a later crash would never be restarted
    $problems += "supervisor_dead"
    $restartReason = "supervisor_dead"
} elseif ($col.Count -eq 0) {
    # supervisor alive, collector gone - give the supervisor's own backoff one
    # cycle (up to 300 s) before stepping in
    $strikes = (Get-Prop $state "collector_missing_strikes" 0) + 1
    Set-Prop $state "collector_missing_strikes" $strikes
    if ($strikes -ge 2) {
        $problems += "collector_dead_x$strikes"
        $restartReason = "collector_dead"
    } else {
        Write-Log "collector missing (strike 1) - letting supervisor backoff work"
    }
} else {
    Set-Prop $state "collector_missing_strikes" 0
}

# (b)+(c) telemetry freshness and counter advance
$snapshot = Get-CollectorSnapshot
$tele = $snapshot.snap
$teleSource = "none"
if ($null -ne $tele) { $teleSource = $tele.source }
$session = "unknown"
$sessionRaw = "unknown"     # what the source literally said, before the trust test
$obsAgeS = -1               # how old the observation itself is
$sessionTrusted = $false
$ageMin = -1
if ($null -ne $tele) {
    $obsAgeS = [int]((Get-Date) - $tele.ts).TotalSeconds
    $sessionRaw = $tele.session
    # An observation older than the trust window says nothing about NOW. Its session is
    # downgraded to 'unknown', which every stall check below reads as "do not judge".
    # This is the guard that would have prevented the 2026-08-04 08:55 restart even if the
    # log fallback had also been unavailable.
    $sessionTrusted = ($obsAgeS -le $StateTrustS)
    $session = $sessionRaw
    if (-not $sessionTrusted) { $session = "unknown" }
    $ageMin = [math]::Round(((Get-Date) - $tele.ts).TotalMinutes, 1)
    if ($ageMin -gt $FreshCritMin -and $null -eq $restartReason -and $col.Count -ge 1) {
        # process alive but log dead
        $problems += "log_stale_${ageMin}min"
        $restartReason = "log_stale"
    }
    $prev = Get-Prop $state "last_telemetry" $null
    # captured before last_telemetry is overwritten below - the W4 contract block needs
    # to know whether this cycle is looking at a genuinely new telemetry line
    $prevTeleTs = ""
    if ($null -ne $prev) { $prevTeleTs = Get-Prop $prev "ts" "" }
    # 'unknown' is NOT open. It used to be (the test was only -ne "closed"), which meant an
    # unreadable session silently enabled every stall check. Not knowing is a reason to
    # abstain, not a reason to act.
    $openNow = ($session -ne "closed" -and $session -ne "unknown")
    if ($null -ne $prev -and $openNow -and (Get-Prop $prev "session" "closed") -ne "closed") {
        if ($tele.counters_sig -eq (Get-Prop $prev "counters" "") -and $tele.ts_str -ne (Get-Prop $prev "ts" "")) {
            $strikes = (Get-Prop $state "freeze_strikes" 0) + 1
            Set-Prop $state "freeze_strikes" $strikes
            if ($strikes -ge $FreezeStrikesToRestart -and $null -eq $restartReason) {
                $problems += "counters_frozen_x$strikes"
                $restartReason = "counters_frozen"
            }
        } else {
            Set-Prop $state "freeze_strikes" 0
        }
    } elseif (-not $openNow) {
        # closed session: frozen counters are NORMAL - never alarm on them here
        Set-Prop $state "freeze_strikes" 0
    }
    Set-Prop $state "last_telemetry" ([PSCustomObject]@{
        ts = $tele.ts_str; session = $session; counters = $tele.counters_sig })
} else {
    # BLIND. This is an incident in its own right, not a log note: while the watchdog
    # cannot read the collector's health, counter-freeze detection is dead - and that is
    # the only early signal we have for a silent API death (docs/11 section 11-1).
    # On 2026-08-03 this state persisted 20+ minutes and produced no alert at all, so the
    # watchdog knew it was blind and told nobody. It must never be quiet again.
    $problems += "no_telemetry"
    Raise-Alert $state "no_telemetry" "CRIT" (
        "THE WATCHDOG IS BLIND - it cannot read the collector's health.`r`n`r`n" +
        "Why it could not read:`r`n  $($snapshot.reason)`r`n`r`n" +
        "Processes seen this cycle: supervisor=$($sup.Count) collector=$($col.Count). " +
        "Note that liveness alone proves nothing - on 2026-08-01 the collector was alive " +
        "and logging while every API call was failing, and only counter-freeze detection " +
        "caught it. That detection is DISABLED while this alert stands.`r`n`r`n" +
        "No automatic restart is performed for blindness: the collector may be perfectly " +
        "healthy (it was on 2026-08-03) and restarting on a read failure would cause the " +
        "outage it is meant to prevent. Check data/collector_state.json and " +
        "data/collector.log by hand.") 1800 | Out-Null
}

# ---- how long has the session been open, and how long has the collector been up ----
# Both answer the same question from different sides: "has enough time passed that a large
# ranking_snap_age_s could POSSIBLY mean a stall?" Right after a session opens, or right
# after a (re)start, last_ranking_snap_ms still points into the previous session by design -
# the collector resumes it from the state file. Alarming there restarts a healthy collector
# and, worse, does it in a loop.
$openSession = ($session -ne "closed" -and $session -ne "unknown")
$wasOpen = Get-Prop $state "session_was_open" $null
$wasName = Get-Prop $state "session_name" ""
if ($sessionTrusted) {
    if ($openSession) {
        # Reset on ANY session change, not just closed->open. The daily hole is only
        # observed as 'closed' if the log fallback happens to be readable at that moment;
        # if it was not, the watchdog goes after -> (unknown, record untouched) -> day and
        # would never notice a transition happened. Keying on the session NAME closes that:
        # after != day, so the grace period still starts. The cost is one skipped cycle at
        # each pre/regular/after boundary, which is a trade the 2026-08-04 restart bought.
        if ($wasOpen -ne $true -or $wasName -ne $session) { Set-Prop $state "session_open_since" $NowEpoch }
        Set-Prop $state "session_was_open" $true
    } else {
        Set-Prop $state "session_was_open" $false
        Set-Prop $state "session_open_since" 0
    }
    Set-Prop $state "session_name" $session
}
# An untrusted/blind cycle deliberately leaves the record alone: not being able to see must
# not silently re-arm the grace period on the next cycle.
$openSinceEpoch = [int](Get-Prop $state "session_open_since" 0)
$openForS = -1
if ($openSinceEpoch -gt 0) { $openForS = $NowEpoch - $openSinceEpoch }
$colUptimeS = Get-CollectorUptimeS $col

$script:SnapshotFrozen = $snapshot.frozen

# The newest telemetry LINE, read at most once per cycle and only when something is about
# to make a decision or write an alert. It is the independent second opinion on 'session':
# the collector logs one every 5 minutes even while closed, so it keeps moving exactly when
# collector_state.json stops.
$script:XCheck = "unset"
function Get-CrossCheck {
    if ($script:XCheck -eq "unset") { $script:XCheck = Get-LastTelemetry }
    return $script:XCheck
}

# A ranking stall is a claim about an OPEN session. Before acting on it, ask the one source
# that keeps ticking through a closed session whether the session is really open.
#
# This catches what the freshness test alone cannot: on 2026-08-04 the state file was only
# 71 s old at 08:50:55 - comfortably inside any trust window - and its session was ALREADY
# wrong, because the collector had logged "session after -> closed" at 08:50:09 and then
# had no work left to trigger a rewrite. A file can be fresh and stale at the same time.
# Only a strictly newer observation is allowed to overrule; an older log line proves nothing.
function Get-ClosedByFresherSource {
    $xc = Get-CrossCheck
    if ($null -eq $xc -or $null -eq $tele) { return $null }
    if ($xc.ts -le $tele.ts) { return $null }
    if ($xc.session -ne "closed") { return $null }
    return ("the newest collector.log telemetry ($($xc.ts_str)) is NEWER than this " +
            "observation ($($tele.ts_str)) and says session=closed")
}

# Counter read that keeps "absent" distinguishable from "zero" at the call site.
# NOT named Get-Counter: that is a built-in PowerShell cmdlet (performance counters)
# and shadowing it would make any snippet lifted out of this file call the wrong thing.
function Get-CounterValue($counters, [string]$key, $default) {
    if ($null -ne $counters -and $counters.ContainsKey($key)) { return $counters[$key] }
    return $default
}

# The MARKET_DATA budget line, read from the newest telemetry line. This is the "FOR WHAT,
# with numbers" half of a tradeoff record - without it the reader is told something was
# sacrificed but not what won. Only the log line carries it (the state file stores counters,
# not budget), so it is read on demand and its absence is said out loud rather than blanked.
function Get-BudgetLine {
    $xc = Get-CrossCheck
    if ($null -eq $xc -or $null -eq $xc.counters) { return "(no telemetry line to read the budget from)" }
    $raw = [string]$xc.counters
    if ($raw -notmatch "\|(.*)$") { return "(telemetry line carries no budget segment)" }
    return ($Matches[1].Trim())
}

# tier2 orderbook: decide WHY the snap counter stopped, then grade. Returns the level, a
# short tag for the summary line, and the full body.
#
# The four mandatory fields of a TRADEOFF_ record (header contract) are assembled here:
# WHAT was given up, FOR WHAT (budget numbers), HOW LONG (since the run started, not this
# cycle), HOW MUCH (skipped vs attempted). A tradeoff record missing any of them is a
# rumour, so each one is written unconditionally - "unknown" is printed where a source
# genuinely cannot supply it.
function Get-Tier2BookVerdict($state, $cur, $tele) {
    $base = Get-Prop $state "t2book_flat_base" $null
    $sinceEpoch = if ($null -ne $base) { [int](Get-Prop $base "since" $NowEpoch) } else { $NowEpoch }
    $durS = [math]::Max($NowEpoch - $sinceEpoch, 0)
    $b0Rate = if ($null -ne $base) { [double](Get-Prop $base "rate" 0) } else { 0 }
    $b0429 = if ($null -ne $base) { [double](Get-Prop $base "n429" 0) } else { 0 }
    $b0Skip = if ($null -ne $base) { [double](Get-Prop $base "skipped" 0) } else { 0 }
    $curRate = [double](Get-CounterValue $cur "tier2_orderbook_skipped_rate" 0)
    $cur429 = [double](Get-CounterValue $cur "tier2_orderbook_skipped_429" 0)
    $curSkip = [double](Get-CounterValue $cur "tier2_orderbook_skipped" 0)
    $runRate = $curRate - $b0Rate
    $run429 = $cur429 - $b0429
    $runSkip = $curSkip - $b0Skip
    $members = [int](Get-CounterValue $cur "tier2_members" -1)
    # $tele is a HASHTABLE, not a PSObject - Get-Prop walks PSObject.Properties and would
    # silently return the default here, which would make every state-sourced verdict claim
    # the rate/429 split was unavailable. Read the key directly.
    $splitOk = $false
    if ($null -ne $tele -and $tele.ContainsKey("skip_split_available")) {
        $splitOk = [bool]$tele["skip_split_available"]
    }
    $snaps = [int](Get-CounterValue $cur "tier2_orderbook_snaps" 0)

    # attempted = what the sweep tried this run. Snaps are flat by definition of being here,
    # so attempts are the skips. Expressing the yield as a rate over attempts avoids needing
    # to know polling.tier2_orderbook_s, which the watchdog cannot see.
    $attempted = $runSkip
    $yieldPct = if ($attempted -gt 0) { [math]::Round(100.0 * $runSkip / $attempted, 1) } else { 0.0 }

    # (a) nothing to poll - not a fault and not a tradeoff either; there was no choice to make
    if ($members -eq 0) {
        return @{ level = "INFO"; key = "tier2_orderbook_no_members"; tag = "no_members"; body = (
            "tier2_orderbook_snaps has not advanced (total $snaps) and there are ZERO tier2 " +
            "members to poll, so the sweep has nothing to do. session=$session, flat for " +
            "${durS}s.`r`n`r`nThis is not a fault and not a tradeoff - no data was given up, " +
            "there was simply no candidate. It becomes worth asking about only if tier2 stays " +
            "empty during an active session, which is a promotion question, not a polling one.") }
    }

    # (b) the collector deliberately yielded - the 2026-08-05 case
    $yielded = ($runRate -gt 0 -or $run429 -gt 0 -or ($runSkip -gt 0 -and -not $splitOk))
    if ($yielded) {
        $why = @()
        if ($splitOk) {
            if ($runRate -gt 0) { $why += "budget pressure (skipped_rate +$runRate)" }
            if ($run429 -gt 0) { $why += "429 cooldown (skipped_429 +$run429)" }
        } else {
            $why += ("cause split unavailable from this source - the collector.log telemetry " +
                     "line carries only the combined tier2_orderbook_skipped (+$runSkip); the " +
                     "rate-vs-429 split exists only in data/collector_state.json")
        }
        $memTxt = if ($members -lt 0) { "unknown" } else { "$members" }
        return @{ level = "TRADEOFF"; key = "tier2_orderbook_yield"; tag = "yielded"; body = (
            "The collector GAVE UP tier2 orderbook polling. This is not a fault - it is the " +
            "designed sacrifice order (tier2 orderbook is the first thing dropped under " +
            "pressure). It is filed as TRADEOFF_ because the size of the sacrifice is a " +
            "judgement call and that call is yours, not the watchdog's.`r`n`r`n" +
            "1. WHAT was given up : tier2 orderbook snapshots - the pre-promotion spread " +
            "trajectory. The strategy's entry-window and exit-cost design reads this; it " +
            "cannot be back-filled, so this window is gone for good.`r`n" +
            "2. FOR WHAT          : " + ($why -join "; ") + "`r`n" +
            "                       MARKET_DATA budget at this moment: " + (Get-BudgetLine) + "`r`n" +
            "                       tier2 members waiting: $memTxt, tier3 members: " +
            "$([int](Get-CounterValue $cur 'tier3_members' -1)) (tier3 polls the same MARKET_DATA " +
            "budget far more densely - 4s per symbol - so a full tier3 is what crowds this out)`r`n" +
            "3. HOW LONG          : ${durS}s continuous (snap counter frozen at $snaps since " +
            "$([DateTimeOffset]::FromUnixTimeSeconds($sinceEpoch).LocalDateTime.ToString('yyyy-MM-dd HH:mm:ss')))`r`n" +
            "4. HOW MUCH          : ${yieldPct}% of attempted polls yielded ($runSkip skipped / " +
            "$attempted attempted over this run; 0 snapshots taken)`r`n`r`n" +
            "This NEVER escalates to ALERT_ however long it lasts (user decision 2026-08-05): " +
            "lasting is not breaking, and an ALERT_ here reads as 'a restart would help', " +
            "which is the wrong response. If the duration or the yield rate above looks too " +
            "expensive, that is a budget decision - raise polling.tier2_orderbook_s, cut " +
            "tier3 capacity, or accept it.") }
    }

    # (c) genuinely stalled or misconfigured - this one MUST stay loud
    $memTxt = if ($members -lt 0) { "unknown (could not read tier membership)" } else { "$members" }
    return @{ level = "WARN"; key = "tier2_orderbook_flat"; tag = "flat"; body = (
        "tier2_orderbook_snaps has not advanced (total $snaps) for ${durS}s during " +
        "session=$session, and the collector did NOT record any deliberate skip in that " +
        "time (skipped_rate +$runRate, skipped_429 +$run429) while tier2 members = $memTxt.`r`n`r`n" +
        "Because no skip counter moved, this is NOT the designed budget yield - the sweep is " +
        "not choosing to skip, it is not running. Remaining causes:`r`n" +
        "  - polling.tier2_orderbook_s is 0 or unset in the live config (the loop idles by " +
        "design when disabled, and logs 'tier2 orderbook: disabled' once at startup)`r`n" +
        "  - the loop is stalled or died inside run_tier2_orderbook`r`n`r`n" +
        "This is silent - nothing errors when it happens - which is why it is graded ALERT_. " +
        "Check the collector.log for 'tier2 orderbook: disabled' first; it is the cheap answer.") }
}

# Evidence block shared by every alert that could conceivably be this false alarm again.
# On 2026-08-04 the ALERT file said "the ranking loop has silently stopped ... permanent
# data loss" when nothing at all was wrong, and it took a person a long morning to tell the
# difference. Everything needed to make that call in 30 seconds goes in here.
function Get-EvidenceBlock {
    $lines = @()
    $srcName = "collector.log telemetry line"
    if ($teleSource -eq "state") { $srcName = "data/collector_state.json" }
    if ($teleSource -eq "none") { $srcName = "(nothing readable)" }
    $trustTxt = "TRUSTED"
    if (-not $sessionTrusted) { $trustTxt = "NOT TRUSTED - older than the ${StateTrustS}s trust window, so it was read as 'unknown'" }
    $lines += "  observed from : $srcName"
    if ($null -ne $tele) {
        $lines += ("  observed at   : {0} ({1}s ago)" -f $tele.ts_str, $obsAgeS)
        $lines += "  session       : $sessionRaw  [$trustTxt]"
        if ($null -ne $tele.last_snap_str) { $lines += "  last ranking  : $($tele.last_snap_str)" }
    }
    if ($openForS -ge 0) { $lines += "  session open  : ${openForS}s (grace threshold ${SessionOpenGraceS}s)" }
    else { $lines += "  session open  : (not observed open since this watchdog last had a trusted reading)" }
    if ($colUptimeS -ge 0) { $lines += ("  collector up  : {0}s (warmup threshold {1}s)" -f [int]$colUptimeS, $CollectorWarmupS) }
    else { $lines += "  collector up  : (could not read process start time)" }
    if ($null -ne $script:SnapshotFrozen) {
        $lines += ("  STALE SOURCE  : collector_state.json last written $($script:SnapshotFrozen.ts_str) " +
                   "($($script:SnapshotFrozen.age_s)s ago) claiming session='$($script:SnapshotFrozen.session)' - ignored")
    }
    # Independent cross-check: what does the newest telemetry LINE say? A disagreement
    # between the two sources is the exact signature of the 2026-08-04 false alarm.
    $xc = Get-CrossCheck
    if ($null -ne $xc) { $lines += "  cross-check   : collector.log $($xc.ts_str) says session=$($xc.session)" }
    else { $lines += "  cross-check   : no telemetry line found in collector.log" }
    return ("EVIDENCE:`r`n" + ($lines -join "`r`n"))
}

# ---- W4 watchdog contract (main 27abc3f): counters + log strings ----
# These counters exist only on post-27abc3f collectors. On an older binary they are
# simply absent from the telemetry line and every check below no-ops (no false alarms).
if ($null -ne $tele) {
    Clear-AlertKey $state "no_telemetry"
    $cur = $tele.counters
    $prevRaw = Get-Prop $state "last_counters" $null
    $prev = @{}
    if ($null -ne $prevRaw) {
        foreach ($p in $prevRaw.PSObject.Properties) { $prev[$p.Name] = [double]$p.Value }
    }
    function Delta([string]$k) {
        if (-not $cur.ContainsKey($k)) { return $null }
        if (-not $prev.ContainsKey($k)) { return 0.0 }
        return ($cur[$k] - $prev[$k])
    }

    # (1) HIGHEST PRIORITY - ranking snapshot age. Rankings have no historical API,
    # so a stalled ranking loop is unrecoverable loss for every minute it stays stalled.
    #
    # But "the ranking loop stopped" and "the ranking loop is not supposed to be running"
    # look IDENTICAL in this counter, and the second one happens every day. Three things
    # must all hold before a large value is allowed to mean a stall:
    #   - the session is open AND that reading is fresh enough to be about now
    #     ($openSession already folds in the trust test: an untrusted session reads
    #      'unknown', and unknown is not open)
    #   - the session has been open longer than the threshold - otherwise the last snapshot
    #     legitimately belongs to the previous session (the 08:50-09:00 KST calendar hole)
    #   - the collector has been up longer than the threshold - a just-started collector
    #     resumes last_ranking_snap_ms from the state file, so it inherits the old value
    $graceReason = ""
    if ($openForS -ge 0 -and $openForS -lt $SessionOpenGraceS) {
        $graceReason = "the session has only been open ${openForS}s (grace ${SessionOpenGraceS}s)"
    } elseif ($colUptimeS -ge 0 -and $colUptimeS -lt $CollectorWarmupS) {
        $graceReason = ("the collector has only been up {0}s (warmup {1}s)" -f [int]$colUptimeS, $CollectorWarmupS)
    } else {
        # last line of defence: a fresh-looking observation whose session a newer source
        # already contradicts
        $closedByFresher = Get-ClosedByFresherSource
        if ($null -ne $closedByFresher) { $graceReason = $closedByFresher }
    }
    if ($openSession -and $cur.ContainsKey("ranking_snap_age_s")) {
        $rsa = [int]$cur["ranking_snap_age_s"]
        if ($rsa -lt 0) {
            # Only strike on a NEW telemetry line. Reading the same stale line twice
            # (telemetry is 5-minutely, so is this watchdog) must not count as two
            # independent observations - that would restart on a single startup -1.
            $strk = Get-Prop $state "ranking_never_strikes" 0
            if ($tele.ts_str -ne $prevTeleTs) { $strk = $strk + 1 }
            Set-Prop $state "ranking_never_strikes" $strk
            if ($strk -ge 2 -and $null -eq $restartReason -and $graceReason -eq "") {
                $problems += "ranking_snap_never"
                $restartReason = "ranking_snap_never"
            }
        } elseif ($rsa -gt $RankingSnapAgeCritS -and $graceReason -eq "") {
            Set-Prop $state "ranking_never_strikes" 0
            $problems += "ranking_snap_age_${rsa}s"
            Raise-Alert $state "ranking_snap_stalled" "CRIT" (
                "HIGHEST PRIORITY: ranking_snap_age_s=$rsa (threshold $RankingSnapAgeCritS s) " +
                "during session=$session. The ranking loop has silently stopped and rankings " +
                "CANNOT be back-filled - every minute of this is permanent data loss. " +
                "Restarting the collector.`r`n`r`n" + (Get-EvidenceBlock) + "`r`n`r`n" +
                "This alarm fired only because all of the following were true: the session " +
                "reading is fresher than ${StateTrustS}s, the session has been open longer " +
                "than ${SessionOpenGraceS}s, and the collector has been up longer than " +
                "${CollectorWarmupS}s. If any of those had failed, the watchdog would " +
                "have written NOTE_*_ranking_stall_suppressed instead and restarted " +
                "nothing.") 900 | Out-Null
            if ($null -eq $restartReason) { $restartReason = "ranking_snap_stalled" }
        } elseif ($rsa -gt $RankingSnapAgeCritS) {
            # The old code restarted here. Say out loud that it did not, and why - a
            # suppressed alarm that leaves no trace is how the next person concludes the
            # guard "only checked the cases on a list".
            Set-Prop $state "ranking_never_strikes" 0
            $problems += "ranking_stall_suppressed_${rsa}s"
            Write-Log ("ranking judgment SKIPPED: ranking_snap_age_s=$rsa exceeds " +
                       "$RankingSnapAgeCritS but $graceReason - not a stall, not restarting")
            Raise-Alert $state "ranking_stall_suppressed" "INFO" (
                "ranking_snap_age_s=$rsa is over the ${RankingSnapAgeCritS}s threshold, and the " +
                "watchdog decided this is NOT a stall: $graceReason.`r`n`r`n" +
                (Get-EvidenceBlock) + "`r`n`r`n" +
                "This is the 2026-08-04 08:55 false alarm being refused. Nothing is wrong " +
                "and NOTHING WAS RESTARTED - this file exists so that the refusal is " +
                "visible rather than silent. It is a NOTE_, not an ALERT_.`r`n" +
                "It becomes suspicious only if it repeats while the session has genuinely " +
                "been open for a long time; check data/watchdog.log for 'ranking judgment " +
                "SKIPPED' lines to see how often it fires.") 21600 | Out-Null
        } else {
            Set-Prop $state "ranking_never_strikes" 0
            Clear-AlertKey $state "ranking_snap_stalled"
            Clear-AlertKey $state "ranking_stall_suppressed"
        }
    } elseif (-not $openSession -and $cur.ContainsKey("ranking_snap_age_s") -and
              [int]$cur["ranking_snap_age_s"] -gt $RankingSnapAgeCritS) {
        # Session closed or unreadable: a stale ranking snapshot is exactly what should be
        # there. Log it so the morning reader can see the watchdog saw it and let it be.
        Write-Log ("ranking judgment SKIPPED: ranking_snap_age_s=$($cur['ranking_snap_age_s']) " +
                   "but session=$session (raw=$sessionRaw trusted=$sessionTrusted) - rankings " +
                   "are not expected to advance, not restarting")
    }

    # (2) auth_failures - the exact 2026-08-01 blind spot (api_errors stayed 0 while
    # every call died). A sustained rise means the token path is dead; restart re-runs
    # the launcher, which guarantees TOSS_BASE_URL (docs/11 section 11-1).
    $dAuth = Delta "auth_failures"
    if ($null -ne $dAuth -and $dAuth -gt 0) {
        Raise-Alert $state "auth_failures" "CRIT" (
            "auth_failures rose by $dAuth (total $($cur['auth_failures'])) - token expired/" +
            "rejected or issuance/lease failure. This is the blind spot that cost 2 hours on " +
            "2026-08-01. Restart threshold is a rise of $AuthFailuresToRestart in one cycle.") 1800 | Out-Null
        if ($dAuth -ge $AuthFailuresToRestart -and $null -eq $restartReason) {
            $problems += "auth_failures_$dAuth"
            $restartReason = "auth_failures"
        }
    }

    # (3a) symbol_not_found (main 4397130) - a symbol in the rankings could not be looked
    # up: http-404 code=stock-not-found, i.e. delisted / halted / renamed. This happens in
    # normal operation and the collector simply skips that symbol. INFO, never CRIT.
    $dSnf = Delta "symbol_not_found"
    if ($null -ne $dSnf -and $dSnf -gt 0) {
        Raise-Alert $state "symbol_not_found" "INFO" (
            "symbol_not_found rose by $dSnf (total $($cur['symbol_not_found'])) - that many " +
            "lookups came back http-404 code=stock-not-found. The symbol is delisted, halted " +
            "or renamed; the collector skips it and everything else is unaffected. This is " +
            "NOT a contract change and there is no reason to distrust today's data.`r`n" +
            "Worth a look only if the number keeps climbing: that would mean the universe " +
            "list has drifted away from what the exchange still lists.") 21600 | Out-Null
    }

    # (3b) schema_mismatch - the API response SHAPE changed. This is the one counter that
    # justifies "do not trust today's data", so the alert must not cry wolf.
    #
    # 2026-08-04: it did. All five occurrences to date were plain http-404
    # code=stock-not-found (a delisted symbol), yet the alert asserted "the API response
    # shape changed. A restart will NOT fix this. Inspect the endpoint contract before
    # trusting today's data." main 4397130 (W4) splits the counter, but a collector started
    # before that build still lumps them together - and the alert has to be honest on both.
    # So classify from the evidence in the log rather than from the counter name, and let
    # the severity follow the evidence:
    #   every matching line is a 404 stock-not-found -> INFO  (nothing is wrong)
    #   at least one line is something else          -> CRIT  (the real thing)
    #   no matching line found at all                -> WARN  (say so; do not assert)
    $dSchema = Delta "schema_mismatch"
    if ($null -ne $dSchema -and $dSchema -gt 0) {
        $smTail = (Get-LogTail 5000) | Where-Object { $_ -match "schema mismatch \(skip\)" }
        $smAll = @($smTail)
        $smNotFound = @($smAll | Where-Object { $_ -match "http-404\b.*\bcode=stock-not-found\b" })
        $smOther = @($smAll | Where-Object { $_ -notmatch "http-404\b.*\bcode=stock-not-found\b" })
        $sample = (@($smAll | Select-Object -Last 5) | ForEach-Object { "  $_" }) -join "`r`n"
        if ($sample -eq "") { $sample = "  (no 'schema mismatch (skip)' line in the last 5000 log lines)" }
        $lvl = "WARN"
        $verdict = ("Could not find the matching log lines (searched the last 5000 lines), so " +
                    "the cause is UNKNOWN. It may be a missing symbol, which is harmless, or a " +
                    "real response-shape change, which is not. Do not conclude either way from " +
                    "this file - read data/collector.log around the time above.")
        if ($smAll.Count -gt 0 -and $smOther.Count -eq 0) {
            $lvl = "INFO"
            $verdict = ("Every one of the $($smAll.Count) matching log lines is a plain " +
                        "http-404 code=stock-not-found - a delisted/halted/renamed symbol, not " +
                        "a shape change. Nothing is wrong with the data. This collector build " +
                        "still counts those under schema_mismatch; main 4397130 moves them to " +
                        "symbol_not_found, and the count will stop rising here once the running " +
                        "collector picks that build up.")
        } elseif ($smOther.Count -gt 0) {
            $lvl = "CRIT"
            $verdict = ("$($smOther.Count) of the $($smAll.Count) matching log lines are NOT " +
                        "http-404 code=stock-not-found. That is the real case: the response " +
                        "shape may have changed. A restart will NOT fix it. Inspect the " +
                        "endpoint contract before trusting today's data for those fields.")
        }
        $problems += "schema_mismatch_$dSchema"
        Raise-Alert $state "schema_mismatch" $lvl (
            "schema_mismatch rose by $dSchema (total $($cur['schema_mismatch'])).`r`n`r`n" +
            "VERDICT: $verdict`r`n`r`nMatching log lines (most recent 5):`r`n$sample") 3600 | Out-Null
    }

    # (4) write-failure family - data reaching the collector but not the DB.
    foreach ($wk in @("event_write_failures", "promotion_write_failures", "rankings_write_failures")) {
        $d = Delta $wk
        if ($null -ne $d -and $d -gt 0) {
            $problems += "${wk}_$d"
            Raise-Alert $state $wk "WARN" (
                "$wk rose by $d (total $($cur[$wk])) - rows are being collected but not stored. " +
                "Check disk space and data/collector.log for the underlying exception.") 3600 | Out-Null
        }
    }

    # (5) rankings_clamped - first-ever fire is an observation milestone (docs/11 11-3):
    # the int64 overflow symbol is back in the rankings and the clamp branch finally ran.
    $dClamp = Delta "rankings_clamped"
    if ($null -ne $dClamp -and $dClamp -gt 0) {
        Raise-Alert $state "rankings_clamped" "INFO" (
            "rankings_clamped rose by $dClamp (total $($cur['rankings_clamped'])) - the int64 " +
            "clamp branch fired. This is the W4 hotfix working as designed (not an outage), " +
            "and it is the observation item from docs/11 section 11-3.") 21600 | Out-Null
    }

    # (6) collection health - silent partial loss rather than a hard stop.
    if ($openSession -and $cur.ContainsKey("fetch_success_pct")) {
        $fs = [double]$cur["fetch_success_pct"]
        if ($fs -lt $FetchSuccessWarnPct) {
            $problems += "fetch_success_$fs"
            Raise-Alert $state "fetch_success_low" "WARN" (
                "fetch_success_pct=$fs (threshold $FetchSuccessWarnPct) with prices_missing=" +
                "$($cur['prices_missing']) - the tier1 sweep is silently losing symbols.") 3600 | Out-Null
        } else {
            Clear-AlertKey $state "fetch_success_low"
        }
    }
    $dLoop = Delta "loop_errors"
    if ($null -ne $dLoop -and $dLoop -gt $LoopErrorSurge) {
        $problems += "loop_errors_$dLoop"
        Raise-Alert $state "loop_errors_surge" "WARN" (
            "loop_errors rose by $dLoop in one cycle (threshold $LoopErrorSurge, total " +
            "$($cur['loop_errors'])) - something is throwing repeatedly inside the loops.") 3600 | Out-Null
    }
    # tier2 orderbook (main 4e6e24b): the pre-promotion spread trajectory the strategy's
    # exit design needs. A flat counter during an open session used to be graded WARN
    # outright, listing three candidate causes - and on 2026-08-05 the real cause was a
    # fourth one that was not even on the list: the collector deliberately yielded its
    # budget because tier3 had filled to capacity. Five ALERTs overnight for a healthy
    # collector, while the block 20 lines below called the very same event "the designed
    # sacrifice order, not a fault".
    #
    # So: separate the cause BEFORE choosing the grade. Every input here is a counter that
    # already exists - nothing in tossmon/** had to change.
    #   snaps flat + a skip counter rising  -> TRADEOFF_ (the system gave something up)
    #   snaps flat + no skips + members > 0 -> ALERT_    (a real stall or misconfig)
    #   snaps flat + members == 0           -> NOTE_     (nothing to poll)
    if ($openSession -and $cur.ContainsKey("tier2_orderbook_snaps")) {
        $dT2 = Delta "tier2_orderbook_snaps"
        if ($null -ne $dT2 -and $dT2 -eq 0) {
            $strk = (Get-Prop $state "t2book_flat_strikes" 0) + 1
            Set-Prop $state "t2book_flat_strikes" $strk
            # Anchor the run the first time it goes flat, so duration and yield rate are
            # measured over the WHOLE yield, not over one 5-minute cycle. Without this the
            # "how long / how much" fields would reset every cycle and a 6-hour yield would
            # read the same as a 15-minute one.
            if ($strk -eq 1) {
                Set-Prop $state "t2book_flat_base" ([PSCustomObject]@{
                    since = $NowEpoch
                    snaps = [double]$cur["tier2_orderbook_snaps"]
                    rate = [double](Get-CounterValue $cur "tier2_orderbook_skipped_rate" 0)
                    n429 = [double](Get-CounterValue $cur "tier2_orderbook_skipped_429" 0)
                    skipped = [double](Get-CounterValue $cur "tier2_orderbook_skipped" 0)
                })
            }
            if ($strk -ge 3) {
                $verdict = Get-Tier2BookVerdict $state $cur $tele
                $problems += "tier2_orderbook_$($verdict.tag)_x$strk"
                # Each verdict owns its own dedup key. Sharing one key would mean that a
                # yield which later turns into a genuine stall stays silent for the rest of
                # the dedup window - the alarm would be suppressed by the very record that
                # said "this is fine". Clearing the other two keys also lets the opposite
                # transition re-fire immediately.
                foreach ($k in @("tier2_orderbook_flat", "tier2_orderbook_yield",
                                 "tier2_orderbook_no_members")) {
                    if ($k -ne $verdict.key) { Clear-AlertKey $state $k }
                }
                Raise-Alert $state $verdict.key $verdict.level $verdict.body 3600 | Out-Null
            }
        } else {
            Set-Prop $state "t2book_flat_strikes" 0
            Set-Prop $state "t2book_flat_base" $null
            foreach ($k in @("tier2_orderbook_flat", "tier2_orderbook_yield",
                             "tier2_orderbook_no_members")) { Clear-AlertKey $state $k }
        }
    } else {
        Set-Prop $state "t2book_flat_strikes" 0
        Set-Prop $state "t2book_flat_base" $null
    }
    # Budget yielding is by design (tier2 orderbook is the first thing sacrificed), so
    # this is INFO - it explains a lower snap count rather than reporting a fault.
    $dSkip = Delta "tier2_orderbook_skipped"
    if ($null -ne $dSkip -and $dSkip -gt 0) {
        Raise-Alert $state "tier2_orderbook_skipped" "INFO" (
            "tier2_orderbook_skipped rose by $dSkip (total $($cur['tier2_orderbook_skipped'])) - " +
            "the collector yielded tier2 orderbook polls under budget pressure or a 429 " +
            "cooldown. This is the designed sacrifice order, not a fault. Sustained growth " +
            "means the MARKET_DATA budget is tight; consider raising polling.tier2_orderbook_s.") 21600 | Out-Null
    }

    if ($openSession -and $cur.ContainsKey("candles_1m")) {
        $dC = Delta "candles_1m"
        if ($null -ne $dC -and $dC -eq 0) {
            $strk = (Get-Prop $state "candles_flat_strikes" 0) + 1
            Set-Prop $state "candles_flat_strikes" $strk
            if ($strk -ge 3) {
                $problems += "candles_1m_flat_x$strk"
                Raise-Alert $state "candles_flat" "WARN" (
                    "candles_1m has not advanced for $strk cycles during session=$session - " +
                    "the tier2 candle loop may be stalled while other loops still run.") 3600 | Out-Null
            }
        } else {
            Set-Prop $state "candles_flat_strikes" 0
            Clear-AlertKey $state "candles_flat"
        }
    } else {
        Set-Prop $state "candles_flat_strikes" 0
    }

    $snapObj = New-Object PSObject
    foreach ($k in $cur.Keys) { Set-Prop $snapObj $k $cur[$k] }
    Set-Prop $state "last_counters" $snapObj
}

# (7) W4 contract log strings - things that are logged but may not be counted.
#
# The window is bounded by LINES **and** BY TIME. Lines alone were not a window at all:
# how much time 500 lines cover depends entirely on how busy the market is. Measured
# 2026-08-09 on the live collector.log:
#
#     Fri regular 23:30 -> 113 min      Fri regular 03:00 ->   74 min
#     Fri regular 01:00 ->  77 min      Sun idle    09:33 -> 1507 min (25.1 h)
#
# The damage is one-directional. collector.log is 5-minute telemetry, not per-request, so
# even at peak the 500 lines still cover 74+ minutes - nothing is missed by being busy.
# But when the market is shut the log barely grows, one old line never leaves the window,
# and Raise-Alert's 3600s dedup re-fires it every hour: a single 'precision drift' at
# 2026-08-08 08:46 had produced 25 ALERT files by 08-09 09:21. A dead event shouting
# forever trains the reader to skip the file - which is how a real one gets missed.
#
# WHY 180 MINUTES, AND WHY THE SAME BOUND FOR ALL SIX PATTERNS:
#  - 180 sits 59% above the busiest measured span of the 500-line window (113 min), so
#    during market hours the LINE cap still binds and detection is unchanged from today.
#    That margin is not cosmetic: log_tape_gap fires on a COUNT (Min=20), and a binding
#    time cap would quietly shrink that numerator.
#  - It is 36x the 5-minute watchdog cadence, so every log line is examined by ~36
#    consecutive runs before it ages out.
#  - Same bound regardless of Level (CRIT..INFO): all six are "a line appeared" detectors,
#    so the bound only decides how long we keep shouting AFTER the condition stops. A
#    condition that is still happening keeps writing fresh lines and keeps the alert alive.
#    Severity changes how loud the file is, not how long a finished event stays news.
$LogWindowMin = 180
$logCutoff = (Get-Date).AddMinutes(-$LogWindowMin)
$tailLines = @(Get-LogTail 500)
if ($tailLines.Count -gt 0) {
    $logPatterns = @(
        @{ Key = "log_auth_failure"; Rx = "AUTH-FAILURE"; Level = "CRIT"; Min = 1;
           Msg = "AUTH-FAILURE lines are present in the recent log - token expired/rejected or issuance/lease failure." },
        @{ Key = "log_forbidden"; Rx = "ForbiddenEndpoint|Forbidden"; Level = "WARN"; Min = 1;
           Msg = "Forbidden/ForbiddenEndpoint in the recent log - an endpoint is refusing this key (contract or entitlement change)." },
        @{ Key = "log_rankings_store"; Rx = "rankings store failed"; Level = "WARN"; Min = 1;
           Msg = "'rankings store failed' in the recent log - ranking rows are being dropped at the DB write." },
        @{ Key = "log_rankings_clamp"; Rx = "rankings clamp"; Level = "INFO"; Min = 1;
           Msg = "'rankings clamp' in the recent log - the int64 clamp branch fired (working as designed)." },
        @{ Key = "log_precision_drift"; Rx = "precision drift"; Level = "WARN"; Min = 1;
           Msg = "'precision drift' in the recent log - API number formatting changed; verify parsed prices." },
        @{ Key = "log_tape_gap"; Rx = "tape gap"; Level = "INFO"; Min = 20;
           Msg = "Many 'tape gap' lines in the recent log - normal right after a restart, suspicious otherwise." }
    )
    foreach ($lp in $logPatterns) {
        $n = 0; $aged = 0; $undated = 0
        $newest = $null; $newestLine = ""; $newestAged = $null
        foreach ($ln in $tailLines) {
            if ($ln -notmatch $lp.Rx) { continue }
            $ts = Get-LogLineTime $ln
            if ($null -eq $ts) {
                # Cannot date it -> KEEP it. Discarding a line we failed to parse would
                # drop a real alert silently, which is the one direction we cannot afford.
                $undated++; $n++
                if ($newestLine -eq "") { $newestLine = $ln }
                continue
            }
            if ($ts -lt $logCutoff) {
                $aged++
                if ($null -eq $newestAged -or $ts -gt $newestAged) { $newestAged = $ts }
                continue
            }
            $n++
            if ($null -eq $newest -or $ts -gt $newest) { $newest = $ts; $newestLine = $ln }
        }
        if ($n -ge $lp.Min) {
            $problems += "$($lp.Key)_$n"
            $when = if ($null -ne $newest) { "{0:yyyy-MM-dd HH:mm:ss}" -f $newest }
                    else { "(undated - no parseable timestamp on the matched line)" }
            $body = "$($lp.Msg)`r`n`r`n" +
                    "Occurrences in the last 500 log lines within ${LogWindowMin}min: $n`r`n" +
                    "Most recent match: $when`r`n" +
                    "  $($newestLine.Trim())"
            if ($undated -gt 0) { $body += "`r`n($undated match(es) had no parseable timestamp and were kept.)" }
            if ($aged -gt 0) { $body += "`r`n($aged older match(es) were outside the ${LogWindowMin}min bound and not counted.)" }
            Raise-Alert $state $lp.Key $lp.Level $body 3600 | Out-Null
        } else {
            Clear-AlertKey $state $lp.Key
            # A suppression nobody can see is just a different blind spot. This does NOT
            # become an alert file: doing so would recreate the every-hour noise it cures.
            # It goes to watchdog.log, which is permanent and greppable.
            if ($aged -gt 0) {
                Write-Log ("LOG-PATTERN-AGED key=$($lp.Key) n=$aged older_than=${LogWindowMin}min " +
                           ("newest={0:yyyy-MM-dd HH:mm:ss}" -f $newestAged) +
                           " - matched the 500-line window but not the time bound, so no alert.")
            }
        }
    }
}

# (8) Sibling scheduled tasks - they can die and nobody finds out.
#
# 2026-08-09: the morning report was simply missing. The machine was on, the collector was
# fine, and tossmon-dailyhealth had RUN and died:
#     last=2026-08-09 08:52:01  result=3221225786 = 0xC000013A = STATUS_CONTROL_C_EXIT
# Not a timeout (ExecutionTimeLimit is PT10M). The same signature is on
# tossmon-collector-oneshot from 08-04. WHAT cleaned up the console at 08:52 is unknown -
# the Task Scheduler operational log is empty, so there is no evidence. This section adds
# OBSERVATION ONLY; it does not try to fix a cause nobody has evidence for.
#
# No alert fired for any of it. Get-ScheduledTaskInfo hands us LastTaskResult for free and
# the watchdog simply never looked at its own siblings.
#
# TWO THINGS KEEP THIS FROM BECOMING THE NOISE IT REPLACES:
#  - Per-task expected results. tossmon-watchdog exits 1 whenever it found problems and 2
#    when it restarted the collector (watchdog.ps1 bottom) - both are NORMAL, and both are
#    already reported through its own alert files. Alerting on them would double-report and
#    would fire on nearly every cycle.
#  - A recency bound. tossmon-collector-oneshot is a manual one-shot whose last result has
#    been 0xC000013A since 08-04 with no next run; without the bound it would alert forever,
#    which is exactly the disease section 7 just cured.
$TASK_RUNNING = 267009      # 0x41301 SCHED_S_TASK_RUNNING - not a verdict yet
$TASK_NEVER_RAN = 267011    # 0x41303 SCHED_S_TASK_HAS_NOT_RUN
$TASK_NO_MORE_RUNS = 267012 # 0x41304 SCHED_S_TASK_NO_MORE_RUNS
$taskOkResults = @{
    # exits 0 clean / 1 "problems found, already alerted" / 2 "restarted the collector"
    "tossmon-watchdog" = @(0, 1, 2)
    "tossmon-sentinel" = @(0, 1, 2)
}
function Get-SiblingTaskInfo([string[]]$names, [string]$jsonPath) {
    if ($jsonPath -ne "") {
        if (-not (Test-Path $jsonPath)) { return @() }
        try {
            $parsed = Get-Content $jsonPath -Raw | ConvertFrom-Json
        } catch { return @() }
        # An empty array parses to $null; @($null) is a one-element array of nothing,
        # which would walk into the loop below and read properties off $null.
        if ($null -eq $parsed) { return @() }
        return @($parsed | Where-Object { $null -ne $_ })
    }
    $out = @()
    foreach ($n in $names) {
        try {
            $i = Get-ScheduledTaskInfo -TaskName $n -ErrorAction Stop
            $out += [pscustomobject]@{ TaskName = $n; LastRunTime = $i.LastRunTime
                                       LastTaskResult = [int64]$i.LastTaskResult }
        } catch {
            # A missing task is itself worth saying, but it is not the same event as a
            # task that ran and died - keep them apart.
            $out += [pscustomobject]@{ TaskName = $n; LastRunTime = $null
                                       LastTaskResult = $null; Missing = $true }
        }
    }
    return $out
}
foreach ($ti in (Get-SiblingTaskInfo $SiblingTasks $TaskInfoJson)) {
    $tn = [string]$ti.TaskName
    $key = "task_result_" + ($tn -replace "[^A-Za-z0-9]", "_")
    if ($ti.PSObject.Properties["Missing"] -and $ti.Missing) {
        $problems += "task_missing_$tn"
        Raise-Alert $state $key "WARN" (
            "Scheduled task '$tn' is not registered. Unattended work this project relies " +
            "on is simply not scheduled - re-register with ops\register_task_scheduler.ps1."
        ) 21600 | Out-Null
        continue
    }
    $res = $ti.LastTaskResult
    if ($null -eq $res) { Clear-AlertKey $state $key; continue }
    $res = [int64]$res
    $lastRun = $null
    if ($null -ne $ti.LastRunTime -and "$($ti.LastRunTime)" -ne "") {
        try { $lastRun = [datetime]$ti.LastRunTime } catch { $lastRun = $null }
    }
    $ageH = if ($null -ne $lastRun) { ((Get-Date) - $lastRun).TotalHours } else { [double]::PositiveInfinity }
    $ok = if ($taskOkResults.ContainsKey($tn)) { $taskOkResults[$tn] } else { @(0) }
    if ($res -in $ok -or $res -eq $TASK_RUNNING -or $res -eq $TASK_NEVER_RAN -or
        $res -eq $TASK_NO_MORE_RUNS) {
        Clear-AlertKey $state $key
        continue
    }
    if ($ageH -gt $TaskResultMaxAgeH) {
        Clear-AlertKey $state $key
        # Visible but not an alert - same rule as the aged-out log patterns above.
        Write-Log ("TASK-RESULT-AGED task=$tn result=$res (0x{0:X}) " -f $res +
                   ("last_run={0:yyyy-MM-dd HH:mm:ss}" -f $lastRun) +
                   " age=$([int]$ageH)h older_than=${TaskResultMaxAgeH}h - not alerted.")
        continue
    }
    $problems += "task_result_${tn}_$res"
    $hint = switch ($res) {
        3221225786 { "0xC000013A STATUS_CONTROL_C_EXIT - the process was killed by a console control event. Not a timeout." }
        267014     { "0x41306 SCHED_S_TASK_TERMINATED - Task Scheduler stopped it (ExecutionTimeLimit)." }
        267010     { "0x41302 SCHED_S_TASK_DISABLED - the task is disabled and will not run again." }
        default    { "non-zero exit from the task's own program." }
    }
    Raise-Alert $state $key "WARN" (
        "Scheduled task '$tn' last run FAILED.`r`n`r`n" +
        ("  last run : {0:yyyy-MM-dd HH:mm:ss} ($([int]$ageH)h ago)`r`n" -f $lastRun) +
        ("  result   : $res (0x{0:X})`r`n" -f $res) +
        "  meaning  : $hint`r`n" +
        "  expected : $($ok -join ', ')`r`n`r`n" +
        "Its output for that run does not exist. Check whether the artefact it produces " +
        "(report / rotation / health file) is missing for that slot."
    ) 21600 | Out-Null
}

# (e) token state: only meaningful during open sessions
if ($null -eq $restartReason -and $session -ne "closed" -and $session -ne "unknown" -and
    (Test-Path $TokenStateFile)) {
    try {
        $tok = Get-Content $TokenStateFile -Raw | ConvertFrom-Json
        $expMs = [double](Get-Prop $tok "expires_at_ms" 0)
        $nowMs = $NowEpoch * 1000.0
        if ($expMs -gt 0 -and $nowMs -gt ($expMs + $TokenGraceMin * 60000)) {
            $tailText = (Get-LogTail 400) -join "`n"
            $reCount = ([regex]::Matches($tailText, "unexpected RuntimeError")).Count
            $baseUrlErr = $tailText -match "TOSS_BASE_URL is not set"
            if ($reCount -ge $RuntimeErrorMin -or $baseUrlErr) {
                $problems += "token_dead_re$reCount"
                $restartReason = "token_dead"
            }
        }
    } catch {
        Write-Log "token_state.json unreadable: $($_.Exception.Message)"
    }
}

# (d) disk defense - tiered: survey (8GB) -> reclaim (6GB) -> critical floor (5GB)
$freeGB = Get-FreeGB
$consumerText = ""
if ($freeGB -lt $DiskSurveyGB) {
    # Only survey when it matters - the walk is too expensive for every 5-minute cycle.
    $top = Get-TopConsumers 8
    $consumerText = ($top | ForEach-Object { "  {0,9:N1} MB  {1}" -f $_.MB, $_.Path }) -join "`r`n"
    Write-Log ("disk survey (free=${freeGB}GB) top consumers:`r`n" + $consumerText)
}
if ($freeGB -lt $DiskReclaimGB) {
    $rec = Invoke-Reclaim 24
    $freedMB = $rec.freed
    $detail = "(nothing was eligible)"
    if ($freedMB -gt 0) {
        $freeGB = Get-FreeGB
        Write-Log "reclaim freed ${freedMB}MB, free now ${freeGB}GB"
        $detail = ($rec.items | ForEach-Object { "  - $_" }) -join "`r`n"
    } else {
        # A bare "reclaimed 0MB" is what made the 2026-08-03 incident slow: three alerts,
        # no reclaim, and a human had to go find the 3.9GB of scratch copies by hand.
        # If we freed nothing, the alert must carry the evidence needed to act.
        if ($consumerText -eq "") {
            $consumerText = ((Get-TopConsumers 8) | ForEach-Object { "  {0,9:N1} MB  {1}" -f $_.MB, $_.Path }) -join "`r`n"
            Write-Log ("disk survey (forced, reclaim freed 0MB) top consumers:`r`n" + $consumerText)
        }
    }
    $top5 = ((Get-TopConsumers 5) | ForEach-Object { "  {0,9:N1} MB  {1}" -f $_.MB, $_.Path }) -join "`r`n"
    Raise-Alert $state "disk_reclaim" "WARN" (
        "Free disk fell below $DiskReclaimGB GB. Reclaimed ${freedMB}MB; free is now $freeGB GB.`r`n`r`n" +
        "Reclaimed items:`r`n$detail`r`n`r`n" +
        "TOP 5 CONSUMERS RIGHT NOW:`r`n$top5`r`n`r`n" +
        "If the reclaim freed 0MB the space is held by something outside the reclaim rules - " +
        "read the list above before assuming the watchdog is broken. Two known cases: " +
        "(1) agent scratch DB copies, now reclaimed automatically except the newest per " +
        "scratchpad and any file still held open; (2) dynamically expanding virtual disks " +
        "(WSL ext4.vhdx, Claude VM rootfs.vhdx), which grow and never shrink and need an " +
        "elevated compaction the watchdog cannot perform - that one is a user action.") 10800 | Out-Null
}
if ($freeGB -lt $DiskCritGB) {
    $problems += "disk_crit_${freeGB}GB"
    Raise-Alert $state "disk_critical" "CRIT" (
        "Free disk $freeGB GB is below the critical floor of $DiskCritGB GB.`r`n" +
        "Actions: log rotation forced; polluted-DB backups " +
        "(tossmon_20260730_polluted.db*, archive_20260730_polluted) will be DELETED to keep " +
        "the live collection writing. See watchdog.log for what was removed.`r`n`r`n" +
        "Top consumers:`r`n$consumerText`r`n`r`n" +
        "USER ACTION if this keeps dropping: compact the virtual disks (elevated) - " +
        "'wsl --shutdown' then Optimize-VHD, and quit the Claude desktop app before compacting " +
        "its rootfs.vhdx. The collector itself only writes about 440MB/day.") 10800 | Out-Null
    $py = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    try { & $py -m ops.rotate_logs --config (Join-Path $RepoRoot "ops\ops_config.yaml") 2>&1 |
            ForEach-Object { Write-Log "rotate: $_" } } catch { Write-Log "rotate failed: $($_.Exception.Message)" }
    foreach ($victim in @("tossmon_20260730_polluted.db", "tossmon_20260730_polluted.db-shm",
                           "tossmon_20260730_polluted.db-wal")) {
        $vp = Join-Path $DataDir $victim
        if (Test-Path $vp) {
            $mb = [math]::Round((Get-Item $vp).Length / 1MB, 1)
            try { Remove-Item $vp -Force; Write-Log "disk-crit deleted $victim (${mb}MB)" } catch { }
        }
    }
    $vd = Join-Path $DataDir "archive_20260730_polluted"
    if (Test-Path $vd) {
        try { Remove-Item $vd -Recurse -Force; Write-Log "disk-crit deleted archive_20260730_polluted/" } catch { }
    }
} elseif ($freeGB -lt $DiskWarnGB) {
    $problems += "disk_warn_${freeGB}GB"
    if (Raise-Alert $state "disk_warn" "WARN" (
            "Free disk $freeGB GB is below warning $DiskWarnGB GB. Running log rotation. " +
            "If this keeps dropping, reclaim runs at $DiskReclaimGB GB and the critical floor " +
            "at $DiskCritGB GB deletes polluted-DB backups.`r`n`r`nTop consumers:`r`n$consumerText") 21600) {
        $py = Join-Path $RepoRoot ".venv\Scripts\python.exe"
        try { & $py -m ops.rotate_logs --config (Join-Path $RepoRoot "ops\ops_config.yaml") 2>&1 |
                ForEach-Object { Write-Log "rotate: $_" } } catch { Write-Log "rotate failed: $($_.Exception.Message)" }
    }
} else {
    Clear-AlertKey $state "disk_warn"
    Clear-AlertKey $state "disk_critical"
    Clear-AlertKey $state "disk_reclaim"
}

# (f) power
$pw = Get-PowerInfo
$powerNow = "ac"
if ($pw.has_battery -and (-not $pw.on_ac)) { $powerNow = "battery" }
$powerPrev = Get-Prop $state "power" "ac"
if ($powerNow -eq "battery") {
    if ($powerPrev -ne "battery") {
        Raise-Alert $state "on_battery" "WARN" (
            "Machine switched to BATTERY power (charge $($pw.pct)%). Collection continues " +
            "(scheduler battery limits were removed) but plug in AC as soon as possible. " +
            "DC sleep timeouts are set to 0 by the hardening pass, but battery drain will " +
            "eventually kill the machine.") 1 | Out-Null
    }
    if ($pw.pct -le 20) {
        Raise-Alert $state "battery_low" "CRIT" (
            "Battery at $($pw.pct)% and still on battery power. The machine will die soon " +
            "and collection with it. PLUG IN NOW.") 1800 | Out-Null
    }
} elseif ($powerPrev -eq "battery") {
    Raise-Alert $state "power_restored" "INFO" ("AC power restored (charge $($pw.pct)%).") 1 | Out-Null
    Clear-AlertKey $state "on_battery"
    Clear-AlertKey $state "battery_low"
}
Set-Prop $state "power" $powerNow

# (g) sentinel heartbeat (mutual watch)
if (Test-Path $SentinelHeartbeatFile) {
    $sAge = $NowEpoch - [int](Get-Item $SentinelHeartbeatFile).LastWriteTimeUtc.Subtract(
        [datetime]'1970-01-01').TotalSeconds
    if ($sAge -gt $SentinelStaleS) {
        Raise-Alert $state "sentinel_silent" "WARN" (
            "Sentinel heartbeat is $([int]$sAge)s old (threshold $SentinelStaleS s). " +
            "Re-kicking task '$SentinelTaskName'.") 3600 | Out-Null
        try { & schtasks /Run /TN $SentinelTaskName 2>&1 | Out-Null } catch { }
    } else {
        Clear-AlertKey $state "sentinel_silent"
    }
}

# act
if ($null -ne $restartReason) {
    # A restart is this project's #1 cause of data loss, so the file that records one has to
    # let the morning reader decide in 30 seconds whether it was justified. Before
    # 2026-08-04 this body was one line and a person spent a morning on a restart that
    # should never have happened.
    #
    # There are TWO families of restart reason and only one of them rests on session
    # freshness. Process-absence (process_dead / supervisor_dead / collector_dead) is
    # decided by the sup/col counts alone - an absent process cannot be defended by a
    # session reading. Until 2026-08-06 both families carried the freshness wording, so
    # ALERT_20260806_094100_watch_process_dead (sup=0 col=0, restart correct, collection
    # actually resumed at 09:41) told the morning reader the restart was "probably wrong"
    # and sent them hunting a watchdog defect that did not exist. The progress-stall
    # wording below is UNCHANGED - it was bought expensively on 2026-08-04.
    $judgeLines =
        if (@("process_dead", "supervisor_dead", "collector_dead") -contains $restartReason) {
            "WAS THIS RESTART JUSTIFIED? Read the evidence above:`r`n" +
            "  - this reason rests on PROCESS COUNTS, not on session freshness: " +
            "sup=$($sup.Count) col=$($col.Count) is the whole basis. Both 0 means nothing " +
            "was running, and then the restart was right - a 'session NOT TRUSTED' line " +
            "above does NOT weaken it, because a process that is absent cannot be " +
            "defended by a session reading.`r`n" +
            "  - if sup or col is non-zero above, the watchdog restarted something that " +
            "was still running -> check -SupervisorPattern/-CollectorPattern first; that " +
            "would be a watchdog defect worth reporting.`r`n" +
            "  - what this does NOT tell you is WHY they were gone (machine reboot, crash, " +
            "someone stopped them). data/watchdog.log and the Windows event log answer that."
        } else {
            "WAS THIS RESTART JUSTIFIED? Read the evidence above:`r`n" +
            "  - if 'session' is marked NOT TRUSTED, or 'cross-check' disagrees with " +
            "'session', the reading this decision rests on was stale -> the restart was " +
            "probably wrong, and that is a watchdog defect worth reporting.`r`n" +
            "  - if 'session open' or 'collector up' is small, the collector had not had " +
            "time to do the work it is being blamed for.`r`n" +
            "  - otherwise the observation was fresh and current, and the fault is real."
        }
    Raise-Alert $state "watch_$restartReason" "CRIT" (
        "Watchdog detected: $($problems -join ', ') (session=$session age_min=$ageMin " +
        "sup=$($sup.Count) col=$($col.Count)). Restarting via $LauncherCmd`r`n`r`n" +
        (Get-EvidenceBlock) + "`r`n`r`n" + $judgeLines) 60 | Out-Null
    Invoke-Restart $state $restartReason | Out-Null
    Set-Prop $state "collector_missing_strikes" 0
    Set-Prop $state "freeze_strikes" 0
}

# session_raw vs session is the whole 2026-08-04 lesson in two fields: what the source
# said, and what the watchdog was willing to believe. open_for/col_up say whether a stall
# verdict was even admissible this cycle.
$sessTxt = $session
if ($sessionRaw -ne $session) { $sessTxt = "$session(raw=$sessionRaw,stale=${obsAgeS}s)" }
$summary = "sup=$($sup.Count) col=$($col.Count) session=$sessTxt age_min=$ageMin " +
    "src=$teleSource open_for=${openForS}s col_up=$([int]$colUptimeS)s " +
    "free_gb=$freeGB power=$powerNow($($pw.pct)%)"
if ($null -ne $tele) {
    $c2 = $tele.counters
    $rsaTxt = "n/a"; if ($c2.ContainsKey("ranking_snap_age_s")) { $rsaTxt = [int]$c2["ranking_snap_age_s"] }
    $afTxt = "n/a"; if ($c2.ContainsKey("auth_failures")) { $afTxt = [int]$c2["auth_failures"] }
    $fsTxt = "n/a"; if ($c2.ContainsKey("fetch_success_pct")) { $fsTxt = $c2["fetch_success_pct"] }
    $t2Txt = "n/a"; if ($c2.ContainsKey("tier2_orderbook_snaps")) { $t2Txt = [int]$c2["tier2_orderbook_snaps"] }
    $summary += " rank_age=$rsaTxt auth_fail=$afTxt fetch_pct=$fsTxt t2book=$t2Txt"
}
if ($problems.Count -gt 0) { $summary += " problems=" + ($problems -join ",") }
else { $summary = "OK $summary" }
Write-Log $summary

Save-State $state
if ($null -ne $restartReason) { exit 2 }
if ($problems.Count -gt 0) { exit 1 }
exit 0
