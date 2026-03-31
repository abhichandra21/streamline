from .base import WatchEvent


def parse(filepath: str) -> list[WatchEvent]:
    """
    Parse Disney+ watch history CSV.
    Not yet implemented — waiting for export data.

    Expected columns (update when data arrives):
    Title, WatchDate, Duration, ...
    """
    raise NotImplementedError(
        "Disney+ parser not implemented. "
        "Set config.PLATFORM_PATHS['disney'] = None until data arrives."
    )
