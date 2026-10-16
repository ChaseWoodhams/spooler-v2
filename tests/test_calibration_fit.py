"""The time and support fits made from the calibration log, on a temporary database with synthetic rows."""
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import calibration
import database
import health
import jobs
import time_estimate

TRUE = {"h1": (30.0, 70.0, 10.0, 5.0), "h2": (36.0, 60.0, 8.0, 6.0)}   # seconds per layer, cm3, support cm3, first-layer cm2


class FitTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        database.configure(Path(self._tmp.name) / "spooler.db")
        database.migrate()
        self.rng = random.Random(7)
        self.clock = 1000.0

    def plate(self, settings_hash="h1", nozzle=0.4, truth=None, kind="full", seconds=None, bundle_id="b1"):
        coef = truth or TRUE[settings_hash]
        layers, volume, support, first = self.rng.randint(20, 400), self.rng.uniform(5, 150), self.rng.uniform(0, 40), self.rng.uniform(10, 120)
        estimate = seconds if seconds is not None else round(coef[0] * layers + coef[1] * volume + coef[2] * support + coef[3] * first)
        self.clock += 1
        with database.transaction() as conn:
            conn.execute("INSERT INTO slice_log (layers, volume_cm3, first_layer_cm2, support_volume_cm3, estimate_s, settings_hash, nozzle, "
                         "kind, bundle_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (layers, volume, first, support, estimate, settings_hash, nozzle, kind, bundle_id, self.clock))
        return layers, volume, support, first

    def plates(self, count, **kwargs):
        for _ in range(count):
            self.plate(**kwargs)

    def predict(self, hash_="h1", nozzle=0.4):
        return calibration.fit_models().predict_seconds(200, 60.0, 50.0, 20.0, settings_hash=hash_, nozzle=nozzle)

    def exact(self, hash_="h1"):
        a, b, c, d = TRUE[hash_]
        return round(a * 200 + b * 60.0 + c * 20.0 + d * 50.0)

    def seed(self):
        a, b, d = time_estimate.COEFFICIENTS
        return round(a * 200 + b * 60.0 + d * 50.0)


class TimeFitTests(FitTestCase):
    def test_with_19_plates_the_seed_is_used_and_with_20_the_bundle_fit(self):
        self.plates(19)
        self.assertEqual(self.predict(), self.seed())
        self.plate()
        self.assertEqual(self.predict(), self.exact())

    def test_with_19_plates_for_a_bundle_the_nozzle_fit_is_used(self):
        self.plates(19, settings_hash="h1")
        self.plates(10, settings_hash="h2")
        nozzle_fit = self.predict()
        self.assertNotIn(nozzle_fit, (self.seed(), self.exact()))
        self.plate(settings_hash="h1")
        self.assertEqual(self.predict(), self.exact())

    def test_a_fit_that_does_not_beat_the_fallback_is_not_used(self):
        a, b, d = time_estimate.COEFFICIENTS
        for _ in range(25):                   # the slicer's times are exactly the seeds' figures: a fit explains them no better
            layers, volume, support, first = self.plate(seconds=1)
            with database.transaction() as conn:
                conn.execute("UPDATE slice_log SET estimate_s = ? WHERE id = (SELECT max(id) FROM slice_log)", (round(a * layers + b * volume + d * first),))
        self.assertEqual(self.predict(), self.seed())

    def test_no_fitted_coefficient_is_negative(self):
        self.plates(30, truth=(30.0, 70.0, -10.0, 5.0))      # a truth with a negative support term
        coefficients = calibration.fit_models().time_model("h1", 0.4).coefficients
        self.assertEqual(len(coefficients), 4)
        self.assertTrue(all(c >= 0 for c in coefficients))

    def test_one_wildly_wrong_row_does_not_move_the_fit(self):
        self.plates(24)
        self.plate(seconds=900000)
        self.assertEqual(self.predict(), self.exact())

    def test_reduced_slices_do_not_train_time(self):
        self.plates(25, kind="reduced")
        self.assertEqual(self.predict(), self.seed())

    def test_only_the_latest_150_rows_of_a_key_are_fitted(self):
        self.plates(30, truth=(500.0, 500.0, 0.0, 0.0))
        self.plates(150)
        self.assertEqual(self.predict(), self.exact())

    def test_predict_seconds_uses_the_fit_when_given_models(self):
        self.plates(20)
        models = calibration.fit_models()
        self.assertEqual(time_estimate.predict_seconds(200, 60.0, 50.0, models=models, support_volume_cm3=20.0, settings_hash="h1", nozzle=0.4), self.exact())
        self.assertEqual(time_estimate.predict_seconds(200, 60.0, 50.0), self.seed())

    def test_the_same_log_gives_the_same_fit(self):
        self.plates(22)
        self.plate(seconds=777777)
        first, second = calibration.fit_models(), calibration.fit_models()
        self.assertEqual(first.time_model("h1", 0.4), second.time_model("h1", 0.4))


class BackfilledRowTests(FitTestCase):
    """Rows logged before the calibration migration carry at most a backfilled bundle id: no settings hash, no nozzle."""

    def machine(self, nozzle):
        return mock.patch("slicer.bundle_settings", side_effect=lambda bundle: {"machine": {"nozzle_diameter": [nozzle]}})

    def test_a_row_with_a_backfilled_bundle_gets_that_bundles_nozzle_and_trains_the_nozzle_fit(self):
        self.plates(20, truth=TRUE["h1"], settings_hash=None, nozzle=None, bundle_id="old")
        with self.machine("0.4"):
            self.assertEqual(self.predict("unseen", 0.4), self.exact())
        with database.transaction() as conn:
            self.assertEqual({r[0] for r in conn.execute("SELECT nozzle FROM slice_log")}, {0.4})

    def test_a_row_without_a_bundle_is_never_fitted(self):
        self.plates(25, truth=TRUE["h1"], settings_hash=None, nozzle=None, bundle_id=None)
        with self.machine("0.4"):
            self.assertEqual(self.predict("unseen", 0.4), self.seed())

    def test_a_row_whose_bundle_is_gone_is_never_fitted(self):
        self.plates(25, truth=TRUE["h1"], settings_hash=None, nozzle=None, bundle_id="gone")
        with mock.patch("slicer.bundle_settings", side_effect=jobs.JobError("Profile not found", 404)):
            self.assertEqual(self.predict("unseen", 0.4), self.seed())
        with database.transaction() as conn:
            self.assertEqual({r[0] for r in conn.execute("SELECT nozzle FROM slice_log")}, {None})


class SupportFitTests(FitTestCase):
    def support_object(self, grams_per_cm3, hash_="h1", nozzle=0.4, volume=None, grams=None):
        volume = volume if volume is not None else self.rng.uniform(1, 30)
        with database.transaction() as conn:
            slice_id = conn.execute("INSERT INTO slice_log (nozzle, settings_hash, kind, created_at) VALUES (?, ?, 'full', ?)",
                                    (nozzle, hash_, self.clock)).lastrowid
            conn.execute("INSERT INTO support_log (slice_log_id, object_name, bundle_hash, support_volume_cm3, support_g, created_at) "
                         "VALUES (?, 'o', ?, ?, ?, ?)", (slice_id, hash_, volume, grams if grams is not None else volume * grams_per_cm3, self.clock))

    def factor(self, hash_="h1", nozzle=0.4):
        return calibration.fit_models().support_g_per_cm3(hash_, nozzle)

    def test_the_seed_is_density_times_0_6_until_10_supported_objects(self):
        for _ in range(9):
            self.support_object(0.5)
        self.assertAlmostEqual(self.factor(), 1.24 * 0.6)
        self.support_object(0.5)
        self.assertAlmostEqual(self.factor(), 0.5)

    def test_a_bundle_short_of_10_objects_uses_its_nozzles_factor(self):
        for _ in range(6):
            self.support_object(0.5, hash_="h1")
        for _ in range(6):
            self.support_object(0.5, hash_="h2")
        self.assertAlmostEqual(self.factor("h1"), 0.5)       # 12 objects at this nozzle
        self.assertAlmostEqual(self.factor("h3", nozzle=0.7), 1.24 * 0.6)

    def test_one_wrong_object_does_not_move_the_factor(self):
        for _ in range(12):
            self.support_object(0.5)
        self.support_object(0, volume=10, grams=400)
        self.assertAlmostEqual(self.factor(), 0.5)


class StatusTests(FitTestCase):
    def test_each_bundle_gets_one_line_with_its_slices_and_typical_error(self):
        with database.transaction() as conn:
            conn.execute("INSERT INTO slices (state, source_name, source_file, bundle_id, bundle_label, bed_type, created_at) "
                         "VALUES ('done', 'x', 'x', 'b1', '0.7 PETG', 'textured', 1)")
        self.plates(25)
        (line,) = calibration.fit_models().status_lines()
        self.assertEqual(line, "Time model, 0.7 PETG: 25 slices, +/-0%")

    def test_a_bundle_without_a_fit_says_so(self):
        self.plates(5)
        (line,) = calibration.fit_models().status_lines()
        self.assertEqual(line, "Time model, b1: 5 slices, not fitted yet")

    def test_the_health_snapshot_lists_the_lines(self):
        self.plates(25)
        self.assertEqual(health.snapshot()["time_models"], ["Time model, b1: 25 slices, +/-0%"])
