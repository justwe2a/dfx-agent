---
name: harmony-debug
description: Investigate and repair HarmonyOS projects with the DFX shared build, deployment, UI and fault-log tools. Loaded for solve tasks with a host configuration.
---

# HarmonyOS investigation

## Clean working-copy dependencies

A lock file records dependency versions; it does not mean the dependencies are installed.
Task copies exclude oh_modules and build caches. Before interpreting unresolved imports or
resources as source defects, inspect the project's dependency setup. In a clean copy, use
the explicit `dependencies` operation when dependency installation is authorized, then build.
Do not add resource values or alter project configuration just to compensate for an unprepared
dependency graph. If preparation is disabled or would change locked dependencies, report the
blocker. build does not silently install dependencies or retry a failed compilation.
Read-only scope patterns include new paths in protected resource/configuration areas. They
also apply to changes made by dependencies, builds, exec and workspace-authored scripts;
detected persistent changes stop the session without automatic rollback. Ordinary diagnostic
scripts outside protected paths remain available under the task's scope.

## Investigation and tools

Read the task, project structure and build profiles to choose module/product/target,
bundle, ability and HAP path. Do not infer these from a known competition case.
Use the ordinary read/search/edit/exec tools for code and developer tests; choose checks
that distinguish the suspected cause. There is no mandatory five-stage sequence.
The task's log_files are available via read(source="logs", path="<listed filename>");
read(source="logs", path=".") lists them. They are not a logs/ folder in the workspace.

Use the harmony tool with `operation: "info"` to report capabilities. Follow the selected
model protocol: JSON mode uses an action named harmony; native mode calls the harmony function.
Use one HarmonyOS action per turn; only read/search may share a model response. Paths are relative to the workspace,
including artifact paths even when cwd is a subdirectory.

For UI work, first inspect an unfamiliar state to choose selectors. Once the relevant
controls are understood, prefer a short workspace-authored run_script for a related group
of actions and assertions over one model request per click or field. This is optional:
you choose the checks and navigation from the task and observations, not a supplied case script.
Within one unchanged state, reuse one captured layout for multiple assertions. Paginate
that saved capture when needed; total_matches and next_offset distinguish a partial
response from a complete result. Obtain fresh state after an action. Stop a check at its
first failed assertion; preserve completed checks and decide the next step yourself.

| operation | Required fields | Observation |
|---|---|---|
| dependencies | optional cwd (default .) | ohpm install --all; requires dependency-install permission |
| build | module, product, target, artifact; optional cwd | debug assembleHap plus required project HSPs; status, exit, packages, diagnostics and full command_log paths |
| install | artifact | checks packaged ABI, installs built HSP dependencies before the HAP; file edits invalidate its record |
| start | bundle, module, ability | first successful start binds this task's application; does not establish a usable page |
| stop | bundle | force-stop response |
| layout | optional offset (0), limit (40, max 80), saved_layout | compact node fields; next_offset and full layout path; saved_layout pages the same capture without HDC |
| tap_text / input_text | selector; text for input_text | fresh layout, unique visible target; ambiguity is an error |
| wait | selector; optional timeout_seconds (5) | observation window; an in-flight capture may finish later, bounded by command/script time; no action retries |
| tap | x, y | click at coordinates taken from the observed layout |
| input | x, y, text | inputText at the observed field, including literal Chinese text |
| swipe | x1, y1, x2, y2, duration_ms | gesture response |
| key | key (Back, Home or Enter) | key event response |
| navigate | key (Back or Home); system_navigation_enabled required | one global navigation event, not a recovery verdict |
| package / pid | bundle | installed identity / observed process ID |
| faults | bundle | available jscrash filenames; may include old crashes |
| fault | bundle, name | selected crash text; choose a name from faults |
| screenshot | none | local image path; this text-model interface does not deliver pixels |
| run_script | script (workspace Python path) | executes your main(device), with assertions and per-operation evidence; see Workspace-authored checks |

