# Stubs for the part of staticmap the /staticmap route uses.
from PIL.Image import Image

class CircleMarker:
    def __init__(self, coord: tuple[float, float], color: str, width: int) -> None: ...

class StaticMap:
    def __init__(
        self,
        width: int,
        height: int,
        padding_x: int = 0,
        padding_y: int = 0,
        url_template: str = ...,
        tile_size: int = 256,
        tile_request_timeout: float | None = None,
        headers: dict[str, str] | None = None,
        reverse_y: bool = False,
        background_color: str = "#fff",
        delay_between_retries: int = 0,
    ) -> None: ...
    def add_marker(self, marker: CircleMarker) -> None: ...
    def render(
        self, zoom: int | None = None, center: tuple[float, float] | None = None
    ) -> Image: ...
