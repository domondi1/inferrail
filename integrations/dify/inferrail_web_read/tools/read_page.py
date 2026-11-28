from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from inferrail_client import InferrailError, read_page, to_markdown


class ReadPageTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        url = str(tool_parameters.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            yield self.create_text_message("Error: give a public http(s) URL.")
            return
        try:
            page = read_page(self.runtime.credentials["api_key"], url,
                             request_id=tool_parameters.get("request_id") or None)  # fmt: skip
        except InferrailError as ex:
            yield self.create_text_message(f"Error: {ex}")
            return
        if not page.get("ok"):
            yield self.create_text_message(
                f"Not read (not charged): {page['error']} {page['detail']}".strip()
            )
            yield self.create_json_message(page)
            return
        yield self.create_text_message(to_markdown(page))
        yield self.create_json_message({k: v for k, v in page.items() if k != "content_markdown"})
