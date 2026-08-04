<#
memory.psm1 - PowerShell module for the memory_backend API.

WHY THIS EXISTS
    The usual advice is "set $U before you start". That fails badly, because
    PowerShell expands an undefined variable to an empty string instead of
    erroring. So "$U/wiki" silently becomes "/wiki", which has no hostname,
    and you get:

        Invoke-RestMethod : Invalid URI: The hostname could not be parsed.

    an error that says nothing about the actual cause. Variables are also
    per-window, so it recurs every time you open a terminal.

    This module removes the variable. You call:

        mem GET /wiki/_stats

    and the base URL is resolved internally. There is nothing to forget and
    nothing to expand to empty. If the server is unreachable you are told
    that, rather than being handed a URI parse error.

    It also unwraps HTTP error bodies. Invoke-RestMethod throws on 4xx/5xx
    and buries the response body, so the API's own message - the thing that
    tells you WHICH entity file is corrupt, or which field failed validation
    - never reaches you. This surfaces it.

INSTALL
    Import-Module .\memory.psm1

    To load it in every new window, append that line to your profile:
        Add-Content $PROFILE "Import-Module '$PWD\memory.psm1'"
        # if the file does not exist yet:
        New-Item -ItemType File -Path $PROFILE -Force

USAGE
    Connect-Memory                      # defaults, pings the server
    Connect-Memory -UserId demo
    Connect-Memory -BaseUrl http://127.0.0.1:8001 -UserId ops

    mem GET  /wiki/_stats
    mem GET  /wiki
    mem POST /wiki/_reconcile
    mem PUT  /wiki @{ type='person'; title='Alice Chen' }
    mem POST /wiki/traverse @{ entry_wiki_ids=@('person/alice-chen'); max_depth=2 }
#>

Set-StrictMode -Version Latest

# Module-scoped state. Not $global:, so nothing here can be clobbered by
# your own variables, and nothing silently expands to empty.
$script:BaseUrl = $null
$script:UserId  = $null

function Connect-Memory {
    <#
    .SYNOPSIS
        Point the module at a server and user, and confirm it is reachable.
    .DESCRIPTION
        Defaults to 127.0.0.1 rather than localhost on purpose. On Windows,
        "localhost" can resolve to IPv6 ::1 first while uvicorn is listening
        on IPv4, producing a connection error that looks nothing like its
        real cause.

        Environment variables MEMORY_BASE_URL and MEMORY_USER_ID override the
        defaults, so a shared setup can be configured once in .env or the
        system environment.
    #>
    [CmdletBinding()]
    param(
        [string]$BaseUrl,
        [string]$UserId
    )

    if (-not $BaseUrl) {
        $BaseUrl = if ($env:MEMORY_BASE_URL) { $env:MEMORY_BASE_URL } else { 'http://127.0.0.1:8000' }
    }
    if (-not $UserId) {
        $UserId = if ($env:MEMORY_USER_ID) { $env:MEMORY_USER_ID } else { 'demo' }
    }

    $script:BaseUrl = $BaseUrl.TrimEnd('/')
    $script:UserId  = $UserId

    try {
        $health = Invoke-RestMethod -Uri "$script:BaseUrl/healthz" -TimeoutSec 5
        Write-Host "  connected: $script:BaseUrl  user=$script:UserId  storage=$($health.storage_backend)" -ForegroundColor Green
    } catch {
        Write-Host "  server NOT reachable at $script:BaseUrl" -ForegroundColor Yellow
        Write-Host "  start it in another window with .\run.ps1" -ForegroundColor Yellow
    }
}

