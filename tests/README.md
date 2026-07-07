# Tests

Metric tests use Python stdlib `unittest`.

```powershell
python -m unittest discover -s tests
```

The UI can be smoke-tested without opening a window:

```powershell
python app\main.py --smoke
```

All asserted token, cache hit, cost, budget, and context usage values are 本地估算 / local estimate.
