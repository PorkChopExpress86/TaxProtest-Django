[CmdletBinding()]
param(
    [switch]$NoRestart,
    [int]$GracefulTimeoutSeconds = 10
)

$ErrorActionPreference = "Stop"

# ChatGPT is the desktop shell that hosts Codex. The other process names cover
# Codex CLI and extension-host sessions started from the desktop app or VS Code.
$targetNames = @("ChatGPT", "codex", "codex-code-mode-host")
$processes = Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessName -in $targetNames }

if ($processes) {
    Write-Host "Requesting shutdown for $($processes.Count) ChatGPT/Codex process(es)..."
    foreach ($process in $processes) {
        if ($process.MainWindowHandle -ne 0) {
            [void]$process.CloseMainWindow()
        }
    }

    $deadline = (Get-Date).AddSeconds($GracefulTimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 500
        $remaining = $processes | Where-Object { -not $_.HasExited }
    } while ($remaining -and (Get-Date) -lt $deadline)

    foreach ($process in $remaining) {
        Write-Host "Force-stopping $($process.ProcessName) (PID $($process.Id))..."
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "No running ChatGPT or Codex processes found."
}

if (-not $NoRestart) {
    # The AppUserModelID is stable across Microsoft Store package updates.
    Start-Process -FilePath "explorer.exe" -ArgumentList "shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"
    Write-Host "ChatGPT started. Open Codex from the desktop app after it finishes launching."
}
