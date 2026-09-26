# Demo capture

The exact output behind [`../inferrail-demo.gif`](../inferrail-demo.gif),
recorded from a real run of the published `inferrail` package by
[`scripts/render_demo_gif.py --capture`](../../../scripts/render_demo_gif.py).
All data is synthetic: a fake provider, made-up prices labeled `DEMO`,
and no provider billing.

| File | What it is |
|---|---|
| `capture.json` | Package version, Python version, commands run, and the count of blocked network attempts (0) |
| `demo.txt` | stdout of `inferrail demo` |
| `report.txt` | stdout of `inferrail report --by customer --receipts ./inferrail-demo-receipts.jsonl` |
| `receipt-priced.txt` | The first receipt, pretty-printed |
| `receipt-unknown.txt` | The receipt for the model with no price on file (`pricing` and `estimated_cost_usd` are `null`) |
| `inferrail-demo-receipts.jsonl` | Every receipt the demo wrote |

The capture ran with no provider API keys in the environment and with IP
connections and DNS lookups blocked inside the child process. The GIF
shows excerpts of these files; nothing in it is typed by hand.

To reproduce: `python -m pip install inferrail pillow`, then
`python scripts/render_demo_gif.py --capture` from the repository root.
Receipt ids and timestamps will differ on each run.
