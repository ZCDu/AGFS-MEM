<#
bench.ps1 - run a full CRUD cycle N times and report timing.

    .\bench.ps1
    .\bench.ps1 -Repeat 25
    .\bench.ps1 -Repeat 10 -UserId bench2 -BaseUrl http://127.0.0.1:8000

Reports three numbers per operation and in total:

  WALL    what the client observed, end to end.
  SERVER  what the server spent handling the request, taken from the
          X-Process-Time-Ms response header set by the middleware in
          app/main.py. This is measured with perf_counter around the route
          handler, so it includes storage I/O but excludes network transit,
          TLS, and PowerShell's own object marshalling.
  OVERHEAD  WALL - SERVER. Network plus client cost.

The split matters because this service talks to S3 through mirage. If WALL
is large and SERVER tracks it, the storage backend is the bottleneck and
tuning the client will not help. If SERVER is small and OVERHEAD dominates,
you are measuring your own loopback and PowerShell, not the service.

Uses Invoke-WebRequest, not Invoke-RestMethod: on Windows PowerShell 5.1
Invoke-RestMethod discards response headers, so the server timing would be
invisible.

Writes to its own user id (default "bench") so it will not disturb real data.
#>

param(
    [int]$Repeat = 10,
    [string]$BaseUrl = 'http://127.0.0.1:8000',
    [string]$UserId = 'bench',
    [switch]$KeepData,
    # Per-request ceiling. Without this a slow or wedged backend makes the
    # whole run hang with no output and no idea how far it got.
    [int]$TimeoutSec = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = "$($BaseUrl.TrimEnd('/'))/v1/users/$UserId"
$samples = [ordered]@{}   # name -> list of [pscustomobject]@{Wall; Server}

function Step {
    param(
        [string]$Name,
        [string]$Method,
        [string]$Path,
        $Body,
        [int[]]$Expect = @(200, 201, 204)
    )

    $uri = "$root$Path"
    $args = @{
        Uri                = $uri
        Method             = $Method
        UseBasicParsing    = $true
        ErrorAction        = 'Stop'
        TimeoutSec         = $TimeoutSec
    }
    if ($null -ne $Body) {
        $args['Body'] = if ($Body -is [string]) { $Body } else { $Body | ConvertTo-Json -Depth 10 -Compress }
        $args['ContentType'] = 'application/json'
    }

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $resp = Invoke-WebRequest @args
        $sw.Stop()
        $status = [int]$resp.StatusCode
    } catch {
        $sw.Stop()
        $status = -1
        try { if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode } } catch { }
        Write-Host "  $Name -> HTTP $status  ($Method $uri)" -ForegroundColor Red
        return $null
    }

    if ($Expect -notcontains $status) {
        Write-Host "  $Name -> unexpected HTTP $status" -ForegroundColor Yellow
    }

    # Header name lookup is case-insensitive in practice, but be defensive:
    # a missing header must not abort the run under StrictMode.
    $server = 0.0
    try {
        $h = $resp.Headers['X-Process-Time-Ms']
        if ($h) { $server = [double]($h | Select-Object -First 1) }
    } catch { }

    if (-not $samples.Contains($Name)) { $samples[$Name] = New-Object System.Collections.ArrayList }
    [void]$samples[$Name].Add([pscustomobject]@{
        Wall   = $sw.Elapsed.TotalMilliseconds
        Server = $server
    })

    if ($resp.Content) { return $resp.Content } else { return '' }
}

Write-Host "`n  benchmarking $Repeat CRUD cycles against $root`n" -ForegroundColor Cyan

$cycleWall = New-Object System.Collections.ArrayList
$overall = [System.Diagnostics.Stopwatch]::StartNew()

