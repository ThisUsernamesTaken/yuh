nssm status BTCBiasEngine
Write-Host ""
Write-Host "=== Python Processes ===" -ForegroundColor Cyan
Get-Process -Name "python*" -ErrorAction SilentlyContinue | ForEach-Object {
    $cpu = if ($_.CPU) { [math]::Round($_.CPU, 1) } else { 0 }
    $mem = [math]::Round($_.WorkingSet64 / 1MB, 1)
    $threads = $_.Threads.Count
    $handles = $_.HandleCount
    Write-Host "  PID $($_.Id)  CPU=${cpu}s  MEM=${mem}MB  Threads=$threads  Handles=$handles"
}

Write-Host ""
Write-Host "=== System Stats ===" -ForegroundColor Cyan
$cs = Get-CimInstance Win32_ComputerSystem
$os = Get-CimInstance Win32_OperatingSystem
$totalMem = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)
$freeMem = [math]::Round($os.FreePhysicalMemory / 1MB / 1024, 1)
$usedPct = [math]::Round(100 - ($os.FreePhysicalMemory / $os.TotalVisibleMemorySize * 100), 1)
Write-Host "  Total RAM: ${totalMem}GB"
Write-Host "  Free RAM:  ${freeMem}GB"
Write-Host "  RAM used:  ${usedPct}%"

$cpu = (Get-Counter '\Processor(_Total)\% Processor Time' -SampleInterval 1 -MaxSamples 1).CounterSamples[0].CookedValue
Write-Host "  CPU usage: $([math]::Round($cpu, 1))%"
