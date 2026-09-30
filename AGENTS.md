# Workspace Rules for Galaxy S5 Linux

## Mandatory Logging Rule: Record All Changes

**All agents working in this repository or interacting with the Galaxy S5 device MUST record every modification in [`CHANGELOG.md`](CHANGELOG.md).**

### Instructions for Agents:
1. **Scope:** Log any change made to:
   - Files, scripts, builds, or configurations in this repository.
   - Any live filesystem, service, sysfs node, kernel, or configuration on the Galaxy S5 device over SSH or serial console.
2. **Log Entry Requirements:** Every entry in `CHANGELOG.md` must include:
   - **Date & Timestamp** (ISO or local with timezone).
   - **Agent / Context:** Who or what performed the change.
   - **Target:** Host machine (repo) or Phone (`galaxy-s5`).
   - **Action & Files:** Exact files created, modified, or deleted; exact commands/services configured.
   - **Rationale:** Why the change was made (e.g., user request, bug fix, safety rule).
   - **Verification:** Observed state/output confirming the change took effect.
3. **Read Before Modifying:**
   - Before applying changes to the device or critical repository artifacts, read `CHANGELOG.md` and `HANDOFF.md` to avoid clobbering existing verified configurations.
4. **Preserve Device Integrity:**
   - Follow ground rules in `HANDOFF.md` (e.g., never restore `.s5bak` inittab/shadow files, do not break serial console, do not enable USB NCM, test read-only before writing).
