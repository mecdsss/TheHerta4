# -*- coding: utf-8 -*-
"""Background worker for point-cloud fitting."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import numpy as np


@dataclass
class PCWorkerResult:
    step: int
    metric_f1: float
    metric_chamfer: float
    best_step: int
    best_f1: float
    basis_map: Optional[Dict[str, np.ndarray]] = None
    changed_bases: Dict[str, np.ndarray] = field(default_factory=dict)
    latest_metric_exact: bool = False
    bone_name: str = ""
    tf_type: str = ""
    accepted: bool = False
    axis: Optional[int] = None
    delta_components: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    linked_count: int = 0
    f1_delta: float = 0.0
    chamfer_delta: float = 0.0
    reward: float = 0.0
    score_delta: float = 0.0


class PCWorkerController:
    """Runs a bpy-free fitting session on a background thread."""

    def __init__(self, session, max_queue: int = 8, steps_per_chunk: int = 8) -> None:
        self.session = session
        self._queue: queue.Queue[PCWorkerResult] = queue.Queue(maxsize=max_queue)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._pause.set()
        self._pause_ack = threading.Event()
        self._pause_ack.set()
        self._lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending_exact_metric: Optional[Tuple[float, float, Optional[float]]] = None
        self._error: Optional[str] = None
        self._steps_per_chunk = max(1, int(steps_per_chunk))

    @property
    def error(self) -> Optional[str]:
        return self._error

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self.resume()
            return
        self._stop.clear()
        self._pause.clear()
        self._pause_ack.clear()
        self._thread = threading.Thread(
            target=self._run, name="PCWorker", daemon=True)
        self._thread.start()

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()
        self._pause_ack.clear()

    def stop(self) -> bool:
        self._stop.set()
        self._pause.clear()
        if self._thread is None:
            self._drain_all()
            return True
        self._thread.join(timeout=1.0)
        if self._thread.is_alive():
            self._error = (
                self._error
                or "worker stop timeout; background thread is still alive"
            )
            return False
        self._thread = None
        self._drain_all()
        return True

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def pause_and_wait(self, timeout: float = 1.0) -> bool:
        self._pause.set()
        if self._thread is None or not self._thread.is_alive():
            return True
        if not self._pause_ack.wait(timeout=max(0.0, float(timeout))):
            self._error = (
                self._error
                or "worker pause timeout; background thread is still running"
            )
            return False
        acquired = self._lock.acquire(timeout=max(0.0, float(timeout)))
        if not acquired:
            self._error = (
                self._error
                or "worker pause timeout; background thread is still holding the session lock"
            )
            return False
        self._lock.release()
        return True

    def snapshot(self, display_step: Optional[int] = None) -> PCWorkerResult:
        last = getattr(self.session, 'last_step_result', None)
        return PCWorkerResult(
            step=self.session.step_count if display_step is None else int(display_step),
            metric_f1=float(self.session.current_metric.f1),
            metric_chamfer=float(self.session.current_metric.chamfer),
            best_step=self.session.best_step,
            best_f1=float(max(0.0, self.session.best_f1)),
            basis_map=self.session._snapshot_state(),
            changed_bases={},
            latest_metric_exact=False,
            bone_name=last.bone_name if last is not None else "",
            tf_type=last.tf_type if last is not None else "",
            accepted=last.accepted if last is not None else False,
            axis=last.axis if last is not None else None,
            delta_components=(last.delta_components if last is not None
                              else (0.0, 0.0, 0.0)),
            linked_count=last.linked_count if last is not None else 0,
            f1_delta=last.f1_delta if last is not None else 0.0,
            chamfer_delta=last.chamfer_delta if last is not None else 0.0,
            reward=last.reward if last is not None else 0.0,
            score_delta=last.score_delta if last is not None else 0.0,
        )

    def seek(self, step: int) -> PCWorkerResult:
        with self._lock:
            self.session.seek(step)
            snap = self.snapshot(display_step=step)
        self._drain_all()
        return snap

    def truncate_after(self, step: int) -> PCWorkerResult:
        with self._lock:
            self.session.seek(step)
            self.session.truncate_after(step)
            snap = self.snapshot()
        self._drain_all()
        return snap

    def latest_result(self) -> Optional[PCWorkerResult]:
        items = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not items:
            return None
        if len(items) == 1:
            return items[0]
        latest = items[-1]
        merged_bases: Dict[str, np.ndarray] = {}
        latest_exact = False
        for item in items:
            merged_bases.update({
                name: np.asarray(basis, dtype=np.float64).copy()
                for name, basis in item.changed_bases.items()
            })
            latest_exact = latest_exact or bool(item.latest_metric_exact)
        latest.changed_bases = merged_bases
        latest.latest_metric_exact = bool(latest.latest_metric_exact or latest_exact)
        return latest

    def publish_exact_metric(self, f1: float, chamfer: float,
                             score: Optional[float] = None) -> None:
        with self._pending_lock:
            self._pending_exact_metric = (
                float(f1),
                float(chamfer),
                None if score is None else float(score),
            )

    def _apply_pending_exact_metric(self) -> None:
        with self._pending_lock:
            pending = self._pending_exact_metric
            self._pending_exact_metric = None
        if pending is None:
            return
        f1, chamfer, score = pending
        self.session.current_metric.f1 = float(f1)
        self.session.current_metric.chamfer = float(chamfer)
        if score is not None and hasattr(self.session.current_metric, 'score'):
            self.session.current_metric.score = float(score)
        if self.session.current_metric.f1 > self.session.best_f1 + 1e-9:
            self.session.best_f1 = self.session.current_metric.f1
            self.session.best_step = self.session.step_count
            self.session.best_snapshot = self.session._snapshot_state()
        if self.session.metrics:
            self.session.metrics[-1] = self.session.current_metric.f1

    def _drain_all(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _push_latest(self, item: PCWorkerResult) -> None:
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except queue.Full:
                try:
                    dropped = self._queue.get_nowait()
                    merged = {
                        name: np.asarray(basis, dtype=np.float64).copy()
                        for name, basis in dropped.changed_bases.items()
                    }
                    merged.update(item.changed_bases)
                    item.changed_bases = merged
                    item.latest_metric_exact = bool(
                        item.latest_metric_exact or dropped.latest_metric_exact)
                    if float(dropped.best_f1) > float(item.best_f1):
                        item.best_f1 = float(dropped.best_f1)
                        item.best_step = int(dropped.best_step)
                except queue.Empty:
                    pass

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if self._pause.is_set():
                    self._pause_ack.set()
                    time.sleep(0.005)
                    continue
                self._pause_ack.clear()
                with self._lock:
                    if self._pause.is_set():
                        self._pause_ack.set()
                        continue
                    self._apply_pending_exact_metric()
                    result = None
                    chunk_changed_bases: Dict[str, np.ndarray] = {}
                    for _ in range(self._steps_per_chunk):
                        step_result = self.session.step()
                        if step_result is None:
                            self._pause.set()
                            break
                        result = step_result
                        for name in getattr(step_result, 'applied_names', ()):
                            if name in self.session.basis_map:
                                chunk_changed_bases[name] = (
                                    self.session.basis_map[name].copy())
                    if result is None:
                        continue
                    item = PCWorkerResult(
                        step=self.session.step_count,
                        metric_f1=float(self.session.current_metric.f1),
                        metric_chamfer=float(self.session.current_metric.chamfer),
                        best_step=self.session.best_step,
                        best_f1=float(max(0.0, self.session.best_f1)),
                        basis_map=None,
                        changed_bases=chunk_changed_bases,
                        latest_metric_exact=False,
                        bone_name=result.bone_name,
                        tf_type=result.tf_type,
                        accepted=result.accepted,
                        axis=result.axis,
                        delta_components=result.delta_components,
                        linked_count=result.linked_count,
                        f1_delta=result.f1_delta,
                        chamfer_delta=result.chamfer_delta,
                        reward=result.reward,
                        score_delta=result.score_delta,
                    )
                self._push_latest(item)
        except Exception as exc:
            self._error = str(exc)
            self._pause.set()
