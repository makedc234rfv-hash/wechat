$ErrorActionPreference = 'Stop'
$baseDir = $PSScriptRoot
$manifest = Get-Content -LiteralPath (Join-Path $baseDir 'SHA256SUMS.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$badItems = @()
foreach ($item in $manifest.files) {
    $candidate = [IO.Path]::GetFullPath((Join-Path $baseDir $item.path))
    if (-not $candidate.StartsWith($baseDir + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Manifest path escapes package root'
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        $badItems += $item.path
        continue
    }
    if ((Get-Item -LiteralPath $candidate).Length -ne $item.size -or
        (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.sha256) {
        $badItems += $item.path
    }
}
if ($badItems.Count) {
    $badItems | ForEach-Object { Write-Output ('FAIL: ' + $_) }
    exit 1
}
Write-Output ('OK: ' + $manifest.files.Count + ' files verified')
exit 0
