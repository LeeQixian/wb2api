"""Web-console endpoints and constants for check-in and daily activity."""

import os

WEB_ORIGIN = "https://www.workbuddy.ai"
WEB_ORIGIN_CN = "https://www.workbuddy.cn"
WEB_CONVERSATIONS_URL = WEB_ORIGIN + "/console/as/conversations/"
PROFILE_PATH = "/console/account"
WEB_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0")
DAILY_CHAT_MODEL = "hy3"   # free x0.00; ds quota is for real traffic
DAILY_CHAT_WEB_PROMPT = "Hi"
WEB_TURN_TIMEOUT = int(os.environ.get("WB_WEB_TURN_TIMEOUT") or "120")
