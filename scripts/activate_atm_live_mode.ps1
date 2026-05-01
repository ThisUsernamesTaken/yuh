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
$text = [regex]::Replace($text, '(?m)^SR_FADE_ENABLED\s*=.*$', 'SR_FADE_ENABLED = False  # ATM_ONLY live-test active. Rollback: scripts\rollback_atm_live_mode.ps1 -Restart')
$text = [regex]::Replace($text, '(?m)^LIVE_STRATEGY_MODE\s*=.*$', 'LIVE_STRATEGY_MODE = "ATM_ONLY"              # "LEGACY" | "ATM_ONLY" | "PAPER_ONLY"')
$text = [regex]::Replace($text, '(?m)^ATM_LIVE_ENABLED\s*=.*$', 'ATM_LIVE_ENABLED = True                        # live fill-test ON; hard-capped below')
Set-Content -Path $config -Value $text -Encoding UTF8

Write-Host "ATM live-test config applied:"
Write-Host "  SR_FADE_ENABLED=False"
Write-Host "  LIVE_STRATEGY_MODE=ATM_ONLY"
Write-Host "  ATM_LIVE_ENABLED=True"
Write-Host "  Caps remain controlled by ATM_LIVE_MAX_CONTRACTS / ATM_LIVE_MAX_NOTIONAL_CENTS"

if ($Restart) {
    nssm restart BTCBiasEngine
    Start-Sleep -Seconds 5
    nssm status BTCBiasEngine
} else {
    Write-Host "Restart required: nssm restart BTCBiasEngine"
}
