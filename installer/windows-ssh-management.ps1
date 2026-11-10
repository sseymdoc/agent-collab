# PowerShell 5.1+/7, run elevated. Optional management entry for a Windows worker:
# Microsoft OpenSSH Server limited to one user, public-key authentication and the given
# source addresses. It carries install/start/diagnose commands only; task lifecycle stays
# with the worker's outbound HTTPS connection. DefaultShell is left unchanged.
#
#   .\windows-ssh-management.ps1 -AllowFrom 192.0.2.10 -PublicKey 'ssh-ed25519 AAAA... name'
#   .\windows-ssh-management.ps1 -Remove
#
# An administrator account logs in over SSH with a full (elevated) token: whoever holds the
# private key controls this machine. Keep the key on the management host only.
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string[]]$AllowFrom,
    [string]$PublicKey,
    [string]$User = $env:USERNAME,
    [ValidateRange(1, 65535)][int]$Port = 22,
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$ruleName = 'agent-collab-ssh-management'
$sshDir = Join-Path $env:ProgramData 'ssh'
$config = Join-Path $sshDir 'sshd_config'
$sshd = Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe'
$begin = '# BEGIN agent-collab management entry'
$end = '# END agent-collab management entry'
$blockPattern = '(?s)' + [regex]::Escape($begin) + '.*?' + [regex]::Escape($end) + '\r?\n?'

function Read-Config { if (Test-Path -LiteralPath $config) { [IO.File]::ReadAllText($config) } else { '' } }
function Write-Config([string]$text) { [IO.File]::WriteAllText($config, $text, [Text.Encoding]::ASCII) }

if ($Remove) {
    Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    if (Get-Service sshd -ErrorAction SilentlyContinue) {
        Stop-Service sshd -Force
        Set-Service sshd -StartupType Disabled
    }
    if (Test-Path -LiteralPath $config) { Write-Config ([regex]::Replace((Read-Config), $blockPattern, '')) }
    'Removed: firewall rule deleted, sshd stopped and disabled, managed sshd_config block removed.'
    'Kept: OpenSSH capability, host keys and authorized keys.'
    return
}

if (-not $AllowFrom) { throw '-AllowFrom requires at least one source address' }
foreach ($address in $AllowFrom) { [void][Net.IPAddress]::Parse($address) }
if ($User -notmatch '^[A-Za-z0-9._-]+$') { throw '-User must be a local account name' }
$keyPattern = '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) ([A-Za-z0-9+/=]+)( [\x20-\x7E]*)?$'
if ($PublicKey -notmatch $keyPattern) { throw '-PublicKey must be a single OpenSSH public key line' }
$keyBlob = $Matches[3]

# Never take over a port that something else already listens on.
# Bound sockets have remote port 0. Do not filter by -State: some Windows builds report listeners
# with other states. Loopback-only ones (for example WSL's localhost relay) coexist with sshd.
$listeners = @(Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object { $_.RemotePort -eq 0 -and $_.LocalAddress -notin '127.0.0.1', '::1' })
foreach ($listener in $listeners) {
    $owner = (Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue).ProcessName
    if ($owner -ne 'sshd') { throw "port $Port is already in use by '$owner' (pid $($listener.OwningProcess)); pass another -Port" }
}

# The managed block only wins where sshd takes the first value of a keyword. Refuse, before any
# change, an existing configuration with directives that add to the block or override it:
# AllowUsers and Port accumulate, Include pulls in files this script does not read, a key command
# or user CA authorizes further keys, and a Match block replaces the block's settings for the
# connections it matches. The stock sshd_config has none of these.
$preserved = [regex]::Replace((Read-Config), $blockPattern, '')
$conflicts = @([regex]::Matches($preserved, '(?im)^[ \t]*(AllowUsers|Port|Include|AuthorizedKeysCommand|TrustedUserCAKeys)\b') |
    ForEach-Object { $_.Groups[1].Value })
$overridable = 'PubkeyAuthentication|PasswordAuthentication|KbdInteractiveAuthentication|ChallengeResponseAuthentication|' +
    'AuthenticationMethods|PermitEmptyPasswords|AllowTcpForwarding|AllowAgentForwarding|X11Forwarding|PermitTunnel|MaxAuthTries'
