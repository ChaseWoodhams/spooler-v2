"""Planner search core on hand-built problems: cost, printer assignment, seed and local search (auto-plating ticket 16)."""
import unittest

import planner
import splits

GEOMETRY = not splits.missing_modules()
BED = (0.0, 0.0, 250.0, 250.0)
STEADY = {"nozzle": 0.7}


def rect(w, d):
    return ((-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2))


def cand(w=100, d=100, h=20, *, volume=50.0, support_g=0.0, needs_support=False, tippy=False, rotation=(0, 0, 0)):
    return planner.Candidate(tuple(rotation), rect(w, d), 6.0, h, volume, w * d / 100, support_g, support_g / 0.7,
                             needs_support, tippy)


def item(key, *cands, model=None, group="g", near=None):
    return planner.Item(key, model if model is not None else key, group, tuple(cands),
                        tuple(near if near is not None else range(len(cands))))


def seconds_by_layers(layers, volume, first, support):   # 1 s per layer, plus volume: simple and independent of the seeds
    return layers * 60 + volume * 60


def problem(items, printers=None, *, groups=None, **kw):
    groups = groups or {"g": planner.Group(0.7, 0.2, seconds_by_layers)}
    return planner.Problem(tuple(items), groups, printers or {"A": 0.7, "B": 0.7}, BED, **kw)


class CostTests(unittest.TestCase):
    def test_weights_on_hand_computed_terms(self):
        got = planner.cost(makespan_min=100, support_g=2, plates=3, machine_min=150, unavoidable_tippy=1, split_pairs_apart=4)
        self.assertAlmostEqual(got, 100 + 12 + 90 + 15 + 120 + 2)

    def test_support_gram_costs_more_than_a_minute_of_print(self):
        base = dict(makespan_min=60, plates=1, machine_min=60, unavoidable_tippy=0, split_pairs_apart=0)
        self.assertGreater(planner.cost(support_g=1, **base), planner.cost(support_g=0, **base) + 5)


class AssignmentTests(unittest.TestCase):
    def test_enumeration_beats_lpt_on_hand_built_times(self):
        times, everywhere = [3, 3, 2, 2, 2], [(0, 1)] * 5
        exact = planner.assign_plates(times, everywhere)
        self.assertEqual(exact.makespan, 6)           # 3+3 against 2+2+2
        self.assertTrue(exact.exact)
        lpt = planner.assign_plates(times, everywhere, exact_max=4)   # above the cap: LPT
        self.assertEqual(lpt.makespan, 7)
        self.assertFalse(lpt.exact)

    def test_a_plate_goes_only_to_a_printer_it_is_eligible_for(self):
        got = planner.assign_plates([5, 4, 3], [(0,), (0, 1), (1,)])
        self.assertEqual(got.printers[0], 0)
        self.assertEqual(got.printers[2], 1)
        self.assertEqual(got.makespan, 7)             # the middle plate goes to printer 1 beside the 3

    def test_one_matching_printer_takes_everything(self):
        got = planner.assign_plates([5, 4, 3], [(1,)] * 3)
        self.assertEqual(got.printers, (1, 1, 1))
        self.assertEqual(got.makespan, 12)

    def test_no_plates(self):
        self.assertEqual(planner.assign_plates([], []).makespan, 0)


