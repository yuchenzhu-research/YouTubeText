# Exercise the real Windows venv/pip fallback without installing the full model stack.
# The caller supplies a supported Python and a fresh, isolated runner temp root.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$python = $env:YOUTUBETEXT_FALLBACK_TEST_PYTHON
$root = $env:YOUTUBETEXT_FALLBACK_TEST_ROOT
if ([string]::IsNullOrWhiteSpace($python) -or -not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "YOUTUBETEXT_FALLBACK_TEST_PYTHON must name a Python executable."
}
if ([string]::IsNullOrWhiteSpace($root) -or (Test-Path -LiteralPath $root)) {
    throw "YOUTUBETEXT_FALLBACK_TEST_ROOT must be a fresh temporary path."
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$scriptDirectory = Join-Path $root "scripts"
$toolDirectory = Join-Path $root "test tools"
New-Item -ItemType Directory -Path $scriptDirectory, $toolDirectory -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot "scripts\install.ps1") -Destination $scriptDirectory
Copy-Item -LiteralPath (Join-Path $projectRoot "scripts\_windows.ps1") -Destination $scriptDirectory

# This tiny editable package proves that pip is really invoked while avoiding
# RapidOCR/Whisper wheels in a second, independent virtual environment.
Set-Content -LiteralPath (Join-Path $root "pyproject.toml") -Encoding Ascii -Value @'
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "youtubetext-fallback-fixture"
version = "0.0.1"

[project.scripts]
youtubetext = "fallback_fixture:main"

[tool.setuptools]
py-modules = ["fallback_fixture"]
'@
Set-Content -LiteralPath (Join-Path $root "fallback_fixture.py") -Encoding Ascii -Value @'
def main():
    print("fallback fixture ready")
'@
Set-Content -LiteralPath (Join-Path $toolDirectory "ffmpeg.cmd") -Encoding Ascii -Value @'
@echo off
exit /b 1
'@

# setup-uv is present on the runner, so remove only PATH entries containing uv.
# The absolute Python path remains usable for venv creation.
$remainingPath = foreach ($entry in ($env:PATH -split ";")) {
    $directory = $entry.Trim().Trim('"')
    if (-not $directory) {
        continue
    }
    $containsUv = $false
    foreach ($name in @("uv.exe", "uv.cmd", "uv.bat", "uv.com")) {
        if (Test-Path -LiteralPath (Join-Path $directory $name)) {
            $containsUv = $true
            break
        }
    }
    if (-not $containsUv) {
        $entry
    }
}
$env:PATH = "$toolDirectory;" + ($remainingPath -join ";")
if ($null -ne (Get-Command "uv" -ErrorAction SilentlyContinue)) {
    throw "uv is still visible; the fallback branch was not isolated."
}
$env:YOUTUBETEXT_PYTHON = $python

$installer = Join-Path $scriptDirectory "install.ps1"
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    # Windows PowerShell 5.1 represents native stderr as error records even
    # when the child succeeds; collect it without aborting before exit checks.
    $output = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $installer 2>&1
    $installerExit = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousErrorAction
}
if ($installerExit -ne 0) {
    throw "Windows fallback installer failed: $($output -join [Environment]::NewLine)"
}
if (($output -join "`n") -notmatch "uv was not found; using Python's built-in venv and pip") {
    throw "The installer did not take the pip fallback branch."
}

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$command = Join-Path $root ".venv\Scripts\youtubetext.exe"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf) -or
    -not (Test-Path -LiteralPath $command -PathType Leaf)) {
    throw "The fallback did not create a working virtual environment and command."
}
$installed = & $command
if ($LASTEXITCODE -ne 0 -or ($installed -join "`n") -notmatch "fallback fixture ready") {
    throw "The editable package did not run from the fallback virtual environment."
}
Write-Host "Windows venv/pip fallback passed in $root"
