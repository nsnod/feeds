# feeds.yaml mistakes

Real `config/feeds.yaml` files that went wrong, kept byte for byte so the tests prove
GemBot keeps working (and says exactly what to fix).

| file | what happened |
|---|---|
| `incident_2026-10-04.yaml` | Edited in GitHub's web editor: a stray indented `feeds:` line inside the first feed, a second `feeds: [2]` block at the end (plain YAML keeps the *last* `feeds`, so every scan stopped with a config error), and a YouTube `channel_id` with `UC` pasted twice (26 characters). It must load all three feeds with one problem for each mistake. |
