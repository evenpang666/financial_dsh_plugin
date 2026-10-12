"""Keep daily research content separate from independent backtest results."""

import re


BACKTEST_SECTION = re.compile(
    r"^##[ \t]+策略回测摘要[ \t]*(?:\r?\n|\Z).*?(?=^#{1,2}[ \t]+|\Z)",
    re.MULTILINE | re.DOTALL,
)


def without_backtest_section(markdown: str) -> str:
    """Remove the former generated summary, preserving other report sections."""
    return BACKTEST_SECTION.sub("", markdown).rstrip()
