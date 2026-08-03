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
    [string]$SentinelTaskName = "tossmon-sentinel"
)

$ErrorActionPreference = "Stop"
if ($DataDir -eq "") { $DataDir = Join-Path $RepoRoot "data" }
if ($StateDir -eq "") { $StateDir = Join-Path $DataDir "ops_state" }
if ($ExeLike -eq "") { $ExeLike = (Join-Path $RepoRoot ".venv") + "*" }
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

    # Prefix decides what a morning glance means. ALERT_ must stay "something needs
    # attention": PLANNED_ for operator-driven work, NOTE_ for INFO-level records that
    # are explicitly not faults (designed budget yielding, the clamp branch firing).
    # Filing those as ALERT_ re-breaks the "any ALERT_ file is trouble" rule.
    $planned = $alwaysPlanned -or $script:PlannedNow.active
    $prefix = "ALERT"
    if ($level -eq "INFO") { $prefix = "NOTE" }
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
    "auth_failures", "loop_errors", "schema_mismatch", "event_write_failures",
    "promotion_write_failures", "rankings_write_failures", "rankings_clamped",
    "prices_missing", "candles_1m", "api_errors", "tier2_orderbook_snaps"
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
    $c["ranking_snap_age_s"] = -1.0
    if ($null -ne $j.last_ranking_snap_ms -and [long]$j.last_ranking_snap_ms -gt 0) {
        $c["ranking_snap_age_s"] = [math]::Max(0,
            [math]::Floor(((Get-Date) - [DateTimeOffset]::FromUnixTimeMilliseconds([long]$j.last_ranking_snap_ms).LocalDateTime).TotalSeconds))
    }
    $sess = "unknown"
    if ($null -ne $j.session) { $sess = [string]$j.session }
    $sig = (($c.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join " ")
    return @{ ts = $ts; ts_str = $ts.ToString("yyyy-MM-dd HH:mm:ss"); session = $sess
              counters = $c; counters_sig = $sig; source = "state" }
}

