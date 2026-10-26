param([Parameter(Mandatory=$true)][string]$Destination, [switch]$IncludeTests)
$ErrorActionPreference = 'Stop'
$project = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$target = [IO.Path]::GetFullPath($Destination)
if (-not $target.StartsWith((Join-Path $project 'build\work') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Stage destination must be a fresh build/work child'
}
if (Test-Path -LiteralPath $target) { throw 'Stage destination already exists' }
New-Item -ItemType Directory -Path $target | Out-Null
foreach ($name in @('app.py','settings.py','listen_msg.py','send_live_message.py','requirements.txt','README.md')) {
    Copy-Item -LiteralPath (Join-Path $project $name) -Destination $target
}
$packages = @('desktop','tiktok_live')
if ($IncludeTests) { $packages += @('tests','build','docs') }
foreach ($package in $packages) {
    $packageRoot = Join-Path $project $package
    foreach ($file in Get-ChildItem -LiteralPath $packageRoot -Recurse -File) {
        $relative = $file.FullName.Substring($project.Length + 1)
        if ($relative -match '(^|\\)(work|__pycache__|runtime|vendor|js_reverse_cache)(\\|$)') { continue }
        if ($file.Extension -notin @('.py','.json','.png','.ico','.spec','.ps1','.sh','.txt','.md')) { continue }
        if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Source reparse point' }
        $output = Join-Path $target $relative
        New-Item -ItemType Directory -Path (Split-Path $output -Parent) -Force | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $output
    }
}
Write-Output $target
