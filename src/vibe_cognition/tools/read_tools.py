"""MCP tools that store documents and read the graph: nodes, search, chains, history."""

from typing import Any

from fastmcp import Context

from ..cognition import (
    CognitionNodeType,
    CognitionStorage,
    get_history_for_context,
    get_incident_resolution,
    get_reasoning_chain,
    get_superseded_chain,
    get_workflow_head,
)
from ..embeddings import EmbeddingGenerator, VectorStore
from .cognition_tools import (
    _embeddings_ready,
    _gated_identity,
    _get_document,
    _get_node,
    _index_coverage,
    _index_syncing,
    _parse_exclude_people,
    _parse_node_type,
    _parse_searchable_node_type,
    _reembed_replayed_nodes,
    _search_cognition,
    _search_with_embedding,
    _store_document,
    _store_last_error,
    _store_unavailable_reason,
    _update_node,
    _validate_direction,
)
from .dispatch import dispatch_tool
from .project_registry import LoadedProjects, resolve_project, tag_results
from .utils import get_lifespan, require_embeddings


def register_read_tools(mcp) -> None:

    @dispatch_tool(mcp)
    def cognition_store_document(
        ctx: Context,
        title: str,
        document_text: str,
        context: str,
        author: str,
        file_path: str | None = None,
        content_text: str | None = None,
        references: str | None = None,
        mime: str | None = None,
        force_new: bool = False,
        store_copy: bool = False,
        local_only: bool | None = None,
        from_agent: bool = True,
    ) -> dict[str, Any]:
        """Store a document as a first-class DOCUMENT node.

        Default REFERENCE mode: the node records the document's PATH + metadata +
        content sha256 — the bytes STAY WHERE THEY LIVE. The recorded path is
        project-relative; a file outside the project gets NO path in the shared
        journal (its location is kept on this machine only). Your extracted
        `document_text` goes into a text sidecar (kept small out of the node so
        journal lines stay small; it powers document search in a later version). To
        capture what's INSIDE the document, record its facts as separate entity
        nodes with cognition_record, citing this document's returned `doc_ref` in
        THEIR `references` — they auto-link `part_of` the document.

        Opt-in COPY mode (`store_copy=true`) also copies the bytes into a content-
        addressed blob under `.cognition/documents/`, so the document survives the
        original moving and can travel via git. By default the blob is committed;
        `local_only=true` keeps it out of git (a per-machine choice). Size policy
        (no hard cap): >=50MB is auto-forced local_only with a warning; >=95MB
        refuses default-commit (forced local_only) so a huge blob can't brick later
        pushes. PRIVACY: a committed blob survives in git history and on the remote
        even after the node is deleted — deleting does NOT un-publish it.

        Provide EITHER `file_path` (a document on disk) OR `content_text` (inline).

        Args:
            title: Short title for the document node (its summary).
            document_text: The full extracted text (you extract it; stored in the sidecar).
            context: Comma-separated topical terms / related areas.
            author: The current git user name.
            file_path: Absolute path to the document on disk.
            content_text: Inline text instead of a file (hashed directly).
            references: Optional extra refs — these go to CONTEXT (the node's own
                references are restricted to its doc: key by design).
            mime: Optional MIME type (metadata only).
            force_new: Store even if a document with the same content already exists.
            store_copy: Copy the bytes into the content-addressed blob store.
            local_only: Keep the copied blob out of version control (copy mode
                        only). On an SVN working copy, new document files are
                        scheduled for addition automatically (never committed) and
                        local-only blobs are skipped.
            from_agent: Set false ONLY when the human explicitly dictated/authored
                        this store themselves; default true. When in doubt, leave
                        the default. Stamped as metadata.from_agent (new documents
                        only — a dedup hit on an existing document is unaffected);
                        surfaced in search results (missing on pre-existing docs).

        New documents are also stamped `metadata.recorded_by` (server-resolved git
        identity, verbatim — same shape as every other write path). Same caveat as
        from_agent: only new documents get it; a dedup hit on an existing
        (pre-existing) document is unaffected and may still lack the stamp.

        Returns:
            {node_id, doc_ref, mode, size, indexed_text_chars, already_stored?,
             blob_bytes?, blob_path?, local_only?, promoted?, already_committed?, warnings?,
             prior_version_id?, consider_supersedes?}
            prior_version_id/consider_supersedes (WP-12, db65f1568fa5) appear ONLY when
            file_path pointed at a path some EXISTING document node already references,
            with DIFFERENT content (sha changed) — a re-store of a changed file. This is
            an OFFER, never automatic: no supersedes edge is created for you — if this
            genuinely is a new version, record it as a new document and run /vibe-curate,
            the ONLY path that writes the supersedes edge (semantic edges, including
            supersedes, are never written directly, cognition_add_edge included).
            Refused with `{"identity_required": true, ...}` when this checkout has
            no CONFIRMED identity, or its profile is missing role/seniority/
            reports_to — the payload names what to ask the human for and the
            `cognition_set_identity` call that fixes it. Nothing is written.
        """
        lifespan = get_lifespan(ctx)
        storage: CognitionStorage = lifespan["cognition_storage"]
        # Embed at store time only if the model is ready; else defer to the next sync
        # (require_embeddings returns an error dict when not ready). Never error here.
        ready = require_embeddings(ctx) is None
        embedding_storage = lifespan["cognition_embedding_storage"] if ready else None
        generator = lifespan["embedding_generator"] if ready else None
        return _store_document(
            storage, title, document_text, context, author,
            file_path=file_path, content_text=content_text,
            references=references, mime=mime, force_new=force_new,
            store_copy=store_copy, local_only=local_only, from_agent=from_agent,
            embedding_storage=embedding_storage, generator=generator,
        )

    @dispatch_tool(mcp)
    def cognition_get_document(
        ctx: Context,
        node_id: str | None = None,
        doc_ref_arg: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve a stored document: metadata + full sidecar text + path, with a
        freshness check.

        Resolve by `node_id` or by `doc_ref_arg` (the `doc:<hash>` returned at
        store time). In reference mode the original file is re-hashed: `freshness`
        is `unchanged | modified | missing`.

        Cross-project note: when project is a foreign tag/path, `freshness` is
        always "cross-project: unavailable" — the path in the node is
        foreign-machine-relative and a local re-hash would mislead.

        Args:
            node_id: Node id to retrieve.
            doc_ref_arg: doc:<hash> ref returned at store time.
            project: None (default, home) or tag/path for a loaded foreign project.
                     "*" is rejected.

        Returns:
            {node_id, doc_ref, metadata, text, path, freshness} or {"error": ...}
            `path` is the source file's absolute location on THIS machine, or null
            when none is known here (inline text, or a file outside the project
            that another machine stored). `metadata.path`, when present, is
            project-relative (absolute only on documents stored before v0.49.2).
            When project is not None, also includes "project": tag.
        """
        lc = get_lifespan(ctx)
        if project is None:
            return _get_document(lc["cognition_storage"], node_id=node_id, doc_ref_arg=doc_ref_arg)
        if project == "*":
            return {"error": 'project="*" is not supported for single-node tools'}
        entries, err = resolve_project(lc, project)
        if err:
            return err
        result = _get_document(entries[0].storage, node_id=node_id, doc_ref_arg=doc_ref_arg)
        if "error" not in result:
            result["project"] = entries[0].tag
            result["freshness"] = "cross-project: unavailable"
        return result

    @dispatch_tool(mcp)
    def cognition_get_node(ctx: Context, node_id: str, project: str | None = None) -> dict[str, Any]:
        """Read a single cognition node's FULL narrative by id.

        Search results and `cognition_get_neighbors` return summaries only (no
        `detail`) — use this after a hit to read the complete node. This is the
        GENERIC node read; for a stored document, `cognition_get_document` is the
        specialized get-by-id (it adds the sidecar text + a freshness check).

        Args:
            node_id: The node to read.
            project: None (default, home) or a tag/path for a loaded foreign
                     project. "*" is rejected (node ids are not project-namespaced).

        Returns:
            {id, type, summary, detail, ...} or {"error": ...} if absent. The
            node's kind is keyed "type" here (the raw graph attribute name) —
            NOTE cognition_search's results use "node_type" instead, not "type";
            the two are NOT interchangeable, check which tool you're reading from.
            When project is not None, also includes "project": tag. A constraint's
            metadata.scope is "personal" or "project" (absent means project); a
            teammate's personal constraint is reported absent. A constraint may also
            carry scope_review: {suggested, reason, flagged_at, curation_session} —
            curation's suggestion that its scope is wrong, awaiting the owner's
            ruling via cognition_update_node(node_id, scope=...). Only the owner
            ever sees that key: it is absent for everyone else, and null once a
            ruling cleared it.
        """
        lc = get_lifespan(ctx)
        if project is None:
            return _get_node(lc["cognition_storage"], node_id)
        if project == "*":
            return {"error": 'project="*" is not supported for single-node tools; node ids are not project-namespaced'}
        entries, err = resolve_project(lc, project)
        if err:
            return err
        result = _get_node(entries[0].storage, node_id)
        if "error" not in result:
            result["project"] = entries[0].tag
        return result

    @dispatch_tool(mcp)
    def cognition_update_node(
        ctx: Context,
        node_id: str,
        summary: str | None = None,
        detail: str | None = None,
        context: str | None = None,
        severity: str | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Edit a node's narrative in place — fix a typo or refine wording WITHOUT
        delete+re-record (which would lose the id, its edges, and its curation marker).

        Only these narrative fields are editable: `summary`, `detail`,
        `context` (comma-separated), `severity`, plus a constraint's `scope`. Structural fields (id, type,
        references, metadata, timestamp) are intentionally NOT editable — changing
        them would corrupt invariants (a document node's sha/mode/`doc:` ref, the
        reference→part_of index, the minted id). To change those, the node should be
        re-created.

        When any editable field changes and the embedding model is loaded, the node
        is RE-EMBEDDED so `cognition_search` reflects the edit — both the match vector
        (summary/detail) and the result metadata it surfaces (context/severity).
        Otherwise search would keep serving the pre-edit values. The result carries
        `reembed`: "done" | "deferred" (model still loading).

        Note: re-embedding a DOCUMENT node refreshes its node-level vector (its chunk
        vectors, derived from the sidecar, are untouched); this is safe but means an
        edited document node's vector metadata takes the entity shape rather than the
        as-stored document shape.

        For `task` nodes, this tool can edit summary/detail/context/severity, but
        `status`, `owner`, and the parent hierarchy live in metadata — use
        `cognition_update_task` for those (it also records the status-transition log).

        Args:
            node_id: The node to edit.
            summary: New summary, if changing.
            detail: New detail body, if changing.
            context: New comma-separated context tags, if changing.
            severity: New severity, if changing.
            scope: Constraint only — "personal" or "project" (see cognition_record's
                CONSTRAINT SCOPE). Only the person who recorded the constraint may
                change it; a constraint with no recorded owner (pre-identity) stays
                project. Any scope value clears a pending curation scope-review
                flag, so passing the CURRENT scope is how the owner rules "keep
                it as is".

        Returns:
            The updated node dict (as cognition_get_node) plus `reembed` — after a
            `scope` edit that dict carries scope_review: null, the cleared curation
            flag — or {"error": ...} if the node is absent (a teammate's personal constraint
            is absent to you), no editable field was given, the scope is invalid or
            not yours to change, or the node is a workflow (workflows are versioned by supersession: record
            a new one instead). Refused with `{"identity_required": true, ...}` when this checkout has
            no CONFIRMED identity, or its profile is missing role/seniority/
            reports_to — the payload names what to ask the human for and the
            `cognition_set_identity` call that fixes it. Nothing is written.
        """
        lc = get_lifespan(ctx)
        storage: CognitionStorage = lc["cognition_storage"]
        gate, _ = _gated_identity(storage)
        if gate is not None:
            return gate
        # WP-Wedge (AC4): same ready-then-read ordering as cognition_update_task.
        ready = _embeddings_ready(lc)
        return _update_node(
            storage,
            lc["cognition_embedding_storage"],
            lc["embedding_generator"] if ready else None,
            node_id=node_id,
            embeddings_ready=ready,
            summary=summary,
            detail=detail,
            context=context,
            severity=severity,
            scope=scope,
        )

    @dispatch_tool(mcp)
    def cognition_search(
        ctx: Context,
        query: str,
        node_type: str | None = None,
        limit: int = 10,
        project: str | None = None,
        exclude_people: str | None = None,
    ) -> dict[str, Any]:
        """Search PROJECT HISTORY (decisions, failures, discoveries, patterns) by natural language.

        This searches the cognition graph only. It does NOT search code —
        this server provides cognition history tools only.

        Read these two flags before trusting an empty or short result:
        ``semantic_unavailable: true`` (with ``reason``) means the search index
        could not serve this call — it is NOT "no history"; retry later or fall
        back to cognition_get_history. ``index_syncing: true`` means the index is
        still being (re)built from the journal, so results may be incomplete.

        Args:
            query: What you're looking for, e.g.:
                   - "caching strategy decisions"
                   - "what failed with the migration"
                   - "localization issues"
            node_type: Optional filter: decision, fail, discovery, assumption,
                       constraint, incident, pattern, episode, workflow, task, person
            limit: Max results (default: 10)
            project: None (default, home only), a tag/path (one loaded foreign
                     project), or "*" (all loaded projects — aggregate search).
                     Single-node tools reject "*"; this aggregate tool accepts it.
            exclude_people: Optional comma-separated email(s), casefolded before
                     matching (case doesn't matter), to drop hits authored by
                     (matched on the server-resolved identity stamp — never the
                     free-text `author` field; a node with no stamped identity is
                     NEVER excluded, since "unstamped" isn't the same as "matches").
                     No restriction on targets — any email, human or agent. Include
                     ONLY when the human explicitly asked to filter people out this
                     session; never volunteer it, and never persist it across calls
                     (there is no config knob for this — it is per-call only, by
                     design). Constraint/incident hits are NEVER excluded regardless
                     of author (same never-wipe doctrine as safety-critical nodes
                     elsewhere in this server) and never count toward excluded_count.

        WP-TC9: every hit's ranking is influenced by a penalty-only seniority/
        agent-origin multiplier — see `weight`/`weighted_score` below. This NEVER
        hides or wipes a lower-seniority or agent-authored hit (it can only push
        it lower in a tie, never drop it from `results`), never boosts anything
        above its raw score, and a constraint/incident hit is always exempt
        (multiplier 1.0, pinned) regardless of who authored it. `weight.seniority`
        is one of `owner`/`senior` (multiplier 1.0, no penalty), `mid` (0.95), or
        `junior` (0.9) when the author is a registered person; an agent-authored
        hit (`basis: "agent"`) is weighted 0.85 — always strictly below every
        seniority tier, so human authorship always outranks agent authorship.
        Applies to `cognition_search` only — `cognition_get_history`/
        `cognition_list_tasks`/session-start priming are unaffected.
        `cognition_get_workflow`'s internal top-1 match search shares this same
        path, so weighting can change WHICH workflow it resolves to, not just how
        a list of hits is ordered. Multi-project (`project="*"`/tag set) search
        weights each entry's hits using that entry's OWN person registry, then
        the final cross-project merge is ALSO sorted by `weighted_score` (not raw
        `score`) — weighting is not a home-only/single-project-only effect.

        Returns:
            {
              query: str,
              results: [                 # hits, sorted by weighted_score desc (see
                                         # WP-TC9 note above), at most `limit`
                {id, node_type, summary, score, weighted_score, weight, …, project: tag}
                                         # weighted_score = score * weight.multiplier
                                         # (score itself is the untouched raw
                                         # similarity — never mutated). weight is
                                         # {multiplier, seniority, from_agent, basis}
                                         # and is ALWAYS present, even when
                                         # multiplier == 1.0 (neutral) — never
                                         # silent. basis is one of:
                                         # "exempt:<node_type>" (constraint/incident,
                                         # pinned 1.0), "agent" (from_agent stamped
                                         # True), "human:<seniority>" (stamped +
                                         # registered person), "human:unregistered"
                                         # (stamped, no matching person node), or
                                         # "unverified" (no identity stamp at all).
                                         # "project" present when project != None.
                                         # NOTE the key is "node_type" here, NOT
                                         # "type" — cognition_get_node/get_history/
                                         # get_neighbors/get_edgeless_nodes/
                                         # get_uncurated_nodes all use "type" instead
                                         # (the raw graph attribute name). The two
                                         # families are NOT interchangeable; branching
                                         # code must key off the right one per tool.
                                         # A "document" hit ALSO gets "staleness":
                                         # "path_missing"/"size_changed" when a CHEAP
                                         # (stat-only, no file read) check catches
                                         # something -- omitted otherwise. Weaker than
                                         # cognition_get_document's full re-hash
                                         # "freshness" field (misses a same-size edit);
                                         # deliberately cheap so search cost doesn't
                                         # scale with re-reading every matched document.
                                         # conflicted: bool, ALWAYS present. True iff the
                                         # hit has >=1 incoming OR outgoing CONTRADICTS
                                         # edge (contradicts is one-way/arbitrary-direction
                                         # -- see superseded_by note below for why both
                                         # directions matter).
                                         # superseded_by: str | null, ALWAYS present. The
                                         # id of the newest node that SUPERSEDES this hit
                                         # (an incoming SUPERSEDES edge), or null if none.
                                         # Only the superseded (older) side gets a
                                         # non-null value -- the newer/resolving node
                                         # (outgoing supersedes) is null here, since it IS
                                         # the current version. Neither flag changes
                                         # weighted_score or result order (no ranking
                                         # change) -- results are NOT HEAD-filtered:
                                         # a superseded hit ranks exactly where its raw
                                         # similarity places it, same as any other hit;
                                         # these two fields are the ONLY marker that it
                                         # has been superseded or is in conflict.
                                         # conflicted_with: [{id, summary, author, reason}],
                                         # present ONLY when conflicted is true — names
                                         # each CONTRADICTS counterparty (both directions).
                                         # author is the counterparty's recorded_by name/
                                         # email when stamped, else its free-text author;
                                         # reason is the edge's own reason field, null if
                                         # the edge carries none (TC2, task 888a21f729dd).
                                         # scope: "personal" | "project", present on
                                         # constraint hits only. Teammates' personal
                                         # constraints never appear, so a personal hit
                                         # is always your own.
              ],
              count: int,                # len(results) — may be < total_found
              total_found: int,          # distinct live (post-exclusion) matches
                                         # discovered before search stopped widening —
                                         # ALWAYS present. A floor unless exhaustive.
              exhaustive: bool,          # true: total_found is the exact match count
                                         # (the store ran dry). false: search stopped at
                                         # `limit`/an internal cap — total_found is a
                                         # floor, more matches may exist unseen. So
                                         # count < total_found is possible even with no
                                         # exclusion in play; never read count alone as
                                         # "that's everything" without checking this.
              excluded_count: int,       # present iff exclude_people was passed AND
                                         # something was actually excluded (never a
                                         # silent 0) — distinct nodes dropped by the
                                         # filter for the query as searched.
              excluded_for: [str, ...],  # present under the same condition as
                                         # excluded_count — the casefolded emails that
                                         # were matched against.
              project_notes: {           # project != None only; omitted when empty
                "<tag>": {semantic_unavailable: true, reason: "<reason>",
                          detail?: str}
                          # or {confidence: "degraded (no model provenance for <tag>)"}
                          # and/or, for a loaded (non-home) project whose index
                          # is missing more than 2% of its nodes (and more than
                          # one): {index_incomplete: true,
                          # indexed_nodes: int, graph_nodes: int} -- its results
                          # are partial (exhaustive is false). The index is still
                          # being built by a session in that project, or the build
                          # stopped when that session closed; it resumes the next
                          # time a session opens there.
              },
              semantic_unavailable: true,  # project=None (home) only; present
              reason: "<reason>",          # when home's index cannot serve this
              detail: str,                 # search — results/count are [] / 0.
                                            # reason is a model_guard state
                                            # (dim-mismatch / model-mismatch: the
                                            # model or dims drifted from config),
                                            # "vector-store-unavailable" (the index
                                            # could not be opened, is damaged
                                            # beyond automatic repair, or failed
                                            # during this search — detail, when
                                            # present, says why; retry later), or
                                            # "newer-schema" (written by a newer
                                            # plugin version; update the plugin)
              index_syncing: true,         # present only while the index is
                                            # being (re)built from the journal
                                            # (first start on a new store, a
                                            # rebuild after damage, or a pass
                                            # that stopped early and waits to
                                            # resume): results may be
                                            # incomplete. Absent otherwise.
              confidence: "degraded — no model provenance in collection metadata "
                          "(pre-stamp)"  # project=None only; present when home
                                         # ran search but provenance is unverified
            }
            semantic_unavailable/project_notes[tag].semantic_unavailable means the
            index is guarded off or unavailable (no-index / dim-mismatch /
            model-mismatch / vector-store-unavailable / newer-schema) — that source
            contributed no hits, but it is NOT "no history." A confidence caveat
            means search ran but the model provenance couldn't be verified.
            Multi-project (project="*"/tag set): total_found is SUMMED across
            entries, exhaustive is AND-reduced (false if any searched entry didn't
            exhaust), and excluded_count/excluded_for likewise combine across
            entries — so post-merge truncation to `limit` can make count < total_found
            for reasons independent of exclusion or exhaustion (it's simply the
            per-project sum, then a single global top-`limit` cut).
        """
        err = require_embeddings(ctx)
        if err:
            return err

        # WP-2: validate node_type via the shared parser BEFORE branching, so an
        # invalid value returns the same clear error shape every other read tool
        # uses (not results:[] — an infra-shaped empty result must never look
        # like "no history"). Both the home and multi-project paths share this
        # one check since it runs before the branch.
        _, nt_err = _parse_searchable_node_type(node_type)
        if nt_err:
            return nt_err

        lifespan = get_lifespan(ctx)

        # Default (project=None): byte-identical to the pre-XP2 path UNLESS the
        # home collection's model/dims have drifted from current config (WP-2
        # item 2) — a dim-mismatch would otherwise be rejected inside the store
        # (vector_search logs and returns results:[]) and a
        # model-mismatch with equal dims wouldn't raise at all, just return
        # confidently-wrong nonsense scores. Both must be surfaced honestly
        # instead, mirroring the multi-project project_notes shape.
        if project is None:
            registry: LoadedProjects = lifespan["loaded_projects"]
            home_entry = registry.get(lifespan["config"].repo_path)
            if home_entry is not None and home_entry.model_guard in (
                "dim-mismatch", "model-mismatch",
            ):
                return {
                    "query": query,
                    "results": [],
                    "count": 0,
                    "semantic_unavailable": True,
                    "reason": home_entry.model_guard,
                }
            # WP-3 (8606d59905a5): close the node<->embedding drift for nodes
            # this process only ever saw via journal REPLAY (a teammate's
            # write) before running the search that would otherwise miss them
            # — see _reembed_replayed_nodes. dashboard/api.py's search handler
            # runs the SAME drain (it has its own request pipeline, bypassing
            # this wrapper) so both search surfaces stay covered. Foreign/
            # multi-project entries are deliberately NOT covered here (scoped
            # to home) — writing new embeddings into a project this process
            # doesn't own is a separate design question this WP doesn't settle.
            home_store = lifespan["cognition_embedding_storage"]
            store_reason = _store_unavailable_reason(home_store)
            if store_reason:
                return {
                    "query": query,
                    "results": [],
                    "count": 0,
                    "semantic_unavailable": True,
                    "reason": store_reason,
                }
            _reembed_replayed_nodes(
                lifespan["cognition_storage"],
                home_store,
                lifespan["embedding_generator"],
                index_syncing=_index_syncing(lifespan),
            )
            result = _search_cognition(
                lifespan["cognition_storage"],
                home_store,
                lifespan["embedding_generator"],
                query,
                node_type=node_type,
                limit=limit,
                exclude_people=exclude_people,
            )
            failed = _store_last_error(home_store)
            if failed:
                return {
                    "query": query,
                    "results": [],
                    "count": 0,
                    "semantic_unavailable": True,
                    "reason": "vector-store-unavailable",
                    "detail": failed,
                }
            if home_entry is not None and home_entry.model_guard == "unknown":
                result["confidence"] = (
                    "degraded — no model provenance in collection metadata (pre-stamp)"
                )
            if _index_syncing(lifespan):
                result["index_syncing"] = True
            return result

        # Multi-project path: resolve entries, embed once, fan out.
        entries, err2 = resolve_project(lifespan, project)
        if err2:
            return err2

        generator: EmbeddingGenerator = lifespan["embedding_generator"]

        # WP-3 redirect: the home-only drain above doesn't run for this branch,
        # but project="*" (or any tag set resolving to include home) searches
        # the SAME home collection through here — without this, a replayed
        # node is visible via a plain search but missing from an aggregate
        # one. Home only (entry.pinned) — foreign entries stay untouched
        # (read-only by design/contract, not a compromise; see XP1 lineage).
        home_in_fanout = next((e for e in entries if e.pinned), None)
        if home_in_fanout is not None and home_in_fanout.embeddings is not None:
            _reembed_replayed_nodes(
                home_in_fanout.storage, home_in_fanout.embeddings, generator,
                index_syncing=_index_syncing(lifespan),
            )

        limit = min(limit, 50)
        exclude_emails = _parse_exclude_people(exclude_people)
        query_embedding = generator.generate_query_embedding(query)

        all_results: list[dict[str, Any]] = []
        project_notes: dict[str, Any] = {}
        total_found = 0
        exhaustive = True
        excluded_count = 0

        for entry in entries:
            tag = entry.tag
            # WP-4 item 0 (454226d592e0): a dim/model-mismatched entry must be
            # treated the same whether it's a guarded foreign attach
            # (embeddings=None) or home (embeddings stays live so writes keep
            # landing — WP-2 — but must NOT be searched against a stale/wrong
            # stamp either). Foreign entries already null out embeddings on
            # mismatch (_load_project_core), so this check is a no-op there;
            # it only changes behavior for home.
            if entry.embeddings is None or entry.model_guard in (
                "dim-mismatch", "model-mismatch",
            ):
                project_notes[tag] = {
                    "semantic_unavailable": True,
                    "reason": entry.model_guard,
                }
                continue
            store_reason = _store_unavailable_reason(entry.embeddings)
            if store_reason:
                project_notes[tag] = {"semantic_unavailable": True, "reason": store_reason}
                continue
            search = _search_with_embedding(
                entry.storage, entry.embeddings, query_embedding, node_type, limit, exclude_emails
            )
            failed = _store_last_error(entry.embeddings)
            if failed:
                project_notes[tag] = {
                    "semantic_unavailable": True,
                    "reason": "vector-store-unavailable",
                    "detail": failed,
                }
                continue
            tag_results(search["results"], tag)
            all_results.extend(search["results"])
            total_found += search["total_found"]
            exhaustive = exhaustive and search["exhaustive"]
            excluded_count += search["excluded_count"]
            note: dict[str, Any] = {}
            if entry.model_guard == "unknown":
                note["confidence"] = "degraded — no model provenance in collection metadata (pre-stamp)"
            coverage = None if entry.pinned else _index_coverage(entry.storage, entry.embeddings)
            if coverage:
                note.update(coverage)
                exhaustive = False
            if note:
                project_notes[tag] = note

        # Sort merged results by weighted_score desc (WP-TC9: THE safety-critical
        # edit for multi-project weighting -- sorting by raw `score` here would
        # silently no-op weighting for project="*" while single-project still
        # worked, since each entry's already-weighted-and-sorted `results` would
        # just get re-sorted back to raw order). No cross-project id-dedup —
        # colliding ids across projects are two legitimately different nodes
        # (ids are content-derived, not namespaced). The project tag disambiguates.
        all_results.sort(key=lambda r: r.get("weighted_score") or 0.0, reverse=True)
        all_results = all_results[:limit]

        result: dict[str, Any] = {
            "query": query,
            "results": all_results,
            "count": len(all_results),
            "total_found": total_found,
            "exhaustive": exhaustive,
        }
        if exclude_emails and excluded_count > 0:
            result["excluded_count"] = excluded_count
            result["excluded_for"] = sorted(exclude_emails)
        if project_notes:
            result["project_notes"] = project_notes
        if home_in_fanout is not None and _index_syncing(lifespan):
            result["index_syncing"] = True
        return result

    @dispatch_tool(mcp)
    def cognition_get_chain(
        ctx: Context,
        node_id: str,
        max_depth: int = 5,
        direction: str = "outgoing",
        project: str | None = None,
    ) -> dict[str, Any]:
        """Get the reasoning chain from/to a cognition node via LED_TO edges.

        Follow the chain of causation: what led to what, or what was caused by what.

        Args:
            node_id: Starting node ID
            max_depth: Maximum depth to traverse (default: 5)
            direction: "outgoing" (what it led to) or "incoming" (what led to it)
            project: None (default, home) or tag/path for a loaded foreign project.
                     "*" is rejected — node ids are not project-namespaced; use a
                     specific tag.

        Returns:
            Nested structure showing the reasoning chain. When project is not None,
            also includes "project": tag at the top level.
        """
        lc = get_lifespan(ctx)
        err = _validate_direction(direction, ("outgoing", "incoming"))
        if err:
            return err
        if project is None:
            return get_reasoning_chain(lc["cognition_storage"], node_id, max_depth, direction)
        if project == "*":
            return {"error": 'project="*" is not supported for single-node tools'}
        entries, err2 = resolve_project(lc, project)
        if err2:
            return err2
        result = get_reasoning_chain(entries[0].storage, node_id, max_depth, direction)
        result["project"] = entries[0].tag
        return result

    @dispatch_tool(mcp)
    def cognition_get_superseded_chain(ctx: Context, node_id: str, project: str | None = None) -> dict[str, Any]:
        """Get a node's version history by following SUPERSEDES edges, newest first.

        When a decision is revised, the new node SUPERSEDES the old one; this walks
        that chain so you can see how the current version came to be (the chain
        `cognition_remove_node` recommends building but no tool could traverse).

        Args:
            node_id: The node to start from (typically the newest version).
            project: None (default, home) or tag/path for a loaded foreign project.
                     "*" is rejected — node ids are not project-namespaced.

        Returns:
            {"node_id": ..., "chain": [ ... ]} newest->oldest.
            When project is not None, also includes "project": tag.
        """
        lc = get_lifespan(ctx)
        if project is None:
            return {"node_id": node_id, "chain": get_superseded_chain(lc["cognition_storage"], node_id)}
        if project == "*":
            return {"error": 'project="*" is not supported for single-node tools'}
        entries, err = resolve_project(lc, project)
        if err:
            return err
        return {"node_id": node_id, "chain": get_superseded_chain(entries[0].storage, node_id), "project": entries[0].tag}

    @dispatch_tool(mcp)
    def cognition_get_workflow(
        ctx: Context,
        name_or_topic: str,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Find a workflow procedure by name or topic and return the current HEAD version.

        **Before starting any multi-step task**, search for an existing workflow first —
        this is the primary retrieval trigger for workflow nodes.

        Performs a type-filtered semantic search for ``workflow`` nodes matching the
        query, then resolves the top hit to the current HEAD (in case an old version
        was matched) via SUPERSEDES edges. Returns the full procedure body, the full
        version chain, and which node was matched.

        This lookup inherits ``cognition_search``'s seniority/agent-origin weighting
        (WP-TC9) — on a graph with more than one similarly-matching workflow, the
        one attributed to a more senior / human author can win the match over an
        agent-authored or less-senior near-duplicate at a comparable raw score. This
        can change WHICH procedure ``matched``/``head`` resolves to, not just its
        rank in a list — a deliberate, unconditional consequence of the same ranking
        change, not a graph-shape-dependent edge case.

        Args:
            name_or_topic: Name or topic of the procedure (e.g. "deploy to production",
                           "onboard a new engineer", "run the release process").
            project: None (default, home) or tag/path for a loaded foreign project.
                     "*" is rejected — node ids are not project-namespaced.

        Returns:
            {"head": {id, type, summary, detail, ...}, "chain": [...], "matched": <id>}
            or {"error": "...", "status": ...} on failure. Two DISTINCT failure
            shapes -- check "status" before concluding no workflow exists:
            status="loading_embeddings" means the embedding index isn't ready yet
            (retry in a few seconds; this is NOT evidence that no workflow exists);
            no "status" key (a plain "No workflow found matching: ..." message)
            means the search ran and genuinely found nothing.
            "chain" is the full version history (newest first) via get_superseded_chain.
            When project is not None, also includes "project": tag.
        """
        lc = get_lifespan(ctx)
        err = require_embeddings(ctx)
        if err:
            if err.get("status") == "loading_embeddings":
                return {
                    "error": (
                        "Embeddings are still loading -- retry cognition_get_workflow "
                        "in a few seconds. This does NOT mean no workflow exists; the "
                        "graph/history tools work now, but workflow lookup needs the "
                        "embedding index to be ready."
                    ),
                    "status": "loading_embeddings",
                }
            return err
        if project == "*":
            return {"error": 'project="*" is not supported for single-node tools'}

        if project is None:
            storage = lc["cognition_storage"]
            embedding_storage: VectorStore = lc["cognition_embedding_storage"]
            generator: EmbeddingGenerator = lc["embedding_generator"]
            project_tag = None
        else:
            entries, err2 = resolve_project(lc, project)
            if err2:
                return err2
            storage = entries[0].storage
            if entries[0].embeddings is None:
                return {"error": "No vector index for the requested project"}
            embedding_storage = entries[0].embeddings
            generator = lc["embedding_generator"]
            project_tag = entries[0].tag

        query_embedding = generator.generate_query_embedding(name_or_topic)
        results = _search_with_embedding(
            storage, embedding_storage, query_embedding,
            node_type=CognitionNodeType.WORKFLOW.value, limit=1,
        )["results"]
        if not results:
            return {"error": f"No workflow found matching: {name_or_topic!r}"}

        matched_id = results[0]["id"]
        head_id = get_workflow_head(storage, matched_id)
        head_node = storage.get_node(head_id)
        if not head_node:
            return {"error": f"Workflow head node not found: {head_id}"}

        chain = get_superseded_chain(storage, head_id)
        result: dict[str, Any] = {
            "head": {"id": head_id, **head_node},
            "chain": chain,
            "matched": matched_id,
        }
        if project_tag:
            result["project"] = project_tag
        return result

    @dispatch_tool(mcp)
    def cognition_get_incident_resolution(ctx: Context, node_id: str, project: str | None = None) -> dict[str, Any]:
        """Get an incident node plus everything that resolved or relates to it.

        Follows RESOLVED_BY edges to the fixes, LED_TO edges to follow-on nodes
        (discoveries/decisions the incident produced), and incoming CONTRADICTS
        edges, so the full story of an incident is one call.

        Args:
            node_id: The incident node.
            project: None (default, home) or tag/path for a loaded foreign project.
                     "*" is rejected — node ids are not project-namespaced.

        Returns:
            {id, ...incident fields, resolutions: [...], discoveries: [...],
            contradictions: [...]} or {"error": ...} if the node is absent.
            When project is not None, also includes "project": tag.
        """
        lc = get_lifespan(ctx)
        if project is None:
            return get_incident_resolution(lc["cognition_storage"], node_id)
        if project == "*":
            return {"error": 'project="*" is not supported for single-node tools'}
        entries, err = resolve_project(lc, project)
        if err:
            return err
        result = get_incident_resolution(entries[0].storage, node_id)
        result["project"] = entries[0].tag
        return result

    @dispatch_tool(mcp)
    def cognition_get_history(
        ctx: Context,
        context_term: str | None = None,
        node_type: str | None = None,
        limit: int = 20,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Get cognition nodes by context area, type, or recency.

        If context_term is provided, filters nodes whose context fields match
        (case-insensitive substring). Otherwise returns the most recent nodes.

        Args:
            context_term: Optional term to search in context fields (file paths, topics)
            node_type: Optional filter: decision, fail, discovery, assumption,
                       constraint, incident, pattern, episode, workflow, task, person
            limit: Max results (default: 20)
            project: None (default, home), tag/path, or "*" (all loaded projects).
                     Aggregate tool: "*" fans across all loaded projects, merges and
                     sorts rows, adds "projects_queried" to the envelope. Each row
                     carries a "project" tag when project != None.

        Returns:
            {context_term, results: [{id, type, summary, detail, ...}, ...], count,
             total_found, exhaustive}
            Each node's kind is keyed "type" (the raw graph attribute name) — NOT
            "node_type" like cognition_search's results use; the two shapes are
            not interchangeable. (The node_type ARG above, which filters the
            query, is unrelated to this key-naming asymmetry.)
            total_found (WP-TC10 M4): the exact count of matching nodes before the
            `limit` slice — count may be < total_found when more matched than
            `limit` allowed through. exhaustive is always True here (a full
            structural scan sees everything, unlike cognition_search's adaptive
            vector search, where it can be a floor).
            When project != None also includes "projects_queried": [tag, …];
            total_found is then SUMMED across entries (so post-merge truncation
            to `limit` can make count < total_found for reasons unrelated to any
            single entry's own completeness).
            (Cross-project note: for semantic search over projects use
            cognition_search with project=; get_history is structural only.)
            NOT HEAD-filtered and carries no conflict/supersession marker: unlike
            cognition_search's results (which carry `conflicted`/`superseded_by`,
            WP-SearchFlags), a superseded row here shows exactly like its
            resolving replacement, and a contradicted node shows exactly like a
            clean one — recency/context alone decide what's returned. Use
            cognition_search when you need those signals.
        """
        lc = get_lifespan(ctx)

        nt, err = _parse_node_type(node_type)
        if err:
            return err

        # Default path: byte-identical to pre-XP2 except for the added total_found/
        # exhaustive keys (WP-TC10 M4).
        if project is None:
            storage: CognitionStorage = lc["cognition_storage"]
            if context_term:
                full = get_history_for_context(storage, context_term, nt)
                results = full[:limit]
                total_found = len(full)
            else:
                results, total_found = storage.get_recent_nodes(
                    limit=limit, node_type=nt, with_total=True
                )
            return {
                "context_term": context_term,
                "results": results,
                "count": len(results),
                "total_found": total_found,
                "exhaustive": True,
            }

        # Multi-project path.
        entries, err2 = resolve_project(lc, project)
        if err2:
            return err2

        all_results: list[dict[str, Any]] = []
        total_found = 0
        for entry in entries:
            if context_term:
                full = get_history_for_context(entry.storage, context_term, nt)
                rows = full[:limit]
                total_found += len(full)
            else:
                rows, entry_total = entry.storage.get_recent_nodes(
                    limit=limit, node_type=nt, with_total=True
                )
                total_found += entry_total
            tag_results(rows, entry.tag)
            all_results.extend(rows)

        all_results.sort(key=lambda n: n.get("timestamp") or "", reverse=True)
        all_results = all_results[:limit]

        return {
            "context_term": context_term,
            "results": all_results,
            "count": len(all_results),
            "total_found": total_found,
            "exhaustive": True,
            "projects_queried": [e.tag for e in entries],
        }
