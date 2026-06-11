GitHub Actions CI (audit: no CI) + a slim-install improvement surfaced along the way.

## CI (`.github/workflows/ci.yml`)
- Runs **ruff, pyright, pytest** on every PR and push to `main` (criterion a).
- `uv sync --locked` (fails on pyproject/lock drift), `astral-sh/setup-uv@v5` with cache, `actions/checkout@v4` — third-party actions pinned to major (criterion e).
- **pyright baseline ratchet**: `.github/pyright-baseline.txt` = 31; CI fails only if the error count exceeds it, warns when it drops (lower it in the same PR). Monotonic to zero, then pyright goes strict. Tested fail/pass/warn paths locally.
- **torch (criterion c)**: CPU-only via the pytorch-cpu index — see below; CI installs no CUDA.
- **model download (criterion d)**: tests use fakes; the real model loads lazily only on `EmbeddingGenerator` init, which tests never hit. Confirming against the green run's logs.
- **journal**: path-ignored on `push` to main (Vince's journal flushes don't burn a run). PRs are unfiltered — WP branches never commit the journal, so a journal-only PR can't occur, sidestepping the required-check deadlock with no fallback job.

## CPU-only torch (audit B-4)
This is a CPU-inference tool; default resolution pulled the multi-GB CUDA stack on Linux. Pinned torch to PyTorch's CPU wheel index.
- Lock: **162 to 144 packages** — removes exactly 18 GPU-only packages (3 cuda-*, 14 nvidia-*, triton); torch re-sourced to download.pytorch.org/whl/cpu at 2.11.0; **no other drift**.
- torch declared **direct** (uv ignores `[tool.uv.sources]` for transitive deps), pinned **exactly** `==2.11.0` (zero drift; a future sentence-transformers bump needing newer torch hard-conflicts at re-lock by design).
- Verified pytorch-cpu serves all three platforms (macOS arm64 plain `2.11.0`; linux/win `2.11.0+cpu`).

## Acceptance
Local: pytest 130 green, ruff clean, pyright 31 (baseline). CI proof = this PR's own run (timings + criterion-d log confirmation to follow). Windows-latest pending the ubuntu timing measurement.

Branch protection / required-checks flips are admin (Vince + Colton). CI-mechanics — gated on human release test.


## Cut-over note (CR-2)
The `.gitattributes -text` change creates a brief migration window: the index blob is LF while the live journal is CRLF, so post-merge the journal shows phantom-modified. **Resolution (manager-executed): merge, then immediately flush** — with `-text` in effect the flush commits the live CRLF bytes verbatim, renormalizing the blob (single writer, seconds). Do NOT re-materialize the journal between merge and that flush. Implementer FREEZE: no git commands from approval until the "cut-over done" ping.
