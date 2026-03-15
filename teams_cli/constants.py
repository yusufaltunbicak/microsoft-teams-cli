import os
from pathlib import Path

# Teams client ID (web SPA)
TEAMS_CLIENT_ID = "5e3ce6c0-2b1f-4285-8d4b-75ee78787346"

# Teams web URL
TEAMS_URL = "https://teams.cloud.microsoft"

# API base templates (region substituted at runtime)
CHATSVC_BASE = "https://teams.cloud.microsoft/api/chatsvc/{region}/v1"
MT_BASE = "https://teams.cloud.microsoft/api/mt/{region}/beta"
CSA_BASE = "https://teams.cloud.microsoft/api/csa/{region}/api/v3"
UPS_BASE = "https://teams.cloud.microsoft/ups/{region}/v1"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SUBSTRATE_SEARCH_BASE = "https://substrate.office.com/searchservice/api/v2/query"

# User-Agent
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Default request headers for IC3 Chat Service
IC3_HEADERS = {
    "behavioroverride": "redirectAs404",
    "x-ms-migration": "True",
}

# Cache paths
CACHE_DIR = Path(os.environ.get("TEAMS_CLI_CACHE", Path.home() / ".cache" / "teams-cli"))
TOKENS_FILE = CACHE_DIR / "tokens.json"
BROWSER_STATE_FILE = CACHE_DIR / "browser-state.json"
ID_MAP_FILE = CACHE_DIR / "id_map.json"
SCHEDULED_FILE = CACHE_DIR / "scheduled.json"
USER_PROFILE_FILE = CACHE_DIR / "user_profile.json"

# Config paths
CONFIG_DIR = Path(os.environ.get("TEAMS_CLI_CONFIG", Path.home() / ".config" / "teams-cli"))
CONFIG_FILE = CONFIG_DIR / "config.yaml"

# Request timeout (seconds). Environment variable overrides config file value.
TEAMS_TIMEOUT = int(os.environ.get("TEAMS_TIMEOUT", "0")) or None
