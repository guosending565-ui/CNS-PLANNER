"""Online tile and geocoder diagnostics kept outside HTTP routing."""

import json
import math
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


def check_online_services(data):
    sources = list(getattr(data, "online_sources", {}).values())
    if not sources:
        return {"ok": False, "message": "当前 QGIS 项目中没有识别到在线瓦片服务", "results": []}
    results, tianditu_key = [], ""
    for source in sources:
        name, template = source.get("name", "在线服务"), source.get("template", "")
        try:
            z = max(int(source.get("zmin", 0)), min(6, int(source.get("zmax", 18))))
            n, lon, lat = 2 ** z, 120.0, 30.0
            x = int((lon + 180.0) / 360.0 * n)
            y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
            request = Request(template.format(z=z, x=x, y=y), headers={"User-Agent": "CNS-Planner/1.0", "Cache-Control": "no-cache"})
            with urlopen(request, timeout=8) as response:
                mime, sample = (response.headers.get("Content-Type") or "").lower(), response.read(128)
                ok = getattr(response, "status", 200) == 200 and "image" in mime and bool(sample)
            results.append({"name": name, "ok": ok, "message": "服务正常" if ok else "返回内容不是有效地图图片"})
            parsed = urlparse(template)
            if (parsed.hostname or "").endswith(".tianditu.gov.cn") and not tianditu_key:
                tianditu_key = parse_qs(parsed.query).get("tk", [""])[0]
        except Exception as exc:
            results.append({"name": name, "ok": False, "message": str(exc)})
    if tianditu_key:
        try:
            post = {"keyWord": "北京", "level": 12, "mapBound": "73,18,135,54", "queryType": 7, "start": 0, "count": 1}
            url = "https://api.tianditu.gov.cn/v2/search?" + urlencode({"postStr": json.dumps(post, ensure_ascii=False), "type": "query", "tk": tianditu_key})
            with urlopen(Request(url, headers={"User-Agent": "CNS-Planner/1.0", "Cache-Control": "no-cache"}), timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
            ok = isinstance(payload, dict) and not payload.get("msg")
            results.append({"name": "天地图地名搜索", "ok": ok, "message": "服务正常" if ok else str(payload.get("msg") or "返回异常")})
        except Exception as exc:
            results.append({"name": "天地图地名搜索", "ok": False, "message": str(exc)})
    return {"ok": bool(results) and all(item["ok"] for item in results), "results": results}
