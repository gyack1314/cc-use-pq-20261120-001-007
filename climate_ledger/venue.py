from decimal import Decimal

from .contracts import (clock, contains, day as valid_day, days, decimal_text, distinct_strings,
                        emit_count, identifier, integer, minutes, moment, period, require, shape)

# 旧式按日行动没有小时记录时，按标准服务日解释其整日能力。
LEGACY_DAY_START = "08:00"
LEGACY_DAY_END = "20:00"
SERVICE_STATUSES = ("reserved", "arrived", "completed", "no_show", "repudiated")
ARRANGEMENT_STATUSES = ("proposed", "confirmed", "withdrawn")
SERVED_STATUSES = ("completed", "arrived")


def normalize_window(record):
    shape(record, ("id", "revision", "facility", "day", "start", "end", "capacity"))
    result = {"id": identifier(record["id"]), "revision": integer(record["revision"], "revision", 1),
              "facility": identifier(record["facility"], "facility"), "day": valid_day(record["day"], "day"),
              "start": clock(record["start"], "start"), "end": clock(record["end"], "end"),
              "capacity": integer(record["capacity"], "capacity", 1)}
    require(minutes(result["start"]) < minutes(result["end"]), "period", "服务时段起止无效")
    return result


def normalize_closure(record):
    shape(record, ("id", "revision", "facility", "day", "from", "to", "occurred", "confirmed"))
    result = {"id": identifier(record["id"]), "revision": integer(record["revision"], "revision", 1),
              "facility": identifier(record["facility"], "facility"), "day": valid_day(record["day"], "day"),
              "from": clock(record["from"], "from"),
              "to": None if record["to"] is None else clock(record["to"], "to"),
              "occurred": moment(record["occurred"], "occurred"),
              "confirmed": moment(record["confirmed"], "confirmed")}
    require(minutes(result["from"]) < minutes("24:00"), "period", "停用界限无效")
    require(result["to"] is None or minutes(result["from"]) < minutes(result["to"]),
            "period", "停用起止无效")
    require(result["occurred"] == result["day"] + "T" + result["from"],
            "closure", "停用界限必须等于实际发生时刻")
    require(result["confirmed"] >= result["occurred"], "closure", "确认时间不能早于发生时间")
    return result


def normalize_service(record):
    shape(record, ("id", "revision", "action", "facility", "day", "group", "count",
                   "start", "end", "status"), ("arrangement",))
    require(record["status"] in SERVICE_STATUSES, "status", "服务记录状态无效")
    result = {"id": identifier(record["id"]), "revision": integer(record["revision"], "revision", 1),
              "action": identifier(record["action"], "action"),
              "facility": identifier(record["facility"], "facility"),
              "day": valid_day(record["day"], "day"), "group": identifier(record["group"], "group"),
              "count": integer(record["count"], "count", 1),
              "start": clock(record["start"], "start"), "end": clock(record["end"], "end"),
              "status": record["status"],
              "arrangement": None if record.get("arrangement") is None
              else identifier(record["arrangement"], "arrangement")}
    require(minutes(result["start"]) < minutes(result["end"]), "period", "服务起止无效")
    return result


def normalize_arrangement(record):
    shape(record, ("id", "revision", "action", "day", "group", "count", "window",
                   "start", "end", "status"))
    require(record["status"] in ARRANGEMENT_STATUSES, "status", "替代安排状态无效")
    result = {"id": identifier(record["id"]), "revision": integer(record["revision"], "revision", 1),
              "action": identifier(record["action"], "action"), "day": valid_day(record["day"], "day"),
              "group": identifier(record["group"], "group"),
              "count": integer(record["count"], "count", 1),
              "window": identifier(record["window"], "window"),
              "start": clock(record["start"], "start"), "end": clock(record["end"], "end"),
              "status": record["status"]}
    require(minutes(result["start"]) < minutes(result["end"]), "period", "安排起止无效")
    return result


