import copy
import json
import tempfile
import unittest
from pathlib import Path

from climate_ledger.application import Application
from climate_ledger.contracts import DomainError
from climate_ledger.formats import decode_csv, encode_csv


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def example(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


class VenueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(Path(self.temp.name) / "city.sqlite3")
        self.addCleanup(self.app.ledger.close)
        self.bundle = example("city.json")
        self.query = example("exposure.json")
        self.app.ingest(self.bundle)

    def action(self, identity, **changes):
        record = copy.deepcopy(next(item for item in self.bundle["records"]
                                    if item["kind"] == "action" and item["data"]["id"] == identity))
        record["data"].update(changes)
        return record

    def change(self, *records, name=None):
        self.counter = getattr(self, "counter", 0) + 1
        return self.app.ingest({"id": name or f"changes-{self.counter}",
                                "source": self.bundle["source"], "records": list(records)})

    def assert_error(self, code, call):
        with self.assertRaises(DomainError) as error:
            call()
        self.assertEqual(error.exception.code, code)

    def daily(self, zone, day, **extra):
        result = self.app.analyze({**self.query, **extra})
        return next(row for row in result["daily"] if row["zone"] == zone and row["day"] == day)

    @staticmethod
    def window(identity, facility, day, start, end, capacity):
        return {"kind": "window", "data": {"id": identity, "revision": 1, "facility": facility,
                                           "day": day, "start": start, "end": end, "capacity": capacity}}

    @staticmethod
    def closure(identity, facility, day, start, occurred, confirmed, to=None):
        return {"kind": "closure", "data": {"id": identity, "revision": 1, "facility": facility,
                                            "day": day, "from": start, "to": to,
                                            "occurred": occurred, "confirmed": confirmed}}

    @staticmethod
    def service(identity, action, facility, day, group, count, start, end, status, arrangement=None):
        return {"kind": "service", "data": {"id": identity, "revision": 1, "action": action,
                                            "facility": facility, "day": day, "group": group,
                                            "count": count, "start": start, "end": end,
                                            "status": status, "arrangement": arrangement}}

    @staticmethod
    def arrangement(identity, action, day, group, count, window, start, end, status):
        return {"kind": "arrangement", "data": {"id": identity, "revision": 1, "action": action,
                                                "day": day, "group": group, "count": count,
                                                "window": window, "start": start, "end": end,
                                                "status": status}}

    def windows_and_claims(self):
        return [self.window("w-lib-1", "river-library", "2026-07-03", "09:00", "12:00", 30),
                self.window("w-lib-2", "river-library", "2026-07-04", "09:00", "12:00", 30),
                self.action("cool-river", revision=2,
                            claims=[{"window": "w-lib-1", "start": "09:00", "end": "12:00", "share": 30},
                                    {"window": "w-lib-2", "start": "09:00", "end": "12:00", "share": 30}])]

    def ingest_venue(self):
        return self.app.ingest(example("venue.json"))

    def test_windowed_action_without_closure_matches_legacy_outcome(self):
        self.change(*self.windows_and_claims())
        row = self.daily("river", "2026-07-03")
        self.assertEqual(row["protected"], 30)
        self.assertEqual(row["exposed"], 70)
        self.assertFalse(row["interrupted"])
        self.assertEqual(row["unfulfilled_people"], 0)
        result = self.app.analyze(self.query)
        self.assertEqual(result["total"]["known_protected_person_days"], 60)
        self.assertEqual(result["total"]["known_weighted_burden"], "2860.000000")

    def test_closure_truncates_legacy_daily_capacity_estimate(self):
        self.change(self.closure("c-1", "river-library", "2026-07-03", "10:00",
                                 "2026-07-03T10:00", "2026-07-03T10:30"))
        row = self.daily("river", "2026-07-03")
        self.assertEqual(row["protected"], 5)
        self.assertEqual(row["exposed"], 95)
        self.assertTrue(row["interrupted"])
        self.assertEqual(row["unfulfilled_people"], 25)
        self.assertEqual(row["unfulfilled_person_hours"], "300.000000")
        self.assertEqual(self.daily("river", "2026-07-04")["protected"], 30)
        result = self.app.analyze(self.query)
        self.assertEqual(result["total"]["known_protected_person_days"], 35)
        old = self.app.analyze(self.query, seq=1)
        self.assertEqual(old["total"]["known_protected_person_days"], 60)

    def test_late_confirmed_closure_still_uses_occurrence_boundary(self):
        self.change(self.closure("c-1", "river-library", "2026-07-03", "10:00",
                                 "2026-07-03T10:00", "2026-07-04T09:00"))
        row = self.daily("river", "2026-07-03")
        self.assertEqual(row["protected"], 5)
        self.assertTrue(row["interrupted"])

    def test_closure_time_contract(self):
        bad_boundary = self.closure("c-1", "river-library", "2026-07-03", "10:00",
                                    "2026-07-03T11:00", "2026-07-03T12:00")
        self.assert_error("closure", lambda: self.change(bad_boundary))
        bad_order = self.closure("c-2", "river-library", "2026-07-03", "10:00",
                                 "2026-07-03T10:00", "2026-07-03T09:00")
        self.assert_error("closure", lambda: self.change(bad_order))
        bad_span = self.closure("c-3", "river-library", "2026-07-03", "10:00",
                                "2026-07-03T10:00", "2026-07-03T10:30", to="09:00")
        self.assert_error("period", lambda: self.change(bad_span))

    def test_completed_services_preserved_and_gap_distinguished(self):
        self.ingest_venue()
        row = self.daily("river", "2026-07-03")
        self.assertEqual(row["protected"], "8.333333")
        self.assertEqual(row["exposed"], "91.666667")
        self.assertEqual(row["weighted_burden"], "525.000000")
        self.assertTrue(row["interrupted"])
        self.assertEqual(row["unfulfilled_people"], 5)
        self.assertEqual(row["unfulfilled_person_hours"], "65.000000")
        self.assertEqual(row["delivered_person_hours"], "25.000000")
        senior = next(g for g in row["groups"] if g["group"] == "senior")
        adult = next(g for g in row["groups"] if g["group"] == "adult")
        self.assertEqual(senior["protected"], "6.666667")
        self.assertEqual(adult["protected"], "1.666667")
        evidence = next(a for a in row["actions"] if a["action"] == "cool-river")
        self.assertEqual(evidence["mode"], "facts")
        result = self.app.analyze(self.query)
        self.assertEqual(result["total"]["known_protected_person_days"], "38.333333")
        self.assertEqual(result["total"]["known_weighted_burden"], "3035.000000")
        hill = next(s for s in result["strata"] if s["zone"] == "hill")
        self.assertEqual(hill["known_protected_person_days"], 0)
        self.assertEqual(hill["known_weighted_burden"], "1680.000000")

    def test_reserved_and_no_show_records_give_no_protection(self):
        records = self.windows_and_claims() + [
            self.service("s-1", "cool-river", "river-library", "2026-07-03",
                         "senior", 20, "09:00", "12:00", "reserved"),
            self.service("s-2", "cool-river", "river-library", "2026-07-03",
                         "adult", 10, "09:00", "12:00", "no_show")]
        self.change(*records)
        row = self.daily("river", "2026-07-03")
        self.assertEqual(row["protected"], 0)
        self.assertEqual(row["unfulfilled_people"], 30)
        self.assertTrue(row["interrupted"])

    def test_arrival_without_completion_counts_only_recorded_hours(self):
        records = self.windows_and_claims() + [
            self.service("s-1", "cool-river", "river-library", "2026-07-03",
                         "senior", 20, "09:00", "09:30", "arrived")]
        self.change(*records)
        row = self.daily("river", "2026-07-03")
        senior = next(g for g in row["groups"] if g["group"] == "senior")
        self.assertEqual(senior["protected"], "3.333333")
        self.assertEqual(senior["exposed"], "16.666667")

    def test_repudiated_completion_loses_credit_but_others_stand(self):
        self.ingest_venue()
        repudiated = self.service("s-1", "cool-river", "river-library", "2026-07-03",
                                  "senior", 10, "09:00", "10:00", "repudiated")
        repudiated["data"]["revision"] = 2
        self.change(repudiated, name="repudiation")
        row = self.daily("river", "2026-07-03")
        senior = next(g for g in row["groups"] if g["group"] == "senior")
        self.assertEqual(senior["protected"], "3.333333")
        self.assertEqual(row["protected"], 5)
        history = self.app.history("service", "s-1")
        self.assertEqual([item["revision"] for item in history], [1, 2])

    def test_attendance_correction_reaudits_only_affected_commitment(self):
        self.ingest_venue()
        corrected = self.service("s-1", "cool-river", "river-library", "2026-07-03",
                                 "senior", 8, "09:00", "10:00", "completed")
        corrected["data"]["revision"] = 2
        self.change(corrected, name="attendance-correction")
        row = self.daily("river", "2026-07-03")
        senior = next(g for g in row["groups"] if g["group"] == "senior")
        adult = next(g for g in row["groups"] if g["group"] == "adult")
        self.assertEqual(senior["protected"], 6)
        self.assertEqual(adult["protected"], "1.666667")
        self.assertEqual(row["unfulfilled_people"], 7)
        operations = self.app.operations(example("operations.json"))
        first = next(a for a in operations["arrangements"] if a["id"] == "ar-1")
        self.assertEqual(first["status"], "confirmed")
        self.assertEqual(first["evaluation"]["executable"], 10)
        self.assertEqual(self.daily("river", "2026-07-04")["protected"], 30)

    def test_late_closure_import_keeps_confirmed_arrangements_and_facts(self):
        venue = example("venue.json")
        facts = [item for item in venue["records"] if item["kind"] != "closure"]
        closures = [item for item in venue["records"] if item["kind"] == "closure"]
        self.change(*facts, name="venue-facts")
        before = self.app.analyze(self.query)
        self.change(*closures, name="late-closure")
        after = self.app.analyze(self.query)
        self.assertEqual(before["total"], after["total"])
        operations = self.app.operations(example("operations.json"))
        closure = next(c for f in operations["facilities"] for d in f["days"]
                       for c in d["closures"] if c["closure"] == "c-lib-1")
        self.assertEqual(closure["impacts"][0]["destroyed_person_hours"], "60.000000")
        self.assertTrue(all(a["status"] == "confirmed" for a in operations["arrangements"]))
        hub_closure = self.closure("c-hub-1", "river-hub", "2026-07-03", "11:00",
                                   "2026-07-03T11:00", "2026-07-03T11:30")
        self.change(hub_closure, name="hub-closure")
        operations = self.app.operations(example("operations.json"))
        first = next(a for a in operations["arrangements"] if a["id"] == "ar-1")
        self.assertEqual(first["status"], "confirmed")
        self.assertEqual(first["evaluation"]["executable"], 0)
        self.assertEqual(first["evaluation"]["reasons"], ["window_closed"])
        row = self.daily("river", "2026-07-03")
        self.assertEqual(row["protected"], "8.333333")

    def test_alternative_capacity_cannot_be_counted_twice(self):
        records = self.windows_and_claims() + [
            self.window("w-hub-1", "river-hub", "2026-07-03", "10:00", "12:00", 15),
            self.action("hub-river", revision=2, status="active",
                        claims=[{"window": "w-hub-1", "start": "10:00", "end": "12:00", "share": 10}]),
            self.closure("c-1", "river-library", "2026-07-03", "10:00",
                         "2026-07-03T10:00", "2026-07-03T10:30"),
            self.arrangement("ar-1", "cool-river", "2026-07-03", "senior", 15,
                             "w-hub-1", "10:00", "12:00", "proposed")]
        self.change(*records)
        operations = self.app.operations(example("operations.json"))
        proposal = next(a for a in operations["arrangements"] if a["id"] == "ar-1")
        self.assertEqual(proposal["evaluation"]["executable"], 5)
        self.assertEqual(proposal["evaluation"]["blocked"], 10)
        self.assertEqual(proposal["evaluation"]["reasons"], ["window_capacity"])
        row = self.daily("river", "2026-07-03")
        self.assertEqual(row["protected"], 20)
        confirmed = self.arrangement("ar-1", "cool-river", "2026-07-03", "senior", 15,
                                     "w-hub-1", "10:00", "12:00", "confirmed")
        confirmed["data"]["revision"] = 2
        self.change(confirmed, name="confirm-overlap")
        row = self.daily("river", "2026-07-03")
        self.assertEqual(row["protected"], 10)
        hub = next(a for a in row["actions"] if a["action"] == "hub-river")
        self.assertEqual(hub["used"], 0)
        self.assertEqual(hub["unfulfilled_person_hours"], "20.000000")

    def test_no_show_seats_are_not_rearranged(self):
        venue = example("venue.json")
        for item in venue["records"]:
            if item["kind"] == "service" and item["data"]["id"] == "s-2":
                item["data"]["status"] = "no_show"
        self.app.ingest(venue)
        operations = self.app.operations(example("operations.json"))
        first = next(a for a in operations["arrangements"] if a["id"] == "ar-1")
        self.assertEqual(first["evaluation"]["executable"], 0)
        self.assertEqual(first["evaluation"]["reasons"], ["source_exhausted"])
        row = self.daily("river", "2026-07-03")
        senior = next(g for g in row["groups"] if g["group"] == "senior")
        self.assertEqual(senior["protected"], "6.666667")

    def test_window_registration_rules(self):
        overlap = [self.window("w-a", "river-park", "2026-07-03", "09:00", "12:00", 10),
                   self.window("w-b", "river-park", "2026-07-03", "11:00", "13:00", 10)]
        self.assert_error("window_overlap", lambda: self.change(*overlap))
        legacy_day = [self.window("w-c", "river-library", "2026-07-03", "09:00", "12:00", 10)]
        self.assert_error("window_conflict", lambda: self.change(*legacy_day))

    def test_claim_validation_rules(self):
        self.change(self.window("w-hub-1", "river-hub", "2026-07-03", "10:00", "12:00", 15),
                    name="window")
        missing = self.action("hub-river", revision=2, status="active",
                              claims=[{"window": "w-x", "start": "10:00", "end": "11:00", "share": 1}])
        self.assert_error("reference", lambda: self.change(missing))
        outside = self.action("hub-river", revision=2, status="active",
                              claims=[{"window": "w-hub-1", "start": "09:00", "end": "11:00", "share": 1}])
        self.assert_error("claim_scope", lambda: self.change(outside))
        foreign = self.action("cool-river", revision=2,
                              claims=[{"window": "w-hub-1", "start": "10:00", "end": "11:00", "share": 1}])
        self.assert_error("reference", lambda: self.change(foreign))
        both = [self.action("hub-river", revision=2, status="active",
                            claims=[{"window": "w-hub-1", "start": "10:00", "end": "12:00", "share": 10}]),
                self.action("shade-river", revision=2, status="active", facility="river-hub",
                            claims=[{"window": "w-hub-1", "start": "10:00", "end": "12:00", "share": 10}])]
        self.assert_error("window_capacity", lambda: self.change(*both, name="over-claim"))

    def test_facility_conflict_rules_with_windowed_actions(self):
        self.change(*self.windows_and_claims())
        mixed = self.action("shade-river", revision=2, status="active", facility="river-library",
                            claims=[{"window": "w-lib-1", "start": "09:00", "end": "10:00", "share": 1}])
        legacy = self.action("cool-river", revision=3, claims=None)
        del legacy["data"]["claims"]
        self.assert_error("facility_conflict", lambda: self.change(mixed, legacy, name="mixed"))
        compatible = [self.window("w-hub-1", "river-hub", "2026-07-03", "10:00", "12:00", 15),
                      self.action("hub-river", revision=2, status="active",
                                  claims=[{"window": "w-hub-1", "start": "10:00", "end": "12:00", "share": 5}]),
                      self.action("shade-river", revision=2, status="active", facility="river-hub",
                                  claims=[{"window": "w-hub-1", "start": "10:00", "end": "12:00", "share": 10}])]
        self.change(*compatible, name="compatible")
        row = self.daily("river", "2026-07-03")
        self.assertEqual(row["protected"], 45)

    def test_service_validation_rules(self):
        self.change(*self.windows_and_claims())
        unknown_group = self.service("s-x", "cool-river", "river-library", "2026-07-03",
                                     "unknown", 1, "09:00", "10:00", "completed")
        self.assert_error("reference", lambda: self.change(unknown_group))
        outside_period = self.service("s-x", "cool-river", "river-library", "2026-07-05",
                                      "senior", 1, "09:00", "10:00", "completed")
        self.assert_error("service_scope", lambda: self.change(outside_period))
        wrong_facility = self.service("s-x", "cool-river", "river-park", "2026-07-03",
                                      "senior", 1, "09:00", "10:00", "completed")
        self.assert_error("service_scope", lambda: self.change(wrong_facility))
        outside_claim = self.service("s-x", "cool-river", "river-library", "2026-07-03",
                                     "senior", 1, "08:00", "09:00", "completed")
        self.assert_error("service_scope", lambda: self.change(outside_claim))
        over_capacity = self.service("s-x", "cool-river", "river-library", "2026-07-03",
                                     "senior", 40, "09:00", "10:00", "completed")
        self.assert_error("window_capacity", lambda: self.change(over_capacity))
        proposed = [self.window("w-hub-1", "river-hub", "2026-07-03", "10:00", "12:00", 15),
                    self.arrangement("ar-1", "cool-river", "2026-07-03", "senior", 5,
                                     "w-hub-1", "10:00", "12:00", "proposed"),
                    self.service("s-y", "cool-river", "river-hub", "2026-07-03",
                                 "senior", 5, "10:00", "11:00", "completed", arrangement="ar-1")]
        self.assert_error("arrangement", lambda: self.change(*proposed, name="proposed-service"))

    def test_legacy_service_count_bounded_by_daily_capacity(self):
        over = self.service("s-x", "cool-river", "river-library", "2026-07-03",
                            "senior", 31, "09:00", "10:00", "completed")
        self.assert_error("service_capacity", lambda: self.change(over))
        within = self.service("s-x", "cool-river", "river-library", "2026-07-03",
                              "senior", 30, "09:00", "10:00", "completed")
        self.change(within)
        row = self.daily("river", "2026-07-03")
        self.assertEqual(row["protected"], "2.500000")

    def test_arrangement_validation_rules(self):
        self.change(*self.windows_and_claims())
        missing_window = self.arrangement("ar-x", "cool-river", "2026-07-03", "senior", 5,
                                          "w-x", "10:00", "12:00", "confirmed")
        self.assert_error("reference", lambda: self.change(missing_window))
        self.change(self.window("w-hub-1", "river-hub", "2026-07-03", "10:00", "12:00", 15),
                    name="hub-window")
        wrong_day = self.arrangement("ar-x", "cool-river", "2026-07-04", "senior", 5,
                                     "w-hub-1", "10:00", "12:00", "confirmed")
        self.assert_error("arrangement_scope", lambda: self.change(wrong_day))
        outside = self.arrangement("ar-x", "cool-river", "2026-07-03", "senior", 5,
                                   "w-hub-1", "09:00", "11:00", "confirmed")
        self.assert_error("arrangement_scope", lambda: self.change(outside))
        unknown_group = self.arrangement("ar-x", "cool-river", "2026-07-03", "unknown", 5,
                                         "w-hub-1", "10:00", "12:00", "confirmed")
        self.assert_error("reference", lambda: self.change(unknown_group))

    def test_operations_view_reports_occupancy_impacts_and_gaps(self):
        self.ingest_venue()
        operations = self.app.operations(example("operations.json"))
        library = next(f for f in operations["facilities"] if f["facility"] == "river-library")
        segments = library["days"][0]["windows"][0]["occupancy"]
        self.assertEqual([(s["start"], s["end"], s["served"], s["closed"]) for s in segments],
                         [("09:00", "10:00", 10, False), ("10:00", "12:00", 0, True)])
        impacts = library["days"][0]["closures"][0]["impacts"][0]
        self.assertEqual(impacts["destroyed_person_hours"], "60.000000")
        self.assertEqual(impacts["unfulfilled_people"], 5)
        hub = next(f for f in operations["facilities"] if f["facility"] == "river-hub")
        hub_segments = hub["days"][0]["windows"][0]["occupancy"]
        self.assertEqual([(s["start"], s["end"], s["arranged"], s["served"]) for s in hub_segments],
                         [("10:00", "11:00", 15, 15), ("11:00", "12:00", 15, 0)])
        senior = next(c for c in operations["commitments"] if c["group"] == "senior")
        adult = next(c for c in operations["commitments"] if c["group"] == "adult")
        self.assertEqual(senior["gap_people"], 0)
        self.assertEqual(senior["unfulfilled_person_hours"], "40.000000")
        self.assertEqual(adult["gap_people"], 5)
        self.assertEqual(adult["unfulfilled_person_hours"], "25.000000")
        self.assert_error("facility", lambda: self.app.operations(
            {"facilities": ["nowhere"], "start": "2026-07-03", "end": "2026-07-03"}))

    def test_study_excludes_interrupted_days_and_pending_controls(self):
        self.ingest_venue()
        result = self.app.compare(example("study.json"))
        self.assertEqual(result["status"], "insufficient")
        interrupted = [r for r in result["excluded"] if r["reason"] == "protection_interrupted"]
        self.assertEqual([(r["zone"], r["day"]) for r in interrupted], [("river", "2026-07-03")])
        hill_action = {"kind": "action", "data": {
            "id": "hill-action", "revision": 1, "name": "山麓避暑点", "zone": "hill", "metric": "heat",
            "start": "2026-07-03", "end": "2026-07-04", "status": "active", "capacity": 50,
            "groups": ["senior", "adult"], "priority": 5, "facility": "hill-hall"}}
        hill_closure = self.closure("c-hill", "hill-hall", "2026-07-03", "10:00",
                                    "2026-07-03T10:00", "2026-07-03T12:00")
        self.change(hill_action, hill_closure, name="hill-interruption")
        result = self.app.compare(example("study.json"))
        contaminated = [r for r in result["excluded"] if r["reason"] == "control_contaminated"]
        self.assertTrue(any(r["zone"] == "hill" and r["day"] == "2026-07-03" for r in contaminated))

    def test_release_stays_frozen_after_venue_corrections(self):
        self.ingest_venue()
        draft = self.app.draft("venue-draft", "heat", self.query, "分析员")
        self.app.review("venue-draft", draft["digest"], "审核员")
        release = self.app.publish("venue-draft", "venue-release")
        corrected = self.service("s-1", "cool-river", "river-library", "2026-07-03",
                                 "senior", 8, "09:00", "10:00", "completed")
        corrected["data"]["revision"] = 2
        self.change(corrected, name="post-release-correction")
        old = self.app.publication.release("venue-release")
        self.assertEqual(old["digest"], release["digest"])
        self.assertFalse(old["current_evidence"])
        frozen = old["content"]["document"]["analysis"]["total"]["known_protected_person_days"]
        self.assertEqual(frozen, "38.333333")
        self.assertEqual(self.app.analyze(self.query)["total"]["known_protected_person_days"], "37.666667")

    def test_diagnose_and_equity_reflect_delivered_person_hours(self):
        self.ingest_venue()
        diagnostics = self.app.diagnose(self.query)
        river = next(z for z in diagnostics["zones"] if z["zone"] == "river")
        usage = next(a for a in river["actions"] if a["action"] == "cool-river")
        self.assertEqual(usage["capacity_days"], 60)
        self.assertEqual(usage["served_days"], "38.333333")
        equity = self.app.equity(self.query)
        senior = next(g for g in equity["groups"] if g["zone"] == "river" and g["group"] == "senior")
        self.assertEqual(senior["known_protected_days"], "26.666667")

    def test_csv_roundtrip_covers_venue_kinds(self):
        self.ingest_venue()
        snapshot = self.app.ledger.snapshot()
        for kind in ("window", "service", "closure", "arrangement", "action"):
            rows = list(snapshot[kind].values())
            rendered = encode_csv(rows, kind)
            decoded = decode_csv("﻿" + rendered, kind)
            self.assertEqual(len(decoded), len(rows))
            rerendered = encode_csv([item["data"] for item in decoded], kind)
            self.assertEqual(rendered, rerendered)
        closure_rows = list(snapshot["closure"].values())
        decoded = decode_csv(encode_csv(closure_rows, "closure"), "closure")
        self.assertIsNone(decoded[0]["data"]["to"])

    def test_portfolio_excludes_infeasible_windowed_candidate(self):
        self.change(self.window("w-hub-1", "river-hub", "2026-07-03", "10:00", "12:00", 15),
                    self.action("hub-river", revision=2,
                                claims=[{"window": "w-hub-1", "start": "10:00", "end": "12:00", "share": 20}]),
                    name="oversized-claim")
        spec = {"query": self.query, "budget_cents": 10000,
                "proposals": [{"action": "hub-river", "cost_cents": 1000}]}
        result = self.app.plan(spec)
        self.assertEqual(result["selected"], [])
        self.assertEqual(result["excluded"], [{"action": "hub-river", "reason": "facility_committed"}])

    def test_history_and_reconcile_track_venue_facts(self):
        self.ingest_venue()
        result = self.app.reconcile(self.query, 1, 2)
        kinds = {change["kind"] for change in result["fact_changes"]}
        self.assertEqual(kinds, {"window", "service", "closure", "arrangement", "action"})
        closed = next(row for row in result["affected_zone_days"] if row["day"] == "2026-07-03"
                      and row["zone"] == "river")
        self.assertEqual(closed["changes"]["protected"]["after"], "8.333333")
        self.assertTrue(closed["changes"]["interrupted"]["after"])
        history = self.app.history("closure", "c-lib-1")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["source"], self.bundle["source"])
