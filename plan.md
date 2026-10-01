# Plan: improving the desktop Peer Review App using ideas from `pr-review-app`

> **Status (2026-10-01): implemented**, steps 0 to 7. New modules: `app/review_schema.py`, `review_prompts.py`,
> `context.py`, `publish.py`, `diff_select.py`, `secrets_scan.py`, `cost.py`, `repo_config.py`, `eligibility.py`,
> `checkout.py`, `doctor.py`, `globs.py`; UI split into `ui/`; tests in `tests/`; CI in `.github/workflows/ci.yml`.
> Deviations: repo access uses option A (local checkout) only, so API mode always runs single-shot; the CLI has no
> max-turns flag, so "finish when out of turns" is a `--resume` follow-up when the output doesn't parse; the
> config.json tier idea is replaced by `pr-review.yml` tiers as 3b.8 says.

Scope: the Tkinter desktop app in this repo (`main.py`, `app/*.py`, `data/prompts.json`).
The comparison target is the Laravel GitHub App in `pr-review-app/`; its own plan lives in `pr-review-app/plan.md`.

The desktop app's main strength is **human-in-the-loop control**: every comment is reviewed and edited before it posts, and the author-side flows (bot triage, lint fix, merge conflict) are unique to it. Keep that. Most of the gaps are in **review quality plumbing**: structured output, line validation, context, model settings, and re-review tracking. The Laravel app already solves these.

---

## 1. Weak spots today

| # | Weak spot | Where | Impact |
|---|---|---|---|
| W1 | Repo prompts (SearchAPI, marketplace, Laravel) each ask for their **own output format** (Verdict/Findings, Blocking/Should-fix, Executive Summary…), which clashes with `SYSTEM_OUTPUT_CONTRACT`'s `FILE:/LINE:` blocks | `data/prompts.json`, `app/review.py:11-40` | The model has to choose between two contracts, and parse misses or dropped findings follow |
| W2 | Free-text output parsed by regex; unknown severity silently becomes `nit`, bad LINE becomes `None` | `app/review_parse.py:33-114` | Findings get silently downgraded or turned into orphans |
| W3 | No check that a finding's line is inside a diff hunk before Submit | `review_parse.py:138-143` | GitHub returns 422 on post and the user finds out one card at a time |
| W4 | `Read` is allowed but the subprocess has **no cwd** pointing at the PR repo, so it reads the app's own folder. `gh` works only if installed and logged in inside that environment | `app/claude_runner.py:24`, `run_claude_wsl`/`run_claude_cli` | The model is effectively diff-only and can't check base classes, callers or tests |
| W5 | Old default model `claude-sonnet-4-20250514`; no effort or thinking control | `claude_runner.py`, config defaults | Weaker reviews than current models give; no way to scale depth to PR size |
| W6 | Posting sends each comment as a standalone `POST /pulls/{n}/comments`; no review object, summary or event | `github_pr.py:429-448` | One notification per comment, and no overall verdict on the PR |
| W7 | No context beyond the diff: no CLAUDE.md/REVIEW.md, Jira, CI status, other reviewers' comments or commit history. SearchAPI's Jira/CI placeholders are never filled | `app/review.py:75-109` | The review can't judge against the ticket, and the placeholders ship as literal text |
| W8 | ~~SSR prompt has leftover pasted CodeRabbit fix instructions at the end~~ **Done:** removed from `data/prompts.json` | `data/prompts.json` (SSR Review) | (fixed) |
| W9 | Peer-review contract never says PR text or diff are **untrusted** (the bot, lint and merge flows do) | `app/review.py:11-40` | Prompt injection through the PR description or code comments |
| W10 | No overall timeout on the streaming read loop | `claude_runner.py` read loop | A hung CLI freezes the job forever (`_busy` stays true) |
| W11 | Diff cut at 120K chars mid-file with no priority order; no lockfile/vendor/generated filters | `github_pr.py:214-233` | Hand-written code can be dropped while `package-lock.json` is kept |
| W12 | No budget or cost estimate, only tokens after the fact | `main.py:3069-3088` | No warning before an expensive run |
| W13 | Tokens and API keys stored in plaintext `data/config.json` | `app/__init__.py` | Local secret exposure (gitignored, but plaintext on disk) |
| W14 | Re-reviews: see §4. They compare only against the app's own history, don't track SHAs or fixed status, and never resolve threads | `app/review.py:43-72`, `history_store.py` | Repeat passes can't say what was fixed |
| W15 | No tests; `main.py` is ~3350 lines | whole repo | Refactors like the ones below are risky |
| W16 | The diff goes to the model as **raw patches with no line numbers**. The model has to count from each `@@ -a,b +c,d @@` header to get `LINE:`, and that's where most wrong anchors come from | `github_pr.py:214-233` (`summarize_diff_for_prompt`) | Comments land on the wrong line or get a 422 from GitHub. The Laravel app writes `R12` / `L40` against every line (`FileDiff::annotated()`) |
| W17 | Rule 5 of the output contract **forces a `praise` inline comment** when nothing is wrong, and the follow-up rules do the same | `app/review.py:30`, `review.py:53` | A clean PR gets a noise comment on some "representative" line, and a clean review can't simply say "approve, nothing to report" |
| W18 | No guard against **double posting**: clicking Submit twice, or re-running and submitting again, posts duplicate comments | `SubmitReviewDialog`, `review_parse.py:128-154` | Duplicate threads on the PR |
| W19 | No PR **commit messages or file history** are sent, so the model can't tell a mistake from a deliberate earlier fix | `build_review_prompt` | False positives on code that looks wrong but was a decision |
| W20 | No eligibility check before spending tokens: **draft** PRs, `WIP` / `do not review` titles, merged/closed PRs, or an unexpected base branch run without warning | `start_review` (`main.py:2592`) | Wasted runs and reviews of the wrong thing |

