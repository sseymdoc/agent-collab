# PowerShell 5.1+/7. All installation policy is in agent_collab/installer.py.
$ErrorActionPreference = 'Stop'
$forward = @($args)
$chosen = $null
$prefixArgs = @()
for ($i = 0; $i -lt $forward.Count; $i++) {
    if ($forward[$i] -eq '--python') {
        $i++
        if ($i -ge $forward.Count) { throw '--python requires an executable' }
        $chosen = $forward[$i]
    } elseif ($forward[$i] -like '--python=*') {
        $chosen = $forward[$i].Substring(9)
    }
}
if (-not $chosen) {
    if (Get-Command python -ErrorAction SilentlyContinue) { $chosen = 'python' }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { $chosen = 'py'; $prefixArgs = @('-3') }
    else { throw 'Install Python >= 3.9 locally or pass --python. Nothing is downloaded.' }
}
$env:PYTHONDONTWRITEBYTECODE = '1'
& $chosen @prefixArgs -B (Join-Path $PSScriptRoot 'entry.py') @forward
exit $LASTEXITCODE
