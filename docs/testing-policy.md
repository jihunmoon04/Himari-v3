# Pytest Execution Policy — Module-Scoped by Default

> 2026-10-13 · **Confirmed policy V3-038**. Module test directories, import boundary check and static CI now exist. Real Provider live tests are opt-in; Sandbox host security gates remain future work.
>
> References: [decisions.md](decisions.md), [contracts.md](contracts.md), [module boundaries](module-boundaries.md), [Sandbox implementation candidate](sandbox-implementation-v0.2.md).

## 1. Rule (CONFIRMED)

**Run full pytest only when a module's public contract is created, changed or removed.** Otherwise run pytest for **only the module(s) whose implementation changed**.

- A module-private implementation change is not a reason for full pytest.
- Number of files changed, refactor size, merge/release events and elapsed time do **not** by themselves trigger full pytest.
- A change touching internal implementations of two modules can run the two respective module pytest suites **without full pytest** if neither published contract changes.
- A public contract change triggers the **entire pytest suite**, not merely the changed module and its declared consumers.
- Contract changes in documents only, while no implementation/tests yet exist, are **design updates**; do not pretend a nonexistent pytest suite ran.
- Tests may include focused contract tests *inside the relevant module suite*. A real Linux security/capability gate is not a substitute for pytest; nor is it inherently a full pytest execution.

## 2. Exact trigger classifier

| Change | Pytest selection | Why |
| --- | --- | --- |
| Private function, backend algorithm, storage adapter implementation; published behavior unchanged | Changed module suite(s) only | Same public contract |
| Sandbox backend internal self-test or code reuse with identical ENV-04/05 guarantees | Environment pytest only **plus separate real-host Sandbox proof when relevant** | Enforcement claims require host evidence, even without full pytest |
| Tool implementation changes but input/output/error/ordering guarantees unchanged | Tool pytest only | Same Tool public contract |
| Cross-module refactor retaining all public DTO/behavior contracts | Test each modified module, **no full suite** | File count is not contract evolution |
| Public Port signature, DTO schema, error/exception taxonomy, status, cancellation, idempotency, transaction or ordering guarantee changes | **Full pytest** | Consumer assumptions may change |
| Authorization or Sandbox policy guarantee changes (e.g. network allow, root scope, failure semantics), including behavior with unchanged Python signature | **Full pytest + applicable host security proof** | Semantics are part of the public contract |
| New/remove/rename Tool exposed schema or Provider wire protocol, Data Event schema, model-facing contract | **Full pytest** | Published interface changed |
| Adding a new public Port, removing one, or changing contract ownership between modules | **Full pytest** | Dependency graph changes |
| Documentation-only changes, *no implemented public contract change* | No pytest required | No executable delta |
| Tests-only changes, *no implemented public contract change* | Changed module's pytest | Validate modified tests without a full suite |
| Unknown whether contract changed | **Classify against current contracts before test execution**. If still uncertain, treat it as a contract change and run full | Prevent accidental under-testing |

**Contract granularity (V3-039, V3-046):** "public contract" is judged per **sub-port / SPI** declared in each owner's `api.py` — e.g. a change limited to `Data.ArtifactStore` is a contract change of that sub-port. Full-suite trigger rules above still apply to any such change; the benefit of the split is that internal Data changes behind an unchanged sub-port stay module-only. SPI conformance suites (`ToolHandler`, `EnvironmentPlugin`, `ProviderAdapter`) live in the owner module's tests and are reused by each plugin.

**Semantics include security.** A method name unchanged while the allowed filesystem root, network policy or effects change **is** a contract change. This classification must be explicit in PR/implementation review.

## 2.1 Live provider tests (opt-in gate)

`tests/live/` calls real provider APIs. It runs only with `HIMARI_LIVE=1` and a key (`OPENROUTER_API_KEY` in the environment or the git-ignored `.env`); otherwise it is skipped, so CI and ordinary module/full pytest never touch the network. Like the Sandbox host check, it is a separate evidence gate for Provider Adapter changes, not part of the module/full pytest classification.

~~~bash
HIMARI_LIVE=1 pytest -s tests/live   # optional HIMARI_LIVE_MODELS="model_a,model_b"
~~~

## 3. Test layout (created 2026-10-13 for the api skeleton)

~~~text
tests/
  control/             # Control module tests + its port contract tests
  data/                # Data module tests
  prompt/              # Prompt module tests
  provider/            # Provider module tests + adapter conformance
  tool/                # Tool module tests
  environment/         # Environment module tests, fake protected backend
  integration/         # cross-module composition, used during full pytest
~~~

Pytest examples (illustrative; paths not created yet):

~~~bash
# Only Environment internals changed and ENV contracts are unchanged
pytest tests/environment/

# Tool and Data internals both changed without a contract delta
pytest tests/tool/ tests/data/

# Any public contract introduced/modified/removed
pytest
~~~

Prefer **one test ownership per module**, rather than duplicating identical tests in many directories. Consumer compatibility is maintained by stable shared public contracts and validated at full-suite contract-change gates.

Module tests can use Fake implementations of other modules' public Ports. The fake need not import their private implementation. Static import boundary checks / formatting / type checking are **separate commands**, not a reason to trigger full pytest.

## 4. Review evidence / Test Selection Record (PROPOSED)

Every code-changing PR or change packet should state:

~~~text
PUBLIC_CONTRACT_DELTA: NONE | CHANGED
CONTRACT_IDS: [e.g. ENV-04, ENV-05]  # only when changed
CHANGED_MODULES: [Environment]
PYTEST_SCOPE: MODULE | FULL | NONE
COMMANDS: ["pytest tests/environment/"]
RESULT: pass counts / failure counts / incomplete
LINUX_SANDBOX_GATE: NOT_APPLICABLE | REQUIRED_PASS | REQUIRED_NOT_RUN | FAIL
RATIONALE: Published guarantees unchanged; backend internal implementation only.
~~~

A contract delta must be justified against [contracts.md](contracts.md) and its corresponding detail. If contract delta is NONE, do not run the complete suite merely to gain reassurance. An independent reviewer can reject the classification and require full pytest **because a contract actually changed**, not because a patch was large.

**No claim that a test passed unless it ran and its result was captured.** An incomplete run is INCOMPLETE.

## 5. Linux Sandbox separate acceptance gate

Environment internal modification of Bubblewrap arguments, Landlock policy, mount layout, activation generation, root admission or network namespace may **preserve** published Environment behavior but still require the real Linux host confinement smoke/security check.

- If ENV-04/05 contract unchanged: `pytest tests/environment/` only, plus real-host security gate as appropriate.
- If public guarantees changed: `pytest` for full suite, plus real-host security gate.
- If real-host gate cannot run: report pending/unverified; module pytest PASS is not host certification.
- Host certification requires an execution-compatible deployment-like Linux host and service identity. Do not substitute a Windows unit test or a fabricated test result.

## 6. Open implementation detail

- Module test directories and static CI exist. Future Sandbox host-test commands and any module-specific CI ownership expansion remain to be implemented.
- Automatic detection of contracts changed can be implemented later (manifest diff, manual reviewer attestation, semantic contract changes still need review).
- This testing policy does **not** silently add mandatory full pytest before every merge, release or deployment.
