param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs = @()
)

# Synchronize the Windows development environment and run tests.
. (Join-Path $PSScriptRoot "_windows.ps1")

Assert-YouTubeTextWindowsX64
$python = Resolve-YouTubeTextPython
Assert-YouTubeTextFFmpeg

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
Set-Location $projectRoot

$uv = Get-Command "uv" -ErrorAction SilentlyContinue
if ($null -ne $uv) {
    Write-Host "Synchronizing development dependencies with uv..."
    Invoke-YouTubeTextChecked -Command $uv.Source -Arguments @(
        "sync", "--locked", "--python", $python
    ) -FailureMessage "uv could not prepare the development environment."
    Write-Host "Running tests..."
    Invoke-YouTubeTextChecked -Command $uv.Source -Arguments (
        @("run", "--no-sync", "pytest") + $PytestArgs
    ) -FailureMessage "The test suite failed."
}
else {
    Write-Host "uv was not found; using Python's built-in venv and pip instead."
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Invoke-YouTubeTextChecked -Command $python -Arguments @(
            "-m", "venv", (Join-Path $projectRoot ".venv")
        ) -FailureMessage "Python could not create .venv."
    }
    Assert-YouTubeTextVenvPython -Executable $venvPython
    Invoke-YouTubeTextChecked -Command $venvPython -Arguments @(
        "-m", "pip", "install", "--editable", $projectRoot,
        "pytest>=8.3,<10", "pytest-asyncio>=0.25,<2"
    ) -FailureMessage "pip could not prepare the development environment."
    Write-Host "Running tests..."
    Invoke-YouTubeTextChecked -Command $venvPython -Arguments (
        @("-m", "pytest") + $PytestArgs
    ) -FailureMessage "The test suite failed."
}
