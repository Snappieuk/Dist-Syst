<#
.SYNOPSIS
    A small asset client written in PowerShell, to show the Python servers can be used from
    another language. It only needs to send and receive JSON, in the servers' format.

.DESCRIPTION
    The wire format, which is all a client in any language has to match:
      1. Connect over TLS (the servers use a self-signed certificate).
      2. Send a 4-byte big-endian length, then that many bytes of UTF-8 JSON.
      3. Read a 4-byte big-endian length, then that many bytes of UTF-8 JSON back.
      4. The server then closes the connection, so each request uses a new connection.
    Every request carries "protocol_version": 1. After a LOGIN, the reply's "token" is sent
    with each later request.

.EXAMPLE
    .\asset_client.ps1 list -User admin
    .\asset_client.ps1 add -User admin -Description "Dell laptop" -Category "IT Equipment" -Location "Site A"
    .\asset_client.ps1 checkout -User admin -Id AST-0001 -Borrower "Sam Lee"
    .\asset_client.ps1 checkin -User admin -Id AST-0001
    .\asset_client.ps1 list -User admin -Server 192.168.1.20:65432 -Fingerprint AB:CD:...
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("list", "add", "checkout", "checkin")]
    [string]$Command,

    [string]$Server = "127.0.0.1:65432",
    [Parameter(Mandatory = $true)] [string]$User,
    [string]$Password,           # asked for (hidden) if left out
    [string]$Fingerprint,        # if given, the server's certificate must match it

    [string]$Id,                 # checkout, checkin
    [string]$Description, [string]$Category, [string]$Location,   # add
    [string]$Borrower,           # checkout
    [string]$Start = (Get-Date).ToString("yyyy-MM-dd"),
    [string]$Due = (Get-Date).AddDays(7).ToString("yyyy-MM-dd")
)

$ErrorActionPreference = "Stop"
$PROTOCOL_VERSION = 1
$MAX_MESSAGE_BYTES = 10 * 1024 * 1024

# Read exactly $Count bytes. A stream may give fewer than asked for, so keep reading.
function Read-Exactly($Stream, [int]$Count) {
    $buffer = New-Object byte[] $Count
    $got = 0
    while ($got -lt $Count) {
        $n = $Stream.Read($buffer, $got, $Count - $got)
        if ($n -le 0) { throw "The connection closed before the whole reply arrived." }
        $got += $n
    }
    return , $buffer
}

# Send one request to the server and return its reply as an object.
function Send-Request($Request) {
    $Request["protocol_version"] = $PROTOCOL_VERSION
    $hostName, $port = $Server -split ":"
    if (-not $port) { $port = 65432 }

    $tcp = New-Object System.Net.Sockets.TcpClient($hostName, [int]$port)
    try {
        # The certificate is self-signed, so the normal check is switched off here and the
        # certificate is compared with -Fingerprint below instead.
        $accept = [System.Net.Security.RemoteCertificateValidationCallback] { $true }
        $tls = New-Object System.Net.Security.SslStream($tcp.GetStream(), $false, $accept)
        $tls.AuthenticateAsClient($hostName)

        $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($tls.RemoteCertificate)
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $found = [BitConverter]::ToString($sha.ComputeHash($cert.RawData))
        $found = $found.Replace("-", ":")
        # This check comes BEFORE anything is sent, so a password never goes to the wrong server.
        if ($Fingerprint -and $found -ne $Fingerprint.ToUpper()) {
            throw "The server's certificate ($found) is not the one you trusted."
        }
        if (-not $Fingerprint -and -not $script:told) {
            Write-Host "Server certificate fingerprint: $found (pass it as -Fingerprint to check it next time)"
            $script:told = $true
        }

        # Send: 4-byte big-endian length, then the JSON.
        $json = $Request | ConvertTo-Json -Depth 10 -Compress
        $payload = [System.Text.Encoding]::UTF8.GetBytes($json)
        if ($payload.Length -gt $MAX_MESSAGE_BYTES) { throw "The request is too large." }
        $header = [BitConverter]::GetBytes([int]$payload.Length)
        [Array]::Reverse($header)   # BitConverter is little-endian here; the protocol is big-endian
        $tls.Write($header, 0, 4)
        $tls.Write($payload, 0, $payload.Length)
        $tls.Flush()

        # Receive: the same format back.
        $header = Read-Exactly $tls 4
        [Array]::Reverse($header)
        $length = [BitConverter]::ToInt32($header, 0)
        if ($length -lt 0 -or $length -gt $MAX_MESSAGE_BYTES) { throw "The reply is too large." }
        $body = Read-Exactly $tls $length
        return [System.Text.Encoding]::UTF8.GetString($body) | ConvertFrom-Json
    }
    finally {
        $tcp.Close()
    }
}

# A reply with status OK worked; anything else has a "detail" saying why.
function Assert-Ok($Reply) {
    if ($Reply.status -ne "OK") {
        throw "The server said $($Reply.status): $($Reply.detail)"
    }
}

# The asset with this ID, from a fresh LIST. Its "version" is needed for changes, so the
# server can refuse a change made from out-of-date data.
function Get-Asset([string]$AssetId) {
    $reply = Send-Request @{ action = "LIST"; token = $script:token }
    Assert-Ok $reply
    $asset = @($reply.assets) | Where-Object { $_.asset_id -eq $AssetId }
    if (-not $asset) { throw "There is no asset $AssetId." }
    return $asset
}

# ---- Log in, then do the command ----

if (-not $Password) {
    $secure = Read-Host "Password for $User" -AsSecureString
    $Password = (New-Object System.Net.NetworkCredential("", $secure)).Password
}
$login = Send-Request @{ action = "LOGIN"; username = $User; password = $Password }
Assert-Ok $login
$script:token = $login.token

switch ($Command) {
    "list" {
        $reply = Send-Request @{ action = "LIST"; token = $script:token }
        Assert-Ok $reply
        foreach ($a in @($reply.assets)) {
            $loan = @($a.loans) | Select-Object -Last 1
            $out = ""
            if ($loan -and -not $loan.returned) { $out = "on loan to $($loan.borrower) until $($loan.due)" }
            "{0,-10} {1,-30} {2,-12} {3}" -f $a.asset_id, $a.description, $a.status, $out
        }
        "$($reply.total) asset(s)"
    }
    "add" {
        if (-not ($Description -and $Category -and $Location)) {
            throw "add needs -Description, -Category and -Location."
        }
        # The server makes the asset ID itself. The request ID makes a retry safe: sending the
        # same request again returns the same asset rather than adding a second one.
        $reply = Send-Request @{
            action     = "REGISTER"
            request_id = [guid]::NewGuid().ToString("N")
            token      = $script:token
            asset      = @{ description = $Description; category = $Category; location = $Location; status = "In Use" }
        }
        Assert-Ok $reply
        "Added $($reply.stored.asset_id)"
    }
    "checkout" {
        if (-not ($Id -and $Borrower)) { throw "checkout needs -Id and -Borrower." }
        $asset = Get-Asset $Id
        $reply = Send-Request @{
            action = "CHECK_OUT"; token = $script:token; asset_id = $Id; borrower = $Borrower
            start = $Start; due = $Due; expected_version = $asset.version
        }
        Assert-Ok $reply
        "Checked out $Id to $Borrower until $Due"
    }
    "checkin" {
        if (-not $Id) { throw "checkin needs -Id." }
        $asset = Get-Asset $Id
        $reply = Send-Request @{
            action = "CHECK_IN"; token = $script:token; asset_id = $Id; expected_version = $asset.version
        }
        Assert-Ok $reply
        "Checked in $Id"
    }
}
