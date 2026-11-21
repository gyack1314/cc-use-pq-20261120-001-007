from decimal import Decimal

from .contracts import (clock, contains, distinct_strings, emit_count, identifier, integer,
                        minutes, overlaps, period, ratio, require, shape, text)
from .residents import Residents
from .venue import Venue


def normalize_action(record):
    shape(record, ("id", "revision", "name", "zone", "metric", "start", "end", "status",
                   "capacity", "groups", "priority", "facility"), ("claims",))
    start, end = period(record["start"], record["end"])
    require(record["status"] in ("planned", "active", "suspended", "closed"), "status", "行动状态无效")
    result = {"id": identifier(record["id"]), "revision": integer(record["revision"], "revision", 1),
              "name": text(record["name"], "name"), "zone": identifier(record["zone"], "zone"),
              "metric": identifier(record["metric"], "metric"), "start": start, "end": end,
              "status": record["status"], "capacity": integer(record["capacity"], "capacity"),
              "groups": distinct_strings(record["groups"], "groups"),
              "priority": integer(record["priority"], "priority", 0, 100),
              "facility": identifier(record["facility"], "facility")}
    claims = record.get("claims")
    if claims is not None:
        require(isinstance(claims, list), "field", "claims需要数组")
        normalized = []
        for claim in claims:
            shape(claim, ("window", "start", "end", "share"))
            normalized.append({"window": identifier(claim["window"], "claims.window"),
                               "start": clock(claim["start"], "claims.start"),
                               "end": clock(claim["end"], "claims.end"),
                               "share": integer(claim["share"], "claims.share", 1)})
            require(minutes(normalized[-1]["start"]) < minutes(normalized[-1]["end"]),
                    "period", "份额起止无效")
        keys = {(claim["window"], claim["start"], claim["end"]) for claim in normalized}
        require(len(keys) == len(normalized), "field", "份额不能重复")
        result["claims"] = sorted(normalized, key=lambda claim: (claim["window"], claim["start"], claim["end"]))
    return result


def facility_clash(first, second):
    """保守判断两行动是否不能同时生效：任一按日承诺即整日互斥，否则看份额时段重叠。"""
    if first["facility"] != second["facility"]:
        return False
    if not first.get("claims") or not second.get("claims"):
        return overlaps(first, second)
    windows = {}
    for claim in first.get("claims") or []:
        windows.setdefault(claim["window"], []).append(claim)
    for other in second.get("claims") or []:
        for claim in windows.get(other["window"], []):
            if (minutes(claim["start"]) < minutes(other["end"])
                    and minutes(other["start"]) < minutes(claim["end"])):
                return True
    return False


def legacy_clash(first, second):
    """入账硬约束：按日行动独占设施当日；双份额行动由时段容量校验裁决。"""
    if not first.get("claims") and not second.get("claims"):
        return facility_clash(first, second)
    if first.get("claims") and second.get("claims"):
        return False
    return first["facility"] == second["facility"] and overlaps(first, second)


