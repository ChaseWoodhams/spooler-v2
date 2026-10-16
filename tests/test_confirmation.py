"""Confirmation slices of the top plans (auto-plating ticket 17), driven with a fake slicer: no OrcaSlicer runs here."""
import threading
import time
import unittest

import confirmation
import planner

BED = (0.0, 0.0, 250.0, 250.0)
TERMS = {"makespan_min": 0, "support_g": 0, "plates": 0, "machine_min": 0, "unavoidable_tippy": 0, "split_pairs_apart": 0}


def rect(w, d):
    return ((-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2))


def cand(*, support_g=0.0, needs_support=False, rotation=(0, 0, 0)):
    return planner.Candidate(tuple(rotation), rect(50, 50), 6.0, 20, 10.0, 25.0, support_g, support_g / 0.7, needs_support, False)


def record(n):
    objects = [{"id": k, "model": k, "name": f"Object {k}", "scale": [1, 1, 1], "rotation": [0, 0, 0], "position": [0, 0], "plate": 1}
               for k in range(1, n + 1)]
    return {"id": 1, "data": {"objects": objects, "plates": [{"id": 1, "name": "Plate 1"}]}, "models": []}


def problem(n=4, printers=None):
    items = tuple(planner.Item(k, k, "g", (cand(), cand(support_g=2.0, needs_support=True, rotation=(90, 0, 0))), (0, 1))
                  for k in range(1, n + 1))
    return planner.Problem(items, {"g": planner.Group(0.7, 0.2, lambda *a: 600)}, printers or {"A": 0.7, "B": 0.7}, BED)


def plate(keys, *, candidate=0, seconds=600, support_g=0.0, printer="A", x=100.0):
    items = tuple(planner.Placed(k, candidate, (0, 0, 0) if candidate == 0 else (90, 0, 0), (x + 10 * k, 100.0), 0.0) for k in keys)
    return planner.PlanPlate(None, "g", items, seconds, support_g, printer, bool(candidate))


def plan(*plates):
    terms = {**TERMS, "plates": len(plates)}
    return planner.Plan(tuple(plates), 0.0, terms, 0.0)


class FakeSlicer:
    """Answers each plate with 100 s per Object and 1 g of support per Object on a supported plate."""

    def __init__(self, seconds_per_object=100, delay=0.0, fail=()):
        self.calls, self.delay, self.fail, self.per_object = [], delay, set(fail), seconds_per_object
        self.lock = threading.Lock()

    def __call__(self, job, timeout_s):
        with self.lock:
            self.calls.append(job)
        time.sleep(min(self.delay, max(timeout_s, 0)))
        if self.fail & set(job.keys):
            raise RuntimeError("OrcaSlicer failed")
        grams = float(len(job.keys)) if job.supports else 0.0
        return confirmation.PlateMeasure(len(job.keys) * self.per_object if job.kind == "full" else None, grams)


def confirm(plans, slicer, *, n=4, **kw):
    return confirmation.confirm_plans(1, record(n), problem(n), plans, slice_plate=slicer, **kw)


class SharedPlateTests(unittest.TestCase):
    def test_a_plate_shared_by_two_plans_is_sliced_once(self):
        shared = plate([1, 2])
        a, b = plan(shared, plate([3, 4])), plan(shared, plate([3], printer="B"), plate([4], printer="B"))
        slicer = FakeSlicer()
        result = confirm([a, b], slicer)
        keys = [tuple(j.keys) for j in slicer.calls]
        self.assertEqual(sorted(keys), [(1, 2), (3,), (3, 4), (4,)])   # (1, 2) once, though both plans hold it
        self.assertEqual(result.stats["slices"], 4)
        self.assertEqual(result.stats["reused"], 1)

    def test_the_same_objects_in_another_orientation_are_another_plate(self):
        slicer = FakeSlicer()
        confirm([plan(plate([1, 2])), plan(plate([1, 2], candidate=1))], slicer)
        self.assertEqual(len(slicer.calls), 2)


