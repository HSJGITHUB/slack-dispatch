[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [Parameter(Mandatory = $true)][string]$ExpectedPrefix,
    [string]$InputFile,
    [switch]$RestrictAcl
)
$AllowedRoots = @((Join-Path $env:USERPROFILE '.local'))
if ($env:SLACK_DISPATCH_ALLOWED_ROOTS) {
    $AllowedRoots += @($env:SLACK_DISPATCH_ALLOWED_ROOTS.Split(';') | Where-Object { $_.Trim() } | ForEach-Object { $_.Trim() })
}
$ErrorActionPreference = 'Stop'
$clipboardUsed = $false
$failure = $null
$success = $null

function Get-RealDestination([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.Substring(2).Contains(':')) { throw 'Alternate data stream paths are not allowed.' }
    $pending = New-Object 'System.Collections.Generic.List[string]'
    $probe = $full
    while ($true) {
        $item = Get-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
        if ($null -ne $item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            if ($probe -eq $full -or -not [IO.Directory]::Exists($probe)) {
                throw 'Target links or broken ancestor links are not allowed.'
            }
        }
        if (Test-Path -LiteralPath $probe) { break }
        $leaf = [IO.Path]::GetFileName($probe)
        if ([string]::IsNullOrEmpty($leaf)) { throw 'Cannot resolve path.' }
        $pending.Insert(0, $leaf)
        $probe = [IO.Path]::GetDirectoryName($probe)
    }
    if (-not ('EnvSecret.NativePath' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
namespace EnvSecret {
    public static class NativePath {
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        static extern SafeFileHandle CreateFile(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        static extern uint GetFinalPathNameByHandle(SafeFileHandle handle, StringBuilder path, uint length, uint flags);
        public static string Resolve(string path) {
            using (var handle = CreateFile(path, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero)) {
                if (handle.IsInvalid) throw new InvalidOperationException();
                var text = new StringBuilder(32768);
                uint size = GetFinalPathNameByHandle(handle, text, (uint)text.Capacity, 0);
                if (size == 0 || size >= text.Capacity) throw new InvalidOperationException();
                return text.ToString();
            }
        }
    }
}
'@ | Out-Null
    }
    $resolved = [EnvSecret.NativePath]::Resolve($probe)
    if ($resolved.StartsWith('\\?\UNC\')) { $resolved = '\\' + $resolved.Substring(8) }
    elseif ($resolved.StartsWith('\\?\')) { $resolved = $resolved.Substring(4) }
    foreach ($part in $pending) { $resolved = [IO.Path]::Combine($resolved, $part) }
    $allowed = $false
    foreach ($root in $AllowedRoots) {
        $boundary = [IO.Path]::GetFullPath($root).TrimEnd('\') + '\'
        if ($resolved.StartsWith($boundary, [StringComparison]::OrdinalIgnoreCase)) { $allowed = $true }
    }
    if (-not $allowed) { throw 'Path is outside allowed roots.' }
    return $resolved
}

try {
    if ($Name -cnotmatch '^[A-Z][A-Z0-9_]{2,63}$') { throw 'Invalid key name format.' }
    if ($ExpectedPrefix.Length -lt 4) { throw 'Prefix must contain at least four characters.' }
    $target = Get-RealDestination $EnvFile
    if (Test-Path -LiteralPath $target -PathType Container) { throw 'Target must be a file.' }
    if ($InputFile) { $value = [IO.File]::ReadAllText([IO.Path]::GetFullPath($InputFile)) }
    else { $clipboardUsed = $true; $value = Get-Clipboard -Raw }
    if ($null -eq $value) { throw 'Input is missing.' }
    $value = $value.Trim()
    if ($value -match '[\r\n]') { throw 'Input must be a single line.' }
    if ($value.Length -lt 20 -or $value.Length -gt 512) { throw 'Input length is outside the allowed range.' }
    if ($value -match '[\s"'']') { throw 'Whitespace and quotes are not allowed.' }
    if (-not $value.StartsWith($ExpectedPrefix, [StringComparison]::Ordinal)) { throw 'Prefix does not match.' }
    $exists = [IO.File]::Exists($target)
    $bytes = [byte[]]@()
    if ($exists) { $bytes = [IO.File]::ReadAllBytes($target) }
    if ($bytes.Length -ge 2 -and (($bytes[0] -eq 255 -and $bytes[1] -eq 254) -or ($bytes[0] -eq 254 -and $bytes[1] -eq 255))) {
        throw 'UTF-16/32 files are not supported.'
    }
    if ($bytes.Length -ge 4 -and $bytes[0] -eq 0 -and $bytes[1] -eq 0 -and $bytes[2] -eq 254 -and $bytes[3] -eq 255) { throw 'UTF-16/32 files are not supported.' }
    $encoding = [Text.Encoding]::GetEncoding(28591)
    $offset = 0
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191) { $offset = 3 }
    $body = $encoding.GetString($bytes, $offset, $bytes.Length - $offset)
    $entry = $encoding.GetString([Text.Encoding]::UTF8.GetBytes($Name + '=' + $value))
    $parts = [regex]::Split($body, '(\r\n|\n)')
    $lines = New-Object 'System.Collections.Generic.List[int]'
    for ($i = 0; $i -lt $parts.Length; $i += 2) {
        if ($parts[$i].StartsWith($Name + '=', [StringComparison]::Ordinal)) {
            $parts[$i] = $entry
            $lines.Add([int]($i / 2) + 1)
        }
    }
    $updated = $parts -join ''
    if ($lines.Count -eq 0) {
        $newline = "`n"
        $firstBreak = [regex]::Match($body, '\r\n|\n')
        if ($firstBreak.Success) { $newline = $firstBreak.Value }
        $lineNumber = ([regex]::Matches($body, '\n')).Count + 1
        if ($body.Length -gt 0 -and -not $body.EndsWith("`n")) { $updated += $newline; $lineNumber++ }
        $lines.Add($lineNumber)
        $updated += $entry + $newline
    }
    $backup = ''
    if ($exists) {
        $backup = $target + '.bak_' + (Get-Date -Format 'yyyyMMdd_HHmmss')
        if ([IO.File]::Exists($backup)) { throw 'A backup already exists for this second. Retry later.' }
    }
    if ($WhatIfPreference) {
        Write-Output ('WhatIf: lines=' + ($lines -join ',') + ' | backup=' + $backup)
    }
    elseif ($PSCmdlet.ShouldProcess($target, 'Store secret')) {
        $parent = [IO.Path]::GetDirectoryName($target)
        [IO.Directory]::CreateDirectory($parent) | Out-Null
        if ($exists) { [IO.File]::Copy($target, $backup, $false) }
        if (-not $exists) { [IO.File]::WriteAllBytes($target, [byte[]]@()) }
        if (-not $exists -or $RestrictAcl) {
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            if ($backup) {
                & icacls.exe $backup /inheritance:r /grant:r ($identity + ':(R,W)') *> $null
                if ($LASTEXITCODE -ne 0) { throw 'Failed to set file permissions.' }
            }
            & icacls.exe $target /inheritance:r /grant:r ($identity + ':(R,W)') *> $null
            if ($LASTEXITCODE -ne 0) { throw 'Failed to set file permissions.' }
        }
        $outputBytes = $encoding.GetBytes($updated)
        if ($offset -eq 3) { $outputBytes = [byte[]](@(239,187,191) + $outputBytes) }
        [IO.File]::WriteAllBytes($target, $outputBytes)
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $hash = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($value)))).Replace('-', '').ToLowerInvariant().Substring(0,8) }
        finally { $sha.Dispose() }
        $success = 'OK: {0} | prefix={1} len={2} sha256[0..8]={3} | file={4} | backup={5}' -f $Name, $value.Substring(0,5), $value.Length, $hash, $target, [IO.Path]::GetFileName($backup)
    }
}
catch {
    # Only report fixed reasons because exceptions may contain file contents or secrets.
    $safeReasons = @('Target links or broken ancestor links are not allowed.', 'Prefix must contain at least four characters.', 'Alternate data stream paths are not allowed.', 'Cannot resolve path.', 'Path is outside allowed roots.', 'Invalid key name format.', 'Target must be a file.', 'Input is missing.', 'Input must be a single line.', 'Input length is outside the allowed range.', 'Whitespace and quotes are not allowed.', 'Prefix does not match.', 'UTF-16/32 files are not supported.', 'A backup already exists for this second. Retry later.', 'Failed to set file permissions.')
    $failure = 'File or input processing failed.'
    if ($safeReasons -contains $_.Exception.Message) { $failure = $_.Exception.Message }
}
finally {
    if ($clipboardUsed) {
        try { Set-Clipboard -Value ' ' }
        catch { $failure = 'Failed to clear clipboard.' }
    }
}
if ($failure) { Write-Output ('ERROR: ' + $failure); exit 2 }
if ($success) {
    if ($clipboardUsed) { $success += ' | clipboard cleared' }
    else { $success += ' | clipboard unused' }
    Write-Output $success
}
