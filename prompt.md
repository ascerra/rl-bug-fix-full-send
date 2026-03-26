Study **`SPEC.md`**, **`ARCHITECTURE.md`**, and **`IMPLEMENTATION-PLAN.md`**. Identify the **highest-priority incomplete item** on the critical path (follow the phase build order and dependency graph). Implement **only that one thing**, test it, and report what to do next.

IMPORTANT:
- `make check` must pass after every change — lint clean, all tests green. If you say it's done, the tests prove it.
- Follow the Operating Rules and Handling Problems sections in `IMPLEMENTATION-PLAN.md`.
- Reference `../fullsend/docs/` for security threat model and architecture context when needed.

---

## Mandatory closing steps

After you finish the implementation for this run:

1. **Update `IMPLEMENTATION-PLAN.md`** — mark completed sub-items with ✅.
2. **Update `README.md`** — reflect what's been built.
3. **Append to `progress/run-log.md`** — add a new `## Run N` entry with:
   - **Phase**: which phase/sub-phase you worked on
   - **What shipped**: what you built (1-2 sentences)
   - **Files changed**: key files created or modified
   - **Test result**: output of `make check`
   - **Decisions made**: any design/architecture choices and why
   - **Issues hit**: blockers, ambiguities, things that failed before working
   - **Next focus**: the single highest-priority item for the next run
4. **Run `python scripts/gen-progress.py`** — regenerates the progress dashboard.
5. **If ALL items in `IMPLEMENTATION-PLAN.md` are now ✅** (including `####` sub-items in Phase 7 — check *every* numbered heading at `###` and `####` level, not just `###`): write `{"ralphComplete": true}` to `progress/status.json`. If any numbered item lacks ✅, do NOT write this file.
6. **Final message**: list **what you shipped**, **test result**, and **next focus**.
