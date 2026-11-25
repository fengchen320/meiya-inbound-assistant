param([string]$Src, [string]$Dst)
$ErrorActionPreference = 'Stop'
$xl = New-Object -ComObject Excel.Application
$xl.Visible = $false
$xl.DisplayAlerts = $false
try {
    $wb = $xl.Workbooks.Open($Src, 0, $true)
    $wb.SaveAs($Dst, 51)
    $wb.Close($false)
    Write-Output ("OK sheets=" + $wb.Worksheets.Count)
} finally {
    $xl.Quit()
    [System.Runtime.Interopservices.Marshal]::ReleaseComObject($xl) | Out-Null
}
