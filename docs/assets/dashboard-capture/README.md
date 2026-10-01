# Dashboard demo capture

The exact output behind
[`../inferrail-dashboard-demo.gif`](../inferrail-dashboard-demo.gif),
recorded by
[`scripts/render_dashboard_gif.py --capture`](../../../scripts/render_dashboard_gif.py).

What ran:

1. `pip install` of the `inferrail` package, plus `openai`, into a fresh
   virtual environment. `capture.json` says which build: its `label` is
   the package version, followed by the git commit when the capture was
   built from a checkout rather than a PyPI release.
2. `inferrail serve --app-mode --config inferrail.yaml` from that
   environment, with its data directory in a temporary folder.
3. `python agent_run.py`: six model calls for one job,
   `work_id=contract-review-42`, with a declared $0.04 budget for it.
4. Playwright screenshots of the real local dashboard while those
   receipts arrived: the Live Feed, the job's Work page, and Budgets.

What is not real: the model upstream. `inferrail.yaml` points the
gateway at a local stand-in that speaks the OpenAI chat-completions
format, replies with a placeholder, and reports token usage computed
from the request size. No API key was set and no provider was called or
billed. Costs are priced with Inferrail's built-in `gpt-4o` list price
through `price_as: openai`. The contract text is synthetic.

| File | What it is |
|---|---|
| `capture.json` | Package build, commands run, and the screenshot sequence |
| `inferrail.yaml` | Gateway config (stand-in port masked) |
| `serve.txt` | Gateway stdout (local API token and temporary paths masked) |
| `agent_run.py`, `show.txt`, `agent_run.txt` | The agent script, `grep Inferrail agent_run.py`, and the script's output |
| `work.txt`, `report-by-work.txt` | `inferrail work contract-review-42` and `inferrail report --by work_id` on the same receipts, afterwards |
| `screens/` | Dashboard screenshots, in order, cropped to the part of each screen the GIF shows |

The GIF scales those crops to fill the frame and adds a caption strip
above each one, outside the product UI. Terminal text comes from these
files.

To reproduce: `python -m pip install pillow playwright`,
`python -m playwright install chromium`, then
`python scripts/render_dashboard_gif.py --capture` from the repository
root (port 8000 must be free; Node is needed to bundle the dashboard
from a checkout). Add `--package inferrail` to capture the latest PyPI
release instead. Timestamps will differ on each run.
