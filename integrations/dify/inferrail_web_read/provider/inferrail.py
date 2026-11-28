from typing import Any

from dify_plugin import ToolProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

from inferrail_client import InferrailError, check_key


class InferrailProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        try:
            check_key(str(credentials.get("api_key") or ""))  # free: reads the credit balance
        except InferrailError as ex:
            raise ToolProviderCredentialValidationError(str(ex)) from ex
        except Exception as ex:  # network trouble: say so without echoing the key
            raise ToolProviderCredentialValidationError(f"Could not reach Inferrail: {type(ex).__name__}") from ex