---

## 2. Phase 1: the six highest-payoff changes (from the comparison)

### 2.1 JSON-schema output in place of regex parsing (fixes W1, W2)
**Borrow:** `pr-review-app/app/Review/Findings/ReviewSchema.php`, `ReviewResult::fromArray`.

- Define one schema in a new `app/review_schema.py`:
  - top level: `verdict` (`approve` | `approve_with_nits` | `changes_requested`), `verdict_reason`, `scope_note`, `tests_note`, `findings[]`, `prior_findings[]` (see §4);
  - each finding: `severity`, `file`, `line`, `start_line`, `side`, `title`, `body`, `suggestion`, `confidence` (`high|medium|low`).
- **API mode:** pass the schema through structured output (`output_config.format = {type: "json_schema", schema}`), as `ClaudeCaller` does.
- **CLI/WSL mode:** ask for a single JSON object and validate it locally against the same schema. If Claude Code has a JSON-schema output flag, use it instead (check `claude --help`; it wasn't reachable from the planning shell).
- Keep `parse_review_comments` as a **fallback** for when the JSON fails to parse. Show a banner saying "parsed via legacy fallback".
- Stop defaulting bad severities to `nit`. Drop invalid entries and surface them in the "Unplaced" panel with a reason.
- Strip every output-format section from the repo prompts (see 2.5), so the schema is the only output contract.

### 2.2 Validate lines against the diff before Submit (fixes W3)
**Borrow:** `DiffParser` / `FileDiff::accepts()` (RIGHT = added or context line, LEFT = deleted or context line).

- `app/diff_model.py` already turns patches into rows. Add `accepts(path, line, side) -> bool`.
- In `SubmitReviewDialog` (`main.py:1142-1271`), mark invalid cards "not in diff". Offer "post as file-level comment" or "move to summary", and disable the inline Submit.
- Add multi-line support (`start_line`/`start_side`) because the schema now carries `start_line`.

### 2.3 Batched review posting with a summary (fixes W6)
**Borrow:** `ReviewPublisher` (one `POST /pulls/{n}/reviews` with `comments[]`) and `ReviewSummary`.

- Keep the per-card edit step, but change the default action to **"Submit selected as one review"**:
  - each card gets an include checkbox (default on);
  - the dialog footer has a summary textarea pre-filled from verdict, scope, tests and findings grouped by severity;
  - event picker: `COMMENT` (default) or `REQUEST_CHANGES`. Never auto-`APPROVE`.
- Keep single-card Submit as a secondary action for one-offs.
- Render `suggestion` as a GitHub suggestion code block.
- Put a hidden marker `<!-- peer-review-app sha=<head_sha> -->` in the review body. Re-reviews depend on it (§4).
- If the batched post fails because of bad inline lines, fall back to posting the summary only and list the demoted comments, as `ReviewPublisher.php:41-56` does.

### 2.4 Status-based re-reviews
The full design is in §4. In short: read existing GitHub comments, record the head SHA per pass, and have the model give every earlier finding a status of `fixed` / `open` / `withdrawn` / `changed`, not just "don't repeat".

### 2.5 Auto-load repo conventions and the Jira ticket (fixes W7)
**Borrow:** `ConventionLoader`, `TicketKeyExtractor`, `JiraClient`, `AdfFlattener`, CI summary from `ContextAssembler`.

- **Conventions:** fetch `CLAUDE.md` and `REVIEW.md` from the **base ref** with `/git/trees/{base}?recursive=1` and `/contents`.
  - Order: root file first, then files from directories that contain changed paths (deepest last).
  - Cap at about 120 KB.
  - Put them in the cached system block (API mode).
- **Jira:** extract keys with `\b([A-Z][A-Z0-9]+-\d+)\b` from the title, body and head branch.
  - Optional Jira base URL, email and token in Settings.
  - Fetch summary, description, status and acceptance criteria (custom field matching `/acceptance/i`, or the AC section of the description).
  - Pre-fill the Story box, but keep it editable.
- **CI:** `GET /commits/{sha}/check-runs` → "green", "pending", or "red: failing checks …".
- **Other reviewers:** `fetch_review_comments`, `fetch_issue_comments` and `fetch_pr_reviews` already exist (`github_pr.py:298-318`). Feed them in, tagged untrusted.
- **Prompt placeholders:** add template variables (`{{jira}}`, `{{ci}}`, `{{conventions}}`, `{{depth}}`) filled by `build_review_prompt`. This fixes SearchAPI's unfilled sections.

### 2.6 Current models, effort settings, and real repo access (fixes W4, W5)
**Model and effort**
- Defaults: `claude-opus-5-5` for review and `claude-haiku-4-5-20251001` for bot triage. Offer `claude-sonnet-5-5` as a cheaper option and `claude-fable-5-1` for the deepest reviews.
- Add an **Effort** dropdown (`low` / `medium` / `high` / `xhigh` / `max`) beside the prompt picker, default `medium`.
  - API mode: `output_config.effort` plus `thinking: {type: "adaptive"}`, as `ClaudeCaller.php:40-105` does; omit thinking for Fable.
  - CLI mode: pass the matching Claude Code flag if one exists; otherwise the setting only applies in API mode and the UI should say so.
- Raise API `max_tokens` from 8192 to about 32000, because structured reviews with thinking truncate at 8K.
- **Simple tiers** (lighter version of `TierConfig`): optional per-repo rules in `data/config.json`, e.g. docs-only PRs → Sonnet at low effort, `migrations/**` or `auth/**` → Opus at high effort. The UI shows the chosen tier, and the user can override it.

**Repo access** (pick A; B only if API mode becomes the main path)
- **A. Local checkout (recommended).** pygit2 is already a dependency and `merge_conflict.py` already fetches SHAs.
  - Shallow-fetch the head SHA into a per-PR cache folder (`data/checkouts/<owner>/<repo>/<sha>`).
  - Run Claude with **cwd set to that folder**, and add `Grep` and `Glob` to `ALLOWED_TOOLS`.
  - Delete `secret_paths` files (`.env*`, `*.pem`, `*.key`…) from the working tree before starting.
  - Clean up checkouts older than N days.
- **B. API-backed tools (API mode).** Port `ReviewTools` + `RepositoryReader`: `read_file(path, start, end)`, `find_files(glob)`, `list_directory`, `file_history`.
  - Run an agentic loop capped at about 14 turns.
  - Truncate tool output at about 48 KB.
  - Ask for "write the review now" when the cap is hit.
- Either way, add an `agentic` / `single-shot` mode toggle, matching `Mode` in the Laravel app.

---

## 3. Phase 2: other areas where the Laravel app is ahead

### 3.1 Prompt architecture
Split every review prompt into layers, as `PromptBuilder` does:
1. **Rubric (shared, fixed).** Port `resources/prompts/rubric.md`:
   - review order: ticket, then correctness, then conventions, then tests;
   - severity definitions;
   - "build a mental model" and "sample big diffs and say so";
   - 1–3 sentences per finding;
   - honest `low` confidence;
   - the untrusted-input rule (fixes W9).
   Merge in the desktop's own style rules: no em dashes, the banned-phrase list, and the "real engineer" tone.
2. **Conventions:** loaded from the repo (2.5).
3. **Repo focus prompt:** today's `prompts.json` entries, cut down to *what to look for*, with every output format removed. (The SSR CodeRabbit leftovers, W8, are already removed.)
4. **Parameters:** a depth prompt (port `depth/{blocker,major,minor,nit}.md`), severity floor, max nits (default 5), mode and effort line, and the re-review block when it's a follow-up.

Also add **path instructions** per prompt (`paths: [...]`, `instructions: ...`), e.g. marketplace → `src/Api/**`: "check error responses for info leaks".

### 3.2 Automatic verification pass
**Borrow:** `VerificationPass` plus `verification.md` ("try to knock each one down").

- Add an optional checkbox "Verify findings". It runs a second call with the findings and `confirmed` / `rejected` / `revised` verdicts: rejected findings are dropped, and revised ones are updated in place.
- Keep the rejected ones in a collapsed "Dropped by verifier" panel so the user can bring them back.
- This automates what `build_verification_prompt` already does manually through the clipboard.

### 3.3 Guardrails
- **Timeout** (W10): wall-clock limit on the stream loop (default 15 min, configurable). Kill the process tree and show the partial output.
- **Diff priority and filters** (W11):
  - skip by default: lockfiles, `vendor/`, `node_modules/`, `dist/`, `*.min.*`, generated files, and `secret_paths`, listed as "withheld";
  - when over budget, keep hand-written source first and truncate at file boundaries, never mid-hunk.
- **Secret scanner:** port `SecretScanner` regexes over *added* lines only. Show the hits in the UI as a Security section, and **mask the values before sending**. The Laravel app doesn't mask them; don't copy that gap.
- **Cost estimate** (W12):
  - count tokens in API mode, or estimate at about 4 chars/token, then show "≈ $X" before Run;
  - optionally warn above a per-review limit;
  - port the pricing table from `ReviewModel` and the effort multipliers from `CostEstimator`.
- **Credentials** (W13): store the GitHub token, Anthropic key and Jira token in the Windows Credential Manager (`keyring`), not plaintext JSON.

### 3.4 Dry-run inspection
Add an "Inspect" button, like `review:inspect`. It shows tier, model, effort, mode, ticket keys found, conventions loaded, withheld files, estimated tokens and cost, and does no review. It's cheap, and it helps with debugging prompt selection.

### 3.5 Tests and structure (W15)
- Add `pytest` covering `review_parse`, the new schema parsing, diff-line validation, `prompts_for_repo`, prompt assembly and the re-review matcher (§4). Port fixture ideas from `pr-review-app/tests/Unit/DiffParserTest.php`.
- Start splitting `main.py` along the tab lines (`ui/review_tab.py`, `ui/prompts_tab.py`, `ui/history_tab.py`, `ui/submit_dialog.py`) before the Phase 1 UI changes.

---

## 3b. Phase 3: further items from a full pass over `pr-review-app`
These came out of a second, file-by-file read of the Laravel app (rubric, all prompt files, `PromptBuilder`, `FileDiff`, `ReviewSummary`, `ReviewPublisher`, the engines, config and CI). None of them are covered in §2–§3 above.

### 3b.1 Line-numbered diff in the prompt (fixes W16), highest value in this section
- Port `FileDiff::annotated()` (`pr-review-app/app/Review/Context/FileDiff.php`). Render each hunk line as `R<new_line> +text`, `R<new_line>  text` for context lines, and `L<old_line> -text`. `app/diff_model.py` already computes both numbers.
- Put this instruction above the diff: "`R` is the right-hand side, `L` the left. Anchor findings to exactly these numbers and that side."
- It pairs with 2.2 (line validation): the model cites numbers it can see, and the app checks them against the same parse.

### 3b.2 Port the rubric content itself, not just the layering
§3.1 says to port `rubric.md`. These are the specific rules worth keeping, because the current desktop contract has none of them:
- **Judge in this order:** ticket and acceptance criteria, then correctness, then the repo's conventions, then tests. When a convention document conflicts with generic style, **the convention wins, and the finding cites which document it rests on**.
- **Build a mental model first:** read the README and the manifest (`composer.json` / `package.json`), and for a changed file read what it extends or imports plus a sibling implementation. Say which conventions came from a document and which were **inferred** from surrounding code.
- **History:** use commit messages to tell a mistake from a decision, and say so when the commit message and the diff disagree (needs 3b.6).
- **Large diffs:** read hand-written code line by line, spot-check generated or vendored code, and **say what was sampled**.
- **Anti-anchoring:** reach your own conclusions from the diff *before* reading other reviewers' comments.
- **Suggestions:** offer one only when the fix is unambiguous and fits entirely within the cited lines. No fences, diff markers or ellipses.
- **Confidence:** mark a finding `low` when a file it depends on wasn't read, and say what couldn't be checked.
- **Length:** 1–3 sentences per finding. Lead with the defect, don't explain the ticket back, cut hedges, don't repeat a point in the verdict or notes, and make the verdict/scope/tests notes one sentence each. This complements the desktop's own banned-phrase list and "no em dash" rule, which should stay.
- **Security at every depth:** a scanner match is **evidence, not a verdict**; a real secret is a blocker; the fix is to **rotate** it, not delete the line; a pre-existing untouched advisory isn't this PR's problem.
- **CI at every depth:** failing required checks are at least major; say *what* failed, never just "CI is red".
- **Nothing wrong means approve with no findings** (fixes W17). Delete rule 5 of `SYSTEM_OUTPUT_CONTRACT` and rule 3 of `FOLLOW_UP_RULES`. With the schema from 2.1, a clean review is `verdict: approve` plus an empty `findings[]`, and the UI shows "No findings" with nothing to post inline.

### 3b.3 Other reviewers: the four-bucket model, as part of the main review
§4.4 lists `disagreements` / `credits` as optional. Make them part of every review whenever other comments exist, following the rubric's "Other reviewers" section:
- **Independently found:** keep it and set `also_flagged_by` (the card shows "Also flagged by CodeRabbit").
- **Valid, you missed it:** goes in `credits`; never absorbed silently.
- **Wrong:** goes in `disagreements` with evidence and the comment id. The UI gets a "Reply with rebuttal" button that posts to that thread (`reply_to_review_comment` already exists, `github_pr.py:451`). "A bot comment left unchallenged gets actioned."
- **Already fixed:** don't re-raise it.
- Two checks from the rubric: a reviewer **quoting a document or comment that doesn't exist**, and a reviewer recommending a change that **defeats the PR's stated design**. Also give partial credit ("right symptom, wrong cause").
- This overlaps with the "Check review comments" flow (`bot_review.py`). Keep that flow for the author-side fix and commit work; the peer review gets the lighter four-bucket judgment. Share the comment cleaner (`sanitize_bot_comment_body`) and the CI-bot filter between them.

### 3b.4 Per-finding fix prompts
Port `Finding::fixPrompt` plus `resources/prompts/fix-preamble.md`: one self-contained, copy-pasteable prompt per finding that carries the handling rules and the ticket keys.
- Add a **"Copy fix prompt"** button on each finding card, next to Submit.
- Add an optional collapsed `<details>` block "Fix prompts: one per finding" to the posted summary (2.3), as `ReviewSummary::fixPrompts()` does.
- This goes alongside the existing "Copy as LLM prompt" verification export.

### 3b.5 Comment and summary formatting details
- **Inline comment body** (`ReviewPublisher::commentBody`): `**Blocker: <title>**`, then the body, then "_Also flagged by X._", then for low confidence "_I could not verify this against the surrounding code, treat it as a question rather than a defect._", then a `suggestion` block. Use a colon, not the em dash the Laravel app uses, to keep the desktop's no-dash rule.
- **Summary body order** (`ReviewSummary::render`): verdict headline ("Changes requested: 2 blockers"), ticket, scope reviewed plus **withheld files**, "Since my last review", findings grouped by severity, other reviewers (credits and disagreements), tests, fix prompts, then a footer with model, effort, mode, tokens, cache % and cost.
- **Trim** summaries over 60,000 chars (GitHub's body limit, `GITHUB_BODY_LIMIT`), dropping fix prompts first.
- **Supersede:** when a new review is posted on a PR that has an earlier one from the app, edit the old body to add "Superseded by <link>" (`markPreviousReview`). Optionally offer "Amend": delete the old inline comments and replace the old body with a pointer (`ReviewAmender`).

### 3b.6 PR commits and file history as context (fixes W19)
Port `RepoHistory`: up to 30 PR commits (`GET /pulls/{n}/commits`) plus the last 5 base-branch commits for each of the first 8 changed files (`GET /commits?path=&sha=<base>&per_page=5`). Keep only the first line of each message, capped at 140 chars. Render it as a `# History` section. It's cheap in tokens and backs the rubric's "mistake vs decision" rule.

### 3b.7 Eligibility checks before a run (fixes W20)
Port the checks from `TriggerConfig::ineligibleReason()` and `TierMatch` as **warnings with a "Run anyway" button**, not hard blocks:
- draft PR;
- title contains an ignore keyword (default `WIP`, `do not review`, `DNR`), matched as a whole word with hyphens counted as part of the word, as `TriggerConfig.php:312-321` does;
- base branch isn't in the allowed list;
- PR is closed or merged;
- the head SHA was already reviewed (the §4.6 skip guard).

### 3b.8 Read the same `.github/pr-review.yml` as the Laravel app
Both apps should behave the same on a given repo, so the desktop should read the **same per-repo config file** from the base ref.
- Port the subset that applies: `defaults.{model, effort, mode, verification_pass}`, `tiers[]` (which replaces the `data/config.json` tier idea in 2.6), `path_instructions`, `tone_instructions` (≤250 chars), `language`, `severity_floor` (plus the `important` alias), `max_nits`, `follow_up.severity_floor`, and `triggers.ignore_title_keywords` / `base_branches` for 3b.7.
- Validate each key separately and show problems in the UI. Don't copy Laravel's "one bad key discards everything" behavior (see §5).
- Precedence: UI override, then `pr-review.yml`, then the desktop defaults. The Inspect view (3.4) shows where each value came from.

### 3b.9 Engine behaviors worth copying
- **Finish when out of turns** (`AgenticEngine::conclude()`): when the turn cap is hit, or the output doesn't parse, send one more call with tools removed saying "Stop reading and write the review now". In CLI mode, pass Claude Code's max-turns flag and run the same fallback call.
- **Tool-use habits** (`resources/prompts/agentic.md`): add them to the prompt in both CLI and API modes. Ask for all the reads needed in one turn, read line ranges not whole files (Claude Code's `Read` supports offset/limit), and stop once the findings are justified.
- **No-tools wording** (`single-shot.md`): when repo access is off, "mark findings low confidence where they depend on unseen code".
- **Refusals:** in API mode, map `stop_reason == "refusal"` to a clear "The model declined to review this PR" message, not a parse error (`ReviewRefused`).
- **Prompt caching layout** (API mode):
  - stable, cached system prefix: rubric, then repo profile, then conventions, loaded once per run so the bytes are identical;
  - uncached parameters block last;
  - cache breakpoint on the diff;
  - in the agentic loop, move the breakpoint forward onto the newest tool result.
  Today only the system block is cached.
- **Token pre-count:** use `messages.count_tokens` in API mode for the cost estimate in 3.3, in place of the chars/4 guess.
- **Budget-fit suggestions:** if the estimate is over the warning threshold, offer the Laravel downgrade order as one click: drop the verification pass, then agentic to single-shot, then lower effort (xhigh, then high, then medium).
- **Verification details** (`VerificationPass`): reuse the same cached prefix and swap only the last system block; ask "is the severity honest?" and "is the cited line the real one?"; **keep the original findings if the verifier's output can't be parsed**.

### 3b.10 Security context from Dependabot
Port `SecurityScanner`: fetch `/dependabot/alerts`, keep only alerts on manifests this PR changes (plus their lockfile companions), and show "scan unavailable" (not "clean") if the call fails. It goes in a `# Security scan` section next to the secret-scanner hits from 3.3. Needs a token with Dependabot alert read access; hide it when that permission is missing.

### 3b.11 Doctor button
The desktop has auth status lights. Extend them into a **Doctor** check along the lines of `review:doctor --repo`:
- the Claude CLI is found in WSL and on Windows, and `claude auth status` passes;
- `gh` is installed and logged in inside the environment Claude runs in (needed for the `gh api` tools);
- the configured model id is valid;
- the GitHub token can read the repo, and has Dependabot access for 3b.10;
- Jira credentials work;
- every prompt in `prompts.json` loads, and none still contains output-format sections (§3.1);
- `pr-review.yml` on the base branch parses (3b.8).

### 3b.12 Tooling from the Laravel repo
- Mirror its CI (`ci.yml`: `pint --test` + `pest --ci`): add a GitHub Actions workflow running `ruff check`, `ruff format --check` and `pytest` on push.
- Add Dependabot for `requirements.txt` (copy `pr-review-app/.github/dependabot.yml`).
- Port the tests that carry over (from `pr-review-app/tests/Feature` and `Unit`): diff parsing (`DiffParserTest`), secret scanner properties (`SecretScannerPropertiesTest`: known placeholders never match, real-shaped keys always do), cost estimate calibration (`EstimatorCalibrationTest`), prompt assembly (`PromptsTest`), and follow-up floor behavior (`FollowUpLevelTest`).

### 3b.13 Double-post guard (fixes W18)
Borrow the Laravel app's dedupe habit (`ShouldBeUnique` on `repo:number:headSha`, delivery-id dedupe):
- Disable a card's Submit once it has posted, and show "Posted ↗" with a link to the comment.
- Before posting, fetch the PR's existing comments and skip any whose fingerprint marker (§4.2) matches for the same head SHA. Show "already on PR" on that card.

## 4. Re-review redesign (combining both apps)

Neither app records **which commit a review covered**, and neither fingerprints findings. Desktop follow-ups rely on its own history text; Laravel's rely on a body marker plus title-substring thread matching. The design below takes the best of both and fills the shared gaps.

### 4.1 Record what each pass covered
- Add `head_sha`, `base_sha` and `findings` (structured JSON, not raw text) to `HistoryEntry` (`app/history_store.py`).
- Write the same data into GitHub:
  - review body marker: `<!-- peer-review-app sha=<head_sha> v=1 -->`;
  - each inline comment: a hidden fingerprint `<!-- pra:fp=<hash> -->`.
- GitHub is the source of truth, so a re-review still works on another machine or after clearing history. Local history remains a cache.

### 4.2 Finding fingerprint (neither app has one)
`fp = sha1(normalized_path + symbol_or_enclosing_function + normalized_title)[:12]`.

- Line numbers are left out, so the fingerprint survives code moving.
- Ask the model for `symbol` (the Laravel schema already has it). Fall back to the nearest hunk header function.

### 4.3 What a re-review sends
1. **Incremental diff since the last reviewed SHA:** `GET /repos/{o}/{r}/compare/{last_sha}...{head_sha}`, labeled "Changed since your last review". Also send the **full PR diff** so fixes can be checked in context. If the last SHA was force-pushed away (compare returns 404), use the full diff and say so.
2. **Prior findings list** from the marker comments. For each: fp, severity, title, file/line (mapped forward, see 4.5), thread URL, and whether the thread is already resolved.
3. **Other reviewers' comments** (CodeRabbit, Amazon Q, humans), tagged untrusted, as Laravel's `PriorReviews` does.

### 4.4 What the model must return
Use Laravel's `re-review.md` contract with the desktop's stricter scope:
- `prior_findings[]`: **every** earlier finding with `status` = `fixed` / `open` / `withdrawn` / `changed`, plus a note and file/line.
  - `fixed` only if verified in the new code. Laravel rule: "only verified-fixed counts as fixed".
- `findings[]` (new only): apply the **follow-up severity floor** (default `major`, configurable; the current desktop rule is blocker/major only).
  - The floor applies only to *new* findings. `prior_findings` are never filtered (Laravel behavior).
  - Also drop any new finding whose fp matches a prior one, in code, so dedupe doesn't rely on the prompt alone.
- `disagreements[]` / `credits[]` for other reviewers: part of every review now (see 3b.3). Show them in the UI, and post a rebuttal only when the user clicks.

### 4.5 Matching to threads
- Match own threads by **fingerprint marker**, which is exact. This replaces Laravel's title-substring match.
- Match other reviewers' threads by path plus a line mapped forward through the compare diff, within ±N lines. Show these as *suggestions* only.
- Map lines forward with the hunk offsets from the compare diff: `diff_model.py` already has the old/new line numbers.

### 4.6 What the user sees and posts (desktop)
- A **"Since last review"** panel above the new findings, with ✅ fixed / ⏳ open / ↩ withdrawn / ✏ changed rows, each linked to its thread.
- For fixed or withdrawn items, a checkbox "Reply + resolve thread". It is **on by default for the app's own threads and off for other reviewers' threads**.
  - The reply says "Verified fixed in `<sha7>`: note". Resolving uses the GraphQL `resolveReviewThread` mutation (port `ThreadResolver`).
- The summary body gets a "Since my last review" section, and the previous review body gets a "Superseded by …" line.
- **Skip guard:** if `head_sha` equals the last reviewed SHA, warn "No new commits since last review" before spending tokens.

### 4.7 Limits
- Keep `MAX_PRIOR_PASSES = 3` for raw text. Structured prior findings are small, so send **all** of them and drop older raw summaries first.

---

## 5. What the desktop app should *not* copy
- Webhooks, the queue, Redis or the GitHub App: the desktop is manual and runs for one user by design.
- Automatic posting with no preview: human-in-the-loop is the desktop's core advantage.
- An always-`neutral` check run: the desktop doesn't create check runs.
- The Laravel config-parse behavior where one bad key discards the whole config. If per-repo config is added, validate each key separately.

---

## 6. Suggested order

| Step | Items | Why this order |
|---|---|---|
| 0 | Tests scaffold + split `main.py` (3.5), CI workflow (3b.12) | Makes the rest safe |
| 1 | 2.1 schema output, **3b.1 line-numbered diff**, 2.2 line validation, 3.1 prompt layers + **3b.2 rubric rules** (incl. W9, W17) | These depend on each other; this fixes the format clash |
| 2 | 2.6 models/effort + repo checkout, 3.3 timeout/filters | Biggest quality jump |
| 3 | 2.3 batched posting with marker + fingerprints, 3b.5 comment/summary format, 3b.13 double-post guard | Needed before re-reviews |
| 4 | 2.5 conventions/Jira/CI/other reviewers, 3b.3 four-bucket other reviewers, 3b.6 history, 3b.8 shared `pr-review.yml` | More context, and both apps behave the same per repo |
| 5 | §4 re-review redesign | Depends on steps 3 and 4 |
| 6 | 3.2 verification + 3b.9 engine behaviors, 3.3 secrets/cost/keyring, 3.4 inspect, 3b.7 eligibility warnings | Polish and safety |
| 7 | 3b.4 fix prompts, 3b.10 Dependabot, 3b.11 Doctor | Nice-to-haves |
