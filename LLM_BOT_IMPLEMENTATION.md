# LLM Bot Generation - Implementation Plan

Goal: improve bot gameplay by adding a per-player **prompt field** in the lobby.
The prompt is fed to an LLM agent that writes Python `Bot` code, which then runs
through the existing validation / upload / hot-reload pipeline unchanged.

---

## TLDR

The right shape is **generation-time codegen, not runtime LLM control**:

- Add a prompt input next to each player's code textarea in the lobby.
- Add `POST /api/bot/generate` which runs an agentic generate -> validate ->
  repair loop against an LLM.
- Drop the generated code into the existing code textarea. Everything
  downstream (`validate_bot_code`, `uploads/player{port}.py`, `BotLoader`
  hot-reload) already works and needs zero changes.
- Repurpose `core/llm_client.py` into the new generator; the per-frame Ollama
  path is a dead end.

Rough effort: ~1 day. New `core/bot_generator.py` (~150 lines), one endpoint,
~40 lines of lobby HTML/JS, a config section.

---

## Why codegen instead of the current runtime-LLM path

`core/llm_client.py` asks Ollama for a controller action per frame with a 0.5s
timeout. That can never work:

- The game runs at 60fps (16ms frame budget) and `_decision_loop` already
  enforces a 50ms cap per bot call.
- Even a fast local model gives ~1-2 actions per second, so the character
  would twitch between stale inputs.
- Ollama is listed as never-installed in the project TODOs; the fallback is
  always `None` today.

Generating *code* once flips the economics: the LLM's latency (seconds) is
paid at the lobby, and gameplay runs at native speed through the exact bot
interface that already exists.

---

## Design

### 1. Backend: `core/bot_generator.py` (replaces `llm_client.py`)

An async function `generate_bot(prompt: str, character: str) -> str` running a
small agentic loop:

1. **Generate.** Call the LLM with a system prompt containing:
   - the `Bot` / `act(gamestate, player_port)` interface contract,
   - the action dict schema (`stick_x`, `stick_y`, `buttons`),
   - the useful gamestate fields (`position.x/.y`, `stock`, `percent`,
     `action`, `character`),
   - the validator's rules stated as hard constraints (only
     `melee` / `math` / `random` imports, no `getattr`, no dunders, no
     `eval` / `exec` / `open`),
   - the 50ms per-call budget,
   - 1-2 of the existing lobby example bots as few-shot anchors,
   - the target character, so it can use character-appropriate moves.
2. **Validate.** Extract the code block, run it through the existing
   `validate_bot_code()` from `core/bot_validator.py`.
3. **Smoke test in a subprocess.** Import the module, instantiate `Bot`, call
   `act()` against a stub gamestate, assert the returned dict shape. This
   catches the most common LLM failure (runtime errors, wrong return shape)
   that AST validation cannot.
4. **Repair.** On any failure, feed the exact error message back to the LLM
   and retry, up to ~3 attempts. `BotValidationError` messages are already
   written to be user-presentable, which makes them good repair feedback too.

### 2. Provider: Claude API by default, Ollama as config fallback

The validator is strict (no `getattr`, no dunders, whitelist imports) and
local llama3 will fail it constantly, burning repair iterations and still
producing weak gameplay. A hosted frontier model (e.g. `claude-sonnet-4-6` is
plenty for this) will pass validation on the first or second try and write
genuinely better fighting logic - which is the actual goal.

Config sketch (`config/settings.toml`):

```toml
[llm]
provider = "anthropic"          # or "ollama"
model    = "claude-sonnet-4-6"
# api key via ANTHROPIC_API_KEY env var, never in the repo

[ollama]                         # kept as offline fallback
model    = "llama3"
base_url = "http://localhost:11434"
```

Tradeoff to note: needs an API key on the Mac and internet access; per-match
cost is a few cents.

### 3. API: `POST /api/bot/generate`

- Body: `{"prompt": "...", "character": "FOX", "port": 1}`
- Returns `{"ok": true, "code": "..."}` or `{"ok": false, "error": "..."}`
  after the repair loop gives up.
- Generation takes seconds, so it must not touch the game loop - a plain
  async handler, callable while a match is running (see live re-generation
  below).

### 4. UI: prompt field feeds the existing textarea

In each player card's `details.bot-code` section in `lobby.html`:

- a one-line prompt input ("Describe how your bot should fight..."),
- a GENERATE button with a spinner / status line.

On success, write the returned code into the existing `code-{i}` textarea and
open the `<details>` element. This is the key integration decision: generated
code lands in the same field a hand-written bot would, so the user can read
it, tweak it, re-validate via `/api/validate`, and `/api/start` needs no
changes at all.

Persist the prompt alongside code in `_last_form` so it survives matches like
everything else.

### 5. Free bonus: live re-generation mid-match

`BotLoader` hot-reloads `uploads/player{port}.py` on mtime change. Add an
optional `{"apply": true, "port": N}` flag to `/api/bot/generate` that writes
the validated code straight to that file. Players can then re-prompt *during*
a match ("stop jumping off the edge, play defensively") and watch their bot
change behavior seconds later on stream. For a party/tournament setting this
is the single most fun consequence of the design, and it costs almost nothing
given the hot-reload machinery already exists.

---

## Things to watch

- **Prompt injection is a non-issue** because the LLM's output goes through
  the same AST validator as human-typed code. Never skip `validate_bot_code()`
  for generated code; treat the LLM as just another untrusted user.
- **Concurrent generations.** Four players may hit GENERATE at once. Fine for
  the Claude API; would serialize painfully on local Ollama.
- **The 50ms `act()` budget.** State it in the system prompt and time the
  smoke test's stub call, or a generated bot with an accidental heavy loop
  will silently time out every frame and stand still.
- **Heavy construction belongs in `__init__`.** libmelee's `FrameData` loads
  CSVs internally; constructing anything heavy per `act()` call blows the
  budget. Few-shot examples should demonstrate the pattern.

---

## Implementation checklist

- [ ] `core/bot_generator.py`: agentic generate -> validate -> smoke-test ->
      repair loop
- [ ] Delete / repurpose `core/llm_client.py` (runtime decide path is dead)
- [ ] `[llm]` section in `config/settings.toml`; `ANTHROPIC_API_KEY` from env
- [ ] `POST /api/bot/generate` in `frontend/app.py`
- [ ] Lobby UI: prompt input + GENERATE button per player card, result fills
      the code textarea
- [ ] Persist prompt in `_last_form` / `GET /api/last-form`
- [ ] Optional: `apply` flag for mid-match hot-reload re-generation
