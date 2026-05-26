# Research artifacts

This directory holds durable research notes and lightweight acceptance artifacts that are not part of runtime behavior.

## H200 rollout-tail router replay

The H200/Prime-RL rollout-tail findings are captured as llm-d Router/EPP and py-scheduler acceptance guidance in [`h200-rollout-tail-routing-20260526.md`](./h200-rollout-tail-routing-20260526.md).

Run the executable fixture contract with:

```bash
make validate-rollout-tail-replay
```

That command validates [`h200-rollout-tail-routing-fixture.json`](./h200-rollout-tail-routing-fixture.json) with [`check-h200-rollout-tail-routing-fixture.py`](./check-h200-rollout-tail-routing-fixture.py). The fixture is synthetic and is not a benchmark result; it encodes the replay contract for request-shape classification, endpoint/rank identity, long-lane spreading, projected-load updates, short-lane isolation, and throughput-regression guardrails.
