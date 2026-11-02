# Contributing

Bug reports, fixes and docs are welcome.

```bash
git clone https://github.com/Ethos6/agent-tillu && cd agent-tillu
pip install -e ".[dev]" && python -m playwright install chromium
pytest            # the whole suite runs in ~10 s with the stub model; no AI model needed
python -m tillu --model stub   # try the UI without a model
```

Guidelines:

- **The firewall is the product.** Any new tool must declare how to classify its calls (`classify`), and anything that leaves the computer must come back as `external`, `money` or `destructive`. Add a test in `tests/test_firewall.py` or `tests/test_runtime.py` that shows the approval happens.
- Keep it dependency-light. The core needs only FastAPI, httpx and croniter; Playwright stays optional.
- No build step for the web client. Plain HTML/CSS/JS in `tillu/web/`.
- Small, focused pull requests with a test.

Security issues: see [SECURITY.md](SECURITY.md).
