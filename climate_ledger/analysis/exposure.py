from decimal import Decimal

from ..adaptation import Adaptation
from ..contracts import (as_decimal, days, decimal_text, emit_count, query_spec, ratio, require)
from ..observatory.readings import Readings
from ..residents import Residents


def emit_action(evidence):
    return {"action": evidence["action"], "revision": evidence["revision"],
            "facility": evidence["facility"], "capacity": evidence["capacity"],
            "used": emit_count(evidence["used"]), "unused": evidence["unused"], "mode": evidence["mode"],
            "promised_person_hours": decimal_text(evidence["promised_person_hours"]),
            "delivered_person_hours": decimal_text(evidence["delivered_person_hours"]),
            "unfulfilled_person_hours": decimal_text(evidence["unfulfilled_person_hours"]),
            "assignments": [{"group": item["group"], "count": item["count"],
                             "protected": emit_count(item["protected"]),
                             "delivered_people": item["delivered_people"],
                             "delivered_person_hours": decimal_text(item["delivered_person_hours"]),
                             "mode": item["mode"]}
                            for item in evidence["assignments"]]}


def emit_daily(row):
    result = dict(row)
    for field in ("protected", "exposed", "unfulfilled_people"):
        result[field] = None if row[field] is None else emit_count(row[field])
    for field in ("weighted_burden", "unfulfilled_person_hours", "delivered_person_hours"):
        result[field] = None if row[field] is None else decimal_text(row[field])
    result["groups"] = [{**group, "protected": emit_count(group["protected"]),
                         "exposed": emit_count(group["exposed"]),
                         "weighted_burden": decimal_text(group["weighted_burden"])}
                        for group in row["groups"]]
    result["actions"] = [emit_action(item) for item in row["actions"]]
    return result


