"""
Time and support fits made on demand from the calibration log (`slice_log`, `support_log`). No fit is stored: the planner
calls `fit_models()` at the start of a run and holds the result for that run, so the same log always gives the same fit.
(The one write is `backfill_nozzles`, which fills in the nozzle of rows logged before the calibration migration.)

Time: non-negative least squares on  a*layers + b*volume + c*support volume + d*first-layer area,  per settings hash, from
full slices only. Support: grams per proxy cm3, per settings hash. Each falls back from the settings hash to the nozzle
diameter to the seeds (`time_estimate.COEFFICIENTS`; grams = proxy cm3 x density x 0.6).

A level is used only with >= MIN_PLATES plates (time) or >= MIN_OBJECTS supported Objects (support), and a time fit only if its
leave-one-out error beats the fallback's on the same rows. The latest MAX_ROWS rows per key are fitted, after dropping
rows whose residual is above OUTLIER_X times the median absolute residual.
"""

from dataclasses import dataclass

import numpy as np
from scipy.optimize import nnls

import database
import support_measure
import time_estimate

MIN_PLATES, MIN_OBJECTS, MAX_ROWS, OUTLIER_X = 20, 10, 150, 3.0
SEED_SUPPORT_FACTOR = support_measure.DEFAULT_DENSITY * 0.6
MARGIN = 0.001   # the fit's typical error must be this much (as a fraction) below the fallback's, so noise is not an improvement


@dataclass(frozen=True)
class TimeModel:
    coefficients: tuple   # seconds per layer, per cm3, per support cm3, per first-layer cm2
    source: str           # "bundle", "nozzle" or "seed"
    slices: int = 0       # rows the fit used
    error: float | None = None   # typical (median) leave-one-out error, as a fraction of the slicer's time


def _predict(coefficients, rows):
    return rows @ np.array(coefficients)


def _typical_error(coefficients, rows, seconds) -> float:
    return float(np.median(np.abs(_predict(coefficients, rows) - seconds) / seconds))


def _fit(rows, seconds):
    return tuple(float(c) for c in nnls(rows, seconds)[0])


def _inliers(rows, seconds):
    """Rows and seconds without those whose residual under a first fit is above OUTLIER_X x the median absolute residual."""
    residual = np.abs(_predict(_fit(rows, seconds), rows) - seconds)
    limit = max(OUTLIER_X * float(np.median(residual)), 1e-6 * float(np.mean(np.abs(seconds))))
    keep = residual <= limit
    return rows[keep], seconds[keep]


def _leave_one_out(rows, seconds) -> float:
    errors = []
    for i in range(len(seconds)):
        mask = np.arange(len(seconds)) != i
        errors.append(abs(_predict(_fit(rows[mask], seconds[mask]), rows[i:i + 1])[0] - seconds[i]) / seconds[i])
    return float(np.median(errors))


class Models:
    def __init__(self, plates: list, objects: list, labels: dict):
        self._plates, self._objects, self._labels = plates, objects, labels
        self._time_cache: dict = {}

    # time

    @staticmethod
    def _arrays(selected):
        rows = np.array([[p["layers"], p["volume_cm3"], p["support_volume_cm3"] or 0.0, p["first_layer_cm2"]] for p in selected], float)
        return rows, np.array([p["estimate_s"] for p in selected], float)

    @staticmethod
    def _seed_model() -> TimeModel:
        layers, volume, first = time_estimate.COEFFICIENTS
        return TimeModel((layers, volume, 0.0, first), "seed")

    def _level(self, source: str, key, fallback: TimeModel) -> TimeModel:
        field = "settings_hash" if source == "bundle" else "nozzle"
        selected = sorted((p for p in self._plates if key is not None and p[field] == key), key=lambda p: p["id"])[-MAX_ROWS:]
        if len(selected) < MIN_PLATES:
            return fallback
        rows, seconds = self._arrays(selected)
        rows, seconds = _inliers(rows, seconds)
        if len(seconds) < 4:
            return fallback
        error = _leave_one_out(rows, seconds)
        if error > _typical_error(fallback.coefficients, rows, seconds) - MARGIN:
            return fallback
        return TimeModel(_fit(rows, seconds), source, len(seconds), error)

    def time_model(self, settings_hash=None, nozzle=None) -> TimeModel:
        """The model for this bundle: its own fit, else its nozzle's, else the seeds."""
        key = (settings_hash, nozzle)
        if key not in self._time_cache:
            by_nozzle = self._level("nozzle", nozzle, self._seed_model())
            self._time_cache[key] = self._level("bundle", settings_hash, by_nozzle)
        return self._time_cache[key]

    def predict_seconds(self, layers, volume_cm3, first_layer_cm2, support_volume_cm3=0.0, *, settings_hash=None, nozzle=None) -> int:
        a, b, c, d = self.time_model(settings_hash, nozzle).coefficients
        return round(a * layers + b * volume_cm3 + c * support_volume_cm3 + d * first_layer_cm2)

    # support

    def _factor(self, field, key):
        selected = sorted((o for o in self._objects if key is not None and o[field] == key), key=lambda o: o["id"])[-MAX_ROWS:]
        if len(selected) < MIN_OBJECTS:
            return None
        volume = np.array([o["support_volume_cm3"] for o in selected], float)
        grams = np.array([o["support_g"] for o in selected], float)
        factor = grams.sum() / volume.sum()
        residual = np.abs(grams - factor * volume)
        keep = residual <= max(OUTLIER_X * float(np.median(residual)), 1e-6 * float(grams.mean()))
        return float(grams[keep].sum() / volume[keep].sum())

    def support_g_per_cm3(self, settings_hash=None, nozzle=None) -> float:
        for field, key in (("bundle_hash", settings_hash), ("nozzle", nozzle)):
            factor = self._factor(field, key)
            if factor is not None:
                return factor
        return SEED_SUPPORT_FACTOR

    # status

    def status_lines(self) -> list:
        """One read-only line per bundle: the slices its time fit used and the typical error."""
        lines = []
        for settings_hash in sorted({p["settings_hash"] for p in self._plates if p["settings_hash"]}):
            own = [p for p in self._plates if p["settings_hash"] == settings_hash]
            newest = max(own, key=lambda p: p["id"])
            model = self.time_model(settings_hash, newest["nozzle"])
            label = self._labels.get(newest["bundle_id"]) or newest["bundle_id"] or settings_hash[:8]
            detail = (f"{model.slices} slices, +/-{round(model.error * 100)}%" if model.source != "seed"
                      else f"{len(own)} slices, not fitted yet")
            lines.append(f"Time model, {label}: {detail}")
        return lines