class SearchTests(unittest.TestCase):
    def test_everything_is_planned_once_and_fits_its_plate(self):
        items = [item(k, cand(100, 100, 20)) for k in range(1, 9)]
        plans = planner.plan_problem(problem(items), budget_s=5, max_steps=60)
        self.assertTrue(plans)
        placed = sorted(i.key for p in plans[0].plates for i in p.items)
        self.assertEqual(placed, list(range(1, 9)))
        self.assertLessEqual(len(plans[0].plates), 3)     # four 100 mm squares fit a 250 mm bed, so two plates would do
        for plate in plans[0].plates:
            self.assertTrue(plate.items)
        self.assertEqual(plans, sorted(plans, key=lambda p: p.cost))

    def test_search_never_ends_worse_than_the_seed_and_cuts_a_plate(self):
        # first-fit by height strands a short part on a plate of its own that moving it would not need
        items = ([item(1, cand(140, 140, 30)), item(2, cand(140, 140, 20)), item(3, cand(140, 140, 10))]
                 + [item(k, cand(60, 60, 5)) for k in range(4, 8)])
        seed = planner.seed_plan(problem(items))
        best = planner.plan_problem(problem(items), budget_s=5, max_steps=150)[0]
        self.assertLessEqual(best.cost, seed.cost)

    def test_same_problem_gives_same_plans(self):
        items = [item(k, cand(90 + 7 * k, 100, 10 + 3 * k)) for k in range(1, 8)]
        first = planner.plan_problem(problem(items), budget_s=60, max_steps=80)
        again = planner.plan_problem(problem(items), budget_s=60, max_steps=80)
        self.assertEqual([p.as_dict() for p in first], [p.as_dict() for p in again])

    def test_a_default_run_ends_on_its_step_budget_so_two_runs_give_the_same_plans(self):
        # the time cap is only a safety net: a normal run ends on MAX_STEPS, which a slower moment cannot change
        items = [item(k, cand(60 + 5 * k, 70, 10 + 2 * k)) for k in range(1, 10)]
        first, again = planner.plan_problem(problem(items)), planner.plan_problem(problem(items))
        self.assertEqual(first[0].stats["steps"], planner.MAX_STEPS)
        self.assertLess(first[0].stats["seconds"], planner.SEARCH_SECONDS)
        self.assertEqual([p.as_dict() for p in first], [p.as_dict() for p in again])

    def test_plates_never_mix_groups_and_go_to_a_printer_with_that_nozzle(self):
        groups = {"fine": planner.Group(0.4, 0.2, seconds_by_layers), "coarse": planner.Group(0.7, 0.2, seconds_by_layers)}
        items = [item(1, cand(50, 50), group="fine"), item(2, cand(50, 50), group="coarse"),
                 item(3, cand(50, 50), group="fine"), item(4, cand(50, 50), group="coarse")]
        plan = planner.plan_problem(problem(items, {"dunt": 0.7, "og": 0.4}, groups=groups), budget_s=5, max_steps=60)[0]
        self.assertEqual(len(plan.plates), 2)
        for plate in plan.plates:
            keys = {i.key for i in plate.items}
            self.assertIn(keys, ({1, 3}, {2, 4}))
            self.assertEqual(plate.printer, "og" if plate.group == "fine" else "dunt")

    def test_single_matching_printer_gets_everything(self):
        items = [item(k, cand(120, 120)) for k in range(1, 7)]
        plan = planner.plan_problem(problem(items, {"A": 0.7, "B": 0.4, "C": None}), budget_s=5, max_steps=40)[0]
        self.assertEqual({p.printer for p in plan.plates}, {"A"})

    def test_pinned_object_stays_and_others_keep_clear_of_it(self):
        pin = planner.Pinned("pin", 7, "g", ((75, 75), (175, 75), (175, 175), (75, 175)), 6.0, 20.0, 50.0, 100.0)
        items = [item(k, cand(80, 80)) for k in range(1, 5)]
        plan = planner.plan_problem(problem(items, pinned=(pin,), anchors={7: "g"}), budget_s=5, max_steps=60)[0]
        keys = [i.key for p in plan.plates for i in p.items]
        self.assertNotIn("pin", keys)
        self.assertEqual(sorted(keys), [1, 2, 3, 4])
        anchored = [p for p in plan.plates if p.anchor == 7]
        self.assertEqual(len(anchored), 1)
        for placed in anchored[0].items:   # nothing overlaps the pinned square
            half = 40
            x, y = placed.center
            self.assertTrue(x + half <= 75 or x - half >= 175 or y + half <= 75 or y - half >= 175, placed)

    def test_copies_are_never_swapped_and_near_tied_orientation_can_be_chosen(self):
        # a tall orientation costs support the flat one does not: the preferred (first) one is kept
        upright = cand(40, 40, 150, support_g=0.0)
        lying = cand(150, 40, 40, support_g=3.0, needs_support=True)
        items = [item(1, upright, lying, model="m", near=(0, 1)), item(2, upright, lying, model="m", near=(0, 1))]
        plan = planner.plan_problem(problem(items), budget_s=5, max_steps=60)[0]
        self.assertEqual(plan.terms["support_g"], 0.0)
        self.assertEqual({i.candidate for p in plan.plates for i in p.items}, {0})

    def test_unavoidable_tippy_is_counted_in_the_cost(self):
        items = [item(1, cand(50, 50, tippy=True)), item(2, cand(50, 50))]
        plan = planner.plan_problem(problem(items), budget_s=5, max_steps=20)[0]
        self.assertEqual(plan.terms["unavoidable_tippy"], 1)

    def test_neighbouring_parts_stay_together_when_it_costs_nothing(self):
        items = [item(k, cand(60, 60, 20)) for k in range(1, 5)]
        spread = problem(items, neighbours=((1, 2), (3, 4)))
        plan = planner.plan_problem(spread, budget_s=5, max_steps=100)[0]
        self.assertEqual(plan.terms["split_pairs_apart"], 0)

    def test_oversize_items_get_a_plate_each_and_are_not_searched(self):
        big = item(9, cand(400, 100, 20))
        plan = planner.plan_problem(problem([item(1, cand(50, 50))], oversize=(big,)), budget_s=2, max_steps=10)[0]
        solo = [p for p in plan.plates if any(i.oversize for i in p.items)]
        self.assertEqual(len(solo), 1)
        self.assertEqual([i.key for i in solo[0].items], [9])

    def test_budget_is_a_time_cap(self):
        items = [item(k, cand(60 + k, 60, 10 + k)) for k in range(1, 12)]
        import time
        started = time.monotonic()
        plans = planner.plan_problem(problem(items), budget_s=0.5, max_steps=10 ** 9)
        self.assertTrue(plans)
        self.assertLess(time.monotonic() - started, 10)


if __name__ == "__main__":
    unittest.main()
