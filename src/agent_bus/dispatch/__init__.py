"""Agent dispatch: a timer-driven run that reads tasks from a tracker,
classifies them by config rules and spawns agent sessions for them.

Importing this package must stay cheap: `config.load()` imports
`dispatch.settings` on every CLI call, so nothing here may import the
network, storage or spawn modules.
"""
