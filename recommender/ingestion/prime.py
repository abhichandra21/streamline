from .base import WatchEvent


def parse(filepath: str) -> list[WatchEvent]:
    """
    Parse Amazon Prime Video watch history CSV.
    Not yet implemented — waiting for export data.

    Expected columns (update when data arrives):
    Title, WatchedDate, WatchedDurationSeconds, ...
    """
    raise NotImplementedError(
        "Prime Video parser not implemented. "
        "Set config.PLATFORM_PATHS['prime'] = None until data arrives."
    )
