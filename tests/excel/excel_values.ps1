# 開発時の確認用：Excel で xlsx を開き、全再計算した後のセルの値を JSON で書き出す。
# 製品には含めない。合成データの出力だけに使う。
#   powershell -NoProfile -ExecutionPolicy Bypass -File excel_values.ps1 -Out result.json file1.xlsx file2.xlsx ...
param(
    [Parameter(Mandatory = $true)][string]$Out,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Paths
)
$ErrorActionPreference = "Stop"
$xl = New-Object -ComObject Excel.Application
$xl.Visible = $false
$xl.DisplayAlerts = $false
$xl.AskToUpdateLinks = $false
$xl.EnableEvents = $false
$results = @()
try {
    foreach ($path in $Paths) {
        $entry = [ordered]@{ path = $path; opened = $false; caption = ""; sheets = @() }
        try {
            $wb = $xl.Workbooks.Open($path, 0, $true)
            $entry.opened = $true
            $entry.caption = $xl.Caption
            $xl.CalculateFull()
            foreach ($ws in $wb.Worksheets) {
                $used = $ws.UsedRange
                $values = $used.Value2
                $cells = New-Object System.Collections.Generic.List[object]
                if ($values -is [System.Array]) {
                    $rows = $values.GetLength(0)
                    $cols = $values.GetLength(1)
                    for ($r = 1; $r -le $rows; $r++) {
                        for ($c = 1; $c -le $cols; $c++) {
                            $v = $values[$r, $c]
                            if ($null -ne $v) {
                                $cells.Add(@(($used.Row + $r - 2), ($used.Column + $c - 2), [string]$v))
                            }
                        }
                    }
                } elseif ($null -ne $values) {
                    $cells.Add(@(($used.Row - 1), ($used.Column - 1), [string]$values))
                }
                $entry.sheets += , [ordered]@{ name = $ws.Name; cells = $cells.ToArray() }
            }
            $wb.Close($false)
        } catch {
            $entry.error = $_.Exception.HResult
        }
        $results += , $entry
    }
} finally {
    $xl.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($xl) | Out-Null
}
$json = ConvertTo-Json -InputObject $results -Depth 8 -Compress
[System.IO.File]::WriteAllText($Out, $json, (New-Object System.Text.UTF8Encoding($false)))
