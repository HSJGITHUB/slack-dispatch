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
$observed = [DateTimeOffset]::Parse($heartbeat.ts)
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