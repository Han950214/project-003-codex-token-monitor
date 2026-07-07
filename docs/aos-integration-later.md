# AOS Integration Later

This project is intentionally independent from AOS during phase 1.

## Future Adapter Boundary

Potential integration should use local artifacts:

- versioned run JSON
- session summary JSON
- markdown report exports
- explicit local paths configured by the user

## Non-Goals For MVP

- no AOS imports
- no writes to the AOS repository
- no shared database
- no cloud sync
- no background daemon controlled by AOS

## Compatibility Principle

The telemetry model should remain plain JSON-compatible so AOS can later consume it through a small adapter without changing the core estimator or Dashboard.

