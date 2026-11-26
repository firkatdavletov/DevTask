# Python to Go Migration Rules

## Goal

Incrementally migrate DevTask from Python to Go while preserving existing behavior and backward compatibility.

## Rules

- Treat the Python implementation as the reference implementation.
- Migrate only the scope explicitly defined in the current task.
- Do not introduce new features during migration.
- Do not mechanically translate Python classes into Go structs. Use idiomatic Go.
- Preserve CLI arguments, exit codes, configuration formats and workflow semantics.
- Preserve compatibility with existing state.json, reports and worktrees.
- Preserve retry limits, cancellation behavior and concurrency restrictions.
- Prefer the Go standard library over third-party dependencies.
- Use context.Context for cancellable operations.
- Keep external process execution behind testable interfaces.
- Never execute real Codex sessions or publish real pull requests in automated tests.
- Add unit tests and compatibility tests for every migrated component.
- Do not remove the Python implementation until behavioral parity is verified.

## Definition of Done

- Go implementation is complete within the task scope.
- Existing Python tests pass.
- Go tests pass.
- Differential tests pass.
- go vet passes.
- No unintended behavior changes.
- Migration documentation is updated.