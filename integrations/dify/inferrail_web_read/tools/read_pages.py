from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

from inferrail_client import InferrailError, parse_urls, read_page, to_markdown


class ReadPagesTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        try:
            urls = parse_urls(str(tool_parameters.get("urls") or ""))
            key = self.runtime.credentials["api_key"]
            pages, failed = [], []
            for url in urls:
                page = read_page(key, url)
                (pages if page.get("ok") else failed).append(page)
        except InferrailError as ex:
            yield self.create_text_message(f"Error: {ex}")
            return
        text = "\n\n---\n\n".join(to_markdown(p) for p in pages)
        if failed:
            text += "\n\nNot read (not charged): " + ", ".join(f"{f['url']} ({f['error']})" for f in failed)
        yield self.create_text_message(text or "No page could be read; nothing was charged.")
        yield self.create_json_message({
            "pages": [{k: v for k, v in p.items() if k != "content_markdown"} for p in pages],
            "failed": failed, "charged_pages": len(pages),
        })