class MeasuredFiguresTests(unittest.TestCase):
    def test_measured_time_and_support_replace_the_proxy_figures(self):
        result = confirm([plan(plate([1, 2], seconds=60, support_g=0.0, candidate=1))], FakeSlicer())
        best = result.plans[0]
        self.assertEqual(best.plates[0].seconds, 200)
        self.assertEqual(best.plates[0].support_g, 2.0)
        self.assertFalse(best.plates[0].partly_estimated)
        self.assertFalse(best.partly_estimated)
        self.assertAlmostEqual(best.terms["support_g"], 2.0)
        self.assertAlmostEqual(best.terms["makespan_min"], 200 / 60)

    def test_plans_are_reranked_by_the_cost_with_measured_figures(self):
        # the search believed plan "a" cheaper: its plates look short; Orca measures them long
        a = plan(plate([1, 2], seconds=60), plate([3, 4], seconds=60, printer="B"))
        b = plan(plate([1, 2, 3, 4], seconds=3000))
        a_ranked_first = planner.Plan(a.plates, 1.0, a.terms, 60)
        b_second = planner.Plan(b.plates, 2.0, b.terms, 3000)

        class Slicer(FakeSlicer):
            def __call__(self, job, timeout_s):
                got = super().__call__(job, timeout_s)
                return confirmation.PlateMeasure(5000 if len(job.keys) == 2 else 700, got.support_g)
        result = confirm([a_ranked_first, b_second], Slicer())
        self.assertEqual([c.plan for c in result.plans], [b_second, a_ranked_first])

    def test_support_grams_add_to_the_cost_so_a_supported_plan_can_lose(self):
        plain = plan(plate([1, 2], candidate=0))
        supported = plan(plate([1, 2], candidate=1))
        result = confirm([supported, plain], FakeSlicer())
        self.assertIs(result.plans[0].plan, plain)

    def test_printers_are_reassigned_with_the_measured_times(self):
        both_on_a = plan(plate([1, 2], printer="A"), plate([3, 4], printer="A"))
        result = confirm([both_on_a], FakeSlicer())
        self.assertAlmostEqual(result.plans[0].makespan_s, 200)   # one plate on each printer
        self.assertEqual({p.printer for p in result.plans[0].plates}, {"A", "B"})

    def test_plates_only_go_to_printers_with_the_nozzle(self):
        only_a = confirmation.confirm_plans(1, record(4), problem(4, {"A": 0.7, "B": 0.4}),
                                            [plan(plate([1, 2], printer="A"), plate([3, 4], printer="A"))], slice_plate=FakeSlicer())
        self.assertEqual({p.printer for p in only_a.plans[0].plates}, {"A"})
        self.assertAlmostEqual(only_a.plans[0].makespan_s, 400)


class FailureTests(unittest.TestCase):
    def test_a_failed_slice_leaves_the_plate_on_its_proxy_figures_and_the_run_completes(self):
        result = confirm([plan(plate([1, 2], seconds=777), plate([3, 4], seconds=600))], FakeSlicer(fail=[3]))
        by_keys = {tuple(i.key for i in p.plate.items): p for p in result.plans[0].plates}
        failed, fine = by_keys[(3, 4)], by_keys[(1, 2)]
        self.assertEqual(failed.seconds, 600)
        self.assertTrue(failed.partly_estimated)
        self.assertIn("OrcaSlicer failed", failed.error)
        self.assertFalse(fine.partly_estimated)
        self.assertEqual(fine.seconds, 200)
        self.assertTrue(result.plans[0].partly_estimated)
        self.assertEqual(result.stats["failed"], 1)

    def test_every_slice_failing_still_returns_every_plan_marked_partly_estimated(self):
        a, b = planner.Plan((plate([1, 2]),), 1.0, TERMS, 600), planner.Plan((plate([3, 4]),), 2.0, TERMS, 600)
        result = confirm([b, a], FakeSlicer(fail=[1, 2, 3, 4]))
        self.assertCountEqual([id(c.plan) for c in result.plans], [id(a), id(b)])
        self.assertTrue(all(c.partly_estimated for c in result.plans))


class BudgetTests(unittest.TestCase):
    def test_slicing_stops_at_the_budget_and_the_rest_is_partly_estimated(self):
        plates = [plate([k], printer="A" if k % 2 else "B", x=10 * k) for k in range(1, 9)]
        slicer = FakeSlicer(delay=0.15)
        started = time.monotonic()
        result = confirm([plan(*plates)], slicer, n=8, budget_s=0.4, workers=1)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertLess(len(slicer.calls), 8)
        self.assertGreaterEqual(len(slicer.calls), 1)
        self.assertTrue(result.stats["budget_exhausted"])
        estimated = [p for p in result.plans[0].plates if p.partly_estimated]
        self.assertEqual(len(estimated), 8 - result.stats["slices"] + result.stats["failed"])
        self.assertTrue(result.plans[0].partly_estimated)

    def test_the_first_plan_is_sliced_before_the_others(self):
        first, second = plan(plate([1])), plan(plate([2]))
        slicer = FakeSlicer()
        confirm([first, second], slicer, workers=1)
        self.assertEqual([tuple(j.keys) for j in slicer.calls], [(1,), (2,)])


class ReducedSliceTests(unittest.TestCase):
    def test_one_heavy_plate_gets_a_reduced_slice_and_keeps_its_proxy_time(self):
        heavy, light = plate([1], seconds=40 * 3600, candidate=1), plate([2], seconds=600)
        slicer = FakeSlicer()
        result = confirm([plan(heavy, light)], slicer, budget_s=25)
        kinds = {tuple(j.keys): j.kind for j in slicer.calls}
        self.assertEqual(kinds, {(1,): "reduced", (2,): "full"})
        by_keys = {tuple(i.key for i in p.plate.items): p for p in result.plans[0].plates}
        self.assertEqual(by_keys[(1,)].seconds, 40 * 3600)     # reduced slices give no time
        self.assertEqual(by_keys[(1,)].support_g, 1.0)         # but measured support
        self.assertTrue(by_keys[(1,)].partly_estimated)
        self.assertFalse(by_keys[(2,)].partly_estimated)

    def test_a_plain_heavy_plate_is_sliced_in_full_since_reduced_would_add_nothing(self):
        slicer = FakeSlicer()
        confirm([plan(plate([1], seconds=40 * 3600))], slicer)
        self.assertEqual([j.kind for j in slicer.calls], ["full"])


if __name__ == "__main__":
    unittest.main()
