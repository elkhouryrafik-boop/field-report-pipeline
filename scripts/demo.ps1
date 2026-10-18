# Windows wrapper for the demo (no `make` needed).
#   .\scripts\demo.ps1                     offline demo, site-inspection profile
#   .\scripts\demo.ps1 -Profile damage-assessment
#   .\scripts\demo.ps1 -Real               real faster-whisper (tiny, CPU) instead of the sidecar transcripts
param(
    [ValidateSet("site-inspection", "damage-assessment", "event-log")]
    [string]$Profile = "site-inspection",
    [string]$Out = "out/demo",
    [switch]$Real
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$python = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
$demoArgs = @("-m", "field_report", "demo", "--profile", $Profile, "--out", $Out)
if (-not $Real) { $demoArgs += "--offline" }

& $python @demoArgs
exit $LASTEXITCODE
