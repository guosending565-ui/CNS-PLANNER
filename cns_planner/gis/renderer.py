"""QGIS viewport rendering isolated from HTTP and application state."""

from collections import OrderedDict
import math
import time

from qgis.PyQt.QtCore import QBuffer, QIODevice, QSize
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsApplication, QgsMapRendererParallelJob, QgsMapSettings, QgsRectangle


class QgisMapRenderer:
    def __init__(self, destination_crs, stale_checker=None, cache_size=32):
        self.destination_crs = destination_crs
        self.stale_checker = stale_checker or (lambda query: False)
        self.cache_size = cache_size
        self.cache = OrderedDict()

    def clear(self):
        self.cache.clear()

    def render(self, sources, revision, query):
        if self.stale_checker(query):
            raise ValueError("过期视图已取消")
        if sources is None:
            raise ValueError("未加载地图")
        bbox = [float(value) for value in query["bbox"][0].split(",")]
        if len(bbox) != 4 or not all(math.isfinite(value) and abs(value) <= 1e9 for value in bbox) or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            raise ValueError("无效地图范围")
        width, height = int(query["w"][0]), int(query["h"][0])
        if not (1 <= width <= 2560 and 1 <= height <= 1800):
            raise ValueError("图像尺寸超限")
        opacity = float(query.get("opacity", ["0.65"])[0])
        terrain_opacity = float(query.get("terrainOpacity", ["0.55"])[0])
        if not math.isfinite(opacity) or not 0 <= opacity <= 1:
            raise ValueError("人口透明度无效")
        if not math.isfinite(terrain_opacity) or not 0 <= terrain_opacity <= 1:
            raise ValueError("地形透明度无效")
        key = (revision, tuple(bbox), width, height, query.get("pop", ["0"])[0],
               query.get("air", ["1"])[0], query.get("terrain", ["0"])[0], opacity, terrain_opacity)
        if key in self.cache:
            return self.cache[key]
        layers = self._layers(sources, query, bbox, opacity, terrain_opacity)
        settings = QgsMapSettings()
        settings.setDestinationCrs(self.destination_crs)
        settings.setTransformContext(sources.project.transformContext())
        settings.setExtent(QgsRectangle(*bbox))
        settings.setOutputSize(QSize(width, height))
        settings.setBackgroundColor(QColor(0, 0, 0, 0))
        settings.setLayers(layers)
        job = QgsMapRendererParallelJob(settings); job.start()
        deadline = time.monotonic() + 60
        while job.isActive() and time.monotonic() < deadline:
            if self.stale_checker(query):
                job.cancel(); raise ValueError("过期视图已取消")
            QgsApplication.processEvents(); time.sleep(0.005)
        if job.isActive():
            job.cancel(); raise ValueError("地图渲染超时，可关闭在线底图后重试")
        if job.errors():
            raise ValueError("部分图层渲染失败，请检查数据或关闭在线底图")
        buffer = QBuffer(); buffer.open(QIODevice.WriteOnly)
        job.renderedImage().save(buffer, "PNG")
        result = bytes(buffer.data())
        self.cache[key] = result
        while len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)
        return result

    @staticmethod
    def _layers(sources, query, bbox, opacity, terrain_opacity):
        layers = []
        if query.get("pop", ["0"])[0] == "1":
            sources.population.renderer().setOpacity(opacity); layers.append(sources.population)
        if query.get("air", ["1"])[0] == "1":
            viewport = QgsRectangle(*bbox)
            for layer in sources.local_layers:
                layer_box = sources.layer_boxes.get(layer.id())
                if layer.isValid() and (layer_box is None or layer_box.intersects(viewport)):
                    layers.append(layer)
        if query.get("terrain", ["0"])[0] == "1":
            sources.terrain.renderer().setOpacity(terrain_opacity); layers.append(sources.terrain)
        return layers
