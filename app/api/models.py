from __future__ import annotations

from pydantic import BaseModel, Field


# ---------- entities ----------

class UpsertEntityRequest(BaseModel):
    type: str = Field(..., description="One of: person, organization, project, event, "
                                          "concept, artifact, preference, decision")
    title: str = Field(..., description="Entity display title, e.g. 'Alice Chen'")
    aliases: list[str] = Field(default_factory=list, description="Alternate names, merged with any existing")
    summary_append: str | None = Field(None, description="Text to append to the entity's summary")
    compact: str | None = Field(None, description="Override the compact one-line view; "
                                                     "auto-derived from summary if omitted")
    significance: float | None = Field(None, ge=0.0, le=1.0)
    on_conflict: str = Field(
        "error",
        description="What to do when a DIFFERENT title already occupies this wiki_id "
                    "(e.g. 'C++' and 'C#' both slugify to 'c'). "
                    "'error' refuses with 409 and suggests an alternative; "
                    "'disambiguate' creates 'c-2'; "
                    "'merge' merges into the existing entity, discarding the incoming title. "
                    "Titles differing only in case, spacing or trailing punctuation "
                    "always merge, recording the variant as an alias.")


class AddFactRequest(BaseModel):
    text: str
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class UpdateFactRequest(BaseModel):
    text: str | None = None
    confidence: float | None = Field(None, ge=0.0, le=1.0)
    evidence: list[str] | None = None


class UpdateRelationRequest(BaseModel):
    label: str | None = None
    weight: float | None = None
    reason: str | None = None
    fact_ids: list[str] | None = None
    evidence: list[str] | None = None


class FactOut(BaseModel):
    fact_id: str
    text: str
    confidence: float
    evidence: list[str]
    created_at: str
    updated_at: str


class RelationOut(BaseModel):
    relation_id: str
    target: str
    category: str
    label: str
    weight: float
    reason: str
    evidence: list[str]
    fact_ids: list[str]
    created_at: str
    updated_at: str


class MetadataOut(BaseModel):
    significance: float
    last_accessed: str
    created_at: str
    updated_at: str
    user_id: str


class EntityOut(BaseModel):
    okf_version: str
    wiki_id: str
    type: str
    title: str
    aliases: list[str]
    compact: str
    summary: str
    facts: list[FactOut]
    relations: list[RelationOut]
    status: str
    merged_into: str | None
    metadata: MetadataOut
    decay_score: float


class MergeEntitiesRequest(BaseModel):
    target_wiki_id: str = Field(..., description="wiki_id of the entity to keep, e.g. "
                                                     "'person/alice-chen'. The entity in the "
                                                     "URL is folded into this one and tombstoned.")
    hard_delete: bool = Field(False, description="Physically remove the source's file after "
                                                     "merging. False (default) keeps a permanent "
                                                     "tombstone recording where it was merged to.")


class MergeEntitiesResponse(BaseModel):
    merged: bool
    target: str
    facts_moved: int
    relations_moved: int
    relations_redirected: int
    failed: str | None = None


class LinkEntitiesRequest(BaseModel):
    target_wiki_id: str = Field(..., description="Target entity's wiki_id, e.g. 'project/orion'")
    category: str = Field("related_to")
    label: str = Field("", description="Free-form domain verb, e.g. 'works_on'")
    weight: float = Field(1.0)
    reason: str = Field("")
    fact_ids: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    bidirectional: bool = Field(False)


# ---------- graph ----------

class TraverseRequest(BaseModel):
    entry_wiki_ids: list[str] = Field(..., min_length=1)
    max_depth: int = Field(1, ge=0, le=10)
    max_nodes: int = Field(50, ge=1, le=1000)
    categories: list[str] | None = None


class TraverseResponse(BaseModel):
    wiki_ids: list[str]


class GraphStatsResponse(BaseModel):
    entities: int
    edges: int


class ReconcileResponse(BaseModel):
    entities_scanned: int
    entities_fixed: int
    relations_removed: int
    entities_failed: list[str] = []


# ---------- title resolver / inbox ----------

class ResolveRequest(BaseModel):
    title: str
    type_hint: str | None = Field(None, description="One of the valid entity types. If omitted, "
                                                        "an unmatched title goes to the inbox instead "
                                                        "of being auto-created.")
    aliases: list[str] = Field(default_factory=list)
    summary_append: str | None = None
    compact: str | None = None
    significance: float | None = Field(None, ge=0.0, le=1.0)


class ResolveResponse(BaseModel):
    action: str  # "matched" | "created" | "inbox"
    wiki_id: str | None = None
    inbox_id: str | None = None
    reason: str = ""
    entity: EntityOut | None = None


