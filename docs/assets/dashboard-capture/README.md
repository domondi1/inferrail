# Dashboard demo capture

The exact output behind
[`../inferrail-dashboard-demo.gif`](../inferrail-dashboard-demo.gif),
recorded by
[`scripts/render_dashboard_gif.py --capture`](../../../scripts/render_dashboard_gif.py).

What ran:

1. `pip install inferrail openai` into a fresh virtual environment
   (version in `capture.json`).
2. `inferrail serve --app-mode --config inferrail.yaml` from that
   environment, with its data directory in a temporary folder.
3. `python agent_run.py`: six model calls tagged `customer=acme` and
   `work_id=contract-review-42`, with a declared $0.04 budget for that
   work.
4. Playwright screenshots of the real local dashboard (Live Feed, Work,
   Budgets) while those receipts arrived.

What is not real: the model upstream. `inferrail.yaml` points the
gateway at a local stand-in that speaks the OpenAI chat-completions
format, replies with a placeholder, and reports token usage computed
from the request size. No API key was set and no provider was called or
billed. Costs are priced with Inferrail's built-in `gpt-4o` list price
through `price_as: openai`. The contract text is synthetic.

| File | What it is |
|---|---|
| `capture.json` | Package version, commands run, and the screenshot sequence with click positions |
| `pip.txt` | The `Successfully installed …` line from the install |
| `inferrail.yaml` | Gateway config (stand-in port masked) |
| `serve.txt` | Gateway stdout (local API token and temporary paths masked) |
| `agent_run.py`, `agent_run.txt` | The agent script and its output |
| `work.txt`, `report-by-customer.txt` | `inferrail work contract-review-42` and `inferrail report --by customer` on the same receipts, afterwards |
| `screens/` | Dashboard screenshots, in order |

The GIF adds only a caption strip and a ring where a real click
happened. Terminal text comes from these files.

To reproduce: `python -m pip install pillow playwright`,
`python -m playwright install chromium`, then
`python scripts/render_dashboard_gif.py --capture` from the repository
root (port 8000 must be free). Timestamps will differ on each run.
