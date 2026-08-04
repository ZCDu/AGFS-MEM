# seed_demo.ps1 - populate a demo knowledge graph for manual testing.
#
# Usage:
#   1. Start the server in another window:  .\run.ps1
#   2. .\seed_demo.ps1
#
# Creates 17 entities covering all 8 valid types, 6 facts, and 20 edges
# covering all 6 relation categories. Every payload here has been run
# against the API and returns 200.
#
# NOTE on bidirectional relations: the reverse edge reuses the SAME label,
# so `alice -leads-> orion` also produces `orion -leads-> alice`. That reads
# backwards for asymmetric verbs. Bidirectional is used below only where the
# reverse direction is tolerable; one-directional edges are used elsewhere.

$ErrorActionPreference = "Stop"
$U = "http://localhost:8000/v1/users/demo"
$J = "application/json"

$ok = 0
$fail = 0

function Send-Payload {
    param($Method, $Path, $Body, $Label)
    try {
        Invoke-RestMethod -Method $Method -Uri "$U$Path" -ContentType $J -Body $Body | Out-Null
        $script:ok++
    } catch {
        $script:fail++
        Write-Host "  FAIL $Label" -ForegroundColor Red
        Write-Host "       $($_.Exception.Message)" -ForegroundColor DarkGray
    }
}

# ---------------- entities ----------------

$entities = @(
    # people
    '{"type":"person","title":"Alice Chen","aliases":["Alice","A. Chen"],"summary_append":"Staff engineer, retrieval team. Based in Taipei.","significance":0.9}',
    '{"type":"person","title":"Marcus Webb","aliases":["Marcus"],"summary_append":"Engineering manager for platform infrastructure.","significance":0.7}',
    '{"type":"person","title":"Priya Raman","aliases":["Priya"],"summary_append":"Product lead for search experiences.","significance":0.8}',
    '{"type":"person","title":"Tom Okafor","aliases":["Tom"],"summary_append":"Data scientist working on ranking evaluation.","significance":0.6}',
    # organizations
    '{"type":"organization","title":"Northwind Labs","aliases":["Northwind"],"summary_append":"Primary employer. Around 400 people.","significance":0.8}',
    '{"type":"organization","title":"Acme Corp","aliases":["Acme"],"summary_append":"Enterprise customer, largest by ARR.","significance":0.7}',
    # projects
    '{"type":"project","title":"Orion","summary_append":"Internal semantic search platform. In beta.","significance":0.95}',
    '{"type":"project","title":"Helios","summary_append":"Infrastructure migration to managed Kubernetes.","significance":0.6}',
    # events
    '{"type":"event","title":"Q3 Planning Offsite","summary_append":"Two-day planning session, Taipei office.","significance":0.5}',
    '{"type":"event","title":"Orion Beta Launch","summary_append":"Beta opened to 50 internal users.","significance":0.85}',
    # concepts
    '{"type":"concept","title":"Vector Retrieval","aliases":["embedding search"],"summary_append":"Dense retrieval over learned embeddings.","significance":0.7}',
    '{"type":"concept","title":"Hybrid Search","summary_append":"Combines lexical BM25 with dense vector scoring.","significance":0.8}',
    # artifacts
    '{"type":"artifact","title":"Orion Design Doc","summary_append":"Architecture and rollout plan for Orion.","significance":0.6}',
    '{"type":"artifact","title":"Retrieval Benchmark v2","summary_append":"Evaluation harness, 1200 labelled queries.","significance":0.5}',
    # preferences
    '{"type":"preference","title":"Async Written Updates","summary_append":"Prefers written async updates over status meetings.","significance":0.4}',
    # decisions
    '{"type":"decision","title":"Adopt Hybrid Retrieval","summary_append":"Chose hybrid over pure vector after benchmark v2.","significance":0.9}',
    '{"type":"decision","title":"Deprecate Legacy Index","summary_append":"Legacy inverted index retired once Orion hits GA.","significance":0.7}'
)

Write-Host "Creating entities..." -ForegroundColor Cyan
foreach ($e in $entities) { Send-Payload "Put" "/wiki" $e $e }

# ---------------- facts ----------------

$facts = @(
    @{ id = "person/alice-chen";               body = '{"text":"Leads the retrieval workstream on Orion.","confidence":0.95,"evidence":["orion-design-doc"]}' },
    @{ id = "person/alice-chen";               body = '{"text":"Joined Northwind in 2024.","confidence":0.9}' },
    @{ id = "project/orion";                   body = '{"text":"Beta opened to 50 internal users in Q3.","confidence":1.0}' },
    @{ id = "project/orion";                   body = '{"text":"p95 latency target is 500ms.","confidence":0.8}' },
    @{ id = "decision/adopt-hybrid-retrieval"; body = '{"text":"Hybrid beat pure vector by 12 points nDCG@10.","confidence":0.85,"evidence":["retrieval-benchmark-v2"]}' },
    @{ id = "concept/hybrid-search";           body = '{"text":"Requires tuning the lexical or dense blend weight.","confidence":0.7}' }
)

