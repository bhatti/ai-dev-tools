"""Exit codes shared by the QA and flaky-test tools (formicary treats any non-zero exit as a failed task)."""

EXIT_USAGE = 2    # invalid arguments, spec or configuration
EXIT_BLOCKED = 3  # nothing trustworthy could be produced: environment, browser or runner unavailable
