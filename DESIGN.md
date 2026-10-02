# Fantasy Basketball Assistant UI

Issue: https://github.com/leo110047/fantasy-basketball-assistant/issues/1

## Product structure

One product with auction and season modes. The season starts with today's work;
setup appears first only when required data is unavailable. Preserve each mode's
data identity: a frozen auction input is different from a synchronized league.
NBA projection research is secondary to the user's Fantasy roster.

## Visual language

- Forest green ink and navigation, warm ivory background, white working surfaces.
- Amber indicates attention; red indicates a failed or destructive operation.
  Every status also has a text label. Color is never the only signal.
- System sans serif with Traditional Chinese fallbacks. Body 14–15px, labels 12–13px,
  page titles 28–32px. Numeric columns use tabular figures.
- Spacing follows 4/8/12/16/24/32px. Compact tables, quiet separators and restrained
  6–12px corners; avoid repeated nested cards of equal visual weight.
- Basketball imagery belongs in entry/empty states. Player identity uses explicit
  provider identity when an image is available, otherwise visible initials.
- Desktop navigation is persistent. At narrow widths, navigation becomes a
  horizontally scrollable row and content becomes one column; tables retain
  independent horizontal scrolling.

## Interaction

Use buttons for actions, links for navigation, checkboxes for independent choices
and completion records, selects for named exclusive choices, numeric inputs for
precise quantities. A completion check records an action the user already performed
on Yahoo; it never submits a roster change. All form inputs have visible labels.

Place actionable work before analysis. Keep data age, failed/incomplete calculations,
injury assumptions and material limitations visible. Group formulas and technical
provenance behind named disclosures without dropping trace access.

Use existing backend results without recomputing business scores in the UI. Keep
missing values distinct from zero, preserve configured scoring/category semantics,
and distinguish percentages from percentage-point changes.

Keyboard focus is clearly visible. Dialogs have a labelled close control. Support
reduced motion. Loading and error status are announced without clearing user input
or claiming that an unfinished calculation succeeded.

Interactive trade searches show the eligible candidate total, completed candidates
and current preparation/evaluation phase, without elapsed time or an ETA. Timing
targets are performance goals, not automatic stop conditions for this workflow.
Users can cancel a search; fully resolved candidates are retained as explicitly
partial results, ranked only within the completed subset. The remaining work is
drained before another calculation is accepted. Results have a named section and
remain available when the user navigates or reloads the current search.
The selected league's saved search is independent of the most recent job: opening
an analysis or computing partners must not erase it on reload. Restored records
show their saved time and identify themselves as a previous calculation; they do
not imply that new data or settings have been applied to old results.

Trade search results recommend only completed trades whose own gain and expected
gain are positive beyond the configured numeric tolerance. Keep all completed
results internally. Start with ten recommendations ordered by expected gain;
users can reveal more, filter directly in the opponent and outgoing-player column
headers, and toggle the four numeric headers between descending and ascending order.
Filtering to zero rows preserves the table headers and offers a clear-filters action.
Each row has an explicit analysis button, separate from its numeric values. Table
browsing remains usable during calculation; actions that start another calculation
stay disabled even after filtering redraws the view. An outgoing
player filter includes multi-player bundles containing that player. New search
results clear the previous filters and reset expected gain to descending order;
incomplete searches remain visibly partial.

## Verification and delivery

Inspect current source and synthetic live pages at desktop, split-window and narrow
widths. Verify main actions, missing-data states, disclosures and keyboard access.
Synthetic UI checks do not prove live Yahoo/provider operation. Each delivery batch
requires Goldband review and a separate commit. Work stays in the UI worktree;
unrelated model or data fixes require a separately evidenced bug issue.

## Image asset

`src/fba/runtime/static/court.jpg` is a locally served image generated with
ImageGen for this UI. Direction: a worn basketball on a forest-green court,
natural afternoon light, quiet space for text; no people, text, logos or branding.
The image is decorative and adds no claim about a player or actual game.

## Shared entry

`fba app` owns the mode picker and launches the existing local tools. Each mode
keeps its own origin, session token, lock and data owner. The picker stores only
the last auction file paths in the user data directory. Mode switching navigates
back to the picker in the same tab; no data migration or combined calculation API
is introduced. Closing the workspace stops only the processes it started.
Existing separately launched tools stay under their original owner.
