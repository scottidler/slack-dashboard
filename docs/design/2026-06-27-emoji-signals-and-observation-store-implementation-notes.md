# Implementation Notes: Emoji State Signals & Observation Store (Triage v3.1)

Design doc: `docs/design/2026-06-27-emoji-signals-and-observation-store.md`

## Phase 1: Spiking glyph

### Design decisions

- `HeatConfig.spiking_threshold: int = 15` added via the existing `_KebabModel` alias generator (`_snake_to_kebab`), which auto-maps to `spiking-threshold` in YAML - no explicit `Field(alias=...)` needed; matches how all other `HeatConfig` fields are declared -- `config.py:HeatConfig`
- `_SPIKING = "\N{HIGH VOLTAGE SIGN}"` constant added alongside `_ZOMBIE`, `_FIRE`, `_VIP` in `web.py`; glyph render order is vip, spiking, fire, zombie (new/unanswered reserved for Phases 3/4) -- `web.py:_emojis`
- `_emojis` computes `replies_in_window` once into a local `riw` variable to avoid calling the function twice (for the glyph check and the debug log) -- `web.py:_emojis`
- Debug log entry added to `_emojis` matching the surrounding pattern in `heat.py` (function name, channel, thread_ts, key computed values) -- `web.py:_emojis`
- `velocity-weight` set to `5.0` in `slack-dashboard.example.yml` (was `0.0`); 5.0 is large enough to materially move the score on a spiking thread without overwhelming the base score on moderate-velocity threads -- `slack-dashboard.example.yml`
- `spiking-threshold: 15` also added to the example yml so the file documents the knob alongside the weight it works with -- `slack-dashboard.example.yml`
- Legend tooltip in `index.html` updated with the hex entity `&#x26A1;` (HIGH VOLTAGE SIGN) and a plain-English description matching the other legend entries -- `templates/index.html`

### Deviations

- The prior `_emojis` had zombie listed first in the glyph order (zombie, vip, fire). Phase 1 reorders to vip, spiking, fire, zombie. The design doc's intended final order is "new, vip, spiking, fire, zombie" (unanswered leads when on). This reorder moves zombie to last, which is the correct final position per the spec. The pre-Phase-1 order was not explicitly specified in the v3 doc; adjusting now avoids a second reorder in Phase 3.

### Tradeoffs

- Reordering zombie to last vs keeping it first - reordering now means zero additional churn in Phase 3; the small risk is that any existing user observing the glyph sequence sees zombie shift position. Given zero external users of this private tool, accepted.
- `velocity-weight: 5.0` in the example vs a larger value - 5.0 was chosen as "materially non-zero" that provides visible score lift on spiking threads (15+ replies in 30 min adds 15/30 * 5.0 = 2.5 to the score) without overpowering the base term for typical threads. The real private config can tune further.

### Open questions

- None.
