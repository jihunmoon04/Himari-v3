# Mobile/tablet workspace review

Reviewed the unified workspace in Chromium and Playwright WebKit 26.5 on
Windows, using an isolated real HIMARI application, scripted model responses,
and browser-local file/PDF fixtures. No production data or model calls.

## Screen configurations

320×568, 390×844, 844×390, 768×1024, 820×1180, 1024×768, 1180×820 and
1366×1024, with touch input enabled. Also tested 350px-high viewports to emulate
reduced visible space, both existing Classic/Warm themes, a long filename,
model and reasoning controls together, editable text and a real PDF fixture.

## Corrections

- On narrow phones, PDF zoom/page controls move into the existing horizontally
  scrollable ink row. The file header stays compact; live controls and the
  selected pen survive resizing back to tablet width.
- In short compact viewports, PDFs retain 180px of pane space; the timeline's
  vertical padding contracts and the composer grows only within its available
  pane height. A long draft remains scrollable without hiding Send or the PDF.
- At tablet widths of 768–1099px, the conversation title is shown and the model
  and reasoning pickers are bounded rather than stretching across the header.
- Resize-driven layout writes are coalesced into animation frames, avoiding
  WebKit's undelivered ResizeObserver notification error.

Desktop's normal layout and header dimensions remain unchanged. File controls
remain in the compact local file header, with no global File tools popup.

## Validation

The browser audit checks visible control bounds, root overflow, file menu bounds,
PDF viewport height, composer bounds, and uncaught page errors. After correction,
the tested configurations have no remaining recorded layout issues or page
errors. `scripts/touch_smoke.py` adds repeatable regressions for a multiline
draft and PDF in a short viewport, live PDF tool relocation across the phone
breakpoint, pen selection and draft retention. Existing navigation, fullscreen,
rapid-cycle, reduced-motion, dirty-editor and resize checks continue to pass.

This is browser-engine and viewport validation. Physical iPad/iPhone Safari,
native on-screen keyboard behavior, Pencil input and OS fullscreen overlays
still need validation on the actual devices.
