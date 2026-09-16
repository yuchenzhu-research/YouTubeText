Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Fail-YouTubeText {
    param([Parameter(Mandatory = $true)][string]$Message)

    [Console]::Error.WriteLine("error: $Message")
    exit 1
}

function Assert-YouTubeTextWindowsX64 {
    $isWindows = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
    if (-not $isWindows) {
        Fail-YouTubeText "This script requires 64-bit Windows."
    }

    try {
        $architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    }
    catch {
        $architecture = $env:PROCESSOR_ARCHITEW6432
        if ([string]::IsNullOrWhiteSpace($architecture)) {
            $architecture = $env:PROCESSOR_ARCHITECTURE
        }
    }
    if ($architecture -ne "X64") {
        Fail-YouTubeText "YouTubeText requires 64-bit x86 Windows (detected: $architecture)."
    }
}

function Resolve-YouTubeTextPython {
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($env:YOUTUBETEXT_PYTHON)) {
        $candidates += [PSCustomObject]@{
            Command = $env:YOUTUBETEXT_PYTHON
            Prefix = @()
        }
    }
    else {
        $candidates += [PSCustomObject]@{ Command = "python"; Prefix = @() }
        foreach ($version in @("3.14", "3.13", "3.12", "3.11")) {
            $candidates += [PSCustomObject]@{
                Command = "py"
                Prefix = @("-$version")
            }
        }
    }

    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Command -ErrorAction SilentlyContinue
        if ($null -eq $command) {
            continue
        }
        $launcher = $command.Source
        if ([string]::IsNullOrWhiteSpace($launcher)) {
            $launcher = $command.Path
        }
        $prefix = @($candidate.Prefix)
        try {
            $probe = & $launcher @prefix -c (
                "import sys; ok=(3,11)<=sys.version_info<(3,15) and sys.maxsize>2**32; " +
                "sys.stdout.write(sys.executable) if ok else sys.exit(1)"
            ) 2>$null
        }
        catch {
            continue
        }
        if ($LASTEXITCODE -ne 0) {
            continue
        }
        $lines = @($probe)
        $executable = [string]$lines[-1]
        if (-not [string]::IsNullOrWhiteSpace($executable)) {
            return $executable.Trim()
        }
    }

    Fail-YouTubeText (
        "Python 3.11 through 3.14 was not found. Install 64-bit Python, " +
        "then reopen PowerShell."
    )
}

function Assert-YouTubeTextFFmpeg {
    if ($null -eq (Get-Command "ffmpeg" -ErrorAction SilentlyContinue)) {
        Fail-YouTubeText (
            "FFmpeg was not found on PATH. Install FFmpeg, add its bin directory " +
            "to PATH, then reopen PowerShell."
        )
    }
}

function Assert-YouTubeTextVenvPython {
    param([Parameter(Mandatory = $true)][string]$Executable)

    & $Executable -c (
        "import sys; ok=(3,11)<=sys.version_info<(3,15) and sys.maxsize>2**32; " +
        "raise SystemExit(0 if ok else 1)"
    )
    if ($LASTEXITCODE -ne 0) {
        Fail-YouTubeText (
            "The existing .venv does not use 64-bit Python 3.11 through 3.14; " +
            "recreate it."
        )
    }
}

function Invoke-YouTubeTextChecked {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        Fail-YouTubeText $FailureMessage
    }
}
