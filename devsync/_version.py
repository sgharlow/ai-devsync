"""The one place the version lives. Import it from HERE, never from the package:
`devsync` can resolve to a namespace package whose __init__ never runs (see
test_runs_when_the_top_level_name_is_only_a_namespace_package)."""
__version__ = "0.2.0"
