"""Injectable exact-HTML PDF renderer."""

from __future__ import annotations

from typing import Protocol


class PdfRendererUnavailable(RuntimeError):
    pass


class PdfRenderer(Protocol):
    def render(self, html_path, pdf_path) -> None: ...


class PlaywrightPdfRenderer:
    def render(self, html_path, pdf_path):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise PdfRendererUnavailable(
                "pdf_renderer_unavailable：PDF渲染器不可用。请安装Playwright并执行 python -m playwright install chromium"
            ) from exc
        try:
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(html_path.resolve().as_uri(), wait_until="load")
                page.pdf(path=str(pdf_path), print_background=True, format="A4")
                browser.close()
        except Exception as exc:
            raise PdfRendererUnavailable(
                "pdf_renderer_unavailable：PDF渲染失败。请确认Chromium已安装，可执行 python -m playwright install chromium"
            ) from exc
