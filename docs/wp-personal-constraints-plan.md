# WP-Personal-Constraints — personal vs project constraints

Status: APPROVED rev 3, rulings final (task 16fc0290e9d2). Target: v0.42.0.

## 1. Problem

A constraint is project-wide: every constraint shows to everyone in the session-start
"Active Constraints" section, in teammates' "Since You Were Gone", on the dashboard,
and in search with no hint about whose rule it is. People record their own working
preferences as constraints, so a teammate's agent obeys rules that were never
meant for them.

Real examples from Northstar (all recorded under Colton's identity):

- "Agents must NEVER modify UI prefabs in this project — Colton authors all prefab changes himself"
- "Agents make NO Unity-side changes in this project — no prefabs, scenes, .asset/.mat/renderer files"
- "Renderer settings ... are ONLY ever changed by [Colton]"

A second person's agent reading these would refuse to touch prefabs for them too.

Per-person journal files (v0.41.0) record who wrote each entry, but every file is
replayed into one shared graph, so they do not limit who sees what.

## 2. Proposed design

### 2.1 How a constraint is marked

A `scope` value in the constraint's metadata: `"project"` or `"personal"`. A personal
constraint applies only to the person who recorded it, identified by the
server-stamped `recorded_by.email` (never the free-text author).

Rejected: a new `preference` node type. Every consumer (prime, dashboard, search
filters, curation analyzers, parity table, doc tables, type validation) would need a
new branch, and preferences that are genuinely hard rules for that person would sit
outside the constraint machinery (severity, supersession HEAD-filter).

Rejected: storing preferences in the per-person env-facts files. Those are key/value
machine facts with loud disclosure, not searchable narrative rules with supersession.

### 2.2 Recording (Colton ruling 2026-09-15: default personal)

`cognition_record` gains `scope: str | None` for constraints. Omitted means
`"personal"`. The docstring and skill carry the judging guidance:

- Personal: how THIS person likes to work or wants agents to behave for them —
  "don't touch prefabs, I do those", "always ask before committing", tool and style
  preferences, who authors what.
- Project: true for anyone working in the repo regardless of who they are — a
  platform or API limit, a build/ship rule, a client or contract requirement, a
  "this breaks if you do X" fact, a security rule.
- Unsure (for example "agents make no Unity-side changes in this project", which
  could be Colton's preference or a studio rule): ASK the human before recording,
  then pass the answer. If the human cannot be asked, record as personal.

The result echoes the stored `scope` so a wrong default is visible immediately.
Passing `scope` for any other node type is an error.

### 2.3 Who sees what (Colton ruling 2026-09-15: teammates cannot find them)

A personal constraint is visible only to its owner (server-resolved identity equals
`recorded_by.email`). For everyone else it does not exist through the plugin:

| Surface | Project constraint | Your personal constraint | A teammate's personal constraint |
|---|---|---|---|
| Prime: Active Constraints | shown | shown in "Your Personal Constraints" | hidden |
| Prime: Since You Were Gone | shown | n/a (your own writes) | hidden |
| cognition_search, get_node, get_neighbors, get_chain, get_history | returned | returned, labeled `scope: personal` | hidden (get_node answers "not found") |
| Dashboard | shown | shown, labeled personal | hidden |
| Curation worklists (uncurated, edgeless) | included | included when you curate | excluded |

Hidden means filtered at one choke point in the query layer keyed on the viewer's
identity, not in each tool, so a new tool cannot leak by forgetting a filter.
Edges touching a hidden node are dropped from the viewer's results. A session with
no confirmed identity sees no personal constraints at all.

Honest limit: this is visibility, not secrecy. The constraint is still a line in the
owner's committed journal file, so anyone who opens `.cognition/journal/*.jsonl`
directly can read it. Nothing secret belongs in a constraint (same rule as env
facts).

### 2.4 Changing scope later

`cognition_update_node` gains `scope`, allowed only on constraints and only by the
person the constraint belongs to (server-resolved identity must equal
`recorded_by.email`; Colton ruling 2026-09-15: owner only, managers get no read or
write access). An unowned legacy constraint (no `recorded_by`) cannot be made
personal. Making a project constraint personal hides it from the team,
so a teammate must not be able to do that to someone else's rule.

### 2.5 Existing constraints (Colton ruling 2026-09-15: stay project, no review)

Every existing constraint (no `scope` in metadata) is `project` forever — exactly
today's behavior; nothing changes on upgrade and there is no review prompt. Owners
reclassify by hand with `cognition_update_node(scope="personal")`. The default-personal
rule applies only to constraints recorded after the upgrade.

### 2.6 Curation

Curation only ever sees the curating person's own personal constraints (2.3), so no
edge can link two people's personal constraints, and nobody's curator links a
teammate's hidden node. Personal-to-project edges written by the owner are hidden
from teammates along with the node.

### 2.7 Storage / replay

`scope` lives in metadata, so it is journaled and replayed like any metadata. The
update is a normal `update_node` entry, last-writer-wins per v0.41.0. No journal
format change, no migration.

## 3. Release gates

Tool change -> tool-surface audit (Args/Returns of cognition_record,
cognition_update_node, cognition_search, cognition_get_node; SKILL.md + README
tables; parity rows; cognition_readme guide). Full pytest, ruff, render checks,
sonnet review. Not an svn-gate release unless the replay engine is touched.

## 4. Rulings (Colton, 2026-09-15)

- New constraints default to personal; the agent judges with the 2.2 guidance and asks
  the human when unsure.
- Teammates cannot find another person's personal constraints through any tool.
- Existing constraints stay project; no review prompt.
- Only constraints can be personal.
- Owner only: managers cannot see or change a report's personal constraints.
