"""Fakes for external tools used by the PR-metrics tests: a `gh` executable and a Bitbucket REST server.

Both are real processes/sockets that the production code talks to unmodified (PATH lookup for `gh`,
BITBUCKET_API_BASE for Bitbucket), so tests exercise the same code paths as production.
"""
