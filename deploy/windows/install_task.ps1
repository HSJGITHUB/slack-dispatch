[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$Workdir,
    [Parameter(Mandatory = $true)][string]$Config,
    [Parameter(Mandatory = $true)][string]$User,
    [string]$TaskName = 'SlackDispatch',
    [string]$OutputDirectory = $env:TEMP
)
$ErrorActionPreference = 'Stop'
if ([IO.Path]::GetFileName($Python) -ine 'pythonw.exe') { throw 'Python must name pythonw.exe.' }
foreach ($value in @($Python, $Workdir, $Config, $User, $TaskName, $OutputDirectory)) {
    if ($value -match '["\r\n]') { throw 'Quotes and newlines are not supported in parameters.' }
}
$rendered = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'task_template.xml'), [Text.Encoding]::UTF8)
$values = @{ python = $Python; workdir = $Workdir; config = $Config; user = $User }
$rendered = [regex]::Replace($rendered, '\{(python|workdir|config|user)\}', {
    param($match)
    [Security.SecurityElement]::Escape($values[$match.Groups[1].Value])
})
$null = [xml]$rendered
$output = Join-Path $OutputDirectory ('dispatch-task-' + [guid]::NewGuid().ToString('N') + '.xml')
if ($PSCmdlet.ShouldProcess($output, 'Write rendered Task Scheduler XML')) {
    $null = [IO.Directory]::CreateDirectory($OutputDirectory)
    [IO.File]::WriteAllText($output, $rendered, [Text.Encoding]::Unicode)
}
Write-Output ('schtasks.exe /Create /TN "{0}" /XML "{1}" /F' -f $TaskName, $output)
# Registration is always manual. No schtasks invocation occurs here.