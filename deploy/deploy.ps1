[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$Checkout,
    [Parameter(Mandatory = $true)][string]$Tag,
    [Parameter(Mandatory = $true)][string]$Target,
    [string]$ArchiveDirectory = $env:TEMP
)
$ErrorActionPreference = 'Stop'
# git exit codes are checked through $LASTEXITCODE below. PowerShell 7 would otherwise
# throw its own error first when the caller turns this preference on; 5.1 ignores it.
$PSNativeCommandUseErrorActionPreference = $false

function Get-PathComparison {
    if ([IO.Path]::DirectorySeparatorChar -eq '\') {
        return [StringComparison]::OrdinalIgnoreCase
    }
    return [StringComparison]::Ordinal
}

function Expand-DeploymentArchive {
    # Extracts every entry itself instead of calling Expand-Archive or any
    # ExtractToDirectory overload, so it behaves the same in Windows PowerShell 5.1
    # and PowerShell 7. All entries are checked before anything is written.
    param(
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][string]$DestinationPath
    )
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $comparison = Get-PathComparison
    $separator = [IO.Path]::DirectorySeparatorChar
    $invalidNameCharacters = [IO.Path]::GetInvalidFileNameChars()
    $prefix = [IO.Path]::GetFullPath($DestinationPath).TrimEnd('\', '/') + $separator
    # GetFullPath expands 8.3 short names of existing directories, but not
    # consistently, so a destination given with short names could resolve to two
    # different spellings. Entry names are therefore resolved under a fresh,
    # nonexistent directory on the same volume and the contained remainder is
    # appended to the destination prefix.
    $probeName = 'deploy-check-' + [guid]::NewGuid().ToString('N')
    $probe = [IO.Path]::GetFullPath([IO.Path]::Combine([IO.Path]::GetPathRoot($prefix), $probeName)) + $separator
    $zip = [System.IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        # A method call, not the Entries property: PowerShell turns an exception in a
        # property getter (such as an entry name with characters Windows PowerShell 5.1
        # rejects) into $null, which would extract nothing and report success.
        try {
            $archiveEntries = $zip.get_Entries()
        }
        catch {
            throw ('Archive entries cannot be read: ' + $_.Exception.GetBaseException().Message)
        }
        $plan = New-Object System.Collections.Generic.List[object]
        foreach ($entry in $archiveEntries) {
            $name = [string]$entry.FullName
            $relative = $name.Replace('/', $separator).Replace('\', $separator)
            if ($name.Length -eq 0 -or $name.Contains(':') -or $relative.StartsWith($separator)) {
                throw ('Archive entry is absolute, drive-qualified or empty: ' + $name)
            }
            foreach ($segment in $relative.Split($separator)) {
                if ($segment.IndexOfAny($invalidNameCharacters) -ge 0) {
                    throw ('Archive entry is not a valid path: ' + $name)
                }
            }
            if ([IO.Path]::IsPathRooted($relative)) {
                throw ('Archive entry is absolute, drive-qualified or empty: ' + $name)
            }
            try {
                $resolved = [IO.Path]::GetFullPath($probe + $relative)
            }
            catch {
                throw ('Archive entry is not a valid path: ' + $name)
            }
            if (-not $resolved.StartsWith($probe, $comparison)) {
                throw ('Archive entry is outside the target directory: ' + $name)
            }
            $path = $prefix + $resolved.Substring($probe.Length)
            $isDirectory = $relative.EndsWith($separator)
            $plan.Add([pscustomobject]@{ Entry = $entry; Path = $path; IsDirectory = $isDirectory })
        }
        $null = [IO.Directory]::CreateDirectory($prefix)
        foreach ($item in $plan) {
            if ($item.IsDirectory) {
                $null = [IO.Directory]::CreateDirectory($item.Path)
                continue
            }
            $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($item.Path))
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($item.Entry, $item.Path, $false)
        }
    }
    finally {
        $zip.Dispose()
    }
}

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
    Expand-DeploymentArchive -ArchivePath $archive -DestinationPath $destination
    $canonicalRoot = (Get-Item -LiteralPath $destination).FullName.TrimEnd('\', '/')
    $rootPrefix = $canonicalRoot + [IO.Path]::DirectorySeparatorChar
    $comparison = Get-PathComparison
    $hashes = [ordered]@{}
    Get-ChildItem -LiteralPath $canonicalRoot -File -Recurse -Force | Sort-Object FullName | ForEach-Object {
        if (-not $_.FullName.StartsWith($rootPrefix, $comparison)) {
            throw 'Deployment file is outside the canonical target directory.'
        }
        $relative = $_.FullName.Substring($rootPrefix.Length).Replace('\', '/')
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            $stream = [IO.File]::OpenRead($_.FullName)
            try {
                $digest = $sha256.ComputeHash($stream)
                $hashes[$relative] = [BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()
            }
            finally {
                $stream.Dispose()
            }
        }
        finally {
            $sha256.Dispose()
        }
    }
    $manifest = [ordered]@{ tag = $Tag; commit = $commit; sha256 = $hashes; time = [DateTimeOffset]::Now.ToString('o') }
    $json = $manifest | ConvertTo-Json -Depth 8
    $manifestPath = Join-Path $canonicalRoot 'DEPLOYED.json'
    [IO.File]::WriteAllText($manifestPath, $json, [Text.UTF8Encoding]::new($false))
    Write-Output ([IO.File]::ReadAllText($manifestPath, [Text.Encoding]::UTF8))
    Write-Verbose ('Archive retained at ' + $archive)
}