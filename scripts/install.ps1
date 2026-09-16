# Prepare a repository-local YouTubeText environment on 64-bit Windows.
. (Join-Path $PSScriptRoot "_windows.ps1")

Assert-YouTubeTextWindowsX64
$python = Resolve-YouTubeTextPython
Assert-YouTubeTextFFmpeg

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$venvCommand = Join-Path $projectRoot ".venv\Scripts\youtubetext.exe"
Set-Location $projectRoot

Write-Host "Preparing YouTubeText in $projectRoot"
$uv = Get-Command "uv" -ErrorAction SilentlyContinue
if ($null -ne $uv) {
    Write-Host "Synchronizing the runtime environment with uv..."
    Invoke-YouTubeTextChecked -Command $uv.Source -Arguments @(
        "sync", "--locked", "--no-dev", "--python", $python
    ) -FailureMessage "uv could not install YouTubeText."
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
        "-m", "pip", "install", "--editable", $projectRoot
    ) -FailureMessage "pip could not install YouTubeText."
}

Write-Host ""
Write-Host "YouTubeText is ready. Run it from the repository root:"
Write-Host "  Set-Location `"$projectRoot`""
Write-Host "  `"$venvCommand`" --help"
Write-Host "  `"$venvCommand`" doctor"
Write-Host "  `"$venvCommand`" `"https://www.youtube.com/watch?v=VIDEO_ID`""
