from .base import WatchEvent


def parse(filepath: str) -> list[WatchEvent]:
    """
    Parse HBO Max watch history CSV.
    Not yet implemented — waiting for export data.

    Expected columns (update when data arrives):
    Title, WatchDate, Duration, ...
    """
    raise NotImplementedError(
        "HBO Max parser not implemented. "
        "Set config.PLATFORM_PATHS['hbo'] = None until data arrives."
    )