class InboxCandidateOut(BaseModel):
    candidate_id: str
    title: str
    type_hint: str | None
    aliases: list[str]
    summary_append: str | None
    compact: str | None
    significance: float | None
    reason: str
    created_at: str


class ResolveInboxCandidateRequest(BaseModel):
    type: str
    title: str | None = Field(None, description="Override the candidate's original title")


# ---------- manifest ----------

class SetPositionsRequest(BaseModel):
    positions: dict[str, list[float]] = Field(
        ..., description="wiki_id -> [x, y]. Unknown ids are ignored.")


class SubgraphRequest(BaseModel):
    entry_wiki_ids: list[str] = Field(
        default_factory=list,
        description="Where to start. If empty, the highest-degree nodes are used — "
                    "hubs are the most useful way into an unfamiliar graph.")
    max_depth: int = Field(1, ge=0, le=10)
    max_nodes: int = Field(60, ge=1, le=1000)
    categories: list[str] | None = Field(
        None, description="Only follow these relation categories")


class SubgraphEdgeOut(BaseModel):
    source: str
    target: str
    category: str


class SubgraphResponse(BaseModel):
    nodes: list["ManifestEntryOut"]
    edges: list[SubgraphEdgeOut]
    seeds: list[str]
    expandable: list[str] = Field(
        default_factory=list,
        description="Nodes with neighbours outside this response — worth expanding.")
    truncated: bool = Field(
        False, description="max_nodes was reached; there is more graph beyond this.")


class SaveLayoutRequest(BaseModel):
    positions: dict[str, dict[str, float]] = Field(
        ..., description='{"person/alice-chen": {"x": 12.0, "y": -40.5}}. '
                         "Merged into whatever is stored, so saving one "
                         "neighbourhood does not erase the rest.")


class ManifestEntryOut(BaseModel):
    wiki_id: str
    type: str
    title: str
    aliases: list[str]
    compact: str
    status: str
    updated_at: str
    # Outbound edges from the adjacency index, as {"t": target, "c": category}.
    # Exposing them here lets a client draw the whole graph from ONE request
    # instead of fetching every entity. None means the entry predates the
    # index and its edges are unknown, which is different from having none.
    edges: list[dict] | None = None
    # Persisted graph-editor layout coordinates (None until a client saves a
    # position via POST /wiki/layout).
    x: float | None = None
    y: float | None = None
    # Populated only on subgraph() responses: total incident edges, and how
    # many of that node's neighbours were left out of THIS response (lets a
    # client offer an expand affordance instead of implying a leaf).
    degree: int = 0
    hidden_neighbours: int = 0
    # Connected-component index within the wiki (see app/graph/components.py)
    # -- entities with no edges connecting them to each other get different
    # values. None when not computed (e.g. outside subgraph()), distinct
    # from "computed as component 0".
    component: int | None = None


# ---------- raw log ----------

class AppendFactsRequest(BaseModel):
    facts: list[dict] = Field(..., min_length=1, description="Arbitrary JSON records to append")


class RawFactsResponse(BaseModel):
    date: str
    count: int
    records: list[dict]


# ---------- share links ----------

class CreateShareRequest(BaseModel):
    label: str = Field("", description="Optional display label for the guest landing "
                                          "page, e.g. the entity's title. Defaults to it "
                                          "if left blank.")
    expires_in_days: float | None = Field(
        None, gt=0, description="Link stops working after this many days. Omit for no "
                                   "expiry (still revocable at any time).")


class ShareOut(BaseModel):
    share_id: str
    owner_user_id: str
    entry_wiki_id: str
    label: str
    created_at: str
    expires_at: str | None
    revoked_at: str | None
    active: bool
    url: str


class SharePreview(BaseModel):
    """What a guest sees before starting a chat -- no memory content, just
    enough to know what they're being invited into."""
    label: str
    entry_wiki_id: str
    entry_title: str
    entry_type: str
    scope_size: int
    active: bool


class SharedChatMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str


class SharedChatRequest(BaseModel):
    messages: list[SharedChatMessage] = Field(..., min_length=1)
    session_id: str | None = Field(
        None, description="Omit on the first turn; echo back the returned id "
                          "on later turns so the whole conversation lands in "
                          "one session file.")


class SharedChatResponse(BaseModel):
    reply: str
    session_id: str
    applied: list[dict] = Field(
        default_factory=list, description="Operations that landed, within scope.")
    rejected: list[dict] = Field(
        default_factory=list, description="Operations the model proposed that fell "
                                             "outside what was shared -- not saved "
                                             "anywhere, with a reason each.")
