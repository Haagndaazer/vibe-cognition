# WP-2 Execution Plan — fix/wp-2-ci (GitHub Actions CI) — REV 2

Branch: `fix/wp-2-ci` off main @ `065600a`. No existing `.github/`. Revised after peer review + Vince's decisions.

## Research findings (verified)
- **torch**: lock pins the CUDA stack — 42 nvidia/cuda packages, `sys_platform == 'linux'`. `uv sync` installs from lock → full CUDA on ubuntu. `uv sync` does NOT accept `--torch-backend`/`UV_TORCH_BACKEND` (that's `uv pip` only) — the "env on sync" approach is a silent no-op. [Vince deciding A vs B below.]
- **model download**: tests use `_FakeEmbeddingGenerator`; none load the real model. `generator.py:9` imports `sentence_transformers` at top level (so torch must be INSTALLED to import), but the model loads lazily in `SentenceTransformersBackend.__init__:49`, which tests never hit → no download. ALSO watch (peer review H2): `storage.py:38` `get_or_create_collection` with no `embedding_function` wires chromadb's default ONNX EF, which *can* fetch a model from chromadb's CDN — lazy at 1.5.5, shouldn't fire for construct-only tests, but confirm in CI logs (criterion d: report if it does).
- **python**: `>=3.11,<3.14`, dev is 3.12. No `.python-version` file → pin deterministically.
- **tooling**: `uv run pytest` / `uv run ruff check .` / `uv run pyright`.
- **baseline**: ruff exit 1 (2 UP042), pyright exit 1 (31), pytest 130 green.

## Vince's decisions (locked)
- **ruff**: `# noqa: UP042` on both class lines (`models.py:10,23`) with a comment citing the WP-1 deferral → `ruff check .` clean + fully gating.
- **pyright**: baseline-count ratchet, GATING. Commit a baseline file containing `31`; CI fails if pyright's error count EXCEEDS it; a WP that lowers the count edits the number in the same PR. Delete the file when it hits 0 → strict.
- **pytest**: gating.
- **journal gitattributes**: add `.cognition/journal.jsonl -text` (one line, cite "C-3/defense: journal byte-determinism") so git stores/checks out the journal verbatim (no autocrlf), making content-equal == byte-equal and merge=union byte-deterministic. Blob renormalizes at Vince's next post-merge flush — expected, self-healing (binary splitlines() replay reads both EOLs).
- **torch**: A vs B PENDING.
  - **A (recommended)**: `[[tool.uv.index]] pytorch-cpu` + `[tool.uv.sources] torch={index=...}`, re-lock. CPU torch CI+local, drops 42 nvidia pkgs, not a new dep. Touches lock + local.
  - **B**: CI-only — `uv sync --no-install-package torch` + `uv pip install --torch-backend=cpu torch`. No lock/local change; verify nvidia deps don't sneak in.

## Workflow: `.github/workflows/ci.yml`
- `name: CI`
- `on: pull_request` + `push: branches:[main]`, both `paths-ignore: ['.cognition/journal.jsonl']`.
- `jobs.test:`
  - `strategy.matrix.os: [ubuntu-latest]` first; add `windows-latest` after measuring ubuntu (report both cold/warm). Windows torch is already CPU wheel → cheaper on the torch axis.
  - Python 3.12 pinned (add `.python-version` = 3.12, or pass to setup-uv) — deterministic.
  - steps (third-party actions pinned ≥ major):
    1. `actions/checkout@v4`
    2. `astral-sh/setup-uv@v5` (consider v6) `with: enable-cache: true`
    3. torch+deps install per A or B
    4. `uv run ruff check .` (gating)
    5. pyright ratchet: `uv run pyright`, capture error count, fail if > baseline (script reads `.pyright-baseline` or inline)
    6. `uv run pytest` (gating)
  - timing: capture cold (cache miss) vs warm (cache hit) durations → report.
- **Required-check deadlock fix (peer review H3)**: a `pull_request`-triggered job that is `paths-ignore`d never reports, leaving a *required* check pending → unmergeable journal-only PRs. Add a second job/workflow with the SAME check name that runs on the ignored path and trivially succeeds (GitHub's documented pattern), OR ensure the check isn't marked required for journal-only changes. Vince owns branch protection; I provide the always-green fallback so the name always reports.

## Sequence
1. Vince picks torch A/B.
2. Commit: noqa the 2 UP042 (ruff clean). Cite WP-1 deferral.
3. Commit: `.gitattributes` `-text` line (C-3/defense).
4. Commit: torch config (if A) / none to lock (if B).
5. Commit: `.github/workflows/ci.yml` + pyright baseline file + fallback job.
6. Push; observe the PR's own run (cold). Second push/re-run (warm). Capture timings; confirm no model download in logs (criterion d).
7. If ubuntu sane, add windows-latest; re-measure.
8. CHANGELOG Unreleased; report to Vince (timings, PR link, criterion-d evidence). End with "gated on human release test" (CI-mechanics) + note branch-protection flips are admin.

## Constraints
- No new deps (g); no version bump; no CLAUDE.md; no marketplace.json.
- Journal not committed on branch; branch-switch = ping Vince (4-step protocol).
- Acceptance = PR's own green run (f).
