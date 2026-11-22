# Inferrail Package Check for CI

Check your locked dependencies for known vulnerabilities on every push or pull request, and fail
the build when one has an advisory. $0.002 per 20 packages, paid with
[x402](https://x402.org) from a wallet you set, with a spending cap enforced before anything is
paid. Data: [OSV.dev](https://osv.dev) (advisories licensed per source).

```yaml
# .github/workflows/dependency-check.yml
name: Dependency vulnerability check
on: [pull_request, push]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: domondi1/inferrail/actions/package-check@<commit-sha>   # pin a reviewed commit
        with:
          wallet-key: ${{ secrets.INFERRAIL_WALLET_KEY }}
          max-usd: "0.02"   # required: the step never pays more than this per run
```

## Security

The step holds a wallet key, so it is built to keep that key and its spending contained:

- **Spending limit you set.** `max-usd` is required. The cost is worked out before any payment
  and the step fails, paying nothing, if it is above the limit. The x402 client also refuses any
  single offer above $0.002 before signing, and pays **only Package Check's address on Base**
  (`0xe9c1249648257C1C1D70F8b7682851C377bb8f53`): an offer to any other address is never signed.
  Your wallet's balance is the hard ceiling, so keep it small.
- **Forks.** Pull requests from forks get no secrets; the step then warns and passes without
  checking or paying (`on-missing-key: fail` to fail instead). It **refuses to run under
  `pull_request_target`**, which would give an untrusted pull request your secrets.
- **Untrusted lockfiles.** Lockfiles are only parsed (JSON or TOML), never executed. Package
  names and versions with unexpected characters are dropped, everything printed is escaped for
  GitHub's workflow-command syntax, and files over 25 MB are skipped.
- **Locked code.** The x402 client and its dependencies are installed from
  `requirements.txt`, every one pinned by version and SHA-256 hash (`--require-hashes`), into
  an isolated virtual environment, with nothing newer than two weeks at lock time. The key is
  not in the environment while installing. Python runs isolated (`-I`), so nothing in your
  repository can shadow its modules. `actions/setup-python` is pinned to a commit.
- **Logs.** The key reaches the script only through the environment, GitHub masks it in logs,
  and the script never prints it (an invalid key gets a generic error).
- **Pin this action** to a commit SHA you have reviewed, as with any action that handles a
  secret, rather than `@main`.

## Set up the wallet

Use a **dedicated, low-balance** Base wallet for this, not a wallet that holds anything else.

1. Create a Base wallet and save its private key as the repository secret
   `INFERRAIL_WALLET_KEY`.
2. Send it a little USDC on Base (for example $1, which covers 500 checks of 20 packages). It
   needs no ETH: the payment facilitator pays the gas.
3. Run it on `push` and `pull_request` (never `pull_request_target`).

## What it does

- Finds lockfiles: npm `package-lock.json` (v1 to v3), Python `requirements*.txt` (exact `==`
  pins only), `poetry.lock`, `uv.lock`, `Pipfile.lock`. Folders such as `node_modules` and
  `.venv` are skipped.
- Works out the cost (one call per 20 packages) and **stops before paying** if it is above
  `max-usd`, telling you the cap it would need.
- Pays each call only when the check is delivered; an OSV.dev outage is never charged. The x402
  client refuses any single offer above $0.002 before signing.
- Re-running the same workflow run within 10 minutes returns the paid results without paying
  again (the request ids come from the run).
- Writes a table to the job summary, an error annotation per vulnerable package, and the outputs
  `packages`, `vulnerable` and `paid-usd`. The step fails on a known vulnerability unless
  `fail-on: never`.

"No known vulnerabilities" means OSV.dev had no matching advisory when checked; it is not a
guarantee that a package is secure.

## Inputs

| Input | Default | |
|---|---|---|
| `wallet-key` | (required) | Private key of the paying wallet, from a secret |
| `max-usd` | (required) | Refuse to run, paying nothing, if checking everything costs more |
| `paths` | `.` | Lockfiles or directories to scan, space-separated |
| `fail-on` | `vulnerable` | `vulnerable` or `never` |
| `on-missing-key` | `skip` | No key (a fork's pull request): `skip` (warn, pass) or `fail` |

To see what would be checked and what it would cost, without paying:
`python actions/package-check/check_deps.py --dry-run --max-usd 1 .`