for ($i = 1; $i -le $Repeat; $i++) {
    $cw = [System.Diagnostics.Stopwatch]::StartNew()

    # Unique per iteration so every cycle does real work rather than
    # re-touching one hot entity, which would flatter the cache.
    $person  = "Bench Person $i"
    $project = "Bench Project $i"
    $pSlug   = "person/bench-person-$i"
    $prSlug  = "project/bench-project-$i"

    Step 'CREATE entity'   PUT    '/wiki' @{ type='person';  title=$person;  summary_append='benchmark subject' } | Out-Null
    Step 'CREATE entity 2' PUT    '/wiki' @{ type='project'; title=$project; summary_append='benchmark project' } | Out-Null
    Step 'READ entity'     GET    "/wiki/$pSlug" $null | Out-Null
    Step 'UPDATE entity'   PUT    '/wiki' @{ type='person'; title=$person; summary_append='updated'; significance=0.8 } | Out-Null

    $factJson = Step 'CREATE fact' POST "/wiki/$pSlug/facts" @{ text='a benchmark fact'; confidence=0.9 }
    $factId = $null
    if ($factJson) { try { $factId = ($factJson | ConvertFrom-Json).facts[0].fact_id } catch { } }

    $relJson = Step 'CREATE relation' POST "/wiki/$pSlug/relations" @{ target_wiki_id=$prSlug; category='related_to'; label='leads' }
    $relId = $null
    if ($relJson) { try { $relId = ($relJson | ConvertFrom-Json).relations[0].relation_id } catch { } }

    if ($factId) { Step 'UPDATE fact' PATCH "/wiki/$pSlug/facts/$factId" @{ text='revised'; confidence=0.95 } | Out-Null }
    if ($relId)  { Step 'UPDATE relation' PATCH "/wiki/$pSlug/relations/$relId" @{ label='tech_lead'; weight=0.9 } | Out-Null }

    Step 'LIST entities'   GET  '/wiki' $null | Out-Null
    Step 'TRAVERSE'        POST '/wiki/traverse' @{ entry_wiki_ids=@($pSlug); max_depth=2 } | Out-Null
    Step 'STATS'           GET  '/wiki/_stats' $null | Out-Null

    if ($factId) { Step 'DELETE fact' DELETE "/wiki/$pSlug/facts/$factId" $null | Out-Null }
    if ($relId)  { Step 'DELETE relation' DELETE "/wiki/$pSlug/relations/$relId" $null | Out-Null }

    if (-not $KeepData) {
        Step 'DELETE entity'   DELETE "/wiki/$prSlug`?hard_delete=true&cascade=true" $null | Out-Null
        Step 'DELETE entity 2' DELETE "/wiki/$pSlug`?hard_delete=true&cascade=true"  $null | Out-Null
    }

    $cw.Stop()
    [void]$cycleWall.Add($cw.Elapsed.TotalMilliseconds)

    # Print each cycle as it completes rather than only at the end. A run
    # against a distant bucket can take minutes, and silent output gives no
    # way to tell "slow" from "hung".
    $done = ($cycleWall | Measure-Object -Sum).Sum
    $eta = ($done / $i) * ($Repeat - $i) / 1000.0
    '  cycle {0,3}/{1}  {2,8:N0} ms   eta {3,6:N0}s' -f $i, $Repeat, $cw.Elapsed.TotalMilliseconds, $eta
    Write-Progress -Activity 'CRUD benchmark' -Status "cycle $i / $Repeat" -PercentComplete (100 * $i / $Repeat)
}

$overall.Stop()
Write-Progress -Activity 'CRUD benchmark' -Completed

function Pct {
    param($Values, [double]$P)
    $vals = @($Values)
    if ($vals.Count -eq 0) { return 0 }
    # @() is required: a single-element pipeline result is a scalar under
    # StrictMode and has no .Count. That happens whenever an operation
    # succeeds in some cycles and fails in others.
    $sorted = @($vals | Sort-Object)
    $idx = [int][math]::Ceiling($P / 100 * $sorted.Count) - 1
    if ($idx -lt 0) { $idx = 0 }
    if ($idx -ge $sorted.Count) { $idx = $sorted.Count - 1 }
    return $sorted[$idx]
}

'{0,-18} {1,6} {2,9} {3,9} {4,9} {5,9}' -f 'OPERATION','N','WALL avg','SERVER avg','OVERHEAD','WALL p95'
'-' * 68

$totalWall = 0.0; $totalServer = 0.0; $totalCalls = 0
foreach ($name in $samples.Keys) {
    $rows   = @($samples[$name])
    $walls  = @($rows | ForEach-Object { $_.Wall })
    $servers= @($rows | ForEach-Object { $_.Server })
    $wAvg = ($walls   | Measure-Object -Average).Average
    $sAvg = ($servers | Measure-Object -Average).Average
    '{0,-18} {1,6} {2,9:N2} {3,9:N2} {4,9:N2} {5,9:N2}' -f `
        $name, $rows.Count, $wAvg, $sAvg, ($wAvg - $sAvg), (Pct -Values $walls -P 95)
    $totalWall   += ($walls   | Measure-Object -Sum).Sum
    $totalServer += ($servers | Measure-Object -Sum).Sum
    $totalCalls  += $rows.Count
}

$cycles = @($cycleWall)
Write-Host ''
Write-Host ('  cycles                 : {0}' -f $Repeat)
Write-Host ('  requests               : {0}  ({1:N1} per cycle)' -f $totalCalls, ($totalCalls / $Repeat))
Write-Host ('  total elapsed          : {0:N1} ms' -f $overall.Elapsed.TotalMilliseconds)
Write-Host ('  avg per cycle          : {0:N1} ms   (p95 {1:N1} ms)' -f (($cycles | Measure-Object -Average).Average, (Pct -Values $cycles -P 95)))
Write-Host ('  avg per request        : {0:N2} ms' -f ($totalWall / $totalCalls))
Write-Host ''
Write-Host ('  server processing time : {0:N1} ms total, {1:N2} ms avg' -f $totalServer, ($totalServer / $totalCalls))
Write-Host ('  client + network       : {0:N1} ms total, {1:N2} ms avg' -f ($totalWall - $totalServer), (($totalWall - $totalServer) / $totalCalls))
if ($totalWall -gt 0) {
    Write-Host ('  server share of wall   : {0:P1}' -f ($totalServer / $totalWall))
}
Write-Host ''
