[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ChromiumPath,
    [string]$PythonExecutable = 'python',
    [switch]$OneFile
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$inputBrowser = (Resolve-Path -LiteralPath $ChromiumPath).Path
if (-not (Test-Path -LiteralPath (Join-Path $inputBrowser 'chrome.exe') -PathType Leaf)) {
    throw 'ChromiumPath must be a clean browser directory containing chrome.exe'
}
$stageSource = Join-Path $projectRoot 'build\work\source_inputs'
$stageBrowser = Join-Path $projectRoot 'browser\chromium'
$verifier = Join-Path $PSScriptRoot 'verify_release.py'
& $PythonExecutable $verifier $inputBrowser --build-project-root $projectRoot --scan-only
if ($LASTEXITCODE -ne 0) { throw 'Chromium input failed path/privacy validation before staging cleanup' }

Push-Location $projectRoot
$previousQt = $env:QT_QPA_PLATFORM
$previousPyinstallerConfig = $env:PYINSTALLER_CONFIG_DIR
$previousBytecode = $env:PYTHONDONTWRITEBYTECODE
try {
    $env:QT_QPA_PLATFORM = 'offscreen'
    # Keep --clean cache deletion inside the validated generated work tree.
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $projectRoot 'build\work\pyinstaller-config'
    # No tests or docs are release inputs. Copy only application source/data.
    # Verify absolute destructive targets before removing stale staging inputs.
    foreach ($stage in @($stageSource, $stageBrowser)) {
        $absolute = [IO.Path]::GetFullPath($stage)
        if (-not $absolute.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Staging directory escaped project root'
        }
        if (Test-Path -LiteralPath $absolute) { Remove-Item -LiteralPath $absolute -Recurse -Force }
        New-Item -ItemType Directory -Path $absolute -Force | Out-Null
    }
    foreach ($name in @('app.py', 'settings.py', 'listen_msg.py', 'send_live_message.py')) {
        Copy-Item -LiteralPath (Join-Path $projectRoot $name) -Destination $stageSource
    }
    foreach ($package in @('desktop', 'tiktok_live')) {
        $packageRoot = Join-Path $projectRoot $package
        foreach ($file in Get-ChildItem -LiteralPath $packageRoot -Recurse -File) {
            if ($file.Extension -notin @('.py', '.json', '.png', '.ico') -or $file.FullName.Contains('__pycache__')) { continue }
            $relative = $file.FullName.Substring($projectRoot.Length + 1)
            $destination = Join-Path $stageSource $relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
            Copy-Item -LiteralPath $file.FullName -Destination $destination
        }
    }
    & $PythonExecutable $verifier $stageSource --scan-only
    if ($LASTEXITCODE -ne 0) { throw 'Application inputs contain credentials' }
    Get-ChildItem -LiteralPath $inputBrowser -Force | Copy-Item -Destination $stageBrowser -Recurse -Force
    & $PythonExecutable $verifier $stageBrowser --scan-only
    if ($LASTEXITCODE -ne 0) { throw 'Staged Chromium failed privacy scan' }
    & $PythonExecutable -X utf8 -X faulthandler -m unittest discover -v
    if ($LASTEXITCODE -ne 0) { throw 'Test suite failed; release not built' }
    if ($OneFile) {
        # Build a candidate only; publication requires the separate five release gates.
        $env:PYTHONDONTWRITEBYTECODE = '1'
        & $PythonExecutable -m PyInstaller --noconfirm --clean --distpath build/work/standalone-wss-candidate --workpath build/work/direct-pyinstaller build/tiktok_desktop_onefile.spec
        if ($LASTEXITCODE -ne 0) { throw 'Onefile candidate build failed' }
        Write-Host 'Candidate built; verify archive, loopback protocol and onefile smoke before publication.'
        return
    }
    & $PythonExecutable -m PyInstaller --noconfirm --clean --distpath dist --workpath build/work/pyinstaller build/tiktok_desktop.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
    & $PythonExecutable $verifier (Join-Path $projectRoot 'dist\TKChatWave') --remove-conflicting-icu --write-manifest
    if ($LASTEXITCODE -ne 0) { throw 'Release verification failed' }
    & $PythonExecutable (Join-Path $PSScriptRoot 'smoke_release.py') (Join-Path $projectRoot 'dist\TKChatWave')
    if ($LASTEXITCODE -ne 0) { throw 'Release startup smoke failed' }
    Write-Host ('Release ready: ' + (Join-Path $projectRoot 'dist\TKChatWave\TKChatWave.exe'))
} finally {
    $env:QT_QPA_PLATFORM = $previousQt
    $env:PYINSTALLER_CONFIG_DIR = $previousPyinstallerConfig
    $env:PYTHONDONTWRITEBYTECODE = $previousBytecode
    Pop-Location
}
