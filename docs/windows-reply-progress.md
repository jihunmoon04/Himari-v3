# Windows reply preparation

## Design

An accepted message or this device's job start opens the assistant bubble before
the first answer text. The bubble displays the web sun theme's waiting vocabulary,
an animated glyph, elapsed time and Stop. The wording is chosen locally, once per
model run; it is not generated reasoning. The existing `/live` stream supplies
run/job identity, text and tool names. No server or provider contract changes.

The first text replaces the waiting control in the same bubble, retaining its
timeline position. Before text, a streamed tool name displays `Preparing <tool>…`.
A new model run clears the previous tool and starts a fresh elapsed counter.
Copy and Expand become available with answer text. The waiting label never enters
Markdown source, clipboard, history or the reading-duration calculation.

Preparation holds the bubble open. Stop acknowledgement replaces the label and
stops animation; it does not pretend the server has finished. Bodyless completion,
transport shutdown, conversation changes and window disposal clear preparation.
Manual dismissal suppresses further waiting displays for that job; actual answer
text retains the existing display behavior. Late acceptance cannot erase text or
reopen a completed reply. OS-disabled client-area animation uses a static glyph.

The initial accepted-job display and first run snapshot have separate identities:
the run snapshot starts the per-run counter. Vocabulary currently mirrors the
web sun list in `ReplyProgressView.cs`; changing that list needs an explicit sync.
The Windows bubble has no web-theme selector, so the dark-theme word list is not
used. Elapsed time measures local display time, not server inference duration.

## Verification scope

PUBLIC_CONTRACT_DELTA: NONE
CHANGED_MODULES: Windows companion UI
PYTEST_SCOPE: NONE
LINUX_SANDBOX_GATE: NOT_APPLICABLE

Build and run the Windows contract and UI suites. UI coverage includes a staged
HTTP/SSE response, accepted-send waiting, elapsed time, tool/run transitions, first
text in the same bubble, late acceptance, Stop acknowledgement, bodyless completion,
foreign conversations, manual dismissal and disconnect timer cleanup. Existing
reply-recovery tests cover both HTTP-acceptance/Live-completion orders.

External review should also exercise a real deployed server, a long model wait,
screen attachment, Stop, and network loss/reconnect on the user's desktop.

## Self-review evidence

- `dotnet build clients/windows/Himari.Companion.UiTests/Himari.Companion.UiTests.csproj --no-restore`: passed. Existing CS0649 warning for `confirmResend` remains.
- `dotnet run --project clients/windows/Himari.Companion.ContractTests/Himari.Companion.ContractTests.csproj --no-restore`: passed.
- `dotnet run --project clients/windows/Himari.Companion.UiTests/Himari.Companion.UiTests.csproj --no-restore -- --reply-progress-only --screenshots build/reply-progress-review`: passed; the rendered preparation bubble was visually inspected.
- `dotnet run --project clients/windows/Himari.Companion.UiTests/Himari.Companion.UiTests.csproj --no-restore -- --reply-flow-only`: passed. This scope runs keyboard/IME, bubble order/expiry/layout, reply recovery, staged preparation SSE and wake-follow checks. It explicitly excludes artwork/animation-preview checks and foreground-dependent `PresentationChecks`.
- Unfiltered UI suite: **not passing**. It fails `mood reaches the character and every mood talks`; the same assertion fails in a separate checkout of unchanged `ca922a3`. One earlier attempt reached the foreground-sensitive type-to-chat assertion and failed there. No full UI pass is claimed.
- No deployed-server/provider session or installed-app update was performed.

Self-review corrections: clear preparation on a saved-final/question refresh as
well as Live completion; clear tool state between runs; stop empty-answer reading
timers; distinguish explicit dismissal from ordinary conversation/transport cleanup.

## PR #160 review follow-up

- P2-1: retain the job identity and full Live display state (run and latest tool)
  for each open stream, including the period before its first frame. Selecting
  a running conversation restores its preparation from that cache or its accepted
  pending request. It does not require another SSE frame or a second connection.
- P2-2: detail reads capture the preparation generation before awaiting HTTP.
  A new preparation invalidates earlier reads, and a mismatched job cannot change
  the current preparation or live display. The checks precede stop/status/question
  updates; generation is rechecked after history/recovery awaits as well.
- Regression coverage: A → B → A without any SSE frame, return during silent SSE
  with a cached tool/run, and delayed old-job detail both requested before and
  after a new preparation. Matching completion still clears the waiting UI.
- Revalidation: `--reply-progress-only`, `--reply-flow-only`, and the Windows
  contract suite passed. The build retains the existing CS0649 `confirmResend`
  warning. The unfiltered UI suite was not rerun for this follow-up; its previously
  documented baseline failure remains outside this claim.
