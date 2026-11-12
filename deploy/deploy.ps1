[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$Checkout,
    [Parameter(Mandatory = $true)][string]$Tag,
    [Parameter(Mandatory = $true)][string]$Target,
    [string]$ArchiveDirectory = $env:TEMP
)
$ErrorActionPreference = 'Stop'
$ref = 'refs/tags/' + $Tag
& git check-ref-format $ref
if ($LASTEXITCODE -ne 0) { throw 'Invalid tag name.' }
$commit = & git -C $Checkout rev-parse --verify ($ref + '^{commit}')
if ($LASTEXITCODE -ne 0) { throw 'Tag cannot be resolved to a commit.' }
$commit = ($commit | Out-String).Trim()
$entries = & git -C $Checkout ls-tree -r $commit
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect tag tree.' }
if ($entries -match '^(120000|160000) ' -or $entries -match '\tDEPLOYED\.json$') {
    throw 'Symlinks, submodules and a tracked DEPLOYED.json are not supported.'
}
$destination = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Target)
if (Test-Path -LiteralPath $destination) { throw 'Target must be a new directory.' }
if ($PSCmdlet.ShouldProcess($destination, 'Extract tagged checkout and write deployment manifest')) {
    $null = [IO.Directory]::CreateDirectory($ArchiveDirectory)
    $archive = Join-Path $ArchiveDirectory ('dispatch-' + [guid]::NewGuid().ToString('N') + '.zip')
    & git -C $Checkout archive --format=zip ('--output=' + $archive) $commit
    if ($LASTEXITCODE -ne 0) { throw 'git archive failed.' }
    Expand-Archive -LiteralPath $archive -DestinationPath $destination
    $hashes = [ordered]@{}
    Get-ChildItem -LiteralPath $destination -File -Recurse -Force | Sort-Object FullName | ForEach-Object {
        $relative = $_.FullName.Substring($destination.TrimEnd('\', '/').Length + 1).Replace('\', '/')
        $hashes[$relative] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $manifest = [ordered]@{ tag = $Tag; commit = $commit; sha256 = $hashes; time = [DateTimeOffset]::Now.ToString('o') }
    $json = $manifest | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText((Join-Path $destination 'DEPLOYED.json'), $json, [Text.UTF8Encoding]::new($false))
    Write-Output $json
    Write-Verbose ('Archive retained at ' + $archive)
}