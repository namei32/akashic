param(
    [Parameter(Mandatory = $true)][string]$Queue,
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
if (-not [Environment]::UserInteractive) {
    throw 'Run the notification reader in the signed-in Windows desktop session.'
}

$icon = [Windows.Forms.NotifyIcon]::new()
$icon.Icon = [Drawing.SystemIcons]::Application
$icon.Text = 'Akashic WSL'
$icon.Visible = $true
$algorithm = [Security.Cryptography.SHA256]::Create()
$queueKey = [BitConverter]::ToString($algorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($Queue.ToLowerInvariant()))).Replace('-', '')
$algorithm.Dispose()
$mutex = [Threading.Mutex]::new($false, "Local\AkashicRelease-$queueKey")
$locked = $false
try {
    try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { return }
    do {
        $items = Get-ChildItem -LiteralPath $Queue -Filter '*.json' -File -ErrorAction SilentlyContinue
        foreach ($item in $items) {
            if ($item.BaseName -notmatch '^[0-9a-f]{64}$') { continue }
            $receipt = Join-Path $Queue ($item.BaseName + '.ack')
            if (Test-Path -LiteralPath $receipt) { continue }
            $notice = Get-Content -LiteralPath $item.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($notice.id -cne $item.BaseName) { continue }
            $icon.BalloonTipTitle = [string]$notice.title
            $text = [string]$notice.text
            $icon.BalloonTipText = $text.Substring(0, [Math]::Min($text.Length, 250))
            $icon.BalloonTipIcon = [Windows.Forms.ToolTipIcon]::Info
            $shown = [hashtable]::Synchronized(@{ Value = $false })
            $handler = [EventHandler]({ $shown.Value = $true }.GetNewClosure())
            $icon.add_BalloonTipShown($handler)
            try {
                $icon.ShowBalloonTip(7000)
                $deadline = [DateTime]::UtcNow.AddSeconds(10)
                while (-not $shown.Value -and [DateTime]::UtcNow -lt $deadline) {
                    [Windows.Forms.Application]::DoEvents()
                    Start-Sleep -Milliseconds 100
                }
                if ($shown.Value) {
                    $temporary = "$receipt.$PID.tmp"
                    [IO.File]::WriteAllText($temporary, [DateTime]::UtcNow.ToString('o'))
                    [IO.File]::Move($temporary, $receipt)
                    if ($Once) { Start-Sleep -Seconds 5 }
                }
            } finally {
                $icon.remove_BalloonTipShown($handler)
            }
        }
        if (-not $Once) { Start-Sleep -Seconds 5 }
    } while (-not $Once)
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
    $icon.Dispose()
}
