# WebUI tour

Open `Plugins → LivingMemory → Pages → dashboard` in AstrBot.

The dashboard adapts to desktop and mobile layouts. On narrow screens, use the top-left navigation button to open the sidebar; tables, toolbars, maintenance actions, and detail dialogs reflow to the available width. Compact landscape layouts keep primary actions visible.

## Daily pages

| Page | Purpose |
| --- | --- |
| Knowledge graph | Inspect Timeline-derived entities and relations |
| Timeline memory | Search, edit, stage, import, export, and inspect Topic links |
| Topic memory | Inspect read-only Topics, fragments, facts, actors, affect, and provenance; search by keyword or Embedding similarity |
| Supplemental profiles | Add stable-ID hints for otherwise ambiguous actor information |
| System overview | Review memory, models, indexes, maintenance, and database status |

## Maintenance

The bottom sidebar keeps **Settings** and **Maintenance** separate from daily browsing. Maintenance includes Topic and Timeline operations, user profiles and persona relationships, account binding and historical rebuild, session audit, database health, recent production recall, recall tests, and model tests.

Long-running tasks expose stage, processed count, and state. Failed records retain a readable error and can be manually cleared.

The profile detail header mirrors the derived architecture rather than presenting another editable memory. **Scope** shows enablement and Bot/persona; **Objective profile** shows its revision and active, pending, conflict, and stale counts; **Persona relationship** shows its revision and whether the current persona was resolved at execution; **Maintenance** shows active jobs and unprojected events. The strip uses four columns on desktop, two at medium widths, and one on mobile.

The effective-injection preview reuses the runtime profile renderer and reports actual characters, facts, and relationship inclusion. Pending, conflicting, and stale facts are explicitly non-injecting, while active maintenance jobs use a warning state so they cannot be mistaken for a completed publication.

Split/merge previews preserve the selected primary Topic and fragment groups across redraws. Confirmation controls lock immediately while an operation is running, following the same duplicate-submit contract as other long tasks.
