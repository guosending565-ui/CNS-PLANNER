"""Bounded, concurrent online tile cache, independent of the QGIS thread."""
from collections import OrderedDict
from hashlib import sha256
from threading import Lock, BoundedSemaphore
from urllib.request import Request, urlopen
from urllib.parse import urlparse
import time


class TileCache:
    def __init__(self, max_bytes=64 * 1024 * 1024):
        self.items = OrderedDict()
        self.lock = Lock()
        self.slots = BoundedSemaphore(8)
        self.max_bytes, self.size = max_bytes, 0

    def get(self, template, z, x, y):
        if not 0 <= z <= 20 or not 0 <= x < 2**z or not 0 <= y < 2**z:
            raise ValueError("瓦片坐标无效")
        url = template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
        if urlparse(url).scheme not in ("https", "http"):
            raise ValueError("仅支持 HTTP(S) XYZ 底图")
        key = sha256(url.encode()).digest()
        with self.lock:
            if key in self.items:
                self.items.move_to_end(key)
                return self.items[key]
        if not self.slots.acquire(timeout=4):
            raise ValueError("在线瓦片繁忙，请稍后重试")
        try:
            with urlopen(Request(url, headers={"User-Agent": "CNS-local-map/0.2"}), timeout=4) as response:
                data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise ValueError("在线瓦片过大")
            mime = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg" if data.startswith(b"\xff\xd8") else None
            if not mime:
                raise ValueError("在线服务未返回有效图片")
            with self.lock:
                if key not in self.items:
                    self.items[key] = (data, mime)
                    self.size += len(data)
                while self.size > self.max_bytes:
                    _, (old, _) = self.items.popitem(last=False)
                    self.size -= len(old)
            return data, mime
        except Exception:
            # Do not leak provider URLs or embedded credentials through errors.
            raise ValueError("在线底图暂不可用；本地图层仍可操作") from None
        finally:
            self.slots.release()