$matchScope = [regex]::Match($preserved, '(?ims)^[ \t]*Match\b.*').Value
$conflicts += @([regex]::Matches($matchScope, "(?im)^[ \t]*($overridable)\b") | ForEach-Object { 'Match ' + $_.Groups[1].Value })
if ($conflicts) {
    throw "sshd_config has directives outside the managed block that add to or override it ($(($conflicts | Select-Object -Unique) -join ', ')); remove or merge them by hand, then rerun"
}

$capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
if (-not $capability) { throw 'OpenSSH.Server capability is not available on this Windows edition' }
if ($capability.State -ne 'Installed') { Add-WindowsCapability -Online -Name $capability.Name | Out-Null }
# The capability ships a rule that admits every source address: disable it before sshd can
# start with its default settings.
Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue | Disable-NetFirewallRule
if (-not (Test-Path -LiteralPath $config)) {
    # First start creates the default configuration and host keys.
    Start-Service sshd
    Stop-Service sshd -Force
}

$previous = Read-Config
$backup = $config + '.before-agent-collab'
if (-not (Test-Path -LiteralPath $backup)) { Copy-Item -LiteralPath $config -Destination $backup }
$allowUsers = ($AllowFrom | ForEach-Object { $User.ToLower() + '@' + $_ }) -join ' '
$block = @($begin, "Port $Port", 'PubkeyAuthentication yes', 'PasswordAuthentication no',
    'KbdInteractiveAuthentication no', 'AuthenticationMethods publickey', 'PermitEmptyPasswords no',
    "AllowUsers $allowUsers", 'AllowTcpForwarding no', 'AllowAgentForwarding no', 'X11Forwarding no',
    'PermitTunnel no', 'MaxAuthTries 3', 'LoginGraceTime 30', $end) -join "`r`n"
# sshd uses the first value of each of these keywords (the accumulating ones were rejected above
# when already present): the managed block goes before the defaults.
Write-Config ($block + "`r`n" + [regex]::Replace($previous, $blockPattern, ''))
& $sshd -t -f $config
if ($LASTEXITCODE -ne 0) {
    Write-Config $previous
    throw 'sshd rejected the configuration; the previous sshd_config was restored'
}

try {
    $administrator = [bool](Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop |
        Where-Object { ($_.Name -split '\\')[-1] -ieq $User })
} catch {
    $administrator = $User -ieq $env:USERNAME   # this script runs elevated
}
if ($administrator) {
    $keys = Join-Path $sshDir 'administrators_authorized_keys'
} else {
    $keys = Join-Path (Join-Path (Split-Path $env:USERPROFILE) $User) '.ssh\authorized_keys'
    [void](New-Item -ItemType Directory -Force -Path (Split-Path $keys))
}
$line = 'from="' + ($AllowFrom -join ',') + '",restrict,pty ' + $PublicKey
$existing = @(if (Test-Path -LiteralPath $keys) { Get-Content -LiteralPath $keys })
$kept = @($existing | Where-Object { $_ -and ($_ -notlike "*$keyBlob*") })
[IO.File]::WriteAllLines($keys, [string[]]($kept + $line), [Text.Encoding]::ASCII)
# sshd ignores an authorized-keys file that other accounts can write: administrators' file may list
# only Administrators and SYSTEM, a user's own file the user as well. /reset drops explicit entries
# left on an existing file (/inheritance:r alone would keep them); inheritance could add others.
$grant = @('*S-1-5-32-544:F', '*S-1-5-18:F')
if (-not $administrator) { $grant += "${User}:F" }
& icacls.exe $keys /reset | Out-Null
if ($LASTEXITCODE -eq 0) { & icacls.exe $keys /inheritance:r /grant $grant | Out-Null }
if ($LASTEXITCODE -ne 0) { throw "cannot restrict access to $keys" }

Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -Name $ruleName -DisplayName 'agent-collab SSH management (listed sources only)' `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress $AllowFrom `
    -Profile Private, Domain | Out-Null

Set-Service sshd -StartupType Automatic
Restart-Service sshd -Force
$listening = @(Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object { $_.RemotePort -eq 0 -and (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName -eq 'sshd' })
if (-not $listening) { throw "sshd is not listening on port $Port; see the OpenSSH/Operational event log" }
$hostKey = & (Join-Path $env:SystemRoot 'System32\OpenSSH\ssh-keygen.exe') -lf (Join-Path $sshDir 'ssh_host_ed25519_key.pub')
[ordered]@{ user = $User; port = $Port; allow_from = $AllowFrom; authorized_keys = $keys
    administrator_token = $administrator; host_key = "$hostKey"; default_shell = 'unchanged' } | ConvertTo-Json
