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
$stockRule = 'OpenSSH-Server-In-TCP'
$sshDir = Join-Path $env:ProgramData 'ssh'
$config = Join-Path $sshDir 'sshd_config'
$sshd = Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe'
$begin = '# BEGIN agent-collab management entry'
$end = '# END agent-collab management entry'
$blockPattern = '(?s)' + [regex]::Escape($begin) + '.*?' + [regex]::Escape($end) + '\r?\n?'

function Read-Config { if (Test-Path -LiteralPath $config) { [IO.File]::ReadAllText($config) } else { '' } }
function Write-Config([string]$text) { [IO.File]::WriteAllText($config, $text, [Text.Encoding]::ASCII) }
# Accounts outside $allowed that own $path or may change it.
function Get-OtherWriters([string]$path, [string[]]$allowed) {
    $acl = Get-Acl -LiteralPath $path
    # 0x50000000 is generic write and generic all, as stored on entries that only children inherit.
    $write = [int][Security.AccessControl.FileSystemRights]'Write, Delete, DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership' -bor 0x50000000
    $others = @($acl.Access | Where-Object {
            $_.AccessControlType -eq 'Allow' -and ([int]$_.FileSystemRights -band $write) -and
            $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -notin $allowed } |
        ForEach-Object { "$($_.IdentityReference)" })
    # The owner can rewrite the permissions whatever the entries say.
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $allowed) { $others += "$($acl.Owner) (owner)" }
    $others | Select-Object -Unique
}

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

# Everything down to the capability installation only reads: a refusal leaves the machine as it was.

# Never take over a port that something else already listens on.
# Bound sockets have remote port 0. Do not filter by -State: some Windows builds report listeners
# with other states. Loopback-only ones (for example WSL's localhost relay) coexist with sshd.
$listeners = @(Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object { $_.RemotePort -eq 0 -and $_.LocalAddress -notin '127.0.0.1', '::1' })
foreach ($listener in $listeners) {
    $owner = (Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue).ProcessName
    if ($owner -ne 'sshd') { throw "port $Port is already in use by '$owner' (pid $($listener.OwningProcess)); pass another -Port" }
}

# The managed block only wins where sshd takes the first value of a keyword. Refuse an existing
# configuration with directives that add to the block or override it: AllowUsers and Port
# accumulate, Include pulls in files this script does not read, a key command or user CA authorizes
# further keys, ForceCommand replaces the management command, and a Match block replaces the
# block's settings for the connections it matches. The stock sshd_config has none of these.
$preserved = [regex]::Replace((Read-Config), $blockPattern, '')
$conflicts = @([regex]::Matches($preserved, '(?im)^[ \t]*(AllowUsers|Port|Include|AuthorizedKeysCommand|TrustedUserCAKeys|ForceCommand)\b') |
    ForEach-Object { $_.Groups[1].Value })
$overridable = 'PubkeyAuthentication|PasswordAuthentication|KbdInteractiveAuthentication|ChallengeResponseAuthentication|' +
    'AuthenticationMethods|PermitEmptyPasswords|AllowTcpForwarding|AllowAgentForwarding|X11Forwarding|PermitTunnel|MaxAuthTries'
$matchScope = [regex]::Match($preserved, '(?ims)^[ \t]*Match\b.*').Value
$conflicts += @([regex]::Matches($matchScope, "(?im)^[ \t]*($overridable)\b") | ForEach-Object { 'Match ' + $_.Groups[1].Value })
# The key goes where the stock configuration reads it: the account's own file, set outside any
# Match block, or the shared file through the stock Match block for administrators. An
# AuthorizedKeysFile anywhere else would make sshd ignore that file or honour keys this script
# never saw.
$stockMatch = '(?im)^[ \t]*Match[ \t]+Group[ \t]+administrators[ \t]*\r?\n[ \t]*AuthorizedKeysFile[ \t]+__PROGRAMDATA__/ssh/administrators_authorized_keys[ \t]*\r?$'
$keysFile = '(?im)^[ \t]*AuthorizedKeysFile[ \t=]+(.*?)[ \t]*\r?$'
$conflicts += @([regex]::Matches($preserved.Substring(0, $preserved.Length - $matchScope.Length), $keysFile) |
    Where-Object { $_.Groups[1].Value -ne '.ssh/authorized_keys' } | ForEach-Object { 'AuthorizedKeysFile ' + $_.Groups[1].Value })
