$ports = @(8000, 8001, 8080)

# Try killing by PID first
$conns = Get-NetTCPConnection -State Listen
foreach ($c in $conns) {
    if ($ports -contains $c.LocalPort) {
        Write-Host "Port $($c.LocalPort) owned by PID $($c.OwningProcess)"
        # Kill entire process tree
        $null = & taskkill /PID $c.OwningProcess /T /F 2>&1
        # Also try killing parent
        $proc = Get-WmiObject Win32_Process -Filter "ProcessId=$($c.OwningProcess)"
        if ($proc -and $proc.ParentProcessId -gt 4) {
            Write-Host "  -> Killing parent PID $($proc.ParentProcessId)"
            $null = & taskkill /PID $proc.ParentProcessId /T /F 2>&1
        }
    }
}

Start-Sleep 2

# Final check
$remaining = @()
$conns2 = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue
foreach ($c in $conns2) {
    if ($ports -contains $c.LocalPort) { $remaining += $c.LocalPort }
}

if ($remaining.Count -gt 0) {
    Write-Host "Still running on: $remaining"
} else {
    Write-Host "All servers stopped."
}
