# crud_demo.ps1 - full create/read/update/delete walkthrough against the API.
#
# Usage:
#   1. Start the server in another window:  .\run.ps1
#   2. .\crud_demo.ps1
#
# Every request here was verified against a live server. Uses a throwaway
# user ("crud") so it will not disturb data seeded by seed_demo.ps1.

$ErrorActionPreference = "Stop"
$U = "http://localhost:8000/v1/users/crud"
$J = "application/json"

function Step($label) { Write-Host "`n$label" -ForegroundColor Cyan }
function Show($name, $value) { Write-Host ("  {0,-26} {1}" -f $name, $value) }

# ---------------- CREATE ----------------

Step "CREATE"

$alice = Invoke-RestMethod -Method Put -Uri "$U/wiki" -ContentType $J -Body '{"type":"person","title":"Alice Chen","aliases":["Alice"],"summary_append":"Engineer on Orion."}'
Show "entity" $alice.wiki_id

$orion = Invoke-RestMethod -Method Put -Uri "$U/wiki" -ContentType $J -Body '{"type":"project","title":"Orion","summary_append":"Internal search platform."}'
Show "entity" $orion.wiki_id

$alice = Invoke-RestMethod -Method Post -Uri "$U/wiki/person/alice-chen/facts" -ContentType $J -Body '{"text":"Leads retrieval.","confidence":0.9}'
Show "fact" $alice.facts[0].fact_id

$alice = Invoke-RestMethod -Method Post -Uri "$U/wiki/person/alice-chen/relations" -ContentType $J -Body '{"target_wiki_id":"project/orion","category":"related_to","label":"leads"}'
Show "relation" $alice.relations[0].relation_id

$raw = Invoke-RestMethod -Method Post -Uri "$U/raw-facts" -ContentType $J -Body '{"facts":[{"text":"Mentioned Orion in standup.","source":"slack"}]}'
Show "raw fact" "$($raw.count) record(s) on $($raw.date)"

# Capture the generated ids for the update/delete steps below. Do not
# hardcode "fact_0001" - ids are assigned per entity and shift as facts are
# added and removed.
$factId = $alice.facts[0].fact_id
$relId  = $alice.relations[0].relation_id

# ---------------- READ ----------------

Step "READ"

$one = Invoke-RestMethod -Uri "$U/wiki/person/alice-chen"
Show "get entity" "$($one.title) | decay $([math]::Round($one.decay_score,3))"

Show "list all" "$((Invoke-RestMethod -Uri "$U/wiki").Count) entities"
Show "list by type" "$((Invoke-RestMethod -Uri "$U/wiki?type=person").Count) person(s)"

$edges = Invoke-RestMethod -Uri "$U/wiki/person/alice-chen/relations"
Show "get relations" "$($edges.Count) edge(s): $($edges[0].label) -> $($edges[0].target)"

$stats = Invoke-RestMethod -Uri "$U/wiki/_stats"
Show "stats" "$($stats.entities) entities, $($stats.edges) edges"

$tr = Invoke-RestMethod -Method Post -Uri "$U/wiki/traverse" -ContentType $J -Body '{"entry_wiki_ids":["person/alice-chen"],"max_depth":2}'
Show "traverse" ($tr.wiki_ids -join ", ")

$today = [DateTime]::UtcNow.ToString('yyyy-MM-dd')
Show "raw facts today" "$((Invoke-RestMethod -Uri "$U/raw-facts?on=$today").count) record(s)"

# ---------------- UPDATE ----------------

Step "UPDATE"

# PUT is an upsert: summary_append adds to the existing summary rather than
# replacing it, and omitted fields are left alone.
$alice = Invoke-RestMethod -Method Put -Uri "$U/wiki" -ContentType $J -Body '{"type":"person","title":"Alice Chen","summary_append":"Promoted in 2026.","significance":0.95}'
Show "entity summary" $alice.summary
Show "entity significance" $alice.metadata.significance

$alice = Invoke-RestMethod -Method Patch -Uri "$U/wiki/person/alice-chen/facts/$factId" -ContentType $J -Body '{"text":"Leads the retrieval workstream.","confidence":0.98}'
Show "fact" "$($alice.facts[0].text) (conf $($alice.facts[0].confidence))"

$alice = Invoke-RestMethod -Method Patch -Uri "$U/wiki/person/alice-chen/relations/$relId" -ContentType $J -Body '{"label":"tech_lead","weight":0.9,"reason":"Promoted."}'
Show "relation" "$($alice.relations[0].label) (weight $($alice.relations[0].weight))"

# ---------------- DELETE ----------------

Step "DELETE"

$alice = Invoke-RestMethod -Method Delete -Uri "$U/wiki/person/alice-chen/facts/$factId"
Show "fact removed" "$($alice.facts.Count) fact(s) left"

$alice = Invoke-RestMethod -Method Delete -Uri "$U/wiki/person/alice-chen/relations/$relId"
Show "relation removed" "$($alice.relations.Count) relation(s) left"

# hard_delete=false keeps a tombstone (status=deleted) as an audit trail.
# The entity stops being readable either way.
Invoke-RestMethod -Method Delete -Uri "$U/wiki/project/orion?hard_delete=false" | Out-Null
Show "entity (soft)" "project/orion tombstoned"

try {
    Invoke-RestMethod -Uri "$U/wiki/project/orion" | Out-Null
    Show "read tombstone" "UNEXPECTED: still readable"
} catch {
    Show "read tombstone" "404 as expected"
}

# cascade=true also strips edges other entities hold pointing at this one.
# Use the slug form (alice-chen), not the display title. A space in the URI
# is an encoding trap; every route accepts the slug directly.
Invoke-RestMethod -Method Delete -Uri "$U/wiki/person/alice-chen?hard_delete=true&cascade=true" | Out-Null
Show "entity (hard)" "person/alice-chen removed"

try {
    Invoke-RestMethod -Method Delete -Uri "$U/wiki/person/alice-chen" | Out-Null
    Show "delete again" "UNEXPECTED: succeeded"
} catch {
    Show "delete again" "404 as expected"
}

Start-Sleep -Seconds 3   # let write-behind buffers flush
$stats = Invoke-RestMethod -Uri "$U/wiki/_stats"
Write-Host "`n  final: $($stats.entities) entities, $($stats.edges) edges" -ForegroundColor Green
