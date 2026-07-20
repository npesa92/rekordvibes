# Resolve-agent prompt (metadata adjudication)

This is the canonical prompt for the delegated model agent that adjudicates
ambiguous metadata cases. The main session spawns it with the Agent tool
(model: sonnet) — it is a subagent, not an API integration.

## How the main session uses this file

1. Run `rbx resolve export [--playlist NAME]` — it prints the cases file path
   and the expected verdicts file path.
2. Spawn an Agent (subagent_type `general-purpose`, `model: "sonnet"`) with
   the prompt template below, filling in both paths.
3. When it returns, sanity-check its summary, then run
   `rbx clean --verdicts <verdicts file>` (dry run), show the user, confirm,
   re-run with `--apply`.

The agent only reads the cases file and writes the verdicts file. It must not
touch the database, run `rbx`, or modify any other file.

## Prompt template

> You are adjudicating DJ-track metadata for a rekordbox library. Read
> `{CASES_FILE}`. Each case has the track's current DB fields (`cur_title`,
> `cur_artist`, `cur_album`), its file `path`, and an uncertain `parser`
> proposal (regex-derived — treat it as a hint, not truth).
>
> For each case decide the correct **artist**, **title**, and (only if clearly
> present in the string) **album** and **track_no**. Use your music knowledge —
> that is why you were delegated this.
>
> Conventions (these are the library owner's rules — follow them exactly):
> 1. **Remix/flip credit**: in "Song (X Flip)" or "Song (X Remix)", X — the
>    remixer/flipper — is the artist. Same for Bootleg/Rework/Refix/Reboot/
>    Mashup. The title keeps its descriptive form, e.g. title
>    "My Way (Ascension Remix)", artist "Ascension".
> 2. **Inversion check — the reason you exist**: sometimes the *flipper* is
>    before the dash and the *famous source artists* are in the parens, e.g.
>    "Adriance - The Game-50 Cent x Lil Wayne Flip". Use world knowledge:
>    well-known rappers/pop artists named inside a flip title are the source
>    material, not the artist. The lesser-known producer name is the artist.
> 3. "A - B Flip" with no parens: A is the artist, "B Flip" is the title.
> 4. Normalize ft./featuring to "feat." and keep it in the artist field.
> 5. Bandcamp pattern "Artist - Album - NN Title": split all four fields.
> 6. Never invent an album. Leave album null unless the string contains one.
> 7. Do not change anything you are not confident about — use `"skip": true`.
> 8. Paths starting with `soundcloud:` are streaming tracks; adjudicate them
>    the same way (fixes are DB-only, handled downstream).
>
> Write your verdicts to `{VERDICTS_FILE}` as JSON:
>
> ```json
> {"verdicts": [
>   {"id": "123", "artist": "Ascension", "title": "My Way (Ascension Remix)",
>    "album": null, "track_no": null, "conf": "high",
>    "reason": "Ascension is a known dubstep producer; My Way is the source"},
>   {"id": "456", "skip": true, "reason": "cannot tell which name is the producer"}
> ]}
> ```
>
> Rules for the output: `id` as a string, copied exactly from the case. Only
> keys `id, title, artist, album, track_no, conf, reason, skip` are allowed —
> anything else makes the whole file rejected. Omit or null any field you are
> not changing. `conf` is "high" (certain), "medium" (probable), or "low"
> (guess). Every verdict needs a one-line `reason`. Cover every case in the
> file — skip explicitly rather than omitting.
>
> Do not touch any other file, do not run any command against the rekordbox
> database. When done, return a summary: counts by confidence, number
> skipped, and the 5 most interesting judgment calls with reasoning.
