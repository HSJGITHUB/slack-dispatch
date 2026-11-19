[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$StateDirectory,
    [int]$TimeoutSeconds = 120
)
$ErrorActionPreference = 'Stop'
if ($TimeoutSeconds -le 0) { throw 'TimeoutSeconds must be positive.' }
$heartbeat = Get-Content -LiteralPath (Join-Path $StateDirectory 'heartbeat.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($heartbeat.schema -ne 'slack-dispatch-heartbeat/1' -or
    $null -eq $heartbeat.pid -or [int]$heartbeat.pid -le 0) {
    throw 'Heartbeat schema or pid is invalid.'
}
# Windows PowerShell 5.1 ConvertFrom-Json keeps ts as the ISO 8601 string the service
# wrote. PowerShell 7 turns it into a DateTime (Kind Local or Utc), and passing that
# DateTime to a string parameter formats it as invariant MM/dd/yyyy HH:mm:ss, which drops
# the fractional seconds and the offset. The value is turned back into a round-trip
# string and parsed the same way in both shells, so the check below compares instants.
$timestamp = $heartbeat.ts
if ($timestamp -is [DateTime] -or $timestamp -is [DateTimeOffset]) {
    # 'o' writes Z for Kind Utc, the offset for Kind Local or a DateTimeOffset, and no
    # offset for Kind Unspecified, which the parse below reads as local time, as in 5.1.
    $timestamp = $timestamp.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
}
elseif ($timestamp -isnot [string]) { throw 'Heartbeat timestamp is invalid.' }
$observed = [DateTimeOffset]::Parse($timestamp, [Globalization.CultureInfo]::InvariantCulture,
    [Globalization.DateTimeStyles]::RoundtripKind)
$process = Get-Process -Id ([int]$heartbeat.pid) -ErrorAction SilentlyContinue
if ($null -eq $process) { throw 'Heartbeat process is absent; no stop flag was written.' }
if ($process.StartTime.ToUniversalTime() -gt $observed.UtcDateTime) {
    throw 'Heartbeat predates this process; possible PID reuse. No stop flag was written.'
}
$null = $process.Handle
$stopPath = Join-Path $StateDirectory 'stop.flag'
if ($PSCmdlet.ShouldProcess($stopPath, 'Request shutdown and wait for heartbeat process')) {
    [IO.File]::WriteAllText($stopPath, [DateTimeOffset]::Now.ToString('o'), [Text.UTF8Encoding]::new($false))
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while (-not $process.WaitForExit(250)) {
        if ($timer.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
            throw 'Timed out waiting for shutdown. The stop flag is retained; inspect the service.'
        }
    }
    Write-Output 'Previous process exited. Task Scheduler must launch the next instance.'
}