$conflicts += @([regex]::Matches([regex]::Replace($matchScope, $stockMatch, ''), $keysFile) |
    ForEach-Object { 'Match AuthorizedKeysFile ' + $_.Groups[1].Value })
if ($conflicts) {
    throw "sshd_config has directives outside the managed block that add to or override it ($(($conflicts | Select-Object -Unique) -join ', ')); remove or merge them by hand, then rerun"
}

# The key file is prepared next to sshd_config. That protects it only while nobody but
# Administrators and SYSTEM can write to that directory, which is how OpenSSH creates it.
if (Test-Path -LiteralPath $sshDir) {
    $others = @(Get-OtherWriters $sshDir 'S-1-5-32-544', 'S-1-5-18')
    if ($others) { throw "$sshDir can be written by $($others -join ', '); restrict it to Administrators and SYSTEM, then rerun" }
}

try {
    $administrator = [bool](Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop |
        Where-Object { ($_.Name -split '\\')[-1] -ieq $User })
} catch {
    $administrator = $User -ieq $env:USERNAME   # this script runs elevated
}
# Administrators' keys live in the shared file only while the stock Match block is in place (a
# configuration that does not exist yet is created with it). Without it sshd reads the account's
# own file, as it does for every other user.
$shared = $administrator -and (-not (Test-Path -LiteralPath $config) -or $preserved -match $stockMatch)
$writers = @('S-1-5-32-544', 'S-1-5-18')
if ($shared) {
    $keys = Join-Path $sshDir 'administrators_authorized_keys'
} else {
    $keys = Join-Path (Join-Path (Split-Path $env:USERPROFILE) $User) '.ssh\authorized_keys'
    $writers += (New-Object Security.Principal.NTAccount $User).Translate([Security.Principal.SecurityIdentifier]).Value
}
# Existing keys are kept, so they must come from a file nobody else could have written. sshd
# ignores a file that others own or can write; tightening it would bring every line in it to life.
$existing = @()
$previousKeys = $null
if (Test-Path -LiteralPath $keys) {
    $others = @(Get-OtherWriters $keys $writers)
    if ($others) {
        throw "$keys can be written by $($others -join ', '); review its contents, then restrict or remove the file and rerun"
    }
    $previousKeys = [IO.File]::ReadAllBytes($keys)
    $existing = @(Get-Content -LiteralPath $keys)
}

$capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
if (-not $capability) { throw 'OpenSSH.Server capability is not available on this Windows edition' }
if ($capability.State -ne 'Installed') { Add-WindowsCapability -Online -Name $capability.Name | Out-Null }
if (-not (Test-Path -LiteralPath $config)) {
    # The capability ships a rule that admits every source address: disable it before sshd first
    # starts with its default settings. That start creates the default configuration and host keys.
    Get-NetFirewallRule -Name $stockRule -ErrorAction SilentlyContinue | Disable-NetFirewallRule
    Start-Service sshd
    Stop-Service sshd -Force
}

$service = Get-Service sshd
$wasRunning = $service.Status -eq 'Running'
$startType = $service.StartType
$previous = Read-Config
$backup = $config + '.before-agent-collab'
if (-not (Test-Path -LiteralPath $backup)) { Copy-Item -LiteralPath $config -Destination $backup }
$allowUsers = ($AllowFrom | ForEach-Object { $User.ToLower() + '@' + $_ }) -join ' '
$block = @($begin, "Port $Port", 'PubkeyAuthentication yes', 'PasswordAuthentication no',
    'KbdInteractiveAuthentication no', 'AuthenticationMethods publickey', 'PermitEmptyPasswords no',
    "AllowUsers $allowUsers", 'AllowTcpForwarding no', 'AllowAgentForwarding no', 'X11Forwarding no',
    'PermitTunnel no', 'MaxAuthTries 3', 'LoginGraceTime 30', $end) -join "`r`n"