# Read the collector's health from the best available source, and say clearly when it
# could not be read at all. Being unable to see is itself an incident: the counter-freeze
# detection - the only early signal we have for a silent API death - is dead while blind.
function Get-CollectorSnapshot {
    $tried = @()
    $s = Get-TelemetryFromState
    if ($null -ne $s) {
        $ageMin = ((Get-Date) - $s.ts).TotalMinutes
        if ($ageMin -le $FreshCritMin) { return @{ ok = $true; snap = $s; reason = "" } }
        $tried += ("collector_state.json is stale ({0:N1} min old, threshold {1} min)" -f $ageMin, $FreshCritMin)
    } elseif (Test-Path $StateJson) {
        $tried += "collector_state.json exists but could not be parsed (truncated or corrupt JSON?)"
    } else {
        $tried += "collector_state.json does not exist at $StateJson"
    }
    $t = Get-LastTelemetry
    if ($null -ne $t) {
        $t["counters"] = Parse-Counters $t.counters
        $t["counters_sig"] = (($t.counters.GetEnumerator() | Sort-Object Name |
            ForEach-Object { "$($_.Name)=$($_.Value)" }) -join " ")
        $t["source"] = "log"
        return @{ ok = $true; snap = $t; reason = ("state file unusable, fell back to log: " + ($tried -join "; ")) }
    }
    if (-not (Test-Path $CollectorLog)) { $tried += "collector.log does not exist at $CollectorLog" }
    else {
        $sz = [math]::Round((Get-Item $CollectorLog).Length / 1MB, 1)
        $tried += ("no parseable 'telemetry session=' line within the time-based log scan " +
                   "(log is ${sz}MB, last modified $((Get-Item $CollectorLog).LastWriteTime.ToString('HH:mm:ss')))")
    }
    return @{ ok = $false; snap = $null; reason = ($tried -join "; ") }
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
$ageMin = -1
if ($null -ne $tele) {
    $session = $tele.session
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
    $openNow = ($session -ne "closed")
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

# ---- W4 watchdog contract (main 27abc3f): counters + log strings ----
# These counters exist only on post-27abc3f collectors. On an older binary they are
# simply absent from the telemetry line and every check below no-ops (no false alarms).
$openSession = ($session -ne "closed" -and $session -ne "unknown")
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
    if ($openSession -and $cur.ContainsKey("ranking_snap_age_s")) {
        $rsa = [int]$cur["ranking_snap_age_s"]
        if ($rsa -lt 0) {
            # Only strike on a NEW telemetry line. Reading the same stale line twice
            # (telemetry is 5-minutely, so is this watchdog) must not count as two
            # independent observations - that would restart on a single startup -1.
            $strk = Get-Prop $state "ranking_never_strikes" 0
            if ($tele.ts_str -ne $prevTeleTs) { $strk = $strk + 1 }
            Set-Prop $state "ranking_never_strikes" $strk
            if ($strk -ge 2 -and $null -eq $restartReason) {
                $problems += "ranking_snap_never"
                $restartReason = "ranking_snap_never"
            }
        } elseif ($rsa -gt $RankingSnapAgeCritS) {
            Set-Prop $state "ranking_never_strikes" 0
            $problems += "ranking_snap_age_${rsa}s"
            Raise-Alert $state "ranking_snap_stalled" "CRIT" (
                "HIGHEST PRIORITY: ranking_snap_age_s=$rsa (threshold $RankingSnapAgeCritS s) " +
                "during session=$session. The ranking loop has silently stopped and rankings " +
                "CANNOT be back-filled - every minute of this is permanent data loss. " +
                "Restarting the collector.") 900 | Out-Null
            if ($null -eq $restartReason) { $restartReason = "ranking_snap_stalled" }
        } else {
            Set-Prop $state "ranking_never_strikes" 0
            Clear-AlertKey $state "ranking_snap_stalled"
        }
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

    # (3) schema_mismatch - API response shape changed. A restart cannot fix this;
    # alert only, so a human looks at it.
    $dSchema = Delta "schema_mismatch"
    if ($null -ne $dSchema -and $dSchema -gt 0) {
        $problems += "schema_mismatch_$dSchema"
        Raise-Alert $state "schema_mismatch" "CRIT" (
            "schema_mismatch rose by $dSchema (total $($cur['schema_mismatch'])) - the API " +
            "response shape changed. A restart will NOT fix this. Inspect collector.log and " +
            "the endpoint contract before trusting today's data.") 3600 | Out-Null
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
    # exit design needs. Flat during an open session means we are silently NOT collecting
    # it - the failure mode is invisible otherwise, because nothing errors.
    if ($openSession -and $cur.ContainsKey("tier2_orderbook_snaps")) {
        $dT2 = Delta "tier2_orderbook_snaps"
        if ($null -ne $dT2 -and $dT2 -eq 0) {
            $strk = (Get-Prop $state "t2book_flat_strikes" 0) + 1
            Set-Prop $state "t2book_flat_strikes" $strk
            if ($strk -ge 3) {
                $problems += "tier2_orderbook_flat_x$strk"
                Raise-Alert $state "tier2_orderbook_flat" "WARN" (
                    "tier2_orderbook_snaps has not advanced for $strk cycles during session=" +
                    "$session (total $($cur['tier2_orderbook_snaps'])). Either polling.tier2_orderbook_s " +
                    "is 0/unset in the live config, or there are no tier2 members, or the loop " +
                    "is stalled. This is silent - no error is logged when it happens.") 3600 | Out-Null
            }
        } else {
            Set-Prop $state "t2book_flat_strikes" 0
            Clear-AlertKey $state "tier2_orderbook_flat"
        }
    } else {
        Set-Prop $state "t2book_flat_strikes" 0
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
$tailText = (Get-LogTail 500) -join "`n"
if ($tailText.Length -gt 0) {
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
        $n = ([regex]::Matches($tailText, $lp.Rx)).Count
        if ($n -ge $lp.Min) {
            $problems += "$($lp.Key)_$n"
            Raise-Alert $state $lp.Key $lp.Level ("$($lp.Msg)`r`n`r`nOccurrences in the last 500 log lines: $n") 3600 | Out-Null
        } else {
            Clear-AlertKey $state $lp.Key
        }
    }
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
    Raise-Alert $state "watch_$restartReason" "CRIT" (
        "Watchdog detected: $($problems -join ', ') (session=$session age_min=$ageMin " +
        "sup=$($sup.Count) col=$($col.Count)). Restarting via $LauncherCmd") 60 | Out-Null
    Invoke-Restart $state $restartReason | Out-Null
    Set-Prop $state "collector_missing_strikes" 0
    Set-Prop $state "freeze_strikes" 0
}

$summary = "sup=$($sup.Count) col=$($col.Count) session=$session age_min=$ageMin " +
    "src=$teleSource free_gb=$freeGB power=$powerNow($($pw.pct)%)"
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
