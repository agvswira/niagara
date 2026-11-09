# Niagara design direction

Design Read: an operations console for a distributor's order admin, AR staff and supervisor, used all day; a calm
light, airy console in the spirit of modern CRM dashboards (latest reference: a light sales dashboard the team
liked; earlier a dark approval tool), with Niagara's own colour and motif. Dial ENERGY 2 / RHYTHM 2 / MOTION 2.

## Identity

- **Name and motif.** Niagara is a waterfall: work flows down from the customer's chat, through the agents, to a
  decision. The motif is the **flow rail**, a thin vertical line with nodes that connects steps. It appears
  wherever work moves through stages: the agent chain on an order, the trace of one agent run, the chat side
  panel. It is never used as decoration elsewhere.
- **Logo.** A lowercase n in ink with the Kas masuk flower in place of the full stop: the moment the money lands
  is where the flow ends. The n follows the theme (ink on light, near-white on dark); the flower stays green.
- **Accent.** One deep blue-green (teal), the colour of falling water. Used for the primary action on a screen,
  the active nav item, and the flow rail. Nowhere else (the chat simulator uses its own WhatsApp-like greens).
- **Agent mascots.** Each of the six agents is one flat shape with two dot eyes, no props: Intake a blue circle,
  Fulfillment an orange rounded square, Kredit a purple hexagon, Penagihan a pink diamond, Kas masuk a green
  four-petal flower, Supervisor an indigo arch. Shape and colour both differ, so agents stay distinguishable for
  colour-blind readers too. The colours appear only on the mascots and a small dot beside the agent's name, which
  is always written out. Drawn in code (SVG), not generated images.
- **Status colours** are functional, not palette: amber = waiting on a person, red = blocked or failed,
  green = done. They appear only as tinted pills, bars and icons, always with a text label.

## Palette

| Token | Light (default) | Dark | Use |
|---|---|---|---|
| `--bg` | #F5F6F8 | #0E1114 | page |
| `--surface` | #FFFFFF | #15191D | panels and cards |
| `--surface-2` | #F2F4F7 | #1B2025 | hover, inner blocks, chips |
| `--line` | #E6E8EC | #262C32 | card borders and dividers (decorative, no contrast duty) |
| `--field` | #858D9B | #5F6B75 | input borders (3:1 non-text) |
| `--ink` | #101828 | #E7ECEF | text |
| `--muted` | #5D6675 | #919CA5 | secondary text (min 5.2:1 on surfaces) |
| `--accent` | #0A7F73 | #2FC2B0 | primary buttons, rail, active nav, brand mark |
| `--accent-ink` | #08695F | #5CD4C5 | accent text and links |

Light is the default because the people using it work in an office in daytime and the latest reference the team
chose is light; dark stays complete and is one click away. The switch under the sidebar is a small track with a
sun on the left and a moon on the right; the knob slides over the active mode and carries its symbol, so the state
reads without a label.

Contrast was checked with the antislop contrast script; every text pairing passes 4.5:1, borders pass 3:1.

## Type

- **Satoshi** (Fontshare, free for commercial use) for everything: geometric like the reference, firm at large
  figure sizes and calm in small table text. The team picked it from a side-by-side sheet of six candidates.
- **IBM Plex Mono** only for codes people copy or match: order, invoice and SKU numbers.
- Section labels are sentence case at 12px semibold, not wide-tracked capitals.

## Composition

- **Shell:** a left sidebar because there are nine destinations and people switch between them all day; it also
  carries the simulation clock and the signed-in role, which every screen depends on. On phones it becomes a
  menu sheet.
- **Meja kerja** is built around one decision: what needs a person now. Three KPI cards of real counts, then the
  decision queue as a table, and on the right an activity rail of real agent runs grouped by day (today's counts,
  filters by work area, a timeline with the agent named on each entry, "show more" per day). Opening a row slides in a side sheet with the reason,
  the rule checks, the agent chain and the action buttons, so the list stays in view.
- **Percakapan** is the one screen that deliberately looks like WhatsApp Desktop (layout and colours taken from a
  community Figma recreation), because the team wants viewers to picture the real channel at a glance. It says it is
  a simulator in three places: the list heading, the chat subtitle, and a pinned note in the thread where WhatsApp
  shows its encryption notice. No WhatsApp logo, icons, wallpaper art, or photos are copied. Eight contacts have
  profile photos generated with Figma AI (fictional people, not stock photos of real ones, so no real face is tied to
  a made-up debt); the rest use a plain silhouette, as WhatsApp does for contacts without a photo. Greys were nudged
  so every text pairing still passes 4.5:1.
- **Numbers shown are real.** Every count and rupiah value comes from the API. No invented deltas or trends.
- Radii: 10px controls, 16px cards and panels, 999px only for status pills, filters and avatars. Cards are flat
  with a hairline border; only the side sheet, dialogs and toasts carry a shadow because they sit above the page.

## Motion

Purposeful only: the side sheet slides in from the right (keeps the list as context), toasts rise from the
bottom, rows get a hover tint. Everything stops under `prefers-reduced-motion`.

## What we do not do

No gradients, glow, glass, background patterns, emoji, stock avatars or photos of real people (initials in the
console; AI portraits of fictional people or a silhouette in the chat simulator), fake statistics, or a copy of the
reference product. The chat simulator mimics
WhatsApp on purpose and always labels itself as a simulator.