def operations_spec(value):
    shape(value, ("facilities", "start", "end"))
    start, end = period(value["start"], value["end"])
    return {"facilities": distinct_strings(value["facilities"], "facilities"),
            "start": start, "end": end}


def overlap_span(first_start, first_end, second_start, second_end):
    return max(0, min(first_end, second_end) - max(first_start, second_start))


def merge_intervals(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def subtract_interval(start, end, blocks):
    parts = [(start, end)]
    for block_start, block_end in blocks:
        remaining = []
        for part_start, part_end in parts:
            if block_end <= part_start or block_start >= part_end:
                remaining.append((part_start, part_end))
                continue
            if part_start < block_start:
                remaining.append((part_start, min(part_end, block_start)))
            if block_end < part_end:
                remaining.append((max(part_start, block_end), part_end))
        parts = remaining
    return parts


def format_minutes(value):
    return f"{value // 60:02d}:{value % 60:02d}"


def span_hours(start, end):
    return Decimal(end - start) / 60


class Venue:
    """场所服务时段、服务记录、临时停用与替代安排的事实层。"""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.windows = snapshot["window"]
        self.services = snapshot["service"]
        self.closures = snapshot["closure"]
        self.arrangements = snapshot["arrangement"]
        self.actions = snapshot["action"]
        self.windows_by_day = {}
        for window in self.windows.values():
            self.windows_by_day.setdefault((window["facility"], window["day"]), []).append(window)
        self.closures_by_day = {}
        for closure in self.closures.values():
            self.closures_by_day.setdefault((closure["facility"], closure["day"]), []).append(closure)
        self.services_by_action_day = {}
        for service in self.services.values():
            self.services_by_action_day.setdefault((service["action"], service["day"]), []).append(service)

    # ---------- 入账校验（结构性硬约束） ----------

    def validate(self):
        actions = self.actions
        for (facility, day), windows in self.windows_by_day.items():
            ordered = sorted(windows, key=lambda w: (minutes(w["start"]), w["id"]))
            for left, right in zip(ordered, ordered[1:]):
                require(minutes(left["end"]) <= minutes(right["start"]), "window_overlap",
                        "同设施同日服务时段不能重叠", facility=facility, day=day)
            for action in actions.values():
                conflict = (action["facility"] == facility and not action.get("claims")
                            and action["status"] == "active" and contains(action, day))
                require(not conflict, "window_conflict",
                        "按日行动已占用该设施当日，不能再登记服务时段",
                        facility=facility, day=day, action=action["id"])
        for action in actions.values():
            seen = []
            for claim in action.get("claims") or []:
                window = self.windows.get(claim["window"])
                require(window is not None, "reference", "份额引用的服务时段不存在", window=claim["window"])
                require(window["facility"] == action["facility"], "reference", "份额时段必须属于行动设施")
                require(contains(action, window["day"]), "claim_scope", "份额时段不在行动有效期内")
                require(minutes(window["start"]) <= minutes(claim["start"])
                        and minutes(claim["end"]) <= minutes(window["end"]),
                        "claim_scope", "份额必须落在服务时段内")
                require(not any(claim["window"] == other["window"]
                                and overlap_span(minutes(claim["start"]), minutes(claim["end"]),
                                                 minutes(other["start"]), minutes(other["end"]))
                                for other in seen), "claim_overlap", "同一行动在同一时段的份额不能重叠")
                seen.append(claim)
        for window in self.windows.values():
            claims = []
            for action in actions.values():
                if action["status"] != "active":
                    continue
                for claim in action.get("claims") or []:
                    if claim["window"] == window["id"]:
                        claims.append((minutes(claim["start"]), minutes(claim["end"]), claim["share"]))
            points = sorted({point for start, end, _ in claims for point in (start, end)})
            for left, right in zip(points, points[1:]):
                used = sum(share for start, end, share in claims if start <= left and end >= right)
                require(used <= window["capacity"], "window_capacity",
                        "重叠份额合计超过服务时段容量", window=window["id"])
        for arrangement in self.arrangements.values():
            action = actions.get(arrangement["action"])
            require(action is not None, "reference", "替代安排的行动不存在")
            require(arrangement["group"] in action["groups"], "reference", "替代安排人群不在行动范围内")
            require(contains(action, arrangement["day"]), "arrangement_scope", "替代安排不在行动有效期内")
            window = self.windows.get(arrangement["window"])
            require(window is not None, "reference", "替代安排的服务时段不存在")
            require(window["day"] == arrangement["day"], "arrangement_scope", "替代安排必须安排在同一日")
            require(minutes(window["start"]) <= minutes(arrangement["start"])
                    and minutes(arrangement["end"]) <= minutes(window["end"]),
                    "arrangement_scope", "替代安排必须落在服务时段内")
        for service in self.services.values():
            action = actions.get(service["action"])
            require(action is not None, "reference", "服务记录的行动不存在")
            require(service["group"] in action["groups"], "reference", "服务记录人群不在行动范围内")
            require(contains(action, service["day"]), "service_scope", "服务记录不在行动有效期内")
            if service["arrangement"] is not None:
                arrangement = self.arrangements.get(service["arrangement"])
                require(arrangement is not None, "reference", "服务记录引用的替代安排不存在")
                require(arrangement["status"] == "confirmed", "arrangement",
                        "只能为已确认的替代安排登记服务")
                require(arrangement["action"] == service["action"]
                        and arrangement["day"] == service["day"]
                        and arrangement["group"] == service["group"],
                        "arrangement", "服务记录与替代安排不一致")
                window = self.windows[arrangement["window"]]
                require(service["facility"] == window["facility"], "arrangement", "服务设施必须与替代安排一致")
                require(minutes(arrangement["start"]) <= minutes(service["start"])
                        and minutes(service["end"]) <= minutes(arrangement["end"]),
                        "arrangement", "服务必须落在替代安排时段内")
            else:
                require(service["facility"] == action["facility"], "service_scope", "服务设施必须是行动设施")
                if action.get("claims"):
                    require(any(window["facility"] == service["facility"]
                                and minutes(claim["start"]) <= minutes(service["start"])
                                and minutes(claim["end"]) >= minutes(service["end"])
                                for claim, window in self.day_claims(action, service["day"])),
                            "service_scope", "服务必须落在行动份额内")
        for (facility, day), windows in self.windows_by_day.items():
            for window in windows:
                w_start, w_end = minutes(window["start"]), minutes(window["end"])
                serving = [row for row in self.services.values()
                           if row["facility"] == facility and row["day"] == day
                           and row["status"] in SERVED_STATUSES
                           and minutes(row["start"]) < w_end and minutes(row["end"]) > w_start]
                points = sorted({point for row in serving
                                 for point in (max(minutes(row["start"]), w_start),
                                               min(minutes(row["end"]), w_end))})
                for left, right in zip(points, points[1:]):
                    total = sum(row["count"] for row in serving
                                if minutes(row["start"]) <= left and minutes(row["end"]) >= right)
                    require(total <= window["capacity"], "window_capacity",
                            "同时在场人数超过服务时段容量", window=window["id"])
        served_native = {}
        for service in self.services.values():
            if service["status"] == "repudiated" or service["arrangement"] is not None:
                continue
            key = (service["action"], service["day"])
            served_native[key] = served_native.get(key, 0) + service["count"]
        for (action_id, day), count in served_native.items():
            action = actions[action_id]
            require(count <= self.capacity_people(action, day), "service_capacity",
                    "服务人数超过行动当日能力", action=action_id, day=day)

    # ---------- 行动日能力 ----------

    def day_claims(self, action, day):
        claims = []
        for claim in action.get("claims") or []:
            window = self.windows.get(claim["window"])
            if window is not None and window["day"] == day:
                claims.append((claim, window))
        return claims

    def capacity_people(self, action, day):
        if not action.get("claims"):
            return action["capacity"]
        claims = self.day_claims(action, day)
        if not claims:
            return 0
        return min(claim["share"] for claim, _ in claims)

    def promised_hours(self, action, day):
        if not action.get("claims"):
            return span_hours(minutes(LEGACY_DAY_START), minutes(LEGACY_DAY_END))
        total = sum(minutes(claim["end"]) - minutes(claim["start"])
                    for claim, _ in self.day_claims(action, day))
        return Decimal(total) / 60

    def closure_intervals(self, facility, day):
        intervals = []
        for closure in self.closures_by_day.get((facility, day), []):
            end = minutes(closure["to"]) if closure["to"] is not None else minutes("24:00")
            intervals.append((minutes(closure["from"]), end))
        return merge_intervals(intervals)

    def effective_segments(self, action, day):
        if not action.get("claims"):
            return [(minutes(LEGACY_DAY_START), minutes(LEGACY_DAY_END), action["capacity"], None)]
        bottleneck = self.capacity_people(action, day)
        return [(minutes(claim["start"]), minutes(claim["end"]), min(claim["share"], bottleneck), window["id"])
                for claim, window in self.day_claims(action, day)]

    def others_arranged(self, window_id, action_id, start, end):
        if window_id is None:
            return 0
        events = []
        for arrangement in self.arrangements.values():
            if arrangement["status"] != "confirmed" or arrangement["action"] == action_id:
                continue
            if arrangement["window"] != window_id:
                continue
            a_start, a_end = minutes(arrangement["start"]), minutes(arrangement["end"])
            if a_start < end and start < a_end:
                events.append((max(a_start, start), min(a_end, end), arrangement["count"]))
        if not events:
            return 0
        peak = 0
        boundaries = sorted({point for start, end, _ in events for point in (start, end)})
        for left, right in zip(boundaries, boundaries[1:]):
            peak = max(peak, sum(count for start, end, count in events if start <= left and end >= right))
        return peak

    def available_capacity(self, action, day):
        segments = self.effective_segments(action, day)
        promised = sum(share * (end - start) for start, end, share, _ in segments)
        if not promised:
            return 0, 0
        closed = self.closure_intervals(action["facility"], day)
        available = 0
        for start, end, share, window_id in segments:
            for free_start, free_end in subtract_interval(start, end, closed):
                others = self.others_arranged(window_id, action["id"], free_start, free_end)
                available += max(0, share - others) * (free_end - free_start)
        return available, promised

    # ---------- 交付事实 ----------

    def records(self, action_id, day):
        return [row for row in self.services_by_action_day.get((action_id, day), [])
                if row["status"] != "repudiated"]

    def deliver(self, action, day, group, promise, hours):
        promised_hours_total = Decimal(promise) * hours
        base = {"mode": "estimate", "protected": Decimal(0), "delivered_people": 0,
                "delivered_person_hours": Decimal(0), "promised_person_hours": promised_hours_total,
                "unfulfilled_people": Decimal(0), "unfulfilled_person_hours": Decimal(0),
                "interrupted": False, "arranged_people": 0, "arranged_person_hours": Decimal(0)}
        if promise <= 0 or hours <= 0:
            return base
        records = self.records(action["id"], day)
        if records:
            people = 0
            person_hours = Decimal(0)
            arranged_people = 0
            arranged_hours = Decimal(0)
            for record in records:
                if record["group"] != group or record["status"] not in SERVED_STATUSES:
                    continue
                span = span_hours(minutes(record["start"]), minutes(record["end"]))
                people += record["count"]
                person_hours += Decimal(record["count"]) * span
                if record["arrangement"] is not None:
                    arranged_people += record["count"]
                    arranged_hours += Decimal(record["count"]) * span
            protected = min(Decimal(promise), person_hours / hours)
            unfulfilled_people = Decimal(max(0, promise - people))
            unfulfilled_hours = promised_hours_total - min(promised_hours_total, person_hours)
            return {"mode": "facts", "protected": protected, "delivered_people": people,
                    "delivered_person_hours": person_hours, "promised_person_hours": promised_hours_total,
                    "unfulfilled_people": unfulfilled_people, "unfulfilled_person_hours": unfulfilled_hours,
                    "interrupted": unfulfilled_people > 0 or unfulfilled_hours > 0,
                    "arranged_people": arranged_people, "arranged_person_hours": arranged_hours}
        available, capacity = self.available_capacity(action, day)
        protected = Decimal(0) if not capacity else Decimal(promise * available) / Decimal(capacity)
        unfulfilled_people = Decimal(promise) - protected
        return {"mode": "estimate", "protected": protected, "delivered_people": 0,
                "delivered_person_hours": Decimal(0), "promised_person_hours": promised_hours_total,
                "unfulfilled_people": unfulfilled_people,
                "unfulfilled_person_hours": unfulfilled_people * hours,
                "interrupted": available < capacity, "arranged_people": 0,
                "arranged_person_hours": Decimal(0)}

    # ---------- 可执行性（派生视图，不阻止入账） ----------

    def arrangeable(self, arrangement, adaptation):
        action = self.actions[arrangement["action"]]
        promise = adaptation.promise_map(action, arrangement["day"]).get(arrangement["group"], 0)
        consumed = 0
        for record in self.records(action["id"], arrangement["day"]):
            if (record["group"] == arrangement["group"] and record["arrangement"] is None
                    and record["status"] in ("completed", "arrived", "no_show")):
                consumed += record["count"]
        held = 0
        for other in self.arrangements.values():
            if (other["id"] != arrangement["id"] and other["status"] == "confirmed"
                    and other["action"] == arrangement["action"] and other["day"] == arrangement["day"]
                    and other["group"] == arrangement["group"]):
                held += other["count"]
        return max(0, promise - consumed - held)

    def spare_capacity(self, window, arrangement):
        start, end = minutes(arrangement["start"]), minutes(arrangement["end"])
        claims = []
        for action in self.actions.values():
            if action["status"] != "active":
                continue
            for claim in action.get("claims") or []:
                if claim["window"] == window["id"]:
                    claims.append((minutes(claim["start"]), minutes(claim["end"]), claim["share"]))
        others = []
        for other in self.arrangements.values():
            if (other["id"] != arrangement["id"] and other["status"] == "confirmed"
                    and other["window"] == window["id"]):
                others.append((minutes(other["start"]), minutes(other["end"]), other["count"]))
        points = {start, end}
        for claim_start, claim_end, _ in claims + others:
            points.add(max(claim_start, start))
            points.add(min(claim_end, end))
        ordered = sorted(point for point in points if start <= point <= end)
        spare = window["capacity"]
        for left, right in zip(ordered, ordered[1:]):
            used = sum(share for claim_start, claim_end, share in claims + others
                       if claim_start <= left and claim_end >= right)
            spare = min(spare, window["capacity"] - used)
        return spare

    def evaluate_arrangement(self, arrangement, adaptation):
        count = arrangement["count"]
        if arrangement["status"] == "withdrawn":
            return {"executable": 0, "blocked": count, "reasons": ["withdrawn"]}
        reasons = []
        executable = count
        window = self.windows[arrangement["window"]]
        closed = self.closure_intervals(window["facility"], arrangement["day"])
        if any(overlap_span(minutes(arrangement["start"]), minutes(arrangement["end"]), start, end)
               for start, end in closed):
            executable = 0
            reasons.append("window_closed")
        spare = self.spare_capacity(window, arrangement)
        if spare < count:
            reasons.append("window_capacity")
            executable = min(executable, spare)
        arrangeable = self.arrangeable(arrangement, adaptation)
        if arrangeable < count:
            reasons.append("source_exhausted")
            executable = min(executable, arrangeable)
        executable = max(0, executable)
        return {"executable": executable, "blocked": count - executable, "reasons": reasons}

    # ---------- 运营视图 ----------

    def operations(self, spec, adaptation):
        known = ({action["facility"] for action in self.actions.values()}
                 | {window["facility"] for window in self.windows.values()}
                 | {closure["facility"] for closure in self.closures.values()})
        facilities = []
        for facility in spec["facilities"]:
            require(facility in known, "facility", "设施不存在", facility=facility)
            facility_days = []
            for day in days(spec["start"], spec["end"]):
                facility_days.append({"day": day, "windows": self.window_occupancy(facility, day),
                                      "closures": self.closure_impacts(facility, day, adaptation)})
            facilities.append({"facility": facility, "days": facility_days})
        commitments = []
        for action in sorted(self.actions.values(), key=lambda row: row["id"]):
            if action["facility"] not in spec["facilities"]:
                continue
            for day in days(spec["start"], spec["end"]):
                if action["status"] != "active" or not contains(action, day):
                    continue
                hours = self.promised_hours(action, day)
                for group, promise in sorted(adaptation.promise_map(action, day).items()):
                    if not promise:
                        continue
                    delivery = self.deliver(action, day, group, promise, hours)
                    arranged = sum(row["count"] for row in self.arrangements.values()
                                   if row["status"] == "confirmed" and row["action"] == action["id"]
                                   and row["day"] == day and row["group"] == group)
                    pending = max(0, arranged - delivery["arranged_people"])
                    gap = max(Decimal(0), delivery["unfulfilled_people"] - Decimal(pending))
                    commitments.append({
                        "action": action["id"], "zone": action["zone"], "day": day, "group": group,
                        "promised_people": promise,
                        "promised_person_hours": decimal_text(delivery["promised_person_hours"]),
                        "mode": delivery["mode"], "delivered_people": delivery["delivered_people"],
                        "delivered_person_hours": decimal_text(delivery["delivered_person_hours"]),
                        "protected_person_days": emit_count(delivery["protected"]),
                        "arranged_people": arranged, "pending_people": pending,
                        "gap_people": emit_count(gap),
                        "unfulfilled_people": emit_count(delivery["unfulfilled_people"]),
                        "unfulfilled_person_hours": decimal_text(delivery["unfulfilled_person_hours"]),
                        "interrupted": delivery["interrupted"]})
        arrangements = []
        for arrangement in sorted(self.arrangements.values(), key=lambda row: row["id"]):
            window = self.windows.get(arrangement["window"])
            action = self.actions.get(arrangement["action"])
            if window is None or action is None:
                continue
            if window["facility"] not in spec["facilities"] and action["facility"] not in spec["facilities"]:
                continue
            served = sum(row["count"] for row in self.services.values()
                         if row["arrangement"] == arrangement["id"] and row["status"] in SERVED_STATUSES)
            arrangements.append({"id": arrangement["id"], "action": arrangement["action"],
                                 "day": arrangement["day"], "group": arrangement["group"],
                                 "count": arrangement["count"], "window": arrangement["window"],
                                 "facility": window["facility"], "start": arrangement["start"],
                                 "end": arrangement["end"], "status": arrangement["status"],
                                 "served_people": served, "over_served": served > arrangement["count"],
                                 "evaluation": self.evaluate_arrangement(arrangement, adaptation)})
        return {"query": spec, "facilities": facilities, "commitments": commitments,
                "arrangements": arrangements,
                "interpretation": "设施占用与未兑现承诺的运营视图；可执行部分随资料修订重审，已确认安排不被自动改排"}

    def window_occupancy(self, facility, day):
        result = []
        closed = self.closure_intervals(facility, day)
        for window in sorted(self.windows_by_day.get((facility, day), []),
                             key=lambda row: (minutes(row["start"]), row["id"])):
            w_start, w_end = minutes(window["start"]), minutes(window["end"])
            claims = []
            for action in self.actions.values():
                if action["status"] != "active":
                    continue
                for claim in action.get("claims") or []:
                    if claim["window"] == window["id"]:
                        claims.append({"action": action["id"], "share": claim["share"],
                                       "start": claim["start"], "end": claim["end"]})
            arrangements = [{"id": row["id"], "action": row["action"], "group": row["group"],
                             "count": row["count"], "start": row["start"], "end": row["end"],
                             "status": row["status"]}
                            for row in self.arrangements.values() if row["window"] == window["id"]]
            services = [{"id": row["id"], "action": row["action"], "group": row["group"],
                         "count": row["count"], "start": row["start"], "end": row["end"],
                         "status": row["status"], "arrangement": row["arrangement"]}
                        for row in self.services.values()
                        if row["facility"] == facility and row["day"] == day
                        and minutes(row["start"]) >= w_start and minutes(row["end"]) <= w_end]
            points = {w_start, w_end}
            for item in claims + arrangements + services:
                points.add(max(minutes(item["start"]), w_start))
                points.add(min(minutes(item["end"]), w_end))
            for start, end in closed:
                points.add(max(start, w_start))
                points.add(min(end, w_end))
            ordered = sorted(point for point in points if w_start <= point <= w_end)
            occupancy = []
            for left, right in zip(ordered, ordered[1:]):
                if left >= right:
                    continue
                occupancy.append({
                    "start": format_minutes(left), "end": format_minutes(right),
                    "capacity": window["capacity"],
                    "closed": any(start <= left and end >= right for start, end in closed),
                    "claimed": sum(item["share"] for item in claims
                                   if minutes(item["start"]) <= left and minutes(item["end"]) >= right),
                    "arranged": sum(item["count"] for item in arrangements
                                    if item["status"] == "confirmed"
                                    and minutes(item["start"]) <= left and minutes(item["end"]) >= right),
                    "served": sum(item["count"] for item in services
                                  if item["status"] in SERVED_STATUSES
                                  and minutes(item["start"]) <= left and minutes(item["end"]) >= right)})
            result.append({"window": window["id"], "start": window["start"], "end": window["end"],
                           "capacity": window["capacity"],
                           "claims": sorted(claims, key=lambda item: item["action"]),
                           "arrangements": sorted(arrangements, key=lambda item: item["id"]),
                           "services": sorted(services, key=lambda item: item["id"]),
                           "occupancy": occupancy})
        return result

    def closure_impacts(self, facility, day, adaptation):
        result = []
        for closure in sorted(self.closures_by_day.get((facility, day), []), key=lambda row: row["id"]):
            end = minutes(closure["to"]) if closure["to"] is not None else minutes("24:00")
            interval = (minutes(closure["from"]), end)
            impacts = []
            for action in sorted(self.actions.values(), key=lambda row: row["id"]):
                if (action["facility"] != facility or action["status"] != "active"
                        or not contains(action, day)):
                    continue
                destroyed = sum(share * overlap_span(start, finish, *interval)
                                for start, finish, share, _ in self.effective_segments(action, day))
                if not destroyed:
                    continue
                hours = self.promised_hours(action, day)
                unfulfilled_people = Decimal(0)
                unfulfilled_hours = Decimal(0)
                for group, promise in adaptation.promise_map(action, day).items():
                    delivery = self.deliver(action, day, group, promise, hours)
                    unfulfilled_people += delivery["unfulfilled_people"]
                    unfulfilled_hours += delivery["unfulfilled_person_hours"]
                impacts.append({"action": action["id"],
                                "destroyed_person_hours": decimal_text(Decimal(destroyed) / 60),
                                "unfulfilled_people": emit_count(unfulfilled_people),
                                "unfulfilled_person_hours": decimal_text(unfulfilled_hours)})
            result.append({"closure": closure["id"], "from": closure["from"], "to": closure["to"],
                           "occurred": closure["occurred"], "confirmed": closure["confirmed"],
                           "impacts": impacts})
        return result