$stage = $null
$movedKeys = $false
$createdRule = $false
$oldRule = $null
# sshd uses the first value of each of these keywords (the accumulating ones were rejected above
# when already present): the managed block goes before the defaults.
Write-Config ($block + "`r`n" + [regex]::Replace($previous, $blockPattern, ''))
try {
    & $sshd -t -f $config
    if ($LASTEXITCODE -ne 0) { throw 'sshd rejected the configuration; the previous sshd_config was restored' }

    # The key file is prepared next to sshd_config, where only Administrators and SYSTEM can write,
    # given its final permissions there and then moved into place: nobody else gets to write into
    # it first. sshd ignores an authorized-keys file that other accounts can write. The shared
    # administrators' file may list only Administrators and SYSTEM; a user's own file belongs to
    # the user.
    $line = 'from="' + ($AllowFrom -join ',') + '",restrict,pty ' + $PublicKey
    $kept = @($existing | Where-Object { $_ -and ($_ -notlike "*$keyBlob*") })
    $stage = Join-Path $sshDir ('agent-collab-keys-' + [guid]::NewGuid().ToString('N'))
    [IO.File]::WriteAllLines($stage, [string[]]($kept + $line), [Text.Encoding]::ASCII)
    $grant = @('*S-1-5-32-544:F', '*S-1-5-18:F')
    if (-not $shared) { $grant += "${User}:F" }
    & icacls.exe $stage /inheritance:r /grant $grant | Out-Null
    if ($LASTEXITCODE -eq 0 -and -not $shared) { & icacls.exe $stage /setowner $User | Out-Null }
    if ($LASTEXITCODE -ne 0) { throw "cannot set the permissions of the authorized keys for $User" }
    [void](New-Item -ItemType Directory -Force -Path (Split-Path $keys))
    Move-Item -LiteralPath $stage -Destination $keys -Force
    $movedKeys = $true

    # An existing rule is updated in place: if that fails, the old rule is still there.
    $rule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    if ($rule) {
        $oldRule = @{ LocalPort = ($rule | Get-NetFirewallPortFilter).LocalPort
            RemoteAddress = ($rule | Get-NetFirewallAddressFilter).RemoteAddress; Profile = $rule.Profile }
        $rule | Set-NetFirewallRule -LocalPort $Port -RemoteAddress $AllowFrom -Profile Private, Domain
    } else {
        New-NetFirewallRule -Name $ruleName -DisplayName 'agent-collab SSH management (listed sources only)' `
            -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress $AllowFrom `
            -Profile Private, Domain | Out-Null
        $createdRule = $true
    }

    Set-Service sshd -StartupType Automatic
    Restart-Service sshd -Force
    # The service reports Running before sshd has bound its socket: wait up to ten seconds for it.
    $listening = @()
    for ($attempt = 0; $attempt -lt 20 -and -not $listening; $attempt++) {
        if ($attempt) { Start-Sleep -Milliseconds 500 }
        $listening = @(Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
            Where-Object { $_.RemotePort -eq 0 -and (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName -eq 'sshd' })
    }
    if (-not $listening) { throw "sshd is not listening on port $Port; see the OpenSSH/Operational event log" }
    # sshd now runs with the managed block, so nothing it should admit depends on the any-source rule.
    Get-NetFirewallRule -Name $stockRule -ErrorAction SilentlyContinue | Disable-NetFirewallRule
} catch {
    # An entry that worked before this run keeps working: its configuration, authorized keys,
    # firewall rule and service state are put back as they were.
    Write-Config $previous
    if ($stage) { Remove-Item -LiteralPath $stage -Force -ErrorAction SilentlyContinue }
    if ($movedKeys) {
        try {
            if ($null -ne $previousKeys) { [IO.File]::WriteAllBytes($keys, $previousKeys) } else { Remove-Item -LiteralPath $keys -Force }
        } catch { Write-Warning "could not restore ${keys}: $_" }
    }
    if ($createdRule) {
        Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    } elseif ($oldRule) {
        Set-NetFirewallRule -Name $ruleName @oldRule -ErrorAction SilentlyContinue
    }
    Set-Service sshd -StartupType $startType -ErrorAction SilentlyContinue
    if ($wasRunning) { Restart-Service sshd -Force -ErrorAction SilentlyContinue } else { Stop-Service sshd -Force -ErrorAction SilentlyContinue }
    throw
}

$hostKey = & (Join-Path $env:SystemRoot 'System32\OpenSSH\ssh-keygen.exe') -lf (Join-Path $sshDir 'ssh_host_ed25519_key.pub')
[ordered]@{ user = $User; port = $Port; allow_from = $AllowFrom; authorized_keys = $keys
    administrator_token = $administrator; host_key = "$hostKey"; default_shell = 'unchanged' } | ConvertTo-Json