def _bundle_nozzle(bundle_id: str) -> float | None:
    """The nozzle diameter of a bundle's machine profile, or None when the bundle is gone or names none."""
    import jobs      # function-local: slicer (and jobs with it) is heavy and not needed for fits alone
    import slicer
    try:
        value = slicer.bundle_settings(bundle_id)["machine"].get("nozzle_diameter")
        return float(value[0] if isinstance(value, (list, tuple)) else str(value).split(",")[0])
    except (jobs.JobError, OSError, KeyError, TypeError, ValueError, IndexError):
        return None


def backfill_nozzles() -> None:
    """Give a nozzle to the rows logged before the calibration migration that got a bundle id from it but have neither a
    settings hash nor a nozzle, from that bundle's machine profile. Without one of the two a row joins no key and is never
    fitted. The settings hash is not backfilled: the bundle's content when the slice was made is not known (an in-place
    edit keeps the id), while its nozzle is part of the machine it was locked for. Rows whose bundle is gone stay as they are."""
    with database.transaction() as conn:
        bundles = [r[0] for r in conn.execute("SELECT DISTINCT bundle_id FROM slice_log WHERE bundle_id IS NOT NULL "
                                              "AND settings_hash IS NULL AND nozzle IS NULL ORDER BY bundle_id")]
    found = {b: n for b in bundles if (n := _bundle_nozzle(b)) is not None}
    if found:
        with database.transaction() as conn:
            conn.executemany("UPDATE slice_log SET nozzle = ? WHERE bundle_id = ? AND settings_hash IS NULL AND nozzle IS NULL",
                             [(n, b) for b, n in found.items()])


def fit_models() -> Models:
    """Read the log and return the fits. Full slices with a slicer time train time; supported Objects train support. Old rows
    get their nozzle first (`backfill_nozzles`); a row with neither a settings hash nor a nozzle is never fitted."""
    backfill_nozzles()
    with database.transaction() as conn:
        plates = [dict(r) for r in conn.execute(
            "SELECT id, bundle_id, settings_hash, nozzle, layers, volume_cm3, first_layer_cm2, support_volume_cm3, estimate_s FROM slice_log "
            "WHERE kind = 'full' AND estimate_s > 0 AND layers IS NOT NULL AND volume_cm3 IS NOT NULL AND first_layer_cm2 IS NOT NULL "
            "AND (settings_hash IS NOT NULL OR nozzle IS NOT NULL) ORDER BY id")]
        objects = [dict(r) for r in conn.execute(
            "SELECT o.id, o.bundle_hash, s.nozzle, o.support_volume_cm3, o.support_g FROM support_log o JOIN slice_log s ON s.id = o.slice_log_id "
            "WHERE o.support_g > 0 AND o.support_volume_cm3 > 0 ORDER BY o.id")]
        labels = {r["bundle_id"]: r["bundle_label"] for r in conn.execute(
            "SELECT bundle_id, bundle_label FROM slices WHERE bundle_label IS NOT NULL ORDER BY id")}
    return Models(plates, objects, labels)


def signature() -> tuple:
    """Changes whenever a row is added to the log, so a caller can reuse a fit until then."""
    with database.transaction() as conn:
        return tuple(tuple(conn.execute(f"SELECT count(*), coalesce(max(id), 0) FROM {t}").fetchone())
                     for t in ("slice_log", "support_log"))
