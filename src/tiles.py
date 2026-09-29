from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


class TileSource:
    def __init__(self, tile_dir: Path | None, base_url: str, map_id: int, timeout: float):
        self.tile_dir = tile_dir
        self.base_url = base_url.rstrip("/")
        self.map_id = map_id
        self.timeout = timeout

    def get(self, x: int, y: int) -> bytes | None:
        if self.tile_dir:
            for suffix in ("jpg", "jpeg", "png"):
                path = self.tile_dir / "18" / str(x) / f"{y}.{suffix}"
                if path.is_file():
                    return path.read_bytes()
            return None
        url = f"{self.base_url}/getomap_{self.map_id}_18_{x}_{y}_0_0.jpg"
        try:
            with urlopen(url, timeout=self.timeout) as response:
                data = response.read()
                content_type = response.headers.get("Content-Type", "")
        except HTTPError as exc:
            if exc.code in (404, 204):
                return None
            raise RuntimeError(f"瓦片请求失败 {url}: HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"无法连接奥维瓦片服务 {self.base_url}: {exc.reason}") from exc
        if not data or ("image" not in content_type.lower() and not data.startswith((b"\xff\xd8", b"\x89PNG"))):
            return None
        return data