function Invoke-Memory {
    <#
    .SYNOPSIS
        Call the API. Paths are relative to /v1/users/{user}.
    .EXAMPLE
        mem GET /wiki/_stats
    .EXAMPLE
        mem PUT /wiki @{ type='person'; title='Alice Chen' }
    .EXAMPLE
        mem GET /raw-facts -Query @{ on = (Get-Date).ToString('yyyy-MM-dd') }
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position = 0)]
        [ValidateSet('GET', 'POST', 'PUT', 'PATCH', 'DELETE')]
        [string]$Method,

        [Parameter(Mandatory, Position = 1)]
        [string]$Path,

        # Hashtable (converted to JSON) or a raw JSON string.
        [Parameter(Position = 2)]
        $Body,

        [hashtable]$Query,

        # Hit a path outside /v1/users/{user}, e.g. -Absolute /healthz
        [switch]$Absolute
    )

    # Auto-connect rather than erroring. The whole point is that there is no
    # setup step to forget.
    if (-not $script:BaseUrl) { Connect-Memory | Out-Null }

    if (-not $Path.StartsWith('/')) { $Path = "/$Path" }

    $uri = if ($Absolute) {
        "$script:BaseUrl$Path"
    } else {
        "$script:BaseUrl/v1/users/$script:UserId$Path"
    }

    if ($Query -and $Query.Count -gt 0) {
        $pairs = foreach ($k in $Query.Keys) {
            '{0}={1}' -f [uri]::EscapeDataString($k), [uri]::EscapeDataString([string]$Query[$k])
        }
        $sep = if ($uri.Contains('?')) { '&' } else { '?' }
        $uri = $uri + $sep + ($pairs -join '&')
    }

    $args = @{ Uri = $uri; Method = $Method; ErrorAction = 'Stop' }

    if ($null -ne $Body) {
        $json = if ($Body -is [string]) { $Body } else { $Body | ConvertTo-Json -Depth 10 -Compress }
        $args['Body'] = $json
        $args['ContentType'] = 'application/json'
    }

    try {
        Invoke-RestMethod @args
    } catch {
        # Surface the API's own message. Invoke-RestMethod throws on 4xx/5xx
        # and hides the body, so details like "Corrupt entity file for
        # 'person/alice-chen'" or a 422 validation hint are otherwise lost.
        $status = $null
        $detail = $null

        if ($_.Exception.PSObject.Properties.Name -contains 'Response' -and $_.Exception.Response) {
            try { $status = [int]$_.Exception.Response.StatusCode } catch { }
        }

        if ($_.PSObject.Properties.Name -contains 'ErrorDetails' -and $_.ErrorDetails -and $_.ErrorDetails.Message) {
            # PowerShell 7 puts the body here.
            $detail = $_.ErrorDetails.Message
        } elseif ($_.Exception.PSObject.Properties.Name -contains 'Response' -and $_.Exception.Response) {
            # PowerShell 5.1 requires reading the stream manually.
            try {
                $stream = $_.Exception.Response.GetResponseStream()
                $stream.Position = 0
                $reader = New-Object System.IO.StreamReader($stream)
                $detail = $reader.ReadToEnd()
                $reader.Close()
            } catch { }
        }

        Write-Host "  $Method $uri" -ForegroundColor DarkGray
        if ($status) { Write-Host "  HTTP $status" -ForegroundColor Red }

        if ($detail) {
            try {
                $parsed = $detail | ConvertFrom-Json
                foreach ($p in $parsed.PSObject.Properties) {
                    Write-Host "  $($p.Name): $($p.Value)" -ForegroundColor Red
                }
            } catch {
                Write-Host "  $detail" -ForegroundColor Red
            }
        } else {
            Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
        }
    }
}

function Get-MemoryConnection {
    <# .SYNOPSIS Show what the module is currently pointed at. #>
    [PSCustomObject]@{
        BaseUrl = $script:BaseUrl
        UserId  = $script:UserId
        Status  = if ($script:BaseUrl) { 'connected' } else { 'not connected - will auto-connect on first call' }
    }
}

Set-Alias -Name mem -Value Invoke-Memory -Scope Global -Force

# Export only when actually loaded as a module. Export-ModuleMember throws
# outside a module context, which would break dot-sourcing — and
# dot-sourcing is the natural fallback when Import-Module cannot resolve the
# path. Guarding it means BOTH of these work:
#     Import-Module .\memory.psm1
#     . .\memory.psm1
if ($MyInvocation.MyCommand.ScriptBlock.Module) {
    Export-ModuleMember -Function Connect-Memory, Invoke-Memory, Get-MemoryConnection -Alias mem
}
