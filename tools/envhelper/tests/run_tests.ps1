$ErrorActionPreference = 'Stop'
$helper = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\set_env_secret.ps1'))
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$tempRoot = Join-Path $repoRoot ('tests\tmp\' + [Guid]::NewGuid().ToString('N'))
$previousRoots = $env:SLACK_DISPATCH_ALLOWED_ROOTS
$env:SLACK_DISPATCH_ALLOWED_ROOTS = $tempRoot
[IO.Directory]::CreateDirectory($tempRoot) | Out-Null
$utf8 = New-Object Text.UTF8Encoding($false)
$good = 'xoxp-FAKE-abcdefghijklmnopqrstuv'
$old = 'xoxp-FAKE-oldabcdefghijklmnopqr'
$inputPath = Join-Path $tempRoot 'input.txt'
[IO.File]::WriteAllText($inputPath, $good, $utf8)
$engine = (Get-Process -Id $PID).Path
$script:passed = 0
$script:skipped = 0
$strictTests = $env:SLACK_DISPATCH_STRICT_TESTS -eq '1'
function Check($Condition, [string]$Label) {
    if (-not $Condition) { throw ('Check failed: ' + $Label) }
    $script:passed++
    Write-Output ('PASS: ' + $Label)
}
function Invoke-Helper([string]$Target, [string[]]$Extra = @(), [string]$Source = $inputPath, [string]$Prefix = 'xoxp-') {
    $messages = @(& $engine -NoProfile -ExecutionPolicy Bypass -File $helper -Name SLACK_USER_TOKEN -EnvFile $Target -ExpectedPrefix $Prefix -InputFile $Source @Extra 2>&1)
    $code = $LASTEXITCODE
    $text = $messages -join "`n"
    if ($text.Contains($good) -or $text.Contains($old)) { throw 'Output contains a complete fake secret.' }
    return @{ Code = $code; Text = $text }
}
function BytesEqual([byte[]]$Left, [byte[]]$Right) {
    return [Convert]::ToBase64String($Left) -ceq [Convert]::ToBase64String($Right)
}
try {
    foreach ($kind in @('crlf', 'lf', 'bom', 'mixed')) {
        $eol = "`n"
        if ($kind -eq 'crlf') { $eol = "`r`n" }
        $before = '# preserve café' + $eol + 'SLACK_USER_TOKEN=' + $old + $eol + 'OTHER=retained' + $eol
        if ($kind -eq 'mixed') { $before = "# café`r`nSLACK_USER_TOKEN=$old`nOTHER=retained`r`n" }
        $original = $utf8.GetBytes($before)
        if ($kind -eq 'bom') { $original = [byte[]](@(239,187,191) + $original) }
        $target = Join-Path $tempRoot ($kind + '.env')
        [IO.File]::WriteAllBytes($target, $original)
        $aclBefore = (Get-Acl -LiteralPath $target).Sddl
        $r = Invoke-Helper $target
        Check ($r.Code -eq 0) ($kind + ' execution')
        $expected = $utf8.GetBytes($before.Replace($old, $good))
        if ($kind -eq 'bom') { $expected = [byte[]](@(239,187,191) + $expected) }
        Check (BytesEqual ([IO.File]::ReadAllBytes($target)) $expected) ($kind + ' all bytes and line endings preserved')
        $backups = @(Get-ChildItem -LiteralPath $tempRoot -Filter ($kind + '.env.bak_*'))
        Check ($backups.Count -eq 1) ($kind + ' backup created')
        Check (BytesEqual ([IO.File]::ReadAllBytes($backups[0].FullName)) $original) ($kind + ' backup matches original')
        Check ((Get-Acl -LiteralPath $target).Sddl -ceq $aclBefore) ($kind + ' existing ACL preserved')
    }
    foreach ($ending in @('none', 'lf', 'empty')) {
        $target = Join-Path $tempRoot ('append-' + $ending + '.env')
        $before = 'OTHER=retained'
        if ($ending -eq 'lf') { $before += "`n" }
        if ($ending -eq 'empty') { $before = '' }
        [IO.File]::WriteAllText($target, $before, $utf8)
        $r = Invoke-Helper $target
        $expected = $before
        if ($ending -eq 'none') { $expected += "`n" }
        $expected += 'SLACK_USER_TOKEN=' + $good + "`n"
        Check ($r.Code -eq 0 -and (BytesEqual ([IO.File]::ReadAllBytes($target)) $utf8.GetBytes($expected))) ('append key ' + $ending)
    }
    $target = Join-Path $tempRoot 'duplicates.env'
    [IO.File]::WriteAllText($target, "SLACK_USER_TOKEN=$old`nSLACK_USER_TOKEN=$old", $utf8)
    $r = Invoke-Helper $target
    Check ($r.Code -eq 0 -and [IO.File]::ReadAllText($target) -ceq "SLACK_USER_TOKEN=$good`nSLACK_USER_TOKEN=$good") 'duplicate keys without final newline'
    $target = Join-Path $tempRoot 'nested\new.env'
    $r = Invoke-Helper $target
    Check ($r.Code -eq 0 -and (BytesEqual ([IO.File]::ReadAllBytes($target)) $utf8.GetBytes("SLACK_USER_TOKEN=$good`n"))) 'new directory and LF file without BOM'
    Check ((Get-Acl -LiteralPath $target).AreAccessRulesProtected) 'new file ACL inheritance removed'
    $target = Join-Path $tempRoot 'restricted.env'
    [IO.File]::WriteAllText($target, 'OTHER=retained', $utf8)
    $r = Invoke-Helper $target @('-RestrictAcl')
    Check ($r.Code -eq 0 -and (Get-Acl -LiteralPath $target).AreAccessRulesProtected) 'explicit RestrictAcl'
    $backups = @(Get-ChildItem -LiteralPath $tempRoot -Filter 'restricted.env.bak_*')
    Check ($backups.Count -eq 1 -and (Get-Acl -LiteralPath $backups[0].FullName).AreAccessRulesProtected) 'RestrictAcl backup ACL inheritance removed'
    Check ([IO.File]::ReadAllText($backups[0].FullName) -ceq 'OTHER=retained') 'RestrictAcl backup original preserved'
    foreach ($prefix in @('x', 'xo', 'xox')) {
        $target = Join-Path $tempRoot ('short-prefix-' + $prefix.Length + '.env')
        $r = Invoke-Helper -Target $target -Prefix $prefix
        Check ($r.Code -eq 2 -and -not (Test-Path -LiteralPath $target)) ('reject short prefix ' + $prefix.Length)
    }
    $r = Invoke-Helper -Target (Join-Path $tempRoot 'prefix-four.env') -Prefix 'xoxp'
    Check ($r.Code -eq 0) 'allow four-character prefix'
    $linkSource = Join-Path $tempRoot 'link-source.env'
    $link = Join-Path $tempRoot 'file-link.env'
    $original = $utf8.GetBytes("SLACK_USER_TOKEN=$old`n")
    [IO.File]::WriteAllBytes($linkSource, $original)
    $fileLinkCreated = $false
    try {
        & cmd.exe /d /c mklink $link $linkSource *> $null
        $fileLinkCreated = $LASTEXITCODE -eq 0
    }
    catch { $fileLinkCreated = $false }
    if ($fileLinkCreated) {
        Check ((Get-Item -LiteralPath $link -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) 'create ReparsePoint test file link'
        $r = Invoke-Helper $link
        Check ($r.Code -eq 2 -and (BytesEqual ([IO.File]::ReadAllBytes($linkSource)) $original) -and @(Get-ChildItem -LiteralPath $tempRoot -Filter 'link-source.env.bak_*').Count -eq 0) 'reject ReparsePoint file without changing original'
    }
    else {
        foreach ($label in @('create ReparsePoint test file link', 'reject ReparsePoint file without changing original')) {
            $script:skipped++
            Write-Output ('SKIPPED (no symlink privilege): ' + $label)
        }
    }
    $broken = Join-Path $tempRoot 'broken-parent'
    $missing = Join-Path $tempRoot 'missing-parent'
    & cmd.exe /d /c mklink /J $broken $missing *> $null
    Check ($LASTEXITCODE -eq 0) 'create broken ancestor junction'
    $r = Invoke-Helper (Join-Path $broken 'nested\reject.env')
    Check ($r.Code -eq 2 -and -not [IO.Directory]::Exists($missing)) 'reject broken ancestor link chain'
    foreach ($bad in @('prefix', 'multiline', 'space', 'quote', 'short', 'long')) {
        $inputBad = Join-Path $tempRoot ($bad + '.txt')
        $badValue = $good
        switch ($bad) {
            prefix { $badValue = 'xoxp-FAKE-wrongprefixabcdefghijkl'.Replace('xoxp-', 'xapp-') }
            multiline { $badValue = $good + "`n" + $good }
            space { $badValue = $good + ' middle' }
            quote { $badValue = $good + '"' }
            short { $badValue = 'xoxp-FAKE' }
            long { $badValue = 'xoxp-FAKE-' + ('a' * 510) }
        }
        [IO.File]::WriteAllText($inputBad, $badValue, $utf8)
        $target = Join-Path $tempRoot ($bad + '-reject.env')
        $r = Invoke-Helper $target @() $inputBad
        Check ($r.Code -eq 2 -and -not [IO.File]::Exists($target) -and -not $r.Text.Contains($badValue)) ($bad + ' reject without disclosing value')
    }
    $target = Join-Path $env:TEMP ('env-helper-reject-' + [Guid]::NewGuid().ToString('N') + '.env')
    $r = Invoke-Helper $target
    Check ($r.Code -eq 2 -and -not (Test-Path -LiteralPath $target)) 'reject TEMP path without creating external files'
    $r = Invoke-Helper ($tempRoot + '-other\reject.env')
    Check ($r.Code -eq 2) 'allowed root name prefix boundary'
    $target = Join-Path $tempRoot 'whatif.env'
    $original = $utf8.GetBytes("OTHER=retained`r`nSLACK_USER_TOKEN=$old`r`n")
    [IO.File]::WriteAllBytes($target, $original)
    $r = Invoke-Helper $target @('-WhatIf')
    Check ($r.Code -eq 0 -and $r.Text -match 'lines=2' -and $r.Text -match '\.bak_\d{8}_\d{6}') 'WhatIf line numbers and backup path'
    Check ((BytesEqual ([IO.File]::ReadAllBytes($target)) $original) -and @(Get-ChildItem -LiteralPath $tempRoot -Filter 'whatif.env.bak_*').Count -eq 0) 'WhatIf leaves file and backups unchanged'
    $target = Join-Path $tempRoot 'whatif-new\new.env'
    $r = Invoke-Helper $target @('-WhatIf')
    Check ($r.Code -eq 0 -and -not (Test-Path -LiteralPath (Split-Path $target))) 'WhatIf does not create directory'
    $trimmed = Join-Path $tempRoot 'trim.txt'
    [IO.File]::WriteAllText($trimmed, " `r`n$good`r`n ", $utf8)
    $r = Invoke-Helper (Join-Path $tempRoot 'trim.env') @() $trimmed
    Check ($r.Code -eq 0) 'trim surrounding whitespace and CRLF'
    $junction = Join-Path $tempRoot 'outside-link'
    & cmd.exe /d /c mklink /J $junction $env:TEMP *> $null
    Check ($LASTEXITCODE -eq 0) 'create path guard test junction'
    $r = Invoke-Helper (Join-Path $junction 'must-not-create.env')
    Check ($r.Code -eq 2) 'reject junction pointing outside'
    $inside = Join-Path $tempRoot 'inside-link'
    & cmd.exe /d /c mklink /J $inside (Join-Path $tempRoot 'nested') *> $null
    Check ($LASTEXITCODE -eq 0) 'create junction inside allowed root'
    $r = Invoke-Helper (Join-Path $inside 'via-link.env')
    Check ($r.Code -eq 0 -and [IO.File]::Exists((Join-Path $tempRoot 'nested\via-link.env'))) 'allow resolved internal junction path'
    foreach ($mode in @('success', 'failure', 'whatif')) {
        $wrapper = Join-Path $tempRoot ('clipboard-' + $mode + '.ps1')
        $marker = Join-Path $tempRoot ('cleared-' + $mode + '.txt')
        $clipTarget = Join-Path $tempRoot ('clipboard-' + $mode + '.env')
        $template = @"
param([string]`$Helper, [string]`$Target, [string]`$Marker, [string]`$Mode)
`$ErrorActionPreference = 'Stop'
function Get-Clipboard { param([switch]`$Raw); if (`$Mode -eq 'failure') { return 'xoxp-FAKE' }; return 'xoxp-FAKE-abcdefghijklmnopqrstuv' }
function Set-Clipboard { param([string]`$Value); if (`$Value -cne ' ') { throw 'Invalid clear argument' }; [IO.File]::WriteAllText(`$Marker, 'cleared') }
`$options = @{}
if (`$Mode -eq 'whatif') { `$options.WhatIf = `$true }
`$global:LASTEXITCODE = 0
& `$Helper -Name SLACK_USER_TOKEN -EnvFile `$Target -ExpectedPrefix xoxp- @options
exit `$LASTEXITCODE
"@
        [IO.File]::WriteAllText($wrapper, $template, (New-Object Text.UTF8Encoding($true)))
        $messages = @(& $engine -NoProfile -ExecutionPolicy Bypass -File $wrapper -Helper $helper -Target $clipTarget -Marker $marker -Mode $mode 2>&1)
        $code = $LASTEXITCODE
        $expectedCode = 0
        if ($mode -eq 'failure') { $expectedCode = 2 }
        Check ($code -eq $expectedCode -and [IO.File]::Exists($marker)) ('mock clipboard finally cleared ' + $mode)
        Check (-not (($messages -join "`n").Contains($good))) ('mock clipboard value not disclosed ' + $mode)
        if ($mode -ne 'success') { Check (-not [IO.File]::Exists($clipTarget)) ('mock clipboard file not created ' + $mode) }
        else { Check (($messages -join "`n") -match 'clipboard cleared') 'mock clipboard success indicator' }
    }
    if ($strictTests -or $fileLinkCreated) { Check $fileLinkCreated 'file symlink test available' }
    Write-Output ('RESULT: PASS | checks=' + $script:passed + ' | skipped=' + $script:skipped + ' | PowerShell=' + $PSVersionTable.PSVersion)
    Write-Output ('TEMP: ' + $tempRoot)
    exit 0
}
catch {
    Write-Output ('RESULT: FAIL | checks=' + $script:passed + ' | skipped=' + $script:skipped + ' | ' + $_.Exception.Message)
    Write-Output ('TEMP: ' + $tempRoot)
    exit 1
}
finally { $env:SLACK_DISPATCH_ALLOWED_ROOTS = $previousRoots }
