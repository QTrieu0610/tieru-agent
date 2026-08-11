# Tieru public demo fixture

This fixture supports the three flows in [`docs/DEMO_GUIDE.md`](../../docs/DEMO_GUIDE.md).
It contains only synthetic source, tests, configuration, and one portable user
skill. It contains no recorded terminal output, credentials, Tieru runtime
state, Capsule archive, or model artifact.

Prepare a disposable copy outside the checkout:

```bash
python scripts/prepare_demo.py --root ../tieru-public-demo --replace --seed-capsule-a
```

The helper refuses to replace an unmarked directory and refuses any target
inside the Tieru repository. Review the helper before using `--replace` or
`--cleanup`.

The tiny project deliberately has one stable local check:

```bash
python -m pytest -q
```

That command is for pre-flight verification. Tieru's live model responses can
vary; the public demos are organized around observable Fabric, Trust, Replay,
Shadow, Forge, and Capsule state transitions instead of exact prose.
