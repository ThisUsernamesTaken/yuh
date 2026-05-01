param(
    [switch]$Restart
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$config = Join-Path $root "user_config.py"

if (-not (Test-Path $config)) {
    throw "user_config.py not found at $config"
}

$text = Get-Content -Raw -Path $config
$text = [regex]::Replace($text, '(?m)^SR_FADE_ENABLED\s*=.*$', 'SR_FADE_ENABLED = True')
$text = [regex]::Replace($text, '(?m)^LIVE_STRATEGY_MODE\s*=.*$', 'LIVE_STRATEGY_MODE = "LEGACY"                # "LEGACY" | "ATM_ONLY" | "PAPER_ONLY"')
$text = [regex]::Replace($text, '(?m)^ATM_LIVE_ENABLED\s*=.*$', 'ATM_LIVE_ENABLED = False                       # live ATM disabled by rollback')
Set-Content -Path $config -Value $text -Encoding UTF8

Write-Host "Rollback config applied:"
Write-Host "  SR_FADE_ENABLED=True"
Write-Host "  LIVE_STRATEGY_MODE=LEGACY"
Write-Host "  ATM_LIVE_ENABLED=False"

if ($Restart) {
    nssm restart BTCBiasEngine
    Start-Sleep -Seconds 5
    nssm status BTCBiasEngine
} else {
    Write-Host "Restart required: nssm restart BTCBiasEngine"
}
