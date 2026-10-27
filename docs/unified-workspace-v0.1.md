# Unified conversation/files workspace

Navigation remains a separate sidebar or overlay drawer, with its existing
collapse/slide animations. The shared conversation/files toolbar retains the
deployed chrome height (56px, 64px for coarse pointers, plus the existing safe
area inset). Fullscreen sits immediately before the navigation button in both
the sidebar header and the collapsed/compact toolbar.

The icon-only workspace button cycles:

1. Conversation only.
2. Conversation main, files sub.
3. Files only.
4. Files main, conversation sub.
5. Conversation only again.

The main view is leftmost on desktop and uppermost below 1100px. Both split
orientations support resizing. Horizontal and vertical preferences are saved
separately; the existing desktop width preference is retained. Hidden panes
leave the focus order. Switching reorders the live DOM, without reopening the
file, reloading an editor, resetting PDF ink, or moving the conversation draft.
Existing view-transition snapshots animate the cycle and navigation; reduced
motion, unsupported browsers, skipped transitions and rapid clicks still commit
each action once.

The toolbar is fixed across all four modes and does not follow pane focus.
Model/reasoning controls stay visible, the folder icon always opens File tools,
and the final More button always opens the conversation menu. File tools are
disabled, with their position reserved, while files are closed. The original
file controls occupy one bounded popover at every width; Back/Up,
breadcrumbs, editing, review, file menus and PDF zoom remain available. PDF ink
tools stay beside their PDF. Tool-app fullscreen retains its page-covering
behavior above the toolbar. No provider, MCP, data or machine API changes.

The cycle replaces the previous conversation Files/Expand/Close controls and
compact Chat/Files tabs. Standalone `/files` keeps its existing sidebar entry,
folder conversations and compact return navigation.

Browser validation: `scripts/touch_smoke.py` checks the complete cycle at 320,
390, 768, 1024, 1180 and 1366px, retained drafts and dirty editors, Back/Up and cancel,
header height, pane order, rapid clicks, reduced motion, resizing and fullscreen
button placement. `scripts/mcp_apps_smoke.py` covers tool-app fullscreen and its
Back/Forward/Escape behavior. Physical iPad Safari/Apple Pencil remains a device
validation step.
