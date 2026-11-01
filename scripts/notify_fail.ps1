# 周更失败通知（toast 气泡；后台计划任务用）。用法：notify_fail.ps1 "消息"
param([string]$Msg = "基金周更失败，请检查 data\pipeline_cron.log")
$ErrorActionPreference = 'SilentlyContinue'
try {
  [void][System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms')
  $nid = New-Object System.Windows.Forms.NotifyIcon
  $nid.Icon = [System.Drawing.SystemIcons]::Warning
  $nid.Visible = $true
  $nid.ShowBalloonTip(8000, '班马日记本 · 周更', $Msg, [System.Windows.Forms.ToolTipIcon]::Warning)
  Start-Sleep -Seconds 9
  $nid.Dispose()
} catch { }