class Adaptation:
    def __init__(self, snapshot):
        self.rows = snapshot["action"]
        self.snapshot = snapshot
        self.residents = Residents(snapshot)
        self.venue = Venue(snapshot)

    def validate(self):
        active = []
        for row in self.rows.values():
            require(row["zone"] in self.snapshot["zone"], "reference", "行动行政区不存在")
            require(row["metric"] in self.snapshot["metric"], "reference", "行动指标不存在")
            known = self.residents.registered_groups(row["zone"])
            require(set(row["groups"]).issubset(known), "reference", "行动人群尚未登记")
            if row["status"] != "active":
                continue
            require(not any(legacy_clash(row, other) for other in active),
                    "facility_conflict", "同一设施不能同时承诺给多项行动", facility=row["facility"])
            active.append(row)

    def active(self, zone, metric, when):
        selected = [row for row in self.rows.values() if row["zone"] == zone and row["metric"] == metric
                    and row["status"] == "active" and contains(row, when)]
        return sorted(selected, key=lambda row: (-row["priority"], row["id"]))

    def is_treated(self, zone, metric, when):
        return any(self.venue.capacity_people(row, when) > 0 for row in self.active(zone, metric, when))

    def promise_map(self, action, when):
        allocation = self.allocate(action["zone"], action["metric"], when, hazardous=True)
        for item in allocation["actions"]:
            if item["action"] == action["id"]:
                return {entry["group"]: entry["count"] for entry in item["assignments"]}
        return {}

    def allocate(self, zone, metric, when, *, hazardous):
        population = self.residents.day(zone, when)
        groups = {row["group"]: row for row in population["groups"]}
        protected = {group: Decimal(0) for group in groups}
        if not hazardous or not population["complete"]:
            return {"complete": population["complete"], "protected": protected, "actions": [],
                    "unused": 0, "interrupted": False, "unfulfilled_people": Decimal(0),
                    "unfulfilled_person_hours": Decimal(0), "delivered_person_hours": Decimal(0)}
        promised = {group: 0 for group in groups}
        evidence = []
        unused = 0
        interrupted = False
        unfulfilled_people = Decimal(0)
        unfulfilled_hours = Decimal(0)
        delivered_hours = Decimal(0)
        for action in self.active(zone, metric, when):
            capacity = self.venue.capacity_people(action, when)
            hours = self.venue.promised_hours(action, when)
            remaining = capacity
            assignments = []
            used = Decimal(0)
            action_promised_hours = Decimal(0)
            action_delivered_hours = Decimal(0)
            action_unfulfilled_hours = Decimal(0)
            mode = "estimate"
            eligible = sorted((groups[group] for group in action["groups"] if group in groups),
                              key=lambda row: (-Decimal(row["vulnerability"]), row["group"]))
            for group in eligible:
                count = min(remaining, group["count"] - promised[group["group"]])
                if not count:
                    continue
                promised[group["group"]] += count
                remaining -= count
                delivery = self.venue.deliver(action, when, group["group"], count, hours)
                protected[group["group"]] += delivery["protected"]
                used += delivery["protected"]
                delivered_hours += delivery["delivered_person_hours"]
                unfulfilled_people += delivery["unfulfilled_people"]
                unfulfilled_hours += delivery["unfulfilled_person_hours"]
                interrupted = interrupted or delivery["interrupted"]
                if delivery["mode"] == "facts":
                    mode = "facts"
                action_promised_hours += delivery["promised_person_hours"]
                action_delivered_hours += delivery["delivered_person_hours"]
                action_unfulfilled_hours += delivery["unfulfilled_person_hours"]
                assignments.append({"group": group["group"], "count": count,
                                    "protected": delivery["protected"],
                                    "delivered_people": delivery["delivered_people"],
                                    "delivered_person_hours": delivery["delivered_person_hours"],
                                    "mode": delivery["mode"]})
            unused += remaining
            evidence.append({"action": action["id"], "revision": action["revision"],
                             "facility": action["facility"], "capacity": capacity,
                             "used": used, "unused": remaining, "mode": mode,
                             "promised_person_hours": action_promised_hours,
                             "delivered_person_hours": action_delivered_hours,
                             "unfulfilled_person_hours": action_unfulfilled_hours,
                             "assignments": assignments})
        return {"complete": True, "protected": protected, "actions": evidence, "unused": unused,
                "interrupted": interrupted, "unfulfilled_people": unfulfilled_people,
                "unfulfilled_person_hours": unfulfilled_hours, "delivered_person_hours": delivered_hours}

    def utilization(self, zone, metric, start, end, readings, coverage):
        from .contracts import days
        actions = {}
        for when in days(start, end):
            weather = readings.zone_day(zone, metric, when, coverage)
            if not weather["usable"]:
                continue
            allocation = self.allocate(zone, metric, when, hazardous=weather["hazardous"])
            for item in allocation["actions"]:
                totals = actions.setdefault(item["action"], {"capacity_days": 0, "served_days": 0, "hazard_days": 0})
                totals["capacity_days"] += item["capacity"]
                totals["served_days"] += item["used"]
                totals["hazard_days"] += 1
        return [{"action": key, "capacity_days": value["capacity_days"],
                 "served_days": emit_count(value["served_days"]), "hazard_days": value["hazard_days"],
                 "utilization": ratio(value["served_days"], value["capacity_days"])}
                for key, value in sorted(actions.items())]
