"""Billing package parsing and the expiring-credits dispatch preference."""

import time

from gateway.account.credentials import deduction_end_text
from gateway.account.jwt import EXPIRY_SENTINEL_DAYS


def parse_package_account(acc):
    """Normalize one upstream package row into the local credit record."""
    pkg_name = acc.get("PackageName") or "Package"
    pkg_code = acc.get("PackageCode") or ""

    def _val(*keys):
        for k in keys:
            v = acc.get(k)
            if v is not None and v != "":
                try:
                    return float(v)
                except (ValueError, TypeError):
                    pass
        return 0.0

    size = _val("CycleCapacitySizePrecise", "CycleCapacitySize", "CapacitySizePrecise", "CapacitySize")
    remain = _val("CycleCapacityRemainPrecise", "CycleCapacityRemain", "CapacityRemainPrecise", "CapacityRemain")
    used = _val("CycleCapacityUsedPrecise", "CycleCapacityUsed", "CapacityUsedPrecise", "CapacityUsed")

    if size > 0 and used <= 0 and remain <= size:
        used = max(0.0, size - remain)
    elif size > 0 and remain <= 0 and used < size:
        remain = max(0.0, size - used)

    grant_reason = ""
    for attr in (acc.get("AccountAttributes") or []):
        if isinstance(attr, dict) and attr.get("Key") == "grantReason":
            grant_reason = str(attr.get("Value") or "")
            break

    create_time = ""
    raw_create = acc.get("CreateTime")
    if raw_create:
        try:
            ts = float(raw_create)
            if ts > 1e11:
                ts /= 1000.0
            create_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        except Exception:
            create_time = str(raw_create)

    end_time_str = acc.get("CycleEndTime") or acc.get("ExpiredTime") or ""
    # DeductionEndTime wins: free and trial packs have no real expiry.
    expire_time_str = deduction_end_text(acc) or end_time_str
    days_left = None
    is_expired = False
    no_expiry = False
    if expire_time_str:
        try:
            clean_time = expire_time_str.replace("T", " ")[:19]
            end_ts = time.mktime(time.strptime(clean_time, "%Y-%m-%d %H:%M:%S"))
            diff_sec = end_ts - time.time()
            if diff_sec > EXPIRY_SENTINEL_DAYS * 86400.0:
                no_expiry = True
            else:
                days_left = round(diff_sec / 86400.0, 1)
                is_expired = diff_sec < 0
        except Exception:
            pass

    in_usage = bool(acc.get("InUsage"))
    if not in_usage and not is_expired and remain > 0 and used > 0:
        in_usage = True

    return {
        "name": pkg_name,
        "package_code": pkg_code,
        "product_name": acc.get("ProductName") or "",
        "sub_product_name": acc.get("SubProductName") or "",
        "grant_reason": grant_reason,
        "resource_id": acc.get("ResourceId") or "",
        "deal_name": acc.get("DealName") or "",
        "create_time": create_time,
        "remain": round(remain, 2),
        "used": round(used, 2),
        "size": round(size, 2),
        "unit": acc.get("CapacityUnit") or "credits",
        "in_usage": in_usage,
        "auto_renew": bool(acc.get("AutoRenewFlag") or acc.get("SupportAutoRenew")),
        "cycle_start_time": acc.get("CycleStartTime") or "",
        "cycle_end_time": end_time_str,
        "expire_time": expire_time_str,
        "no_expiry": no_expiry,
        "days_left": days_left,
        "is_expired": is_expired,
        "status": acc.get("Status", 0),
    }


class ExpiryMixin(object):
    """Expiring-credits preference; fields are owned by Account."""

    def soonest_expiring_days(self):
        """Days left on the earliest usable package; None when unknown."""
        credits = self.credits
        if not isinstance(credits, dict):
            return None
        soonest = None
        for package in (credits.get("packages") or []):
            if not isinstance(package, dict):
                continue
            if package.get("no_expiry"):
                continue
            if package.get("package_code") == "enterprise":
                continue
            if package.get("is_expired"):
                continue
            days = package.get("days_left")
            remain = package.get("remain")
            if not isinstance(days, (int, float)) or days < 0:
                continue
            if not isinstance(remain, (int, float)) or remain <= 0:
                continue
            if soonest is None or days < soonest:
                soonest = float(days)
        return soonest

    def in_expiring_window(self):
        """True when credits expire inside the configured window."""
        try:
            window = int(self.expiring_window_days or 0)
        except (TypeError, ValueError):
            window = 0
        if window <= 0:
            return False
        soonest = self.soonest_expiring_days()
        return soonest is not None and soonest <= window