class Exposure:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.readings = Readings(snapshot)
        self.residents = Residents(snapshot)
        self.adaptation = Adaptation(snapshot)

    def compute(self, zone, metric, when, minimum_coverage):
        weather = self.readings.zone_day(zone, metric, when, minimum_coverage)
        population = self.residents.day(zone, when)
        usable = weather["usable"] and population["complete"]
        result = {"zone": zone, "day": when, "usable": usable, "weather": weather,
                  "population": population, "hazardous": weather["hazardous"],
                  "protected": None, "exposed": None, "weighted_burden": None,
                  "groups": [], "actions": [], "reason": [], "interrupted": False,
                  "unfulfilled_people": None, "unfulfilled_person_hours": None,
                  "delivered_person_hours": None}
        if not weather["usable"]:
            result["reason"].append("observation_gap")
        if not population["complete"]:
            result["reason"].append("population_gap")
        if not usable:
            return result
        allocation = self.adaptation.allocate(zone, metric, when, hazardous=weather["hazardous"])
        burden = self.residents.burden(zone, when, weather["severity"], allocation["protected"])
        exposed = burden["exposed"]
        groups = burden["groups"]
        if not weather["hazardous"]:
            exposed = Decimal(0)
            groups = [{**group, "exposed": Decimal(0)} for group in groups]
        result.update(protected=sum(allocation["protected"].values(), Decimal(0)), exposed=exposed,
                      weighted_burden=burden["weighted_burden"], groups=groups,
                      actions=allocation["actions"], interrupted=allocation["interrupted"],
                      unfulfilled_people=allocation["unfulfilled_people"],
                      unfulfilled_person_hours=allocation["unfulfilled_person_hours"],
                      delivered_person_hours=allocation["delivered_person_hours"])
        return result

    def daily(self, zone, metric, when, minimum_coverage):
        return emit_daily(self.compute(zone, metric, when, minimum_coverage))

    def analyze(self, spec):
        spec = query_spec(spec)
        self.readings.catalog.metric(spec["metric"])
        require(set(spec["zones"]).issubset(self.snapshot["zone"]), "zone", "查询含未知行政区")
        rows = [self.compute(zone, spec["metric"], when, spec["minimum_coverage"])
                for zone in spec["zones"] for when in days(spec["start"], spec["end"])]
        strata = [self.summarize(zone, [row for row in rows if row["zone"] == zone]) for zone in spec["zones"]]
        qualified = all(row["complete"] for row in strata)
        all_known = self.summarize("all", rows)
        return {"query": spec, "status": "qualified" if qualified else "incomplete", "strata": strata,
                "total": all_known, "daily": [emit_daily(row) for row in rows],
                "episodes": self.episodes(rows)}

    @staticmethod
    def summarize(zone, rows):
        valid = [row for row in rows if row["usable"]]
        hazard = [row for row in valid if row["hazardous"]]
        population_days = sum(row["population"]["population"] for row in valid)
        hazard_population = sum(row["population"]["population"] for row in hazard)
        protected = sum((row["protected"] for row in hazard), Decimal(0))
        exposed = sum((row["exposed"] for row in hazard), Decimal(0))
        burden = sum((row["weighted_burden"] for row in valid), Decimal(0))
        unfulfilled = sum((row["unfulfilled_person_hours"] for row in valid), Decimal(0))
        delivered = sum((row["delivered_person_hours"] for row in valid), Decimal(0))
        complete = len(valid) == len(rows)
        return {"zone": zone, "complete": complete, "expected_zone_days": len(rows),
                "usable_zone_days": len(valid), "unknown_zone_days": len(rows) - len(valid),
                "known_population_days": population_days, "known_hazard_population_days": hazard_population,
                "known_protected_person_days": emit_count(protected),
                "known_exposed_person_days": emit_count(exposed),
                "known_weighted_burden": decimal_text(burden),
                "known_unfulfilled_person_hours": decimal_text(unfulfilled),
                "known_delivered_person_hours": decimal_text(delivered),
                "hazard_fraction": ratio(hazard_population, population_days) if complete else None,
                "protection_fraction": ratio(protected, hazard_population) if complete else None,
                "coverage_fraction": ratio(len(valid), len(rows)),
                "groups": Exposure.group_summary(valid, complete)}

    @staticmethod
    def group_summary(valid, complete):
        groups = {}
        for row in valid:
            for group in row["groups"]:
                target = groups.setdefault(group["group"],
                                           {"population_days": 0, "hazard_population_days": 0,
                                            "protected_person_days": Decimal(0),
                                            "weighted_burden": Decimal(0)})
                target["population_days"] += group["count"]
                if row["hazardous"]:
                    target["hazard_population_days"] += group["count"]
                    target["protected_person_days"] += group["protected"]
                target["weighted_burden"] += group["weighted_burden"]
        return [{"group": key, "population_days": value["population_days"],
                 "hazard_population_days": value["hazard_population_days"],
                 "protected_person_days": emit_count(value["protected_person_days"]),
                 "weighted_burden": decimal_text(value["weighted_burden"]),
                 "protection_fraction": ratio(value["protected_person_days"],
                                              value["hazard_population_days"]) if complete else None}
                for key, value in sorted(groups.items())]

    @staticmethod
    def episodes(rows):
        result = []
        for zone in sorted({row["zone"] for row in rows}):
            current = None
            for row in sorted((r for r in rows if r["zone"] == zone), key=lambda r: r["day"]):
                if not row["usable"] or not row["hazardous"]:
                    if current:
                        current["right_boundary"] = "unknown" if not row["usable"] else "observed"
                        current["exposed_person_days"] = emit_count(current["exposed_person_days"])
                        result.append(current)
                        current = None
                    continue
                if current is None:
                    current = {"zone": zone, "start": row["day"], "end": row["day"], "days": 0,
                               "exposed_person_days": Decimal(0), "right_boundary": "window"}
                current["end"] = row["day"]
                current["days"] += 1
                current["exposed_person_days"] += row["exposed"]
            if current:
                current["exposed_person_days"] = emit_count(current["exposed_person_days"])
                result.append(current)
        return result

    def priorities(self, spec):
        analysis = self.analyze(spec)
        rows = []
        deferred = []
        for stratum in analysis["strata"]:
            zone = stratum["zone"]
            if not stratum["complete"]:
                deferred.append({"zone": zone, "reason": "资料不完整，不能将未知负担排为零风险",
                                 "unknown_zone_days": stratum["unknown_zone_days"]})
                continue
            population_days = stratum["known_population_days"]
            burden = Decimal(stratum["known_weighted_burden"])
            if not population_days:
                deferred.append({"zone": zone, "reason": "没有有效人口分母", "unknown_zone_days": 0})
                continue
            rate = burden / population_days
            uncovered = as_decimal(stratum["known_exposed_person_days"])
            hazard = stratum["known_hazard_population_days"]
            rows.append({"zone": zone, "weighted_burden": decimal_text(burden),
                         "burden_per_person_day": decimal_text(rate),
                         "uncovered_person_days": emit_count(uncovered),
                         "uncovered_fraction": ratio(uncovered, hazard),
                         "hazard_population_days": hazard, "groups": stratum["groups"]})
        rows.sort(key=lambda row: (-Decimal(row["burden_per_person_day"]),
                                   -Decimal(row["weighted_burden"]), row["zone"]))
        rank = 0
        previous = None
        for index, row in enumerate(rows, 1):
            key = (row["burden_per_person_day"], row["weighted_burden"])
            if key != previous:
                rank = index
            row["rank"] = rank
            previous = key
        return {"query": analysis["query"], "priorities": rows, "deferred": deferred,
                "interpretation": "按单位人口日的未保护加权负担比较，不能替代设施可行性或因果评估"}