Example harmony parameters (replace values using this project's profiles):
`{"operation":"build","cwd":".","module":"entry","product":"default","target":"default","artifact":"entry/build/default/outputs/default/entry-default-unsigned.hap"}`.
In JSON mode add `"action":"harmony"`; in native mode these are the function arguments.

Build does not install dependencies or change signing automatically. Diagnose compiler
output and edit source, then rebuild. A previous target HAP is moved to the tool evidence
directory before building so a missing output cannot silently reuse it. After source edits,
build again before installation. Plain exec does not invalidate a build record: commands
that actually modify source/dependencies require you to rebuild; pure inspection does not.
The tool does not infer arbitrary shell side effects or certify source/artifact equivalence.
Shared command timeouts are fixed at task launch.
The build deadline covers the entry HAP and all required HSPs together. HSP dependencies
come from packaged module.json, not a guessed directory ordering. A shared failure
keeps its own log and does not trigger an automatic rebuild or install. Read shared_builds
and error when diagnosing it. A dependency HAR with an ARM library is only a risk before
packaging; actual incompatible HAP/HSP native libraries stop installation. Never delete
libraries, alter architecture targets or disable features to evade this check.
Dependency preparation may create/update oh-package-lock.json5 and resolve declared version
conflicts. Inspect the output and resulting lock files. Do not describe unpinned resolution as
unchanged dependencies. Report any resolution beyond the task's approved versions.

Device tools require enabled device capability. When disabled, do local investigation
and report device behavior as unverified; do not bypass the pause through exec. Similarly,
do not bypass disabled dependency installation using shell commands. These permissions
are task policy, not an OS sandbox. New terms/privacy/service agreements require
`request_user_action` BEFORE acceptance, including first-run dialogs inside the target app.
Repair/install/device authorization is not consent. Ask the operator to read and handle the
specific decision locally; do not tap Agree/Accept, press Enter, use script/exec, or modify
startup code to accept or bypass it. After the operator handles it and continues this task,
observe the page again; do not replay the acceptance or infer approval of future agreements.
Ordinary navigation remains autonomous. Do not alter privacy, payment, signing or startup
behavior without task authorization.

After installation, observe launch/layout and verify the relevant visible behavior;
installation may deliver delayed package events. Use observations to decide any next
action rather than assuming a fixed sleep proves readiness. Compare fault filenames
before/after a trigger and check process identity/time; an old crash is not new evidence.
Environment failures are BLOCKED, compiler failures FAILED, successful commands EXECUTED,
and reads OBSERVED. None alone proves the task is repaired. Do not silently rerun an
unavailable environment or weaken assertions. Report blockers and missing checks.
Genuine environment BLOCKED ends the agent session with the observation preserved;
ordinary tool misuse (including installing without a current build) remains correctable.
Successful install includes one recorded five-second package-update settle window,
not an install or launch retry.
Start the intended application before UI tools or run_script. UI captures use
uitest dumpLayout -p <unique path> and verify each retained window's bundleName locally.
Only the target windows are made model-readable; system bars/keyboard/other app text are omitted.
When a valid capture has no target window (empty windows or only identified other bundles),
the tool returns NOT_VISIBLE with UI content withheld. This is not an empty business result,
and no previous layout is substituted. Decide from the recorded state whether to explicitly
start the bound application, use authorized system navigation, make a new observation,
or finish with the missing checks stated. NOT_VISIBLE alone does not identify a lock screen
or the cause of an obstruction; distinguish observations from hypotheses in your report.
Nothing is automatically started or retried. Unknown window ownership, invalid captures,
screen lock, HDC failures and unconfirmed cleanup still stop the session. Saved captures use
the same identity check and are explicitly marked as not fresh.
Do not use exec or temporary capture paths to bypass this observation boundary.
Coordinate and ordinary key actions check target-window presence immediately before dispatch; this is
not an atomic OS input sandbox and does not prove the window cannot change afterwards.
Back/Home can change or leave the page; observe the resulting state rather than assuming
that a fixed number of key presses restores an earlier page.

If system_navigation_enabled is true, `navigate` is a separate explicit operation for Back/Home
even when the target is absent in a valid capture. It dispatches one key and reports
target_visible_before and target_state_after=NOT_CHECKED. Choose it from the observed situation,
then capture the target again before claiming recovery or continuing assertions. It does not
read another application's UI, click dialog buttons, confirm agreements or unlock the device.
Actual capture/HDC/known screen-lock errors still stop work. Do not loop blind key presses,
automatically replay an interrupted script, or treat successful navigation as a passed check.
System dialogs that require a specific button or a consent decision may remain unresolved;
report that limitation instead of guessing coordinates or using Enter to confirm blindly.
If an app or system dialog needs an operator decision, use request_user_action to pause the same task
with a concrete environment request. After confirmation, observe current state before proceeding.
Do not spend remaining requests repeating the same observation or ask the operator for a repair solution.
At the final finish-only request, report the unresolved decision in unverified instead.

Inside main(device), perform('faults'), perform('pid') and perform('package') default
to this task's bound bundle; perform('fault', name=...) also needs the selected filename.
Missing action arguments are correctable tool errors. An evidence I/O failure may occur
after a command's side effect: it stops execution and must not replay that command.

Prefer current selectors and control bounds to fixed screen positions. When a control needs
coordinates, derive them from a fresh layout and the intended control's bounds; keep gestures
inside that control, away from screen edges and system navigation areas. After a gesture,
observe the actual selected value before continuing. A changed picker value is a side effect
even if it has not yet been confirmed on the business page.

Tool results include an evidence directory and a readable tool-log result.json.
For more UI nodes, call layout with saved_layout set to the returned layout path and
offset set to next_offset. This reads the same saved capture; after a UI action obtain
a fresh layout. layout points to a formatted full tree for line-based reads; raw_layout
contains the verified target-window capture; unrelated windows are not retained. Shortened node values list truncated_fields.
Build observations expose diagnostics and a short tail; command_log retains the full output.
If truncated, read the saved command.log, layout JSON or crash file in bounded pieces with
`read` and `source:"tool-log"`. Runtime logs may contain sensitive data: use only the
project/log scope approved for this task. Do not read hidden evaluation answers or
change existing test expectations. Finish with actual checks, remaining uncertainty and
original-source root-cause location if supported; independent evaluation stays separate.

Layout accepts selector fields text/id/type (AND), with match exact or contains for text.
It searches the entire tree before pagination. Search can use source workspace/original/logs/tool-log.
For truncated long lines, read again with the returned next_read start/column.
The workspace has its own initial Git commit; use git status/diff locally.

## Explicit screen recovery

When `screen_unlock_enabled` is true, `harmony(operation="screen", mode="observe")`
captures fresh system screen state without exposing system/foreign UI content. It does not
wake or swipe. `mode="unlock"` explicitly requests at most one wake and one upward swipe,
only for the declared no-password `swipe_only` device. No credential entry is available.
It observes the result for at most 10 seconds within a 60-second whole-operation deadline
(also bounded by the enclosing script). There is no automatic repeat or application launch.
`UNLOCKED` and `target_visible` are separate: choose subsequent target operations yourself.
`UNKNOWN` is not success. `wake_sent`/`swipe_sent` record attempted dispatch, not success.
An already unlocked device receives no swipe. Credential requirements or real HDC failure
produce BLOCKED; never try PINs, passwords, blind confirmation or alternative shell recovery.

With this capability enabled, ordinary target operations encountering a known lock return
SCREEN_LOCKED to you. Choose whether to observe, unlock, or submit a limitation. An interrupted
script ends with script_completed=false and partial effects; do not replay its completed work.
`device.perform("screen", mode="observe" or "unlock")` uses the same shared implementation.
Without the capability, locks retain the existing BLOCKED behavior. System UI bodies are not
readable through tool logs; no-password configuration is a host declaration, not inferred
from the absence of a visible password box. No task-specific repair or navigation is prescribed.

## Workspace-authored checks

Use `harmony(operation="run_script", script="checks/smoke.py")` to execute a workspace Python
file defining `main(device)`. The script uses the same driver as individual tools. Available:
`device.layout(offset=0, limit=40, saved_layout=None, **selector)`, `tap(**selector)`, `input(value, **selector)`,
`wait(timeout=5, **selector)`, `key("Back")`, `swipe(x1,y1,x2,y2,duration_ms)`.
Layout pagination is separate from selector fields (text/id/type/match); limit is 1..80.
Both direct layout and script layout return the same flat nodes and pagination fields.
saved_layout refers to an existing tool-log layout, not a fresh device observation.
For example, device.layout(type="Text", match="exact", limit=80) reads one page;
next_offset means more nodes remain. A single wait timeout must not exceed the device
command timeout (normally 60 seconds); the 120-second whole-script limit does not enlarge it.
`device.perform(operation, **fields)` also exposes layout, coordinate tap/input, wait,
package/pid/faults/fault/screenshot and authorized navigate, using the table's fields.
It does not build or install. `device.perform("navigate", key="Back")` uses the same
explicit navigation permission and records one action; it is not an automatic error handler.
Example (illustrative selectors, not a task solution):

```python
def main(device):
    device.tap(text="设置")
    assert device.wait(text="通知", timeout=5)["matches"], "Expected settings entry"
    device.key("Back")
```

Write checks based on the task's business requirements and current observations. Assert actual
values; process survival or a successful click alone is not functional verification. Script
stdout/tracebacks and every HDC command are retained in tool-log. Script success is not independent
acceptance. Device pause applies before script launch. The entire script has its own
script_timeout (default 120 seconds, fixed at task launch); individual HDC commands are
limited by both command_timeout (default 60 seconds) and remaining script time.
After the operation deadline no further device command is dispatched. The runner has a
separate bounded 35-second finalization window for cleanup and its result, not more device work.
A FAILED/TOOL_ERROR can be diagnosed and revised;
NOT_VISIBLE stops the current script with script_completed=false, preserving completed actions
and the interrupted operation. The remaining lines are not run. Decide what to do next from
that record; do not treat the script as passed or blindly replay the completed sequence.
TIMEOUT after cleanup may have partial side effects, so observe before choosing another action.
Environment BLOCKED and uncertain cleanup stop the session. No tool is automatically replayed.
These checks are ordinary Python under task policy, not a secure sandbox.
Split checks that exceed the script budget using the actual last observed state. Do not blindly
repeat an interrupted navigation sequence. Assert the required results (including completeness
when required), not just printed values. Do not catch arbitrary exceptions and proceed as if
the operation succeeded. A short wait's TIMEOUT means the selector was not confirmed within its
observation window; it does not kill an otherwise permitted HDC capture at that short interval.
Keep each script focused on a bounded set of checks. A single saved layout can support several
assertions about the same state; avoid fresh captures for every field when the UI has not changed.
Assert the relevant context (such as selected city), submitted input and complete result, and
record completed checks before moving on. On TIMEOUT, use the preserved actions and observations
to distinguish completed, uncertain and unstarted work; never report the whole script as passed.
