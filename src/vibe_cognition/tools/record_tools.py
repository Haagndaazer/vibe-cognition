"""MCP tools that write the graph's own records: nodes, tasks, people, machine facts."""

from typing import Any

from fastmcp import Context

from ..cognition import SENIORITY_LEVELS, CognitionNodeType, CognitionStorage
from ..cognition.identity import (
    identity_write_path,
    is_valid_email,
    resolve_identity,
    write_confirmed_identity,
)
from ..cognition.people_facts import fold_email
from ..cognition.profiles import NO_MANAGER
from .cognition_tools import (
    _add_task,
    _clear_env_facts,
    _delete_env_fact,
    _embeddings_ready,
    _get_person,
    _list_env_facts,
    _list_people,
    _list_tasks,
    _record_node,
    _register_person,
    _remove_person,
    _set_env_fact,
    _update_person,
    _update_task,
)
from .dispatch import dispatch_tool
from .utils import get_lifespan


def register_record_tools(mcp) -> None:

    @dispatch_tool(mcp)
    def cognition_record(
        ctx: Context,
        node_type: str,
        summary: str,
        detail: str,
        context: str,
        author: str,
        severity: str | None = None,
        references: str | None = None,
        from_agent: bool = True,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Record a cognition node — a decision, failure, discovery, or other knowledge artifact.

        Use this to capture important context from conversations: what was decided,
        what failed, what was discovered, assumptions made, constraints identified,
        production incidents, generalized patterns, or episode summaries of completed work.

        TRIGGERING CURATION IS YOUR JOB for semantic relationships — never author them
        yourself. Deterministic edges are created automatically ONLY for these pairs
        sharing a reference: entity<->episode (part_of, ANY shared ref) and
        document-involving pairs sharing a doc: ref specifically (entity<->document ->
        part_of; document<->episode -> relates_to). entity<->entity, episode<->episode,
        and document<->document NEVER auto-link even when they share a reference — e.g.
        two decisions citing the same commit stay fully edgeless until curation. All
        semantic relationships (led_to, supersedes, contradicts, resolved_by,
        relates_to) and any entity<->entity linking are NOT created for you — after
        recording, run the `/vibe-curate` skill to link the new nodes.

        NODE TYPES:
        - decision: A choice between alternatives. Include what was chosen AND rejected.
        - fail: Something that didn't work — a build, test, approach, or assumption.
        - discovery: A non-obvious finding about the codebase, library, API, or platform.
        - assumption: Something being assumed true without full verification.
        - constraint: A hard limitation, scoping exclusion, or defensive rule. PERSONAL by
          default — see CONSTRAINT SCOPE below.
        - incident: A production problem that affected users.
        - pattern: A reusable approach, convention, or anti-pattern.
        - workflow: A reusable multi-step procedure or runbook — record when you work
          one out (deploy steps, onboarding, a debugging recipe you'll repeat). See
          WORKFLOW NODES below for its shape; retrieve with cognition_get_workflow.
        - episode: Full narrative of completed work (Linear task, feature, debugging session).
          Create when a body of work is done — the episode captures the full story.
        - document: NOT creatable here — use cognition_store_document instead (it handles
          content storage, a text sidecar for search, and embedding together).
        - task: NOT creatable here — use cognition_add_task instead (it resolves the git
          creator server-side and seeds the status/transition lifecycle; passing
          node_type="task" to this tool returns an error, not a task node).
        - person: NOT creatable here — use cognition_register_person instead (a HUMAN
          identity, never an agent; passing node_type="person" here returns an error).

        ENTITY NODES (decision, fail, discovery, assumption, constraint, incident, pattern):
        - summary: MAX 250 chars. Write like a commit message — scannable at a glance.
          Good: "Double-filter bug: query filters by language after opening language-scoped box"
          Bad: "Found a bug in the data source that was causing data to be invisible"
        - detail: 1-3 sentences of rationale. NOT the full story — that goes in an episode.

        CONSTRAINT SCOPE (constraint only):
        - personal (the default): how THIS person works or wants agents to behave for
          them — their preferences, "don't touch prefabs, I author those", "ask before
          committing", tool and style choices. Visible ONLY to the person who recorded
          it: teammates cannot find it through any tool.
        - project: true for anyone working in the repo regardless of who they are — a
          platform/API limit, a build or ship rule, a client or contract requirement, a
          "this breaks if you do X" fact, a security rule. Visible to everyone.
        - Unsure (e.g. "agents make no engine-side changes in this project" could be one
          person's preference or a studio rule)? ASK the human before recording and pass
          their answer. If the human cannot be asked, record it personal.
        - A wrongly personal project rule hides it from teammates who could break it;
          a wrongly project preference makes teammates' agents obey it. Both are bugs.

        WORKFLOW NODES (workflow):
        - A step-by-step procedure stored as ONE cohesive unit. Verbose detail (like episode).
          Versioned by supersession: to update, record a NEW workflow node with the full revised
          procedure, then run /vibe-curate (the only path that writes the `supersedes` edge);
          never edit in place (update_node is blocked).
          summary: Brief title of the procedure. detail: The full procedure, verbose.
          Retrieve with cognition_get_workflow(name_or_topic) to resolve to the current HEAD.

        EPISODE NODES:
        - summary: Brief title of the work (e.g., "LL-298: Data wipe investigation and fix")
        - detail: Full narrative — everything that happened. Verbose is fine for episodes.

        IMPORTANT:
        - Always include references (issue numbers, PR numbers, commit hashes) so nodes
          link to their episode via deterministic part_of matching, and so /vibe-curate
          has the signal to relate them. Format: "issue:LL-298,pr:97,commit:abc123"
        - Use both file paths AND topical terms in context for better discovery.
        - author should be the current git user name.

        Args:
            node_type: One of: decision, fail, discovery, assumption, constraint, incident, pattern, episode, workflow
            summary: Short description (max 250 chars for entities, brief title for episodes/workflows)
            detail: Brief rationale for entities (1-3 sentences), or full narrative for episodes
            context: Related code areas, file paths, AND topical terms (comma-separated).
                     Example: "flashcard_local_datasource.dart, HiveService, data migration, LL-298"
            author: The current git user name (e.g., "Colton Dyck"), OR an agent
                    persona name — this is UNVERIFIED client-supplied text, not
                    server-resolved like a task's created_by. Use whichever
                    identifies who/what actually made this call.
            severity: Optional priority — critical, high, normal, low
            references: Optional external refs, comma-separated. Include issue/PR/commit refs
                        so nodes link to their episode (part_of) and /vibe-curate can
                        relate them. Example: "issue:LL-298,pr:97"
            from_agent: Set false ONLY when the human explicitly dictated/authored this
                        content themselves; default true ("via agent" — an undeclared
                        write is honestly agent-originated). When in doubt, leave the
                        default. Stamped as metadata.from_agent; surfaced in search
                        results and get_node (missing on pre-existing nodes, never
                        coerced to true/false).
            scope: constraint only — "personal" (default when omitted) or "project".
                   See CONSTRAINT SCOPE above. Passing it for any other node type is
                   an error.

        Returns:
            {id, type, summary, timestamp} plus, for constraints, scope
            ("personal" | "project", as stored) and, when non-empty,
            deterministic_edges_created (int) and, for episode nodes only,
            possible_duplicate_of: [node_id, ...] — other EXISTING episodes
            sharing a reference with this one (e.g. two clones each minted an
            episode for the same commit). The node is still created either
            way (never silently reused/merged) — this is a visibility signal
            for a curator to reconcile manually (e.g. supersedes), not an
            automatic dedup.
            Refused with `{"identity_required": true, ...}` when this checkout has
            no CONFIRMED identity, or its profile is missing role/seniority/
            reports_to — the payload names what to ask the human for and the
            `cognition_set_identity` call that fixes it. Nothing is written.
            {"error": ...} for an invalid scope or a scope on a non-constraint.
        """
        try:
            nt = CognitionNodeType(node_type)
        except ValueError:
            valid = [e.value for e in CognitionNodeType]
            return {"error": f"Invalid node_type '{node_type}'. Valid: {valid}"}

        # task is NOT creatable here: cognition_record would stamp a client-supplied
        # author and skip the server-resolved created_by + status/transition seeding,
        # producing an un-attributed, lifecycle-less task (defeats the type's purpose).
        # Redirect to the dedicated tool. (This is the WRITE path only — `task` stays a
        # valid filter on the read tools.)
        if nt == CognitionNodeType.TASK:
            return {
                "error": (
                    "Use cognition_add_task to create a task node — it resolves the "
                    "creator server-side from the confirmed identity and seeds the lifecycle (status, transitions). "
                    "cognition_record cannot attribute or track a task."
                )
            }
        if nt == CognitionNodeType.PERSON:
            return {
                "error": (
                    "Use cognition_register_person to create a person node — it enforces "
                    "the one-node-per-email invariant and the human-only identity rule. "
                    "cognition_record cannot create or attribute a person node."
                )
            }

        return _record_node(
            ctx, nt, summary, detail, context, author,
            severity, references, from_agent, scope,
        )

    @dispatch_tool(mcp)
    def cognition_add_task(
        ctx: Context,
        summary: str,
        detail: str,
        context: str,
        priority: str = "normal",
        owner: str | None = None,
        parent_id: str | None = None,
        references: str | None = None,
        from_agent: bool = True,
        assigned_to_email: str | None = None,
    ) -> dict[str, Any]:
        """Create a trackable task — open, actionable work, attributed to the git user.

        Use this to file work that should be tracked and shown back at session start
        (open tasks inject into context like constraints do) and listed via
        `cognition_list_tasks`. The graph itself becomes the backlog. **Before picking
        up work, check open tasks with `cognition_list_tasks` first.**

        The creator is resolved SERVER-SIDE from the confirmed identity for this checkout — there is no
        `created_by` parameter and a client value cannot override it. The task is seeded
        `status="open"` with an initial transition record; change it later with
        `cognition_update_task` (the only path to status/owner/parent/assignment edits).

        Tasks support an ARBITRARY-depth parent hierarchy: pass `parent_id` to file this
        task under a parent task/epic (it creates an explicit `part_of` edge). Re-parent
        later with `cognition_update_task(parent_id=...)`. Tasks are fully curatable —
        run `/vibe-curate` to link a task `relates_to` the decision/pattern it implements,
        or `resolved_by`/`led_to` the episode that closed it.

        Args:
            summary: Short task title (like a commit message — scannable).
            detail: Description / acceptance criteria.
            context: Comma-separated related files AND topical terms.
            priority: critical | high | normal | low (default normal). Stored as the
                node's `severity`, so it sorts the backlog and the session-start view.
            owner: Optional free-text "who's on it" (distinct from the git creator).
                Never identity-matched — use `assigned_to_email` for that.
            parent_id: Optional id of a parent task to file this under (must be a task).
            references: Optional external refs (issue/PR/commit), comma-separated.
            from_agent: Set false ONLY when the human explicitly dictated/authored this
                        task themselves; default true. When in doubt, leave the default.
                        Stamped as metadata.from_agent; surfaced in list_tasks rows and
                        search results (missing on pre-existing tasks, never coerced).
            assigned_to_email: Optional email of the human this task is directed AT —
                        distinct from `owner` (free text, unmatched): this is casefolded
                        and IS identity-matched, so it surfaces under the assignee's "Your
                        Open Tasks" at their next session start even if they neither
                        created nor claimed it. Need not be a registered person yet
                        (dangling is legal). Blank/whitespace is treated as NOT PROVIDED
                        (seeds nothing) — pass a real email or omit entirely; there is no
                        "assign at creation to nobody" case. Anyone may assign anyone (no
                        ACL); the append-only `metadata.assignments` audit trail is the
                        control. Assigning is NOT claiming — the assignee still claims it
                        via `cognition_update_task(status="in_progress")` to accept it.

        Returns:
            The created task node ({id, type, summary, ..., severity, metadata}) or
            {"error": ...} if the parent is missing or not a task.
            Refused with `{"identity_required": true, ...}` when this checkout has
            no CONFIRMED identity, or its profile is missing role/seniority/
            reports_to — the payload names what to ask the human for and the
            `cognition_set_identity` call that fixes it. Nothing is written.
        """
        return _add_task(
            ctx, summary, detail, context,
            priority=priority, owner=owner, parent_id=parent_id, references=references,
            from_agent=from_agent, assigned_to_email=assigned_to_email,
        )

    @dispatch_tool(mcp)
    def cognition_list_tasks(
        ctx: Context,
        status: str | None = None,
        priority: str | None = None,
        owner: str | None = None,
        parent_id: str | None = None,
        include_done: bool = False,
        exclude_people: str | None = None,
    ) -> dict[str, Any]:
        """List the project's tasks — the backlog view. **Check this before picking up work.**

        Returns open tasks (home project only) sorted by priority then recency, grouped
        as a tree via a `depth` field per row (a child whose parent is filtered out or
        deleted is shown ungrouped at depth 0). By default `done`/`cancelled` tasks are
        EXCLUDED — pass `include_done=true` to see them.

        Args:
            status: Optional exact-status filter (open | in_progress | blocked | done |
                    cancelled). Filtering for a closed status implies include_done.
            priority: Optional priority filter (critical | high | normal | low).
            owner: Optional owner filter (exact match on the free-text owner).
            parent_id: Optional — only tasks whose direct parent is this id.
            include_done: Include done/cancelled tasks (default False).
            exclude_people: Optional comma-separated email(s), casefolded before
                    matching (case doesn't matter), to drop tasks CREATED BY
                    (matched on the server-resolved `created_by.email` stamp —
                    never the free-text `owner`; a task with no stamped creator is
                    NEVER excluded). No restriction on targets — any email, human or
                    agent. Include ONLY when the human explicitly asked to filter
                    people out this session; never volunteer it, and never persist
                    it across calls (no config knob for this — per-call only).

        Returns:
            {"tasks": [{id, summary, status, priority, owner, parent_id, created_by,
             claimed_by, timestamp, depth, from_agent, assigned_to, claimed_at}, ...],
             "count": N, "excluded_count": N, "excluded_for": [str, ...]} or
            {"error": ...} for a bad status. `assigned_to` is the casefolded email of
            who this task is directed at, or None when unassigned (WP-TC8; never
            coerced, same convention as `from_agent`). `claimed_by` is the
            server-resolved identity dict ({name, email}) of whoever last claimed the
            task (the ->in_progress transition), or None if it has never been claimed
            — it stays stamped after the task closes (done/cancelled), it is NOT
            cleared, so it reads as claim HISTORY, not "is this task live right now"
            (use `status` for that). Check it BEFORE calling
            cognition_update_task(status="in_progress") on a task that
            looks active, so you learn who holds it without triggering a collision.
            `claimed_at` is the ISO timestamp of that claim (the latest ->in_progress
            transition), or None to match. `excluded_count`/`excluded_for` are present
            iff `exclude_people` was passed AND something was actually excluded
            (never a silent 0).
        """
        storage: CognitionStorage = get_lifespan(ctx)["cognition_storage"]
        return _list_tasks(
            storage,
            status=status, priority=priority, owner=owner,
            parent_id=parent_id, include_done=include_done,
            exclude_people=exclude_people,
        )

    @dispatch_tool(mcp)
    def cognition_update_task(
        ctx: Context,
        node_id: str,
        status: str | None = None,
        priority: str | None = None,
        owner: str | None = None,
        summary: str | None = None,
        detail: str | None = None,
        parent_id: str | None = None,
        note: str | None = None,
        assigned_to_email: str | None = None,
    ) -> dict[str, Any]:
        """Update a task's lifecycle or narrative in place — the ONLY path to
        status/owner/parent/assignment.

        `cognition_update_node` cannot edit a task's `status`/`owner`/`parent`/
        `assigned_to` (they live in metadata, which it doesn't touch) — use this tool
        for those. A status change is validated against the legal transitions and
        appends an audit record (`{status, at, by}`, `by` server-resolved) to the
        task's transition log. Reopening a done/cancelled task (→ open) is allowed.

        Re-parent via `parent_id`: a task id moves this task (and its whole subtree) under
        that task; the empty string `""` DETACHES it to top-level; omitting it leaves the
        parent unchanged. A move is rejected if it would create a cycle or the target is
        not a task.

        Assign via `assigned_to_email` (WP-TC8): sets/reassigns who this task is directed
        at, distinct from claiming it (`status="in_progress"`) — assigning does not claim,
        and claiming does not require being the assignee. Anyone may assign anyone (no
        ACL; the audit trail is the control). Each EFFECTIVE change (a genuinely different
        email, casefolded) appends one entry to `metadata.assignments`; resubmitting the
        SAME email is a no-op and appends nothing.

        Claim collisions (WP-TC4) almost never block — the one exception is claiming
        (`status="in_progress"`) a task someone ELSE is actively working via
        `blocked → in_progress`: that specific call REQUIRES `note=`, or it is rejected
        (before any change is made) with an error naming who holds it and since when;
        retry the same call with a `note=` and it succeeds. "Actively working" means
        `in_progress` or `blocked` with a DIFFERENT person's `claimed_by` on it — a task
        that's `open`/`done`/`cancelled` has no live claim, so reclaiming it never needs a
        note. Every other case where you brush up against someone else's claim succeeds
        outright and adds a `claim_warning` to the response instead of blocking: passing
        `status="in_progress"` again on a task that's already `in_progress` under someone
        else either takes it over (if you also pass `note=`) or just pokes it without
        taking over (if you don't) — read the warning either way. Reopening someone
        else's `done`/`cancelled` task also warns, no note needed. You'll never see a
        `claim_warning` for your own actions, and identity that can't be verified (a
        blank git-config email on either side) is treated as unknown, not as a match.

        Args:
            node_id: The task to update (must be a task node).
            status: New status (open | in_progress | blocked | done | cancelled).
            priority: New priority (critical | high | normal | low).
            owner: New owner free-text; pass "" to clear it.
            summary: New title, if changing.
            detail: New description, if changing.
            parent_id: Re-parent target task id, "" to detach, or omit for no change.
            note: Annotation recorded on the status transition (with `status`).
                REQUIRED to take over someone else's live claim via `blocked ->
                in_progress`, or via `status="in_progress"` again on an already
                `in_progress` task someone else holds — omitted, those two calls
                either reject (the first) or succeed without taking over and warn
                (the second). Optional everywhere else.
            assigned_to_email: New assignee's email; pass "" to unassign; omit (None,
                the default) for no change. Casefolded before comparing/storing, so
                resubmitting the SAME email in a different case is still a no-op, not
                a reassignment. Need not be a registered person yet.

        Returns:
            The updated task node plus `reembed` ("done" | "deferred"), or {"error": ...}
            for a non-task id, an illegal/invalid status, a bad re-parent, no fields
            (including a same-email assignment resubmission with nothing else to apply),
            or a note-less takeover of someone else's live `blocked -> in_progress` claim
            (the one case above that blocks — naming the current claimant and how long
            they've held it; retry with `note=` to proceed).

            A successful response MAY additionally carry `claim_warning` — read it when
            present, it means the call touched someone else's claim or closure:
            `{"kind": "claim_collision" | "takeover_note_required" | "reopen",
            "claimant": {"name", "email"}, "claimed_at": "<ISO timestamp or null>",
            "assigned_to": "<email, present only if the task is currently assigned>",
            "message": "<human-readable summary>"}`. `claim_collision` = you took the
            claim over (with a note). `takeover_note_required` = the task is still
            claimed by `claimant`; your call went through but did NOT take it over —
            resend with `note=` if you meant to. `reopen` = you reopened a task someone
            else closed (`claimant` is who closed it, `claimed_at` is when). The key is
            simply absent when nothing collision-worthy happened (your own task, a
            released claim, or an identity that couldn't be verified on either side).

            Refused with `{"identity_required": true, ...}` when this checkout has
            no CONFIRMED identity, or its profile is missing role/seniority/
            reports_to — the payload names what to ask the human for and the
            `cognition_set_identity` call that fixes it. Nothing is written.
        """
        lc = get_lifespan(ctx)
        # WP-Wedge (AC4): compute readiness FIRST, then read the generator
        # conditioned on it — never subscript the generator before the ready
        # check (that ordering is exactly the stale-capture pattern AC4 closes).
        ready = _embeddings_ready(lc)
        return _update_task(
            lc["cognition_storage"],
            lc["cognition_embedding_storage"],
            lc["embedding_generator"] if ready else None,
            node_id=node_id,
            embeddings_ready=ready,
            status=status,
            priority=priority,
            owner=owner,
            summary=summary,
            detail=detail,
            parent_id=parent_id,
            note=note,
            assigned_to_email=assigned_to_email,
        )

    @dispatch_tool(mcp)
    def cognition_set_identity(
        ctx: Context,
        name: str,
        email: str,
        role: str | None = None,
        seniority: str | None = None,
        reports_to: str | None = None,
    ) -> dict[str, Any]:
        """Confirm WHO IS DRIVING this checkout AND who they are, so memories are
        attributable. This is the one call that unblocks a refused write.

        Call it when any write was refused with `identity_required: true`, or when
        the human says the attributed identity is wrong. Every value MUST come from
        the human — ASK THEM. Never guess, never infer from a role title, never
        invent an address, and never pass an agent's own name: memories are always
        attributed to the person, not the assistant.

        Two things are written, and BOTH are needed before any graph write is
        allowed:
          * the pointer — `.cognition/local/identity.json`, MACHINE-LOCAL and
            ignored by git and SVN. It says who is driving THIS working copy, not
            who exists on the project, so it is never committed. It is bound to
            the machine, OS account and checkout folder it was written in; a copy
            found anywhere else (committed by mistake, or a copied project) is
            refused as NOT trusted, and this call re-binds it for whoever is
            ACTUALLY driving -- which may not be the person the old file named.
          * the profile — `.cognition/people/<email>.profile.jsonl`, COMMITTED and
            shared with the team. Name, role, seniority and reporting line live
            here; this is what makes search ranking, the manager chain and the
            prime digest work for a real person instead of an anonymous address.
            On an SVN working copy a new profile file is scheduled for addition
            automatically (never committed), so the user's next commit includes it.

        Passing the pointer alone leaves the gate closed. The return says exactly
        what is still missing in `missing_profile_fields`, and `write_ready` is
        True only once nothing is. The three profile arguments are optional purely
        so that (a) re-pointing an already-profiled identity at a new address does
        not force the human to re-answer everything, and (b) a teammate whose
        manager already pre-registered them is finished in one call.

        This overrides whatever identity the checkout's own VCS suggests for all future writes, so it
        is also the fix when the VCS reports a personal address and the human wants
        their work address used instead. It does NOT change attribution already
        recorded — for that, see the `vibe-cognition-remap-identity` CLI.

        Args:
            name: The human's display name, e.g. "Ada Lovelace". Must not be blank.
            email: Their email address, lowercased on write. Must be a real
                address (local@domain.tld); a bare login name is rejected.
            role: Free text, whatever the human says — "backend engineer", "PM",
                "lead". Omit to leave an existing value untouched.
            seniority: Exactly one of owner | senior | mid | junior. A CLOSED SET:
                present the four options to the human and ask which applies. Do
                NOT infer a tier from a role title or an offhand word. Anything
                else is rejected and nothing is written.
            reports_to: The manager's EMAIL, or the literal "nobody". "nobody" is
                valid and is the expected answer on a solo project — say so when
                asking, or a solo user has no true answer to give. A manager's
                NAME is rejected, because the reporting chain is resolved by
                email and a name would break it silently.

        Returns:
            On success: `{"name": str, "email": str, "source": "confirmed",
            "path": str, "previous": dict | None, "registered_person": bool,
            "profile": dict, "profile_written": list[str],
            "profile_skipped": list[str], "missing_profile_fields": list[str],
            "write_ready": bool}`.
            `previous` is the identity in effect before this call (with its own
            `source`, plus `identity_file: {status, name, email, machine,
            mismatch}` when an untrusted identity file was present); `registered_person` reports whether a legacy person node
            also exists for this email; `profile_written` lists the fields this
            call actually appended and `profile_skipped` those already at that
            value (re-confirming an unchanged profile writes nothing);
            `missing_profile_fields` is empty exactly when `write_ready` is True.
            On failure: `{"error": str}` — a blank name, an address that is not a
            valid email, a seniority outside the closed set, or a `reports_to`
            that is neither "nobody" nor an email. Nothing is written on failure.

        Suggestions for what to offer the human are in the refusal payload of any
        gated write (`suggestions`), drawn from the checkout's own VCS (git config
        on git, cached SVN usernames on Subversion). Treat them as candidates to
        confirm, never as answers.
        """
        lc = get_lifespan(ctx)
        storage: CognitionStorage = lc["cognition_storage"]
        cognition_dir = storage.cognition_dir

        if seniority is not None and seniority.strip().casefold() not in SENIORITY_LEVELS:
            return {
                "error": (
                    f"seniority must be one of {list(SENIORITY_LEVELS)}, got "
                    f"{seniority!r} — ASK THE HUMAN which of those four applies; "
                    "do not infer it from their role title"
                )
            }
        if reports_to is not None:
            folded_mgr = fold_email(reports_to)
            if folded_mgr != NO_MANAGER and not is_valid_email(reports_to):
                return {
                    "error": (
                        "reports_to must be the manager's EMAIL or the literal "
                        f'"{NO_MANAGER}", got {reports_to!r} — the reporting chain '
                        "is resolved by email, so a name cannot be stored here. Ask "
                        "the human for their manager's address, or "
                        f'"{NO_MANAGER}" if they have none (normal on a solo project).'
                    )
                }

        previous = resolve_identity(cognition_dir.parent, cognition_dir)
        written = write_confirmed_identity(cognition_dir, name, email)
        if "error" in written:
            return written

        folded = written["email"]
        fields: dict[str, Any] = {"name": written["name"], "email": folded}
        if role is not None:
            fields["role"] = role.strip()
        if seniority is not None:
            fields["seniority"] = seniority.strip().casefold()
        if reports_to is not None:
            fields["reports_to"] = fold_email(reports_to)
        by = {"name": written["name"], "email": folded}
        try:
            profile_result = storage.set_profile_fields(folded, fields, by)
        except OSError as exc:
            return {
                "error": (
                    f"identity confirmed as {folded}, but the profile could not be "
                    f"written: {exc}. The gate stays closed until it is — retry this "
                    "same call once the problem is fixed."
                )
            }
        if "error" in profile_result:
            return profile_result

        missing = storage.profile_missing_required(folded)
        return {
            **written,
            "source": "confirmed",
            "path": str(identity_write_path(cognition_dir)),
            "previous": previous,
            "registered_person": storage.roster().is_registered(folded),
            "profile": storage.get_profile(folded) or {},
            "profile_written": profile_result.get("written", []),
            "profile_skipped": profile_result.get("skipped", []),
            "missing_profile_fields": missing,
            "write_ready": not missing,
        }

    @dispatch_tool(mcp)
    def cognition_register_person(
        ctx: Context,
        name: str,
        role: str,
        seniority: str,
        reports_to: str | None = None,
        email: str | None = None,
        detail: str = "",
        from_agent: bool = True,
    ) -> dict[str, Any]:
        """Register a HUMAN on the project roster — their committed profile.

        The roster models HUMANS ONLY — agent identity lives in teammate-comms,
        never here. Use this for onboarding (introducing your human) or for a
        manager pre-registering a teammate who hasn't onboarded yet.

        Profiles are COMMITTED and shared with the team, one file per person
        (`.cognition/people/<email>.profile.jsonl`), append-only. They are what
        makes search ranking, the reporting chain and the prime digest work for a
        real person rather than an anonymous address.

        Identity rule: omit `email` to self-register — the server uses YOUR
        confirmed identity's email (impersonation-resistant). Pass an explicit
        `email` to register someone ELSE (allowed, trust-based; the profile records
        carry you as the author, so who did it is in the audit trail rather than
        enforced).

        One profile per (casefolded) email. Registering an email whose profile is
        already COMPLETE returns it with `already_registered: true` and writes
        nothing — never a silent duplicate, never a data-losing error. An
        INCOMPLETE profile is filled in instead, so a manager can pre-register a
        teammate with role/seniority/reporting line and that person then only has
        to confirm the checkout with `cognition_set_identity`. To change fields on
        a complete profile, use `cognition_update_person`.

        SENIORITY IS A CLOSED SET, ROLE IS NOT: `role` is free text (whatever the
        human says — "backend engineer", "PM", "lead", anything). `seniority` is
        exactly one of owner | senior | mid | junior — nothing else is valid.
        Before calling this tool, PRESENT these four options to the human and ask
        which applies; do NOT silently infer/guess a tier from a role title or an
        offhand word (a human saying "owner, senior" was previously mapped to the
        wrong tier because the assistant guessed instead of asking — don't repeat
        that).

        Args:
            name: The person's display name.
            role: Their role (e.g. "backend engineer", "PM") — free text, not the
                closed set below.
            seniority: One of: owner, senior, mid, junior (closed set, casefolded).
                Ask the human to pick one of these four words; never map free text
                onto a tier yourself.
            reports_to: The direct manager's EMAIL, or the literal "nobody" for
                top-of-chain — valid and expected on a solo project. A manager's
                NAME is rejected: the chain is resolved by email, and a name would
                break it with no error anywhere. Need not be on the roster yet (a
                manager may register later) — dangling is legal, surfaced as
                `reports_to_registered: false`. Self-reporting and transitive
                cycles (A→B→...→A, walked through people already on the roster)
                are rejected.
            email: Omit to self-register (your confirmed identity); pass to
                register someone else.
            detail: Optional free-text bio/notes.
            from_agent: Set false ONLY when the human explicitly dictated/authored
                this registration themselves; default true ("via agent"). When in
                doubt, leave the default.

        Returns:
            A roster row: `{email, name, role, seniority, reports_to,
            answered_reports_to, reports_to_registered, detail, summary, source,
            id}`. `reports_to` is a manager's email or the literal "nobody";
            `answered_reports_to` is False when nobody has been asked yet;
            `source` is "profile", or "node" for a legacy person node not yet
            migrated, whose id is then in `id` (None otherwise).
            Plus `already_registered` (bool). On a NEW or filled-in profile also
            `profile_written` (the fields this call appended) and
            `missing_profile_fields` (what is still absent — empty when the
            profile is complete); the `already_registered: true` branch omits
            both, since it wrote nothing.
            Returns {"error": ...} for an invalid seniority, a `reports_to` that
            is neither an email nor "nobody", self-reporting, a reports_to cycle,
            or a blank explicit email; and `{"identity_required": true, ...}` when
            YOUR own identity is unconfirmed or your profile incomplete —
            registering someone else still records you as the author, so call
            `cognition_set_identity` first.
        """
        return _register_person(
            ctx, name, role, seniority,
            reports_to=reports_to, email=email, detail=detail,
            from_agent=from_agent,
        )

    @dispatch_tool(mcp)
    def cognition_update_person(
        ctx: Context,
        email_or_id: str,
        name: str | None = None,
        role: str | None = None,
        seniority: str | None = None,
        reports_to: str | None = None,
        detail: str | None = None,
        from_agent: bool = True,
    ) -> dict[str, Any]:
        """Change fields on someone's committed profile, with an audit trail.

        Anyone may update anyone — this is a local trust domain; the append-only
        record trail (one record per changed field, each carrying who and when) is
        the control, not an ACL. Only fields whose value actually changes are
        written, so re-submitting an unchanged profile costs nothing.

        SENIORITY IS A CLOSED SET, ROLE IS NOT: `role` is free text; `seniority`
        is exactly one of owner | senior | mid | junior — nothing else is valid.
        If the human wants to change seniority, PRESENT these four options and
        ask which applies; do NOT silently infer/guess a tier from a role title
        or an offhand word.

        Args:
            email_or_id: The person's email OR node id.
            name: New name, if changing.
            role: New role, if changing — free text, not the closed set below.
            seniority: New seniority (owner | senior | mid | junior), if
                changing. Ask the human to pick one of these four words; never
                map free text onto a tier yourself.
            reports_to: New manager's EMAIL, or "nobody" for top of chain; omit
                for no change. A manager's NAME is rejected — the chain is
                resolved by email. Self-reporting and cycles are rejected; a
                dangling (not-yet-registered) email is legal.
            detail: New bio/notes, if changing.
            from_agent: Set false ONLY when the human explicitly dictated/authored
                this update themselves; default true. When in doubt, leave default.

        Returns:
            A roster row: `{email, name, role, seniority, reports_to,
            answered_reports_to, reports_to_registered, detail, summary, source,
            id}`. `reports_to` is a manager's email or the literal "nobody";
            `answered_reports_to` is False when nobody has been asked yet;
            `source` is "profile", or "node" for a legacy person node not yet
            migrated, whose id is then in `id` (None otherwise).
            Plus `profile_written` (fields actually appended), `profile_skipped`
            (fields already at that value, so not journalled) and
            `missing_profile_fields`. Returns {"error": ...} for no matching
            person, an invalid seniority, a `reports_to` that is neither an email
            nor "nobody", self-reporting, a reports_to cycle, or no updatable
            field passed at all; and `{"identity_required": true, ...}` when your
            own identity is unconfirmed or your profile incomplete.
            NOTE a change from earlier versions: passing only values identical to
            the current ones is no longer an error — it succeeds with everything
            in `profile_skipped` and nothing written.
        """
        return _update_person(
            ctx, email_or_id, name=name, role=role, seniority=seniority,
            reports_to=reports_to, detail=detail, from_agent=from_agent,
        )

    @dispatch_tool(mcp)
    def cognition_get_person(ctx: Context, email_or_id: str) -> dict[str, Any]:
        """Get one person's full profile, its audit trail, and their environment.

        Args:
            email_or_id: The person's email. A legacy person-node id also
                resolves while that node still exists, but email is the identity
                key — prefer it.

        Returns:
            A roster row: `{email, name, role, seniority, reports_to,
            answered_reports_to, reports_to_registered, detail, summary, source,
            id}`. `reports_to` is a manager's email or the literal "nobody";
            `answered_reports_to` is False when nobody has been asked yet;
            `source` is "profile", or "node" for a legacy person node not yet
            migrated, whose id is then in `id` (None otherwise).
            Plus `profile_history` (the append-only trail, oldest first) and
            `environment` (their stored env facts, {machine: {key: value}}, empty
            dict when none — see cognition_list_env_facts).
            {"error": ...} if not found.

            THREE record shapes appear in `profile_history`; check `action` before
            reading anything else, or a removed-then-re-registered person will
            KeyError on `field`:
              * `profile_set` / `profile_unset` — one field change: `action`,
                `field`, `value` (set only), `by`, `at`, `from_agent`.
              * `profile_removed` — a removal tombstone: `action`, `by`, `at`.
                NO `field`, NO `value`.
              * `profile_history_legacy` — a migrated person node's older trail,
                carried verbatim: `action`, `from_node`, `entries` (a list of
                `{changed: {field: {from, to}}, at, by}`), `at`. NO `field`.
            A row still backed by a legacy person node that was never migrated
            returns that node's `{changed: ...}` entries directly instead.
        """
        storage: CognitionStorage = get_lifespan(ctx)["cognition_storage"]
        return _get_person(storage, email_or_id)

    @dispatch_tool(mcp)
    def cognition_list_people(ctx: Context) -> dict[str, Any]:
        """List everyone on the project roster.

        Returns:
            {"people": [row, ...], "count": N}, sorted by name, where each row is:
            A roster row: `{email, name, role, seniority, reports_to,
            answered_reports_to, reports_to_registered, detail, summary, source,
            id}`. `reports_to` is a manager's email or the literal "nobody";
            `answered_reports_to` is False when nobody has been asked yet;
            `source` is "profile", or "node" for a legacy person node not yet
            migrated, whose id is then in `id` (None otherwise).
        """
        storage: CognitionStorage = get_lifespan(ctx)["cognition_storage"]
        return _list_people(storage)

    @dispatch_tool(mcp)
    def cognition_remove_person(
        ctx: Context, email: str, from_agent: bool = True
    ) -> dict[str, Any]:
        """Take someone off the project roster — they left the team.

        Clears every field of their committed profile and records a removal
        tombstone. Profiles are append-only, so the record trail of who they were,
        and who changed what, stays intact and still merges cleanly; they simply
        stop appearing on the roster, in anyone's reporting chain, and in search
        ranking. Registering them again later just works — their new records
        outrank the tombstone and the whole history is still there.

        NOT a cascade, deliberately: their environment facts and everything they
        authored are untouched. Attribution is history — rewriting it because
        someone left would make the graph lie about who decided what. To remove
        stored environment facts, THEY run cognition_clear_env_facts (self-only).

        If they managed anyone, those people's reporting lines are left dangling
        and named in `orphaned_reports` — ASK THE HUMAN who those people report to
        now rather than guessing, then set it with cognition_update_person.

        Removing YOURSELF is refused: clearing the profile of the identity driving
        this checkout would close the write gate immediately, and the next record
        or task would be refused with no obvious cause. Correct your own details
        with cognition_update_person, or hand the checkout over with
        cognition_set_identity.

        Args:
            email: The person's email. A legacy person-node id also resolves
                while that node still exists.
            from_agent: Set false ONLY when the human explicitly dictated this
                removal themselves; default true.

        Returns:
            {"removed": true, "email", "name", "cleared_fields", "orphaned_reports",
            "from_agent"}, plus `warning` when they managed someone, and
            `legacy_node_id` + `warning_legacy_node` when a pre-v0.38 person node
            for them is still in the graph — they are off the roster either way,
            but that node still shows in graph listings and still has an embedding,
            so it should be removed with `cognition_remove_node`.
            {"error": ...} when no such person, when the email is your own, or when
            `.cognition/people/` is not writable.
            `{"identity_required": true, ...}` when your own identity is
            unconfirmed or your profile incomplete.
        """
        return _remove_person(ctx, email, from_agent)

    @dispatch_tool(mcp)
    def cognition_set_env_fact(
        ctx: Context,
        key: str,
        value: Any,
        machine: str | None = None,
        from_agent: bool = True,
    ) -> dict[str, Any]:
        """Store one durable environment fact about YOURSELF (self-only).

        The target identity is ALWAYS the server-resolved identity (your confirmed identity if set, else your VCS identity) — there
        is no email parameter (impersonation-resistant by construction). Facts
        are per-machine durable environment truths: project root, OS, tool
        choices ("container_runtime": "podman"), setup gotchas. They live in
        a committed per-person delta file (.cognition/people/), so teammates'
        sessions can detect environment divergence — do NOT store secrets,
        tokens, or anything that must stay out of git history.

        DISCLOSURE CONTRACT: every successful write returns a `disclosure`
        string. You MUST relay it to the human — they can ask for any stored
        fact to be removed (cognition_delete_env_fact /
        cognition_clear_env_facts) at any time.

        Args:
            key: Fact name (e.g. "project_root", "os", "container_runtime").
            value: JSON value (string/number/bool/null/list/object).
            machine: Machine key; DEFAULT: this machine's hostname
                (casefolded). Pass explicitly to record a fact about one of
                YOUR other machines.
            from_agent: Set false ONLY when the human explicitly dictated the
                fact themselves; default true.

        Returns:
            {email, machine, key, value, written, noop, old, disclosure} —
            `written` false + `noop` true when the identical value was already
            stored (nothing journaled). {"error": ...} (retryable) on a blank
            key/machine, or a NEW machine at the per-person machine cap (the error
            names the prune remedy).
            Refused with `{"identity_required": true, ...}` when this checkout has
            no CONFIRMED identity, or its profile is missing role/seniority/
            reports_to — the payload names what to ask the human for and the
            `cognition_set_identity` call that fixes it. Nothing is written.
        """
        return _set_env_fact(ctx, key, value, machine, from_agent)

    @dispatch_tool(mcp)
    def cognition_delete_env_fact(
        ctx: Context,
        key: str,
        machine: str | None = None,
        from_agent: bool = True,
    ) -> dict[str, Any]:
        """Remove one of YOUR OWN stored environment facts (self-only).

        Same identity rule as cognition_set_env_fact: no email parameter, the
        server-resolved git identity is always the target. Deleting a fact
        removes it from the live state; the historical delta line remains in
        git history (same posture as every journal write).

        Args:
            key: Fact name to remove.
            machine: Machine key; DEFAULT: this machine's hostname (casefolded).
            from_agent: Set false ONLY when the human dictated the removal.

        Returns:
            {email, machine, key, written, noop, old, disclosure} — noop when
            the key wasn't stored. {"error": ...} (retryable) on a blank
            key/machine.
            Refused with `{"identity_required": true, ...}` when this checkout has
            no CONFIRMED identity, or its profile is missing role/seniority/
            reports_to — the payload names what to ask the human for and the
            `cognition_set_identity` call that fixes it. Nothing is written.
        """
        return _delete_env_fact(ctx, key, machine, from_agent)

    @dispatch_tool(mcp)
    def cognition_clear_env_facts(
        ctx: Context,
        machine: str | None = None,
        from_agent: bool = True,
    ) -> dict[str, Any]:
        """Bulk-remove YOUR OWN environment facts (self-only) — the
        removal-on-request path: "forget everything you stored about me" is
        ONE call, never an enumerate-and-loop.

        Args:
            machine: Scope. A machine key clears that ONE machine's facts.
                OMITTED means ALL machines — deliberately NOT defaulted to the
                current hostname, because the no-arg call means "forget
                everything", not "forget this machine". (Also the prune remedy
                when the machine cap rejects a new machine.)
            from_agent: Set false ONLY when the human dictated the removal.

        Returns:
            {email, machine, written, noop, cleared_keys, disclosure} —
            cleared_keys is uniformly "machine/key" strings; the audit line
            enumerates exactly what was removed (never an opaque wipe). noop
            when nothing was stored in scope.
            Refused with `{"identity_required": true, ...}` when this checkout has
            no CONFIRMED identity, or its profile is missing role/seniority/
            reports_to — the payload names what to ask the human for and the
            `cognition_set_identity` call that fixes it. Nothing is written.
        """
        return _clear_env_facts(ctx, machine, from_agent)

    @dispatch_tool(mcp)
    def cognition_list_env_facts(
        ctx: Context, email_or_id: str | None = None
    ) -> dict[str, Any]:
        """List stored environment facts — yours by default, or any teammate's.

        Reads are OPEN (writes are self-only): pass a teammate's email or
        person node id to see their machines' facts, e.g. to check how their
        environment diverges from yours (different project root, different
        container runtime) before acting on graph content they authored.

        Args:
            email_or_id: Omit for YOUR facts (server-resolved identity). Or a
                person's email (works even if they have no person node yet) or
                person node id.

        Returns:
            {email, registered, environment: {machine: {key: value}},
             machine_count, current_machine} — `registered` false means facts
            exist for an identity with no person node (first-class legal, not
            an error); `current_machine` is THIS machine's casefolded hostname
            so a caller can tell this-machine facts from elsewhere-facts.
            {"error": ...} when no target identity is resolvable.
        """
        return _list_env_facts(ctx, email_or_id)