Write-Host "Adding facts..." -ForegroundColor Cyan
foreach ($f in $facts) { Send-Payload "Post" "/wiki/$($f.id)/facts" $f.body $f.id }

# ---------------- relations (all 6 categories) ----------------

$relations = @(
    @{ id = "person/alice-chen";                 body = '{"target_wiki_id":"project/orion","category":"related_to","label":"leads","weight":1.0}' },
    @{ id = "project/orion";                     body = '{"target_wiki_id":"person/alice-chen","category":"related_to","label":"led_by"}' },
    @{ id = "person/alice-chen";                 body = '{"target_wiki_id":"organization/northwind-labs","category":"related_to","label":"works_at"}' },
    @{ id = "person/marcus-webb";                body = '{"target_wiki_id":"project/helios","category":"related_to","label":"manages"}' },
    @{ id = "person/priya-raman";                body = '{"target_wiki_id":"project/orion","category":"related_to","label":"product_lead"}' },
    @{ id = "person/tom-okafor";                 body = '{"target_wiki_id":"artifact/retrieval-benchmark-v2","category":"related_to","label":"authored"}' },
    @{ id = "project/orion";                     body = '{"target_wiki_id":"organization/acme-corp","category":"related_to","label":"piloted_by"}' },
    @{ id = "artifact/orion-design-doc";         body = '{"target_wiki_id":"project/orion","category":"related_to","label":"documents"}' },
    @{ id = "preference/async-written-updates";  body = '{"target_wiki_id":"person/alice-chen","category":"related_to","label":"held_by"}' },
    # symmetric - safe to make bidirectional
    @{ id = "person/alice-chen";                 body = '{"target_wiki_id":"person/priya-raman","category":"related_to","label":"collaborates_with","bidirectional":true}' },
    # refines
    @{ id = "concept/hybrid-search";             body = '{"target_wiki_id":"concept/vector-retrieval","category":"refines","label":"extends","reason":"Adds a lexical channel on top of dense retrieval."}' },
    # causes
    @{ id = "decision/adopt-hybrid-retrieval";   body = '{"target_wiki_id":"concept/hybrid-search","category":"causes","label":"selected","reason":"Benchmark v2 favoured hybrid."}' },
    @{ id = "decision/adopt-hybrid-retrieval";   body = '{"target_wiki_id":"project/orion","category":"causes","label":"shaped"}' },
    # contradicts
    @{ id = "decision/deprecate-legacy-index";   body = '{"target_wiki_id":"decision/adopt-hybrid-retrieval","category":"contradicts","label":"tension_with","reason":"Legacy index still serves the lexical channel hybrid depends on."}' },
    # temporal
    @{ id = "event/q3-planning-offsite";         body = '{"target_wiki_id":"event/orion-beta-launch","category":"temporal_before","label":"preceded"}' },
    @{ id = "event/orion-beta-launch";           body = '{"target_wiki_id":"event/q3-planning-offsite","category":"temporal_after","label":"followed"}' },
    @{ id = "event/orion-beta-launch";           body = '{"target_wiki_id":"project/orion","category":"related_to","label":"milestone_of"}' },
    @{ id = "person/marcus-webb";                body = '{"target_wiki_id":"organization/northwind-labs","category":"related_to","label":"works_at"}' },
    @{ id = "person/priya-raman";                body = '{"target_wiki_id":"organization/northwind-labs","category":"related_to","label":"works_at"}' },
    @{ id = "person/tom-okafor";                 body = '{"target_wiki_id":"organization/northwind-labs","category":"related_to","label":"works_at"}' }
)

Write-Host "Linking relations..." -ForegroundColor Cyan
foreach ($r in $relations) { Send-Payload "Post" "/wiki/$($r.id)/relations" $r.body $r.id }

# ---------------- summary ----------------

Write-Host ""
Write-Host "  succeeded: $ok   failed: $fail" -ForegroundColor $(if ($fail -eq 0) { "Green" } else { "Red" })

Start-Sleep -Seconds 3   # let the write-behind buffers flush
$stats = Invoke-RestMethod -Uri "$U/wiki/_stats"
Write-Host "  graph: $($stats.entities) entities, $($stats.edges) edges"
Write-Host ""
Write-Host "Try next:" -ForegroundColor Cyan
Write-Host '  Invoke-RestMethod -Uri "$U/wiki?type=person" | Select-Object title, compact'
Write-Host '  Invoke-RestMethod -Method Post -Uri "$U/wiki/traverse" -ContentType $J -Body ''{"entry_wiki_ids":["person/alice-chen"],"max_depth":2}'''
