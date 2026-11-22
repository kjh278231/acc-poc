# acc-poc

Outbound driver-call agent PoC: **A. Haiku Only** vs **B. Jev + Haiku**, text + voice, A2A-ready for Amazon Connect.
Docs: `README.md`, `docs/architecture.md`, `docs/voice-design.md`. Talk to the user in Korean.

## Commands

```bash
uv sync
uv run pytest -q                                   # offline (LLM_MODE=mock set in tests/conftest.py), ~5 s
uv run uvicorn app.main:create_app --factory --port 8080   # / text A/B · /voice voice + workflow canvas · /results
uv run python -m app.voice.simulate --engine both  # voice latency without a browser (real AWS + OpenRouter)
uv run python -m app.benchmark.runner --reps 5     # real API benchmark, cost-capped by BENCH_MAX_COST_USD
```

## Rules

- Engines (`app/engines/`), rules (`app/core/rules.py`) and templates are text-in/text-out. Channels (REST, A2A, voice)
  only convert to `InputMessage` and render `OutputEvent`; don't put channel logic in the core.
- New live-progress info goes through `STAGE` events (`app/core/progress.py`); A2A ignores them.
- Never commit `.env`, `deploy/.secrets`, `deploy/.state.json`, `.cache/`, `results/voice/` (all gitignored).
- Benchmark numbers in docs come from real runs only; mock runs are labelled as mock.
- Run `uv run pytest -q` after code changes.

## Public demo (EC2)

`deploy/provision.py` (idempotent AWS resources, t3.micro + EIP, IAM role with Polly/Transcribe only) and
`deploy/deploy.sh` (ships the working tree, Caddy HTTPS on `<ip>.sslip.io` with basic auth, systemd `acc-poc`).
Redeploy after changes: `bash deploy/deploy.sh`. The server sets `ALLOW_TEST_RUNNER=false`; keep the test-runner
endpoint disabled on anything network-exposed. SSH: `ssh -i ~/.ssh/acc-poc.pem ubuntu@<ip>` (only from the admin IP).
